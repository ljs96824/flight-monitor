"""R44: price and rank ordinary round trips before applying the leg safety cap."""
import copy
import hashlib
import inspect
import unittest
from unittest.mock import patch

import analyzer
import notifier
import test_same_day_business_mode as same_day
from test_mixed_cabin_phase1 import MIXED_ALLOCATION, MIXED_PASSENGERS
from tests import test_baggage_eligibility_and_budget_caliber as baggage
from tests import test_mojibake_literals as literals
from tests import test_r38_duration_and_layover_contracts as duration
from tests import test_recommendation_identity as identity


REQUIRED = dict(need_baggage="required", passengers=baggage.PASSENGERS, route_type="international")
MIXED = dict(cabin_arrangement="mixed", cabin_allocation=MIXED_ALLOCATION, passengers=MIXED_PASSENGERS,
             route_type="international", max_budget_scope="all")


def leg(number, price, inbound=False, **extras):
    orig, dest, day = ("KIX", "PVG", "2026-12-06") if inbound else ("PVG", "KIX", "2026-12-01")
    flight = identity.make_flight(number, price, flight_no=number, **extras)
    dep, arr = day + " 08:15", day + " 10:25"
    flight.update(departure_time=dep, arrival_time=arr, departure_airport=orig,
                  arrival_airport=dest, route_summary=f"{orig} -> {dest}")
    flight["segments"][0].update(dep_airport=orig, arr_airport=dest, dep_time=dep, arr_time=arr)
    return flight


def free():
    return {"fare_rules": {"baggage": {"included": True, "checked_pieces": 1}}}


def paid(amount):
    return {"checked_baggage_fee_spec": {"amount": amount, "currency": "CNY", "fee_unit": "per_passenger",
            "itinerary_scope": "leg", "applicable_to_current_quote": True, "included_in_fare": False}}


def analysis(rows, prefs=None, business=()):
    return {"roundtrip_candidate_flights": rows, "all_flights": rows, "user_preferences": prefs or {},
            "business_flights": list(business)}


def select(out, ret, **opts):
    return analyzer.analyze_round_trip(out, ret, emit_diagnostics=False, **opts)


def numbers(combo):
    return combo["outbound"]["flight_combo"], combo["return"]["flight_combo"]


def bag_inputs(package=False):
    service = {"service_id": "rt-bag", "passenger_scope": "all_seated_passengers", "itinerary_scope": "roundtrip",
               "included_in_fare": False, "amount": 260, "quantity": 1, "currency": "CNY"}
    result = []
    for inbound in (False, True):
        start = 200 if inbound else 100
        rows = [leg(f"MU{start+i}", price, inbound, **paid(100 if package else 200))
                for i, price in enumerate((900, 910, 920))]
        number = (504 if package else 400) + int(inbound)
        rows.append(leg(f"MU{number}", 950 if package else 960, inbound,
                        **({"baggage_services": [service]} if package else free())))
        result.append(analysis(rows, REQUIRED))
    return result


def mixed_inputs(cheap_count=3, fifth=False):
    result = []
    for inbound, chosen, economy, business in ((False, "MU225", 3000, 5683), (True, "JL891", 3200, 6000)):
        prefix = "GK" if inbound else "MM"
        prices = (800, 860, 920) if cheap_count == 3 else range(500, 500 + cheap_count * 10, 10)
        rows = [leg(f"{prefix}{100+i}", price, inbound) for i, price in enumerate(prices)]
        rows.append(leg(chosen, economy, inbound))
        premium = [leg(chosen, 7500 if fifth else business, inbound, cabin_class="business", price_source="serpapi")]
        if fifth:
            extra = "JL895" if inbound else "MU229"
            rows.append(leg(extra, 3150, inbound))
            premium.append(leg(extra, 4800, inbound, cabin_class="business", price_source="serpapi"))
        result.append(analysis(rows, MIXED, premium))
    return result


class TestTrack1FullPoolAndSafetyCap(unittest.TestCase):
    def test_g01_top_flights_helper_supports_full_pool_and_rejects_non_scalar_prices(self):
        values = [1200, 900, 800, 1100, 1000, True, float("inf"), float("nan")]
        data = analysis([leg(f"MU{100+i}", p) for i, p in enumerate(values)] + [leg("MU200", 500, reference_only=True)])
        self.assertEqual([f["price"] for f in analyzer._top_flights_for_round_trip(data)], [800, 900, 1000])
        self.assertEqual([f["price"] for f in analyzer._top_flights_for_round_trip(data, None)], sorted(values[:5]))

    def test_g02_ordinary_roundtrip_evaluates_beyond_top3_and_breaks_equal_price_ties_by_effective_cost(self):
        out = analysis([leg(f"MU{401+i}", 1000, total_duration_min=300 if i < 3 else 100) for i in range(4)])
        ret = analysis([leg("JL601", 1000, True)])
        before = copy.deepcopy((out, ret))
        result = select(out, ret)
        self.assertEqual(numbers(result["top_combinations"][0]), ("MU404", "JL601"))
        self.assertEqual((out, ret), before)

    def test_g03_multiairport_safety_cap_bounds_combinations_without_losing_optimal_baggage_leg(self):
        def data(prefix, best, inbound):
            rows = [leg(f"{prefix}{100+i}", 800+i, inbound, **(free() if i >= best else paid(500)))
                    for i in range(40)]
            for index, row in enumerate(rows):
                airport = ("KIX", "ITM")[index % 2]
                field, segment = ("departure_airport", "dep_airport") if inbound else ("arrival_airport", "arr_airport")
                row[field] = row["segments"][0][segment] = airport
            return analysis(rows, REQUIRED)
        with patch.object(analyzer, "calc_effective_cost", wraps=analyzer.calc_effective_cost) as cost, patch.object(
            analyzer, "_dedupe_roundtrip_combinations", wraps=analyzer._dedupe_roundtrip_combinations
        ) as dedupe:
            result = select(data("MU", 14, False), data("JL", 17, True))
        self.assertEqual(numbers(result["top_combinations"][0]), ("MU114", "JL117"))
        self.assertLessEqual(cost.call_count, 80)
        self.assertLessEqual(max(len(call.args[0]) for call in dedupe.call_args_list), 24 * 24)

    def test_g04_single_leg_effective_cost_is_precomputed_once_per_leg_and_max_combination_spans_pool(self):
        out = analysis([leg(f"MU{100+i}", 800+i) for i in range(6)])
        ret = analysis([leg(f"JL{100+i}", 900+i, True) for i in range(6)])
        with patch.object(analyzer, "calc_effective_cost", wraps=analyzer.calc_effective_cost) as cost:
            result = select(out, ret)
        self.assertLessEqual(cost.call_count, 12)
        self.assertEqual(numbers(result["max_combination"]), ("MU105", "JL105"))
        best = result["top_combinations"][0]
        self.assertEqual(best["effective_cost"], analyzer.calc_roundtrip_effective_cost(best["outbound"], best["return"], {}))


class TestTrack2BaggageInclusiveRoundtripSelection(unittest.TestCase):
    def test_g05_fourth_cheapest_free_baggage_flight_beats_top3_bare_fare_paid_baggage(self):
        result = select(*bag_inputs())
        best = result["top_combinations"][0]
        self.assertEqual(numbers(best), ("MU400", "MU401"))
        self.assertEqual(best["selection_cost_all"], 5280)
        self.assertEqual((result["outbound_min"], result["return_min"], result["total_min"]), (960, 960, 1920))
        self.assertEqual(result["outbound_top3"][0]["flight_combo"], "MU400")
        self.assertEqual(result["return_top3"][0]["flight_combo"], "MU401")

    def test_g06_uncalculable_per_person_baggage_in_top3_does_not_block_fourth_calculable_flight(self):
        services = [{"included_in_fare": True, "passenger_ids": ["adult_1", "adult_2"], "itinerary_scope": "leg"},
                    {"included_in_fare": False, "passenger_ids": ["child_1"], "itinerary_scope": "leg",
                     "amount": 100, "currency": "CNY", "quantity": 1}]
        rows = [leg(f"MU{401+i}", 700+i*10, baggage_services=services) for i in range(3)] + [leg("MU404", 850, **paid(100))]
        prefs = dict(REQUIRED, max_budget_scope="per_person")
        result = select(analysis(rows, prefs), analysis([leg("JL600", 800, True, **free())], prefs), max_budget=2500)
        self.assertTrue(result["top_combinations"])
        self.assertEqual(numbers(result["top_combinations"][0]), ("MU404", "JL600"))
        self.assertNotIn("no_primary_reason", result)

    def test_g07_all_uncalculable_baggage_legs_clear_roundtrip_minimums_and_top3_fallbacks(self):
        result = select(*(analysis([leg(f"MU{start+i}", 800+i, inbound) for i in range(4)], REQUIRED)
                          for start, inbound in ((100, False), (200, True))))
        self.assertEqual(result["top_combinations"], [])
        self.assertEqual(result["no_primary_reason"], baggage.MISSING_PRIMARY)
        for key in ("outbound_min", "return_min", "total_min", "budget_price", "budget_price_compare"):
            self.assertIsNone(result[key], key)
        self.assertEqual((result["outbound_top3"], result["return_top3"]), ([], []))

    def test_g08_fourth_flight_with_shared_roundtrip_baggage_package_wins_without_double_charge(self):
        best = select(*bag_inputs(package=True))["top_combinations"][0]
        self.assertEqual(numbers(best), ("MU504", "MU505"))
        self.assertEqual(best["baggage_fee_all"], 260)


class TestTrack3MixedCabinFullPoolPairing(baggage.ContractBase):
    def test_g09_fourth_cheapest_economy_pairs_with_same_flight_business_when_top3_are_lcc(self):
        result = select(*mixed_inputs())
        self.assertTrue(result["top_combinations"])
        best = result["top_combinations"][0]
        self.assertEqual(numbers(best), ("MU225", "JL891"))
        self.assertTrue(best["mixed_cabin"])
        self.assertEqual((best["passenger_total_price"], result["budget_price_compare"]), (40416, 40416))
        self.assertEqual(result["mixed_cabin_matching"]["stats"], {"candidates": 16, "full": 1, "partial": 15})

    def test_g10_multiairport_safety_cap_preserves_business_matched_economy_beyond_rank_24(self):
        result = select(*mixed_inputs(29))
        self.assertTrue(result["top_combinations"])
        best = result["top_combinations"][0]
        self.assertEqual(numbers(best), ("MU225", "JL891"))
        self.assertEqual(best["passenger_total_price"], 40416)
        self.assertEqual(result["mixed_cabin_matching"]["stats"]["candidates"], 24 * 24)

    def test_g11_fifth_economy_with_lower_combined_mixed_total_beats_fourth_economy(self):
        result = select(*mixed_inputs(fifth=True))
        self.assertTrue(result["top_combinations"])
        self.assertEqual(numbers(result["top_combinations"][0]), ("MU229", "JL895"))

    def test_g12_notification_payload_surfaces_fourth_rank_baggage_and_mixed_cabin_winners(self):
        route = dict(origin="PVG", destination="KIX", depart_date="2026-12-01", return_date="2026-12-06",
                     round_trip=True, route_type="international")
        for inputs, expected in ((bag_inputs(), ("MU400", "MU401")), (mixed_inputs(), ("MU225", "JL891"))):
            with self.subTest(winner=expected):
                out, ret = inputs
                rt = select(out, ret)
                bundle = dict(out, round_trip_analysis=rt, return_analysis=ret)
                payload = notifier.build_notification_payload(bundle, route_info=route, subscription=out["user_preferences"])
                plans = payload["recommended_plans"]
                self.assertTrue(plans)
                self.assertEqual((plans[0]["outbound_flight"]["flight_combo"], plans[0]["return_flight"]["flight_combo"]), expected)
                self.assertEqual(numbers(notifier._round_trip_combinations(bundle)[0]), expected)


class TestTrack4EvergreenCompatibilityGuards(unittest.TestCase):
    def test_g13_r33_candidate_pool_and_same_day_roundtrip_contracts_remain_intact(self):
        case = identity.RoundtripCandidatePoolTest
        names = unittest.defaultTestLoader.getTestCaseNames(case)
        self.assertEqual(len(names), 5)
        for name in names:
            case(name).debug()
        same_day.SameDayBusinessModeTest("test_analyze_round_trip_same_day_uses_all_candidates_before_recommendation").debug()

    def test_g14_ast_source_and_mojibake_anchors_remain_exact(self):
        duration.TestTrack4FrozenAndAstGuards("test_g23_ast_anchors_are_unchanged").debug()
        for func, digest in ((analyzer.parse_flight_time, "9a55d7e7d427adf2469b269546262ebc3d3264ec7f7cae4b7df537f1a4177297"),
                             (analyzer.match_time_preference, "2e21c6f1cb34a5300b194c59b167b52753e321f2d7e696685c153f64c98af93f")):
            self.assertEqual(hashlib.sha256(inspect.getsource(func).encode("utf-8")).hexdigest(), digest)
        literals.RestoredReachableTextTest("test_round_trip_insight_text_is_readable").debug()

    def test_g15_three_frozen_carriers_and_normal_roundtrip_baseline_remain_exact(self):
        baggage.TestTrack4FrozenAndAstGuards("test_g24_three_frozen_carriers_and_normal_roundtrip_remain_exact").debug()
