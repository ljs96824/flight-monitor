import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from airlines import (
    EXPECTED_LCC_CARRIER_CODES,
    HYBRID_NOTES,
    LCC_CARRIERS,
    classify_itinerary,
    classify_segment,
    validate_lcc_carriers,
)
from analyzer import (
    _apply_lcc_policy,
    _apply_user_preferences,
    _all_roundtrip_flights_for_same_day,
    _attach_filter_reason_details,
    analyze_all_flights,
    migrate_old_subscription,
)
import main
from method_registry import (
    EXPECTED_REGISTRY_KEYS,
    REGISTRY_VERSIONS,
    method_version,
)
from notifier import (
    _payload_combo_plan,
    _render_payload_plan_card,
)
from sources.juhe_source import JuheSource
from subscription_preflight import evaluate_subscription_preflight
import web_form


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
        "origin_select": "PVG",
        "destination": "KIX",
        "monitor_mode": "precise",
        "round_trip": "true",
        "depart_date": "2026-10-01",
        "return_date": "2026-10-06",
        "transfer_policy": "reasonable",
        "baggage": "required",
        "primary_goal": "buy_timing",
        "travel_scenario": ["personal"],
        "notification_method": "pushplus",
        "notification_frequency": "important_only",
    }
    data.update(overrides)
    return _Form(data)


def _flight(
    combo,
    airline,
    *,
    price=500,
    segments=None,
):
    return {
        "flight_no": combo,
        "flight_combo": combo,
        "airline": airline,
        "airline_summary": airline,
        "airlines": [airline],
        "price": price,
        "stops": max(len(segments or []) - 1, 0),
        "total_duration_min": 120,
        "route_summary": combo,
        "departure_time": "2026-10-01 10:00",
        "arrival_time": "2026-10-01 12:00",
        "segments": segments
        or [
            {
                "flight_no": combo,
                "airline": airline,
                "dep_airport": "PVG",
                "arr_airport": "KIX",
                "dep_time": "2026-10-01 10:00",
                "arr_time": "2026-10-01 12:00",
            }
        ],
    }


class LccRegistryTest(unittest.TestCase):
    def test_registry_codes_are_frozen_and_versioned(self):
        expected = {
            "9C", "AQ", "PN", "8L", "KN",
            "MM", "GK", "IJ", "ZG",
            "7C", "LJ", "BX", "RS", "TW",
            "UO", "IT",
            "TR", "AK", "D7", "FD", "QZ", "Z2", "VJ", "VZ", "5J", "SL",
        }

        self.assertEqual(EXPECTED_LCC_CARRIER_CODES, expected)
        self.assertEqual(set(LCC_CARRIERS), expected)
        self.assertTrue(validate_lcc_carriers())
        self.assertEqual(
            EXPECTED_REGISTRY_KEYS,
            {"lcc_registry", "holiday_calendar"},
        )
        self.assertEqual(REGISTRY_VERSIONS["lcc_registry"], "lcc_v1")
        self.assertEqual(method_version("lcc_registry"), "lcc_v1")
        self.assertIn("GJ", HYBRID_NOTES)
        self.assertNotIn("GJ", LCC_CARRIERS)
        self.assertIn("2016", LCC_CARRIERS["KN"]["note"])

    def test_codeshare_uses_operating_carrier_and_falls_back_to_marketing(self):
        operating = classify_segment(
            {
                "flightNo": "MU1234",
                "airlineCode": "MU",
                "isCodeShare": True,
                "opAirline": "9C",
            }
        )
        fallback = classify_segment(
            {
                "flightNo": "9C6575",
                "airlineCode": "9C",
                "isCodeShare": True,
                "opAirline": "",
            }
        )

        self.assertEqual(
            operating,
            {"is_lcc": True, "carrier_code": "9C", "basis": "operating"},
        )
        self.assertEqual(
            fallback,
            {
                "is_lcc": True,
                "carrier_code": "9C",
                "basis": "marketing_fallback",
            },
        )

    def test_juhe_and_hasdata_segment_shapes_share_one_classifier(self):
        juhe = classify_segment(
            {
                "flightNo": "MU0001",
                "airlineCode": "MU",
                "isCodeShare": True,
                "opAirline": "MM",
            }
        )
        hasdata = classify_segment(
            {
                "flight_number": "JL999",
                "airline": "Japan Airlines",
                "is_codeshare": True,
                "operating_airline": {"iata_code": "GK"},
            }
        )

        self.assertEqual(juhe["carrier_code"], "MM")
        self.assertEqual(juhe["basis"], "operating")
        self.assertEqual(hasdata["carrier_code"], "GK")
        self.assertEqual(hasdata["basis"], "operating")

    def test_juhe_normalize_keeps_codeshare_operating_and_fallback_basis(self):
        source = JuheSource()
        common = {
            "airlineName": "市场承运",
            "equipment": "320",
            "departure": "PVG",
            "arrival": "KIX",
            "departureDate": "2026-10-01",
            "departureTime": "10:00",
            "arrivalDate": "2026-10-01",
            "arrivalTime": "13:00",
            "duration": "03h00m",
            "transferNum": 1,
            "ticketPrice": 500,
            "isCodeShare": True,
        }
        flights = source.normalize(
            [
                {
                    **common,
                    "flightNo": "MU1234",
                    "airline": "MU",
                    "opAirline": "9C",
                },
                {
                    **common,
                    "flightNo": "MM80",
                    "airline": "MM",
                    "opAirline": "",
                },
            ],
            collected_at="2026-07-25T12:00:00",
        )

        self.assertEqual(len(flights), 2)
        operating = classify_segment(flights[0]["segments"][0])
        fallback = classify_segment(flights[1]["segments"][0])
        self.assertEqual(
            operating,
            {"is_lcc": True, "carrier_code": "9C", "basis": "operating"},
        )
        self.assertEqual(
            fallback,
            {
                "is_lcc": True,
                "carrier_code": "MM",
                "basis": "marketing_fallback",
            },
        )

    def test_combo_summary_distinguishes_any_and_all_lcc(self):
        summary = classify_itinerary(
            {
                "segments": [
                    {"flight_no": "MM80", "airline": "Peach"},
                    {"flight_no": "JL891", "airline": "Japan Airlines"},
                ]
            }
        )

        self.assertTrue(summary["has_lcc"])
        self.assertFalse(summary["all_lcc"])
        self.assertEqual(len(summary["matched_segments"]), 1)
        self.assertEqual(summary["matched_segments"][0]["carrier_code"], "MM")


class LccFilterTest(unittest.TestCase):
    def setUp(self):
        self.lcc = _flight("MM80", "Peach Aviation")
        self.full_service = _flight("JL891", "Japan Airlines", price=600)

    def _apply(self, policy, flights=None, **extra):
        preferences = {
            "lcc_policy": policy,
            "time_preference_mode": "unlimited",
            "red_eye": "accept",
            **extra,
        }
        kept, excluded, meta = _apply_user_preferences(
            copy.deepcopy(flights or [self.lcc, self.full_service]),
            preferences,
        )
        kept, lcc_excluded = _apply_lcc_policy(kept, preferences)
        excluded.extend(lcc_excluded)
        _attach_filter_reason_details(excluded, preferences)
        return kept, excluded, meta

    def test_three_policies_and_rejection_codes(self):
        kept_any, excluded_any, _ = self._apply("any")
        kept_exclude, excluded_exclude, _ = self._apply("exclude_lcc")
        kept_only, excluded_only, _ = self._apply("lcc_only")

        self.assertEqual([item["flight_combo"] for item in kept_any], ["MM80", "JL891"])
        self.assertEqual(excluded_any, [])
        self.assertEqual([item["flight_combo"] for item in kept_exclude], ["JL891"])
        self.assertEqual(excluded_exclude[0]["filter_reason_code"], "lcc_excluded")
        self.assertIn("MM80:MM", excluded_exclude[0]["filter_reason_value"])
        self.assertEqual([item["flight_combo"] for item in kept_only], ["MM80"])
        self.assertEqual(excluded_only[0]["filter_reason_code"], "lcc_only_unmet")

    def test_lcc_only_requires_every_segment_to_be_lcc(self):
        mixed = _flight(
            "MM80+JL891",
            "Peach / Japan Airlines",
            segments=[
                {
                    "flight_no": "MM80",
                    "airline": "Peach",
                    "dep_time": "2026-10-01 10:00",
                    "arr_time": "2026-10-01 11:00",
                },
                {
                    "flight_no": "JL891",
                    "airline": "Japan Airlines",
                    "dep_time": "2026-10-01 12:00",
                    "arr_time": "2026-10-01 13:00",
                },
            ],
        )

        kept, excluded, _ = self._apply("lcc_only", [mixed])

        self.assertEqual(kept, [])
        self.assertEqual(excluded[0]["filter_reason_code"], "lcc_only_unmet")

    def test_exclude_airlines_runs_before_lcc_policy(self):
        kept, excluded, _ = self._apply(
            "exclude_lcc",
            [self.lcc],
            exclude_airlines=["Peach Aviation"],
        )

        self.assertEqual(kept, [])
        self.assertEqual(excluded[0]["filter_reason_code"], "exclude_airlines")

    def test_survivor_relative_order_is_unchanged(self):
        full_2 = _flight("NH970", "ANA", price=550)
        original = [self.full_service, self.lcc, full_2]

        kept_any, _, _ = self._apply("any", original)
        kept_exclude, _, _ = self._apply("exclude_lcc", original)

        any_survivors = [
            item["flight_combo"]
            for item in kept_any
            if item["flight_combo"] != "MM80"
        ]
        self.assertEqual(
            [item["flight_combo"] for item in kept_exclude],
            any_survivors,
        )

    def test_final_scores_and_relative_order_use_pre_lcc_reference_pool(self):
        lcc_extreme = _flight(
            "MM80",
            "Peach Aviation",
            price=100,
        )
        lcc_extreme["total_duration_min"] = 60
        full_1 = _flight("JL891", "Japan Airlines", price=500)
        full_1["total_duration_min"] = 120
        full_2 = _flight("NH970", "ANA", price=650)
        full_2["total_duration_min"] = 180
        pool = [lcc_extreme, full_1, full_2]
        base_preferences = {
            "time_preference_mode": "unlimited",
            "red_eye": "accept",
            "route_type": "international",
        }

        any_result = analyze_all_flights(
            copy.deepcopy(pool),
            user_preferences={**base_preferences, "lcc_policy": "any"},
        )
        excluded_result = analyze_all_flights(
            copy.deepcopy(pool),
            user_preferences={
                **base_preferences,
                "lcc_policy": "exclude_lcc",
            },
        )
        any_survivors = {
            flight["flight_combo"]: flight
            for flight in any_result["all_flights"]
            if flight["flight_combo"] != "MM80"
        }
        excluded_survivors = {
            flight["flight_combo"]: flight
            for flight in excluded_result["all_flights"]
        }

        self.assertEqual(set(excluded_survivors), set(any_survivors))
        for combo in excluded_survivors:
            self.assertEqual(
                excluded_survivors[combo]["scores"],
                any_survivors[combo]["scores"],
            )
            self.assertEqual(
                excluded_survivors[combo]["final_score"],
                any_survivors[combo]["final_score"],
            )
        any_order = [
            item["flight_combo"]
            for item in any_result["economy_recommendations"]
            if item["flight_combo"] != "MM80"
        ]
        excluded_order = [
            item["flight_combo"]
            for item in excluded_result["economy_recommendations"]
        ]
        self.assertEqual(excluded_order, any_order)

    def test_missing_policy_and_explicit_any_are_identical_end_to_end(self):
        pool = [
            self.lcc,
            self.full_service,
            _flight("NH970", "ANA", price=550),
        ]
        preferences = {
            "time_preference_mode": "unlimited",
            "red_eye": "accept",
            "route_type": "international",
        }

        missing = analyze_all_flights(
            copy.deepcopy(pool),
            user_preferences=preferences,
        )
        explicit_any = analyze_all_flights(
            copy.deepcopy(pool),
            user_preferences={**preferences, "lcc_policy": "any"},
        )

        missing.pop("user_preferences", None)
        explicit_any.pop("user_preferences", None)
        self.assertEqual(missing, explicit_any)

    def test_same_day_candidate_pool_honors_lcc_hard_filter(self):
        analysis = {
            "same_day_base_flights": [
                copy.deepcopy(self.lcc),
                copy.deepcopy(self.full_service),
            ],
            "user_preferences": {"lcc_policy": "exclude_lcc"},
        }

        candidates = _all_roundtrip_flights_for_same_day(analysis)

        self.assertEqual(
            [item["flight_combo"] for item in candidates],
            ["JL891"],
        )


class LccRenderingTest(unittest.TestCase):
    def _combo_plan(self, *, baggage="required", codeshare_missing_op=False):
        outbound = _flight("MM80", "Peach Aviation", price=480)
        if codeshare_missing_op:
            outbound["segments"][0]["is_codeshare"] = True
        inbound = _flight("JL891", "Japan Airlines", price=520)
        inbound["segments"][0]["dep_airport"] = "KIX"
        inbound["segments"][0]["arr_airport"] = "PVG"
        inbound["segments"][0]["dep_time"] = "2026-10-06 18:00"
        inbound["segments"][0]["arr_time"] = "2026-10-06 20:00"
        return _payload_combo_plan(
            {
                "outbound": outbound,
                "return": inbound,
                "total_price": 1000,
                "transaction_total": 1000,
            },
            {
                "depart_date": "2026-10-01",
                "return_date": "2026-10-06",
                "need_baggage": baggage,
            },
            0,
            "推荐",
        )

    def test_card_labels_lcc_segment_combo_and_required_baggage_warning(self):
        plan = self._combo_plan()
        rendered = _render_payload_plan_card(plan)

        self.assertIn("含廉航段", plan["tags"])
        self.assertIn("廉航", rendered)
        self.assertIn(
            "⚠ 含廉航段:票价通常不含托运行李,请以支付页为准",
            rendered,
        )

    def test_marketing_fallback_is_disclosed(self):
        rendered = _render_payload_plan_card(
            self._combo_plan(codeshare_missing_op=True)
        )

        self.assertIn("廉航(按市场承运)", rendered)

    def test_baggage_warning_only_for_lcc_and_required(self):
        lcc_optional = _render_payload_plan_card(
            self._combo_plan(baggage="optional")
        )
        non_lcc = _payload_combo_plan(
            {
                "outbound": _flight("JL891", "Japan Airlines"),
                "return": _flight("NH970", "ANA"),
                "total_price": 1000,
            },
            {
                "depart_date": "2026-10-01",
                "return_date": "2026-10-06",
                "need_baggage": "required",
            },
            0,
            "推荐",
        )

        self.assertNotIn("⚠ 含廉航段", lcc_optional)
        self.assertNotIn(
            "⚠ 含廉航段",
            _render_payload_plan_card(non_lcc),
        )


class LccSubscriptionTest(unittest.TestCase):
    def test_form_template_and_roundtrip_persist_policy(self):
        page = web_form.app.test_client().get("/settings").get_data(as_text=True)
        self.assertEqual(page.count('name="lcc_policy"'), 1)
        self.assertIn('value="exclude_lcc"', page)
        self.assertIn('value="lcc_only"', page)

        subscription = web_form.build_subscription(
            _base_form(lcc_policy="exclude_lcc")
        )
        self.assertEqual(subscription["lcc_policy"], "exclude_lcc")
        self.assertEqual(
            subscription["hard_constraints"]["lcc_policy"],
            "exclude_lcc",
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subscriptions.json"
            with patch.object(web_form, "SUBSCRIPTIONS_PATH", path):
                web_form.save_subscription(subscription)
                loaded = web_form.load_subscriptions()
        self.assertEqual(loaded[0]["lcc_policy"], "exclude_lcc")

    def test_legacy_subscription_migrates_to_any(self):
        subscriptions = [
            {
                "name": "旧订阅",
                "origin": "上海",
                "destination": "大阪",
                "hard_constraints": {},
            }
        ]

        migrated, records = web_form.migrate_lcc_policies(subscriptions)

        self.assertEqual(migrated[0]["lcc_policy"], "any")
        self.assertEqual(records[0]["route"], "上海->大阪")

    def test_constraints_only_policy_survives_analyzer_migration(self):
        migrated = migrate_old_subscription(
            {
                "constraints": {"lcc_policy": "exclude_lcc"},
                "hard_constraints": {},
            }
        )

        self.assertEqual(migrated["lcc_policy"], "exclude_lcc")

    def test_preflight_rejects_invalid_policy(self):
        result = evaluate_subscription_preflight(
            {
                "origin": "PVG",
                "destination": "KIX",
                "depart_date": "2099-10-01",
                "lcc_policy": "maybe",
            }
        )

        self.assertTrue(result["skip"])
        self.assertEqual(result["reason_code"], "invalid_lcc_policy")
        self.assertIn("lcc_policy", result["reason"])

    def test_invalid_policy_preflight_log_keeps_real_reason(self):
        subscription = {
            "name": "错误廉航策略",
            "origin": "PVG",
            "destination": "KIX",
        }
        preflight = {
            "skip": True,
            "reason_code": "invalid_lcc_policy",
            "reason": "lcc_policy取值无效(maybe)",
            "latest_date": None,
        }

        with patch("main.safe_log") as log:
            main._log_preflight_skip(subscription, preflight)

        message = str(log.call_args.args[0])
        self.assertIn("lcc_policy取值无效", message)
        self.assertNotIn("全部采集日期已过期", message)


if __name__ == "__main__":
    unittest.main()
