import unittest

from forecast import (
    assert_no_walk_forward_leakage,
    build_shape,
    estimate_level,
    evaluate_skill_gate,
    predict_price,
    walk_forward_backtest,
)


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
        shape = build_shape(cells)
        self.assertAlmostEqual(shape[9]["median"], 1.0)
        self.assertAlmostEqual(shape[10]["median"], 5 / 6)
        self.assertAlmostEqual(shape[8]["median"], 7 / 6)

    def test_level_prediction_and_no_interpolation(self):
        cells = [cell("2026-10-01", f"2026-09-{day}", t, price) for day, t, price in ((21, 10, 100), (22, 9, 120), (23, 8, 140), (24, 7, 160))]
        shape = build_shape(cells)
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


if __name__ == "__main__":
    unittest.main()
