import sys
import types
import unittest


sys.modules.setdefault("httpx", types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None))

from analyzer import build_excluded_roundtrip_combos
from notifier import (
    _email_channel_picker,
    _email_price_calendar_body,
    _plan_effective_cost_line,
    _plan_feasibility_line,
)


class NotificationNumericScopesTest(unittest.TestCase):
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
