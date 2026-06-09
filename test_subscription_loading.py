import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None),
)
logging.basicConfig = lambda *a, **k: None

import main


class SubscriptionLoadingTest(unittest.TestCase):
    def test_collect_for_airport_matrix_filters_to_requested_active_airports(self):
        class FakeAggregator:
            def collect(self, origin, destination, date_str, cabin_classes=None):
                return {
                    "flights": [
                        {
                            "flight_combo": "ACTIVE",
                            "price": 680,
                            "departure_airport": origin,
                            "arrival_airport": destination,
                        },
                        {
                            "flight_combo": "INACTIVE_DEST",
                            "price": 500,
                            "departure_airport": origin,
                            "arrival_airport": "PKX",
                        },
                    ],
                    "source_stats": {},
                }

        data = main.collect_for_airport_matrix(
            FakeAggregator(),
            ["PVG"],
            ["PEK"],
            "2026-06-10",
        )

        self.assertEqual([flight["flight_combo"] for flight in data["flights"]], ["ACTIVE"])

    def test_bad_subscription_is_skipped_without_stopping_batch(self):
        records = [
            {
                "id": "bad-location",
                "origin": "上海",
                "destination": "重庆",
                "depart_date": "2026-10-01",
                "status": "active",
            },
            {
                "id": "good-osaka",
                "origin": "上海",
                "destination": "大阪",
                "depart_date": "2026-10-01",
                "status": "active",
            },
        ]

        original_path = main.SUBSCRIPTIONS_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
            main.SUBSCRIPTIONS_PATH = path
            try:
                loaded = main.load_file_subscriptions()
            finally:
                main.SUBSCRIPTIONS_PATH = original_path

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["id"], "good-osaka")
        self.assertEqual(loaded[0]["destination"], "大阪")
        self.assertEqual(loaded[0]["destination_airports"], ["KIX", "ITM"])


    def test_subscription_preferences_include_travel_scenarios(self):
        prefs = main.subscription_preferences(
            {
                "soft_preferences": {
                    "travel_scenarios": ["tourism", "family"],
                    "travel_scenario": "tourism",
                },
                "companions": "solo",
            }
        )

        self.assertEqual(prefs["travel_scenarios"], ["tourism", "family"])
        self.assertEqual(prefs["travel_scenario"], "tourism")

    def test_normalized_subscription_preserves_canonical_passenger_fields(self):
        normalized = main._normalize_subscription(
            {
                "id": "family-trip",
                "origin": "上海",
                "destination": "大阪",
                "depart_date": "2026-10-01",
                "status": "active",
                "basic": {
                    "passenger_count": 5,
                },
                "preferences": {
                    "passengers": {"adult": 2, "child": 1, "elderly": 2, "infant": 0},
                    "passenger_count": 5,
                    "travel_purposes": ["tourism", "family"],
                },
                "soft_preferences": {
                    "travel_scenarios": ["tourism", "family"],
                },
            }
        )

        self.assertEqual(normalized["basic"]["passenger_count"], 5)
        self.assertEqual(
            normalized["preferences"]["passengers"],
            {"adult": 2, "child": 1, "elderly": 2, "infant": 0},
        )
        self.assertEqual(normalized["soft_preferences"]["passengers"]["elderly"], 2)


if __name__ == "__main__":
    unittest.main()
