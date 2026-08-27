import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


def _ledger(*, dates=None, entries=None) -> dict:
    return {
        "version": 2,
        "dates": dates or {},
        "entries": entries or [],
    }


def _write_pending(
    path: Path,
    *,
    evidence_id: str = "evidence-one",
    day: str = "2026-08-28",
    counts=None,
) -> None:
    path.write_text(
        json.dumps(
            {
                "evidence_id": evidence_id,
                "recorded_at": f"{day}T09:00:00+08:00",
                "day": day,
                "round_id": "round-one",
                "status": "pending_reconciliation",
                "usage_path": str(path.parent / "api_usage.json"),
                "reason": "test lock timeout",
                "counts": counts or {"juhe": 2},
                "workload_class": "manual_live",
                "entrypoint": "web",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


class ApiUsageReconciliationTest(unittest.TestCase):
    def test_transient_atomic_replace_error_retries_without_losing_count(self):
        import api_usage

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            usage_path = root / "api_usage.json"
            conflict_path = root / "api_usage_conflict.log"
            api_usage.initialize_usage_ledger(usage_path)
            original_writer = api_usage._atomic_write_json
            attempts = 0

            def flaky_writer(path, payload):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise PermissionError("simulated transient replace denial")
                return original_writer(path, payload)

            with patch.object(api_usage, "_atomic_write_json", side_effect=flaky_writer):
                payload = api_usage.record_actual_requests(
                    {"juhe": 1},
                    path=usage_path,
                    day="2026-08-28",
                    round_id="replace-retry",
                    lock_retries=1,
                    conflict_log_path=conflict_path,
                )
            conflict_exists = conflict_path.exists()

        self.assertEqual(attempts, 2)
        self.assertEqual(payload["dates"]["2026-08-28"]["juhe"], 1)
        self.assertFalse(conflict_exists)

    def test_pending_list_apply_is_idempotent_and_restores_health(self):
        from api_usage import load_usage_strict, usage_ledger_health
        from scripts.reconcile_api_usage import main

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            usage_path = root / "api_usage.json"
            conflict_path = root / "api_usage_conflict.log"
            usage_path.write_text(
                json.dumps(_ledger(), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            original_sha = hashlib.sha256(usage_path.read_bytes()).hexdigest()
            _write_pending(conflict_path)

            listed = io.StringIO()
            with redirect_stdout(listed):
                list_code = main(
                    [
                        "--usage-path",
                        str(usage_path),
                        "--conflict-log-path",
                        str(conflict_path),
                        "list",
                    ]
                )
            applied = io.StringIO()
            with redirect_stdout(applied):
                apply_code = main(
                    [
                        "--usage-path",
                        str(usage_path),
                        "--conflict-log-path",
                        str(conflict_path),
                        "apply",
                        "--evidence-id",
                        "evidence-one",
                        "--confirm",
                        "APPLY",
                    ]
                )
            second_applied = io.StringIO()
            with redirect_stdout(second_applied):
                second_apply_code = main(
                    [
                        "--usage-path",
                        str(usage_path),
                        "--conflict-log-path",
                        str(conflict_path),
                        "apply",
                        "--evidence-id",
                        "evidence-one",
                        "--confirm",
                        "APPLY",
                    ]
                )

            payload = load_usage_strict(usage_path)
            backups = list(root.glob("api_usage.json.reconcile-*.bak"))
            backup_sha = (
                hashlib.sha256(backups[0].read_bytes()).hexdigest()
                if backups
                else None
            )
            health = usage_ledger_health(
                usage_path,
                conflict_log_path=conflict_path,
            )

        self.assertEqual(list_code, 0)
        self.assertIn("evidence-one", listed.getvalue())
        self.assertIn("manual_live", listed.getvalue())
        self.assertEqual(apply_code, 0)
        self.assertEqual(second_apply_code, 0)
        self.assertEqual(payload["dates"]["2026-08-28"]["juhe"], 2)
        reconciled_entries = [
            row
            for row in payload["entries"]
            if row.get("reconciliation_evidence_id") == "evidence-one"
        ]
        self.assertEqual(len(reconciled_entries), 1)
        self.assertEqual(len(backups), 1)
        self.assertEqual(backup_sha, original_sha)
        self.assertTrue(health["healthy"])
        self.assertEqual(health["pending_reconciliation_count"], 0)
        self.assertIn("status=already_reconciled", second_applied.getvalue())

    def test_dismiss_requires_reason_and_keeps_counts_unchanged(self):
        from api_usage import UsageReconciliationError, reconcile_usage_evidence

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            usage_path = root / "api_usage.json"
            conflict_path = root / "api_usage_conflict.log"
            original = json.dumps(_ledger(), ensure_ascii=False) + "\n"
            usage_path.write_text(original, encoding="utf-8")
            _write_pending(conflict_path, evidence_id="dismiss-me")
            legacy_row = json.loads(conflict_path.read_text(encoding="utf-8"))
            legacy_row.pop("day")
            legacy_row["recorded_at"] = "legacy-time-without-offset"
            conflict_path.write_text(
                json.dumps(legacy_row, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(UsageReconciliationError):
                reconcile_usage_evidence(
                    "dismiss-me",
                    action="dismiss",
                    reason="",
                    usage_path=usage_path,
                    conflict_log_path=conflict_path,
                )

            result = reconcile_usage_evidence(
                "dismiss-me",
                action="dismiss",
                reason="duplicate external evidence",
                usage_path=usage_path,
                conflict_log_path=conflict_path,
            )
            payload = json.loads(usage_path.read_text(encoding="utf-8"))
            events = [
                json.loads(line)
                for line in conflict_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(result["status"], "reconciled")
        self.assertEqual(result["action"], "dismiss")
        self.assertEqual(payload, _ledger())
        self.assertEqual(events[-1]["status"], "reconciled")
        self.assertEqual(events[-1]["reason"], "duplicate external evidence")

    def test_consistency_allows_only_exact_pre_epoch_legacy_aggregate(self):
        from api_usage import UsageLedgerReadError, load_usage_strict

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            accepted = root / "accepted.json"
            accepted.write_text(
                json.dumps(
                    _ledger(
                        dates={
                            "2026-07-22": {
                                "duffel": 50,
                                "hasdata": 88,
                                "juhe": 196,
                            }
                        }
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            rejected = root / "rejected.json"
            rejected.write_text(
                json.dumps(_ledger(dates={"2026-08-28": {"juhe": 1}})) + "\n",
                encoding="utf-8",
            )

            accepted_payload = load_usage_strict(accepted)
            with self.assertRaises(UsageLedgerReadError) as captured:
                load_usage_strict(rejected)

        self.assertEqual(accepted_payload["dates"]["2026-07-22"]["juhe"], 196)
        self.assertIn("dates/entries", str(captured.exception))

    def test_cli_rejects_mutation_without_exact_confirmation(self):
        from scripts.reconcile_api_usage import main

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            usage_path = root / "api_usage.json"
            conflict_path = root / "api_usage_conflict.log"
            usage_path.write_text(json.dumps(_ledger()) + "\n", encoding="utf-8")
            _write_pending(conflict_path)
            errors = io.StringIO()
            with redirect_stderr(errors):
                code = main(
                    [
                        "--usage-path",
                        str(usage_path),
                        "--conflict-log-path",
                        str(conflict_path),
                        "apply",
                        "--evidence-id",
                        "evidence-one",
                        "--confirm",
                        "wrong",
                    ]
                )

        self.assertEqual(code, 2)
        self.assertIn("--confirm APPLY", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
