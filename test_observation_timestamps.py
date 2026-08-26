import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


class ObservationTimestampTest(unittest.TestCase):
    def test_canonical_conversion_uses_project_timezone_across_boundaries(self):
        from observation_time import canonicalize_observed_at

        utc_boundary = canonicalize_observed_at("2026-08-26T16:30:00Z")
        self.assertEqual(utc_boundary.observed_at_utc, "2026-08-26T16:30:00+00:00")
        self.assertEqual(utc_boundary.observed_day_shanghai, "2026-08-27")

        date_line = canonicalize_observed_at("2026-01-01T00:30:00+14:00")
        self.assertEqual(date_line.observed_at_utc, "2025-12-31T10:30:00+00:00")
        self.assertEqual(date_line.observed_day_shanghai, "2025-12-31")

        daylight_saving_source = canonicalize_observed_at(
            "2026-07-01T23:30:00-04:00"
        )
        self.assertEqual(
            daylight_saving_source.observed_at_utc,
            "2026-07-02T03:30:00+00:00",
        )
        self.assertEqual(daylight_saving_source.observed_day_shanghai, "2026-07-02")

    def test_naive_new_timestamp_is_explicitly_interpreted_as_shanghai(self):
        from observation_time import canonicalize_observed_at

        timestamp = canonicalize_observed_at("2026-08-27T00:30:00")

        self.assertEqual(timestamp.observed_at_utc, "2026-08-26T16:30:00+00:00")
        self.assertEqual(timestamp.observed_day_shanghai, "2026-08-27")

    def _legacy_db(self, path: Path) -> None:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                """
                CREATE TABLE observations (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  observed_at TEXT NOT NULL,
                  round_id TEXT NOT NULL,
                  route_type TEXT NOT NULL,
                  origin_airport TEXT NOT NULL,
                  dest_airport TEXT NOT NULL,
                  depart_date TEXT NOT NULL,
                  days_to_departure INTEGER NOT NULL,
                  cabin_class TEXT NOT NULL,
                  source TEXT NOT NULL,
                  flight_combo TEXT NOT NULL,
                  airline TEXT,
                  stops INTEGER,
                  duration_min INTEGER,
                  price_cny REAL NOT NULL,
                  method_version TEXT NOT NULL,
                  UNIQUE(round_id, source, origin_airport, dest_airport,
                         depart_date, flight_combo, cabin_class)
                )
                """
            )
            rows = [
                ("2026-08-26T16:30:00Z", "aware", "MU225"),
                ("2026-08-27T00:30:00", "naive", "MU226"),
                ("not-a-time", "invalid", "MU227"),
            ]
            for observed_at, round_id, combo in rows:
                connection.execute(
                    """
                    INSERT INTO observations (
                        observed_at, round_id, route_type, origin_airport,
                        dest_airport, depart_date, days_to_departure, cabin_class,
                        source, flight_combo, airline, stops, duration_min,
                        price_cny, method_version
                    ) VALUES (?, ?, 'international', 'PVG', 'KIX',
                              '2026-09-10', 14, 'economy', 'juhe', ?,
                              'MU', 0, 120, 900, 'v1')
                    """,
                    (observed_at, round_id, combo),
                )
            connection.commit()

    def test_legacy_migration_requires_explicit_naive_policy(self):
        from observations_store import (
            audit_observation_timestamps,
            migrate_observation_timestamps,
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "observations.sqlite3"
            self._legacy_db(db_path)

            audit = audit_observation_timestamps(db_path)
            self.assertEqual(
                audit["classification_counts"],
                {"aware": 1, "naive": 1, "invalid": 1},
            )
            self.assertEqual(audit["would_be_ambiguous"], 2)
            with self.assertRaisesRegex(ValueError, "assume_naive_shanghai"):
                migrate_observation_timestamps(db_path)

            result = migrate_observation_timestamps(
                db_path,
                assume_naive_shanghai=True,
            )

            self.assertEqual(result["migrated_aware"], 1)
            self.assertEqual(result["migrated_naive_shanghai"], 1)
            self.assertEqual(result["legacy_time_ambiguous"], 1)
            with closing(sqlite3.connect(db_path)) as connection:
                rows = connection.execute(
                    """
                    SELECT round_id, observed_at_utc, observed_day_shanghai,
                           legacy_time_ambiguous
                    FROM observations ORDER BY id
                    """
                ).fetchall()
            self.assertEqual(
                rows,
                [
                    ("aware", "2026-08-26T16:30:00+00:00", "2026-08-27", 0),
                    ("naive", "2026-08-26T16:30:00+00:00", "2026-08-27", 0),
                    ("invalid", None, None, 1),
                ],
            )

    def test_append_writes_both_canonical_fields_and_v2_days(self):
        from observations_store import append_observations

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "observations.sqlite3"
            append_observations(
                [{"flight_combo": "MU225", "price": 900}],
                db_path=db_path,
                round_id="round-v2",
                route_type="international",
                origin_airport="PVG",
                dest_airport="KIX",
                depart_date="2026-09-10",
                cabin_class="economy",
                source="juhe",
                observed_at="2026-08-26T16:30:00Z",
            )

            with closing(sqlite3.connect(db_path)) as connection:
                row = connection.execute(
                    """
                    SELECT observed_at_utc, observed_day_shanghai,
                           legacy_time_ambiguous, days_to_departure,
                           method_version
                    FROM observations
                    """
                ).fetchone()
            self.assertEqual(
                row,
                ("2026-08-26T16:30:00+00:00", "2026-08-27", 0, 14, "v2"),
            )

    def test_tcurve_prefers_canonical_day_and_excludes_ambiguous(self):
        from observations_store import init_observations_db
        from tcurve import build_tcurve

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "observations.sqlite3"
            init_observations_db(db_path)
            with closing(sqlite3.connect(db_path)) as connection:
                for round_id, combo, ambiguous in (
                    ("canonical", "MU225", 0),
                    ("ambiguous", "MU226", 1),
                ):
                    connection.execute(
                        """
                        INSERT INTO observations (
                            observed_at, observed_at_utc, observed_day_shanghai,
                            legacy_time_ambiguous, round_id, route_type,
                            origin_airport, dest_airport, depart_date,
                            days_to_departure, cabin_class, source, flight_combo,
                            airline, stops, duration_min, price_cny, method_version
                        ) VALUES (?, ?, ?, ?, ?, 'international', 'PVG', 'KIX',
                                  '2026-09-10', 15, 'economy', 'juhe', ?,
                                  'MU', 0, 120, 900, 'v2')
                        """,
                        (
                            "2026-08-26T16:30:00+00:00",
                            "2026-08-26T16:30:00+00:00" if not ambiguous else None,
                            "2026-08-27" if not ambiguous else None,
                            ambiguous,
                            round_id,
                            combo,
                        ),
                    )
                connection.commit()

            curve = build_tcurve(db_path, route="上海-大阪", min_sample=1)

            self.assertEqual(curve["method_version"], "tcurve_v2")
            self.assertEqual(curve["daily_cell_count"], 1)
            self.assertEqual(curve["ambiguous_excluded_count"], 1)
            self.assertEqual(curve["daily_cells"][0]["observed_day"], "2026-08-27")
            self.assertEqual(curve["daily_cells"][0]["days_to_departure"], 14)

    def test_method_versions_are_explicitly_raised(self):
        from method_registry import METHOD_VERSIONS

        self.assertEqual(METHOD_VERSIONS["obs_store"], "v2")
        self.assertEqual(METHOD_VERSIONS["tcurve"], "tcurve_v2")


if __name__ == "__main__":
    unittest.main()
