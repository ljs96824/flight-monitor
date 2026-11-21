"""R43: a checked allowance is not proof that the current fare includes it."""
from contextlib import redirect_stdout
from hashlib import sha256
import inspect
import io
import unittest
from unittest.mock import patch

import analyzer
import collector
import notifier
import test_basket_outcome_consumption as outcomes
from sources.aggregator import FlightAggregator
from sources.duffel_source import DuffelSource
from tests import test_baggage_eligibility_and_budget_caliber as r37
from tests import test_r38_duration_and_layover_contracts as r38


def rules(**fields):
    return {"fare_rules": {"baggage": fields}}


def checked(**fields):
    return {"extra": {"baggage_detail": {"checked": fields}}}


def listed(kind="checked", **fields):
    return {"extra": {"baggage": [{"type": kind, **fields}]}}


def duffel_offer(*bags):
    segment = {
        "marketing_carrier": {"iata_code": "MU", "name": "China Eastern"},
        "marketing_carrier_flight_number": "101",
        "origin": {"iata_code": "PVG"}, "destination": {"iata_code": "KIX"},
        "departing_at": "2026-12-01T13:00:00", "arriving_at": "2026-12-01T15:30:00",
        "duration": "PT2H30M", "passengers": [{"baggages": list(bags)}],
    }
    source = DuffelSource.__new__(DuffelSource)
    return source._parse_offer({"total_amount": "1500", "total_currency": "CNY",
                                "slices": [{"segments": [segment]}]})


class TestTrack1ExplicitPositiveEvidenceGates(unittest.TestCase):
    def test_g01_pieces_or_weight_do_not_prove_inclusion(self):
        for key in ("checked_pieces", "checked_kg"):
            for flag in ("included", "included_in_fare"):
                for extra in ({}, *({flag: value} for value in (None, "true", "false", 1, 0))):
                    flight = rules(**{key: 1}, **extra)
                    self.assertFalse(analyzer._has_free_checked_baggage(flight), flight)
                    self.assertFalse(notifier._has_free_checked_baggage(flight), flight)
        self.assertFalse(analyzer._has_free_checked_baggage(None))

    def test_g02_checked_detail_needs_boolean_true_and_allowance(self):
        for key in ("quantity", "weight_kg", "weight"):
            for flags in ({}, *({"is_free": v} for v in (False, None, "true", "false", 1, 0))):
                self.assertFalse(analyzer._has_free_checked_baggage(checked(**{key: 2}, **flags)), flags)
            self.assertTrue(analyzer._has_free_checked_baggage(checked(**{key: 2}, is_free=True)))
            for amount in (0, -1, True, float("nan"), float("inf")):
                self.assertFalse(analyzer._has_free_checked_baggage(checked(**{key: amount}, is_free=True)))

    def test_g03_list_needs_checked_type_exact_flag_and_allowance(self):
        for kind in ("checked", "hold", "checked_baggage"):
            for key in ("quantity", "weight_kg", "weight"):
                for flags in ({}, *({"is_free": v} for v in (False, None, "true", "false", 1, 0))):
                    self.assertFalse(analyzer._has_free_checked_baggage(listed(kind, **{key: 1}, **flags)), flags)
                self.assertTrue(analyzer._has_free_checked_baggage(listed(kind, **{key: 1}, is_free=True)))
        self.assertFalse(analyzer._has_free_checked_baggage(listed("carry_on", quantity=1, is_free=True)))
        self.assertFalse(analyzer._has_free_checked_baggage(listed(quantity=0, is_free=True)))

    def test_g04_normalized_quantity_does_not_upgrade_evidence(self):
        for flags in ({}, {"is_free": False}):
            flight = collector._normalize_detail_flight(r37.flight(**checked(quantity=1, **flags)))
            self.assertEqual(flight["fare_rules"]["baggage"]["checked_pieces"], 1)
            self.assertFalse(analyzer._has_free_checked_baggage(flight))
            self.assertEqual(analyzer.evaluate_checked_baggage_eligibility(flight)["state"], "pending_confirmation")


class TestTrack2DuffelAdapterAndFeeRoutingContracts(r37.ContractBase):
    def test_g05_duffel_preserves_items_and_marks_only_positive_allowances(self):
        cases = [
            ({"type": "checked", "quantity": 1}, True), ({"type": "carry_on", "quantity": 1}, True),
            ({"quantity": 0, "weight": 20}, False), ({"quantity": None, "weight": 20}, True),
            ({"weight": 5, "weight_unit": "kg"}, True), ({"quantity": -1}, False),
            ({"quantity": True}, False), ({"quantity": float("inf")}, False),
            ({"quantity": float("nan")}, False), ({"weight": False}, False), ({}, False),
        ]
        items = duffel_offer(*(bag for bag, _ in cases))["extra"]["baggage"]
        self.assertEqual(len(items), len(cases))
        for item, (bag, included) in zip(items, cases):
            self.assertIs(item.get("is_free"), included)
            self.assertEqual({k: v for k, v in item.items() if k != "is_free"},
                             {"type": bag.get("type", ""), "quantity": bag.get("quantity", 0),
                              "weight": bag.get("weight"), "weight_unit": bag.get("weight_unit")})

    def test_g06_full_and_list_only_duffel_paths_remain_eligible(self):
        parsed = duffel_offer({"type": "checked", "quantity": 1})
        self.assertIs(parsed["extra"]["baggage"][0].get("is_free"), True)
        for extra in (parsed["extra"], {"baggage": parsed["extra"]["baggage"]}):
            flight = collector._normalize_detail_flight({**parsed, "extra": extra})
            self.assertEqual(analyzer.evaluate_checked_baggage_eligibility(flight)["state"], "included_in_fare")

    def test_g07_known_fee_is_counted_instead_of_zero(self):
        for data in (rules(checked_pieces=1), listed(quantity=1)):
            flight = r37.paid_flight(100, **data)
            actual = analyzer.evaluate_checked_baggage_eligibility(flight, passengers=r37.PASSENGERS)
            self.assertEqual(actual["state"], "paid_allowed_known_fee")
            self.assertEqual(self.fee(flight)["amount"], 300)

    def test_g08_unverified_pieces_only_enter_references(self):
        for data in (rules(checked_pieces=1), listed(quantity=1)):
            result = self.analyze([r37.flight(**data)])
            self.assertEqual(result["roundtrip_candidate_flights"], [])
            self.assertEqual(result["recommended_plans"], [])
            self.assertEqual([f["filter_reason_code"] for f in result["reference_flights"]],
                             ["baggage_pending_confirmation"])


class TestTrack3LowPriceContradictionAndNotifierContracts(r37.ContractBase):
    def test_g09_unverified_allowance_cannot_supply_a_low_price(self):
        result = self.analyze([r37.flight(**rules(checked_pieces=1))], budget_strategy="low_price_alert")
        self.assertEqual(result["all_flights"], [])
        self.assertEqual(result["low_price_alert_state"], "unknown")

    def test_g10_vetoes_and_zero_detail_placeholder(self):
        proof = {"type": "checked", "quantity": 1, "is_free": True}
        for flight in ({"extra": {"baggage": [proof], "checked_baggage_allowed": False}},
                       {**checked(quantity=1, is_free=False), "baggage": [proof]},
                       rules(included=True, checked_pieces=0, checked_kg=0),
                       {**rules(included_in_fare=False), "baggage": [proof]}):
            self.assertFalse(analyzer._has_free_checked_baggage(flight), flight)
        self.assertTrue(analyzer._has_free_checked_baggage({**checked(quantity=0, is_free=False), "baggage": [proof]}))

    def test_g11_family_advice_and_scoring_do_not_promise_unverified_baggage(self):
        self.assertNotIn("含托运行李", "".join(notifier.format_baggage(listed(quantity=1)["extra"])))
        flight = r37.flight(**rules(checked_pieces=1))
        self.assertEqual(analyzer._flight_baggage_score(flight), 60)
        kept, _, _ = self.filter([flight], need_baggage="optional", companions="with_child")
        self.assertEqual(len(kept), 1)
        self.assertNotIn("含免费托运", "".join(kept[0]["preference_notes"]))
        self.assertIn("托运行李需官网确认", kept[0]["preference_penalties"])

    def test_g12_enrichment_allowance_is_not_proof_for_cheaper_juhe_quote(self):
        hi = duffel_offer({"type": "checked", "quantity": 1})
        self.assertIs(hi["extra"]["baggage"][0].get("is_free"), True)
        lo = r37.flight(900, price_source="juhe", data_source="juhe", source="juhe")
        search = outcomes.OfflineSource("juhe", {"source_status": "success", "flights": [lo]})
        enrich = outcomes.OfflineSource("duffel", {"source_status": "success", "flights": [hi]})
        search.role, search.weight = "primary", 1.0
        enrich.role, enrich.weight = "enrichment", 0.0
        agg = FlightAggregator([search], [enrich], route_type="international")
        inputs = [outcomes.make_outcome(s, s.result, destination="KIX", depart_date="2026-12-01")
                  for s in (search, enrich)]
        with patch.object(agg, "_ordered_search_sources", return_value=[search]), redirect_stdout(io.StringIO()):
            result = agg.collect_from_outcomes("PVG", "KIX", "2026-12-01", outcomes=inputs,
                                               cabin_classes=["economy"], route_type="international")
        self.assertEqual(len(result["flights"]), 1)
        merged = result["flights"][0]
        self.assertEqual(merged["price"], 900)
        self.assertEqual(merged["baggage_applicability"], "flight_number_match_only")
        self.assertFalse(analyzer._has_free_checked_baggage(merged))
        self.assertEqual(analyzer.evaluate_checked_baggage_eligibility(merged)["subreason"], "cross_offer_unverified")


class TestTrack4EvergreenCompatibilityGuards(unittest.TestCase):
    def test_g13_explicit_inclusion_remains_valid_without_add_on_purchase(self):
        for data in (rules(included=True), rules(included=True, checked_pieces=1),
                     rules(included_in_fare=True, checked_kg=20), checked(quantity=1, is_free=True),
                     listed(quantity=1, is_free=True)):
            for flag in ({}, {"can_purchase": False}):
                result = analyzer.evaluate_checked_baggage_eligibility({**data, **flag})
                self.assertEqual(result["state"], "included_in_fare", data)

    def test_g14_analyzer_and_renderer_anchors_remain_exact(self):
        r38.TestTrack4FrozenAndAstGuards("test_g23_ast_anchors_are_unchanged").test_g23_ast_anchors_are_unchanged()
        for fn, digest in (
            (analyzer.parse_flight_time, "9a55d7e7d427adf2469b269546262ebc3d3264ec7f7cae4b7df537f1a4177297"),
            (analyzer.match_time_preference, "2e21c6f1cb34a5300b194c59b167b52753e321f2d7e696685c153f64c98af93f"),
        ):
            self.assertEqual(sha256(inspect.getsource(fn).encode()).hexdigest(), digest)

    def test_g15_three_frozen_carriers_and_normal_roundtrip(self):
        case = r37.TestTrack4FrozenAndAstGuards("test_g24_three_frozen_carriers_and_normal_roundtrip_remain_exact")
        case.test_g24_three_frozen_carriers_and_normal_roundtrip_remain_exact()
