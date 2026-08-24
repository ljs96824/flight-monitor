import unittest

from forecast import (
    MIN_SHAPE_N,
    assess_overall_reliability,
    assert_no_walk_forward_leakage,
    build_shape,
    build_shapes_by_regime,
    classify_regime,
    estimate_level,
    evaluate_skill_gate,
    predict_price,
    walk_forward_backtest,
)
from tcurve import MIN_SAMPLE_FOR_TCURVE


def cell(depart, observed, t, price):
    return {
        "depart_date": depart,
        "observed_day": observed,
        "days_to_departure": t,
        "min_price": price,
        "degraded": False,
        "min_sources": ["juhe"],
    }


class ForecastTest(unittest.TestCase):
    def test_shape_normalizes_each_departure_before_pooling(self):
        cells = [
            cell("2026-10-01", "2026-09-21", 10, 100),
            cell("2026-10-01", "2026-09-22", 9, 120),
            cell("2026-10-01", "2026-09-23", 8, 140),
            cell("2026-10-08", "2026-09-28", 10, 200),
            cell("2026-10-08", "2026-09-29", 9, 240),
            cell("2026-10-08", "2026-09-30", 8, 280),
        ]
        shape = build_shape(cells, min_shape_n=2)
        self.assertAlmostEqual(shape[9]["median"], 1.0)
        self.assertAlmostEqual(shape[10]["median"], 5 / 6)
        self.assertAlmostEqual(shape[8]["median"], 7 / 6)

    def test_level_prediction_and_no_interpolation(self):
        cells = [cell("2026-10-01", f"2026-09-{day}", t, price) for day, t, price in ((21, 10, 100), (22, 9, 120), (23, 8, 140), (24, 7, 160))]
        shape = build_shape(cells, min_shape_n=1)
        level = estimate_level(cells, shape, depart_date="2026-10-01", min_obs=4)
        self.assertTrue(level["reliable"])
        self.assertIsNotNone(predict_price(level, shape, target_t=8)["median"])
        self.assertEqual(predict_price(level, shape, target_t=6)["status"], "无可用shape")

    def test_skill_gate_uses_case_floor_and_improvement(self):
        self.assertFalse(evaluate_skill_gate(model_mape=9, naive_mape=10, case_n=4)["passed"])
        self.assertTrue(evaluate_skill_gate(model_mape=9, naive_mape=10, case_n=5)["passed"])
        self.assertFalse(evaluate_skill_gate(model_mape=9.1, naive_mape=10, case_n=5)["passed"])

    def test_leakage_guard_rejects_future_fit_day(self):
        with self.assertRaises(AssertionError):
            assert_no_walk_forward_leakage({"cutoff_day": "2026-09-20", "target_day": "2026-09-21", "fit_observed_days": ["2026-09-21"]})

    def test_walk_forward_returns_three_horizons(self):
        cells = []
        for offset in range(12):
            day = 10 + offset
            cells.append(cell("2026-10-01", f"2026-09-{day:02d}", 21 - offset, 100 + offset))
        report = walk_forward_backtest(cells, horizons=(1, 3, 7), min_level_obs=2)
        self.assertEqual(set(report["horizons"]), {"1", "3", "7"})
        for cases in report["cases"].values():
            for case in cases:
                assert_no_walk_forward_leakage(case)

    def test_shape_gate_uses_tcurve_threshold_and_hides_low_n_statistics(self):
        self.assertEqual(MIN_SHAPE_N, MIN_SAMPLE_FOR_TCURVE)
        cells = [cell(f"2026-10-{day:02d}", "2026-09-01", 30, 100 + day) for day in range(1, 6)]

        low = build_shape(cells[:4])[30]
        enough = build_shape(cells)[30]

        self.assertFalse(low["sufficient"])
        self.assertEqual(low["status"], "样本不足(n=4)")
        self.assertIsNone(low["median"])
        self.assertIsNotNone(low["raw"]["median"])
        self.assertTrue(enough["sufficient"])
        self.assertEqual(enough["n"], 5)
        self.assertIsNotNone(enough["median"])

    def test_regime_classification_and_shape_pool_never_borrow_across_regimes(self):
        self.assertEqual(classify_regime("2026-09-19", []), "weekend")
        self.assertEqual(classify_regime("2026-09-21", ["日本·敬老日(当天)"]), "holiday")
        self.assertEqual(classify_regime("2026-09-21", ["日本·秋分日(节前1日)"]), "holiday_eve")
        self.assertEqual(classify_regime("2026-09-21", ["日本·秋分日(节后1日)"]), "holiday_return")
        self.assertEqual(classify_regime("2026-09-21", []), "normal")

        cells = [
            cell("2026-10-01", "2026-09-01", 30, 100),
            cell("2026-10-02", "2026-09-02", 30, 200),
        ]
        regimes = {"2026-10-01": "holiday", "2026-10-02": "normal"}
        shapes = build_shapes_by_regime(cells, regimes, min_shape_n=1)

        self.assertEqual(shapes["holiday"][30]["n"], 1)
        self.assertEqual(shapes["normal"][30]["n"], 1)
        self.assertEqual(shapes["holiday"][30]["raw"]["median"], 1.0)
        self.assertEqual(shapes["normal"][30]["raw"]["median"], 1.0)

    def test_overall_reliability_is_component_min_and_names_bottleneck(self):
        result = assess_overall_reliability(
            level={"reliable": True, "n": 4},
            shape_points=[{"n": 1, "sufficient": False}],
            backtest_gate={"passed": True},
            source_coverage=True,
            regime_sample_n=5,
        )

        self.assertEqual(result["value"], 0)
        self.assertFalse(result["passed"])
        self.assertEqual(result["bottlenecks"], ["shape"])
        self.assertEqual(result["components"]["shape_reliability"]["detail"], "shape(n=1)")

    def test_prediction_rejects_insufficient_shape_point(self):
        shape = build_shape([cell("2026-10-01", "2026-09-01", 30, 100)])
        level = {"reliable": True, "value": 100}

        self.assertEqual(predict_price(level, shape, target_t=30)["status"], "shape样本不足(n=1)")

if __name__ == "__main__":
    unittest.main()
