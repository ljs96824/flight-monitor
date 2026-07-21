import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from contextlib import closing, redirect_stdout
from io import StringIO


class ObservationSource:
    def __init__(self, name, price):
        self.name = name
        self.price = price
        self.role = "primary" if name == "juhe" else "cross_check"
        self.weight = 1.0 if name == "juhe" else 0.6

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        combo = "MU 225" if self.name == "hasdata" else "MU225"
        return {
            "source_status": "success",
            "flights": [
                {
                    "flight_combo": combo,
                    "flight_no": combo.replace(" ", ""),
                    "airline": "MU",
                    "departure_airport": origin,
                    "arrival_airport": dest,
                    "departure_time": f"{date_str} 09:00",
                    "arrival_time": f"{date_str} 12:00",
                    "stops": 0,
                    "price": self.price,
                    "data_source": self.name,
                }
            ],
        }


class ObservationsStoreTest(unittest.TestCase):
    def test_append_observations_is_idempotent_and_records_single_person_oneway_price(self):
        from observations_store import append_observations, count_observations, init_observations_db

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "observations.sqlite3"
            init_observations_db(db_path)
            flights = [
                {
                    "flight_combo": "MU 225",
                    "airline": "MU",
                    "stops": 0,
                    "price": 1234,
                }
            ]

            first = append_observations(
                flights,
                db_path=db_path,
                round_id="round-1",
                route_type="international",
                origin_airport="PVG",
                dest_airport="KIX",
                depart_date="2099-12-28",
                cabin_class="economy",
                source="hasdata",
                observed_at="2099-12-01T10:00:00",
            )
            second = append_observations(
                flights,
                db_path=db_path,
                round_id="round-1",
                route_type="international",
                origin_airport="PVG",
                dest_airport="KIX",
                depart_date="2099-12-28",
                cabin_class="economy",
                source="hasdata",
                observed_at="2099-12-01T10:00:00",
            )

            self.assertEqual(first, {"written": 1, "skipped": 0})
            self.assertEqual(second, {"written": 0, "skipped": 1})
            self.assertEqual(count_observations(db_path), 1)
            with closing(sqlite3.connect(db_path)) as conn, conn:
                row = conn.execute(
                    "SELECT days_to_departure, source, flight_combo, price_cny, method_version FROM observations"
                ).fetchone()
            self.assertEqual(row, (27, "hasdata", "MU225", 1234.0, "v1"))

    def test_count_observations_for_round_counts_only_requested_round(self):
        from observations_store import append_observations, count_observations_for_round

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "observations.sqlite3"
            common = {
                "route_type": "domestic",
                "origin_airport": "SHA",
                "dest_airport": "PEK",
                "depart_date": "2099-12-28",
                "cabin_class": "economy",
                "source": "juhe",
                "observed_at": "2099-12-01T10:00:00",
                "db_path": db_path,
            }
            append_observations(
                [{"flight_combo": "MU225", "price": 831}],
                round_id="basket_20991201T100000",
                **common,
            )
            append_observations(
                [{"flight_combo": "MU226", "price": 900}],
                round_id="subscription_20991201T100000",
                **common,
            )

            self.assertEqual(
                count_observations_for_round("basket_20991201T100000", db_path),
                1,
            )

    def test_append_observations_normalizes_transfer_combo_before_storage(self):
        from observations_store import append_observations, init_observations_db

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "observations.sqlite3"
            init_observations_db(db_path)
            result = append_observations(
                [{"flight_combo": "BR0705|BR0182", "price": 1888}],
                db_path=db_path,
                round_id="round-transfer",
                route_type="international",
                origin_airport="PVG",
                dest_airport="KIX",
                depart_date="2099-12-28",
                cabin_class="economy",
                source="juhe",
                observed_at="2099-12-01T10:00:00",
            )

            self.assertEqual(result, {"written": 1, "skipped": 0})
            with closing(sqlite3.connect(db_path)) as conn, conn:
                combo = conn.execute("SELECT flight_combo FROM observations").fetchone()[0]
            self.assertEqual(combo, "BR705+BR182")

    def test_aggregator_writes_per_source_observations_before_dedup(self):
        from request_cache import reset_request_cache
        from sources.aggregator import FlightAggregator

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            reset_request_cache()
            db_path = Path(tmp) / "observations.sqlite3"
            aggregator = FlightAggregator(
                [ObservationSource("juhe", 1200), ObservationSource("hasdata", 1000)],
                [],
                route_type="greater_china",
            )

            from observations_store import clear_current_round, set_current_round

            set_current_round("round-gc", db_path=db_path)
            self.addCleanup(clear_current_round)
            result = aggregator.collect(
                "PVG",
                "HKG",
                "2099-12-28",
                route_type="greater_china",
            )

            self.assertEqual(result["source_stats"]["after_dedup"], 1)
            with closing(sqlite3.connect(db_path)) as conn, conn:
                rows = conn.execute(
                    "SELECT source, flight_combo, price_cny FROM observations ORDER BY source"
                ).fetchall()
            self.assertEqual(rows, [("hasdata", "MU225", 1000.0), ("juhe", "MU225", 1200.0)])

    def test_observation_write_failure_does_not_break_collect(self):
        from request_cache import reset_request_cache
        from sources.aggregator import FlightAggregator

        reset_request_cache()
        aggregator = FlightAggregator([ObservationSource("juhe", 1200)], [], route_type="domestic")

        from observations_store import clear_current_round, set_current_round

        set_current_round("round-fail")
        self.addCleanup(clear_current_round)
        with patch("observations_store.append_observations", side_effect=sqlite3.OperationalError("locked")):
            result = aggregator.collect(
                "SHA",
                "PEK",
                "2099-12-28",
                route_type="domestic",
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["source_stats"]["after_dedup"], 1)

    def test_cached_fetch_writes_observations_for_calendar_path(self):
        from observations_store import clear_current_round, count_observations, set_current_round
        from price_calendar import _source_fetch
        from request_cache import reset_request_cache

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "observations.sqlite3"
            reset_request_cache()
            source = ObservationSource("juhe", 831)
            source.route_type = "domestic"
            set_current_round("round-calendar", db_path=db_path)
            self.addCleanup(clear_current_round)

            flights = _source_fetch(source, "SHA", "PEK", "2099-12-28", "economy", {"adult": 1}, ttl_seconds=0)

            self.assertEqual(len(flights), 1)
            self.assertEqual(count_observations(db_path), 1)

    def test_cached_fetch_records_each_fetch_argument_date_instead_of_subscription_date(self):
        from observations_store import clear_current_round, set_current_round
        from request_cache import cached_fetch, reset_request_cache

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "observations.sqlite3"
            reset_request_cache()
            source = ObservationSource("juhe", 831)
            source.route_type = "international"
            set_current_round("round-multi-date", db_path=db_path)
            self.addCleanup(clear_current_round)
            output = StringIO()

            with redirect_stdout(output):
                cached_fetch(source, "PVG", "KIX", "2026-09-28", {"adult": 1}, "economy", ttl_seconds=0, persist=False)
                cached_fetch(source, "PVG", "KIX", "2026-10-01", {"adult": 1}, "economy", ttl_seconds=0, persist=False)
                cached_fetch(source, "PVG", "KIX", "2026-10-04", {"adult": 1}, "economy", ttl_seconds=0, persist=False)

            with closing(sqlite3.connect(db_path)) as conn, conn:
                rows = conn.execute(
                    "SELECT depart_date, COUNT(*), MIN(days_to_departure), MAX(days_to_departure) "
                    "FROM observations GROUP BY depart_date ORDER BY depart_date"
                ).fetchall()

            expected_rows = [
                (
                    depart_date,
                    1,
                    (date.fromisoformat(depart_date) - date.today()).days,
                    (date.fromisoformat(depart_date) - date.today()).days,
                )
                for depart_date in ("2026-09-28", "2026-10-01", "2026-10-04")
            ]
            self.assertEqual(rows, expected_rows)
            log = output.getvalue()
            self.assertIn("\u65e5\u671f=2026-09-28", log)
            self.assertIn("\u65e5\u671f=2026-10-01", log)
            self.assertIn("\u65e5\u671f=2026-10-04", log)

    def test_cached_fetch_without_round_logs_skip_instead_of_silent_gap(self):
        from observations_store import clear_current_round
        from request_cache import cached_fetch, reset_request_cache

        clear_current_round()
        reset_request_cache()
        source = ObservationSource("juhe", 831)
        source.route_type = "domestic"
        output = StringIO()

        with redirect_stdout(output):
            cached_fetch(source, "SHA", "PEK", "2099-12-28", {"adult": 1}, "economy", ttl_seconds=0, persist=False)

        self.assertIn("[\u89c2\u6d4b\u843d\u5e93\u8df3\u8fc7]", output.getvalue())
        self.assertIn("\u539f\u56e0=\u65e0round_id", output.getvalue())

    def test_append_observations_records_duration_min_column(self):
        from observations_store import append_observations, init_observations_db

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "observations.sqlite3"
            init_observations_db(db_path)
            append_observations(
                [{"flight_combo": "SQ0825|SQ622", "price": 1888, "total_duration_min": 375}],
                db_path=db_path,
                round_id="round-duration",
                route_type="international",
                origin_airport="PVG",
                dest_airport="KIX",
                depart_date="2099-12-28",
                cabin_class="economy",
                source="hasdata",
                observed_at="2099-12-01T10:00:00",
            )

            with closing(sqlite3.connect(db_path)) as conn, conn:
                columns = [row[1] for row in conn.execute("PRAGMA table_info(observations)").fetchall()]
                row = conn.execute("SELECT flight_combo, duration_min FROM observations").fetchone()
            self.assertIn("duration_min", columns)
            self.assertEqual(row, ("SQ825+SQ622", 375))

    def test_migrate_normalized_combos_merges_format_twins_and_keeps_lower_price(self):
        from observations_store import init_observations_db, migrate_normalized_combos

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "observations.sqlite3"
            init_observations_db(db_path)
            with closing(sqlite3.connect(db_path)) as conn, conn:
                base = (
                    "2099-12-01T10:00:00",
                    "round-migrate",
                    "international",
                    "PVG",
                    "KIX",
                    "2099-12-28",
                    27,
                    "economy",
                    "juhe",
                    "SQ0825|SQ622",
                    "SQ",
                    1,
                    1100,
                    None,
                    "v1",
                )
                cheaper = list(base)
                cheaper[9] = "SQ825|SQ0622"
                cheaper[12] = 900
                conn.execute(
                    """
                    INSERT INTO observations (
                        observed_at, round_id, route_type, origin_airport, dest_airport,
                        depart_date, days_to_departure, cabin_class, source, flight_combo,
                        airline, stops, price_cny, duration_min, method_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    base,
                )
                conn.execute(
                    """
                    INSERT INTO observations (
                        observed_at, round_id, route_type, origin_airport, dest_airport,
                        depart_date, days_to_departure, cabin_class, source, flight_combo,
                        airline, stops, price_cny, duration_min, method_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    cheaper,
                )

            result = migrate_normalized_combos(db_path)

            with closing(sqlite3.connect(db_path)) as conn, conn:
                rows = conn.execute("SELECT flight_combo, price_cny FROM observations").fetchall()
                pipe_count = conn.execute("SELECT COUNT(*) FROM observations WHERE instr(flight_combo, char(124)) > 0").fetchone()[0]
            self.assertEqual(result["merged"], 1)
            self.assertEqual(rows, [("SQ825+SQ622", 900.0)])
            self.assertEqual(pipe_count, 0)


if __name__ == "__main__":
    unittest.main()
