"""R13b(R13 登记 ①②)：篮子哨兵去重状态的两类既有缺口。

基线(R21 合并后的 main，bc050c94，tree cbc5c8ff)：
① run_basket_sentinel 在提醒发送成功后写去重状态；_write_state 抛 OSError(状态目录不可建、文件被占用或只读等)时异常向外抛，
   main._run_basket_sentinel_for_main 记"[篮子哨兵] 检查失败"并返回 status=error、notified=False——提醒其实已经发出；
   状态没写进去，同日之后再到点且条件仍成立时会再发一次，并再记一次"检查失败"。临时文件清理失败时，清理的异常会盖掉写入的原始错误。
② _load_state 只捕获 OSError、UnicodeError 与 JSONDecodeError；合法 UTF-8 但 json 拒收的内容——整数超过 4300 位(ValueError)、
   嵌套过深(RecursionError)——穿出 _load_state，当次检查在调用通知器之前失败，之后每天到点该发的提醒都被跳过，直到文件被修好。
口径(用户 2026-10-31"开工r13"，按建议口径)：
① 发送成功后写状态遇 OSError 不再向外抛：结论仍为 notified，结果带 state_saved=False，记一行"[篮子哨兵] 已通知，但去重状态写入失败 原因=…"，
   主入口不再记"检查失败"；写入成功时 state_saved=True。先发后记的顺序与发送频率不变，所以状态写不进去时同日再到点仍会再提醒(至少一次)，
   状态恢复可写后下一次到点发一次并写入，之后去重恢复。临时文件清理失败只记一行，不再掩盖原始错误。
② 上述两类解析异常与坏 JSON、非法 UTF-8 同口径：按今日未通知处理，读取不改写文件；到点照常发一次，发送成功后整份重写(R13 既有口径)。
两处捕获都只限上述异常类型：编程错误(如 TypeError)照旧向外抛，由主入口记"检查失败"(独立复核后补测)。
"""

from contextlib import ExitStack, contextmanager, redirect_stdout
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import basket_sentinel as sentinel
import main


AFTER = datetime(2026, 10, 18, 21, 0)
NO_LEDGER = {"entries": [{"round_id": "collection_20261018T090000", "recorded_at": "2026-10-18T09:05:00+08:00"}]}
NO_ROUNDS = {"readable": True, "rounds": []}
TODAY_RECORD = {"last_notified_day": "2026-10-18", "notified_at": "2026-10-18T21:00:00"}
REJECTED_STATES = (
    ("int_over_digit_limit", ('{"last_notified_day": "2026-10-18", "n": ' + "9" * 5000 + "}").encode("utf-8")),
    ("nesting_too_deep", ("[" * 200000 + "]" * 200000).encode("utf-8")),
)
WRITE_FAILURE_LINE = "[篮子哨兵] 已通知，但去重状态写入失败 原因="
CLEANUP_FAILURE_LINE = "[篮子哨兵] 临时文件清理失败 "


@contextmanager
def default_int_digit_limit():
    """把整数位数上限固定为 CPython 缺省 4300，测试不受 PYTHONINTMAXSTRDIGITS 影响(独立复核后补)。"""
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(previous)


def run(state_path, *, notifier_result=True):
    calls, logs = [], []
    with redirect_stdout(io.StringIO()), patch.object(sentinel, "safe_log", side_effect=logs.append):
        result = sentinel.run_basket_sentinel(
            usage_payload=NO_LEDGER, now=AFTER, threshold="20:00", state_path=state_path,
            round_evidence=NO_ROUNDS,
            notifier=lambda title, content: calls.append(title) or notifier_result)
    return result, calls, logs


def replace_denied():
    return patch.object(sentinel.os, "replace", side_effect=PermissionError(13, "Permission denied", "replace-target"))


def temp_files(root: Path) -> list[str]:
    return sorted(path.name for path in root.iterdir() if path.name.endswith(".tmp"))


def run_main_wrapper(root: Path, state_path: Path, *, extra=()):
    alerts, main_logs, sentinel_logs = [], [], []
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"BASKET_SENTINEL_AFTER": "20:00"}))
        stack.enter_context(patch.object(main, "ROUND_LOG_ROOT", root / "rounds"))
        stack.enter_context(patch.object(main, "BASKET_SENTINEL_STATE_PATH", state_path))
        stack.enter_context(patch.object(main, "_basket_run_expectation_for_main",
                                         return_value={"expected": True, "reason": ""}))
        stack.enter_context(patch.object(main, "load_usage_strict", return_value=NO_LEDGER))
        stack.enter_context(patch.object(main, "_notify_system_alert",
                                         side_effect=lambda subs, title, content: alerts.append(title) or True))
        stack.enter_context(patch.object(main, "safe_log", side_effect=main_logs.append))
        stack.enter_context(patch.object(sentinel, "safe_log", side_effect=sentinel_logs.append))
        for context in extra:
            stack.enter_context(context)
        result = main._run_basket_sentinel_for_main([], now=AFTER)
    return result, alerts, main_logs, sentinel_logs


class RejectedStateContentTest(unittest.TestCase):
    """②：json 拒收的合法 UTF-8 内容按今日未通知处理。"""

    def test_load_state_treats_rejected_content_as_not_notified(self):
        for name, data in REJECTED_STATES:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp, default_int_digit_limit():
                path = Path(tmp) / "basket_sentinel.json"
                path.write_bytes(data)
                self.assertEqual(sentinel._load_state(path), {})
                self.assertEqual(path.read_bytes(), data, "LOAD_IS_READ_ONLY")

    def test_due_check_notifies_once_then_rewrites_and_dedupes(self):
        for name, data in REJECTED_STATES:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp, default_int_digit_limit():
                path = Path(tmp) / "basket_sentinel.json"
                path.write_bytes(data)
                first, calls, _logs = run(path)
                self.assertEqual((first["status"], first["state_saved"], calls),
                                 ("notified", True, ["[篮子哨兵] 今日篮子未运行"]))
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), TODAY_RECORD)
                second, more, _logs = run(path)
                self.assertEqual((second["status"], more), ("already_notified", []), "DEDUPE_RESTORED")

    def test_main_wrapper_no_longer_reports_check_failure(self):
        for name, data in REJECTED_STATES:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp, default_int_digit_limit():
                state_path = Path(tmp) / "basket_sentinel.json"
                state_path.write_bytes(data)
                result, alerts, main_logs, _sentinel_logs = run_main_wrapper(Path(tmp), state_path)
                self.assertEqual((result["status"], result["notified"]), ("notified", True))
                self.assertEqual(alerts, ["[篮子哨兵] 今日篮子未运行"])
                self.assertEqual([line for line in main_logs if line.startswith("[篮子哨兵] 检查失败")], [])

    def test_programming_errors_in_parsing_still_propagate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "basket_sentinel.json"
            path.write_text('{"last_notified_day": "2026-10-17"}', encoding="utf-8")
            with patch.object(sentinel.json, "loads", side_effect=TypeError("parser bug")):
                with self.assertRaises(TypeError):
                    sentinel._load_state(path)


class StateWriteFailureTest(unittest.TestCase):
    """①：提醒发出后去重状态写不进去，照实记录，不再算检查失败。"""

    def test_write_failure_after_send_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "basket_sentinel.json"
            path.write_text('{"last_notified_day": "2026-10-17"}', encoding="utf-8")
            with replace_denied():
                result, calls, logs = run(path)
            self.assertEqual((result["status"], result["notified"], result["state_saved"]), ("notified", True, False))
            self.assertEqual(calls, ["[篮子哨兵] 今日篮子未运行"])
            failures = [line for line in logs if line.startswith(WRITE_FAILURE_LINE)]
            self.assertEqual(len(failures), 1)
            self.assertIn("PermissionError", failures[0])
            self.assertEqual(path.read_text(encoding="utf-8"), '{"last_notified_day": "2026-10-17"}', "STATE_UNCHANGED")
            self.assertEqual(temp_files(Path(tmp)), [])

    def test_unwritable_state_directory_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "state_dir"
            blocker.write_text("not a directory", encoding="utf-8")
            result, calls, logs = run(blocker / "basket_sentinel.json")
            self.assertEqual((result["status"], result["state_saved"], len(calls)), ("notified", False, 1))
            self.assertEqual(len([line for line in logs if line.startswith(WRITE_FAILURE_LINE)]), 1)
            self.assertEqual(blocker.read_text(encoding="utf-8"), "not a directory")

    def test_state_path_occupied_by_directory_is_reported_not_raised(self):
        """状态文件位置被同名目录占住：读取按未通知(OSError 分支)，写入失败照实记录(独立复核后补)。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "basket_sentinel.json"
            path.mkdir()
            result, calls, logs = run(path)
            self.assertEqual((result["status"], result["state_saved"], len(calls)), ("notified", False, 1))
            self.assertEqual(len([line for line in logs if line.startswith(WRITE_FAILURE_LINE)]), 1)
            self.assertTrue(path.is_dir())
            self.assertEqual(temp_files(Path(tmp)), [])

    def test_same_day_repeat_until_state_is_writable_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "basket_sentinel.json"
            with replace_denied():
                first, first_calls, _logs = run(path)
                second, second_calls, _logs = run(path)
            third, third_calls, _logs = run(path)
            fourth, fourth_calls, _logs = run(path)
        self.assertEqual([r["status"] for r in (first, second, third, fourth)],
                         ["notified", "notified", "notified", "already_notified"])
        self.assertEqual([r.get("state_saved") for r in (first, second, third)], [False, False, True])
        self.assertEqual([len(c) for c in (first_calls, second_calls, third_calls, fourth_calls)], [1, 1, 1, 0])

    def test_temp_cleanup_failure_does_not_mask_the_write_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "basket_sentinel.json"
            with replace_denied(), patch.object(sentinel.Path, "unlink",
                                                side_effect=PermissionError(13, "Permission denied", "temp-file")):
                result, _calls, logs = run(path)
        failures = [line for line in logs if line.startswith(WRITE_FAILURE_LINE)]
        cleanups = [line for line in logs if line.startswith(CLEANUP_FAILURE_LINE)]
        self.assertEqual((result["status"], result["state_saved"], len(failures)), ("notified", False, 1))
        self.assertIn("replace-target", failures[0])
        self.assertNotIn("temp-file", failures[0])
        self.assertEqual(len(cleanups), 1)
        self.assertIn("temp-file", cleanups[0])

    def test_programming_errors_in_writing_still_propagate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "basket_sentinel.json"
            with patch.object(sentinel.json, "dump", side_effect=TypeError("serializer bug")):
                with self.assertRaises(TypeError):
                    run(path)
            self.assertFalse(path.exists())
            self.assertEqual(temp_files(Path(tmp)), [])

    def test_failed_notification_still_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "basket_sentinel.json"
            result, calls, logs = run(path, notifier_result=False)
            exists = path.exists()
        self.assertEqual((result["status"], len(calls), exists), ("notify_failed", 1, False))
        self.assertNotIn("state_saved", result)
        self.assertEqual([line for line in logs if line.startswith(WRITE_FAILURE_LINE)], [])

    def test_main_wrapper_reports_notified_instead_of_check_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "basket_sentinel.json"
            result, alerts, main_logs, sentinel_logs = run_main_wrapper(Path(tmp), state_path, extra=(replace_denied(),))
        self.assertEqual((result["status"], result["notified"], result["state_saved"]), ("notified", True, False))
        self.assertEqual(alerts, ["[篮子哨兵] 今日篮子未运行"])
        self.assertEqual([line for line in main_logs if line.startswith("[篮子哨兵] 检查失败")], [])
        self.assertEqual(len([line for line in sentinel_logs if line.startswith(WRITE_FAILURE_LINE)]), 1)


if __name__ == "__main__":
    unittest.main()
