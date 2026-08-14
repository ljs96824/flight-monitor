import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.datastructures import MultiDict

import form_structure
import main
import web_form


FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "form_normalization_baseline_v1.json"


def _fixture_case(name="solo_minimal"):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return copy.deepcopy(payload["scenarios"][name])


class NotificationChannelRegressionTest(unittest.TestCase):
    def setUp(self):
        web_form.app.config.update(TESTING=True)

    def test_station_six_exposes_three_channels_with_both_as_default(self):
        station = next(
            item for item in form_structure.FORM_STATIONS
            if item["id"] == "notifications"
        )
        self.assertIn("notification_method", station["fields"])
        self.assertIn("notification_email", station["fields"])

        template = web_form.app.test_client().get("/settings").get_data(as_text=True)
        self.assertEqual(template.count('name="notification_method"'), 1)
        for value in ("email", "pushplus", "both"):
            self.assertIn(f'value="{value}"', template)
        self.assertIn('<option value="both" selected>', template)
        self.assertNotIn('value="page_only"', template)
        self.assertIn('data-visibility-contract="notification-email"', template)
        self.assertEqual(
            form_structure.OPTIONAL_SECTION_DEFAULTS["notifications"]["notification_method"],
            "both",
        )
        self.assertIn(
            "邮箱+PushPlus",
            form_structure.summarize_optional_sections({})["notifications"],
        )
    def test_full_page_reveals_email_control_for_email_or_both(self):
        template = web_form.app.test_client().get("/settings").get_data(as_text=True)
        self.assertIn("function updateEmailVisibility()", template)
        self.assertIn("['email', 'both'].includes(method)", template)
        self.assertIn('data-visibility-contract="notification-email"', template)
        self.assertNotIn("setOptionalSectionExpanded", template)
    def test_notification_email_visibility_matches_selected_channel(self):
        self.assertIn(
            "notification_email",
            form_structure.visible_field_names({"notification_method": "email"}),
        )
        self.assertIn(
            "notification_email",
            form_structure.visible_field_names({"notification_method": "both"}),
        )
        self.assertNotIn(
            "notification_email",
            form_structure.visible_field_names({"notification_method": "pushplus"}),
        )

    def test_three_notification_methods_roundtrip_through_form_and_normalizer(self):
        base = _fixture_case()["form_input"]
        for method, email in (
            ("email", "email-only@example.com"),
            ("pushplus", ""),
            ("both", "both@example.com"),
        ):
            with self.subTest(method=method):
                form = dict(base)
                form.update(notification_method=method, notification_email=email)
                built = web_form.build_subscription(MultiDict(form))
                normalized = main.normalize_subscription(built)
                goals = normalized["notification_goals"]
                self.assertEqual(goals["method"], method)
                self.assertEqual(goals["email"], email)
                self.assertEqual(
                    set(goals),
                    {
                        "primary",
                        "secondary",
                        "method",
                        "email",
                        "frequency",
                        "price_change_threshold",
                        "digest_time",
                    },
                )

    def test_missing_method_defaults_to_both_and_logs_real_email_presence(self):
        subscription = _fixture_case()["normalized_subscription"]
        subscription["notification_goals"].pop("method", None)
        subscription["notification_goals"]["email"] = "saved@example.com"

        with patch.object(main, "safe_log") as log:
            normalized = main.normalize_subscription(subscription)

        self.assertEqual(normalized["notification_goals"]["method"], "both")
        self.assertTrue(
            any("[通知配置] method=both email=有" in str(call.args[0]) for call in log.call_args_list)
        )

    def test_edit_projection_and_route_keep_existing_email_and_method(self):
        subscription = _fixture_case()["normalized_subscription"]
        subscription["notification_goals"].update(
            {"method": "email", "email": "saved@example.com"}
        )
        values = form_structure.subscription_to_form_values(subscription)
        self.assertEqual(values["notification_method"], "email")
        self.assertEqual(values["notification_email"], "saved@example.com")

        original = web_form.SUBSCRIPTIONS_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            path.write_text(
                json.dumps([subscription], ensure_ascii=False),
                encoding="utf-8",
            )
            web_form.SUBSCRIPTIONS_PATH = path
            try:
                page = web_form.app.test_client().get(
                    "/settings?edit=0"
                ).get_data(as_text=True)
            finally:
                web_form.SUBSCRIPTIONS_PATH = original
        self.assertIn('<option value="email" selected>', page)
        self.assertIn(
            'name="notification_email" type="email" value="saved@example.com"',
            page,
        )
    def test_phase_one_fixture_adds_email_only_and_both_contracts(self):
        scenarios = json.loads(FIXTURE.read_text(encoding="utf-8"))["scenarios"]
        self.assertEqual(len(scenarios), 10)
        self.assertEqual(
            scenarios["email_only_notification"]["normalized_subscription"]["notification_goals"]["method"],
            "email",
        )
        self.assertEqual(
            scenarios["both_notification"]["normalized_subscription"]["notification_goals"]["method"],
            "both",
        )
        for name in ("email_only_notification", "both_notification"):
            self.assertEqual(
                set(scenarios[name]["normalized_subscription"]["notification_goals"]),
                {
                    "primary",
                    "secondary",
                    "method",
                    "email",
                    "frequency",
                    "price_change_threshold",
                    "digest_time",
                },
            )

    def test_read_only_audit_lists_incomplete_and_suspicious_phase_one_records(self):
        from scripts.list_incomplete_notification_subs import scan_notification_config_issues

        records = [
            {
                "_index": 1,
                "created_at": "2026-08-13T09:00:00+08:00",
                "origin": "PVG",
                "destination": "KIX",
                "notification_goals": {"method": "both", "email": "ok@example.com"},
            },
            {
                "_index": 2,
                "created_at": "2026-08-13T09:01:00+08:00",
                "origin": "PVG",
                "destination": "KIX",
                "notification_goals": {"email": "missing@example.com"},
            },
            {
                "_index": 3,
                "created_at": "2026-08-13T09:02:00+08:00",
                "origin": "PVG",
                "destination": "KIX",
                "notification_goals": {"method": "email", "email": ""},
            },
            {
                "_index": 4,
                "created_at": "2026-08-13T09:03:00+08:00",
                "origin": "PVG",
                "destination": "KIX",
                "notification_goals": {"method": "pushplus", "email": ""},
            },
            {
                "_index": 5,
                "created_at": "2026-08-01T09:00:00+08:00",
                "origin": "PVG",
                "destination": "KIX",
                "notification_goals": {"method": "pushplus", "email": ""},
            },
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
            before = path.read_bytes()
            issues = scan_notification_config_issues(path)
            after = path.read_bytes()

        self.assertEqual(before, after)
        self.assertEqual([item["_index"] for item in issues], [2, 3, 4])
        self.assertIn("method缺失", issues[0]["issue"])
        self.assertIn("邮箱缺失", issues[1]["issue"])
        self.assertIn("旧默认", issues[2]["issue"])


if __name__ == "__main__":
    unittest.main()
