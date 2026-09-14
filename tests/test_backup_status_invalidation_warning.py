"""Offline contracts for create's evidence reset and operator warning."""
import ast
import contextlib
import copy
from datetime import datetime, timezone
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)
STAMP = "2026-09-14T00:00:00Z"
EMPTY_COPY = {
    "verified": False, "verified_at": None, "destination_kind": None,
    "copied_sha256": None, "source_device": None, "destination_device": None,
    "different_device_verified": False, "device_verification_method": None,
    "trusted_cloud_root_verified": False,
}
WARNING = "[运行备份提示]"


class BackupStatusInvalidationWarningTest(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(contextlib.ExitStack())
        stack.enter_context(patch.dict(os.environ, {
            "NO_LIVE_API": "1", "PYTHON_DOTENV_DISABLED": "1",
        }))
        stack.enter_context(patch("dotenv.load_dotenv", return_value=False))
        self.network = [
            stack.enter_context(patch(target, side_effect=AssertionError("NETWORK_FORBIDDEN")))
            for target in ("socket.create_connection", "socket.socket.connect",
                           "socket.getaddrinfo")
        ]
        import backup_status
        from scripts import runtime_backup
        self.status = backup_status
        self.cli = runtime_backup
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.project = self.root / "project"
        self.data = self.project / "data"
        self.data.mkdir(parents=True)
        self.default = self.data / "backup_status.json"
        self.explicit = self.root / "audit" / "backup_status.json"
        self.explicit.parent.mkdir()
        self.archive_bytes = b"synthetic archive bytes, not a real backup"
        self.sha = hashlib.sha256(self.archive_bytes).hexdigest()
        self.created = {
            "status": "created", "exit_code": 0, "backup_id": "synthetic-B",
            "archive_path": str(self.root / "output" / "synthetic.tar.gz"),
            "archive_sha256": self.sha, "file_count": 3, "total_bytes": 42,
            "sqlite_integrity": {"synthetic.sqlite3": "ok"}, "json_valid": True,
        }

    def tearDown(self):
        for outbound in self.network:
            outbound.assert_not_called()

    def _ready(self, path, backup_id="synthetic-A", sha="a" * 64):
        value = {
            "status_version": "backup_status_v2", "backup_id": backup_id,
            "archive_sha256": sha, "verified_restore_at": STAMP,
            "off_disk_copy": {
                "verified": True, "verified_at": STAMP,
                "destination_kind": "external_path", "copied_sha256": sha,
                "source_device": "synthetic-source", "destination_device": "synthetic-copy",
                "different_device_verified": True,
                "device_verification_method": "device_identifier",
                "trusted_cloud_root_verified": False,
            },
        }
        path.write_text(json.dumps(value), encoding="utf-8")
        return value

    def _assert_reset(self, record_created=None):
        self._ready(self.explicit)
        function = record_created or self.status.record_backup_created
        value = function(self.explicit, backup_id="synthetic-B", archive_sha256=self.sha)
        self.assertIsNone(value["verified_restore_at"], "RESTORE_PROOF_NOT_RESET")
        self.assertEqual(value["off_disk_copy"], EMPTY_COPY, "OFF_DISK_PROOF_NOT_RESET")
        self.assertEqual(value, self.status.load_backup_status(self.explicit))
        self.assertEqual((value["backup_id"], value["archive_sha256"]),
                         ("synthetic-B", self.sha))
        return value

    def test_create_resets_both_proofs(self):
        self._assert_reset()

    def test_copy_only_does_not_restore_restore_proof(self):
        self._assert_reset()
        copied = self.root / "copy.tar.gz"
        copied.write_bytes(self.archive_bytes)
        with patch.object(self.status, "_device_fingerprint",
                          side_effect=["synthetic-source", "synthetic-copy"]):
            value = self.status.verify_off_disk_copy_from_status(
                copied, status_path=self.explicit, verified_at=NOW)
        self.assertIsNone(value["verified_restore_at"])
        self.assertTrue(value["off_disk_copy"]["verified"])
        evidence = self.status.evaluate_backup_evidence(value, now=NOW)
        self.assertFalse(evidence["checks"]["backup_restore_verified"])
        for name in ("off_disk_copy_verified", "different_device_verified", "off_disk_copy_fresh"):
            self.assertTrue(evidence["checks"][name], name)

    def test_same_archive_restore_preserves_copy(self):
        before = self._ready(self.explicit)
        value = self.status.record_restore_verified(
            self.explicit, backup_id="synthetic-A", archive_sha256="a" * 64,
            verified_at=NOW)
        self.assertEqual(value["off_disk_copy"], before["off_disk_copy"])
        self.assertEqual(value["verified_restore_at"], STAMP)

    def test_changed_id_or_sha_restore_resets_copy(self):
        for name, backup_id, sha in (
            ("id_only", "synthetic-B", "a" * 64),
            ("sha_only", "synthetic-A", "b" * 64),
            ("both", "synthetic-B", "b" * 64),
        ):
            with self.subTest(identity_change=name):
                self._ready(self.explicit)
                value = self.status.record_restore_verified(
                    self.explicit, backup_id=backup_id, archive_sha256=sha,
                    verified_at=NOW)
                self.assertEqual(value["off_disk_copy"], EMPTY_COPY)
                self.assertEqual(value["verified_restore_at"], STAMP)
                self.assertEqual((value["backup_id"], value["archive_sha256"]), (backup_id, sha))

    def _invoke(self, *, direct=False, status="created", failure=None,
                main=None, explicit=True, include_data=True, invalid=False):
        self._ready(self.default)
        self._ready(self.explicit)
        before = self.default.read_bytes()
        calls = []
        result = copy.deepcopy(self.created)
        if status == "busy":
            result = {"status": "busy", "exit_code": 2, "backup_id": "synthetic-busy"}
        elif status != "created":
            result.update(status=status, exit_code=1)

        def create(**kwargs):
            calls.append(kwargs)
            # Guard even mutated wiring before the real atomic status writer runs.
            target = Path(kwargs["status_path"]).resolve()
            self.assertTrue(target.is_relative_to(self.root.resolve()), "NON_TEMP_STATUS_TARGET")
            self.assertEqual(Path(kwargs["project_root"]), self.project)
            self.assertIn(kwargs["data_root"], (None, str(self.data)))
            if failure:
                raise failure
            if status == "created":
                self.status.record_backup_created(
                    target, backup_id=result["backup_id"], archive_sha256=result["archive_sha256"])
            return result

        args = ([] if direct else ["create"]) + [
            "--project-root", str(self.project), "--output-dir", str(self.root / "output"),
        ]
        if include_data:
            args += ["--data-root", str(self.data)]
        if explicit:
            args += ["--backup-status", str(self.explicit)]
        if invalid:
            args += ["--unknown-option"]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(self.cli, "create_runtime_backup", side_effect=create), \
                (patch.dict(main.__globals__, {"create_runtime_backup": create})
                 if main is not None else contextlib.nullcontext()), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = (main or self.cli.main)(args)
            except SystemExit as exc:
                if not invalid:
                    raise
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue(), calls, before

    def _expected_summary(self, direct, status="created"):
        if status == "busy":
            result = {"status": "busy", "backup_id": "synthetic-busy",
                      "production_state_changed": False, "real_api_calls": 0}
            return {"operation": "create", "passed": False, **result} if direct else result
        if direct:
            return {
                "operation": "create", "passed": status == "created",
                "archive_path": self.created["archive_path"], "archive_sha256": self.sha,
                "file_count": 3, "total_bytes": 42,
                "sqlite_integrity": {"synthetic.sqlite3": "ok"}, "json_valid": True,
                "status_fields_written": ["backup_id", "archive_sha256",
                                          "verified_restore_at", "off_disk_copy"],
                "real_api_calls": 0,
            }
        return {
            "status": status, "backup_id": "synthetic-B", "archive_sha256": self.sha,
            "file_count": 3, "total_bytes": 42,
            "sqlite_integrity": {"synthetic.sqlite3": "ok"}, "json_valid": True,
            "replay_sha256": {}, "production_state_changed": False, "real_api_calls": 0,
        }

    def _assert_stdout(self, stdout, direct, status="created"):
        expected = self._expected_summary(direct, status)
        self.assertEqual(json.loads(stdout), expected)
        self.assertEqual(stdout, json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    def _assert_warning(self, direct, main=None):
        code, stdout, stderr, calls, _ = self._invoke(direct=direct, main=main)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self._assert_stdout(stdout, direct)
        self.assertIn(WARNING, stderr, "CREATE_WARNING_MISSING")
        for meaning in ("所选状态文件", "重新建立归档证据", "旧恢复证明", "异盘副本证明",
                        "不再沿用", "若研究任务读取该状态文件", "补证完成前", "拒绝研究准入",
                        "同一归档", "同一 --backup-status", "隔离恢复", "--verify-off-disk",
                        "再检查研究就绪"):
            self.assertIn(meaning, stderr)
        self.assertEqual(stderr.count(WARNING), 1)

    def test_created_subcommand_warns_without_changing_stdout(self):
        self._assert_warning(False)

    def test_created_direct_warns_without_changing_stdout(self):
        self._assert_warning(True)

    def test_noncreated_paths_do_not_warn(self):
        for direct in (False, True):
            for status in ("busy", "failed"):
                with self.subTest(direct=direct, status=status):
                    code, stdout, stderr, calls, before = self._invoke(direct=direct, status=status)
                    self.assertEqual(code, 2 if status == "busy" else 1)
                    self._assert_stdout(stdout, direct, status)
                    self.assertEqual(stderr, "")
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(self.default.read_bytes(), before)
            for invalid, failure in ((True, None), (False, OSError("synthetic create failure"))):
                with self.subTest(direct=direct, invalid=invalid):
                    code, stdout, stderr, calls, before = self._invoke(
                        direct=direct, invalid=invalid, failure=failure)
                    self.assertEqual(code, 2 if invalid else 1)
                    self.assertEqual(stdout, "")
                    self.assertNotIn(WARNING, stderr)
                    self.assertIn("unrecognized arguments" if invalid else "synthetic create failure", stderr)
                    self.assertEqual(len(calls), 0 if invalid else 1)
                    self.assertEqual(self.default.read_bytes(), before)

    def _assert_isolated(self, main=None, direct=False):
        code, _, _, calls, before = self._invoke(main=main, direct=direct)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(Path(calls[0]["status_path"]), self.explicit, "STATUS_PATH_NOT_FORWARDED")
        self.assertEqual(self.default.read_bytes(), before, "DEFAULT_STATUS_WAS_CHANGED")
        value = self.status.load_backup_status(self.explicit)
        self.assertIsNone(value["verified_restore_at"])
        self.assertEqual(value["off_disk_copy"], EMPTY_COPY)
        self.assertEqual(value["backup_id"], "synthetic-B")

    def test_explicit_status_is_used_and_default_is_untouched(self):
        for direct in (False, True):
            with self.subTest(direct=direct):
                self._assert_isolated(direct=direct)

    def test_existing_status_path_precedence(self):
        self.data = self.root / "override-data"
        self.data.mkdir()
        for explicit, include_data, target in (
            (True, True, self.explicit), (True, False, self.explicit),
            (False, True, self.data / "backup_status.json"), (False, False, self.default),
        ):
            with self.subTest(explicit=explicit, data_root=include_data):
                code, _, _, calls, _ = self._invoke(explicit=explicit, include_data=include_data)
                self.assertEqual(code, 0)
                self.assertEqual(Path(calls[0]["status_path"]), target if explicit else target.resolve())
                self.assertEqual(self.status.load_backup_status(target)["backup_id"], "synthetic-B")

    def _mutated(self, kind):
        module = self.status if kind == "preserve_copy" else self.cli
        names = {"record_backup_created"} if kind == "preserve_copy" else {"main", "_create_kwargs"}
        parsed = ast.parse(inspect.getsource(module))
        tree = ast.Module(body=[node for node in parsed.body
                               if isinstance(node, ast.FunctionDef) and node.name in names],
                          type_ignores=[])
        before = ast.dump(tree)
        matches = 0
        for function in tree.body:
            if kind == "remove_warning" and function.name == "main":
                for node in ast.walk(function):
                    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                        continue
                    call = node.value
                    if (isinstance(call.func, ast.Name) and call.func.id == "print"
                            and call.args and isinstance(call.args[0], ast.Constant)
                            and isinstance(call.args[0].value, str)
                            and call.args[0].value.startswith(WARNING)):
                        node.value = ast.Constant(value=None)
                        matches += 1
            elif kind == "ignore_override" and function.name == "_create_kwargs":
                for node in ast.walk(function):
                    if isinstance(node, ast.Dict):
                        for index, key in enumerate(node.keys):
                            if isinstance(key, ast.Constant) and key.value == "status_path":
                                node.values[index] = ast.parse(
                                    'Path(args.data_root).resolve() / "backup_status.json"',
                                    mode="eval").body
                                matches += 1
            elif kind == "preserve_copy" and function.name == "record_backup_created":
                for node in ast.walk(function):
                    if isinstance(node, ast.Lambda):
                        node.body = ast.Dict(
                            keys=[None, ast.Constant(value="off_disk_copy")],
                            values=[node.body, ast.parse('_current["off_disk_copy"]', mode="eval").body])
                        matches += 1
        self.assertEqual(matches, 1, "MUTATION_NODE_COUNT")
        self.assertNotEqual(ast.dump(tree), before, "MUTATION_WAS_NOOP")
        ast.fix_missing_locations(tree)
        code = compile(tree, "<synthetic-backup-mutation>", "exec")
        namespace = dict(vars(module))
        exec(code, namespace)
        return namespace["record_backup_created" if kind == "preserve_copy" else "main"]

    def test_mutation_missing_warning_is_rejected(self):
        mutant = self._mutated("remove_warning")
        for direct in (False, True):
            with self.subTest(direct=direct):
                with self.assertRaisesRegex(AssertionError, "CREATE_WARNING_MISSING"):
                    self._assert_warning(direct, main=mutant)

    def test_mutation_ignored_status_override_is_rejected(self):
        mutant = self._mutated("ignore_override")
        with self.assertRaisesRegex(AssertionError, "STATUS_PATH_NOT_FORWARDED"):
            self._assert_isolated(main=mutant)

    def test_mutation_preserved_old_copy_is_rejected(self):
        mutant = self._mutated("preserve_copy")
        with self.assertRaisesRegex(AssertionError, "OFF_DISK_PROOF_NOT_RESET"):
            self._assert_reset(record_created=mutant)


if __name__ == "__main__":
    unittest.main()
