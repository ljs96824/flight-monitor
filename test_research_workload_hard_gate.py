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
        "manual_live_in_epoch": 0,
        "manual_live_buffer": 30,
        "canary_used": 0,
        "canary_in_epoch": 0,
        "canary_buffer": 12,
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
                "different_device_verified": True,
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
        result = _evaluate(_quota(manual_live_in_epoch=31))
        self.assertFalse(result["checks"]["monitoring_reserve"])

    def test_epoch_counter_is_enforced_without_legacy_alias(self):
        quota = _quota(manual_live_in_epoch=31)
        quota.pop("manual_live_used")

        result = _evaluate(quota)

        self.assertFalse(result["checks"]["monitoring_reserve"])

    def test_pre_epoch_manual_lifetime_does_not_block_monitoring_gate(self):
        result = _evaluate(
            _quota(
                manual_live_lifetime=55,
                manual_live_used=55,
                manual_live_in_epoch=5,
            )
        )
        self.assertTrue(result["checks"]["monitoring_reserve"])

    def test_canary_buffer_uses_only_epoch_usage(self):
        passed = _evaluate(
            _quota(canary_lifetime=25, canary_used=25, canary_in_epoch=5)
        )
        blocked = _evaluate(_quota(canary_in_epoch=13))

        self.assertTrue(passed["checks"]["monitoring_reserve"])
        self.assertFalse(blocked["checks"]["monitoring_reserve"])


if __name__ == "__main__":
    unittest.main()
