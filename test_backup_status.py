import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


class BackupStatusTest(unittest.TestCase):
    def test_matching_off_disk_copy_records_verified_evidence(self):
        from backup_status import (
            evaluate_backup_evidence,
            record_backup_created,
            record_restore_verified,
            verify_off_disk_copy,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "runtime.tar.gz"
            copy = root / "off-disk.tar.gz"
            status_path = root / "backup_status.json"
            archive.write_bytes(b"verified archive")
            archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
            shutil.copy2(archive, copy)
            created = record_backup_created(
                status_path,
                backup_id="backup-1",
                archive_sha256=archive_hash,
            )
            self.assertFalse(created["off_disk_copy"]["verified"])
            record_restore_verified(
                status_path,
                backup_id="backup-1",
                archive_sha256=archive_hash,
                verified_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
            result = verify_off_disk_copy(
                archive,
                copy,
                status_path=status_path,
                backup_id="backup-1",
                destination_kind="physical_disk",
                verified_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
            )
            evidence = evaluate_backup_evidence(
                result,
                now=datetime(2026, 8, 27, tzinfo=timezone.utc),
                max_age_days=30,
            )

        self.assertTrue(result["off_disk_copy"]["verified"])
        self.assertEqual(
            result["off_disk_copy"]["copied_sha256"],
            result["archive_sha256"],
        )
        self.assertEqual(result["off_disk_copy"]["destination_kind"], "physical_disk")
        self.assertEqual(
            evidence["checks"],
            {
                "backup_restore_verified": True,
                "off_disk_copy_verified": True,
                "off_disk_copy_fresh": True,
            },
        )

    def test_mismatch_does_not_mark_copy_verified(self):
        from backup_status import (
            OffDiskCopyMismatch,
            record_backup_created,
            record_restore_verified,
            verify_off_disk_copy,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "runtime.tar.gz"
            copy = root / "off-disk.tar.gz"
            status_path = root / "backup_status.json"
            archive.write_bytes(b"archive")
            copy.write_bytes(b"different")
            record_backup_created(
                status_path,
                backup_id="backup-1",
                archive_sha256="a" * 64,
            )
            record_restore_verified(
                status_path,
                backup_id="backup-1",
                archive_sha256="a" * 64,
            )

            with self.assertRaises(OffDiskCopyMismatch):
                verify_off_disk_copy(
                    archive,
                    copy,
                    status_path=status_path,
                    backup_id="backup-1",
                    destination_kind="physical_disk",
                )
            status = json.loads(status_path.read_text(encoding="utf-8"))

        self.assertFalse(status["off_disk_copy"]["verified"])
        self.assertIsNone(status["off_disk_copy"]["copied_sha256"])

    def test_missing_copy_does_not_change_status(self):
        from backup_status import (
            OffDiskCopyMissing,
            record_backup_created,
            verify_off_disk_copy,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "runtime.tar.gz"
            status_path = root / "backup_status.json"
            archive.write_bytes(b"archive")
            record_backup_created(
                status_path,
                backup_id="backup-1",
                archive_sha256="a" * 64,
            )
            before = status_path.read_bytes()
            with self.assertRaises(OffDiskCopyMissing):
                verify_off_disk_copy(
                    archive,
                    root / "missing.tar.gz",
                    status_path=status_path,
                    backup_id="backup-1",
                    destination_kind="physical_disk",
                )
            after = status_path.read_bytes()

        self.assertEqual(after, before)

    def test_stale_copy_blocks_readiness_and_exact_boundary_passes(self):
        from backup_status import evaluate_backup_evidence

        status = {
            "backup_id": "backup-1",
            "archive_sha256": "a" * 64,
            "verified_restore_at": "2026-07-28T00:00:00Z",
            "off_disk_copy": {
                "verified": True,
                "verified_at": "2026-07-28T00:00:00Z",
                "destination_kind": "physical_disk",
                "copied_sha256": "a" * 64,
            },
        }
        boundary = evaluate_backup_evidence(
            status,
            now=datetime(2026, 8, 27, tzinfo=timezone.utc),
            max_age_days=30,
        )
        stale = evaluate_backup_evidence(
            status,
            now=datetime(2026, 8, 27, 0, 0, 1, tzinfo=timezone.utc),
            max_age_days=30,
        )

        self.assertTrue(boundary["checks"]["off_disk_copy_fresh"])
        self.assertFalse(stale["checks"]["off_disk_copy_fresh"])
        self.assertIn("异盘副本证据已过期", stale["reasons"]["off_disk_copy_fresh"])


if __name__ == "__main__":
    unittest.main()
