import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


class SampleRoleConsumptionTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "observations.sqlite3"
        self.addCleanup(self.temp_dir.cleanup)

    def _insert_observation(self, *, round_id, observed_day, depart_date, price):
        from observations_store import append_observations

        append_observations(
            [{"flight_combo": f"MU{price}", "price": price}],
            round_id=round_id,
            route_type="international",
            origin_airport="PVG",
            dest_airport="KIX",
            depart_date=depart_date,
            cabin_class="economy",
            source="juhe",
            observed_at=f"{observed_day}T09:00:00+08:00",
            db_path=self.db_path,
        )

    def test_tcurve_discloses_roles_and_forecast_excludes_probe_cells(self):
        from collection_ledger import init_collection_ledger
        from forecast import filter_forecast_cells
        from tcurve import build_tcurve

        self._insert_observation(
            round_id="round-anchor",
            observed_day="2026-08-20",
            depart_date="2026-09-08",
            price=1000,
        )
        self._insert_observation(
            round_id="round-probe",
            observed_day="2026-08-21",
            depart_date="2026-09-18",
            price=900,
        )
        init_collection_ledger(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            for round_id, observed_day, depart_date, role in (
                (
                    "round-anchor",
                    "2026-08-20",
                    "2026-09-08",
                    "trajectory_anchor",
                ),
                (
                    "round-probe",
                    "2026-08-21",
                    "2026-09-18",
                    "cross_sectional_probe",
                ),
            ):
                connection.execute(
                    """
                    INSERT INTO collection_cells (
                        round_id, request_fingerprint, planned_at_utc,
                        observed_day_shanghai, sample_role, route_type,
                        origin_airport, dest_airport, depart_date, cabin_class,
                        source, execution_status, raw_result_count,
                        valid_result_count, written_count, method_version
                    ) VALUES (?, ?, '2026-08-20T01:00:00Z', ?, ?,
                              'international', 'PVG', 'KIX', ?, 'economy',
                              'juhe', 'success', 1, 1, 1,
                              'collection_ledger_v1')
                    """,
                    (round_id, f"fp-{round_id}", observed_day, role, depart_date),
                )

        curve = build_tcurve(
            self.db_path,
            route="上海-大阪",
            min_sample=1,
        )
        cells = curve["daily_cells"]

        self.assertEqual(
            curve["sample_role_counts"],
            {"cross_sectional_probe": 1, "trajectory_anchor": 1},
        )
        self.assertEqual(len(cells), 2)
        self.assertEqual(curve["collection_state_counts"], {"valid": 2})
        filtered = filter_forecast_cells(cells)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["sample_roles"], ["trajectory_anchor"])

    def test_rows_without_ledger_are_legacy(self):
        from tcurve import build_tcurve

        self._insert_observation(
            round_id="round-old",
            observed_day="2026-08-20",
            depart_date="2026-09-08",
            price=1000,
        )
        curve = build_tcurve(self.db_path, route="上海-大阪", min_sample=1)
        self.assertEqual(curve["sample_role_counts"], {"legacy": 1})
        self.assertEqual(curve["daily_cells"][0]["sample_roles"], ["legacy"])

    def test_method_versions_record_role_semantics(self):
        from method_registry import METHOD_VERSIONS

        self.assertEqual(METHOD_VERSIONS["collection_ledger"], "collection_ledger_v1")
        self.assertEqual(METHOD_VERSIONS["forecast"], "forecast_v2")


if __name__ == "__main__":
    unittest.main()
