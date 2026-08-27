import math
import unittest
from datetime import date


def _entry(day, count, workload_class=None):
    row = {
        "day": day,
        "round_id": f"{workload_class or 'legacy'}-{day}",
        "counts": {"juhe": count},
    }
    if workload_class is not None:
        row["workload_class"] = workload_class
    return row


def _policy():
    return {
        "kind": "purchased_packs",
        "packs": [{"id": "pack", "added": 1000, "added_at": "2026-08-01"}],
        "reserve": {
            "kind": "workload_p90",
            "window_complete_days": 7,
            "target_date": "2026-10-01",
            "minimum_daily_p90": 10,
            "safety_multiplier": 1.2,
            "manual_live_buffer": 30,
            "research_batch_calls": 30,
            "scheduled_anomaly_threshold": 12,
            "scheduled_anomaly_consecutive_days": 2,
        },
    }


def _expected_reserve(effective_p90, as_of):
    config = _policy()["reserve"]
    days_remaining = (date.fromisoformat(config["target_date"]) - as_of).days
    return (
        math.ceil(effective_p90 * days_remaining * config["safety_multiplier"])
        + config["manual_live_buffer"]
    )


class WorkloadQuotaPolicyTest(unittest.TestCase):
    def test_recent_complete_days_exclude_today_and_manual_spikes(self):
        import quota_policy

        entries = [
            _entry(f"2026-08-{day:02d}", day - 18, "scheduled_user_monitor")
            for day in range(20, 27)
        ]
        entries.extend(
            [
                _entry("2026-08-26", 50, "manual_live"),
                _entry("2026-08-27", 99, "scheduled_user_monitor"),
            ]
        )
        usage = {"dates": {}, "entries": entries}
        as_of = date(2026, 8, 27)

        details = quota_policy.workload_reserve_details(
            _policy(),
            usage_payload=usage,
            source="juhe",
            as_of=as_of,
        )

        self.assertEqual(
            [row["day"] for row in details["daily_counts"]],
            [f"2026-08-{day:02d}" for day in range(20, 27)],
        )
        self.assertEqual(details["raw_scheduled_daily_p90"], 8)
        self.assertEqual(details["scheduled_daily_p90"], 10)
        self.assertTrue(details["minimum_floor_applied"])
        self.assertEqual(details["manual_live_used"], 50)
        self.assertEqual(
            details["monitoring_reserve"],
            _expected_reserve(details["effective_scheduled_p90"], as_of),
        )

    def test_p90_above_floor_and_unknown_are_conservatively_reserved(self):
        import quota_policy

        entries = []
        for day, scheduled in zip(range(20, 27), range(8, 15)):
            entries.append(
                _entry(f"2026-08-{day:02d}", scheduled, "scheduled_user_monitor")
            )
        entries.append(_entry("2026-08-26", 2))
        as_of = date(2026, 8, 27)

        details = quota_policy.workload_reserve_details(
            _policy(),
            usage_payload={"dates": {}, "entries": entries},
            source="juhe",
            as_of=as_of,
        )

        self.assertEqual(details["daily_counts"][-1]["scheduled_user_monitor"], 14)
        self.assertEqual(details["daily_counts"][-1]["unknown"], 2)
        self.assertEqual(details["daily_counts"][-1]["reserve_basis"], 16)
        self.assertEqual(details["scheduled_daily_p90"], 16)
        self.assertFalse(details["minimum_floor_applied"])
        self.assertEqual(
            details["monitoring_reserve"],
            _expected_reserve(details["effective_scheduled_p90"], as_of),
        )

    def test_reserve_shrinks_as_target_date_approaches(self):
        import quota_policy

        usage = {"dates": {}, "entries": []}
        first_day = date(2026, 8, 27)
        second_day = date(2026, 8, 28)
        first = quota_policy.workload_reserve_details(
            _policy(), usage_payload=usage, source="juhe", as_of=first_day
        )
        second = quota_policy.workload_reserve_details(
            _policy(), usage_payload=usage, source="juhe", as_of=second_day
        )

        self.assertEqual(
            first["monitoring_reserve"],
            _expected_reserve(first["effective_scheduled_p90"], first_day),
        )
        self.assertEqual(
            second["monitoring_reserve"],
            _expected_reserve(second["effective_scheduled_p90"], second_day),
        )
        self.assertGreater(first["monitoring_reserve"], second["monitoring_reserve"])

    def test_metrics_expose_signed_research_available_and_batch_gate(self):
        import quota_policy

        snapshot = {"today": {}, "month": {}, "cumulative": {"juhe": 395}}
        as_of = date(2026, 8, 27)
        result = quota_policy.metrics(
            _policy(),
            snapshot,
            "juhe",
            usage_payload={"dates": {}, "entries": []},
            as_of=as_of,
        )
        expected_reserve = _expected_reserve(
            result["reserve_details"]["effective_scheduled_p90"],
            as_of,
        )

        self.assertEqual(result["remaining"], 605)
        self.assertEqual(result["reserve"], expected_reserve)
        self.assertEqual(
            result["research_available"],
            result["remaining"] - expected_reserve,
        )
        self.assertTrue(result["reserve_details"]["next_batch_can_start"])


if __name__ == "__main__":
    unittest.main()
