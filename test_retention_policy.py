import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


class RetentionPolicyTest(unittest.TestCase):
    def _touch(self, path: Path, when: datetime):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
        stamp = when.timestamp()
        os.utime(path, (stamp, stamp))

    def test_collects_three_categories_and_zero_means_permanent(self):
        from retention import collect_retention_candidates

        now = datetime(2026, 8, 21, 12, 0, 0)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_payload = root / "data" / "payloads" / "old.json"
            new_payload = root / "data" / "payloads" / "new.json"
            old_round = root / "data" / "logs" / "rounds" / "old.log"
            old_backup = root / "data" / "subscriptions.json.bak.1"
            self._touch(old_payload, now - timedelta(days=91))
            self._touch(new_payload, now - timedelta(days=89))
            self._touch(old_round, now - timedelta(days=91))
            self._touch(old_backup, now - timedelta(days=181))

            result = collect_retention_candidates(
                root,
                {"payloads": 90, "round_archives": 0, "backups": 180},
                now=now,
            )

        self.assertEqual(
            result["counts"],
            {"payloads": 1, "round_archives": 0, "backups": 1},
        )
        self.assertIn(old_payload, result["items"]["payloads"])
        self.assertNotIn(new_payload, result["items"]["payloads"])
        self.assertNotIn(old_round, result["items"]["round_archives"])

    def test_cleanup_is_dry_run_by_default_and_execute_is_explicit(self):
        from retention import run_retention_cleanup

        now = datetime(2026, 8, 21, 12, 0, 0)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_payload = root / "data" / "payloads" / "old.json"
            self._touch(old_payload, now - timedelta(days=91))
            policy = {"payloads": 90, "round_archives": 90, "backups": 180}

            dry = run_retention_cleanup(root, policy, now=now)
            self.assertEqual(dry["expired_total"], 1)
            self.assertEqual(dry["deleted"], 0)
            self.assertTrue(old_payload.exists())

            applied = run_retention_cleanup(root, policy, now=now, execute=True)
            self.assertEqual(applied["deleted"], 1)
            self.assertFalse(old_payload.exists())


if __name__ == "__main__":
    unittest.main()
