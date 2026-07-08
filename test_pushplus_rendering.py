import sys
import types
import unittest

sys.modules.setdefault("httpx", types.SimpleNamespace())

from notifier import (
    _candidate_price_summary_text,
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
        self.assertIn("验证首选方案A", msg)
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
        self.assertIn("去程¥680 单人单程 + 返程¥720 单人单程", rendered)

    def test_pushplus_surfaces_same_day_no_feasible_note_near_top(self):
        note = "本次无方案主因是【时间窗口】：会议要求 08:55 前落地，最早去程 MU5099 09:15 到，晚20分钟。"
        msg = render_pushplus(
            {
                "push_type": "商务会议时间提示",
                "route": "上海 → 北京",
                "current_price": None,
                "transaction_price": None,
                "verify_price": None,
                "recommendation": "时间窗口太紧",
                "buy_condition": "建议调整会议缓冲或前一晚到达",
                "same_day_no_feasible_note": note,
                "candidate_price_summary": {"lowest": 831, "count": 5, "reason": "时间不符合会议窗口"},
                "recommended_plans": [],
                "detail_url": "https://example.com/detail",
            }
        )

        self.assertIn("主因:" + note, msg)
        self.assertIn("价格:候选中最低", msg)
        self.assertLess(msg.index("主因:"), msg.index("价格:"))
        self.assertNotIn("????", msg)
        self.assertNotIn("返程当天无符合航班", msg)


    def test_no_primary_headline_prefers_diagnose_reason_over_stale_same_day_note(self):
        payload = {
            "push_type": "无符合方案·备选参考",
            "route": "上海 → 北京",
            "recommended_plans": [],
            "no_primary_reason": "本次无方案主因是【去程时间】:最早MU5099 09:15到,需08:00前落地,晚1h15m;返程有4个可选,非阻塞。",
            "no_primary_diagnosis": {
                "reason": "本次无方案主因是【去程时间】:最早MU5099 09:15到,需08:00前落地,晚1h15m;返程有4个可选,非阻塞。",
                "primary_cause": "outbound_time",
            },
            "same_day_no_feasible_note": "本次无方案主因是【返程时间】:去程可赶到，但返程需20:45后出发，当天没有符合返程窗口的航班。",
            "same_day_alternatives": [],
        }

        push_msg = render_pushplus(payload)
        _subject, email_html = render_email(payload)

        self.assertIn("本次无方案主因是【去程时间】", push_msg)
        self.assertIn("本次无方案主因是【去程时间】", email_html)
        self.assertNotIn("去程可赶到", push_msg)
        self.assertNotIn("去程可赶到", email_html)
        self.assertNotIn("本次无方案主因是【返程时间】", push_msg)
        self.assertNotIn("本次无方案主因是【返程时间】", email_html)
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

    def test_same_day_alternatives_body_renders_roundtrip_legs_and_total(self):
        body = _same_day_alternatives_body(
            {
                "route": "SHA -> PEK",
                "depart_date": "2026-06-19",
                "same_day_alternatives": [
                    {
                        "category": "previous_evening",
                        "title": "Alternative A previous evening",
                        "date": "2026-06-18",
                        "outbound": {
                            "flight_no": "MU5137",
                            "departure_airport": "SHA",
                            "arrival_airport": "PEK",
                            "departure_date": "2026-06-18",
                            "arrival_date": "2026-06-18",
                            "departure_time": "19:00",
                            "arrival_time": "21:15",
                            "price": 620,
                        },
                        "return": {
                            "flight_no": "CA1589",
                            "departure_airport": "PEK",
                            "arrival_airport": "SHA",
                            "departure_date": "2026-06-19",
                            "arrival_date": "2026-06-19",
                            "departure_time": "21:30",
                            "arrival_time": "23:30",
                            "price": 1350,
                        },
                        "outbound_price": 620,
                        "return_price": 1350,
                        "roundtrip_price": 1970,
                        "price": 1970,
                        "tradeoff": "extra hotel cost, most stable",
                    }
                ],
            }
        )

        self.assertIn("MU5137", body)
        self.assertIn("CA1589", body)
        self.assertIn("1,970", body)
        self.assertIn("620", body)
        self.assertIn("1,350", body)
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

    def test_same_day_reserve_rendering_uses_breakdown_single_source(self):
        windows = {
            "buffer_model": "airport_split",
            "business_start": "10:00",
            "business_end": "16:00",
            "arrival_buffer_min": 999,
            "checkin_buffer_min": 999,
            "transport_min": 999,
            "outbound_reserve_minutes": 999,
            "return_reserve_minutes": 999,
            "outbound_arrive_by": "00:00",
            "return_depart_after": "23:59",
            "reserve_breakdown": {
                "legacy": False,
                "outbound": {
                    "airport_iata": "PKX",
                    "airport_size": "mega",
                    "airport_buffer_min": 120,
                    "buffer_label": "到达机场缓冲",
                    "transport_min": 60,
                    "transport_source": "用户填写",
                    "margin_min": 24,
                    "margin_ratio": 0.4,
                    "rush_hour": True,
                    "safety_min": 25,
                    "total_min": 229,
                },
                "return": {
                    "airport_iata": "PKX",
                    "airport_size": "mega",
                    "airport_buffer_min": 110,
                    "buffer_label": "值机安检缓冲",
                    "transport_min": 60,
                    "transport_source": "用户填写",
                    "margin_min": 18,
                    "margin_ratio": 0.3,
                    "rush_hour": False,
                    "safety_min": 25,
                    "total_min": 213,
                },
                "windows": {"arrive_by": "06:11", "depart_after": "19:33"},
            },
        }
        payload = {
            "push_type": "time check",
            "route": "SHA -> PEK",
            "display_price": 1400,
            "transaction_price": 1400,
            "verify_price": 1500,
            "recommendation": "check",
            "buy_condition": "pay page <= 1500",
            "recommended_plans": [
                {
                    "label": "Plan A",
                    "is_roundtrip": True,
                    "same_day_round_trip": True,
                    "same_day_windows": windows,
                    "stay_hours": 6,
                    "price": 1400,
                    "estimated_price": 1400,
                    "outbound_price": 680,
                    "return_price": 720,
                    "outbound_flight": {"flight_no": "MU1", "departure_time": "05:00", "arrival_time": "07:00"},
                    "return_flight": {"flight_no": "MU2", "departure_time": "19:40", "arrival_time": "21:30"},
                }
            ],
        }

        msg = render_pushplus(payload)
        _, email_html = render_email(payload)

        self.assertNotIn("去程总预留≈229分钟", msg)
        self.assertIn("更多完整分析见网页详情", msg)
        self.assertNotIn("预留999", msg)
        self.assertNotIn("缓冲999", msg)
        self.assertNotIn("车程999", msg)
        self.assertIn("去程总预留≈229分钟", email_html)
        self.assertNotIn("预留999", email_html)
        self.assertNotIn("缓冲999", email_html)
        self.assertNotIn("车程999", email_html)

    def test_pushplus_and_email_acknowledge_previous_feedback(self):
        payload = {
            "push_type": "值得验证",
            "route": "上海 → 北京",
            "display_price": 680,
            "transaction_price": 680,
            "verify_price": 720,
            "recommendation": "值得验证",
            "buy_condition": "支付页≤¥720",
            "feedback_ack": "📌 你反馈过这条买不到,本次已重新核实可购买性,以下为最新采集结果。",
            "recommended_plans": [
                {
                    "label": "方案A",
                    "price": 680,
                    "estimated_price": 680,
                    "summary": "MU5101",
                    "main_flight": {"flight_no": "MU5101", "price": 680},
                }
            ],
            "detail_url": "https://example.com/detail",
            "form_url": "https://example.com/",
        }

        push_msg = render_pushplus(payload)
        _, email_html = render_email(payload)

        self.assertIn("你反馈过这条买不到", push_msg)
        self.assertIn("重新核实可购买性", email_html)

    def test_detail_link_copy_marks_web_page_as_supplemental(self):
        _, email_html = render_email(
            {
                "push_type": "值得验证",
                "route": "上海 → 北京",
                "display_price": 680,
                "transaction_price": 680,
                "verify_price": 720,
                "recommendation": "值得验证",
                "buy_condition": "支付页≤¥720",
                "recommended_plans": [],
                "detail_url": "https://example.com/detail",
                "form_url": "https://example.com/",
            }
        )

        self.assertIn("查看网页版完整分析(如未显示请稍后刷新)", email_html)
        self.assertNotIn(">查看网页详情<", email_html)

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



    def test_candidate_price_summary_raises_when_budget_primary_price_is_below_budget(self):
        from notifier import _candidate_price_summary_text

        payload = {
            "candidate_price_summary": {
                "lowest": 1200,
                "count": 2,
                "reason": "\u8d85\u51fa\u9884\u7b97",
                "price_scope": "per_person_roundtrip",
                "max_budget": 1700,
                "max_budget_scope": "per_person_roundtrip",
                "primary_cause": "budget",
            }
        }

        with self.assertRaises(AssertionError):
            _candidate_price_summary_text(payload)

    def test_candidate_price_summary_labels_budget_scope_price(self):
        from notifier import _candidate_price_summary_text

        text = _candidate_price_summary_text(
            {
                "candidate_price_summary": {
                    "lowest": 2551,
                    "count": 2,
                "reason": "\u8d85\u51fa\u9884\u7b97",
                    "price_scope": "per_person_roundtrip",
                    "max_budget": 1700,
                    "max_budget_scope": "per_person_roundtrip",
                    "primary_cause": "budget",
                }
            }
        )

        self.assertIn("\u5019\u9009\u4e2d\u6700\u4f4e\u00a52,551 \u5355\u4eba\u5f80\u8fd4(\u4f46\u8d85\u51fa\u9884\u7b97)", text)
        self.assertNotIn("?", text)


    def test_candidate_price_summary_defaults_to_readable_reason_without_question_marks(self):
        from notifier import _candidate_price_summary_text

        text = _candidate_price_summary_text(
            {
                "candidate_price_summary": {
                    "lowest": 2551,
                    "count": 1,
                    "price_scope": "per_person_roundtrip",
                    "max_budget": 1700,
                    "max_budget_scope": "per_person_roundtrip",
                }
            }
        )

        self.assertIn("\u5019\u9009\u4e2d\u6700\u4f4e\u00a52,551 \u5355\u4eba\u5f80\u8fd4(\u4f46\u4e0d\u6ee1\u8db3\u5f53\u524d\u7ea6\u675f)", text)
        self.assertNotIn("?", text)

    def test_candidate_price_summary_repairs_legacy_question_mark_budget_reason(self):
        from notifier import _candidate_price_summary_text

        text = _candidate_price_summary_text(
            {
                "candidate_price_summary": {
                    "lowest": 2551,
                    "count": 2,
                    "reason": "????",
                    "price_scope": "per_person_roundtrip",
                    "max_budget": 1700,
                    "max_budget_scope": "per_person_roundtrip",
                    "primary_cause": "budget",
                }
            }
        )

        self.assertIn("\u5019\u9009\u4e2d\u6700\u4f4e\u00a52,551 \u5355\u4eba\u5f80\u8fd4(\u4f46\u8d85\u51fa\u9884\u7b97)", text)
        self.assertNotIn("?", text)

if __name__ == "__main__":
    unittest.main()

