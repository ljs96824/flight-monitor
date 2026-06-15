import sys
import types
import unittest
from datetime import date, timedelta


sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(get=lambda *args, **kwargs: None, post=lambda *args, **kwargs: None),
)


class PriceCalendarPayloadTest(unittest.TestCase):
    def test_payload_carries_price_calendar_and_detail_renders_it(self):
        from notifier import build_notification_payload, render_detail_html

        selected_date = (date.today() + timedelta(days=2)).isoformat()
        cheaper_date = (date.today() + timedelta(days=1)).isoformat()
        analysis = {
            "all_flights": [
                {
                    "flight_no": "KN5978",
                    "flight_combo": "KN5978",
                    "airline": "KN",
                    "price": 527,
                    "total_duration_min": 130,
                    "stops": 0,
                    "segments": [
                        {
                            "flight_no": "KN5978",
                            "dep_airport": "PVG",
                            "arr_airport": "PKX",
                            "dep_time": f"{selected_date} 08:15",
                            "arr_time": f"{selected_date} 10:25",
                        }
                    ],
                    "availability": {"age_minutes": 5, "source_count": 1},
                }
            ],
            "price_range": [527, 900],
            "current_min_price": 527,
            "price_calendar": {
                "rows": [
                    {"date": cheaper_date, "weekday": "周二", "min_price": 480, "lowest": True, "selected": False},
                    {"date": selected_date, "weekday": "周三", "min_price": 527, "lowest": False, "selected": True},
                ],
                "savings": [
                    {"date": cheaper_date, "weekday": "周二", "price": 480, "save": 220, "tip": f"提前1天({cheaper_date} 周二)出发，省¥220"}
                ],
                "weekday_pattern": {"cheapest_weekday": "周二", "tip": "本航线周二通常更便宜"},
                "scope": "oneway",
            },
        }

        payload = build_notification_payload(
            analysis_result=analysis,
            route_info={
                "origin": "PVG",
                "destination": "PEK",
                "depart_date": selected_date,
                "round_trip": False,
                "subscription_id": "calendar-test",
                "notification_goals": {
                    "primary": "cheaper_date",
                    "secondary": ["nearby_date_cheaper"],
                },
            },
            source_stats={"juhe": {"count": 2, "status": "成功", "route_type": "domestic"}},
        )
        html = render_detail_html(payload)

        self.assertEqual(payload["price_calendar"]["rows"][0]["date"], cheaper_date)
        self.assertEqual(payload["price_calendar"]["rows"][1]["date"], selected_date)
        self.assertEqual(payload["push_type"], "前后日期更便宜")
        self.assertIn("低价日历", html)
        self.assertIn("单程最低参考价", html)
        self.assertIn("提前1天", html)
        self.assertIn("周二", html)


if __name__ == "__main__":
    unittest.main()
