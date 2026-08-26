import hashlib
import io
import json
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_runtime_backup import _empty_permission_metadata, _runtime_fixture


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sidecar(archive: Path) -> Path:
    path = Path(str(archive) + ".sha256")
    path.write_text(f"{_hash(archive)}  {archive.name}\n", encoding="ascii")
    return path


def _replace_archive_member(archive: Path, member_name: str, content: bytes) -> None:
    members = []
    with tarfile.open(archive, "r:gz", encoding="utf-8") as source:
        for member in source.getmembers():
            payload = source.extractfile(member).read() if member.isfile() else None
            members.append((member, content if member.name == member_name else payload))
    with tarfile.open(archive, "w:gz", encoding="utf-8") as target:
        for original, payload in members:
            info = tarfile.TarInfo(original.name)
            info.type = original.type
            info.mode = original.mode
            if original.isdir():
                target.addfile(info)
            else:
                info.size = len(payload)
                target.addfile(info, io.BytesIO(payload))


class RuntimeRestoreSafetyTest(unittest.TestCase):
    def _backup(self, root: Path):
        from runtime_backup import create_runtime_backup

        project, data = _runtime_fixture(root)
        result = create_runtime_backup(
            output_dir=root / "outside",
            project_root=project,
            data_root=data,
            permission_metadata_builder=_empty_permission_metadata,
        )
        return project, data, result

    def test_archive_hash_mismatch_is_rejected_before_extraction(self):
        from runtime_restore import ArchiveChecksumMismatch, restore_runtime_backup

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _project, _data, result = self._backup(root)
            archive = Path(result["archive_path"])
            archive.write_bytes(archive.read_bytes() + b"tampered")
            destination = root / "restored"

            with self.assertRaises(ArchiveChecksumMismatch):
                restore_runtime_backup(
                    archive,
                    checksum_path=result["checksum_path"],
                    destination=destination,
                )
            self.assertFalse(destination.exists())

    def test_member_hash_mismatch_is_rejected(self):
        from runtime_restore import ManifestVerificationError, restore_runtime_backup

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _project, _data, result = self._backup(root)
            archive = Path(result["archive_path"])
            with tarfile.open(archive, "r:gz", encoding="utf-8") as bundle:
                original = bundle.extractfile("state/subscriptions.json").read()
            tampered = bytes([original[0] ^ 1]) + original[1:]
            _replace_archive_member(
                archive,
                "state/subscriptions.json",
                tampered,
            )
            checksum = _sidecar(archive)

            with self.assertRaisesRegex(ManifestVerificationError, "SHA256"):
                restore_runtime_backup(
                    archive,
                    checksum_path=checksum,
                    destination=root / "restored",
                )

    def test_corrupt_sqlite_and_json_are_rejected_even_when_hashes_match(self):
        from runtime_restore import ManifestVerificationError, restore_runtime_backup

        for member_name, corrupt, expected in (
            ("core_snapshot/prices.db", b"not sqlite", "SQLite"),
            ("state/subscriptions.json", b"{broken", "JSON"),
        ):
            with self.subTest(member=member_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _project, _data, result = self._backup(root)
                archive = Path(result["archive_path"])
                with tarfile.open(archive, "r:gz", encoding="utf-8") as source:
                    manifest = json.load(source.extractfile("manifest.json"))
                item = next(entry for entry in manifest["files"] if entry["path"] == member_name)
                item["sha256"] = hashlib.sha256(corrupt).hexdigest()
                item["bytes"] = len(corrupt)
                _replace_archive_member(archive, member_name, corrupt)
                _replace_archive_member(
                    archive,
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                )
                checksum = _sidecar(archive)

                with self.assertRaisesRegex(ManifestVerificationError, expected):
                    restore_runtime_backup(
                        archive,
                        checksum_path=checksum,
                        destination=root / "restored",
                    )

    def test_path_traversal_and_link_members_are_rejected(self):
        from runtime_restore import UnsafeArchiveError, restore_runtime_backup

        for kind in ("traversal", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / "malicious.tar.gz"
                with tarfile.open(archive, "w:gz", encoding="utf-8") as bundle:
                    if kind == "traversal":
                        info = tarfile.TarInfo("../escape.txt")
                        payload = b"escape"
                        info.size = len(payload)
                        bundle.addfile(info, io.BytesIO(payload))
                    else:
                        info = tarfile.TarInfo("state/link")
                        info.type = tarfile.SYMTYPE
                        info.linkname = "outside"
                        bundle.addfile(info)
                checksum = _sidecar(archive)

                with self.assertRaises(UnsafeArchiveError):
                    restore_runtime_backup(
                        archive,
                        checksum_path=checksum,
                        destination=root / "restored",
                    )
                self.assertFalse((root / "escape.txt").exists())

    def test_member_count_and_total_size_limits_are_enforced(self):
        from runtime_restore import UnsafeArchiveError, restore_runtime_backup

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "limit.tar.gz"
            with tarfile.open(archive, "w:gz", encoding="utf-8") as bundle:
                for index in range(2):
                    payload = b"12345"
                    info = tarfile.TarInfo(f"file-{index}.txt")
                    info.size = len(payload)
                    bundle.addfile(info, io.BytesIO(payload))
            checksum = _sidecar(archive)
            with self.assertRaises(UnsafeArchiveError):
                restore_runtime_backup(
                    archive,
                    checksum_path=checksum,
                    destination=root / "count",
                    max_files=1,
                )
            with self.assertRaises(UnsafeArchiveError):
                restore_runtime_backup(
                    archive,
                    checksum_path=checksum,
                    destination=root / "size",
                    max_total_bytes=9,
                )

    def test_existing_restore_destination_is_rejected(self):
        from runtime_restore import RestoreDestinationExists, restore_runtime_backup

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _project, _data, result = self._backup(root)
            destination = root / "restored"
            destination.mkdir()
            with self.assertRaises(RestoreDestinationExists):
                restore_runtime_backup(
                    result["archive_path"],
                    checksum_path=result["checksum_path"],
                    destination=destination,
                )

    def test_valid_restore_verifies_all_files_and_does_not_change_source(self):
        from runtime_restore import restore_runtime_backup

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _project, data, result = self._backup(root)
            before = {path.name: _hash(path) for path in data.iterdir() if path.is_file()}

            restored = restore_runtime_backup(
                result["archive_path"],
                checksum_path=result["checksum_path"],
                destination=root / "restored",
            )

            after = {path.name: _hash(path) for path in data.iterdir() if path.is_file()}
            self.assertEqual(before, after)
            self.assertEqual(restored["status"], "verified")
            self.assertTrue(restored["sqlite_integrity"])
            self.assertTrue(restored["json_valid"])


class RuntimeProductionRestoreGuardTest(unittest.TestCase):
    def test_production_mapping_rejects_unclassified_source_paths(self):
        from runtime_restore import ManifestVerificationError, _production_mappings

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            restored = root / "restored"
            (restored / "diagnostics").mkdir(parents=True)
            manifest = {
                "files": [
                    {
                        "present": True,
                        "source_rel": "../outside",
                        "path": "diagnostics/placeholder",
                    }
                ]
            }
            with self.assertRaises(ManifestVerificationError):
                _production_mappings(restored, manifest, root / "data")
    def test_production_restore_requires_both_flag_and_exact_confirmation(self):
        from runtime_restore import ProductionRestoreNotConfirmed, restore_to_production

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for force, confirmation in (
                (False, "RESTORE"),
                (True, "restore"),
                (True, ""),
            ):
                with self.subTest(force=force, confirmation=confirmation):
                    with self.assertRaises(ProductionRestoreNotConfirmed):
                        restore_to_production(
                            root / "unused.tar.gz",
                            force_production=force,
                            confirmation=confirmation,
                            project_root=root / "project",
                            pre_restore_output_dir=root / "pre-backups",
                        )

    def test_switch_failure_rolls_back_original_runtime_files(self):
        from runtime_backup import create_runtime_backup
        from runtime_restore import restore_to_production

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_project, source_data = _runtime_fixture(root / "source")
            backup = create_runtime_backup(
                output_dir=root / "archives",
                project_root=source_project,
                data_root=source_data,
                permission_metadata_builder=_empty_permission_metadata,
            )
            production, production_data = _runtime_fixture(root / "production")
            original_hashes = {
                name: _hash(production_data / name)
                for name in ("prices.db", "observations.sqlite3", "subscriptions.json", "api_usage.json")
            }

            def fail_after_first(_source, _destination, index):
                if index == 1:
                    raise RuntimeError("switch failed")

            with self.assertRaisesRegex(RuntimeError, "switch failed"):
                restore_to_production(
                    backup["archive_path"],
                    checksum_path=backup["checksum_path"],
                    force_production=True,
                    confirmation="RESTORE",
                    project_root=production,
                    pre_restore_output_dir=root / "pre-restore",
                    lock_path=root / "production.lock",
                    permission_metadata_builder=_empty_permission_metadata,
                    switch_hook=fail_after_first,
                )

            self.assertEqual(
                original_hashes,
                {
                    name: _hash(production_data / name)
                    for name in original_hashes
                },
            )

    def test_switch_failure_rolls_back_while_json_locks_are_still_held(self):
        import runtime_restore
        from runtime_backup import create_runtime_backup
        from runtime_restore import restore_to_production

        active_locks = {"count": 0}
        rollback_lock_depths = []

        class Lock:
            def __enter__(self):
                active_locks["count"] += 1

            def __exit__(self, *_args):
                active_locks["count"] -= 1

        def observe_remove(path):
            rollback_lock_depths.append(active_locks["count"])
            return original_remove(path)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_project, source_data = _runtime_fixture(root / "source")
            backup = create_runtime_backup(
                output_dir=root / "archives",
                project_root=source_project,
                data_root=source_data,
                permission_metadata_builder=_empty_permission_metadata,
            )
            production, _production_data = _runtime_fixture(root / "production")
            original_remove = runtime_restore._remove_path

            def fail_after_first(_source, _destination, _index):
                raise RuntimeError("switch failed under lock")

            with (
                patch(
                    "runtime_restore.file_lock",
                    side_effect=lambda *_args, **_kwargs: Lock(),
                ),
                patch("runtime_restore._remove_path", side_effect=observe_remove),
                self.assertRaisesRegex(RuntimeError, "switch failed under lock"),
            ):
                restore_to_production(
                    backup["archive_path"],
                    checksum_path=backup["checksum_path"],
                    force_production=True,
                    confirmation="RESTORE",
                    project_root=production,
                    pre_restore_output_dir=root / "pre-restore",
                    lock_path=root / "production.lock",
                    permission_metadata_builder=_empty_permission_metadata,
                    switch_hook=fail_after_first,
                )

            self.assertTrue(rollback_lock_depths)
            self.assertTrue(all(depth > 0 for depth in rollback_lock_depths))


class RuntimeReplayTest(unittest.TestCase):
    def test_report_replay_hashes_match_after_restore(self):
        from runtime_backup import create_runtime_backup
        from runtime_restore import rehearse_runtime_backup

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, data = _runtime_fixture(root)
            source_reports = {
                "tcurve_source.txt": b"fixed tcurve report\n",
                "forecast_source.txt": b"fixed forecast report\n",
            }

            def report_builder(_snapshot, output, _route, _pair=None):
                output.mkdir(parents=True, exist_ok=True)
                result = {}
                for name, payload in source_reports.items():
                    path = output / name
                    path.write_bytes(payload)
                    result[name] = _hash(path)
                return result

            backup = create_runtime_backup(
                output_dir=root / "archives",
                project_root=project,
                data_root=data,
                replay_route="fixture-route",
                permission_metadata_builder=_empty_permission_metadata,
                report_builder=report_builder,
            )
            result = rehearse_runtime_backup(
                backup["archive_path"],
                checksum_path=backup["checksum_path"],
                route="fixture-route",
                restore_destination=root / "restored",
                report_builder=report_builder,
            )

            self.assertTrue(result["replay_match"])
            self.assertEqual(
                result["source_report_sha256"], result["restored_report_sha256"]
            )


if __name__ == "__main__":
    unittest.main()
