from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.datastructures import MultiDict

import analyzer
import web_form
from form_pages import VISIBILITY_CONTRACTS, build_form_page_context


FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "form_normalization_baseline_v1.json"


def _field_by_name(page: dict, name: str) -> dict:
    for group in page.get("groups") or []:
        for field in group.get("fields") or []:
            if field["name"] == name:
                return field
    for section in page.get("sections") or []:
        for concept in section.get("concepts") or []:
            for field in [*(concept.get("hidden_fields") or []), *(concept.get("fields") or [])]:
                if field["name"] == name:
                    return field
    raise AssertionError(f"未找到字段: {name}")


class FormUx34ParallelDimensionsTest(unittest.TestCase):
    def setUp(self):
        web_form.app.config.update(TESTING=True)

    def test_scenarios_and_companion_constraints_are_parallel_multi_selects(self):
        for mode in ("quick", "full"):
            page = build_form_page_context(mode)
            self.assertEqual(_field_by_name(page, "travel_scenario")["type"], "multi", mode)
            self.assertEqual(_field_by_name(page, "companion_constraints")["type"], "multi", mode)

    def test_quick_submission_preserves_both_parallel_lists(self):
        form = MultiDict(
            [
                ("monitor_mode", "quick"),
                ("origin_select", "PVG"),
                ("destination", "KIX"),
                ("depart_date", "2026-12-01"),
                ("round_trip", "false"),
                ("passenger_count", "4"),
                ("travel_scenario", "tourism"),
                ("travel_scenario", "family"),
                ("travel_scenario", "elderly"),
                ("companion_constraints", "direct_preferred"),
                ("companion_constraints", "no_redeye"),
            ]
        )
        subscription = web_form.build_subscription(form)
        soft = subscription["soft_preferences"]
        self.assertEqual(soft["travel_scenarios"], ["tourism", "family", "elderly"])
        self.assertEqual(soft["companion_constraints"], ["direct_preferred", "no_redeye"])
        self.assertEqual(soft["companions"], "multiple")

    def test_passenger_mix_maps_to_single_companions_enum_and_complete_basis(self):
        profile = analyzer.build_travel_profile(
            {
                "travel_scenarios": ["tourism", "family", "elderly"],
                "passengers": {"adult": 2, "child": 1, "elderly": 1, "infant": 0},
                "companion_constraints": ["direct_preferred", "no_redeye"],
            }
        )
        basis = analyzer.build_recommendation_basis(profile, [])
        self.assertEqual(profile["travelers"], "with_elderly_child")
        self.assertEqual(basis["scenario_labels"], ["旅游", "家庭/亲子", "有老人同行"])
        self.assertEqual(" + ".join(basis["scenario_labels"]), "旅游 + 家庭/亲子 + 有老人同行")
        self.assertEqual(profile["time"], "high")

    def test_route_type_is_read_only_badge_and_price_hint_returns_auto_classification(self):
        for mode in ("quick", "full"):
            page = build_form_page_context(mode)
            self.assertEqual(_field_by_name(page, "route_type")["type"], "hidden", mode)
        html = web_form.app.test_client().get("/settings").get_data(as_text=True)
        self.assertIn('data-route-type-badge="true"', html)
        self.assertNotIn('<select id="field-route-type"', html)
        with patch.object(web_form, "load_calendar", return_value={}):
            response = web_form.app.test_client().get("/price_hint?origin=PVG&dest=KIX")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["route_type"], "international")
        self.assertEqual(payload["route_type_label"], "国际")

    def test_business_group_is_third_visibility_contract_and_edit_opens_it(self):
        self.assertEqual(
            VISIBILITY_CONTRACTS,
            frozenset({"passenger-profile", "notification-email", "business-scenario"}),
        )
        default_groups = {
            item["id"]: item for item in build_form_page_context("full")["secondary_groups"]
        }
        business_default = default_groups["business-travel"]
        self.assertEqual(business_default["visibility"], "business-scenario")
        self.assertTrue(business_default["hidden"])

        edit_groups = {
            item["id"]: item
            for item in build_form_page_context(
                "full", {"travel_scenario": ["tourism", "business"]}, edit_index=72
            )["secondary_groups"]
        }
        business_edit = edit_groups["business-travel"]
        self.assertFalse(business_edit["hidden"])
        self.assertTrue(business_edit["open"])

        legacy_edit_groups = {
            item["id"]: item
            for item in build_form_page_context(
                "full", {"travel_scenario": ["tourism"], "business_start": "10:30"}, edit_index=72
            )["secondary_groups"]
        }
        legacy_business = legacy_edit_groups["business-travel"]
        self.assertFalse(legacy_business["hidden"])
        self.assertTrue(legacy_business["open"])

    def test_tenth_fixture_freezes_multi_scenario_elderly_child_contract(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))["scenarios"]
        self.assertEqual(len(fixture), 10)
        case = fixture["parallel_scenarios_elderly_child"]
        soft = case["normalized_subscription"]["soft_preferences"]
        self.assertEqual(soft["travel_scenarios"], ["tourism", "family", "elderly"])
        self.assertEqual(soft["companions"], "with_elderly_child")


if __name__ == "__main__":
    unittest.main()
