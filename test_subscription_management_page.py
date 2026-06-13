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
        web_form.SUBSCRIPTIONS_PATH = self.tmp_path / "subscriptions.json"
        web_form.PAGE_PAYLOADS_DIR = self.tmp_path / "payloads"
        web_form.PAGE_PAYLOADS_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        web_form.SUBSCRIPTIONS_PATH = self.old_subscriptions_path
        web_form.PAGE_PAYLOADS_DIR = self.old_payloads_dir
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


if __name__ == "__main__":
    unittest.main()
