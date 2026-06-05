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


if __name__ == "__main__":
    unittest.main()
