import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch


SCHEMA = """
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


class TCurveTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "observations.sqlite3"
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(SCHEMA)
            connection.commit()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _insert_cell(
        self,
        *,
        depart_date,
        observed_day,
        price,
        origin="PVG",
        dest="KIX",
        route_type="international",
        sources=("hasdata", "juhe"),
    ):
        departure = date.fromisoformat(depart_date)
        observed = date.fromisoformat(observed_day)
        days = (departure - observed).days
        with closing(sqlite3.connect(self.db_path)) as connection:
            for index, source in enumerate(sources):
                connection.execute(
                    """
                    INSERT INTO observations (
                        observed_at, round_id, route_type, origin_airport,
                        dest_airport, depart_date, days_to_departure,
                        cabin_class, source, flight_combo, airline, stops,
                        duration_min, price_cny, method_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"{observed_day}T09:00:00",
                        f"r_{depart_date}_{observed_day}",
                        route_type,
                        origin,
                        dest,
                        depart_date,
                        days,
                        "economy",
                        source,
                        f"MU{days}{index}",
                        "测试航司",
                        0,
                        120,
                        float(price + (20 if source == "hasdata" else 0)),
                        "v1",
                    ),
                )
            connection.commit()

    def _point(self, curve, t_value):
        return next(item for item in curve["points"] if item["t"] == t_value)

    def test_pools_three_depart_dates_into_precise_t_median_and_iqr(self):
        from tcurve import build_tcurve

        departures = [date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12)]
        for departure, price_t10, price_t9 in zip(
            departures,
            (100, 120, 140),
            (90, 110, 160),
        ):
            self._insert_cell(
                depart_date=departure.isoformat(),
                observed_day=(departure - timedelta(days=10)).isoformat(),
                price=price_t10,
            )
            self._insert_cell(
                depart_date=departure.isoformat(),
                observed_day=(departure - timedelta(days=9)).isoformat(),
                price=price_t9,
            )

        curve = build_tcurve(self.db_path, route="上海-大阪", min_sample=3)

        t10 = self._point(curve, 10)
        self.assertEqual((t10["n"], t10["median"], t10["p25"], t10["p75"]), (3, 120, 110, 130))
        t9 = self._point(curve, 9)
        self.assertEqual((t9["n"], t9["median"], t9["p25"], t9["p75"]), (3, 110, 100, 135))
        self.assertEqual(curve["depart_dates"], ["2026-08-10", "2026-08-11", "2026-08-12"])
        self.assertEqual(curve["method_version"], "tcurve_v1")

    def test_sample_gate_is_insufficient_at_four_and_open_at_five(self):
        from tcurve import build_tcurve

        departures = [date(2026, 9, 1) + timedelta(days=index) for index in range(5)]
        for index, departure in enumerate(departures):
            if index < 4:
                self._insert_cell(
                    depart_date=departure.isoformat(),
                    observed_day=(departure - timedelta(days=20)).isoformat(),
                    price=100 + index * 10,
                )
            self._insert_cell(
                depart_date=departure.isoformat(),
                observed_day=(departure - timedelta(days=21)).isoformat(),
                price=200 + index * 10,
            )

        curve = build_tcurve(self.db_path, route="上海-大阪", min_sample=5)

        t20 = self._point(curve, 20)
        self.assertEqual(t20["n"], 4)
        self.assertFalse(t20["sufficient"])
        self.assertIsNone(t20["median"])
        t21 = self._point(curve, 21)
        self.assertEqual(t21["n"], 5)
        self.assertTrue(t21["sufficient"])
        self.assertEqual(t21["median"], 220)

    def test_degraded_daily_cell_is_excluded_by_default_and_optional(self):
        from tcurve import build_tcurve

        self._insert_cell(
            depart_date="2026-09-20",
            observed_day="2026-08-21",
            price=100,
        )
        self._insert_cell(
            depart_date="2026-09-21",
            observed_day="2026-08-22",
            price=150,
            sources=("hasdata",),
        )

        default_curve = build_tcurve(self.db_path, route="上海-大阪", min_sample=1)
        included_curve = build_tcurve(
            self.db_path,
            route="上海-大阪",
            min_sample=1,
            include_degraded=True,
        )

        self.assertEqual(default_curve["daily_cell_count"], 2)
        self.assertEqual(default_curve["degraded_count"], 1)
        self.assertEqual(default_curve["included_cell_count"], 1)
        degraded_cell = next(cell for cell in default_curve["daily_cells"] if cell["degraded"])
        self.assertEqual(degraded_cell["source_coverage"], ["hasdata"])
        self.assertEqual(degraded_cell["expected_sources"], ["juhe"])
        self.assertEqual(self._point(default_curve, 30)["n"], 1)
        self.assertEqual(included_curve["included_cell_count"], 2)
        self.assertTrue(included_curve["include_degraded"])

    def test_city_fold_and_airport_pair_filter_use_the_same_route_key(self):
        from tcurve import build_tcurve

        self._insert_cell(
            depart_date="2026-09-10",
            observed_day="2026-08-31",
            price=100,
            origin="PVG",
            dest="KIX",
        )
        self._insert_cell(
            depart_date="2026-09-11",
            observed_day="2026-09-01",
            price=120,
            origin="SHA",
            dest="ITM",
        )

        city_curve = build_tcurve(self.db_path, route="上海-大阪", min_sample=1)
        pair_curve = build_tcurve(
            self.db_path,
            route="上海-大阪",
            airport_pair=("PVG", "KIX"),
            min_sample=1,
        )

        self.assertEqual(city_curve["included_cell_count"], 2)
        self.assertEqual(self._point(city_curve, 10)["n"], 2)
        self.assertEqual(pair_curve["included_cell_count"], 1)
        self.assertEqual(pair_curve["airport_pair"], "PVG-KIX")

    def test_engine_and_report_leave_database_bytes_unchanged(self):
        from scripts.tcurve_report import generate_report
        from tcurve import build_tcurve

        self._insert_cell(
            depart_date="2026-09-20",
            observed_day="2026-08-21",
            price=100,
        )
        before = self.db_path.read_bytes()

        build_tcurve(self.db_path, route="上海-大阪", min_sample=1)
        report = generate_report(
            db_path=self.db_path,
            route="上海-大阪",
            min_sample=1,
        )

        self.assertIn("上海→大阪", report)
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_expected_sources_come_from_route_profiles_without_enrichment(self):
        from tcurve import expected_search_sources

        self.assertEqual(expected_search_sources("domestic"), {"juhe"})
        self.assertEqual(expected_search_sources("international"), {"juhe"})
        self.assertEqual(expected_search_sources("greater_china"), {"juhe"})

    def test_notification_curve_omits_raw_daily_cells(self):
        from tcurve import build_notification_tcurve

        self._insert_cell(
            depart_date="2026-09-20",
            observed_day="2026-08-21",
            price=100,
        )

        curve = build_notification_tcurve(
            {
                "origin_airports_active": ["PVG"],
                "destination_airports_active": ["KIX"],
                "depart_date": "2026-09-20",
            },
            db_path=self.db_path,
            min_sample=1,
            as_of_date=date(2026, 8, 21),
        )

        self.assertNotIn("daily_cells", curve)
        self.assertEqual(curve["included_cell_count"], 1)
        self.assertEqual(curve["current_t"], 30)

    def test_email_gate_skips_two_qualified_cells_and_renders_three(self):
        import notifier

        base_curve = {
            "route": "上海-大阪",
            "coverage": {"t_min": 10, "t_max": 12},
            "current_t": 11,
            "degraded_count": 1,
            "include_degraded": False,
            "method_version": "tcurve_v1",
        }
        point = lambda t, price: {
            "t": t,
            "n": 5,
            "median": price,
            "p25": price - 10,
            "p75": price + 10,
            "sufficient": True,
            "status": "ok",
        }

        with patch.object(notifier, "safe_log") as log:
            _, skipped_html = notifier.render_email(
                {"route": "上海→大阪", "tcurve": {**base_curve, "points": [point(10, 100), point(11, 110)]}}
            )
        self.assertNotIn("提前购买参考(同航线历史观测)", skipped_html)
        self.assertTrue(
            any("[T曲线] 样本不足 n合格格数=2 跳过渲染" in str(call.args[0]) for call in log.call_args_list)
        )

        _, rendered_html = notifier.render_email(
            {
                "route": "上海→大阪",
                "tcurve": {
                    **base_curve,
                    "points": [point(10, 100), point(11, 110), point(12, 120)],
                },
            }
        )
        self.assertIn("提前购买参考(同航线历史观测)", rendered_html)
        self.assertIn("本订阅当前 T=11 天", rendered_html)
        self.assertIn("已剔除1个源覆盖不完整日格", rendered_html)


if __name__ == "__main__":
    unittest.main()
