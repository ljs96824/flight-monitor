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
SECTION_IDS = (
    "section-where",
    "section-when",
    "section-who",
    "section-budget",
    "section-flight-preferences",
    "section-notifications",
)


class FormUxPhase1Test(unittest.TestCase):
    def setUp(self):
        web_form.app.config.update(TESTING=True)
        self.client = web_form.app.test_client()

    def test_quick_and_full_pages_expose_the_two_page_contract(self):
        quick = self.client.get("/").get_data(as_text=True)
        full = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('data-page-mode="quick"', quick)
        self.assertIn('name="monitor_mode" value="quick"', quick)
        self.assertIn('href="/settings"', quick)
        self.assertIn("创建监控", quick)
        self.assertIn('data-page-mode="full"', full)
        self.assertIn('name="monitor_mode" value="precise"', full)
        for section_id in SECTION_IDS:
            self.assertIn(f'id="{section_id}"', full)
            self.assertIn(f'href="#{section_id}"', full)
        self.assertNotIn("station-breadcrumbs", quick + full)
        self.assertNotIn("scenario-preset-chips", quick + full)

    def test_full_page_anchor_directory_and_summaries_use_six_station_ownership(self):
        full = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('class="anchor-directory"', full)
        self.assertEqual(full.count('data-form-section="'), 6)
        for section_id in SECTION_IDS:
            station = section_id.removeprefix("section-").replace("-", "_")
            self.assertIn(f'data-section-summary="{station}"', full)
            self.assertIn(f'data-confirm-edit="{section_id}"', full)

    def test_business_and_cabin_fields_live_once_on_the_full_page(self):
        full = self.client.get("/settings").get_data(as_text=True)
        for field in (
            "trip_natures",
            "meeting_start",
            "meeting_end",
            "cabin_policy",
            "cabin_arrangement",
            "business_seats",
            "economy_seats",
        ):
            self.assertEqual(full.count(f'name="{field}"'), 1, field)
        self.assertNotIn("canonical-preference-chips", full)
        self.assertNotIn("openWizardStation", full)

    def test_conditional_families_are_limited_to_the_three_approved_contracts(self):
        quick = self.client.get("/").get_data(as_text=True)
        full = self.client.get("/settings").get_data(as_text=True)
        html = quick + full
        self.assertIn('data-visibility-contract="passenger-profile"', html)
        self.assertIn('data-visibility-contract="notification-email"', html)
        self.assertIn('data-visibility-contract="business-scenario"', html)
        self.assertNotIn("data-show-if", html)
        self.assertNotIn("data-advanced-depth", html)
        self.assertEqual(quick.count("element.hidden ="), 3)
        self.assertEqual(full.count("element.hidden ="), 3)

    def test_defaults_preview_uses_local_default_engine_and_never_saves(self):
        form = json.loads(FIXTURE.read_text(encoding="utf-8"))["scenarios"][
            "family_elderly_tourism"
        ]["form_input"]
        with tempfile.TemporaryDirectory() as tmpdir:
            original = web_form.SUBSCRIPTIONS_PATH
            web_form.SUBSCRIPTIONS_PATH = Path(tmpdir) / "subscriptions.json"
            try:
                response = self.client.post("/defaults_preview", data=form)
            finally:
                web_form.SUBSCRIPTIONS_PATH = original
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(
            set(payload["station_summaries"]),
            {"where", "when", "who", "budget", "flight_preferences", "notifications"},
        )
        self.assertEqual(
            payload["constraint_summary_text"],
            format_constraint_summary(payload["constraint_summary"]),
        )
        self.assertTrue(payload["defaults_applied"])
        self.assertTrue(payload["chips"])
        self.assertFalse((Path(tmpdir) / "subscriptions.json").exists())

    def test_eight_post_baselines_remain_field_for_field_equal(self):
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
                            main.normalize_subscription(saved),
                            case["normalized_subscription"],
                            name,
                        )
            finally:
                web_form.SUBSCRIPTIONS_PATH = original

    def test_edit_projection_roundtrip_remains_normalization_idempotent(self):
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

        original = web_form.SUBSCRIPTIONS_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            saved = cases["solo_minimal"]["normalized_subscription"]
            path.write_text(json.dumps([saved], ensure_ascii=False), encoding="utf-8")
            web_form.SUBSCRIPTIONS_PATH = path
            try:
                response = self.client.get("/?edit=0")
            finally:
                web_form.SUBSCRIPTIONS_PATH = original
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/settings?edit=0"))

    def test_price_hint_and_location_rejection_routes_remain_present(self):
        with patch.object(
            web_form,
            "load_calendar",
            side_effect=AssertionError("部分地点禁止读日历"),
        ):
            response = self.client.get("/price_hint?origin=SHA&dest=北")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["has_data"])
        bad = self.client.post(
            "/defaults_preview",
            data={"origin_select": "PVG", "destination": "不存在城市"},
        )
        self.assertEqual(bad.status_code, 200)
        self.assertFalse(bad.get_json()["ok"])
        self.assertIn("无法识别目的地", bad.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
