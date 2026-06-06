import sys
import types
import unittest

sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(get=lambda *args, **kwargs: None, post=lambda *args, **kwargs: None),
)

from notifier import _email_source_body
from sources.aggregator import FlightAggregator


class DummySource:
    def __init__(self, name, role=None, weight=None):
        self.name = name
        if role is not None:
            self.role = role
        if weight is not None:
            self.weight = weight


class SourceWeightingTest(unittest.TestCase):
    def test_domestic_merge_keeps_juhe_price_as_primary(self):
        aggregator = FlightAggregator([], [])
        juhe_flight = {
            "flight_combo": "KN5978",
            "price": 527,
            "data_source": "juhe",
            "source_role": "primary",
            "source_weight": 1.0,
            "segments": [{"flight_no": "KN5978", "aircraft": "波音737"}],
        }
        google_flight = {
            "flight_combo": "KN5978",
            "price": 499,
            "data_source": "serpapi",
            "source_role": "cross_check",
            "source_weight": 0.6,
            "segments": [{"flight_no": "KN5978", "aircraft": "B737-800"}],
        }

        merged = aggregator._merge_flights(
            [
                {"source": "serpapi", "flights": [google_flight]},
                {"source": "juhe", "flights": [juhe_flight]},
            ],
            is_domestic=True,
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["price"], 527)
        self.assertEqual(merged[0]["primary_source"], "juhe")
        self.assertEqual(merged[0]["data_source"], "serpapi+juhe")

    def test_international_route_filters_juhe_source(self):
        aggregator = FlightAggregator(
            [DummySource("juhe"), DummySource("serpapi"), DummySource("hasdata")],
            [],
        )

        ordered = aggregator._ordered_search_sources("PVG", "KIX")

        self.assertEqual([source.name for source in ordered], ["serpapi", "hasdata"])
        self.assertEqual([source.role for source in ordered], ["primary", "primary"])

    def test_email_source_body_shows_domestic_primary_and_google_cross_check(self):
        body = _email_source_body(
            {
                "route_type": "domestic",
                "source_stats": {
                    "juhe": {"count": 12, "status": "成功", "role": "primary"},
                    "serpapi": {"count": 8, "status": "成功", "role": "cross_check"},
                    "duffel": {"count": 78, "status": "成功（仅用于行李退改信息）"},
                },
                "collected_at": "2026-06-06T12:00:00",
            }
        )

        self.assertIn("主源:聚合数据", body)
        self.assertIn("交叉验证:Google Flights", body)
        self.assertIn("国内航线以聚合数据", body)


if __name__ == "__main__":
    unittest.main()
