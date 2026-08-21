import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from scripts.cleanup_legacy_payloads import cleanup_legacy_payloads, main as cleanup_main


VALID_ID = "123e4567-e89b-12d3-a456-426614174000"


class CleanupLegacyPayloadsTest(unittest.TestCase):
    def _fixture(self, root: Path):
        payloads = root / "data" / "payloads"
        payloads.mkdir(parents=True)
        (payloads / "107.json").write_text("{}", encoding="utf-8")
        (payloads / "PVG-KIX-2026.json").write_text("{}", encoding="utf-8")
        (payloads / f"{VALID_ID}.json").write_text("{}", encoding="utf-8")
        (root / "data" / "page_results.json").write_text("[]", encoding="utf-8")

    def test_dry_run_preserves_all_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixture(root)
            result = cleanup_legacy_payloads(root)

            self.assertFalse(result["execute"])
            self.assertEqual(len(result["legacy_files"]), 2)
            self.assertEqual(len(result["uuid_files"]), 1)
            self.assertTrue((root / "data" / "payloads" / "107.json").exists())
            self.assertTrue((root / "data" / "page_results.json").exists())

    def test_execute_requires_backup_and_preserves_only_uuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixture(root)
            with self.assertRaisesRegex(ValueError, "backup-archive"):
                cleanup_legacy_payloads(root, execute=True)

            archive = root / "payloads-backup.tgz"
            archive.write_bytes(b"fixture")
            result = cleanup_legacy_payloads(
                root,
                execute=True,
                backup_archive=archive,
            )

            remaining = sorted(
                path.name for path in (root / "data" / "payloads").glob("*.json")
            )
            self.assertEqual(remaining, [f"{VALID_ID}.json"])
            self.assertFalse((root / "data" / "page_results.json").exists())
            self.assertEqual(result["deleted_payloads"], 2)
            self.assertEqual(result["deleted_page_results"], 1)

    def test_cli_missing_backup_prints_correct_usage_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixture(root)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cleanup_main(["--root", str(root), "--execute"])

            output = stdout.getvalue() + stderr.getvalue()
            self.assertEqual(code, 2)
            self.assertIn("正确用法示例", output)
            self.assertIn("--execute --backup-archive", output)
            self.assertTrue((root / "data" / "payloads" / "107.json").exists())

    def test_dry_run_lists_first_ten_then_reports_remaining_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixture(root)
            payloads = root / "data" / "payloads"
            for index in range(10):
                (payloads / f"{200 + index}.json").write_text("{}", encoding="utf-8")
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                code = cleanup_main(["--root", str(root)])

            output = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertEqual(output.count("待清理="), 10)
            self.assertIn("另有2条(--verbose查看全部)", output)

    def test_verbose_lists_every_legacy_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixture(root)
            payloads = root / "data" / "payloads"
            for index in range(10):
                (payloads / f"{200 + index}.json").write_text("{}", encoding="utf-8")
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                code = cleanup_main(["--root", str(root), "--verbose"])

            output = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertEqual(output.count("待清理="), 12)
            self.assertNotIn("另有", output)

if __name__ == "__main__":
    unittest.main()
