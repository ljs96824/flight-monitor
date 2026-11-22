"""R45: separate permission to refund/change a quote from the cost of exercising it."""
import ast
from contextlib import closing, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import analyzer
import config_loader
import main
import notifier
import storage
import test_basket_outcome_consumption as outcomes
import test_legacy_notification_renderer_retirement as renderer
import test_verify_fare_rules_characterization as characterization
from sources import aggregator, duffel_source, fare_rules
from tests import test_baggage_eligibility_and_budget_caliber as baggage
from tests import test_low_price_same_caliber_history as history
from tests import test_r40_same_day_combo_contracts as meeting
from tests import test_r41_legitimate_empty_response_contracts as collection
from tests import test_r44_roundtrip_full_pool_selection_contracts as pairing


ROOT = Path(__file__).resolve().parents[1]
PENDING = "refund_change_pending_confirmation"
MISSING = "暂无已确认满足退改要求的方案"
REFERENCE = "退改资格待确认的参考最低报价"
VERIFY = "退改:资格待确认(支付页核实)"
STRICT = "low_price_alert_v2_refund_strict"


def rules(refundable=True, changeable=True, rf=None, cf=None):
    return {"refund": {"allowed": refundable, "fee": rf}, "change": {"allowed": changeable, "fee": cf}}


def bound(row, **terms):
    row.update(fare_rules=rules(**terms), refund_change_offer_matched=True)
    return row


def carriers(payload):
    with redirect_stdout(io.StringIO()):
        return (notifier.render_email(deepcopy(payload))[1],
                notifier.prepare_push_render(notifier.render_pushplus_sections(deepcopy(payload))).content,
                notifier.render_detail_html(deepcopy(payload)))


class Contracts(baggage.ContractBase):
    def require_attr(self, module, name):
        self.assertTrue(hasattr(module, name), f"missing public contract {module.__name__}.{name}")
        return getattr(module, name)

    def preferences(self, **overrides):
        return super().preferences(**{"need_baggage": "unknown", "refund_flexibility": "required",
                                      "passengers": {"adult": 2}, **overrides})


class TestTrack1SourceAndCrossOfferRefundTruth(Contracts):
    def test_g01_only_strict_bools_and_zero_is_not_missing(self):
        for value in ("false", "true", 0, 1, None, False, True):
            with self.subTest(value=value):
                result = fare_rules.standardize_fare_rules(
                    {"refund_change": {"refundable": value, "changeable": value, "refund_fee": 0, "change_fee": 0}}, "MU101")
                for kind in ("change", "refund"):
                    self.assertIs(result[kind]["allowed"], value if type(value) is bool else None)
                    self.assertEqual(result[kind]["fee"], 0)

    def test_g02_duffel_missing_penalty_is_not_free(self):
        source = duffel_source.DuffelSource.__new__(duffel_source.DuffelSource)
        segment = {"marketing_carrier": {"iata_code": "MU"}, "marketing_carrier_flight_number": "101",
                   "origin": {"iata_code": "PVG"}, "destination": {"iata_code": "KIX"},
                   "departing_at": "2026-12-01T13:00", "arriving_at": "2026-12-01T15:30", "duration": "PT150M"}
        for condition, allowed, fee in (
            ({"allowed": False}, False, None),
            ({"allowed": True, "penalty_amount": "50.00", "penalty_currency": "GBP"}, True, "GBP 50"),
            ({"allowed": True}, True, "退改费用待确认"),
            ({"allowed": True, "penalty_amount": "0.00"}, True, "免费"),
            ({"allowed": "false"}, None, None),
        ):
            with self.subTest(condition=condition):
                offer = {"total_amount": "1000", "total_currency": "CNY", "slices": [{"segments": [segment]}],
                         "conditions": {k+"_before_departure": condition for k in ("refund", "change")}}
                facts = source._parse_offer(offer)["extra"]["refund_change"]
                for key, kind in (("refundable", "refund"), ("changeable", "change")):
                    self.assertIs(facts[key], allowed)
                    self.assertEqual(facts[kind+"_fee"], fee)

    def test_g03_no_cross_offer_proofs_in_merge_or_enrichment(self):
        low = baggage.flight(900, offer_id="low", data_source="juhe", price_source="juhe")
        high = bound(baggage.flight(1500, price_source="duffel", offer_id="high",
                                    extra={"changeable": True, "refundable": True, "refund_change_offer_id": "high",
                                           "baggage": [{"type": "checked", "quantity": 1}]}))
        high["refund_change_offer_id"] = "high"
        original = deepcopy((low, high))
        agg = aggregator.FlightAggregator([], [])

        def check(row):
            self.assertEqual(row["price"], 900)
            self.assertEqual(row.get("offer_id"), "low")
            self.assertIs(row.get("refund_change_offer_matched"), False)
            self.assertEqual(row.get("refund_change_applicability"), "flight_number_match_only")
            self.assertNotIn("refund_change_offer_id", row.get("extra") or {})
            self.assertNotIn("refund_change_offer_id", row)

        batches = [{"source": s, "flights": [f]} for s, f in (("juhe", low), ("duffel", high))]
        for order in (batches, batches[::-1]):
            check(agg._merge_flights(order)[0])
        sources = [outcomes.OfflineSource(s, {"source_status": "success", "flights": [deepcopy(f)]})
                   for s, f in (("juhe", low), ("duffel", high))]
        sources[0].role, sources[0].weight = "primary", 1
        sources[1].role, sources[1].weight = "enrichment", 0
        agg = aggregator.FlightAggregator(sources[:1], sources[1:], route_type="international")
        inputs = [outcomes.make_outcome(s, deepcopy(s.result), destination="KIX", depart_date="2026-12-01")
                  for s in sources]
        before = deepcopy([o.result for o in inputs])
        with patch.object(agg, "_ordered_search_sources", return_value=sources[:1]), redirect_stdout(io.StringIO()):
            result = agg.collect_from_outcomes("PVG", "KIX", "2026-12-01", outcomes=inputs,
                                               cabin_classes=["economy"], route_type="international")
        check(result["flights"][0])
        self.assertEqual([o.result for o in inputs], before)
        self.assertEqual((low, high), original)
        for keep, other in (("refund_change", "baggage"), ("baggage", "refund_change")):
            row = {keep+"_offer_matched": True}
            aggregator._mark_unbound_baggage(row, "duffel")
            self.assertIs(row[keep+"_offer_matched"], True)
            self.assertIs(row[other+"_offer_matched"], False)

    def test_g04_soft_score_rejects_dirty_inferred_and_unbound_rules(self):
        for row in (baggage.flight(extra={"refundable": "false", "changeable": "false"}),
                    baggage.flight(fare_rules={**rules(), "source": "国内标准规则推断"}),
                    baggage.flight(fare_rules=rules(), refund_change_offer_matched=False)):
            for required in (True, False):
                self.assertFalse(analyzer._has_refund_change_flexibility(row, required=required))
            self.assertLess(analyzer._flight_refund_score(row), 80)
        case = characterization.VerifyFareRulesCharacterizationTest()
        case.test_system_inferred_full_service_rules_lock_evidence_fields()
        case.test_domestic_source_conflict_is_overwritten_by_inferred_lcc_rules()


class TestTrack2StrictEligibilityAndBudgetDecoupling(Contracts):
    def test_g05_nested_and_return_only_preferences(self):
        required = self.require_attr(analyzer, "is_refund_change_required")
        for prefs in ({"refund_flexibility": "must"}, {"hard_constraints": {"refund_policy": "必须退改"}},
                      {"constraints": {"need_refund_change": True}},
                      {"soft_preferences": {"companion_constraints": ["need_refund_change"]}},
                      {"refund_flexibility": "unknown", "refund_change_required": True}):
            sub = {**collection.scenario.subscription(), **deepcopy(prefs), "processing_phase": "return_only"}
            before = deepcopy(sub)
            self.assertTrue(required(sub))
            self.assertEqual(main.subscription_preferences(sub)["refund_flexibility"], "required")
            self.assertEqual(sub, before)
        self.assertFalse(required({"refund_flexibility": False, "companion_constraints": ["need_refund_change"]}))
        case = collection._IsolatedCase()
        case.setUp()
        self.addCleanup(case.doCleanups)
        sub = collection.scenario.subscription()
        sub["hard_constraints"].update(refund_policy="required", budget_strategy="low_price_alert")
        ok, sent, _ = case.run_subscription(collection._FakeMatrix({}), sub=sub,
                                            today=collection.TODAY.replace(month=10, day=2), managed=True)
        self.assertTrue(ok)
        self.assertTrue(sent)
        self.assertEqual(sent[0]["analysis_result"].get("low_price_alert_state"), "not_applicable")

    def test_g06_explicit_denial_precedes_unknown_baggage(self):
        for r, c, reason in ((False, True, "non_refundable"), (False, None, "non_refundable"),
                             (None, False, "non_changeable"), (False, False, "non_refundable_and_non_changeable")):
            row = baggage.flight(fare_rules=rules(r, c))
            kept, excluded, summary = self.filter([row], need_baggage="required")
            self.assertEqual((kept, summary["reference_flights"]), ([], []))
            self.assertEqual(len(excluded), 1)
            self.assertEqual(excluded[0].get("filter_reason_code"), "need_refund_change")
            self.assertEqual(excluded[0]["refund_change_eligibility"]["subreason"], reason)

    def test_g07_pending_quotes_are_reference_only_and_keep_other_hard_gates(self):
        for row in (baggage.flight(), baggage.flight(price_source="juhe", fare_rules=rules()),
                    baggage.flight(fare_rules={**rules(), "source": "国内标准规则推断"}),
                    baggage.flight(fare_rules=rules(), refund_change_offer_matched=False),
                    baggage.flight(fare_rules=rules(None, True))):
            kept, excluded, summary = self.filter([row])
            self.assertEqual((kept, excluded), ([], []))
            self.assertEqual([f.get("filter_reason_code") for f in summary["reference_flights"]], [PENDING])
        early = baggage.flight(departure_time="02:00")
        early["segments"][0]["dep_time"] = "2026-12-01 02:00"
        lcc = pairing.leg("GK101", 1000)
        for row, opts in ((early, {"red_eye": "reject"}),
                          (early, {"red_eye": "accept", "time_preference_mode": "custom",
                                   "departure_time_windows": ["08:00-20:00"]}),
                          (baggage.flight(), {"exclude_airlines": ["China Eastern"]}),
                          (baggage.flight(), {"max_budget": 999}),
                          (baggage.flight(stops=1), {"direct_only": "must"}),
                          (baggage.flight(stops=1, layovers=[{"wait_minutes": 600}]), {}),
                          (lcc, {"lcc_policy": "exclude_lcc"})):
            with self.subTest(opts=opts):
                kept, excluded, summary = self.filter([row], **opts)
                self.assertTrue(excluded)
                self.assertEqual((kept, summary["reference_flights"]), ([], []))
        analysis = self.analyze([baggage.flight()])
        self.assertEqual(analysis.get("no_primary_reason"), MISSING)
        self.assertEqual(analysis.get("recommended_plans"), [])
        with redirect_stdout(io.StringIO()):
            payload = notifier.build_notification_payload(analysis_result=analysis, route_info={
                **self.preferences(), "origin": "PVG", "destination": "KIX", "depart_date": "2026-12-01"})
        for body in carriers(payload):
            for word in (MISSING, REFERENCE, VERIFY):
                self.assertIn(word, body)

    def test_g08_future_fees_are_not_part_of_the_purchase_price(self):
        evaluate = self.require_attr(analyzer, "evaluate_refund_change_eligibility")
        for rf, cf, status in (("¥300", "¥200", "paid_known"), (None, None, "unknown"), (0, 0, "free")):
            row = baggage.flight(fare_rules=rules(rf=rf, cf=cf))
            before = deepcopy(row)
            eligibility = evaluate(row)
            self.assertEqual((eligibility["state"], eligibility["refund_fee_status"], eligibility["change_fee_status"]),
                             ("eligible", status, status))
            self.assertEqual(row, before)
            self.assertEqual(len(self.filter([row])[0]), 1)
            for rt in (False, True):
                for people, headcount in (("per_person", 1), ("all_passengers", 2)):
                    scope = people + ("_roundtrip" if rt else "_oneway")
                    price = analyzer._scoped_offer_price(row, self.preferences(), scope, round_trip=rt,
                                                         return_flight=row if rt else None)
                    self.assertEqual(price, 1000 * headcount * (2 if rt else 1))

    def test_g09_roundtrip_same_day_mixed_and_optional(self):
        def no_combo(result):
            self.assertEqual(result.get("top_combinations"), [])
            for k in ("outbound_min", "return_min", "total_min", "budget_price", "budget_price_compare"):
                self.assertIsNone(result[k])
            self.assertEqual((result["outbound_top3"], result["return_top3"]), ([], []))
        prefs = self.preferences()
        a, b = pairing.leg("MU101", 800), pairing.leg("MU102", 800, True)
        no_combo(pairing.select(pairing.analysis([bound(a)], prefs), pairing.analysis([b], prefs)))
        self.assertTrue(pairing.select(pairing.analysis([a], prefs), pairing.analysis([bound(b)], prefs))["top_combinations"])
        a, b = meeting.short_pair()
        no_combo(pairing.select(*meeting.leg_analyses([bound(a)], [b], {**meeting.BASE_PREFS, **prefs})))
        a, b = pairing.mixed_inputs()
        for side in (a, b):
            side["user_preferences"] = {**side["user_preferences"], "refund_flexibility": "required"}
            for f in side["all_flights"]:
                bound(f)
        bound(a["business_flights"][0])
        no_combo(pairing.select(a, b))
        flight = baggage.flight(cabin_class="business")
        result = self.analyze([bound(baggage.flight()), flight], **pairing.MIXED)
        self.assertEqual(result.get("no_primary_reason"), MISSING)
        for mode in ("preferred", "unknown"):
            self.assertEqual(len(self.filter([baggage.flight()], refund_flexibility=mode)[0]), 1)


class TestTrack3LowPriceIsolationAndCollectionTruth(Contracts):
    def market(self, *, strict=True, qualified=True, **options):
        m = history.market(**options)
        m[0]["refund_flexibility"] = "required" if strict else "preferred"
        if qualified:
            for side in m[1].values():
                for f in side["roundtrip_candidate_flights"]:
                    bound(f)
        return m

    def test_g10_reference_offer_cannot_write_or_trigger(self):
        unknown = history.offer(self.market(qualified=False))
        self.assertEqual((unknown.get("state"), unknown.get("reason"), unknown.get("subreason")),
                         ("unknown", "no_valid_offers", PENDING))
        self.assertEqual(unknown["sample_delta"], 0)
        analysis = analyzer.apply_low_price_evaluation({}, unknown)
        self.assertFalse(analysis["low_price_alert_triggered"])
        self.assertIsNone(analysis["target_price_effective"])
        db = self.root / "no-sample.db"
        with self.assertRaises(ValueError):
            storage.evaluate_and_persist_low_price_alert(unknown, db_path=db)
        self.assertFalse(db.exists())

    def test_g11_real_processing_keeps_quotes_out_of_qualified_history(self):
        case = collection._IsolatedCase()
        case.setUp()
        self.addCleanup(case.doCleanups)
        sub = collection.scenario.subscription()
        sub.update(round_trip=False, refund_flexibility="required")
        sub["hard_constraints"]["round_trip"] = False
        sub.pop("return_date", None)
        sub["hard_constraints"].pop("return_date", None)
        ok, captured, call = case.run_subscription(collection._FakeMatrix({}), sub=sub, managed=True)
        self.assertTrue(ok)
        self.assertEqual(len(captured), 1)
        result = captured[0]["analysis_result"]
        self.assertTrue(call["save_raw_response"].called)
        raw = call["save_raw_response"].call_args.args[2]
        self.assertEqual(main._classify_leg_collection_outcome(raw), "quoted")
        self.assertNotEqual(result.get("collection_outcome"), "covered_empty")
        self.assertEqual(result.get("no_primary_reason"), MISSING)
        self.assertTrue(result["reference_flights"])
        self.assertFalse(result["roundtrip_candidate_flights"])
        for args in call["save_flight_details"].call_args_list:
            self.assertEqual(args.args[2], [])
        route_info = captured[0]["route_info"]
        with redirect_stdout(io.StringIO()):
            payload = notifier.build_notification_payload(analysis_result=result, route_info=route_info)
        self.assertFalse(notifier._covered_empty_state(payload))
        for body in carriers(payload):
            for word in ("售罄", "停飞"):
                self.assertNotIn(word, body)

    def test_g12_v1_history_survives_and_is_invisible_to_strict_v2(self):
        db = self.root / "historical.db"
        def write(strict, index, price):
            offer = history.offer(self.market(strict=strict, econ=price, event=f"event-{index}"))
            self.assertEqual(offer.get("method_version"), STRICT if strict else "low_price_alert_v1")
            return storage.evaluate_and_persist_low_price_alert(offer, db_path=db, recorded_at_utc=history.T)
        for i in range(6):
            write(False, i, 1600+i*100)
        def legacy_rows():
            with closing(sqlite3.connect(db)) as conn:
                return conn.execute(f"SELECT * FROM {history.OBS} WHERE method_version=? ORDER BY id",
                                    ("low_price_alert_v1",)).fetchall()
        before = legacy_rows()
        self.assertEqual(len(before), 6)
        fresh = write(True, 7, 1000)
        self.assertEqual((fresh["reason"], fresh["prior_n"]), ("history_unavailable", 0))
        self.assertEqual(before, legacy_rows())
        normal = write(False, 8, 1000)
        self.assertEqual((normal["state"], normal["prior_n"]), ("triggered", 6))

    def test_g13_strict_mixed_offer_requires_each_cabin_on_each_leg(self):
        for leg in ("outbound", "return"):
            for cabin in ("economy", "business"):
                m = self.market(rt=True, arrangement="mixed")
                self.assertTrue(history.offer(m).get("can_evaluate_current"))
                for f in m[1][leg]["roundtrip_candidate_flights"]:
                    if f["cabin_class"] == cabin:
                        del f["fare_rules"]
                missing = history.offer(m)
                self.assertEqual((missing.get("reason"), missing.get("subreason")), ("no_valid_offers", PENDING))
                self.assertFalse(missing["can_append_sample"])


class TestTrack4ProtectedContractsAndCarrierGuards(Contracts):
    def test_g14_protected_files_and_ddl(self):
        source = (ROOT / "storage.py").read_text(encoding="utf-8")
        ddl, = (node for node in ast.parse(source).body if isinstance(node, ast.Assign)
                and getattr(node.targets[0], "id", None) == "_LP_DDL")
        self.assertEqual(hashlib.sha256(ast.get_source_segment(source, ddl).encode()).hexdigest(),
                         "da79886185e4c7aec6a0bdcb4021189171929fa1f06d33b148480365e0f11c03")
        self.assertEqual(storage.LOW_PRICE_ALERT_METHOD_VERSION, "low_price_alert_v1")
        guard = collection.TestTrack4FrozenAndAstGuards("test_g22_protected_files_and_source_anchors_are_exact").run()
        self.assertTrue(guard.wasSuccessful(), guard.failures + guard.errors)

    def test_g15_source_segment_hashes_of_existing_functions(self):
        for file, expected in (("notifier.py", renderer.EXPECTED_MAIN_CHAIN_SHA256), ("analyzer.py", {
            "verify_fare_rules": "87ddfdf2bfdfc728057901b36751a544287568ef6f54ba1d8a14638daf43e082",
            "parse_flight_time": "dfbff457554895bf182c1c80900ac82f64c94d7aeaff28189ce547c85c31069f",
            "overall_score": "c762984d55d286c49e4f69387290c131161c5e8ebdcfaee796fdcf1f38296745",
            "calc_transfer_risk": "660cb5ea9a31f053122f705765a3299969f0bd14555c7d65509e5d3ad982e829",
            "_min_layover_minutes": "334a2adbf312e3a90fff48d7ac830900efedd271e83db62a5b07820240dd04fe",
            "match_time_preference": "ce0bbaf1024f6d98e16271b3580a5d7b55c8b0ebda1fcbdb636e9163f5f4d5ce",
        })):
            source = (ROOT / file).read_text(encoding="utf-8")
            nodes = {n.name: n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)}
            for name, digest in expected.items():
                self.assertEqual(hashlib.sha256(ast.get_source_segment(source, nodes[name]).encode()).hexdigest(),
                                 digest, name)

    def test_g16_three_carriers_remain_byte_exact(self):
        payload = json.loads((ROOT / "tests/fixtures/frozen_email/economy_payload.json").read_text(encoding="utf8"))
        expected = ((53307, "1dd353d2b7bcafc02087957ccccb6661478e20472a22fb3e15dae6a56473ee6b"),
                    (5552, "2ac0e80d9f82dbe7ebccf524c0832f09d42d916212b6178316b0b7bcd60574ff"),
                    (54248, "e31b8728b8d0bc05c9bbd4bd99c299aaf6dca7b66674b6c560ec089b1681ffa6"))
        with patch.object(config_loader, "RUNTIME_CONFIG_PATH", self.root / "absent.yaml"):
            for text, spec in zip(carriers(payload), expected):
                raw = text.encode("utf-8")
                self.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()), spec)
