import math
import unittest
from datetime import date, timedelta


FLOOR = 10
AS_OF = date(2026, 8, 27)


def _entry(day, count, workload_class_marker=...):
    entry = {
        "day": day,
        "round_id": f"round-{day}-{count}",
        "counts": {"juhe": count},
    }
    if workload_class_marker is not ...:
        entry["workload_class"] = workload_class_marker
    return entry


def _config():
    return {
        "window_complete_days": 7,
        "target_date": "2026-10-01",
        "minimum_daily_p90": FLOOR,
        "safety_multiplier": 1.2,
        "manual_live_buffer": 30,
        "research_batch_calls": 30,
        "scheduled_anomaly_threshold": 12,
        "scheduled_anomaly_consecutive_days": 2,
    }


def _calculate(entries, dates=None, *, as_of=AS_OF):
    from workload_reserve import calculate_workload_reserve

    return calculate_workload_reserve(
        _config(),
        usage_payload={"entries": entries, "dates": dates or {}},
        source="juhe",
        as_of=as_of,
    )


class WorkloadReserveColdStartTest(unittest.TestCase):
    def test_four_day_types_have_the_contract_sample_values(self):
        day_keys = [
            (AS_OF - timedelta(days=offset)).isoformat()
            for offset in range(7, 0, -1)
        ]
        fully, pure_unknown, mixed, missing = day_keys[:4]
        entries = [
            _entry(fully, 6, "scheduled_user_monitor"),
            _entry(pure_unknown, 16),
            _entry(mixed, 4, "scheduled_user_monitor"),
            _entry(mixed, 3),
        ]
        dates = {
            fully: {"juhe": 6},
            pure_unknown: {"juhe": 16},
            mixed: {"juhe": 7},
        }

        details = _calculate(entries, dates)
        rows = {row["day"]: row for row in details["daily_counts"]}

        self.assertEqual(rows[fully]["day_type"], "fully_classified")
        self.assertEqual(rows[fully]["sample_value"], 6)
        self.assertEqual(rows[pure_unknown]["day_type"], "pure_unknown")
        self.assertEqual(rows[pure_unknown]["sample_value"], FLOOR)
        self.assertEqual(rows[mixed]["day_type"], "mixed")
        self.assertEqual(rows[mixed]["sample_value"], FLOOR)
        self.assertEqual(rows[missing]["day_type"], "telemetry_missing")
        self.assertTrue(rows[missing]["telemetry_missing"])
        self.assertEqual(rows[missing]["sample_value"], FLOOR)

    def test_all_unknown_uses_floor_without_rewriting_history(self):
        days = [
            (AS_OF - timedelta(days=offset)).isoformat()
            for offset in range(7, 0, -1)
        ]
        entries = [_entry(day, 16) for day in days]
        dates = {day: {"juhe": 16} for day in days}

        details = _calculate(entries, dates)

        self.assertEqual(details["observed_raw_p90"], FLOOR)
        self.assertEqual(details["effective_scheduled_p90"], FLOOR)
        self.assertEqual(details["fully_classified_days"], [])
        self.assertEqual(details["pure_unknown_days"], days)
        self.assertTrue(details["cold_start_active"])
        self.assertTrue(details["cold_start_estimated"])
        self.assertTrue(all("workload_class" not in entry for entry in entries))

        expected_reserve = (
            math.ceil(
                FLOOR
                * (date(2026, 10, 1) - AS_OF).days
                * _config()["safety_multiplier"]
            )
            + _config()["manual_live_buffer"]
        )
        self.assertEqual(details["monitoring_reserve"], expected_reserve)

    def test_mixed_day_never_advances_exit_progress(self):
        days = [
            (AS_OF - timedelta(days=offset)).isoformat()
            for offset in range(7, 0, -1)
        ]
        entries = []
        dates = {}
        for day in days:
            entries.append(_entry(day, 5, "scheduled_user_monitor"))
            dates[day] = {"juhe": 5}
        entries.append(_entry(days[-1], 2))
        dates[days[-1]]["juhe"] = 7

        details = _calculate(entries, dates)

        self.assertEqual(len(details["fully_classified_days"]), 6)
        self.assertEqual(details["mixed_days"], [days[-1]])
        self.assertTrue(details["cold_start_active"])

    def test_cold_start_exits_only_after_seven_fully_classified_days(self):
        days = [
            (AS_OF - timedelta(days=offset)).isoformat()
            for offset in range(7, 0, -1)
        ]
        entries = [_entry(day, count, "scheduled_user_monitor") for day, count in zip(days, range(4, 11))]
        dates = {day: {"juhe": count} for day, count in zip(days, range(4, 11))}

        details = _calculate(entries, dates)

        self.assertEqual(details["fully_classified_days"], days)
        self.assertFalse(details["cold_start_active"])
        self.assertFalse(details["cold_start_estimated"])
        self.assertEqual(details["cold_start_expected_exit_at"], AS_OF.isoformat())

    def test_missing_workload_class_blocks_false_exit(self):
        days = [
            (AS_OF - timedelta(days=offset)).isoformat()
            for offset in range(7, 0, -1)
        ]
        entries = [_entry(day, 5, "scheduled_user_monitor") for day in days]
        dates = {day: {"juhe": 5} for day in days}
        entries[2].pop("workload_class")

        details = _calculate(entries, dates)

        self.assertIn(days[2], details["pure_unknown_days"])
        self.assertTrue(details["cold_start_active"])

    def test_expected_exit_date_uses_trailing_fully_classified_streak(self):
        days = [
            (AS_OF - timedelta(days=offset)).isoformat()
            for offset in range(7, 0, -1)
        ]
        trailing_days = days[-3:]
        entries = [_entry(day, 5, "scheduled_user_monitor") for day in trailing_days]
        dates = {day: {"juhe": 5} for day in trailing_days}

        details = _calculate(entries, dates)

        self.assertEqual(details["cold_start_expected_exit_at"], "2026-08-31")
        self.assertEqual(
            details["cold_start_exit_condition"],
            "最近7个完整上海日全部为完全分类日",
        )


if __name__ == "__main__":
    unittest.main()
