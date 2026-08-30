import ast
from datetime import datetime, timedelta, timezone
import re
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
EXCLUDED_PARTS = {".git", "data", "__pycache__", ".pytest_cache"}
WINDOWS_ONLY_MODULES = {"msvcrt", "winreg", "winsound"}
ABSOLUTE_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")


def _python_files():
    return [
        path
        for path in ROOT.rglob("*.py")
        if not any(part in EXCLUDED_PARTS for part in path.parts)
    ]


def _call_name(call):
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _literal_mode(call, *, builtin_open):
    index = 1 if builtin_open else 0
    if len(call.args) <= index:
        return "r"
    value = call.args[index]
    return value.value if isinstance(value, ast.Constant) else None


class CiPortabilityTest(unittest.TestCase):
    def test_project_timezone_falls_back_without_system_tzdata(self):
        import project_time
        from zoneinfo import ZoneInfoNotFoundError

        with patch.object(
            project_time,
            "ZoneInfo",
            side_effect=ZoneInfoNotFoundError("Asia/Shanghai"),
        ):
            shanghai_tz = project_time.load_project_timezone()

        self.assertEqual(
            shanghai_tz.utcoffset(datetime(2026, 7, 24)),
            timedelta(hours=8),
        )

    def test_freshness_rendering_is_independent_of_runner_timezone(self):
        import notifier

        class UtcRunnerDateTime(datetime):
            @classmethod
            def fromisoformat(cls, value):
                parsed = datetime.fromisoformat(value)
                return cls(
                    parsed.year,
                    parsed.month,
                    parsed.day,
                    parsed.hour,
                    parsed.minute,
                    parsed.second,
                    parsed.microsecond,
                    tzinfo=parsed.tzinfo,
                )

            def astimezone(self, tz=None):
                return super().astimezone(tz or timezone.utc)

        with patch.object(notifier, "datetime", UtcRunnerDateTime):
            parsed = notifier._freshness_timestamp(
                "2026-07-23T10:35:00+08:00"
            )

        self.assertEqual(parsed.strftime("%H:%M"), "10:35")
        self.assertEqual(parsed.utcoffset(), timedelta(hours=8))

    def test_offline_dual_os_workflow_replaces_legacy_monitor(self):
        workflow = ROOT / ".github" / "workflows" / "tests.yml"
        legacy = ROOT / ".github" / "workflows" / "monitor.yml"
        readme = ROOT / "README.md"

        self.assertTrue(workflow.exists(), "缺少离线测试 workflow")
        text = workflow.read_text(encoding="utf-8")
        required = [
            "name: tests",
            "branches: [main]",
            "pull_request:",
            "workflow_dispatch:",
            "group: ci-${{ github.workflow }}-${{ github.event_name }}-${{ github.event.pull_request.number || github.run_id }}",
            "cancel-in-progress: true",
            "fail-fast: false",
            "os: [ubuntu-latest, windows-latest]",
            "timeout-minutes: 20",
            "MPLBACKEND: Agg",
            'python-version: "3.13"',
            'cache: "pip"',
            "cache-dependency-path:",
            "requirements-dev.txt",
            "pip install -r requirements.txt -r requirements-dev.txt",
            "python -X utf8 -m pytest -q",
            "python -X utf8 -m unittest discover",
        ]
        missing = [item for item in required if item not in text]

        self.assertEqual(missing, [])
        self.assertNotIn("secrets.", text)
        self.assertFalse(legacy.exists(), "SerpAPI 时代 monitor.yml 仍存在")
        self.assertIn(
            "actions/workflows/tests.yml/badge.svg",
            readme.read_text(encoding="utf-8"),
        )

    def test_python_files_are_free_of_unhandled_platform_dependencies(self):
        violations = []
        for path in _python_files():
            source = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(source, filename=str(path))
            relative = path.relative_to(ROOT).as_posix()

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = {alias.name.split(".", 1)[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom):
                    modules = {(node.module or "").split(".", 1)[0]}
                else:
                    modules = set()
                for module in modules & WINDOWS_ONLY_MODULES:
                    if module == "msvcrt" and relative == "local_file_lock.py":
                        continue
                    violations.append(f"{relative}:{node.lineno} import {module}")

                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if ABSOLUTE_WINDOWS_PATH.search(node.value):
                        violations.append(
                            f"{relative}:{getattr(node, 'lineno', 0)} "
                            "硬编码Windows绝对路径"
                        )

                if not isinstance(node, ast.Call):
                    continue
                name = _call_name(node)
                if name == "startfile":
                    violations.append(f"{relative}:{node.lineno} os.startfile")
                if name in {"show"}:
                    violations.append(f"{relative}:{node.lineno} GUI show()")
                if name not in {"open", "read_text", "write_text"}:
                    continue
                if (
                    name == "open"
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "os"
                ):
                    continue
                builtin_open = isinstance(node.func, ast.Name)
                mode = (
                    _literal_mode(node, builtin_open=builtin_open)
                    if name == "open"
                    else "r"
                )
                if isinstance(mode, str) and "b" in mode:
                    continue
                if not any(keyword.arg == "encoding" for keyword in node.keywords):
                    violations.append(
                        f"{relative}:{node.lineno} 文本{name}缺encoding"
                    )

        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
