import unittest
import sys
import types

sys.modules.setdefault("httpx", types.SimpleNamespace(post=lambda *a, **k: None))
from notifier import render_email, render_pushplus


class NotifierTravelProfileTest(unittest.TestCase):
    def test_pushplus_and_email_show_travel_profile_basis(self):
        payload = {
            "push_type": "值得验证",
            "route": "上海 → 大阪",
            "display_price": 6522,
            "transaction_price": 7182,
            "verify_price": 6848,
            "recommendation": "值得验证，不建议直接下单",
            "buy_condition": "支付页≤¥6,848且含托运行李",
            "trigger_reason": ["搜索参考价达标"],
            "travel_profile": {
                "scenario": "family",
                "price": "medium",
                "time": "medium",
                "comfort": "high",
                "risk_averse": "high",
                "baggage": "high",
            },
            "travel_profile_explanation": {
                "scenario": "family",
                "scenario_label": "家庭/亲子",
                "basis": "优先了白天直飞、行李明确、低中转风险的方案。",
                "dimensions": {
                    "舒适度需求": "高",
                    "执行风险厌恶": "高",
                    "行李票规重要性": "高",
                },
            },
            "scenario_recommendation": "该方案优先考虑白天直飞、行李明确和低中转风险，适合带孩子出行，减少折腾。",
            "recommended_plans": [],
            "price_history": [],
        }

        push = render_pushplus(payload)
        subject, email_html = render_email(payload)

        self.assertIn("推荐依据", push)
        self.assertIn("家庭/亲子", push)
        self.assertIn("推荐依据", email_html)
        self.assertIn("舒适度需求", email_html)
        self.assertIn("适合带孩子出行", email_html)
        self.assertIn("值得验证", subject)


if __name__ == "__main__":
    unittest.main()
