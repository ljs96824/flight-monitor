import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.modules.setdefault("httpx", types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None))

from analyzer import (
    build_passenger_friendly_rules,
    build_passenger_profile,
    build_travel_profile,
    calc_effective_cost,
    calc_final_score,
)
from notifier import build_notification_payload
import storage


class PassengerFriendlyRulesTest(unittest.TestCase):
    def setUp(self):
        self._storage_tmp = tempfile.TemporaryDirectory()
        self._storage_patch = patch.object(
            storage,
            "DB_PATH",
            Path(self._storage_tmp.name) / "prices.db",
        )
        self._storage_patch.start()

    def tearDown(self):
        self._storage_patch.stop()
        self._storage_tmp.cleanup()

    def test_passenger_profile_derives_child_elderly_needs(self):
        profile = build_passenger_profile(
            {"adult": 2, "child": 1, "elderly": 1, "infant": 0},
            {"mobility_limited": True},
        )

        self.assertTrue(profile["has_child"])
        self.assertTrue(profile["has_elderly"])
        self.assertTrue(profile["needs_low_fatigue"])
        self.assertTrue(profile["needs_baggage_clarity"])
        self.assertTrue(profile["needs_time_stability"])
        self.assertTrue(profile["mobility_sensitive"])
        self.assertTrue(profile["mobility_limited"])

    def test_child_type_extra_can_trigger_child_profile(self):
        profile = build_passenger_profile({"adult": 1}, {"child_type": "infant"})

        self.assertTrue(profile["has_child"])
        self.assertTrue(profile["has_infant"])
        self.assertTrue(profile["needs_baggage_clarity"])
    def test_friendly_rules_shift_weights_and_connection_safety(self):
        adult_rules = build_passenger_friendly_rules(
            build_passenger_profile({"adult": 1, "child": 0, "elderly": 0, "infant": 0})
        )
        family_rules = build_passenger_friendly_rules(
            build_passenger_profile({"adult": 2, "child": 1, "elderly": 1, "infant": 0})
        )

        self.assertTrue(family_rules["prefer_direct"])
        self.assertFalse(family_rules["allow_red_eye"])
        self.assertFalse(family_rules["allow_self_transfer"])
        self.assertFalse(family_rules["allow_airport_change"])
        self.assertFalse(family_rules["allow_overnight_transfer"])
        self.assertTrue(family_rules["require_baggage_clarity"])
        self.assertEqual(family_rules["max_transfers"], 1)
        self.assertEqual(family_rules["min_connection_min"], adult_rules["min_connection_min"] + 30)
        self.assertLess(family_rules["weights"]["price"], adult_rules["weights"]["price"])
        self.assertGreater(
            family_rules["weights"]["execution_risk"],
            adult_rules["weights"]["execution_risk"],
        )

    def test_quick_family_elderly_tags_build_profile_without_precise_counts(self):
        profile = build_travel_profile(
            {
                "travel_scenarios": ["family", "elderly"],
                "passenger_count": 3,
            }
        )

        passenger_profile = profile["passenger_profile"]
        self.assertTrue(passenger_profile["has_child"])
        self.assertTrue(passenger_profile["has_elderly"])
        self.assertTrue(profile["passenger_rules"]["prefer_direct"])
        self.assertEqual(profile["score_weights"]["price"], profile["passenger_rules"]["weights"]["price"])

    def test_family_score_prefers_direct_daytime_baggage_clarity(self):
        profile = build_travel_profile(
            {"passengers": {"adult": 2, "child": 1, "elderly": 1, "infant": 0}}
        )
        friendly = {
            "flight_no": "MU100",
            "price": 1200,
            "stops": 0,
            "departure_time": "09:00",
            "arrival_time": "11:00",
            "total_duration_min": 120,
            "fare_rules": {"baggage": {"included": True, "checked_kg": 20}, "refund": {"level": "中"}},
            "execution_risk": {"score": 10},
            "scores": {"price_score": 5, "total": 7},
        }
        tiring = {
            "flight_no": "XX200",
            "price": 900,
            "stops": 1,
            "departure_time": "23:30",
            "arrival_time": "05:30",
            "total_duration_min": 520,
            "fare_rules": {"baggage": {"included": False}, "refund": {"level": "低"}},
            "execution_risk": {"score": 40},
            "scores": {"price_score": 8, "total": 5},
        }

        self.assertGreater(
            calc_final_score(friendly, target_price=1000, profile=profile),
            calc_final_score(tiring, target_price=1000, profile=profile),
        )
        self.assertLess(friendly["score_components"]["weights"]["price"], 0.25)

    def test_effective_cost_adds_family_fatigue_penalty(self):
        flight = {
            "flight_no": "XX200",
            "price": 900,
            "stops": 1,
            "departure_airport": "SHA",
            "arrival_airport": "PEK",
            "departure_time": "23:30",
            "arrival_time": "05:30",
            "total_duration_min": 520,
            "fare_rules": {"baggage": {"included": False}},
        }

        adult = calc_effective_cost(flight, {"passengers": {"adult": 1}})
        family = calc_effective_cost(
            flight,
            {"passengers": {"adult": 2, "child": 1, "elderly": 1, "infant": 0}},
        )

        self.assertGreater(family["effective_cost"], adult["effective_cost"])
        self.assertGreater(family["family_fatigue_cost"], 0)

    def test_notification_payload_exposes_passenger_friendly_tags(self):
        combo = {
            "outbound": {
                "flight_no": "MU100",
                "airline": "MU",
                "price": 1200,
                "stops": 0,
                "departure_airport": "SHA",
                "arrival_airport": "PEK",
                "departure_time": "09:00",
                "arrival_time": "11:00",
                "fare_rules": {"baggage": {"included": True, "checked_kg": 20}, "refund": {"level": "中"}},
            },
            "return": {
                "flight_no": "MU101",
                "airline": "MU",
                "price": 1300,
                "stops": 0,
                "departure_airport": "PEK",
                "arrival_airport": "SHA",
                "departure_time": "17:00",
                "arrival_time": "19:00",
                "fare_rules": {"baggage": {"included": True, "checked_kg": 20}, "refund": {"level": "中"}},
            },
            "total_price": 2500,
        }
        payload = build_notification_payload(
            {"round_trip_analysis": {"top_combinations": [combo]}},
            route_info={"round_trip": True, "depart_date": "2026-06-26", "return_date": "2026-06-30"},
            subscription={
                "soft_preferences": {"travel_scenarios": ["family", "elderly"]},
                "preferences": {"passengers": {"adult": 2, "child": 1, "elderly": 1, "infant": 0}},
            },
        )

        plan = payload["recommended_plans"][0]
        self.assertTrue(payload["passenger_profile"]["has_child"])
        self.assertTrue(payload["passenger_profile"]["has_elderly"])
        self.assertIn("亲子·老人友好", plan["tags"])
        self.assertNotIn("亲子/老人友好", plan["tags"])
        self.assertNotIn("亲子友好 | 老人友好", plan["tags"])
        self.assertIn("白天直飞", plan["friendly_reason"])


if __name__ == "__main__":
    unittest.main()
