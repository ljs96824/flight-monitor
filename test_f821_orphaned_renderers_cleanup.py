import ast
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MANUAL_RENDERER_DEBT = frozenset()
RETIRED_FULL_FLOW_TEST = "test_full.py"
PROHIBITED_TEST_ENTRYPOINT_CALLS = frozenset(
    {
        "app.run",
        "cached_fetch",
        "collect_and_classify",
        "init_db",
        "sys.stderr.reconfigure",
        "sys.stdout.reconfigure",
    }
)
NETWORK_CALL_PREFIXES = ("httpx.", "requests.", "urllib.request.")


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


def _call_path(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_path(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _is_main_guard(node):
    if not isinstance(node, ast.If):
        return False
    comparison = node.test
    if (
        not isinstance(comparison, ast.Compare)
        or len(comparison.ops) != 1
        or not isinstance(comparison.ops[0], ast.Eq)
    ):
        return False
    values = [comparison.left, *comparison.comparators]
    return any(
        isinstance(value, ast.Name) and value.id == "__name__"
        for value in values
    ) and any(
        isinstance(value, ast.Constant) and value.value == "__main__"
        for value in values
    )


def _flight_aggregator_bindings(node):
    bindings = set()
    for assignment in ast.walk(node):
        if (
            not isinstance(assignment, ast.Assign)
            or not isinstance(assignment.value, ast.Call)
            or _call_path(assignment.value.func) != "FlightAggregator"
        ):
            continue
        bindings.update(
            target.id for target in assignment.targets if isinstance(target, ast.Name)
        )
    return bindings


def _classified_prohibited_call(call_path, flight_aggregators=frozenset()):
    if call_path in PROHIBITED_TEST_ENTRYPOINT_CALLS:
        return call_path
    receiver, _, method = call_path.rpartition(".")
    if method == "collect" and (
        receiver == "FlightAggregator" or receiver in flight_aggregators
    ):
        return "FlightAggregator.collect"
    if call_path == "GoogleSearch" or call_path.startswith(NETWORK_CALL_PREFIXES):
        return f"network:{call_path}"
    return None


def _reachable_prohibited_calls(functions, entrypoint):
    pending = [entrypoint]
    visited = set()
    violations = set()
    while pending:
        function_name = pending.pop()
        if function_name in visited:
            continue
        visited.add(function_name)
        function = functions[function_name]
        flight_aggregators = _flight_aggregator_bindings(function)
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            called = _call_path(node.func)
            if called in functions:
                pending.append(called)
            prohibited = _classified_prohibited_call(called, flight_aggregators)
            if prohibited:
                violations.add((function_name, prohibited))
    return violations


def _retired_full_flow_violations():
    violations = set()
    tracked_tests = [
        path for path in _tracked_python_files() if path.name.startswith("test_")
    ]
    if any(path.name == RETIRED_FULL_FLOW_TEST for path in tracked_tests):
        violations.add((RETIRED_FULL_FLOW_TEST, "tracked", "legacy-live-test-module"))

    for path in tracked_tests:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        module_flight_aggregators = set()
        for statement in tree.body:
            if not isinstance(
                statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                module_flight_aggregators.update(
                    _flight_aggregator_bindings(statement)
                )
        for statement in tree.body:
            if isinstance(
                statement,
                (
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                    ast.ClassDef,
                    ast.Import,
                    ast.ImportFrom,
                ),
            ):
                continue
            if _is_main_guard(statement):
                guard_flight_aggregators = (
                    module_flight_aggregators
                    | _flight_aggregator_bindings(statement)
                )
                for child in statement.body:
                    for node in ast.walk(child):
                        if not isinstance(node, ast.Call):
                            continue
                        prohibited = _classified_prohibited_call(
                            _call_path(node.func), guard_flight_aggregators
                        )
                        if prohibited:
                            violations.add((path.name, "__main__", prohibited))
                entrypoints = {
                    _call_path(node.func)
                    for child in statement.body
                    for node in ast.walk(child)
                    if isinstance(node, ast.Call) and _call_path(node.func) in functions
                }
                for entrypoint in entrypoints:
                    reachable = _reachable_prohibited_calls(functions, entrypoint)
                    if not reachable:
                        continue
                    violations.add(
                        (path.name, "__main__", f"custom-live-main:{entrypoint}")
                    )
                    violations.update(
                        (path.name, scope, prohibited)
                        for scope, prohibited in reachable
                    )
                continue
            for node in ast.walk(statement):
                if not isinstance(node, ast.Call):
                    continue
                prohibited = _classified_prohibited_call(
                    _call_path(node.func), module_flight_aggregators
                )
                if prohibited:
                    violations.add((path.name, "<module>", prohibited))
    return frozenset(violations)


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

        self.assertFalse((ROOT / RETIRED_FULL_FLOW_TEST).exists())
        self.assertFalse(MANUAL_RENDERER_DEBT & KNOWN_F821_DEBT)


class TestModuleEntrypointSafetyTest(unittest.TestCase):
    def test_retired_full_flow_live_entrypoint_is_not_reintroduced(self):
        self.assertEqual(_retired_full_flow_violations(), frozenset())


if __name__ == "__main__":
    unittest.main()
