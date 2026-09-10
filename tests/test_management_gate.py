"""Single-maintainer authentication contracts, using synthetic credentials only."""

from contextlib import ExitStack, redirect_stdout
import importlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from flask import Flask
from werkzeug.datastructures import MultiDict


TOKEN = "management-contract-only-" + "m" * 40
DETAIL_TOKEN = "detail-contract-only-" + "d" * 40
SECRET = "session-contract-only-" + "s" * 40
FIXED_EPOCH = 1800000000
MANAGEMENT_PATHS = {
    "settings": "/settings",
    "subscribe": "/subscribe",
    "subscription_list": "/subscriptions",
    "toggle_subscription": "/subscriptions/00000000-0000-0000-0000-000000000001/toggle",
    "delete_subscription": "/subscription/00000000-0000-0000-0000-000000000001/delete",
    "quick_update_subscription": "/subscriptions/00000000-0000-0000-0000-000000000001/quick-update",
    "success": "/success",
    "feedback": "/feedback",
}


class ManagementGateTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {
            "NO_LIVE_API": "1", "PYTHON_DOTENV_DISABLED": "1",
            "FLASK_SECRET_KEY": SECRET, "SHARED_DETAIL_TOKEN": DETAIL_TOKEN,
            "MANAGEMENT_TOKEN": TOKEN, "MANAGEMENT_AUTH_REQUIRED": "1",
        }, clear=True))
        self.stack.enter_context(patch("dotenv.load_dotenv", return_value=False))
        self.stack.enter_context(patch("dotenv.dotenv_values", return_value={}, create=True))
        self.denials = {}
        for name in (
            "socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto",
            "socket.create_connection", "socket.getaddrinfo", "smtplib.SMTP",
            "smtplib.SMTP_SSL", "sqlite3.connect", "requests.sessions.Session.request",
            "httpx.get", "httpx.post", "httpx.request", "httpx.Client", "httpx.AsyncClient",
        ):
            self.denials[name] = self.stack.enter_context(
                patch(name, side_effect=AssertionError(name), create=True)
            )
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.web = importlib.import_module("web_form")
        self.stack.enter_context(patch.object(self.web, "SUBSCRIPTIONS_PATH", self.root / "subscriptions.json"))
        self.stack.enter_context(patch.object(self.web, "FEEDBACK_PATH", self.root / "feedback.json"))
        self.stack.enter_context(patch.object(self.web, "PAGE_PAYLOADS_DIR", self.root / "payloads"))
        self.now = FIXED_EPOCH
        self.stack.enter_context(patch.dict(self.web.app.config, {
            "TESTING": True, "SECRET_KEY": SECRET, "SESSION_COOKIE_SECURE": False,
            "CSRF_CLOCK": lambda: self.now, "MANAGEMENT_CLOCK": lambda: self.now,
        }))
        self.stack.enter_context(patch.object(self.web, "load_calendar", return_value={}))
        self.effects = {
            name: self.stack.enter_context(patch.object(self.web, name, side_effect=AssertionError(name)))
            for name in ("_subscription_repository", "load_subscriptions",
                         "start_background_collection", "save_feedback", "notify_feedback_author")
        }
        self.client_a = self.web.app.test_client()
        self.client_b = self.web.app.test_client()
        self.addCleanup(self._assert_no_io)

    def _assert_no_io(self):
        for name, mock in {**self.denials, **self.effects}.items():
            self.assertEqual(mock.call_count, 0, name)
        self.assertEqual(list(self.root.rglob("*")), [])
        self.assertNotIn(TOKEN, self.output.getvalue())

    def _csrf(self, client):
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        return re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True)).group(1)

    def _login(self, client, token=TOKEN):
        return client.post("/unlock", data={"csrf_token": self._csrf(client), "token": token})

    def test_management_pages_require_session_before_views(self):
        views = {name: Mock(return_value="business entered") for name in MANAGEMENT_PATHS}
        with patch.dict(self.web.app.view_functions, views):
            for endpoint in ("settings", "subscription_list", "delete_subscription", "success", "feedback"):
                with self.subTest(endpoint=endpoint):
                    self.assertEqual(self.client_b.get(MANAGEMENT_PATHS[endpoint]).status_code, 404)
                    views[endpoint].assert_not_called()

    def test_valid_anonymous_csrf_does_not_authorize_writes(self):
        token = self._csrf(self.client_b)
        views = {name: Mock(return_value="business entered") for name in MANAGEMENT_PATHS}
        with patch.dict(self.web.app.view_functions, views):
            for endpoint in ("subscribe", "toggle_subscription", "delete_subscription", "quick_update_subscription", "feedback"):
                with self.subTest(endpoint=endpoint):
                    self.assertEqual(self.client_b.post(MANAGEMENT_PATHS[endpoint], data={"csrf_token": token}).status_code, 404)
                    views[endpoint].assert_not_called()

    def test_unlock_get_form_and_no_query_login(self):
        response = self.client_b.get("/unlock", query_string={"token": TOKEN})
        self.assertEqual(response.status_code, 200)
        self.assertIn('type="password"', response.get_data(as_text=True))
        self.assertIn('name="csrf_token"', response.get_data(as_text=True))
        self.assertNotIn(TOKEN, response.get_data(as_text=True))
        self.assertEqual(self.client_b.get("/settings").status_code, 404)

    def test_unlock_post_establishes_session(self):
        response = self._login(self.client_a)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/subscriptions")

    def test_required_missing_token_denies_management(self):
        os.environ.pop("MANAGEMENT_TOKEN")
        self.assertEqual(self.client_b.get("/settings").status_code, 404)

    def test_configured_token_enables_protection_without_force(self):
        os.environ.pop("MANAGEMENT_AUTH_REQUIRED")
        self.assertEqual(self.client_b.get("/settings").status_code, 404)

    def test_two_clients_authorization_is_not_shared(self):
        self.assertEqual(self._login(self.client_a).status_code, 302)
        self.assertEqual(self.client_a.get("/settings").status_code, 200)
        self.assertEqual(self.client_b.get("/settings").status_code, 404)
        self.assertEqual(self.client_b.post("/subscribe", data={
            "csrf_token": self._csrf(self.client_b),
        }).status_code, 404)

    def test_complete_endpoint_method_matrix(self):
        expected = {
            "index": {"GET", "HEAD", "OPTIONS"},
            "favicon": {"GET", "HEAD", "OPTIONS"},
            "price_hint": {"GET", "HEAD", "OPTIONS"},
            "defaults_preview": {"POST", "OPTIONS"},
            "static": {"GET", "HEAD", "OPTIONS"},
            "detail": {"GET", "HEAD", "OPTIONS"},
            "settings": {"GET", "HEAD", "OPTIONS"},
            "subscribe": {"POST", "OPTIONS"},
            "subscription_list": {"GET", "HEAD", "OPTIONS"},
            "toggle_subscription": {"POST", "OPTIONS"},
            "delete_subscription": {"GET", "HEAD", "POST", "OPTIONS"},
            "quick_update_subscription": {"POST", "OPTIONS"},
            "success": {"GET", "HEAD", "OPTIONS"},
            "feedback": {"GET", "HEAD", "POST", "OPTIONS"},
            "unlock": {"GET", "HEAD", "POST", "OPTIONS"},
            "lock": {"POST", "OPTIONS"},
        }
        rules = list(self.web.app.url_map.iter_rules())
        self.assertEqual(len(rules), len(expected))
        self.assertEqual({r.endpoint: set(r.methods) for r in rules}, expected)
        self.assertEqual(self._login(self.client_a).status_code, 302)
        csrf = {client: self._csrf(client) for client in (self.client_a, self.client_b)}
        views = {name: Mock(return_value="entered") for name in expected}
        with patch.dict(self.web.app.view_functions, views):
            for rule in rules:
                path = str(rule).replace("<subscription_id>", "00000000-0000-0000-0000-000000000001")
                path = path.replace("<path:filename>", "synthetic.css")
                for method in sorted(rule.methods):
                    for client, authorized in ((self.client_a, True), (self.client_b, False)):
                        with self.subTest(endpoint=rule.endpoint, method=method, authorized=authorized):
                            view = views[rule.endpoint]
                            view.reset_mock()
                            response = getattr(client, method.lower())(path, data={"csrf_token": csrf[client]})
                            denied = rule.endpoint in MANAGEMENT_PATHS and not authorized
                            denied |= rule.endpoint == "delete_subscription" and method == "HEAD"
                            self.assertEqual(response.status_code, 404 if denied else 200)
                            if denied or method == "OPTIONS":
                                view.assert_not_called()
                            else:
                                view.assert_called_once()

    def test_unknown_registered_endpoint_defaults_to_protected(self):
        import management_access as access
        from web_security import install_csrf_protection, issue_csrf_token

        app = Flask("synthetic-unclassified")
        app.config.update(SECRET_KEY=SECRET, TESTING=True)
        install_csrf_protection(app, logger=Mock())
        access.install_management_access(app)
        app.add_url_rule("/", "index", issue_csrf_token)
        view = Mock(return_value="unexpected business")
        app.add_url_rule("/new", "new_endpoint", lambda: view(), methods=["GET", "POST"])
        client = app.test_client()
        token = client.get("/").get_data(as_text=True)
        for method in ("GET", "HEAD", "OPTIONS", "POST"):
            with self.subTest(method=method):
                self.assertEqual(getattr(client, method.lower())("/new", data={"csrf_token": token}).status_code, 404)
        view.assert_not_called()

    def test_candidate_inputs_are_safely_rejected_without_echo(self):
        for candidate in ("wrong", "", "\u975eASCII", "x" * 513):
            with self.subTest(kind=len(candidate)):
                response = self._login(self.client_b, candidate)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                if candidate:
                    self.assertNotIn(candidate, response.get_data(as_text=True))
                self.assertEqual(self.client_b.get("/settings").status_code, 404)

    def test_only_one_form_body_token_is_accepted(self):
        csrf = self._csrf(self.client_b)
        requests = (
            {"query_string": {"token": TOKEN}, "data": {"csrf_token": csrf}},
            {"json": {"token": TOKEN}, "headers": {"X-CSRF-Token": csrf}},
            {"data": {"csrf_token": csrf}, "headers": {"Authorization": "Bearer " + TOKEN}},
            {"data": MultiDict([("csrf_token", csrf), ("token", TOKEN), ("token", TOKEN)])},
        )
        for index, kwargs in enumerate(requests):
            with self.subTest(representation=index):
                self.assertEqual(self.client_b.post("/unlock", **kwargs).status_code, 404)
        for method in ("GET", "HEAD"):
            self.assertEqual(getattr(self.client_b, method.lower())("/unlock", data={"token": TOKEN}).status_code, 200)
            self.assertEqual(self.client_b.get("/settings").status_code, 404)

    def test_csrf_precedes_management_and_login_rotates_nonce(self):
        import management_access as access

        hooks = [hook.__name__ for hook in self.web.app.before_request_funcs[None]]
        self.assertLess(hooks.index("_enforce_global_csrf"), hooks.index("_enforce_management_access"))
        with patch.object(access, "management_session_authorized") as authorization:
            self.assertEqual(self.client_b.post("/subscribe").status_code, 403)
            authorization.assert_not_called()
        before = self._csrf(self.client_a)
        with self.client_a.session_transaction() as state:
            old_nonce = state["_csrf_nonce"]
            state["anonymous_state_canary"] = "old"
        self.assertEqual(self._login(self.client_a).status_code, 302)
        with self.client_a.session_transaction() as state:
            self.assertNotEqual(old_nonce, state["_csrf_nonce"])
            self.assertNotIn("anonymous_state_canary", state)
        for data in ({}, {"csrf_token": before}):
            self.assertEqual(self.client_a.post("/subscribe", data=data).status_code, 403)
        self.assertEqual(self.client_a.get("/settings").status_code, 200)

    def test_auth_responses_are_no_store_including_csrf_rejection(self):
        for method, path, data, status in (
            ("GET", "/unlock", {}, 200), ("HEAD", "/unlock", {}, 200),
            ("OPTIONS", "/unlock", {}, 200), ("POST", "/unlock", {}, 403),
            ("POST", "/lock", {}, 403), ("OPTIONS", "/lock", {}, 200),
        ):
            with self.subTest(method=method, path=path):
                response = getattr(self.client_b, method.lower())(path, data=data)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        response = self._login(self.client_a)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn(TOKEN, str(response.headers))

    def test_absolute_ttl_does_not_slide(self):
        import management_access as access

        self.assertEqual(self._login(self.client_a).status_code, 302)
        for elapsed, status in ((0, 200), (1799, 200), (1800, 404), (1801, 404)):
            with self.subTest(elapsed=elapsed):
                self.now = FIXED_EPOCH + elapsed
                self.assertEqual(self.client_a.get("/settings").status_code, status)
                with self.client_a.session_transaction() as state:
                    self.assertEqual(state[access.SESSION_KEY]["issued_at"], FIXED_EPOCH)

    def test_future_missing_and_malformed_session_fields_are_rejected(self):
        import management_access as access

        generation = access.token_generation()
        records = (
            None, True, {}, {"generation": generation}, {"issued_at": FIXED_EPOCH},
            {"issued_at": FIXED_EPOCH + 1, "generation": generation},
            {"issued_at": str(FIXED_EPOCH), "generation": generation},
            {"issued_at": True, "generation": generation},
            {"issued_at": float("inf"), "generation": generation},
            {"issued_at": FIXED_EPOCH, "generation": "\u975e" * 64},
            {"issued_at": FIXED_EPOCH, "generation": "0" * 64},
            {"issued_at": FIXED_EPOCH, "generation": generation, "extra": True},
        )
        for index, record in enumerate(records):
            with self.subTest(record=index):
                with self.client_b.session_transaction() as state:
                    state[access.SESSION_KEY] = record
                self.assertEqual(self.client_b.get("/settings").status_code, 404)

    def test_signed_cookie_contains_no_raw_token_and_tampering_is_rejected(self):
        import management_access as access

        self.assertEqual(self._login(self.client_a).status_code, 302)
        cookie = self.client_a.get_cookie("session").value
        serializer = self.web.app.session_interface.get_signing_serializer(self.web.app)
        decoded = serializer.loads(cookie)
        self.assertNotIn(TOKEN, json.dumps(decoded))
        self.assertEqual(set(decoded[access.SESSION_KEY]), {"issued_at", "generation"})
        self.client_a.set_cookie("session", ("A" if cookie[0] != "A" else "B") + cookie[1:])
        self.assertEqual(self.client_a.get("/settings").status_code, 404)

    def test_rotation_and_invalid_configuration_reject_existing_session(self):
        self.assertEqual(self._login(self.client_a).status_code, 302)
        for token in (TOKEN + "new", "", "\u975eASCII", "x" * 513):
            with self.subTest(kind=len(token)), patch.dict(os.environ, {"MANAGEMENT_TOKEN": token}):
                self.assertEqual(self.client_a.get("/settings").status_code, 404)

    def test_post_logout_and_no_get_logout(self):
        self.assertEqual(self._login(self.client_a).status_code, 302)
        self.assertEqual(self.client_a.get("/lock").status_code, 405)
        self.assertEqual(self.client_a.get("/settings").status_code, 200)
        self.assertEqual(self.client_a.post("/lock").status_code, 403)
        response = self.client_a.post("/lock", data={"csrf_token": self._csrf(self.client_a)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/unlock")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.client_a.get("/settings").status_code, 404)

    def test_detail_and_management_credentials_are_not_interchangeable(self):
        from detail_access import delivery_payload_with_detail_token

        url = "/detail?sub=00000000-0000-0000-0000-000000000001"
        payload = {"html": "<p>synthetic detail</p>", "payload": {}}
        with patch.object(self.web, "_load_payload_result", return_value=payload) as load:
            self.assertEqual(self._login(self.client_a).status_code, 302)
            self.assertEqual(self.client_a.get(url).status_code, 404)
            self.assertEqual(self.client_a.get(url + "&token=" + TOKEN).status_code, 404)
            load.assert_not_called()
            delivery = delivery_payload_with_detail_token({"detail_url": url})
            self.assertNotIn(TOKEN, json.dumps(delivery))
            self.assertEqual(self.client_b.get(delivery["detail_url"]).status_code, 200)
            self.assertEqual(self._login(self.client_b, DETAIL_TOKEN).status_code, 404)
            self.assertEqual(self.client_b.get("/settings").status_code, 404)

    def test_real_management_views_never_run_when_denied(self):
        for name in ("email_notifier.send_email", "notifier.send"):
            self.effects[name] = self.stack.enter_context(patch(name, side_effect=AssertionError(name)))
        # Installing an upload spy must not import main or bind its notification aliases.
        original_main = sys.modules.get("main")
        isolated_main = ModuleType("main")
        for name in ("process_subscription", "_upload_payload_to_pythonanywhere"):
            mock = Mock(side_effect=AssertionError(name))
            setattr(isolated_main, name, mock)
            self.effects["main." + name] = mock
        csrf = self._csrf(self.client_b)
        with patch.dict(sys.modules, {"main": isolated_main}):
            for rule in self.web.app.url_map.iter_rules():
                if rule.endpoint not in MANAGEMENT_PATHS:
                    continue
                for method in sorted(rule.methods):
                    with self.subTest(endpoint=rule.endpoint, method=method):
                        response = getattr(self.client_b, method.lower())(
                            MANAGEMENT_PATHS[rule.endpoint], data={"csrf_token": csrf, "confirm_delete": "yes"})
                        self.assertEqual(response.status_code, 404)
        self.assertIs(sys.modules.get("main"), original_main)
        # The lazy main import (and hence PA upload) is downstream of this denied entry.
        for mock in self.effects.values():
            mock.assert_not_called()

    def test_delete_head_body_is_rejected_even_with_management_session(self):
        self.assertEqual(self._login(self.client_a).status_code, 302)
        response = self.client_a.head(MANAGEMENT_PATHS["delete_subscription"], data={"confirm_delete": "yes"})
        self.assertEqual(response.status_code, 404)
        self.effects["_subscription_repository"].assert_not_called()

    def test_explicit_configuration_states_and_candidate_validation(self):
        import management_access as access

        for value, expected in (("1", True), ("true", True), ("YES", True), ("on", True),
                                ("0", False), ("false", False), ("NO", False), ("off", False), ("", False)):
            with self.subTest(flag=value), patch.dict(os.environ, {"MANAGEMENT_AUTH_REQUIRED": value}):
                self.assertIs(access.management_auth_required(), expected)
        for value in ("typo", "01", "2"):
            with self.subTest(invalid=value), patch.dict(os.environ, {"MANAGEMENT_AUTH_REQUIRED": value}):
                with self.assertRaisesRegex(ValueError, "MANAGEMENT_AUTH_REQUIRED must be an explicit boolean"):
                    access.install_management_access(Flask("invalid-config"))
        with patch.dict(os.environ, {"MANAGEMENT_TOKEN": "  " + TOKEN + "  "}):
            self.assertEqual(access.management_token(), TOKEN)
            self.assertTrue(access.management_token_authorized(TOKEN))
        for token in ("", "\u975eASCII", "x" * 513, "embedded space", "line\nbreak"):
            with self.subTest(configured_length=len(token)), patch.dict(os.environ, {"MANAGEMENT_TOKEN": token}):
                self.assertFalse(access.management_token_authorized(token))
                self.assertEqual(access.token_generation(), "")
        for candidate in (None, 1, b"bytes", [], "", "\u975eASCII", "x" * 513):
            with self.subTest(candidate_type=type(candidate).__name__):
                self.assertFalse(access.management_token_authorized(candidate))

    def test_invalid_nonempty_token_does_not_restore_compatibility(self):
        with patch.dict(os.environ, {"MANAGEMENT_AUTH_REQUIRED": "0", "MANAGEMENT_TOKEN": "\u975eASCII"}):
            self.assertEqual(self.client_b.get("/settings").status_code, 404)

    def _assert_functional_comparison(self, authorize):
        self.assertTrue(authorize(TOKEN), "correct_token_rejected")
        self.assertFalse(authorize("wrong"), "wrong_token_authorized")

    def _assert_constant_time_dependency(self, authorize):
        import management_access as access

        with patch.object(access.hmac, "compare_digest", return_value=False) as compare:
            authorized = authorize(TOKEN)
            self.assertEqual(compare.call_args_list, [unittest.mock.call(TOKEN, TOKEN)], "compare_digest_not_used")
            self.assertFalse(authorized, "compare_digest_result_ignored")

    def test_real_comparator_and_constant_time_dependency(self):
        import management_access as access

        self._assert_functional_comparison(access.management_token_authorized)
        self._assert_constant_time_dependency(access.management_token_authorized)

    def test_permanent_comparison_mutations_are_rejected(self):
        import management_access as access

        with self.assertRaisesRegex(AssertionError, "wrong_token_authorized"):
            self._assert_functional_comparison(lambda candidate: True)
        with self.assertRaisesRegex(AssertionError, "compare_digest_not_used"):
            self._assert_constant_time_dependency(lambda candidate: candidate == access.management_token())

        def ignored_result(candidate):
            access.hmac.compare_digest(candidate, access.management_token())
            return True

        with self.assertRaisesRegex(AssertionError, "compare_digest_result_ignored"):
            self._assert_constant_time_dependency(ignored_result)

    def test_unlock_result_depends_on_authorization_helper(self):
        with patch.object(self.web, "management_token_authorized", return_value=False) as authorize:
            self.assertEqual(self._login(self.client_a).status_code, 404)
            authorize.assert_called_once_with(TOKEN)
        self.assertEqual(self.client_a.get("/settings").status_code, 404)


if __name__ == "__main__":
    unittest.main()
