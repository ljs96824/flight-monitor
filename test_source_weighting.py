import sys
import types
import unittest
from unittest.mock import patch

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
    def test_domestic_merge_uses_global_min_price_but_keeps_primary_role(self):
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
        self.assertEqual(merged[0]["price"], 499)
        self.assertEqual(merged[0]["price_source"], "serpapi")
        self.assertEqual(merged[0]["primary_source"], "juhe")
        self.assertEqual(merged[0]["data_source"], "serpapi+juhe")

    def test_international_route_uses_hasdata_then_juhe_sources(self):
        aggregator = FlightAggregator(
            [DummySource("juhe"), DummySource("serpapi"), DummySource("hasdata")],
            [],
        )

        ordered = aggregator._ordered_search_sources("PVG", "KIX")

        self.assertEqual([source.name for source in ordered], ["hasdata", "juhe"])
        self.assertEqual([source.role for source in ordered], ["primary", "cross_check"])

    def test_collect_merges_hasdata_and_juhe_same_combo_with_price_details(self):
        class FetchSource(DummySource):
            def __init__(self, name, combo, price):
                super().__init__(name)
                self.combo = combo
                self.price = price

            def fetch(self, origin, dest, date_str, cabin_class="economy"):
                return {
                    "source_status": "success",
                    "flights": [
                        {
                            "flight_combo": self.combo,
                            "flight_no": self.combo.replace(" ", ""),
                            "departure_airport": origin,
                            "arrival_airport": dest,
                            "departure_time": f"{date_str} 09:00",
                            "arrival_time": f"{date_str} 12:00",
                            "price": self.price,
                            "data_source": self.name,
                        }
                    ],
                }

        def direct_cached_fetch(source, origin, dest, date_str, passengers, cabin_class, **kwargs):
            return source.fetch(origin, dest, date_str, cabin_class)

        aggregator = FlightAggregator(
            [FetchSource("hasdata", "MU 225", 1000), FetchSource("juhe", "MU225", 1200)],
            [],
        )
        with patch("sources.aggregator.cached_fetch", side_effect=direct_cached_fetch):
            result = aggregator.collect("PVG", "KIX", "2026-07-01", route_type="international")

        self.assertIsNotNone(result)
        self.assertIn("hasdata", result["source_stats"])
        self.assertIn("juhe", result["source_stats"])
        self.assertEqual(result["source_stats"]["after_dedup"], 1)
        flight = result["flights"][0]
        self.assertEqual(flight["price"], 1000)
        self.assertEqual(flight["price_source"], "hasdata")
        self.assertEqual(flight["data_source"], "hasdata+juhe")
        self.assertEqual(flight["primary_source"], "hasdata")
        prices = {entry["source"]: entry["price"] for entry in flight["source_price_details"]}
        self.assertEqual(prices, {"hasdata": 1000.0, "juhe": 1200.0})
        self.assertTrue(result["price_anomalies"])

    def test_collect_uses_juhe_when_it_is_the_global_min_price(self):
        class FetchSource(DummySource):
            def __init__(self, name, price):
                super().__init__(name)
                self.price = price

            def fetch(self, origin, dest, date_str, cabin_class="economy"):
                return {
                    "source_status": "success",
                    "flights": [
                        {
                            "flight_combo": "MU225",
                            "flight_no": "MU225",
                            "departure_airport": origin,
                            "arrival_airport": dest,
                            "departure_time": f"{date_str} 09:00",
                            "arrival_time": f"{date_str} 12:00",
                            "price": self.price,
                            "data_source": self.name,
                        }
                    ],
                }

        def direct_cached_fetch(source, origin, dest, date_str, passengers, cabin_class, **kwargs):
            return source.fetch(origin, dest, date_str, cabin_class)

        aggregator = FlightAggregator(
            [FetchSource("hasdata", 1200), FetchSource("juhe", 1000)],
            [],
        )
        with (
            patch("sources.aggregator.cached_fetch", side_effect=direct_cached_fetch),
            patch("sources.aggregator.safe_log") as log,
        ):
            result = aggregator.collect("PVG", "KIX", "2026-07-01", route_type="international")

        self.assertEqual(result["flights"][0]["price"], 1000)
        self.assertEqual(result["flights"][0]["price_source"], "juhe")
        messages = [call.args[0] for call in log.call_args_list if call.args]
        source_price_logs = [message for message in messages if message.startswith("[源价对比]")]
        merge_price_logs = [message for message in messages if message.startswith("[合并选价]")]
        self.assertEqual(len(source_price_logs), 1)
        self.assertEqual(len(merge_price_logs), 1)
        self.assertEqual(
            merge_price_logs[0],
            "[合并选价] combo=MU225 入池价=CNY1000 取自=juhe "
            "候选=hasdata:CNY1200/juhe:CNY1000 规则=global_min",
        )
        self.assertEqual(len(result["dual_source_price_anomalies"]), 1)

    def test_collect_merges_separator_and_leading_zero_combo_across_sources(self):
        class FetchSource(DummySource):
            def __init__(self, name, combo, price):
                super().__init__(name)
                self.combo = combo
                self.price = price

            def fetch(self, origin, dest, date_str, cabin_class="economy"):
                return {
                    "source_status": "success",
                    "flights": [
                        {
                            "flight_combo": self.combo,
                            "flight_no": self.combo,
                            "departure_airport": origin,
                            "arrival_airport": dest,
                            "departure_time": f"{date_str} 09:00",
                            "arrival_time": f"{date_str} 14:00",
                            "price": self.price,
                            "data_source": self.name,
                        }
                    ],
                }

        def direct_cached_fetch(source, origin, dest, date_str, passengers, cabin_class, **kwargs):
            return source.fetch(origin, dest, date_str, cabin_class)

        aggregator = FlightAggregator(
            [FetchSource("hasdata", "BR705+BR182", 2000), FetchSource("juhe", "BR0705|BR0182", 2100)],
            [],
        )
        with patch("sources.aggregator.cached_fetch", side_effect=direct_cached_fetch):
            result = aggregator.collect("PVG", "KIX", "2026-07-01", route_type="international")

        self.assertEqual(result["source_stats"]["after_dedup"], 1)
        flight = result["flights"][0]
        self.assertEqual(flight["flight_combo"], "BR705+BR182")
        self.assertEqual(flight["data_source"], "hasdata+juhe")

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
