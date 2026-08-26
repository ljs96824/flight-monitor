import ast
import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
ACTIVE_PREFIX_SHA256 = "b3868a6da7ad7c54d5f9dc74a51a46683c9582ac78bebed7fcfea0cd4dec63eb"


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


def _outer_scope_yields(function_node):
    found = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            if node is function_node:
                self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node):
            if node is function_node:
                self.generic_visit(node)

        def visit_ClassDef(self, node):
            return

        def visit_Lambda(self, node):
            return

        def visit_Yield(self, node):
            found.append(node.lineno)

        def visit_YieldFrom(self, node):
            found.append(node.lineno)

    Visitor().visit(function_node)
    return found


def assert_no_statements_after_terminal_return(
    module="notifier.py", function="format_html_message"
):
    path = ROOT / module
    source, function_node = _find_module_function(path, function)
    direct_returns = [
        (index, node)
        for index, node in enumerate(function_node.body)
        if isinstance(node, ast.Return)
    ]
    if not direct_returns:
        raise AssertionError(f"{function} has no direct terminal return")
    return_index, return_node = direct_returns[0]
    trailing = function_node.body[return_index + 1 :]
    if trailing:
        locations = [f"{type(node).__name__}@{node.lineno}" for node in trailing]
        raise AssertionError(
            f"{function} has statements after terminal return: {locations}"
        )
    lines = source.splitlines(keepends=True)
    active_prefix = "".join(lines[function_node.lineno - 1 : return_node.end_lineno])
    return {
        "return_is_direct_child": return_node in function_node.body,
        "outer_yield_lines": _outer_scope_yields(function_node),
        "active_prefix_sha256": hashlib.sha256(active_prefix.encode()).hexdigest(),
    }


class DeadTailStructureContractTest(unittest.TestCase):
    def test_format_html_message_ends_at_its_unconditional_return(self):
        evidence = assert_no_statements_after_terminal_return()

        self.assertTrue(evidence["return_is_direct_child"])
        self.assertEqual(evidence["outer_yield_lines"], [])
        self.assertEqual(evidence["active_prefix_sha256"], ACTIVE_PREFIX_SHA256)

    def test_format_html_message_keeps_short_and_long_dispatch_behavior(self):
        import notifier

        with patch(
            "notifier._format_structured_html_message", return_value="short-message"
        ) as renderer:
            self.assertEqual(notifier.format_html_message(), "short-message")
        self.assertEqual(
            [(call.kwargs["compact"], call.kwargs["persist_snapshot"]) for call in renderer.call_args_list],
            [(False, False), (False, True)],
        )

        long_message = "x" * (notifier.PUSHPLUS_COMPACT_CHARS + 1)

        def render_by_mode(**kwargs):
            return "compact-message" if kwargs["compact"] else long_message

        with patch(
            "notifier._format_structured_html_message", side_effect=render_by_mode
        ) as renderer:
            self.assertEqual(notifier.format_html_message(), "compact-message")
        self.assertEqual(
            [(call.kwargs["compact"], call.kwargs["persist_snapshot"]) for call in renderer.call_args_list],
            [(False, False), (True, True)],
        )


if __name__ == "__main__":
    unittest.main()
