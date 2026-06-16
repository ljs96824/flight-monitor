import sys
import types
import unittest
from unittest.mock import patch


sys.modules.setdefault("httpx", types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None))

from notifier import build_notification_payload, render_email


class NotificationContradictionsTest(unittest.TestCase):
    def test_rising_over_budget_caps_verify_price_and_waits(self):
        analysis = {
            "recommendations": [
                {
                    "flight_no": "MU5099",
                    "price": 2560,
                    "stops": 0,
                    "execution_grade": "A",
                    "price_estimate": {"transaction_price": 2560},
                },
                {
                    "flight_no": "MU5128",
                    "price": 2710,
                    "stops": 0,
                    "execution_grade": "A",
                    "price_estimate": {"transaction_price": 2710},
                },
            ],
            "days_to_dept": 10,
            "waiting_risk": {"up_probability": 70, "down_probability": 20},
        }

        with patch("notifier.get_last_push_price", return_value={"price": 1640}), patch(
            "notifier.get_last_push_snapshot", return_value=None
        ), patch("notifier.track_plan_status", return_value=None):
            payload = build_notification_payload(
                analysis,
                route_info={
                    "origin": "上海",
                    "destination": "北京",
                    "depart_date": "2026-06-19",
                    "target_price": 1600,
                    "max_budget": 2000,
                },
                subscription={"id": "rise-over-budget"},
            )

        self.assertEqual(payload["push_type"], "涨价风险")
        self.assertEqual(payload["verify_price"], 2000)
        self.assertIn("继续", payload["recommendation"])
        self.assertNotIn("可以购买前验证", payload["recommendation"])
        tiers = [plan.get("tier") for plan in payload["recommended_plans"]]
        self.assertEqual(tiers[0], "首选推荐")
        self.assertEqual(tiers[1], "次选方案")
        self.assertEqual(tiers.count("首选推荐"), 1)

    def test_email_normalizes_duplicate_primary_tiers_and_dedupes_bottom_booking_links(self):
        payload = {
            "push_type": "涨价风险",
            "route": "上海 → 北京",
            "recommendation": "建议继续监控",
            "display_price": 2560,
            "transaction_price": 2560,
            "verify_price": 2000,
            "ideal_price": 1600,
            "max_price": 2000,
            "buy_condition": "当前搜索价¥2,560已超过最高可接受价¥2,000，建议继续监控",
            "recommended_plans": [
                {
                    "label": "方案A",
                    "tier": "首选推荐",
                    "is_roundtrip": False,
                    "price": 2560,
                    "estimated_price": 2560,
                    "main_flight": {"flight_no": "MU5099", "price": 2560, "stops": 0},
                    "links": {"main": '<a href="https://example.com/a">携程</a>'},
                },
                {
                    "label": "方案B",
                    "tier": "首选推荐",
                    "is_roundtrip": False,
                    "price": 2710,
                    "estimated_price": 2710,
                    "main_flight": {"flight_no": "MU5128", "price": 2710, "stops": 0},
                    "links": {"main": '<a href="https://example.com/b">携程</a>'},
                },
            ],
            "trigger_reason": [],
            "price_history": [],
            "detail_url": "https://example.com/detail",
            "form_url": "https://example.com/",
            "feedback_url": "https://example.com/feedback",
        }

        _, html = render_email(payload)

        self.assertIn("方案A ｜ 首选推荐", html)
        self.assertIn("方案B ｜ 次选方案", html)
        self.assertNotIn("方案B ｜ 首选推荐", html)
        self.assertEqual(html.count("快速验证首选方案A"), 1)

    def test_single_airport_combo_hides_airport_comparison(self):
        payload = {
            "push_type": "值得验证",
            "route": "上海 → 北京",
            "recommendation": "可以观察",
            "display_price": 680,
            "verify_price": 720,
            "route_airports": {"origins": ["SHA"], "destinations": ["PEK"]},
            "airport_cost_comparison": {
                "rows": [
                    {"airport": "PEK", "ticket_price": 680, "effective_cost": 910},
                ]
            },
            "recommended_plans": [
                {
                    "label": "方案A",
                    "tier": "首选推荐",
                    "is_roundtrip": False,
                    "price": 680,
                    "main_flight": {"flight_no": "MU5101", "price": 680},
                    "links": {},
                }
            ],
            "trigger_reason": [],
            "price_history": [],
        }

        _, html = render_email(payload)

        self.assertNotIn("机场选择对比", html)


if __name__ == "__main__":
    unittest.main()
