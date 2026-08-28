import ast
import configparser
from contextlib import redirect_stdout
import io
import re
import subprocess
import tempfile
import tokenize
import tomllib
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
ALLOWED_F821_SUPPRESSIONS = frozenset()
RUFF_CONFIG_NAMES = frozenset(
    {"pyproject.toml", "ruff.toml", ".ruff.toml", "setup.cfg", "tox.ini"}
)


def _tracked_paths() -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return tuple(line for line in completed.stdout.splitlines() if line)


def _f821_comment_suppressions() -> frozenset[tuple[str, str, str]]:
    findings = set()
    for relative in _tracked_paths():
        if not relative.endswith(".py"):
            continue
        path = ROOT / relative
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        names_by_line: dict[int, set[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names_by_line.setdefault(node.lineno, set()).add(node.id)
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for token in tokens:
            if token.type != tokenize.COMMENT:
                continue
            comment = token.string.strip()
            if re.fullmatch(
                r"#\s*(?:ruff:\s*)?noqa(?:\s*#.*)?",
                comment,
                flags=re.IGNORECASE,
            ):
                findings.add((relative, "<all>", "bare suppression"))
                continue
            if not re.match(
                r"#\s*(?:ruff:\s*)?noqa\s*:",
                comment,
                flags=re.IGNORECASE,
            ):
                continue
            if not re.search(r"\bF821\b", comment, flags=re.IGNORECASE):
                continue
            symbols = names_by_line.get(token.start[0]) or {"<unknown>"}
            for symbol in symbols:
                findings.add((relative, symbol, "inline F821 suppression"))
    return frozenset(findings)


def _value_contains_f821(value) -> bool:
    if isinstance(value, str):
        return bool(re.search(r"\bF821\b", value, flags=re.IGNORECASE))
    if isinstance(value, dict):
        return any(_value_contains_f821(child) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_value_contains_f821(child) for child in value)
    return False


def _f821_config_suppressions() -> frozenset[tuple[str, str, str]]:
    findings = set()
    for relative in _tracked_paths():
        path = ROOT / relative
        if path.name not in RUFF_CONFIG_NAMES:
            continue
        if path.suffix == ".toml":
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))
            stack = [((), parsed)]
            while stack:
                key_path, value = stack.pop()
                if isinstance(value, dict):
                    for key, child in value.items():
                        normalized = str(key).replace("_", "-").lower()
                        child_path = (*key_path, str(key))
                        if normalized in {
                            "per-file-ignores",
                            "extend-per-file-ignores",
                        } and _value_contains_f821(child):
                            findings.add(
                                (
                                    relative,
                                    ".".join(child_path),
                                    "Ruff config F821 suppression",
                                )
                            )
                        stack.append((child_path, child))
            continue
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read(path, encoding="utf-8")
        for section in parser.sections():
            for key, value in parser.items(section):
                normalized = key.replace("_", "-").lower()
                if normalized in {
                    "per-file-ignores",
                    "extend-per-file-ignores",
                } and _value_contains_f821(value):
                    findings.add(
                        (
                            relative,
                            f"{section}.{key}",
                            "Ruff config F821 suppression",
                        )
                    )
    return frozenset(findings)


class F821StaticContractTest(unittest.TestCase):
    def test_scan_fails_loudly_when_ruff_cannot_start(self):
        from scripts.check_f821 import scan_f821

        scan_f821.cache_clear()
        self.addCleanup(scan_f821.cache_clear)
        completed = CompletedProcess(
            args=["python", "-m", "ruff"],
            returncode=1,
            stdout="",
            stderr="No module named ruff",
        )
        with patch("scripts.check_f821.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "No module named ruff"):
                scan_f821(ROOT)

    def test_repository_has_zero_f821_findings(self):
        from scripts.check_f821 import scan_f821

        findings = scan_f821()
        self.assertEqual(findings, frozenset())

    def test_full_script_rejects_a_temporary_undefined_name(self):
        from scripts.check_f821 import main, scan_f821

        scan_f821.cache_clear()
        self.addCleanup(scan_f821.cache_clear)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "broken.py").write_text(
                "def broken():\n    return missing_symbol\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                result = main([], root=root)

        self.assertEqual(result, 1)
        self.assertIn(
            "broken.py | broken | missing_symbol",
            output.getvalue(),
        )

    def test_zero_gate_has_no_known_debt_escape_hatch(self):
        from scripts.check_f821 import enforce_zero_f821, main

        output = io.StringIO()
        with redirect_stdout(output):
            result = enforce_zero_f821(frozenset())
        self.assertEqual(result, 0)
        self.assertIn("zero-debt gate passed", output.getvalue())

        main_source = Path(main.__code__.co_filename).read_text(encoding="utf-8")
        main_tree = ast.parse(main_source)
        main_node = next(
            node
            for node in main_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        main_segment = ast.get_source_segment(main_source, main_node)
        self.assertNotIn("KNOWN_F821_DEBT", main_segment)
        for retired_text in (
            "unregistered findings",
            "resolved debt still registered",
            "exact debt matched",
            "新增债务",
            "已解决但仍登记",
        ):
            self.assertNotIn(retired_text, main_source)

    def test_f821_suppression_allowlist_is_empty_and_repository_has_none(self):
        # A future exception must name (path, symbol, why static analysis cannot
        # recognize it). Hidden debt is not an acceptable way to keep this gate green.
        self.assertEqual(ALLOWED_F821_SUPPRESSIONS, frozenset())
        self.assertEqual(_f821_comment_suppressions(), frozenset())
        self.assertEqual(_f821_config_suppressions(), frozenset())

    def test_comparison_message_scope_has_no_registered_or_current_f821(self):
        from scripts.check_f821 import scan_f821

        target = "format_comparison_message"
        self.assertFalse(any(scope == target for _path, scope, _name in scan_f821()))

    def test_ruff_is_dev_only_and_ci_static_gates_precede_behavior_tests(self):
        dev_in = (ROOT / "requirements-dev.in").read_text(encoding="utf-8").splitlines()
        runtime_in = (ROOT / "requirements.in").read_text(encoding="utf-8").splitlines()
        dev_lock = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("ruff", [line.strip() for line in dev_in])
        self.assertNotIn("ruff", [line.strip() for line in runtime_in])
        self.assertIn("ruff==", dev_lock)
        self.assertIn("name: Ruff F821 zero-debt gate", workflow)
        self.assertNotIn("name: Ruff F821 exact-debt gate", workflow)
        ordered_commands = [
            "python -X utf8 scripts/check_f821.py",
            'python -c "import notifier"',
            'python -c "import web_form"',
            "python -X utf8 -m pytest -q -p no:cacheprovider",
            "python -X utf8 -m unittest discover",
        ]
        positions = [workflow.index(command) for command in ordered_commands]
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
