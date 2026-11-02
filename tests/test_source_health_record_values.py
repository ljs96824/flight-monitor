"""R24b：源健康记录里某个源的连续失败次数写坏时，本轮健康检查不再因此失败。

基线(R24 合并后的 main，7562aa10，tree a0f4077d)：health_check._apply_source_history_updates 在该源本轮失败时用
int(record.get("consecutive_failures", 0)) 读取已记录的次数；值为 null、数组、对象时抛 TypeError，"x"、"3.5" 这类非整数
字符串与 NaN 抛 ValueError，Infinity 抛 OverflowError。_update_source_history 只捕获 JsonStoreReadError，异常从
system_health_check(main.py:2906)抛出，由 main._process_subscription_locked 自己的异常处理接住(main.py:3017–3031)：
打印"[处理失败]"、记该订阅失败并返回 False，批次计为失败(main.py:3263–3264)；提醒在健康检查之后才生成，本轮不发；
文件不被改写，只要该源继续失败，之后每轮如此。该源本轮成功时次数直接置 0、不读取旧值，不会出错；记录本身不是对象时
既有代码按新记录处理。
口径(用户 2026-11-01"开工r24"，即 R24 独立复核登记的这一项，按建议口径)：读取次数时值无法按整数读取一律按 0 计并记一行
"[源健康] source_health_record_reset"，本轮照常计为第 1 次失败并写回；可按整数读取的写法(如 "2"、2.0)与缺少该字段时
照旧沿用，其他源的记录不受影响。代价(独立复核后补测)：写坏的原值被覆盖、日志只记类型名；连续失败从 1 重新累计，
"连续N次失败"告警要到之后第 3 次失败才再出现；这一行只在该源本轮失败时记，本轮成功与记录本身不是对象时的修复不记。
时间、日志与文件路径在测试内固定。
"""

from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import health_check


NOW = "2026-11-01T12:00:00"
OLD = "2026-10-31T12:00:00"
RESET = "source_health_record_reset"
FAILED = {"status": "failed", "count": 0}
SUCCESS = {"status": "success", "count": 3}
BAD_VALUES = (
    ("null", b"null", "NoneType"),
    ("text", b'"x"', "str"),
    ("decimal_text", b'"3.5"', "str"),
    ("nan", b"NaN", "float"),
    ("infinity", b"Infinity", "float"),
    ("array", b"[1]", "list"),
    ("object", b'{"n": 1}', "dict"),
)


class _FixedDateTime:
    @classmethod
    def now(cls):
        return datetime(2026, 11, 1, 12, 0, 0)


def _record(failures, status, count, updated_at=NOW):
    return {"consecutive_failures": failures, "last_status": status, "last_count": count, "updated_at": updated_at}


def _raw_history(source, raw_failures):
    """按字节拼出一条记录，便于写入 NaN、Infinity 这类只有解析时才会出现的值。"""
    return (
        b'{"' + source.encode() + b'": {"consecutive_failures": ' + raw_failures
        + b', "last_status": "failed", "last_count": 0, "updated_at": "' + OLD.encode() + b'"}}'
    )


class SourceHealthRecordValuesTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = Path(self.tmpdir.name) / "source_health.json"
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

    def reset_lines(self):
        return [line for line in self.logs if RESET in str(line)]

    def test_unreadable_count_restarts_at_one_failure(self):
        for name, raw, type_name in BAD_VALUES:
            with self.subTest(value=name):
                self.path.write_bytes(_raw_history("juhe", raw))
                self.logs.clear()
                history, warnings = health_check._update_source_history({"juhe": FAILED})
                self.assertEqual(history, {"juhe": _record(1, "failed", 0)})
                self.assertEqual(warnings, [])
                self.assertEqual(self.saved(), {"juhe": _record(1, "failed", 0)})
                self.assertEqual(
                    self.reset_lines(),
                    [f"[源健康] {RESET} source=juhe 原因=连续失败次数无法按整数读取({type_name}); 按 0 计"],
                )

    def test_success_still_resets_without_reading_the_old_value(self):
        for name, raw, _type_name in BAD_VALUES:
            with self.subTest(value=name):
                self.path.write_bytes(_raw_history("juhe", raw))
                self.logs.clear()
                history, _warnings = health_check._update_source_history({"juhe": SUCCESS})
                self.assertEqual(history, {"juhe": _record(0, "success", 3)})
                self.assertEqual(self.saved(), {"juhe": _record(0, "success", 3)})
                self.assertEqual(self.reset_lines(), [])

    def test_integer_readable_counts_still_accumulate_and_warn(self):
        for name, raw in (("int", b"2"), ("float", b"2.0"), ("digit_text", b'"2"')):
            with self.subTest(value=name):
                self.path.write_bytes(_raw_history("serpapi", raw))
                self.logs.clear()
                history, warnings = health_check._update_source_history({"serpapi": FAILED})
                self.assertEqual(history, {"serpapi": _record(3, "failed", 0)})
                self.assertEqual(warnings, ["数据源异常：serpapi连续3次失败"])
                self.assertEqual(self.reset_lines(), [])

    def test_record_without_a_count_starts_at_one_without_a_reset_line(self):
        self.path.write_text(json.dumps({"juhe": {"last_status": "failed"}}), encoding="utf-8")
        history, warnings = health_check._update_source_history({"juhe": FAILED})
        self.assertEqual(history, {"juhe": _record(1, "failed", 0)})
        self.assertEqual(warnings, [])
        self.assertEqual(self.reset_lines(), [])

    def test_non_object_record_is_still_a_fresh_record_without_a_reset_line(self):
        self.path.write_text(json.dumps({"juhe": "broken"}), encoding="utf-8")
        history, warnings = health_check._update_source_history({"juhe": FAILED})
        self.assertEqual(history, {"juhe": _record(1, "failed", 0)})
        self.assertEqual(warnings, [])
        self.assertEqual(self.saved(), {"juhe": _record(1, "failed", 0)})
        self.assertEqual(self.reset_lines(), [])

    def test_streak_restarts_after_a_reset_and_warns_again_on_the_third_failure(self):
        self.path.write_bytes(_raw_history("juhe", b'"5.0"'))
        observed = [health_check._update_source_history({"juhe": FAILED}) for _ in range(3)]
        self.assertEqual([history["juhe"]["consecutive_failures"] for history, _ in observed], [1, 2, 3])
        self.assertEqual([warnings for _, warnings in observed], [[], [], ["数据源异常：juhe连续3次失败"]])
        self.assertEqual(len(self.reset_lines()), 1, self.logs)

    def test_other_sources_are_unaffected(self):
        self.path.write_text(
            json.dumps({"juhe": {"consecutive_failures": None}, "serpapi": _record(2, "failed", 0, OLD)}),
            encoding="utf-8",
        )
        history, warnings = health_check._update_source_history({"juhe": FAILED, "serpapi": FAILED})
        expected = {"juhe": _record(1, "failed", 0), "serpapi": _record(3, "failed", 0)}
        self.assertEqual(history, expected)
        self.assertEqual(warnings, ["数据源异常：serpapi连续3次失败"])
        self.assertEqual(self.saved(), expected)
        self.assertEqual(len(self.reset_lines()), 1, self.logs)

    def test_system_health_check_completes_over_an_unreadable_count(self):
        self.path.write_bytes(_raw_history("juhe", b'"x"'))
        result = health_check.system_health_check(source_stats={"juhe": FAILED}, flights=[])
        self.assertEqual(result["source_history"], {"juhe": _record(1, "failed", 0)})
        self.assertEqual(self.saved(), {"juhe": _record(1, "failed", 0)})
        self.assertEqual(len(self.reset_lines()), 1, self.logs)


if __name__ == "__main__":
    unittest.main()
