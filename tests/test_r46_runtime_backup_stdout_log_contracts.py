import hashlib
import inspect
import io
import json
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import config_loader
import notifier
import runtime_backup as backup
import runtime_restore as restore
from scripts import runtime_backup as cli
from test_runtime_backup import _empty_permission_metadata, _runtime_fixture


STDOUT_LOG = "monitor_stdout.log"


class _BackupContract(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.project, self.data = _runtime_fixture(self.root)
        self.options = dict(output_dir=self.root / "archives", project_root=self.project,
                            data_root=self.data, permission_metadata_builder=_empty_permission_metadata)

    def add_stdout(self):
        (self.data / STDOUT_LOG).write_bytes(b"redirected console output\n")

    def assert_no_backup_error(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except backup.RuntimeBackupError as exc:
            self.fail(f"Unexpected RuntimeBackupError: {exc}")


class TestTrack1ExplicitClassificationContract(_BackupContract):
    def test_g01_only_explicit_stdout_exclusion(self):
        spec = backup.RUNTIME_BACKUP_SPEC
        self.assertIn(STDOUT_LOG, spec["excluded_exact"])
        self.assertEqual(backup._classify(STDOUT_LOG), "excluded")
        self.assertEqual(spec["version"], "runtime_backup_v2")
        self.assertEqual(spec["diagnostics"], ("monitor.log", "analysis_log.jsonl", "notifications_log.txt"))
        self.assertNotIn("*.log", spec["excluded_patterns"])
        self.assertIsNone(backup._classify("unclassified_daemon.log"))

    def test_g02_stdout_never_selected_or_listed_as_absent(self):
        self.add_stdout()
        (self.data / "monitor.log").write_bytes(b"diagnostic\n")
        for diagnostics in (False, True):
            inv = self.assert_no_backup_error(backup.scan_runtime_state, self.data,
                                              include_diagnostics=diagnostics, strict=True)
            selected = {item.source_rel for item in inv["selected"]}
            absent = {item["source_rel"]: item["status"] for item in inv["absent"]}
            self.assertEqual(inv["unknown"], [])
            self.assertNotIn(STDOUT_LOG, selected)
            self.assertNotIn(STDOUT_LOG, absent)
            self.assertEqual("monitor.log" in selected, diagnostics)
            if not diagnostics:
                self.assertEqual(absent["monitor.log"], "omitted_by_config")


class TestTrack2DefaultCliCreateAndRestoreContract(_BackupContract):
    def test_g03_default_create_and_verified_restore(self):
        self.add_stdout()
        for rel, content in (("payloads/fixture.json", b"{}"), ("logs/rounds/20260826.log", b"round\n")):
            path = self.data / rel
            path.parent.mkdir(parents=True)
            path.write_bytes(content)
        result = self.assert_no_backup_error(backup.create_runtime_backup, **self.options,
                                             generated_at="2026-08-26T12:00:00Z")
        archive = Path(result["archive_path"])
        self.assertTrue(archive.is_file())
        self.assertTrue(Path(result["checksum_path"]).is_file())
        with tarfile.open(archive, "r:gz", encoding="utf-8") as bundle:
            names = bundle.getnames()
            self.assertNotIn(STDOUT_LOG, {PurePosixPath(name).name for name in names})
            self.assertIn("delivery/payloads/fixture.json", names)
            self.assertIn("diagnostics/round_logs/20260826.log", names)
            entries = json.load(bundle.extractfile("manifest.json"))["files"]
            self.assertNotIn(STDOUT_LOG, {item["source_rel"] for item in entries})
            self.assertNotIn(STDOUT_LOG, {PurePosixPath(item["path"]).name for item in entries})
        verified = self.assert_no_backup_error(restore.restore_runtime_backup, archive,
                                               destination=self.root / "restored")
        self.assertEqual(verified["status"], "verified")
        self.assertIs(verified["sqlite_integrity"], True)
        self.assertIs(verified["json_valid"], True)
        self.assertEqual(verified["real_api_calls"], 0)

    def test_g04_cli_direct_create_and_verify_same_archive(self):
        self.add_stdout()
        status = self.root / "status.json"
        shared = ["--project-root", str(self.project), "--backup-status", str(status)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = cli.main(["--output-dir", str(self.options["output_dir"]), "--data-root", str(self.data), *shared])
        self.assertEqual(rc, 0, stderr.getvalue())
        created = json.loads(stdout.getvalue())
        before = json.loads(status.read_bytes())
        self.assertTrue(before["backup_id"])
        archive = Path(created["archive_path"])
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertEqual(before["archive_sha256"], digest)
        self.assertFalse(before["verified_restore_at"])
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = cli.main(["verify", "--archive", str(archive), *shared])
        self.assertEqual(rc, 0, stderr.getvalue())
        verified = json.loads(stdout.getvalue())
        after = json.loads(status.read_bytes())
        self.assertEqual(after["backup_id"], before["backup_id"])
        self.assertEqual(after["backup_id"], verified["backup_id"])
        self.assertEqual(after["archive_sha256"], digest)
        self.assertTrue(after["verified_restore_at"])

    def test_g05_pre_restore_backup_of_isolated_target(self):
        archive = backup.create_runtime_backup(**self.options)["archive_path"]
        self.add_stdout()
        result = self.assert_no_backup_error(
            restore.restore_to_production, archive, force_production=True, confirmation="RESTORE",
            project_root=self.project, pre_restore_output_dir=self.root / "pre-restore",
            permission_metadata_builder=_empty_permission_metadata,
        )
        self.assertEqual(result["status"], "restored_to_production")
        self.assertTrue(result["pre_restore_backup_id"])
        self.assertEqual(result["real_api_calls"], 0)
        self.assertEqual((self.data / STDOUT_LOG).read_bytes(), b"redirected console output\n")


class TestTrack3StrictUnknownGuardPreserved(_BackupContract):
    def test_g06_default_strict_only_names_real_unknowns(self):
        for fn in (backup.scan_runtime_state, backup.create_runtime_backup):
            self.assertIs(inspect.signature(fn).parameters["strict"].default, True)
        self.assertEqual(backup._classify(STDOUT_LOG), "excluded")
        self.add_stdout()
        for other in ("unclassified_daemon.log", "unexpected_state.json"):
            path = self.data / other
            path.write_bytes(b"{}")
            for fn, kwargs in ((backup.scan_runtime_state, {"data_root": self.data}),
                               (backup.create_runtime_backup, self.options)):
                with self.assertRaises(backup.UnknownRuntimePathsError) as caught:
                    fn(**kwargs)
                self.assertEqual(str(caught.exception).rsplit(": ", 1)[-1], other)
            path.unlink()

    def test_g07_unknown_log_rejected_with_diagnostics_disabled(self):
        (self.data / "rogue.log").write_bytes(b"log\n")
        with self.assertRaisesRegex(backup.UnknownRuntimePathsError, r"rogue\.log"):
            backup.create_runtime_backup(**self.options, include_diagnostics=False)


class TestTrack4FrozenCarrierAndContractGuards(_BackupContract):
    def test_g08_restore_mapping_and_frozen_carriers(self):
        with self.assertRaises(restore.ManifestVerificationError):
            restore._top_source_root(STDOUT_LOG)
        fixture = Path(__file__).parent / "fixtures/frozen_email/economy_payload.json"
        payload = json.loads(fixture.read_bytes())
        with patch.object(config_loader, "RUNTIME_CONFIG_PATH", self.root / "absent.yaml"), redirect_stdout(io.StringIO()):
            email = notifier.render_email(deepcopy(payload))[1]
            push = notifier.prepare_push_render(notifier.render_pushplus_sections(deepcopy(payload))).content
            detail = notifier.render_detail_html(deepcopy(payload))
        for content, size, digest in (
            (email, 53307, "1dd353d2b7bcafc02087957ccccb6661478e20472a22fb3e15dae6a56473ee6b"),
            (push, 5552, "2ac0e80d9f82dbe7ebccf524c0832f09d42d916212b6178316b0b7bcd60574ff"),
            (detail, 54248, "e31b8728b8d0bc05c9bbd4bd99c299aaf6dca7b66674b6c560ec089b1681ffa6"),
        ):
            raw = content.encode("utf-8")
            self.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()), (size, digest))
