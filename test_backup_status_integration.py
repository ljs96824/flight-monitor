import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_runtime_backup import _empty_permission_metadata, _runtime_fixture


class BackupStatusIntegrationTest(unittest.TestCase):
    def test_create_restore_and_off_disk_verification_update_one_status(self):
        from backup_status import verify_off_disk_copy
        from runtime_backup import create_runtime_backup
        from runtime_restore import restore_runtime_backup

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, data = _runtime_fixture(root)
            status_path = data / "backup_status.json"
            backup = create_runtime_backup(
                output_dir=root / "archives",
                project_root=project,
                data_root=data,
                status_path=status_path,
                permission_metadata_builder=_empty_permission_metadata,
            )
            created = json.loads(status_path.read_text(encoding="utf-8"))
            copy = root / "external" / Path(backup["archive_path"]).name
            copy.parent.mkdir()
            shutil.copy2(backup["archive_path"], copy)
            restored = restore_runtime_backup(
                backup["archive_path"],
                checksum_path=backup["checksum_path"],
                destination=root / "restored",
                status_path=status_path,
            )
            verified = verify_off_disk_copy(
                backup["archive_path"],
                copy,
                status_path=status_path,
                backup_id=backup["backup_id"],
                destination_kind="physical_disk",
            )

        self.assertEqual(created["backup_id"], backup["backup_id"])
        self.assertEqual(created["archive_sha256"], backup["archive_sha256"])
        self.assertIsNone(created["verified_restore_at"])
        self.assertEqual(restored["status"], "verified")
        self.assertIsNotNone(verified["verified_restore_at"])
        self.assertTrue(verified["off_disk_copy"]["verified"])

    def test_restore_cli_verifies_requested_off_disk_path(self):
        from scripts.runtime_backup import main

        result = {
            "status": "verified",
            "archive_sha256": "a" * 64,
            "file_count": 4,
            "total_bytes": 100,
            "sqlite_integrity": True,
            "json_valid": True,
            "manifest": {"backup_id": "backup-1"},
        }
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("scripts.runtime_backup.restore_runtime_backup", return_value=result),
                patch("scripts.runtime_backup.verify_off_disk_copy") as verify,
                redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "restore",
                        "--archive",
                        str(root / "archive.tar.gz"),
                        "--project-root",
                        str(root),
                        "--verify-off-disk",
                        str(root / "external.tar.gz"),
                        "--off-disk-kind",
                        "private_encrypted_cloud",
                    ]
                )

        self.assertEqual(exit_code, 0)
        verify.assert_called_once_with(
            str(root / "archive.tar.gz"),
            str(root / "external.tar.gz"),
            status_path=root.resolve() / "data" / "backup_status.json",
            backup_id="backup-1",
            destination_kind="private_encrypted_cloud",
        )


if __name__ == "__main__":
    unittest.main()
