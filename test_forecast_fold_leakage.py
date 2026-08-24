import unittest
from datetime import date, timedelta

from forecast import assert_no_walk_forward_leakage, walk_forward_backtest


def _cell(depart_date, observed_day, price):
    depart = date.fromisoformat(depart_date)
    observed = date.fromisoformat(observed_day)
    return {
        "depart_date": depart_date,
        "observed_day": observed_day,
        "days_to_departure": (depart - observed).days,
        "min_price": float(price),
        "degraded": False,
        "min_sources": ["juhe"],
    }


def _trajectory(depart_date, start_day, end_day, base_price, step):
    start = date.fromisoformat(start_day)
    end = date.fromisoformat(end_day)
    rows = []
    current = start
    offset = 0
    while current <= end:
        rows.append(
            _cell(
                depart_date,
                current.isoformat(),
                base_price + offset * step,
            )
        )
        current += timedelta(days=1)
        offset += 1
    return rows


def _base_cells():
    return [
        *_trajectory(
            "2026-09-24",
            "2026-08-25",
            "2026-09-03",
            80,
            2,
        ),
        *_trajectory(
            "2026-10-01",
            "2026-09-01",
            "2026-09-12",
            100,
            3,
        ),
    ]


def _historical_fold(report, horizon):
    return next(
        case
        for case in report["cases"][str(horizon)]
        if case["depart_date"] == "2026-10-01"
        and case["target_day"] == "2026-09-10"
    )


def _single_fold_mape(case):
    return abs(float(case["model"]) - float(case["actual"])) / float(
        case["actual"]
    ) * 100


class ForecastFoldLeakageTest(unittest.TestCase):
    def test_later_observation_does_not_change_historical_fold_for_all_horizons(self):
        base_cells = _base_cells()
        global_as_of = max(item["observed_day"] for item in base_cells)

        for horizon in (1, 3, 7):
            with self.subTest(horizon=horizon):
                baseline = walk_forward_backtest(
                    base_cells,
                    horizons=(horizon,),
                    min_level_obs=1,
                    min_shape_n=1,
                )
                baseline_case = _historical_fold(baseline, horizon)
                cutoff = date.fromisoformat(baseline_case["cutoff_day"])
                inserted_day = cutoff + timedelta(days=2 if horizon == 1 else 1)
                self.assertLess(inserted_day.isoformat(), global_as_of)

                late_depart = inserted_day + timedelta(days=21)
                changed = walk_forward_backtest(
                    [
                        *base_cells,
                        _cell(
                            late_depart.isoformat(),
                            inserted_day.isoformat(),
                            99999,
                        ),
                    ],
                    horizons=(horizon,),
                    min_level_obs=1,
                    min_shape_n=1,
                )
                changed_case = _historical_fold(changed, horizon)

                self.assertEqual(
                    baseline_case["fit_training_signature"],
                    changed_case["fit_training_signature"],
                )
                self.assertEqual(baseline_case["fit_n"], changed_case["fit_n"])
                self.assertEqual(
                    baseline_case["fit_observed_days"],
                    changed_case["fit_observed_days"],
                )
                self.assertEqual(
                    _single_fold_mape(baseline_case),
                    _single_fold_mape(changed_case),
                )

    def test_deliberately_leaking_fold_is_rejected(self):
        report = walk_forward_backtest(
            _base_cells(),
            horizons=(3,),
            min_level_obs=1,
            min_shape_n=1,
        )
        case = dict(_historical_fold(report, 3))
        case["fit_observed_days"] = [
            *case["fit_observed_days"],
            "2026-09-09",
        ]

        with self.assertRaises(AssertionError):
            assert_no_walk_forward_leakage(case)


if __name__ == "__main__":
    unittest.main()
