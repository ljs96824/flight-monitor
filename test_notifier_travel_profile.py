import unittest
import sys
import types

sys.modules.setdefault("httpx", types.SimpleNamespace(post=lambda *a, **k: None))
from notifier import build_notification_payload, render_email, render_pushplus


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

        self.assertNotIn("推荐依据", push)
        self.assertIn("未找到完全符合条件的方案", push)
        self.assertIn("推荐依据", email_html)
        self.assertIn("舒适度需求", email_html)
        self.assertIn("适合带孩子出行", email_html)
        self.assertIn("无符合方案", subject)

    def test_email_explains_combined_scenarios_and_tradeoff(self):
        payload = {
            "push_type": "值得验证",
            "route": "上海 → 大阪",
            "display_price": 6522,
            "transaction_price": 7182,
            "verify_price": 6848,
            "recommendation": "值得验证",
            "buy_condition": "支付页≤¥6,848且含托运行李",
            "trigger_reason": ["搜索参考价达标"],
            "travel_profile": {
                "scenario": "tourism",
                "scenarios": ["tourism", "family"],
                "price": "high",
                "time": "medium",
                "comfort": "high",
                "risk_averse": "high",
                "baggage": "high",
            },
            "travel_profile_explanation": {
                "scenario": "tourism",
                "scenarios": ["tourism", "family"],
                "scenario_label": "旅游 + 家庭/亲子",
                "basis": "系统合并了多个场景的需求：旅游保留价格敏感；家庭/亲子提高白天直飞、行李明确和低中转风险权重。",
                "tradeoff": "旅游保留价格敏感，但家庭/亲子的安全舒适要求会优先于纯低价。",
                "dimensions": {
                    "价格敏感度": "高",
                    "舒适度需求": "高",
                },
            },
            "scenario_recommendation": "该方案白天直飞、行李明确，价格也在合理区间，适合带孩子的旅行，兼顾省心和性价比。",
            "recommended_plans": [],
            "price_history": [],
        }

        push = render_pushplus(payload)
        subject, email_html = render_email(payload)

        self.assertNotIn("推荐依据", push)
        self.assertIn("旅游 + 家庭/亲子", email_html)
        self.assertIn("孩子", email_html)
        self.assertIn("纯低价", email_html)
        self.assertIn("无符合方案", subject)

    def test_payload_prefers_subscription_scenarios_over_stale_analysis_profile(self):
        payload = build_notification_payload(
            analysis_result={
                "travel_profile": {
                    "scenario": "personal",
                    "scenarios": ["personal"],
                    "price": "high",
                    "time": "medium",
                    "comfort": "medium",
                    "risk_averse": "medium",
                    "baggage": "medium",
                },
                "travel_profile_explanation": {
                    "scenario": "personal",
                    "scenarios": ["personal"],
                    "scenario_label": "个人出行",
                    "basis": "按个人出行排序",
                    "dimensions": {},
                },
                "recommendations": [],
            },
            route_info={"origin": "上海", "destination": "大阪", "depart_date": "2026-10-01"},
            subscription={
                "soft_preferences": {
                    "travel_scenarios": ["tourism", "family"],
                    "travel_scenario": "tourism",
                }
            },
            price_history=[],
        )

        self.assertEqual(payload["travel_scenarios"], ["tourism", "family"])
        self.assertEqual(payload["travel_profile"]["scenarios"], ["tourism", "family"])
        self.assertEqual(payload["travel_profile_explanation"]["scenario_label"], "旅游 + 家庭/亲子")
        self.assertIn("孩子安全舒适", payload["recommendation_basis"]["conflict_note"])

    def test_payload_uses_precise_passenger_breakdown_over_stale_count(self):
        payload = build_notification_payload(
            analysis_result={"recommendations": [], "display_price": 6521},
            route_info={"origin": "\u4e0a\u6d77", "destination": "\u5927\u962a", "depart_date": "2026-10-01"},
            subscription={
                "basic": {"passenger_count": 3},
                "preferences": {
                    "passengers": {"adult": 2, "child": 1, "elderly": 2, "infant": 0},
                    "travel_purposes": ["tourism", "family"],
                },
                "soft_preferences": {
                    "passenger_count": 3,
                    "travel_scenarios": ["tourism", "family"],
                },
            },
            price_history=[],
        )

        self.assertEqual(payload["travel_profile"]["passenger_count"], 5)
        self.assertEqual(payload["travel_profile"]["passengers"]["elderly"], 2)

        _, email_html = render_email(payload)

        self.assertIn("5\u4eba\u51fa\u884c", email_html)
        self.assertIn("\u6210\u4eba2+\u513f\u7ae51+\u8001\u4eba2", email_html)


if __name__ == "__main__":
    unittest.main()
