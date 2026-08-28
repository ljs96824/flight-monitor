import ast
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MANUAL_RENDERER_DEBT = frozenset()


def _tracked_python_files():
    paths = subprocess.check_output(
        ["git", "ls-files", "*.py"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
    ).splitlines()
    return [ROOT / path for path in paths]


def _symbol_references(symbol):
    references = []
    for path in _tracked_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            kind = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "definition" if node.name == symbol else None
            elif isinstance(node, ast.ImportFrom):
                kind = "from-import" if any(alias.name == symbol for alias in node.names) else None
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                kind = "name-load" if node.id == symbol else None
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                kind = "attribute-load" if node.attr == symbol else None
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                kind = "string-reference" if symbol in node.value else None
            if kind:
                references.append(
                    (path.relative_to(ROOT).as_posix(), node.lineno, kind)
                )
    return references


def _direct_calls(function_name):
    source = (ROOT / "notifier.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    return {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


class OrphanedRendererCallGraphContractTest(unittest.TestCase):
    def test_removed_round_trip_block_has_no_code_or_dynamic_reference(self):
        symbol = "_append_round_trip" + "_block"
        self.assertEqual(_symbol_references(symbol), [])

    def test_retired_legacy_renderer_chain_is_absent_and_diagnostic_is_migrated(self):
        from scripts.check_f821 import KNOWN_F821_DEBT

        notifier_source = (ROOT / "notifier.py").read_text(encoding="utf-8")
        notifier_tree = ast.parse(notifier_source)
        retired = {
            "_format_structured_html_" + "message",
            "_append_detailed_analysis_" + "section",
        }
        definitions = {
            node.name
            for node in ast.walk(notifier_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name in retired
        }
        loads = {
            node.id
            for node in ast.walk(notifier_tree)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in retired
        }
        self.assertEqual(definitions, set())
        self.assertEqual(loads, set())
        self.assertEqual(MANUAL_RENDERER_DEBT, frozenset())

        test_full = ast.parse((ROOT / "test_full.py").read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(test_full)
            if isinstance(node, ast.ImportFrom) and node.module == "notifier"
            for alias in node.names
        }
        self.assertTrue(
            {
                "build_notification_payload",
                "render_email",
                "render_detail_html",
                "render_pushplus_sections",
                "format_html_message",
            }
            <= imports
        )
        self.assertFalse(MANUAL_RENDERER_DEBT & KNOWN_F821_DEBT)


if __name__ == "__main__":
    unittest.main()
