import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import ANY, patch


class ResearchReadinessTest(unittest.TestCase):
    def test_report_lists_quota_health_plus_three_existing_quota_gates(self):
        from research_readiness import build_readiness_summary

        hard_gate = {
            "ready": False,
            "checks": {
                "quota_ledger_healthy": True,
                "expected_days_remaining": True,
                "worst_case_days_remaining": False,
                "monitoring_reserve": True,
                "backup_restore_verified": True,
                "off_disk_copy_verified": True,
                "off_disk_copy_fresh": False,
                "timestamp_migration": True,
                "lineage_migration": True,
                "old_data_readable": True,
            },
            "current": {
                "expected_days_remaining": 31,
                "worst_case_days_remaining": 19,
                "remaining_after_research": 510,
                "monitoring_reserve": 500,
                "verified_restore_at": "2026-08-26T08:00:00Z",
                "off_disk_copy_verified": True,
                "off_disk_copy_age_days": 31.0,
                "timestamp_migration": True,
                "lineage_migration": True,
                "old_data_readable": True,
            },
            "requirements": {
                "minimum_expected_days": 30,
                "minimum_worst_case_days": 20,
                "max_backup_age_days": 30,
            },
        }
        summary = build_readiness_summary(hard_gate)

        self.assertEqual(set(summary["groups"]), {"quota", "backup", "migration"})
        self.assertEqual(len(summary["groups"]["quota"]), 4)
        self.assertEqual(len(summary["groups"]["backup"]), 3)
        self.assertEqual(len(summary["groups"]["migration"]), 3)
        self.assertFalse(summary["ready"])

    def test_cli_prints_all_gate_names_without_writing(self):
        from scripts.research_readiness import main

        report = {
            "hard_gate": {
                "ready": False,
                "checks": {
                    "quota_ledger_healthy": True,
                    "expected_days_remaining": True,
                    "worst_case_days_remaining": False,
                    "monitoring_reserve": True,
                    "backup_restore_verified": False,
                    "off_disk_copy_verified": False,
                    "off_disk_copy_fresh": False,
                    "timestamp_migration": True,
                    "lineage_migration": True,
                    "old_data_readable": True,
                },
                "current": {},
                "requirements": {},
            }
        }
        output = io.StringIO()
        with (
            patch("scripts.research_readiness.build_report", return_value=report),
            redirect_stdout(output),
        ):
            exit_code = main(["--today", "2026-08-27"])

        rendered = output.getvalue()
        self.assertEqual(exit_code, 0)
        for name in report["hard_gate"]["checks"]:
            self.assertIn(name, rendered)
        self.assertIn("还差", rendered)

    def test_quota_report_passes_backup_status_path_to_evidence_loader(self):
        from scripts.research_quota_simulation import build_report

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status_path = root / "backup_status.json"
            with (
                patch(
                    "scripts.research_quota_simulation._build_report_inputs",
                    return_value=(
                        {
                            "complete": True,
                            "expected_days_remaining": 30,
                            "worst_case_days_remaining": 20,
                            "remaining_after_research": 500,
                            "monitoring_reserve": 400,
                        },
                        {
                            "timestamp_ready": True,
                            "lineage_ready": True,
                            "old_data_readable": True,
                        },
                        {"research_cohort_v2_gates": {}},
                        [],
                    ),
                ),
                patch(
                    "scripts.research_quota_simulation.load_backup_evidence",
                    return_value={
                        "checks": {
                            "backup_restore_verified": True,
                            "off_disk_copy_verified": True,
                            "off_disk_copy_fresh": True,
                        },
                        "current": {},
                        "reasons": {},
                    },
                ) as loader,
            ):
                report = build_report(
                    today=date(2026, 8, 27),
                    config_path=root / "config.yaml",
                    state_path=root / "basket_state.json",
                    subscriptions_path=root / "subscriptions.json",
                    observations_path=root / "observations.sqlite3",
                    prices_path=root / "prices.db",
                    usage_path=root / "api_usage.json",
                    backup_status_path=status_path,
                )

        loader.assert_called_once_with(status_path, now=ANY, max_age_days=30)
        self.assertTrue(report["hard_gate"]["checks"]["off_disk_copy_verified"])


if __name__ == "__main__":
    unittest.main()
