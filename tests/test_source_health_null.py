"""R24：源健康记录 source_health.json 内容为 JSON null 时与其他损坏内容同样处理——不覆盖，本轮降级为只用当前统计。

基线(R23 合并后的 main，16e2da65，tree e7cf2009)：health_check._update_source_history 在本轮有数据源统计时经 update_json
写入；update_json 对"文件不存在"与"现有内容为 JSON null"都把 None 交给 mutator(缺省 missing=None)，mutator 把 None 一律
当作空记录，于是 null 被本轮记录覆盖。同一文件的其他损坏内容(无法解析、根节点不是对象)不覆盖，记一行
"[源健康] source_health_state_degraded"，本轮只用当前统计(既有测试 test_corrupt_history_is_not_overwritten_and_current_stats_still_score)；
本轮没有数据源统计时，读取(_load_source_health)对 null 同样按损坏降级。
口径(用户 2026-11-01"开工下一件"，按登记顺序，按建议口径)：写入改传 missing={}——文件不存在时照旧从空记录开始并写入；
现有内容为 null 与其他非对象内容一样抛 JsonStoreReadError、不写入，走既有降级分支；缺失与 null 在 update_json 的同一把锁内区分。
代价(独立复核后补测)：文件保持 null 期间连续失败次数不跨轮累计(每轮从空记录算起)，"连续N次失败"告警不会出现，与其他
损坏内容的既有表现相同；删除该文件后下一轮重新建立，恢复累计。基线第一轮就把 null 覆盖为本轮记录(null 本身不含历史)。
"""

from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import atomic_json_store
import health_check


NULL_SPELLINGS = (b"null", b"null\n", b" \r\n null\t\r\n")
OTHER_NON_OBJECTS = (b"[]", b'"{}"', b"0", b"false", b"true")
DEGRADED = "source_health_state_degraded"
STATS = {"juhe": {"status": "success", "count": 3}, "serpapi": {"status": "failed", "count": 0}}
NOW = "2026-11-01T12:00:00"


class _FixedDateTime:
    @classmethod
    def now(cls):
        return datetime(2026, 11, 1, 12, 0, 0)


def _record(failures, status, count):
    return {"consecutive_failures": failures, "last_status": status, "last_count": count, "updated_at": NOW}


CURRENT_ONLY = {"juhe": _record(0, "success", 3), "serpapi": _record(1, "failed", 0)}


def _lock_then(action):
    """换掉 update_json 用的文件锁：拿到真实的锁之后先对目标文件执行 action，再交回调用方。"""
    real_lock = atomic_json_store.file_lock

    @contextmanager
    def lock(target, *args, **kwargs):
        with real_lock(target, *args, **kwargs):
            action(Path(target))
            yield

    return patch.object(atomic_json_store, "file_lock", lock)


class SourceHealthNullTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dir = Path(self.tmpdir.name)
        self.path = self.dir / "source_health.json"
        self.logs = []
        for patcher in (
            patch.object(health_check, "SOURCE_HEALTH_PATH", self.path),
            patch.object(health_check, "datetime", _FixedDateTime),
            patch.object(health_check, "safe_log", side_effect=self.logs.append),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def degraded_lines(self):
        return [line for line in self.logs if DEGRADED in str(line)]

    def assert_kept_and_degraded(self, raw):
        self.path.write_bytes(raw)
        self.logs.clear()
        history, warnings = health_check._update_source_history(STATS)
        self.assertEqual(history, CURRENT_ONLY)
        self.assertEqual(warnings, [])
        self.assertEqual(self.path.read_bytes(), raw)
        lines = self.degraded_lines()
        self.assertEqual(len(lines), 1, self.logs)
        self.assertIn("source_health 根节点必须是对象", lines[0])

    def test_json_null_is_kept_and_the_round_uses_current_stats(self):
        for raw in NULL_SPELLINGS:
            with self.subTest(raw=raw):
                self.assert_kept_and_degraded(raw)

    def test_other_non_objects_are_still_kept(self):
        for raw in OTHER_NON_OBJECTS:
            with self.subTest(raw=raw):
                self.assert_kept_and_degraded(raw)

    def test_missing_file_is_still_created(self):
        history, warnings = health_check._update_source_history(STATS)
        self.assertEqual(history, CURRENT_ONLY)
        self.assertEqual(warnings, [])
        self.assertEqual(self.saved(), CURRENT_ONLY)
        self.assertEqual(self.degraded_lines(), [])

    def test_existing_history_still_accumulates_and_warns(self):
        self.path.write_text(json.dumps({"serpapi": _record(2, "failed", 0)}), encoding="utf-8")
        history, warnings = health_check._update_source_history(STATS)
        expected = {"serpapi": _record(3, "failed", 0), "juhe": _record(0, "success", 3)}
        self.assertEqual(history, expected)
        self.assertEqual(warnings, ["数据源异常：serpapi连续3次失败"])
        self.assertEqual(self.saved(), expected)

    def test_null_written_after_the_lock_is_taken_is_kept(self):
        """调用时文件还不存在，拿到锁之后才被写成 null：按锁内读到的内容判定，仍不覆盖、按损坏降级。"""
        with _lock_then(lambda target: target.write_bytes(b"null\n")):
            history, _warnings = health_check._update_source_history(STATS)
        self.assertEqual(history, CURRENT_ONLY)
        self.assertEqual(self.path.read_bytes(), b"null\n")
        self.assertEqual(len(self.degraded_lines()), 1, self.logs)

    def test_file_removed_after_the_lock_is_taken_is_still_created(self):
        """调用时文件是对象，拿到锁之后才消失：按锁内看到的"不存在"照旧从空记录开始并写入。"""
        self.path.write_text("{}", encoding="utf-8")
        with _lock_then(lambda target: target.unlink()):
            history, _warnings = health_check._update_source_history(STATS)
        self.assertEqual(history, CURRENT_ONLY)
        self.assertEqual(self.saved(), CURRENT_ONLY)
        self.assertEqual(self.degraded_lines(), [])

    def test_round_without_source_stats_reads_null_as_degraded(self):
        self.path.write_bytes(b"null\n")
        self.assertEqual(health_check._update_source_history({"after_dedup": 0}), ({}, []))
        self.assertEqual(self.path.read_bytes(), b"null\n")
        self.assertEqual(len(self.degraded_lines()), 1, self.logs)

    def test_failures_do_not_accumulate_while_null_and_resume_after_removal(self):
        failing = {"serpapi": {"status": "failed", "count": 0}}
        self.path.write_bytes(b"null\n")
        for round_no in (1, 2, 3):
            with self.subTest(null_round=round_no):
                history, warnings = health_check._update_source_history(failing)
                self.assertEqual(history["serpapi"]["consecutive_failures"], 1)
                self.assertEqual(warnings, [])
        self.assertEqual(self.path.read_bytes(), b"null\n")
        self.assertEqual(len(self.degraded_lines()), 3, self.logs)
        self.path.unlink()
        for round_no in (1, 2, 3):
            history, warnings = health_check._update_source_history(failing)
            self.assertEqual(history["serpapi"]["consecutive_failures"], round_no)
        self.assertEqual(warnings, ["数据源异常：serpapi连续3次失败"])
        self.assertEqual(self.saved(), {"serpapi": _record(3, "failed", 0)})

    def test_system_health_check_over_null_matches_a_round_without_history(self):
        kwargs = {
            "source_stats": {"juhe": {"status": "success", "count": 3}, "serpapi": {"status": "failed", "count": 0}},
            "flights": [{"price": 100}, {"price": 120}],
        }
        self.path.write_bytes(b"null\n")
        over_null = health_check.system_health_check(**kwargs)
        self.assertEqual(self.path.read_bytes(), b"null\n")
        self.assertEqual(len(self.degraded_lines()), 1, self.logs)
        self.path.unlink()
        without_history = health_check.system_health_check(**kwargs)
        self.assertEqual(over_null, without_history)
        self.assertEqual(over_null["source_history"], CURRENT_ONLY)
        self.assertEqual(self.saved(), CURRENT_ONLY)


if __name__ == "__main__":
    unittest.main()
