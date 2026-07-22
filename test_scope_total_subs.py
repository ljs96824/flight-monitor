import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


class ScopeTotalSubscriptionScriptTest(unittest.TestCase):
    def test_lists_only_subscriptions_with_total_scope_without_modifying_file(self):
        from scripts.list_scope_total_subs import list_scope_total_subscriptions, render_report

        subscriptions = [
            {
                "name": "全员预算",
                "origin": "SHA",
                "destination": "PEK",
                "budget_scope": "total",
                "max_budget_scope": "per_person",
                "target_price_scope": "per_person",
            },
            {
                "name": "单人预算",
                "origin": "PVG",
                "destination": "KIX",
                "budget_scope": "per_person",
                "max_budget_scope": "per_person",
                "target_price_scope": "per_person",
            },
            {
                "name": "规范化整单预算",
                "origin": "PVG",
                "destination": "HKG",
                "budget_scope": "all",
                "max_budget_scope": "per_person",
                "target_price_scope": "per_person",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subscriptions.json"
            original = json.dumps(subscriptions, ensure_ascii=False, indent=2)
            path.write_text(original, encoding="utf-8")

            items = list_scope_total_subscriptions(path)
            output = StringIO()
            with redirect_stdout(output):
                render_report(items)

            after = path.read_text(encoding="utf-8")

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["_index"], 0)
        self.assertEqual(items[0]["name"], "全员预算")
        self.assertEqual(after, original)
        self.assertIn("budget_scope=total", output.getvalue())
        self.assertIn("budget_scope=all", output.getvalue())
        self.assertIn("统计: 整单口径订阅=2", output.getvalue())


if __name__ == "__main__":
    unittest.main()
