import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask

import main
import web_form
from web_security import configure_session_security, install_csrf_protection


TEST_SUBSCRIPTION_ID = "123e4567-e89b-12d3-a456-426614174099"
CSRF_PATTERN = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_token(client, path="/"):
    response = client.get(path)
    match = CSRF_PATTERN.search(response.get_data(as_text=True))
    if match is None:
        raise AssertionError(f"{path} 未渲染 csrf_token")
    return match.group(1)


class SessionSecurityBoundaryTest(unittest.TestCase):
    def test_missing_secret_uses_process_local_fallback_without_logging_secret(self):
        app = Flask("csrf-secret-test")
        logs = []

        result = configure_session_security(
            app,
            environ={},
            logger=logs.append,
            secret_factory=lambda: "generated-secret-must-not-be-logged",
        )

        self.assertTrue(result["temporary_secret"])
        self.assertEqual(app.secret_key, "generated-secret-must-not-be-logged")
        self.assertTrue(any("仅限本地开发兜底" in line for line in logs))
        self.assertNotIn("generated-secret-must-not-be-logged", "\n".join(logs))
        self.assertTrue(app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(app.config["SESSION_COOKIE_SAMESITE"], "Lax")
        self.assertFalse(app.config["SESSION_COOKIE_SECURE"])

    def test_fixed_secret_and_secure_cookie_switch_are_honored(self):
        app = Flask("csrf-fixed-secret-test")

        result = configure_session_security(
            app,
            environ={
                "FLASK_SECRET_KEY": "fixed-test-secret",
                "SESSION_COOKIE_SECURE": "1",
                "CSRF_TOKEN_TTL_SECONDS": "7200",
            },
            logger=lambda _message: None,
        )

        self.assertFalse(result["temporary_secret"])
        self.assertEqual(app.secret_key, "fixed-test-secret")
        self.assertTrue(app.config["SESSION_COOKIE_SECURE"])
        self.assertEqual(app.config["CSRF_TOKEN_TTL_SECONDS"], 7200)


class GlobalCsrfContractTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmpdir.name)
        self.old_subscriptions = web_form.SUBSCRIPTIONS_PATH
        self.old_feedback = web_form.FEEDBACK_PATH
        web_form.SUBSCRIPTIONS_PATH = self.root / "subscriptions.json"
        web_form.FEEDBACK_PATH = self.root / "feedback.json"
        web_form.app.config.update(TESTING=True)
        self.client = web_form.app.test_client()

    def tearDown(self):
        web_form.SUBSCRIPTIONS_PATH = self.old_subscriptions
        web_form.FEEDBACK_PATH = self.old_feedback
        web_form.app.config.pop("CSRF_CLOCK", None)
        self.tmpdir.cleanup()

    def test_every_unsafe_route_rejects_missing_token_before_side_effects(self):
        side_effects = {
            "subscription_repository": Mock(),
            "start_background_collection": Mock(),
            "update_json": Mock(),
            "save_feedback": Mock(),
            "notify_feedback_author": Mock(),
            "acquire_collection_singleflight": Mock(),
        }
        sample_values = {
            "index": 0,
            "subscription_id": TEST_SUBSCRIPTION_ID,
        }

        with (
            patch.object(
                web_form,
                "_subscription_repository",
                side_effects["subscription_repository"],
            ),
            patch.object(
                web_form,
                "start_background_collection",
                side_effects["start_background_collection"],
            ),
            patch.object(web_form, "update_json", side_effects["update_json"]),
            patch.object(web_form, "save_feedback", side_effects["save_feedback"]),
            patch.object(
                web_form,
                "notify_feedback_author",
                side_effects["notify_feedback_author"],
            ),
            patch.object(
                main,
                "acquire_collection_singleflight",
                side_effects["acquire_collection_singleflight"],
            ),
        ):
            checked = []
            for rule in web_form.app.url_map.iter_rules():
                unsafe_methods = sorted(
                    set(rule.methods or ()) & {"POST", "PUT", "PATCH", "DELETE"}
                )
                for method in unsafe_methods:
                    values = {
                        name: sample_values.get(name, TEST_SUBSCRIPTION_ID)
                        for name in rule.arguments
                    }
                    path = web_form.app.url_map.bind("").build(rule.endpoint, values)
                    response = getattr(self.client, method.lower())(path)
                    self.assertEqual(
                        response.status_code,
                        403,
                        f"{method} {path} 未被全局CSRF拦截",
                    )
                    checked.append((method, path))

        self.assertGreaterEqual(len(checked), 6)
        for side_effect in side_effects.values():
            side_effect.assert_not_called()

    def test_wrong_and_expired_tokens_return_403_without_logging_token(self):
        now = datetime(2099, 1, 1, 10, 0, tzinfo=timezone.utc)
        web_form.app.config["CSRF_CLOCK"] = lambda: now
        valid = csrf_token(self.client)

        with patch.object(
            web_form,
            "_subscription_repository",
        ) as repository_factory, patch.object(
            web_form,
            "start_background_collection",
        ) as start:
            wrong = self.client.post(
                "/subscribe",
                data={"csrf_token": "forged-secret-token"},
            )
            web_form.app.config["CSRF_CLOCK"] = lambda: now + timedelta(hours=3)
            expired = self.client.post(
                "/subscribe",
                data={"csrf_token": valid},
            )

        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(expired.status_code, 403)
        self.assertNotIn("forged-secret-token", wrong.get_data(as_text=True))
        repository_factory.assert_not_called()
        start.assert_not_called()

    def test_non_ascii_and_oversized_tokens_are_rejected_as_403(self):
        csrf_token(self.client)

        for forged in ("1:a:￥", "1:a:" + ("x" * 4096)):
            with self.subTest(
                token_kind="unicode" if "￥" in forged else "oversized"
            ):
                response = self.client.post(
                    "/subscribe",
                    data={"csrf_token": forged},
                )

                self.assertEqual(response.status_code, 403)

    def test_get_is_unaffected_and_header_token_is_accepted(self):
        token = csrf_token(self.client)
        self.assertEqual(self.client.get("/price_hint").status_code, 200)

        response = self.client.post(
            "/defaults_preview",
            headers={"X-CSRF-Token": token},
            data={},
        )

        self.assertNotEqual(response.status_code, 403)


class CsrfLoggingContractTest(unittest.TestCase):
    def test_rejected_token_never_enters_diagnostic_log(self):
        app = Flask("csrf-log-test")
        logs = []
        configure_session_security(
            app,
            environ={"FLASK_SECRET_KEY": "fixed-test-secret"},
            logger=logs.append,
        )
        install_csrf_protection(app, logger=logs.append)

        @app.post("/write")
        def write():
            return "ok"

        rejected = app.test_client().post(
            "/write",
            headers={"X-CSRF-Token": "token-must-not-be-logged"},
        )

        self.assertEqual(rejected.status_code, 403)
        self.assertNotIn("token-must-not-be-logged", "\n".join(logs))


class UiSmokeSecurityContractTest(unittest.TestCase):
    def test_browser_smoke_covers_csrf_submission_and_server_delete_confirmation(self):
        root = Path(__file__).parent
        server = (root / "scripts" / "ui_smoke.py").read_text(encoding="utf-8")
        driver = (root / "scripts" / "ui_smoke_driver.mjs").read_text(
            encoding="utf-8"
        )

        self.assertIn('"status": "started"', server)
        self.assertIn("页1 CSRF隐藏字段=PASS", driver)
        self.assertIn("页2 CSRF隐藏字段=PASS", driver)
        self.assertIn("服务端删除确认=PASS", driver)
        self.assertIn("confirm_delete", driver)
        self.assertIn("删除GET产生副作用", driver)


class DeleteConfirmationContractTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmpdir.name) / "subscriptions.json"
        self.old_path = web_form.SUBSCRIPTIONS_PATH
        web_form.SUBSCRIPTIONS_PATH = self.path
        web_form.app.config.update(TESTING=True)
        self.client = web_form.app.test_client()
        self.path.write_text(
            '[{"subscription_id":"'
            + TEST_SUBSCRIPTION_ID
            + '","origin":"PVG","destination":"KIX"}]',
            encoding="utf-8",
        )

    def tearDown(self):
        web_form.SUBSCRIPTIONS_PATH = self.old_path
        self.tmpdir.cleanup()

    def test_delete_get_is_read_only_and_post_requires_explicit_confirmation(self):
        path = f"/subscription/{TEST_SUBSCRIPTION_ID}/delete"
        before = self.path.read_bytes()

        confirmation = self.client.get(path)
        self.assertEqual(confirmation.status_code, 200)
        self.assertIn("确认删除", confirmation.get_data(as_text=True))
        self.assertEqual(self.path.read_bytes(), before)

        token = csrf_token(self.client, path)
        missing_confirmation = self.client.post(path, data={"csrf_token": token})
        self.assertEqual(missing_confirmation.status_code, 400)
        self.assertIn("必须明确确认", missing_confirmation.get_data(as_text=True))
        self.assertEqual(self.path.read_bytes(), before)

        deleted = self.client.post(
            path,
            data={"csrf_token": token, "confirm_delete": "yes"},
        )
        self.assertEqual(deleted.status_code, 302)
        self.assertEqual(web_form.load_subscriptions(), [])


if __name__ == "__main__":
    unittest.main()
