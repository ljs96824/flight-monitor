import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.datastructures import MultiDict

from constraint_summary import format_constraint_summary
from form_structure import subscription_to_form_values
import main
import web_form


FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "form_normalization_baseline_v1.json"


class FormUxPhase1Test(unittest.TestCase):
    def setUp(self):
        web_form.app.config.update(TESTING=True)

    def test_template_has_six_stations_and_declarative_metadata(self):
        response = web_form.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        template = response.get_data(as_text=True)
        for number, station_id in enumerate(
            ["where", "when", "who", "budget", "flight_preferences", "notifications"],
            start=1,
        ):
            self.assertIn(f'data-step="{number}"', template)
            self.assertIn(f'data-station-id="{station_id}"', template)
            self.assertIn(f'data-station-summary="{station_id}"', template)
        self.assertIn('data-field-owners=', template)
        self.assertIn('data-visibility-rules=', template)
        self.assertIn('data-advanced-depth', template)
        self.assertIn('id="scenario-preset-chips"', template)
        self.assertIn('id="constraint-summary-preview"', template)
        self.assertIn('type="hidden" id="monitor_mode" name="monitor_mode" value="quick"', template)
        self.assertNotIn('type="radio" name="monitor_mode"', template)
        self.assertNotIn('切换精准模式', template)
        self.assertNotIn('快速模式只需', template)
        self.assertNotIn('展开“精准监控”', template)
        self.assertGreaterEqual(template.count('await refreshDefaultsPreview();'), 4)

    def test_mobile_navigation_and_summary_edits_use_six_station_ownership(self):
        template = web_form.app.test_client().get("/").get_data(as_text=True)
        self.assertIn("function stationNumberForField", template)
        self.assertIn("step: stationNumberForField('depart_date')", template)
        self.assertIn("trip_dates: {step: stationNumberForField('depart_date')", template)
        self.assertIn("price_strategy: {step: stationNumberForField('price_strategy')", template)
        self.assertIn("notification: {step: stationNumberForField('notification_method')", template)

    def test_relocated_business_cabin_visibility_and_chip_depth_label_stay_consistent(self):
        template = web_form.app.test_client().get("/").get_data(as_text=True)
        self.assertIn(
            'id="business-cabin-fields" data-form-section="flight_preferences" data-show-if="trip_natures=business|meeting|team_building"',
            template,
        )
        self.assertIn('data-breadcrumb-station="5"', template)
        self.assertIn(
            "openWizardStation(requiredStationCount + 1)",
            template,
        )

    def test_conditional_families_declare_their_real_station_owners(self):
        template = web_form.app.test_client().get("/").get_data(as_text=True)
        self.assertIn(
            'id="domestic-invoice-trigger" data-advanced-depth', template
        )
        self.assertIn(
            'id="business-rules-module" data-advanced-depth data-form-section="who"', template
        )
        self.assertIn(
            'id="business-meeting-fields" data-advanced-depth data-form-section="when"', template
        )
        self.assertIn(
            'id="business-budget-fields" data-advanced-depth data-form-section="budget"', template
        )
        self.assertIn(
            'id="advanced-alert-settings" data-advanced-depth data-form-section="notifications"', template
        )
        self.assertNotIn("const scenarioDefaults", template)
        self.assertIn("control.dataset.explicit = 'true'", template)
        self.assertIn(
            "document.querySelectorAll('[data-advanced-depth] input", template
        )
        self.assertIn("function visibilityRuleMatches", template)
        self.assertNotIn("if (name === 'has_child' || name === 'has_elderly')", template)
        self.assertNotIn("if (name === 'business_context')", template)

    def test_defaults_preview_uses_local_default_engine_and_never_saves(self):
        form = json.loads(FIXTURE.read_text(encoding="utf-8"))["scenarios"][
            "family_elderly_tourism"
        ]["form_input"]
        with tempfile.TemporaryDirectory() as tmpdir:
            original = web_form.SUBSCRIPTIONS_PATH
            web_form.SUBSCRIPTIONS_PATH = Path(tmpdir) / "subscriptions.json"
            try:
                client = web_form.app.test_client()
                response = client.post("/defaults_preview", data=form)
            finally:
                web_form.SUBSCRIPTIONS_PATH = original
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(set(payload["station_summaries"]), {
            "where", "when", "who", "budget", "flight_preferences", "notifications"
        })
        self.assertIn("constraint_summary_text", payload)
        self.assertEqual(
            payload["constraint_summary_text"],
            format_constraint_summary(payload["constraint_summary"]),
        )
        self.assertTrue(payload["defaults_applied"])
        self.assertTrue(payload["chips"])
        self.assertIn("constraint_summary", payload)
        self.assertFalse((Path(tmpdir) / "subscriptions.json").exists())

    def test_five_post_baselines_remain_field_for_field_equal(self):
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

    def test_edit_hydration_roundtrip_is_normalization_idempotent_for_all_scenarios(self):
        cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["scenarios"]
        for name, case in cases.items():
            values = subscription_to_form_values(case["normalized_subscription"])
            items = []
            for field, value in values.items():
                if isinstance(value, (list, tuple)):
                    items.extend((field, str(item)) for item in value)
                elif value is True:
                    items.append((field, "true"))
                elif value not in (False, None):
                    items.append((field, str(value)))
            rebuilt = web_form.build_subscription(MultiDict(items))
            self.assertEqual(
                main.normalize_subscription(rebuilt),
                case["normalized_subscription"],
                name,
            )
        template = web_form.app.test_client().get("/").get_data(as_text=True)
        self.assertIn("function hydrateFormValues", template)
        self.assertIn("hydrateFormValues(data)", template)

    def test_price_hint_and_location_rejection_routes_remain_present(self):
        client = web_form.app.test_client()
        with patch.object(web_form, "load_calendar", side_effect=AssertionError("部分地点禁止读日历")):
            response = client.get("/price_hint?origin=SHA&dest=北")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["has_data"])
        bad = client.post(
            "/defaults_preview",
            data={"origin_select": "PVG", "destination": "不存在城市"},
        )
        self.assertEqual(bad.status_code, 200)
        self.assertFalse(bad.get_json()["ok"])
        self.assertIn("无法识别目的地", bad.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
