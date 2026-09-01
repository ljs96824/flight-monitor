from __future__ import annotations

import ast
import builtins
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parent
RUN_WEB_PATH = ROOT / "run_web.py"
CANARY_SECRET = "TEST_ONLY_RUN_WEB_CANARY_7f913c"


def _load_run_web():
    fake_logging = types.ModuleType("log_utils")
    configure_run_logging = Mock(name="configure_run_logging")
    fake_logging.configure_run_logging = configure_run_logging

    fake_web_form = types.ModuleType("web_form")
    fake_app = Mock(name="app")
    fake_web_form.app = fake_app

    spec = importlib.util.spec_from_file_location("_run_web_under_test", RUN_WEB_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("run_web.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {"log_utils": fake_logging, "web_form": fake_web_form},
    ):
        spec.loader.exec_module(module)
    configure_run_logging.reset_mock()
    fake_app.reset_mock()
    return module, configure_run_logging, fake_web_form, fake_app


def _tracking_import(events):
    original_import = builtins.__import__

    def tracked(name, *args, **kwargs):
        if name == "web_form":
            events.append("web_form")
        return original_import(name, *args, **kwargs)

    return tracked


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


def _runtime_nodes(node):
    yield node
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
        return
    for child in ast.iter_child_nodes(node):
        yield from _runtime_nodes(child)


def _valid_main_guard(node):
    if not _is_main_guard(node) or len(node.body) != 1 or node.orelse:
        return False
    statement = node.body[0]
    if not isinstance(statement, ast.Raise) or not isinstance(statement.exc, ast.Call):
        return False
    exit_call = statement.exc
    if _dotted_name(exit_call.func) != "SystemExit" or len(exit_call.args) != 1:
        return False
    main_call = exit_call.args[0]
    return (
        isinstance(main_call, ast.Call)
        and _dotted_name(main_call.func) == "main"
        and not main_call.args
        and not main_call.keywords
    )


def _run_web_ast_violations(source):
    tree = ast.parse(source, filename="run_web.py")
    violations = set()
    function_names = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    function_bind_keys = {
        node.value
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(statement)
        if isinstance(node, ast.Constant) and node.value in {"WEB_HOST", "WEB_PORT"}
    }
    module_bind_keys = set()

    if "main" not in function_names:
        violations.add("ast.main_defined")
    if not {"WEB_HOST", "WEB_PORT"} <= function_bind_keys:
        violations.add("ast.bind_settings_in_function")

    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom) and statement.module == "web_form":
            violations.add("ast.no_top_level_web_form_import")
        if isinstance(statement, ast.Import) and any(
            alias.name == "web_form" for alias in statement.names
        ):
            violations.add("ast.no_top_level_web_form_import")
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue

        guarded = _is_main_guard(statement)
        if guarded and not _valid_main_guard(statement):
            violations.add("ast.main_guard_shape")

        for node in _runtime_nodes(statement):
            if isinstance(node, ast.Constant) and node.value in {"WEB_HOST", "WEB_PORT"}:
                module_bind_keys.add(node.value)
            if isinstance(node, ast.Call):
                name = _dotted_name(node.func)
                if name == "configure_run_logging":
                    violations.add("ast.no_top_level_logging_call")
                if name == "app.run":
                    violations.add("ast.no_top_level_app_run")
                if not guarded and name in {"sys.exit", "SystemExit"}:
                    violations.add("ast.no_unguarded_exit")
            if (
                isinstance(node, ast.Raise)
                and not guarded
                and isinstance(node.exc, ast.Call)
                and _dotted_name(node.exc.func) == "SystemExit"
            ):
                violations.add("ast.no_unguarded_exit")

    if module_bind_keys:
        violations.add("ast.bind_settings_in_function")
    return frozenset(violations)


class RunWebImportSafetyTest(unittest.TestCase):
    def test_import_has_no_side_effects_in_fresh_process(self):
        probe = textwrap.dedent(
            f"""
            import builtins
            from pathlib import Path
            import socket
            import sys
            import types

            state = {{"configure": 0, "web_form": 0, "app_run": 0, "socket": 0}}
            sys.path.insert(0, {json.dumps(str(ROOT))})
            before = {{item.name for item in Path.cwd().iterdir()}}

            import log_utils
            def configure_run_logging(*args, **kwargs):
                state["configure"] += 1
            log_utils.configure_run_logging = configure_run_logging

            fake_web_form = types.ModuleType("web_form")
            class FakeApp:
                def run(self, *args, **kwargs):
                    state["app_run"] += 1
            fake_web_form.app = FakeApp()
            sys.modules["web_form"] = fake_web_form

            original_import = builtins.__import__
            def tracked_import(name, *args, **kwargs):
                if name == "web_form":
                    state["web_form"] += 1
                return original_import(name, *args, **kwargs)
            builtins.__import__ = tracked_import

            def forbidden_socket(*args, **kwargs):
                state["socket"] += 1
                raise AssertionError("socket creation during import")
            socket.socket = forbidden_socket

            import run_web
            after = {{item.name for item in Path.cwd().iterdir()}}
            if any(state.values()):
                raise AssertionError(f"import side effects: {{state}}")
            if after != before:
                raise AssertionError(f"import created files: {{sorted(after - before)}}")
            """
        )
        with tempfile.TemporaryDirectory() as tmp:
            env = {"NO_LIVE_API": "1", "PYTHONDONTWRITEBYTECODE": "1"}
            for key in ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP"):
                if key in os.environ:
                    env[key] = os.environ[key]
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "-c", probe],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")


class RunWebBindSettingsTest(unittest.TestCase):
    def test_default_settings_use_loopback_and_port_5000(self):
        module, _, _, _ = _load_run_web()
        self.assertEqual(module._resolve_bind_settings({}), ("127.0.0.1", 5000))

    def test_loopback_host_matrix_requires_no_public_opt_in(self):
        module, _, _, _ = _load_run_web()
        cases = {
            "127.0.0.1": "127.0.0.1",
            " 127.0.0.2 ": "127.0.0.2",
            "::1": "::1",
            "localhost": "localhost",
            "LOCALHOST": "LOCALHOST",
        }
        for raw, expected in cases.items():
            with self.subTest(host=raw):
                self.assertTrue(module._is_loopback_host(raw))
                self.assertEqual(
                    module._resolve_bind_settings({"WEB_HOST": raw}),
                    (expected, 5000),
                )

    def test_non_loopback_requires_exact_public_opt_in(self):
        module, _, _, _ = _load_run_web()
        for host in ("0.0.0.0", "::", "192.168.1.10", "devbox.local"):
            with self.subTest(host=host):
                self.assertFalse(module._is_loopback_host(host))
                for opt_in in (None, "true", "01", " 1"):
                    environ = {"WEB_HOST": host, "SERPAPI_KEY": CANARY_SECRET}
                    if opt_in is not None:
                        environ["ALLOW_PUBLIC_WEB_BIND"] = opt_in
                    with self.assertRaisesRegex(
                        RuntimeError, "ALLOW_PUBLIC_WEB_BIND=1"
                    ) as caught:
                        module._resolve_bind_settings(environ)
                    self.assertNotIn(host, str(caught.exception))
                    self.assertNotIn(CANARY_SECRET, str(caught.exception))
                self.assertEqual(
                    module._resolve_bind_settings(
                        {
                            "WEB_HOST": host,
                            "WEB_PORT": "5001",
                            "ALLOW_PUBLIC_WEB_BIND": "1",
                        }
                    ),
                    (host, 5001),
                )


class RunWebMainTest(unittest.TestCase):
    def test_default_main_runs_once_on_loopback(self):
        module, configure_logging, fake_web_form, fake_app = _load_run_web()
        events = []
        configure_logging.side_effect = lambda path: events.append("logging")
        fake_app.run.side_effect = lambda **kwargs: events.append("run")
        with tempfile.TemporaryDirectory() as tmp:
            module.__file__ = str(Path(tmp) / "run_web.py")
            stderr = io.StringIO()
            stdout = io.StringIO()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.dict(sys.modules, {"web_form": fake_web_form}),
                patch("builtins.__import__", side_effect=_tracking_import(events)),
                patch("socket.socket") as socket_factory,
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                result = module.main()
            configure_logging.assert_called_once_with(
                Path(tmp).resolve() / "data" / "run_latest.log"
            )
        self.assertEqual(result, 0)
        fake_app.run.assert_called_once_with(host="127.0.0.1", port=5000)
        socket_factory.assert_not_called()
        self.assertEqual(events, ["logging", "web_form", "run"])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_non_loopback_rejected_before_every_side_effect(self):
        module, configure_logging, fake_web_form, fake_app = _load_run_web()
        for host in ("0.0.0.0", "::", "192.168.1.10"):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as tmp:
                module.__file__ = str(Path(tmp) / "run_web.py")
                configure_logging.reset_mock()
                fake_app.reset_mock()
                imports = []
                with (
                    patch.dict(
                        os.environ,
                        {"WEB_HOST": host, "SERPAPI_KEY": CANARY_SECRET},
                        clear=True,
                    ),
                    patch.dict(sys.modules, {"web_form": fake_web_form}),
                    patch(
                        "builtins.__import__",
                        side_effect=_tracking_import(imports),
                    ),
                    patch("socket.socket") as socket_factory,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "ALLOW_PUBLIC_WEB_BIND=1"
                    ) as caught:
                        module.main()
                configure_logging.assert_not_called()
                fake_app.run.assert_not_called()
                socket_factory.assert_not_called()
                self.assertNotIn("web_form", imports)
                self.assertFalse((Path(tmp) / "data" / "run_latest.log").exists())
                self.assertNotIn(host, str(caught.exception))
                self.assertNotIn(CANARY_SECRET, str(caught.exception))

    def test_explicit_public_bind_warns_without_secret(self):
        module, configure_logging, fake_web_form, fake_app = _load_run_web()
        events = []
        configure_logging.side_effect = lambda path: events.append("logging")
        fake_app.run.side_effect = lambda **kwargs: events.append("run")
        with tempfile.TemporaryDirectory() as tmp:
            module.__file__ = str(Path(tmp) / "run_web.py")
            stderr = io.StringIO()
            stdout = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {
                        "WEB_HOST": "0.0.0.0",
                        "WEB_PORT": "5001",
                        "ALLOW_PUBLIC_WEB_BIND": "1",
                        "SERPAPI_KEY": CANARY_SECRET,
                    },
                    clear=True,
                ),
                patch.dict(sys.modules, {"web_form": fake_web_form}),
                patch("builtins.__import__", side_effect=_tracking_import(events)),
                patch("socket.socket") as socket_factory,
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                result = module.main()
        self.assertEqual(result, 0)
        fake_app.run.assert_called_once_with(host="0.0.0.0", port=5001)
        socket_factory.assert_not_called()
        self.assertEqual(events, ["logging", "web_form", "run"])
        self.assertEqual(stdout.getvalue(), "")
        warning = stderr.getvalue()
        self.assertIn("0.0.0.0", warning)
        self.assertIn("5001", warning)
        self.assertIn("没有身份认证", warning)
        self.assertIn("CSRF", warning)
        self.assertIn("同网段", warning)
        self.assertNotIn(CANARY_SECRET, warning)

    def test_invalid_ports_fail_before_every_side_effect(self):
        module, configure_logging, fake_web_form, fake_app = _load_run_web()
        for port in ("0", "65536", "not-an-integer"):
            with self.subTest(port=port), tempfile.TemporaryDirectory() as tmp:
                module.__file__ = str(Path(tmp) / "run_web.py")
                configure_logging.reset_mock()
                fake_app.reset_mock()
                imports = []
                with (
                    patch.dict(os.environ, {"WEB_PORT": port}, clear=True),
                    patch.dict(sys.modules, {"web_form": fake_web_form}),
                    patch(
                        "builtins.__import__",
                        side_effect=_tracking_import(imports),
                    ),
                    patch("socket.socket") as socket_factory,
                ):
                    with self.assertRaisesRegex(RuntimeError, "WEB_PORT") as caught:
                        module.main()
                configure_logging.assert_not_called()
                fake_app.run.assert_not_called()
                socket_factory.assert_not_called()
                self.assertNotIn("web_form", imports)
                self.assertFalse((Path(tmp) / "data" / "run_latest.log").exists())
                self.assertNotIn(port, str(caught.exception))


class RunWebAstContractTest(unittest.TestCase):
    def test_run_web_has_import_safe_module_shape(self):
        source = RUN_WEB_PATH.read_text(encoding="utf-8")
        self.assertEqual(_run_web_ast_violations(source), frozenset())


if __name__ == "__main__":
    unittest.main()
