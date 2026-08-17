import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from form_structure import (
    FORM_STATIONS,
    REQUIRED_STATION_COUNT,
    summarize_optional_sections,
)
import main
import web_form
from tests.form_fixture_contract import without_storage_identity


FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "form_normalization_baseline_v1.json"


class FormUxQuickFinishTest(unittest.TestCase):
    def setUp(self):
        web_form.app.config.update(TESTING=True)
        self.client = web_form.app.test_client()

    def test_four_core_stations_and_two_full_settings_stations_remain_declared(self):
        self.assertEqual(REQUIRED_STATION_COUNT, 4)
        self.assertEqual(
            [station["id"] for station in FORM_STATIONS],
            ["where", "when", "who", "budget", "flight_preferences", "notifications"],
        )

    def test_optional_section_summaries_are_generated_from_current_values(self):
        defaults = summarize_optional_sections({})
        self.assertEqual(defaults["feasibility"], "已按场景预设")
        self.assertIn("时间不限", defaults["flight_preferences"])
        self.assertEqual(defaults["notifications"], "邮箱+PushPlus · 重要变化")

        custom = summarize_optional_sections(
            {
                "outbound_set_off": "07:15",
                "user_transport_min": "45",
                "transport_margin_mode": "tight",
                "time_preference": "daytime",
                "no_late_arrival": "true",
                "transfer_policy": "direct_only",
                "baggage": "required",
                "notification_method": "email",
                "notification_frequency": "daily_digest",
            }
        )
        self.assertIn("07:15", custom["feasibility"])
        self.assertIn("车程45分钟", custom["feasibility"])
        self.assertIn("白天优先", custom["flight_preferences"])
        self.assertIn("邮箱", custom["notifications"])

    def test_retired_optional_expansion_state_is_absent_from_both_pages(self):
        html = (
            self.client.get("/").get_data(as_text=True)
            + self.client.get("/settings").get_data(as_text=True)
        )
        for marker in (
            "data-optional-section",
            "data-edit-expanded",
            "data-default-collapsed",
            "optional-section-attention",
            "optional-settings-toggle",
        ):
            self.assertNotIn(marker, html)

    def test_quick_page_is_the_static_short_path_and_links_to_full_settings(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-page-mode="quick"', html)
        self.assertIn('name="form_page" value="quick"', html)
        self.assertIn("创建监控", html)
        self.assertIn("时间/航司/行李/提醒等已按场景预设", html)
        self.assertIn('href="/settings"', html)
        self.assertNotIn("scenario-preset-chips", html)
        self.assertNotIn("quick-finish-button", html)

    def test_quick_and_full_pages_share_the_server_defaults_preview_pipeline(self):
        quick = self.client.get("/").get_data(as_text=True)
        full = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("/defaults_preview", quick)
        self.assertIn("/defaults_preview", full)
        self.assertIn("scheduleSummary", quick)
        self.assertIn("scheduleSummary", full)

    def test_edit_entry_redirects_to_full_static_page_with_all_sections_visible(self):
        subscription = {
            "monitor_mode": "precise",
            "origin": "PVG",
            "destination": "KIX",
            "route_type": "international",
            "round_trip": False,
            "depart_date": "2026-12-01",
            "hard_constraints": {
                "transfer_policy": "reasonable",
                "baggage": "required",
                "lcc_policy": "exclude_lcc",
            },
            "soft_preferences": {
                "time_preference": "unlimited",
                "refund_flexibility": "preferred",
                "airline_policy": "any",
                "passengers": {"adult": 1, "child": 0, "elderly": 0, "infant": 0},
            },
            "notification_goals": {
                "main_goal": "buy_timing",
                "method": "both",
                "frequency": "important_only",
            },
        }
        original = web_form.SUBSCRIPTIONS_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            path.write_text(json.dumps([subscription], ensure_ascii=False), encoding="utf-8")
            web_form.SUBSCRIPTIONS_PATH = path
            try:
                redirect_response = self.client.get("/?edit=0")
                full = self.client.get("/settings?edit=0").get_data(as_text=True)
            finally:
                web_form.SUBSCRIPTIONS_PATH = original
        self.assertEqual(redirect_response.status_code, 302)
        self.assertTrue(redirect_response.headers["Location"].endswith("/settings?edit=0"))
        self.assertEqual(full.count('data-form-section="'), 6)
        self.assertNotIn("hidden-section", full)

    def test_quick_and_full_path_markers_preserve_the_same_normalized_payload(self):
        case = next(iter(json.loads(FIXTURE.read_text(encoding="utf-8"))["scenarios"].values()))
        original = web_form.SUBSCRIPTIONS_PATH

        def post_once(page_mode):
            with tempfile.TemporaryDirectory() as tmpdir:
                web_form.SUBSCRIPTIONS_PATH = Path(tmpdir) / "subscriptions.json"
                payload = dict(case["form_input"])
                payload["form_page"] = page_mode
                with patch.object(web_form, "start_background_collection"):
                    response = self.client.post("/subscribe", data=payload)
                self.assertEqual(response.status_code, 302)
                saved = json.loads(
                    web_form.SUBSCRIPTIONS_PATH.read_text(encoding="utf-8")
                )[0]
                return without_storage_identity(
                    main.normalize_subscription(saved)
                )

        try:
            quick_result = post_once("quick")
            full_result = post_once("full")
        finally:
            web_form.SUBSCRIPTIONS_PATH = original

        self.assertEqual(quick_result, full_result)
        self.assertEqual(quick_result, case["normalized_subscription"])

    def test_eight_scenario_normalization_fixture_remains_unchanged(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        original = web_form.SUBSCRIPTIONS_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            web_form.SUBSCRIPTIONS_PATH = Path(tmpdir) / "subscriptions.json"
            try:
                with patch.object(web_form, "start_background_collection"):
                    for name, case in fixture["scenarios"].items():
                        response = self.client.post("/subscribe", data=case["form_input"])
                        self.assertEqual(response.status_code, 302, name)
                        saved = json.loads(
                            web_form.SUBSCRIPTIONS_PATH.read_text(encoding="utf-8")
                        )[-1]
                        self.assertEqual(
                            without_storage_identity(
                                main.normalize_subscription(saved)
                            ),
                            case["normalized_subscription"],
                            name,
                        )
            finally:
                web_form.SUBSCRIPTIONS_PATH = original


if __name__ == "__main__":
    unittest.main()
