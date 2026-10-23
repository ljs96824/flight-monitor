"""R14：篮子哨兵的轮档证据按完整 round_id 配对并校验日期归属，不完整的证据不再算作"已运行"。

基线(R13 合并后的 main)：basket_sentinel.read_basket_round_evidence(basket_sentinel.py:34)遇到篮子结束标记就直接建轮次项
(:56–60)，即使此前没有同一 round_id 的开始标记；也不核对 round_id 中的日期是否等于当日轮档的日期。classify_basket_rounds
(:68)只看结束状态，于是"只有 status=ok 的结束标记"与"当日日文件里放着前一天的完整轮次"都被分类为 ran，哨兵不报。
正常写入时轮档文件名、round_id 与 started_at 取自同一个时刻(log_utils.start_round_log_archive、basket_collect.make_round_id)，
二者不一致只可能来自截断、拷贝或手工改动。
口径(用户裁定 2026-10-23，采纳建议)：每个轮次另记 start_seen(结束标记之前见过同一 round_id 的开始标记)与 day_matches
(round_id 中的日期等于当日)；缺任一项的轮次不能证明当日运行。没有证据完整的 ok/partial 轮次时：有证据完整的轮次则按其中
最后一个判 blocked/abnormal(不变)；只有不完整的轮次则判 incomplete，标题"今日篮子运行证据不完整"，正文写明轮次与原因。
只有开始没有结束仍判"未正常结束"；不补写轮档、不伪造结束状态；"较早一次成功即算已运行"(earlier_ok_then_blocked)与
"台账条目优先"(ledger_entry_wins)两条既有政策不变；不含新字段的旧格式证据按完整处理。
独立复核补充(同一裁定内)：print 经 log_utils._Utf8TeeStream 把正文与换行分两次写入、各自刷盘，上一个写入者在两次之间被终止时文件以残行结尾，
下一轮的开始标记会接在残行后面；解析先在标记起点切开、再按整段匹配，不把这种正常运行误报为证据不完整，也不把行内引用的
标记文字当作标记。原因行只归属到尚未见到自己结束标记的那个轮次，其他轮次的结束标记不再清空它。
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
TODAY = "basket_20261018T093000"
LATER = "basket_20261018T120000"
YESTERDAY = "basket_20261017T093000"


def start(round_id, started_at="2026-10-18T09:30:00"):
    return f"===== [轮档开始] round_id={round_id} started_at={started_at} ====="


def end(round_id, status):
    return f"===== [轮档结束] round_id={round_id} status={status} ====="


def write_raw(root: Path, lines):
    path = root / "20261018.log"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def write_real(root: Path, rounds):
    """用真实的轮档写入函数生成当日轮档；rounds 为 (round_id, 结束状态)。"""
    console = io.StringIO()
    private_sys = SimpleNamespace(stdout=console, stderr=io.StringIO())
    with (
        patch.object(log_utils, "sys", private_sys),
        patch.object(log_utils, "_round_log_state", None),
        patch.object(log_utils, "print", lambda *a, **k: print(*a, file=private_sys.stdout, **k), create=True),
    ):
        for round_id, status in rounds:
            log_utils.start_round_log_archive(round_id, root_dir=root, now=datetime(2026, 10, 18, 9, 30))
            log_utils.end_round_log_archive(status=status)
    return root / "20261018.log"


def classify_text(lines, usage=NO_LEDGER, now=AFTER):
    with tempfile.TemporaryDirectory() as tmp:
        path = write_raw(Path(tmp), lines)
        before = path.read_bytes()
        evidence = sentinel.read_basket_round_evidence(Path(tmp), DAY)
        unchanged = path.read_bytes() == before
    result = sentinel.evaluate_basket_sentinel(usage, now=now, threshold="20:00", round_evidence=evidence)
    return evidence, result, unchanged


class RoundPairingParseTest(unittest.TestCase):
    def test_real_archive_rounds_are_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_real(Path(tmp), [(TODAY, "ok")])
            evidence = sentinel.read_basket_round_evidence(Path(tmp), DAY)
        self.assertEqual(evidence["rounds"], [{"round_id": TODAY, "status": "ok", "reason": "",
                                               "start_seen": True, "day_matches": True}])

    def test_pairing_and_day_flags(self):
        cases = (
            ("orphan_end", [end(TODAY, "ok")], [(TODAY, "ok", False, True)]),
            ("end_before_start", [end(TODAY, "ok"), start(TODAY)], [(TODAY, "ok", False, True)]),
            ("previous_day_round", [start(YESTERDAY, "2026-10-17T09:30:00"), end(YESTERDAY, "ok")],
             [(YESTERDAY, "ok", True, False)]),
            ("undated_round_id", [start("basket_manual"), end("basket_manual", "ok")], [("basket_manual", "ok", True, False)]),
            ("start_only", [start(TODAY)], [(TODAY, None, True, True)]),
            ("non_basket_orphan_ignored", [end("collection_20261018T090000", "ok")], []),
        )
        for name, lines, expected in cases:
            with self.subTest(case=name):
                evidence, _result, unchanged = classify_text(lines)
                self.assertEqual([(item["round_id"], item["status"], item["start_seen"], item["day_matches"])
                                  for item in evidence["rounds"]], expected)
                self.assertTrue(unchanged, "ARCHIVE_READ_ONLY")


class RoundPairingClassificationTest(unittest.TestCase):
    CASES = (
        # 名称, 轮档行, 期望(due, run_class, 选中的 round_id)
        ("orphan_end_ok", [end(TODAY, "ok")], (True, "incomplete", TODAY)),
        ("orphan_end_partial", [end(TODAY, "partial")], (True, "incomplete", TODAY)),
        ("end_before_start", [end(TODAY, "ok"), start(TODAY)], (True, "incomplete", TODAY)),
        ("previous_day_complete_round", [start(YESTERDAY, "2026-10-17T09:30:00"), end(YESTERDAY, "ok")],
         (True, "incomplete", YESTERDAY)),
        ("undated_round_id", [start("basket_manual"), end("basket_manual", "ok")], (True, "incomplete", "basket_manual")),
        ("complete_ok_then_orphan_blocked", [start(TODAY), end(TODAY, "ok"), end(LATER, "blocked")], (False, "ran", TODAY)),
        ("complete_blocked_then_orphan_ok", [start(TODAY), end(TODAY, "blocked"), end(LATER, "ok")],
         (True, "blocked", TODAY)),
        ("orphan_ok_then_complete_interrupted", [end(TODAY, "ok"), start(LATER), end(LATER, "interrupted")],
         (True, "abnormal", LATER)),
        ("start_only_still_abnormal", [start(TODAY)], (True, "abnormal", TODAY)),
        ("complete_ok_unchanged", [start(TODAY), end(TODAY, "ok")], (False, "ran", TODAY)),
        ("earlier_ok_then_blocked_unchanged", [start(TODAY), end(TODAY, "ok"), start(LATER), end(LATER, "blocked")],
         (False, "ran", TODAY)),
        ("no_basket_round", [end("collection_20261018T090000", "ok")], (True, "none", None)),
    )

    def test_classification_after_threshold(self):
        for name, lines, (due, run_class, round_id) in self.CASES:
            with self.subTest(case=name):
                _evidence, result, _unchanged = classify_text(lines)
                self.assertEqual((result["due"], result["run_class"]), (due, run_class))
                self.assertEqual((result["basket_round"] or {}).get("round_id"), round_id)
                expected_reason = sentinel._DUE_REASONS.get(run_class, "") if due else ""
                self.assertEqual(result["reason"], expected_reason)

    def test_never_due_before_threshold(self):
        for name, lines, _expected in self.CASES:
            with self.subTest(case=name):
                _evidence, result, _unchanged = classify_text(lines, now=BEFORE)
                self.assertFalse(result["due"])

    def test_ledger_entry_still_wins_over_incomplete_evidence(self):
        ledger = {"entries": [{"round_id": TODAY, "recorded_at": "2026-10-18T09:31:00"}]}
        _evidence, result, _unchanged = classify_text([end(TODAY, "ok")], usage=ledger)
        self.assertEqual((result["due"], result["run_class"]), (False, "ledger"))

    def test_evidence_without_pairing_fields_keeps_previous_classification(self):
        legacy = {"readable": True, "rounds": [{"round_id": "basket_a", "status": "ok", "reason": ""},
                                               {"round_id": "basket_b", "status": "blocked", "reason": ""}]}
        self.assertEqual(sentinel.classify_basket_rounds(legacy)[0], "ran")
        self.assertEqual(sentinel.classify_basket_rounds({"readable": True, "rounds": [
            {"round_id": "basket_b", "status": "blocked", "reason": ""}]})[0], "blocked")


class DamagedLineTest(unittest.TestCase):
    def test_marker_glued_after_a_broken_line_still_pairs(self):
        cases = (
            ("writer_killed_mid_line", ["[采集] 正在请求 PVG->KIX 2026-11-0" + start(TODAY), end(TODAY, "ok")], TODAY),
            ("power_loss_nul_padding", ["\x00\x00\x00" + start(TODAY), end(TODAY, "ok")], TODAY),
            ("end_marker_without_newline", [start(TODAY), end(TODAY, "blocked") + start(LATER), end(LATER, "ok")], LATER),
        )
        for name, lines, ran_round in cases:
            with self.subTest(case=name):
                evidence, result, unchanged = classify_text(lines)
                self.assertTrue(all(item["start_seen"] and item["day_matches"] for item in evidence["rounds"]))
                self.assertEqual((result["due"], result["run_class"], result["basket_round"]["round_id"]),
                                 (False, "ran", ran_round), "NORMAL_RUN_NOT_FLAGGED")
                self.assertTrue(unchanged)

    def test_quoted_marker_text_inside_a_line_is_not_a_marker(self):
        quoted = "    f\"===== [轮档结束] round_id={state['round_id']} status={status} =====\""
        evidence, result, _unchanged = classify_text([
            start(TODAY), quoted, "[篮子跳过] 原因=研究采样硬门未通过", end(TODAY, "blocked")])
        self.assertEqual(evidence["rounds"], [{"round_id": TODAY, "status": "blocked",
                                               "reason": "[篮子跳过] 原因=研究采样硬门未通过",
                                               "start_seen": True, "day_matches": True}])
        self.assertEqual((result["due"], result["run_class"]), (True, "blocked"))

    def test_reason_belongs_to_the_open_round_not_to_an_orphan_end(self):
        lines = [start(TODAY), end(LATER, "ok"), "[篮子跳过] 原因=研究采样硬门未通过", end(TODAY, "blocked")]
        evidence, result, _unchanged = classify_text(lines)
        self.assertEqual((result["due"], result["run_class"], result["basket_round"]["round_id"]),
                         (True, "blocked", TODAY))
        self.assertEqual(result["basket_round"]["reason"], "[篮子跳过] 原因=研究采样硬门未通过")
        title, content = sentinel._sentinel_message(result, evidence, "")
        self.assertIn("原因：[篮子跳过] 原因=研究采样硬门未通过", content)
        self.assertNotIn("轮档未记录", content)


class IncompleteEvidenceMessageTest(unittest.TestCase):
    def run_sentinel(self, lines):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            write_raw(Path(tmp), lines)
            evidence = sentinel.read_basket_round_evidence(Path(tmp), DAY)
            with redirect_stdout(io.StringIO()):
                result = sentinel.run_basket_sentinel(
                    usage_payload=NO_LEDGER, now=AFTER, threshold="20:00",
                    state_path=Path(tmp) / "basket_sentinel.json", round_evidence=evidence,
                    notifier=lambda title, content: calls.append((title, content)) or True)
        return result, calls

    def test_title_and_content_name_the_round_and_the_gap(self):
        cases = (
            ("orphan_end", [end(TODAY, "ok")], [TODAY, "缺少开始标记", "结束状态=ok"]),
            ("previous_day", [start(YESTERDAY, "2026-10-17T09:30:00"), end(YESTERDAY, "ok")],
             [YESTERDAY, "日期与当日轮档不符", "结束状态=ok"]),
            ("previous_day_without_end", [start(YESTERDAY, "2026-10-17T09:30:00")],
             [YESTERDAY, "日期与当日轮档不符", "结束状态=未见结束标记"]),
        )
        for name, lines, fragments in cases:
            with self.subTest(case=name):
                result, calls = self.run_sentinel(lines)
                self.assertEqual((result["status"], len(calls)), ("notified", 1))
                self.assertEqual(calls[0][0], "[篮子哨兵] 今日篮子运行证据不完整")
                for fragment in (*fragments, "不能作为今日运行证据", "请查看当日轮档与 basket.log"):
                    self.assertIn(fragment, calls[0][1])


class MainIncompleteEvidenceTest(unittest.TestCase):
    def test_main_reports_incomplete_evidence_from_today_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "rounds").mkdir()
            write_raw(root / "rounds", [end(TODAY, "ok")])
            alerts = []
            with (
                patch.object(main, "ROUND_LOG_ROOT", root / "rounds"),
                patch.object(main, "BASKET_SENTINEL_STATE_PATH", root / "basket_sentinel.json"),
                patch.object(main, "_basket_run_expectation_for_main", return_value={"expected": True, "reason": ""}),
                patch.object(main, "load_usage_strict", return_value=NO_LEDGER),
                patch.object(main, "_notify_system_alert", side_effect=lambda subs, title, content:
                             alerts.append((title, content)) or True),
                redirect_stdout(io.StringIO()),
            ):
                result = main._run_basket_sentinel_for_main([], now=AFTER)
        self.assertEqual((result["run_class"], result["status"]), ("incomplete", "notified"))
        self.assertEqual(alerts[0][0], "[篮子哨兵] 今日篮子运行证据不完整")
        self.assertIn(TODAY, alerts[0][1])


if __name__ == "__main__":
    unittest.main()
