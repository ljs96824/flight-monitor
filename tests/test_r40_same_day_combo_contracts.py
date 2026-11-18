"""R40 same-day pairing, diagnosis and offline notification contracts."""

import ast
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import inspect
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import analyzer
import config_loader
import constraint_fingerprint
import main
import notifier
import plan_tracker
import storage
import web_form
from tests import test_return_phase_processing as scenario


ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-12-01"
NEXT = "2026-12-02"
BASE_PREFS = {
    "same_day_round_trip": True, "depart_date": DAY,
    "business_start": "10:00", "business_end": "11:00",
    "meeting_importance": "normal", "destination_transport_min": 10,
    "arrival_exit_min": 10, "airport_advance_min": 20,
    "delay_buffer_min": 5, "pre_meeting_buffer_min": 5, "post_meeting_buffer_min": 5,
}


def flight(departure, arrival, *, back=False, day=DAY, arrival_day=None, airport="PKX", number=None, price=500):
    row = scenario.flight(not back, price)
    origin, dest = (airport, "PVG") if back else ("PVG", airport)
    number = number or ("MU102" if back else "MU101")
    end_day = arrival_day or day
    row.update(flight_no=number, flight_combo=number, departure_airport=origin, arrival_airport=dest,
               departure_date=day, arrival_date=end_day, departure_time=departure, arrival_time=arrival)
    row["segments"][0].update(flight_no=number, dep_airport=origin, arr_airport=dest,
                              dep_time=f"{day} {departure}", arr_time=f"{end_day} {arrival}")
    return row


def short_pair():
    return flight("07:10", "09:05"), flight("12:45", "14:45", back=True)


def leg_analyses(outbound, returns, prefs, *, next_returns=()):
    return (
        dict(same_day_base_flights=outbound, roundtrip_candidate_flights=outbound,
             all_flights=outbound, user_preferences=prefs, depart_date=DAY),
        dict(same_day_base_flights=returns, roundtrip_candidate_flights=returns,
             all_flights=returns, next_day_flights=list(next_returns), depart_date=DAY),
    )


def low_price_offer(outbound, ret, prefs):
    outbound, ret = deepcopy((outbound, ret))
    analyses, data = {}, {}
    for name, row in (("outbound", outbound), ("return", ret)):
        origin, airport = row["departure_airport"], row["arrival_airport"]
        unit = dict(source="juhe", cabin_class="economy", origin_airport=origin, dest_airport=airport,
                    depart_date=DAY, source_status="success", evidence_qualified=True,
                    observed_at_utc="2026-11-25T00:00:00+00:00",
                    observation_round_id="collection_synthetic", fetch_event_id=f"r40_{name}")
        row.update(source="juhe", price_source="juhe", source_collection={"juhe": unit})
        analyses[name] = {"roundtrip_candidate_flights": [row]}
        data[name] = {"attempted_airport_pairs": [(origin, airport)], "fetch_evidence_units": [unit]}
    sub = dict(origin="PVG", destination="PKX", depart_date=DAY, return_date=DAY,
               hard_constraints={**prefs, "cabin_arrangement": "economy_all"})
    return analyzer.build_current_low_price_offer(
        sub, analyses, data, trip_type="round_trip", route_type="domestic",
        origins=["PVG"], destinations=["PKX"], passengers={"adult": 1}, constraint_fp="r40",
    )


class ContractBase(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        self.root = Path(stack.enter_context(TemporaryDirectory()))
        stack.enter_context(redirect_stdout(io.StringIO()))
        stack.enter_context(patch.object(config_loader, "RUNTIME_CONFIG_PATH", self.root / "absent.yaml"))
        stack.enter_context(patch.object(storage, "DB_PATH", self.root / "prices.db"))
        stack.enter_context(patch.object(web_form, "SUBSCRIPTIONS_PATH", self.root / "subscriptions.json"))
        stack.enter_context(patch.object(plan_tracker, "DEFAULT_DATA_DIR", self.root / "plans"))
        stack.enter_context(patch.object(main, "LOW_PRICE_NOTICE_STATE_PATH", self.root / "notices.json"))
        stack.enter_context(patch.object(main, "ANALYSIS_LOG", self.root / "analysis.jsonl"))
        stack.enter_context(patch.object(analyzer, "safe_log"))
        stack.enter_context(patch.object(notifier, "safe_log"))

    def analyze(self, outbound, returns, prefs=None, **kwargs):
        return analyzer.analyze_round_trip(
            *leg_analyses(outbound, returns, prefs if prefs is not None else dict(BASE_PREFS)),
            emit_diagnostics=False, **kwargs,
        )


class TestTrack1MeetingStayGateAndLowPriceParity(ContractBase):
    def test_g01_short_meeting_respects_only_an_explicit_floor(self):
        out, ret = short_pair()
        combos = analyzer.build_same_day_combos
        self.assertEqual(len(combos([out], [ret], DAY, constraints=BASE_PREFS)), 1)
        self.assertEqual(combos([out], [ret], DAY, min_stay_hours=4, constraints=BASE_PREFS), [])
        for minimum in (None, 0, 3.5):
            self.assertEqual(len(combos([out], [ret], DAY, min_stay_hours=minimum, constraints=BASE_PREFS)), 1)

    def test_g02_inverted_meeting_and_negative_stay_do_not_pass(self):
        prefs = dict(BASE_PREFS, business_start="17:00", business_end="10:00")
        self.assertEqual(analyzer.compute_same_day_windows(prefs, None, "PKX"), {})
        out, ret = short_pair()
        self.assertEqual(analyzer.build_same_day_combos([out], [ret], DAY, constraints=prefs), [])
        windows = {"outbound_arrive_by": "09:15", "return_depart_after": "08:55"}
        early = flight("09:00", "10:50", back=True)
        for minimum in (None, 0):
            self.assertEqual(analyzer.build_same_day_combos([out], [early], windows, DAY, min_stay_hours=minimum), [])

    def test_g03_invalid_explicit_minimum_fails_closed_in_both_modes(self):
        ob, rt = flight("07:30", "09:00"), flight("19:00", "21:00", back=True)
        for bad in (-1, False, True, float("nan"), float("inf"), float("-inf"), "4"):
            for windows in (DAY, {"outbound_arrive_by": "09:15", "return_depart_after": "16:00"}):
                self.assertEqual(analyzer.build_same_day_combos(
                    [ob], [rt], windows, DAY, min_stay_hours=bad), [], repr(bad))

    def test_g04_defaults_do_not_rescue_invalid_configuration(self):
        ob, rt = flight("07:30", "09:00"), flight("19:00", "21:00", back=True)
        for prefs in ({"business_start": "10:00"}, {"business_start": "10:00a", "business_end": "11:00"}):
            self.assertEqual(analyzer.build_same_day_combos([ob], [rt], DAY, constraints=prefs), [])
        self.assertEqual(analyzer.build_same_day_combos([ob], [rt], {"outbound_arrive_by": "09:00"}, DAY), [])
        self.assertEqual(len(analyzer.build_same_day_combos([ob], [rt], {
            "outbound_arrive_by": "09:00", "return_depart_after": "18:00"}, DAY)), 1)
        self.assertEqual(len(analyzer.build_same_day_combos([ob], [rt], {
            "outbound_arrive_by_by_airport": {"PKX": "09:00"},
            "return_depart_after_by_airport": {"PKX": "18:00"}}, DAY)), 1)

    def test_g05_default_profiles_keep_priority_and_accept_explicit_floor(self):
        standard = flight("07:00", "09:30")
        relaxed = flight("11:30", "13:30", number="MU103")
        late = flight("19:00", "21:00", back=True)
        early = flight("16:10", "18:00", back=True)
        build = analyzer.build_same_day_combos
        self.assertEqual(len(build([relaxed], [early], DAY, min_stay_hours=2.5)), 1)
        self.assertEqual(build([relaxed], [early], DAY), [])
        self.assertEqual(len(build([relaxed], [early], DAY, min_stay_hours=0)), 1)
        self.assertEqual(build([relaxed], [flight("15:50", "17:40", back=True)], DAY, min_stay_hours=0), [])
        options = build([relaxed, standard], [late], DAY, min_stay_hours=None)
        self.assertEqual([combo["outbound"] for combo in options], [standard])
        self.assertFalse(options[0]["same_day_default_relaxed"])
        self.assertTrue(build([relaxed], [late], DAY)[0]["same_day_default_relaxed"])

    def test_g06_public_low_price_pool_matches_main_recommendation(self):
        out, ret = short_pair()
        self.assertTrue(self.analyze([out], [ret]).get("top_combinations"))
        self.assertTrue(low_price_offer(out, ret, BASE_PREFS).get("can_evaluate_current"))
        for bad_out, bad_ret in (
            (flight("07:30", "10:05"), ret), (out, flight("08:55", "10:30", back=True)),
        ):
            self.assertEqual(self.analyze([bad_out], [bad_ret]).get("top_combinations"), [])
            self.assertEqual(low_price_offer(bad_out, bad_ret, BASE_PREFS).get("reason"), "no_valid_offers")


class TestTrack2NoResultDiagnosisAndFeasibleNote(ContractBase):
    def test_g07_real_filter_counts_take_precedence_over_pairing_fallback(self):
        out = flight("11:00", "13:30")
        ret = flight("16:10", "18:00", back=True)
        prefs = {"same_day_round_trip": True, "depart_date": DAY}
        analysis = self.analyze([out], [ret], prefs)
        self.assertEqual(analysis.get("top_combinations"), [])
        diagnosis = analyzer.build_no_result_diagnosis(
            [out, ret], stage_counts=analysis.get("filter_counts"),
            fallback_reason="去返程未能组成完整往返",
        )
        self.assertEqual(diagnosis["counts"].get("primary_cause"), "time_window")
        self.assertIn("【时间窗口】", diagnosis["reason"])
        self.assertNotIn("恢复返程", diagnosis["reason"])
        self.assertNotIn("预算", diagnosis["reason"])
        self.assertEqual(analysis["filter_counts"].get("standard_combos"), 0)
        self.assertEqual(analysis["filter_counts"]["after_meeting_outbound"], 1)
        self.assertEqual(analysis["filter_counts"]["return_after_lowerbound"], 1)

    def test_g08_only_complete_matched_roundtrip_prices_prove_budget(self):
        stages = dict(after_meeting_outbound=1, return_after_lowerbound=1, same_day_combos=1, after_budget=0)
        prefs = {"max_budget": 900, "round_trip": True, "max_budget_scope": "per_person", "passengers": {"adult": 2}}
        for rows in ([], [{"outbound_price": 1000}, {"return_price": 1000}], [{"outbound_price": 400, "return_price": 500}]):
            result = analyzer.build_no_result_diagnosis([], constraints=prefs,
                                                        stage_counts=dict(stages, budget_excluded_candidates=rows))
            self.assertEqual(result["counts"].get("primary_cause"), "post_pairing_unverified")
            self.assertNotIn("预算", result["reason"])
            self.assertNotIn("primary_cause", result["price_summary"])
            self.assertIsNone(result["counts"]["after_budget"])
        result = analyzer.build_no_result_diagnosis(
            [], constraints=prefs,
            stage_counts=dict(stages, budget_excluded_candidates=[dict(outbound_price=500, return_price=600)]),
        )
        self.assertEqual(result["counts"].get("primary_cause"), "budget")
        self.assertTrue(result["counts"].get("budget_evaluated"))
        self.assertEqual(result["counts"]["after_budget"], 0)
        self.assertIn("【预算】", result["reason"])
        self.assertIn("¥200", result["reason"])

    def test_g09_mixed_and_baggage_zero_are_not_budget_failures(self):
        for overrides, field, cause in (
            ({"cabin_arrangement": "mixed", "cabin_allocation": {"economy": {"adult": 1}, "business": {"adult": 1}},
              "passengers": {"adult": 2}}, "after_mixed_cabin", "mixed_cabin"),
            ({"need_baggage": "required"}, "after_baggage", "baggage_pending_confirmation"),
        ):
            # Give the qualification stages an old-floor-compatible meeting pair.
            rt = flight("19:00", "21:00", back=True)
            ob = flight("07:00", "08:10")
            prefs = dict(BASE_PREFS, **overrides)
            result = self.analyze([ob], [rt], prefs)
            counts = result.get("filter_counts", {})
            self.assertGreater(counts.get("same_day_combos", 0), 0)
            self.assertEqual(counts.get(field), 0)
            self.assertNotIn("after_budget", counts)
            diagnosis = analyzer.build_no_result_diagnosis([], constraints=prefs, stage_counts=counts, fallback_reason="不能配对")
            self.assertEqual(diagnosis["counts"].get("primary_cause"), cause)
            self.assertNotIn("预算", diagnosis["reason"])

    def test_g10_structured_diagnosis_stays_in_the_correct_pool(self):
        for extra, cause, count, scope in (
            ({"same_day_mode": "invalid_meeting_window"}, "invalid_meeting_window", 9, "候选航班池"),
            ({"after_meeting_outbound": 0}, "outbound_time", 4, "去程池"),
            ({"return_after_lowerbound": 0}, "return_time", 5, "返程池"),
            ({}, "time_window", 6, "同日配对候选对池"),
        ):
            counts = dict(outbound_collected=4, return_collected=5, after_meeting_outbound=2,
                          return_after_lowerbound=3, same_day_combos=0)
            counts.update(extra)
            diagnosis = analyzer.build_no_result_diagnosis([], stage_counts=counts, fallback_reason="不能配对")
            state = diagnosis["counts"]
            self.assertEqual(state.get("primary_cause"), cause)
            self.assertEqual(state["max_bottleneck"]["key"], cause)
            self.assertEqual(state["max_bottleneck"]["pool_scope"], scope)
            self.assertEqual((state["max_bottleneck"]["count"], state["max_bottleneck"]["ratio"]), (count, 100.0))
            self.assertEqual(state.get("outbound_collected"), 4)
            self.assertEqual(state.get("return_collected"), 5)
            self.assertIsNone(state["after_budget"])
            self.assertIs(state.get("budget_evaluated"), False)
            self.assertNotIn("primary_cause", diagnosis["price_summary"])
            self.assertNotIn("预算", diagnosis["reason"])

    def test_g11_default_diagnostic_cause_matches_the_rendering_gate(self):
        examples = (
            (flight("12:10", "14:05"), flight("19:00", "21:00", back=True), "outbound_time"),
            (flight("07:30", "09:30"), flight("15:55", "18:00", back=True), "return_time"),
            (flight("11:30", "13:30"), flight("16:10", "18:30", back=True), "time_window"),
        )
        for ob, rt, key in examples:
            prefs = {"depart_date": DAY, "same_day_round_trip": True}
            result = self.analyze([ob], [rt], prefs)
            note = result.get("same_day_no_feasible_note")
            self.assertEqual(notifier._no_primary_cause_key(note), key)
            diagnosis = analyzer.build_no_result_diagnosis([], stage_counts=result["filter_counts"])
            self.assertEqual(notifier._same_day_note_for_no_primary(
                {"same_day_no_feasible_note": note}, diagnosis["reason"]), note)
            if key == "time_window":
                self.assertIn("最长同日停留2小时40分钟", note)

    def test_g12_next_day_arrival_is_not_a_same_day_outbound(self):
        ob = flight("22:30", "00:30", arrival_day=NEXT)
        rt = flight("19:00", "21:00", back=True)
        note = analyzer._same_day_no_feasible_note([ob], [rt], BASE_PREFS)
        self.assertEqual(notifier._no_primary_cause_key(note), "outbound_time")
        self.assertIn("当天无当日到达", note)
        self.assertIn("次日00:30", note)

    def test_g13_show_the_return_airports_own_floor(self):
        prefs = dict(same_day_round_trip=True, depart_date=DAY, business_start="10:30",
                     business_end="17:00", meeting_location="大兴区", meeting_importance="important")
        ob = flight("06:30", "07:30")
        pek, pkx = flight("18:00", "20:00", back=True, airport="PEK"), flight("18:15", "20:15", back=True)
        floors = {airport: analyzer.compute_same_day_windows(prefs, None, airport)["return_depart_after"]
                  for airport in ("PKX", "PEK")}
        self.assertNotEqual(floors["PKX"], floors["PEK"])
        for returns in ([pek], [pkx, pek]):
            note = analyzer._same_day_no_feasible_note([ob], returns, prefs)
            self.assertIn(f"PEK {floors['PEK']}", note)
            if len(returns) > 1:
                self.assertIn(f"PKX {floors['PKX']}", note)

    def test_g14_relaxation_keeps_explicit_times_and_never_increases_legacy_buffer(self):
        prefs = dict(BASE_PREFS, meeting_importance="important", arrival_exit_min=20)
        ob, rt = flight("08:00", "09:30"), flight("18:00", "20:00", back=True)
        with patch.object(analyzer, "compute_same_day_windows", wraps=analyzer.compute_same_day_windows) as observed:
            analyzer._same_day_no_feasible_note([ob], [rt], prefs)
        normal = [c.args[0]["constraints"] for c in observed.call_args_list
                  if (c.args[0].get("constraints") or {}).get("meeting_importance") == "normal"]
        self.assertTrue(normal)
        self.assertTrue(all(candidate.get("arrival_exit_min") == 20 for candidate in normal))
        legacy = {"depart_date": DAY, "business_start": "10:00", "business_end": "17:00", "buffer_hours": 1}
        with patch.object(analyzer, "compute_same_day_windows", wraps=analyzer.compute_same_day_windows) as observed:
            analyzer._same_day_no_feasible_note([ob], [rt], legacy)
        self.assertNotIn(2, [(c.args[0].get("constraints") or {}).get("buffer_hours") for c in observed.call_args_list])

    def test_g15_no_op_advice_is_suppressed_and_real_advice_counts_both_pools(self):
        ob = flight("07:45", "09:45")
        rt = flight("19:00", "21:00", back=True)
        normal = analyzer._same_day_no_feasible_note([ob], [rt], BASE_PREFS)
        self.assertNotIn("会议重要", normal)
        prefs = dict(BASE_PREFS, meeting_importance="important")
        old = analyzer.compute_same_day_windows(prefs, None, "PKX")
        new = analyzer.compute_same_day_windows(dict(prefs, meeting_importance="normal"), None, "PKX")
        self.assertGreater(new["outbound_arrive_by_minutes"], old["outbound_arrive_by_minutes"])
        minute = new["outbound_arrive_by_minutes"]
        near = flight("07:00", f"{minute // 60:02d}:{minute % 60:02d}")
        note = analyzer._same_day_no_feasible_note([near], [rt], prefs)
        self.assertIn("有1个去程可选（可配对1个完整往返方案）", note)


def manual_window(*, arrival_limit="09:00", transport=20, model="meeting_fixed"):
    if model == "meeting_fixed":
        breakdown = dict(legacy=False, arrival_exit_min=10, destination_transport_min=transport,
                         destination_transport_margin_min=10, delay_buffer_min=15, pre_meeting_buffer_min=5,
                         custom_redundancy_min=50)
    else:
        breakdown = dict(legacy=False, airport_buffer_min=10, transport_min=transport, margin_min=10, safety_min=75)
    return dict(business_start="10:00", business_end="11:00", outbound_arrive_by=arrival_limit,
                return_depart_after="12:30", transport_min=transport, buffer_model=model,
                reserve_breakdown=dict(legacy=False, model=model, outbound=breakdown))


class TestTrack3AlternativesLateMetricsAndThreeCarriers(ContractBase):
    def alternative(self, outbound, *, windows=None, returns=(), next_returns=(), prefs=None):
        alternatives = analyzer.build_same_day_alternatives(
            [outbound], list(returns), windows or manual_window(), DAY,
            next_day_return=list(next_returns), constraints=prefs,
        )
        selected = [value for value in alternatives if value.get("category") == "same_day_earliest"]
        self.assertEqual(len(selected), 1)
        return selected[0]

    def assert_metrics(self, alternative, window_late, meeting_time, meeting_late):
        self.assertEqual(alternative.get("late_minutes"), window_late)
        self.assertEqual(alternative.get("meeting_arrival_time"), meeting_time)
        self.assertIn("estimated_meeting_late_minutes", alternative)
        self.assertEqual(alternative.get("estimated_meeting_late_minutes"), meeting_late)
        self.assertIn("unmet_reason", alternative)

    def test_g16_window_excess_can_use_reserve_without_missing_meeting(self):
        ob, rt = flight("07:15", "09:10"), flight("12:45", "14:45", back=True)
        alt = self.alternative(ob, returns=[rt])
        self.assert_metrics(alt, 10, "09:50", 0)
        for field in ("unmet_reason", "note"):
            self.assertIn("尚未晚于会议开始", alt[field])
        self.assertIn("压缩规划预留", alt["tradeoff"])
        prefs = dict(BASE_PREFS, destination_transport_min=20, custom_redundancy_min=30)
        self.assertEqual(self.analyze([ob], [rt], prefs).get("top_combinations"), [])
        self.assertEqual(low_price_offer(ob, rt, prefs).get("reason"), "no_valid_offers")

    def test_g17_real_estimated_meeting_lateness_is_a_separate_metric(self):
        alt = self.alternative(flight("07:30", "09:30"))
        self.assert_metrics(alt, 30, "10:10", 10)
        for field in ("unmet_reason", "note"):
            self.assertIn("超出最晚落地窗口(09:00)30分钟", alt[field])
            self.assertIn("预计晚于会议开始10分钟", alt[field])
        self.assertIn("有迟到风险", alt["tradeoff"])

    def test_g18_legacy_reserve_cannot_prove_meeting_arrival(self):
        prefs = {"business_start": "10:00", "business_end": "11:00", "buffer_hours": 2.5}
        windows = analyzer.compute_same_day_windows(prefs, None, "PKX")
        self.assertIs(windows["reserve_breakdown"]["legacy"], True)
        alt = self.alternative(flight("07:15", "08:00"), windows=windows, prefs=prefs)
        self.assert_metrics(alt, 30, "", None)
        self.assertIn("超出旧版预留窗口(07:30)", alt["unmet_reason"])
        self.assertIn("无法据此核实预计到会时间", alt["note"])
        self.assertNotIn("预计不迟到", alt["note"])

    def test_g19_selected_airport_controls_both_window_and_traffic(self):
        pkx = manual_window(transport=20)
        pek = manual_window(arrival_limit="08:45", transport=40)
        for sample, selected, expected in (("PEK", "PKX", (10, "09:50", 0)), ("PKX", "PEK", (25, "10:10", 10))):
            choices = {"PKX": pkx, "PEK": pek}
            windows = dict(choices[sample],
                           outbound_arrive_by_by_airport={a: w["outbound_arrive_by"] for a, w in choices.items()},
                           reserve_breakdown_by_airport={a: w["reserve_breakdown"] for a, w in choices.items()})
            alt = self.alternative(flight("07:10", "09:10", airport=selected), windows=windows)
            self.assert_metrics(alt, *expected)

    def test_g20_outbound_within_window_does_not_imply_lateness(self):
        ob = flight("07:10", "08:50")
        next_return = flight("09:30", "11:30", back=True, day=NEXT)
        for returns, next_returns, reason in (
            ([], [next_return], "去程符合会议窗口，需改为次日返程"),
            ([], [], "去程符合会议窗口，暂不能组成完整往返"),
            ([flight("12:45", "14:45", back=True)], [], "去程符合到达窗口，但同日停留或配对条件未满足"),
        ):
            alt = self.alternative(ob, returns=returns, next_returns=next_returns)
            self.assert_metrics(alt, 0, "09:30", 0)
            self.assertEqual(alt["unmet_reason"], reason)
            self.assertEqual(alt["tradeoff"], reason)
            self.assertNotIn("有迟到风险", " ".join(alt[key] for key in ("unmet_reason", "tradeoff", "note")))

    def test_g21_real_no_result_pipeline_renders_both_card_shapes_on_all_three_carriers(self):
        prefs = dict(BASE_PREFS)
        ob = flight("07:30", "09:50")
        eligible_return = flight("12:45", "14:45", back=True)
        early_return = flight("10:05", "12:05", back=True)
        route = dict(prefs, origin="PVG", destination="PKX", route_type="domestic",
                     depart_date=DAY, return_date=DAY, round_trip=True)
        for ret, paired in ((eligible_return, True), (early_return, False)):
            out_analysis, ret_analysis = leg_analyses([ob], [ret], prefs)
            result = analyzer.analyze_round_trip(out_analysis, ret_analysis, emit_diagnostics=False)
            self.assertEqual(result["top_combinations"], [])
            result = dict(result, round_trip_analysis=result)
            payload = notifier.build_notification_payload(result, out_analysis, ret_analysis, route_info=route)
            cards = [card for card in payload.get("same_day_alternatives", []) if card.get("category") == "same_day_earliest"]
            self.assertEqual(len(cards), 1)
            self.assertEqual(bool(cards[0].get("return")), paired)
            note = cards[0].get("unmet_reason") or ""
            self.assertIn("预计晚于会议开始", note)
            _, email = notifier.render_email(deepcopy(payload))
            detail = notifier.render_detail_html(deepcopy(payload))
            push = notifier.prepare_push_render(notifier.render_pushplus_sections(deepcopy(payload))).content
            for label, rendered in (("email", email), ("detail", detail), ("PushPlus", push)):
                self.assertIn(note, rendered, label)
                self.assertIn("【去程时间】", rendered, label)

    def test_g22_airport_split_uses_travel_components_without_safety_reserve(self):
        alt = self.alternative(flight("07:20", "09:30"), windows=manual_window(model="airport_split"))
        self.assert_metrics(alt, 30, "10:10", 10)
        self.assertIn("预计晚于会议开始10分钟", alt["unmet_reason"])


class TestTrack4FrozenAndAstGuards(unittest.TestCase):
    def test_g23_locked_files_and_renderer_sources_are_unchanged(self):
        locked = (
            ("notifier.py", 647512, "ea48611112477ae0aafcb20bd0b7511ba13ef047f7f5d454a343bcbabb6cebff"),
            ("constraint_fingerprint.py", 11459, "b396aef5431cc976754bab2a5a2474641b78d38002844a808521b2f228c415a9"),
            ("constraint_summary.py", 3595, "ab7a1505ff953891c7959e7e2412bc3ce7f93f93732e48c7557f5d43ebb3752b"),
            ("main.py", 162587, "a56ac6f05b300e5892bdbf0b5f982a5cbdf4918147987589ca1299c91269e7c7"),
            ("web_form.py", 125145, "47f45bc97aa879bfb48e996482f13a504f1d68bc85fc0d893e885ea5b0f9ab1d"),
            ("form_concepts.py", 37206, "cf535034c4242c291b6cf1f66c7335497e0fb2e4ecbf238492662b6621e1e9b6"),
            ("form_structure.py", 53116, "5b9160c3cec816afeb92bf13dc868f1405c8db1eda3b6bb41479bc399b063882"),
            ("tests/fixtures/form_normalization_baseline_v1.json", 180179,
             "88172833002fbe747b289848341a85d4c23470070339a4775ce2f61f183a2fd9"),
        )
        for path, size, digest in locked:
            raw = (ROOT / path).read_text(encoding="utf-8").encode("utf-8")
            self.assertEqual((len(raw), sha256(raw).hexdigest()), (size, digest), path)
        fixture = json.loads((ROOT / locked[-1][0]).read_text(encoding="utf-8"))
        self.assertEqual(len(fixture["scenarios"]), 11)
        self.assertEqual(len(constraint_fingerprint.EXPECTED_CONSTRAINT_FINGERPRINT_FIELDS), 54)
        self.assertEqual(set(constraint_fingerprint.CONSTRAINT_FINGERPRINT_FIELDS),
                         constraint_fingerprint.EXPECTED_CONSTRAINT_FINGERPRINT_FIELDS)
        for function, digest in (
            (analyzer.parse_flight_time, "9a55d7e7d427adf2469b269546262ebc3d3264ec7f7cae4b7df537f1a4177297"),
            (analyzer.match_time_preference, "2e21c6f1cb34a5300b194c59b167b52753e321f2d7e696685c153f64c98af93f"),
        ):
            self.assertEqual(sha256(inspect.getsource(function).encode()).hexdigest(), digest, function.__name__)
        from test_legacy_notification_renderer_retirement import EXPECTED_MAIN_CHAIN_SHA256

        source = (ROOT / "notifier.py").read_text(encoding="utf-8")
        for node in ast.parse(source).body:
            if isinstance(node, ast.FunctionDef) and node.name in EXPECTED_MAIN_CHAIN_SHA256:
                self.assertEqual(sha256(ast.get_source_segment(source, node).encode()).hexdigest(),
                                 EXPECTED_MAIN_CHAIN_SHA256[node.name], node.name)

    def test_g24_frozen_three_carriers_and_normal_roundtrip_stay_exact(self):
        payload = json.loads((ROOT / "tests/fixtures/frozen_email/economy_payload.json").read_text(encoding="utf-8"))
        with (TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()),
              patch.object(config_loader, "RUNTIME_CONFIG_PATH", Path(tmp) / "absent.yaml")):
            _, email = notifier.render_email(deepcopy(payload))
            detail = notifier.render_detail_html(deepcopy(payload))
            push = notifier.prepare_push_render(notifier.render_pushplus_sections(deepcopy(payload))).content
        for content, size, digest in (
            (email, 53307, "1dd353d2b7bcafc02087957ccccb6661478e20472a22fb3e15dae6a56473ee6b"),
            (detail, 54248, "e31b8728b8d0bc05c9bbd4bd99c299aaf6dca7b66674b6c560ec089b1681ffa6"),
            (push, 5552, "2ac0e80d9f82dbe7ebccf524c0832f09d42d916212b6178316b0b7bcd60574ff"),
        ):
            raw = content.encode()
            self.assertEqual((len(raw), sha256(raw).hexdigest()), (size, digest))
        cls = scenario.ReturnPhaseProcessingTest
        cls.setUpClass()
        case = cls("test_normal_roundtrip_baseline")
        try:
            result = case.run()
            self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        finally:
            cls.tearDownClass()
