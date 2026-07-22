import tempfile
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch


class FakeSource:
    calls = []

    def __init__(self, name):
        self.name = name

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        self.__class__.calls.append((self.name, origin, dest, date_str, cabin_class))
        return {
            "source_status": "success",
            "flights": [{"flight_combo": "TEST1", "price": 100}],
        }


class FakeAggregator:
    instances = []
    collect_calls = []

    def __init__(self, search_sources, enrichment_sources, route_type=None):
        self.search_sources = search_sources
        self.enrichment_sources = enrichment_sources
        self.route_type = route_type
        self.__class__.instances.append(self)

    def collect(self, origin, dest, depart_date, **kwargs):
        self.__class__.collect_calls.append((origin, dest, depart_date, kwargs))
        if (origin, dest) == ("PVG", "HKG"):
            raise RuntimeError("provider unavailable")
        return {"flights": [{"flight_combo": "TEST1", "price": 100}]}


def fake_source_builder(origin, dest, route_type=None):
    names = {
        "domestic": ["juhe"],
        "greater_china": ["juhe", "hasdata"],
        "international": ["hasdata", "juhe"],
    }[route_type]
    return [FakeSource(name) for name in names], [FakeSource("duffel")]


class FreshObservationSource:
    calls = []

    def __init__(self, name):
        self.name = name

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        self.__class__.calls.append((self.name, origin, dest, date_str, cabin_class))
        combo = {
            ("SHA", "PEK"): "MU225",
            ("PVG", "HKG"): "MU501",
            ("PVG", "KIX"): "MU225",
        }[(origin, dest)]
        return {
            "source_status": "success",
            "flights": [
                {
                    "flight_combo": combo,
                    "flight_no": combo,
                    "departure_airport": origin,
                    "arrival_airport": dest,
                    "departure_time": f"{date_str} 09:00",
                    "arrival_time": f"{date_str} 12:00",
                    "total_duration_min": 180,
                    "stops": 0,
                    "price": 800 if self.name == "hasdata" else 900,
                    "data_source": self.name,
                }
            ],
        }


def fresh_source_builder(origin, dest, route_type=None):
    names = {
        "domestic": ["juhe"],
        "greater_china": ["juhe", "hasdata"],
        "international": ["hasdata", "juhe"],
    }[route_type]
    return [FreshObservationSource(name) for name in names], [FreshObservationSource("duffel")]


class BasketCollectTest(unittest.TestCase):
    def setUp(self):
        FakeAggregator.instances.clear()
        FakeAggregator.collect_calls.clear()
        FakeSource.calls.clear()
        FreshObservationSource.calls.clear()

    def test_initial_queue_dates_are_fixed_after_first_creation(self):
        from basket_collect import load_or_create_state

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "basket_state.json"
            first = load_or_create_state(state_path, date(2026, 7, 10))
            second = load_or_create_state(state_path, date(2026, 7, 11))

        self.assertEqual(first, second)
        self.assertEqual(first["routes"]["SHA->PEK"], {"A": "2026-07-31", "B": "2026-09-08"})
        self.assertEqual(first["routes"]["PVG->HKG"], {"A": "2026-08-24", "B": "2026-09-08"})
        self.assertEqual(first["routes"]["PVG->KIX"], {"A": "2026-10-01", "B": "2026-09-08"})

    def test_expired_queue_is_renewed_to_today_plus_60(self):
        from basket_collect import renew_expired_queues

        state = {
            "version": 1,
            "routes": {
                "SHA->PEK": {"A": "2026-07-10", "B": "2026-09-08"},
            },
        }
        output = StringIO()
        with redirect_stdout(output):
            renewals = renew_expired_queues(state, date(2026, 7, 10))

        self.assertEqual(state["routes"]["SHA->PEK"]["A"], "2026-09-08")
        self.assertEqual(
            renewals,
            [{"route": "SHA->PEK", "queue": "A", "old": "2026-07-10", "new": "2026-09-08"}],
        )
        self.assertIn("[队列续期] route=SHA->PEK 旧=2026-07-10 新=2026-09-08", output.getvalue())

    def test_run_basket_forces_fresh_skips_duffel_and_isolates_route_failures(self):
        from basket_collect import run_basket

        with tempfile.TemporaryDirectory() as tmp:
            output = StringIO()
            with patch("basket_collect.count_observations_for_round", return_value=123):
                with redirect_stdout(output):
                    summary = run_basket(
                        today=date(2026, 7, 10),
                        now=datetime(2026, 7, 10, 9, 30, 0),
                        state_path=Path(tmp) / "basket_state.json",
                        db_path=Path(tmp) / "observations.sqlite3",
                        usage_path=Path(tmp) / "api_usage.json",
                        source_builder=fake_source_builder,
                        aggregator_factory=FakeAggregator,
                    )

        self.assertEqual(summary, {"round_id": "basket_20260710T093000", "queues": 6, "success": 4, "failed": 2, "written": 123})
        self.assertTrue(all(call[3]["force_fresh"] is False for call in FakeAggregator.collect_calls))
        self.assertEqual(len(FakeSource.calls), 10)
        self.assertTrue(all(instance.enrichment_sources == [] for instance in FakeAggregator.instances))
        self.assertEqual(
            [[source.name for source in instance.search_sources] for instance in FakeAggregator.instances],
            [["juhe"], ["juhe", "hasdata"], ["hasdata", "juhe"]],
        )
        log = output.getvalue()
        self.assertEqual(log.count("[篮子失败] route=PVG->HKG"), 2)
        self.assertIn("[篮子完成] 队列=6 成功=4 失败=2 总写入=123", log)

    def test_real_aggregator_writes_each_fresh_source_without_duffel(self):
        from basket_collect import run_basket
        from sources.aggregator import FlightAggregator

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            db_path = root / "observations.sqlite3"
            output = StringIO()
            with patch("request_cache.DEFAULT_CACHE_DIR", root / "cache"):
                with redirect_stdout(output):
                    summary = run_basket(
                        today=date(2026, 7, 10),
                        now=datetime(2026, 7, 10, 9, 30, 0),
                        state_path=root / "basket_state.json",
                        db_path=db_path,
                        usage_path=root / "api_usage.json",
                        source_builder=fresh_source_builder,
                        aggregator_factory=FlightAggregator,
                    )

            with closing(sqlite3.connect(db_path)) as conn, conn:
                rows = conn.execute(
                    "SELECT origin_airport, dest_airport, depart_date, source, COUNT(*) "
                    "FROM observations GROUP BY origin_airport, dest_airport, depart_date, source"
                ).fetchall()

        self.assertEqual(summary["queues"], 6)
        self.assertEqual(summary["success"], 6)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["written"], 10)
        self.assertEqual(len(rows), 10)
        self.assertEqual(sum(1 for call in FreshObservationSource.calls if call[0] == "juhe"), 6)
        self.assertEqual(sum(1 for call in FreshObservationSource.calls if call[0] == "hasdata"), 4)
        self.assertEqual(sum(1 for call in FreshObservationSource.calls if call[0] == "duffel"), 0)
        self.assertIn("[采集计划] 唯一请求=10", output.getvalue())
        self.assertIn("[篮子完成] 队列=6 成功=6 失败=0 总写入=10", output.getvalue())


if __name__ == "__main__":
    unittest.main()
