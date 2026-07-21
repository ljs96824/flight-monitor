import sys
import types
import unittest


sys.modules.setdefault("httpx", types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None))

from notifier import (
    _apply_passenger_friendly_to_plans,
    _apply_passenger_pricing_to_plans,
    _budget_reason_line,
    _payload_price_policy_decision,
    _plan_feasibility_line,
    render_detail_html,
    render_email,
    render_pushplus,
)


def _flight(number, departure, arrival, price):
    return {
        "flight_no": number,
        "flight_combo": number,
        "price": price,
        "stops": 0,
        "departure_airport": "PVG" if number == "MU225" else "KIX",
        "arrival_airport": "KIX" if number == "MU225" else "PVG",
        "departure_time": departure,
        "arrival_time": arrival,
        "fare_rules": {
            "baggage": {"included": True, "checked_kg": 20},
            "refund": {"level": "中"},
        },
    }


class PushTextSweepTest(unittest.TestCase):
    def test_per_person_budget_policy_never_calls_single_person_price_total(self):
        decision = _payload_price_policy_decision(
            9230,
            9230,
            9000,
            8000,
            8500,
            price_scope="per_person_roundtrip",
        )

        self.assertIn("单人参考价(成人口径)约¥9,230(单人往返)", decision["conclusion"])
        self.assertNotIn("人均预估实付", decision["conclusion"])
        self.assertNotIn("预估实付总价¥9,230", decision["conclusion"])

    def test_budget_reason_labels_single_person_roundtrip_scope(self):
        line = _budget_reason_line(
            {
                "is_roundtrip": True,
                "budget_compare_price": 9230,
                "budget_compare_scope": "per_person_roundtrip",
                "max_price": 8500,
            },
            "fallback",
        )

        self.assertIn("往返搜索参考价¥9,230(单人往返)", line)
        self.assertIn("最高可接受价¥8,500(单人往返)", line)

    def test_roundtrip_all_passenger_total_uses_one_canonical_rounding(self):
        plan = {
            "is_roundtrip": True,
            "outbound_price": 4001,
            "return_price": 5229,
            "estimated_price": 9230,
        }

        _apply_passenger_pricing_to_plans(
            [plan],
            {"adult": 2, "child": 1, "elderly": 2, "infant": 0},
            "international",
        )

        self.assertEqual(plan["passenger_pricing"]["factor"], 4.75)
        self.assertEqual(plan["price"], 43843)
        self.assertEqual(plan["roundtrip_price"], 43843)
        self.assertEqual(plan["estimated_price"], 43843)
        self.assertEqual(plan["price_tiers"]["total_roundtrip_ref"], 43843)
        self.assertEqual(plan["price_tiers"]["total_estimated"], 43843)

    def test_non_meeting_feasibility_uses_neutral_time_wording(self):
        line = _plan_feasibility_line(
            {
                "feasibility": {
                    "outbound": {
                        "level": "可行",
                        "margin_min": 90,
                        "transport_min": 40,
                        "transport_margin_min": 15,
                        "buffer_label": "值机安检缓冲",
                        "departure_buffer_min": 90,
                        "safety_min": 25,
                    }
                }
            },
            meeting_context=False,
        )

        self.assertIn("时间余量约1小时30分钟", line)
        self.assertNotIn("会议", line)

    def test_child_and_elderly_friendly_tags_collapse_to_one_semantic_tag(self):
        plan = {
            "is_roundtrip": True,
            "tags": "亲子友好 | 老人友好 | 亲子/老人友好 | 价格较低",
            "outbound_flight": _flight("MU225", "09:00", "11:00", 4001),
            "return_flight": _flight("JL891", "18:00", "20:00", 5229),
        }

        result = _apply_passenger_friendly_to_plans(
            [plan],
            {"has_child": True, "has_elderly": True},
        )[0]

        self.assertIn("亲子·老人友好", result["tags"])
        self.assertNotIn("亲子友好", result["tags"])
        self.assertNotIn("亲子/老人友好", result["tags"])
        self.assertEqual(result["tags"].count("老人友好"), 1)

    def test_all_renderers_keep_price_scope_rounding_and_non_meeting_copy_consistent(self):
        plan = {
            "label": "方案A",
            "tier": "首选推荐",
            "is_roundtrip": True,
            "price": 43843,
            "roundtrip_price": 43843,
            "estimated_price": 43843,
            "outbound_price": 4001,
            "return_price": 5229,
            "purchase_mode": "两个单程拼接",
            "outbound_flight": _flight("MU225", "2026-10-01 09:00", "2026-10-01 11:00", 4001),
            "return_flight": _flight("JL891", "2026-10-06 18:00", "2026-10-06 20:00", 5229),
            "passenger_pricing": {
                "applies": True,
                "factor": 4.75,
                "passengers": {"adult": 2, "child": 1, "elderly": 2, "infant": 0},
                "passenger_label": "2成人+1儿童+2老人",
                "single_adult_price": 9230,
                "total_price": 43843,
            },
            "price_tiers": {
                "unit_roundtrip": 9230,
                "total_roundtrip_ref": 43843,
                "total_estimated": 43843,
                "per_person_estimated": 8769,
                "passenger_count": 5,
                "passenger_label": "2成人+1儿童+2老人",
                "passengers": {"adult": 2, "child": 1, "elderly": 2, "infant": 0},
                "route_type": "international",
            },
            "feasibility": {
                "outbound": {
                    "level": "可行",
                    "margin_min": 90,
                    "transport_min": 40,
                    "transport_margin_min": 15,
                    "buffer_label": "值机安检缓冲",
                    "departure_buffer_min": 150,
                    "safety_min": 25,
                }
            },
            "tags": "亲子·老人友好 | 白天直飞",
            "links": {},
        }
        payload = {
            "push_type": "价格提醒",
            "route": "上海 → 大阪",
            "route_type": "international",
            "is_roundtrip": True,
            "recommendation": "单人参考价(成人口径)约¥9,230(单人往返)已超过预算，建议保持监控本条航线",
            "price_policy_reason": "单人往返参考价超过预算",
            "display_price": 43843,
            "current_price": 43843,
            "transaction_price": 43843,
            "budget_compare_price": 9230,
            "budget_compare_scope": "per_person_roundtrip",
            "max_price": 8500,
            "price_tiers": plan["price_tiers"],
            "passenger_pricing": plan["passenger_pricing"],
            "recommended_plans": [plan],
            "trigger_reason": ["当前价格超过预算"],
            "action_range": {"ranges": []},
            "price_history": [],
            "detail_url": "https://example.com/detail",
            "form_url": "https://example.com/form",
            "feedback_url": "https://example.com/feedback",
            "collected_at": "2026-07-10 09:30",
        }

        _, email_body = render_email(payload)
        outputs = [render_pushplus(payload), email_body, render_detail_html(payload)]

        for output in outputs:
            self.assertNotIn("当前预估实付总价¥9,230", output)
            self.assertNotIn("距会议开始", output)
            self.assertNotIn("¥43,842", output)
        self.assertIn("往返搜索参考价¥9,230(单人往返)", outputs[0])
        self.assertIn("¥43,843", email_body)
        self.assertIn("单人参考价(成人口径)约¥9,230(单人往返)", email_body)
        self.assertIn("人均摊薄(全员÷5,含儿童折扣):约¥8,769", email_body)
        self.assertNotIn("人均预估实付", email_body)


if __name__ == "__main__":
    unittest.main()
