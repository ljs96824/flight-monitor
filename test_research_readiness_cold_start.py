import unittest


MACHINE_FIELDS = (
    "reserve_window_days",
    "fully_classified_days",
    "pure_unknown_days",
    "mixed_days",
    "telemetry_missing_days",
    "observed_raw_p90",
    "effective_scheduled_p90",
    "scheduled_daily_floor",
    "cold_start_active",
    "cold_start_reason",
    "cold_start_estimated",
    "cold_start_exit_condition",
    "cold_start_expected_exit_at",
    "monitoring_reserve",
    "research_available",
)


def _hard_gate(details):
    return {
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
                "remaining_after_research": 600,
                "required_reserve": details["monitoring_reserve"],
                "reserve_details": details,
            }
        },
        "requirements": {},
    }


class ResearchReadinessColdStartTest(unittest.TestCase):
    def test_readiness_prints_all_machine_fields_and_cold_start_disclosure(self):
        from research_readiness import render_readiness_summary

        unknown_days = [f"2026-08-{day:02d}" for day in range(20, 27)]
        details = {
            "daily_counts": [],
            "reserve_window_days": 7,
            "fully_classified_days": [],
            "pure_unknown_days": unknown_days,
            "mixed_days": [],
            "telemetry_missing_days": [],
            "observed_raw_p90": 10,
            "effective_scheduled_p90": 10,
            "scheduled_daily_floor": 10,
            "cold_start_active": True,
            "cold_start_reason": "window_contains_unclassified_days",
            "cold_start_estimated": True,
            "cold_start_exit_condition": "最近7个完整上海日全部为完全分类日",
            "cold_start_expected_exit_at": "2026-09-03",
            "monitoring_reserve": 450,
            "research_available": 155,
            "next_batch_can_start": True,
            "manual_live_used": 0,
            "manual_live_buffer": 30,
            "scheduled_anomaly": False,
        }

        rendered = render_readiness_summary(_hard_gate(details))

        for field in MACHINE_FIELDS:
            self.assertIn(f"{field}=", rendered)
        self.assertIn(
            "冷启动期:最近7个完整日尚未形成完整工作负载分类,其中7日为历史unknown;"
            "储备暂按每日10次下限估算,非实测结论。"
            "连续获得7个完整分类日后自动退出该规则。",
            rendered,
        )

    def test_readiness_reports_evidence_based_exit(self):
        from research_readiness import render_readiness_summary

        days = [f"2026-08-{day:02d}" for day in range(20, 27)]
        details = {
            "daily_counts": [],
            "reserve_window_days": 7,
            "fully_classified_days": days,
            "pure_unknown_days": [],
            "mixed_days": [],
            "telemetry_missing_days": [],
            "observed_raw_p90": 11,
            "effective_scheduled_p90": 11,
            "scheduled_daily_floor": 10,
            "cold_start_active": False,
            "cold_start_reason": "seven_fully_classified_days",
            "cold_start_estimated": False,
            "cold_start_exit_condition": "最近7个完整上海日全部为完全分类日",
            "cold_start_expected_exit_at": "2026-08-27",
            "monitoring_reserve": 492,
            "research_available": 113,
            "next_batch_can_start": True,
            "manual_live_used": 0,
            "manual_live_buffer": 30,
            "scheduled_anomaly": False,
        }

        rendered = render_readiness_summary(_hard_gate(details))

        self.assertIn("冷启动期已结束", rendered)
        self.assertNotIn("非实测结论", rendered)


if __name__ == "__main__":
    unittest.main()
