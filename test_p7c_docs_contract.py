import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class P7CDocsContractTest(unittest.TestCase):
    def test_markdown_has_no_workspace_absolute_paths(self):
        markdown_files = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
        pattern = re.compile(r"(?i)\bF:[/\\]codex(?:[/\\]|$)")
        findings = []
        for path in markdown_files:
            for line_no, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                if pattern.search(line):
                    findings.append(f"{path.relative_to(ROOT)}:{line_no}")
        self.assertEqual(findings, [])

    def test_reload_criterion_is_import_based(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "Reload判据=Web进程是否import改动模块,非是否改web_form.py",
            readme,
        )

    def test_audit_uses_forecast_gating_safety_equivalence_term(self):
        audit = (
            ROOT / "docs" / "p7-forecast-gating-equivalence-audit-2026-08-24.md"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "预测门控安全等价 (forecast gating safety equivalence)",
            audit,
        )


if __name__ == "__main__":
    unittest.main()
