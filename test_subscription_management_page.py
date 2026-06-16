import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
try:
    import flask  # noqa: F401
except ModuleNotFoundError:
    class _DummyFlask:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, *args, **kwargs):
            return lambda func: func

        def post(self, *args, **kwargs):
            return lambda func: func

        def route(self, *args, **kwargs):
            return lambda func: func

    sys.modules.setdefault(
        "flask",
        types.SimpleNamespace(
            Flask=_DummyFlask,
            jsonify=lambda value=None, **kwargs: value if value is not None else kwargs,
            redirect=lambda value: value,
            render_template_string=lambda template, **kwargs: template,
            request=types.SimpleNamespace(args={}, form={}, values={}, headers={}),
            url_for=lambda endpoint, **kwargs: f"/{endpoint}",
        ),
    )

import web_form


class SubscriptionManagementPageTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.old_subscriptions_path = web_form.SUBSCRIPTIONS_PATH
        self.old_payloads_dir = web_form.PAGE_PAYLOADS_DIR
        self.old_feedback_path = web_form.FEEDBACK_PATH
        web_form.SUBSCRIPTIONS_PATH = self.tmp_path / "subscriptions.json"
        web_form.PAGE_PAYLOADS_DIR = self.tmp_path / "payloads"
        web_form.FEEDBACK_PATH = self.tmp_path / "feedback.json"
        web_form.PAGE_PAYLOADS_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        web_form.SUBSCRIPTIONS_PATH = self.old_subscriptions_path
        web_form.PAGE_PAYLOADS_DIR = self.old_payloads_dir
        web_form.FEEDBACK_PATH = self.old_feedback_path
        self.tmpdir.cleanup()

    def _write_subscriptions(self):
        records = [
            {
                "id": "sub-active",
                "status": "active",
                "round_trip": True,
                "basic": {
                    "origin": "上海",
                    "destination": "北京",
                    "route_type": "domestic",
                    "departure_date": "2026-06-19",
                    "return_date": "2026-06-19",
                },
                "soft_preferences": {"travel_scenarios": ["business"]},
            },
            {
                "id": "sub-paused",
                "status": "paused",
                "round_trip": False,
                "basic": {
                    "origin": "上海",
                    "destination": "大阪",
                    "route_type": "international",
                    "departure_date": "2026-10-01",
                },
                "soft_preferences": {"travel_scenarios": ["tourism", "family"]},
            },
        ]
        web_form.SUBSCRIPTIONS_PATH.write_text(
            json.dumps(records, ensure_ascii=False),
            encoding="utf-8",
        )
        (web_form.PAGE_PAYLOADS_DIR / "sub-active.json").write_text(
            json.dumps(
                {
                    "created_at": "2026-06-13T10:00:00",
                    "payload": {"push_type": "值得验证", "current_price": 680},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_subscription_list_items_include_display_fields_and_actions_data(self):
        self._write_subscriptions()

        with patch.object(web_form, "url_for", side_effect=lambda endpoint, **kwargs: f"/{endpoint}?{kwargs}"):
            items = web_form.build_subscription_list_items(web_form.load_subscriptions())

        self.assertEqual(items[0]["route"], "上海 → 北京")
        self.assertEqual(items[0]["route_type_label"], "国内")
        self.assertEqual(items[0]["status"], "active")
        self.assertIn("值得验证(¥680)", items[0]["last_decision"])
        self.assertEqual(items[1]["status"], "paused")
        self.assertEqual(items[1]["scenario"], "旅游 + 家庭/亲子")

    def test_toggle_and_delete_subscription(self):
        self._write_subscriptions()

        with patch.object(web_form, "url_for", return_value="/subscriptions"), \
             patch.object(web_form, "redirect", side_effect=lambda value: value):
            response = web_form.toggle_subscription(0)
        self.assertEqual(response, "/subscriptions")
        subscriptions = json.loads(web_form.SUBSCRIPTIONS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(subscriptions[0]["status"], "paused")

        with patch.object(web_form, "url_for", return_value="/subscriptions"), \
             patch.object(web_form, "redirect", side_effect=lambda value: value):
            response = web_form.delete_subscription(1)
        self.assertEqual(response, "/subscriptions")
        subscriptions = json.loads(web_form.SUBSCRIPTIONS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(subscriptions), 1)
        self.assertEqual(subscriptions[0]["id"], "sub-active")

    def test_success_page_sets_next_step_expectations(self):
        self._write_subscriptions()
        client = getattr(web_form.app, "test_client", None)
        if client is None:
            self.skipTest("Flask test client is unavailable")

        response = web_form.app.test_client().get("/success?index=0")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("监控已创建", body)
        self.assertIn("立即进行第一次采集和购买判断", body)
        self.assertIn("约30秒-1分钟", body)
        self.assertIn("查看我的所有监控", body)
        self.assertIn("第一次判断稍后到达", body)

    def test_success_and_feedback_templates_include_next_step_copy(self):
        self.assertIn("立即进行第一次采集和购买判断", web_form.SUCCESS_TEMPLATE)
        self.assertIn("约30秒-1分钟", web_form.SUCCESS_TEMPLATE)
        self.assertIn("查看我的所有监控", web_form.SUCCESS_TEMPLATE)
        self.assertIn("下次采集时重新核实", web_form.FEEDBACK_TEMPLATE)
        self.assertIn("返回我的监控", web_form.FEEDBACK_TEMPLATE)

    def test_feedback_success_page_promises_next_collection_response(self):
        client = getattr(web_form.app, "test_client", None)
        if client is None:
            self.skipTest("Flask test client is unavailable")

        response = web_form.app.test_client().post(
            "/feedback",
            data={"subscription_id": "sub-active", "feedback_type": "unavailable"},
        )
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("下次采集时重新核实", body)
        self.assertIn("返回我的监控", body)

    def test_feedback_post_saves_locally_and_notifies_author_email(self):
        client = getattr(web_form.app, "test_client", None)
        if client is None:
            self.skipTest("Flask test client is unavailable")

        with patch.dict("os.environ", {"FEEDBACK_NOTIFY_EMAIL": "author@example.com"}), patch(
            "email_notifier.send_email", return_value=True
        ) as send_email:
            response = web_form.app.test_client().post(
                "/feedback",
                data={
                    "subscription_id": "sub-active",
                    "feedback_type": "unavailable",
                    "unavailable_reason": "sold_out",
                    "comment": "price changed",
                },
                headers={"User-Agent": "UnitTestAgent"},
            )

        records = json.loads(web_form.FEEDBACK_PATH.read_text(encoding="utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(records[-1]["subscription_id"], "sub-active")
        send_email.assert_called_once()
        args, _kwargs = send_email.call_args
        self.assertEqual(args[0], "author@example.com")
        self.assertIn("unavailable", args[1])
        self.assertIn("sub-active", args[1])
        self.assertIn("price changed", args[2])

    def test_feedback_author_email_helper_uses_configured_recipient(self):
        record = {
            "subscription_id": "sub-active",
            "feedback_type": "unavailable",
            "unavailable_reason": "sold_out",
            "comment": "price changed",
            "created_at": "2026-06-16T10:00:00",
            "user_agent": "UnitTestAgent",
        }

        with patch.dict("os.environ", {"FEEDBACK_NOTIFY_EMAIL": "author@example.com"}), patch(
            "email_notifier.send_email", return_value=True
        ) as send_email:
            web_form.notify_feedback_author(record)

        send_email.assert_called_once()
        args, _kwargs = send_email.call_args
        self.assertEqual(args[0], "author@example.com")
        self.assertIn("unavailable", args[1])
        self.assertIn("sub-active", args[1])
        self.assertIn("price changed", args[2])


if __name__ == "__main__":
    unittest.main()
