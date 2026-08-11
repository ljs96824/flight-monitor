import unittest
from unittest.mock import patch

from scripts.forecast_report import generate_report


class ForecastReportTest(unittest.TestCase):
    def test_no_data_is_explicit_and_has_no_empty_tables(self):
        with patch("scripts.forecast_report.load_tcurve_daily_cells", return_value=[]):
            text, payload = generate_report(db_path="unused", route="上海-大阪")
        self.assertIn("无可用非退化观测数据", text)
        self.assertNotIn("T | n", text)
        self.assertEqual(payload["status"], "无数据")

    def test_report_contains_scorecard_and_schema_gap(self):
        cells = []
        for offset in range(10):
            cells.append({"depart_date": "2026-10-01", "observed_day": f"2026-09-{10 + offset:02d}", "days_to_departure": 21 - offset, "min_price": 100 + offset, "degraded": False, "min_sources": ["juhe"]})
        patterns = {"combo_occurrence": [], "supply_mix": {"direct": 1, "transfer": 0, "n": 1, "basis": "基于组合结构"}, "departure_period": {"status": "字段不可得", "reason": "面板未存起飞时刻(obs_store v1),待schema扩展后自动点亮"}}
        with patch("scripts.forecast_report.load_tcurve_daily_cells", return_value=cells), patch("scripts.forecast_report.load_route_observations", return_value=[]), patch("scripts.forecast_report.build_route_patterns", return_value=patterns):
            text, _ = generate_report(db_path="unused", route="上海-大阪")
        self.assertIn("## shape(T)", text)
        self.assertIn("k=1:", text)
        self.assertIn("k=3:", text)
        self.assertIn("k=7:", text)
        self.assertIn("面板未存起飞时刻(obs_store v1)", text)


if __name__ == "__main__":
    unittest.main()
