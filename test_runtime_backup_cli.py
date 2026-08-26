import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


class RuntimeBackupCliTest(unittest.TestCase):
    def test_create_requires_explicit_output_directory(self):
        from scripts.runtime_backup import build_parser

        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["create"])

    def test_busy_returns_two_and_prints_only_sanitized_summary(self):
        from scripts.runtime_backup import main

        result = {
            "status": "busy",
            "exit_code": 2,
            "backup_id": "backup-safe-id",
            "archive_path": None,
            "checksum_path": None,
            "holder": {
                "pid": 123,
                "round_id": "private-round-id",
                "hostname": "private-host",
            },
        }
        output = io.StringIO()
        with (
            patch("scripts.runtime_backup.create_runtime_backup", return_value=result),
            redirect_stdout(output),
        ):
            exit_code = main(["create", "--output-dir", str(Path(tempfile.gettempdir()).resolve())])

        rendered = output.getvalue()
        self.assertEqual(exit_code, 2)
        self.assertIn('"status": "busy"', rendered)
        self.assertIn("backup-safe-id", rendered)
        self.assertNotIn("private-round-id", rendered)
        self.assertNotIn("private-host", rendered)
        self.assertNotIn('"pid"', rendered)

    def test_created_summary_has_only_public_contract_fields(self):
        from scripts.runtime_backup import main

        result = {
            "status": "created",
            "exit_code": 0,
            "backup_id": "backup-safe-id",
            "archive_path": "/private/route/PVG-KIX.tar.gz",
            "checksum_path": "/private/route/PVG-KIX.tar.gz.sha256",
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
            patch("scripts.runtime_backup.create_runtime_backup", return_value=result),
            redirect_stdout(output),
        ):
            exit_code = main(["create", "--output-dir", str(Path(tempfile.gettempdir()).resolve())])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            set(payload),
            {
                "status",
                "backup_id",
                "archive_sha256",
                "file_count",
                "total_bytes",
                "sqlite_integrity",
                "json_valid",
                "replay_sha256",
                "production_state_changed",
                "real_api_calls",
            },
        )
        self.assertNotIn("PVG", output.getvalue())
        self.assertNotIn("private", output.getvalue())

    def test_rehearse_combines_create_restore_and_replay_without_private_paths(self):
        from scripts.runtime_backup import main

        backup = {
            "status": "created",
            "exit_code": 0,
            "backup_id": "backup-safe-id",
            "archive_path": "/private/archive.tar.gz",
            "checksum_path": "/private/archive.tar.gz.sha256",
            "archive_sha256": "b" * 64,
            "file_count": 9,
            "total_bytes": 4321,
            "sqlite_integrity": True,
            "json_valid": True,
            "source_report_sha256": {"tcurve_source.txt": "c" * 64},
            "real_api_calls": 0,
        }
        rehearsal = {
            "status": "verified",
            "sqlite_integrity": True,
            "json_valid": True,
            "replay_match": True,
            "restored_report_sha256": {"tcurve_source.txt": "c" * 64},
            "path": "/private/restored",
        }
        output = io.StringIO()
        with (
            patch("scripts.runtime_backup.create_runtime_backup", return_value=backup),
            patch("scripts.runtime_backup.rehearse_runtime_backup", return_value=rehearsal),
            redirect_stdout(output),
        ):
            exit_code = main(
                [
                    "rehearse",
                    "--output-dir",
                    str(Path(tempfile.gettempdir()).resolve()),
                    "--route",
                    "private-route",
                ]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["replay_sha256"], rehearsal["restored_report_sha256"])
        self.assertFalse(payload["production_state_changed"])
        self.assertEqual(payload["real_api_calls"], 0)
        self.assertNotIn("private-route", output.getvalue())
        self.assertNotIn("/private", output.getvalue())

    def test_validation_error_returns_one_without_traceback(self):
        from runtime_backup import UnknownRuntimePathsError
        from scripts.runtime_backup import main

        output = io.StringIO()
        errors = io.StringIO()
        with (
            patch(
                "scripts.runtime_backup.create_runtime_backup",
                side_effect=UnknownRuntimePathsError("future_state.bin"),
            ),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            exit_code = main(["create", "--output-dir", str(Path(tempfile.gettempdir()).resolve())])

        self.assertEqual(exit_code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("future_state.bin", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
