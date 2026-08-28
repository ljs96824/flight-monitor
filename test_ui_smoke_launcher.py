from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
