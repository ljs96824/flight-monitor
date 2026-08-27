import json
import io
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


def _valid_ledger() -> dict:
    return {"version": 2, "dates": {}, "entries": []}


class _CountingSource:
    name = "juhe"

    def __init__(self):
        self.calls = 0

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        self.calls += 1
        return {
            "source": self.name,
            "flights": [{"flight_combo": "MU1", "price": 100}],
        }


class ApiUsageFailClosedTest(unittest.TestCase):
    def test_strict_read_rejects_missing_corrupt_and_incomplete_ledgers(self):
        from api_usage import UsageLedgerReadError, load_usage_strict

        cases = {
            "missing": None,
            "invalid_json": b"{not-json",
            "root_not_object": b"[]\n",
            "missing_entries": b'{"version":2,"dates":{}}\n',
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            for name, content in cases.items():
                with self.subTest(case=name):
                    path = root / f"{name}.json"
                    if content is not None:
                        path.write_bytes(content)
                    before = path.read_bytes() if path.exists() else None

                    with self.assertRaises(UsageLedgerReadError):
                        load_usage_strict(path)

                    after = path.read_bytes() if path.exists() else None
                    self.assertEqual(after, before)

    def test_bad_ledger_blocks_source_call_and_is_not_overwritten(self):
        import request_cache
        from api_usage import UsageLedgerReadError

        cases = {
            "missing": None,
            "invalid_json": b"{not-json",
            "root_not_object": b"[]\n",
            "missing_dates": b'{"version":2,"entries":[]}\n',
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            for name, content in cases.items():
                with self.subTest(case=name):
                    usage_path = root / f"{name}.json"
                    if content is not None:
                        usage_path.write_bytes(content)
                    before = usage_path.read_bytes() if usage_path.exists() else None
                    source = _CountingSource()
                    request_cache.reset_for_tests(root / f"cache-{name}")

                    with self.assertRaises(UsageLedgerReadError):
                        request_cache.start_request_cache_round(
                            f"round-{name}",
                            track_usage=True,
                            usage_path=usage_path,
                        )
                        request_cache.cached_fetch(
                            source,
                            "PVG",
                            "KIX",
                            "2099-10-01",
                            persist=False,
                            force_fresh=True,
                        )

                    after = usage_path.read_bytes() if usage_path.exists() else None
                    self.assertEqual(source.calls, 0)
                    self.assertEqual(after, before)
            request_cache.reset_for_tests(None)

    def test_explicit_initializer_is_the_only_empty_ledger_creation_path(self):
        from api_usage import (
            UsageLedgerAlreadyExists,
            initialize_usage_ledger,
            load_usage_strict,
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "api_usage.json"
            initialized = initialize_usage_ledger(path)
            self.assertEqual(initialized, _valid_ledger())
            self.assertEqual(load_usage_strict(path), _valid_ledger())
            with self.assertRaises(UsageLedgerAlreadyExists):
                initialize_usage_ledger(path)

    def test_initializer_cli_creates_once_and_refuses_overwrite(self):
        from scripts.initialize_api_usage import main

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "api_usage.json"
            output = io.StringIO()
            with redirect_stdout(output):
                first = main(["--path", str(path)])
            error = io.StringIO()
            with redirect_stderr(error):
                second = main(["--path", str(path)])

        self.assertEqual(first, 0)
        self.assertEqual(second, 2)
        self.assertIn("version=2", output.getvalue())
        self.assertIn("拒绝覆盖", error.getvalue())

    def test_diagnostic_read_marks_damage_without_inventing_empty_usage(self):
        from api_usage import load_usage_for_diagnostics

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "api_usage.json"
            path.write_text("{broken", encoding="utf-8")
            result = load_usage_for_diagnostics(path)

        self.assertFalse(result["healthy"])
        self.assertIsNone(result["usage"])
        self.assertEqual(result["error_type"], "UsageLedgerReadError")

    def test_quota_overview_discloses_unavailable_ledger_without_creating_one(self):
        import api_usage
        import notifier

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "api_usage.json"
            with (
                patch(
                    "collection_plan.load_collection_settings",
                    return_value={"source_quota_budget": {}},
                ),
                patch.object(api_usage, "DEFAULT_USAGE_PATH", path),
                patch.object(notifier, "safe_log") as log_mock,
            ):
                text = notifier._quota_overview_text()

            self.assertFalse(path.exists())

        self.assertEqual(text, "配额总览:台账不可用(不得据此恢复配额)")
        self.assertTrue(
            any("展示不可用" in str(call.args[0]) for call in log_mock.call_args_list)
        )

    def test_quota_overview_discloses_missing_runtime_config(self):
        import notifier

        from config_loader import RuntimeConfigError

        with (
            patch(
                "collection_plan.load_collection_settings",
                side_effect=RuntimeConfigError("fixture missing"),
            ),
            patch.object(notifier, "safe_log") as log_mock,
        ):
            text = notifier._quota_overview_text()

        self.assertEqual(text, "配额总览:运行配置不可用(禁止真实API)")
        self.assertTrue(
            any("禁止真实API" in str(call.args[0]) for call in log_mock.call_args_list)
        )

    def test_lock_conflict_keeps_pending_counts_and_blocks_research_gate(self):
        from api_usage import (
            _usage_lock,
            initialize_usage_ledger,
            record_actual_requests,
            usage_ledger_health,
        )
        from research_cohort import evaluate_research_hard_gates

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            usage_path = root / "api_usage.json"
            conflict_path = root / "api_usage_conflict.log"
            initialize_usage_ledger(usage_path)
            acquired = threading.Event()
            release = threading.Event()

            def hold_lock():
                with _usage_lock(usage_path, timeout=1.0):
                    acquired.set()
                    release.wait(timeout=5)

            holder = threading.Thread(target=hold_lock)
            holder.start()
            self.assertTrue(acquired.wait(timeout=2))
            try:
                record_actual_requests(
                    {"juhe": 2},
                    path=usage_path,
                    round_id="pending-round",
                    workload_class="manual_live",
                    entrypoint="web",
                    lock_timeout=0.01,
                    lock_retries=0,
                    conflict_log_path=conflict_path,
                )
            finally:
                release.set()
                holder.join(timeout=2)

            row = json.loads(conflict_path.read_text(encoding="utf-8").splitlines()[-1])
            health = usage_ledger_health(
                usage_path,
                conflict_log_path=conflict_path,
            )

        self.assertEqual(row["status"], "pending_reconciliation")
        self.assertEqual(row["counts"], {"juhe": 2})
        self.assertEqual(row["workload_class"], "manual_live")
        self.assertEqual(row["entrypoint"], "web")
        self.assertFalse(health["healthy"])
        self.assertEqual(health["pending_reconciliation_count"], 1)

        result = evaluate_research_hard_gates(
            backup_evidence={
                "checks": {
                    "backup_restore_verified": True,
                    "off_disk_copy_verified": True,
                    "different_device_verified": True,
                    "off_disk_copy_fresh": True,
                }
            },
            quota_simulation={
                "complete": True,
                "expected_days_remaining": 60,
                "worst_case_days_remaining": 40,
                "remaining_after_research": 600,
                "monitoring_reserve": 400,
                "quota_ledger_healthy": False,
                "pending_reconciliation_count": 1,
            },
            migration_status={
                "timestamp_ready": True,
                "lineage_ready": True,
                "old_data_readable": True,
            },
        )
        self.assertFalse(result["checks"]["quota_ledger_healthy"])
        self.assertIn("quota_ledger_healthy", result["missing"])

    def test_pending_evidence_keeps_user_plan_readable_in_conservative_mode(self):
        from api_usage import initialize_usage_ledger
        import main

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            usage_path = root / "api_usage.json"
            initialize_usage_ledger(usage_path)
            (root / "api_usage_conflict.log").write_text(
                json.dumps(
                    {
                        "evidence_id": "pending-one",
                        "status": "pending_reconciliation",
                        "counts": {"juhe": 1},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            settings = {
                "source_quota_budget": {},
                "source_quota_low_remaining_threshold": 50,
                "freshness_hours": 6,
                "sub_round_fresh_scope": "primary_only",
            }
            with (
                patch.object(main, "API_USAGE_PATH", usage_path),
                patch.object(main, "load_collection_settings", return_value=settings),
                patch.object(main, "safe_log") as log_mock,
            ):
                options = main._collection_plan_log_options()

        self.assertEqual(options["usage_snapshot"]["cumulative"], {})
        self.assertTrue(
            any("用户监控保守模式" in str(call.args[0]) for call in log_mock.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
