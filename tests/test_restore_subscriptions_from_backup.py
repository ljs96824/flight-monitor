"""R9-3：从运行备份只回填 data/subscriptions.json 的薄封装与规程。

基线(R9-2 合并后的 main)：restore_runtime_backup() 能把归档校验后隔离恢复到新目录，只有传入 status_path 才写
backup_status.json；但两个命令入口(scripts/runtime_restore.py --archive、scripts/runtime_backup.py restore)默认都会
写 data/backup_status.json；restore_to_production() 按 manifest 切换全部状态，没有单文件选项。订阅文件丢失后，
没有任何入口能"只把它写回去"。
口径(R9 定案 Q3)：scripts/restore_subscriptions_from_backup.py 默认只读预览——隔离恢复到临时目录、完整核验、
列出备份身份与订阅成员(只含编号、状态、航线与日期)和订阅文件 SHA-256，随后删除临时目录，不写 backup_status.json。
回填须同时给出 --apply、--confirm RESTORE-SUBSCRIPTIONS 与 --expect-sha256：持有采集单飞锁，在订阅文件锁内确认
目标仍不存在，再把备份中的原始字节原子发布；目标已存在(含被其他入口重建)、确认串或哈希不符、锁被占用一律拒绝
(退出码 2)，归档损坏或订阅内容不是数组为失败(退出码 1)，已写入但读回核对未通过为退出码 3。不启动采集，不写其他文件。
预览按各采集入口的规则标出会被当作 active 的订阅(缺 status 也算)；隔离副本删不掉时报出路径。全部用临时目录里现做的合成备份，
不读取任何真实备份。
"""

import ast
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import date
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from collection_singleflight import acquire_collection_singleflight
from local_file_lock import FileLockTimeout
from runtime_backup import create_runtime_backup
from scripts import restore_subscriptions_from_backup as cli


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "restore_subscriptions_from_backup.py"
DOC = ROOT / "docs" / "subscriptions-single-file-restore.md"
TODAY = date(2026, 10, 19)
SUBSCRIPTIONS = (
    "[\r\n"
    '  {"id": "sub-active", "status": "active", "origin": "上海", "destination": "大阪", '
    '"depart_date": "2026-12-01", "notification_goals": {"method": "both", "email": "owner@example.com"}, '
    '"max_budget": 3210.75},\r\n'
    '  {"id": "sub-paused", "status": "paused", "basic": {"origin": "上海", "destination": "东京", '
    '"departure_date": "2026-12-05", "return_date": "2026-12-09"}},\r\n'
    '  {"id": "sub-expired", "status": "active", "origin": "上海", "destination": "札幌", '
    '"depart_date": "2026-01-02"},\r\n'
    '  {"status": "active", "origin": "广州", "destination": "首尔", "depart_date": "2026-12-20"},\r\n'
    '  {"id": "sub-legacy", "origin": "上海", "destination": "福冈", "depart_date": "2026-12-10"},\r\n'
    "  7\r\n"
    "]"
).encode("utf-8")
SUBSCRIPTIONS_SHA256 = hashlib.sha256(SUBSCRIPTIONS).hexdigest()
RUNTIME_CONFIG = """source_quota_budget:
  juhe:
    packs:
      - {id: p1, added: 100, added_at: '2026-01-01'}
    reconciliation: {checked_at: '2026-01-01', console_remaining: 10}
    reserve: {epoch_started_at: '2026-01-01T00:00:00', target_date: '2026-12-31'}
RESEARCH_BASKET_ENABLED: false
RESEARCH_BASKET_STRATEGY: cohort_v2
paused_research_routes: []
subscriptions: []
"""


def make_backup(root: Path, name: str, subscriptions: bytes) -> dict:
    data = root / name / "data"
    data.mkdir(parents=True)
    (data / "api_usage.json").write_text("{}", encoding="utf-8")
    (data / "subscriptions.json").write_bytes(subscriptions)
    (data / "runtime_config.yaml").write_text(RUNTIME_CONFIG, encoding="utf-8")
    for database in ("prices.db", "observations.sqlite3"):
        connection = sqlite3.connect(data / database)
        try:
            connection.execute("CREATE TABLE t (x INTEGER)")
            connection.commit()
        finally:
            connection.close()
    with patch("runtime_backup._git_commit", return_value="synthetic"):
        return create_runtime_backup(
            output_dir=root / f"{name}-backups",
            project_root=root / name,
            data_root=data,
            include_payloads=False,
            generated_at="2026-10-01T00:00:00Z",
            permission_metadata_builder=lambda _snapshot: {},
            lock_path=root / f"{name}-backup.lock",
        )


def tree(path: Path) -> dict:
    return {
        item.relative_to(path).as_posix(): item.read_bytes() if item.is_file() else None
        for item in sorted(path.rglob("*"))
    }


class SubscriptionRestoreTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._fixtures = tempfile.TemporaryDirectory()
        fixtures = Path(cls._fixtures.name)
        cls.backup = make_backup(fixtures, "source", SUBSCRIPTIONS)
        cls.object_backup = make_backup(fixtures, "object-source", b'{"not": "an array"}')

    @classmethod
    def tearDownClass(cls):
        cls._fixtures.cleanup()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.data = self.project / "data"
        self.data.mkdir(parents=True)
        (self.data / "backup_status.json").write_bytes(b'{"sentinel": true}\n')
        (self.data / "prices.db").write_bytes(b"SENTINEL-DB")
        self.target = self.data / "subscriptions.json"
        self.lock_path = self.root / "collection_singleflight.lock"

    def run_cli(self, *arguments, archive=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            self.record = stack.enter_context(patch("runtime_restore.record_restore_verified"))
            self.lock_resolver = stack.enter_context(
                patch.object(cli, "resolve_collection_lock_path", return_value=self.lock_path)
            )
            self.acquire = stack.enter_context(
                patch.object(
                    cli,
                    "acquire_collection_singleflight",
                    wraps=acquire_collection_singleflight,
                )
            )
            self.restore = stack.enter_context(
                patch.object(cli, "restore_runtime_backup", wraps=cli.restore_runtime_backup)
            )
            stack.enter_context(patch.object(cli, "shanghai_today", return_value=TODAY))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            code = cli.main(
                [
                    "--archive",
                    str(archive or self.backup["archive_path"]),
                    "--project-root",
                    str(self.project),
                    *arguments,
                ]
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def apply_arguments(self, sha256=SUBSCRIPTIONS_SHA256, confirmation=cli.CONFIRMATION_WORD):
        return ("--apply", "--confirm", confirmation, "--expect-sha256", sha256)

    def assert_gate_free(self):
        gate = acquire_collection_singleflight(
            "probe",
            lock_path=self.lock_path,
            heartbeat_interval_seconds=0,
        )
        try:
            self.assertTrue(gate.acquired)
        finally:
            gate.release()

    def assert_no_temporary_files(self):
        self.assertEqual([path.name for path in self.data.glob("*.tmp")], [])


class PreviewTest(SubscriptionRestoreTestCase):
    def test_preview_lists_backup_members_and_changes_nothing(self):
        before = tree(self.project)
        code, stdout, stderr = self.run_cli()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(tree(self.project), before)
        result = json.loads(stdout)
        self.assertEqual(result["operation"], "preview")
        self.assertEqual(result["backup_id"], self.backup["backup_id"])
        self.assertEqual(result["backup_created_at_utc"], "2026-10-01T00:00:00Z")
        self.assertEqual(result["archive_sha256"], self.backup["archive_sha256"])
        self.assertEqual(result["archive_member"], "state/subscriptions.json")
        self.assertEqual(result["subscriptions_sha256"], SUBSCRIPTIONS_SHA256)
        self.assertEqual(result["subscriptions_bytes"], len(SUBSCRIPTIONS))
        self.assertEqual(result["subscription_count"], 6)
        self.assertEqual(result["active_count"], 4)
        self.assertEqual(result["active_ids"], ["sub-active", "sub-expired", "sub-legacy"])
        self.assertEqual(result["status_counts"], {"(missing)": 1, "active": 3, "paused": 1})
        self.assertEqual(
            result["needs_attention"],
            [
                {"index": 3, "reason": "missing_id"},
                {"index": 4, "reason": "status_nonstandard"},
                {"index": 5, "reason": "not_object"},
            ],
        )
        members = {item["index"]: item for item in result["subscriptions"]}
        self.assertEqual(members[0]["preflight_skip"], "")
        self.assertEqual(members[0]["collected_by"], ["main", "basket_collect"])
        self.assertEqual(members[1]["collected_by"], [])
        self.assertEqual(members[2]["preflight_skip"], "expired")
        self.assertEqual((members[4]["status"], members[4]["collected_by"]), ("(missing)", ["main", "basket_collect"]))
        self.assertIsNone(result["isolated_copy_left"])
        self.assertEqual(
            {key: members[1][key] for key in ("id", "status", "origin", "destination", "depart_date", "return_date")},
            {
                "id": "sub-paused",
                "status": "paused",
                "origin": "上海",
                "destination": "东京",
                "depart_date": "2026-12-05",
                "return_date": "2026-12-09",
            },
        )
        self.assertFalse(result["target_exists"])
        self.assertFalse(result["production_state_changed"])
        self.assertFalse(result["backup_status_written"])
        self.assertFalse(result["collection_started"])
        self.assertEqual(result["real_api_calls"], 0)
        self.assertIn(f"--expect-sha256 {SUBSCRIPTIONS_SHA256}", result["apply_arguments"])
        self.assertNotIn("owner@example.com", stdout)
        # G19：预算测试值带小数点，归档摘要、备份编号与临时目录名里都不会出现，旧值 3210 曾与 archive_sha256 偶然重合。
        self.assertNotIn("3210.75", stdout)
        self.record.assert_not_called()
        self.lock_resolver.assert_not_called()
        self.acquire.assert_not_called()

    def test_preview_removes_its_isolated_restore_directory(self):
        created = []
        real_mkdtemp = tempfile.mkdtemp

        def tracking(*args, **kwargs):
            directory = real_mkdtemp(*args, **kwargs)
            created.append(Path(directory))
            return directory

        with patch.object(cli.tempfile, "mkdtemp", side_effect=tracking):
            code, _stdout, stderr = self.run_cli()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].exists())

    def test_undeletable_isolated_copy_is_reported_not_hidden(self):
        created = []
        real_mkdtemp = tempfile.mkdtemp

        def tracking(*args, **kwargs):
            directory = real_mkdtemp(*args, **kwargs)
            created.append(Path(directory))
            return directory

        try:
            with patch.object(cli.tempfile, "mkdtemp", side_effect=tracking), \
                    patch.object(cli.shutil, "rmtree") as rmtree, \
                    patch.object(cli.time, "sleep"):
                code, stdout, stderr = self.run_cli()
            self.assertEqual(code, 0, stderr)
            self.assertEqual(rmtree.call_count, cli.ISOLATED_CLEANUP_ATTEMPTS)
            self.assertEqual(json.loads(stdout)["isolated_copy_left"], str(created[0]))
            self.assertIn(str(created[0]), stderr)
            self.assertIn("请手动删除", stderr)
        finally:
            for directory in created:
                shutil.rmtree(directory, ignore_errors=True)

    def test_collection_status_rules_follow_each_entrypoint(self):
        cases = (
            ({}, ["main", "basket_collect"]),
            ({"status": "active"}, ["main", "basket_collect"]),
            ({"status": "Active"}, ["basket_collect"]),
            ({"status": "enabled"}, ["basket_collect"]),
            ({"status": ""}, ["basket_collect"]),
            ({"status": "paused"}, []),
        )
        for subscription, expected in cases:
            with self.subTest(subscription=subscription):
                self.assertEqual(cli._collected_by(subscription), expected)

    def test_preview_reports_existing_target_without_touching_it(self):
        self.target.write_bytes(b"[]")
        code, stdout, stderr = self.run_cli()
        self.assertEqual(code, 0, stderr)
        self.assertTrue(json.loads(stdout)["target_exists"])
        self.assertEqual(self.target.read_bytes(), b"[]")

    def test_confirmation_without_apply_is_refused(self):
        code, stdout, stderr = self.run_cli("--confirm", cli.CONFIRMATION_WORD)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("reason=apply_flag_missing", stderr)
        self.assertFalse(self.target.exists())
        self.restore.assert_not_called()


class ApplyTest(SubscriptionRestoreTestCase):
    def test_apply_publishes_backup_bytes_exactly_and_nothing_else(self):
        before = tree(self.project)
        code, stdout, stderr = self.run_cli(*self.apply_arguments())
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.target.read_bytes(), SUBSCRIPTIONS)
        after = tree(self.project)
        self.assertEqual(
            sorted(set(after) - set(before)),
            ["data/subscriptions.json", "data/subscriptions.json.lock"],
        )
        self.assertEqual({key: after[key] for key in before}, before)
        self.assert_no_temporary_files()
        result = json.loads(stdout)
        self.assertEqual(result["operation"], "apply")
        self.assertTrue(result["published"])
        self.assertEqual(result["published_sha256"], SUBSCRIPTIONS_SHA256)
        self.assertTrue(result["production_state_changed"])
        self.assertFalse(result["backup_status_written"])
        self.assertFalse(result["collection_started"])
        self.assertEqual(result["active_ids"], ["sub-active", "sub-expired", "sub-legacy"])
        self.record.assert_not_called()
        self.lock_resolver.assert_called_once_with(base_dir=self.project.resolve())
        self.assertEqual(self.acquire.call_count, 1)
        self.assert_gate_free()

    def test_existing_target_is_refused_before_any_restore_work(self):
        self.target.write_bytes(b"[]")
        code, stdout, stderr = self.run_cli(*self.apply_arguments())
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("reason=target_exists", stderr)
        self.assertEqual(self.target.read_bytes(), b"[]")
        self.restore.assert_not_called()
        self.acquire.assert_not_called()

    def test_target_recreated_by_another_entry_is_never_overwritten(self):
        recreated = b'[{"id": "web-new", "status": "active"}]'
        real_inspect = cli.inspect_backup_subscriptions

        def inspect_then_recreate(*args, **kwargs):
            result = real_inspect(*args, **kwargs)
            self.target.write_bytes(recreated)
            return result

        with patch.object(cli, "inspect_backup_subscriptions", side_effect=inspect_then_recreate):
            code, stdout, stderr = self.run_cli(*self.apply_arguments())
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("reason=target_exists", stderr)
        self.assertEqual(self.target.read_bytes(), recreated)
        self.assert_no_temporary_files()
        self.assert_gate_free()

    def test_sha256_mismatch_is_refused(self):
        code, _stdout, stderr = self.run_cli(*self.apply_arguments(sha256="0" * 64))
        self.assertEqual(code, 2)
        self.assertIn("reason=sha256_mismatch", stderr)
        self.assertFalse(self.target.exists())
        self.assert_gate_free()

    def test_wrong_confirmation_or_malformed_hash_is_refused_before_locking(self):
        cases = (
            (self.apply_arguments(confirmation="RESTORE"), "reason=confirmation_mismatch"),
            (self.apply_arguments(sha256="abc"), "reason=expected_sha256_invalid"),
        )
        for arguments, reason in cases:
            with self.subTest(reason=reason):
                code, _stdout, stderr = self.run_cli(*arguments)
                self.assertEqual(code, 2)
                self.assertIn(reason, stderr)
                self.assertFalse(self.target.exists())
                self.acquire.assert_not_called()
                self.restore.assert_not_called()

    def test_busy_collection_lock_is_refused(self):
        holder = acquire_collection_singleflight(
            "running_round",
            lock_path=self.lock_path,
            heartbeat_interval_seconds=0,
        )
        try:
            self.assertTrue(holder.acquired)
            code, _stdout, stderr = self.run_cli(*self.apply_arguments())
        finally:
            holder.release()
        self.assertEqual(code, 2)
        self.assertIn("reason=collection_busy", stderr)
        self.assertFalse(self.target.exists())
        self.restore.assert_not_called()

    def test_failed_read_back_is_reported_as_written_but_unverified(self):
        with patch.object(cli, "_read_back", side_effect=PermissionError("sharing violation")):
            code, stdout, stderr = self.run_cli(*self.apply_arguments())
        self.assertEqual(code, 3)
        self.assertEqual(stdout, "")
        self.assertIn("SubscriptionPublishedUnverified", stderr)
        self.assertEqual(self.target.read_bytes(), SUBSCRIPTIONS)
        self.assert_no_temporary_files()
        self.assert_gate_free()

    def test_busy_subscriptions_lock_is_refused(self):
        with patch.object(cli, "file_lock", side_effect=FileLockTimeout("busy")):
            code, _stdout, stderr = self.run_cli(*self.apply_arguments())
        self.assertEqual(code, 2)
        self.assertIn("reason=subscriptions_lock_busy", stderr)
        self.assertFalse(self.target.exists())
        self.assert_gate_free()


class SourceValidationTest(SubscriptionRestoreTestCase):
    def corrupted_archive(self) -> Path:
        source = Path(self.backup["archive_path"])
        corrupted = self.root / "corrupted.tar.gz"
        data = bytearray(source.read_bytes())
        data[-1] ^= 0xFF
        corrupted.write_bytes(bytes(data))
        shutil.copyfile(f"{source}.sha256", f"{corrupted}.sha256")
        return corrupted

    def test_corrupted_archive_fails_without_any_write(self):
        archive = self.corrupted_archive()
        before = tree(self.project)
        for arguments in ((), self.apply_arguments()):
            with self.subTest(apply=bool(arguments)):
                code, stdout, stderr = self.run_cli(*arguments, archive=archive)
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("ArchiveChecksumMismatch", stderr)
                self.assertEqual(tree(self.project), before)
                self.record.assert_not_called()
        self.assert_gate_free()

    def test_backup_whose_subscriptions_are_not_an_array_fails(self):
        archive = self.object_backup["archive_path"]
        sha256 = hashlib.sha256(b'{"not": "an array"}').hexdigest()
        for arguments in ((), self.apply_arguments(sha256=sha256)):
            with self.subTest(apply=bool(arguments)):
                code, stdout, stderr = self.run_cli(*arguments, archive=archive)
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("SubscriptionBackupInvalid", stderr)
                self.assertFalse(self.target.exists())
        self.assert_gate_free()

    def test_manifest_must_hold_exactly_one_present_subscriptions_file(self):
        entry = {"path": "state/subscriptions.json", "source_rel": "subscriptions.json", "present": True}
        self.assertEqual(cli._subscriptions_member({"files": [entry]}), "state/subscriptions.json")
        for files in (
            [],
            [entry, dict(entry)],
            [{**entry, "present": False}],
            [{**entry, "entry_type": "directory"}],
        ):
            with self.subTest(files=files):
                with self.assertRaises(cli.SubscriptionBackupInvalid):
                    cli._subscriptions_member({"files": files})


class BoundaryTest(unittest.TestCase):
    def test_script_imports_no_collection_notification_or_network_module(self):
        tree_ = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        imported = set()
        names = set()
        for node in ast.walk(tree_):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        forbidden = {
            "main",
            "basket_collect",
            "collector",
            "sync_subscriptions",
            "notifier",
            "email_notifier",
            "ops_alert",
            "web_form",
            "requests",
            "smtplib",
            "urllib.request",
            "http.client",
        }
        self.assertEqual(imported & forbidden, set())
        self.assertNotIn("restore_to_production", names)
        self.assertNotIn("record_restore_verified", names)

    def test_help_is_offline_and_names_every_apply_guard(self):
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPT), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env={**os.environ, "NO_LIVE_API": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        compact = "".join(result.stdout.split())
        for term in ("--archive", "--apply", "--confirm", "--expect-sha256", cli.CONFIRMATION_WORD):
            self.assertIn(term, compact)

    def test_procedure_is_documented_and_linked(self):
        text = DOC.read_text(encoding="utf-8")
        for phrase in (
            "维护窗口",
            "Disable-ScheduledTask",
            "Enable-ScheduledTask",
            "scripts/restore_subscriptions_from_backup.py",
            "--expect-sha256",
            cli.CONFIRMATION_WORD,
            "backup_status.json",
            "--force-production",
            "active",
            "preflight_skip",
            "不启动采集",
        ):
            self.assertIn(phrase, text)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        manual = (ROOT / "docs" / "runtime-backup-and-restore.md").read_text(encoding="utf-8")
        self.assertIn("docs/subscriptions-single-file-restore.md", readme)
        self.assertIn("subscriptions-single-file-restore.md", manual)


if __name__ == "__main__":
    unittest.main()
