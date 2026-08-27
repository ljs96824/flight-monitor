import unittest


def _quota(**overrides):
    payload = {
        "quota_remaining": 605,
        "monitoring_reserve": 450,
        "research_available": 155,
        "research_batch_calls": 30,
        "scheduled_anomaly": False,
        "manual_live_used": 0,
        "manual_live_buffer": 30,
    }
    payload.update(overrides)
    return payload


class ResearchWorkloadGuardTest(unittest.TestCase):
    def test_remaining_at_reserve_stops_only_research(self):
        from research_cohort import apply_research_quota_guard

        state = {}
        result = apply_research_quota_guard(
            state,
            _quota(quota_remaining=450, research_available=0),
            now="2026-08-27T12:00:00+08:00",
        )

        self.assertTrue(result["triggered"])
        self.assertIn("monitoring_reserve_reached", result["reason_codes"])
        self.assertFalse(state["research_cohort_v2"]["runtime_enabled"])
        self.assertTrue(state["research_cohort_v2"]["user_monitoring_enabled"])

    def test_less_than_one_batch_stops_research(self):
        from research_cohort import apply_research_quota_guard

        result = apply_research_quota_guard({}, _quota(research_available=29))

        self.assertTrue(result["triggered"])
        self.assertEqual(result["reason_codes"], ["research_batch_budget_insufficient"])

    def test_two_consecutive_high_scheduled_days_stop_research(self):
        from research_cohort import apply_research_quota_guard

        result = apply_research_quota_guard({}, _quota(scheduled_anomaly=True))

        self.assertTrue(result["triggered"])
        self.assertEqual(result["reason_codes"], ["scheduled_usage_anomaly"])

    def test_manual_live_over_buffer_stops_research(self):
        from research_cohort import apply_research_quota_guard

        result = apply_research_quota_guard({}, _quota(manual_live_used=31))

        self.assertTrue(result["triggered"])
        self.assertEqual(result["reason_codes"], ["manual_live_buffer_exceeded"])

    def test_normal_quota_does_not_stop_research(self):
        from research_cohort import apply_research_quota_guard

        result = apply_research_quota_guard({}, _quota())

        self.assertEqual(
            result,
            {"triggered": False, "notified": False, "reason_codes": []},
        )


if __name__ == "__main__":
    unittest.main()
