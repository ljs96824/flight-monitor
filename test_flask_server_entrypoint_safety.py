"""Contracts for repository-provided Flask server entrypoints.

本笔关闭的是仓库自带的 direct script 与 module execution 启动旁路;操作者仍可用 flask CLI / Gunicorn / Waitress 等外部 WSGI 方式自行绑定地址,该行为不在本笔控制范围内,须作为 residual risk 明列。
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
WEB_FORM_PATH = ROOT / "web_form.py"
EXPECTED_EXIT_MESSAGE = (
    "请使用 python run_web.py 启动本地 Web 服务；"
    "run_web.py 提供回环默认绑定与显式公网授权门。"
)
TARGET_SCOPE_STATEMENT = (
    "本笔关闭的是仓库自带的 direct script 与 module execution 启动旁路;"
    "操作者仍可用 flask CLI / Gunicorn / Waitress 等外部 WSGI 方式自行绑定地址,"
    "该行为不在本笔控制范围内,须作为 residual risk 明列。"
)

EXPECTED_RED_TEST_IDS = frozenset(
    {
        "test_flask_server_entrypoint_safety.FlaskServerEntrypointSafetyTest."
        "test_repository_server_call_inventory_matches_scoped_policy",
        "test_flask_server_entrypoint_safety.FlaskServerEntrypointSafetyTest."
        "test_web_form_has_early_direct_execution_rejection",
        "test_flask_server_entrypoint_safety.FlaskServerEntrypointSafetyTest."
        "test_direct_script_exits_with_migration_guidance",
        "test_flask_server_entrypoint_safety.FlaskServerEntrypointSafetyTest."
        "test_module_execution_exits_with_migration_guidance",
        "test_flask_server_entrypoint_safety.FlaskServerEntrypointSafetyTest."
        "test_runpy_rejects_before_dotenv_or_flask_run",
    }
)
EXPECTED_RED_VIOLATION_SET = frozenset(
    {("web_form.py", "__main__", "app.run")}
)
CONTROLLED_PRODUCTION_SERVER_CALLS = frozenset(
    {("run_web.py", "main", "app.run")}
)
RESIDUAL_TEST_HARNESS_SERVER_CALLS = frozenset(
    {("scripts/ui_smoke.py", "_serve", "app.run")}
)


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def _resolved_name(node: ast.AST, aliases: dict[str, str]) -> str:
    name = _dotted_name(node)
    seen: set[str] = set()
    while name in aliases and name not in seen:
        seen.add(name)
        name = aliases[name]
    if "." in name:
        first, rest = name.split(".", 1)
        resolved_first = aliases.get(first, first)
        return f"{resolved_first}.{rest}"
    return name


def _is_app_run(node: ast.Call, aliases: dict[str, str]) -> bool:
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "run":
        return False
    owner = _resolved_name(node.func.value, aliases)
    return owner == "app" or owner == "web_form.app" or owner.endswith(".app")


class _ScopeCallVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        path: str,
        scope: str,
        aliases: dict[str, str],
        calls: set[tuple[str, str, str]],
    ):
        self.path = path
        self.scope = scope
        self.aliases = aliases
        self.calls = calls

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            self.aliases[item.asname or item.name] = item.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if not node.module:
            return
        for item in node.names:
            local = item.asname or item.name
            resolved = f"{node.module}.{item.name}"
            self.aliases[local] = "app" if resolved == "web_form.app" else resolved

    def visit_Assign(self, node: ast.Assign) -> None:
        resolved = _resolved_name(node.value, self.aliases)
        if resolved == "app" or resolved == "web_form.app" or resolved.endswith(".app"):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.aliases[target.id] = resolved
        self.generic_visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            resolved = _resolved_name(node.value, self.aliases)
            if isinstance(node.target, ast.Name) and (
                resolved == "app"
                or resolved == "web_form.app"
                or resolved.endswith(".app")
            ):
                self.aliases[node.target.id] = resolved
            self.visit(node.value)

    def visit_Call(self, node: ast.Call) -> None:
        if _is_app_run(node, self.aliases):
            self.calls.add((self.path, self.scope, "app.run"))
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


def _scan_statements(
    statements: list[ast.stmt],
    *,
    path: str,
    scope: str,
    inherited_aliases: dict[str, str],
    calls: set[tuple[str, str, str]],
) -> None:
    aliases = dict(inherited_aliases)
    for statement in statements:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _scan_statements(
                statement.body,
                path=path,
                scope=statement.name,
                inherited_aliases=aliases,
                calls=calls,
            )
            continue
        if isinstance(statement, ast.ClassDef):
            continue
        if scope == "<module>" and _is_main_guard(statement):
            _scan_statements(
                statement.body,
                path=path,
                scope="__main__",
                inherited_aliases=aliases,
                calls=calls,
            )
            continue
        visitor = _ScopeCallVisitor(
            path=path,
            scope=scope,
            aliases=aliases,
            calls=calls,
        )
        visitor.visit(statement)


def _flask_server_calls(source: str, *, path: str) -> frozenset[tuple[str, str, str]]:
    tree = ast.parse(source, filename=path)
    calls: set[tuple[str, str, str]] = set()
    _scan_statements(
        tree.body,
        path=path,
        scope="<module>",
        inherited_aliases={},
        calls=calls,
    )
    return frozenset(calls)


def _tracked_python_paths() -> tuple[Path, ...]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z", "*.py"],
        cwd=ROOT,
    ).decode("utf-8")
    return tuple(ROOT / item for item in output.split("\0") if item)


def _repository_flask_server_calls() -> frozenset[tuple[str, str, str]]:
    calls: set[tuple[str, str, str]] = set()
    for path in _tracked_python_paths():
        relative = path.relative_to(ROOT).as_posix()
        calls.update(
            _flask_server_calls(path.read_text(encoding="utf-8-sig"), path=relative)
        )
    return frozenset(calls)


def _early_rejection_violations(source: str) -> frozenset[str]:
    tree = ast.parse(source, filename="web_form.py")
    violations: set[str] = set()
    if len(tree.body) < 3:
        return frozenset({"guard.missing"})
    if not (
        isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        violations.add("guard.after_docstring")
    future = tree.body[1]
    if not (
        isinstance(future, ast.ImportFrom)
        and future.module == "__future__"
        and any(item.name == "annotations" for item in future.names)
    ):
        violations.add("guard.after_future")
    guard = tree.body[2]
    if not _is_main_guard(guard):
        violations.add("guard.missing")
        return frozenset(violations)
    if guard.orelse or len(guard.body) != 1 or not isinstance(guard.body[0], ast.Raise):
        violations.add("guard.shape")
        return frozenset(violations)
    raised = guard.body[0].exc
    if not (
        isinstance(raised, ast.Call)
        and _dotted_name(raised.func) == "SystemExit"
        and len(raised.args) == 1
        and isinstance(raised.args[0], ast.Constant)
        and raised.args[0].value == EXPECTED_EXIT_MESSAGE
        and not raised.keywords
    ):
        violations.add("guard.message")
    if TARGET_SCOPE_STATEMENT not in "".join(source.splitlines()):
        violations.add("guard.scope_comment")
    return frozenset(violations)


def _subprocess_environment(temp_root: Path) -> dict[str, str]:
    dependency_paths = tuple(
        item
        for item in sys.path
        if item and Path(item).is_absolute() and Path(item).exists()
    )
    environment = {
        "NO_LIVE_API": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "FLASK_SECRET_KEY": "TEST_ONLY_ENTRYPOINT_SECRET",
        "PYTHONPATH": os.pathsep.join(
            (str(temp_root), str(ROOT), *dependency_paths)
        ),
        "BIND_MARKER": str(temp_root / "bind-attempted.txt"),
        "IMPORT_MARKER": str(temp_root / "web-import-chain-entered.txt"),
    }
    for key in ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP"):
        if key in os.environ:
            environment[key] = os.environ[key]
    return environment


def _write_sitecustomize(temp_root: Path) -> None:
    (temp_root / "sitecustomize.py").write_text(
        textwrap.dedent(
            """
            import importlib.abc
            import os
            import socket
            import sys

            class _ImportWatch(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname.split('.', 1)[0] in {
                        'dotenv', 'flask', 'atomic_json_store', 'airports'
                    }:
                        with open(os.environ['IMPORT_MARKER'], 'a', encoding='utf-8') as stream:
                            stream.write(fullname + '\\n')
                    return None

            class _NoBindSocket(socket.socket):
                def bind(self, address):
                    with open(os.environ['BIND_MARKER'], 'a', encoding='utf-8') as stream:
                        stream.write(repr(address) + '\\n')
                    raise AssertionError('socket bind forbidden by entrypoint contract')

            sys.meta_path.insert(0, _ImportWatch())
            socket.socket = _NoBindSocket
            """
        ),
        encoding="utf-8",
    )


class FlaskServerEntrypointSafetyTest(unittest.TestCase):
    def test_repository_server_call_inventory_matches_scoped_policy(self):
        actual = _repository_flask_server_calls()
        expected = CONTROLLED_PRODUCTION_SERVER_CALLS | RESIDUAL_TEST_HARNESS_SERVER_CALLS
        self.assertEqual(actual, expected)
        self.assertEqual(
            actual - expected,
            frozenset(),
            "This is a scoped gate, not a claim that external WSGI launchers are controlled.",
        )

    def test_web_form_has_early_direct_execution_rejection(self):
        source = WEB_FORM_PATH.read_text(encoding="utf-8")
        self.assertEqual(_early_rejection_violations(source), frozenset())
        self.assertEqual(_flask_server_calls(source, path="web_form.py"), frozenset())

    def _assert_rejected_process(self, command: list[str]) -> None:
        source = WEB_FORM_PATH.read_text(encoding="utf-8")
        self.assertEqual(
            _early_rejection_violations(source),
            frozenset(),
            "DIRECT_ENTRYPOINT_GUARD_MISSING; subprocess intentionally not started",
        )
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            _write_sitecustomize(temp_root)
            before = {item.name for item in temp_root.iterdir()}
            started = time.monotonic()
            result = subprocess.run(
                command,
                cwd=temp_root,
                env=_subprocess_environment(temp_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=5,
                check=False,
            )
            elapsed = time.monotonic() - started
            after = {item.name for item in temp_root.iterdir()}
            self.assertFalse((temp_root / "bind-attempted.txt").exists())
            self.assertFalse((temp_root / "web-import-chain-entered.txt").exists())
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.strip(), EXPECTED_EXIT_MESSAGE)
        self.assertLess(elapsed, 5)
        self.assertEqual(after, before)

    def test_direct_script_exits_with_migration_guidance(self):
        self._assert_rejected_process(
            [sys.executable, "-X", "utf8", str(WEB_FORM_PATH)]
        )

    def test_module_execution_exits_with_migration_guidance(self):
        self._assert_rejected_process(
            [sys.executable, "-X", "utf8", "-m", "web_form"]
        )

    def test_runpy_rejects_before_dotenv_or_flask_run(self):
        with (
            patch("dotenv.load_dotenv") as load_dotenv,
            patch("flask.app.Flask.run") as flask_run,
            self.assertRaisesRegex(SystemExit, "python run_web.py") as caught,
        ):
            runpy.run_path(str(WEB_FORM_PATH), run_name="__main__")
        self.assertEqual(caught.exception.code, EXPECTED_EXIT_MESSAGE)
        load_dotenv.assert_not_called()
        flask_run.assert_not_called()

    def test_import_and_pa_wsgi_style_import_still_export_app(self):
        probe = textwrap.dedent(
            """
            from unittest.mock import patch

            with patch("dotenv.load_dotenv") as load_dotenv:
                from web_form import app
                assert app.import_name == "web_form"
                load_dotenv.assert_called_once()
            """
        )
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "-c", probe],
                cwd=temp_root,
                env=_subprocess_environment(temp_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=10,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_ast_mutations_distinguish_server_calls_from_rejection_gate(self):
        cases = {
            "top_level_app_run": (
                "from web_form import app\napp.run()\n",
                frozenset({("fixture.py", "<module>", "app.run")}),
            ),
            "main_guard_app_run": (
                "from web_form import app\nif __name__ == '__main__':\n    app.run()\n",
                frozenset({("fixture.py", "__main__", "app.run")}),
            ),
            "app_alias": (
                "from web_form import app\nserver = app\nserver.run()\n",
                frozenset({("fixture.py", "<module>", "app.run")}),
            ),
            "reachable_helper": (
                "from web_form import app\ndef serve():\n    app.run()\n"
                "if __name__ == '__main__':\n    serve()\n",
                frozenset({("fixture.py", "serve", "app.run")}),
            ),
            "valid_rejection": (
                "if __name__ == '__main__':\n"
                "    raise SystemExit('use run_web.py')\n",
                frozenset(),
            ),
            "controlled_run_web_main": (
                "def main():\n    from web_form import app\n"
                "    app.run(host='127.0.0.1', port=5000)\n",
                frozenset({("run_web.py", "main", "app.run")}),
            ),
        }
        for name, (source, expected) in cases.items():
            with self.subTest(mutation=name):
                path = "run_web.py" if name == "controlled_run_web_main" else "fixture.py"
                self.assertEqual(_flask_server_calls(source, path=path), expected)

    def test_red_contract_metadata_is_stable(self):
        self.assertEqual(len(EXPECTED_RED_TEST_IDS), 5)
        self.assertEqual(
            EXPECTED_RED_VIOLATION_SET,
            frozenset({("web_form.py", "__main__", "app.run")}),
        )
        self.assertIn("\u5916\u90e8 WSGI", TARGET_SCOPE_STATEMENT)
        self.assertIn("residual risk", TARGET_SCOPE_STATEMENT)


if __name__ == "__main__":
    unittest.main()
