import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from scripts.audit_permission_pollution import (
    build_audit,
    infer_epoch_candidates,
    load_failure_requests,
)


OBSERVATIONS_SCHEMA = """
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
  method_version TEXT NOT NULL
)
"""


class PermissionPollutionAuditTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.observations = self.root / "observations.sqlite3"
        self.prices = self.root / "prices.db"
        self.logs = self.root / "logs"
        self.logs.mkdir()
        with closing(sqlite3.connect(self.observations)) as connection:
            connection.execute(OBSERVATIONS_SCHEMA)
            connection.commit()
        with closing(sqlite3.connect(self.prices)) as connection:
            connection.executescript(
                """
                CREATE TABLE flight_details (
                  snapshot_time TEXT,
                  constraint_fingerprint TEXT
                );
                CREATE TABLE roundtrip_price_history (
                  snapshot_time TEXT,
                  constraint_fingerprint TEXT
                );
                CREATE TABLE push_snapshots (
                  pushed_at TEXT,
                  constraint_fingerprint TEXT
                );
                """
            )
            connection.commit()

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _sha256(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def _insert_observation(
        self,
        *,
        round_id,
        observed_at,
        origin,
        destination,
        depart_date,
        cabin,
        source,
        combo,
        price,
    ):
        observed_day = observed_at[:10]
        days = (
            __import__("datetime").date.fromisoformat(depart_date)
            - __import__("datetime").date.fromisoformat(observed_day)
        ).days
        with closing(sqlite3.connect(self.observations)) as connection:
            connection.execute(
                """
                INSERT INTO observations (
                  observed_at, round_id, route_type, origin_airport, dest_airport,
                  depart_date, days_to_departure, cabin_class, source,
                  flight_combo, airline, stops, duration_min, price_cny,
                  method_version
                ) VALUES (?, ?, 'international', ?, ?, ?, ?, ?, ?, ?, 'MU', 0, 120, ?, 'v1')
                """,
                (
                    observed_at,
                    round_id,
                    origin,
                    destination,
                    depart_date,
                    days,
                    cabin,
                    source,
                    combo,
                    price,
                ),
            )
            connection.commit()

    def _write_log(self, round_id, observed_at="2026-08-17T21:00:13"):
        path = self.logs / "20260817.log"
        path.write_text(
            "\n".join(
                [
                    f"===== [轮档开始] round_id={round_id} observed_at={observed_at} =====",
                    "[采集失败入池] 源=juhe 航线=PVG->KIX 日期=2026-10-01 "
                    "原因=PermissionError: [WinError 5] Access is denied: data/cache/x.json",
                    "[采集失败入池] 源=juhe 航线=KIX->PVG 日期=2026-10-06 "
                    "原因=PermissionError: [WinError 5] Access is denied: data/cache/y.json\\nextra",
                    f"===== [轮档结束] round_id={round_id} =====",
                ]
            ),
            encoding="utf-8",
        )

    def test_failure_parser_binds_requests_to_round_and_handles_literal_newline(self):
        round_id = "collection_20260817T210013799318"
        self._write_log(round_id)

        rows = load_failure_requests(self.logs, [round_id])

        self.assertEqual(len(rows), 2)
        self.assertEqual({row["round_id"] for row in rows}, {round_id})
        self.assertEqual(
            {(row["origin"], row["destination"], row["depart_date"]) for row in rows},
            {("PVG", "KIX", "2026-10-01"), ("KIX", "PVG", "2026-10-06")},
        )

    def test_business_rows_are_exactly_reported_but_do_not_pollute_economy_stats(self):
        round_id = "collection_20260817T210013799318"
        self._write_log(round_id)
        self._insert_observation(
            round_id=round_id,
            observed_at="2026-08-17T21:00:37",
            origin="PVG",
            destination="KIX",
            depart_date="2026-10-01",
            cabin="business",
            source="serpapi",
            combo="MU225",
            price=5683,
        )

        audit = build_audit(
            observations_db=self.observations,
            prices_db=self.prices,
            logs_dir=self.logs,
            round_ids=[round_id],
        )

        self.assertEqual(audit["exact_observations"]["row_count"], 1)
        self.assertEqual(audit["exact_observations"]["economy_row_count"], 0)
        self.assertEqual(audit["tcurve_impact"]["affected_n_total"], 0)
        self.assertEqual(audit["agreement_impact"]["affected_pair_count"], 0)
        self.assertFalse(audit["conclusion"]["unmarked_pollution_confirmed"])

    def test_same_day_unaffected_cell_is_not_counted_as_affected_pollution(self):
        round_id = "collection_20260817T210013799318"
        self._write_log(round_id)
        self._insert_observation(
            round_id="basket_20260817T194300",
            observed_at="2026-08-17T19:43:00",
            origin="PVG",
            destination="KIX",
            depart_date="2026-10-01",
            cabin="economy",
            source="juhe",
            combo="MU225",
            price=5000,
        )

        audit = build_audit(
            observations_db=self.observations,
            prices_db=self.prices,
            logs_dir=self.logs,
            round_ids=[round_id],
        )
        outbound = next(
            cell for cell in audit["affected_cells"] if cell["direction"] == "去程"
        )

        self.assertEqual(outbound["all_day_row_count"], 1)
        self.assertEqual(outbound["affected_round_economy_rows"], 0)
        self.assertEqual(outbound["global_min"], 5000)
        self.assertFalse(outbound["degraded"])
        self.assertTrue(outbound["entered_tcurve"])
        self.assertFalse(outbound["affected_round_contributed"])
        self.assertEqual(audit["tcurve_impact"]["same_day_other_round_t_values"], [45])
        self.assertEqual(audit["tcurve_impact"]["missing_cell_t_values"], [50])
        self.assertEqual(audit["tcurve_impact"]["missing_cell_count"], 1)

    def test_pre_retirement_hasdata_only_cell_is_degraded_and_excluded(self):
        round_id = "collection_20260813T210009452471"
        path = self.logs / "20260813.log"
        path.write_text(
            "\n".join(
                [
                    f"===== [轮档开始] round_id={round_id} =====",
                    "[采集失败入池] 源=juhe 航线=PVG->KIX 日期=2026-10-01 "
                    "原因=PermissionError: [WinError 5] denied",
                    f"===== [轮档结束] round_id={round_id} =====",
                ]
            ),
            encoding="utf-8",
        )
        self._insert_observation(
            round_id="unaffected_round",
            observed_at="2026-08-13T10:00:00",
            origin="PVG",
            destination="KIX",
            depart_date="2026-10-01",
            cabin="economy",
            source="hasdata",
            combo="MU225",
            price=5100,
        )

        audit = build_audit(
            observations_db=self.observations,
            prices_db=self.prices,
            logs_dir=self.logs,
            round_ids=[round_id],
        )
        cell = audit["affected_cells"][0]

        self.assertEqual(cell["expected_sources"], ["hasdata", "juhe"])
        self.assertTrue(cell["degraded"])
        self.assertFalse(cell["entered_tcurve"])

    def test_epoch_candidates_are_explicitly_time_inferred(self):
        round_id = "collection_20260817T210013799318"
        with closing(sqlite3.connect(self.prices)) as connection:
            connection.execute(
                "INSERT INTO flight_details VALUES ('2026-08-17T21:00:55', 'fp-a')"
            )
            connection.execute(
                "INSERT INTO roundtrip_price_history VALUES ('2026-08-17T21:03:00', 'fp-a')"
            )
            connection.execute(
                "INSERT INTO push_snapshots VALUES ('2026-08-17T22:30:00', 'fp-late')"
            )
            connection.commit()

        result = infer_epoch_candidates(self.prices, [round_id])

        self.assertEqual(result[0]["evidence_level"], "时间链候选")
        self.assertEqual(result[0]["counts"]["flight_details"], 1)
        self.assertEqual(result[0]["counts"]["roundtrip_price_history"], 1)
        self.assertEqual(result[0]["counts"]["push_snapshots"], 0)
        self.assertEqual(result[0]["fingerprints"], ["fp-a"])

    def test_audit_is_byte_for_byte_read_only(self):
        round_id = "collection_20260817T210013799318"
        self._write_log(round_id)
        before = (self._sha256(self.observations), self._sha256(self.prices))

        build_audit(
            observations_db=self.observations,
            prices_db=self.prices,
            logs_dir=self.logs,
            round_ids=[round_id],
        )

        after = (self._sha256(self.observations), self._sha256(self.prices))
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
