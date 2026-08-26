import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent


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

    def test_current_findings_match_exact_registered_debt(self):
        from scripts.check_f821 import KNOWN_F821_DEBT, scan_f821

        findings = scan_f821()
        self.assertEqual(findings, KNOWN_F821_DEBT)

    def test_comparison_message_scope_has_no_registered_or_current_f821(self):
        from scripts.check_f821 import KNOWN_F821_DEBT, scan_f821

        target = "format_comparison_message"
        self.assertFalse(any(scope == target for _path, scope, _name in KNOWN_F821_DEBT))
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