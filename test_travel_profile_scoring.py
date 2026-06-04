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
