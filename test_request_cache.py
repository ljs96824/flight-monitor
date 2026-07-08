import tempfile
import unittest
from pathlib import Path


class CountingSource:
    name = "fake"

    def __init__(self):
        self.calls = []

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        self.calls.append((origin, dest, date_str, cabin_class))
        return {
            "flights": [
                {
                    "flight_combo": f"{origin}{dest}{date_str}{cabin_class}",
                    "price": 100,
                }
            ],
            "source": self.name,
        }


class RequestCacheTest(unittest.TestCase):
    def test_same_request_reuses_in_memory_result(self):
        from request_cache import cached_fetch, reset_request_cache

        reset_request_cache()
        source = CountingSource()
        passengers = {"adult": 2, "child": 1, "elderly": 0, "infant": 0}

        first = cached_fetch(source, "SHA", "PEK", "2026-06-20", passengers, "economy")
        second = cached_fetch(source, "SHA", "PEK", "2026-06-20", passengers, "economy")

        self.assertEqual(first, second)
        self.assertEqual(source.calls, [("SHA", "PEK", "2026-06-20", "economy")])

    def test_key_keeps_direction_and_cabin_separate(self):
        from request_cache import cached_fetch, reset_request_cache

        reset_request_cache()
        source = CountingSource()
        passengers = {"adult": 1}

        cached_fetch(source, "SHA", "PEK", "2026-06-20", passengers, "economy")
        cached_fetch(source, "PEK", "SHA", "2026-06-20", passengers, "economy")
        cached_fetch(source, "SHA", "PEK", "2026-06-20", passengers, "business")

        self.assertEqual(len(source.calls), 3)

    def test_persistent_cache_reuses_result_after_memory_reset(self):
        from request_cache import cached_fetch, reset_request_cache

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            source = CountingSource()
            passengers = {"adult": 1}

            reset_request_cache()
            cached_fetch(
                source,
                "SHA",
                "PEK",
                "2026-06-20",
                passengers,
                "economy",
                cache_dir=cache_dir,
            )
            reset_request_cache()
            cached_fetch(
                source,
                "SHA",
                "PEK",
                "2026-06-20",
                passengers,
                "economy",
                cache_dir=cache_dir,
            )

            self.assertEqual(source.calls, [("SHA", "PEK", "2026-06-20", "economy")])



    def test_stats_requested_counts_real_fetch_not_cache_hits(self):
        from request_cache import cached_fetch, get_request_cache_stats, reset_request_cache

        reset_request_cache()
        source = CountingSource()
        passengers = {"adult": 1}

        cached_fetch(source, "SHA", "PEK", "2026-06-20", passengers, "economy")
        cached_fetch(source, "SHA", "PEK", "2026-06-20", passengers, "economy")

        fake_stats = get_request_cache_stats()["by_source"]["fake"]
        self.assertEqual(fake_stats["actual"], 1)
        self.assertEqual(fake_stats["requested"], 1)
        self.assertEqual(fake_stats["hits"], 1)

    def test_aggregator_collect_reuses_cached_source_result(self):
        from request_cache import reset_request_cache
        from sources.aggregator import FlightAggregator

        reset_request_cache()
        source = CountingSource()
        source.name = "juhe"
        aggregator = FlightAggregator([source], [], route_type="domestic")

        aggregator.collect("SHA", "PEK", "2026-06-20", passengers={"adult": 1})
        aggregator.collect("SHA", "PEK", "2026-06-20", passengers={"adult": 1})

        self.assertEqual(source.calls, [("SHA", "PEK", "2026-06-20", "economy")])

    def test_price_calendar_source_fetch_reuses_cached_source_result(self):
        from price_calendar import _source_fetch
        from request_cache import reset_request_cache

        reset_request_cache()
        source = CountingSource()

        _source_fetch(source, "SHA", "PEK", "2026-06-20", "economy", {"adult": 1})
        _source_fetch(source, "SHA", "PEK", "2026-06-20", "economy", {"adult": 1})

        self.assertEqual(source.calls, [("SHA", "PEK", "2026-06-20", "economy")])

if __name__ == "__main__":
    unittest.main()
