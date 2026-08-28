import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import web_form
from subscription_identity import ensure_subscription_id
from web_test_utils import enable_csrf


SUBSCRIPTION_ID = "123e4567-e89b-12d3-a456-426614174120"


def _stored_subscription(*, status: str = "active", budget: int = 1000) -> dict:
    return {
        "subscription_id": SUBSCRIPTION_ID,
        "status": status,
        "origin": "PVG",
        "destination": "KIX",
        "depart_date": "2026-10-01",
        "round_trip": False,
        "route_type": "international",
        "basic": {
            "origin": "PVG",
            "destination": "KIX",
            "departure_date": "2026-10-01",
            "route_type": "international",
        },
        "hard_constraints": {"max_budget": budget},
        "soft_preferences": {},
        "notification_goals": {"method": "both", "email": ""},
    }


class SubscriptionRepositoryWebContractTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "subscriptions.json"
        self.path.write_text(
            json.dumps([_stored_subscription()], ensure_ascii=False),
            encoding="utf-8",
        )
        self.old_path = web_form.SUBSCRIPTIONS_PATH
        web_form.SUBSCRIPTIONS_PATH = self.path
        web_form.app.config.update(TESTING=True)
        self.client = web_form.app.test_client()
        enable_csrf(self.client)

    def tearDown(self):
        web_form.SUBSCRIPTIONS_PATH = self.old_path
        self.tmpdir.cleanup()

    def _saved(self) -> list[dict]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_new_list_links_use_subscription_id_not_array_index(self):
        body = self.client.get("/subscriptions").get_data(as_text=True)

        self.assertIn(f"/?edit={SUBSCRIPTION_ID}", body)
        self.assertIn(f"/subscriptions/{SUBSCRIPTION_ID}/toggle", body)
        self.assertNotIn("/?edit=0", body)
        self.assertNotIn("/subscriptions/0/toggle", body)

    def test_list_assigns_identity_before_rendering_links_for_legacy_record(self):
        legacy = _stored_subscription()
        legacy.pop("subscription_id")
        self.path.write_text(
            json.dumps([legacy], ensure_ascii=False),
            encoding="utf-8",
        )

        generated_id = "0aec3430-5248-4e02-903d-dd9e31d89459"

        def assign_deterministic_identity(subscription):
            return ensure_subscription_id(
                subscription,
                id_factory=lambda: generated_id,
            )

        with (
            patch(
                "subscription_repository.ensure_subscription_id",
                side_effect=assign_deterministic_identity,
            ),
            patch.object(web_form, "safe_log") as log_mock,
        ):
            body = self.client.get("/subscriptions").get_data(as_text=True)

        saved_id = self._saved()[0]["subscription_id"]
        self.assertEqual(saved_id, generated_id)
        self.assertIn(f"/?edit={saved_id}", body)
        self.assertIn(f"/subscriptions/{saved_id}/toggle", body)
        self.assertNotIn('href="/?edit=0"', body)
        self.assertNotIn('action="/subscriptions/0/toggle"', body)
        self.assertTrue(
            any("[身份迁移]" in str(call.args[0]) for call in log_mock.call_args_list)
        )

    def test_m0_numeric_edit_resolves_to_id_then_logs_migration(self):
        # M1 removes this numeric compatibility entrypoint.
        with patch.object(web_form, "safe_log") as log_mock:
            body = self.client.get("/settings?edit=0").get_data(as_text=True)

        self.assertIn(
            f'name="subscription_index" value="{SUBSCRIPTION_ID}"',
            body,
        )
        self.assertTrue(
            any(
                "[订阅编辑迁移]" in str(call.args[0])
                and "M0" in str(call.args[0])
                and SUBSCRIPTION_ID in str(call.args[0])
                for call in log_mock.call_args_list
            )
        )
        self.assertEqual(web_form.LEGACY_INDEX_EDIT_COMPATIBILITY, "M0_REMOVE_IN_M1")

    def test_m0_numeric_edit_returns_404_when_index_no_longer_exists(self):
        response = self.client.get("/settings?edit=9")

        self.assertEqual(response.status_code, 404)

    def test_stable_id_edit_does_not_use_legacy_migration(self):
        with patch.object(web_form, "safe_log") as log_mock:
            body = self.client.get(
                f"/settings?edit={SUBSCRIPTION_ID}"
            ).get_data(as_text=True)

        self.assertIn(
            f'name="subscription_index" value="{SUBSCRIPTION_ID}"',
            body,
        )
        self.assertFalse(
            any("[订阅编辑迁移]" in str(call.args[0]) for call in log_mock.call_args_list)
        )

    def test_edit_post_updates_by_id_keeps_count_and_starts_collection_after_save(self):
        rebuilt = _stored_subscription(budget=2500)
        rebuilt.pop("subscription_id")
        observed = []

        def start(saved):
            observed.append(("start", self._saved(), saved))
            return {"status": "started", "entrypoint": "web"}

        with (
            patch.object(web_form, "build_subscription", return_value=rebuilt),
            patch.object(web_form, "start_background_collection", side_effect=start),
        ):
            response = self.client.post(
                "/subscribe?owner_id=query-owner-must-be-ignored",
                data={
                    "form_page": "full",
                    "subscription_index": SUBSCRIPTION_ID,
                    "owner_id": "request-owner-must-be-ignored",
                },
                headers={"X-Owner-ID": "header-owner-must-be-ignored"},
            )

        saved = self._saved()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.headers["Location"].endswith(
                f"/success?subscription_id={SUBSCRIPTION_ID}"
            )
        )
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["subscription_id"], SUBSCRIPTION_ID)
        self.assertEqual(saved[0]["hard_constraints"]["max_budget"], 2500)
        self.assertEqual(observed[0][0], "start")
        self.assertEqual(observed[0][1], saved)
        self.assertEqual(observed[0][2]["subscription_id"], SUBSCRIPTION_ID)

    def test_toggle_quick_update_and_delete_are_id_scoped_and_csrf_protected(self):
        unauthorized = web_form.app.test_client()
        before = self.path.read_bytes()
        denied = unauthorized.post(f"/subscriptions/{SUBSCRIPTION_ID}/toggle")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.path.read_bytes(), before)

        toggle = self.client.post(f"/subscriptions/{SUBSCRIPTION_ID}/toggle")
        self.assertEqual(toggle.status_code, 302)
        self.assertEqual(self._saved()[0]["status"], "paused")
        enabled = self.client.post(f"/subscriptions/{SUBSCRIPTION_ID}/toggle")
        self.assertEqual(enabled.status_code, 302)
        self.assertEqual(self._saved()[0]["status"], "active")

        quick = self.client.post(
            f"/subscriptions/{SUBSCRIPTION_ID}/quick-update",
            data={"field": "refund_flexibility", "value": "preferred"},
        )
        self.assertEqual(quick.status_code, 302)
        self.assertEqual(
            self._saved()[0]["soft_preferences"]["refund_flexibility"],
            "preferred",
        )

        confirm = self.client.get(f"/subscription/{SUBSCRIPTION_ID}/delete")
        self.assertEqual(confirm.status_code, 200)
        self.assertEqual(len(self._saved()), 1)

        missing_confirmation = self.client.post(
            f"/subscription/{SUBSCRIPTION_ID}/delete",
            data={},
        )
        self.assertEqual(missing_confirmation.status_code, 400)
        self.assertEqual(len(self._saved()), 1)

        deleted = self.client.post(
            f"/subscription/{SUBSCRIPTION_ID}/delete",
            data={"confirm_delete": "yes"},
        )
        self.assertEqual(deleted.status_code, 302)
        self.assertEqual(self._saved(), [])

    def test_missing_id_crud_becomes_404_without_recreating_subscription(self):
        missing = "123e4567-e89b-12d3-a456-426614174199"

        self.assertEqual(
            self.client.post(f"/subscriptions/{missing}/toggle").status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                f"/subscriptions/{missing}/quick-update",
                data={"field": "refund_flexibility", "value": "preferred"},
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f"/subscription/{missing}/delete").status_code,
            404,
        )
        self.assertEqual(len(self._saved()), 1)

    def test_startup_handshake_four_states_remain_truthful_for_id_route(self):
        expected = {
            "started": "1-2分钟",
            "busy": "已有采集轮正在执行",
            "startup_error": "首次采集未能启动",
            "confirming": "状态正在确认",
        }
        for status, phrase in expected.items():
            with self.subTest(status=status):
                with patch.object(
                    web_form,
                    "build_subscription",
                    return_value=_stored_subscription(budget=2000),
                ), patch.object(
                    web_form,
                    "start_background_collection",
                    return_value={"status": status, "entrypoint": "web"},
                ):
                    response = self.client.post(
                        "/subscribe",
                        data={
                            "form_page": "full",
                            "subscription_index": SUBSCRIPTION_ID,
                        },
                        follow_redirects=True,
                    )
                body = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn(phrase, body)
                if status != "started":
                    self.assertNotIn("1-2分钟", body)

    def test_owner_scope_is_never_read_from_request_data(self):
        source = Path(web_form.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        repository_methods = {
            "list_for_owner",
            "get",
            "create",
            "update",
            "delete",
            "mutate",
            "resolve_legacy_index",
        }

        def is_repository_factory_call(node):
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_subscription_repository"
            )

        repository_variables = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and is_repository_factory_call(node.value)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in repository_methods
            and (
                is_repository_factory_call(node.func.value)
                or (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id in repository_variables
                )
            )
        ]
        self.assertGreater(len(calls), 0)
        for call in calls:
            with self.subTest(method=call.func.attr, line=call.lineno):
                self.assertGreater(len(call.args), 0)
                self.assertIsInstance(call.args[0], ast.Name)
                self.assertEqual(call.args[0].id, "LOCAL_OWNER_ID")
        self.assertNotIn("def save_subscription(", source)
        self.assertNotIn("<int:index>", source)
        self.assertIn(
            '@app.post("/subscriptions/<subscription_id>/toggle")',
            source,
        )


if __name__ == "__main__":
    unittest.main()
