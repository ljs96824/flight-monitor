import sys
import types
import unittest

sys.modules.setdefault("httpx", types.SimpleNamespace())

from notifier import (
    _payload_combo_plan,
    _render_payload_plan_card,
    _round_trip_combinations,
    _same_day_alternatives_body,
    render_detail_html,
    render_email,
    render_pushplus,
)


def _flight(combo, airline, dep, arr, dep_time, arr_time):
    return {
        "price": 3402,
        "flight_combo": combo,
        "airline_summary": airline,
        "airlines": [airline],
        "stops": 0,
        "segments": [
            {
                "flight_no": combo,
                "airline": airline,
                "aircraft": "Airbus A321",
                "dep_airport": dep,
                "arr_airport": arr,
                "dep_time": dep_time,
                "arr_time": arr_time,
            }
        ],
        "availability": {"age_minutes": 5, "source_count": 2},
        "fare_verification": {"level": "partial"},
        "execution_risk": {"level": "low"},
        "execution_grade": "A",
    }


class PushPlusRenderingTest(unittest.TestCase):
    def test_roundtrip_pushplus_includes_airline_local_time_and_all_channels(self):
        route_info = {
            "origin": "上海",
            "destination": "大阪",
            "depart_date": "2026-10-01",
            "return_date": "2026-10-06",
        }
        combo = {
            "outbound": _flight(
                "9C6575",
                "春秋航空",
                "PVG",
                "KIX",
                "2026-10-01 08:05",
                "2026-10-01 11:20",
            ),
            "return": _flight(
                "9C6582",
                "春秋航空",
                "KIX",
                "PVG",
                "2026-10-06 19:30",
                "2026-10-06 21:00",
            ),
            "total_price": 6551,
            "transaction_total": 7211,
        }
        plan = _payload_combo_plan(combo, route_info, 0, "推荐")
        msg = render_pushplus(
            {
                "push_type": "低价提醒",
                "route": "上海 → 大阪",
                "current_price": 6551,
                "recommendation": "值得验证后购买",
                "buy_condition": "支付页≤¥6,900且含托运行李",
                "recommended_plans": [plan],
                "trigger_reason": ["接近理想价"],
                "freshness_minutes": 5,
                "collected_at": "2026-06-02 14:32",
                "detail_url": "https://example.com/detail",
            }
        )

        self.assertIn("9C6575｜春秋航空", msg)
        self.assertIn("浦东(PVG) 08:05(上海当地)", msg)
        self.assertIn("关西(KIX) 11:20(大阪当地)", msg)
        self.assertIn("9C6582｜春秋航空", msg)
        for channel in ["携程", "飞猪", "去哪儿", "Trip.com", "天巡", "Google Flights"]:
            self.assertGreaterEqual(msg.count(f">{channel}</a>"), 2)
        self.assertIn("点击验证最终价格", msg)
        self.assertIn("价格以各平台支付页为准", msg)

    def test_plan_card_channel_advice_does_not_fake_channel_prices(self):
        route_info = {"depart_date": "2026-06-10", "return_date": "2026-06-10"}
        combo = {
            "outbound": _flight(
                "MU5101",
                "MU",
                "SHA",
                "PEK",
                "2026-06-10 08:10",
                "2026-06-10 10:25",
            ),
            "return": _flight(
                "MU5108",
                "MU",
                "PEK",
                "SHA",
                "2026-06-10 18:40",
                "2026-06-10 20:55",
            ),
            "total_price": 1520,
        }
        plan = _payload_combo_plan(combo, route_info, 0, "推荐")

        rendered = _render_payload_plan_card(plan)

        self.assertIn("购买渠道建议", rendered)
        self.assertIn("支付页", rendered)
        self.assertNotIn("携程 ¥", rendered)
        self.assertNotIn("飞猪 ¥", rendered)
        self.assertNotIn("去哪儿 ¥", rendered)

    def test_pushplus_mentions_meeting_time_handoff(self):
        msg = render_pushplus(
            {
                "push_type": "低价提醒",
                "route": "上海 → 北京",
                "current_price": 1520,
                "recommendation": "值得验证",
                "buy_condition": "支付页最终价≤¥1,600",
                "recommended_plans": [],
                "trigger_reason": ["进入低价区间"],
                "time_filter_note": "时间筛选:按会议安排(10:00-16:00)+2.5h预留推算,你的通用时间偏好本次未参与筛选。",
                "detail_url": "https://example.com/detail",
            }
        )

        self.assertIn("通用时间偏好本次未参与筛选", msg)

    def test_roundtrip_plan_card_separates_outbound_return_and_total_prices(self):
        route_info = {"depart_date": "2026-06-10", "return_date": "2026-06-10"}
        outbound = _flight(
            "MU5101",
            "东航",
            "PVG",
            "PKX",
            "2026-06-10 08:00",
            "2026-06-10 10:20",
        )
        outbound["price"] = 680
        return_flight = _flight(
            "MU5108",
            "东航",
            "PKX",
            "PVG",
            "2026-06-10 19:50",
            "2026-06-10 22:05",
        )
        return_flight["price"] = 720
        combo = {
            "outbound": outbound,
            "return": return_flight,
            "outbound_price": 680,
            "return_price": 720,
            "total_price": 1400,
            "transaction_total": 1400,
        }
        plan = _payload_combo_plan(combo, route_info, 0, "推荐")

        rendered = _render_payload_plan_card(plan)

        self.assertIn("━━ 去程 ━━", rendered)
        self.assertIn("去程票价", rendered)
        self.assertIn("¥680", rendered)
        self.assertIn("━━ 返程 ━━", rendered)
        self.assertIn("返程票价", rendered)
        self.assertIn("¥720", rendered)
        self.assertIn("━━ 合计 ━━", rendered)
        self.assertIn("往返总价", rendered)
        self.assertIn("去程¥680 + 返程¥720", rendered)

    def test_pushplus_surfaces_same_day_no_feasible_note_near_top(self):
        msg = render_pushplus(
            {
                "push_type": "商务会议时间提示",
                "route": "上海 → 北京",
                "current_price": None,
                "transaction_price": None,
                "verify_price": None,
                "recommendation": "时间窗口太紧",
                "buy_condition": "建议调整会议缓冲或前一晚到达",
                "same_day_no_feasible_note": "按你的会议安排(10:00开始，2.5h预留)，去程需07:30前到达，当天无符合的早班直飞。",
                "recommended_plans": [],
                "detail_url": "https://example.com/detail",
            }
        )

        self.assertIn("当天往返提示", msg)
        self.assertLess(msg.index("当前判断:"), msg.index("当天往返提示"))


    def test_pushplus_surfaces_same_day_alternatives(self):
        msg = render_pushplus(
            {
                "push_type": "business time conflict",
                "route": "SHA -> PEK",
                "current_price": None,
                "transaction_price": None,
                "verify_price": None,
                "recommendation": "time window too tight",
                "buy_condition": "consider alternatives",
                "same_day_no_feasible_note": "need arrive before 06:35",
                "same_day_alternatives": [
                    {
                        "category": "previous_evening",
                        "title": "A previous evening",
                        "flight": {"flight_no": "MU5137", "departure_time": "19:00", "arrival_time": "21:15"},
                        "price": 620,
                        "tradeoff": "hotel cost, highest schedule stability",
                    },
                    {
                        "category": "previous_redeye",
                        "title": "B previous late night",
                        "flight": {"flight_no": "HU7610", "departure_time": "22:30", "arrival_time": "00:40"},
                        "price": 520,
                        "tradeoff": "fatigue risk",
                    },
                    {
                        "category": "same_day_earliest",
                        "title": "C same day earliest",
                        "flight": {"flight_no": "MU5099", "departure_time": "07:00", "arrival_time": "09:15"},
                        "price": 894,
                        "tradeoff": "late arrival risk",
                    },
                ],
                "recommended_plans": [],
                "detail_url": "https://example.com/detail",
            }
        )

        self.assertIn("MU5137", msg)
        self.assertIn("HU7610", msg)
        self.assertIn("MU5099", msg)
        self.assertIn("620", msg)

    def test_pushplus_no_primary_uses_no_plan_action_panel_and_alternatives_first(self):
        msg = render_pushplus(
            {
                "push_type": "business time conflict",
                "route": "SHA -> PEK",
                "recommendation": "time window too tight",
                "buy_condition": "consider alternatives",
                "same_day_no_feasible_note": "need arrive before 06:45, earliest arrival is 09:15",
                "same_day_alternatives": [
                    {
                        "category": "previous_evening",
                        "title": "Alternative A previous evening",
                        "flight": {"flight_no": "MU5137", "departure_time": "19:00", "arrival_time": "21:15"},
                        "price": 620,
                        "tradeoff": "extra hotel cost, most stable",
                    },
                    {
                        "category": "previous_redeye",
                        "title": "Alternative B late night",
                        "flight": {"flight_no": "HU7610", "departure_time": "22:30", "arrival_time": "00:40"},
                        "price": 520,
                        "tradeoff": "fatigue risk",
                    },
                    {
                        "category": "same_day_earliest",
                        "title": "Alternative C same day earliest",
                        "flight": {"flight_no": "MU5099", "departure_time": "07:00", "arrival_time": "09:15"},
                        "price": 894,
                        "tradeoff": "late arrival risk",
                    },
                ],
                "recommended_plans": [],
                "detail_url": "https://example.com/detail",
            }
        )

        self.assertIn("无符合方案", msg)
        self.assertIn("当前判断:❌ 未找到完全符合条件的方案", msg)
        self.assertIn("可用备选:3个", msg)
        self.assertLess(msg.index("可用备选:3个"), msg.index("Alternative A"))
        self.assertIn("航班:MU5137", msg)
        self.assertIn("价格:¥620", msg)

    def test_same_day_alternatives_body_uses_table_cards_and_booking_links(self):
        body = _same_day_alternatives_body(
            {
                "route": "SHA -> PEK",
                "depart_date": "2026-06-19",
                "same_day_alternatives": [
                    {
                        "category": "previous_evening",
                        "title": "Alternative A previous evening",
                        "date": "2026-06-18",
                        "flight": {
                            "flight_no": "MU5137",
                            "flight_combo": "MU5137",
                            "departure_airport": "SHA",
                            "arrival_airport": "PEK",
                            "departure_time": "19:00",
                            "arrival_time": "21:15",
                            "airline": "MU",
                            "aircraft": "A330",
                            "price": 620,
                            "stops": 0,
                        },
                        "price": 620,
                        "tradeoff": "extra hotel cost, most stable",
                        "feasibility": "next day meeting is relaxed",
                    }
                ],
            }
        )

        self.assertIn("<table", body)
        self.assertIn("Alternative A previous evening", body)
        self.assertIn("日期", body)
        self.assertIn("出发前一天", body)
        self.assertIn("航班", body)
        self.assertIn("验证购票", body)
        self.assertIn("携程", body)
        self.assertIn("飞猪", body)
        self.assertIn("去哪儿", body)

    def test_email_and_detail_put_alternatives_before_analysis_when_no_primary(self):
        payload = {
            "push_type": "business time conflict",
            "route": "SHA -> PEK",
            "same_day_no_feasible_note": "need arrive before 06:45, earliest arrival is 09:15",
            "same_day_alternatives": [
                {
                    "category": "previous_evening",
                    "title": "Alternative A previous evening",
                    "date": "2026-06-18",
                    "flight": {
                        "flight_no": "MU5137",
                        "departure_airport": "SHA",
                        "arrival_airport": "PEK",
                        "departure_time": "19:00",
                        "arrival_time": "21:15",
                        "price": 620,
                    },
                    "price": 620,
                    "tradeoff": "extra hotel cost, most stable",
                }
            ],
            "recommended_plans": [],
            "detail_url": "https://example.com/detail",
            "form_url": "https://example.com/",
        }

        subject, email_html = render_email(payload)
        detail_html = render_detail_html(payload)

        self.assertIn("【无符合方案】", subject)
        self.assertLess(email_html.index("可选备选方案"), email_html.index("价格口径与信号"))
        self.assertLess(detail_html.index("可选备选方案"), detail_html.index("为什么提醒你"))

    def test_round_trip_combinations_do_not_fallback_when_same_day_time_conflicts(self):
        combos = _round_trip_combinations(
            {
                "round_trip_analysis": {
                    "same_day_time_conflict": True,
                    "top_combinations": [],
                    "outbound_top3": [],
                    "return_top3": [
                        {"flight_no": "CA1511", "price": 700, "departure_time": "19:00"}
                    ],
                },
                "all_flights": [
                    {"flight_no": "CA1510", "price": 300, "arrival_time": "23:55"}
                ],
                "return_analysis": {
                    "all_flights": [
                        {"flight_no": "CA1511", "price": 700, "departure_time": "19:00"}
                    ]
                },
            }
        )

        self.assertEqual(combos, [])


if __name__ == "__main__":
    unittest.main()
