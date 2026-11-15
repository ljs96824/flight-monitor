"""R38: four independent tracks, with 22 semantic RED groups and two guards."""

import ast
import copy
from contextlib import ExitStack, closing, redirect_stdout
from datetime import date, datetime, timedelta, timezone
import hashlib
import importlib
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import analyzer
import collector
import config_loader
import flight_time
import notifier
import observations_store
import storage
from sources import juhe_source


ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-11-15T06:00:00+08:00"
PROFILE = {"transfer_policy": "reasonable", "travel_scenario": "business", "red_eye": "accept",
           "time_preference_mode": "unlimited", "allow_self_transfer": True, "allow_overnight_transfer": True}
PARSING = (("2小时30分", 150), ("2小时30分钟", 150), ("2时5分", 125), ("45分", 45),
           ("02:30", 150), ("25:45", 1545), ("PT2H30M", 150), ("2h30m", 150),
           ("2小时半", 150), ("45m", 45), (150, 150), (150.4, 150), ("150.4", 150),
           (None, None), (True, None), (False, None), (float("nan"), None),
           (float("inf"), None), (0, None), (-5, None), ("0h0m", None), ("02:75", None),
           ("A320m", None), ("PT", None), ("PT0H0M", None), ("", None), ({}, None))


def _segment(number, origin, departure, destination, arrival, duration=120):
    return {"flight_no": number, "airline": "ANA", "duration_min": duration,
            "dep_airport": origin, "dep_time": departure, "arr_airport": destination,
            "arr_time": arrival, "aircraft": "A320"}


def _flight(waits=(120,), *, combo="NH100+NH200", duration=420):
    segments = [_segment("NH100", "PVG", "2026-12-01 08:00", "HKG", "2026-12-01 10:00"),
                _segment("NH200", "HKG", "2026-12-01 12:00", "KIX", "2026-12-01 16:00", 180)]
    if len(waits) == 2:
        segments[1]["arr_airport"] = "NRT"
        segments.append(_segment("NH300", "NRT", "2026-12-01 18:00", "KIX", "2026-12-01 19:30", 90))
    return {"flight_combo": combo, "price": 800, "total_duration_min": duration,
            "stops": len(segments) - 1, "cabin_class": "economy", "airlines": ["ANA"],
            "data_source": "juhe", "source": "juhe", "collected_at": STAMP,
            "segments": segments, "layovers": [dict(airport=s["arr_airport"], wait_minutes=w)
                                                  for s, w in zip(segments, waits)],
            "route_summary": "PVG-HKG-KIX"}


def _raw_segment(number, origin, departure, destination, arrival, duration="2h00m", airline="NH"):
    return {"flightNo": number, "airline": airline, "airlineName": airline,
            "departure": origin, "departureDate": departure[:10], "departureTime": departure[11:],
            "arrival": destination, "arrivalDate": arrival[:10], "arrivalTime": arrival[11:],
            "flightTime": duration, "equipment": "320", "departureName": origin, "arrivalName": destination}


def _raw(*, transfer=True, arrival="2026-12-01 10:00", departure="2026-12-01 12:00", a="HKG", b="HKG"):
    segments = [_raw_segment("NH100", "PVG", "2026-12-01 08:00", a, arrival),
                _raw_segment("NH200", b, departure, "KIX", "2026-12-01 16:00", "3h00m")]
    return {"flightNo": "NH100 | NH200" if transfer else "NH100", "airline": "NH", "airlineName": "ANA",
            "departure": "PVG", "arrival": "KIX", "departureDate": "2026-12-01", "departureTime": "08:00",
            "arrivalDate": "2026-12-01", "arrivalTime": "16:00", "duration": "7h00m",
            "ticketPrice": 800, "transferNum": 2 if transfer else 1, "segments": segments if transfer else []}


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = datetime(2026, 11, 15, 6, 0, tzinfo=timezone(timedelta(hours=8)))
        return instant.replace(tzinfo=None) if tz is None else instant.astimezone(tz)


class _ContractBase(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {"NO_LIVE_API": "1", "PYTHON_DOTENV_DISABLED": "1"}))
        self.stack.enter_context(patch.object(config_loader, "RUNTIME_CONFIG_PATH", self.root / "absent.yaml"))
        self.stack.enter_context(patch.object(storage, "DB_PATH", self.root / "prices.db"))
        self.stack.enter_context(patch.object(storage, "safe_log"))
        self.stack.enter_context(patch.object(analyzer, "datetime", _FixedDateTime))
        self.stack.enter_context(patch.object(analyzer, "shanghai_today", return_value=date(2026, 11, 15)))
        self.stack.enter_context(patch.object(analyzer, "get_target_history", return_value=[]))
        self.stack.enter_context(patch.object(analyzer, "get_latest_alternatives", return_value=[]))
        self.log = self.stack.enter_context(patch.object(analyzer, "safe_log"))
        self.stack.enter_context(patch.object(juhe_source, "safe_log"))
        self.stack.enter_context(patch.object(notifier, "get_last_push_price", return_value=None))
        self.stack.enter_context(patch.object(notifier, "get_last_push_snapshot", return_value=None))
        self.stack.enter_context(patch.object(notifier, "track_plan_status", return_value={}))
        self.stack.enter_context(patch.object(notifier, "safe_log"))
        for target in ("socket.create_connection", "socket.socket.connect", "smtplib.SMTP", "smtplib.SMTP_SSL"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("R38 must be offline")))
        tokens = observations_store.set_current_round("collection_r38_" + self.id(), self.root / "observations.sqlite3")
        self.addCleanup(observations_store.reset_current_round, tokens)

    def utils(self):
        self.assertIsNotNone(importlib.util.find_spec("duration_utils"), "R38_DURATION_API_MISSING")
        return importlib.import_module("duration_utils")

    def safety(self, flight, threshold=120):
        self.assertTrue(hasattr(analyzer, "evaluate_connection_safety"), "R38_SAFETY_API_MISSING")
        return analyzer.evaluate_connection_safety(flight, threshold)

    def normalize(self, raw):
        source = juhe_source.JuheSource.__new__(juhe_source.JuheSource)
        return source.normalize([copy.deepcopy(raw)], collected_at=STAMP)[0]

    def analyze(self, flights, preferences=None, **kwargs):
        with redirect_stdout(io.StringIO()):
            return analyzer.analyze_all_flights(flights, user_preferences=preferences or {}, **kwargs)


class TestTrack1DurationParsingAndRecovery(_ContractBase):
    def test_g01_anchored_duration_matrix(self):
        parse = self.utils().parse_duration_minutes
        for value, expected in PARSING:
            with self.subTest(value=repr(value)):
                self.assertEqual(parse(value), expected)

    def test_g02_both_existing_callers_agree(self):
        for value, expected in PARSING:
            with self.subTest(value=repr(value)):
                try:
                    source_minutes = juhe_source._duration_minutes(value)
                    observed_minutes = observations_store._parse_duration_minutes(value)
                except (TypeError, ValueError, OverflowError) as exc:
                    self.fail(f"Duration caller rejected {value!r}: {type(exc).__name__}: {exc}")
                self.assertEqual(source_minutes, expected)
                self.assertEqual(observed_minutes, expected)

    def test_g03_explicit_none_can_recover_from_alternate_fields(self):
        for key, value, expected in (("duration_str", "02:30", 150), ("duration_str", "2小时30分", 150), ("duration_hours", 2.5, 150)):
            with self.subTest(key=key, value=value):
                flight = _flight(duration=None)
                flight[key] = value
                self.assertEqual(collector._normalize_detail_flight(flight)["total_duration_min"], expected)

    def test_g04_complete_segments_and_all_connections(self):
        flight = _flight(duration=None)
        detail = collector._normalize_detail_flight(flight)
        self.assertEqual(detail["total_duration_min"], 420)
        self.assertEqual(detail["total_hours"], 7)

    def test_g05_endpoint_recovery_requires_known_offsets(self):
        flight = _flight(duration=None)
        flight["segments"][1]["duration_min"] = None
        self.assertEqual(collector._normalize_detail_flight(copy.deepcopy(flight))["total_duration_min"], 420)
        for origin, destination in (("LHR", "JFK"), ("XXX", "YYY")):
            with self.subTest(origin=origin):
                flight["segments"][0]["dep_airport"] = origin
                flight["segments"][-1]["arr_airport"] = destination
                detail = collector._normalize_detail_flight(copy.deepcopy(flight))
                self.assertIsNone(detail["total_duration_min"])
                self.assertIsNone(detail["total_hours"])
        utils = self.utils()
        self.assertEqual(utils.compute_endpoint_elapsed_minutes("2026-12-01T08:00:00+08:00", "XXX", "2026-12-01T11:00:00+09:00", "YYY"), 120)
        self.assertIsNone(utils.compute_endpoint_elapsed_minutes("08:00", "PVG", "11:00", "KIX"))
        self.assertIsNone(utils.resolve_fixed_utc_offset_minutes("LHR"))

    def test_g06_incomplete_evidence_never_sums_a_partial_duration(self):
        for reason in ("timezone_unverified", "segment_time_inverted"):
            with self.subTest(reason=reason):
                flight = _flight(duration=None)
                flight["segments"][0]["duration_min"] = None
                flight["segments"][0]["dep_airport"] = "LHR"
                flight["layovers"] = [{"wait_unverified_reason": reason}]
                detail = collector._normalize_detail_flight(flight)
                self.assertIsNone(detail["total_hours"])
                self.assertIsNone(detail["total_duration_min"])

    def test_g07_explicit_duration_wins_and_conflict_is_preserved(self):
        flight = _flight(duration=480)
        detail = collector._normalize_detail_flight(flight)
        self.assertIn("duration_conflict", detail)
        self.assertEqual(detail["total_duration_min"], 480)
        self.assertEqual(detail["duration_conflict"], {"explicit_min": 480, "derived_min": 420, "basis": "complete_segments_and_layovers"})


class TestTrack2JuheSegmentsAndAirportChange(_ContractBase):
    def test_g08_raw_segments_codeshares_and_direct_keys(self):
        raw = _raw()
        raw["flightNo"] = "CA8884(SC8086) | CA4635(SC1165)"
        raw["segments"][0].update(opAirline="SC", opFlightNo="SC8086", isCodeShare=True)
        row = self.normalize(raw)
        self.assertEqual(len(row["segments"]), 2)
        self.assertEqual((row["flight_combo"], row["flight_no"]), (raw["flightNo"], raw["flightNo"]))
        self.assertEqual(row["segments"][0]["operating_flight_no"], "SC8086")
        self.assertTrue(row["segments"][0]["is_codeshare"])
        self.assertEqual(row["segments"][0]["dep_time"], "2026-12-01 08:00")
        self.assertEqual(row["segments"][0]["airline_code"], "NH")
        self.assertEqual(row["airlines"], ["NH"])
        direct = self.normalize(_raw(transfer=False))
        self.assertNotIn("airlines", direct)
        self.assertEqual(len(direct["segments"]), 1)
        self.assertEqual(direct["layovers"], [])

    def test_g09_same_clock_cross_airport_connections(self):
        for a, b in (("NRT", "HND"), ("ICN", "GMP"), ("TSA", "TPE"), ("PKX", "PEK"), ("TFU", "CTU"), ("BKK", "DMK")):
            with self.subTest(a=a, b=b):
                row = self.normalize(_raw(a=a, b=b))
                self.assertEqual(len(row["layovers"]), 1)
                layover = row["layovers"][0]
                self.assertEqual(layover["wait_minutes"], 120)
                self.assertTrue(layover["airport_change"])
                self.assertEqual(layover["next_airport"], b)
                self.assertTrue(analyzer._has_airport_change_transfer(row))
        raw = _raw(departure="2026-12-01 22:00")
        raw["segments"][1].update(airline="OZ", airlineName="Asiana")
        row = self.normalize(raw)
        self.assertTrue(analyzer._is_likely_self_transfer(row))
        kept, excluded, _ = analyzer._apply_user_preferences([row], {**PROFILE, "allow_overnight_transfer": False})
        self.assertEqual(kept, [])
        self.assertIn("过夜", excluded[0]["exclude_reason"])

    def test_g10_zero_minutes_has_provenance_then_consumes_marker(self):
        row = self.normalize(_raw(departure="2026-12-01 10:00"))
        self.assertEqual(len(row["layovers"]), 1)
        self.assertEqual(row["layovers"][0]["wait_minutes"], 0)
        self.assertIs(row["layovers"][0]["_wait_computed"], True)
        detail = collector._normalize_detail_flight(row)
        self.assertEqual(detail["layovers"][0]["wait_minutes"], 0)
        self.assertNotIn("_wait_computed", detail["layovers"][0])
        self.assertEqual(self.safety(detail)["status"], "insufficient")
        with redirect_stdout(io.StringIO()):
            analyzer.analyze_all_flights([copy.deepcopy(detail)], user_preferences={"red_eye": "accept", "allow_self_transfer": True, "allow_overnight_transfer": True})

    def test_g11_signed_inverted_unknown_and_missing_clocks(self):
        for raw, reason in ((_raw(departure="2026-12-01 09:17"), "segment_time_inverted"),
                            (_raw(a="LHR", b="JFK"), "timezone_unverified")):
            with self.subTest(reason=reason):
                row = self.normalize(raw)
                self.assertEqual(len(row["layovers"]), 1)
                detail = collector._normalize_detail_flight(row)
                self.assertNotIn("wait_minutes", detail["layovers"][0])
                self.assertEqual(detail["layovers"][0]["wait_unverified_reason"], reason)
        raw = _raw()
        raw["segments"][0]["arrivalDate"] = ""
        row = self.normalize(raw)
        self.assertEqual(len(row["layovers"]), 1)
        self.assertEqual(row["layovers"][0]["wait_unverified_reason"], "timestamp_missing")

    def test_g12_source_to_notification_reference_chain(self):
        row = self.normalize(_raw(departure="2026-12-01 09:17"))
        detail = collector._normalize_detail_flight(row)
        analysis = self.analyze([detail], PROFILE)
        self.assertEqual(len(analysis.get("reference_flights", [])), 1)
        self.assertEqual(analysis.get("all_flights"), [])
        self.assertEqual(analysis.get("roundtrip_candidate_flights"), [])
        reference = analysis["reference_flights"][0]
        self.assertNotIn("wait_minutes", reference["layovers"][0])
        self.assertIn("航段时间顺序异常", reference["reference_reason"])
        with redirect_stdout(io.StringIO()):
            payload = notifier.build_notification_payload(analysis, route_info={"origin": "PVG", "destination": "KIX"})
            html = notifier.render_detail_html(payload)
        self.assertIn("航段时间顺序异常", html)

    def test_g13_short_aliases_never_bypass_airport_change_rule(self):
        flight = _flight((375,))
        flight["segments"][0]["arr_airport"] = " nrt "
        flight["segments"][1]["dep_airport"] = "hnd"
        self.assertTrue(analyzer._has_airport_change_transfer(flight))
        family = {**PROFILE, "companions": "with_child", "passengers": {"adult": 1, "child": 1}}
        kept, excluded, _ = analyzer._apply_user_preferences([flight], family)
        self.assertEqual(kept, [])
        self.assertIn("换机场", excluded[0]["exclude_reason"])


class TestTrack3SafetyGatesAndReferenceIsolation(_ContractBase):
    def test_g14_known_short_waits_exclude_under_each_gate(self):
        self.safety(_flight())
        for waits in ((0,), (-5,), (0.5,), (30,), (0, 120)):
            for profile in (PROFILE, {**PROFILE, "companions": "with_child", "passengers": {"adult": 1, "child": 1}}):
                with self.subTest(waits=waits, family="passengers" in profile):
                    kept, excluded, _ = analyzer._apply_user_preferences([_flight(waits)], profile)
                    self.assertEqual(kept, [])
                    self.assertEqual(len(excluded), 1)
        for invalid in (True, float("nan"), float("inf")):
            self.assertEqual(self.safety(_flight((invalid,)))["status"], "unverified")

    def test_g15_missing_incomplete_and_misaligned_connections(self):
        self.safety(_flight())
        for lays in ([], [{}], [{"wait_minutes": None}], [None], [{"wait_minutes": 120}]):
            with self.subTest(layovers=lays):
                flight = _flight((120, 120))
                flight["layovers"] = lays
                self.assertEqual(self.safety(flight)["status"], "unverified")
                kept, _, summary = analyzer._apply_user_preferences([flight], PROFILE)
                self.assertEqual(kept, [])
                self.assertEqual(len(summary["reference_flights"]), 1)
        flight = _flight((120, 120))
        flight["layovers"] = [{"airport": "HKG", "wait_minutes": 120}] * 2
        self.assertEqual(self.safety(flight)["status"], "unverified")

    def test_g16_known_violation_wins_over_unknown(self):
        flight = _flight((30, None))
        self.assertEqual(self.safety(flight)["status"], "insufficient")
        self.assertEqual(self.safety(_flight((120, None)))["status"], "unverified")
        self.assertEqual(self.safety(_flight((120, 120)))["status"], "ok")
        self.assertEqual(self.safety(_flight((90,)), 90)["status"], "ok")
        self.assertEqual(self.safety(_flight((90,)), 120)["status"], "insufficient")

    def test_g17_direct_exemption_and_ordinary_adult_risk(self):
        direct = _flight()
        direct.update(stops=0, segments=[direct["segments"][0]], layovers=[])
        self.assertEqual(self.safety(direct)["status"], "not_applicable")
        flight = _flight()
        flight["layovers"] = []
        kept, excluded, _ = analyzer._apply_user_preferences([flight], {"red_eye": "accept", "allow_self_transfer": True})
        self.assertEqual(len(kept), 1)
        self.assertEqual(excluded, [])
        self.assertIn("中转等待时间资料不完整，无法核实衔接时间，请核对航段详情。", analyzer.calc_transfer_risk(flight)["factors"])
        self.assertGreaterEqual(analyzer.calc_transfer_risk(flight)["score"], 40)

    def test_g18_price_classification_duration_reference_and_log_bound(self):
        missing = _flight(duration=None)
        original = copy.deepcopy(missing)
        result = self.analyze([missing])
        self.assertEqual(result["error"], "no_actionable_flights")
        self.assertEqual(missing, original)
        self.assertEqual(self.analyze([])["error"], "no_flights")
        for price in (0, -100, None, True, float("nan"), float("inf")):
            with self.subTest(price=repr(price)):
                flight = _flight()
                flight["price"] = price
                result = self.analyze([flight])
                self.assertEqual(result["error"], "no_valid_prices")
                self.assertEqual(result.get("reference_flights", []), [])
        result = self.analyze([_flight(), _flight(duration=None, combo="NH999")], {"red_eye": "accept", "allow_self_transfer": True})
        self.assertEqual(len(result["reference_flights"]), 1)
        self.assertEqual(len(result["all_flights"]), 1)
        self.analyze([_flight(duration=None, combo=f"NH{i}") for i in range(30)])
        self.assertLessEqual(sum("[时长缺失参考]" in str(c) for c in self.log.call_args_list), 10)

    def test_g19_double_missing_reason_is_single_and_idempotent(self):
        for inverted in (False, True):
            with self.subTest(inverted=inverted):
                flight = _flight(duration=None)
                flight["layovers"] = [{"wait_unverified_reason": "segment_time_inverted"}] if inverted else []
                result = self.analyze([copy.deepcopy(flight), copy.deepcopy(flight)], PROFILE)
                self.assertEqual(len(result.get("reference_flights", [])), 1)
                reason = result["reference_flights"][0]["reference_reason"]
                self.assertIn("总行程时长缺失", reason)
                self.assertIn("航段时间顺序异常" if inverted else "中转衔接时间待确认", reason)

    def test_g20_reference_pool_cannot_supply_recommendations_or_history(self):
        flight = _flight(duration=None)
        result = self.analyze([flight], PROFILE)
        self.assertEqual(len(result.get("reference_flights", [])), 1)
        refs = result["reference_flights"]
        self.assertEqual(analyzer.select_recommendations(refs, [])[0], [])
        self.assertEqual(analyzer._top_flights_for_round_trip(result), [])
        main = importlib.import_module("main")
        self.assertEqual(main._constraint_history_flights(result), [])
        diagnostic = analyzer.build_no_result_diagnosis(refs)
        self.assertEqual(diagnostic["reason"], "暂无满足当前要求的可执行方案")
        alternatives = analyzer.build_no_result_alternatives(refs)
        self.assertEqual(alternatives[0]["unmet_reason"], refs[0]["reference_reason"])
        self.assertEqual(result["no_primary_reason"], "暂无满足当前要求的可执行方案")

    def test_g21_low_price_early_states_and_zero_database_writes(self):
        flight = _flight()
        flight["layovers"] = []
        result = self.analyze([flight], {**PROFILE, "budget_strategy": "low_price_alert"})
        self.assertEqual(len(result.get("reference_flights", [])), 1)
        self.assertEqual(result["low_price_alert_state"], "unknown")
        with closing(sqlite3.connect(self.root / "prices.db")) as connection:
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
        from tests import test_low_price_same_caliber_history as history
        market = history.market()
        market[1]["outbound"] = result
        self.assertFalse(history.offer(market)["can_evaluate_current"])
        self.assertEqual(analyzer.select_recommendations([result["reference_flights"][0]], [])[0], [])
        baggage = _flight()
        baggage["layovers"] = []
        baggage["extra"] = {"fare_rules": {"baggage": {"included": True}}}
        for preferences in (PROFILE, {**PROFILE, "need_baggage": "required"}):
            rejected = self.analyze([copy.deepcopy(baggage)], preferences)
            self.assertEqual(rejected.get("all_flights", []), [])
        self.assertEqual(self.analyze([_flight(duration=None)])["low_price_alert_state"], "not_applicable")

    def test_g22_four_real_cached_payload_shapes(self):
        # Field-for-field representatives from the locked local Juhe cache audit.
        direct = {"airline": "9C", "flightNo": "9C6576", "departure": "KIX", "arrival": "PVG", "departureDate": "2026-10-06", "departureTime": "12:20", "arrivalDate": "2026-10-06", "arrivalTime": "13:50", "duration": "02h30m", "transferNum": 1, "ticketPrice": 3242, "segments": []}
        cross = {"airline": "OZ", "flightNo": "OZ0117 | OZ3615", "departure": "KIX", "arrival": "SHA", "departureDate": "2026-10-06", "departureTime": "00:15", "arrivalDate": "2026-10-06", "arrivalTime": "09:45", "duration": "10h30m", "transferNum": 2, "ticketPrice": 3223,
                 "segments": [_raw_segment("OZ0117", "KIX", "2026-10-06 00:15", "ICN", "2026-10-06 02:15", "02h00m", "OZ"), _raw_segment("OZ3615", "GMP", "2026-10-06 08:30", "SHA", "2026-10-06 09:45", "02h15m", "OZ")]}
        zero = {"airline": "JL", "flightNo": "JL080 | JL3009", "departure": "PVG", "arrival": "ITM", "departureDate": "2026-09-03", "departureTime": "09:05", "arrivalDate": "2026-09-03", "arrivalTime": "19:50", "duration": "9h45m", "transferNum": 2, "ticketPrice": 3549,
                "segments": [_raw_segment("JL080", "PVG", "2026-09-03 09:05", "HND", "2026-09-03 13:00", "2h55m", "JL"), _raw_segment("JL3009", "NRT", "2026-09-03 18:25", "ITM", "2026-09-03 19:50", "0h0m", "JL")]}
        inverted = {"airline": "KE", "flightNo": "KE0722 | OZ0365", "departure": "KIX", "arrival": "PVG", "departureDate": "2026-10-06", "departureTime": "09:05", "arrivalDate": "2026-10-06", "arrivalTime": "15:00", "duration": "06h55m", "transferNum": 2, "ticketPrice": 3885,
                    "segments": [_raw_segment("KE0722", "KIX", "2026-10-06 09:05", "ICN", "2026-10-06 10:55", "01h50m", "KE"), _raw_segment("OZ0365", "ICN", "2026-10-06 10:12", "PVG", "2026-10-06 15:00", "05h48m", "OZ")]}
        self.assertEqual(collector._normalize_detail_flight(self.normalize(direct))["total_duration_min"], 150)
        row = self.normalize(cross)
        self.assertEqual(len(row["segments"]), 2)
        self.assertEqual(row["layovers"][0]["wait_minutes"], 375)
        self.assertTrue(analyzer._has_airport_change_transfer(row))
        row = self.normalize(zero)
        self.assertIsNone(row["segments"][1]["duration_min"])
        self.assertEqual(collector._normalize_detail_flight(row)["total_duration_min"], 585)
        row = collector._normalize_detail_flight(self.normalize(inverted))
        self.assertNotIn("wait_minutes", row["layovers"][0])
        self.assertEqual(row["layovers"][0]["wait_unverified_reason"], "segment_time_inverted")


class TestTrack4FrozenAndAstGuards(unittest.TestCase):
    def test_g23_ast_anchors_are_unchanged(self):
        expected = {"_min_layover_minutes": "bd83c072300ff407f3841d9cbf64647efc4cc98dcb2ca2c95b12a5f5852a69e0", "calc_transfer_risk": "97e57a332b0c3ba0d0eb35634b8be3b72ed57579bfc8b58efbeb78a1f52003dc", "overall_score": "fc36ef0220e5004a86c5cac1ca65524af67b73751c718a51ba967c1f10e41a37", "parse_flight_datetime": "deb57b9d436751899f21cc8f4a00d3ab395bd54323d843f23ac3681887e942cd", "calculate_layover_minutes": "3746b6ce100f10221b2ef3c9dfa7bd187e81bd2c9b0ca4c23893578ed534a298"}
        for module, names in ((analyzer, ("_min_layover_minutes", "calc_transfer_risk", "overall_score")), (flight_time, ("parse_flight_datetime", "calculate_layover_minutes"))):
            for name in names:
                node = ast.parse(textwrap.dedent(inspect.getsource(getattr(module, name)))).body[0]
                self.assertEqual(hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest(), expected[name])
        tree = ast.parse(textwrap.dedent(inspect.getsource(collector._normalize_detail_flight)))
        markers = [n for n in ast.walk(tree) if isinstance(n, ast.If) and "_wait_computed" in ast.unparse(n.test)]
        self.assertEqual(len(markers), 1)
        self.assertEqual(hashlib.sha256(ast.dump(markers[0], include_attributes=False).encode()).hexdigest(), "1e10cc31f7bd83f33e24bb0b8eb17dbeff6faab0aa01dcef011b2a5188febeba")
        import test_legacy_notification_renderer_retirement as locked
        source = (ROOT / "notifier.py").read_text(encoding="utf-8")
        nodes = {node.name: node for node in ast.parse(source).body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name, expected_hash in locked.EXPECTED_MAIN_CHAIN_SHA256.items():
            node = nodes[name]
            self.assertEqual(hashlib.sha256(ast.get_source_segment(source, node).encode("utf-8")).hexdigest(), expected_hash)

    def test_g24_frozen_carriers_and_normal_roundtrip(self):
        payload = json.loads((ROOT / "tests/fixtures/frozen_email/economy_payload.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp, patch.object(config_loader, "RUNTIME_CONFIG_PATH", Path(tmp) / "absent.yaml"), redirect_stdout(io.StringIO()):
            _, email = notifier.render_email(copy.deepcopy(payload))
            push = notifier.prepare_push_render(notifier.render_pushplus_sections(copy.deepcopy(payload))).content
            detail = notifier.render_detail_html(copy.deepcopy(payload))
        for content, size, digest in ((email, 53307, "1dd353d2b7bcafc02087957ccccb6661478e20472a22fb3e15dae6a56473ee6b"), (push, 5552, "2ac0e80d9f82dbe7ebccf524c0832f09d42d916212b6178316b0b7bcd60574ff"), (detail, 54248, "e31b8728b8d0bc05c9bbd4bd99c299aaf6dca7b66674b6c560ec089b1681ffa6")):
            raw = content.encode()
            self.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()), (size, digest))
        from tests import test_return_phase_processing as scenario
        cls = scenario.ReturnPhaseProcessingTest
        cls.setUpClass()
        case = cls("test_normal_roundtrip_baseline")
        try:
            case.setUp()
            try:
                case.test_normal_roundtrip_baseline()
            finally:
                case.tearDown()
                case.doCleanups()
        finally:
            cls.tearDownClass()


if __name__ == "__main__":
    unittest.main()
