import unittest


class ResearchReadinessWorkloadTest(unittest.TestCase):
    def test_readiness_prints_workload_reserve_derivation(self):
        from research_readiness import render_readiness_summary

        daily = [
            {
                "day": f"2026-08-{day:02d}",
                "scheduled_user_monitor": count,
                "unknown": 0,
                "reserve_basis": count,
            }
            for day, count in zip(range(20, 27), (0, 2, 4, 6, 8, 10, 12))
        ]
        hard_gate = {
            "ready": False,
            "checks": {
                "expected_days_remaining": True,
                "worst_case_days_remaining": True,
                "monitoring_reserve": True,
                "backup_restore_verified": False,
                "off_disk_copy_verified": False,
                "off_disk_copy_fresh": False,
                "timestamp_migration": True,
                "lineage_migration": True,
                "old_data_readable": True,
            },
            "current": {
                "monitoring_reserve": {
                    "remaining_after_research": 599,
                    "required_reserve": 450,
                    "reserve_details": {
                        "daily_counts": daily,
                        "raw_scheduled_daily_p90": 12,
                        "scheduled_daily_p90": 12,
                        "minimum_daily_p90": 10,
                        "minimum_floor_applied": False,
                        "days_remaining": 35,
                        "monitoring_reserve": 534,
                        "research_available": 71,
                        "research_batch_calls": 30,
                        "next_batch_can_start": True,
                        "scheduled_anomaly": False,
                        "manual_live_used": 4,
                        "manual_live_buffer": 30,
                    },
                }
            },
            "requirements": {},
        }

        rendered = render_readiness_summary(hard_gate)

        self.assertIn("2026-08-20 scheduled=0 unknown=0 reserve_basis=0", rendered)
        self.assertIn("P90=12", rendered)
        self.assertIn("下限10生效=False", rendered)
        self.assertIn("剩余天数=35", rendered)
        self.assertIn("research_available=71", rendered)
        self.assertIn("下一批可启动=True", rendered)


if __name__ == "__main__":
    unittest.main()
