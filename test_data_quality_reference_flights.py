import unittest
import sys
import types

sys.modules.setdefault("httpx", types.SimpleNamespace(get=lambda *a, **k: None))

from analyzer import analyze_all_flights
from collector import _normalize_detail_flight
from sources.travelpayouts_source import TravelpayoutsSource


class DataQualityReferenceFlightsTest(unittest.TestCase):
    def test_layover_summary_uses_stops_even_without_layover_details(self):
        flight = _normalize_detail_flight(
            {
                "price": 1880,
                "flight_combo": "NH429",
                "airline_summary": "NH",
                "route_summary": "PVG → KIX",
                "total_duration_min": 720,
                "stops": 2,
                "stopovers": 2,
                "segments": [],
                "layovers": [],
            },
            "travelpayouts",
        )

        self.assertEqual(flight["layover_summary"], "中转2次")

    def test_travelpayouts_sparse_price_does_not_enter_recommendations(self):
        sparse_reference = TravelpayoutsSource()._parse_price_item(
            {
                "price": 1200,
                "airline": "NH",
                "flight_number": "429",
                "transfers": 2,
                "duration": 720,
            },
            "PVG",
            "KIX",
            "economy",
        )
        detailed_flight = {
            "price": 1800,
            "flight_combo": "MU515",
            "airline_summary": "MU",
            "route_summary": "PVG → KIX",
            "total_duration_min": 195,
            "total_hours": 3.3,
            "stops": 0,
            "segments": [
                {
                    "flight_no": "MU515",
                    "airline": "MU",
                    "aircraft": "A321",
                    "dep_airport": "PVG",
                    "dep_time": "2026-10-01 09:50",
                    "arr_airport": "KIX",
                    "arr_time": "2026-10-01 13:20",
                    "duration_min": 195,
                }
            ],
            "layovers": [],
            "data_source": "serpapi",
            "cabin_class": "economy",
        }

        analysis = analyze_all_flights([sparse_reference, detailed_flight])
        recommended_combos = {
            flight.get("flight_combo")
            for flight in analysis.get("economy_recommendations", [])
        }

        self.assertNotIn("NH429", recommended_combos)
        self.assertIn("MU515", recommended_combos)
        self.assertTrue(analysis.get("reference_flights"))


if __name__ == "__main__":
    unittest.main()
