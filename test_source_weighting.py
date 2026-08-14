import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(get=lambda *args, **kwargs: None, post=lambda *args, **kwargs: None),
)

from notifier import _email_source_body
from sources.aggregator import FlightAggregator
DUAL_SOURCE_PROFILE = {
    "sources": [
        {"name": "juhe", "role": "primary", "weight": 1.0},
        {"name": "hasdata", "role": "cross_check", "weight": 0.6},
    ],
    "query": {},
}




class DummySource:
    def __init__(self, name, role=None, weight=None):
        self.name = name
        if role is not None:
            self.role = role
        if weight is not None:
            self.weight = weight


class SourceWeightingTest(unittest.TestCase):
    def setUp(self):
        from request_cache import reset_for_tests

        self._request_cache_tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._request_cache_dir = (
            Path(self._request_cache_tmp.name) / self._testMethodName
        )
        reset_for_tests(self._request_cache_dir)
        self.addCleanup(self._cleanup_request_cache)

    def _cleanup_request_cache(self):
        from request_cache import reset_for_tests

        reset_for_tests(None)
        self._request_cache_tmp.cleanup()

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

    def test_international_route_uses_only_juhe_after_hasdata_retirement(self):
        aggregator = FlightAggregator(
            [DummySource("juhe"), DummySource("serpapi"), DummySource("hasdata")],
            [],
        )

        ordered = aggregator._ordered_search_sources("PVG", "KIX")

        self.assertEqual([source.name for source in ordered], ["juhe"])
        self.assertEqual([source.role for source in ordered], ["primary"])

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
        with (
            patch("sources.aggregator.cached_fetch", side_effect=direct_cached_fetch),
            patch("sources.aggregator.get_source_profile", return_value=DUAL_SOURCE_PROFILE),
        ):
            result = aggregator.collect("PVG", "HKG", "2026-07-01", route_type="greater_china")

        self.assertIsNotNone(result)
        self.assertIn("hasdata", result["source_stats"])
        self.assertIn("juhe", result["source_stats"])
        self.assertEqual(result["source_stats"]["after_dedup"], 1)
        flight = result["flights"][0]
        self.assertEqual(flight["price"], 1000)
        self.assertEqual(flight["price_source"], "hasdata")
        self.assertEqual(flight["data_source"], "juhe+hasdata")
        self.assertEqual(flight["primary_source"], "juhe")
        prices = {entry["source"]: entry["price"] for entry in flight["source_price_details"]}
        self.assertEqual(prices, {"hasdata": 1000.0, "juhe": 1200.0})
        self.assertTrue(result["price_anomalies"])


    def test_collect_preserves_explicit_empty_status_beside_positive_source(self):
        class MixedSource(DummySource):
            def fetch(self, origin, dest, date_str, cabin_class="economy"):
                if self.name == "juhe":
                    return {
                        "source_status": "empty",
                        "reason": "HTTP成功但空结果",
                        "flights": [],
                    }
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
                            "price": 1000,
                            "data_source": self.name,
                        }
                    ],
                }

        def direct_cached_fetch(source, origin, dest, date_str, passengers, cabin_class, **kwargs):
            return source.fetch(origin, dest, date_str, cabin_class)

        aggregator = FlightAggregator(
            [MixedSource("hasdata"), MixedSource("juhe")],
            [],
            route_type="greater_china",
        )
        with (
            patch("sources.aggregator.cached_fetch", side_effect=direct_cached_fetch),
            patch("sources.aggregator.get_source_profile", return_value=DUAL_SOURCE_PROFILE),
        ):
            result = aggregator.collect("PVG", "HKG", "2026-08-20")

        self.assertEqual(result["source_stats"]["hasdata"]["status"], "成功")
        self.assertEqual(result["source_stats"]["juhe"]["count"], 0)
        self.assertEqual(result["source_stats"]["juhe"]["status"], "empty")

    def test_collect_treats_quota_status_as_source_error_not_empty(self):
        from request_cache import reset_request_cache

        reset_request_cache()
        self.addCleanup(reset_request_cache)

        class FailedSource(DummySource):
            def fetch(self, origin, dest, date_str, cabin_class="economy"):
                return {
                    "source_status": "failed_quota",
                    "error": "配额不足(112)",
                    "flights": [],
                }

        aggregator = FlightAggregator([FailedSource("juhe")], [], route_type="domestic")
        result = aggregator.collect("SHA", "PEK", "2026-08-20")

        self.assertIsNone(result)
        self.assertEqual(
            aggregator.last_source_errors,
            [{"source": "juhe", "cabin_class": "economy", "error": "配额不足(112)"}],
        )

    def test_collect_skips_enrichment_when_search_pool_is_empty(self):
        from request_cache import reset_request_cache

        reset_request_cache()
        self.addCleanup(reset_request_cache)

        class EmptySource(DummySource):
            def fetch(self, origin, dest, date_str, cabin_class="economy"):
                return {"source_status": "success", "flights": []}

        class EnrichmentSource(DummySource):
            def __init__(self, name):
                super().__init__(name)
                self.calls = []

            def fetch(self, origin, dest, date_str, cabin_class="economy"):
                self.calls.append((origin, dest, date_str, cabin_class))
                return {"source_status": "success", "flights": []}

        enrichment = EnrichmentSource("duffel")
        aggregator = FlightAggregator([EmptySource("juhe")], [enrichment], route_type="domestic")
        result = aggregator.collect("SHA", "PEK", "2026-08-20")

        self.assertIsNone(result)
        self.assertEqual(enrichment.calls, [])

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
            patch("sources.aggregator.get_source_profile", return_value=DUAL_SOURCE_PROFILE),
        ):
            result = aggregator.collect("PVG", "HKG", "2026-07-01", route_type="greater_china")

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
        with (
            patch("sources.aggregator.cached_fetch", side_effect=direct_cached_fetch),
            patch("sources.aggregator.get_source_profile", return_value=DUAL_SOURCE_PROFILE),
        ):
            result = aggregator.collect("PVG", "HKG", "2026-07-01", route_type="greater_china")

        self.assertEqual(result["source_stats"]["after_dedup"], 1)
        flight = result["flights"][0]
        self.assertEqual(flight["flight_combo"], "BR705+BR182")
        self.assertEqual(flight["data_source"], "juhe+hasdata")

    def test_email_source_body_shows_domestic_primary_only(self):
        body = _email_source_body(
            {
                "route_type": "domestic",
                "source_stats": {
                    "juhe": {"count": 12, "status": "成功", "role": "primary"},
                    "duffel": {"count": 78, "status": "成功（仅用于行李退改信息）"},
                },
                "collected_at": "2026-06-06T12:00:00",
            }
        )

        self.assertIn("主源:聚合数据(Juhe)—12个方案", body)
        self.assertNotIn("Google Flights", body)
        self.assertIn("国内航线按当前源策略以聚合数据为搜索源", body)

    def test_email_source_body_shows_source_degradation_warning(self):
        body = _email_source_body(
            {
                "route_type": "international",
                "source_stats": {},
                "source_degradation": {
                    "reason": "本轮OTA交叉源不可用(配额不足),入池仅Google,与上次价格不可直接比"
                },
                "collected_at": "2026-06-06T12:00:00",
            }
        )

        self.assertIn("本轮OTA交叉源不可用", body)
        self.assertIn("与上次价格不可直接比", body)


if __name__ == "__main__":
    unittest.main()
