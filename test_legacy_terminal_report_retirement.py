from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
RETIRED_ENTRYPOINT = ROOT / "check.py"
ACTIVE_OPERATIONAL_DOCS = (
    ROOT / "README.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "docs" / "collection-concurrency.md",
    ROOT / "docs" / "readonly-validation-snapshots.md",
    ROOT / "docs" / "runtime-backup-and-restore.md",
    ROOT / "docs" / "web-write-security.md",
)
EXECUTABLE_SUFFIXES = {".py", ".bat", ".cmd", ".ps1", ".yml", ".yaml"}


def _tracked_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return [ROOT / item for item in completed.stdout.splitlines() if item]


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts = [func.attr]
        value = func.value
        while isinstance(value, ast.Attribute):
            parts.append(value.attr)
            value = value.value
        if isinstance(value, ast.Name):
            parts.append(value.id)
        return ".".join(reversed(parts))
    return ""


def _python_references_legacy_entrypoint(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    scope_types = (
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.ClassDef,
        ast.Lambda,
    )

    def lexical_scope(node):
        current = parents.get(node)
        while current is not None:
            if isinstance(current, scope_types):
                return current
            current = parents.get(current)
        return tree

    def node_end_position(node):
        return (
            getattr(node, "end_lineno", node.lineno),
            getattr(node, "end_col_offset", node.col_offset),
        )

    docstring_nodes = set()
    for owner in ast.walk(tree):
        if not isinstance(
            owner,
            (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            continue
        if not owner.body:
            continue
        first = owner.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstring_nodes.add(id(first.value))

    string_values = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstring_nodes
    ]
    if any("check.py" in value for value in string_values):
        return True

    constant_bindings = {}
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        parameters = [
            *scope.args.posonlyargs,
            *scope.args.args,
            *scope.args.kwonlyargs,
        ]
        if scope.args.vararg is not None:
            parameters.append(scope.args.vararg)
        if scope.args.kwarg is not None:
            parameters.append(scope.args.kwarg)
        for parameter in parameters:
            binding_key = (id(scope), parameter.arg)
            position = (scope.lineno, scope.col_offset)
            constant_bindings.setdefault(binding_key, []).append((position, None))

    dynamic_import_names = {"__import__", "import_module", "run_module"}
    for node in ast.walk(tree):
        targets = ()
        value = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                value = node.value.value
        elif isinstance(node, ast.AnnAssign):
            targets = (node.target,)
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                value = node.value.value
        elif isinstance(node, ast.NamedExpr):
            targets = (node.target,)
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                value = node.value.value
        elif isinstance(node, ast.ImportFrom) and node.module in {"importlib", "runpy"}:
            for alias in node.names:
                if alias.name in {"import_module", "run_module"}:
                    dynamic_import_names.add(alias.asname or alias.name)
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            binding_key = (id(lexical_scope(node)), target.id)
            position = node_end_position(node)
            constant_bindings.setdefault(binding_key, []).append((position, value))

    def resolve_constant_binding(node, name):
        scope = lexical_scope(node)
        threshold = (node.lineno, node.col_offset)
        while True:
            bindings = constant_bindings.get((id(scope), name), ())
            if bindings:
                candidates = [
                    (position, value)
                    for position, value in bindings
                    if position < threshold
                ]
                if candidates:
                    return max(candidates, key=lambda item: item[0])[1]
                return None
            if scope is tree:
                return None
            threshold = (scope.lineno, scope.col_offset)
            scope = lexical_scope(scope)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(alias.name == "check" for alias in node.names):
            return True
        if isinstance(node, ast.ImportFrom):
            if node.module == "check" or (
                node.level > 0
                and node.module is None
                and any(alias.name == "check" for alias in node.names)
            ):
                return True
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "check":
                return True
        if not isinstance(node, ast.Call):
            continue
        call_leaf = _call_name(node).rsplit(".", 1)[-1]
        if call_leaf not in dynamic_import_names or not node.args:
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            module_name = argument.value
        elif isinstance(argument, ast.Name):
            module_name = resolve_constant_binding(node, argument.id)
        else:
            module_name = None
        if module_name == "check" or (
            isinstance(module_name, str) and module_name.startswith("check.")
        ):
            return True
    return False


class LegacyTerminalReportRetirementContractTest(unittest.TestCase):
    def test_python_scanner_catches_supported_executable_reference_shapes(self):
        cases = {
            "direct_import": "import check\n",
            "relative_import": "from . import check\n",
            "dynamic_import": (
                "from importlib import import_module\n"
                "module_name = 'check'\n"
                "import_module(module_name)\n"
            ),
            "subprocess_variable": (
                "import subprocess\n"
                "command = ['python', 'check.py']\n"
                "subprocess.run(command)\n"
            ),
            "subprocess_call": (
                "import subprocess\n"
                "subprocess.call(['python', 'check.py'])\n"
            ),
            "runpy_path": "import runpy\nrunpy.run_path('check.py')\n",
            "module_constant": (
                "from importlib import import_module\n"
                "module_name = 'check'\n"
                "def load():\n"
                "    return import_module(module_name)\n"
            ),
            "closure_constant": (
                "from importlib import import_module\n"
                "def outer():\n"
                "    module_name = 'check'\n"
                "    def inner():\n"
                "        return import_module(module_name)\n"
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, source in cases.items():
                path = root / f"{name}.py"
                path.write_text(source, encoding="utf-8")
                with self.subTest(shape=name):
                    self.assertTrue(_python_references_legacy_entrypoint(path))

            docstring_only = root / "docstring_only.py"
            docstring_only.write_text(
                '"""Historical prose mentioning check.py only."""\n',
                encoding="utf-8",
            )
            self.assertFalse(_python_references_legacy_entrypoint(docstring_only))
            unrelated_dynamic = root / "unrelated_dynamic.py"
            unrelated_dynamic.write_text(
                "from importlib import import_module\n"
                "label = 'check'\n"
                "import_module('json')\n",
                encoding="utf-8",
            )
            self.assertFalse(_python_references_legacy_entrypoint(unrelated_dynamic))
            cross_scope_same_name = root / "cross_scope_same_name.py"
            cross_scope_same_name.write_text(
                "from importlib import import_module\n"
                "module_name = 'json'\n"
                "def unrelated():\n"
                "    module_name = 'check'\n"
                "import_module(module_name)\n",
                encoding="utf-8",
            )
            with self.subTest(shape="cross_scope_same_name"):
                self.assertFalse(_python_references_legacy_entrypoint(cross_scope_same_name))
            parameter_shadow = root / "parameter_shadow.py"
            parameter_shadow.write_text(
                "from importlib import import_module\n"
                "module_name = 'check'\n"
                "def load(module_name):\n"
                "    return import_module(module_name)\n",
                encoding="utf-8",
            )
            with self.subTest(shape="parameter_shadow"):
                self.assertFalse(_python_references_legacy_entrypoint(parameter_shadow))

            reassigned_after_call = root / "reassigned_after_call.py"
            reassigned_after_call.write_text(
                "from importlib import import_module\n"
                "module_name = 'check'\n"
                "import_module(module_name)\n"
                "module_name = 'json'\n",
                encoding="utf-8",
            )
            with self.subTest(shape="reassigned_after_call"):
                self.assertTrue(_python_references_legacy_entrypoint(reassigned_after_call))


    def test_root_terminal_report_entrypoint_is_absent(self):
        self.assertFalse(
            RETIRED_ENTRYPOINT.exists(),
            "root check.py still exposes the legacy write-capable terminal report",
        )

    def test_tracked_executable_files_do_not_reference_legacy_entrypoint(self):
        offenders = []
        current_test = Path(__file__).resolve()
        for path in _tracked_files():
            if path.resolve() == current_test or path.suffix.lower() not in EXECUTABLE_SUFFIXES:
                continue
            if path.suffix.lower() == ".py":
                referenced = _python_references_legacy_entrypoint(path)
            else:
                referenced = "check.py" in path.read_text(
                    encoding="utf-8", errors="replace"
                )
            if referenced:
                offenders.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(offenders, [])

    def test_current_operational_docs_do_not_direct_users_to_check_py(self):
        self.assertTrue(all(path.is_file() for path in ACTIVE_OPERATIONAL_DOCS))
        for path in ACTIVE_OPERATIONAL_DOCS:
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertNotIn("check.py", path.read_text(encoding="utf-8"))

    def test_run_batches_continue_to_invoke_main_only(self):
        for relative in ("run.bat", "run_once.bat"):
            path = ROOT / relative
            command_lines = [
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip().lower().startswith("python ")
            ]
            with self.subTest(path=relative):
                self.assertEqual(command_lines, ["python main.py"])
                self.assertNotIn("check.py", path.read_text(encoding="utf-8"))

    def test_historical_documents_are_outside_the_active_document_set(self):
        relative_paths = {
            path.relative_to(ROOT).as_posix() for path in ACTIVE_OPERATIONAL_DOCS
        }
        self.assertFalse(any(path.startswith("docs/superpowers/") for path in relative_paths))
        self.assertFalse(any("audit" in path for path in relative_paths))


if __name__ == "__main__":
    unittest.main()
