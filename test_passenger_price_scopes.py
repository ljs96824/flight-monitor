import sys
import types
import unittest


class _DummyFlask:
    def __init__(self, *args, **kwargs):
        pass

    def route(self, *args, **kwargs):
        return lambda func: func

    def get(self, *args, **kwargs):
        return lambda func: func

    def post(self, *args, **kwargs):
        return lambda func: func

    def run(self, *args, **kwargs):
        return None


sys.modules.setdefault("httpx", types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None))
sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
sys.modules.setdefault(
    "flask",
    types.SimpleNamespace(
        Flask=_DummyFlask,
        jsonify=lambda *a, **k: {},
        redirect=lambda *a, **k: None,
        render_template_string=lambda *a, **k: "",
        request=types.SimpleNamespace(form={}),
        url_for=lambda *a, **k: "",
    ),
)

from price_estimator import (
    build_passenger_price_breakdown,
    build_price_tiers,
    calc_total_for_passengers,
    calc_total_price_for_passengers,
)
from web_form import build_subscription
from notifier import (
    _apply_passenger_pricing_to_plans,
    _email_price_calendar_body,
    _render_payload_plan_card,
    build_notification_payload,
)


class _Form(dict):
    def getlist(self, key):
        value = self.get(key, [])
        if isinstance(value, list):
            return value
        if value in (None, ""):
            return []
        return [value]


def _base_form(**overrides):
    data = {
        "origin_select": "SHA",
        "destination": "PEK",
        "round_trip": "true",
        "depart_date": "2026-06-26",
        "return_date": "2026-06-30",
        "date_flexibility": "0",
        "price_strategy": "explicit",
        "max_budget": "7000",
        "target_price": "6000",
        "transfer_policy": "reasonable",
        "baggage": "required",
        "primary_goal": "buy_timing",
        "travel_scenario": ["business"],
        "companions": "solo",
        "notification_method": "pushplus",
        "notification_frequency": "important_only",
    }
    data.update(overrides)
    return _Form(data)


class PassengerPriceScopesTest(unittest.TestCase):
    def test_domestic_passenger_total_uses_child_and_infant_factors(self):
        passengers = {"adult": 2, "child": 1, "elderly": 0, "infant": 1}

        total = calc_total_price_for_passengers(1410, passengers, "economy", "domestic")
        breakdown = build_passenger_price_breakdown(1410, passengers, "economy", "domestic")

        self.assertEqual(total, 3666)
        self.assertEqual(breakdown["factor"], 2.6)
        self.assertIn("儿童票按国内常规5折估算", breakdown["note"])
        self.assertIn("婴儿票按国内常规1折估算", breakdown["note"])

    def test_precise_form_saves_budget_scope(self):
        sub = build_subscription(
            _base_form(
                monitor_mode="precise",
                budget_scope="per_person",
                adult_count="2",
                child_count="1",
                elderly_count="0",
                infant_count="0",
            )
        )

        self.assertEqual(sub["constraints"]["budget_scope"], "per_person")
        self.assertEqual(sub["hard_constraints"]["budget_scope"], "per_person")
        self.assertEqual(sub["soft_preferences"]["budget_scope"], "per_person")

    def test_roundtrip_plan_card_shows_all_passenger_total_and_unit_reference(self):
        plan = {
            "label": "方案A",
            "tier": "首选推荐",
            "is_roundtrip": True,
            "price": 2760,
            "estimated_price": 2760,
            "outbound_price": 1410,
            "return_price": 1350,
            "purchase_mode": "两个单程拼接",
            "outbound_line": "去程 MU5099 SHA07:00→PEK09:15",
            "return_line": "返程 CA1589 PEK20:30→SHA22:40",
            "links": {},
        }
        _apply_passenger_pricing_to_plans(
            [plan],
            {"adult": 2, "child": 1, "elderly": 0, "infant": 0},
            "domestic",
        )

        html = _render_payload_plan_card(plan, compact=True)

        self.assertEqual(plan["price"], 6900)
        self.assertIn("往返总价(2成人+1儿童)", html)
        self.assertIn("¥6,900", html)
        self.assertIn("去程全员¥3,525", html)
        self.assertIn("成人¥1,410×2", html)
        self.assertIn("儿童¥705", html)
        self.assertIn("返程全员¥3,375", html)
        self.assertIn("单人往返参考", html)
        self.assertIn("约¥2,760/成人", html)
        self.assertIn("儿童票按国内常规5折估算", html)

    def test_roundtrip_calendar_mentions_single_person_and_all_passenger_conversion(self):
        payload = {
            "is_roundtrip": True,
            "display_price": 6900,
            "passenger_pricing": {
                "applies": True,
                "factor": 2.5,
                "passenger_label": "2成人+1儿童",
            },
            "price_calendar": {
                "scope": "roundtrip",
                "return_date": "2026-06-30",
                "return_min_price": 557,
                "rows": [
                    {
                        "date": "2026-06-23",
                        "weekday": "周二",
                        "outbound_min_price": 547,
                        "return_min_price": 557,
                        "min_price": 1104,
                        "lowest": True,
                    },
                    {
                        "date": "2026-06-26",
                        "weekday": "周五",
                        "outbound_min_price": 679,
                        "return_min_price": 557,
                        "min_price": 1236,
                        "selected": True,
                    },
                ],
            },
        }

        body = _email_price_calendar_body(payload)

        self.assertIn("往返参考价(单人", body)
        self.assertIn("2成人+1儿童", body)
        self.assertIn("全员约¥2,760", body)
        self.assertIn("下方为单人往返参考价", body)

    def test_international_child_factor_uses_75_percent_estimate(self):
        passengers = {"adult": 2, "child": 1, "elderly": 0, "infant": 0}

        total = calc_total_for_passengers(1000, passengers, "international")
        breakdown = build_passenger_price_breakdown(1000, passengers, "economy", "international")

        self.assertEqual(total, 2750)
        self.assertEqual(breakdown["factor"], 2.75)

    def test_price_tiers_capture_five_price_scopes(self):
        tiers = build_price_tiers(
            1410,
            1350,
            {"adult": 2, "child": 1, "elderly": 0, "infant": 0},
            "domestic",
            purchase_type="two_oneways",
            estimated_outbound=1510,
            estimated_return=1450,
        )

        self.assertEqual(tiers["unit_oneway"]["outbound"], 1410)
        self.assertEqual(tiers["unit_roundtrip"], 2760)
        self.assertEqual(tiers["total_roundtrip_ref"], 6900)
        self.assertEqual(tiers["total_estimated"], 7400)
        self.assertEqual(tiers["per_person_estimated"], 2467)
        self.assertEqual(tiers["passenger_count"], 3)
        self.assertTrue(tiers["is_roundtrip"])
        self.assertEqual(tiers["purchase_type"], "two_oneways")

    def test_roundtrip_analysis_budget_uses_all_passenger_total_scope(self):
        from analyzer import analyze_round_trip

        outbound = {
            "flight_no": "MU5099",
            "price": 1410,
            "departure_airport": "SHA",
            "arrival_airport": "PEK",
            "departure_time": "07:00",
            "arrival_time": "09:15",
        }
        ret = {
            "flight_no": "CA1589",
            "price": 1350,
            "departure_airport": "PEK",
            "arrival_airport": "SHA",
            "departure_time": "20:30",
            "arrival_time": "22:40",
        }
        preferences = {
            "passengers": {"adult": 2, "child": 1, "elderly": 0, "infant": 0},
            "budget_scope": "per_person",
            "route_type": "domestic",
        }

        result = analyze_round_trip(
            {
                "economy_recommendations": [outbound],
                "all_flights": [outbound],
                "user_preferences": preferences,
                "route_type": "domestic",
            },
            {
                "economy_recommendations": [ret],
                "all_flights": [ret],
                "user_preferences": preferences,
                "route_type": "domestic",
            },
            target_price=1500,
            max_budget=1600,
        )

        self.assertEqual(result["passenger_total_min"], 6900)
        self.assertEqual(result["budget_price"], 6900)
        self.assertEqual(result["budget_limits"]["max_budget_total"], 4800)
        self.assertEqual(result["budget_limits"]["ideal_price_total"], 4500)
        self.assertEqual(result["decision_summary"]["price_judgment"], "\u8d85\u51fa\u9884\u7b97")

    def test_payload_uses_total_estimated_tier_for_budget_and_verify_price(self):
        analysis_result = {
            "round_trip_analysis": {
                "top_combinations": [
                    {
                        "outbound": {
                            "flight_no": "MU5099",
                            "price": 1410,
                            "departure_airport": "SHA",
                            "arrival_airport": "PEK",
                        },
                        "return": {
                            "flight_no": "CA1589",
                            "price": 1350,
                            "departure_airport": "PEK",
                            "arrival_airport": "SHA",
                        },
                        "outbound_price": 1410,
                        "return_price": 1350,
                        "total_price": 2760,
                        "transaction_total": 2960,
                    }
                ]
            },
            "decision": {"conclusion": "can_watch", "confidence": "medium"},
        }
        route_info = {
            "round_trip": True,
            "depart_date": "2026-06-26",
            "return_date": "2026-06-30",
            "target_price": 7200,
            "max_budget": 7500,
            "route_type": "domestic",
        }
        subscription = {
            "basic": {"route_type": "domestic", "passenger_count": 3},
            "preferences": {"passengers": {"adult": 2, "child": 1, "elderly": 0, "infant": 0}},
            "constraints": {"budget_scope": "total"},
        }

        payload = build_notification_payload(
            analysis_result,
            route_info=route_info,
            subscription=subscription,
        )

        self.assertEqual(payload["price_tiers"]["total_roundtrip_ref"], 6900)
        self.assertEqual(payload["price_tiers"]["total_estimated"], 7400)
        self.assertEqual(payload["price_tiers"]["per_person_estimated"], 2467)
        self.assertEqual(payload["display_price"], 6900)
        self.assertEqual(payload["transaction_price"], 7400)
        self.assertEqual(payload["verify_price"], 7245)



    def test_no_primary_calendar_uses_quick_mode_passenger_count(self):
        calendar = {
            "scope": "roundtrip",
            "return_date": "2026-06-30",
            "return_min_price": 557,
            "rows": [
                {
                    "date": "2026-06-23",
                    "weekday": "\u5468\u4e8c",
                    "outbound_min_price": 547,
                    "return_min_price": 557,
                    "roundtrip_ref_price": 1104,
                    "min_price": 1104,
                    "lowest": True,
                }
            ],
        }
        payload = build_notification_payload(
            {"price_calendar": calendar, "round_trip_analysis": {"top_combinations": []}},
            route_info={
                "origin": "SHA",
                "destination": "PEK",
                "depart_date": "2026-06-23",
                "return_date": "2026-06-30",
                "price_calendar": calendar,
            },
            subscription={"basic": {"passenger_count": 3}, "constraints": {"route_type": "domestic"}},
        )

        body = _email_price_calendar_body(payload)

        self.assertEqual(payload["passenger_pricing"]["passengers"]["adult"], 3)
        self.assertIn("3\u6210\u4eba", body)
        self.assertIn("\u5168\u5458\u7ea6\u00a53,312", body)

if __name__ == "__main__":
    unittest.main()
