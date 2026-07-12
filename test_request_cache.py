import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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


class EquipmentSource:
    route_type = "international"

    def __init__(self, name, flights):
        self.name = name
        self.flights = flights

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        return {"flights": [dict(item) for item in self.flights], "source": self.name}


class RequestCacheTest(unittest.TestCase):
    def test_cached_fetch_reports_fresh_then_cache_without_changing_payload(self):
        from request_cache import cached_fetch, reset_request_cache

        reset_request_cache()
        source = CountingSource()
        passengers = {"adult": 1}

        first, first_status = cached_fetch(
            source,
            "SHA",
            "PEK",
            "2026-08-20",
            passengers,
            "economy",
            persist=False,
            include_cache_status=True,
        )
        second, second_status = cached_fetch(
            source,
            "SHA",
            "PEK",
            "2026-08-20",
            passengers,
            "economy",
            persist=False,
            include_cache_status=True,
        )

        self.assertEqual(first, second)
        self.assertEqual((first_status, second_status), ("fresh", "cache"))
        self.assertEqual(len(source.calls), 1)

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

    def test_force_fresh_bypasses_memory_and_persistent_cache_reads(self):
        from request_cache import cached_fetch, get_request_cache_stats, reset_request_cache

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            source = CountingSource()
            passengers = {"adult": 1}

            reset_request_cache()
            cached_fetch(
                source,
                "SHA",
                "PEK",
                "2026-07-31",
                passengers,
                "economy",
                cache_dir=cache_dir,
            )
            cached_fetch(
                source,
                "SHA",
                "PEK",
                "2026-07-31",
                passengers,
                "economy",
                cache_dir=cache_dir,
                force_fresh=True,
            )

            stats = get_request_cache_stats()
            self.assertEqual(len(source.calls), 2)
            self.assertEqual(stats["actual"], 2)
            self.assertEqual(stats["hits"], 0)



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

    def test_round_stats_reset_while_process_totals_accumulate(self):
        from request_cache import (
            cached_fetch,
            get_process_request_cache_stats,
            get_request_cache_stats,
            print_request_cache_stats,
            reset_request_cache,
            start_request_cache_round,
        )

        reset_request_cache()
        hasdata = CountingSource()
        hasdata.name = "hasdata"
        juhe = CountingSource()
        juhe.name = "juhe"

        start_request_cache_round("round-international")
        cached_fetch(
            hasdata,
            "PVG",
            "KIX",
            "2026-10-01",
            {"adult": 1},
            persist=False,
            force_fresh=True,
        )
        start_request_cache_round("round-domestic")
        cached_fetch(
            juhe,
            "SHA",
            "PEK",
            "2026-07-31",
            {"adult": 1},
            persist=False,
            force_fresh=True,
        )

        round_stats = get_request_cache_stats()
        process_stats = get_process_request_cache_stats()
        self.assertNotIn("hasdata", round_stats["by_source"])
        self.assertEqual(round_stats["by_source"]["juhe"]["requested"], 1)
        self.assertEqual(round_stats["actual"], 1)
        self.assertEqual(process_stats["by_source"]["hasdata"]["requested"], 1)
        self.assertEqual(process_stats["by_source"]["juhe"]["requested"], 1)
        self.assertEqual(process_stats["actual"], 2)

        with patch("request_cache.safe_log") as log:
            print_request_cache_stats()
        messages = [call.args[0] for call in log.call_args_list]
        round_line = next(line for line in messages if line.startswith("[API统计] "))
        process_line = next(
            line for line in messages if line.startswith("[API统计-进程累计] ")
        )
        self.assertIn("round=round-domestic", round_line)
        self.assertNotIn("hasdata", round_line)
        self.assertIn("hasdata", process_line)

    def test_equipment_codes_are_summarized_once_per_source_and_round(self):
        from request_cache import (
            cached_fetch,
            print_request_cache_stats,
            reset_request_cache,
            start_request_cache_round,
        )

        reset_request_cache()
        self.addCleanup(reset_request_cache)
        juhe = EquipmentSource(
            "juhe",
            [
                {"flight_combo": "MU225", "price": 4883, "aircraft_code": "320"},
                {"flight_combo": "MU730", "price": 4153, "aircraft_code": "32S"},
            ],
        )
        hasdata = EquipmentSource(
            "hasdata",
            [
                {
                    "flight_combo": "MU225",
                    "price": 5124,
                    "segments": [{"aircraft": "Airbus A320"}],
                },
                {
                    "flight_combo": "JL891",
                    "price": 7268,
                    "segments": [{"aircraft": "Boeing 787"}],
                },
            ],
        )

        with patch("request_cache.safe_log") as log:
            start_request_cache_round("round-equipment")
            cached_fetch(juhe, "PVG", "KIX", "2026-10-01", {"adult": 1}, persist=False)
            cached_fetch(juhe, "KIX", "PVG", "2026-10-06", {"adult": 1}, persist=False)
            cached_fetch(hasdata, "PVG", "KIX", "2026-10-01", {"adult": 1}, persist=False)
            print_request_cache_stats()

        messages = [str(call.args[0]) for call in log.call_args_list]
        self.assertFalse(any(message.startswith("[机型码收集]") for message in messages))
        summaries = [message for message in messages if message.startswith("[机型码汇总]")]
        self.assertEqual(len(summaries), 2)
        juhe_summary = next(message for message in summaries if "源=juhe" in message)
        hasdata_summary = next(message for message in summaries if "源=hasdata" in message)
        self.assertIn("组合数=4", juhe_summary)
        self.assertIn("机型种类=2", juhe_summary)
        self.assertIn("未映射机型=[]", juhe_summary)
        self.assertIn("组合数=2", hasdata_summary)
        self.assertIn("机型种类=2", hasdata_summary)
        self.assertIn("未映射机型=[]", hasdata_summary)

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

    def test_aggregator_reports_fresh_then_cached_collection(self):
        from request_cache import reset_request_cache
        from sources.aggregator import FlightAggregator

        reset_request_cache()
        source = CountingSource()
        source.name = "juhe"
        aggregator = FlightAggregator([source], [], route_type="domestic")

        first = aggregator.collect("SHA", "PEK", "2026-08-20", passengers={"adult": 1})
        second = aggregator.collect("SHA", "PEK", "2026-08-20", passengers={"adult": 1})

        self.assertEqual(first["request_cache_status"], "fresh")
        self.assertEqual(second["request_cache_status"], "cache")
        self.assertEqual(len(first["flights"]), len(second["flights"]))

    def test_aggregator_force_fresh_reaches_request_cache(self):
        from request_cache import reset_request_cache
        from sources.aggregator import FlightAggregator

        reset_request_cache()
        source = CountingSource()
        source.name = "juhe"
        aggregator = FlightAggregator([source], [], route_type="domestic")

        aggregator.collect(
            "SHA",
            "PEK",
            "2026-07-31",
            passengers={"adult": 1},
            force_fresh=True,
        )
        aggregator.collect(
            "SHA",
            "PEK",
            "2026-07-31",
            passengers={"adult": 1},
            force_fresh=True,
        )

        self.assertEqual(len(source.calls), 2)

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
