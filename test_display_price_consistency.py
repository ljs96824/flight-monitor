import copy
import unittest
from unittest.mock import patch

from analyzer import build_passenger_roundtrip_pricing
from notifier import (
    _apply_departure_feasibility_to_plans,
    _apply_passenger_pricing_to_plans,
    _log_recommended_total_consistency,
    _render_payload_plan_cards,
    render_email,
)
from price_estimator import build_passenger_price_breakdown


PASSENGERS = {"adult": 2, "child": 1, "elderly": 2, "infant": 0}


def _flight(flight_no, price, dep_airport, arr_airport, dep_time, arr_time):
    return {
        "flight_no": flight_no,
        "flight_combo": flight_no,
        "price": price,
        "departure_airport": dep_airport,
        "arrival_airport": arr_airport,
        "dep_airport": dep_airport,
        "arr_airport": arr_airport,
        "departure_time": dep_time,
        "arrival_time": arr_time,
        "dep_time": dep_time,
        "arr_time": arr_time,
        "stops": 0,
    }


def _plan():
    plan = {
        "label": "方案A",
        "variant": "首选推荐:直飞省心",
        "tier": "首选推荐",
        "route_type": "international",
        "is_roundtrip": True,
        "outbound_price": 4923,
        "return_price": 4126,
        "price": 42983,
        "roundtrip_price": 42983,
        "estimated_price": 9049,
        "purchase_mode": "两个单程拼接",
        "outbound_flight": _flight("CA857", 4923, "PVG", "KIX", "11:50", "15:00"),
        "return_flight": _flight("MU730", 4126, "KIX", "PVG", "09:30", "11:10"),
        "tags": "超预算 | 亲子·老人友好",
        "feasibility": {
            "outbound": {"level": "不可行", "need_set_off": "07:15", "short_min": 105},
            "return": {"level": "可行", "margin_min": 810},
        },
        "feasibility_rank": 2,
        "links": {},
    }
    return _apply_passenger_pricing_to_plans([plan], PASSENGERS, "international")[0]


class DisplayPriceTreeTest(unittest.TestCase):
    def test_recommended_consistency_allows_two_half_up_leg_drifts(self):
        plan = {
            "route_type": "international",
            "is_roundtrip": True,
            "outbound_price": 4818,
            "return_price": 5082,
            "passenger_pricing": {
                "passengers": {"adult": 1, "child": 1, "elderly": 0, "infant": 0},
            },
        }

        with patch("notifier.safe_log") as log:
            _log_recommended_total_consistency(plan)

        message = log.call_args.args[0]
        self.assertIn("面板=17326", message)
        self.assertIn("原始浮点=17325.0", message)
        self.assertIn("漂移=+1.0", message)
        self.assertIn("分项数=4", message)
        self.assertIn("允差=2.0", message)
        self.assertIn("一致=True", message)

    def test_shanghai_osaka_consistency_uses_passenger_leg_item_count(self):
        plan = {
            "route_type": "international",
            "is_roundtrip": True,
            "outbound_price": 4923,
            "return_price": 4126,
            "passenger_pricing": {"passengers": PASSENGERS},
        }

        with patch("notifier.safe_log") as log:
            _log_recommended_total_consistency(plan)

        message = log.call_args.args[0]
        self.assertIn("面板=42983", message)
        self.assertIn("原始浮点=42982.75", message)
        self.assertIn("漂移=+0.25", message)
        self.assertIn("分项数=10", message)
        self.assertIn("允差=5.0", message)
        self.assertIn("一致=True", message)

    def test_recommended_consistency_reports_drift_beyond_bound(self):
        invalid_tree = {
            "total": 103,
            "raw_total": 100.0,
            "outbound": {"parts": [{"count": 1}, {"count": 1}]},
            "return": {"parts": [{"count": 1}, {"count": 1}]},
        }

        with (
            patch("notifier._display_price_tree_for_item", return_value=invalid_tree),
            patch("notifier.safe_log") as log,
        ):
            _log_recommended_total_consistency({"label": "反例"})

        message = log.call_args.args[0]
        self.assertIn("漂移=+3.0", message)
        self.assertIn("分项数=4", message)
        self.assertIn("允差=2.0", message)
        self.assertIn("一致=False", message)

    def test_child_half_boundary_uses_round_half_up(self):
        breakdown = build_passenger_price_breakdown(
            4126,
            PASSENGERS,
            "economy",
            "international",
        )

        child = next(item for item in breakdown["parts"] if item["type"] == "child")
        self.assertEqual(child["unit_price"], 3095)
        self.assertEqual(breakdown["total"], 19599)
        self.assertEqual(sum(item["total"] for item in breakdown["parts"]), breakdown["total"])

    def test_display_tree_matches_all_expected_card_amounts(self):
        from price_estimator import build_display_prices

        recommended = build_display_prices(4923, 4126, PASSENGERS, "international")
        self.assertEqual(recommended["outbound"]["parts_by_type"]["child"]["unit_price"], 3692)
        self.assertEqual(recommended["return"]["parts_by_type"]["child"]["unit_price"], 3095)
        self.assertEqual(recommended["outbound"]["total"], 23384)
        self.assertEqual(recommended["return"]["total"], 19599)
        self.assertEqual(recommended["total"], 42983)
        self.assertEqual(recommended["per_person_blended"], 8597)

        expected = [
            (1770, 3167, 23451, 19532),
            (1770, 3217, 23689, 19294),
            (2969, 3167, 29146, 13837),
        ]
        for outbound, ret, total, difference in expected:
            with self.subTest(outbound=outbound, return_price=ret):
                tree = build_display_prices(outbound, ret, PASSENGERS, "international")
                self.assertEqual(tree["total"], total)
                self.assertEqual(recommended["total"] - tree["total"], difference)
                self.assertEqual(
                    tree["outbound"]["total"] + tree["return"]["total"],
                    tree["total"],
                )

    def test_calendar_display_values_keep_existing_expected_amounts(self):
        from price_estimator import build_display_prices

        low = build_display_prices(4409, None, PASSENGERS, "international")["total"]
        selected = build_display_prices(6079, None, PASSENGERS, "international")["total"]

        self.assertEqual(low, 20943)
        self.assertEqual(selected, 28875)
        self.assertEqual(selected - low, 7932)

    def test_analyzer_price_keeps_unrounded_float_for_decisions(self):
        pricing = build_passenger_roundtrip_pricing(
            4923,
            4126,
            PASSENGERS,
            "international",
        )

        self.assertEqual(pricing["total_price"], 42982.75)


class PlanCardOwnershipTest(unittest.TestCase):
    def test_infeasible_plan_gets_adjustment_tag_in_data_layer(self):
        plan = {
            "label": "方案A",
            "tags": "超预算",
            "is_roundtrip": True,
            "outbound_flight": _flight("CA857", 4923, "PVG", "KIX", "11:50", "15:00"),
            "return_flight": _flight("MU730", 4126, "KIX", "PVG", "09:30", "11:10"),
        }
        result = _apply_departure_feasibility_to_plans(
            [plan],
            {
                "outbound_set_off": "09:00",
                "user_transport_min": 50,
                "transport_margin_mode": "standard",
            },
            "international",
            {"depart_date": "2026-10-01", "return_date": "2026-10-06"},
        )

        self.assertIn("需调整动身时间", result[0]["tags"])

    def test_compact_render_never_contains_full_card_title(self):
        plan = _plan()
        compact_html = _render_payload_plan_cards(
            {"route_type": "international"},
            [plan],
            plan,
            compact=True,
        )

        self.assertNotIn("方案A ｜ 首选推荐", compact_html)
        self.assertIn("方案A", compact_html)

    def test_compact_render_fails_fast_if_full_title_leaks(self):
        plan = _plan()
        with patch(
            "notifier._compact_adjustment_reference",
            return_value="方案A ｜ 首选推荐",
        ):
            with self.assertRaisesRegex(AssertionError, "compact 输出含完整卡标题"):
                _render_payload_plan_cards(
                    {"route_type": "international"},
                    [plan],
                    plan,
                    compact=True,
                )

    def test_same_combo_renders_as_full_card_only_once_across_sections(self):
        plan = _plan()
        payload = {
            "push_type": "超预算",
            "route": "上海 → 大阪",
            "route_type": "international",
            "recommendation": "继续观察",
            "display_price": 42983,
            "transaction_price": 42983,
            "recommended_plans": [plan],
            "adjustment_required_plans": [copy.deepcopy(plan)],
            "trigger_reason": [],
            "price_history": [],
            "action_range": {"ranges": []},
        }

        _subject, email_html = render_email(payload)

        self.assertEqual(email_html.count("方案A ｜ 首选推荐"), 1)
        self.assertIn("方案A(见上)", email_html)


if __name__ == "__main__":
    unittest.main()
