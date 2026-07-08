import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT days_to_departure, source, flight_combo, price_cny, method_version FROM observations"
                ).fetchone()
            self.assertEqual(row, (27, "hasdata", "MU225", 1234.0, "v1"))

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

            result = aggregator.collect(
                "PVG",
                "HKG",
                "2099-12-28",
                route_type="greater_china",
                round_id="round-gc",
                observations_db_path=db_path,
            )

            self.assertEqual(result["source_stats"]["after_dedup"], 1)
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT source, flight_combo, price_cny FROM observations ORDER BY source"
                ).fetchall()
            self.assertEqual(rows, [("hasdata", "MU225", 1000.0), ("juhe", "MU225", 1200.0)])

    def test_observation_write_failure_does_not_break_collect(self):
        from request_cache import reset_request_cache
        from sources.aggregator import FlightAggregator

        reset_request_cache()
        aggregator = FlightAggregator([ObservationSource("juhe", 1200)], [], route_type="domestic")

        with patch("sources.aggregator.append_observations", side_effect=sqlite3.OperationalError("locked")):
            result = aggregator.collect(
                "SHA",
                "PEK",
                "2099-12-28",
                route_type="domestic",
                round_id="round-fail",
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["source_stats"]["after_dedup"], 1)


if __name__ == "__main__":
    unittest.main()
