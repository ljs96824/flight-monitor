import ast
import copy
from dataclasses import dataclass
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

MISSING = "<MISSING>"
PRESENT = "<PRESENT>"
UI_SMOKE_SUMMARY_COMMAND = (
    "{\n"
    '  echo "## UI smoke observation"\n'
    '  echo "- steps.smoke.outcome: \\`${{ steps.smoke.outcome }}\\`"\n'
    '  echo "- github.event_name: \\`${{ github.event_name }}\\`"\n'
    '  echo "- github.sha: \\`${{ github.sha }}\\`"\n'
    '} | tee -a "$GITHUB_STEP_SUMMARY"\n'
)
CANONICAL_WORKFLOW_RULE_IDS = frozenset(
    {
        "WF001_NAME",
        "WF002_TRIGGERS",
        "WF003_CONCURRENCY_GROUP",
        "WF004_CONCURRENCY_CANCEL",
        "WF005_CONCURRENCY_QUEUE_ABSENT",
        "WF006_TOP_LEVEL_PERMISSIONS",
        "WF007_JOB_PERMISSIONS_ABSENT",
        "WF008_JOB_SET",
        "WF009_JOB_NAMES_ABSENT",
        "WF010_TEST_STRATEGY",
        "WF011_RUNNERS",
        "WF012_TIMEOUTS",
        "WF013_PYTHON_VERSIONS",
        "WF014_NO_LIVE_API",
        "WF015_MPLBACKEND",
        "WF016_ACTION_SEQUENCE",
        "WF017_CHECKOUT_CREDENTIALS",
        "WF018_PIP_CACHES",
        "WF019_NODE_CACHE_AND_VERSION",
        "WF020_TESTS_INSTALL",
        "WF021_UI_INSTALL",
        "WF022_PYTEST_COMMAND",
        "WF023_UNITTEST_COMMANDS",
        "WF024_SECRETS_ABSENT",
        "WF025_CONTINUE_ON_ERROR_ABSENT",
        "WF026_NPM_COMMANDS",
        "WF027_PLAYWRIGHT_INSTALL",
        "WF028_BROWSER_PATH_EXPORT",
        "WF029_SMOKE_COMMAND",
        "WF030_ARTIFACT_CONTRACT",
        "WF031_SUMMARY_CONTRACT",
        "WF032_DISABLED_STEPS_ABSENT",
        "WF033_UI_DEV_REQUIREMENTS_ABSENT",
    }
)
NON_WORKFLOW_CONTRACTS = frozenset(
    {
        "NW001_WORKFLOW_FILE_EXISTS",
        "NW002_LEGACY_MONITOR_ABSENT",
        "NW003_README_TESTS_BADGE",
        "NW004_CI_PORTABILITY_NO_WORKFLOW_PARSER",
        "NW005_ACTIONS_POLICY_ADR",
        "NW006_NODE_MANIFEST",
        "NW007_CONTRIBUTING_BLOCKING_GATE",
        "NW008_CONTRIBUTING_ROUTE_COVERAGE",
    }
)


@dataclass(frozen=True)
class WorkflowMutation:
    mutation_id: str
    changed_path: str
    path: tuple
    operation: str
    before: object
    after: object
    expected_violation_ids: frozenset[str]


def _mutation(
    mutation_id,
    changed_path,
    path,
    before,
    after,
    *expected_violation_ids,
    operation="set",
):
    return WorkflowMutation(
        mutation_id=mutation_id,
        changed_path=changed_path,
        path=path,
        operation=operation,
        before=before,
        after=after,
        expected_violation_ids=frozenset(expected_violation_ids),
    )


WORKFLOW_MUTATIONS = (
    _mutation("M001", "name", ("name",), "tests", "not-tests", "WF001_NAME"),
    _mutation(
        "M002", "on.pull_request", ("on", "pull_request"),
        {"branches": ["main"]}, MISSING, "WF002_TRIGGERS", operation="delete",
    ),
    _mutation(
        "M003", "concurrency.group", ("concurrency", "group"),
        "ci-${{ github.workflow }}-${{ github.event_name }}-${{ github.event.pull_request.number || github.run_id }}",
        "ci-${{ github.ref }}", "WF003_CONCURRENCY_GROUP",
    ),
    _mutation(
        "M004", "concurrency.cancel-in-progress",
        ("concurrency", "cancel-in-progress"), True, False,
        "WF004_CONCURRENCY_CANCEL",
    ),
    _mutation(
        "M005", "concurrency.queue", ("concurrency", "queue"),
        MISSING, "single", "WF005_CONCURRENCY_QUEUE_ABSENT", operation="add",
    ),
    _mutation(
        "M006", "permissions.contents", ("permissions", "contents"),
        "read", "write", "WF006_TOP_LEVEL_PERMISSIONS",
    ),
    _mutation(
        "M007", "jobs.tests.permissions", ("jobs", "tests", "permissions"),
        MISSING, {"contents": "read"}, "WF007_JOB_PERMISSIONS_ABSENT",
        operation="add",
    ),
    _mutation(
        "M008", "jobs.ui-smoke", ("jobs", "ui-smoke"), PRESENT, MISSING,
        "WF008_JOB_SET", "WF011_RUNNERS", "WF012_TIMEOUTS",
        "WF013_PYTHON_VERSIONS", "WF014_NO_LIVE_API", "WF015_MPLBACKEND",
        "WF016_ACTION_SEQUENCE", "WF017_CHECKOUT_CREDENTIALS",
        "WF018_PIP_CACHES", "WF019_NODE_CACHE_AND_VERSION",
        "WF021_UI_INSTALL", "WF025_CONTINUE_ON_ERROR_ABSENT",
        "WF026_NPM_COMMANDS", "WF027_PLAYWRIGHT_INSTALL",
        "WF028_BROWSER_PATH_EXPORT", "WF029_SMOKE_COMMAND",
        "WF030_ARTIFACT_CONTRACT", "WF031_SUMMARY_CONTRACT",
        "WF033_UI_DEV_REQUIREMENTS_ABSENT", operation="delete",
    ),
    _mutation(
        "M009", "jobs.tests.name", ("jobs", "tests", "name"),
        MISSING, "Tests", "WF009_JOB_NAMES_ABSENT", operation="add",
    ),
    _mutation(
        "M010", "jobs.tests.strategy.fail-fast",
        ("jobs", "tests", "strategy", "fail-fast"), False, True,
        "WF010_TEST_STRATEGY",
    ),
    _mutation(
        "M011", "jobs.tests.strategy.matrix.os",
        ("jobs", "tests", "strategy", "matrix", "os"),
        ["ubuntu-latest", "windows-latest"], ["ubuntu-latest"],
        "WF010_TEST_STRATEGY",
    ),
    _mutation(
        "M012", "jobs.tests.runs-on", ("jobs", "tests", "runs-on"),
        "${{ matrix.os }}", "ubuntu-latest", "WF011_RUNNERS",
    ),
    _mutation(
        "M013", "jobs.ui-smoke.runs-on", ("jobs", "ui-smoke", "runs-on"),
        "ubuntu-latest", "windows-latest", "WF011_RUNNERS",
    ),
    _mutation(
        "M014", "jobs.tests.timeout-minutes",
        ("jobs", "tests", "timeout-minutes"), 20, 21, "WF012_TIMEOUTS",
    ),
    _mutation(
        "M015", "jobs.ui-smoke.timeout-minutes",
        ("jobs", "ui-smoke", "timeout-minutes"), 20, 21, "WF012_TIMEOUTS",
    ),
    _mutation(
        "M016", "jobs.tests.steps[1].with.python-version",
        ("jobs", "tests", "steps", 1, "with", "python-version"),
        "3.13", "3.12", "WF013_PYTHON_VERSIONS",
    ),
    _mutation(
        "M017", "jobs.ui-smoke.steps[1].with.python-version",
        ("jobs", "ui-smoke", "steps", 1, "with", "python-version"),
        "3.13", "3.12", "WF013_PYTHON_VERSIONS",
    ),
    _mutation(
        "M018", "jobs.tests.env.NO_LIVE_API",
        ("jobs", "tests", "env", "NO_LIVE_API"),
        "1", "0", "WF014_NO_LIVE_API",
    ),
    _mutation(
        "M019", "jobs.ui-smoke.env.NO_LIVE_API",
        ("jobs", "ui-smoke", "env", "NO_LIVE_API"),
        "1", "0", "WF014_NO_LIVE_API",
    ),
    _mutation(
        "M020", "jobs.tests.env.MPLBACKEND",
        ("jobs", "tests", "env", "MPLBACKEND"),
        "Agg", "TkAgg", "WF015_MPLBACKEND",
    ),
    _mutation(
        "M021", "jobs.ui-smoke.env.MPLBACKEND",
        ("jobs", "ui-smoke", "env", "MPLBACKEND"),
        "Agg", "TkAgg", "WF015_MPLBACKEND",
    ),
    _mutation(
        "M022", "jobs.tests.steps[0].uses",
        ("jobs", "tests", "steps", 0, "uses"),
        "actions/checkout@v7", "actions/checkout@v6",
        "WF016_ACTION_SEQUENCE",
    ),
    _mutation(
        "M023", "jobs.tests.steps[0].with.persist-credentials",
        ("jobs", "tests", "steps", 0, "with", "persist-credentials"),
        False, True, "WF017_CHECKOUT_CREDENTIALS",
    ),
    _mutation(
        "M024", "jobs.tests.steps[1].with.cache",
        ("jobs", "tests", "steps", 1, "with", "cache"),
        "pip", "", "WF018_PIP_CACHES",
    ),
    _mutation(
        "M025", "jobs.tests.steps[1].with.cache-dependency-path",
        ("jobs", "tests", "steps", 1, "with", "cache-dependency-path"),
        "requirements.txt\nrequirements-dev.txt\n", "requirements.txt",
        "WF018_PIP_CACHES",
    ),
    _mutation(
        "M026", "jobs.ui-smoke.steps[1].with.cache",
        ("jobs", "ui-smoke", "steps", 1, "with", "cache"),
        "pip", "", "WF018_PIP_CACHES",
    ),
    _mutation(
        "M027", "jobs.ui-smoke.steps[1].with.cache-dependency-path",
        ("jobs", "ui-smoke", "steps", 1, "with", "cache-dependency-path"),
        "requirements.txt", "requirements-dev.txt",
        "WF018_PIP_CACHES", "WF033_UI_DEV_REQUIREMENTS_ABSENT",
    ),
    _mutation(
        "M028", "jobs.ui-smoke.steps[4].with.node-version",
        ("jobs", "ui-smoke", "steps", 4, "with", "node-version"),
        "22", "20", "WF019_NODE_CACHE_AND_VERSION",
    ),
    _mutation(
        "M029", "jobs.ui-smoke.steps[4].with.cache",
        ("jobs", "ui-smoke", "steps", 4, "with", "cache"),
        "npm", "", "WF019_NODE_CACHE_AND_VERSION",
    ),
    _mutation(
        "M030", "jobs.ui-smoke.steps[4].with.cache-dependency-path",
        ("jobs", "ui-smoke", "steps", 4, "with", "cache-dependency-path"),
        "package-lock.json", "package.json", "WF019_NODE_CACHE_AND_VERSION",
    ),
    _mutation(
        "M031", "jobs.tests.steps[3].run",
        ("jobs", "tests", "steps", 3, "run"),
        "pip install -r requirements.txt -r requirements-dev.txt",
        "pip install -r requirements.txt", "WF020_TESTS_INSTALL",
    ),
    _mutation(
        "M032", "jobs.ui-smoke.steps[3].run",
        ("jobs", "ui-smoke", "steps", 3, "run"),
        "pip install -r requirements.txt", "pip install -r requirements-dev.txt",
        "WF021_UI_INSTALL", "WF033_UI_DEV_REQUIREMENTS_ABSENT",
    ),
    _mutation(
        "M033", "jobs.tests.steps[7].run",
        ("jobs", "tests", "steps", 7, "run"),
        "python -X utf8 -m pytest -q -p no:cacheprovider",
        "python -X utf8 -m pytest -q", "WF022_PYTEST_COMMAND",
    ),
    _mutation(
        "M034", "jobs.tests.steps[8].run",
        ("jobs", "tests", "steps", 8, "run"),
        "python -X utf8 -m unittest discover -b",
        "python -X utf8 -m unittest discover -q", "WF023_UNITTEST_COMMANDS",
    ),
    _mutation(
        "M035", "jobs.tests.steps", ("jobs", "tests", "steps"),
        MISSING, {"run": "python -X utf8 -m unittest discover"},
        "WF023_UNITTEST_COMMANDS", operation="append",
    ),
    _mutation(
        "M036", "jobs.tests.env.CANARY", ("jobs", "tests", "env", "CANARY"),
        MISSING, "${{ secrets.CANARY }}", "WF024_SECRETS_ABSENT",
        operation="add",
    ),
    _mutation(
        "M037", "jobs.ui-smoke.continue-on-error",
        ("jobs", "ui-smoke", "continue-on-error"), MISSING, True,
        "WF025_CONTINUE_ON_ERROR_ABSENT", operation="add",
    ),
    _mutation(
        "M038", "jobs.ui-smoke.steps[5].run",
        ("jobs", "ui-smoke", "steps", 5, "run"),
        "npm ci", "npm ci --ignore-scripts", "WF026_NPM_COMMANDS",
    ),
    _mutation(
        "M039", "jobs.ui-smoke.steps", ("jobs", "ui-smoke", "steps"),
        MISSING, {"run": "npm install"}, "WF026_NPM_COMMANDS",
        operation="append",
    ),
    _mutation(
        "M040", "jobs.ui-smoke.steps[6].run",
        ("jobs", "ui-smoke", "steps", 6, "run"),
        "npx playwright install --with-deps chromium",
        "npx playwright install chromium", "WF027_PLAYWRIGHT_INSTALL",
    ),
    _mutation(
        "M041", "jobs.ui-smoke.steps[7].run",
        ("jobs", "ui-smoke", "steps", 7, "run"),
        "node -e \"console.log('BROWSER_PATH=' + require('playwright').chromium.executablePath())\" >> \"$GITHUB_ENV\"",
        "echo BROWSER_PATH=missing", "WF028_BROWSER_PATH_EXPORT",
    ),
    _mutation(
        "M042", "jobs.ui-smoke.steps[8].id",
        ("jobs", "ui-smoke", "steps", 8, "id"),
        "smoke", "browser", "WF029_SMOKE_COMMAND",
    ),
    _mutation(
        "M043", "jobs.ui-smoke.steps[8].run",
        ("jobs", "ui-smoke", "steps", 8, "run"),
        "python -X utf8 scripts/ui_smoke.py --log-path \"${{ runner.temp }}/ui-smoke/ui-smoke.log\" --artifact-dir \"${{ runner.temp }}/ui-smoke\"",
        "python -X utf8 scripts/ui_smoke.py", "WF029_SMOKE_COMMAND",
    ),
    _mutation(
        "M044", "jobs.ui-smoke.steps[9].if",
        ("jobs", "ui-smoke", "steps", 9, "if"),
        "${{ always() && steps.smoke.outcome == 'failure' }}",
        "${{ failure() }}", "WF030_ARTIFACT_CONTRACT",
    ),
    _mutation(
        "M045", "jobs.ui-smoke.steps[9].uses",
        ("jobs", "ui-smoke", "steps", 9, "uses"),
        "actions/upload-artifact@v7", "actions/upload-artifact@v6",
        "WF016_ACTION_SEQUENCE", "WF030_ARTIFACT_CONTRACT",
    ),
    _mutation(
        "M046", "jobs.ui-smoke.steps[9].with.name",
        ("jobs", "ui-smoke", "steps", 9, "with", "name"),
        "ui-smoke-${{ github.run_id }}-${{ github.run_attempt }}",
        "ui-smoke", "WF030_ARTIFACT_CONTRACT",
    ),
    _mutation(
        "M047", "jobs.ui-smoke.steps[9].with.path",
        ("jobs", "ui-smoke", "steps", 9, "with", "path"),
        "${{ runner.temp }}/ui-smoke", "artifacts", "WF030_ARTIFACT_CONTRACT",
    ),
    _mutation(
        "M048", "jobs.ui-smoke.steps[9].with.if-no-files-found",
        ("jobs", "ui-smoke", "steps", 9, "with", "if-no-files-found"),
        "warn", "error", "WF030_ARTIFACT_CONTRACT",
    ),
    _mutation(
        "M049", "jobs.ui-smoke.steps[9].with.retention-days",
        ("jobs", "ui-smoke", "steps", 9, "with", "retention-days"),
        7, 1, "WF030_ARTIFACT_CONTRACT",
    ),
    _mutation(
        "M050", "jobs.ui-smoke.steps[10].if",
        ("jobs", "ui-smoke", "steps", 10, "if"),
        "${{ always() }}", "${{ success() }}", "WF031_SUMMARY_CONTRACT",
    ),
    _mutation(
        "M051", "jobs.ui-smoke.steps[10].run",
        ("jobs", "ui-smoke", "steps", 10, "run"),
        UI_SMOKE_SUMMARY_COMMAND,
        "echo summary", "WF031_SUMMARY_CONTRACT",
    ),
    _mutation(
        "M052", "jobs.ui-smoke.steps", ("jobs", "ui-smoke", "steps"),
        MISSING, {"name": "UI smoke (browser provisioning pending)", "run": "echo old"},
        "WF032_DISABLED_STEPS_ABSENT", operation="append",
    ),
    _mutation(
        "M053", "jobs.ui-smoke.steps", ("jobs", "ui-smoke", "steps"),
        MISSING, {"name": "Disabled probe", "if": "${{ false }}", "run": "echo old"},
        "WF032_DISABLED_STEPS_ABSENT", operation="append",
    ),
    _mutation(
        "M054", "jobs.ui-smoke.env.DEV_REQUIREMENTS",
        ("jobs", "ui-smoke", "env", "DEV_REQUIREMENTS"),
        MISSING, "requirements-dev.txt", "WF033_UI_DEV_REQUIREMENTS_ABSENT",
        operation="add",
    ),
)


def _normalize_workflow(workflow):
    normalized = copy.deepcopy(workflow) if isinstance(workflow, dict) else {}
    if True in normalized:
        normalized.setdefault("on", normalized[True])
        del normalized[True]
    return normalized


def _load_workflow(path=WORKFLOW):
    return _normalize_workflow(yaml.safe_load(path.read_text(encoding="utf-8")))


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _steps(job):
    steps = _mapping(job).get("steps", [])
    return steps if isinstance(steps, list) else []


def _action_step(steps, prefix):
    return next(
        (
            step
            for step in steps
            if isinstance(step, dict)
            and str(step.get("uses", "")).startswith(prefix)
        ),
        {},
    )


def _named_step(steps, name):
    return next(
        (
            step
            for step in steps
            if isinstance(step, dict) and step.get("name") == name
        ),
        {},
    )


def _run_commands(steps):
    return [
        step["run"]
        for step in steps
        if isinstance(step, dict) and "run" in step
    ]


def _contains_key(value, target):
    if isinstance(value, dict):
        return target in value or any(
            _contains_key(child, target) for child in value.values()
        )
    if isinstance(value, list):
        return any(_contains_key(child, target) for child in value)
    return False


def _workflow_contract_violations(workflow):
    workflow = _normalize_workflow(workflow)
    violations = set()

    def require(rule_id, condition):
        if not condition:
            violations.add(rule_id)

    jobs = _mapping(workflow.get("jobs"))
    tests = _mapping(jobs.get("tests"))
    ui = _mapping(jobs.get("ui-smoke"))
    tests_steps = _steps(tests)
    ui_steps = _steps(ui)
    tests_runs = _run_commands(tests_steps)
    ui_runs = _run_commands(ui_steps)
    concurrency = _mapping(workflow.get("concurrency"))

    require("WF001_NAME", workflow.get("name") == "tests")
    require(
        "WF002_TRIGGERS",
        workflow.get("on")
        == {
            "push": {"branches": ["main"]},
            "pull_request": {"branches": ["main"]},
            "workflow_dispatch": None,
        },
    )
    require(
        "WF003_CONCURRENCY_GROUP",
        concurrency.get("group")
        == "ci-${{ github.workflow }}-${{ github.event_name }}-${{ github.event.pull_request.number || github.run_id }}",
    )
    require(
        "WF004_CONCURRENCY_CANCEL",
        concurrency.get("cancel-in-progress") is True,
    )
    require("WF005_CONCURRENCY_QUEUE_ABSENT", "queue" not in concurrency)
    require(
        "WF006_TOP_LEVEL_PERMISSIONS",
        workflow.get("permissions") == {"contents": "read"},
    )
    require(
        "WF007_JOB_PERMISSIONS_ABSENT",
        all("permissions" not in _mapping(job) for job in jobs.values()),
    )
    require("WF008_JOB_SET", set(jobs) == {"tests", "ui-smoke"})
    require(
        "WF009_JOB_NAMES_ABSENT",
        all("name" not in _mapping(job) for job in jobs.values()),
    )
    require(
        "WF010_TEST_STRATEGY",
        tests.get("strategy")
        == {
            "fail-fast": False,
            "matrix": {"os": ["ubuntu-latest", "windows-latest"]},
        },
    )
    require(
        "WF011_RUNNERS",
        tests.get("runs-on") == "${{ matrix.os }}"
        and ui.get("runs-on") == "ubuntu-latest",
    )
    require(
        "WF012_TIMEOUTS",
        tests.get("timeout-minutes") == 20 and ui.get("timeout-minutes") == 20,
    )

    tests_python = _action_step(tests_steps, "actions/setup-python@")
    ui_python = _action_step(ui_steps, "actions/setup-python@")
    setup_node = _action_step(ui_steps, "actions/setup-node@")
    require(
        "WF013_PYTHON_VERSIONS",
        _mapping(tests_python.get("with")).get("python-version") == "3.13"
        and _mapping(ui_python.get("with")).get("python-version") == "3.13",
    )
    require(
        "WF014_NO_LIVE_API",
        _mapping(tests.get("env")).get("NO_LIVE_API") == "1"
        and _mapping(ui.get("env")).get("NO_LIVE_API") == "1",
    )
    require(
        "WF015_MPLBACKEND",
        _mapping(tests.get("env")).get("MPLBACKEND") == "Agg"
        and _mapping(ui.get("env")).get("MPLBACKEND") == "Agg",
    )

    action_steps = [
        step.get("uses")
        for job_steps in (tests_steps, ui_steps)
        for step in job_steps
        if isinstance(step, dict) and str(step.get("uses", "")).startswith("actions/")
    ]
    require(
        "WF016_ACTION_SEQUENCE",
        action_steps
        == [
            "actions/checkout@v7",
            "actions/setup-python@v7",
            "actions/checkout@v7",
            "actions/setup-python@v7",
            "actions/setup-node@v7",
            "actions/upload-artifact@v7",
        ],
    )
    checkout_steps = [
        step
        for job_steps in (tests_steps, ui_steps)
        for step in job_steps
        if isinstance(step, dict)
        and str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    require(
        "WF017_CHECKOUT_CREDENTIALS",
        len(checkout_steps) == 2
        and all(
            _mapping(step.get("with")).get("persist-credentials") is False
            for step in checkout_steps
        ),
    )
    require(
        "WF018_PIP_CACHES",
        _mapping(tests_python.get("with")).get("cache") == "pip"
        and _mapping(tests_python.get("with")).get("cache-dependency-path")
        == "requirements.txt\nrequirements-dev.txt\n"
        and _mapping(ui_python.get("with")).get("cache") == "pip"
        and _mapping(ui_python.get("with")).get("cache-dependency-path")
        == "requirements.txt",
    )
    require(
        "WF019_NODE_CACHE_AND_VERSION",
        _mapping(setup_node.get("with")).get("node-version") == "22"
        and _mapping(setup_node.get("with")).get("cache") == "npm"
        and _mapping(setup_node.get("with")).get("cache-dependency-path")
        == "package-lock.json",
    )
    require(
        "WF020_TESTS_INSTALL",
        "pip install -r requirements.txt -r requirements-dev.txt" in tests_runs,
    )
    require(
        "WF021_UI_INSTALL",
        "pip install -r requirements.txt" in ui_runs,
    )
    require(
        "WF022_PYTEST_COMMAND",
        "python -X utf8 -m pytest -q -p no:cacheprovider" in tests_runs,
    )
    require(
        "WF023_UNITTEST_COMMANDS",
        "python -X utf8 -m unittest discover -b" in tests_runs
        and "python -X utf8 -m unittest discover" not in tests_runs,
    )
    require(
        "WF024_SECRETS_ABSENT",
        "secrets."
        not in json.dumps(workflow, ensure_ascii=False, sort_keys=True),
    )
    require(
        "WF025_CONTINUE_ON_ERROR_ABSENT",
        bool(ui) and not _contains_key(ui, "continue-on-error"),
    )
    require(
        "WF026_NPM_COMMANDS",
        "npm ci" in ui_runs and "npm install" not in ui_runs,
    )
    require(
        "WF027_PLAYWRIGHT_INSTALL",
        "npx playwright install --with-deps chromium" in ui_runs,
    )
    require(
        "WF028_BROWSER_PATH_EXPORT",
        "node -e \"console.log('BROWSER_PATH=' + require('playwright').chromium.executablePath())\" >> \"$GITHUB_ENV\""
        in ui_runs,
    )

    smoke = _named_step(ui_steps, "Run UI smoke")
    require(
        "WF029_SMOKE_COMMAND",
        smoke.get("id") == "smoke"
        and smoke.get("run")
        == "python -X utf8 scripts/ui_smoke.py --log-path \"${{ runner.temp }}/ui-smoke/ui-smoke.log\" --artifact-dir \"${{ runner.temp }}/ui-smoke\"",
    )
    artifact = _named_step(ui_steps, "Upload UI smoke failure artifacts")
    artifact_with = _mapping(artifact.get("with"))
    require(
        "WF030_ARTIFACT_CONTRACT",
        artifact.get("uses") == "actions/upload-artifact@v7"
        and artifact.get("if")
        == "${{ always() && steps.smoke.outcome == 'failure' }}"
        and artifact_with.get("name")
        == "ui-smoke-${{ github.run_id }}-${{ github.run_attempt }}"
        and artifact_with.get("path") == "${{ runner.temp }}/ui-smoke"
        and artifact_with.get("if-no-files-found") == "warn"
        and artifact_with.get("retention-days") == 7,
    )
    summary = _named_step(ui_steps, "Record UI smoke observation")
    summary_run = str(summary.get("run", ""))
    require(
        "WF031_SUMMARY_CONTRACT",
        summary.get("if") == "${{ always() }}"
        and all(
            value in summary_run
            for value in (
                "steps.smoke.outcome",
                "github.event_name",
                "github.sha",
                "GITHUB_STEP_SUMMARY",
            )
        ),
    )
    require(
        "WF032_DISABLED_STEPS_ABSENT",
        all(
            step.get("name") != "UI smoke (browser provisioning pending)"
            and step.get("if") not in (False, "${{ false }}")
            for step in ui_steps
            if isinstance(step, dict)
        ),
    )
    require(
        "WF033_UI_DEV_REQUIREMENTS_ABSENT",
        bool(ui)
        and "requirements-dev.txt"
        not in json.dumps(ui, ensure_ascii=False, sort_keys=True),
    )
    return frozenset(violations)


def _path_value(root, path):
    value = root
    for part in path:
        value = value[part]
    return value


def _assert_mutation_precondition(workflow, case):
    if case.operation == "append":
        target = _path_value(workflow, case.path)
        if (
            not isinstance(target, list)
            or case.before != MISSING
            or case.after in target
        ):
            raise AssertionError(
                f"mutation precondition mismatch: {case.mutation_id}"
            )
        return

    parent = _path_value(workflow, case.path[:-1])
    key = case.path[-1]
    if case.operation == "add":
        if case.before != MISSING or key in parent:
            raise AssertionError(
                f"mutation precondition mismatch: {case.mutation_id}"
            )
        return

    if key not in parent:
        raise AssertionError(f"mutation precondition mismatch: {case.mutation_id}")
    if case.before != PRESENT and parent[key] != case.before:
        raise AssertionError(f"mutation precondition mismatch: {case.mutation_id}")


def _apply_workflow_mutation(workflow, case):
    _assert_mutation_precondition(workflow, case)
    mutated = copy.deepcopy(workflow)
    if case.operation == "append":
        _path_value(mutated, case.path).append(copy.deepcopy(case.after))
        return mutated

    parent = _path_value(mutated, case.path[:-1])
    key = case.path[-1]
    if case.operation == "delete":
        del parent[key]
    elif case.operation == "add":
        if key in parent:
            raise AssertionError(f"mutation target already exists: {case.changed_path}")
        parent[key] = copy.deepcopy(case.after)
    elif case.operation == "set":
        parent[key] = copy.deepcopy(case.after)
    else:
        raise AssertionError(f"unknown mutation operation: {case.operation}")
    return mutated


def _evaluate_workflow_mutation(workflow, case):
    mutated = _apply_workflow_mutation(workflow, case)
    actual = _workflow_contract_violations(mutated)
    killed = actual == case.expected_violation_ids and bool(actual)
    return {
        "mutation_id": case.mutation_id,
        "changed_path": case.changed_path,
        "before": case.before,
        "after": case.after,
        "expected_violation_ids": case.expected_violation_ids,
        "actual_violation_ids": actual,
        "killed": killed,
        "survived": not killed,
    }


def _markdown_section(text: str, heading: str) -> str:
    lines = text.splitlines()
    try:
        start = lines.index(heading)
    except ValueError as exc:
        raise AssertionError(f"missing-contract: Markdown缺少章节 {heading}") from exc
    level = len(heading) - len(heading.lstrip("#"))
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = re.match(r"^(#+)\s", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    return "\n".join(lines[start:end])


class WorkflowContractTest(unittest.TestCase):
    def test_actual_workflow_has_zero_contract_violations(self):
        self.assertTrue(WORKFLOW.is_file())
        workflow = _load_workflow(WORKFLOW)
        self.assertEqual(_workflow_contract_violations(workflow), frozenset())

    def test_loader_normalizes_pyyaml_boolean_on_key(self):
        parsed = yaml.safe_load("on:\n  workflow_dispatch:\n")
        self.assertIn(True, parsed)
        normalized = _normalize_workflow(parsed)
        self.assertNotIn(True, normalized)
        self.assertEqual(normalized["on"], {"workflow_dispatch": None})

    def test_every_canonical_rule_has_a_mutation(self):
        mutation_ids = [case.mutation_id for case in WORKFLOW_MUTATIONS]
        self.assertEqual(len(mutation_ids), len(set(mutation_ids)))
        covered = frozenset(
            rule_id
            for case in WORKFLOW_MUTATIONS
            for rule_id in case.expected_violation_ids
        )
        self.assertEqual(covered, CANONICAL_WORKFLOW_RULE_IDS)
        self.assertTrue(NON_WORKFLOW_CONTRACTS)

    def test_all_workflow_mutations_are_killed(self):
        workflow = _load_workflow(WORKFLOW)
        for case in WORKFLOW_MUTATIONS:
            with self.subTest(mutation_id=case.mutation_id):
                result = _evaluate_workflow_mutation(workflow, case)
                self.assertEqual(
                    set(result),
                    {
                        "mutation_id",
                        "changed_path",
                        "before",
                        "after",
                        "expected_violation_ids",
                        "actual_violation_ids",
                        "killed",
                        "survived",
                    },
                )
                self.assertEqual(
                    result["actual_violation_ids"],
                    result["expected_violation_ids"],
                )
                self.assertTrue(result["killed"])
                self.assertFalse(result["survived"])

        drifted = copy.deepcopy(workflow)
        drifted["name"] = "drifted-before-mutation"
        with self.assertRaisesRegex(AssertionError, "mutation precondition"):
            _apply_workflow_mutation(drifted, WORKFLOW_MUTATIONS[0])

    def test_ci_portability_does_not_parse_tests_workflow(self):
        source = (ROOT / "test_ci_portability.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        exact_strings = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertNotIn("yaml", imported_modules)
        self.assertNotIn("tests.yml", exact_strings)
        self.assertNotIn("yaml.safe_load", source)


class UiSmokeSpecificContractTest(unittest.TestCase):
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


class UiSmokeDocumentationContractTest(unittest.TestCase):
    def test_contributing_documents_blocking_gate_and_zero_api_evidence(self):
        section = _markdown_section(
            CONTRIBUTING.read_text(encoding="utf-8"),
            "## 浏览器 smoke 阻断模式",
        )
        self.assertNotIn(
            "观察期已经完成",
            section,
            "stale-lock: CONTRIBUTING仍以观察期描述当前ui-smoke",
        )
        required = (
            "阻断模式",
            "截至 2026-08-31",
            "`main` 分支保护",
            "required check",
            "workflow 不使用 `continue-on-error`",
            "失败会直接阻断 workflow",
            "NO_LIVE_API=1",
            "start_background_collection",
            "load_calendar",
            "临时数据目录",
            "临时端口",
            "三库与配额台账哈希不变",
        )
        self.assertEqual(
            [item for item in required if item not in section],
            [],
            "missing-contract: CONTRIBUTING缺少带时点的required阻断边界",
        )

    def test_contributing_documents_smoke_route_coverage_boundary(self):
        section = _markdown_section(
            CONTRIBUTING.read_text(encoding="utf-8"),
            "## 浏览器 smoke 阻断模式",
        )
        self.assertNotIn(
            "尚未覆盖 `/price_hint`、`/feedback`、暂停等其余 CRUD 路由",
            section,
            "stale-lock: CONTRIBUTING锁定了已过时的smoke覆盖矩阵",
        )
        self.assertNotIn(
            "`/feedback` 仍未覆盖",
            section,
            "stale-lock: CONTRIBUTING仍把通知反馈深链记录为未覆盖",
        )
        for stale_text in ("只证明这次间接访问", "没有专项业务语义断言"):
            self.assertNotIn(
                stale_text,
                section,
                "stale-lock: CONTRIBUTING仍把/price_hint记录为仅间接访问",
            )
        required = (
            "完整 `web_form.app`",
            "`/`",
            "`/settings`",
            "`/subscribe`",
            "`/success`",
            "`/subscriptions`",
            "`/subscription/<subscription_id>/delete`",
            "`/subscriptions/<subscription_id>/toggle`",
            "暂停与恢复",
            "`/subscriptions/<subscription_id>/quick-update`",
            "`/price_hint`",
            "请求参数",
            "无数据 JSON",
            "DOM 回退",
            "route type",
            "隐藏字段",
            "有数据价格显示文案",
            "尚未裁决",
            "`/feedback`",
            "通知反馈深链",
            "GET",
            "原生必填校验",
            "有效 CSRF POST",
            "临时 `feedback.json`",
            "已收到反馈",
            "零真实 SMTP",
            "服务端无效字段校验",
            "已登记缺口",
            "普通 Web 页面",
            "不宣称",
            "smoke 绿不等于 Web 全绿",
            "扩展 smoke 驱动",
        )
        self.assertEqual(
            [item for item in required if item not in section],
            [],
            "missing-contract: CONTRIBUTING缺少实测smoke路径与未覆盖边界",
        )


if __name__ == "__main__":
    unittest.main()
