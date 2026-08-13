import sys
import types
import unittest
import flask  # noqa: F401
import io
import tempfile
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch


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
    passenger_price_factor,
    reset_passenger_factor_log_cache,
)
from analyzer import _roundtrip_exclusion_basis, passenger_budget_limits
from web_form import build_subscription
from notifier import (
    _apply_passenger_pricing_to_plans,
    _email_channel_picker,
    _email_price_calendar_body,
    _plan_roundtrip_price_text,
    _render_payload_plan_card,
    build_notification_payload,
)
import storage


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
                max_budget_scope="per_person",
                target_price_scope="all",
                adult_count="2",
                child_count="1",
                elderly_count="0",
                infant_count="0",
            )
        )

        self.assertEqual(sub["constraints"]["budget_scope"], "per_person")
        self.assertEqual(sub["constraints"]["max_budget_scope"], "per_person")
        self.assertEqual(sub["constraints"]["target_price_scope"], "all")
        self.assertEqual(sub["hard_constraints"]["budget_scope"], "per_person")
        self.assertEqual(sub["hard_constraints"]["max_budget_scope"], "per_person")
        self.assertEqual(sub["hard_constraints"]["target_price_scope"], "all")
        self.assertEqual(sub["soft_preferences"]["budget_scope"], "per_person")
        self.assertEqual(sub["soft_preferences"]["max_budget_scope"], "per_person")
        self.assertEqual(sub["soft_preferences"]["target_price_scope"], "all")

    def test_quick_form_defaults_budget_scopes_to_per_person(self):
        sub = build_subscription(
            _base_form(
                monitor_mode="quick",
                max_budget="1700",
                target_price="1200",
                adult_count="3",
                passenger_count="3",
            )
        )

        self.assertEqual(sub["constraints"]["budget_scope"], "per_person")
        self.assertEqual(sub["constraints"]["max_budget_scope"], "per_person")
        self.assertEqual(sub["constraints"]["target_price_scope"], "per_person")
        self.assertEqual(sub["hard_constraints"]["budget_scope"], "per_person")
        self.assertEqual(sub["soft_preferences"]["target_price_scope"], "per_person")

    def test_legacy_budget_scope_total_does_not_override_per_person_default(self):
        sub = build_subscription(
            _base_form(
                monitor_mode="quick",
                origin_select="上海",
                destination="大阪",
                route_type="international",
                max_budget="8000",
                target_price="6000",
                budget_scope="total",
                passenger_count="5",
                adult_count="2",
                child_count="1",
                elderly_count="2",
                infant_count="0",
            )
        )

        for container_name in (
            "constraints",
            "hard_constraints",
            "soft_preferences",
        ):
            container = sub[container_name]
            self.assertEqual(container["budget_scope"], "per_person")
            self.assertEqual(container["max_budget_scope"], "per_person")

    def test_explicit_all_budget_scope_remains_all(self):
        sub = build_subscription(
            _base_form(
                monitor_mode="quick",
                max_budget_scope="all",
                target_price_scope="all",
                budget_scope="per_person",
            )
        )

        for container_name in (
            "constraints",
            "hard_constraints",
            "soft_preferences",
        ):
            container = sub[container_name]
            self.assertEqual(container["budget_scope"], "all")
            self.assertEqual(container["max_budget_scope"], "all")
            self.assertEqual(container["target_price_scope"], "all")

    def test_legacy_shanghai_osaka_budget_scope_stays_consistent_end_to_end(self):
        passengers = {"adult": 2, "child": 1, "elderly": 2, "infant": 0}
        sub = build_subscription(
            _base_form(
                monitor_mode="precise",
                origin_select="上海",
                destination="大阪",
                route_type="international",
                max_budget="8000",
                target_price="6000",
                budget_scope="total",
                adult_count="2",
                child_count="1",
                elderly_count="2",
                infant_count="0",
            )
        )
        constraints = sub["hard_constraints"]
        limits = passenger_budget_limits(
            max_budget=constraints["max_budget"],
            ideal_price=constraints["target_price"],
            budget_scope=constraints["budget_scope"],
            passengers=passengers,
            route_type="international",
            round_trip=True,
            max_budget_scope=constraints["max_budget_scope"],
            target_price_scope=constraints["target_price_scope"],
        )
        basis = _roundtrip_exclusion_basis(
            constraints,
            limits["max_budget_total"],
            passengers,
            "international",
        )

        self.assertEqual(limits["max_budget_compare"], 8000)
        self.assertEqual(limits["max_budget_compare_scope"], "per_person_roundtrip")
        self.assertEqual(limits["max_budget_total"], 38000)
        self.assertIn(
            "最高可接受价¥38,000(全员,=单人¥8,000×4.75)",
            basis,
        )

        analysis_result = {
            "round_trip_analysis": {
                "top_combinations": [
                    {
                        "outbound": {"flight_no": "CA857", "price": 7200},
                        "return": {"flight_no": "MU730", "price": 7197},
                        "outbound_price": 7200,
                        "return_price": 7197,
                        "total_price": 14397,
                    }
                ],
                "total_min": 14397,
            },
            "decision": {"conclusion": "can_watch", "confidence": "medium"},
        }
        route_info = {
            "round_trip": True,
            "origin": "PVG",
            "destination": "KIX",
            "depart_date": "2026-10-01",
            "return_date": "2026-10-06",
            "target_price": 6000,
            "max_budget": 8000,
            "route_type": "international",
        }
        output = io.StringIO()
        with patch("notifier.get_last_push_price", return_value=None), patch(
            "notifier.get_last_push_snapshot", return_value=None
        ), patch("notifier.track_plan_status", return_value=None), redirect_stdout(output):
            payload = build_notification_payload(
                analysis_result,
                route_info=route_info,
                subscription=sub,
            )

        self.assertEqual(payload["budget_compare_scope"], "per_person_roundtrip")
        self.assertEqual(payload["max_price"], 8000)
        self.assertEqual(payload["purchase_budget_decision"]["max_budget"], 8000)
        self.assertIn("¥8,000(单人往返)", payload["buy_condition"])
        self.assertIn(
            "[购买建议] unit_roundtrip=14397 max_budget=8000",
            output.getvalue(),
        )

    def test_explicit_all_shanghai_osaka_budget_uses_raw_total(self):
        passengers = {"adult": 2, "child": 1, "elderly": 2, "infant": 0}
        sub = build_subscription(
            _base_form(
                monitor_mode="precise",
                origin_select="上海",
                destination="大阪",
                route_type="international",
                max_budget="8000",
                target_price="6000",
                max_budget_scope="all",
                target_price_scope="all",
                adult_count="2",
                child_count="1",
                elderly_count="2",
                infant_count="0",
            )
        )
        constraints = sub["hard_constraints"]
        limits = passenger_budget_limits(
            max_budget=constraints["max_budget"],
            ideal_price=constraints["target_price"],
            budget_scope=constraints["budget_scope"],
            passengers=passengers,
            route_type="international",
            round_trip=True,
            max_budget_scope=constraints["max_budget_scope"],
            target_price_scope=constraints["target_price_scope"],
        )
        basis = _roundtrip_exclusion_basis(
            constraints,
            limits["max_budget_total"],
            passengers,
            "international",
        )

        self.assertEqual(limits["max_budget_compare_scope"], "all_passengers_roundtrip")
        self.assertEqual(limits["max_budget_compare"], 8000)
        self.assertEqual(limits["max_budget_total"], 8000)
        self.assertEqual(limits["max_budget_pp_oneway"] * 2, 1684.22)
        self.assertIn("最高可接受价¥8,000(全员往返)", basis)
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
        self.assertIn("约¥2,760 单人往返/成人", html)
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

    def test_international_factor_survives_fast_verification_rendering(self):
        plan = {
            "label": "方案A",
            "is_roundtrip": True,
            "price": 6503,
            "estimated_price": 6503,
            "outbound_price": 3000,
            "return_price": 3503,
            "purchase_mode": "两个单程拼接",
            "outbound_flight": {"flight_no": "MU515", "price": 3000},
            "return_flight": {"flight_no": "MU516", "price": 3503},
            "links": {
                "outbound": '<a href="https://example.com/out">携程</a>',
                "return": '<a href="https://example.com/ret">携程</a>',
            },
        }
        passengers = {"adult": 2, "child": 1, "elderly": 2, "infant": 0}

        _apply_passenger_pricing_to_plans([plan], passengers, "international")

        self.assertEqual(plan["passenger_pricing"]["factor"], 4.75)
        text = _plan_roundtrip_price_text(plan)
        self.assertIn("4.75", text)
        self.assertNotIn("4.5", text)
        quick_html = _email_channel_picker(plan, context_label="快速验证首选方案A")
        self.assertIn("4.75", quick_html)
        self.assertNotIn("4.5", quick_html)

    def test_passenger_factor_log_prints_once_per_round_and_passenger_mix(self):
        from observations_store import clear_current_round, set_current_round

        passengers = {"adult": 3, "child": 0, "elderly": 0, "infant": 0}
        reset_passenger_factor_log_cache()
        set_current_round("round-a")
        self.addCleanup(clear_current_round)
        self.addCleanup(reset_passenger_factor_log_cache)

        output = io.StringIO()
        with redirect_stdout(output):
            passenger_price_factor(passengers, "domestic")
            passenger_price_factor(passengers, "domestic")

        self.assertEqual(output.getvalue().count("[\u7968\u4ef7\u7cfb\u6570]"), 1)

        output = io.StringIO()
        with redirect_stdout(output):
            set_current_round("round-b")
            passenger_price_factor(passengers, "domestic")

        self.assertEqual(output.getvalue().count("[\u7968\u4ef7\u7cfb\u6570]"), 1)

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
        self.assertEqual(result["budget_limits"]["max_budget_total"], 4000)
        self.assertEqual(result["budget_limits"]["ideal_price_total"], 3750)
        self.assertEqual(result["decision_summary"]["price_judgment"], "\u8d85\u51fa\u9884\u7b97")

    def test_roundtrip_budget_scope_switches_between_per_person_and_all_passengers(self):
        from analyzer import analyze_round_trip

        outbound = {
            "flight_no": "MU5099",
            "price": 699,
            "departure_airport": "SHA",
            "arrival_airport": "PKX",
            "departure_time": "07:00",
            "arrival_time": "09:15",
        }
        ret = {
            "flight_no": "MU5170",
            "price": 699,
            "departure_airport": "PKX",
            "arrival_airport": "SHA",
            "departure_time": "21:00",
            "arrival_time": "23:05",
        }

        def run(scope):
            preferences = {
                "passengers": {"adult": 3, "child": 0, "elderly": 0, "infant": 0},
                "budget_scope": scope,
                "max_budget_scope": scope,
                "target_price_scope": scope,
                "route_type": "domestic",
            }
            return analyze_round_trip(
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
                target_price=1200,
                max_budget=1700,
            )

        per_person = run("per_person")
        all_passengers = run("all")

        self.assertEqual(per_person["budget_limits"]["max_budget_compare"], 1700)
        self.assertEqual(per_person["budget_limits"]["max_budget_compare_scope"], "per_person_roundtrip")
        self.assertEqual(per_person["budget_price_compare"], 1398)
        self.assertIn("\u9884\u7b97\u5185", per_person["decision_summary"]["price_judgment"])

        self.assertEqual(all_passengers["budget_limits"]["max_budget_compare"], 1700)
        self.assertEqual(all_passengers["budget_limits"]["max_budget_compare_scope"], "all_passengers_roundtrip")
        self.assertEqual(all_passengers["budget_price_compare"], 4194)
        self.assertEqual(all_passengers["decision_summary"]["price_judgment"], "\u8d85\u51fa\u9884\u7b97")
    def test_payload_budget_scope_switches_compare_price(self):
        analysis_result = {
            "round_trip_analysis": {
                "top_combinations": [
                    {
                        "outbound": {"flight_no": "MU5099", "price": 699},
                        "return": {"flight_no": "MU5170", "price": 699},
                        "outbound_price": 699,
                        "return_price": 699,
                        "total_price": 1398,
                    }
                ]
            },
            "decision": {"conclusion": "can_watch", "confidence": "medium"},
        }
        route_info = {
            "round_trip": True,
            "depart_date": "2026-06-26",
            "return_date": "2026-06-26",
            "target_price": 1200,
            "max_budget": 1700,
            "route_type": "domestic",
        }

        def payload(scope):
            return build_notification_payload(
                analysis_result,
                route_info=route_info,
                subscription={
                    "basic": {"route_type": "domestic", "passenger_count": 3},
                    "preferences": {"passengers": {"adult": 3, "child": 0, "elderly": 0, "infant": 0}},
                    "constraints": {
                        "budget_scope": scope,
                        "max_budget_scope": scope,
                        "target_price_scope": scope,
                    },
                },
            )

        per_person = payload("per_person")
        all_passengers = payload("all")

        self.assertEqual(per_person["budget_compare_scope"], "per_person_roundtrip")
        self.assertEqual(per_person["budget_compare_price"], 1398)
        self.assertEqual(per_person["max_price"], 1700)
        self.assertFalse(per_person["budget_gap"]["is_over_budget"])

        self.assertEqual(all_passengers["budget_compare_scope"], "all_passengers_roundtrip")
        self.assertEqual(all_passengers["budget_compare_price"], 4194)
        self.assertEqual(all_passengers["max_price"], 1700)
        self.assertTrue(all_passengers["budget_gap"]["is_over_budget"])

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
        depart_date = date.today() + timedelta(days=21)
        return_date = depart_date + timedelta(days=7)
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        calendar = {
            "scope": "roundtrip",
            "return_date": return_date.isoformat(),
            "return_min_price": 557,
            "rows": [
                {
                    "date": depart_date.isoformat(),
                    "weekday": weekday_names[depart_date.weekday()],
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
                "depart_date": depart_date.isoformat(),
                "return_date": return_date.isoformat(),
                "price_calendar": calendar,
            },
            subscription={"basic": {"passenger_count": 3}, "constraints": {"route_type": "domestic"}},
        )

        body = _email_price_calendar_body(payload)

        self.assertEqual(payload["passenger_pricing"]["passengers"]["adult"], 3)
        self.assertIn("3\u6210\u4eba", body)
        self.assertIn("\u5168\u5458\u7ea6\u00a53,312", body)

    def test_calendar_percentile_uses_all_passenger_price_array(self):
        payload = {
            "is_roundtrip": True,
            "display_price": 3708,
            "passenger_pricing": {
                "applies": True,
                "factor": 3,
                "passenger_count": 3,
                "passenger_label": "3\u6210\u4eba",
                "passengers": {"adult": 3, "child": 0, "elderly": 0, "infant": 0},
            },
            "price_calendar": {
                "scope": "roundtrip",
                "return_date": "2026-06-30",
                "rows": [
                    {
                        "date": "2026-06-23",
                        "weekday": "\u5468\u4e8c",
                        "min_price": 1104,
                        "lowest": True,
                    },
                    {
                        "date": "2026-06-26",
                        "weekday": "\u5468\u4e94",
                        "min_price": 1236,
                        "selected": True,
                    },
                    {
                        "date": "2026-06-29",
                        "weekday": "\u5468\u4e00",
                        "min_price": 1300,
                    },
                ],
            },
        }
        output = io.StringIO()

        with redirect_stdout(output):
            _email_price_calendar_body(payload)

        log = output.getvalue()
        self.assertIn("\u4f60\u9009\u65e5\u671f\u4ef7=3708", log)
        self.assertIn("\u5168\u90e8\u4ef7\u683c=[3312, 3708, 3900]", log)
        self.assertIn("\u662f\u5426\u5df2\u00d7\u4eba\u6570=True", log)

    def test_calendar_percentile_respects_pre_multiplied_passenger_rows(self):
        payload = {
            "is_roundtrip": True,
            "display_price": 4437,
            "passenger_pricing": {
                "applies": True,
                "factor": 3,
                "passenger_count": 3,
                "passenger_label": "3\u6210\u4eba",
                "passengers": {"adult": 3, "child": 0, "elderly": 0, "infant": 0},
            },
            "price_calendar": {
                "scope": "passenger_roundtrip",
                "return_date": "2026-06-26",
                "rows": [
                    {
                        "date": "2026-06-23",
                        "weekday": "\u5468\u4e8c",
                        "unit_roundtrip_price": 1195,
                        "min_price": 3585,
                        "scope": "passenger_roundtrip",
                        "passenger_factor": 3,
                        "lowest": True,
                    },
                    {
                        "date": "2026-06-24",
                        "weekday": "\u5468\u4e09",
                        "unit_roundtrip_price": 1218,
                        "min_price": 3654,
                        "scope": "passenger_roundtrip",
                        "passenger_factor": 3,
                    },
                    {
                        "date": "2026-06-27",
                        "weekday": "\u5468\u516d",
                        "unit_roundtrip_price": 1408,
                        "min_price": 4224,
                        "scope": "passenger_roundtrip",
                        "passenger_factor": 3,
                    },
                    {
                        "date": "2026-06-26",
                        "weekday": "\u5468\u4e94",
                        "unit_roundtrip_price": 1479,
                        "min_price": 4437,
                        "scope": "passenger_roundtrip",
                        "passenger_factor": 3,
                        "selected": True,
                    },
                ],
            },
        }
        output = io.StringIO()

        with redirect_stdout(output):
            _email_price_calendar_body(payload)

        log = output.getvalue()
        self.assertIn("\u4f60\u9009\u65e5\u671f\u4ef7=4437", log)
        self.assertIn("\u5168\u90e8\u4ef7\u683c=[3585.0, 3654.0, 4224.0, 4437.0]", log)
        self.assertIn("[\u65e5\u5386\u5bf9\u6bd4] \u6570\u7ec4\u524d3(before\u5355\u4eba)=[1195.0, 1218.0, 1408.0], after=[3585.0, 3654.0, 4224.0], \u662f\u5426\u5df2\u00d7\u4eba\u6570=True", log)
        self.assertNotIn("10755", log)

if __name__ == "__main__":
    unittest.main()

