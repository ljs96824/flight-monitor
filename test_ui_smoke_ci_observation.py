import json
import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).parent
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
PACKAGE_JSON = ROOT / "package.json"
PACKAGE_LOCK = ROOT / "package-lock.json"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
ACTIONS_POLICY_ADR = ROOT / "docs" / "github-actions-version-policy.md"


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

    def test_job_has_complete_runtime_and_blocking_boundary(self):
        job = self._ui_job()
        required = (
            "runs-on: ubuntu-latest",
            "timeout-minutes: 20",
            "MPLBACKEND: Agg",
            'NO_LIVE_API: "1"',
            "actions/checkout@v7",
            "actions/setup-python@v7",
            'python-version: "3.13"',
            'cache: "pip"',
            "cache-dependency-path: requirements.txt",
            "pip install -r requirements.txt",
            "actions/setup-node@v7",
            'node-version: "22"',
            'cache: "npm"',
            "cache-dependency-path: package-lock.json",
            "npm ci",
            "npx playwright install --with-deps chromium",
        )
        self.assertEqual([item for item in required if item not in job], [])
        self.assertNotIn("continue-on-error:", job)
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
        self.assertIn("uses: actions/upload-artifact@v7", job)
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

    def test_official_actions_permissions_credentials_and_caches_are_exact(self):
        workflow = yaml.safe_load(self.workflow)
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(set(workflow["jobs"]), {"tests", "ui-smoke"})
        self.assertEqual(
            workflow["jobs"]["tests"]["strategy"]["matrix"]["os"],
            ["ubuntu-latest", "windows-latest"],
        )
        for job_name in ("tests", "ui-smoke"):
            self.assertEqual(
                workflow["jobs"][job_name]["env"]["NO_LIVE_API"], "1"
            )

        action_steps = []
        checkout_steps = []
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                action = step.get("uses", "")
                if action.startswith("actions/"):
                    action_steps.append(action)
                if action == "actions/checkout@v7":
                    checkout_steps.append(step)

        self.assertEqual(
            action_steps,
            [
                "actions/checkout@v7",
                "actions/setup-python@v7",
                "actions/checkout@v7",
                "actions/setup-python@v7",
                "actions/setup-node@v7",
                "actions/upload-artifact@v7",
            ],
        )
        self.assertEqual(len(checkout_steps), 2)
        for step in checkout_steps:
            self.assertIs(step["with"]["persist-credentials"], False)

        tests_steps = workflow["jobs"]["tests"]["steps"]
        ui_steps = workflow["jobs"]["ui-smoke"]["steps"]
        tests_python = next(
            step for step in tests_steps if step.get("uses") == "actions/setup-python@v7"
        )
        ui_python = next(
            step for step in ui_steps if step.get("uses") == "actions/setup-python@v7"
        )
        setup_node = next(
            step for step in ui_steps if step.get("uses") == "actions/setup-node@v7"
        )
        self.assertEqual(
            tests_python["with"]["cache-dependency-path"],
            "requirements.txt\nrequirements-dev.txt\n",
        )
        self.assertEqual(
            ui_python["with"]["cache-dependency-path"], "requirements.txt"
        )
        self.assertEqual(setup_node["with"]["node-version"], "22")
        self.assertEqual(setup_node["with"]["cache"], "npm")
        self.assertEqual(
            setup_node["with"]["cache-dependency-path"], "package-lock.json"
        )

        artifact = next(
            step for step in ui_steps if step.get("name") == "Upload UI smoke failure artifacts"
        )
        self.assertEqual(artifact["uses"], "actions/upload-artifact@v7")
        self.assertEqual(
            artifact["if"], "${{ always() && steps.smoke.outcome == 'failure' }}"
        )
        self.assertEqual(artifact["with"]["if-no-files-found"], "warn")
        self.assertEqual(artifact["with"]["retention-days"], 7)

    def test_workflow_triggers_jobs_commands_and_concurrency_remain_exact(self):
        # TODO: 合并本合同与 test_ci_portability.py 的重复 workflow 断言，建立单一事实源。
        workflow = yaml.safe_load(self.workflow)
        self.assertEqual(workflow["name"], "tests")
        triggers = workflow.get("on", workflow.get(True))
        self.assertEqual(
            triggers,
            {
                "push": {"branches": ["main"]},
                "pull_request": {"branches": ["main"]},
                "workflow_dispatch": None,
            },
        )
        concurrency = workflow["concurrency"]
        self.assertEqual(
            concurrency,
            {
                "group": (
                    "ci-${{ github.workflow }}-${{ github.event_name }}-"
                    "${{ github.event.pull_request.number || github.run_id }}"
                ),
                "cancel-in-progress": True,
            },
        )
        self.assertNotIn("queue", concurrency)
        group = concurrency["group"]
        self.assertNotIn("github.ref", group)
        self.assertNotIn("github.head_ref", group)
        self.assertIn(
            "github.event.pull_request.number || github.run_id",
            group,
        )

        tests_job = workflow["jobs"]["tests"]
        ui_job = workflow["jobs"]["ui-smoke"]
        for job in (tests_job, ui_job):
            self.assertNotIn("permissions", job)
            self.assertNotIn("name", job)
            self.assertEqual(job["timeout-minutes"], 20)

        self.assertEqual(tests_job["runs-on"], "${{ matrix.os }}")
        self.assertEqual(
            tests_job["strategy"],
            {
                "fail-fast": False,
                "matrix": {"os": ["ubuntu-latest", "windows-latest"]},
            },
        )
        self.assertEqual(ui_job["runs-on"], "ubuntu-latest")

        tests_steps = tests_job["steps"]
        ui_steps = ui_job["steps"]
        for steps in (tests_steps, ui_steps):
            setup_python = next(
                step
                for step in steps
                if step.get("uses") == "actions/setup-python@v7"
            )
            self.assertEqual(setup_python["with"]["python-version"], "3.13")

        test_commands = [step["run"] for step in tests_steps if "run" in step]
        self.assertIn(
            "python -X utf8 -m pytest -q -p no:cacheprovider", test_commands
        )
        self.assertIn("python -X utf8 -m unittest discover -b", test_commands)
        self.assertNotIn("python -X utf8 -m unittest discover", test_commands)

        artifact = next(
            step
            for step in ui_steps
            if step.get("name") == "Upload UI smoke failure artifacts"
        )
        self.assertEqual(
            artifact["with"]["name"],
            "ui-smoke-${{ github.run_id }}-${{ github.run_attempt }}",
        )
        self.assertEqual(
            artifact["with"]["path"], "${{ runner.temp }}/ui-smoke"
        )

    def test_actions_version_policy_adr_records_movable_major_tradeoff(self):
        text = ACTIONS_POLICY_ADR.read_text(encoding="utf-8")
        self.assertIn(
            "官方大版本标签可移动；同一个仓库提交在不同日期运行时，"
            "Action底层提交可能发生变化。本仓选择自动接收同主版本安全补丁，"
            "不宣称Action执行代码完全可复现。",
            text,
        )
        for release, workflow_tag in (
            ("actions/checkout v7.0.1", "actions/checkout@v7"),
            ("actions/setup-python v7.0.0", "actions/setup-python@v7"),
            ("actions/setup-node v7.0.0", "actions/setup-node@v7"),
            ("actions/upload-artifact v7.0.1", "actions/upload-artifact@v7"),
        ):
            self.assertIn(release, text)
            self.assertIn(workflow_tag, text)
        for forbidden in (
            "supply-chain lock",
            "immutable Actions",
            "fully reproducible Actions",
            "完全锁定供应链",
        ):
            self.assertNotIn(forbidden, text)


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

    def test_contributing_documents_blocking_gate_and_zero_api_evidence(self):
        text = CONTRIBUTING.read_text(encoding="utf-8")
        required = (
            "阻断模式",
            "7/7",
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

    def test_contributing_documents_smoke_route_coverage_boundary(self):
        text = CONTRIBUTING.read_text(encoding="utf-8")
        required = (
            "完整 `web_form.app`",
            "`/settings`",
            "`/subscribe`",
            "`/success`",
            "`/subscriptions`",
            "删除确认",
            "尚未覆盖 `/price_hint`、`/feedback`、暂停等其余 CRUD 路由",
            "smoke 绿不等于 Web 全绿",
            "扩展 smoke 驱动",
        )
        self.assertEqual([item for item in required if item not in text], [])


if __name__ == "__main__":
    unittest.main()
