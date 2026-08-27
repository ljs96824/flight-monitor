import hashlib
import tempfile
import unittest
from pathlib import Path


class BackupStatusPathContractTest(unittest.TestCase):
    def test_local_archive_cannot_self_verify_as_off_disk_copy(self):
        from backup_status import (
            OffDiskCopyMismatch,
            record_backup_created,
            record_restore_verified,
            verify_off_disk_copy,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "archive.tar.gz"
            status_path = root / "backup_status.json"
            archive.write_bytes(b"archive")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            record_backup_created(
                status_path,
                backup_id="backup-1",
                archive_sha256=digest,
            )
            record_restore_verified(
                status_path,
                backup_id="backup-1",
                archive_sha256=digest,
            )

            with self.assertRaisesRegex(OffDiskCopyMismatch, "不能是同一文件"):
                verify_off_disk_copy(
                    archive,
                    archive,
                    status_path=status_path,
                    backup_id="backup-1",
                    destination_kind="physical_disk",
                )


if __name__ == "__main__":
    unittest.main()
