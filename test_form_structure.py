import unittest

from analyzer import apply_default_rules
from form_structure import (
    ADVANCED_FIELD_NAMES,
    FIELD_OWNERS,
    FORM_STATIONS,
    build_default_chips,
    derive_monitor_mode,
    summarize_stations,
    VISIBILITY_RULES,
    visible_field_names,
)


class FormStructureTest(unittest.TestCase):
    def test_six_station_contract_and_unique_field_ownership(self):
        self.assertEqual(
            [station["id"] for station in FORM_STATIONS],
            ["where", "when", "who", "budget", "flight_preferences", "notifications"],
        )
        self.assertEqual([station["number"] for station in FORM_STATIONS], list(range(1, 7)))
        flattened = [field for station in FORM_STATIONS for field in station["fields"]]
        self.assertEqual(len(flattened), len(set(flattened)))
        self.assertEqual(set(flattened), set(FIELD_OWNERS))
        self.assertEqual(FIELD_OWNERS["outbound_set_off"], "who")
        self.assertEqual(FIELD_OWNERS["lcc_policy"], "flight_preferences")
        self.assertEqual(FIELD_OWNERS["trip_natures"], "who")
        self.assertEqual(FIELD_OWNERS["user_level"], "who")
        self.assertEqual(FIELD_OWNERS["price_sensitivity"], "flight_preferences")

    def test_visibility_matrix_for_family_business_same_day_and_team(self):
        family = visible_field_names(
            {"travel_scenario": ["family"], "child_count": 1, "elderly_count": 0}
        )
        self.assertIn("child_type", family)
        rule_map = {rule["id"]: rule for rule in VISIBILITY_RULES}
        self.assertTrue(all(rule.get("when") for rule in rule_map.values()))
        self.assertIn("business_context", rule_map)
        self.assertNotIn("elderly_condition", family)

        elderly = visible_field_names(
            {"travel_scenario": ["elderly"], "child_count": 0, "elderly_count": 1}
        )
        self.assertIn("elderly_condition", elderly)
        self.assertNotIn("child_type", elderly)

        same_day = visible_field_names({"same_day_round_trip": True})
        self.assertTrue(
            {"business_start", "business_end", "meeting_location", "meeting_importance"}
            <= same_day
        )

        team = visible_field_names(
            {"travel_scenario": ["business"], "trip_natures": ["team_building"]}
        )
        self.assertTrue(
            {"team_passenger_count", "team_date_flexibility", "same_flight_required"}
            <= team
        )

    def test_hidden_condition_fields_are_not_reported_visible(self):
        visible = visible_field_names(
            {
                "round_trip": False,
                "same_day_round_trip": False,
                "travel_scenario": ["personal"],
                "route_type": "international",
            }
        )
        self.assertNotIn("return_date", visible)
        self.assertNotIn("business_start", visible)
        self.assertNotIn("invoice_needed", visible)
        self.assertNotIn("team_passenger_count", visible)

        business_quick = visible_field_names(
            {"monitor_mode": "quick", "travel_scenario": ["business"]}
        )
        business_precise = visible_field_names(
            {"monitor_mode": "precise", "travel_scenario": ["business"]}
        )
        self.assertNotIn("invoice_needed", business_quick)
        self.assertIn("invoice_needed", business_precise)

    def test_monitor_mode_derivation_three_states(self):
        self.assertEqual(derive_monitor_mode(), "quick")
        self.assertEqual(derive_monitor_mode(advanced_opened=True), "precise")
        self.assertEqual(derive_monitor_mode(stored_mode="precise", editing=True), "precise")
        self.assertEqual(derive_monitor_mode(stored_mode="quick", editing=True), "quick")
        self.assertTrue({"time_preference", "lcc_policy", "cabin_policy"} <= ADVANCED_FIELD_NAMES)

    def test_station_summaries_are_python_generated(self):
        summaries = summarize_stations(
            {
                "origin_select": "PVG",
                "destination": "KIX",
                "round_trip": "true",
                "depart_date": "2026-12-01",
                "return_date": "2026-12-06",
                "adult_count": "2",
                "child_count": "1",
                "elderly_count": "0",
                "infant_count": "0",
                "travel_scenario": ["tourism", "family"],
                "max_budget": "8000",
                "max_budget_scope": "per_person",
                "transfer_policy": "direct_only",
                "time_preference": "no_redeye",
                "notification_method": "email",
                "notification_frequency": "important_only",
            }
        )
        self.assertEqual(set(summaries), {station["id"] for station in FORM_STATIONS})
        self.assertIn("PVG", summaries["where"])
        self.assertIn("2026-12-01", summaries["when"])
        self.assertIn("2成人", summaries["who"])
        self.assertIn("1儿童", summaries["who"])
        self.assertIn("单人", summaries["budget"])
        self.assertIn("必须直飞", summaries["flight_preferences"])
        self.assertIn("邮箱", summaries["notifications"])

    def test_default_chips_read_apply_default_rules_output(self):
        defaulted = apply_default_rules(
            {
                "monitor_mode": "quick",
                "route_type": "international",
                "soft_preferences": {
                    "travel_scenarios": ["family", "tourism"],
                    "passengers": {"adult": 2, "child": 1, "elderly": 0, "infant": 0},
                },
                "hard_constraints": {},
                "notification_goals": {},
            }
        )
        chips = build_default_chips(defaulted)
        labels = [chip["label"] for chip in chips]
        self.assertIn("不红眼", labels)
        self.assertIn("需托运", labels)
        self.assertTrue(all({"field", "value", "label", "selected"} <= set(chip) for chip in chips))
        self.assertEqual(
            {chip["source"] for chip in chips},
            {"defaults_applied"},
        )


if __name__ == "__main__":
    unittest.main()
