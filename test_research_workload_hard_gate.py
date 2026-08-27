import unittest


def _quota(**overrides):
    payload = {
        "complete": True,
        "expected_days_remaining": 60,
        "worst_case_days_remaining": 40,
        "remaining_after_research": 599,
        "monitoring_reserve": 450,
        "research_available": 155,
        "research_batch_calls": 30,
        "scheduled_anomaly": False,
        "manual_live_used": 0,
        "manual_live_buffer": 30,
        "reserve_details": {},
    }
    payload.update(overrides)
    return payload


def _evaluate(quota):
    from research_cohort import evaluate_research_hard_gates

    return evaluate_research_hard_gates(
        backup_evidence={
            "checks": {
                "backup_restore_verified": True,
                "off_disk_copy_verified": True,
                "off_disk_copy_fresh": True,
            },
            "current": {},
        },
        quota_simulation=quota,
        migration_status={
            "timestamp_ready": True,
            "lineage_ready": True,
            "old_data_readable": True,
        },
    )


class ResearchWorkloadHardGateTest(unittest.TestCase):
    def test_normal_workload_quota_passes_monitoring_gate(self):
        self.assertTrue(_evaluate(_quota())["checks"]["monitoring_reserve"])

    def test_next_batch_budget_is_part_of_monitoring_gate(self):
        result = _evaluate(_quota(research_available=29))
        self.assertFalse(result["checks"]["monitoring_reserve"])

    def test_scheduled_anomaly_is_part_of_monitoring_gate(self):
        result = _evaluate(_quota(scheduled_anomaly=True))
        self.assertFalse(result["checks"]["monitoring_reserve"])

    def test_manual_buffer_is_part_of_monitoring_gate(self):
        result = _evaluate(_quota(manual_live_used=31))
        self.assertFalse(result["checks"]["monitoring_reserve"])


if __name__ == "__main__":
    unittest.main()
