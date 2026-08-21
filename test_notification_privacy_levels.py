import copy
import json
import unittest
from pathlib import Path

from werkzeug.datastructures import MultiDict

import form_structure
import main
import web_form
from notifier import render_email, render_pushplus


FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "form_normalization_baseline_v1.json"


def _multi(data):
    items = []
    for key, value in data.items():
        if isinstance(value, (list, tuple)):
            items.extend((key, str(item)) for item in value)
        else:
            items.append((key, str(value)))
    return MultiDict(items)


def _privacy_payload(level=None):
    payload = {
        "route": "上海 → 大阪",
        "push_type": "涨价风险",
        "display_price": 10431,
        "current_price": 10431,
        "transaction_price": 63987,
        "passenger_summary": "2成人+1儿童+2老人",
        "passengers": {"adult": 2, "child": 1, "elderly": 2, "infant": 0},
        "detail_url": "http://127.0.0.1:5000/detail?sub=test",
    }
    if level is not None:
        payload["notification_privacy_level"] = level
    return payload


class NotificationPrivacyLevelTest(unittest.TestCase):
    def setUp(self):
        web_form.app.config.update(TESTING=True)

    def test_full_level_is_byte_identical_to_legacy_missing_level(self):
        legacy = _privacy_payload()
        explicit = _privacy_payload("full")
        self.assertEqual(render_pushplus(legacy), render_pushplus(explicit))
        self.assertEqual(render_email(legacy), render_email(explicit))

    def test_redacted_level_buckets_price_and_hides_passenger_details(self):
        push = render_pushplus(_privacy_payload("redacted"))
        _subject, email_html = render_email(_privacy_payload("redacted"))
        for rendered in (push, email_html):
            self.assertIn("上海 → 大阪", rendered)
            self.assertIn("¥10,000-10,999", rendered)
            self.assertIn("本地详情页", rendered)
            self.assertNotIn("10,431", rendered)
            self.assertNotIn("63,987", rendered)
            self.assertNotIn("2成人", rendered)
            self.assertNotIn("1儿童", rendered)

    def test_minimal_level_only_reports_route_change(self):
        push = render_pushplus(_privacy_payload("minimal"))
        _subject, email_html = render_email(_privacy_payload("minimal"))
        for rendered in (push, email_html):
            self.assertIn("上海 → 大阪", rendered)
            self.assertIn("有变动", rendered)
            self.assertNotIn("10,431", rendered)
            self.assertNotIn("10,000-10,999", rendered)
            self.assertNotIn("2成人", rendered)
            self.assertNotIn("详情页", rendered)

    def test_page_two_renders_privacy_selector_and_roundtrips_non_default(self):
        page = web_form.app.test_client().get("/settings").get_data(as_text=True)
        self.assertEqual(page.count('name="notification_privacy_level"'), 1)
        for value in ("full", "redacted", "minimal"):
            self.assertIn(f'value="{value}"', page)
        self.assertIn(
            "notification_privacy_level",
            form_structure.FORM_STATIONS[5]["fields"],
        )
        self.assertIn("notification_privacy", form_structure.CONCEPTS)

        scenarios = json.loads(FIXTURE.read_text(encoding="utf-8"))["scenarios"]
        form = copy.deepcopy(scenarios["solo_minimal"]["form_input"])
        form["monitor_mode"] = "precise"
        form["notification_privacy_level"] = "redacted"
        normalized = main.normalize_subscription(web_form.build_subscription(_multi(form)))
        self.assertEqual(
            normalized["notification_goals"]["privacy_level"],
            "redacted",
        )
        values = form_structure.subscription_to_form_values(normalized)
        self.assertEqual(values["notification_privacy_level"], "redacted")

    def test_default_full_does_not_change_legacy_normalized_shape(self):
        scenarios = json.loads(FIXTURE.read_text(encoding="utf-8"))["scenarios"]
        form = copy.deepcopy(scenarios["solo_minimal"]["form_input"])
        form["notification_privacy_level"] = "full"
        normalized = main.normalize_subscription(web_form.build_subscription(_multi(form)))
        self.assertNotIn("privacy_level", normalized["notification_goals"])


if __name__ == "__main__":
    unittest.main()
