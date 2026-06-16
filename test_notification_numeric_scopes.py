import sys
import types
import unittest


sys.modules.setdefault("httpx", types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None))

from analyzer import build_excluded_roundtrip_combos
from notifier import (
    _email_channel_picker,
    _email_detail_charts_body,
    _email_excluded_compact_body,
    _email_action_panel_body,
    _email_price_calendar_body,
    _plan_effective_cost_line,
    _plan_feasibility_line,
    build_notification_payload,
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
        self.assertIn("\u7ee7\u7eed\u76d1\u63a7\u7b49\u964d\u4ef7", body)
        self.assertIn("\u8c03\u6574\u9884\u7b97\u6216\u65e5\u671f", body)
        self.assertIn("\u521a\u9700\u5fc5\u987b\u51fa\u884c", body)
        self.assertIn("\u5546\u52a1\u4f1a\u8bae", body)

    def test_calendar_body_starts_with_selected_date_comparison(self):
        payload = self._over_budget_payload()

        body = _email_price_calendar_body(payload)

        self.assertIn("\u4f60\u9009\u768406-18\u5355\u7a0b\u00a51,280", body)
        self.assertIn("\u5904\u4e8e\u504f\u8d35", body)
        self.assertIn("\u5355\u7a0b\u6700\u4f4e\u00a5520(06-20 \u5468\u516d)", body)
        self.assertIn("\u7701\u7ea6\u00a5760/\u5355\u7a0b", body)
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

        self.assertIn("\u4f60\u9009\u768406-26\u5355\u7a0b\u00a5636", body)
        self.assertIn("\u4e2d\u7b49\u6c34\u5e73", body)
        self.assertIn("\u5355\u7a0b\u6700\u4f4e\u00a5537(06-23 \u5468\u4e8c)", body)
        self.assertIn("\u7701\u7ea6\u00a599/\u5355\u7a0b", body)
        self.assertIn("\u5f80\u8fd4\u603b\u4ef7\u7ea6\u00a52,760", body)
        self.assertNotIn("\u5f53\u524d\u5f80\u8fd4\u00a52,760", body)
        self.assertNotIn("\u4f60\u9009\u768406-26\u504f\u8d35", body)

    def test_pushplus_is_slim_and_uses_budget_action_panel(self):
        payload = self._over_budget_payload()
        payload["plan_status_change"] = {"msg": "\u4e0a\u6b21\u65b9\u6848\u6da8\u4ef7"}
        payload["recommendation_basis"]["plain_language"] = "\u8fd9\u662f\u5f88\u957f\u7684\u6392\u5e8f\u4f9d\u636e"

        text = render_pushplus(payload)

        self.assertIn("\u9884\u7b97\u5dee\u8ddd", text)
        self.assertIn("\u4f60\u53ef\u4ee5", text)
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
                    "outbound": {"flight_combo": "MU5107", "departure_airport": "SHA", "departure_time": "11:00", "arrival_airport": "PEK", "arrival_time": "13:15"},
                    "return": {"flight_combo": "CA1507", "departure_airport": "PEK", "departure_time": "07:30", "arrival_airport": "SHA", "arrival_time": "10:00"},
                    "total_price": 2386,
                    "reason": "\u8fd4\u7a0b07:30\u51fa\u53d1,\u4f60\u5f53\u592917:00\u624d\u7ed3\u675f\u4f1a\u8bae,\u65f6\u95f4\u4e0d\u7b26",
                },
                {
                    "scope": "roundtrip",
                    "all_over_budget_reference": True,
                    "outbound": {"flight_combo": "MU5107", "departure_airport": "SHA", "departure_time": "11:00", "arrival_airport": "PEK", "arrival_time": "13:15"},
                    "return": {"flight_combo": "MU5102", "departure_airport": "PEK", "departure_time": "08:00", "arrival_airport": "SHA", "arrival_time": "10:20"},
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
        self.assertIn("MU5102", body)
        self.assertNotIn("\u5df2\u6392\u9664\u7684\u66f4\u4f4e\u4ef7", body)


if __name__ == "__main__":
    unittest.main()
