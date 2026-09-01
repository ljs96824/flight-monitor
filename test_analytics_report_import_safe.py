import ast
import importlib
import inspect
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
REPORT_SCRIPT = ROOT / "analytics" / "report.py"
REPORT_OUT = ROOT / "analytics" / "out"
EXPECTED_HELP_FLAGS = {"--help", "--csv", "--min-n", "--db"}
DIRECT_NETWORK_CALLS = frozenset(
    {
        "httpx.get",
        "httpx.post",
        "httpx.put",
        "httpx.patch",
        "httpx.delete",
        "httpx.request",
        "requests.get",
        "requests.post",
        "requests.put",
        "requests.patch",
        "requests.delete",
        "requests.request",
        "urllib.request.urlopen",
        "urlopen",
        "GoogleSearch",
        "serpapi.GoogleSearch",
    }
)


def _dotted_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _is_main_guard(node):
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if (
        not isinstance(test, ast.Compare)
        or len(test.ops) != 1
        or not isinstance(test.ops[0], ast.Eq)
        or len(test.comparators) != 1
    ):
        return False
    pairs = (
        (test.left, test.comparators[0]),
        (test.comparators[0], test.left),
    )
    return any(
        isinstance(left, ast.Name)
        and left.id == "__name__"
        and isinstance(right, ast.Constant)
        and right.value == "__main__"
        for left, right in pairs
    )


def _scan_import_time_node(node, filename, violations):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
        return
    if _is_main_guard(node):
        for statement in node.orelse:
            _scan_import_time_node(statement, filename, violations)
        return

    if isinstance(node, ast.Raise):
        exception = node.exc
        exception_name = (
            _dotted_name(exception.func)
            if isinstance(exception, ast.Call)
            else _dotted_name(exception)
        )
        if exception_name in {"SystemExit", "builtins.SystemExit"}:
            violations.append(
                (filename, node.lineno, "unguarded_system_exit", exception_name)
            )

    if isinstance(node, ast.Call):
        call_name = _dotted_name(node.func)
        if call_name == "sys.exit":
            violations.append(
                (filename, node.lineno, "unguarded_sys_exit_call", call_name)
            )
        if call_name in DIRECT_NETWORK_CALLS:
            violations.append(
                (filename, node.lineno, "top_level_network_call", call_name)
            )

    for child in ast.iter_child_nodes(node):
        _scan_import_time_node(child, filename, violations)


def _module_import_time_violations(source, filename="<snippet>"):
    tree = ast.parse(source, filename=filename)
    violations = []
    for statement in tree.body:
        _scan_import_time_node(statement, filename, violations)
    return violations


def _package_module_violations(root=ROOT):
    package_roots = {
        init_file.parent
        for init_file in root.rglob("__init__.py")
        if not {".git", "data", "__pycache__"}.intersection(init_file.parts)
    }
    module_paths = {
        path
        for package_root in package_roots
        for path in package_root.rglob("*.py")
        if "__pycache__" not in path.parts
    }
    violations = []
    for path in sorted(module_paths):
        relative_path = path.relative_to(root).as_posix()
        violations.extend(
            _module_import_time_violations(
                path.read_text(encoding="utf-8"),
                filename=relative_path,
            )
        )
    return violations


def _directory_snapshot(path):
    path = Path(path)
    if not path.exists():
        return None
    return tuple(
        (
            item.relative_to(path).as_posix(),
            item.is_dir(),
            item.stat().st_size if item.is_file() else None,
        )
        for item in sorted(path.rglob("*"))
    )


def _subprocess_env(*python_paths):
    environment = os.environ.copy()
    sensitive_markers = (
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "API_KEY",
        "SERPAPI",
        "DUFFEL",
        "PUSHPLUS",
    )
    for key in tuple(environment):
        if any(marker in key.upper() for marker in sensitive_markers):
            environment.pop(key, None)
    environment.update(
        {
            "NO_LIVE_API": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": os.pathsep.join(str(path) for path in python_paths),
        }
    )
    return environment


def _write_network_and_sqlite_guard(directory):
    guard = Path(directory) / "sitecustomize.py"
    guard.write_text(
        textwrap.dedent(
            """
            import socket
            import sqlite3

            def _deny_sqlite(*args, **kwargs):
                raise AssertionError("sqlite3.connect called during --help")

            def _deny_network(*args, **kwargs):
                raise AssertionError("network called during --help")

            class _DeniedSocket(socket.socket):
                def connect(self, *args, **kwargs):
                    return _deny_network(*args, **kwargs)

                def connect_ex(self, *args, **kwargs):
                    return _deny_network(*args, **kwargs)

            sqlite3.connect = _deny_sqlite
            socket.socket = _DeniedSocket
            socket.create_connection = _deny_network
            """
        ).lstrip(),
        encoding="utf-8",
    )


def _run_help(arguments):
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        _write_network_and_sqlite_guard(temporary_path)
        temporary_before = _directory_snapshot(temporary_path)
        out_before = _directory_snapshot(REPORT_OUT)
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", *arguments],
            cwd=temporary_path,
            env=_subprocess_env(temporary_path, ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        temporary_after = _directory_snapshot(temporary_path)
        out_after = _directory_snapshot(REPORT_OUT)
    return completed, temporary_before, temporary_after, out_before, out_after


class AnalyticsReportImportSafetyTest(unittest.TestCase):
    def test_import_is_silent_and_does_not_load_cli_or_touch_io(self):
        code = textwrap.dedent(
            """
            import socket
            import sqlite3
            import sys
            import types

            calls = {"run_cli": 0, "sqlite": 0, "network": 0}
            fake_report_lib = types.ModuleType("analytics.report_lib")

            def fake_run_cli(argv=None):
                calls["run_cli"] += 1
                return 0

            def deny_sqlite(*args, **kwargs):
                calls["sqlite"] += 1
                raise AssertionError("sqlite3.connect called during import")

            def deny_network(*args, **kwargs):
                calls["network"] += 1
                raise AssertionError("network called during import")

            class DeniedSocket(socket.socket):
                def connect(self, *args, **kwargs):
                    return deny_network(*args, **kwargs)

                def connect_ex(self, *args, **kwargs):
                    return deny_network(*args, **kwargs)

            fake_report_lib.run_cli = fake_run_cli
            sys.modules["analytics.report_lib"] = fake_report_lib
            sqlite3.connect = deny_sqlite
            socket.socket = DeniedSocket
            socket.create_connection = deny_network

            import analytics.report

            assert calls == {"run_cli": 0, "sqlite": 0, "network": 0}, calls
            """
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            temporary_before = _directory_snapshot(temporary_path)
            out_before = _directory_snapshot(REPORT_OUT)
            completed = subprocess.run(
                [sys.executable, "-X", "utf8", "-c", code],
                cwd=temporary_path,
                env=_subprocess_env(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            temporary_after = _directory_snapshot(temporary_path)
            out_after = _directory_snapshot(REPORT_OUT)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")
        self.assertEqual(temporary_after, temporary_before)
        self.assertEqual(out_after, out_before)

    def test_module_help_is_available_without_sqlite_network_or_output(self):
        completed, temp_before, temp_after, out_before, out_after = _run_help(
            ["-m", "analytics.report", "--help"]
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("usage:", completed.stdout)
        self.assertEqual(
            set(re.findall(r"--[a-z][a-z-]*", completed.stdout)),
            EXPECTED_HELP_FLAGS,
        )
        self.assertEqual(completed.stderr, "")
        self.assertEqual(temp_after, temp_before)
        self.assertEqual(out_after, out_before)

    def test_direct_script_help_preserves_cli_compatibility(self):
        completed, temp_before, temp_after, out_before, out_after = _run_help(
            [str(REPORT_SCRIPT), "--help"]
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("usage:", completed.stdout)
        self.assertEqual(
            set(re.findall(r"--[a-z][a-z-]*", completed.stdout)),
            EXPECTED_HELP_FLAGS,
        )
        self.assertEqual(completed.stderr, "")
        self.assertEqual(temp_after, temp_before)
        self.assertEqual(out_after, out_before)

    def test_main_forwards_argv_and_returns_run_cli_status(self):
        try:
            report = importlib.import_module("analytics.report")
        except Exception as exc:
            self.fail(f"analytics.report import failed: {type(exc).__name__}: {exc}")

        received = []

        def fake_run_cli(argv=None):
            received.append(argv)
            return 37

        argv = ["--db", "fixture.sqlite3", "--min-n", "7"]
        with patch.object(report, "_load_run_cli", return_value=fake_run_cli):
            result = report.main(argv)

        self.assertEqual(result, 37)
        self.assertEqual(received, [argv])
        self.assertEqual(str(inspect.signature(report.main)), "(argv=None) -> int")


class PackageImportTimeAstContractTest(unittest.TestCase):
    def test_rejects_unguarded_system_exit(self):
        violations = _module_import_time_violations(
            "raise SystemExit(main())\n",
        )
        self.assertEqual(
            violations,
            [("<snippet>", 1, "unguarded_system_exit", "SystemExit")],
        )

    def test_allows_guarded_main_exit(self):
        violations = _module_import_time_violations(
            'if __name__ == "__main__":\n    raise SystemExit(main())\n',
        )
        self.assertEqual(violations, [])

    def test_rejects_unguarded_sys_exit_call(self):
        violations = _module_import_time_violations(
            "import sys\nsys.exit(2)\n",
        )
        self.assertEqual(
            violations,
            [("<snippet>", 2, "unguarded_sys_exit_call", "sys.exit")],
        )

    def test_rejects_top_level_network_but_ignores_function_body(self):
        top_level = _module_import_time_violations(
            'import httpx\nhttpx.get("https://example.invalid")\n',
        )
        inside_function = _module_import_time_violations(
            'def fetch():\n    return httpx.get("https://example.invalid")\n',
        )
        self.assertEqual(
            top_level,
            [("<snippet>", 2, "top_level_network_call", "httpx.get")],
        )
        self.assertEqual(inside_function, [])

    def test_package_modules_have_no_import_time_exit_or_network(self):
        self.assertEqual(_package_module_violations(), [])


if __name__ == "__main__":
    unittest.main()
