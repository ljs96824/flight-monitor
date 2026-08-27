import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_runtime_backup import _empty_permission_metadata, _runtime_fixture


ROOT = Path(__file__).resolve().parent


class RuntimeBackupDirectCliTest(unittest.TestCase):
    def test_direct_create_accepts_output_dir_and_label(self):
        from scripts.runtime_backup import main

        result = {
            "status": "created",
            "exit_code": 0,
            "backup_id": "backup-weekly",
            "archive_path": "/private/flight-monitor-backup-weekly.tar.gz",
            "checksum_path": "/private/flight-monitor-backup-weekly.tar.gz.sha256",
            "archive_sha256": "a" * 64,
            "file_count": 8,
            "total_bytes": 1234,
            "sqlite_integrity": True,
            "json_valid": True,
            "source_report_sha256": {},
            "real_api_calls": 0,
        }
        output = io.StringIO()
        with (
            patch("scripts.runtime_backup.create_runtime_backup", return_value=result) as create,
            redirect_stdout(output),
        ):
            exit_code = main(
                [
                    "--output-dir",
                    str(Path(tempfile.gettempdir()).resolve()),
                    "--label",
                    "weekly",
                ]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["operation"], "create")
        self.assertTrue(payload["passed"])
        self.assertEqual(payload["archive_path"], result["archive_path"])
        self.assertEqual(payload["archive_sha256"], "a" * 64)
        self.assertEqual(payload["file_count"], 8)
        self.assertEqual(payload["total_bytes"], 1234)
        self.assertEqual(create.call_args.kwargs["label"], "weekly")

    def test_direct_create_rejects_output_inside_project(self):
        from scripts.runtime_backup import main

        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, data = _runtime_fixture(root)
            with redirect_stderr(errors):
                exit_code = main(
                    [
                        "--output-dir",
                        str(data / "backups"),
                        "--project-root",
                        str(project),
                        "--data-root",
                        str(data),
                    ]
                )

        self.assertEqual(exit_code, 1)
        self.assertIn("不得位于project_root或data_root内", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_label_is_a_safe_archive_slug(self):
        from runtime_backup import InvalidBackupLabel, create_runtime_backup

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, data = _runtime_fixture(root)
            result = create_runtime_backup(
                output_dir=root / "archives",
                project_root=project,
                data_root=data,
                label="weekly",
                permission_metadata_builder=_empty_permission_metadata,
            )
            self.assertIn("-weekly-", Path(result["archive_path"]).name)
            with self.assertRaises(InvalidBackupLabel):
                create_runtime_backup(
                    output_dir=root / "other-archives",
                    project_root=project,
                    data_root=data,
                    label="../private-route",
                    permission_metadata_builder=_empty_permission_metadata,
                )


class RuntimeRestoreCliTest(unittest.TestCase):
    def _create_archive(self, root: Path) -> tuple[dict, Path]:
        from runtime_backup import create_runtime_backup

        project, data = _runtime_fixture(root)
        status_path = data / "backup_status.json"
        backup = create_runtime_backup(
            output_dir=root / "archives",
            project_root=project,
            data_root=data,
            status_path=status_path,
            permission_metadata_builder=_empty_permission_metadata,
        )
        return backup, status_path

    def test_restore_and_off_disk_verification_turn_backup_gates_green(self):
        from backup_status import load_backup_evidence
        from research_cohort import evaluate_research_hard_gates
        from scripts.runtime_restore import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup, status_path = self._create_archive(root)
            restore_output = io.StringIO()
            with redirect_stdout(restore_output):
                restore_exit = main(
                    [
                        "--archive",
                        backup["archive_path"],
                        "--backup-status",
                        str(status_path),
                    ]
                )

            copied = root / "off-disk" / Path(backup["archive_path"]).name
            copied.parent.mkdir()
            shutil.copy2(backup["archive_path"], copied)
            copy_output = io.StringIO()
            with redirect_stdout(copy_output):
                copy_exit = main(
                    [
                        "--verify-off-disk",
                        str(copied),
                        "--backup-status",
                        str(status_path),
                    ]
                )

            evidence = load_backup_evidence(status_path)
            hard_gate = evaluate_research_hard_gates(
                backup_evidence=evidence,
                quota_simulation={
                    "complete": True,
                    "expected_days_remaining": 30,
                    "worst_case_days_remaining": 20,
                    "remaining_after_research": 500,
                    "monitoring_reserve": 400,
                },
                migration_status={
                    "timestamp_ready": True,
                    "lineage_ready": True,
                    "old_data_readable": True,
                },
            )

        restored = json.loads(restore_output.getvalue())
        copied_result = json.loads(copy_output.getvalue())
        self.assertEqual(restore_exit, 0)
        self.assertEqual(copy_exit, 0)
        self.assertEqual(restored["operation"], "restore")
        self.assertTrue(restored["passed"])
        self.assertIn("verified_restore_at", restored["status_fields_written"])
        self.assertEqual(copied_result["operation"], "verify_off_disk")
        self.assertTrue(copied_result["passed"])
        self.assertIn("off_disk_copy", copied_result["status_fields_written"])
        self.assertTrue(hard_gate["checks"]["backup_restore_verified"])
        self.assertTrue(hard_gate["checks"]["off_disk_copy_verified"])
        self.assertTrue(hard_gate["checks"]["off_disk_copy_fresh"])

    def test_status_prints_every_persisted_field(self):
        from scripts.runtime_restore import main

        status = {
            "status_version": "backup_status_v1",
            "backup_id": "backup-safe-id",
            "archive_sha256": "b" * 64,
            "verified_restore_at": "2026-08-27T00:00:00Z",
            "off_disk_copy": {
                "verified": True,
                "verified_at": "2026-08-27T00:01:00Z",
                "destination_kind": "physical_disk",
                "copied_sha256": "b" * 64,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / "backup_status.json"
            status_path.write_text(json.dumps(status), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(["--status", "--backup-status", str(status_path)])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["operation"], "status")
        self.assertTrue(payload["passed"])
        self.assertEqual(payload["backup_status"], status)

    def test_restore_corrupt_archive_returns_nonzero(self):
        from scripts.runtime_restore import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "corrupt.tar.gz"
            archive.write_bytes(b"not a tar archive")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            Path(f"{archive}.sha256").write_text(
                f"{digest}  {archive.name}\n", encoding="ascii"
            )
            errors = io.StringIO()
            with redirect_stderr(errors):
                exit_code = main(
                    [
                        "--archive",
                        str(archive),
                        "--backup-status",
                        str(root / "backup_status.json"),
                    ]
                )

        self.assertEqual(exit_code, 1)
        self.assertIn("归档无法安全读取或解压", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_off_disk_sha_mismatch_and_missing_file_return_nonzero(self):
        from scripts.runtime_restore import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status_path = root / "backup_status.json"
            status_path.write_text(
                json.dumps(
                    {
                        "status_version": "backup_status_v1",
                        "backup_id": "backup-safe-id",
                        "archive_sha256": "c" * 64,
                        "verified_restore_at": None,
                        "off_disk_copy": {"verified": False},
                    }
                ),
                encoding="utf-8",
            )
            wrong = root / "wrong.tar.gz"
            wrong.write_bytes(b"wrong")

            mismatch_error = io.StringIO()
            with redirect_stderr(mismatch_error):
                mismatch_exit = main(
                    [
                        "--verify-off-disk",
                        str(wrong),
                        "--backup-status",
                        str(status_path),
                    ]
                )
            missing_error = io.StringIO()
            with redirect_stderr(missing_error):
                missing_exit = main(
                    [
                        "--verify-off-disk",
                        str(root / "missing.tar.gz"),
                        "--backup-status",
                        str(status_path),
                    ]
                )

        self.assertEqual(mismatch_exit, 1)
        self.assertIn("SHA256", mismatch_error.getvalue())
        self.assertEqual(missing_exit, 1)
        self.assertIn("不存在", missing_error.getvalue())

    def test_status_missing_file_returns_nonzero(self):
        from scripts.runtime_restore import main

        with tempfile.TemporaryDirectory() as directory:
            errors = io.StringIO()
            with redirect_stderr(errors):
                exit_code = main(
                    [
                        "--status",
                        "--backup-status",
                        str(Path(directory) / "missing.json"),
                    ]
                )

        self.assertEqual(exit_code, 1)
        self.assertIn("backup_status.json不存在", errors.getvalue())


class RuntimeRootModuleEntrypointTest(unittest.TestCase):
    def test_root_modules_redirect_users_to_scripts(self):
        cases = {
            "runtime_backup.py": "scripts/runtime_backup.py",
            "runtime_restore.py": "scripts/runtime_restore.py",
        }
        for module, script in cases.items():
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-X", "utf8", module],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn(script, result.stderr)


if __name__ == "__main__":
    unittest.main()
