import unittest
from tempfile import TemporaryDirectory

from werkzeug.datastructures import MultiDict


MIXED_PASSENGERS = {
    "adult": 2,
    "child": 1,
    "elderly": 2,
    "infant": 0,
}

MIXED_ALLOCATION = {
    "business": {"adult": 2, "child": 0, "elderly": 0, "infant": 0},
    "economy": {"adult": 0, "child": 1, "elderly": 2, "infant": 0},
}


class CabinAllocationTest(unittest.TestCase):
    def test_explicit_allocation_is_normalized_and_counted(self):
        from cabin_allocation import validate_cabin_allocation

        result = validate_cabin_allocation(MIXED_ALLOCATION, MIXED_PASSENGERS)

        self.assertEqual(result["allocation"], MIXED_ALLOCATION)
        self.assertEqual(result["business_seats"], 2)
        self.assertEqual(result["economy_seats"], 3)
        self.assertEqual(result["label"], "商务2人+经济3人")

    def test_allocation_rejects_negative_or_mismatched_counts(self):
        from cabin_allocation import validate_cabin_allocation

        negative = {
            **MIXED_ALLOCATION,
            "business": {**MIXED_ALLOCATION["business"], "adult": -1},
        }
        with self.assertRaisesRegex(ValueError, "不能为负数"):
            validate_cabin_allocation(negative, MIXED_PASSENGERS)

        mismatched = {
            **MIXED_ALLOCATION,
            "economy": {**MIXED_ALLOCATION["economy"], "elderly": 1},
        }
        with self.assertRaisesRegex(ValueError, "老人.*2人.*1人"):
            validate_cabin_allocation(mismatched, MIXED_PASSENGERS)

    def test_normalization_forces_all_scope_only_for_explicit_allocation(self):
        from main import normalize_subscription

        explicit = normalize_subscription(
            {
                "origin": "PVG",
                "destination": "KIX",
                "depart_date": "2026-10-01",
                "return_date": "2026-10-06",
                "round_trip": True,
                "budget_scope": "per_person",
                "max_budget_scope": "per_person",
                "target_price_scope": "per_person",
                "preferences": {"passengers": MIXED_PASSENGERS},
                "hard_constraints": {
                    "cabin_arrangement": "mixed",
                    "cabin_allocation": MIXED_ALLOCATION,
                    "max_budget_scope": "per_person",
                    "target_price_scope": "per_person",
                },
            }
        )
        self.assertEqual(explicit["budget_scope"], "all")
        self.assertEqual(explicit["max_budget_scope"], "all")
        self.assertEqual(explicit["target_price_scope"], "all")
        self.assertEqual(explicit["hard_constraints"]["cabin_allocation"], MIXED_ALLOCATION)
        self.assertEqual(explicit["hard_constraints"]["business_seats"], 2)
        self.assertEqual(explicit["hard_constraints"]["economy_seats"], 3)
        self.assertEqual(explicit["cabin_classes"], ["economy", "business"])

        legacy = normalize_subscription(
            {
                "origin": "PVG",
                "destination": "KIX",
                "depart_date": "2026-10-01",
                "budget_scope": "per_person",
                "preferences": {"passengers": MIXED_PASSENGERS},
                "hard_constraints": {
                    "cabin_arrangement": "mixed",
                    "business_seats": 2,
                    "economy_seats": 3,
                },
            }
        )
        self.assertEqual(legacy["budget_scope"], "per_person")
        self.assertNotIn("cabin_allocation", legacy["hard_constraints"])


class MixedCabinPriceTreeTest(unittest.TestCase):
    def test_member_by_cabin_price_tree_sums_from_rounded_components(self):
        from price_estimator import build_display_prices

        result = build_display_prices(
            None,
            None,
            MIXED_PASSENGERS,
            "international",
            per_cabin_unit_prices={
                "outbound": {"economy": 3000, "business": 5683},
                "return": {"economy": 3200, "business": 6000},
            },
            cabin_allocation=MIXED_ALLOCATION,
        )

        self.assertEqual(result["outbound"]["cabins"]["business"]["total"], 11366)
        self.assertEqual(result["outbound"]["cabins"]["economy"]["total"], 8250)
        self.assertEqual(result["outbound"]["total"], 19616)
        self.assertEqual(result["return"]["total"], 20800)
        self.assertEqual(result["total"], 40416)
        self.assertEqual(result["cabin_label"], "商务2人+经济3人")
        self.assertEqual(result["outbound"]["component_sum"], result["outbound"]["total"])
        self.assertEqual(result["return"]["component_sum"], result["return"]["total"])
        self.assertEqual(
            result["outbound"]["total"] + result["return"]["total"],
            result["total"],
        )
        self.assertIn("商务舱儿童票规差异大", result["note"])


class MixedCabinMatchingTest(unittest.TestCase):
    @staticmethod
    def _combo():
        return {
            "outbound": {"flight_combo": "MU 225", "price": 3000},
            "return": {"flight_combo": "JL 891", "price": 3200},
            "outbound_price": 3000,
            "return_price": 3200,
            "total_price": 6200,
        }

    def test_same_flight_business_match_all_partial_and_none(self):
        from mixed_cabin import match_mixed_cabin_combinations

        outbound_business = [
            {
                "flight_combo": "MU225",
                "flight_no": "MU225",
                "price": 5683,
                "airline": "中国东方航空",
                "cabin_class": "business",
                "data_source": "serpapi",
                "price_note": "SerpAPI展示价,税费构成未拆分,以支付页为准",
            }
        ]
        return_business = [
            {
                "flight_combo": "JL891",
                "flight_no": "JL891",
                "price": 6000,
                "airline": "日本航空",
                "cabin_class": "business",
                "data_source": "serpapi",
                "price_note": "SerpAPI展示价,税费构成未拆分,以支付页为准",
            }
        ]

        full = match_mixed_cabin_combinations(
            [self._combo()],
            outbound_business,
            return_business,
            cabin_allocation=MIXED_ALLOCATION,
            passengers=MIXED_PASSENGERS,
            route_type="international",
        )
        self.assertEqual(full["stats"], {"candidates": 1, "full": 1, "partial": 0})
        self.assertEqual(len(full["priceable"]), 1)
        self.assertEqual(full["priceable"][0]["passenger_total_price"], 40416)
        self.assertEqual(full["priceable"][0]["business_price_source"], "serpapi")
        self.assertEqual(full["business_visible_count"], 2)
        self.assertEqual(full["business_reference"]["price"], 5683)

        partial = match_mixed_cabin_combinations(
            [self._combo()],
            outbound_business,
            [],
            cabin_allocation=MIXED_ALLOCATION,
            passengers=MIXED_PASSENGERS,
            route_type="international",
        )
        self.assertEqual(partial["stats"], {"candidates": 1, "full": 0, "partial": 1})
        self.assertEqual(partial["priceable"], [])
        self.assertIn("商务舱价未获取", partial["unpriceable"][0]["mixed_cabin_reason"])
        self.assertIn("JL891", partial["unpriceable"][0]["mixed_cabin_reason"])

        none = match_mixed_cabin_combinations(
            [self._combo()],
            [],
            [],
            cabin_allocation=MIXED_ALLOCATION,
            passengers=MIXED_PASSENGERS,
            route_type="international",
        )
        self.assertEqual(none["stats"], {"candidates": 1, "full": 0, "partial": 1})
        self.assertEqual(none["priceable"], [])


class MixedCabinCollectionPlanTest(unittest.TestCase):
    def test_business_cabin_only_plans_primary_outbound_and_return_dates(self):
        from collection_plan import build_collection_plan

        class Source:
            def __init__(self, name, cabins):
                self.name = name
                self.supported_cabins = frozenset(cabins)

        juhe = Source("juhe", {"economy"})
        serpapi = Source("serpapi", {"business"})
        duffel = Source("duffel", {"economy", "business"})

        def source_builder(_origin, _dest, route_type=None):
            self.assertEqual(route_type, "international")
            return [juhe, serpapi], [duffel]

        plan = build_collection_plan(
            subscriptions=[
                {
                    "_index": 11,
                    "origin_airports_active": ["PVG"],
                    "destination_airports_active": ["KIX"],
                    "depart_date": "2026-10-01",
                    "return_date": "2026-10-06",
                    "round_trip": True,
                    "date_flexibility": 3,
                    "return_date_flexibility": 3,
                    "route_type": "international",
                    "cabin_classes": ["economy", "business"],
                    "preferences": {"passengers": MIXED_PASSENGERS},
                }
            ],
            basket_requests=[],
            source_builder=source_builder,
            include_calendars=True,
        )

        business = [
            request
            for request in plan._requests.values()
            if request.cabin_class == "business"
        ]
        self.assertEqual(
            {(item.origin, item.dest, item.date_str, item.source.name) for item in business},
            {
                ("PVG", "KIX", "2026-10-01", "serpapi"),
                ("PVG", "KIX", "2026-10-01", "duffel"),
                ("KIX", "PVG", "2026-10-06", "serpapi"),
                ("KIX", "PVG", "2026-10-06", "duffel"),
            },
        )
        pricing = [item for item in business if item.source.name == "serpapi"]
        enrichment = [item for item in business if item.source.name == "duffel"]
        self.assertTrue(all(item.conditional is None for item in pricing))
        self.assertTrue(all(item.reasons == {"主行程"} for item in pricing))
        self.assertTrue(all(item.conditional == "search_has_candidates" for item in enrichment))
        self.assertTrue(all(item.reasons == {"行李退改补充"} for item in enrichment))
        self.assertEqual(plan.cabin_counts.get("business"), 2)
        self.assertEqual(plan.enrichment_cabin_counts.get("business"), 2)

    def test_business_basket_request_is_not_planned(self):
        from collection_plan import build_collection_plan

        class Source:
            name = "serpapi"
            supported_cabins = frozenset({"business"})

        plan = build_collection_plan(
            subscriptions=[],
            basket_requests=[
                {
                    "queue": "no-business-basket",
                    "origin": "PVG",
                    "dest": "KIX",
                    "depart_date": "2026-10-01",
                    "route_type": "international",
                    "cabin_class": "business",
                }
            ],
            source_builder=lambda *_args, **_kwargs: ([Source()], []),
        )
        self.assertEqual(plan.unique_count, 0)


class MixedCabinFingerprintTest(unittest.TestCase):
    def test_allocation_changes_constraint_fingerprint(self):
        from constraint_fingerprint import constraint_fingerprint

        first = constraint_fingerprint({"cabin_allocation": MIXED_ALLOCATION})
        changed = {
            "business": {"adult": 1, "child": 0, "elderly": 1, "infant": 0},
            "economy": {"adult": 1, "child": 1, "elderly": 1, "infant": 0},
        }
        second = constraint_fingerprint({"cabin_allocation": changed})
        self.assertNotEqual(first, second)

    def test_empty_allocation_does_not_change_legacy_fingerprint_payload(self):
        from constraint_fingerprint import normalized_constraint_set

        without_field = normalized_constraint_set({})
        empty_field = normalized_constraint_set({"cabin_allocation": {}})
        self.assertNotIn("cabin_allocation", without_field)
        self.assertEqual(empty_field, without_field)


class MixedCabinFormContractTest(unittest.TestCase):
    def test_full_page_renders_allocation_once_and_submits_total_scope(self):
        import web_form

        web_form.app.config.update(TESTING=True)
        page = web_form.app.test_client().get("/settings").get_data(as_text=True)
        for cabin in ("business", "economy"):
            for passenger_type in ("adult", "elderly", "child", "infant"):
                name = f"cabin_{cabin}_{passenger_type}"
                self.assertEqual(page.count(f'name="{name}"'), 1, name)
        self.assertIn('data-visibility-contract="mixed-cabin"', page)

        from scripts.capture_form_normalization_baseline import _base_form
        from web_form import build_subscription

        form = _base_form(
            monitor_mode="precise",
            travel_scenario=["tourism"],
            adult_count="2",
            child_count="1",
            elderly_count="2",
            infant_count="0",
            cabin_arrangement="mixed",
            cabin_business_adult="2",
            cabin_business_child="0",
            cabin_business_elderly="0",
            cabin_business_infant="0",
            cabin_economy_adult="0",
            cabin_economy_child="1",
            cabin_economy_elderly="2",
            cabin_economy_infant="0",
        )
        subscription = build_subscription(MultiDict(form))
        self.assertEqual(subscription["cabin_allocation"], MIXED_ALLOCATION)
        self.assertEqual(subscription["constraints"]["cabin_allocation"], MIXED_ALLOCATION)
        self.assertEqual(subscription["hard_constraints"]["cabin_allocation"], MIXED_ALLOCATION)
        self.assertEqual(subscription["budget_scope"], "all")
        self.assertEqual(subscription["max_budget_scope"], "all")
        self.assertEqual(subscription["target_price_scope"], "all")
        from web_form import build_success_summary

        summary = build_success_summary(subscription)
        self.assertEqual(summary["cabin_text"], "商务2人+经济3人")

    def test_form_rejects_mismatched_allocation(self):
        from scripts.capture_form_normalization_baseline import _base_form
        from web_form import build_subscription

        form = _base_form(
            monitor_mode="precise",
            travel_scenario=["tourism"],
            adult_count="2",
            child_count="1",
            elderly_count="2",
            cabin_arrangement="mixed",
            cabin_business_adult="2",
            cabin_economy_child="1",
            cabin_economy_elderly="1",
        )
        with self.assertRaisesRegex(ValueError, "老人.*2人.*1人"):
            build_subscription(MultiDict(form))

    def test_ui_smoke_covers_mixed_cabin_visibility_roundtrip_and_rejection(self):
        from pathlib import Path

        driver = (Path(__file__).parent / "scripts" / "ui_smoke_driver.mjs").read_text(encoding="utf-8")
        self.assertIn("混舱显隐=PASS", driver)
        self.assertIn("混舱分配回读=PASS", driver)
        self.assertIn("混舱错误回显=PASS", driver)


class MixedCabinAnalyzerIntegrationTest(unittest.TestCase):
    @staticmethod
    def _analysis(combo, price, business_price, allocation=MIXED_ALLOCATION):
        economy = {
            "flight_combo": combo,
            "flight_no": combo,
            "price": price,
            "total_duration_min": 120,
            "stops": 0,
            "execution_grade": "A",
            "cabin_class": "economy",
        }
        business = {
            **economy,
            "price": business_price,
            "cabin_class": "business",
            "data_source": "serpapi",
            "price_source": "serpapi",
            "price_note": "SerpAPI展示价,税费构成未拆分,以支付页为准",
        }
        return {
            "economy_recommendations": [economy],
            "all_flights": [economy],
            "business_flights": [business],
            "user_preferences": {
                "passengers": MIXED_PASSENGERS,
                "route_type": "international",
                "round_trip": True,
                "budget_scope": "all",
                "max_budget_scope": "all",
                "target_price_scope": "all",
                "cabin_arrangement": "mixed",
                "cabin_allocation": allocation,
            },
            "travel_profile": {},
        }

    def test_roundtrip_analysis_matches_business_price_before_budget(self):
        from analyzer import analyze_round_trip

        result = analyze_round_trip(
            self._analysis("MU225", 3000, 5683),
            self._analysis("JL891", 3200, 6000),
            target_price=40000,
            max_budget=41000,
            emit_diagnostics=False,
        )
        primary = result["top_combinations"][0]
        self.assertTrue(primary["mixed_cabin"])
        self.assertEqual(primary["passenger_total_price"], 40416)
        self.assertEqual(result["budget_scope"], "all")
        self.assertEqual(result["budget_price_compare_scope"], "all_passengers_roundtrip")
        self.assertEqual(result["budget_price_compare"], 40416)
        self.assertEqual(result["mixed_cabin_matching"]["stats"], {"candidates": 1, "full": 1, "partial": 0})


class MixedCabinNotifierTest(unittest.TestCase):
    def test_mixed_cabin_without_business_match_never_falls_back_to_economy_combo(self):
        from notifier import _round_trip_combinations

        economy_outbound = {"flight_combo": "MU225", "price": 3000}
        economy_return = {"flight_combo": "JL891", "price": 3200}
        analysis_result = {
            "round_trip_analysis": {
                "top_combinations": [],
                "outbound_top3": [economy_outbound],
                "return_top3": [economy_return],
                "mixed_cabin_matching": {
                    "stats": {"candidates": 1, "full": 0, "partial": 1},
                    "unpriceable": [{"mixed_cabin_reason": "商务舱价未获取"}],
                },
            },
            "return_analysis": {"top_flights": [economy_return]},
        }

        self.assertEqual(_round_trip_combinations(analysis_result), [])

    def test_payload_plan_preserves_mixed_tree_and_disclosure(self):
        from mixed_cabin import match_mixed_cabin_combinations
        from notifier import (
            _apply_passenger_pricing_to_plans,
            _passenger_pricing_rows,
            _payload_combo_plan,
        )

        combo = MixedCabinMatchingTest._combo()
        matched = match_mixed_cabin_combinations(
            [combo],
            [{"flight_combo": "MU225", "price": 5683, "data_source": "serpapi"}],
            [{"flight_combo": "JL891", "price": 6000, "data_source": "serpapi"}],
            cabin_allocation=MIXED_ALLOCATION,
            passengers=MIXED_PASSENGERS,
            route_type="international",
        )["priceable"][0]
        plan = _payload_combo_plan(
            matched,
            {"depart_date": "2026-10-01", "return_date": "2026-10-06"},
            0,
            "推荐",
        )
        _apply_passenger_pricing_to_plans([plan], MIXED_PASSENGERS, "international")
        self.assertTrue(plan["mixed_cabin"])
        self.assertEqual(plan["passenger_pricing"]["total"], 40416)
        self.assertEqual(plan["price"], 40416)
        self.assertIn("混舱", plan["tags"])
        rows = _passenger_pricing_rows(plan)
        text = " ".join(f"{name}:{value}" for name, value in rows)
        self.assertIn("商务2人+经济3人", text)
        self.assertIn("同航班两舱库存需分别验证", text)

    def test_final_payload_exposes_business_provenance_and_allocation(self):
        from unittest.mock import patch

        from mixed_cabin import match_mixed_cabin_combinations
        from notifier import build_notification_payload

        matching = match_mixed_cabin_combinations(
            [MixedCabinMatchingTest._combo()],
            [{"flight_combo": "MU225", "price": 5683, "data_source": "serpapi"}],
            [{"flight_combo": "JL891", "price": 6000, "data_source": "serpapi"}],
            cabin_allocation=MIXED_ALLOCATION,
            passengers=MIXED_PASSENGERS,
            route_type="international",
        )
        analysis = {
            "round_trip_analysis": {
                "top_combinations": matching["priceable"],
                "total_min": 40416,
                "mixed_cabin_matching": matching,
            }
        }
        subscription = {
            "id": "mixed-payload",
            "route_type": "international",
            "basic": {"passenger_count": 5, "route_type": "international"},
            "budget_scope": "all",
            "max_budget_scope": "all",
            "target_price_scope": "all",
            "preferences": {"passengers": MIXED_PASSENGERS},
            "soft_preferences": {"passengers": MIXED_PASSENGERS},
            "hard_constraints": {
                "cabin_arrangement": "mixed",
                "cabin_allocation": MIXED_ALLOCATION,
                "budget_scope": "all",
                "max_budget_scope": "all",
                "target_price_scope": "all",
            },
            "cabin_allocation": MIXED_ALLOCATION,
        }
        with (
            patch("notifier.get_last_push_price", return_value=None),
            patch("notifier.get_last_push_snapshot", return_value=None),
        ):
            payload = build_notification_payload(
                analysis,
                route_info={
                    "round_trip": True,
                    "origin": "PVG",
                    "destination": "KIX",
                    "depart_date": "2026-10-01",
                    "return_date": "2026-10-06",
                    "route_type": "international",
                },
                subscription=subscription,
            )

        self.assertEqual(payload["mixed_cabin"]["cabin_allocation"], MIXED_ALLOCATION)
        self.assertEqual(payload["mixed_cabin"]["provenance"]["business_source"], "serpapi")
        self.assertIn("税费构成未拆分", payload["mixed_cabin"]["provenance"]["price_note"])
        self.assertIn("同航班两舱库存需分别验证", payload["mixed_cabin"]["disclosure"])

    def test_pricing_rows_show_match_rate_and_business_reference(self):
        from mixed_cabin import match_mixed_cabin_combinations
        from notifier import _passenger_pricing_rows, _payload_combo_plan

        matched_result = match_mixed_cabin_combinations(
            [MixedCabinMatchingTest._combo()],
            [{"flight_combo": "MU225", "price": 5683, "airline": "中国东方航空"}],
            [{"flight_combo": "JL891", "price": 6000, "airline": "日本航空"}],
            cabin_allocation=MIXED_ALLOCATION,
            passengers=MIXED_PASSENGERS,
            route_type="international",
        )
        plan = _payload_combo_plan(
            matched_result["priceable"][0],
            {"depart_date": "2026-10-01", "return_date": "2026-10-06"},
            0,
            "推荐",
        )
        plan["mixed_cabin_matching"] = {
            **matched_result["stats"],
            "business_visible_count": matched_result["business_visible_count"],
            "business_reference": matched_result["business_reference"],
        }

        text = " ".join(f"{name}:{value}" for name, value in _passenger_pricing_rows(plan))
        self.assertIn("商务舱报价匹配:1/1", text)
        self.assertIn("本轮可见2个", text)
        self.assertIn("商务舱单程参考", text)
        self.assertIn("¥5,683", text)
        self.assertIn("非方案价", text)


class MixedCabinTrackerTest(unittest.TestCase):
    @staticmethod
    def _plan(total=40416, raw_total=40416.0, allocation=MIXED_ALLOCATION):
        return {
            "label": "方案A",
            "is_roundtrip": True,
            "mixed_cabin": True,
            "outbound_flight": {"flight_combo": "MU225", "price": 3000},
            "return_flight": {"flight_combo": "JL891", "price": 3200},
            "outbound_price": 3000,
            "return_price": 3200,
            "price": total,
            "passenger_total_price": total,
            "raw_passenger_total_price": raw_total,
            "cabin_allocation": allocation,
            "mixed_cabin_pricing": {
                "mixed_cabin": True,
                "total": total,
                "raw_total": raw_total,
                "cabin_allocation": allocation,
                "passengers": MIXED_PASSENGERS,
            },
            "passenger_pricing": {
                "mixed_cabin": True,
                "total": total,
                "raw_total": raw_total,
                "cabin_allocation": allocation,
                "passengers": MIXED_PASSENGERS,
            },
        }

    def test_tracking_uses_same_allocation_all_passenger_total(self):
        from plan_tracker import (
            MIXED_TRACKING_SCOPE,
            extract_pushed_plan_records,
            save_pushed_plans,
            track_plan_status,
        )

        record = extract_pushed_plan_records([self._plan()])["plan_a"]
        self.assertEqual(record["price_scope"], MIXED_TRACKING_SCOPE)
        self.assertEqual(record["mixed_cabin_total"], 40416.0)
        self.assertIsNone(record["unit_roundtrip_price"])

        with TemporaryDirectory() as temp_dir:
            save_pushed_plans("mixed", [self._plan()], data_dir=temp_dir)
            status = track_plan_status(
                "mixed",
                [self._plan(total=40516, raw_total=40516.0)],
                data_dir=temp_dir,
            )
        self.assertEqual(status["scope"], MIXED_TRACKING_SCOPE)
        self.assertEqual(status["price_diff"], 100.0)
        self.assertIn("混舱全员往返", status["msg"])

    def test_tracking_skips_changed_allocation(self):
        from plan_tracker import save_pushed_plans, track_plan_status

        changed = {
            "business": {"adult": 1, "child": 0, "elderly": 1, "infant": 0},
            "economy": {"adult": 1, "child": 1, "elderly": 1, "infant": 0},
        }
        with TemporaryDirectory() as temp_dir:
            save_pushed_plans("mixed", [self._plan()], data_dir=temp_dir)
            status = track_plan_status(
                "mixed",
                [self._plan(allocation=changed)],
                data_dir=temp_dir,
            )
        self.assertEqual(status["status"], "comparison_skipped")
        self.assertEqual(status["reason"], "cabin_allocation_changed")


if __name__ == "__main__":
    unittest.main()
