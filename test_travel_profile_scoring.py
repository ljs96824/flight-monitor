import unittest

from analyzer import build_recommendation_basis, build_travel_profile, calc_final_score


class TravelProfileScoringTest(unittest.TestCase):
    def test_family_profile_overrides_for_child_travelers(self):
        profile = build_travel_profile(
            {
                "travel_scenario": "family",
                "travelers": "with_child",
            }
        )

        self.assertEqual(profile["price"], "medium")
        self.assertEqual(profile["comfort"], "high")
        self.assertEqual(profile["risk_averse"], "high")
        self.assertEqual(profile["baggage"], "high")

    def test_multiple_scenarios_merge_highest_dimension_requirements(self):
        profile = build_travel_profile(
            {
                "travel_scenarios": ["tourism", "family"],
                "travelers": "solo",
            }
        )

        self.assertEqual(profile["scenarios"], ["tourism", "family"])
        self.assertEqual(profile["scenario"], "tourism")
        self.assertEqual(profile["price"], "high")
        self.assertEqual(profile["comfort"], "high")
        self.assertEqual(profile["risk_averse"], "high")
        self.assertEqual(profile["baggage"], "high")
        self.assertIn("tourism+family", profile["scenario_combo"])

    def test_legacy_single_scenario_string_is_normalized_to_list(self):
        profile = build_travel_profile({"travel_scenarios": "business"})

        self.assertEqual(profile["scenarios"], ["business"])
        self.assertEqual(profile["scenario"], "business")
        self.assertEqual(profile["time"], "high")
        self.assertEqual(profile["risk_averse"], "high")

    def test_precise_passenger_counts_drive_companion_profile(self):
        profile = build_travel_profile(
            {
                "travel_purposes": ["tourism"],
                "passengers": {"adult": 2, "child": 1, "elderly": 1, "infant": 1},
            }
        )

        self.assertEqual(profile["scenarios"], ["tourism"])
        self.assertEqual(profile["travelers"], "with_elderly_child")
        self.assertEqual(profile["passenger_count"], 5)
        self.assertEqual(profile["comfort"], "high")
        self.assertEqual(profile["risk_averse"], "high")
        self.assertEqual(profile["baggage"], "high")
        self.assertEqual(profile["stock_check"], "high")
        self.assertTrue(profile["infant"])

    def test_legacy_companion_field_is_converted_to_passenger_shape(self):
        profile = build_travel_profile({"travel_scenarios": "tourism", "companions": "with_child"})

        self.assertEqual(profile["travelers"], "with_child")
        self.assertEqual(profile["passengers"], {"adult": 1, "child": 1, "elderly": 0, "infant": 0})
        self.assertEqual(profile["passenger_count"], 2)
        self.assertEqual(profile["comfort"], "high")
        self.assertEqual(profile["stock_check"], "high")

    def test_infant_count_triggers_child_profile(self):
        profile = build_travel_profile({"passengers": {"adult": 2, "child": 0, "elderly": 0, "infant": 1}})

        self.assertEqual(profile["travelers"], "with_child")
        self.assertEqual(profile["passenger_count"], 3)
        self.assertTrue(profile["infant"])
        self.assertEqual(profile["risk_averse"], "high")

    def test_recommendation_basis_uses_same_combined_profile(self):
        profile = build_travel_profile({"travel_scenarios": ["tourism", "family"]})
        basis = build_recommendation_basis(profile)

        self.assertEqual(basis["scenarios"], ["tourism", "family"])
        self.assertEqual(basis["scenario_labels"], ["旅游", "家庭/亲子"])
        self.assertIn("价格敏感", " ".join(basis["applied_rules"]))
        self.assertIn("白天直飞", " ".join(basis["applied_rules"]))
        self.assertIn("孩子安全舒适", basis["conflict_note"])

    def test_family_profile_prefers_reliable_comfort_over_cheapest(self):
        stable = {
            "price": 6200,
            "stops": 0,
            "segments": [{"dep_time": "2026-10-01 09:00", "arr_time": "2026-10-01 12:00"}],
            "total_duration_min": 180,
            "execution_risk": {"score": 5},
            "fare_rules": {"baggage": {"checked_pieces": 1, "checked_kg": 23}},
            "scores": {"price_score": 7, "total": 8},
            "preference_score": 90,
        }
        cheap_risky = {
            "price": 5600,
            "stops": 1,
            "segments": [{"dep_time": "2026-10-01 23:30", "arr_time": "2026-10-02 05:30"}],
            "total_duration_min": 520,
            "execution_risk": {"score": 55},
            "fare_rules": {},
            "scores": {"price_score": 9, "total": 6},
            "preference_score": 60,
        }
        profile = build_travel_profile({"travel_scenario": "family", "travelers": "with_child"})

        stable_score = calc_final_score(stable, target_price=6000, profile=profile)
        risky_score = calc_final_score(cheap_risky, target_price=6000, profile=profile)

        self.assertGreater(stable_score, risky_score)
        self.assertEqual(stable["travel_profile"], profile)


if __name__ == "__main__":
    unittest.main()
