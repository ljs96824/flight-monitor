import unittest
from unittest.mock import patch


def _cell(depart_date, observed_day, t_value, *, degraded=False, lineage=True):
    return {
        "depart_date": depart_date,
        "observed_day": observed_day,
        "days_to_departure": t_value,
        "min_price": 100,
        "degraded": degraded,
        "min_sources": ["juhe"],
        "lineage_complete": lineage,
        "round_ids": [f"round-{depart_date}-{observed_day}"] if lineage else [],
    }


def _shape():
    return {
        t_value: {
            "n": 5,
            "sufficient": True,
            "median": 1,
            "p10": 1,
            "p25": 1,
            "p75": 1,
            "p90": 1,
        }
        for t_value in range(3, 11)
    }


def _passed_backtest():
    horizon = {
        "n": 8,
        "model": {"mape": 5},
        "naive": {"mape": 6},
        "tcurve": {"mape": 7},
        "skill_gate": {"passed": True, "case_n": 8},
    }
    return {
        "horizons": {str(value): dict(horizon) for value in (1, 3, 7)},
        "cases": {str(value): [] for value in (1, 3, 7)},
    }


def _patterns():
    return {
        "combo_occurrence": [],
        "supply_mix": {
            "direct": 0,
            "transfer": 0,
            "n": 0,
            "basis": "基于组合结构",
        },
        "departure_period": {
            "status": "字段不可得",
            "reason": "面板未存起飞时刻(obs_store v2),待schema扩展后自动点亮",
        },
    }


class ForecastAsOfAndEvidenceTest(unittest.TestCase):
    def test_report_backtest_never_receives_cells_after_as_of(self):
        from scripts.forecast_report import generate_report

        cells = [
            _cell("2026-10-01", "2026-09-21", 10),
            _cell("2026-10-01", "2026-09-30", 1),
        ]
        decision = {
            "status": "eligible",
            "bottleneck": None,
            "reason_codes": [],
            "human_text": "预测资格已满足",
            "overall_reliability": {
                "value": 1,
                "passed": True,
                "components": {},
                "bottlenecks": [],
                "bottleneck_details": [],
            },
        }
        with patch(
            "scripts.forecast_report.load_tcurve_daily_cells",
            return_value=cells,
        ), patch(
            "scripts.forecast_report.load_route_observations", return_value=[]
        ), patch(
            "scripts.forecast_report.build_regime_map",
            return_value={"2026-10-01": "normal"},
        ), patch(
            "scripts.forecast_report.build_shapes_by_regime",
            return_value={"normal": _shape()},
        ), patch(
            "scripts.forecast_report.walk_forward_backtest"
        ) as backtest_mock, patch(
            "scripts.forecast_report.estimate_level",
            return_value={"reliable": True, "n": 5, "value": 100},
        ), patch(
            "scripts.forecast_report.evaluate_forecast_eligibility",
            return_value=decision,
        ), patch(
            "scripts.forecast_report.predict_price",
            return_value={
                "status": "ok",
                "t": 10,
                "median": 100,
                "p10": 80,
                "p25": 90,
                "p75": 110,
                "p90": 120,
            },
        ), patch(
            "scripts.forecast_report.build_route_patterns",
            return_value=_patterns(),
        ) as patterns_mock:
            backtest_mock.return_value = _passed_backtest()
            generate_report(
                db_path="unused",
                route="上海-大阪",
                as_of_day="2026-09-21",
            )

        received = backtest_mock.call_args.args[0]
        self.assertEqual([item["observed_day"] for item in received], ["2026-09-21"])
        self.assertEqual(patterns_mock.call_args.kwargs["as_of_day"], "2026-09-21")

    def test_route_patterns_exclude_observations_after_as_of(self):
        from patterns import build_route_patterns

        rows = [
            {
                "observed_at": "2026-09-21T09:00:00+08:00",
                "flight_combo": "MU225",
                "depart_date": "2026-10-01",
                "price_cny": 100,
                "stops": 0,
            },
            {
                "observed_at": "2026-09-30T09:00:00+08:00",
                "flight_combo": "JL891",
                "depart_date": "2026-10-01",
                "price_cny": 90,
                "stops": 0,
            },
        ]
        with patch("patterns.load_route_observations", return_value=rows):
            result = build_route_patterns(
                "unused",
                route="上海-大阪",
                as_of_day="2026-09-21",
            )

        self.assertEqual(result["observed_day_n"], 1)
        self.assertEqual(
            [item["combo"] for item in result["combo_occurrence"]],
            ["MU225"],
        )

    def test_notification_uses_real_lineage_evidence(self):
        from forecast import build_notification_forecast

        cells = [
            _cell(
                f"2026-10-{1 + index:02d}",
                "2026-09-21",
                10 + index,
                lineage=index != 4,
            )
            for index in range(5)
        ]
        with patch(
            "forecast.load_tcurve_daily_cells", return_value=cells
        ), patch(
            "forecast.build_regime_map",
            return_value={item["depart_date"]: "normal" for item in cells},
        ), patch(
            "forecast.build_shapes_by_regime",
            return_value={"normal": _shape()},
        ), patch(
            "forecast.estimate_level",
            return_value={"reliable": True, "n": 5, "value": 100},
        ), patch(
            "forecast.walk_forward_backtest", return_value=_passed_backtest()
        ), patch(
            "forecast.predict_price", return_value={"status": "ok", "median": 100}
        ):
            result = build_notification_forecast(
                {
                    "origin": "PVG",
                    "destination": "KIX",
                    "depart_date": "2026-10-01",
                },
                db_path="unused",
                as_of_day="2026-09-21",
            )

        self.assertEqual(result["eligibility"]["status"], "lineage_incomplete")
        self.assertNotIn("source_degraded", result["eligibility"]["reason_codes"])

    def test_notification_uses_real_source_coverage_evidence(self):
        from forecast import build_notification_forecast

        cells = [
            _cell(f"2026-10-{1 + index:02d}", "2026-09-21", 10 + index)
            for index in range(5)
        ]
        cells.append(
            _cell(
                "2026-10-01",
                "2026-09-21",
                10,
                degraded=True,
            )
        )
        with patch(
            "forecast.load_tcurve_daily_cells", return_value=cells
        ), patch(
            "forecast.build_regime_map",
            return_value={item["depart_date"]: "normal" for item in cells},
        ), patch(
            "forecast.build_shapes_by_regime",
            return_value={"normal": _shape()},
        ), patch(
            "forecast.estimate_level",
            return_value={"reliable": True, "n": 5, "value": 100},
        ), patch(
            "forecast.walk_forward_backtest", return_value=_passed_backtest()
        ), patch(
            "forecast.predict_price", return_value={"status": "ok", "median": 100}
        ):
            result = build_notification_forecast(
                {
                    "origin": "PVG",
                    "destination": "KIX",
                    "depart_date": "2026-10-01",
                },
                db_path="unused",
                as_of_day="2026-09-21",
            )

        self.assertEqual(result["eligibility"]["status"], "source_degraded")


if __name__ == "__main__":
    unittest.main()
