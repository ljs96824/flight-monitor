import unittest


def _quota():
    return {
        "complete": True,
        "expected_days_remaining": 30,
        "worst_case_days_remaining": 20,
        "remaining_after_research": 500,
        "monitoring_reserve": 400,
    }


def _migrations():
    return {
        "timestamp_ready": True,
        "lineage_ready": True,
        "old_data_readable": True,
    }


class ResearchHardGateBackupTest(unittest.TestCase):
    def test_stale_copy_blocks_gate_even_when_every_other_gate_passes(self):
        from research_cohort import evaluate_research_hard_gates

        result = evaluate_research_hard_gates(
            backup_evidence={
                "checks": {
                    "backup_restore_verified": True,
                    "off_disk_copy_verified": True,
                    "off_disk_copy_fresh": False,
                },
                "current": {"off_disk_copy_age_days": 31.0},
                "requirements": {"max_backup_age_days": 30},
                "reasons": {"off_disk_copy_fresh": "异盘副本证据已过期"},
            },
            quota_simulation=_quota(),
            migration_status=_migrations(),
        )

        self.assertFalse(result["ready"])
        self.assertEqual(result["missing"], ["off_disk_copy_fresh"])
        self.assertEqual(len(result["checks"]), 9)

    def test_all_nine_gates_pass_with_fresh_verified_evidence(self):
        from research_cohort import evaluate_research_hard_gates

        result = evaluate_research_hard_gates(
            backup_evidence={
                "checks": {
                    "backup_restore_verified": True,
                    "off_disk_copy_verified": True,
                    "off_disk_copy_fresh": True,
                },
                "current": {},
                "requirements": {"max_backup_age_days": 30},
                "reasons": {},
            },
            quota_simulation=_quota(),
            migration_status=_migrations(),
        )

        self.assertTrue(result["ready"])
        self.assertEqual(result["missing"], [])
        self.assertEqual(len(result["checks"]), 9)


if __name__ == "__main__":
    unittest.main()
