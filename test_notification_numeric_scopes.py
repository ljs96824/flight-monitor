import sys
import types
import unittest


sys.modules.setdefault("httpx", types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None))

from analyzer import build_excluded_roundtrip_combos
from notifier import (
    _email_channel_picker,
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

        self.assertIn("\u4f60\u9009\u768406-18\u504f\u8d35", body)
        self.assertIn("\u00a52,560", body)
        self.assertIn("\u5355\u7a0b\u4f4e\u81f3\u00a5520", body)
        self.assertIn("\u5468\u516d/\u5468\u65e5", body)

    def test_pushplus_is_slim_and_uses_budget_action_panel(self):
        payload = self._over_budget_payload()
        payload["plan_status_change"] = {"msg": "\u4e0a\u6b21\u65b9\u6848\u6da8\u4ef7"}
        payload["recommendation_basis"]["plain_language"] = "\u8fd9\u662f\u5f88\u957f\u7684\u6392\u5e8f\u4f9d\u636e"

        text = render_pushplus(payload)

        self.assertIn("\u9884\u7b97\u5dee\u8ddd", text)
        self.assertIn("\u4f60\u53ef\u4ee5", text)
        self.assertIn("\u4f60\u9009\u768406-18\u504f\u8d35", text)
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


if __name__ == "__main__":
    unittest.main()
