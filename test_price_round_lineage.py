import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


class PriceRoundLineageTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "prices.db"
        self.addCleanup(self.temp_dir.cleanup)

    def test_new_price_records_store_current_round_and_history_stays_null(self):
        import storage
        from observations_store import reset_current_round, set_current_round

        with patch.object(storage, "DB_PATH", self.db_path):
            storage.init_db()
            with closing(sqlite3.connect(self.db_path)) as connection, connection:
                connection.execute(
                    """
                    INSERT INTO flight_details (
                        route, depart_date, snapshot_time, flight_combo, price
                    ) VALUES ('PVGKIX', '2026-10-01', '2026-08-01T00:00:00', 'MU225', 1)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO roundtrip_price_history (
                        route, depart_date, return_date, snapshot_time,
                        roundtrip_lowest
                    ) VALUES ('PVGKIX', '2026-10-01', '2026-10-06',
                              '2026-08-01T00:00:00', 2)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO push_snapshots (
                        route, depart_date, pushed_at, price
                    ) VALUES ('PVGKIX', '2026-10-01', '2026-08-01T00:00:00', 2)
                    """
                )

            tokens = set_current_round("round-lineage", Path(self.temp_dir.name) / "obs.db")
            try:
                storage.save_flight_details(
                    "PVGKIX",
                    "2026-10-01",
                    [{"flight_combo": "MU225", "price": 4883}],
                )
                storage.save_roundtrip_snapshot(
                    "PVGKIX",
                    "2026-10-01",
                    "2026-10-06",
                    4883,
                    7220,
                    12103,
                    "2026-08-26T12:00:00",
                )
                storage.save_push_snapshot(
                    "PVGKIX",
                    "2026-10-01",
                    "2026-10-06",
                    12103,
                )
            finally:
                reset_current_round(tokens)

            with closing(sqlite3.connect(self.db_path)) as connection:
                columns = {
                    table: {
                        row[1]
                        for row in connection.execute(
                            f"PRAGMA table_info({table})"
                        ).fetchall()
                    }
                    for table in (
                        "flight_details",
                        "roundtrip_price_history",
                        "push_snapshots",
                    )
                }
                values = {
                    table: connection.execute(
                        f"SELECT round_id FROM {table} ORDER BY id"
                    ).fetchall()
                    for table in columns
                }

        self.assertTrue(all("round_id" in names for names in columns.values()))
        self.assertEqual(values["flight_details"], [(None,), ("round-lineage",)])
        self.assertEqual(
            values["roundtrip_price_history"], [(None,), ("round-lineage",)]
        )
        self.assertEqual(values["push_snapshots"], [(None,), ("round-lineage",)])

    def test_missing_round_id_is_null_and_logged_as_degraded(self):
        import storage

        with patch.object(storage, "_lineage_degraded_logged", False), patch.object(
            storage, "safe_log"
        ) as logged:
            self.assertIsNone(storage._current_round_id())
            self.assertIsNone(storage._current_round_id())

        logged.assert_called_once_with(
            "[价格lineage降级] round_id不可用,本记录保持NULL"
        )


if __name__ == "__main__":
    unittest.main()
