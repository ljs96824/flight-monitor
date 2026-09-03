import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from serpapi_credentials import SERPAPI_KEY_ALIASES


ROOT = Path(__file__).resolve().parent
README = ROOT / "README.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
ENV_EXAMPLE = ROOT / ".env.example"
LICENSE = ROOT / "LICENSE"
REQUIREMENTS_INPUT = ROOT / "requirements.in"
DEV_REQUIREMENTS_INPUT = ROOT / "requirements-dev.in"
DEV_REQUIREMENTS_LOCK = ROOT / "requirements-dev.txt"
RUNTIME_BACKUP_MANUAL = ROOT / "docs" / "runtime-backup-and-restore.md"
EXTERNAL_NETWORK_COVERAGE = (
    ROOT / "docs" / "external-network-no-live-api-coverage-2026-09-03.md"
)

EXPECTED_SECTIONS = (
    "定位",
    "设计哲学",
    "功能清单",
    "架构",
    "数据源与配额经济学",
    "快速开始",
    "日常运行",
    "工程纪律",
    "目录导览",
    "限制与非目标",
)

ACTIVE_SECRET_VARIABLES = {
    "JUHE_FLIGHT_KEY",
    "SERPAPI_KEY",
    "SERPAPI_API_KEY",
    "SERP_API_KEY",
    "DUFFEL_TOKEN",
    "PUSHPLUS_TOKEN",
    "SMTP_USER",
    "SMTP_PASS",
    "PYTHONANYWHERE_TOKEN",
    "SHARED_DETAIL_TOKEN",
    "FLASK_SECRET_KEY",
}

ACTIVE_ENV_VARIABLES = ACTIVE_SECRET_VARIABLES | {
    "SMTP_PROVIDER",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_SSL",
    "TEST_EMAIL_TO",
    "PYTHONANYWHERE_USER",
    "SUBSCRIPTION_FORM_URL",
    "PYTHONANYWHERE_FORM_URL",
    "FEEDBACK_NOTIFY_EMAIL",
    "JUHE_FLIGHT_ENDPOINT",
    "JUHE_ONTIME_ENDPOINT",
    "JUHE_QUOTA_CODES",
    "BASKET_SENTINEL_AFTER",
    "CHECK_INTERVAL_HOURS",
    "FLIGHT_DEBUG_FULL_ARRAYS",
    "COLLECTION_LOCK_PATH",
    "SESSION_COOKIE_SECURE",
    "CSRF_TOKEN_TTL_SECONDS",
    "COLLECTION_STARTUP_TIMEOUT_SECONDS",
    "MIN_SAMPLE_FOR_PRICE_SIGNAL",
    "MIN_SAMPLE_FOR_TCURVE",
    "TCURVE_MIN_CELLS",
    "AGREEMENT_WINDOW_DAYS",
    "MIN_PAIRS_FOR_AGREEMENT",
    "MIN_PATTERN_N",
    "MIN_OBS_FOR_LEVEL",
    "MIN_BACKTEST_CASES",
    "SKILL_GATE_IMPROVEMENT",
    "HOLIDAY_SHOULDER_DAYS",
    "SNAPSHOT_DEPART_DATE",
    "SNAPSHOT_RETURN_DATE",
    "BROWSER_PATH",
    "EDGE_PATH",
}

SAFETY_ONLY_ENV_VARIABLES = {"NO_LIVE_API"}
DOCUMENTED_ENV_VARIABLES = ACTIVE_ENV_VARIABLES | SAFETY_ONLY_ENV_VARIABLES

RETIRED_OR_DORMANT_SOURCE_VARIABLES = {
    "HASDATA_KEY",
    "SEARCHAPI_KEY",
    "TRAVELPAYOUTS_TOKEN",
    "RAPIDAPI_KEY",
}


def _dotenv_entries(text: str) -> list[dict[str, str | bool]]:
    entries = []
    for line in text.splitlines():
        match = re.match(
            r"^\s*(?P<comment>#\s*)?(?P<name>[A-Z][A-Z0-9_]*)\s*=(?P<value>.*)$",
            line,
        )
        if match:
            entries.append(
                {
                    "variable_name": match.group("name"),
                    "commented": bool(match.group("comment")),
                    "raw_value_present": bool(match.group("value").strip()),
                }
            )
    return entries


def _dotenv_names(text: str) -> set[str]:
    return {str(entry["variable_name"]) for entry in _dotenv_entries(text)}


def _dotenv_section(text: str, heading: str) -> str:
    lines = text.splitlines()
    marker = re.compile(rf"^#\s*=+\s*{re.escape(heading)}\s*=+\s*$")
    start = next((index for index, line in enumerate(lines) if marker.match(line)), None)
    if start is None:
        return ""
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if re.match(r"^#\s*=+\s*.+?\s*=+\s*$", lines[index])
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _local_markdown_links(text: str) -> list[str]:
    links = re.findall(r"\[[^]]+\]\(([^)]+)\)", text)
    return [
        item.split("#", 1)[0]
        for item in links
        if item
        and not item.startswith(("http://", "https://", "#", "mailto:"))
        and not item.startswith("../../actions/")
    ]


def _script_references(text: str) -> set[str]:
    return {
        item.replace("\\", "/")
        for item in re.findall(
            r"(?<![A-Za-z0-9_.-])((?:scripts|analytics)/[A-Za-z0-9_.-]+\.py|[A-Za-z0-9_.-]+\.py)",
            text.replace("\\", "/"),
        )
    }


def _documented_python_commands(text: str) -> set[str]:
    commands = set()
    for line in text.replace("\\", "/").splitlines():
        stripped = line.strip()
        if not re.match(r"^(?:python|python3(?:\.13)?)\s", stripped):
            continue
        match = re.search(
            r"((?:scripts|analytics)/[A-Za-z0-9_.-]+\.py|[A-Za-z0-9_.-]+\.py)",
            stripped,
        )
        if match:
            commands.add(match.group(1))
    return commands


def _fenced_blocks(text: str, language: str) -> list[str]:
    return re.findall(
        rf"```{re.escape(language)}\s*\r?\n(.*?)```",
        text,
        flags=re.DOTALL,
    )


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


class DocsAccuracyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme = README.read_text(encoding="utf-8")
        cls.env_example = ENV_EXAMPLE.read_text(encoding="utf-8")

    def test_readme_has_the_approved_ten_section_skeleton(self):
        headings = re.findall(r"^##\s+(?:\d+\.\s*)?(.+?)\s*$", self.readme, re.MULTILINE)
        for section in EXPECTED_SECTIONS:
            self.assertTrue(
                any(section in heading for heading in headings),
                f"README缺少章节: {section}",
            )
        self.assertIn("An evidence-first flight monitoring system", self.readme)
        self.assertIn(
            "[![tests](../../actions/workflows/tests.yml/badge.svg)]",
            self.readme,
        )
        self.assertIn("### License", self.readme)
        self.assertIn("### 开发方式", self.readme)

    def test_license_file_uses_mit(self):
        self.assertTrue(LICENSE.is_file(), "缺少LICENSE文件")
        self.assertIn("MIT License", LICENSE.read_text(encoding="utf-8"))

    def test_readme_has_no_unresolved_placeholders(self):
        self.assertNotIn("待定", self.readme)
        self.assertNotRegex(
            self.readme,
            r"(?im)^\s*(?:TODO|TBD)(?:\s|:|$)",
        )

    def test_all_linked_paths_and_python_script_references_exist(self):
        missing = []
        for relative in _local_markdown_links(self.readme):
            if not (ROOT / relative).exists():
                missing.append(relative)
        for relative in sorted(_script_references(self.readme)):
            if not (ROOT / relative).is_file():
                missing.append(relative)
        self.assertEqual(missing, [])

    def test_env_example_and_readme_match_active_secret_contract(self):
        env_names = _dotenv_names(self.env_example)
        aliases = set(SERPAPI_KEY_ALIASES)
        readme_aliases = {
            name for name in aliases if re.search(rf"\b{re.escape(name)}\b", self.readme)
        }

        self.assertEqual(readme_aliases, aliases)
        self.assertTrue(aliases <= env_names)
        self.assertTrue(ACTIVE_SECRET_VARIABLES <= env_names)
        self.assertEqual(env_names, DOCUMENTED_ENV_VARIABLES)
        self.assertEqual(RETIRED_OR_DORMANT_SOURCE_VARIABLES & env_names, set())
        self.assertNotIn("ljs96824", self.env_example)
        self.assertNotIn("@", self.env_example)
        self.assertNotRegex(self.env_example, r"(?i)(secret|token|key)_[a-z0-9]{16,}")

    def test_safety_only_env_variables_are_commented(self):
        entries = _dotenv_entries(self.env_example)
        by_name = {
            name: [
                entry
                for entry in entries
                if entry["variable_name"] == name
            ]
            for name in SAFETY_ONLY_ENV_VARIABLES
        }
        self.assertEqual(
            [name for name, matching in by_name.items() if not matching],
            [],
            "missing-contract: 缺少安全专用变量",
        )
        self.assertEqual(
            [
                name
                for name, matching in by_name.items()
                if not all(entry["commented"] for entry in matching)
            ],
            [],
            "safety-only变量不得默认启用",
        )
        no_live_entries = by_name["NO_LIVE_API"]
        self.assertEqual(len(no_live_entries), 1)
        self.assertTrue(no_live_entries[0]["raw_value_present"])

    def test_no_live_api_safety_section_contract(self):
        section = _dotenv_section(self.env_example, "测试与受控审计安全开关")
        self.assertTrue(section, "missing-contract: 缺少NO_LIVE_API安全开关分区")
        self.assertEqual(
            [term for term in ("CI", "离线测试", "受控审计") if term not in section],
            [],
        )
        self.assertRegex(section, r"只有精确值\s*1\s*生效")
        self.assertIn("不保护", section)
        self.assertEqual(
            [
                term
                for term in ("PA Files", "Juhe", "SerpAPI", "Duffel")
                if term not in section
            ],
            [],
        )

    def test_readme_links_no_live_api_coverage_from_env_setup(self):
        section = _markdown_section(self.readme, "### 6.3 创建 `.env`")
        self.assertEqual(
            [
                term
                for term in (
                    "NO_LIVE_API",
                    "CI",
                    "受控离线验证",
                    "不是全局断网开关",
                    "docs/external-network-no-live-api-coverage-2026-09-03.md",
                )
                if term not in section
            ],
            [],
            "missing-contract: README 6.3缺少NO_LIVE_API安全边界导航",
        )

    def test_quick_start_contains_required_install_run_and_scheduler_contracts(self):
        required = (
            "Python 3.13",
            "python -m pip install -r requirements.txt -r requirements-dev.txt",
            "requirements.in",
            "requirements-dev.in",
            "requirements-dev.txt",
            "pip-compile",
            "锁文件由 pip-compile 生成，勿手改",
            "python -u -X utf8 run_web.py",
            "python -X utf8 -m pytest -q",
            "python -X utf8 -m unittest discover",
            "schtasks.exe /Create",
            "30 9 * * *",
            "git pull --ff-only",
            "Reload",
            "出站",
        )
        missing = [item for item in required if item not in self.readme]
        self.assertEqual(missing, [])
        self.assertTrue(REQUIREMENTS_INPUT.is_file())
        self.assertTrue(DEV_REQUIREMENTS_INPUT.is_file())
        self.assertTrue(DEV_REQUIREMENTS_LOCK.is_file())

    def test_readme_source_and_quota_claims_match_current_profiles(self):
        from source_profiles import ROUTE_SOURCE_PROFILES

        international = ROUTE_SOURCE_PROFILES["international"]
        active = {item["name"] for item in international["sources"]}
        retired = {item["name"] for item in international["retired_sources"]}

        self.assertEqual(active, {"juhe", "serpapi", "duffel"})
        self.assertEqual(retired, {"hasdata"})
        for phrase in (
            "聚合数据（Juhe）",
            "SerpAPI",
            "Duffel",
            "HasData",
            "550",
            "250",
            "2026-08-14",
        ):
            self.assertIn(phrase, self.readme)
        self.assertNotIn("monitor.yml", self.readme)

    def test_readme_documents_blocking_smoke_and_subscription_fact_sources(self):
        source_section = _markdown_section(
            self.readme,
            "## 5. 数据源与配额经济学",
        )
        runtime_section = _markdown_section(
            self.readme,
            "### 6.2 创建本地运行配置",
        )
        test_section = _markdown_section(
            self.readme,
            "### 6.4 运行离线测试",
        )

        self.assertNotRegex(
            source_section,
            r"本地订阅\s*属于运行事实.*data/runtime_config\.yaml",
            "stale-lock: README仍把现行订阅写成runtime_config运行事实",
        )
        self.assertNotIn(
            "目标日期、研究开关及本地订阅",
            runtime_section,
            "stale-lock: README运行配置步骤仍要求把真实订阅写入runtime_config",
        )
        self.assertNotIn(
            "观察模式",
            test_section,
            "stale-lock: README仍把ui-smoke描述为观察模式",
        )

        required = (
            "现行 Web CRUD、订阅采集、尝试状态与 PA 同步",
            "`data/subscriptions.json`",
            "权威持久化源",
            "`data/runtime_config.yaml`",
            "`subscriptions: []`",
            "配置校验",
            "legacy 迁移",
            "6b 完成前必须保持为空数组",
            "不得写入真实订阅",
            "不得提前删除该字段",
            "`validate_runtime_config`",
        )
        combined = "\n".join((source_section, runtime_section))
        self.assertEqual(
            [item for item in required if item not in combined],
            [],
            "missing-contract: README缺少订阅事实源与兼容占位边界",
        )
        self.assertIn(
            "阻断",
            test_section,
            "missing-contract: README缺少ui-smoke阻断语义",
        )

    def test_contributing_delivery_evidence_contract_is_normative_and_complete(self):
        section = _markdown_section(
            CONTRIBUTING.read_text(encoding="utf-8"),
            "## 交付声明与证据要求",
        )
        normative_terms = (
            "未来交付声明的规范性合同",
            "docs/codex-operational-evidence-audit-2026-08-30.md",
            "形成过程的历史出处",
            "后续规则更新只改 `CONTRIBUTING.md`",
            "不回写历史审计报告",
            "真实输出属于每次交付报告",
            "不写进静态规范",
        )
        self.assertEqual(
            [item for item in normative_terms if item not in section],
            [],
            "missing-contract: CONTRIBUTING缺少规范源与真实输出边界",
        )

        claims = {
            "### 1. 声明：已创建 PR": (
                "gh pr view <N> --repo ljs96824/flight-monitor --json number,state,url,baseRefOid,headRefOid,headRefName,commits,files",
                "state=OPEN",
                "baseRefOid == 任务基线",
                "headRefOid == 本地提交 SHA",
                "len(commits) == 1",
            ),
            "### 2. 声明：已推送": (
                "LOCAL=$(git rev-parse HEAD)",
                "git ls-remote --heads origin refs/heads/<branch>",
                "LOCAL == REMOTE",
                "命令有输出不等于声明成立",
            ),
            "### 3. 声明：main 为 X": (
                "git fetch --prune origin",
                "git branch --show-current",
                "git rev-parse refs/heads/main",
                "git rev-parse origin/main",
                "当前分支为 `main`",
                "refs/heads/main == origin/main == X",
            ),
            "### 4. 声明：CI 全绿": (
                "run_id",
                "head_sha",
                "jobs[].name/status/conclusion",
                "run.head_sha == 被验收提交 SHA",
                "completed/success",
                "PR 分支 checks 不等于 main post-merge checks",
            ),
            "### 5. 声明：哈希不变": (
                "before",
                "after",
                "静默窗口起止",
                "不与历史数值比较",
                "prices.db",
                "observations.sqlite3",
                "api_usage.json",
            ),
            "### 6. 声明：某文件无消费者": (
                "扫描命令",
                "命中数",
                "仓库内",
                "仓库外",
                "user_reported",
            ),
            "### 7. 声明：worktree 合规": (
                "git worktree list --porcelain",
                "项目目录与 `data/` 目录之外",
                "固定路径",
                "任务结束清理",
            ),
            "### 8. 声明：提交身份已核对": (
                "git config --get user.name",
                "git config --get user.email",
                "提交前",
            ),
            "### 9. 声明：可以删除远端 PR 分支": (
                "state=MERGED",
                "git merge-base --is-ancestor <MERGE_SHA> origin/main",
                "git ls-remote --heads origin refs/heads/<branch>",
                "已验收 head",
                "无其他 open PR 使用该分支",
                "不得以网页提示语或本地 `git pull` 单独作为依据",
                "本地 `main` 同步是独立收尾动作",
            ),
            "### 10. 声明：冻结 SHA 未漂移": (
                "完整 64 位",
                "fixture 路径",
                "生成命令",
                "字节数",
                "不得直接更新期望值",
            ),
        }
        self.assertEqual(len(claims), 10)
        for heading, required_terms in claims.items():
            with self.subTest(claim=heading):
                claim_section = _markdown_section(section, heading)
                self.assertIn("声明", claim_section)
                self.assertIn("命令", claim_section)
                self.assertIn("必填字段", claim_section)
                self.assertIn("**通过条件**", claim_section)
                self.assertEqual(
                    [item for item in required_terms if item not in claim_section],
                    [],
                    f"missing-contract: {heading} 证据合同不完整",
                )

    def test_external_network_no_live_api_coverage_contract(self):
        self.assertTrue(
            EXTERNAL_NETWORK_COVERAGE.is_file(),
            "missing-contract: 缺少外部网络NO_LIVE_API覆盖清单",
        )
        text = EXTERNAL_NETWORK_COVERAGE.read_text(encoding="utf-8")
        snapshot_section = _markdown_section(text, "## 快照与总声明")
        self.assertEqual(
            [
                item
                for item in (
                    "c59b8bc16041df97cad8baa7650b5f211a846870",
                    "Asia/Shanghai",
                    "静态快照",
                    "source_profiles",
                    "新增适配器",
                    "gateway",
                    "SMTP",
                    "PushPlus",
                    "不是全局网络防火墙",
                )
                if item not in snapshot_section
            ],
            [],
            "missing-contract: 快照点、复核触发器或非全局防火墙边界不完整",
        )

        scan_section = _markdown_section(text, "## 范围完备性扫描")
        self.assertIn("生产 Python", scan_section)
        self.assertIn("测试文件", scan_section)
        self.assertIn("异常项", scan_section)

        active_section = _markdown_section(text, "## 现役网络路径")
        actual_service_ids = set(
            re.findall(
                r"^\|\s*`([a-z][a-z0-9_]*)`\s*\|",
                active_section,
                flags=re.MULTILINE,
            )
        )
        self.assertEqual(
            actual_service_ids,
            {
                "smtp_email",
                "pushplus",
                "pa_subscription_download",
                "pa_payload_upload",
                "juhe",
                "serpapi",
                "duffel",
            },
        )
        for term in (
            "gate_status",
            "operational_controls",
            "runtime_contracts",
            "evidence_basis",
            "evidence_level",
        ):
            self.assertIn(term, active_section)

        inactive_section = _markdown_section(text, "## 非现役或退役适配器")
        self.assertIn("直接", inactive_section)
        self.assertIn("NO_LIVE_API", inactive_section)
        self.assertIn("当前调用方", inactive_section)

        controls_section = _markdown_section(text, "## 控制层分类")
        for term in ("prevention", "containment", "detection", "不能阻止"):
            self.assertIn(term, controls_section)

        contracts_section = _markdown_section(
            text,
            "## runtime_contracts 与 documentation_contract",
        )
        for term in (
            "SMTP",
            "PushPlus",
            "私有 gateway 调用图",
            "runtime_contracts",
            "documentation_contract",
        ):
            self.assertIn(term, contracts_section)

        gaps_section = _markdown_section(text, "## 当前缺口")
        for term in (
            "PA 订阅下载",
            "PA payload 上传",
            "Juhe",
            "SerpAPI",
            "Duffel",
            "当前无门，依赖上游控制及调用方测试隔离",
        ):
            self.assertIn(term, gaps_section)

        boundaries_section = _markdown_section(text, "## 已知边界")
        for term in (
            "process",
            ".env",
            "effective",
            "import",
            "patch target",
            "WSGI",
            "计划任务",
            "仓库内 Python",
        ):
            self.assertIn(term, boundaries_section)

    def test_documented_python_entrypoints_have_offline_help_or_import_probe(self):
        probes = {
            "run_web.py": [sys.executable, "-X", "utf8", "-m", "py_compile", "run_web.py"],
            "main.py": [sys.executable, "-X", "utf8", "-m", "py_compile", "main.py"],
            "basket_collect.py": [sys.executable, "-X", "utf8", "-m", "py_compile", "basket_collect.py"],
            "scripts/snapshot_run.py": [sys.executable, "-X", "utf8", "scripts/snapshot_run.py", "--help"],
            "scripts/tcurve_report.py": [sys.executable, "-X", "utf8", "scripts/tcurve_report.py", "--help"],
            "scripts/provenance_report.py": [sys.executable, "-X", "utf8", "scripts/provenance_report.py", "--help"],
            "scripts/forecast_report.py": [sys.executable, "-X", "utf8", "scripts/forecast_report.py", "--help"],
            "scripts/list_expired_subs.py": [sys.executable, "-X", "utf8", "scripts/list_expired_subs.py", "--help"],
            "scripts/list_unresolvable_subs.py": [sys.executable, "-X", "utf8", "scripts/list_unresolvable_subs.py", "--help"],
            "scripts/list_incomplete_notification_subs.py": [sys.executable, "-X", "utf8", "scripts/list_incomplete_notification_subs.py", "--help"],
            "scripts/ui_smoke.py": [sys.executable, "-X", "utf8", "-m", "py_compile", "scripts/ui_smoke.py"],
            "scripts/migrate_runtime_config.py": [
                sys.executable,
                "-X",
                "utf8",
                "scripts/migrate_runtime_config.py",
                "--help",
            ],
            "scripts/initialize_api_usage.py": [
                sys.executable,
                "-X",
                "utf8",
                "scripts/initialize_api_usage.py",
                "--help",
            ],
        }
        referenced = _documented_python_commands(self.readme)
        unchecked = sorted(
            item
            for item in referenced
            if item not in probes and item not in {"test_docs_accuracy.py", "test_frozen_email_baseline.py"}
        )
        self.assertEqual(unchecked, [], f"README脚本缺少离线探针: {unchecked}")

        for relative in sorted(referenced & probes.keys()):
            with self.subTest(script=relative):
                result = subprocess.run(
                    probes[relative],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    check=False,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    f"{relative}离线探针失败:\n{result.stdout}\n{result.stderr}",
                )

    def test_all_documented_non_api_commands_have_safe_probes(self):
        self.assertEqual(sys.version_info[:2], (3, 13))
        cli_python = shutil.which("python") or sys.executable
        commands = {
            line.strip()
            for block in _fenced_blocks(self.readme, "bash")
            for line in block.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        copy_command = (
            'python -c "from pathlib import Path; src=Path(\'.env.example\'); '
            "dst=Path('.env'); dst.exists() or dst.write_bytes(src.read_bytes())\""
        )
        live_commands = {
            "python -u -X utf8 main.py",
            "python -u -X utf8 basket_collect.py",
        }
        path_only_commands = {"cd ~/flight-monitor"}
        probes = {
            "python --version": [cli_python, "--version"],
            "python -X utf8 scripts/initialize_api_usage.py": [
                cli_python,
                "-X",
                "utf8",
                "scripts/initialize_api_usage.py",
                "--help",
            ],
            "python -m pip install -r requirements.txt -r requirements-dev.txt": [
                cli_python, "-m", "pip", "install", "--help",
            ],
            "python -m piptools compile --allow-unsafe --generate-hashes --no-emit-index-url --no-emit-trusted-host --output-file requirements.txt --strip-extras requirements.in": [
                cli_python, "-m", "pip", "install", "--help",
            ],
            "python -m piptools compile --allow-unsafe --generate-hashes --no-emit-index-url --no-emit-trusted-host --output-file requirements-dev.txt --strip-extras requirements-dev.in": [
                cli_python, "-m", "pip", "install", "--help",
            ],
            "python -u -X utf8 run_web.py": [
                cli_python, "-X", "utf8", "-m", "py_compile", "run_web.py",
            ],
            "python -X utf8 -m pytest -q": [
                cli_python, "-X", "utf8", "-m", "pytest", "--version",
            ],
            "python -X utf8 -m unittest discover": [
                cli_python, "-X", "utf8", "-m", "unittest", "-h",
            ],
            "python -X utf8 scripts/ui_smoke.py --log-path data/ui-smoke-artifacts/ui-smoke.log --artifact-dir data/ui-smoke-artifacts": [
                cli_python, "-X", "utf8", "-m", "py_compile", "scripts/ui_smoke.py",
            ],
            "git pull --ff-only": ["git", "pull", "-h"],
            "python3.13 -m pip install --user -r requirements.txt": [
                cli_python, "-m", "pip", "install", "--help",
            ],
            "python -X utf8 scripts/list_expired_subs.py --help": [
                cli_python, "-X", "utf8", "scripts/list_expired_subs.py", "--help",
            ],
            "python -X utf8 scripts/list_unresolvable_subs.py --help": [
                cli_python, "-X", "utf8", "scripts/list_unresolvable_subs.py", "--help",
            ],
            "python -X utf8 scripts/list_incomplete_notification_subs.py --help": [
                cli_python, "-X", "utf8", "scripts/list_incomplete_notification_subs.py", "--help",
            ],
            "python -X utf8 scripts/tcurve_report.py --help": [
                cli_python, "-X", "utf8", "scripts/tcurve_report.py", "--help",
            ],
            "python -X utf8 scripts/provenance_report.py --help": [
                cli_python, "-X", "utf8", "scripts/provenance_report.py", "--help",
            ],
            "python -X utf8 scripts/forecast_report.py --help": [
                cli_python, "-X", "utf8", "scripts/forecast_report.py", "--help",
            ],
            "python -X utf8 scripts/migrate_runtime_config.py --source <path-to-legacy-config>": [
                cli_python,
                "-X",
                "utf8",
                "scripts/migrate_runtime_config.py",
                "--help",
            ],
            "python -X utf8 scripts/snapshot_run.py --output data/snapshot_check.json": [
                cli_python, "-X", "utf8", "scripts/snapshot_run.py", "--help",
            ],
        }
        handled = set(probes) | live_commands | path_only_commands | {copy_command}
        self.assertEqual(
            commands,
            handled,
            f"README命令缺少安全探针: {sorted(commands - handled)}",
        )

        for documented, probe in probes.items():
            with self.subTest(command=documented):
                result = subprocess.run(
                    probe,
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    check=False,
                )
                allowed = {0, 129} if documented == "git pull --ff-only" else {0}
                self.assertIn(
                    result.returncode,
                    allowed,
                    f"命令安全探针失败: {documented}\n{result.stdout}\n{result.stderr}",
                )

        copy_code = copy_command[len('python -c "'):-1]
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy2(ENV_EXAMPLE, Path(tmp) / ".env.example")
            result = subprocess.run(
                [cli_python, "-c", copy_code],
                cwd=tmp,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (Path(tmp) / ".env").read_bytes(),
                (Path(tmp) / ".env.example").read_bytes(),
            )

        powershell_blocks = _fenced_blocks(self.readme, "powershell")
        self.assertEqual(len(powershell_blocks), 1)
        if os.name == "nt":
            shell = shutil.which("powershell.exe") or shutil.which("powershell")
            self.assertIsNotNone(shell)
            result = subprocess.run(
                [
                    shell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "$null=[scriptblock]::Create([Console]::In.ReadToEnd())",
                ],
                input=powershell_blocks[0],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        cron_blocks = _fenced_blocks(self.readme, "cron")
        self.assertEqual(len(cron_blocks), 1)
        cron_line = cron_blocks[0].strip()
        self.assertRegex(cron_line, r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+.+$")
        self.assertIn("basket_collect.py", cron_line)
    def test_live_collection_commands_are_visibly_marked_as_quota_consuming(self):
        for command in (
            "python -u -X utf8 main.py",
            "python -u -X utf8 basket_collect.py",
        ):
            position = self.readme.find(command)
            self.assertNotEqual(position, -1, f"README缺少命令: {command}")
            context = self.readme[max(0, position - 180): position + len(command) + 180]
            self.assertIn("消耗配额", context)

    def test_runtime_backup_manual_has_restore_replay_and_privacy_contracts(self):
        self.assertTrue(RUNTIME_BACKUP_MANUAL.is_file())
        text = RUNTIME_BACKUP_MANUAL.read_text(encoding="utf-8")
        for phrase in (
            "只有成功恢复过的备份才算有效备份",
            "每周至少一次",
            "每次重大改动前",
            "--output-dir",
            "必须是绝对路径",
            "create",
            "verify",
            "restore",
            "rehearse",
            "--force-production",
            "--confirm-production-restore RESTORE",
            "未加密归档不得上传公共或共享云目录",
            "age",
            "7-Zip AES",
            "real_api_calls",
        ):
            self.assertIn(phrase, text)
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "scripts/runtime_backup.py", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--output-dir", result.stdout)
        self.assertIn("--label", result.stdout)
        self.assertIn("--round-log-days", result.stdout)
        self.assertIn("兼容子命令", result.stdout)
        restore_help = subprocess.run(
            [sys.executable, "-X", "utf8", "scripts/runtime_restore.py", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        self.assertEqual(restore_help.returncode, 0, restore_help.stderr)
        self.assertIn("--archive", restore_help.stdout)
        self.assertIn("--verify-off-disk", restore_help.stdout)
        self.assertIn("--status", restore_help.stdout)

if __name__ == "__main__":
    unittest.main()
