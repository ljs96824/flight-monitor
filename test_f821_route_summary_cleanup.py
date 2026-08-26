import ast
import contextlib
import copy
import inspect
import io
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class RouteSummaryRenameContractTest(unittest.TestCase):
    def test_current_helper_preserves_the_renamed_route_summary_contract(self):
        from notifier import format_route_summary

        signature = inspect.signature(format_route_summary)
        self.assertEqual(list(signature.parameters), ["route_summary"])
        self.assertIs(signature.parameters["route_summary"].default, inspect.Parameter.empty)

        cases = [
            (None, ""),
            ("", ""),
            ("PVG → KIX", "上海浦东(PVG) → 关西国际机场(KIX)"),
            ("PVG-KIX via HND", "上海浦东(PVG)-关西国际机场(KIX) via 东京羽田(HND)"),
            ("already lower pvg", "already lower pvg"),
        ]
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            for raw, expected in cases:
                result = format_route_summary(raw)
                self.assertIsInstance(result, str)
                self.assertEqual(result, expected)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")

    def test_alternative_message_uses_the_current_route_summary_helper(self):
        from notifier import format_alternative_message

        analysis = {
            "target_combo": "MU225",
            "current_price": 2000,
            "cheapest_alt": {
                "flight_combo": "JL891",
                "price": 1500,
                "route_summary": "PVG → KIX",
                "duration_hours": 3.5,
            },
        }
        before = copy.deepcopy(analysis)
        expected = "\n".join(
            [
                "发现更便宜的航线方案",
                "",
                "当前关注：MU225，¥2,000",
                "替代方案：JL891，¥1,500",
                "价差：¥500",
                "路线：上海浦东(PVG) → 关西国际机场(KIX)",
                "总时长：3小时30分钟",
                "",
                "---",
                "以上内容基于历史价格数据分析，仅供参考。",
                "实际购买请以航司或OTA官网价格为准。",
            ]
        )

        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            rendered = format_alternative_message(analysis)

        self.assertEqual(rendered, expected)
        self.assertEqual(analysis, before)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")

    def test_test_module_main_block_only_calls_defined_tests(self):
        path = ROOT / "test_price_policy_email.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        main_calls = set()
        for node in tree.body:
            if not isinstance(node, ast.If):
                continue
            if not (
                isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "__name__"
            ):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                    main_calls.add(child.func.id)

        self.assertTrue(main_calls)
        self.assertEqual(main_calls - defined, set())


if __name__ == "__main__":
    unittest.main()
