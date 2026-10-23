"""R13：篮子哨兵去重状态不是合法 UTF-8 时，与坏 JSON 同口径处理，不再让当次检查提前退出。

基线(R16 合并后的 main)：basket_sentinel._load_state(basket_sentinel.py:168)只捕获 OSError 与 JSONDecodeError；
状态文件含非法 UTF-8 字节时 read_text 抛 UnicodeDecodeError，run_basket_sentinel 在调用通知器之前就向外抛出；
main._run_basket_sentinel_for_main 把它记成"[篮子哨兵] 检查失败"后返回，状态文件不被改写，于是之后每天到点该发的提醒都被跳过。
口径(用户裁定 2026-10-23，采纳建议)：沿用本模块对坏 JSON 的既有处理——视为今日未通知，到点发一次，发送成功后整份重写；
不使用 errors="ignore"/"replace" 之类的容错解码，不把部分可读的损坏内容当作可信的去重记录；不改发送频率与阈值。
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import basket_sentinel as sentinel
import main


AFTER = datetime(2026, 10, 18, 21, 0)
BEFORE = datetime(2026, 10, 18, 19, 59)
NO_LEDGER = {"entries": [{"round_id": "collection_20261018T090000", "recorded_at": "2026-10-18T09:05:00+08:00"}]}
NO_ROUNDS = {"readable": True, "rounds": []}
UNREADABLE_STATES = (
    ("invalid_utf8_bytes", b"\xff\xfe{}"),
    ("invalid_byte_inside_today_record", b'{"last_notified_day": "2026-10-18", "note": "\xff"}'),
    ("truncated_multibyte", '{"last_notified_day": "2026-10-18", "note": "篮"}'.encode("utf-8")[:-3]),
    ("corrupt_json_same_policy", b"{bad"),
)


def run(state_path, *, now=AFTER, notifier_result=True):
    calls = []
    with redirect_stdout(io.StringIO()):
        result = sentinel.run_basket_sentinel(
            usage_payload=NO_LEDGER, now=now, threshold="20:00", state_path=state_path,
            round_evidence=NO_ROUNDS,
            notifier=lambda title, content: calls.append((title, content)) or notifier_result)
    return result, calls


class LoadStateDecodeTest(unittest.TestCase):
    def test_unreadable_state_is_treated_as_not_notified_today(self):
        for name, data in UNREADABLE_STATES:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "basket_sentinel.json"
                path.write_bytes(data)
                self.assertEqual(sentinel._load_state(path), {})
                self.assertEqual(path.read_bytes(), data, "LOAD_IS_READ_ONLY")

    def test_valid_state_is_still_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "basket_sentinel.json"
            path.write_text(json.dumps({"last_notified_day": "2026-10-18", "note": "篮子"}, ensure_ascii=False),
                            encoding="utf-8")
            self.assertEqual(sentinel._load_state(path), {"last_notified_day": "2026-10-18", "note": "篮子"})


class SentinelUnreadableStateTest(unittest.TestCase):
    def test_due_check_notifies_once_then_rewrites_and_dedupes(self):
        for name, data in UNREADABLE_STATES:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "basket_sentinel.json"
                path.write_bytes(data)
                first, calls = run(path)
                self.assertEqual((first["status"], len(calls)), ("notified", 1), "NOT_TRUSTED_AS_ALREADY_NOTIFIED")
                self.assertEqual(calls[0][0], "[篮子哨兵] 今日篮子未运行")
                stored = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(stored, {"last_notified_day": "2026-10-18", "notified_at": "2026-10-18T21:00:00"})
                second, more = run(path)
                self.assertEqual((second["status"], more), ("already_notified", []), "DEDUPE_RESTORED")

    def test_failed_notification_keeps_the_damaged_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "basket_sentinel.json"
            path.write_bytes(b"\xff\xfe{}")
            result, calls = run(path, notifier_result=False)
            self.assertEqual((result["status"], len(calls)), ("notify_failed", 1))
            self.assertEqual(path.read_bytes(), b"\xff\xfe{}")

    def test_not_due_before_threshold_leaves_state_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "basket_sentinel.json"
            path.write_bytes(b"\xff\xfe{}")
            result, calls = run(path, now=BEFORE)
            self.assertEqual((result["due"], calls), (False, []))
            self.assertEqual(path.read_bytes(), b"\xff\xfe{}")


class MainSentinelUnreadableStateTest(unittest.TestCase):
    def test_main_wrapper_no_longer_reports_check_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "basket_sentinel.json"
            state_path.write_bytes(b"\xff\xfe{}")
            alerts, logs = [], []
            with (
                patch.object(main, "ROUND_LOG_ROOT", root / "rounds"),
                patch.object(main, "BASKET_SENTINEL_STATE_PATH", state_path),
                patch.object(main, "_basket_run_expectation_for_main", return_value={"expected": True, "reason": ""}),
                patch.object(main, "load_usage_strict", return_value=NO_LEDGER),
                patch.object(main, "_notify_system_alert", side_effect=lambda subs, title, content:
                             alerts.append((title, content)) or True),
                patch.object(main, "safe_log", side_effect=logs.append),
                patch.object(sentinel, "safe_log"),
            ):
                result = main._run_basket_sentinel_for_main([], now=AFTER)
            stored = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual((result["run_class"], result["status"]), ("none", "notified"))
        self.assertEqual([title for title, _content in alerts], ["[篮子哨兵] 今日篮子未运行"])
        self.assertFalse(any(line.startswith("[篮子哨兵] 检查失败") for line in logs), "NO_CHECK_FAILURE")
        self.assertEqual(stored["last_notified_day"], "2026-10-18")


if __name__ == "__main__":
    unittest.main()
