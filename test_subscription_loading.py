import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None),
)
logging.basicConfig = lambda *a, **k: None

import main


class SubscriptionLoadingTest(unittest.TestCase):
    def test_bad_subscription_is_skipped_without_stopping_batch(self):
        records = [
            {
                "id": "bad-location",
                "origin": "上海",
                "destination": "重庆",
                "depart_date": "2026-10-01",
                "status": "active",
            },
            {
                "id": "good-osaka",
                "origin": "上海",
                "destination": "大阪",
                "depart_date": "2026-10-01",
                "status": "active",
            },
        ]

        original_path = main.SUBSCRIPTIONS_PATH
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
            main.SUBSCRIPTIONS_PATH = path
            try:
                loaded = main.load_file_subscriptions()
            finally:
                main.SUBSCRIPTIONS_PATH = original_path

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["id"], "good-osaka")
        self.assertEqual(loaded[0]["destination"], "大阪")
        self.assertEqual(loaded[0]["destination_airports"], ["KIX", "ITM"])


if __name__ == "__main__":
    unittest.main()
