import sys
import types
import unittest


sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(get=lambda *args, **kwargs: None, post=lambda *args, **kwargs: None),
)

from notifier import render_email


class RouteTypeDomesticCardTest(unittest.TestCase):
    def test_domestic_email_uses_integrated_domestic_recommendation_card(self):
        flight = {
            "flight_no": "MU5101",
            "flight_combo": "MU5101",
            "airline": "MU",
            "airline_name": "东方航空",
            "price": 680,
            "bare_price": 520,
            "airport_tax": 50,
            "fuel_tax": 110,
            "price_note": "含票面、机建、燃油",
            "departure_airport": "SHA",
            "arrival_airport": "PEK",
            "departure_time": "08:00",
            "arrival_time": "10:20",
            "aircraft": "空客A330",
            "stops": 0,
            "route_type": "domestic",
            "domestic_tags": ["商务友好", "低风险", "价格偏低"],
            "buyability": {"label": "需支付页确认"},
            "fare_rules": {
                "baggage": {"included": True, "checked_kg": 20, "note": "含20kg托运"},
                "refund": {"label": "退改适中", "note": "高舱位"},
            },
            "punctuality": {"level": "较高", "note": "估算"},
            "effective_cost": {
                "effective_cost": 910,
                "breakdown_note": "票价¥680+机场交通约¥130+时间成本约¥100",
            },
        }
        payload = {
            "push_type": "低价线索",
            "route": "上海 → 北京",
            "route_type": "domestic",
            "display_price": 680,
            "transaction_price": 680,
            "verify_price": 720,
            "recommendation": "值得验证",
            "buy_condition": "支付页最终价≤¥720",
            "recommended_plans": [
                {
                    "label": "方案A",
                    "tier": "首选推荐",
                    "price": 680,
                    "estimated_price": 680,
                    "buy_condition": "若支付页最终价≤¥720，可购买",
                    "main_flight": flight,
                    "flight": flight,
                    "links": {"main": '<a href="https://example.com">携程</a>'},
                    "tags": "商务友好 | 低风险 | 价格偏低",
                    "baggage_line": "含20kg托运",
                }
            ],
            "trigger_reason": ["国内实时参考价进入低价区间"],
            "detail_url": "https://example.com/detail",
            "source_stats": {"juhe": {"count": 12, "route_type": "domestic"}},
            "invoice_preferences": {
                "invoice_needed": True,
                "invoice_special_vat": True,
                "invoice_cabin_limit": True,
                "cabin_policy": "economy_only",
            },
        }

        _, html = render_email(payload)

        self.assertIn("国内航班推荐卡", html)
        self.assertIn("实时含税价", html)
        self.assertIn("库存状态", html)
        self.assertIn("含20kg托运", html)
        self.assertIn("退改适中", html)
        self.assertIn("准点率", html)
        self.assertIn("有效出行成本", html)
        self.assertIn("商务友好 | 低风险 | 价格偏低", html)
        self.assertIn("开票/报销", html)
        self.assertIn("航司官网/携程", html)
        self.assertIn("企业差旅渠道", html)


if __name__ == "__main__":
    unittest.main()
