import tempfile
import unittest
from datetime import datetime
from pathlib import Path


class BasketSentinelTest(unittest.TestCase):
    def test_sentinel_only_due_after_threshold_without_basket_entry(self):
        from basket_sentinel import evaluate_basket_sentinel

        usage = {
            "entries": [
                {
                    "round_id": "20260812T190000_sub",
                    "recorded_at": "2026-08-12T19:05:00+08:00",
                }
            ]
        }
        before = evaluate_basket_sentinel(
            usage,
            now=datetime(2026, 8, 12, 19, 59),
            threshold="20:00",
        )
        after = evaluate_basket_sentinel(
            usage,
            now=datetime(2026, 8, 12, 20, 1),
            threshold="20:00",
        )
        with_basket = evaluate_basket_sentinel(
            {
                "entries": usage["entries"]
                + [
                    {
                        "round_id": "basket_20260812T093000",
                        "recorded_at": "2026-08-12T09:30:00+08:00",
                    }
                ]
            },
            now=datetime(2026, 8, 12, 20, 1),
            threshold="20:00",
        )

        self.assertFalse(before["due"])
        self.assertTrue(after["due"])
        self.assertEqual(after["reason"], "今日篮子未运行")
        self.assertFalse(with_basket["due"])

    def test_sentinel_notifies_once_per_day(self):
        from basket_sentinel import run_basket_sentinel

        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "basket_sentinel.json"
            kwargs = {
                "usage_payload": {"entries": []},
                "now": datetime(2026, 8, 12, 21, 0),
                "threshold": "20:00",
                "state_path": state_path,
                "notifier": lambda title, content: calls.append((title, content)) or True,
            }
            first = run_basket_sentinel(**kwargs)
            second = run_basket_sentinel(**kwargs)

        self.assertTrue(first["notified"])
        self.assertFalse(second["notified"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "[篮子哨兵] 今日篮子未运行")
