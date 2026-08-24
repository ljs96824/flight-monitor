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

    def test_low_sample_shape_is_hidden_by_default_and_visible_only_in_diagnostic_mode(self):
        cells = [
            {
                "depart_date": f"2026-10-{day:02d}",
                "observed_day": "2026-09-01",
                "days_to_departure": 30,
                "min_price": 100 + day,
                "degraded": False,
                "min_sources": ["juhe"],
            }
            for day in range(5, 9)
        ]
        patterns = {"combo_occurrence": [], "supply_mix": {"direct": 0, "transfer": 0, "n": 0, "basis": "基于组合结构"}, "departure_period": {"status": "字段不可得", "reason": "面板未存起飞时刻(obs_store v1),待schema扩展后自动点亮"}}
        patches = (
            patch("scripts.forecast_report.load_tcurve_daily_cells", return_value=cells),
            patch("scripts.forecast_report.load_route_observations", return_value=[]),
            patch("scripts.forecast_report.build_route_patterns", return_value=patterns),
        )
        with patches[0], patches[1], patches[2]:
            default_text, _ = generate_report(db_path="unused", route="上海-大阪")
        with patch("scripts.forecast_report.load_tcurve_daily_cells", return_value=cells), patch("scripts.forecast_report.load_route_observations", return_value=[]), patch("scripts.forecast_report.build_route_patterns", return_value=patterns):
            diagnostic_text, _ = generate_report(db_path="unused", route="上海-大阪", diagnostic=True)

        self.assertIn("本报告为内部诊断输出;技能门=未过;预测未进入用户推送", default_text)
        self.assertIn("30 | 4 | 样本不足(n=4) | 样本不足(n=4) | 样本不足(n=4)", default_text)
        self.assertNotIn("原始值,不可用于判断", default_text)
        self.assertIn("原始值,不可用于判断", diagnostic_text)

    def test_report_uses_corrected_semantics_and_reliability_bottleneck(self):
        cells = [
            {
                "depart_date": f"2026-10-{day:02d}",
                "observed_day": "2026-09-01",
                "days_to_departure": 30,
                "min_price": 100 + day,
                "degraded": False,
                "min_sources": ["juhe"],
            }
            for day in range(5, 10)
        ]
        patterns = {
            "combo_occurrence": [{"combo": "MU225", "label": "在33次有效观测中均出现(100%)"}],
            "supply_mix": {"direct": 1850, "transfer": 17316, "n": 19166, "basis": "基于组合结构"},
            "departure_period": {"status": "字段不可得", "reason": "面板未存起飞时刻(obs_store v1),待schema扩展后自动点亮"},
        }
        with patch("scripts.forecast_report.load_tcurve_daily_cells", return_value=cells), patch("scripts.forecast_report.load_route_observations", return_value=[]), patch("scripts.forecast_report.build_route_patterns", return_value=patterns):
            text, payload = generate_report(db_path="unused", route="上海-大阪")

        self.assertIn("价格基准 level=CNY", text)
        self.assertNotIn("× n=", text)
        self.assertIn("候选组合结构:直飞组合1,850 / 中转组合17,316(共19,166)", text)
        self.assertIn("反映搜索结果组合结构,不代表座位库存或真实运力;中转组合存在拼接膨胀", text)
        self.assertIn("市场最低参考价·与用户筛选无关", text)
        self.assertIn("暂不提供预测", text)
        self.assertIn("瓶颈=", text)
        self.assertTrue(all("overall_reliability" in item for item in payload["forecasts"].values()))

if __name__ == "__main__":
    unittest.main()
