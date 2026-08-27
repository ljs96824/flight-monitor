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
                "different_device_verified": False,
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
                        "reserve_epoch_started_at": "2026-08-27T15:39:15+08:00",
                        "manual_live_lifetime": 54,
                        "manual_live_in_epoch": 4,
                        "manual_live_buffer_remaining": 26,
                        "manual_live_buffer": 30,
                        "canary_lifetime": 17,
                        "canary_in_epoch": 2,
                        "canary_buffer_remaining": 10,
                        "canary_buffer": 12,
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
        self.assertIn("储备纪元=2026-08-27T15:39:15+08:00", rendered)
        self.assertIn("manual_live=4/30 剩余=26 lifetime=54", rendered)
        self.assertIn("canary=2/12 剩余=10 lifetime=17", rendered)


if __name__ == "__main__":
    unittest.main()
