"""R5(F6a)：篮子哨兵在台账之外再读当日轮档，分清"未运行"与"已运行但零请求/被阻断/未正常结束"。

基线(96f02c7)只看台账里有没有当日 basket_ 条目；零请求的轮次(全部命中缓存、研究硬门 blocked、状态冲突)
不写台账条目，20:00 后一律报"今日篮子未运行，请检查 Windows 任务计划"，诊断方向是错的。
口径：台账有当日篮子条目 → 不报(不变)；否则读当日轮档(只读)：有轮次以 ok/partial 结束 → 已运行，不报；
最后一个轮次 blocked → 仍报(运行期阻断照旧告警)，标题与正文写明"已运行但被阻断"及轮档里的原因行；
interrupted、其他状态或未见结束标记 → 报"未正常结束"；轮档读不到 → 报"运行情况无法确认"；没有轮次 → 报"未运行"(原文不变)。
配置不可读时正文追加"今日是否应运行未能确认"。不向台账或轮档写任何内容；每日只通知一次不变。
"""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import basket_sentinel as sentinel
import log_utils
import main


DAY = date(2026, 10, 18)
AFTER = datetime(2026, 10, 18, 21, 0)
BEFORE = datetime(2026, 10, 18, 19, 59)
NO_LEDGER = {"entries": [{"round_id": "collection_20261018T090000", "recorded_at": "2026-10-18T09:05:00+08:00"}]}


def write_archive(root, rounds):
    """用真实的轮档写入函数生成当日轮档；rounds 为 (round_id, [行], 结束状态或 None=不写结束标记)。"""
    console = io.StringIO()
    private_sys = SimpleNamespace(stdout=console, stderr=io.StringIO())
    with (
        patch.object(log_utils, "sys", private_sys),
        patch.object(log_utils, "_round_log_state", None),
        patch.object(log_utils, "print", lambda *a, **k: print(*a, file=private_sys.stdout, **k), create=True),
    ):
        for round_id, lines, status in rounds:
            log_utils.start_round_log_archive(round_id, root_dir=root, now=datetime(2026, 10, 18, 9, 30))
            for line in lines:
                log_utils.safe_log(line)
            if status is not None:
                log_utils.end_round_log_archive(status=status)
            else:
                state = log_utils._round_log_state
                private_sys.stdout, private_sys.stderr = state["stdout"], state["stderr"]
                state["file"].close()
                log_utils._round_log_state = None
    return root / "20261018.log"


class RoundEvidenceParsingTest(unittest.TestCase):
    def test_parses_real_archive_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_archive(Path(tmp), [
                ("collection_20261018T090000", ["[篮子跳过] 原因=不属于篮子轮次"], "ok"),
                ("basket_20261018T093000", ["[篮子轮次] round_id=basket_20261018T093000",
                                            "[篮子跳过] 原因=研究采样硬门未通过"], "blocked"),
                ("basket_20261018T120000", ["[篮子中断] round_id=basket_20261018T120000 阶段=准备 原因=OSError:disk"],
                 "interrupted"),
                ("basket_20261018T150000", ["[研究状态冲突] round=basket_20261018T150000 x 本轮研究进度不推进"], None),
            ])
            evidence = sentinel.read_basket_round_evidence(Path(tmp), DAY)
        self.assertTrue(evidence["readable"])
        self.assertEqual([(item["round_id"], item["status"], item["reason"]) for item in evidence["rounds"]], [
            ("basket_20261018T093000", "blocked", "[篮子跳过] 原因=研究采样硬门未通过"),
            ("basket_20261018T120000", "interrupted", "[篮子中断] round_id=basket_20261018T120000 阶段=准备 原因=OSError:disk"),
            ("basket_20261018T150000", None, "[研究状态冲突] round=basket_20261018T150000 x 本轮研究进度不推进"),
        ], "collection_round_lines_not_attributed")

    def test_missing_and_unreadable_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = sentinel.read_basket_round_evidence(Path(tmp), DAY)
            (Path(tmp) / "20261018.log").mkdir()
            unreadable = sentinel.read_basket_round_evidence(Path(tmp), DAY)
        self.assertEqual((missing["readable"], missing["rounds"]), (True, []))
        self.assertFalse(unreadable["readable"])
        self.assertTrue(unreadable["error"])


def evidence(*rounds, readable=True):
    return {"readable": readable, "rounds": [{"round_id": rid, "status": status, "reason": reason}
                                             for rid, status, reason in rounds], "error": "PermissionError"}


class SentinelClassificationTest(unittest.TestCase):
    CASES = (
        # 名称, 台账, 轮档证据, 期望(due, run_class, reason)
        ("no_evidence_argument", NO_LEDGER, None, (True, "none", "今日篮子未运行")),
        ("no_basket_round", NO_LEDGER, evidence(), (True, "none", "今日篮子未运行")),
        ("ok_without_requests", NO_LEDGER, evidence(("basket_a", "ok", "")), (False, "ran", "")),
        ("partial_without_requests", NO_LEDGER, evidence(("basket_a", "partial", "")), (False, "ran", "")),
        ("blocked_hard_gate", NO_LEDGER, evidence(("basket_a", "blocked", "[篮子跳过] 原因=研究采样硬门未通过")),
         (True, "blocked", "今日篮子已运行但被阻断")),
        ("interrupted", NO_LEDGER, evidence(("basket_a", "interrupted", "[篮子中断] x")),
         (True, "abnormal", "今日篮子未正常结束")),
        ("no_end_marker", NO_LEDGER, evidence(("basket_a", None, "")), (True, "abnormal", "今日篮子未正常结束")),
        ("atexit_completed", NO_LEDGER, evidence(("basket_a", "completed", "")), (True, "abnormal", "今日篮子未正常结束")),
        ("earlier_ok_then_blocked", NO_LEDGER, evidence(("basket_a", "ok", ""), ("basket_b", "blocked", "")),
         (False, "ran", "")),
        ("unreadable", NO_LEDGER, evidence(readable=False), (True, "unreadable", "今日篮子运行情况无法确认")),
        ("ledger_entry_wins", {"entries": [{"round_id": "basket_20261018T093000", "recorded_at": "2026-10-18T09:31:00"}]},
         evidence(("basket_a", "blocked", "")), (False, "ledger", "")),
    )

    def test_classification_matrix_after_threshold(self):
        for name, usage, round_evidence, expected in self.CASES:
            with self.subTest(case=name):
                result = sentinel.evaluate_basket_sentinel(usage, now=AFTER, threshold="20:00", round_evidence=round_evidence)
                self.assertEqual((result["due"], result["run_class"], result["reason"]), expected)

    def test_never_due_before_threshold(self):
        for name, usage, round_evidence, _expected in self.CASES:
            with self.subTest(case=name):
                self.assertFalse(sentinel.evaluate_basket_sentinel(usage, now=BEFORE, threshold="20:00",
                                                          round_evidence=round_evidence)["due"])

    def test_classify_picks_last_round(self):
        run_class, last = sentinel.classify_basket_rounds(evidence(("basket_a", "interrupted", ""), ("basket_b", "blocked", "x")))
        self.assertEqual((run_class, last["round_id"]), ("blocked", "basket_b"))


class SentinelMessageTest(unittest.TestCase):
    def run_sentinel(self, round_evidence, *, expectation_reason="", days=1):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            for _ in range(days):
                with redirect_stdout(io.StringIO()):
                    result = sentinel.run_basket_sentinel(
                        usage_payload=NO_LEDGER, now=AFTER, threshold="20:00",
                        state_path=Path(tmp) / "basket_sentinel.json", round_evidence=round_evidence,
                        expectation_reason=expectation_reason,
                        notifier=lambda title, content: calls.append((title, content)) or True)
        return result, calls

    def test_titles_and_contents_name_the_real_situation(self):
        cases = (
            ("none", evidence(), "[篮子哨兵] 今日篮子未运行",
             ["今日固定篮子尚无采集台账记录，请检查 Windows 任务计划与 basket.log。"]),
            ("blocked", evidence(("basket_20261018T093000", "blocked", "[篮子跳过] 原因=研究采样硬门未通过")),
             "[篮子哨兵] 今日篮子已运行但被阻断",
             ["basket_20261018T093000", "以 blocked 结束", "[篮子跳过] 原因=研究采样硬门未通过", "这不是任务计划未运行"]),
            ("abnormal", evidence(("basket_20261018T120000", None, "")), "[篮子哨兵] 今日篮子未正常结束",
             ["basket_20261018T120000", "状态=未见结束标记"]),
            ("unreadable", evidence(readable=False), "[篮子哨兵] 今日篮子运行情况无法确认",
             ["当日轮档无法读取(PermissionError)"]),
        )
        for name, round_evidence, title, fragments in cases:
            with self.subTest(case=name):
                result, calls = self.run_sentinel(round_evidence)
                self.assertEqual((result["status"], len(calls)), ("notified", 1))
                self.assertEqual(calls[0][0], title)
                for fragment in fragments:
                    self.assertIn(fragment, calls[0][1])
                self.assertNotIn("未能确认", calls[0][1])

    def test_ran_round_is_not_notified(self):
        result, calls = self.run_sentinel(evidence(("basket_a", "ok", "")))
        self.assertEqual((result["due"], result.get("status"), calls), (False, None, []))

    def test_settings_unavailable_note_and_daily_dedupe(self):
        result, calls = self.run_sentinel(evidence(), expectation_reason="settings_unavailable", days=2)
        self.assertEqual(len(calls), 1, "notified_once_per_day")
        self.assertTrue(calls[0][1].endswith("另：篮子配置不可读，今日是否应运行未能确认。"))
        self.assertEqual(result["status"], "already_notified")


class MainSentinelEvidenceTest(unittest.TestCase):
    def test_main_reads_today_archive_and_passes_expectation_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_archive(root / "rounds", [("basket_20261018T093000", ["[篮子跳过] 原因=研究采样硬门未通过"], "blocked")])
            alerts = []
            with (
                patch.object(main, "ROUND_LOG_ROOT", root / "rounds"),
                patch.object(main, "BASKET_SENTINEL_STATE_PATH", root / "basket_sentinel.json"),
                patch.object(main, "_basket_run_expectation_for_main",
                             return_value={"expected": True, "reason": "settings_unavailable"}),
                patch.object(main, "load_usage_strict", return_value=NO_LEDGER),
                patch.object(main, "_notify_system_alert", side_effect=lambda subs, title, content:
                             alerts.append((title, content)) or True),
                redirect_stdout(io.StringIO()),
            ):
                result = main._run_basket_sentinel_for_main([], now=AFTER)
        self.assertEqual((result["run_class"], result["status"]), ("blocked", "notified"))
        self.assertEqual(alerts[0][0], "[篮子哨兵] 今日篮子已运行但被阻断")
        self.assertIn("[篮子跳过] 原因=研究采样硬门未通过", alerts[0][1])
        self.assertIn("今日是否应运行未能确认", alerts[0][1])

    def test_archive_reader_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_archive(Path(tmp), [("basket_20261018T093000", [], "ok")])
            before = path.read_bytes()
            sentinel.read_basket_round_evidence(Path(tmp), DAY)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["20261018.log"])


if __name__ == "__main__":
    unittest.main()
