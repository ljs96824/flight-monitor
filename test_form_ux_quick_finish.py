import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from form_structure import (
    FORM_STATIONS,
    REQUIRED_STATION_COUNT,
    edit_expanded_sections,
    form_structure_payload,
    summarize_optional_sections,
)
import main
import web_form


FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "form_normalization_baseline_v1.json"


class FormUxQuickFinishTest(unittest.TestCase):
    def setUp(self):
        web_form.app.config.update(TESTING=True)

    def test_four_required_stations_and_two_optional_stations_are_declared(self):
        self.assertEqual(REQUIRED_STATION_COUNT, 4)
        self.assertEqual(
            [station["depth"] for station in FORM_STATIONS],
            ["required", "required", "required", "required", "optional", "optional"],
        )
        self.assertEqual(
            [station["default_collapsed"] for station in FORM_STATIONS],
            [False, False, False, False, True, True],
        )
        payload = form_structure_payload()
        self.assertEqual(payload["required_station_count"], 4)
        self.assertEqual(
            [item["id"] for item in payload["optional_sections"]],
            ["feasibility", "flight_preferences", "notifications"],
        )

    def test_optional_section_summaries_are_generated_from_current_values(self):
        defaults = summarize_optional_sections({})
        self.assertEqual(defaults["feasibility"], "已按场景预设")
        self.assertIn("时间不限", defaults["flight_preferences"])
        self.assertIn("PushPlus", defaults["notifications"])

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
        self.assertIn("紧凑冗余", custom["feasibility"])
        self.assertIn("白天优先", custom["flight_preferences"])
        self.assertIn("不深夜到达", custom["flight_preferences"])
        self.assertIn("邮箱", custom["notifications"])
        self.assertIn("每日摘要", custom["notifications"])

    def test_edit_expansion_only_opens_sections_with_non_default_values(self):
        self.assertEqual(
            edit_expanded_sections({"monitor_mode": "precise"}, editing=True),
            [],
        )
        self.assertEqual(
            edit_expanded_sections(
                {"monitor_mode": "precise", "outbound_set_off": "07:15"},
                editing=True,
            ),
            ["feasibility"],
        )
        self.assertEqual(
            edit_expanded_sections(
                {"monitor_mode": "precise", "lcc_policy": "exclude_lcc"},
                editing=True,
            ),
            ["flight_preferences"],
        )
        self.assertEqual(
            edit_expanded_sections(
                {"monitor_mode": "precise", "notification_method": "email"},
                editing=True,
            ),
            ["notifications"],
        )
        self.assertEqual(
            edit_expanded_sections(
                {"monitor_mode": "quick", "lcc_policy": "exclude_lcc"},
                editing=True,
            ),
            [],
        )

    def test_template_promises_four_steps_and_places_quick_finish_before_optional_settings(self):
        template = web_form.app.test_client().get("/").get_data(as_text=True)
        self.assertIn("必填4步 · 其余可选", template)
        self.assertIn('data-required-stations="4"', template)
        self.assertEqual(template.count('data-station-depth="required"'), 4)
        self.assertEqual(template.count('data-station-depth="optional"'), 2)
        self.assertEqual(template.count('data-default-collapsed="true"'), 2)
        self.assertIn('id="quick-finish-button"', template)
        self.assertIn("✓ 完成创建(使用下方预设)", template)
        self.assertIn('id="optional-settings-toggle"', template)
        self.assertIn("想细调时间/航司/提醒?展开可选设置", template)
        self.assertIn('id="feasibility-optional-section"', template)
        self.assertIn('data-optional-section="feasibility"', template)

        quick_finish = template.index('id="quick-finish-button"')
        chips = template.index('id="scenario-preset-chips"')
        optional_toggle = template.index('id="optional-settings-toggle"')
        self.assertLess(quick_finish, chips)
        self.assertLess(chips, optional_toggle)

    def test_quick_finish_and_full_flow_share_the_same_preview_pipeline(self):
        template = web_form.app.test_client().get("/").get_data(as_text=True)
        self.assertIn("async function showSubmissionPreview()", template)
        self.assertIn(
            "quickFinishButton?.addEventListener('click', showSubmissionPreview)",
            template,
        )
        self.assertIn(
            "previewButton.addEventListener('click', showSubmissionPreview)",
            template,
        )

    def test_edit_route_marks_only_non_default_optional_section_expanded(self):
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
                "method": "pushplus",
                "frequency": "important_only",
            },
        }
        original = web_form.SUBSCRIPTIONS_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            path.write_text(json.dumps([subscription], ensure_ascii=False), encoding="utf-8")
            web_form.SUBSCRIPTIONS_PATH = path
            try:
                template = web_form.app.test_client().get("/?edit=0").get_data(as_text=True)
            finally:
                web_form.SUBSCRIPTIONS_PATH = original

        self.assertIn(
            'data-optional-section="flight_preferences" data-edit-expanded="true"',
            template,
        )
        self.assertIn(
            'data-optional-section="feasibility" data-edit-expanded="false"',
            template,
        )
        self.assertIn(
            'data-optional-section="notifications" data-edit-expanded="false"',
            template,
        )
        self.assertIn("optional-section-attention", template)

    def test_quick_finish_and_full_path_post_the_same_normalized_payload(self):
        case = next(
            iter(
                json.loads(FIXTURE.read_text(encoding="utf-8"))["scenarios"].values()
            )
        )
        original = web_form.SUBSCRIPTIONS_PATH

        def post_once():
            with tempfile.TemporaryDirectory() as tmpdir:
                web_form.SUBSCRIPTIONS_PATH = Path(tmpdir) / "subscriptions.json"
                with patch.object(web_form, "start_background_collection"):
                    response = web_form.app.test_client().post(
                        "/subscribe",
                        data=case["form_input"],
                    )
                self.assertEqual(response.status_code, 302)
                saved = json.loads(
                    web_form.SUBSCRIPTIONS_PATH.read_text(encoding="utf-8")
                )[0]
                return main.normalize_subscription(saved)

        try:
            quick_result = post_once()
            full_result = post_once()
        finally:
            web_form.SUBSCRIPTIONS_PATH = original

        self.assertEqual(quick_result, full_result)
        self.assertEqual(quick_result, case["normalized_subscription"])

    def test_phase_one_five_scenario_normalization_fixture_remains_unchanged(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        original = web_form.SUBSCRIPTIONS_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            web_form.SUBSCRIPTIONS_PATH = Path(tmpdir) / "subscriptions.json"
            try:
                with patch.object(web_form, "start_background_collection"):
                    client = web_form.app.test_client()
                    for name, case in fixture["scenarios"].items():
                        response = client.post("/subscribe", data=case["form_input"])
                        self.assertEqual(response.status_code, 302, name)
                        saved = json.loads(
                            web_form.SUBSCRIPTIONS_PATH.read_text(encoding="utf-8")
                        )[-1]
                        self.assertEqual(
                            main.normalize_subscription(saved),
                            case["normalized_subscription"],
                            name,
                        )
            finally:
                web_form.SUBSCRIPTIONS_PATH = original


if __name__ == "__main__":
    unittest.main()
