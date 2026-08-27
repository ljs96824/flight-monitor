import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


def _status_with_copy(root: Path):
    from backup_status import record_backup_created, record_restore_verified

    copied = root / "copies" / "runtime.tar.gz"
    copied.parent.mkdir(parents=True)
    copied.write_bytes(b"runtime backup")
    archive_hash = hashlib.sha256(copied.read_bytes()).hexdigest()
    status_path = root / "data" / "backup_status.json"
    status_path.parent.mkdir(parents=True)
    record_backup_created(
        status_path,
        backup_id="backup-device-test",
        archive_sha256=archive_hash,
    )
    record_restore_verified(
        status_path,
        backup_id="backup-device-test",
        archive_sha256=archive_hash,
        verified_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
    )
    return status_path, copied


class BackupDeviceEvidenceTest(unittest.TestCase):
    def test_platform_backend_returns_only_hashed_device_evidence_for_storage(self):
        from backup_status import _device_fingerprint, _raw_device_identifier

        with tempfile.TemporaryDirectory() as directory:
            raw = _raw_device_identifier(Path(directory))
            fingerprint = _device_fingerprint(Path(directory))

        expected_prefix = (
            "windows-volume-serial:" if os.name == "nt" else "posix-st_dev:"
        )
        self.assertTrue(raw.startswith(expected_prefix))
        self.assertEqual(len(fingerprint), 64)
        self.assertNotIn(raw, fingerprint)

    def test_same_device_different_directories_do_not_pass(self):
        from backup_status import BackupEvidenceError, verify_off_disk_copy_from_status

        with tempfile.TemporaryDirectory() as directory:
            status_path, copied = _status_with_copy(Path(directory))

            with self.assertRaises(BackupEvidenceError):
                verify_off_disk_copy_from_status(
                    copied,
                    status_path=status_path,
                    destination_kind="physical_disk",
                )

    def test_different_devices_record_hashed_identifiers(self):
        from backup_status import (
            evaluate_backup_evidence,
            verify_off_disk_copy_from_status,
        )

        with tempfile.TemporaryDirectory() as directory:
            status_path, copied = _status_with_copy(Path(directory))
            with patch(
                "backup_status._device_fingerprint",
                side_effect=["source-device-hash", "destination-device-hash"],
                create=True,
            ):
                status = verify_off_disk_copy_from_status(
                    copied,
                    status_path=status_path,
                    destination_kind="physical_disk",
                )
            evidence = evaluate_backup_evidence(
                status,
                now=datetime(2026, 8, 27, tzinfo=timezone.utc),
            )

        copy = status["off_disk_copy"]
        self.assertEqual(copy["source_device"], "source-device-hash")
        self.assertEqual(copy["destination_device"], "destination-device-hash")
        self.assertTrue(copy["different_device_verified"])
        self.assertTrue(evidence["checks"]["different_device_verified"])

    def test_encrypted_cloud_exception_requires_switch_and_trusted_root(self):
        from backup_status import BackupEvidenceError, verify_off_disk_copy_from_status

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status_path, copied = _status_with_copy(root)
            trusted_root = copied.parent
            with patch(
                "backup_status._device_fingerprint",
                return_value="same-device-hash",
                create=True,
            ):
                status = verify_off_disk_copy_from_status(
                    copied,
                    status_path=status_path,
                    destination_kind="encrypted_cloud",
                    allow_trusted_cloud_exception=True,
                    trusted_cloud_roots=[trusted_root],
                )
            self.assertTrue(
                status["off_disk_copy"]["different_device_verified"]
            )
            self.assertEqual(
                status["off_disk_copy"]["device_verification_method"],
                "trusted_encrypted_cloud_root",
            )

            with (
                patch(
                    "backup_status._device_fingerprint",
                    return_value="same-device-hash",
                ),
                self.assertRaises(BackupEvidenceError),
            ):
                verify_off_disk_copy_from_status(
                    copied,
                    status_path=status_path,
                    destination_kind="encrypted_cloud",
                    trusted_cloud_roots=[trusted_root],
                )

            other_root = root / "other-cloud"
            other_root.mkdir()
            with (
                patch(
                    "backup_status._device_fingerprint",
                    return_value="same-device-hash",
                    create=True,
                ),
                self.assertRaises(BackupEvidenceError),
            ):
                verify_off_disk_copy_from_status(
                    copied,
                    status_path=status_path,
                    destination_kind="encrypted_cloud",
                    allow_trusted_cloud_exception=True,
                    trusted_cloud_roots=[other_root],
                )

    def test_legacy_status_without_device_fields_blocks_readiness(self):
        from backup_status import evaluate_backup_evidence

        status = {
            "status_version": "backup_status_v1",
            "backup_id": "legacy",
            "archive_sha256": "a" * 64,
            "verified_restore_at": "2026-08-27T00:00:00Z",
            "off_disk_copy": {
                "verified": True,
                "verified_at": "2026-08-27T00:00:00Z",
                "destination_kind": "physical_disk",
                "copied_sha256": "a" * 64,
            },
        }

        evidence = evaluate_backup_evidence(
            status,
            now=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )

        self.assertTrue(evidence["checks"]["off_disk_copy_verified"])
        self.assertFalse(evidence["checks"]["different_device_verified"])
        self.assertIn(
            "重跑",
            evidence["reasons"]["different_device_verified"],
        )
        from research_cohort import evaluate_research_hard_gates

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
        self.assertIn("different_device_verified", hard_gate["missing"])


if __name__ == "__main__":
    unittest.main()
