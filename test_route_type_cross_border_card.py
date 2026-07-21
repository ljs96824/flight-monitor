import sys
import types
import unittest


sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(get=lambda *args, **kwargs: None, post=lambda *args, **kwargs: None),
)

from notifier import render_email


class RouteTypeCrossBorderCardTest(unittest.TestCase):
    def test_international_email_shows_cross_border_risk_modules(self):
        outbound = {
            "flight_combo": "MU583+AA123",
            "airline": "MU",
            "departure_airport": "PVG",
            "arrival_airport": "LAX",
            "departure_time": "2026-10-01 23:50",
            "arrival_time": "2026-10-01 19:30",
            "segments": [
                {
                    "flight_no": "MU583",
                    "airline": "MU",
                    "dep_airport": "PVG",
                    "arr_airport": "JFK",
                    "dep_time": "2026-10-01 23:50",
                    "arr_time": "2026-10-01 13:20",
                    "aircraft": "B777",
                },
                {
                    "flight_no": "AA123",
                    "airline": "AA",
                    "dep_airport": "JFK",
                    "arr_airport": "LAX",
                    "dep_time": "2026-10-01 16:00",
                    "arr_time": "2026-10-01 19:30",
                    "aircraft": "A321",
                },
            ],
            "layovers": [{"airport": "JFK", "city": "New York", "wait_minutes": 160}],
            "stops": 1,
            "total_duration_min": 1050,
            "route_type": "international",
        }
        payload = {
            "push_type": "值得验证",
            "route": "上海 → 洛杉矶",
            "route_type": "international",
            "recommendation": "值得验证",
            "buy_condition": "支付页≤¥6,900且含托运行李",
            "recommended_plans": [
                {
                    "label": "方案A",
                    "tier": "首选推荐",
                    "price": 6521,
                    "estimated_price": 7181,
                    "purchase_mode": "非联程",
                    "purchase_note": "两个单程分别购买",
                    "outbound_flight": outbound,
                    "return_flight": {**outbound, "flight_combo": "AA124+MU584"},
                    "is_roundtrip": True,
                    "links": {"outbound": '<a href="https://example.com">Trip.com</a>'},
                }
            ],
            "source_stats": {
                "hasdata": {"count": 10, "route_type": "international"},
                "juhe": {"count": 18, "route_type": "international"},
            },
            "detail_url": "https://example.com/detail",
        }

        _, body = render_email(payload)

        self.assertIn("过境签", body)
        self.assertIn("美国", body)
        self.assertIn("当地时间", body)
        self.assertIn("时差", body)
        self.assertIn("非联程", body)
        self.assertIn("自行转机提行李", body)
        self.assertIn("国际票务提示", body)
        self.assertIn("主源:Google Flights(HasData)—10个方案", body)
        self.assertIn("交叉/OTA:聚合数据—18个方案", body)

    def test_greater_china_email_shows_pass_permit_and_no_transit_visa_warning(self):
        flight = {
            "flight_no": "CX365",
            "airline": "CX",
            "departure_airport": "PVG",
            "arrival_airport": "HKG",
            "departure_time": "2026-10-01 09:20",
            "arrival_time": "2026-10-01 12:00",
            "aircraft": "A330",
            "stops": 0,
            "route_type": "greater_china",
        }
        payload = {
            "push_type": "低价线索",
            "route": "上海 → 香港",
            "route_type": "greater_china",
            "recommendation": "值得验证",
            "buy_condition": "以支付页为准",
            "recommended_plans": [
                {
                    "label": "方案A",
                    "tier": "首选推荐",
                    "price": 1880,
                    "main_flight": flight,
                    "flight": flight,
                    "links": {"main": '<a href="https://example.com">携程</a>'},
                }
            ],
            "source_stats": {
                "hasdata": {"count": 8, "route_type": "greater_china"},
                "juhe": {"count": 13, "route_type": "greater_china"},
            },
            "detail_url": "https://example.com/detail",
        }

        _, body = render_email(payload)

        self.assertIn("港澳通行证/台湾通行证", body)
        self.assertIn("签注", body)
        self.assertIn("国内OTA", body)
        self.assertIn("主源:Google Flights(HasData)—8个方案", body)
        self.assertIn("交叉/OTA:聚合数据—13个方案", body)
        self.assertNotIn("美国转机", body)
        self.assertNotIn("聚合数据(国内实时报价)", body)


if __name__ == "__main__":
    unittest.main()
