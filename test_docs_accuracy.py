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
ENV_EXAMPLE = ROOT / ".env.example"
LICENSE = ROOT / "LICENSE"

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
    "EDGE_PATH",
}

RETIRED_OR_DORMANT_SOURCE_VARIABLES = {
    "HASDATA_KEY",
    "SEARCHAPI_KEY",
    "TRAVELPAYOUTS_TOKEN",
    "RAPIDAPI_KEY",
}


def _dotenv_names(text: str) -> set[str]:
    return {
        match.group(1)
        for match in re.finditer(
            r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=",
            text,
            flags=re.MULTILINE,
        )
    }


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
        self.assertNotIn("占位", self.readme)

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
        self.assertEqual(env_names, ACTIVE_ENV_VARIABLES)
        self.assertEqual(RETIRED_OR_DORMANT_SOURCE_VARIABLES & env_names, set())
        self.assertNotIn("ljs96824", self.env_example)
        self.assertNotIn("@", self.env_example)
        self.assertNotRegex(self.env_example, r"(?i)(secret|token|key)_[a-z0-9]{16,}")

    def test_quick_start_contains_required_install_run_and_scheduler_contracts(self):
        required = (
            "Python 3.13",
            "python -m pip install -r requirements.txt",
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
            "python -m pip install -r requirements.txt": [
                cli_python, "-m", "pip", "install", "--help",
            ],
            "python -m pip install pytest": [
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
            "python -X utf8 scripts/ui_smoke.py": [
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


if __name__ == "__main__":
    unittest.main()
