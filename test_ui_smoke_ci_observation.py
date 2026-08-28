import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parent
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
PACKAGE_JSON = ROOT / "package.json"
PACKAGE_LOCK = ROOT / "package-lock.json"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"


class UiSmokeCiObservationContractTest(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def _ui_job(self) -> str:
        match = re.search(
            r"(?ms)^  ui-smoke:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
            self.workflow,
        )
        self.assertIsNotNone(match, "workflow缺少独立ui-smoke job")
        return match.group(0)

    def test_job_has_complete_runtime_and_observation_boundary(self):
        job = self._ui_job()
        required = (
            "runs-on: ubuntu-latest",
            "timeout-minutes: 20",
            "continue-on-error: true",
            "MPLBACKEND: Agg",
            'NO_LIVE_API: "1"',
            "actions/checkout@v4",
            "actions/setup-python@v5",
            'python-version: "3.13"',
            'cache: "pip"',
            "cache-dependency-path: requirements.txt",
            "pip install -r requirements.txt",
            "actions/setup-node@v4",
            'node-version: "22"',
            'cache: "npm"',
            "npm ci",
            "npx playwright install --with-deps chromium",
        )
        self.assertEqual([item for item in required if item not in job], [])
        self.assertNotIn("requirements-dev.txt", job)
        self.assertNotRegex(job, r"(?m)^\s*- run: npm install(?:\s|$)")

    def test_browser_path_smoke_artifacts_and_summary_are_exact(self):
        job = self._ui_job()
        self.assertIn(
            "node -e \"console.log('BROWSER_PATH=' + "
            "require('playwright').chromium.executablePath())\" >> \"$GITHUB_ENV\"",
            job,
        )
        self.assertIn("id: smoke", job)
        self.assertIn("--log-path \"${{ runner.temp }}/ui-smoke/ui-smoke.log\"", job)
        self.assertIn("--artifact-dir \"${{ runner.temp }}/ui-smoke\"", job)
        self.assertIn(
            "if: ${{ always() && steps.smoke.outcome == 'failure' }}", job
        )
        self.assertIn("uses: actions/upload-artifact@v4", job)
        self.assertIn(
            "name: ui-smoke-${{ github.run_id }}-${{ github.run_attempt }}", job
        )
        self.assertIn("path: ${{ runner.temp }}/ui-smoke", job)
        self.assertIn("if-no-files-found: warn", job)
        self.assertIn("retention-days: 7", job)
        self.assertIn("if: ${{ always() }}", job)
        self.assertIn("steps.smoke.outcome", job)
        self.assertIn("github.event_name", job)
        self.assertIn("github.sha", job)
        self.assertIn("GITHUB_STEP_SUMMARY", job)

    def test_disabled_matrix_step_is_removed(self):
        self.assertNotIn("UI smoke (browser provisioning pending)", self.workflow)
        self.assertNotIn("if: ${{ false }}", self.workflow)

    def test_node_manifest_is_playwright_only_and_exactly_pinned(self):
        self.assertTrue(PACKAGE_JSON.is_file())
        self.assertTrue(PACKAGE_LOCK.is_file())
        package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
        self.assertEqual(set(package), {"devDependencies"})
        self.assertNotIn("dependencies", package)
        self.assertEqual(set(package["devDependencies"]), {"playwright"})
        version = package["devDependencies"]["playwright"]
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertFalse(version.startswith(("^", "~")))
        lock = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
        self.assertEqual(
            lock["packages"][""]["devDependencies"]["playwright"], version
        )

    def test_contributing_documents_observation_and_zero_api_evidence(self):
        text = CONTRIBUTING.read_text(encoding="utf-8")
        required = (
            "观察模式",
            "steps.smoke.outcome",
            "连续 7 次",
            "pull_request",
            "workflow_dispatch",
            "continue-on-error",
            "NO_LIVE_API=1",
            "start_background_collection",
            "load_calendar",
            "临时数据目录",
            "临时端口",
            "三库与配额台账哈希不变",
        )
        self.assertEqual([item for item in required if item not in text], [])
        self.assertIn("浏览器安装、启动、端口或日期时区", text)


if __name__ == "__main__":
    unittest.main()
