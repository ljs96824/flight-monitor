import unittest

from analyzer import apply_default_rules


class TravelScenarioDefaultsTest(unittest.TestCase):
    def test_family_scenario_applies_safe_quick_defaults(self):
        sub = apply_default_rules(
            {
                "monitor_mode": "quick",
                "soft_preferences": {"travel_scenario": "family"},
                "hard_constraints": {"baggage": "unknown"},
                "notification_goals": {},
            }
        )

        soft = sub["soft_preferences"]
        hard = sub["hard_constraints"]

        self.assertEqual(soft["travel_scenario"], "family")
        self.assertEqual(soft["time_preference_mode"], "no_redeye")
        self.assertFalse(soft["allow_self_transfer"])
        self.assertFalse(soft["allow_overnight_transfer"])
        self.assertEqual(hard["baggage_default"], "prefer_included")
        self.assertIn("family", soft["scenario_rules"])
        self.assertTrue(any("家庭" in item for item in sub["defaults_applied"]))

    def test_companion_constraints_override_scene_defaults(self):
        sub = apply_default_rules(
            {
                "monitor_mode": "precise",
                "soft_preferences": {
                    "travel_scenario": "price_first",
                    "companions": "with_elderly",
                    "companion_constraints": [
                        "need_baggage",
                        "no_redeye",
                        "limited_mobility",
                    ],
                },
                "hard_constraints": {"transfer_policy": "price_first"},
                "notification_goals": {},
            }
        )

        soft = sub["soft_preferences"]
        hard = sub["hard_constraints"]

        self.assertEqual(soft["travel_scenario"], "price_first")
        self.assertEqual(soft["companions"], "with_elderly")
        self.assertEqual(soft["time_preference_mode"], "no_redeye")
        self.assertEqual(hard["baggage"], "required")
        self.assertFalse(soft["allow_self_transfer"])
        self.assertLessEqual(hard["max_extra_duration_hours"], 3)
        self.assertTrue(soft["direct_preferred"])


if __name__ == "__main__":
    unittest.main()
