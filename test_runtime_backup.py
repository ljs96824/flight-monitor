import hashlib
import json
import os
import sqlite3
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_sqlite(path: Path, marker: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE sample (id INTEGER PRIMARY KEY, marker TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO sample (marker) VALUES (?)", (marker,))
        connection.commit()
    finally:
        connection.close()


def _runtime_fixture(root: Path) -> tuple[Path, Path]:
    project = root / "project"
    data = project / "data"
    data.mkdir(parents=True)
    _write_sqlite(data / "prices.db", "prices")
    _write_sqlite(data / "observations.sqlite3", "observations")
    (data / "subscriptions.json").write_text(
        json.dumps([{"id": "fixture-subscription"}]), encoding="utf-8"
    )
    (data / "api_usage.json").write_text(
        json.dumps({"dates": {}}), encoding="utf-8"
    )
    (data / "runtime_config.yaml").write_text(
        """
source_quota_budget:
  juhe:
    packs:
      - id: pack-fixture
        added: 100
        added_at: 2026-08-01
    reconciliation:
      checked_at: 2026-08-02
      console_remaining: 90
    reserve:
      epoch_started_at: 2026-08-01T00:00:00+08:00
      target_date: 2026-10-01
RESEARCH_BASKET_ENABLED: false
RESEARCH_BASKET_STRATEGY: cohort_v2
paused_research_routes: []
subscriptions: []
""".lstrip(),
        encoding="utf-8",
    )
    return project, data


def _empty_permission_metadata(_snapshot_dir: Path) -> dict:
    return {
        "permission_quality_round_ids": [],
        "permission_quality_cells": [],
    }


class RuntimeBackupInventoryTest(unittest.TestCase):
    def test_runtime_backup_spec_is_versioned_and_has_four_tiers(self):
        from runtime_backup import RUNTIME_BACKUP_SPEC

        self.assertEqual(RUNTIME_BACKUP_SPEC["version"], "runtime_backup_v2")
        self.assertEqual(
            set(RUNTIME_BACKUP_SPEC["required_core"]),
            {
                "prices.db",
                "observations.sqlite3",
                "subscriptions.json",
                "api_usage.json",
                "runtime_config.yaml",
            },
        )
        self.assertIn("feedback.json", RUNTIME_BACKUP_SPEC["business_state"])
        self.assertIn("payloads", RUNTIME_BACKUP_SPEC["evidence"])
        self.assertIn("monitor.log", RUNTIME_BACKUP_SPEC["diagnostics"])

    def test_missing_required_file_fails_before_capture(self):
        from runtime_backup import RequiredRuntimeStateMissing, create_runtime_backup

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, data = _runtime_fixture(root)
            (data / "prices.db").unlink()
            output = root / "outside"

            with self.assertRaisesRegex(RequiredRuntimeStateMissing, "prices.db"):
                create_runtime_backup(
                    output_dir=output,
                    project_root=project,
                    data_root=data,
                    permission_metadata_builder=_empty_permission_metadata,
                )

            self.assertFalse(output.exists())

    def test_unknown_data_file_fails_in_strict_mode(self):
        from runtime_backup import UnknownRuntimePathsError, create_runtime_backup

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, data = _runtime_fixture(root)
            (data / "future_state.bin").write_bytes(b"new-state")

            with self.assertRaisesRegex(UnknownRuntimePathsError, "future_state.bin"):
                create_runtime_backup(
                    output_dir=root / "outside",
                    project_root=project,
                    data_root=data,
                    permission_metadata_builder=_empty_permission_metadata,
                )

    def test_output_directory_must_be_absolute_and_outside_project(self):
        from runtime_backup import InvalidBackupOutput, validate_output_directory

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project, data = _runtime_fixture(root)
            with self.assertRaises(InvalidBackupOutput):
                validate_output_directory(
                    Path("relative"), project_root=project, data_root=data
                )
            with self.assertRaises(InvalidBackupOutput):
                validate_output_directory(
                    project / "backups", project_root=project, data_root=data
                )
            with self.assertRaises(InvalidBackupOutput):
                validate_output_directory(
                    data / "backups", project_root=project, data_root=data
                )
            self.assertEqual(
                validate_output_directory(
                    root / "outside", project_root=project, data_root=data
                ),
                root / "outside",
            )

    def test_busy_capture_returns_exit_two_without_archive_or_manifest(self):
        from runtime_backup import create_runtime_backup

        class BusyGate:
            acquired = False
            holder = {"pid": 111, "round_id": "running-round"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, data = _runtime_fixture(root)
            output = root / "outside"
            with patch(
                "runtime_backup.acquire_collection_singleflight",
                return_value=BusyGate(),
            ):
                result = create_runtime_backup(
                    output_dir=output,
                    project_root=project,
                    data_root=data,
                    permission_metadata_builder=_empty_permission_metadata,
                )

            self.assertEqual(result["status"], "busy")
            self.assertEqual(result["exit_code"], 2)
            self.assertIsNone(result["archive_path"])
            self.assertFalse(output.exists())

    def test_acquired_gate_is_released_when_locked_inventory_refresh_fails(self):
        import runtime_backup
        from runtime_backup import UnknownRuntimePathsError, create_runtime_backup

        class Gate:
            acquired = True
            holder = {}

            def __init__(self):
                self.release_count = 0

            def release(self):
                self.release_count += 1

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, data = _runtime_fixture(root)
            initial = runtime_backup.scan_runtime_state(data)
            gate = Gate()
            with (
                patch(
                    "runtime_backup.scan_runtime_state",
                    side_effect=[initial, UnknownRuntimePathsError("late-state")],
                ),
                patch(
                    "runtime_backup.acquire_collection_singleflight",
                    return_value=gate,
                ),
                self.assertRaisesRegex(UnknownRuntimePathsError, "late-state"),
            ):
                create_runtime_backup(
                    output_dir=root / "outside",
                    project_root=project,
                    data_root=data,
                    permission_metadata_builder=_empty_permission_metadata,
                )

            self.assertEqual(gate.release_count, 1)
            self.assertFalse((root / "outside").exists())

    def test_jsonl_open_failure_is_reported_as_validation_error(self):
        from runtime_backup import RuntimeStateValidationError, _strict_jsonl

        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.jsonl"
            with self.assertRaisesRegex(
                RuntimeStateValidationError,
                r"JSONL解析失败: missing\.jsonl:0: FileNotFoundError",
            ):
                _strict_jsonl(missing)


class RuntimeBackupCaptureTest(unittest.TestCase):
    def test_archive_has_required_layout_manifest_and_optional_absent_entries(self):
        from runtime_backup import create_runtime_backup

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, data = _runtime_fixture(root)
            (data / "basket_state.json").write_text(
                json.dumps({"queues": []}), encoding="utf-8"
            )
            payloads = data / "payloads"
            payloads.mkdir()
            (payloads / "fixture.json").write_text(
                json.dumps({"status": "fixture"}), encoding="utf-8"
            )

            result = create_runtime_backup(
                output_dir=root / "outside",
                project_root=project,
                data_root=data,
                generated_at="2026-08-26T10:00:00+00:00",
                permission_metadata_builder=_empty_permission_metadata,
            )

            self.assertEqual(result["status"], "created")
            archive = Path(result["archive_path"])
            sidecar = Path(result["checksum_path"])
            self.assertTrue(archive.is_file())
            self.assertTrue(sidecar.is_file())
            self.assertEqual(sidecar.read_text(encoding="ascii").split()[0], _sha256(archive))
            if os.name != "nt":
                self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
                self.assertEqual(archive.parent.stat().st_mode & 0o777, 0o700)

            with tarfile.open(archive, "r:gz", encoding="utf-8") as bundle:
                names = set(bundle.getnames())
                self.assertTrue(
                    {
                        "core_snapshot/prices.db",
                        "core_snapshot/observations.sqlite3",
                        "core_snapshot/api_usage.json",
                        "core_snapshot/snapshot_manifest.json",
                        "state/subscriptions.json",
                        "state/basket_state.json",
                        "delivery/payloads/fixture.json",
                        "manifest.json",
                    }
                    <= names
                )
                manifest = json.load(bundle.extractfile("manifest.json"))
                core_manifest = json.load(
                    bundle.extractfile("core_snapshot/snapshot_manifest.json")
                )

            self.assertEqual(manifest["manifest_version"], "runtime_backup_manifest_v1")
            self.assertEqual(manifest["runtime_backup_spec_version"], "runtime_backup_v2")
            self.assertEqual(
                manifest["capture_consistency"],
                {
                    "collection_singleflight": True,
                    "sqlite_online_backup": True,
                    "json_locked_reads": True,
                },
            )
            self.assertEqual(core_manifest["permission_quality_cells"], [])
            feedback = next(
                item for item in manifest["files"] if item["source_rel"] == "feedback.json"
            )
            self.assertFalse(feedback["present"])
            self.assertEqual(feedback["status"], "absent")
            sqlite_entry = next(
                item
                for item in manifest["files"]
                if item["path"] == "core_snapshot/prices.db"
            )
            self.assertEqual(sqlite_entry["integrity_check"], "ok")
            self.assertEqual(sqlite_entry["table_rows"], {"sample": 1})

    def test_lock_order_finishes_before_compression(self):
        from runtime_backup import create_runtime_backup

        events = []

        class Gate:
            acquired = True
            holder = {}

            def release(self):
                events.append("release:collection")

        class Lock:
            def __init__(self, name):
                self.name = name

            def __enter__(self):
                events.append(f"enter:{self.name}")

            def __exit__(self, *_args):
                events.append(f"exit:{self.name}")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, data = _runtime_fixture(root)

            def fake_lock(path, **_kwargs):
                return Lock(Path(path).name)

            def before_archive(_staging):
                events.append("archive")

            with (
                patch("runtime_backup.acquire_collection_singleflight", return_value=Gate()),
                patch("runtime_backup.file_lock", side_effect=fake_lock),
            ):
                create_runtime_backup(
                    output_dir=root / "outside",
                    project_root=project,
                    data_root=data,
                    permission_metadata_builder=_empty_permission_metadata,
                    before_archive=before_archive,
                )

        self.assertEqual(
            [item for item in events if item.startswith("enter:")],
            [
                "enter:api_usage.json",
                "enter:subscriptions.json",
                "enter:feedback.json",
                "enter:runtime_config.yaml",
            ],
        )
        self.assertLess(events.index("release:collection"), events.index("archive"))
        self.assertLess(events.index("exit:api_usage.json"), events.index("archive"))

    def test_json_concurrent_update_is_captured_as_complete_old_or_new_document(self):
        from atomic_json_store import update_json
        from runtime_backup import create_runtime_backup
        from runtime_restore import restore_runtime_backup

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, data = _runtime_fixture(root)
            old = [{"id": "old", "payload": "a" * 2000}]
            new = [{"id": "new", "payload": "b" * 2000}]
            (data / "subscriptions.json").write_text(
                json.dumps(old), encoding="utf-8"
            )
            barrier = threading.Barrier(2)

            def writer():
                barrier.wait()
                update_json(data / "subscriptions.json", lambda _current: new)

            thread = threading.Thread(target=writer)
            thread.start()
            barrier.wait()
            result = create_runtime_backup(
                output_dir=root / "outside",
                project_root=project,
                data_root=data,
                permission_metadata_builder=_empty_permission_metadata,
            )
            thread.join(timeout=5)
            restored = restore_runtime_backup(
                result["archive_path"],
                checksum_path=result["checksum_path"],
                destination=root / "restored",
            )
            captured = json.loads(
                (Path(restored["path"]) / "state" / "subscriptions.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn(captured, (old, new))

    def test_existing_evidence_omitted_by_config_is_recorded(self):
        from runtime_backup import scan_runtime_state

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _project, data = _runtime_fixture(root)
            (data / "payloads").mkdir()
            (data / "payloads" / "fixture.json").write_text("{}", encoding="utf-8")

            inventory = scan_runtime_state(data, include_payloads=False)

            payload_entry = next(
                item for item in inventory["absent"] if item["source_rel"] == "payloads"
            )
            self.assertEqual(payload_entry["status"], "omitted_by_config")

    def test_inventory_is_refreshed_after_singleflight_is_acquired(self):
        import runtime_backup

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, data = _runtime_fixture(root)
            with patch(
                "runtime_backup.scan_runtime_state",
                wraps=runtime_backup.scan_runtime_state,
            ) as scanner:
                runtime_backup.create_runtime_backup(
                    output_dir=root / "outside",
                    project_root=project,
                    data_root=data,
                    permission_metadata_builder=_empty_permission_metadata,
                )

            self.assertEqual(scanner.call_count, 2)

    def test_sidecar_failure_removes_the_published_archive(self):
        from runtime_backup import create_runtime_backup

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, data = _runtime_fixture(root)
            with (
                patch("runtime_backup._atomic_text", side_effect=OSError("sidecar failed")),
                self.assertRaisesRegex(OSError, "sidecar failed"),
            ):
                create_runtime_backup(
                    output_dir=root / "outside",
                    project_root=project,
                    data_root=data,
                    permission_metadata_builder=_empty_permission_metadata,
                )

            self.assertEqual(list((root / "outside").glob("*.tar.gz")), [])
            self.assertEqual(list((root / "outside").glob("*.sha256")), [])
    def test_failure_before_atomic_publish_leaves_no_archive(self):
        from runtime_backup import create_runtime_backup

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, data = _runtime_fixture(root)

            def fail(_staging):
                raise RuntimeError("simulated archive failure")

            with self.assertRaisesRegex(RuntimeError, "simulated archive failure"):
                create_runtime_backup(
                    output_dir=root / "outside",
                    project_root=project,
                    data_root=data,
                    permission_metadata_builder=_empty_permission_metadata,
                    before_archive=fail,
                )

            output = root / "outside"
            self.assertEqual(list(output.glob("*.tar.gz")) if output.exists() else [], [])
            self.assertEqual(list(output.glob("*.sha256")) if output.exists() else [], [])


if __name__ == "__main__":
    unittest.main()
