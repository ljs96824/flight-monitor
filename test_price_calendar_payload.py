import sys
import tempfile
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch


sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(get=lambda *args, **kwargs: None, post=lambda *args, **kwargs: None),
)
import storage


class PriceCalendarPayloadTest(unittest.TestCase):
    def setUp(self):
        self._storage_tmp = tempfile.TemporaryDirectory()
        self._storage_patch = patch.object(
            storage,
            "DB_PATH",
            Path(self._storage_tmp.name) / "prices.db",
        )
        self._storage_patch.start()

    def tearDown(self):
        self._storage_patch.stop()
        self._storage_tmp.cleanup()

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

    def test_international_nearby_dates_build_roundtrip_calendar(self):
        from notifier import _payload_price_calendar

        selected_date = (date.today() + timedelta(days=72)).isoformat()
        cheaper_date = (date.today() + timedelta(days=71)).isoformat()
        return_date = (date.today() + timedelta(days=77)).isoformat()
        analysis = {
            "round_trip": True,
            "nearby_dates": [
                {"date": selected_date, "min_price": 5000, "selected": True},
                {"date": cheaper_date, "min_price": 4000, "selected": False},
            ],
            "return_analysis": {
                "nearby_dates": [
                    {"date": return_date, "min_price": 3000, "selected": True},
                ]
            },
        }
        route_info = {
            "origin": "PVG",
            "destination": "KIX",
            "depart_date": selected_date,
            "return_date": return_date,
            "round_trip": True,
        }

        calendar = _payload_price_calendar(route_info, analysis)
        prices = {row["date"]: row["min_price"] for row in calendar["rows"]}

        self.assertEqual(calendar["scope"], "roundtrip")
        self.assertEqual(prices[selected_date], 8000)
        self.assertEqual(prices[cheaper_date], 7000)
        self.assertEqual(calendar["return_min_price"], 3000)

    def test_cheaper_date_email_has_calendar_evidence_and_layered_headline(self):
        from notifier import render_email

        selected_date = "2026-10-01"
        cheaper_date = "2026-09-30"
        payload = {
            "push_type": "前后日期更便宜",
            "route": "上海 → 大阪",
            "route_type": "international",
            "depart_date": selected_date,
            "return_date": "2026-10-06",
            "is_roundtrip": True,
            "recommendation": "保持监控本条航线",
            "buy_condition": "支付页单人价≤¥7,500(单人往返)",
            "budget_compare_price": 8000,
            "display_price": 8000,
            "current_price": 8000,
            "max_price": 7500,
            "ideal_price": 6000,
            "budget_compare_scope": "per_person_roundtrip",
            "budget_gap": {
                "is_over_budget": True,
                "over_max": 500,
                "over_ideal": 2000,
                "text": "高于最高价¥500 | 高于理想价¥2,000",
            },
            "price_calendar": {
                "scope": "roundtrip",
                "return_date": "2026-10-06",
                "return_min_price": 3000,
                "rows": [
                    {
                        "date": cheaper_date,
                        "weekday": "周三",
                        "min_price": 7000,
                        "outbound_min_price": 4000,
                        "return_min_price": 3000,
                        "selected": False,
                        "lowest": True,
                    },
                    {
                        "date": selected_date,
                        "weekday": "周四",
                        "min_price": 8000,
                        "outbound_min_price": 5000,
                        "return_min_price": 3000,
                        "selected": True,
                        "lowest": False,
                    },
                ],
                "savings": [],
            },
            "recommended_plans": [
                {
                    "label": "方案A",
                    "tier": "首选推荐",
                    "is_roundtrip": True,
                    "price": 8000,
                    "outbound_price": 5000,
                    "return_price": 3000,
                    "outbound_flight": {"flight_combo": "MU225", "stops": 0},
                    "return_flight": {"flight_combo": "JL891", "stops": 0},
                }
            ],
            "trigger_reason": ["前后日期存在更低往返参考价"],
        }

        subject, body = render_email(payload)

        self.assertIn("【超预算·别的日期更便宜】上海 → 大阪", subject)
        self.assertIn("低价日历", body)
        self.assertIn(
            "触发依据:09-30 ¥7,000(单人往返) 比你选的 10-01 ¥8,000(单人往返) 低 13%",
            body,
        )

    def test_roundtrip_cheaper_date_trigger_uses_roundtrip_calendar_scope(self):
        from analyzer import determine_push_type

        analysis_without_calendar = {
            "round_trip": True,
            "nearby_dates": [
                {"date": "2026-09-30", "min_price": 4000, "scope": "oneway"},
            ],
            "decision_prices": {
                "display_price": 8000,
                "budget_compare_price": 8000,
            },
        }
        without_calendar = determine_push_type(
            8000,
            target_price=6000,
            max_budget=7500,
            analysis_result=analysis_without_calendar,
        )

        analysis_with_calendar = {
            **analysis_without_calendar,
            "price_calendar": {
                "scope": "roundtrip",
                "rows": [
                    {"date": "2026-09-30", "min_price": 7000, "selected": False},
                    {"date": "2026-10-01", "min_price": 8000, "selected": True},
                ],
            },
        }

        with_calendar = determine_push_type(
            8000,
            target_price=6000,
            max_budget=7500,
            analysis_result=analysis_with_calendar,
        )

        self.assertNotEqual(without_calendar["type"], "前后日期更便宜")
        self.assertEqual(with_calendar["type"], "前后日期更便宜")

    @patch("main.collect_for_airport_matrix")
    @patch("logging.basicConfig")
    def test_international_nearby_dates_use_all_search_sources(
        self,
        _logging_mock,
        collect_mock,
    ):
        from main import collect_nearby_dates

        collect_mock.return_value = {"flights": [{"price": 1000}]}
        aggregator = types.SimpleNamespace(
            search_sources=[
                types.SimpleNamespace(name="hasdata"),
                types.SimpleNamespace(name="juhe"),
            ]
        )
        depart_date = (date.today() + timedelta(days=30)).isoformat()
        sub = {
            "route_type": "international",
            "origin": "PVG",
            "destination": "KIX",
            "origin_airports_active": ["PVG"],
            "destination_airports_active": ["KIX"],
            "depart_date": depart_date,
            "date_flexibility": 1,
        }

        collect_nearby_dates(aggregator, sub, target_min_price=1500)

        used_aggregator = collect_mock.call_args_list[0].args[0]
        self.assertEqual(
            [source.name for source in used_aggregator.search_sources],
            ["hasdata", "juhe"],
        )

    def test_international_payload_wires_nearby_calendar_into_email(self):
        from notifier import build_notification_payload, render_email

        selected_date = (date.today() + timedelta(days=72)).isoformat()
        cheaper_date = (date.today() + timedelta(days=71)).isoformat()
        return_date = (date.today() + timedelta(days=77)).isoformat()
        outbound = {
            "flight_no": "MU225",
            "flight_combo": "MU225",
            "price": 5000,
            "stops": 0,
        }
        return_flight = {
            "flight_no": "JL891",
            "flight_combo": "JL891",
            "price": 3000,
            "stops": 0,
        }
        return_analysis = {
            "all_flights": [return_flight],
            "price_range": [3000, 3000],
            # 生产链路通常只给去程设置弹性；固定返程日仍有本轮最低价。
            "nearby_dates": [],
        }
        analysis = {
            "all_flights": [outbound],
            "price_range": [5000, 5000],
            "current_min_price": 5000,
            "round_trip": True,
            "nearby_dates": [
                {"date": selected_date, "min_price": 5000, "selected": True},
                {"date": cheaper_date, "min_price": 4000, "selected": False},
            ],
            "return_analysis": return_analysis,
            "round_trip_analysis": {
                "top_combinations": [
                    {
                        "outbound": outbound,
                        "return": return_flight,
                        "outbound_price": 5000,
                        "return_price": 3000,
                        "total_price": 8000,
                    }
                ],
                "total_min": 8000,
            },
        }
        route_info = {
            "origin": "PVG",
            "destination": "KIX",
            "depart_date": selected_date,
            "return_date": return_date,
            "round_trip": True,
            "target_price": 6000,
            "max_budget": 7500,
            "budget_scope": "per_person",
            "notification_goals": {"primary": "cheaper_date"},
        }

        payload = build_notification_payload(
            analysis,
            return_analysis=return_analysis,
            route_info=route_info,
            subscription={
                "id": "intl-nearby-calendar",
                "basic": {"route_type": "international"},
            },
        )
        subject, body = render_email(payload)

        self.assertEqual(payload["push_type"], "前后日期更便宜")
        self.assertEqual(payload["price_calendar"]["scope"], "roundtrip")
        calendar_prices = {
            row["date"]: row["min_price"]
            for row in payload["price_calendar"]["rows"]
        }
        self.assertEqual(calendar_prices[selected_date], 8000)
        self.assertEqual(calendar_prices[cheaper_date], 7000)
        self.assertIn("【超预算·别的日期更便宜】", subject)
        self.assertIn(
            f"触发依据:{cheaper_date[5:]} ¥7,000(单人往返) "
            f"比你选的 {selected_date[5:]} ¥8,000(单人往返) 低 13%",
            body,
        )
        self.assertIn("低价日历", body)


if __name__ == "__main__":
    unittest.main()
