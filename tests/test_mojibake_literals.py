"""F11a：生产源码乱码字面量守卫与可达文案还原合同。

乱码成因：UTF-8 字节被按 Windows CP936 解码后再以 UTF-8 保存。
还原映射：私用区字符按 GB18030 回编码，U+20AC 回编码为 0x80，其余按 GBK 回编码，再按 UTF-8 解码。
"""

from __future__ import annotations

import ast
import collections
import pathlib
import re
import unittest
from datetime import date
from unittest.mock import patch

import analyzer
import price_calendar

ROOT = pathlib.Path(__file__).resolve().parents[1]
NON_ASCII_RUN = re.compile(r"[^\x00-\x7f]+")
PUA_LO = chr(0xE000)
PUA_HI = chr(0xF8FF)
EURO = chr(0x20AC)
REPLACEMENT = chr(0xFFFD)

# F11a 之后仍保留的乱码字面量：仅限不可达旧代码（待 F11c 删除）与判定集合（待 F11b 裁决）。
# 键为 (文件, 最内层函数/类名或模块级赋值名)，值为该作用域内含乱码的字符串常量节点数。
ALLOWED_RESIDUAL = {
    ("analyzer.py", "FULL_SERVICE_AIRLINES"): 3,
    ("analyzer.py", "LCC_AIRLINES"): 2,
    ("analyzer.py", "TIME_SLOT_LABELS"): 6,
    ("analyzer.py", "airline_competition_analysis"): 2,
    ("analyzer.py", "detect_anomaly"): 4,
    ("analyzer.py", "nearby_dates_comparison"): 8,
    ("analyzer.py", "timing_analysis"): 1,
    ("analyzer.py", "weekday_analysis"): 8,
    ("notifier.py", "_append_push_reason_section"): 2,
    ("notifier.py", "_freshness_label"): 4,
    ("notifier.py", "_min_date"): 1,
    ("notifier.py", "_price_discrepancy_notice"): 1,
    ("notifier.py", "_round_trip_price_estimate_line"): 4,
    ("notifier.py", "_route_is_domestic"): 17,
    ("notifier.py", "_service_info_lines"): 3,
}


def _is_pua(ch: str) -> bool:
    return PUA_LO <= ch <= PUA_HI


def _to_cp936_bytes(run: str) -> bytes:
    out = bytearray()
    for ch in run:
        if _is_pua(ch):
            out += ch.encode("gb18030")
        elif ch == EURO:
            out += b"\x80"
        else:
            out += ch.encode("gbk")
    return bytes(out)


def is_mojibake_run(run: str) -> bool:
    if any(_is_pua(ch) or ch == REPLACEMENT for ch in run):
        return True
    data = bytearray()
    for ch in run:
        if ch == EURO:
            data += b"\x80"
            continue
        try:
            data += ch.encode("gbk")
        except UnicodeEncodeError:
            data += b"?"
    decoded = bytes(data).decode("utf-8", errors="replace")
    bad = decoded.count(REPLACEMENT)
    good = sum(1 for ch in decoded if ch != REPLACEMENT and ord(ch) >= 0x2000)
    return good >= 1 and bad <= max(1, len(decoded) // 3) and len(decoded) < len(run)


def _production_files() -> list[pathlib.Path]:
    files = sorted(ROOT.glob("*.py")) + sorted((ROOT / "scripts").glob("*.py"))
    return [p for p in files if not p.name.startswith(("test_", "conftest"))]


def _scope_index(tree: ast.Module) -> list[tuple[int, int, str, int]]:
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            spans.append((node.lineno, node.end_lineno, node.name, 0))
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            name = getattr(target, "id", "<module>")
            spans.append((node.lineno, node.end_lineno, name, 1))
    return spans


def _scope_of(spans, lineno: int) -> str:
    best = None
    for start, end, name, _ in spans:
        if start <= lineno <= end and (best is None or start >= best[0]):
            best = (start, name)
    return best[1] if best else "<module>"


def mojibake_inventory() -> collections.Counter:
    counter: collections.Counter = collections.Counter()
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        spans = _scope_index(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if any(is_mojibake_run(m.group()) for m in NON_ASCII_RUN.finditer(node.value)):
                    counter[(path.relative_to(ROOT).as_posix(), _scope_of(spans, node.lineno))] += 1
    return counter


def _function_node(module_path: pathlib.Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(module_path.read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"未找到函数 {name}")


def _string_constants(node: ast.AST) -> set[str]:
    return {
        sub.value
        for sub in ast.walk(node)
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
    }


class MojibakeDetectorSelfTest(unittest.TestCase):
    def test_detector_flags_known_mojibake_and_spares_normal_chinese(self):
        samples = ["鎻愬墠", "鎺ㄨ繜", "缁煎悎璇勫垎", "鍒拌揪鏈" + EURO + "蹇"]
        for clean in ("国际航线票规可能存在渠道差异", "当前价格或方案状态触发了你的监控条件"):
            raw = clean.encode("utf-8")
            samples.append(raw.decode("gbk", errors="replace"))
            samples.append(raw.decode("gb18030", errors="replace"))
        for text in samples:
            runs = [m.group() for m in NON_ASCII_RUN.finditer(text)]
            self.assertTrue(any(is_mojibake_run(run) for run in runs), text)
        for text in ("提前", "推迟", "渲染", "中国国际航空", "春秋航空", "周一", "上海"):
            self.assertFalse(is_mojibake_run(text), text)


class MojibakeResidualGuardTest(unittest.TestCase):
    def test_residual_mojibake_matches_allowlist_exactly(self):
        self.assertEqual(dict(mojibake_inventory()), ALLOWED_RESIDUAL)


class RestoredReachableTextTest(unittest.TestCase):
    def test_roundtrip_row_savings_direction_is_readable(self):
        rows = [
            {"date": "2026-12-10", "min_price": 3000, "selected": True, "scope": "roundtrip"},
            {"date": "2026-12-08", "min_price": 2500, "scope": "roundtrip"},
            {"date": "2026-12-12", "min_price": 2600, "scope": "roundtrip"},
        ]
        with patch("price_calendar.shanghai_today", return_value=date(2026, 12, 1)):
            tips = [item["tip"] for item in price_calendar.analyze_row_savings(rows, "2026-12-10")]
        self.assertEqual(
            tips,
            [
                "提前2天(2026-12-08 周二)出发，省¥500/往返",
                "推迟2天(2026-12-12 周六)出发，省¥400/往返",
            ],
        )

    def test_analyze_all_flights_recommendation_text_is_readable(self):
        node = _function_node(ROOT / "analyzer.py", "analyze_all_flights")
        by_tag = {}
        for sub in ast.walk(node):
            if isinstance(sub, ast.Dict):
                keys = [k.value for k in sub.keys if isinstance(k, ast.Constant)]
                if "tag" in keys and "desc" in keys:
                    values = {
                        k.value: v.value
                        for k, v in zip(sub.keys, sub.values)
                        if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
                    }
                    by_tag[values["tag"]] = (values["desc"], values["reason"])
        self.assertEqual(by_tag["赶时间选这个"], ("到达最快，价格稍高", "到达最快，价格稍高"))
        self.assertEqual(by_tag["怕折腾选这个"], ("转机最轻松，不用在机场过夜", "转机最轻松，不用在机场过夜"))

    def test_round_trip_insight_text_is_readable(self):
        constants = _string_constants(_function_node(ROOT / "analyzer.py", "analyze_round_trip"))
        for text in ("去程好价但返程偏贵，总价¥", "返程好价但去程偏贵，总价¥", "去程和返程价格相对均衡，总价¥"):
            self.assertTrue(text in constants, text)

    def test_reachable_docstrings_are_readable(self):
        self.assertEqual(analyzer.price_position_description.__doc__.strip(), "用历史数据计算当前价格的位置描述")
        self.assertEqual(analyzer.waiting_risk_description.__doc__.strip(), "计算继续等待一周的风险收益")
        self.assertEqual(analyzer.overall_score.__doc__.strip(), "综合评分 0-10")


if __name__ == "__main__":
    unittest.main()
