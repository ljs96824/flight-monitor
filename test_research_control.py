import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch


def _report(*, ready: bool) -> dict:
    checks = {
        "quota_ledger_healthy": ready,
        "expected_days_remaining": ready,
        "worst_case_days_remaining": ready,
        "monitoring_reserve": ready,
        "backup_restore_verified": ready,
        "off_disk_copy_verified": ready,
        "different_device_verified": ready,
        "off_disk_copy_fresh": ready,
        "timestamp_migration": ready,
        "lineage_migration": ready,
        "old_data_readable": ready,
    }
    return {
        "hard_gate": {
            "ready": ready,
            "checks": checks,
            "missing": [] if ready else list(checks),
            "reasons": {
                name: "test gate missing" for name, value in checks.items() if not value
            },
            "current": {},
            "requirements": {},
        }
    }


class ResearchControlTest(unittest.TestCase):
    def test_enable_refuses_when_readiness_is_false_and_keeps_bytes(self):
        from scripts.research_control import enable_research

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "basket_state.json"
            path.write_text(
                json.dumps(
                    {"research_cohort_v2": {"runtime_enabled": False}},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            before = path.read_bytes()
            result = enable_research(
                path,
                _report(ready=False),
                now="2026-08-28T09:00:00+08:00",
            )
            after = path.read_bytes()

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["runtime_enabled"])
        self.assertEqual(before, after)
        self.assertIn("quota_ledger_healthy", result["missing"])

    def test_enable_atomically_sets_runtime_and_archives_old_guard(self):
        from scripts.research_control import enable_research

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "basket_state.json"
            path.write_text(
                json.dumps(
                    {
                        "research_cohort_v2": {
                            "runtime_enabled": False,
                            "quota_guard": {
                                "triggered": True,
                                "disabled_at": "2026-08-27T09:00:00+08:00",
                                "reason_codes": ["monitoring_reserve_reached"],
                            },
                        }
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            result = enable_research(
                path,
                _report(ready=True),
                now="2026-08-28T09:00:00+08:00",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        cohort = payload["research_cohort_v2"]
        self.assertEqual(result["status"], "enabled")
        self.assertTrue(cohort["runtime_enabled"])
        self.assertEqual(cohort["runtime_control"]["action"], "enable")
        self.assertEqual(cohort["runtime_control"]["at"], "2026-08-28T09:00:00+08:00")
        self.assertNotIn("quota_guard", cohort)
        self.assertEqual(len(cohort["quota_guard_history"]), 1)

    def test_disable_records_reason_and_time_without_erasing_other_state(self):
        from scripts.research_control import disable_research

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "basket_state.json"
            path.write_text(
                json.dumps(
                    {
                        "research_cohort_v2": {
                            "runtime_enabled": True,
                            "probes": {"probe_1": {"valid_n": 2}},
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            result = disable_research(
                path,
                reason="operator pause",
                now="2026-08-28T10:00:00+08:00",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        cohort = payload["research_cohort_v2"]
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(cohort["runtime_enabled"])
        self.assertTrue(cohort["user_monitoring_enabled"])
        self.assertEqual(cohort["runtime_control"]["reason"], "operator pause")
        self.assertEqual(cohort["runtime_control"]["at"], "2026-08-28T10:00:00+08:00")
        self.assertEqual(cohort["probes"]["probe_1"]["valid_n"], 2)

    def test_status_cli_is_read_only_and_lists_every_hard_gate(self):
        from scripts.research_control import main

        report = _report(ready=False)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "basket_state.json"
            path.write_text(
                json.dumps({"research_cohort_v2": {"runtime_enabled": False}})
                + "\n",
                encoding="utf-8",
            )
            before = path.read_bytes()
            output = io.StringIO()
            with (
                patch("scripts.research_control.build_report", return_value=report),
                redirect_stdout(output),
            ):
                code = main(["--state", str(path), "status"])
            after = path.read_bytes()

        self.assertEqual(code, 0)
        self.assertEqual(before, after)
        self.assertIn("runtime_enabled=false", output.getvalue())
        for gate_name in report["hard_gate"]["checks"]:
            self.assertIn(gate_name, output.getvalue())

    def test_guard_notifier_degrades_on_json_store_read_error_and_still_alerts(self):
        from atomic_json_store import JsonStoreReadError
        import basket_collect

        alert = Mock(return_value=True)
        fake_main = types.SimpleNamespace(_notify_system_alert=alert)
        with (
            patch.object(
                basket_collect,
                "read_json",
                side_effect=JsonStoreReadError("damaged subscriptions"),
            ),
            patch.dict(sys.modules, {"main": fake_main}),
        ):
            sent = basket_collect._default_quota_guard_notifier(
                "data/basket_state.json",
                "guard title",
                "guard body",
            )

        self.assertTrue(sent)
        alert.assert_called_once_with([], "guard title", "guard body")


if __name__ == "__main__":
    unittest.main()
