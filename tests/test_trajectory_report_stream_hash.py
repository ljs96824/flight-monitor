"""R17：快照报告的数据库摘要改用仓库已有的分块流式函数，不再把整库一次读进内存。

基线(R14 合并后的 main)：scripts/trajectory_report._snapshot_db(scripts/trajectory_report.py:27)先按同一份已读字节校验并解析
清单，再以 hashlib.sha256(db.read_bytes()) 计算数据库摘要(:46)，数据库字节整体读入内存；仓库已有
readonly_snapshot.sha256_file(readonly_snapshot.py:30)按 1 MiB 分块计算同一摘要，备份与恢复都在用它。
口径(用户 2026-10-24 开工指令，按建议口径)：只把数据库摘要这一步委托给 sha256_file；清单仍只读一次、按同一份字节校验并解析；
摘要不符、缺失或无效照旧拒绝；数据库读取失败照旧显式抛错，且发生在任何 SQLite 读取之前；报告内容不变。
只改摘要阶段的内存用法，不承诺整份报告的内存恒定(其后仍会读取观测记录)。
"""

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import readonly_snapshot
from tests.test_trajectory_report import DEPARTURES, ROUTE, load_module, make_snapshot


class StreamDigestTest(unittest.TestCase):
    def test_stream_digest_equals_whole_file_digest_across_chunk_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            for size in (0, 1, 1024 * 1024 - 1, 1024 * 1024, 2 * 1024 * 1024 + 123):
                with self.subTest(size=size):
                    path = Path(tmp) / f"blob-{size}.bin"
                    data = bytes((index * 31 + 7) % 251 for index in range(size))
                    path.write_bytes(data)
                    self.assertEqual(readonly_snapshot.sha256_file(path), hashlib.sha256(data).hexdigest())


class SnapshotStreamHashTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.snapshot = Path(temporary.name) / "snapshot"
        self.sha = make_snapshot(self.snapshot)
        self.db = self.snapshot / "observations.sqlite3"

    def report(self, module):
        return module.generate_report(db_path=self.snapshot, expect_manifest_sha=self.sha,
                                      route=ROUTE, airport_pair="PVG-KIX", departures=DEPARTURES)

    def test_database_is_digested_by_streaming_not_read_whole(self):
        module = load_module()
        read_bytes = Path.read_bytes
        db = self.db

        def guarded(path):
            if path == db:
                raise AssertionError("DATABASE_READ_WHOLE")
            return read_bytes(path)

        with patch.object(Path, "read_bytes", guarded):
            self.assertEqual(module._snapshot_db(self.snapshot, self.sha), self.db)
        self.assertIs(module.sha256_file, readonly_snapshot.sha256_file, "SHARED_STREAM_FUNCTION")

    def test_report_is_identical_to_whole_file_digest(self):
        module = load_module()
        streamed = self.report(module)
        with patch.object(module, "sha256_file", lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()):
            whole = self.report(module)
        self.assertEqual(streamed, whole, "REPORT_UNCHANGED")
        self.assertTrue(streamed[0])

    def test_database_read_failure_stays_explicit_before_any_sqlite_read(self):
        module = load_module()
        failure = PermissionError("synthetic database read failure")
        with patch.object(module, "sha256_file", side_effect=failure), \
                patch.object(module, "load_tcurve_daily_cells") as daily, \
                patch.object(module, "_load_evidence") as raw:
            with self.assertRaises(PermissionError) as caught:
                self.report(module)
        self.assertIs(caught.exception, failure)
        daily.assert_not_called()
        raw.assert_not_called()

    def test_missing_database_file_is_still_an_error(self):
        module = load_module()
        self.db.unlink()
        with self.assertRaises(FileNotFoundError):
            module._snapshot_db(self.snapshot, self.sha)


if __name__ == "__main__":
    unittest.main()
