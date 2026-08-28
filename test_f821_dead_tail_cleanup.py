import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _find_module_function(path: Path, function: str):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    matches = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected one module function {function}, found {len(matches)}")
    return source, matches[0]


class DeadTailStructureContractTest(unittest.TestCase):
    def test_format_html_message_is_a_deterministic_raise_stub(self):
        import notifier

        source, function_node = _find_module_function(
            ROOT / "notifier.py", "format_html_message"
        )
        statements = function_node.body
        self.assertEqual(len(statements), 2)
        self.assertIsInstance(statements[0], ast.Expr)
        self.assertIsInstance(statements[1], ast.Raise)
        self.assertIn(
            "LegacyNotificationRendererUnavailable",
            ast.get_source_segment(source, statements[1]),
        )

    def test_format_html_message_rejects_all_old_compaction_modes(self):
        import notifier

        for enforce_limit in (False, True):
            with self.subTest(enforce_pushplus_limit=enforce_limit):
                with self.assertRaises(notifier.LegacyNotificationRendererUnavailable):
                    notifier.format_html_message(
                        analysis_result={"text": "x" * 30001},
                        detail_level="short",
                        enforce_pushplus_limit=enforce_limit,
                    )


if __name__ == "__main__":
    unittest.main()
