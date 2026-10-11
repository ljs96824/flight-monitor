"""F5：返程阶段呈现措辞合同；普通往返三载体逐字节不变；钉住函数摘要迁移可机械还原。"""

import ast
import copy
import hashlib
import html
import io
import json
import re
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

import notifier
import test_legacy_notification_renderer_retirement as retirement
from tests import test_return_phase_processing as rp


ROOT = Path(__file__).resolve().parents[1]
FROZEN_PAYLOAD = ROOT / "tests" / "fixtures" / "frozen_email" / "economy_payload.json"

TREND_NOTE = "趋势图仅展示返程参考价的历史变化，不作购买判断。"
BUDGET_NA = "不适用（原设定为往返口径）"
FOOTNOTE = "返程阶段不设购买条件，请以支付页最终价、行李和票规为准。"
FINAL_DAY_NOTE = "今天是返程日，也是本条监控的最后有效日：今天之后不再推送本航线行情。"
DEFAULT_MONITORING = "保持本条监控:无需操作,系统会继续盯这条航线,有变化会再次提醒你。"
OLD_TREND_MARK = "可验证区间"
OLD_FOOTNOTE_MARK = "不满足上方条件"
CONTINUE_MARK = "继续盯"
RETURN_HISTORY = (
    {"date": "2026-09-28", "price": 1300.0},
    {"date": "2026-09-29", "price": 1250.0},
    {"date": "2026-09-30", "price": 1200.0},
)
ROUNDTRIP_HISTORY = [
    {"date": "2026-08-01", "price": 2100.0},
    {"date": "2026-08-02", "price": 2050.0},
    {"date": "2026-08-03", "price": 2000.0},
]

# dffd464 上捕获的往返三载体 (字节数, SHA-256)；trend_default 变体走到趋势默认句分支。
ROUNDTRIP_CARRIERS = {
    "frozen": {
        "email": (53307, "1dd353d2b7bcafc02087957ccccb6661478e20472a22fb3e15dae6a56473ee6b"),
        "detail": (54248, "e31b8728b8d0bc05c9bbd4bd99c299aaf6dca7b66674b6c560ec089b1681ffa6"),
        "push": (5552, "2ac0e80d9f82dbe7ebccf524c0832f09d42d916212b6178316b0b7bcd60574ff"),
    },
    "departure_day": {
        "email": (53307, "1dd353d2b7bcafc02087957ccccb6661478e20472a22fb3e15dae6a56473ee6b"),
        "detail": (54248, "e31b8728b8d0bc05c9bbd4bd99c299aaf6dca7b66674b6c560ec089b1681ffa6"),
        "push": (5552, "2ac0e80d9f82dbe7ebccf524c0832f09d42d916212b6178316b0b7bcd60574ff"),
    },
    "trend_default": {
        "email": (53384, "cc452e8ca35200365b0879f8c08a3d362a9cb51ae713b55857ced716203786d7"),
        "detail": (54402, "21a4c7a8950e0f606b418b516053313fc62c537b6b3a3d06b6d84f46af150a72"),
        "push": (5552, "2ac0e80d9f82dbe7ebccf524c0832f09d42d916212b6178316b0b7bcd60574ff"),
    },
}

# F5 之前 (dffd464) 的钉住摘要；把 F5 的辅助函数调用逐字还原后必须精确复现。
PRE_F5_DIGESTS = {
    "render_email": "d877278daa9099b02455fd9e26ad221452ec14b1264da3af380f4d1b0fb5a27d",
    "render_detail_html": "2dbafb52012c71315357a34a5a85138c0456e650e7897b545449322ad21f9aa1",
}
F5_REVERT = (
    ('_budget_field_text(payload, "ideal_price")', '_price_text(payload.get("ideal_price"))',
     {"render_email": 2, "render_detail_html": 1}),
    ('_budget_field_text(payload, "max_price")', '_price_text(payload.get("max_price"))',
     {"render_email": 2, "render_detail_html": 1}),
    ("_budget_field_text(payload, 'ideal_price')", "_price_text(payload.get('ideal_price'))",
     {"render_email": 1, "render_detail_html": 0}),
    ("_budget_field_text(payload, 'max_price')", "_price_text(payload.get('max_price'))",
     {"render_email": 1, "render_detail_html": 0}),
    ("+ _action_card_footnote(payload),",
     "+ \"<div style='margin-top:8px;color:#666;font-size:12px;'>"
     "若支付页最终价、行李和票规不满足上方条件，建议保持本条航线监控。</div>\",",
     {"render_email": 1, "render_detail_html": 1}),
)


def _plain(text: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text)))


def _budget_values(text: str, label: str) -> list[str]:
    return re.findall(re.escape(label) + r"[：:]? (\S+)", _plain(text))


class ReturnPhaseWordingRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rp.ReturnPhaseProcessingTest.setUpClass()

    def setUp(self):
        self.harness = rp.ReturnPhaseProcessingTest("test_return_new_collection")
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def run_return(self, today, hour=16, history=RETURN_HISTORY):
        clock = self.harness.clock

        def now(cls, tz=None):
            value = cls(cls.today.year, cls.today.month, cls.today.day, hour, 0, 0)
            return value if tz is None else value.replace(tzinfo=tz)

        with patch.object(clock, "now", classmethod(now)):
            result = self.harness.run_case(today=today, history=history)
        self.assertTrue(result["ok"], result["stdout"][-2000:])
        payload = result["payloads"][0]
        self.assertEqual(payload["processing_phase"], "return_only")
        carriers = {
            "email": result["emails"][0][2],
            "detail": result["pages"][0][1],
            "push": result["pushes"][0][1],
        }
        return payload, carriers

    def assert_return_budget_and_trend(self, carriers, expected_rows):
        for name in ("email", "detail"):
            text = carriers[name]
            self.assertTrue(TREND_NOTE in text, f"{name}:trend_note")
            self.assertFalse(OLD_TREND_MARK in text, f"{name}:old_trend")
            self.assertFalse("暂无报价" in text, f"{name}:missing_quote_wording")
            self.assertTrue(FOOTNOTE in text, f"{name}:footnote")
            self.assertFalse(OLD_FOOTNOTE_MARK in text, f"{name}:old_footnote")
            for label in ("理想入手价", "最高可接受价"):
                self.assertEqual(_budget_values(text, label), [BUDGET_NA] * expected_rows[name], f"{name}:{label}")
        self.assertFalse(OLD_TREND_MARK in carriers["push"], "push:old_trend")

    def test_regular_return_day(self):
        payload, carriers = self.run_return(date(2026, 10, 2))
        self.assertEqual(payload["days_to_dept"], 4)
        self.assertTrue(payload["recommended_plans"], "candidate_present")
        self.assert_return_budget_and_trend(carriers, {"email": 3, "detail": 1})
        for name in ("email", "detail"):
            self.assertTrue(DEFAULT_MONITORING in carriers[name], f"{name}:default_monitoring")
            self.assertFalse(FINAL_DAY_NOTE in carriers[name], f"{name}:no_final_note")

    def test_day_before_final_keeps_default_note(self):
        payload, carriers = self.run_return(date(2026, 10, 5))
        self.assertEqual(payload["days_to_dept"], 1)
        for name in ("email", "detail"):
            self.assertTrue(DEFAULT_MONITORING in carriers[name], f"{name}:default_monitoring")
            self.assertFalse(FINAL_DAY_NOTE in carriers[name], f"{name}:no_final_note")

    def test_final_valid_day_with_candidates(self):
        payload, carriers = self.run_return(date(2026, 10, 6), hour=9)
        self.assertEqual(payload["days_to_dept"], 0)
        self.assertTrue(payload["recommended_plans"], "candidate_present")
        self.assert_return_budget_and_trend(carriers, {"email": 3, "detail": 1})
        for name in ("email", "detail"):
            self.assertEqual(carriers[name].count(FINAL_DAY_NOTE), 2, f"{name}:final_note")
        for name, text in carriers.items():
            self.assertFalse(CONTINUE_MARK in text, f"{name}:no_continue_claim")

    def test_final_valid_day_without_candidates(self):
        payload, carriers = self.run_return(date(2026, 10, 6), hour=16)
        self.assertEqual(payload["days_to_dept"], 0)
        self.assertEqual(payload["recommended_plans"], [], "departed_candidate_excluded")
        for name in ("email", "detail"):
            self.assertEqual(carriers[name].count(FINAL_DAY_NOTE), 2, f"{name}:final_note")
        for name, text in carriers.items():
            self.assertFalse(CONTINUE_MARK in text, f"{name}:no_continue_claim")


class ReturnPhaseFinalDayFlagTest(unittest.TestCase):
    def test_flag_reads_payload_days_only(self):
        cases = [
            ({"processing_phase": "return_only", "days_to_dept": 0}, True),
            ({"processing_phase": "return_only", "days_to_dept": 1}, False),
            ({"processing_phase": "return_only", "days_to_dept": None}, False),
            ({"processing_phase": "return_only"}, False),
            ({"processing_phase": "return_only", "days_to_dept": False}, False),
            ({"processing_phase": "return_only", "days_to_dept": "0"}, False),
            ({"processing_phase": "return_only", "days_to_dept": 0.0}, False),
            ({"days_to_dept": 0}, False),
            ({"processing_phase": "roundtrip", "days_to_dept": 0}, False),
        ]
        with patch.object(notifier, "shanghai_today", side_effect=AssertionError("clock_read")):
            for payload, expected in cases:
                self.assertIs(notifier._return_phase_final_day(payload), expected, repr(payload))
                note = notifier._monitoring_note(payload)
                self.assertEqual(note, FINAL_DAY_NOTE if expected else DEFAULT_MONITORING, repr(payload))

    def test_wording_constants(self):
        self.assertEqual(notifier.RETURN_PHASE_TREND_NOTE, TREND_NOTE)
        self.assertEqual(notifier.RETURN_PHASE_BUDGET_FIELD_TEXT, BUDGET_NA)
        self.assertEqual(notifier.RETURN_PHASE_ACTION_FOOTNOTE, FOOTNOTE)
        self.assertEqual(notifier.RETURN_PHASE_FINAL_DAY_MONITORING_NOTE, FINAL_DAY_NOTE)


class RoundTripCarriersUnchangedTest(unittest.TestCase):
    def test_roundtrip_carriers_are_byte_identical(self):
        payload = json.loads(FROZEN_PAYLOAD.read_text(encoding="utf-8"))
        variants = {
            "frozen": payload,
            "departure_day": {**copy.deepcopy(payload), "days_to_dept": 0},
            "trend_default": {**copy.deepcopy(payload), "trend_summary": "", "price_history": ROUNDTRIP_HISTORY},
        }
        actual = {}
        for name, item in variants.items():
            with redirect_stdout(io.StringIO()):
                _, email = notifier.render_email(copy.deepcopy(item))
                detail = notifier.render_detail_html(copy.deepcopy(item))
                push = notifier.prepare_push_render(notifier.render_pushplus_sections(copy.deepcopy(item))).content
            actual[name] = {
                carrier: (len(text.encode("utf-8")), hashlib.sha256(text.encode("utf-8")).hexdigest())
                for carrier, text in (("email", email), ("detail", detail), ("push", push))
            }
        self.assertEqual(actual, ROUNDTRIP_CARRIERS)


class PinnedDigestMigrationTest(unittest.TestCase):
    def test_reverting_f5_helper_calls_restores_pre_f5_digests(self):
        tree, source = retirement._notifier_tree_and_source()
        for name, pre_digest in PRE_F5_DIGESTS.items():
            segment = ast.get_source_segment(source, retirement._module_function(tree, name))
            self.assertEqual(hashlib.sha256(segment.encode()).hexdigest(), retirement.EXPECTED_MAIN_CHAIN_SHA256[name])
            reverted = segment
            for new, old, counts in F5_REVERT:
                self.assertEqual(reverted.count(new), counts[name], f"{name}:{new}")
                reverted = reverted.replace(new, old)
            self.assertEqual(hashlib.sha256(reverted.encode()).hexdigest(), pre_digest, f"{name}:revert")


if __name__ == "__main__":
    unittest.main()
