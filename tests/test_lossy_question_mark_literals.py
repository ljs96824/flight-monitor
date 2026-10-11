"""F12：问号丢字常量恢复；返程无候选推送不再渲染空首选卡。

问号丢字与 F11 乱码不同：中文在写入时已被替换为 '?'，没有可逆映射，只能依同源证据恢复。
证据：5401e11 同批写入的 analyzer.normalize_budget_scope 别名集合完好
("整单", "全员", "全部人")，main.py 的丢字集合与之逐项同形；"方案A" 见 5401e11 父提交原文。
"""

import ast
import copy
import hashlib
import io
import json
import pathlib
import unittest
from contextlib import redirect_stdout
from datetime import date

import analyzer
import main
import notifier
import web_form
from tests import test_return_phase_processing as rp


ROOT = pathlib.Path(__file__).resolve().parents[1]
FROZEN_PAYLOAD = ROOT / "tests" / "fixtures" / "frozen_email" / "economy_payload.json"

# c96d4ac 上捕获：往返无候选变体三载体不变；返程有候选推送不变；返程无候选推送为修正后的新值。
ROUNDTRIP_NO_CANDIDATE = {
    "email": (51693, "5aeec15a842572f1f9b97ae029ca4497bea4abefe8c10b18950c494b7ba61983"),
    "detail": (39995, "13198941a830dd8499df9438274b53612d1626103a6ca565eeff38423474bf93"),
    "push": (1046, "5caf31ce2ea52ba619e3c18c428ae0365829a2017d2238d1f521d543898fe164"),
}
RETURN_WITH_CANDIDATE_PUSH = (800, "470c6afd53605826d765673d968f80f0d3b406afd2c385b7f6c810d1692e9f21")
RETURN_NO_CANDIDATE_PUSH = (486, "1da2c6efcc79f60f3bd7e09b2ccc78f970c96de5576d00638c7578198f4d0ee0")


def _production_files() -> list[pathlib.Path]:
    files = sorted(ROOT.glob("*.py")) + sorted((ROOT / "scripts").glob("*.py"))
    return [p for p in files if not p.name.startswith(("test_", "conftest"))]


def _digest(text: str) -> tuple[int, str]:
    data = text.encode("utf-8")
    return len(data), hashlib.sha256(data).hexdigest()


class LossyQuestionMarkGuardTest(unittest.TestCase):
    def test_production_string_constants_have_no_question_mark_runs(self):
        hits = []
        for path in _production_files():
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and "??" in node.value:
                    hits.append((path.relative_to(ROOT).as_posix(), node.lineno, node.value[:40]))
        self.assertEqual(hits, [])


class PriceScopeAliasRestorationTest(unittest.TestCase):
    PROBES = (
        None, "", "per_person", " ALL ", "all", "total", "all_passengers", "all_passenger", "overall",
        "整单", "全员", "全部人", "单人", "??", "???",
    )

    def test_main_matches_analyzer_same_batch_aliases(self):
        for value in self.PROBES:
            self.assertEqual(main._normalize_price_scope(value), analyzer.normalize_budget_scope(value), repr(value))

    def test_restored_aliases(self):
        for value in ("整单", "全员", "全部人"):
            self.assertEqual(main._normalize_price_scope(value), "all", value)
        for value in ("整单", "全员"):
            self.assertEqual(web_form.normalize_price_scope(value), "all", value)
        for value in (None, "", "per_person", "单人", "??", "???"):
            self.assertEqual(main._normalize_price_scope(value), "per_person", repr(value))
            self.assertEqual(web_form.normalize_price_scope(value), "per_person", repr(value))
        for value in ("all", "total", "all_passengers", "all_passenger", " ALL "):
            self.assertEqual(web_form.normalize_price_scope(value), "all", value)


class PlanBriefLinesTest(unittest.TestCase):
    def test_empty_plan_renders_no_card(self):
        for payload in ({}, {"recommended_plans": []}, {"recommended_plans": [{}]}, {"recommended_plans": [None]}):
            self.assertEqual(notifier._pushplus_plan_brief_lines(payload), [], repr(payload))

    def test_missing_label_falls_back_to_plan_a(self):
        with redirect_stdout(io.StringIO()):
            lines = notifier._pushplus_plan_brief_lines({"recommended_plans": [{"price": 1200, "main_push_line": "MU102"}]})
        self.assertEqual(lines[0], "方案A | 当前最优候选")


class ReturnPhaseNoCandidatePushTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rp.ReturnPhaseProcessingTest.setUpClass()

    def setUp(self):
        self.harness = rp.ReturnPhaseProcessingTest("test_return_new_collection")
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def push_for(self, today):
        result = self.harness.run_case(today=today)
        self.assertTrue(result["ok"], result["stdout"][-2000:])
        payload = result["payloads"][0]
        with redirect_stdout(io.StringIO()):
            render = notifier.render_pushplus_sections(payload)
            content = notifier.prepare_push_render(render).content
        return payload, {section.section_id: section.html for section in render.sections}, content

    def test_no_candidate_return_push_has_no_primary_card(self):
        payload, sections, content = self.push_for(date(2026, 10, 6))
        self.assertEqual(payload["recommended_plans"], [], "departed_candidate_excluded")
        self.assertEqual(sections["primary_plan"], "")
        self.assertTrue("当前约束下未找到符合条件的返程方案" in content, "no_candidate_reason")
        self.assertFalse("当前最优候选" in content, "no_phantom_primary")
        self.assertFalse("??" in content, "no_question_marks")
        self.assertEqual(_digest(content), RETURN_NO_CANDIDATE_PUSH)

    def test_candidate_return_push_unchanged(self):
        payload, sections, content = self.push_for(date(2026, 10, 2))
        self.assertTrue(payload["recommended_plans"], "candidate_present")
        self.assertTrue(sections["primary_plan"].startswith("方案A | 当前最优候选"), "primary_card_kept")
        self.assertEqual(_digest(content), RETURN_WITH_CANDIDATE_PUSH)


class RoundTripNoCandidateCarriersTest(unittest.TestCase):
    def test_roundtrip_no_candidate_carriers_unchanged(self):
        payload = json.loads(FROZEN_PAYLOAD.read_text(encoding="utf-8"))
        item = {**payload, "recommended_plans": [], "alternative_plans": []}
        with redirect_stdout(io.StringIO()):
            _, email = notifier.render_email(copy.deepcopy(item))
            detail = notifier.render_detail_html(copy.deepcopy(item))
            push = notifier.prepare_push_render(notifier.render_pushplus_sections(copy.deepcopy(item))).content
        actual = {"email": _digest(email), "detail": _digest(detail), "push": _digest(push)}
        self.assertEqual(actual, ROUNDTRIP_NO_CANDIDATE)


if __name__ == "__main__":
    unittest.main()
