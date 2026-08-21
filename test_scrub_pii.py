import tempfile
import unittest
from datetime import datetime
from pathlib import Path


class ScrubPiiTest(unittest.TestCase):
    def test_dry_run_does_not_modify_and_execute_backs_up_before_scrubbing(self):
        from scripts.scrub_pii import scrub_files

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "data" / "logs" / "rounds" / "20260821.log"
            log_path.parent.mkdir(parents=True)
            original = "recipient=private@example.com\n"
            log_path.write_text(original, encoding="utf-8")

            dry = scrub_files(
                [log_path],
                root=root,
                execute=False,
                now=datetime(2026, 8, 21, 12, 0, 0),
            )
            self.assertEqual(dry["matched_files"], 1)
            self.assertEqual(dry["matched_emails"], 1)
            self.assertEqual(log_path.read_text(encoding="utf-8"), original)

            applied = scrub_files(
                [log_path],
                root=root,
                execute=True,
                now=datetime(2026, 8, 21, 12, 0, 0),
            )
            backup = Path(applied["backup_dir"]) / log_path.relative_to(root)
            self.assertEqual(backup.read_text(encoding="utf-8"), original)
            self.assertEqual(log_path.read_text(encoding="utf-8"), "recipient=<EMAIL>\n")

            repeated = scrub_files(
                [log_path],
                root=root,
                execute=True,
                now=datetime(2026, 8, 21, 12, 1, 0),
            )
            self.assertEqual(repeated["matched_files"], 0)


if __name__ == "__main__":
    unittest.main()
