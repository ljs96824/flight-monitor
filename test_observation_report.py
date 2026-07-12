import sqlite3
import tempfile
import unittest
from pathlib import Path


from analytics.report_lib import (
    build_reference_rows,
    build_source_pairs,
    fold_daily_cells,
    load_observations,
    render_consistency_block,
    summarize_source_consistency,
)


def _row(
    *,
    row_id,
    observed_at,
    round_id,
    origin,
    dest,
    depart_date,
    source,
    combo,
    price,
    stops=0,
):
    return {
        "id": row_id,
        "observed_at": observed_at,
        "round_id": round_id,
        "route_type": "domestic",
        "origin_airport": origin,
        "dest_airport": dest,
        "depart_date": depart_date,
        "days_to_departure": 20,
        "cabin_class": "economy",
        "source": source,
        "flight_combo": combo,
        "airline": "MU",
        "stops": stops,
        "duration_min": 120,
        "price_cny": price,
        "method_version": "v1",
    }


class ObservationReportTest(unittest.TestCase):
    def test_daily_fold_merges_airports_and_same_day_rounds(self):
        rows = [
            _row(row_id=1, observed_at="2026-07-10T08:00:00", round_id="r1", origin="SHA", dest="PEK", depart_date="2026-07-31", source="juhe", combo="MU1", price=800),
            _row(row_id=2, observed_at="2026-07-10T08:01:00", round_id="r1", origin="PVG", dest="PKX", depart_date="2026-07-31", source="juhe", combo="MU2", price=900),
            _row(row_id=3, observed_at="2026-07-10T18:00:00", round_id="r2", origin="SHA", dest="PKX", depart_date="2026-07-31", source="juhe", combo="MU3", price=700),
            _row(row_id=4, observed_at="2026-07-10T18:01:00", round_id="r2", origin="PVG", dest="PEK", depart_date="2026-07-31", source="juhe", combo="MU4", price=1000),
        ]

        cells = fold_daily_cells(rows)

        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0]["origin_city"], "上海")
        self.assertEqual(cells[0]["dest_city"], "北京")
        self.assertEqual(cells[0]["min_price"], 700)
        self.assertEqual(cells[0]["median_price"], 850)
        self.assertEqual(cells[0]["obs_rows"], 4)
        self.assertEqual(cells[0]["rounds"], 2)
        self.assertEqual(cells[0]["days_to_departure"], 21)

    def test_reference_rows_use_latest_observed_day_for_current_layers(self):
        cells = [
            {
                "origin_city": "上海",
                "dest_city": "北京",
                "depart_date": "2026-07-31",
                "observed_day": "2026-07-01",
                "days_to_departure": 30,
                "min_price": 500,
                "median_price": 650,
                "obs_rows": 20,
                "rounds": 1,
            },
            {
                "origin_city": "上海",
                "dest_city": "北京",
                "depart_date": "2026-07-31",
                "observed_day": "2026-07-10",
                "days_to_departure": 21,
                "min_price": 700,
                "median_price": 800,
                "obs_rows": 30,
                "rounds": 2,
            },
        ]

        refs = build_reference_rows(cells)

        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["historical_low"], 500)
        self.assertEqual(refs[0]["recent_7d_low"], 700)
        self.assertEqual(refs[0]["current_low"], 700)
        self.assertEqual(refs[0]["current_median"], 800)
        self.assertEqual(refs[0]["current_observed_day"], "2026-07-10")

    def test_source_consistency_keeps_both_price_directions_and_gates_small_cells(self):
        rows = [
            _row(row_id=1, observed_at="2026-07-10T08:00:00", round_id="r1", origin="SHA", dest="PEK", depart_date="2026-07-31", source="hasdata", combo="MU1", price=1100),
            _row(row_id=2, observed_at="2026-07-10T08:00:01", round_id="r1", origin="PVG", dest="PKX", depart_date="2026-07-31", source="juhe", combo="MU1", price=1000),
            _row(row_id=3, observed_at="2026-07-10T09:00:00", round_id="r2", origin="SHA", dest="PEK", depart_date="2026-07-31", source="hasdata", combo="MU2", price=900),
            _row(row_id=4, observed_at="2026-07-10T09:00:01", round_id="r2", origin="PVG", dest="PKX", depart_date="2026-07-31", source="juhe", combo="MU2", price=1000),
            _row(row_id=5, observed_at="2026-07-10T10:00:00", round_id="r3", origin="PEK", dest="SHA", depart_date="2026-07-31", source="hasdata", combo="MU3", price=1000),
            _row(row_id=6, observed_at="2026-07-10T10:00:01", round_id="r3", origin="PKX", dest="PVG", depart_date="2026-07-31", source="juhe", combo="MU3", price=1000),
        ]

        pairs = build_source_pairs(rows)
        stats = summarize_source_consistency(pairs, min_n=2)
        direct_outbound = next(item for item in stats if item["stop_kind"] == "direct" and item["direction"] == "outbound")
        direct_return = next(item for item in stats if item["stop_kind"] == "direct" and item["direction"] == "return")

        self.assertEqual(direct_outbound["pair_count"], 2)
        self.assertTrue(direct_outbound["sufficient"])
        self.assertEqual(direct_outbound["hasdata_high_count"], 1)
        self.assertEqual(direct_outbound["juhe_high_count"], 1)
        self.assertAlmostEqual(direct_outbound["median_gap_pct"], 10.5556, places=3)
        self.assertFalse(direct_return["sufficient"])
        self.assertIsNone(direct_return["median_gap_pct"])

        gated = render_consistency_block(stats, min_n=2)
        self.assertIn("直飞/返程: 数据不足(n=1)", gated)

    def test_readonly_loader_does_not_change_database_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.sqlite3"
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    """
                    CREATE TABLE observations (
                      id INTEGER PRIMARY KEY,
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
                )
                conn.execute(
                    "INSERT INTO observations VALUES (1, '2026-07-10T08:00:00', 'r1', 'domestic', 'SHA', 'PEK', '2026-07-31', 21, 'economy', 'juhe', 'MU1', 'MU', 0, 120, 800, 'v1')"
                )
                conn.commit()
            finally:
                conn.close()
            before = path.stat()

            rows = load_observations(path)

            after = path.stat()
            self.assertEqual(len(rows), 1)
            self.assertEqual(after.st_size, before.st_size)
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)


if __name__ == "__main__":
    unittest.main()
