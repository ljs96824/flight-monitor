import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


OLD_PRICE_SCHEMA = """
CREATE TABLE flight_details (
  id INTEGER PRIMARY KEY AUTOINCREMENT, route TEXT, depart_date TEXT,
  snapshot_time TEXT, flight_combo TEXT, airline_summary TEXT, price REAL,
  total_duration_min INTEGER, stops INTEGER, route_summary TEXT,
  layover_summary TEXT, segments_json TEXT, data_source TEXT,
  price_source TEXT, constraint_fingerprint TEXT
);
CREATE TABLE roundtrip_price_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, route TEXT, depart_date TEXT,
  return_date TEXT, snapshot_time TEXT, outbound_lowest REAL,
  return_lowest REAL, roundtrip_lowest REAL, constraint_fingerprint TEXT,
  sources_json TEXT
);
CREATE TABLE push_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, subscription_key TEXT, route TEXT,
  depart_date TEXT, return_date TEXT, pushed_at TEXT, price REAL,
  confidence TEXT, channels TEXT, fare_status TEXT, push_type TEXT,
  constraint_fingerprint TEXT, constraint_sample_n INTEGER
);
"""


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RoundLineageMigrationTest(unittest.TestCase):
    def setUp(self):
        from observations_store import init_observations_db

        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name)
        self.observations = self.root / "observations.sqlite3"
        self.prices = self.root / "prices.db"
        init_observations_db(self.observations)
        with closing(sqlite3.connect(self.prices)) as connection, connection:
            connection.executescript(OLD_PRICE_SCHEMA)
        self.addCleanup(self.temp_dir.cleanup)

    def test_dry_run_changes_neither_database(self):
        from scripts.migrate_round_lineage import migrate_sections

        before = (_sha(self.observations), _sha(self.prices))
        result = migrate_sections(
            observations_db=self.observations,
            prices_db=self.prices,
        )
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual((_sha(self.observations), _sha(self.prices)), before)

    def test_collection_cells_section_does_not_touch_prices_schema(self):
        from scripts.migrate_round_lineage import migrate_sections

        price_before = _sha(self.prices)
        result = migrate_sections(
            observations_db=self.observations,
            prices_db=self.prices,
            section="collection_cells",
            write=True,
        )
        self.assertTrue(result["after"]["collection_cells"])
        self.assertFalse(any(result["after"]["price_lineage"].values()))
        self.assertEqual(_sha(self.prices), price_before)

    def test_price_lineage_section_does_not_create_collection_cells(self):
        from scripts.migrate_round_lineage import migrate_sections

        observations_before = _sha(self.observations)
        result = migrate_sections(
            observations_db=self.observations,
            prices_db=self.prices,
            section="price_lineage",
            write=True,
        )
        self.assertFalse(result["after"]["collection_cells"])
        self.assertTrue(all(result["after"]["price_lineage"].values()))
        self.assertEqual(_sha(self.observations), observations_before)

    def test_general_init_does_not_apply_explicit_lineage_migration(self):
        import storage

        original_path = storage.DB_PATH
        try:
            storage.DB_PATH = self.prices
            storage.init_db()
        finally:
            storage.DB_PATH = original_path

        with closing(sqlite3.connect(self.prices)) as connection:
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
        self.assertTrue(all("round_id" not in names for names in columns.values()))


if __name__ == "__main__":
    unittest.main()
