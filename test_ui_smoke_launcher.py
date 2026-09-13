from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.util
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parent
SMOKE_PATH = ROOT / "scripts" / "ui_smoke.py"
DRIVER_PATH = ROOT / "scripts" / "ui_smoke_driver.mjs"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location("ui_smoke_launcher_under_test", SMOKE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BrowserDiscoveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.smoke = _load_smoke_module()

    def test_browser_path_precedence_is_browser_then_edge_alias_then_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            browser = root / "browser.exe"
            edge_alias = root / "edge.exe"
            candidate = root / "candidate.exe"
            for path in (browser, edge_alias, candidate):
                path.write_bytes(b"browser")

            with mock.patch.dict(
                os.environ,
                {"BROWSER_PATH": str(browser), "EDGE_PATH": str(edge_alias)},
                clear=False,
            ), mock.patch.object(self.smoke, "BROWSER_CANDIDATES", (candidate,)):
                self.assertEqual(self.smoke._browser_path(), browser)

            with mock.patch.dict(os.environ, {"EDGE_PATH": str(edge_alias)}, clear=True), mock.patch.object(
                self.smoke, "BROWSER_CANDIDATES", (candidate,)
            ):
                self.assertEqual(self.smoke._browser_path(), edge_alias)

            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                self.smoke, "BROWSER_CANDIDATES", (candidate,)
            ):
                self.assertEqual(self.smoke._browser_path(), candidate)

    def test_missing_browser_error_lists_every_probed_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            browser = root / "missing-browser"
            edge_alias = root / "missing-edge"
            candidates = (root / "missing-chrome", root / "missing-chromium")
            with mock.patch.dict(
                os.environ,
                {"BROWSER_PATH": str(browser), "EDGE_PATH": str(edge_alias)},
                clear=True,
            ), mock.patch.object(self.smoke, "BROWSER_CANDIDATES", candidates):
                with self.assertRaises(RuntimeError) as caught:
                    self.smoke._browser_path()
            message = str(caught.exception)
            for path in (browser, edge_alias, *candidates):
                self.assertIn(str(path), message)

    def test_platform_candidates_cover_edge_chrome_and_chromium(self):
        windows = self.smoke._default_browser_candidates(
            platform_name="win32",
            environ={
                "PROGRAMFILES": "program-files",
                "PROGRAMFILES(X86)": "program-files-x86",
                "LOCALAPPDATA": "local-app-data",
            },
            which=lambda _name: None,
        )
        windows_text = "\n".join(map(str, windows)).lower()
        self.assertIn("microsoft", windows_text)
        self.assertIn("chrome", windows_text)

        resolved = {
            "microsoft-edge": "/usr/bin/microsoft-edge",
            "google-chrome": "/usr/bin/google-chrome",
            "chromium": "/usr/bin/chromium",
        }
        posix = self.smoke._default_browser_candidates(
            platform_name="linux",
            environ={},
            which=lambda name: resolved.get(name),
        )
        posix_names = {path.name for path in posix}
        self.assertTrue({Path(value).name for value in resolved.values()} <= posix_names)
        self.assertIn("chromium-browser", posix_names)


class BrowserLaunchContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.smoke = _load_smoke_module()

    def test_playwright_chromium_on_linux_disables_unavailable_runner_sandbox(self):
        command = self.smoke._browser_command(
            Path("/tmp/chromium"),
            cdp_port=9222,
            profile_dir=Path("/tmp/profile"),
            base_url="http://127.0.0.1:5001",
            platform_name="linux",
        )
        self.assertIn("--no-sandbox", command)

        windows = self.smoke._browser_command(
            Path("browser.exe"),
            cdp_port=9222,
            profile_dir=Path("profile"),
            base_url="http://127.0.0.1:5001",
            platform_name="win32",
        )
        self.assertNotIn("--no-sandbox", windows)


class UiSmokeArtifactContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.smoke = _load_smoke_module()

    def test_cli_forwards_log_and_artifact_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_path = root / "custom.log"
            artifact_dir = root / "artifacts"
            with mock.patch.object(self.smoke, "run_smoke", return_value=0) as run:
                result = self.smoke.main(
                    ["--log-path", str(log_path), "--artifact-dir", str(artifact_dir)]
                )
            self.assertEqual(result, 0)
            run.assert_called_once_with(log_path=log_path, artifact_dir=artifact_dir)
            self.assertFalse(log_path.exists())
            self.assertFalse(artifact_dir.exists())

    def test_failure_log_writer_emits_launcher_and_server_logs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_path = root / "logs" / "ui-smoke.log"
            artifact_dir = root / "artifacts"
            self.smoke._write_failure_logs(
                "[UI smoke] 结果=FAIL\n",
                "server traceback\n",
                log_path=log_path,
                artifact_dir=artifact_dir,
            )
            self.assertEqual(log_path.read_text(encoding="utf-8"), "[UI smoke] 结果=FAIL\n")
            self.assertEqual(
                (artifact_dir / "server.log").read_text(encoding="utf-8"),
                "server traceback\n",
            )

    def test_driver_uses_local_dates_and_captures_failure_evidence_via_cdp(self):
        driver = DRIVER_PATH.read_text(encoding="utf-8")
        self.assertNotIn("toISOString().slice(0, 10)", driver)
        self.assertIn("getFullYear()", driver)
        self.assertIn("getMonth() + 1", driver)
        self.assertIn("getDate()", driver)
        self.assertIn('command("Page.captureScreenshot"', driver)
        self.assertIn("failure.html", driver)
        self.assertIn("failure.png", driver)
        self.assertIn("browser-console.json", driver)
        self.assertIn("message.params.args", driver)


class UiSmokeFeedbackIsolationContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.smoke = _load_smoke_module()

    def test_serve_clears_feedback_recipient_and_installs_no_network_notifier(self):
        original_notifier = mock.Mock(
            side_effect=AssertionError("real feedback notifier was called")
        )
        observed: dict[str, object] = {}
        fake_web_form = types.SimpleNamespace(notify_feedback_author=original_notifier)

        def run_app(**kwargs):
            observed["recipient"] = os.environ.get("FEEDBACK_NOTIFY_EMAIL")
            observed["notify_result"] = fake_web_form.notify_feedback_author(
                {"comment": "UI_SMOKE_FEEDBACK_CANARY"}
            )
            observed["run_kwargs"] = kwargs

        fake_web_form.app = types.SimpleNamespace(run=run_app)
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(
            os.environ,
            {"FEEDBACK_NOTIFY_EMAIL": "must-not-survive@example.invalid"},
            clear=False,
        ), mock.patch.dict(sys.modules, {"web_form": fake_web_form}):
            self.smoke._serve(54321, Path(tmpdir))

        self.assertEqual(observed["recipient"], "")
        self.assertFalse(observed["notify_result"])
        self.assertEqual(
            fake_web_form.FEEDBACK_PATH,
            Path(tmpdir) / "feedback.json",
        )
        self.assertEqual(
            observed["run_kwargs"],
            {
                "host": "127.0.0.1",
                "port": 54321,
                "debug": False,
                "use_reloader": False,
                "threaded": True,
            },
        )
        original_notifier.assert_not_called()

    def test_run_smoke_guards_production_feedback_presence_size_and_sha(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "data"
            data_dir.mkdir()
            feedback = data_dir / "feedback.json"
            feedback.write_bytes(b'[{"feedback_type":"synthetic"}]')
            expected_sha = hashlib.sha256(feedback.read_bytes()).hexdigest()

            with mock.patch.object(self.smoke, "ROOT", root):
                states = self.smoke._protected_production_states()

        self.assertEqual(
            states["feedback"],
            {
                "exists": True,
                "bytes": 31,
                "sha256": expected_sha,
            },
        )
        self.assertEqual(
            states["subscriptions"],
            {"exists": False, "bytes": None, "sha256": None},
        )
        source = inspect.getsource(self.smoke.run_smoke)
        self.assertGreaterEqual(source.count("_protected_production_states()"), 2)
        self.assertIn("PRODUCTION_FEEDBACK_HASH_NOT_GUARDED", source)


SERVER_TEST_SETTINGS = {
    "PYTHON_DOTENV_DISABLED": "1",
    "MANAGEMENT_TOKEN": "",
    "MANAGEMENT_AUTH_REQUIRED": "0",
    "SHARED_DETAIL_TOKEN": "",
    "FLASK_SECRET_KEY": "ui-smoke-test-only-session-key",
    "NO_LIVE_API": "1",
    "FEEDBACK_NOTIFY_EMAIL": "",
    "SESSION_COOKIE_SECURE": "0",
    "CSRF_TOKEN_TTL_SECONDS": "7200",
    "COLLECTION_STARTUP_TIMEOUT_SECONDS": "3.0",
    "JUHE_FLIGHT_KEY": "",
    "SERPAPI_KEY": "",
    "SERPAPI_API_KEY": "",
    "SERP_API_KEY": "",
    "HASDATA_KEY": "",
    "SEARCHAPI_KEY": "",
    "TRAVELPAYOUTS_TOKEN": "",
    "RAPIDAPI_KEY": "",
    "DUFFEL_TOKEN": "",
    "PUSHPLUS_TOKEN": "",
    "SMTP_PROVIDER": "qq",
    "SMTP_HOST": "",
    "SMTP_PORT": "",
    "SMTP_SSL": "",
    "SMTP_USER": "",
    "SMTP_PASS": "",
    "PYTHONANYWHERE_TOKEN": "",
    "PYTHONANYWHERE_USER": "",
}

# Executed by a fresh interpreter, before the application's equivalent dotenv load.
DOTENV_PROBE = r'''
import builtins
import importlib.metadata
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
from dotenv import load_dotenv

path = Path(sys.argv[1])
assert path.is_absolute()
before = dict(os.environ)
with patch("builtins.open", wraps=builtins.open) as opened:
    returned = load_dotenv(path, encoding="utf-8")
count = sum(Path(call.args[0]) == path for call in opened.call_args_list)
print(json.dumps({
    "version": importlib.metadata.version("python-dotenv"),
    "disabled_before_load": before.get("PYTHON_DOTENV_DISABLED"),
    "returned": returned,
    "file_opens": count,
    "file_only_present": "UI_SMOKE_FILE_ONLY" in os.environ,
    "preset_preserved": os.environ.get("FLASK_SECRET_KEY") == before.get("FLASK_SECRET_KEY"),
    "parent_only_preserved": os.environ.get("UI_SMOKE_PARENT_ONLY") == "parent-only",
}))
'''


class UiSmokeServerEnvironmentContractTest(unittest.TestCase):
    def setUp(self):
        self.smoke = _load_smoke_module()
        self.parent = {key: value for key, value in os.environ.items() if key.upper() in {
            "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT",
            "LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOME",
        }}
        self.parent.update({key: "synthetic-parent-conflict" for key in SERVER_TEST_SETTINGS})
        self.parent.update(PYTHON_DOTENV_DISABLED="0", UI_SMOKE_PARENT_ONLY="parent-only")
        self.parent.setdefault("PATH", os.defpath)

    def _build(self, smoke=None):
        smoke = smoke or self.smoke
        self.assertTrue(callable(getattr(smoke, "_server_environment", None)),
                        "SERVER_ENV_BUILDER_MISSING")
        return smoke._server_environment()

    def _assert_mapping(self, smoke=None):
        with mock.patch.dict(os.environ, self.parent, clear=True):
            before = dict(os.environ)
            result = self._build(smoke)
            self.assertEqual(dict(os.environ), before, "LAUNCHER_ENV_MUTATED")
            self.assertIsNot(result, os.environ, "SERVER_ENV_NOT_INDEPENDENT")
            for name, value in SERVER_TEST_SETTINGS.items():
                self.assertEqual(result.get(name), value, "SERVER_SETTING:" + name)
            for name, value in before.items():
                if name not in SERVER_TEST_SETTINGS:
                    self.assertIn(name, result, "PARENT_VALUE_DROPPED:" + name)
                    self.assertEqual(result[name], value, "PARENT_VALUE_CHANGED:" + name)
            result["UI_SMOKE_PARENT_ONLY"] = "child-copy-only"
            self.assertEqual(dict(os.environ), before, "LAUNCHER_ENV_MUTATED")

    def _capture_service_environment(self, smoke=None):
        smoke = smoke or self.smoke
        calls = []

        def popen(command, **kwargs):
            calls.append((command, kwargs))
            if "--serve" in command:
                kwargs["stdout"].write(smoke.FEEDBACK_NOTIFY_STUB_MARKER + "\n")
                kwargs["stdout"].flush()
            return mock.Mock()

        with tempfile.TemporaryDirectory() as tmpdir, contextlib.ExitStack() as stack:
            root = Path(tmpdir)
            stack.enter_context(mock.patch.dict(os.environ, self.parent, clear=True))
            stack.enter_context(mock.patch.object(smoke, "ROOT", root))
            stack.enter_context(mock.patch.object(smoke, "_browser_path", return_value=root / "browser"))
            stack.enter_context(mock.patch.object(smoke, "_node_path", return_value="synthetic-node"))
            stack.enter_context(mock.patch.object(smoke, "_free_port", side_effect=[54321, 54322]))
            stack.enter_context(mock.patch.object(smoke, "_wait_http"))
            stack.enter_context(mock.patch.object(smoke.subprocess, "Popen", side_effect=popen))
            stack.enter_context(mock.patch.object(smoke.subprocess, "run", return_value=
                                                  types.SimpleNamespace(stdout="", stderr="", returncode=0)))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            result = smoke.run_smoke(artifact_dir=root / "artifacts")
        self.assertEqual(result, 0, "STUB_LAUNCHER_FAILED")
        services = [kwargs for command, kwargs in calls if "--serve" in command]
        self.assertEqual(len(services), 1, "SERVICE_POPEN_NOT_OBSERVED")
        self.assertEqual(len(calls), 2, "BROWSER_POPEN_NOT_DISTINGUISHED")
        self.assertIn("env", services[0], "SERVICE_ENV_NOT_PASSED")
        return services[0]["env"]

    def _probe(self, environment):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir).resolve() / ".env"
            path.write_text("FLASK_SECRET_KEY=synthetic-file-conflict\n"
                            "UI_SMOKE_FILE_ONLY=file-only\n", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", "-c", DOTENV_PROBE, str(path)],
                cwd=tmpdir, env=environment, capture_output=True, text=True,
                encoding="utf-8", timeout=15, check=False,
            )
        self.assertEqual(completed.returncode, 0, "DOTENV_PROBE_PROCESS_FAILED:" + completed.stderr)
        self.assertEqual(completed.stderr, "")
        return json.loads(completed.stdout)

    def _assert_disabled(self, result):
        self.assertEqual(result["file_opens"], 0, "DOTENV_FILE_OPENED")
        self.assertFalse(result["file_only_present"], "DOTENV_FILE_ONLY_INJECTED")
        self.assertIs(result["returned"], False, "DOTENV_NOT_DISABLED")
        self.assertEqual(result["disabled_before_load"], "1", "DISABLE_NOT_SET_BEFORE_LOAD")
        self.assertTrue(result["preset_preserved"], "PRESET_CHANGED")
        self.assertTrue(result["parent_only_preserved"], "UNREGISTERED_PARENT_LOST")

    def test_server_environment_overrides_without_mutating_parent(self):
        self._assert_mapping()

    def test_service_popen_receives_environment_before_serve(self):
        environment = self._capture_service_environment()
        for name, value in SERVER_TEST_SETTINGS.items():
            self.assertEqual(environment.get(name), value, "SERVICE_SETTING:" + name)
        self.assertEqual(environment["UI_SMOKE_PARENT_ONLY"], "parent-only")

    def test_real_dotenv_positive_control(self):
        result = self._probe(self.parent)
        self.assertIs(result["returned"], True)
        self.assertEqual(result["file_opens"], 1)
        self.assertTrue(result["file_only_present"])
        self.assertTrue(result["preset_preserved"])
        self.assertTrue(result["parent_only_preserved"])

    def test_child_dotenv_disabled_before_equivalent_import(self):
        self._assert_disabled(self._probe(self._capture_service_environment()))

    def test_serve_keeps_collection_calendar_and_temporary_path_isolation(self):
        fake = types.SimpleNamespace(app=types.SimpleNamespace(run=mock.Mock()))
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(sys.modules, {"web_form": fake}), \
                mock.patch.dict(os.environ, self.parent, clear=True), \
                mock.patch.object(sys, "path", list(sys.path)):
            root = Path(tmpdir)
            self.smoke._serve(54321, root)
            self.assertEqual(fake.SUBSCRIPTIONS_PATH, root / "subscriptions.json")
            self.assertEqual(fake.FEEDBACK_PATH, root / "feedback.json")
            self.assertEqual(fake.PAGE_PAYLOADS_DIR, root / "payloads")
            self.assertEqual(fake.start_background_collection({}),
                             {"status": "started", "entrypoint": "ui_smoke"})
            self.assertEqual(fake.load_calendar("synthetic-route"), [])
            self.assertEqual(os.environ["FEEDBACK_NOTIFY_EMAIL"], "")

    def _mutated_smoke(self, mutation):
        tree = ast.parse(SMOKE_PATH.read_text(encoding="utf-8"))
        before = ast.dump(tree)
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        builder = functions["_server_environment"]
        if mutation == "remove_service_env":
            matches = [node for node in ast.walk(functions["run_smoke"])
                       if isinstance(node, ast.Call) and ast.unparse(node.func) == "subprocess.Popen"
                       and node.args and isinstance(node.args[0], ast.List)
                       and any(isinstance(arg, ast.Constant) and arg.value == "--serve"
                               for arg in node.args[0].elts)]
            self.assertEqual(len(matches), 1, "MUTATION_SERVICE_NODE_COUNT")
            node = matches[0]
            self.assertEqual(sum(keyword.arg == "env" for keyword in node.keywords), 1)
            node.keywords = [keyword for keyword in node.keywords if keyword.arg != "env"]
        elif mutation == "mutate_launcher":
            matches = [node for node in builder.body if isinstance(node, ast.Assign)
                       and ast.unparse(node.value) == "dict(os.environ)"]
            self.assertEqual(len(matches), 1, "MUTATION_COPY_NODE_COUNT")
            matches[0].value = ast.parse("os.environ", mode="eval").body
        elif mutation == "drop_path":
            matches = [node for node in builder.body if isinstance(node, ast.Return)
                       and ast.unparse(node.value) == "server_env"]
            self.assertEqual(len(matches), 1, "MUTATION_RETURN_NODE_COUNT")
            builder.body.insert(builder.body.index(matches[0]),
                                ast.parse('server_env.pop("PATH", None)').body[0])
        elif mutation == "remove_dotenv_disable":
            matches = [(node, index) for node in ast.walk(builder) if isinstance(node, ast.Dict)
                       for index, key in enumerate(node.keys)
                       if isinstance(key, ast.Constant) and key.value == "PYTHON_DOTENV_DISABLED"]
            self.assertEqual(len(matches), 1, "MUTATION_DISABLE_NODE_COUNT")
            node, index = matches[0]
            del node.keys[index]
            del node.values[index]
        else:
            self.fail("UNKNOWN_MUTATION")
        self.assertNotEqual(ast.dump(tree), before, "MUTATION_NO_CHANGE")
        compiled = compile(ast.fix_missing_locations(tree), str(SMOKE_PATH), "exec")
        module = types.ModuleType("mutated_ui_smoke")
        module.__file__ = str(SMOKE_PATH)
        exec(compiled, module.__dict__)
        return module

    def test_mutation_service_env_removed_is_rejected_by_wiring(self):
        smoke = self._mutated_smoke("remove_service_env")
        with self.assertRaisesRegex(AssertionError, "SERVICE_ENV_NOT_PASSED"):
            self._capture_service_environment(smoke)

    def test_mutation_launcher_environment_write_is_rejected(self):
        smoke = self._mutated_smoke("mutate_launcher")
        with self.assertRaisesRegex(AssertionError, "LAUNCHER_ENV_MUTATED"):
            self._assert_mapping(smoke)

    def test_mutation_existing_path_dropped_is_rejected(self):
        smoke = self._mutated_smoke("drop_path")
        with self.assertRaisesRegex(AssertionError, "PARENT_VALUE_DROPPED:PATH"):
            self._assert_mapping(smoke)

    def test_mutation_dotenv_disable_removed_is_rejected_by_real_load(self):
        smoke = self._mutated_smoke("remove_dotenv_disable")
        result = self._probe(self._capture_service_environment(smoke))
        self.assertTrue(result["returned"])
        self.assertTrue(result["file_only_present"])
        self.assertEqual(result["file_opens"], 1)
        with self.assertRaisesRegex(AssertionError, "DOTENV_FILE_OPENED"):
            self._assert_disabled(result)


class UiSmokeLifecycleContractTest(unittest.TestCase):
    def setUp(self):
        self.smoke = _load_smoke_module()

    def _exercise(self, failure=None, *, smoke=None, close_error=False,
                  terminate_error=False, kill_required=False):
        smoke = smoke or self.smoke
        server, browser = mock.Mock(), mock.Mock()
        for process in (server, browser):
            if kill_required:
                process.wait.side_effect = [subprocess.TimeoutExpired("synthetic", 5), 0]
        if terminate_error:
            browser.terminate.side_effect = OSError("SYNTHETIC_CLEANUP_ERROR")
        stream = io.StringIO()
        stream_close = mock.Mock()
        created = []
        real_open, real_read = Path.open, Path.read_text

        def close_stream():
            stream_close()
            stream.close()
            if close_error:
                raise OSError("SYNTHETIC_CLOSE_ERROR")

        handle = mock.Mock()
        handle.close.side_effect = close_stream

        def open_path(path, *args, **kwargs):
            if path.name == "server-process.log":
                if failure == "log_open":
                    raise PermissionError("SYNTHETIC_LOG_OPEN")
                created.append("log")
                return handle
            return real_open(path, *args, **kwargs)

        def read_path(path, *args, **kwargs):
            if path.name == "server-process.log":
                return "synthetic-server-log\n" + smoke.FEEDBACK_NOTIFY_STUB_MARKER + "\n"
            return real_read(path, *args, **kwargs)

        def popen(command, **kwargs):
            if "--serve" in command:
                if failure == "server_popen":
                    raise OSError("SYNTHETIC_SERVER_POPEN")
                created.append("server")
                return server
            if failure == "browser_popen":
                raise OSError("SYNTHETIC_BROWSER_POPEN")
            created.append("browser")
            return browser

        output = io.StringIO()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir, contextlib.ExitStack() as stack:
            root = Path(tmpdir)
            artifacts = root / "artifacts"
            stack.enter_context(mock.patch.object(smoke, "ROOT", root))
            stack.enter_context(mock.patch.object(smoke, "_browser_path", return_value=root / "browser"))
            stack.enter_context(mock.patch.object(smoke, "_node_path", side_effect=
                RuntimeError("SYNTHETIC_NODE_MISSING") if failure == "node" else lambda: "synthetic-node"))
            stack.enter_context(mock.patch.object(smoke, "_free_port", side_effect=[54321, 54322]))
            stack.enter_context(mock.patch.object(smoke, "_wait_http"))
            stack.enter_context(mock.patch.object(smoke.subprocess, "Popen", side_effect=popen))
            driver = stack.enter_context(mock.patch.object(smoke.subprocess, "run", side_effect=
                subprocess.TimeoutExpired("synthetic-driver", 60, output=b"partial-out", stderr=b"partial-error")
                if failure == "driver_timeout" else lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")))
            writer = stack.enter_context(mock.patch.object(smoke, "_write_failure_logs", wraps=smoke._write_failure_logs))
            stack.enter_context(mock.patch.object(Path, "open", open_path))
            stack.enter_context(mock.patch.object(Path, "read_text", read_path))
            stack.enter_context(contextlib.redirect_stdout(output))
            result = {"returncode": None, "escaped": None, "message": ""}
            try:
                result["returncode"] = smoke.run_smoke(artifact_dir=artifacts)
            except Exception as exc:
                result["escaped"] = type(exc)
                result["message"] = str(exc)
            result.update(
                writer_calls=writer.call_count, created=created,
                close_calls=stream_close.call_count, stream_closed=stream.closed,
                server_calls=list(server.mock_calls), browser_calls=list(browser.mock_calls),
                stdout=output.getvalue(),
                files={path.name: path.read_text(encoding="utf-8") for path in artifacts.glob("*")},
                driver_calls=list(driver.call_args_list),
            )
        if not stream.closed:
            stream.close()
        return result

    def _assert_failure(self, result, reason):
        self.assertNotIn(result["escaped"], (NameError, UnboundLocalError), "SECONDARY_NAME_ERROR")
        self.assertIsNone(result["escaped"], "ORIGINAL_FAILURE_ESCAPED:" + result["message"])
        self.assertEqual(result["returncode"], 1, "FAILURE_NOT_RETURNED")
        self.assertEqual(result["writer_calls"], 1, "LAUNCHER_EVIDENCE_NOT_WRITTEN")
        self.assertEqual(set(result["files"]), {"ui-smoke.log", "server.log"}, "FAILURE_ARTIFACT_SET")
        report = result["files"]["ui-smoke.log"]
        self.assertIn(reason, report, "PRIMARY_FAILURE_LOST")
        self.assertIn(reason, result["stdout"], "PRIMARY_FAILURE_LOST")
        self.assertNotIn("UnboundLocalError", report)
        if "browser" not in result["created"]:
            self.assertIn("截图未生成", report, "MISSING_SCREENSHOT_REASON")
            self.assertIn("浏览器尚未启动", report, "MISSING_SCREENSHOT_REASON")
            self.assertEqual(result["browser_calls"], [], "UNCREATED_BROWSER_CLEANED")
        if "server" not in result["created"]:
            self.assertEqual(result["server_calls"], [], "UNCREATED_SERVER_CLEANED")
        if "log" not in result["created"]:
            self.assertEqual(result["close_calls"], 0, "UNCREATED_LOG_CLOSED")
            self.assertIn("服务日志未生成", result["files"]["server.log"])
        else:
            self.assertEqual(result["close_calls"], 1, "CREATED_LOG_NOT_CLOSED")
            self.assertTrue(result["stream_closed"])
            self.assertIn("synthetic-server-log", result["files"]["server.log"])

    def _assert_killed_processes_awaited(self, result):
        self.assertIsNone(result["escaped"])
        self.assertEqual(result["returncode"], 0)
        expected = [mock.call.terminate(), mock.call.wait(timeout=5),
                    mock.call.kill(), mock.call.wait(timeout=5)]
        self.assertEqual(result["server_calls"], expected, "SERVER_KILL_NOT_AWAITED")
        self.assertEqual(result["browser_calls"], expected, "BROWSER_KILL_NOT_AWAITED")

    def test_missing_node_writes_launcher_evidence(self):
        self._assert_failure(self._exercise("node"), "SYNTHETIC_NODE_MISSING")

    def test_log_open_failure_writes_launcher_evidence(self):
        self._assert_failure(self._exercise("log_open"), "SYNTHETIC_LOG_OPEN")

    def test_service_popen_failure_preserves_original_reason(self):
        self._assert_failure(self._exercise("server_popen"), "SYNTHETIC_SERVER_POPEN")

    def test_service_popen_cleanup_error_does_not_replace_primary(self):
        result = self._exercise("server_popen", close_error=True)
        self._assert_failure(result, "SYNTHETIC_SERVER_POPEN")
        report = result["files"]["ui-smoke.log"]
        self.assertIn("SYNTHETIC_CLOSE_ERROR", report)
        self.assertLess(report.index("SYNTHETIC_SERVER_POPEN"), report.index("SYNTHETIC_CLOSE_ERROR"))

    def test_killed_browser_and_server_are_awaited(self):
        self._assert_killed_processes_awaited(self._exercise(kill_required=True))

    def test_browser_popen_failure_remains_logged(self):
        result = self._exercise("browser_popen")
        # Existing catch already preserves failure code, original reason and server cleanup.
        self.assertIsNone(result["escaped"])
        self.assertEqual(result["returncode"], 1)
        self.assertEqual(result["writer_calls"], 1)
        self.assertIn("SYNTHETIC_BROWSER_POPEN", result["files"]["ui-smoke.log"])
        self.assertEqual(result["server_calls"], [mock.call.terminate(), mock.call.wait(timeout=5)])
        self.assertEqual(result["browser_calls"], [])
        self.assertTrue(result["stream_closed"])

    def test_driver_timeout_preserves_partial_output_and_cleanup(self):
        result = self._exercise("driver_timeout")
        self._assert_failure(result, "synthetic-driver")
        report = result["files"]["ui-smoke.log"]
        self.assertIn("partial-out", report)
        self.assertIn("partial-error", report)
        self.assertIn("timed out after 60 seconds", report)
        self.assertEqual(result["driver_calls"][0].kwargs["timeout"], 60)
        for calls in (result["server_calls"], result["browser_calls"]):
            self.assertEqual(calls, [mock.call.terminate(), mock.call.wait(timeout=5)])

    def test_cleanup_error_keeps_primary_and_still_cleans_other_resources(self):
        result = self._exercise("driver_timeout", terminate_error=True)
        self._assert_failure(result, "synthetic-driver")
        self.assertIn("SYNTHETIC_CLEANUP_ERROR", result["files"]["ui-smoke.log"])
        self.assertEqual(result["server_calls"], [mock.call.terminate(), mock.call.wait(timeout=5)])
        self.assertTrue(result["stream_closed"])

    def _lifecycle_mutation(self, mutation):
        smoke = _load_smoke_module()
        tree = ast.parse(inspect.getsource(smoke.run_smoke))
        before = ast.dump(tree, include_attributes=False)
        function = tree.body[0]
        matches = 0
        if mutation == "drop_early_evidence":
            for node in ast.walk(function):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                for statement in list(node.body):
                    if (isinstance(statement, ast.Expr)
                            and isinstance(statement.value, ast.Call)
                            and isinstance(statement.value.func, ast.Name)
                            and statement.value.func.id == "_write_failure_logs"):
                        node.body.remove(statement)
                        matches += 1
        elif mutation == "drop_browser_kill_wait":
            for node in ast.walk(function):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                if any(isinstance(item, ast.Call)
                       and ast.unparse(item.func) == "edge_process.kill"
                       for statement in node.body for item in ast.walk(statement)):
                    for statement in list(node.body):
                        if (isinstance(statement, ast.Expr)
                                and isinstance(statement.value, ast.Call)
                                and ast.unparse(statement.value.func) == "edge_process.wait"):
                            node.body.remove(statement)
                            matches += 1
        elif mutation == "replace_primary_with_cleanup":
            for node in ast.walk(function):
                if (isinstance(node, ast.Try)
                        and any(isinstance(item, ast.Call)
                                and ast.unparse(item.func) == "server_log_stream.close"
                                for statement in node.body for item in ast.walk(statement))):
                    handler = node.handlers[0]
                    handler.body[0] = ast.parse('lines = [str(exc)]').body[0]
                    matches += 1
        elif mutation == "unbound_server_output":
            # With early failures now caught, removing initialization can expose
            # a secondary name error. This is not a reproduced baseline failure.
            for statement in list(function.body):
                if (isinstance(statement, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "server_output"
                                for target in statement.targets)):
                    function.body.remove(statement)
                    matches += 1
            for node in ast.walk(function):
                if not isinstance(node, ast.Try):
                    continue
                for index, statement in enumerate(node.body):
                    if (isinstance(statement, ast.Assign)
                            and isinstance(statement.value, ast.Call)
                            and ast.unparse(statement.value.func) == "server_log_path.read_text"):
                        node.body[index] = ast.If(
                            test=ast.parse("server is not None", mode="eval").body,
                            body=[statement], orelse=[],
                        )
                        matches += 1
        else:
            self.fail("UNKNOWN_MUTATION")
        self.assertEqual(matches, 2 if mutation == "unbound_server_output" else 1,
                         "MUTATION_TARGET_COUNT")
        self.assertNotEqual(ast.dump(tree, include_attributes=False), before, "MUTATION_NO_CHANGE")
        compiled = compile(ast.fix_missing_locations(tree), str(SMOKE_PATH), "exec")
        exec(compiled, smoke.__dict__)
        return smoke

    def test_mutation_early_evidence_removed_is_rejected(self):
        smoke = self._lifecycle_mutation("drop_early_evidence")
        with self.assertRaisesRegex(AssertionError, "LAUNCHER_EVIDENCE_NOT_WRITTEN"):
            self._assert_failure(self._exercise("node", smoke=smoke), "SYNTHETIC_NODE_MISSING")

    def test_mutation_browser_kill_wait_removed_is_rejected(self):
        smoke = self._lifecycle_mutation("drop_browser_kill_wait")
        with self.assertRaisesRegex(AssertionError, "BROWSER_KILL_NOT_AWAITED"):
            self._assert_killed_processes_awaited(self._exercise(smoke=smoke, kill_required=True))

    def test_mutation_cleanup_replaces_primary_is_rejected(self):
        smoke = self._lifecycle_mutation("replace_primary_with_cleanup")
        with self.assertRaisesRegex(AssertionError, "PRIMARY_FAILURE_LOST"):
            self._assert_failure(self._exercise("server_popen", smoke=smoke, close_error=True),
                                 "SYNTHETIC_SERVER_POPEN")

    def test_mutation_unbound_server_output_is_rejected(self):
        smoke = self._lifecycle_mutation("unbound_server_output")
        with self.assertRaisesRegex(AssertionError, "SECONDARY_NAME_ERROR"):
            self._assert_failure(self._exercise("server_popen", smoke=smoke), "SYNTHETIC_SERVER_POPEN")


if __name__ == "__main__":
    unittest.main()
