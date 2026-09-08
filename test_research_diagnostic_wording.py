"""Wording contracts; synthetic inputs do not change report calculations."""

from copy import deepcopy
import unittest
from unittest.mock import patch


def _curve():
    return {
        "origin_city": "上海", "dest_city": "大阪",
        "price_caliber": "单人单程CNY含税", "method_version": "tcurve_v1",
        "airport_pair": None, "daily_cell_count": 4, "included_cell_count": 4,
        "included_depart_dates": [], "degraded_count": 0,
        "degraded_excluded_count": 0, "coverage": {}, "points": [],
        "lowest_median_t_ranges": [], "min_sample": 5, "daily_cells": [],
        "sample_role_counts": {"trajectory_anchor": 4, "cross_sectional_probe": 3},
        "collection_state_counts": {"success": 4},
    }


class ResearchDiagnosticWordingTest(unittest.TestCase):
    def test_role_line_distinguishes_memberships_from_included_cells(self):
        from scripts.tcurve_report import generate_report

        for roles, included in (({"trajectory_anchor": 4, "cross_sectional_probe": 3}, 4), ({}, 0)):
            with self.subTest(roles=roles):
                curve = _curve()
                curve.update(sample_role_counts=roles, included_cell_count=included)
                before = deepcopy(curve)
                with patch("scripts.tcurve_report.build_tcurve", return_value=curve) as build:
                    text = generate_report(db_path="unused", route="上海-大阪", quality_cells=[])
                line = next(line for line in text.splitlines() if line.startswith("样本角色构成:"))
                self.assertIn("每个日格的每个角色各计一次", line)
                total = sum(roles.values())
                self.assertIn(f"角色标签总数={total}", line)
                self.assertIn(f"纳入日格总数={included}", line)
                self.assertIn(f"额外角色归属次数={total - included}", line)
                self.assertNotIn("多角色日格", line)
                self.assertEqual(curve, before)
                build.assert_called_once()

    def test_quality_headings_explain_directed_prices_and_reverse_supplement(self):
        from scripts.tcurve_report import generate_report

        with patch("scripts.tcurve_report.build_tcurve", return_value=_curve()):
            text = generate_report(db_path="unused", route="上海-大阪", quality_cells=[])
        for prefix in ("缺失格清单", "degraded格清单"):
            with self.subTest(prefix=prefix):
                line = next(line for line in text.splitlines() if line.startswith(prefix))
                self.assertIn("有向价格统计", line)
                self.assertIn("含反向的 PermissionError 补充诊断", line)

    def test_source_coverage_discloses_binary_predicate_without_reordering_values(self):
        from scripts.forecast_report import generate_report

        cells = [{"depart_date": "2026-10-01", "observed_day": "2026-09-01",
                  "days_to_departure": 30, "min_price": 680,
                  "degraded": False, "min_sources": ["juhe"]}]
        for covered in (False, True):
            with self.subTest(covered=covered):
                with (
                    patch("scripts.forecast_report.load_tcurve_daily_cells", return_value=cells),
                    patch("scripts.forecast_report.load_route_observations", return_value=[]),
                    patch("scripts.forecast_report.build_route_patterns", return_value={
                        "combo_occurrence": [],
                        "supply_mix": {"direct": 0, "transfer": 0, "n": 0, "basis": "合成空组合"},
                        "departure_period": {"status": "字段不可得", "reason": "合成空组合"},
                    }),
                    patch("scripts.forecast_report.source_coverage_for_departure", return_value=covered),
                ):
                    text, report = generate_report(db_path="unused", route="上海-大阪", as_of_day="2026-09-01")
                components = report["forecasts"]["2026-10-01"]["overall_reliability"]["components"]
                line = next(line for line in text.splitlines() if " 分量=" in line)
                legend = "(二元门:相关日格非空且全部非退化)"
                self.assertIn(f"source_coverage:{int(covered)}{legend}", line)
                rendered = line.split(" 分量=", 1)[1].split(" 瓶颈=", 1)[0]
                expected = ";".join(f"{name}:{item['value']}" for name, item in components.items())
                self.assertEqual(rendered.replace(legend, ""), expected)

    def test_cold_start_exit_does_not_exclude_floor_application(self):
        from research_readiness import render_readiness_summary

        for applied in (False, True):
            with self.subTest(applied=applied):
                details = {"daily_counts": [], "reserve_window_days": 7,
                           "cold_start_active": False, "minimum_floor_applied": applied,
                           "scheduled_daily_p90": 10, "observed_raw_p90": 6 if applied else 10,
                           "minimum_daily_p90": 10}
                gate = {"checks": {}, "current": {"monitoring_reserve": {"reserve_details": details}}}
                before = deepcopy(gate)
                text = render_readiness_summary(gate)
                line = next(line for line in text.splitlines() if line.startswith("冷启动期已结束"))
                self.assertIn("仅表示分类完整", line)
                self.assertIn("minimum_floor_applied", line)
                self.assertIn("不互斥", line)
                quota = next(line for line in text.splitlines() if line.startswith("[配额推导]"))
                self.assertIn(f"生效P90=10 原始P90={details['observed_raw_p90']} 下限=10 托底生效={applied}", quota)
                self.assertEqual(gate, before)


if __name__ == "__main__":
    unittest.main()
