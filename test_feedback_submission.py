from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import web_form
from web_test_utils import enable_csrf


SUBSCRIPTION_ID = "11111111-2222-4333-8444-555555555555"
FEEDBACK_CANARY = "UI_SMOKE_FEEDBACK_CANARY"


class FeedbackSubmissionContractTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_feedback_path = web_form.FEEDBACK_PATH
        web_form.FEEDBACK_PATH = Path(self.tmpdir.name) / "feedback.json"
        web_form.app.config.update(TESTING=True)
        self.notify_patcher = patch.object(
            web_form,
            "notify_feedback_author",
            return_value=False,
        )
        self.notify = self.notify_patcher.start()
        self.socket_patcher = patch.object(
            socket.socket,
            "connect",
            side_effect=AssertionError("feedback route attempted network access"),
        )
        self.socket_connect = self.socket_patcher.start()
        self.environment_patcher = patch.dict(
            os.environ,
            {"FEEDBACK_NOTIFY_EMAIL": ""},
            clear=False,
        )
        self.environment_patcher.start()
        self.client = web_form.app.test_client()
        enable_csrf(self.client, path=f"/feedback?sub={SUBSCRIPTION_ID}")

    def tearDown(self):
        self.environment_patcher.stop()
        self.socket_patcher.stop()
        self.notify_patcher.stop()
        web_form.FEEDBACK_PATH = self.old_feedback_path
        self.tmpdir.cleanup()

    def test_feedback_deep_link_get_is_read_only_and_prefills_subscription_id(self):
        response = self.client.get(f"/feedback?sub={SUBSCRIPTION_ID}")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('name="csrf_token"', body)
        self.assertIn(
            f'name="subscription_id" value="{SUBSCRIPTION_ID}"',
            body,
        )
        self.assertIn('name="feedback_type" value="unavailable"', body)
        self.assertFalse(web_form.FEEDBACK_PATH.exists())
        self.notify.assert_not_called()
        self.socket_connect.assert_not_called()

    def test_feedback_post_persists_exact_record_and_notifies_with_same_record(self):
        response = self.client.post(
            "/feedback",
            data={
                "subscription_id": SUBSCRIPTION_ID,
                "feedback_type": "unavailable",
                "unavailable_reason": "sold_out",
                "comment": FEEDBACK_CANARY,
            },
            headers={"User-Agent": "FeedbackContractAgent/1.0"},
        )
        body = response.get_data(as_text=True)
        records = json.loads(web_form.FEEDBACK_PATH.read_text(encoding="utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("已收到反馈", body)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(
            set(record),
            {
                "subscription_id",
                "feedback_type",
                "unavailable_reason",
                "comment",
                "created_at",
                "user_agent",
            },
        )
        self.assertTrue(all(isinstance(value, str) for value in record.values()))
        self.assertEqual(record["subscription_id"], SUBSCRIPTION_ID)
        self.assertEqual(record["feedback_type"], "unavailable")
        self.assertEqual(record["unavailable_reason"], "sold_out")
        self.assertEqual(record["comment"], FEEDBACK_CANARY)
        self.assertEqual(record["user_agent"], "FeedbackContractAgent/1.0")
        datetime.fromisoformat(record["created_at"])
        self.notify.assert_called_once_with(record)
        self.socket_connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
