import tempfile
import unittest
from pathlib import Path

from scripts.cleanup_legacy_payloads import cleanup_legacy_payloads


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


if __name__ == "__main__":
    unittest.main()
