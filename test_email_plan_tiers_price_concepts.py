import sys
import types
import unittest


sys.modules.setdefault("httpx", types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None))

from analyzer import build_execution_advice, build_price_signal
from notifier import render_email


class EmailPlanTiersPriceConceptsTest(unittest.TestCase):
    def test_analyzer_splits_price_signal_from_execution_advice(self):
        price_signal = build_price_signal(
            display_price=6521,
            target_price=7800,
            price_history=[6971, 6530, 6527, 6521],
        )
        execution_advice = build_execution_advice(
            display_price=6521,
            transaction_price=7181,
            verify_price=6847,
            target_price=7800,
        )

        self.assertEqual(price_signal["label"], "\u5f3a")
        self.assertIn("\u8fd1\u671f\u4f4e\u4f4d", price_signal["summary"])
        self.assertEqual(execution_advice["label"], "\u9a8c\u8bc1\u540e\u518d\u4e70")
        self.assertEqual(execution_advice["conclusion"], "\u503c\u5f97\u9a8c\u8bc1\uff0c\u4e0d\u5efa\u8bae\u76f4\u63a5\u4e0b\u5355")

    def test_email_renders_tiers_and_separate_price_concepts(self):
        payload = {
            "push_type": "\u503c\u5f97\u9a8c\u8bc1",
            "route": "\u4e0a\u6d77 \u2192 \u5927\u962a",
            "recommendation": "\u503c\u5f97\u9a8c\u8bc1\uff0c\u4e0d\u5efa\u8bae\u76f4\u63a5\u4e0b\u5355",
            "price_policy_reason": "\u641c\u7d22\u53c2\u8003\u4ef7\u8fbe\u6807\uff0c\u4f46\u9884\u4f30\u5b9e\u4ed8\u4ef7\u9ad8\u4e8e\u9a8c\u8bc1\u8d2d\u4e70\u4ef7",
            "price_signal": {
                "label": "\u5f3a",
                "summary": "\u641c\u7d22\u53c2\u8003\u4ef7\u5904\u4e8e\u8fd1\u671f\u4f4e\u4f4d",
            },
            "execution_advice": {
                "label": "\u9a8c\u8bc1\u540e\u518d\u4e70",
                "summary": "\u9884\u4f30\u5b9e\u4ed8\u4ef7\u9ad8\u4e8e\u672c\u6b21\u9a8c\u8bc1\u4ef7\uff0c\u9700\u786e\u8ba4\u6700\u7ec8\u4ef7\u548c\u884c\u674e",
                "condition": "\u652f\u4ed8\u9875\u6700\u7ec8\u4ef7\u2264\u00a56,847\uff0c\u4e14\u542b\u6258\u8fd0\u884c\u674e",
            },
            "display_price": 6521,
            "transaction_price": 7181,
            "verify_price": 6847,
            "ideal_price": 7800,
            "max_price": 8394,
            "buy_condition": "\u652f\u4ed8\u9875\u6700\u7ec8\u4ef7\u2264\u00a56,847\uff0c\u4e14\u542b\u6258\u8fd0\u884c\u674e",
            "buy_condition_explanation": (
                "\u672c\u6b21\u9a8c\u8bc1\u4ef7\u00a56,847 = \u5f53\u524d\u641c\u7d22\u53c2\u8003\u4ef7\u00a56,521 "
                "+ \u53ef\u63a5\u53d7\u6d6e\u52a8\u548c\u8d39\u7528\u5bb9\u5fcd\u533a\u95f4"
            ),
            "confidence": "\u4e2d\u9ad8",
            "recommended_plans": [
                {
                    "label": "\u65b9\u6848A",
                    "tier": "\u9996\u9009\u63a8\u8350",
                    "tier_reason": "\u76f4\u98de\u7701\u5fc3\uff0c\u9002\u5408\u5bb6\u5ead/\u8001\u4eba\u540c\u884c",
                    "variant": "\u9996\u9009\u63a8\u8350:\u76f4\u98de\u7701\u5fc3\uff0c\u9002\u5408\u5bb6\u5ead/\u8001\u4eba\u540c\u884c",
                    "is_roundtrip": True,
                    "price": 6521,
                    "estimated_price": 7181,
                    "outbound_line": "\u53bb\u7a0b:9C6575",
                    "return_line": "\u8fd4\u7a0b:9C6582",
                    "baggage_line": "\u884c\u674e:\u652f\u4ed8\u9875\u9700\u786e\u8ba4",
                    "purchase_mode": "\u5f80\u8fd4\u7ec4\u5408",
                    "links": {},
                },
                {
                    "label": "\u65b9\u6848B",
                    "tier": "\u4f4e\u4ef7\u5907\u9009",
                    "tier_reason": "\u4ef7\u683c\u66f4\u4f4e\uff0c\u4f46\u53bb\u7a0b\u4e2d\u8f6c\u4e14\u4e3a\u4e24\u4e2a\u5355\u7a0b\u62fc\u63a5",
                    "variant": "\u4f4e\u4ef7\u5907\u9009:\u4ef7\u683c\u66f4\u4f4e\uff0c\u4f46\u53bb\u7a0b\u4e2d\u8f6c\u4e14\u4e3a\u4e24\u4e2a\u5355\u7a0b\u62fc\u63a5",
                    "is_roundtrip": True,
                    "price": 6200,
                    "estimated_price": 7600,
                    "outbound_line": "\u53bb\u7a0b:KE888+KE721",
                    "return_line": "\u8fd4\u7a0b:9C6582",
                    "baggage_line": "\u884c\u674e:\u652f\u4ed8\u9875\u9700\u786e\u8ba4",
                    "purchase_mode": "\u4e24\u4e2a\u5355\u7a0b\u62fc\u63a5",
                    "purchase_note": "\u8be5\u65b9\u6848\u4e3a\u53bb\u7a0b\u548c\u8fd4\u7a0b\u5206\u522b\u8d2d\u4e70\uff0c\u552e\u540e\u53ef\u80fd\u5206\u522b\u5904\u7406",
                    "links": {},
                },
            ],
            "trigger_reason": [],
            "price_history": [],
            "action_range": {"ranges": []},
            "detail_url": "https://example.com/detail",
            "form_url": "https://example.com/",
            "feedback_url": "https://example.com/feedback",
        }

        _, html = render_email(payload)

        self.assertIn("\u4ef7\u683c\u4fe1\u53f7", html)
        self.assertIn("\u6267\u884c\u5efa\u8bae", html)
        self.assertIn("\u65b9\u6848A \uff5c \u9996\u9009\u63a8\u8350", html)
        self.assertIn("\u65b9\u6848B \uff5c \u4f4e\u4ef7\u5907\u9009", html)
        self.assertIn("\u9002\u5408\u6761\u4ef6", html)
        self.assertIn("\u672c\u6b21\u9a8c\u8bc1\u4ef7", html)
        self.assertIn("\u5f53\u524d\u641c\u7d22\u53c2\u8003\u4ef7", html)
        self.assertNotIn("\u641c\u7d22\u4ef7\u4f4d\u7f6e", html)

    def test_email_simplifies_source_stats_and_excluded_plans(self):
        payload = {
            "push_type": "\u503c\u5f97\u9a8c\u8bc1",
            "route": "\u4e0a\u6d77 \u2192 \u5927\u962a",
            "recommendation": "\u503c\u5f97\u9a8c\u8bc1",
            "display_price": 6521,
            "transaction_price": 7181,
            "verify_price": 6847,
            "confidence": "\u4e2d\u9ad8",
            "is_roundtrip": True,
            "freshness_minutes": 0,
            "source_stats": {
                "serpapi": {"count": 10, "status": "\u6210\u529f"},
                "hasdata": {"count": 11, "status": "\u6210\u529f"},
                "searchapi": {"count": 0, "status": "\u5931\u8d25 429"},
                "travelpayouts": {"count": 0, "status": "\u5931\u8d25"},
                "skyscanner": {"count": 0, "status": "\u5931\u8d25 429"},
                "after_dedup_by_cabin": {"count": 20, "status": "\u6210\u529f"},
                "duffel": {"count": 78, "status": "\u6210\u529f"},
            },
            "recommended_plans": [
                {
                    "label": "\u65b9\u6848A",
                    "tier": "\u9996\u9009\u63a8\u8350",
                    "is_roundtrip": True,
                    "price": 6521,
                    "estimated_price": 7181,
                    "purchase_mode": "\u5f80\u8fd4\u7ec4\u5408",
                    "links": {},
                }
            ],
            "excluded_plans": [
                {
                    "price": 4564,
                    "scope": "outbound",
                    "flight_combo": "KE888+KE721",
                    "reason": "\u53bb\u7a0b04:05\u8d77\u98de\uff0c\u4e2d\u8f6c\u7b49\u5f859h50m\uff0c\u89e6\u53d1\u7ea2\u773c/\u957f\u4e2d\u8f6c\u98ce\u9669",
                },
                {
                    "total_price": 5714,
                    "scope": "roundtrip",
                    "reason": "\u53bb\u7a0b04:05+\u8fd4\u7a0b22:20\uff0c\u89e6\u53d1\u7ea2\u773c/\u51cc\u6668\u5230\u8fbe\u98ce\u9669",
                },
            ],
            "trigger_reason": [],
            "price_history": [],
            "action_range": {"ranges": []},
            "detail_url": "https://example.com/detail",
            "form_url": "https://example.com/",
            "feedback_url": "https://example.com/feedback",
        }

        _, html = render_email(payload)

        self.assertIn("\u4ef7\u683c:Google Flights \u591a\u6e90\u4ea4\u53c9\u9a8c\u8bc1,21\u4e2a\u5019\u9009\u65b9\u6848", html)
        self.assertIn("\u884c\u674e/\u9000\u6539:Duffel \u8fd4\u56de78\u6761\u89c4\u5219\u53c2\u8003", html)
        self.assertIn("\u5f53\u524d\u6709\u6548\u65b9\u6848:\u5df2\u53bb\u91cd\u7b5b\u9009", html)
        self.assertIn("\u91c7\u96c6\u65f6\u95f4:\u521a\u521a\u91c7\u96c6", html)
        self.assertNotIn("after_dedup_by_cabin", html)
        self.assertNotIn("travelpayouts", html)
        self.assertNotIn("skyscanner", html)
        self.assertNotIn("429", html)
        self.assertIn("\u5b8c\u6574\u6392\u9664\u65b9\u6848\u8be6\u60c5\u89c1\u7f51\u9875\u8be6\u60c5\u9875", html)
        self.assertNotIn("\u822a\u73ed\u7ec4\u5408", html)

    def test_email_labels_chart_price_scope_and_avoids_plan_template_residue(self):
        payload = {
            "push_type": "\u503c\u5f97\u9a8c\u8bc1",
            "route": "\u4e0a\u6d77 \u2192 \u5927\u962a",
            "recommendation": "\u503c\u5f97\u9a8c\u8bc1",
            "display_price": 6521,
            "transaction_price": 7181,
            "verify_price": 6847,
            "confidence": "\u4e2d\u9ad8",
            "is_roundtrip": True,
            "nearby_date_prices": [
                {"label": "2026-10-01", "value": 2887, "scope": "oneway"},
                {"label": "2026-10-04", "value": 1402, "scope": "oneway", "highlight": "low"},
            ],
            "plan_price_rows": [
                {
                    "label": "\u65b9\u6848A",
                    "value": 6521,
                    "scope": "roundtrip",
                    "note": "B",
                    "description": "\u76f4\u98de\u5f80\u8fd4,\u9996\u9009\u63a8\u8350",
                },
                {
                    "label": "\u65b9\u6848B",
                    "value": 6186,
                    "scope": "roundtrip",
                    "note": "B",
                    "description": "\u53bb\u7a0b\u4e2d\u8f6c+\u4e24\u4e2a\u5355\u7a0b\u62fc\u63a5,\u4f4e\u4ef7\u5907\u9009",
                },
            ],
            "recommended_plans": [],
            "trigger_reason": [],
            "price_history": [],
            "action_range": {"ranges": []},
            "detail_url": "https://example.com/detail",
            "form_url": "https://example.com/",
            "feedback_url": "https://example.com/feedback",
        }

        _, html = render_email(payload)

        self.assertIn("\u524d\u540e\u65e5\u671f\u6700\u4f4e\u4ef7(\u5355\u7a0b\u53c2\u8003\u4ef7)", html)
        self.assertIn("\u00a51,402(\u5355\u7a0b)", html)
        self.assertIn("\u6ce8:\u4e3a\u5355\u7a0b\u4ef7,\u975e\u5f80\u8fd4\u603b\u4ef7", html)
        self.assertIn("\u65b9\u6848A:\u00a56,521(\u5f80\u8fd4),\u76f4\u98de\u5f80\u8fd4,\u9996\u9009\u63a8\u8350", html)
        self.assertIn("\u65b9\u6848B:\u00a56,186(\u5f80\u8fd4),\u53bb\u7a0b\u4e2d\u8f6c+\u4e24\u4e2a\u5355\u7a0b\u62fc\u63a5,\u4f4e\u4ef7\u5907\u9009", html)
        self.assertNotIn("\u00a56,521 B", html)
        self.assertNotIn("\u00a56,186 B", html)

    def test_email_starts_with_action_panel_and_plan_tradeoffs(self):
        payload = {
            "push_type": "\u503c\u5f97\u9a8c\u8bc1",
            "route": "\u4e0a\u6d77 \u2192 \u5927\u962a",
            "recommendation": "\u503c\u5f97\u9a8c\u8bc1,\u4e0d\u5efa\u8bae\u76f4\u63a5\u4e0b\u5355",
            "price_policy_reason": "\u641c\u7d22\u53c2\u8003\u4ef7\u8fdb\u5165\u7406\u60f3\u5165\u624b\u533a\u95f4,\u4f46\u9884\u4f30\u5b9e\u4ed8\u4ecd\u9700\u9a8c\u8bc1",
            "display_price": 6521,
            "transaction_price": 7181,
            "verify_price": 6847,
            "ideal_price": 7800,
            "max_price": 8394,
            "buy_condition": "\u652f\u4ed8\u9875\u6700\u7ec8\u4ef7\u2264\u00a56,847,\u4e14\u542b\u6258\u8fd0\u884c\u674e",
            "buy_condition_explanation": (
                "\u672c\u6b21\u9a8c\u8bc1\u4ef7\u00a56,847 = \u5f53\u524d\u641c\u7d22\u53c2\u8003\u4ef7\u00a56,521 "
                "+ \u53ef\u63a5\u53d7\u6d6e\u52a8\u548c\u8d39\u7528\u5bb9\u5fcd\u533a\u95f4"
            ),
            "confidence": "\u4e2d\u9ad8",
            "trigger_reason": ["\u641c\u7d22\u53c2\u8003\u4ef7\u8fdb\u5165\u7406\u60f3\u5165\u624b\u533a\u95f4"],
            "recommended_plans": [
                {
                    "label": "\u65b9\u6848A",
                    "tier": "\u9996\u9009\u63a8\u8350",
                    "tier_reason": "\u76f4\u98de\u7701\u5fc3,\u9002\u5408\u5bb6\u5ead/\u8001\u4eba\u540c\u884c",
                    "is_roundtrip": True,
                    "price": 6521,
                    "estimated_price": 7181,
                    "purchase_mode": "\u5f80\u8fd4\u7ec4\u5408",
                    "baggage_line": "\u884c\u674e:\u652f\u4ed8\u9875\u9700\u786e\u8ba4",
                    "links": {
                        "outbound": '<a href="https://buy.example/out">Trip.com</a>',
                        "return": '<a href="https://buy.example/ret">Trip.com</a>',
                    },
                    "outbound_flight": {"stops": 0, "flight_combo": "9C6575"},
                    "return_flight": {"stops": 0, "flight_combo": "9C6582"},
                },
                {
                    "label": "\u65b9\u6848B",
                    "tier": "\u4f4e\u4ef7\u5907\u9009",
                    "tier_reason": "\u4ef7\u683c\u66f4\u4f4e,\u4f46\u53bb\u7a0b\u4e2d\u8f6c\u4e14\u4e3a\u4e24\u4e2a\u5355\u7a0b\u62fc\u63a5",
                    "suitable_condition": "\u9002\u5408\u4f60\u613f\u610f\u63a5\u53d7\u4e2d\u8f6c\u548c\u552e\u540e\u5206\u79bb,\u4ee5\u6362\u53d6\u66f4\u4f4e\u4ef7\u683c\u3002",
                    "is_roundtrip": True,
                    "price": 6186,
                    "estimated_price": 7300,
                    "purchase_mode": "\u4e24\u4e2a\u5355\u7a0b\u62fc\u63a5",
                    "links": {},
                    "outbound_flight": {"stops": 1, "flight_combo": "KE888+KE721"},
                    "return_flight": {"stops": 0, "flight_combo": "9C6582"},
                },
            ],
            "price_history": [],
            "action_range": {"ranges": []},
            "detail_url": "https://example.com/detail",
            "form_url": "https://example.com/",
            "feedback_url": "https://example.com/feedback",
        }

        _, html = render_email(payload)

        self.assertIn("\u5f53\u524d\u5224\u65ad:\u503c\u5f97\u9a8c\u8bc1,\u4e0d\u5efa\u8bae\u76f4\u63a5\u4e0b\u5355", html)
        self.assertIn("\u9996\u9009\u65b9\u6848:\u65b9\u6848A,\u76f4\u98de\u5f80\u8fd4,\u641c\u7d22\u53c2\u8003\u4ef7\u00a56,521", html)
        self.assertIn("\u8d2d\u4e70\u6761\u4ef6:\u652f\u4ed8\u9875\u6700\u7ec8\u4ef7\u2264\u00a56,847,\u4e14\u542b\u6258\u8fd0\u884c\u674e", html)
        self.assertIn("\u4e0b\u4e00\u6b65:\u53bb\u9a8c\u8bc1\u4ef7\u683c | \u67e5\u770b\u8be6\u60c5 | \u7ee7\u7eed\u76d1\u63a7", html)
        self.assertIn("\u89e6\u53d1\u7c7b\u578b:\u4f4e\u4ef7\u7ebf\u7d22 | \u9700\u9a8c\u8bc1 | \u975e\u76f4\u63a5\u8d2d\u4e70", html)
        self.assertIn("\u89e6\u53d1\u539f\u56e0:\u641c\u7d22\u53c2\u8003\u4ef7\u8fdb\u5165\u7406\u60f3\u5165\u624b\u533a\u95f4,\u4f46\u9884\u4f30\u5b9e\u4ed8\u4ecd\u9700\u9a8c\u8bc1", html)
        self.assertLess(html.index("\u53bb\u9a8c\u8bc1\u4ef7\u683c"), html.index("\u4ef7\u683c\u53e3\u5f84\u4e0e\u4fe1\u53f7"))
        self.assertIn("\u65b9\u6848A \uff5c \u9996\u9009\u63a8\u8350 \uff5c \u66f4\u7701\u5fc3", html)
        self.assertIn("\u65b9\u6848A:\u76f4\u98de,\u7701\u5fc3", html)
        self.assertIn("\u65b9\u6848B \uff5c \u4f4e\u4ef7\u5907\u9009 \uff5c \u66f4\u4fbf\u5b9c\u4f46\u98ce\u9669\u66f4\u9ad8", html)
        self.assertIn("\u65b9\u6848B:\u4fbf\u5b9c\u7ea6\u00a5335,\u4f46\u4ef7\u683c\u66f4\u4f4e,\u4f46\u53bb\u7a0b\u4e2d\u8f6c\u4e14\u4e3a\u4e24\u4e2a\u5355\u7a0b\u62fc\u63a5", html)
        self.assertIn("\u9002\u5408\u4f60\u613f\u610f\u63a5\u53d7\u4e2d\u8f6c\u548c\u552e\u540e\u5206\u79bb", html)
        self.assertIn("\u9a8c\u8bc1\u4ef7\u8bf4\u660e:\u8fd9\u6b21\u65b9\u6848\u503c\u5f97\u4e70\u7684\u4e0a\u9650", html)


if __name__ == "__main__":
    unittest.main()
