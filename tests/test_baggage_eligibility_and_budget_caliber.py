"""R37: four orthogonal tracks for quote-bound baggage and budget scopes."""
import ast
import copy
import hashlib
import io
import json
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import analyzer
import config_loader
import constraint_summary
import main
import notifier
import storage
import sources.aggregator as aggregator
import test_basket_outcome_consumption as outcome_fixtures
from tests import test_return_phase_processing as return_phase


PASSENGERS = {"adult": 2, "child": 1, "elderly": 0, "infant": 0}
MISSING_PRIMARY = "暂无已确认满足托运要求的方案"


def flight(price=1000, **updates):
    value = return_phase.flight(True, price)
    value.update(departure_date="2026-12-01", departure_time="13:00", arrival_time="15:30",
                 collected_at="2026-09-01T09:00:00+08:00", source="synthetic",
                 data_source="synthetic", price_source="synthetic")
    value["segments"][0].update(dep_time="2026-12-01 13:00", arr_time="2026-12-01 15:30", id="s1")
    value.update(updates)
    return value


def free_flight(**updates):
    return flight(fare_rules={"baggage": {"included": True, "checked_pieces": 1}}, **updates)


def paid_flight(amount=100, **updates):
    spec = {"amount": amount, "currency": "CNY", "fee_unit": "per_passenger",
            "itinerary_scope": "leg", "applicable_to_current_quote": True, "included_in_fare": False}
    return flight(checked_baggage_fee_spec=spec, **updates)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 9, 1, 9, 0, 0, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)


class ContractBase(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for module, name, value in (
            (storage, "DB_PATH", self.root / "prices.db"),
            (main, "ANALYSIS_LOG", self.root / "analysis.jsonl"),
            (config_loader, "RUNTIME_CONFIG_PATH", self.root / "nonexistent-runtime.yaml"),
            (analyzer, "datetime", FixedDatetime),
            (notifier, "datetime", FixedDatetime),
        ):
            self.stack.enter_context(patch.object(module, name, value))
        self.stack.enter_context(patch.object(main, "log_signal", return_value={"logged": False}))
        self.stack.enter_context(patch.object(notifier, "get_last_push_price", return_value=None))
        self.stack.enter_context(patch.object(notifier, "get_last_push_snapshot", return_value=None))
        self.stack.enter_context(patch.object(notifier, "track_plan_status", return_value={}))

    def api(self, name="evaluate_checked_baggage_eligibility"):
        self.assertTrue(hasattr(analyzer, name), "R37 public contract: " + name)
        return getattr(analyzer, name)

    def preferences(self, **overrides):
        return {"need_baggage": "required", "max_budget": None, "target_price": None,
                "route_type": "international", "red_eye": "accept", "passengers": dict(PASSENGERS),
                **overrides}

    def filter(self, flights, **overrides):
        with redirect_stdout(io.StringIO()):
            return analyzer._apply_user_preferences(copy.deepcopy(flights), self.preferences(**overrides))

    def analyze(self, flights, **overrides):
        with redirect_stdout(io.StringIO()):
            return analyzer.analyze_all_flights(copy.deepcopy(flights), user_preferences=self.preferences(**overrides))

    def fee(self, f, scope="all_passengers_oneway", passengers=PASSENGERS, **kwargs):
        return self.api("resolve_checked_baggage_fee_in_scope")(
            f, passengers=passengers, visible_scope=scope, **kwargs)


class TestTrack1BaggageEligibility(ContractBase):
    def test_g01_no_carry_on_or_zero_piece_false_positives(self):
        self.api()
        for baggage in ([{"type": "carry_on", "quantity": 1}], [{"type": "checked", "quantity": 0}],
                        "0件", "仅手提", "无免费托运", "0kg", "no checked", "hand baggage only"):
            f = flight(extra={"baggage": baggage}, has_baggage_info=True)
            self.assertFalse(analyzer._has_free_checked_baggage(f))
            self.assertFalse(notifier._has_free_checked_baggage(f))
            self.assertNotIn("已含托运", notifier._pushplus_baggage_line_for_flight(f))
            self.assertNotIn("含托运行李", "".join(notifier.format_baggage(f["extra"])))

    def test_g02_matched_included_baggage_enters_main_pool(self):
        evaluate = self.api()
        f = free_flight()
        before = copy.deepcopy(f)
        self.assertEqual(evaluate(f, passengers=PASSENGERS)["state"], "included_in_fare")
        self.assertEqual(f, before)
        result = self.analyze([f])
        self.assertEqual(len(result["roundtrip_candidate_flights"]), 1)
        self.assertEqual(len(result["reference_flights"]), 0)

    def test_g03_paid_known_service_is_not_free_baggage(self):
        evaluate = self.api()
        f = paid_flight()
        self.assertEqual(evaluate(f, passengers=PASSENGERS)["state"], "paid_allowed_known_fee")
        kept, excluded, summary = self.filter([f])
        self.assertEqual((len(kept), excluded, summary["reference_flights"]), (1, [], []))
        self.assertFalse(notifier._has_free_checked_baggage(kept[0]))
        self.assertIn("付费", notifier._pushplus_baggage_line_for_flight(kept[0]))

    def test_g04_explicit_baggage_prohibition_is_excluded(self):
        self.api()
        f = flight(extra={"checked_baggage_allowed": False}, has_baggage_info=True)
        kept, excluded, summary = self.filter([f])
        self.assertEqual(kept, [])
        self.assertEqual(excluded[0]["filter_reason_code"], "need_baggage")
        self.assertEqual(summary["reference_flights"], [])

    def test_g05_collector_empty_shell_is_unknown_not_ineligible(self):
        evaluate = self.api()
        f = flight(extra={}, has_baggage_info=False,
                   fare_rules={"baggage": {"checked_pieces": 0, "checked_kg": None, "extra_bag_price": None}})
        self.assertEqual(evaluate(f)["subreason"], "rule_unknown")
        result = self.analyze([f])
        self.assertEqual(result["roundtrip_candidate_flights"], [])
        self.assertEqual(result["reference_flights"][0]["filter_reason_code"], "baggage_pending_confirmation")

    def test_g06_estimated_lcc_fee_is_not_a_known_fee(self):
        evaluate = self.api()
        for value in ("约¥50-150", "约50~150", None):
            f = flight(fare_rules={"baggage": {"included": False, "extra_bag_price": value}},
                       has_baggage_info=True)
            self.assertEqual(evaluate(f, passengers=PASSENGERS)["subreason"], "paid_fee_unknown")
            self.assertEqual(self.filter([f])[0], [])

    def test_g07_domestic_inference_is_reference_only(self):
        evaluate = self.api()
        f = flight(fare_rules={"source": "国内标准规则推断", "baggage": {"included": True, "checked_kg": 20}})
        result = evaluate(f)
        self.assertEqual(result["subreason"], "domestic_rule_inferred")
        self.assertEqual(result["message"], "当前报价的托运行李权益未确认；国内常规规则仅供参考，请以该票价产品的支付页票规为准。")
        kept, _, summary = self.filter([f])
        self.assertEqual(kept, [])
        text = summary["reference_flights"][0]["reference_reason"]
        for forbidden in ("含20kg", "已确认托运"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(notifier._pushplus_baggage_line_for_flight(summary["reference_flights"][0]), "行李:权益未确认(支付页核实)")

    def test_g08_losing_offer_proofs_never_attach_to_winning_quote(self):
        evaluate = self.api()
        low = flight(900, price_source="juhe", data_source="juhe", offer_id="off_low")
        high = paid_flight(100, offer_id="off_hi", quote_offer_id="off_hi", baggage_offer_id="off_hi",
                           baggage_offer_matched=True, baggage_applicability="quote_matched")
        high["price"] = 1500
        high["extra"] = {"baggage": [{"type": "checked", "quantity": 1}],
                         "changeable": True, "offer_id": "off_hi"}
        high["baggage_services"] = [{"offer_id": "off_hi", "amount": 100}]
        before = copy.deepcopy([low, high])
        agg = aggregator.FlightAggregator([], [])
        for values in ([low, high], [high, low]):
            merged = agg._merge_flights([{"source": "juhe", "flights": [values[0]]},
                                         {"source": "duffel", "flights": [values[1]]}])[0]
            self.assertEqual(merged["price"], 900)
            self.assertEqual(merged["offer_id"], "off_low")
            self.assertIs(merged["baggage_offer_matched"], False)
            self.assertEqual(merged["baggage_applicability"], "flight_number_match_only")
            self.assertNotIn("offer_id", merged["extra"])
            self.assertEqual(evaluate(merged, passengers=PASSENGERS)["subreason"], "cross_offer_unverified")
        self.assertEqual([low, high], before)
        search = outcome_fixtures.OfflineSource("juhe", {"source_status": "success", "flights": [copy.deepcopy(low)]})
        enrichment = outcome_fixtures.OfflineSource("duffel", {"source_status": "success", "flights": [copy.deepcopy(high)]})
        search.role, search.weight = "primary", 1.0
        enrichment.role, enrichment.weight = "enrichment", 0.0
        agg = aggregator.FlightAggregator([search], [enrichment], route_type="international")
        outcomes = [outcome_fixtures.make_outcome(source, copy.deepcopy(source.result),
                     destination="KIX", depart_date="2026-12-01") for source in (search, enrichment)]
        original = copy.deepcopy([outcome.result for outcome in outcomes])
        with patch.object(agg, "_ordered_search_sources", return_value=[search]), redirect_stdout(io.StringIO()):
            result = agg.collect_from_outcomes("PVG", "KIX", "2026-12-01", outcomes=outcomes,
                                               cabin_classes=["economy"], route_type="international")
        self.assertEqual([outcome.result for outcome in outcomes], original)
        self.assertEqual(evaluate(result["flights"][0], passengers=PASSENGERS)["subreason"], "cross_offer_unverified")
        mismatch = free_flight(quote_offer_id="low", baggage_offer_id="high")
        self.assertEqual(evaluate(mismatch)["subreason"], "cross_offer_unverified")
        partial = free_flight(segment_ids=["s2"])
        self.assertEqual(evaluate(partial)["subreason"], "cross_offer_unverified")

    def test_g09_empty_main_pool_never_promotes_pending_baggage(self):
        self.api()
        result = self.analyze([flight()])
        self.assertEqual(result["recommended_plans"], [])
        self.assertEqual(result["recommendations"], [])
        self.assertEqual(result["no_primary_reason"], MISSING_PRIMARY)
        with redirect_stdout(io.StringIO()):
            diagnosis = analyzer.build_no_result_diagnosis(result["reference_flights"], constraints=self.preferences())
        self.assertEqual(diagnosis["reason"], MISSING_PRIMARY)
        references = notifier._no_result_candidate_flights(result, None, None, False)
        self.assertEqual(len(references), 1)
        self.assertEqual(notifier._pushplus_baggage_line_for_flight(references[0]), "行李:权益未确认(支付页核实)")
        with redirect_stdout(io.StringIO()):
            payload = notifier.build_notification_payload(
                analysis_result=result,
                route_info={**self.preferences(), "origin": "PVG", "destination": "KIX", "depart_date": "2026-12-01"})
            _, email = notifier.render_email(payload)
            detail = notifier.render_detail_html(payload)
            push = notifier.prepare_push_render(notifier.render_pushplus_sections(payload)).content
        self.assertEqual(payload["recommended_plans"], [])
        for body in (email, detail, push):
            self.assertIn(MISSING_PRIMARY, body)

    def test_g10_optional_family_preferences_are_not_mutated_or_hard_filtered(self):
        self.api("is_checked_baggage_required")
        for mode in ("unknown", "optional", "not_needed"):
            prefs = self.preferences(need_baggage=mode)
            before = copy.deepcopy(prefs)
            kept, excluded, _ = analyzer._apply_user_preferences([flight()], prefs)
            self.assertEqual(prefs, before)
            self.assertEqual((len(kept), excluded), (1, []))
            self.assertEqual(kept[0]["price"], 1000)
        self.assertEqual(self.filter([flight()])[0], [])


class TestTrack2BudgetCaliber(ContractBase):
    def preferences(self, **overrides):
        return super().preferences(need_baggage="not_needed", **overrides)

    def test_g11_single_trip_all_budget_is_not_a_unit_budget(self):
        self.api("resolve_effective_budget_scopes")
        kept, excluded, _ = self.filter([flight(1200)], max_budget=3000, max_budget_scope="all", passengers={"adult": 3})
        self.assertEqual(kept, [])
        self.assertIn("全员单程", excluded[0]["exclude_reason"])
        self.assertEqual(len(self.filter([flight(1200)], max_budget=3000, max_budget_scope="per_person")[0]), 1)

    def test_g12_four_scopes_and_passenger_discounts_match_notification(self):
        self.api("resolve_effective_budget_scopes")
        people = {**PASSENGERS, "infant": 1}
        for route, factor in (("domestic", 2.6), ("international", 2.85)):
            for rt in (False, True):
                for scope in ("per_person", "all"):
                    visible = ("all_passengers_" if scope == "all" else "per_person_") + ("roundtrip" if rt else "oneway")
                    expected = 1000 * (2 if rt else 1) * (factor if scope == "all" else 1)
                    actual = analyzer.price_in_scope(1000, people, scope=visible, route_type=route, round_trip=rt)
                    plan = {"main_flight": flight(), "outbound_flight": flight(), "return_flight": flight()}
                    self.assertAlmostEqual(actual, expected)
                    self.assertEqual(notifier._plan_price_in_budget_scope(plan, None, people, route, rt, visible), round(expected))

    def test_g13_business_and_mixed_force_all_even_on_oneway(self):
        resolve = self.api("resolve_effective_budget_scopes")
        for arrangement in ("business_all", "mixed"):
            prefs = self.preferences(cabin_arrangement=arrangement, max_budget_scope="per_person", target_price_scope="per_person")
            before = copy.deepcopy(prefs)
            self.assertEqual(resolve(prefs), ("all", "all"))
            self.assertEqual(notifier._payload_budget_scopes({"preferences": prefs}), ("all", "all"))
            self.assertEqual(prefs, before)
            analysis = {"roundtrip_candidate_flights": [flight()], "user_preferences": prefs}
            with redirect_stdout(io.StringIO()):
                result = analyzer.analyze_round_trip(analysis, analysis, max_budget=6000, target_price=5000, emit_diagnostics=False)
            self.assertEqual((result["budget_scope"], result["target_price_scope"]), ("all", "all"))
            self.assertEqual(result["budget_price_compare"], 5500)
        self.assertEqual(notifier._plan_price_in_budget_scope(
            {"mixed_cabin": True, "raw_passenger_total_price": 3500}, None, PASSENGERS,
            "international", False, "all_passengers_oneway"), 3500)

    def test_g14_maximum_and_target_scopes_are_independent(self):
        self.api("resolve_effective_budget_scopes")
        kept, _, _ = self.filter([flight(1100)], max_budget=1500, max_budget_scope="per_person",
                                  target_price=3000, target_price_scope="all", passengers={"adult": 3})
        self.assertEqual(kept[0]["budget_compare_price"], 1100)
        self.assertEqual(kept[0]["target_compare_price"], 3300)
        self.assertFalse(kept[0]["at_or_below_target"])
        self.assertNotIn("低于理想入手价", kept[0]["preference_notes"])
        prefs = self.preferences(max_budget_scope="per_person", target_price_scope="all", passengers={"adult": 3})
        analysis = {"roundtrip_candidate_flights": [flight()], "user_preferences": prefs}
        with redirect_stdout(io.StringIO()):
            result = analyzer.analyze_round_trip(analysis, analysis, max_budget=3000, target_price=5000, emit_diagnostics=False)
        self.assertEqual(result["budget_price_compare"], 2000)
        self.assertNotIn("达到理想价", str(result["advice"]))

    def test_g15_downstream_summary_risk_and_single_card_use_same_scope(self):
        self.api("resolve_effective_budget_scopes")
        prefs = self.preferences(max_budget=4000, target_price=3000, max_budget_scope="all",
                                 target_price_scope="all", passengers={"adult": 3})
        result = self.analyze([flight(1100)], **{k: v for k, v in prefs.items() if k != "need_baggage"})
        self.assertEqual(result["budget_price_compare"], 3300)
        self.assertEqual(result["ideal_price_pp_oneway"], 1000)
        f = result["roundtrip_candidate_flights"][0]
        self.assertNotIn("接近理想", notifier._flight_status_tags(f, prefs, result))
        self.assertNotIn("可购买前验证", notifier._human_recommendation_text(f, prefs, result))
        self.assertEqual(notifier._execution_target_price(f, prefs, result), 1000)

    def test_g16_oneway_rejection_diagnosis_and_summary_never_double(self):
        self.api("resolve_effective_budget_scopes")
        prefs = self.preferences(round_trip=False, max_budget=3000, max_budget_scope="all", passengers={"adult": 3})
        with redirect_stdout(io.StringIO()):
            rows = analyzer._budget_excluded_candidate_rows(
                {"budget_excluded_candidates": [{"outbound_price": 1100, "return_price": 1100}]}, prefs)
        self.assertEqual(rows[0]["price"], 3300)
        self.assertEqual(rows[0]["price_scope"], "all_passengers_oneway")
        summary = constraint_summary.build_constraint_summary(prefs, 3000)
        self.assertIn("全员单程", "".join(summary))


class TestTrack3OrthogonalBaggageFeeAndScope(ContractBase):
    def test_g17_return_only_budget_clear_does_not_clear_baggage_requirement(self):
        self.api()
        self.assertEqual(main.subscription_preferences(return_phase.subscription())["need_baggage"], "unknown")
        placements = (
            (None, "need_baggage"), (None, "baggage"),
            ("hard_constraints", "baggage"), ("hard_constraints", "need_baggage"),
            ("constraints", "baggage"), ("constraints", "need_baggage"),
            ("preferences", "need_baggage"), ("preferences", "baggage"),
            ("soft_preferences", "need_baggage"), ("soft_preferences", "baggage"),
        )
        for index, (container, key) in enumerate(placements):
            sub = return_phase.subscription()
            for later_container, later_key in placements[index:]:
                target = sub if later_container is None else sub.setdefault(later_container, {})
                target[later_key] = "required" if (later_container, later_key) == (container, key) else "optional"
            self.assertEqual(main.subscription_preferences(sub)["need_baggage"], "required")
            for earlier_container, earlier_key in placements[:index]:
                target = sub if earlier_container is None else sub.setdefault(earlier_container, {})
                target[earlier_key] = None if index % 2 else ""
            self.assertEqual(main.subscription_preferences(sub)["need_baggage"], "required")
        for container, key in ((None, "need_baggage"), ("hard_constraints", "baggage"),
                               ("soft_preferences", "need_baggage")):
            sub = return_phase.subscription()
            target = sub if container is None else sub[container]
            target[key] = "required"
            sub.update(max_budget=1, target_price=1)
            sub["hard_constraints"].update(max_budget=1, target_price=1)
            sub["soft_preferences"].update(max_budget=1, target_price=1)
            context = main._return_phase_context(sub, datetime(2026, 10, 2).date())
            mapped = main._return_phase_preferences(sub, context)
            combined = {**mapped["preferences"], **mapped["constraints"]}
            self.assertTrue(analyzer.is_checked_baggage_required(combined))
            self.assertIsNone(combined.get("max_budget"))
            self.assertIsNone(combined.get("target_price"))
            def analyze_candidate(candidate):
                with redirect_stdout(io.StringIO()):
                    return analyzer.analyze_all_flights(
                        [candidate], user_preferences=mapped["preferences"], hard_constraints=mapped["constraints"])
            free = analyze_candidate(free_flight())
            self.assertEqual(len(free["roundtrip_candidate_flights"]), 1)
            self.assertEqual(free["roundtrip_candidate_flights"][0]["checked_baggage_eligibility"]["state"], "included_in_fare")
            paid = analyze_candidate(paid_flight())
            self.assertEqual(len(paid["roundtrip_candidate_flights"]), 1)
            self.assertEqual(paid["roundtrip_candidate_flights"][0]["checked_baggage_eligibility"]["state"], "paid_allowed_known_fee")
            unknown = analyze_candidate(flight())
            self.assertEqual(unknown["roundtrip_candidate_flights"], [])
            self.assertEqual(unknown["reference_flights"][0]["filter_reason_code"], "baggage_pending_confirmation")
            prohibited = analyze_candidate(flight(extra={"checked_baggage_allowed": False}, has_baggage_info=True))
            self.assertEqual(prohibited["roundtrip_candidate_flights"], [])
            self.assertEqual(prohibited["reference_flights"], [])
            self.assertEqual(prohibited["excluded_flights"][0]["filter_reason_code"], "need_baggage")

    def test_g18_s1_counts_seated_people_not_ticket_discounts(self):
        self.api()
        before = paid_flight()
        f = copy.deepcopy(before)
        fee = self.fee(f)
        self.assertEqual(fee["amount"], 300)
        self.assertIn("按每名占座乘客各补购1份估算", fee["disclosure"])
        self.assertEqual(f, before)
        fee = self.fee(f, passengers={**PASSENGERS, "infant": 1})
        self.assertEqual(fee["amount"], 300)
        self.assertIn("婴儿", fee["infant_note"])
        for people in ({"adult": 0}, {"adult": 0, "infant": 1}, {"adult": True}, {"adult": -1},
                       {"adult": 1.5}, {"adult": "2"}):
            value = self.fee(f, passengers=people)
            self.assertFalse(value["calculable"])
            self.assertIsNone(value["amount"])
        self.assertFalse(self.fee(f, passengers=None)["calculable"])
        self.assertEqual(self.fee(f, "per_person_oneway", passengers=None)["amount"], 100)

    def test_g19_partial_service_only_charges_missing_person_and_per_person_is_unknown(self):
        self.api()
        services = [
            {"included_in_fare": True, "passenger_ids": ["p1", "p2"], "itinerary_scope": "leg"},
            {"included_in_fare": False, "passenger_ids": ["p3"], "itinerary_scope": "leg",
             "amount": 100, "currency": "CNY", "quantity": 1},
        ]
        f = flight(passenger_ids=["p1", "p2", "p3"], baggage_services=services)
        self.assertEqual(self.fee(f)["amount"], 100)
        self.assertIsNone(self.fee(f, "per_person_oneway")["amount"])
        kept, _, summary = self.filter([f], max_budget=2000, max_budget_scope="per_person")
        self.assertEqual(kept, [])
        self.assertEqual(summary["reference_flights"][0]["checked_baggage_eligibility"]["subreason"], "paid_fee_scope_unknown")
        kept, _, _ = self.filter([f], max_budget=4000, max_budget_scope="all", target_price=2000, target_price_scope="per_person")
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["budget_compare_price"], 2850)
        self.assertFalse(kept[0]["at_or_below_target"])

    def test_g20_group_roundtrip_and_multisegment_services_are_not_multiplied(self):
        self.api()
        service = {"service_id": "bag-package", "passenger_scope": "all_seated_passengers",
                   "itinerary_scope": "roundtrip", "included_in_fare": False,
                   "amount": 260, "quantity": 1, "currency": "CNY"}
        f = flight(baggage_services=[service], segments=[{"id": "s1"}, {"id": "s2"}])
        self.assertEqual(self.fee(f, "all_passengers_roundtrip", round_trip=True, return_flight=f)["amount"], 260)
        self.assertFalse(self.fee(f, "per_person_roundtrip", round_trip=True, return_flight=f)["calculable"])
        leg = copy.deepcopy(service)
        leg["itinerary_scope"] = "oneway_itinerary"
        self.assertEqual(self.fee(flight(baggage_services=[leg], segments=[{"id": "s1"}, {"id": "s2"}]))["amount"], 260)
        expensive_bag = paid_flight(300, flight_combo="MU100")
        expensive_bag["price"] = 900
        included = free_flight(flight_combo="MU200")
        prefs = self.preferences(round_trip=True, max_budget_scope="all")
        outbound = {"roundtrip_candidate_flights": [expensive_bag, included], "user_preferences": prefs}
        inbound = {"roundtrip_candidate_flights": [free_flight(flight_combo="MU300")], "user_preferences": prefs}
        with redirect_stdout(io.StringIO()):
            result = analyzer.analyze_round_trip(outbound, inbound, emit_diagnostics=False)
        self.assertEqual(result["top_combinations"][0]["outbound"]["flight_combo"], "MU200")

    def test_g21_unknown_currency_unit_range_or_included_status_never_becomes_zero(self):
        self.api()
        for update in ({"currency": "USD"}, {"fee_unit": "per_5kg"}, {"itinerary_scope": None},
                       {"included_in_fare": None}, {"amount": "50-150"}, {"amount": float("inf")}):
            f = paid_flight()
            f["checked_baggage_fee_spec"].update(update)
            result = self.fee(f)
            self.assertFalse(result["calculable"])
            self.assertIsNone(result["amount"])
            self.assertEqual(analyzer.evaluate_checked_baggage_eligibility(f, passengers=PASSENGERS)["state"], "pending_confirmation")

    def test_g22_unknown_fee_never_enters_current_low_price_offer_pool(self):
        self.api()
        result = self.analyze([paid_flight(currency="CNY")], max_budget=4000, max_budget_scope="all")
        self.assertEqual(len(result["roundtrip_candidate_flights"]), 1)
        bad = paid_flight()
        bad["checked_baggage_fee_spec"].pop("currency")
        result = self.analyze([bad], max_budget=4000, max_budget_scope="all", budget_strategy="low_price_alert")
        self.assertEqual(result["roundtrip_candidate_flights"], [])
        self.assertEqual(result["all_flights"], [])
        self.assertTrue(result["reference_flights"][0]["reference_only"])


class TestTrack4FrozenAndAstGuards(unittest.TestCase):
    def test_g23_locked_renderer_ast_and_international_fare_rule_nonmutation(self):
        expected = {
            "build_notification_payload": "6ec18a811b7cb6353af949b74237ffae1352147eb7cd6c170141ae75942d6912",
            "render_email": "429fabb2eea4a9b88cc0c653843aa372a8e0faad9f8e2865ad37e1a55cd7471e",
            "render_detail_html": "2586b469355356e88a09a6c4872c348a8c70129864d9e93b7515280c4100cd29",
            "render_pushplus_sections": "666472ec424df694891027abf2a125e47a8c0ba7d7d92b0400d64419a2ee56ef",
        }
        source = Path(notifier.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in expected:
                text = ast.get_source_segment(source, node)
                self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), expected.pop(node.name))
        self.assertEqual(expected, {})
        f = flight()
        before = copy.deepcopy(f)
        self.assertIsInstance(analyzer.verify_fare_rules(f, {}), dict)
        self.assertEqual(f, before)

    def test_g24_three_frozen_carriers_and_normal_roundtrip_remain_exact(self):
        path = Path(__file__).resolve().parent / "fixtures/frozen_email/economy_payload.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            config_loader, "RUNTIME_CONFIG_PATH", Path(tmp) / "nonexistent-runtime.yaml"
        ), redirect_stdout(io.StringIO()):
            _, email = notifier.render_email(copy.deepcopy(payload))
            push = notifier.prepare_push_render(notifier.render_pushplus_sections(copy.deepcopy(payload))).content
            detail = notifier.render_detail_html(copy.deepcopy(payload))
        specs = [
            (email, 53307, "1dd353d2b7bcafc02087957ccccb6661478e20472a22fb3e15dae6a56473ee6b"),
            (push, 5552, "2ac0e80d9f82dbe7ebccf524c0832f09d42d916212b6178316b0b7bcd60574ff"),
            (detail, 54248, "e31b8728b8d0bc05c9bbd4bd99c299aaf6dca7b66674b6c560ec089b1681ffa6"),
        ]
        for text, size, sha in specs:
            b = text.encode("utf-8")
            self.assertEqual((len(b), hashlib.sha256(b).hexdigest()), (size, sha))
        case = return_phase.ReturnPhaseProcessingTest("test_normal_roundtrip_baseline")
        case.setUpClass()
        try:
            setup_complete = False
            try:
                case.setUp()
                setup_complete = True
                case.test_normal_roundtrip_baseline()
            finally:
                try:
                    if setup_complete:
                        case.tearDown()
                finally:
                    case.doCleanups()
        finally:
            case.tearDownClass()


if __name__ == "__main__":
    unittest.main()

