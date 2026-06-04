import unittest

from analyzer import build_travel_profile, calc_final_score


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
