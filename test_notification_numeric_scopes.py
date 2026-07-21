import sys
import types
import unittest


sys.modules.setdefault("httpx", types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None))

from analyzer import build_excluded_roundtrip_combos, determine_push_type
from notifier import (
    _email_channel_picker,
    _email_detail_charts_body,
    _email_excluded_compact_body,
    _email_action_panel_body,
    _email_price_calendar_body,
    _same_day_alternatives_body,
    _plan_effective_cost_line,
    _plan_feasibility_line,
    build_notification_payload,
    render_email,
    render_pushplus,
)


class NotificationNumericScopesTest(unittest.TestCase):
    def _over_budget_payload(self):
        plan = {
            "label": "\u65b9\u6848A",
            "tier": "\u9996\u9009\u63a8\u8350",
            "is_roundtrip": True,
            "price": 2560,
            "outbound_price": 795,
            "return_price": 1765,
            "purchase_mode": "\u4e24\u4e2a\u5355\u7a0b\u62fc\u63a5",
            "outbound_push_line": "\u53bb\u7a0b MU5099 SHA07:00\u2192PEK09:15",
            "return_push_line": "\u8fd4\u7a0b MU5166 PEK21:30\u2192SHA23:25",
            "links": {
                "outbound": '<a href="https://example.com/out">携程</a>',
                "return": '<a href="https://example.com/ret">携程</a>',
            },
            "tags": "\u4ef7\u683c\u6700\u4f18\u00b7\u65f6\u95f4\u6700\u4f18\u00b7\u5c11\u6298\u817e",
            "feasibility": {
                "outbound": {"level": "\u53ef\u884c", "margin_min": 1235},
            },
        }
        return {
            "push_type": "\u6da8\u4ef7\u98ce\u9669",
            "route": "\u4e0a\u6d77 \u2192 \u5317\u4eac",
            "recommendation": "\u4e0d\u5efa\u8bae\u8d2d\u4e70,\u7ee7\u7eed\u76d1\u63a7",
            "display_price": 2560,
            "current_price": 2560,
            "ideal_price": 1600,
            "max_price": 2000,
            "buy_condition": "\u5f53\u524d\u641c\u7d22\u4ef7\u5df2\u8d85\u8fc7\u6700\u9ad8\u53ef\u63a5\u53d7\u4ef7",
            "diff_from_last": {"diff": 920},
            "trigger_reason": ["\u8f83\u4e0a\u6b21\u63d0\u9192\u4e0a\u6da8\u00a5920"],
            "travel_scenarios": ["business"],
            "recommendation_basis": {"scenario_labels": ["\u5546\u52a1\u4f1a\u8bae"]},
            "recommended_plans": [plan],
            "detail_url": "https://example.com/detail",
            "form_url": "https://example.com/form",
            "feedback_url": "https://example.com/feedback",
            "price_calendar": {
                "scope": "oneway",
                "rows": [
                    {"date": "2026-06-18", "weekday": "\u5468\u56db", "min_price": 1280, "selected": True},
                    {"date": "2026-06-20", "weekday": "\u5468\u516d", "min_price": 520, "lowest": True},
                    {"date": "2026-06-21", "weekday": "\u5468\u65e5", "min_price": 560},
                ],
                "weekday_pattern": {"tip": "\u672c\u822a\u7ebf\u5468\u516d/\u5468\u65e5\u901a\u5e38\u66f4\u4fbf\u5b9c"},
            },
        }

    def test_action_panel_shows_budget_gap_and_three_next_steps(self):
        payload = self._over_budget_payload()
        plan = payload["recommended_plans"][0]

        body = _email_action_panel_body(payload, plan, "\u652f\u4ed8\u9875\u2264\u00a52,000", "\u8f83\u4e0a\u6b21\u63d0\u9192\u4e0a\u6da8\u00a5920")

        self.assertIn("\u9884\u7b97\u5dee\u8ddd", body)
        self.assertIn("\u00a5560", body)
        self.assertIn("\u00a5960", body)
        self.assertIn("\u7ee7\u7eed\u76ef\u8fd9\u6761\u822a\u7ebf\u7b49\u964d\u4ef7", body)
        self.assertIn("\u4fdd\u6301\u5f53\u524d\u76d1\u63a7", body)
        self.assertNotIn("\u4e0b\u4e00\u6b65:\u7ee7\u7eed\u76d1\u63a7", body)
        self.assertIn("\u8c03\u6574\u9884\u7b97\u6216\u65e5\u671f", body)
        self.assertIn("\u521a\u9700\u5fc5\u987b\u51fa\u884c", body)
        self.assertIn("\u5546\u52a1\u4f1a\u8bae", body)

    def test_roundtrip_tracking_difference_is_labeled_in_email_and_pushplus(self):
        payload = self._over_budget_payload()
        payload["diff_from_last"] = {
            "last_price": 12215,
            "diff": 211,
            "scope": "per_person_roundtrip",
        }
        payload["trigger_reason"] = []

        pushplus = render_pushplus(payload)
        _, email = render_email(payload)

        self.assertIn("比上次涨¥211（单人往返）", pushplus)
        self.assertIn("较上次提醒：上涨¥211（单人往返）", email)

    def test_action_panel_budget_reason_uses_budget_compare_price_scope(self):
        plan = {
            "label": "\u65b9\u6848A",
            "tier": "\u9996\u9009\u63a8\u8350",
            "is_roundtrip": True,
            "price": 2111,
            "outbound_push_line": "\u53bb\u7a0b MU5099 SHA07:00\u2192PKX09:15",
            "return_push_line": "\u8fd4\u7a0b MU5128 PKX21:00\u2192SHA23:10",
        }
        payload = {
            "recommendation": "\u53ef\u9a8c\u8bc1",
            "is_roundtrip": True,
            "display_price": 6333,
            "current_price": 6333,
            "budget_compare_price": 2111,
            "budget_compare_scope": "per_person_roundtrip",
            "max_price": 4000,
            "max_budget_scope": "per_person",
            "recommended_plans": [plan],
            "trigger_reason": ["\u641c\u7d22\u53c2\u8003\u4ef7\u5728\u9884\u7b97\u5185"],
            "detail_url": "https://example.com/detail",
            "form_url": "https://example.com/form",
            "feedback_url": "https://example.com/feedback",
        }

        body = _email_action_panel_body(payload, plan, "\u652f\u4ed8\u9875\u2264\u00a54,000", "")

        self.assertIn("\u00a52,111", body)
        self.assertIn("\u00a54,000", body)
        self.assertIn("\u672a\u8d85\u9884\u7b97", body)
        self.assertNotIn("\u00a56,333", body)
        self.assertNotIn("\u9ad8\u4e8e\u4f60\u7684\u6700\u9ad8\u53ef\u63a5\u53d7\u4ef7", body)

        pushplus = render_pushplus(payload)
        self.assertIn("\u00a52,111", pushplus)
        self.assertIn("\u00a54,000", pushplus)
        self.assertIn("\u672a\u8d85\u9884\u7b97", pushplus)
        self.assertNotIn("\u00a56,333", pushplus)
        self.assertNotIn("\u9ad8\u4e8e\u4f60\u7684\u6700\u9ad8\u53ef\u63a5\u53d7\u4ef7", pushplus)
    def test_same_day_alternative_card_marks_single_adult_roundtrip_budget_overage(self):
        payload = {
            "same_day_alternatives": [
                {
                    "title": "备选C·当天最早班",
                    "category": "same_day_earliest",
                    "outbound": {
                        "flight_combo": "MU5099",
                        "departure_airport": "SHA",
                        "arrival_airport": "PKX",
                        "departure_date": "2026-06-26",
                        "arrival_date": "2026-06-26",
                        "departure_time": "07:00",
                        "arrival_time": "09:15",
                        "price": 831,
                    },
                    "return": {
                        "flight_combo": "MU5170",
                        "departure_airport": "PKX",
                        "arrival_airport": "SHA",
                        "departure_date": "2026-06-26",
                        "arrival_date": "2026-06-26",
                        "departure_time": "21:00",
                        "arrival_time": "23:10",
                        "price": 1720,
                    },
                    "outbound_price": 831,
                    "return_price": 1720,
                    "roundtrip_price": 2551,
                    "over_budget": True,
                    "budget_overage": 951,
                    "budget_scope_label": "单人往返 vs 上限1600",
                }
            ],
            "depart_date": "2026-06-26",
            "return_date": "2026-06-26",
            "route_info": {"depart_date": "2026-06-26", "return_date": "2026-06-26"},
        }

        body = _same_day_alternatives_body(payload)

        self.assertIn("MU5170", body)
        self.assertNotIn("MU5128", body)
        self.assertIn("¥1,720", body)
        self.assertNotIn("¥1,921", body)
        self.assertIn("验证此备选", body)
        self.assertIn("去程", body)
        self.assertIn("返程", body)
        self.assertIn("超出预算", body)
        self.assertIn("¥951", body)
        self.assertIn("单人往返 vs 上限1600", body)

    def test_same_day_alternative_card_separates_total_and_single_adult_scope(self):
        payload = {
            "same_day_alternatives": [
                {
                    "title": "备选C·当天最早班",
                    "category": "same_day_earliest",
                    "outbound": {
                        "flight_combo": "MU5099",
                        "departure_airport": "SHA",
                        "arrival_airport": "PKX",
                        "departure_date": "2026-06-26",
                        "arrival_date": "2026-06-26",
                        "departure_time": "07:00",
                        "arrival_time": "09:15",
                        "price": 831,
                    },
                    "return": {
                        "flight_combo": "MU5170",
                        "departure_airport": "PKX",
                        "arrival_airport": "SHA",
                        "departure_date": "2026-06-26",
                        "arrival_date": "2026-06-26",
                        "departure_time": "21:00",
                        "arrival_time": "23:10",
                        "price": 1720,
                    },
                    "outbound_price": 831,
                    "return_price": 1720,
                    "single_adult_price": 2551,
                    "roundtrip_price": 7653,
                    "price": 7653,
                    "passenger_pricing": {
                        "applies": True,
                        "factor": 3,
                        "passenger_label": "3成人",
                        "total_price": 7653,
                        "single_adult_price": 2551,
                    },
                    "over_budget": True,
                    "budget_overage": 6053,
                    "budget_scope_label": "全员往返 vs 总上限1600",
                }
            ],
            "depart_date": "2026-06-26",
            "return_date": "2026-06-26",
            "route_info": {"depart_date": "2026-06-26", "return_date": "2026-06-26"},
        }

        body = _same_day_alternatives_body(payload)

        self.assertIn("全员往返总价", body)
        self.assertIn("¥7,653", body)
        self.assertIn("单人往返参考", body)
        self.assertIn("¥2,551", body)
        self.assertIn("去¥831 单人单程 + 返¥1,720 单人单程", body)
        self.assertIn("超出预算", body)
        self.assertIn("¥6,053", body)
        self.assertIn("全员往返 vs 总上限1600", body)


    def test_calendar_body_starts_with_selected_date_comparison(self):
        payload = self._over_budget_payload()

        body = _email_price_calendar_body(payload)

        self.assertIn("\u4f60\u9009\u768406-18\u5355\u7a0b\u00a51,280", body)
        self.assertIn("\u5904\u4e8e\u504f\u8d35", body)
        self.assertIn("单程最低¥520 单人单程(06-20 周六)", body)
        self.assertIn("省约¥760 单人单程/单程", body)
        self.assertIn("\u5468\u516d/\u5468\u65e5", body)

    def test_calendar_insight_uses_oneway_scope_for_roundtrip_payload(self):
        payload = {
            "is_roundtrip": True,
            "display_price": 2760,
            "price_calendar": {
                "scope": "oneway",
                "rows": [
                    {"date": "2026-06-20", "weekday": "\u5468\u516d", "min_price": 607},
                    {"date": "2026-06-21", "weekday": "\u5468\u65e5", "min_price": 599},
                    {"date": "2026-06-22", "weekday": "\u5468\u4e00", "min_price": 659},
                    {"date": "2026-06-23", "weekday": "\u5468\u4e8c", "min_price": 537, "lowest": True},
                    {"date": "2026-06-24", "weekday": "\u5468\u4e09", "min_price": 570},
                    {"date": "2026-06-25", "weekday": "\u5468\u56db", "min_price": 760},
                    {"date": "2026-06-26", "weekday": "\u5468\u4e94", "min_price": 636, "selected": True},
                    {"date": "2026-06-27", "weekday": "\u5468\u516d", "min_price": 646},
                    {"date": "2026-06-28", "weekday": "\u5468\u65e5", "min_price": 665},
                    {"date": "2026-06-29", "weekday": "\u5468\u4e00", "min_price": 679},
                    {"date": "2026-06-30", "weekday": "\u5468\u4e8c", "min_price": 605},
                    {"date": "2026-07-01", "weekday": "\u5468\u4e09", "min_price": 679},
                    {"date": "2026-07-02", "weekday": "\u5468\u56db", "min_price": 834},
                    {"date": "2026-07-03", "weekday": "\u5468\u4e94", "min_price": 669},
                ],
                "weekday_pattern": {
                    "min_date": "2026-06-23",
                    "min_weekday": "\u5468\u4e8c",
                    "min_price": 537,
                    "tip": "\u8fd1\u671f\u6700\u4f4e\u51fa\u73b0\u5728\u5468\u4e8c(2026-06-23,\u5355\u7a0b\u00a5537)",
                },
                "note": "\u4e3a\u5355\u7a0b\u6700\u4f4e\u53c2\u8003\u4ef7\uff0c\u5b9e\u4ed8\u4ee5\u652f\u4ed8\u9875\u4e3a\u51c6\u3002",
            },
        }

        body = _email_price_calendar_body(payload)

        self.assertIn("\u5355\u7a0b\u4ef7\u683c\u8d8b\u52bf", body)
        self.assertIn("\u4ec5\u4f9b\u53c2\u8003\u51fa\u53d1\u65e5\u9009\u62e9", body)
        self.assertIn("\u4f60\u9009\u768406-26\u5355\u7a0b\u00a5636", body)
        self.assertIn("\u4e2d\u7b49\u6c34\u5e73", body)
        self.assertIn("单程最低¥537 单人单程(06-23 周二)", body)
        self.assertIn("省约¥99 单人单程/单程", body)
        self.assertIn("\u5f80\u8fd4\u603b\u4ef7\u7ea6\u00a52,760", body)
        self.assertIn("\u5355\u7a0b\u8d8b\u52bf\u4ec5\u5e2e\u4f60\u53d1\u73b0\u4fbf\u5b9c\u7684\u51fa\u53d1\u65e5", body)
        self.assertIn("\u4e0d\u7b49\u4e8e\u5f80\u8fd4\u603b\u4ef7", body)
        self.assertNotIn("\u5f53\u524d\u5f80\u8fd4\u00a52,760", body)
        self.assertNotIn("\u4f60\u9009\u768406-26\u504f\u8d35", body)

    def test_roundtrip_calendar_body_uses_roundtrip_reference_scope(self):
        payload = {
            "is_roundtrip": True,
            "display_price": 2760,
            "price_calendar": {
                "scope": "roundtrip",
                "return_date": "2026-06-30",
                "return_min_price": 557,
                "rows": [
                    {
                        "date": "2026-06-23",
                        "weekday": "\u5468\u4e8c",
                        "outbound_min_price": 547,
                        "return_min_price": 557,
                        "min_price": 1104,
                        "lowest": True,
                    },
                    {
                        "date": "2026-06-26",
                        "weekday": "\u5468\u4e94",
                        "outbound_min_price": 679,
                        "return_min_price": 557,
                        "min_price": 1236,
                        "selected": True,
                    },
                ],
                "note": "\u6bcf\u884c=\u8be5\u51fa\u53d1\u65e5\u5355\u7a0b\u6700\u4f4e+\u8fd4\u7a0b\u65e5\u5355\u7a0b\u6700\u4f4e,\u4e3a\u5f80\u8fd4\u4ef7\u683c\u53c2\u8003\u4e0b\u9650\u3002",
            },
        }

        body = _email_price_calendar_body(payload)

        self.assertIn("\u5f80\u8fd4\u53c2\u8003\u4ef7", body)
        self.assertIn("\u8fd4\u7a0b\u65e5\u56fa\u5b9a06-30", body)
        self.assertIn("\u6bcf\u884c=\u8be5\u51fa\u53d1\u65e5\u5355\u7a0b\u6700\u4f4e + \u8fd4\u7a0b\u65e5(06-30)\u5355\u7a0b\u6700\u4f4e", body)
        self.assertIn("\u4f60\u9009\u768406-26\u5f80\u8fd4\u00a51,236", body)
        self.assertIn("往返最低¥1,104 单人往返(06-23 周二)", body)
        self.assertIn("省约¥132 单人往返/往返", body)
        self.assertIn("\u53bb\u00a5679+\u8fd4\u00a5557", body)
        self.assertIn("\u5f53\u524d\u5b9e\u9645\u65b9\u6848\u5f80\u8fd4\u00a52,760", body)
        self.assertNotIn("\u5355\u7a0b\u4ef7\u683c\u8d8b\u52bf", body)

    def test_pushplus_is_slim_and_uses_budget_action_panel(self):
        payload = self._over_budget_payload()
        payload["plan_status_change"] = {"msg": "\u4e0a\u6b21\u65b9\u6848\u6da8\u4ef7"}
        payload["recommendation_basis"]["plain_language"] = "\u8fd9\u662f\u5f88\u957f\u7684\u6392\u5e8f\u4f9d\u636e"

        text = render_pushplus(payload)

        self.assertIn("\u9884\u7b97\u5dee\u8ddd", text)
        self.assertIn("\u4f60\u53ef\u4ee5", text)
        self.assertIn("\u5355\u7a0b\u4ef7\u683c\u8d8b\u52bf\u6458\u8981", text)
        self.assertIn("\u4f60\u9009\u768406-18\u5355\u7a0b\u00a51,280", text)
        self.assertNotIn("\u63a8\u8350\u4f9d\u636e", text)
        self.assertNotIn("\u4e0a\u6b21\u65b9\u6848\u8ffd\u8e2a", text)

    def test_no_primary_payload_distinguishes_filtered_candidates_from_no_quote(self):
        outbound = [
            {"flight_no": "MU5099", "price": 795, "stops": 0, "departure_time": "07:00", "arrival_time": "09:15"},
            {"flight_no": "MU5128", "price": 2095, "stops": 0, "departure_time": "14:00", "arrival_time": "16:20"},
        ] + [
            {"flight_no": f"MU{i}", "price": 900 + i, "stops": 0, "departure_time": "12:00", "arrival_time": "14:00"}
            for i in range(17)
        ]
        return_flights = [
            {"flight_no": "MU5166", "price": 1765, "stops": 0, "departure_time": "21:30", "arrival_time": "23:25"}
        ]
        analysis = {
            "round_trip_analysis": {
                "same_day_time_conflict": True,
                "top_combinations": [],
                "same_day_alternatives": [],
                "same_day_no_feasible_note": "\u4f1a\u8bae\u7a97\u53e3\u65e0\u5b8c\u5168\u5339\u914d\u65b9\u6848",
                "total_min": 2560,
            },
            "all_flights": outbound,
            "return_analysis": {"all_flights": return_flights},
            "excluded_flights": [
                {"flight": f, "price": f["price"], "reason": "\u4f1a\u8bae\u65f6\u95f4\u7a97\u53e3\u4e0d\u7b26"}
                for f in outbound
            ],
        }

        payload = build_notification_payload(
            analysis,
            route_info={
                "round_trip": True,
                "origin": "SHA",
                "destination": "PEK",
                "depart_date": "2026-06-18",
                "return_date": "2026-06-18",
                "max_budget": 2000,
            },
            subscription={"id": "no-primary-diagnosis"},
        )
        text = render_pushplus(payload)

        self.assertEqual(payload["no_primary_diagnosis"]["total_candidates"], 19)
        self.assertEqual(payload["no_primary_diagnosis"]["valid_price_count"], 19)
        self.assertEqual(payload["candidate_price_summary"]["lowest"], 795)
        self.assertGreaterEqual(len(payload["same_day_alternatives"]), 1)
        self.assertIn("\u91c7\u96c6\u523019\u4e2a\u822a\u73ed", text)
        self.assertIn("\u5019\u9009\u4e2d\u6700\u4f4e\u00a5795", text)
        self.assertIn("MU5099", text)
        self.assertNotIn("\u7b26\u5408\u4f60\u8bbe\u7f6e\u7684\u76f4\u98de\u6761\u4ef6", text)

    def test_split_ticket_channel_picker_uses_leg_prices(self):
        plan = {
            "is_roundtrip": True,
            "purchase_mode": "\u4e24\u4e2a\u5355\u7a0b\u62fc\u63a5",
            "price": 2560,
            "outbound_price": 795,
            "return_price": 1765,
            "links": {
                "outbound": '<a href="https://example.com/out">携程</a>',
                "return": '<a href="https://example.com/ret">携程</a>',
            },
        }

        html = _email_channel_picker(plan)

        self.assertIn("795", html)
        self.assertIn("1,765", html)
        self.assertIn("2,560", html)
        self.assertIn("out", html)
        self.assertIn("ret", html)
        self.assertLess(html.find("795"), html.find("https://example.com/out"))
        self.assertLess(html.find("1,765"), html.find("https://example.com/ret"))

    def test_plan_cards_bind_verification_links_to_each_plan(self):
        def links(prefix):
            return (
                f'<a href="https://example.com/{prefix}-ctrip" target="_blank">携程</a> | '
                f'<a href="https://example.com/{prefix}-fliggy" target="_blank">飞猪</a> | '
                f'<a href="https://example.com/{prefix}-qunar" target="_blank">去哪儿</a> | '
                f'<a href="https://example.com/{prefix}-airline" target="_blank">航司官网</a>'
            )

        payload = {
            "push_type": "值得验证",
            "route": "上海 → 北京",
            "recommendation": "值得验证",
            "buy_condition": "以支付页为准",
            "detail_url": "https://example.com/detail",
            "form_url": "https://example.com/form",
            "feedback_url": "https://example.com/feedback",
            "recommended_plans": [
                {
                    "label": "方案A",
                    "tier": "首选推荐",
                    "is_roundtrip": True,
                    "purchase_mode": "两个单程拼接",
                    "price": 2760,
                    "outbound_price": 1410,
                    "return_price": 1350,
                    "outbound_push_line": "去程:MU5099 SHA07:00→PEK09:15",
                    "return_push_line": "返程:CA1589 PEK21:30→SHA23:25",
                    "outbound_flight": {"flight_combo": "MU5099", "stops": 0},
                    "return_flight": {"flight_combo": "CA1589", "stops": 0},
                    "links": {"outbound": links("a-out"), "return": links("a-ret")},
                },
                {
                    "label": "方案B",
                    "tier": "次选方案",
                    "is_roundtrip": True,
                    "purchase_mode": "两个单程拼接",
                    "price": 2760,
                    "outbound_price": 1410,
                    "return_price": 1350,
                    "outbound_push_line": "去程:MU5101 SHA08:00→PEK10:15",
                    "return_push_line": "返程:CA1589 PEK21:30→SHA23:25",
                    "outbound_flight": {"flight_combo": "MU5101", "stops": 0},
                    "return_flight": {"flight_combo": "CA1589", "stops": 0},
                    "links": {"outbound": links("b-out"), "return": links("b-ret")},
                },
            ],
            "trigger_reason": ["测试"],
        }

        _subject, email_html = render_email(payload)
        action_panel = email_html[email_html.find("行动面板"):email_html.find("价格口径与信号")]
        plan_a = email_html[email_html.find("方案A"):email_html.find("方案B")]
        plan_b = email_html[email_html.find("方案B"):]
        operation_links = email_html[email_html.rfind("操作链接"):]
        push_text = render_pushplus(payload)

        self.assertNotIn("快速验证首选方案A", action_panel)
        self.assertNotIn("a-out-ctrip", action_panel)
        self.assertNotIn("b-out-ctrip", action_panel)
        self.assertIn("验证此方案", plan_a)
        self.assertIn("a-out-ctrip", plan_a)
        self.assertIn("a-ret-airline", plan_a)
        self.assertIn("验证此方案", plan_b)
        self.assertIn("b-out-ctrip", plan_b)
        self.assertIn("b-ret-airline", plan_b)
        self.assertNotIn("a-out-ctrip", plan_b)
        self.assertNotIn("a-out-ctrip", operation_links)
        self.assertIn("验证首选方案A", push_text)
        self.assertIn("a-out-ctrip", push_text)
        self.assertNotIn("b-out-ctrip", push_text)

    def test_split_ticket_verification_shows_leg_specific_status_and_rules(self):
        def links(prefix):
            return (
                f'<a href="https://example.com/{prefix}-ctrip" target="_blank">携程</a> | '
                f'<a href="https://example.com/{prefix}-fliggy" target="_blank">飞猪</a> | '
                f'<a href="https://example.com/{prefix}-qunar" target="_blank">去哪儿</a> | '
                f'<a href="https://example.com/{prefix}-airline" target="_blank">航司官网</a>'
            )

        payload = {
            "push_type": "值得验证",
            "route": "上海 → 北京",
            "recommendation": "值得验证",
            "buy_condition": "以支付页为准",
            "detail_url": "https://example.com/detail",
            "form_url": "https://example.com/form",
            "feedback_url": "https://example.com/feedback",
            "recommended_plans": [
                {
                    "label": "方案A",
                    "tier": "首选推荐",
                    "is_roundtrip": True,
                    "purchase_mode": "两个单程拼接",
                    "price": 2760,
                    "outbound_price": 1410,
                    "return_price": 1350,
                    "outbound_push_line": "去程:MU5099 SHA07:00→PEK09:15",
                    "return_push_line": "返程:CA1589 PEK20:30→SHA22:40",
                    "outbound_flight": {
                        "flight_combo": "MU5099",
                        "stops": 0,
                        "buyability": {"label": "可购买"},
                        "fare_rules": {
                            "baggage": {"included": True, "checked_kg": 20},
                            "refund": {"label": "退改适中"},
                        },
                    },
                    "return_flight": {
                        "flight_combo": "CA1589",
                        "stops": 0,
                        "buyability": {"label": "需验证"},
                        "fare_rules": {
                            "baggage": {"included": False, "note": "托运需另购"},
                            "refund": {"label": "退改严格"},
                        },
                    },
                    "links": {"outbound": links("a-out"), "return": links("a-ret")},
                }
            ],
            "trigger_reason": ["测试"],
        }

        _subject, email_html = render_email(payload)
        push_text = render_pushplus(payload)

        self.assertIn("验证此方案(两段需分别购买)", email_html)
        self.assertIn("去程 MU5099", email_html)
        self.assertIn("票面价:¥1,410", email_html)
        self.assertIn("库存:可购买", email_html)
        self.assertIn("行李:已含托运20kg", email_html)
        self.assertIn("退改:退改适中", email_html)
        self.assertIn("a-out-ctrip", email_html)
        self.assertIn("返程 CA1589", email_html)
        self.assertIn("票面价:¥1,350", email_html)
        self.assertIn("库存:需验证", email_html)
        self.assertIn("行李:仅含手提,托运需另购", email_html)
        self.assertIn("退改:退改严格", email_html)
        self.assertIn("a-ret-ctrip", email_html)
        self.assertIn("两段是独立机票,需分别下单", email_html)
        self.assertIn("去程 MU5099", push_text)
        self.assertIn("返程 CA1589", push_text)
        self.assertIn("注:两段独立票,需分别下单", push_text)

    def test_roundtrip_detail_hides_single_leg_channel_comparison(self):
        html = _email_detail_charts_body(
            {
                "is_roundtrip": True,
                "channel_price_rows": [
                    {
                        "label": "Google Flights",
                        "value": 1420,
                        "scope": "oneway",
                    }
                ],
                "plan_price_rows": [],
            }
        )

        self.assertNotIn("\u4e0d\u540c\u6e20\u9053\u62a5\u4ef7\u5bf9\u6bd4", html)

    def test_roundtrip_payload_does_not_promote_leg_channel_prices_to_combo_comparison(self):
        outbound = {
            "flight_no": "MU5099",
            "flight_combo": "MU5099",
            "price": 1420,
            "stops": 0,
            "departure_time": "07:00",
            "arrival_time": "09:15",
            "booking_options": [
                {"platform": "Google Flights", "price": 1420},
                {"platform": "\u643a\u7a0b", "price": 1430},
            ],
        }
        return_flight = {
            "flight_no": "MU5166",
            "flight_combo": "MU5166",
            "price": 1340,
            "stops": 0,
            "departure_time": "21:30",
            "arrival_time": "23:25",
        }
        analysis = {
            "round_trip_analysis": {
                "top_combinations": [
                    {
                        "outbound": outbound,
                        "return": return_flight,
                        "outbound_price": 1420,
                        "return_price": 1340,
                        "total_price": 2760,
                    }
                ],
                "total_min": 2760,
            },
        }

        payload = build_notification_payload(
            analysis,
            route_info={
                "round_trip": True,
                "origin": "SHA",
                "destination": "PEK",
                "depart_date": "2026-06-18",
                "return_date": "2026-06-18",
            },
            subscription={"id": "roundtrip-leg-channel"},
        )

        self.assertEqual(payload["channel_price_rows"], [])

    def test_roundtrip_payload_dedupes_identical_combinations(self):
        outbound = {"flight_no": "MU5099", "flight_combo": "MU5099", "price": 1420, "stops": 0}
        return_flight = {"flight_no": "MU5166", "flight_combo": "MU5166", "price": 1340, "stops": 0}
        combo = {
            "outbound": outbound,
            "return": return_flight,
            "outbound_price": 1420,
            "return_price": 1340,
            "total_price": 2760,
        }
        payload = build_notification_payload(
            {"round_trip_analysis": {"top_combinations": [combo, dict(combo)], "total_min": 2760}},
            route_info={
                "round_trip": True,
                "origin": "SHA",
                "destination": "PEK",
                "depart_date": "2026-06-18",
                "return_date": "2026-06-18",
            },
            subscription={"id": "roundtrip-dedupe"},
        )

        self.assertEqual(len(payload["recommended_plans"]), 1)

    def test_roundtrip_effective_cost_line_uses_roundtrip_components(self):
        plan = {
            "is_roundtrip": True,
            "price": 2560,
            "outbound_flight": {
                "effective_cost": {
                    "ticket_price": 795,
                    "transport_cost": 160,
                    "time_cost": 189,
                    "effective_cost": 1144,
                }
            },
            "return_flight": {
                "effective_cost": {
                    "ticket_price": 1765,
                    "transport_cost": 180,
                    "time_cost": 169,
                    "effective_cost": 2114,
                }
            },
        }

        line = _plan_effective_cost_line(plan)

        self.assertIn("3,258", line)
        self.assertIn("2,560", line)
        self.assertIn("340", line)
        self.assertIn("358", line)

    def test_feasibility_large_margin_is_humanized(self):
        line = _plan_feasibility_line(
            {
                "feasibility": {
                    "outbound": {
                        "level": "\u53ef\u884c",
                        "margin_min": 1235,
                        "transport_min": 30,
                        "transport_margin_min": 15,
                        "buffer_label": "\u503c\u673a\u5b89\u68c0\u7f13\u51b2",
                        "departure_buffer_min": 75,
                        "safety_min": 25,
                    }
                }
            }
        )

        self.assertIn("20", line)
        self.assertIn("\u65f6\u95f4\u5145\u8db3", line)
        self.assertNotIn("1235\u5206\u949f", line)

    def test_calendar_header_and_prices_mark_oneway_scope(self):
        body = _email_price_calendar_body(
            {
                "price_calendar": {
                    "scope": "oneway",
                    "rows": [
                        {
                            "date": "2026-06-20",
                            "weekday": "\u5468\u516d",
                            "min_price": 520,
                            "lowest": True,
                        }
                    ],
                }
            }
        )

        self.assertIn("\u5355\u7a0b\u6700\u4f4e\u53c2\u8003\u4ef7", body)
        self.assertIn("\u00a5520(\u5355\u7a0b)", body)

    def test_excluded_roundtrip_budget_reason_uses_roundtrip_budget_scope(self):
        outbound = {
            "scope": "outbound",
            "price": 900,
            "reason": "\u8d85\u8fc7\u6700\u9ad8\u53ef\u63a5\u53d7\u4ef7\u00a52000",
            "flight": {"price": 900, "flight_combo": "MU100", "stops": 0},
        }
        ret = {
            "price": 900,
            "flight_combo": "MU101",
            "stops": 0,
        }

        combos = build_excluded_roundtrip_combos(
            {"excluded_flights": [outbound]},
            {"all_flights": [ret]},
            recommended_total=2560,
            max_show=3,
            max_budget=2000,
        )

        self.assertEqual(len(combos), 1)
        reason_text = " ".join(combos[0]["reasons"])
        self.assertNotIn("\u8d85\u8fc7\u6700\u9ad8\u53ef\u63a5\u53d7\u4ef7", reason_text)
        self.assertIn("\u65f6\u95f4", reason_text)

    def test_excluded_roundtrip_all_over_budget_does_not_blame_cheaper_combo_budget(self):
        outbound = {
            "scope": "outbound",
            "price": 1000,
            "reason": "\u8d85\u8fc7\u6700\u9ad8\u53ef\u63a5\u53d7\u4ef7\u00a51,500",
            "flight": {"price": 1000, "flight_combo": "MU5107", "stops": 0},
        }
        ret = {
            "price": 1386,
            "flight_combo": "CA1507",
            "stops": 0,
        }

        combos = build_excluded_roundtrip_combos(
            {"excluded_flights": [outbound]},
            {"all_flights": [ret]},
            recommended_total=2760,
            max_show=3,
            max_budget=1500,
        )

        self.assertEqual(len(combos), 1)
        reason_text = " ".join(combos[0]["reasons"])
        self.assertTrue(combos[0].get("all_over_budget_reference"))
        self.assertIn("\u9884\u7b97\u5916", reason_text)
        self.assertNotIn("\u8d85\u8fc7\u6700\u9ad8\u53ef\u63a5\u53d7\u4ef7", reason_text)
        self.assertNotIn("\u5f80\u8fd4\u603b\u4ef7\u00a52,386\u8d85\u8fc7", reason_text)

    def test_excluded_compact_body_groups_shared_outbound_and_marks_budget_reference(self):
        payload = {
            "is_roundtrip": True,
            "current_price": 2760,
            "max_price": 1500,
            "excluded_plans": [
                {
                    "scope": "roundtrip",
                    "all_over_budget_reference": True,
                    "outbound": {"flight_combo": "MU5107", "departure_airport": "SHA", "departure_time": "11:00", "arrival_airport": "PEK", "arrival_time": "13:15", "aircraft": "773", "fare_rules": {"baggage": {"included": True, "checked_kg": 20}, "refund": {"label": "\u9000\u6539\u9002\u4e2d"}}, "buyability": {"label": "\u9700\u9a8c\u8bc1"}},
                    "return": {"flight_combo": "CA1507", "departure_airport": "PEK", "departure_time": "07:30", "arrival_airport": "SHA", "arrival_time": "10:00", "aircraft": "789", "fare_rules": {"baggage": {"included": False, "note": "\u6258\u8fd0\u9700\u53e6\u8d2d"}, "refund": {"label": "\u9000\u6539\u4e25\u683c"}}, "buyability": {"label": "\u9700\u652f\u4ed8\u9875\u786e\u8ba4"}},
                    "total_price": 2386,
                    "reason": "\u8fd4\u7a0b07:30\u51fa\u53d1,\u4f60\u5f53\u592917:00\u624d\u7ed3\u675f\u4f1a\u8bae,\u65f6\u95f4\u4e0d\u7b26",
                },
                {
                    "scope": "roundtrip",
                    "all_over_budget_reference": True,
                    "outbound": {"flight_combo": "MU5107", "departure_airport": "SHA", "departure_time": "11:00", "arrival_airport": "PEK", "arrival_time": "13:15", "aircraft": "773"},
                    "return": {"flight_combo": "MU5102", "departure_airport": "PEK", "departure_time": "08:00", "arrival_airport": "SHA", "arrival_time": "10:20", "aircraft": "33L"},
                    "total_price": 2483,
                    "reason": "\u8fd4\u7a0b08:00\u51fa\u53d1,\u4f1a\u8bae\u7ed3\u675f\u524d\u65e0\u6cd5\u4e58\u5750",
                },
            ],
        }

        body = _email_excluded_compact_body(payload)

        self.assertIn("\u9884\u7b97\u5916\u4f4e\u4ef7\u53c2\u8003", body)
        self.assertIn("\u5171\u540c\u53bb\u7a0b", body)
        self.assertIn("MU5107", body)
        self.assertIn("CA1507", body)
        self.assertIn("PEK07:30\u2192SHA10:00", body)
        self.assertIn("MU5102", body)
        self.assertIn("\u6ce2\u97f3777-300", body)
        self.assertIn("\u6ce2\u97f3787-9", body)
        self.assertIn("\u8fd4\u7a0b\u884c\u674e", body)
        self.assertIn("\u6258\u8fd0\u9700\u53e6\u8d2d", body)
        self.assertIn("\u8fd4\u7a0b\u9000\u6539", body)
        self.assertIn("\u9000\u6539\u4e25\u683c", body)
        self.assertNotIn("\u5df2\u6392\u9664\u7684\u66f4\u4f4e\u4ef7", body)

    def test_excluded_roundtrip_combo_carries_specific_basis_and_comparison(self):
        outbound = {
            "price": 1036,
            "flight_combo": "MU5107",
            "departure_airport": "SHA",
            "departure_time": "11:00",
            "arrival_airport": "PEK",
            "arrival_time": "13:15",
            "aircraft": "333",
            "stops": 0,
        }
        ret = {
            "price": 1350,
            "flight_combo": "CA1507",
            "departure_airport": "PEK",
            "departure_time": "07:30",
            "arrival_airport": "SHA",
            "arrival_time": "10:00",
            "aircraft": "789",
            "stops": 0,
        }
        recommended = {
            "total_price": 2760,
            "return": {
                "flight_combo": "CA1589",
                "departure_time": "20:30",
            },
        }

        combos = build_excluded_roundtrip_combos(
            {"all_flights": [outbound]},
            {"excluded_flights": [{"flight": ret, "reason": "\u4f1a\u8bae\u65f6\u95f4\u7a97\u53e3\u4e0d\u7b26"}]},
            recommended_total=2760,
            max_show=3,
            max_budget=3000,
            constraints={
                "same_day_round_trip": True,
                "business_start": "13:00",
                "business_end": "17:00",
                "direct_only": "must",
            },
            recommended_combo=recommended,
        )

        self.assertEqual(len(combos), 1)
        reason_text = combos[0]["reason"]
        comparison = "\n".join(combos[0]["comparison_points"])
        basis = " ".join(combos[0]["exclusion_basis"])
        self.assertIn("\u8fd4\u7a0b07:30\u51fa\u53d1", reason_text)
        self.assertIn("\u4f1a\u8bae13:00-17:00", reason_text)
        self.assertIn("\u65e9\u4e86\u7ea69\u5c0f\u65f630\u5206\u949f", reason_text)
        self.assertIn("\u65e0\u6cd5\u4e58\u5750", reason_text)
        self.assertIn("\u6bd4\u63a8\u8350\u4fbf\u5b9c\u00a5374", comparison)
        self.assertIn("\u63a8\u835020:30", comparison)
        self.assertIn("\u5f53\u5929\u5f80\u8fd4", basis)
        self.assertIn("\u4f1a\u8bae13:00-17:00", basis)

    def test_roundtrip_comparison_points_uses_combo_flight_context_without_nameerror(self):
        from analyzer import _roundtrip_comparison_points

        points = _roundtrip_comparison_points(
            {
                "total_price": 2386,
                "outbound": {"flight_combo": "MU5107", "arrival_time": "13:15"},
                "return": {"flight_combo": "CA1507", "departure_time": "07:30"},
                "reason": "返程时间不符",
            },
            {"return": {"flight_combo": "CA1589", "departure_time": "20:30"}},
            2760,
        )

        joined = "\n".join(points)
        self.assertIn("MU5107", joined)
        self.assertIn("13:15", joined)
        self.assertIn("比推荐便宜¥374", joined)
    def test_email_excluded_body_renders_full_roundtrip_cards_with_basis_and_comparison(self):
        payload = {
            "is_roundtrip": True,
            "current_price": 2760,
            "excluded_plans": [
                {
                    "scope": "roundtrip",
                    "outbound": {
                        "flight_combo": "MU5107",
                        "airline": "\u4e1c\u65b9\u822a\u7a7a",
                        "departure_airport": "SHA",
                        "departure_time": "11:00",
                        "arrival_airport": "PEK",
                        "arrival_time": "13:15",
                        "aircraft": "333",
                        "price": 1036,
                        "stops": 0,
                    },
                    "return": {
                        "flight_combo": "CA1507",
                        "airline": "\u4e2d\u56fd\u56fd\u9645\u822a\u7a7a",
                        "departure_airport": "PEK",
                        "departure_time": "07:30",
                        "arrival_airport": "SHA",
                        "arrival_time": "10:00",
                        "aircraft": "789",
                        "price": 1350,
                        "stops": 0,
                    },
                    "outbound_price": 1036,
                    "return_price": 1350,
                    "total_price": 2386,
                    "recommended_price": 2760,
                    "reason": "\u8fd4\u7a0b07:30\u51fa\u53d1,\u4f46\u4f60\u7684\u4f1a\u8bae13:00-17:00\u8fd8\u6ca1\u7ed3\u675f,\u8fd4\u7a0b\u65e9\u4e86\u7ea69\u5c0f\u65f630\u5206\u949f,\u65e0\u6cd5\u4e58\u5750\u3002",
                    "exclusion_basis": ["\u5f53\u5929\u5f80\u8fd4", "\u4f1a\u8bae13:00-17:00", "\u5fc5\u987b\u76f4\u98de"],
                    "comparison_points": [
                        "\u4ef7\u683c:\u6b64\u65b9\u6848\u00a52,386,\u6bd4\u63a8\u8350\u4fbf\u5b9c\u00a5374 \u2713",
                        "\u8fd4\u7a0b\u65f6\u95f4:\u6b64\u65b9\u684807:30(\u4e0d\u53ef\u7528) vs \u63a8\u835020:30(\u53ef\u7528) \u2717",
                    ],
                }
            ],
        }

        body = _email_excluded_compact_body(payload)

        self.assertIn("\u5df2\u6392\u9664\u65b9\u6848", body)
        self.assertIn("\u5f80\u8fd4\u00a52,386", body)
        self.assertIn("\u6bd4\u63a8\u8350\u65b9\u6848\u4fbf\u5b9c\u00a5374", body)
        self.assertIn("\u53bb\u7a0b", body)
        self.assertIn("MU5107", body)
        self.assertIn("\u8679\u6865(SHA) 11:00", body)
        self.assertIn("\u9996\u90fd(PEK) 13:15", body)
        self.assertIn("\u7a7a\u5ba2A330-300", body)
        self.assertIn("\u7968\u9762\u4ef7", body)
        self.assertIn("\u00a51,036", body)
        self.assertIn("\u8fd4\u7a0b", body)
        self.assertIn("CA1507", body)
        self.assertIn("\u9996\u90fd(PEK) 07:30", body)
        self.assertIn("\u6ce2\u97f3787-9", body)
        self.assertIn("\u00a51,350", body)
        self.assertIn("\u6392\u9664\u539f\u56e0(\u57fa\u4e8e\u4f60\u7684\u8bbe\u7f6e)", body)
        self.assertIn("\u8fd4\u7a0b\u65e9\u4e86\u7ea69\u5c0f\u65f630\u5206\u949f", body)
        self.assertIn("\u4f9d\u636e:\u5f53\u5929\u5f80\u8fd4\u00b7\u4f1a\u8bae13:00-17:00\u00b7\u5fc5\u987b\u76f4\u98de", body)
        self.assertIn("\u5bf9\u6bd4\u63a8\u8350\u65b9\u6848", body)
        self.assertIn("\u63a8\u835020:30", body)
        self.assertNotIn("\u5b8c\u6574\u6392\u9664\u65b9\u6848\u8be6\u60c5\u89c1\u7f51\u9875", body)

    def test_trigger_reason_uses_budget_compare_price_for_ideal_gap(self):
        meta = determine_push_type(
            8931,
            target_price=6000,
            max_budget=8000,
            analysis_result={
                "decision_prices": {
                    "display_price": 42422,
                    "budget_compare_price": 8931,
                    "budget_compare_scope": "per_person_roundtrip",
                }
            },
        )

        reasons = " | ".join(meta["reasons"])
        self.assertIn("距离理想入手价还差¥2,931", reasons)
        self.assertNotIn("¥36,422", reasons)

    def test_excluded_budget_basis_labels_all_passenger_conversion(self):
        outbound = {
            "flight": {"flight_combo": "MU225", "price": 3000, "stops": 0},
            "price": 3000,
            "excluded": True,
            "reason": "时间窗口不符",
        }
        ret = {
            "flight": {"flight_combo": "JL891", "price": 2000, "stops": 0},
            "price": 2000,
            "excluded": False,
        }

        combos = build_excluded_roundtrip_combos(
            {"excluded_flights": [outbound]},
            {"all_flights": [ret]},
            recommended_total=42422,
            max_budget=38000,
            constraints={
                "max_budget": 8000,
                "budget_scope": "per_person",
                "max_budget_scope": "per_person",
            },
            passengers={"adult": 2, "child": 1, "elderly": 2, "infant": 0},
            route_type="international",
        )

        self.assertEqual(len(combos), 1)
        basis = " ".join(combos[0]["exclusion_basis"])
        self.assertIn("最高可接受价¥38,000(全员,=单人¥8,000×4.75)", basis)

    def test_roundtrip_effective_cost_uses_single_person_ticket_scope(self):
        plan = {
            "is_roundtrip": True,
            "price": 42422,
            "single_adult_price": 8931,
            "passenger_pricing": {
                "applies": True,
                "factor": 4.75,
                "passengers": {"adult": 2, "child": 1, "elderly": 2, "infant": 0},
                "route_type": "international",
            },
            "outbound_flight": {
                "effective_cost": {
                    "ticket_price": 4805,
                    "transport_cost": 260,
                    "time_cost": 206,
                    "effective_cost": 5271,
                }
            },
            "return_flight": {
                "effective_cost": {
                    "ticket_price": 4126,
                    "transport_cost": 260,
                    "time_cost": 206,
                    "effective_cost": 4592,
                }
            },
        }

        line = _plan_effective_cost_line(plan)

        self.assertIn(
            "约¥9,863=机票¥8,931(单人往返)+机场交通约¥520+时间成本约¥412",
            line,
        )
        self.assertNotIn("机票¥42,422", line)

    def test_calendar_lowest_parenthesis_uses_lowest_row_unit_price(self):
        payload = {
            "is_roundtrip": True,
            "passenger_pricing": {
                "applies": True,
                "factor": 3,
                "passenger_count": 3,
                "passenger_label": "3成人",
                "passengers": {"adult": 3, "child": 0, "elderly": 0, "infant": 0},
                "single_adult_price": 1254,
            },
            "price_calendar": {
                "scope": "roundtrip",
                "return_date": "2026-07-31",
                "rows": [
                    {
                        "date": "2026-07-28",
                        "weekday": "周二",
                        "min_price": 1135,
                        "lowest": True,
                    },
                    {
                        "date": "2026-07-31",
                        "weekday": "周五",
                        "min_price": 1254,
                        "selected": True,
                    },
                ],
            },
        }

        body = _email_price_calendar_body(payload)

        self.assertIn("(单人往返¥1,135 单人往返×3)", body)
        self.assertNotIn("(单人往返¥1,254 单人往返×3)", body)


if __name__ == "__main__":
    unittest.main()

