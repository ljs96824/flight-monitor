"""Current application behavior, not an authorization policy or a deployment audit.

Both clients are anonymous to Flask: client_a only models the maintainer's
session. A separately issued CSRF token is not proof of subscription ownership.
"""

from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
import importlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch


class AnonymousAccessBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {
            "NO_LIVE_API": "1",
            "PYTHON_DOTENV_DISABLED": "1",
            "FLASK_SECRET_KEY": "anonymous-boundary-test-only-session-key",
            "SHARED_DETAIL_TOKEN": "anonymous-boundary-test-only-detail-token",
        }, clear=True))
        self.stack.enter_context(patch("dotenv.load_dotenv", return_value=False))
        # Existing suites may install minimal dotenv/httpx module substitutes.
        self.stack.enter_context(patch("dotenv.dotenv_values", return_value={}, create=True))
        self.denials = {
            name: self.stack.enter_context(patch(name, side_effect=AssertionError(name)))
            for name in (
                "socket.socket.connect", "socket.socket.connect_ex",
                "socket.socket.sendto", "socket.create_connection", "socket.getaddrinfo",
                "smtplib.SMTP", "smtplib.SMTP_SSL", "sqlite3.connect",
                "requests.sessions.Session.request",
            )
        }
        for name in ("httpx.get", "httpx.post", "httpx.request", "httpx.Client", "httpx.AsyncClient"):
            self.denials[name] = self.stack.enter_context(
                patch(name, side_effect=AssertionError(name), create=True)
            )
        self.addCleanup(self._assert_no_real_io)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.web = importlib.import_module("web_form")
        for name, path in {
            "SUBSCRIPTIONS_PATH": self.root / "subscriptions.json",
            "FEEDBACK_PATH": self.root / "feedback.json",
            "PAGE_PAYLOADS_DIR": self.root / "payloads",
        }.items():
            self.stack.enter_context(patch.object(self.web, name, path))
        self.stack.enter_context(patch.dict(self.web.app.config, {
            "TESTING": True,
            "SECRET_KEY": "anonymous-boundary-test-only-session-key",
            "SESSION_COOKIE_SECURE": False,
            "CSRF_CLOCK": lambda: 1800000000,
        }))
        clock = self.stack.enter_context(patch.object(self.web, "datetime", wraps=datetime))
        clock.now.return_value = datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc)
        self.stack.enter_context(patch.object(self.web, "safe_log"))
        self.calendar = self.stack.enter_context(patch.object(self.web, "load_calendar", return_value={}))
        self.collection = self.stack.enter_context(patch.object(
            self.web, "start_background_collection",
            return_value={"status": "started", "entrypoint": "web"},
        ))
        self.notification = self.stack.enter_context(patch.object(
            self.web, "notify_feedback_author", return_value=False,
        ))
        self.client_a = self.web.app.test_client()
        self.client_b = self.web.app.test_client()
        self.token_a = self._token(self.client_a)
        created = self.client_a.post("/subscribe", data={
            **self._form(), "csrf_token": self.token_a,
        })
        self.assertEqual(created.status_code, 302)
        saved = self._subscriptions()
        self.assertEqual(len(saved), 1)
        self.subscription_id = saved[0]["subscription_id"]
        self.collection.assert_called_once()
        self.collection.reset_mock()
        self.token_b = self._token(self.client_b)

    def _assert_no_real_io(self):
        for name, mock in self.denials.items():
            self.assertEqual(mock.call_count, 0, name)
        for name in ("prices.db", "observations.sqlite3", "api_usage.json"):
            self.assertFalse((self.root / name).exists(), name)

    def _form(self):
        # These fixed form fields do not enter a current-date collection path.
        return {
            "origin_select": "PVG", "destination": "PEK",
            "depart_date": "2026-09-01", "round_trip": "false",
            "adult_count": "1", "max_budget": "1200", "target_price": "800",
            "notification_method": "page_only",
        }

    def _token(self, client):
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
        self.assertIsNotNone(match, "anonymous GET must issue this client's own token")
        return match.group(1)

    def _subscriptions(self):
        return json.loads(self.web.SUBSCRIPTIONS_PATH.read_text(encoding="utf-8"))

    def _post_b(self, path, **data):
        return self.client_b.post(path, data={**data, "csrf_token": self.token_b})

    def _assert_no_actions(self):
        self.collection.assert_not_called()
        self.notification.assert_not_called()

    def test_clients_have_distinct_csrf_sessions_and_cannot_exchange_tokens(self):
        with self.client_a.session_transaction() as session_a:
            nonce_a = session_a["_csrf_nonce"]
        with self.client_b.session_transaction() as session_b:
            self.assertNotEqual(session_b["_csrf_nonce"], nonce_a)
        self.assertNotEqual(self.token_a, self.token_b)
        before = self.web.SUBSCRIPTIONS_PATH.read_bytes()
        response = self.client_b.post(
            f"/subscriptions/{self.subscription_id}/toggle",
            data={"csrf_token": self.token_a},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.web.SUBSCRIPTIONS_PATH.read_bytes(), before)
        self._assert_no_actions()

    def test_anonymous_read_pages_expose_other_clients_subscription_id(self):
        before = self.web.SUBSCRIPTIONS_PATH.read_bytes()
        for path in ("/subscriptions", f"/settings?edit={self.subscription_id}", "/success"):
            with self.subTest(path=path):
                response = self.client_b.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(self.subscription_id, response.get_data(as_text=True))
                self.assertEqual(self.web.SUBSCRIPTIONS_PATH.read_bytes(), before)
        self._assert_no_actions()

    def test_anonymous_subscribe_creates_and_reaches_mock_collection(self):
        response = self._post_b("/subscribe", **self._form())
        self.assertEqual(response.status_code, 302)
        saved = self._subscriptions()
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[0]["subscription_id"], self.subscription_id)
        self.assertNotEqual(saved[1]["subscription_id"], self.subscription_id)
        self.collection.assert_called_once()
        self.assertEqual(self.collection.call_args.args[0]["subscription_id"], saved[1]["subscription_id"])
        self.notification.assert_not_called()

    def test_anonymous_subscribe_can_edit_other_clients_subscription(self):
        form = {**self._form(), "subscription_index": self.subscription_id, "max_budget": "1400"}
        response = self._post_b("/subscribe", **form)
        self.assertEqual(response.status_code, 302)
        saved = self._subscriptions()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["subscription_id"], self.subscription_id)
        self.assertEqual(saved[0]["hard_constraints"]["max_budget"], 1400)
        self.collection.assert_called_once()
        self.assertEqual(self.collection.call_args.args[0]["subscription_id"], self.subscription_id)
        self.notification.assert_not_called()

    def test_anonymous_toggle_mutates_other_clients_subscription(self):
        self.assertEqual(self._subscriptions()[0]["status"], "active")
        response = self._post_b(f"/subscriptions/{self.subscription_id}/toggle")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._subscriptions()[0]["status"], "paused")
        self._assert_no_actions()

    def test_anonymous_quick_update_mutates_other_clients_subscription(self):
        response = self._post_b(f"/subscriptions/{self.subscription_id}/quick-update",
                                field="time_preference", value="daytime")
        self.assertEqual(response.status_code, 302)
        saved = self._subscriptions()[0]
        self.assertEqual(saved["hard_constraints"]["time_preference"], "daytime")
        self.assertEqual(saved["soft_preferences"]["time_preference"], "daytime")
        self._assert_no_actions()

    def test_anonymous_feedback_persists_and_reaches_mock_notification(self):
        before = self.web.SUBSCRIPTIONS_PATH.read_bytes()
        response = self._post_b("/feedback", sub=self.subscription_id,
                                feedback_type="other", comment="synthetic anonymous feedback")
        self.assertEqual(response.status_code, 200)
        records = json.loads(self.web.FEEDBACK_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["subscription_id"], self.subscription_id)
        self.assertEqual(records[0]["comment"], "synthetic anonymous feedback")
        self.notification.assert_called_once_with(records[0])
        self.collection.assert_not_called()
        self.assertEqual(self.web.SUBSCRIPTIONS_PATH.read_bytes(), before)

    def test_anonymous_delete_get_is_confirmation_only_without_posted_csrf(self):
        before = self.web.SUBSCRIPTIONS_PATH.read_bytes()
        response = self.client_b.get(f"/subscription/{self.subscription_id}/delete")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.subscription_id, response.get_data(as_text=True))
        self.assertIn("PVG", response.get_data(as_text=True))
        self.assertIn("PEK", response.get_data(as_text=True))
        self.assertIn('name="confirm_delete"', response.get_data(as_text=True))
        self.assertEqual(self.web.SUBSCRIPTIONS_PATH.read_bytes(), before)
        self._assert_no_actions()

    def test_anonymous_confirmed_delete_post_can_remove_other_clients_subscription(self):
        response = self._post_b(f"/subscription/{self.subscription_id}/delete", confirm_delete="yes")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._subscriptions(), [])
        self._assert_no_actions()

    def _store_detail(self):
        self.web.PAGE_PAYLOADS_DIR.mkdir()
        path = self.web.PAGE_PAYLOADS_DIR / f"{self.subscription_id}.json"
        path.write_text(json.dumps({"html": "<p>synthetic detail marker</p>", "payload": {}}), encoding="utf-8")
        return path

    def test_anonymous_detail_requires_configured_token_and_accepts_delivery_link(self):
        from detail_access import delivery_payload_with_detail_token

        path = self._store_detail()
        before = path.read_bytes()
        bare_url = f"/detail?sub={self.subscription_id}"
        for url in (bare_url, bare_url + "&token=wrong-test-only-token"):
            with self.subTest(url=url):
                self.assertEqual(self.client_b.get(url).status_code, 404)
        original = {"detail_url": bare_url}
        delivery = delivery_payload_with_detail_token(original)
        accepted = self.client_b.get(delivery["detail_url"])
        self.assertEqual(accepted.status_code, 200)
        self.assertIn("synthetic detail marker", accepted.get_data(as_text=True))
        self.assertEqual(original, {"detail_url": bare_url})
        self.assertEqual(path.read_bytes(), before)
        self._assert_no_actions()

    def test_anonymous_detail_token_check_is_optional_when_not_configured(self):
        self._store_detail()
        with patch.dict(os.environ, {"SHARED_DETAIL_TOKEN": ""}):
            response = self.client_b.get(f"/detail?sub={self.subscription_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("synthetic detail marker", response.get_data(as_text=True))
        self._assert_no_actions()

    def test_missing_csrf_rejects_all_write_routes_before_side_effects(self):
        before = self.web.SUBSCRIPTIONS_PATH.read_bytes()
        routes = {
            "/subscribe": self._form(),
            "/defaults_preview": self._form(),
            f"/subscriptions/{self.subscription_id}/toggle": {},
            f"/subscriptions/{self.subscription_id}/quick-update": {"field": "time_preference", "value": "daytime"},
            f"/subscription/{self.subscription_id}/delete": {"confirm_delete": "yes"},
            "/feedback": {"sub": self.subscription_id, "feedback_type": "other", "comment": "synthetic"},
        }
        for path, data in routes.items():
            with self.subTest(path=path):
                response = self.client_b.post(path, data=data)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.web.SUBSCRIPTIONS_PATH.read_bytes(), before)
                self.assertFalse(self.web.FEEDBACK_PATH.exists())
                self._assert_no_actions()


if __name__ == "__main__":
    unittest.main()
