"""R33：推荐判重按行程身份，往返价格候选取自专用候选池。

合同（2026-11-08 维护者裁定）：
- 同路线不同航班组合不互相去重；route_summary、推荐标签、说明文字不参与判重。
- 最便宜但综合分最低的一班仍在价格候选与往返价格选择中。
- 往返价格候选从 roundtrip_candidate_flights 取（满足约束、未经展示截断）；
  新键为 [] 时保持无候选不回退；仅在新键缺失时回退旧产物的 all_flights；
  新键为非法类型时明确报错；不再引用 economy_recommendations。
- 身份不足（flight_combo 缺失或规范化后为空）的记录互不判重、各自保留。
- 同产品重复报价不膨胀成多个方案且保留选价来源（既有合并行为回归锁）。
"""
import unittest

import analyzer
from analyzer import _top_flights_for_round_trip, select_recommendations
from main import _dedupe_flights
from notifier import _select_compact_recommendations


def make_flight(combo, price, route_summary="PVG → KIX", cabin="economy", **extra):
    flight = {
        "flight_combo": combo,
        "cabin_class": cabin,
        "price": price,
        "route_summary": route_summary,
        "departure_time": "2026-12-01 08:15",
        "arrival_time": "2026-12-01 10:25",
        "total_duration_min": 130,
        "stops": 0,
        "layovers": [],
        "segments": [
            {
                "flight_no": combo,
                "dep_airport": "PVG",
                "arr_airport": "KIX",
                "dep_time": "2026-12-01 08:15",
                "arr_time": "2026-12-01 10:25",
            }
        ],
    }
    flight.update(extra)
    return flight


def combos_of(flights):
    return [flight.get("flight_combo") for flight in flights]


class ItineraryIdentityKeyTest(unittest.TestCase):
    def test_helper_ignores_route_summary_text(self):
        helper = getattr(analyzer, "flight_itinerary_identity_key", None)
        self.assertIsNotNone(helper, "analyzer 缺少 flight_itinerary_identity_key")
        first = helper(make_flight("MU100", 800, route_summary="PVG → KIX"))
        second = helper(make_flight("MU100", 800, route_summary="上海 → 大阪"))
        self.assertIsNotNone(first)
        self.assertEqual(first, second)

    def test_helper_distinguishes_combos_and_cabins(self):
        helper = getattr(analyzer, "flight_itinerary_identity_key", None)
        self.assertIsNotNone(helper, "analyzer 缺少 flight_itinerary_identity_key")
        self.assertNotEqual(
            helper(make_flight("MU100", 800)),
            helper(make_flight("CA200", 800)),
        )
        self.assertNotEqual(
            helper(make_flight("MU100", 800, cabin="economy")),
            helper(make_flight("MU100", 3000, cabin="business")),
        )

    def test_helper_returns_none_when_identity_insufficient(self):
        helper = getattr(analyzer, "flight_itinerary_identity_key", None)
        self.assertIsNotNone(helper, "analyzer 缺少 flight_itinerary_identity_key")
        self.assertIsNone(helper(make_flight("", 800)))
        self.assertIsNone(helper({"price": 800}))
        self.assertIsNone(helper("not-a-dict"))


class RecommendationIdentityTest(unittest.TestCase):
    def test_same_route_text_different_combos_not_deduped(self):
        flights = [
            make_flight(f"MU10{index}", 800 + index * 10, route_summary="PVG → KIX")
            for index in range(5)
        ]
        recs, _business = select_recommendations(flights, [], mode="budget")
        self.assertEqual(len(recs), 4)

    def test_route_summary_rewrite_does_not_change_selection(self):
        base = [
            make_flight("MU100", 800, route_summary="X"),
            make_flight("CA200", 850, route_summary="X"),
            make_flight("CZ300", 900, route_summary="Z"),
        ]
        rewritten = [
            make_flight("MU100", 800, route_summary="X"),
            make_flight("CA200", 850, route_summary="Z"),
            make_flight("CZ300", 900, route_summary="Z"),
        ]
        first, _ = select_recommendations(base, [], mode="budget")
        second, _ = select_recommendations(rewritten, [], mode="budget")
        self.assertEqual(combos_of(first), combos_of(second))
        self.assertEqual(combos_of(first), ["MU100", "CA200", "CZ300"])

    def test_missing_combo_flights_are_not_deduped(self):
        flights = [
            make_flight("", 800, route_summary="PVG → KIX"),
            make_flight("", 850, route_summary="PVG → KIX"),
        ]
        recs, _business = select_recommendations(flights, [], mode="budget")
        self.assertEqual(len(recs), 2)


class RoundtripCandidatePoolTest(unittest.TestCase):
    def test_cheapest_low_score_flight_stays_in_roundtrip_candidates(self):
        cheap = make_flight("MU100", 800, final_score=10)
        mid = make_flight("CA200", 850, final_score=60)
        dear = make_flight("CZ300", 900, final_score=90)
        analysis = {
            "economy_recommendations": [dear, mid],
            "all_flights": [dear, mid],
            "roundtrip_candidate_flights": [dear, cheap, mid],
        }
        result = _top_flights_for_round_trip(analysis, 3)
        self.assertEqual(combos_of(result), ["MU100", "CA200", "CZ300"])

    def test_empty_new_pool_is_authoritative_and_does_not_fall_back(self):
        flight = make_flight("MU100", 800)
        analysis = {
            "economy_recommendations": [flight],
            "all_flights": [flight],
            "roundtrip_candidate_flights": [],
        }
        self.assertEqual(_top_flights_for_round_trip(analysis, 3), [])

    def test_missing_key_falls_back_to_all_flights(self):
        dear = make_flight("CZ300", 900)
        cheap = make_flight("MU100", 800)
        analysis = {"all_flights": [dear, cheap]}
        result = _top_flights_for_round_trip(analysis, 3)
        self.assertEqual(combos_of(result), ["MU100", "CZ300"])

    def test_invalid_pool_type_raises_instead_of_silent_fallback(self):
        analysis = {
            "all_flights": [make_flight("MU100", 800)],
            "roundtrip_candidate_flights": None,
        }
        with self.assertRaises(TypeError):
            _top_flights_for_round_trip(analysis, 3)

    def test_new_pool_skips_reference_and_insufficient_detail(self):
        good = make_flight("MU100", 800)
        reference = make_flight("CA200", 700, reference_only=True)
        no_detail = make_flight("CZ300", 750, segments=[],
                                departure_time="", arrival_time="")
        analysis = {"roundtrip_candidate_flights": [good, reference, no_detail]}
        result = _top_flights_for_round_trip(analysis, 3)
        self.assertEqual(combos_of(result), ["MU100"])


class SameProductMergeContractTest(unittest.TestCase):
    def test_same_product_quotes_keep_min_price_and_do_not_multiply(self):
        dear = make_flight("MU100", 900, departure_airport="PVG", arrival_airport="KIX")
        cheap = make_flight("mu100 ", 800, departure_airport="PVG", arrival_airport="KIX",
                            data_source="juhe")
        result = _dedupe_flights([dear, cheap])
        self.assertEqual(len(result), 1)
        self.assertEqual(float(result[0]["price"]), 800.0)
        self.assertEqual(result[0].get("data_source"), "juhe")


class NotifierFallbackIdentityTest(unittest.TestCase):
    def test_fallback_not_deduped_by_route_text(self):
        flights = [
            make_flight(f"MU10{index}", 800 + index * 10, route_summary="PVG → KIX")
            for index in range(5)
        ]
        recs, _business = _select_compact_recommendations({"all_flights": flights})
        self.assertEqual(len(recs), 4)


if __name__ == "__main__":
    unittest.main()
