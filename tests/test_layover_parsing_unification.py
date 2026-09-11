"""Explicit elapsed-time contracts, distinct from analyzer's local-clock projection."""

import ast
import copy
from datetime import datetime, timedelta, timezone
import importlib
import inspect
import json
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from tests import test_layover_timezone as legacy


# Aware expectations use hand-converted UTC instants; mixed inputs are a policy fallback.
MINUTE_CASES = tuple((name, arrival, departure, expected, "independent_utc")
                     for name, arrival, departure, expected in legacy.OFFSET_CASES) + (
    ("same_offset", "2026-03-01T01:30:00+08:00", "2026-03-01T03:00:00+08:00", 90, "independent_utc"),
    ("naive", "2026-03-01 01:30", "2026-03-01 03:00", 90, "local_difference"),
    ("aware_naive", "2026-03-01T01:30:00+08:00", "2026-03-01T03:00:00", 0, "mixed_fallback"),
    ("naive_aware", "2026-03-01T01:30:00", "2026-03-01T03:00:00+08:00", 0, "mixed_fallback"),
    ("cross_day", "2026-03-01T23:30:00+08:00", "2026-03-02T01:00:00+08:00", 90, "independent_utc"),
    ("z_equivalent", "2026-03-01T01:30:00Z", "2026-03-01T03:00:00+00:00", 90, "independent_utc"),
    ("empty", None, "2026-03-01 03:00", 0, "invalid_fallback"),
    ("invalid", "not-a-date", "2026-03-01 03:00", 0, "invalid_fallback"),
    ("negative_naive", "2026-03-01 03:00", "2026-03-01 01:30", 0, "local_difference_clamped"),
    ("seconds", "2026-03-01T01:30:00Z", "2026-03-01T01:31:59Z", 1, "independent_utc_truncated"),
    ("microseconds", "2026-03-01T01:30:00.500Z", "2026-03-01T01:31:00.499Z", 0, "independent_utc_truncated"),
)

# G9: UTC calculations keep offset seconds; display retains the old local minute string.
G9_CASES = (
    ("same_offset", "2026-03-01T01:30:00+08:00:30", "2026-03-01T03:00:00+08:00:30",
     90, "2026-03-01 01:30", "2026-03-01 03:00"),
    ("short_twenty_seconds", "2026-03-01T01:30:00+08:00:10", "2026-03-01T01:31:00+08:00:50",
     0, "2026-03-01 01:30", "2026-03-01 01:31"),
    ("full_precision", "2026-03-01T01:30:50+08:00:30", "2026-03-01T03:00:10+08:00",
     89, "2026-03-01 01:30", "2026-03-01 03:00+08:00"),
    ("negative", "2026-03-01T01:30:00+08:00:10", "2026-03-01T01:30:00+08:00:50",
     0, "2026-03-01 01:30", "2026-03-01 01:30"),
    ("negative_offset", "2026-03-01T01:30:00-04:00:10", "2026-03-01T01:31:00-04:00:50",
     1, "2026-03-01 01:30", "2026-03-01 01:31"),
)


def _serpapi_input(arrival, departure):
    flight = legacy._flight(arrival, departure)
    return {"best_flights": [{"price": flight["price"], "total_duration": 240, "flights": [
        {"flight_number": seg["flight_no"], "airline": seg["airline"], "duration": 60,
         "departure_airport": {"id": seg["dep_airport"], "time": seg["dep_time"]},
         "arrival_airport": {"id": seg["arr_airport"], "time": seg["arr_time"]}}
        for seg in flight["segments"]]}]}


def _duffel_offer(arrival, departure):
    flight = legacy._flight(arrival, departure)
    return {"total_amount": "800", "total_currency": "CNY", "slices": [{"segments": [
        {"marketing_carrier": {"iata_code": "CA", "name": "Air China"},
         "marketing_carrier_flight_number": str(100 + index),
         "origin": {"iata_code": seg["dep_airport"]},
         "destination": {"iata_code": seg["arr_airport"]},
         "departing_at": seg["dep_time"], "arriving_at": seg["arr_time"], "duration": "PT1H"}
        for index, seg in enumerate(flight["segments"])]}]}


class LayoverParsingUnificationTest(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy.LayoverTimezoneTest()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.addCleanup(self.fixture._assert_no_io)
        self.collector = self.fixture.collector
        self.serpapi = importlib.import_module("sources.serpapi_source")
        self.duffel = importlib.import_module("sources.duffel_source")
        self.analyzer = importlib.import_module("analyzer")

    def _shared(self):
        return importlib.import_module("flight_time")

    def _offer(self, offer):
        # Parsing does not use token state; do not call a live adapter constructor.
        return self.duffel.DuffelSource.__new__(self.duffel.DuffelSource)._parse_offer(offer)

    def test_shared_and_wrapper_minute_matrix(self):
        calculators = {"shared": self._shared().calculate_layover_minutes,
                       "collector": self.collector.calc_layover_minutes,
                       "serpapi": self.serpapi._layover_minutes, "duffel": self.duffel._layover_minutes}
        for owner, calculate in calculators.items():
            for name, arrival, departure, expected, basis in MINUTE_CASES:
                with self.subTest(owner=owner, case=name, basis=basis):
                    legacy._assert_minutes(self, calculate, (name, arrival, departure, expected))

    def test_parser_preserves_explicit_offset_without_inference(self):
        parser = self._shared().parse_flight_datetime
        cases = (
            ("2026-03-01T01:30:00+01:00", datetime(2026, 3, 1, 1, 30, tzinfo=timezone(timedelta(hours=1)))),
            ("2026-03-01T01:30:00Z", datetime(2026, 3, 1, 1, 30, tzinfo=timezone.utc)),
            ("2026-03-01T01:30:00.500", datetime(2026, 3, 1, 1, 30, 0, 500000)),
            ("2026-03-01 01:30", datetime(2026, 3, 1, 1, 30)),
            ("01:30", None), (None, None), ("bad-time", None),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                actual = parser(value)
                self.assertEqual(actual, expected)
                if expected is not None:
                    self.assertEqual(actual.utcoffset(), expected.utcoffset())

    def _assert_delegate(self, calculate):
        with patch.object(self._shared(), "calculate_layover_minutes", return_value=317) as spy:
            value = calculate("arrival-sentinel", "departure-sentinel")
        self.assertEqual(spy.call_count, 1, "delegate_not_called")
        spy.assert_called_once_with("arrival-sentinel", "departure-sentinel")
        self.assertEqual(value, 317, "delegate_result_not_forwarded")

    def test_wrappers_delegate_to_one_shared_implementation(self):
        for name, calculate in (("collector", self.collector.calc_layover_minutes),
                                ("serpapi", self.serpapi._layover_minutes),
                                ("duffel", self.duffel._layover_minutes)):
            with self.subTest(owner=name):
                self._assert_delegate(calculate)
        value = datetime(2026, 3, 1, tzinfo=timezone.utc)
        with patch.object(self._shared(), "parse_flight_datetime", return_value=value) as spy:
            self.assertIs(self.serpapi._parse_datetime("parser-sentinel"), value)
        spy.assert_called_once_with("parser-sentinel")

    def test_serpapi_aware_pipeline(self):
        for case in legacy.OFFSET_CASES:
            name, arrival, departure, expected = case
            with self.subTest(case=name):
                flight = self.serpapi.parse_google_flights(
                    _serpapi_input(arrival, departure), "synthetic", date_str="2026-03-01")[0]
                self.assertEqual(flight["layovers"][0]["wait_minutes"], expected, "serpapi_elapsed_minutes")
                self.assertEqual(flight["segments"][0]["arr_time"], arrival)
                self.assertEqual(flight["segments"][1]["dep_time"], departure)
                self.assertEqual(flight["total_duration_min"], 240, "source_duration_still_authoritative")
                self.assertEqual(flight["price"], 800)
                self.assertNotIn("_wait_computed", flight["layovers"][0], "serpapi_remains_unmarked")
                with patch.object(self.collector, "calc_layover_minutes",
                                  wraps=self.collector.calc_layover_minutes) as spy:
                    normalized = self.collector._normalize_detail_flight(copy.deepcopy(flight))
                self.assertEqual(spy.call_count, int(expected == 0))
                self.assertEqual(normalized["layovers"][0]["wait_minutes"], expected, "zero_not_rescued")

    def _assert_serpapi_bare_pipeline(self):
        arrival = "2026-03-01T01:30:00+01:00"
        try:
            flight = self.serpapi.parse_google_flights(
                _serpapi_input(arrival, "01:45"), "synthetic", date_str="2026-03-01")[0]
        except TypeError as error:
            self.fail("aware_naive_comparison: " + str(error))
        self.assertEqual(flight["segments"][1]["dep_time"], "01:45", "bare_time_not_guessed")
        self.assertEqual(flight["layovers"][0]["wait_minutes"], 0)
        with patch.object(self.collector, "calc_layover_minutes",
                          wraps=self.collector.calc_layover_minutes) as spy:
            normalized = self.collector._normalize_detail_flight(copy.deepcopy(flight))
        spy.assert_called_once_with(arrival, "01:45")
        self.assertEqual(normalized["layovers"][0]["wait_minutes"], 0)

    def test_serpapi_aware_then_bare_pipeline(self):
        with self.subTest(case="aware_then_bare"):
            self._assert_serpapi_bare_pipeline()

    def test_serpapi_naive_date_inference_and_midnight(self):
        cases = (("same_day", "2026-03-01 01:30", "01:45", "2026-03-01 01:45", 15),
                 ("midnight", "2026-03-01 23:30", "01:00", "2026-03-02 01:00", 90))
        for name, arrival, departure, formatted, expected in cases:
            with self.subTest(case=name):
                raw = _serpapi_input(arrival, departure)
                segments = raw["best_flights"][0]["flights"]
                segments[0]["departure_airport"]["time"] = "00:30"
                segments[1]["arrival_airport"]["time"] = "02:45"
                result = self.serpapi.parse_google_flights(raw, "synthetic", date_str="2026-03-01")[0]
                self.assertEqual(result["segments"][0]["dep_time"], "2026-03-01 00:30")
                self.assertEqual(result["segments"][1]["dep_time"], formatted)
                self.assertEqual(result["layovers"][0]["wait_minutes"], expected)

    def _assert_duffel_pipeline(self, case):
        name, arrival, departure, expected = case
        flight = self._offer(_duffel_offer(arrival, departure))
        # Input cases have explicitly fixed whole seconds; this expectation is textual, not parsed.
        expected_arrival = arrival.replace("T", " ").replace(":00+", "+").replace(":00-", "-")
        expected_departure = departure.replace("T", " ").replace(":00+", "+").replace(":00-", "-")
        self.assertEqual(flight["segments"][0]["arr_time"], expected_arrival, "duffel_offset_preserved")
        self.assertEqual(flight["segments"][1]["dep_time"], expected_departure, "duffel_offset_preserved")
        self.assertEqual(flight["layovers"][0]["wait_minutes"], expected, "duffel_elapsed_minutes")
        self.assertEqual(flight["total_duration_min"], 120 + expected)
        self.assertEqual(flight["price"], 800)
        self.assertEqual(flight["layovers"][0].get("_wait_computed", False), expected == 0)
        with patch.object(self.collector, "calc_layover_minutes",
                          wraps=self.collector.calc_layover_minutes) as spy:
            result = self.collector._normalize_detail_flight(copy.deepcopy(flight))
        self.assertEqual(spy.call_count, 0)
        self.assertEqual(result["layovers"][0]["wait_minutes"], expected, "zero_not_rescued")
        self.assertNotIn("_wait_computed", result["layovers"][0])
        self.assertEqual(result["total_duration_min"], 120 + expected)
        for actual, local in ((expected_arrival, (2026, 11, 1, 1, 30) if name == "explicit_fallback_75"
                              else (2026, 3, 1, 1, 30)),
                             (expected_departure, (2026, 11, 1, 1, 45) if name == "explicit_fallback_75"
                              else (2026, 3, 1, 3, 0) if name == "overestimate_30" else (2026, 3, 1, 1, 45))):
            projected = self.analyzer.parse_flight_time(actual, "invalid-default-not-used")
            self.assertEqual(projected, datetime(*local), "local_clock_not_utc")
            self.assertIsNone(projected.tzinfo)
        return flight

    def test_duffel_aware_pipeline(self):
        for case in legacy.OFFSET_CASES:
            with self.subTest(case=case[0]):
                self._assert_duffel_pipeline(case)

    def test_duffel_minute_precision_is_not_upgraded(self):
        for suffix in ("", "+08:00", "Z"):
            with self.subTest(suffix=suffix):
                offer = _duffel_offer("2026-03-01T01:30:59" + suffix, "2026-03-01T01:31:00" + suffix)
                flight = self._offer(offer)
                offset = "+00:00" if suffix == "Z" else suffix
                self.assertEqual(flight["segments"][0]["arr_time"], "2026-03-01 01:30" + offset)
                self.assertEqual(flight["segments"][1]["dep_time"], "2026-03-01 01:31" + offset)
                self.assertEqual(flight["layovers"][0]["wait_minutes"], 1, "legacy_minute_quantization")
                self.assertEqual(flight["total_duration_min"], 121)
        self.assertEqual(self.duffel._format_time("bad-time"), "bad-time")
        self.assertEqual(self.duffel._format_time(None), "")

    def test_consumer_keeps_local_date_for_supported_whole_second_formats(self):
        for value in ("2026-03-02T01:45:00+08:00", "2026-03-02T01:45:00+00:00", "2026-03-02T01:45:00Z"):
            with self.subTest(value=value):
                projected = self.analyzer.parse_flight_time(value, "2026-03-01")
                self.assertEqual(projected, datetime(2026, 3, 2, 1, 45))
                self.assertIsNone(projected.tzinfo)

    def test_g9_display_fallback_and_consumer(self):
        for name, arrival, departure, _, arr_display, dep_display in G9_CASES:
            with self.subTest(case=name):
                for value, display in ((arrival, arr_display), (departure, dep_display)):
                    self.assertEqual(self.duffel._format_time(value), display, "g9_display_fallback")
                    parsed = self.analyzer.parse_flight_time(display, "invalid-default-not-used")
                    self.assertEqual(parsed, datetime.strptime(display[:16], "%Y-%m-%d %H:%M"))
                    self.assertIsNone(parsed.tzinfo)

    def test_g9_raw_offer_calculation(self):
        for name, arrival, departure, expected, _, _ in G9_CASES:
            with self.subTest(case=name):
                self.assertEqual(self._shared().calculate_layover_minutes(arrival, departure), expected)
                with patch.object(self.duffel, "_layover_minutes", wraps=self.duffel._layover_minutes) as spy:
                    flight = self._offer(_duffel_offer(arrival, departure))
                spy.assert_called_once_with(arrival, departure)
                self.assertEqual(flight["layovers"][0]["wait_minutes"], expected, "g9_raw_calculation")
                self.assertEqual(flight["total_duration_min"], 120 + expected)
                self.assertEqual(flight["price"], 800)
                self.assertEqual(flight["layovers"][0].get("_wait_computed", False), expected == 0)
                with patch.object(self.collector, "calc_layover_minutes", wraps=self.collector.calc_layover_minutes) as calc:
                    normalized = self.collector._normalize_detail_flight(copy.deepcopy(flight))
                calc.assert_not_called()
                self.assertEqual(normalized["layovers"][0]["wait_minutes"], expected)
                self.assertNotIn("_wait_computed", normalized["layovers"][0])

    def _assert_zero_normalization(self, marked):
        flight = legacy._flight("2026-03-01 01:30", "2026-03-01 01:31")
        flight["layovers"] = [{"airport": "HGH", "wait_minutes": 0}]
        if marked:
            flight["layovers"][0]["_wait_computed"] = True
        normalizer = self.collector._normalize_detail_flight
        with patch.object(self.collector, "calc_layover_minutes", wraps=self.collector.calc_layover_minutes) as spy:
            # Compiled mutants have their own globals; patch the actual lookup site too.
            with patch.dict(normalizer.__globals__, {"calc_layover_minutes": spy}):
                normalized = normalizer(flight)
        self.assertEqual(normalized["layovers"][0]["wait_minutes"], 0 if marked else 1,
                         "computed_zero_preserved" if marked else "unmarked_zero_recalculated")
        self.assertEqual(spy.call_count, 0 if marked else 1)
        self.assertNotIn("_wait_computed", normalized["layovers"][0], "internal_marker_consumed")
        return normalized

    def test_marked_zero_is_preserved(self):
        self._assert_zero_normalization(True)

    def test_unmarked_zero_still_recalculates(self):
        self._assert_zero_normalization(False)

    def test_duffel_cross_offset_short_interval_zero(self):
        # 01:30:00 UTC -> 01:30:20 UTC: independent elapsed time is 20 seconds.
        flight = self._offer(_duffel_offer("2026-03-01T01:30:00+00:00", "2026-03-01T02:30:20+01:00"))
        self.assertEqual(flight["layovers"][0]["wait_minutes"], 0)
        self.assertIs(flight["layovers"][0]["_wait_computed"], True)
        with patch.object(self.collector, "calc_layover_minutes", wraps=self.collector.calc_layover_minutes) as spy:
            normalized = self.collector._normalize_detail_flight(flight)
        spy.assert_not_called()
        self.assertEqual(normalized["layovers"][0]["wait_minutes"], 0)
        self.assertNotIn("_wait_computed", normalized["layovers"][0])

    def test_duffel_uncomputed_fallback_is_not_marked(self):
        for name, arrival, departure in (("mixed", "2026-03-01T01:30:00+08:00", "2026-03-01T03:00:00"),
                                         ("invalid", "bad-arrival", "2026-03-01T03:00:00")):
            with self.subTest(case=name):
                flight = self._offer(_duffel_offer(arrival, departure))
                self.assertEqual(flight["layovers"][0]["wait_minutes"], 0)
                self.assertNotIn("_wait_computed", flight["layovers"][0])
                with patch.object(self.collector, "calc_layover_minutes", wraps=self.collector.calc_layover_minutes) as spy:
                    normalized = self.collector._normalize_detail_flight(flight)
                self.assertEqual(spy.call_count, 1)
                self.assertEqual(normalized["layovers"][0]["wait_minutes"], 0)

    def test_internal_marker_absent_from_delivery_artifacts(self):
        flight = self._assert_zero_normalization(True)
        fixture = Path(__file__).parent / "fixtures" / "frozen_email" / "economy_payload.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        payload["flights"] = [flight]
        notifier = importlib.import_module("notifier")
        pushplus = importlib.import_module("pushplus_sections")
        html = notifier.render_email(payload)[1]
        push = pushplus.prepare_push_render(notifier.render_pushplus_sections(payload)).content
        main = importlib.import_module("main")
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, "PAGE_PAYLOADS_DIR", Path(tmp)), patch.object(
                main, "_upload_payload_to_pythonanywhere", return_value=True) as upload:
            sub_id = "123e4567-e89b-12d3-a456-426614174000"
            self.assertTrue(main._save_result_for_page(sub_id, html, payload))
            stored = (Path(tmp) / (sub_id + ".json")).read_text(encoding="utf-8")
            upload.assert_called_once()
        for name, artifact in (("email", html), ("pushplus", push), ("detail_payload", stored)):
            with self.subTest(carrier=name):
                self.assertNotIn("_wait_computed", artifact)

    def _mutant(self, original, mutation):
        tree = ast.parse(textwrap.dedent(inspect.getsource(original)))
        before = ast.dump(tree)
        matches = []
        if mutation == "drop_shared_offset":
            matches = [n for n in ast.walk(tree) if isinstance(n, ast.Return)
                       and isinstance(n.value, ast.Name) and n.value.id == "parsed"]
            self.assertEqual(len(matches), 1, "shared ISO return must match exactly once")
            matches[0].value = ast.parse("parsed.replace(tzinfo=None)", mode="eval").body
        elif mutation == "bypass_wrapper":
            matches = [n for n in ast.walk(tree) if isinstance(n, ast.Return)
                       and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute)
                       and n.value.func.attr == "calculate_layover_minutes"]
            self.assertEqual(len(matches), 1, "delegation must match exactly once")
            matches[0].value = ast.Constant(0)
        elif mutation == "drop_duffel_offset":
            matches = [n for n in ast.walk(tree) if isinstance(n, ast.Return)
                       and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute)
                       and n.value.func.attr == "isoformat"]
            self.assertEqual(len(matches), 1, "aware formatting must match exactly once")
            matches[0].value = ast.parse('dt.strftime("%Y-%m-%d %H:%M")', mode="eval").body
        elif mutation == "compare_aware_naive":
            matches = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                       and any(isinstance(c, ast.Attribute) and c.attr == "utcoffset" for c in ast.walk(n.test))]
            self.assertEqual(len(matches), 1, "aware-context guard must match exactly once")
            matches[0].test = ast.Constant(False)
        elif mutation == "ignore_computed_zero":
            matches = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                       and any(isinstance(c, ast.Constant) and c.value == "_wait_computed"
                               for c in ast.walk(n.test))]
            self.assertEqual(len(matches), 1, "computed-zero guard must match exactly once")
            matches[0].test = ast.Constant(False)
        else:
            self.fail("unknown mutation")
        self.assertNotEqual(ast.dump(tree), before, "mutation must change real source")
        namespace = dict(original.__globals__)
        code = compile(ast.fix_missing_locations(tree), "<layover-mutation>", "exec")
        exec(code, namespace)
        mutant = namespace[original.__name__]
        mutant.mutation_evidence = {"mutation": mutation, "matched_nodes": len(matches),
                                    "source_changed": True, "compiled": True}
        return mutant

    def test_mutation_shared_offset_loss_is_rejected(self):
        shared = self._shared()
        mutant = self._mutant(shared.parse_flight_datetime, "drop_shared_offset")
        with patch.object(shared, "parse_flight_datetime", mutant):
            for case in legacy.OFFSET_CASES:
                with self.subTest(case=case[0]), self.assertRaisesRegex(AssertionError, case[0]):
                    legacy._assert_minutes(self, shared.calculate_layover_minutes, case)

    def test_mutation_wrapper_bypass_is_rejected(self):
        mutant = self._mutant(self.serpapi._layover_minutes, "bypass_wrapper")
        with self.assertRaisesRegex(AssertionError, "delegate_not_called"):
            self._assert_delegate(mutant)

    def test_mutation_duffel_offset_loss_is_rejected_by_offer_pipeline(self):
        mutant = self._mutant(self.duffel._format_time, "drop_duffel_offset")
        with patch.object(self.duffel, "_format_time", mutant):
            with self.assertRaisesRegex(AssertionError, "duffel_offset_preserved"):
                self._assert_duffel_pipeline(legacy.OFFSET_CASES[0])

    def test_mutation_serpapi_aware_naive_comparison_is_rejected_by_pipeline(self):
        mutant = self._mutant(self.serpapi._normalize_airport_time, "compare_aware_naive")
        with patch.object(self.serpapi, "_normalize_airport_time", mutant):
            with self.assertRaisesRegex(AssertionError, "aware_naive_comparison: can't compare offset-naive and offset-aware datetimes"):
                self._assert_serpapi_bare_pipeline()

    def test_mutation_computed_zero_guard_is_required(self):
        mutant = self._mutant(self.collector._normalize_detail_flight, "ignore_computed_zero")
        with patch.object(self.collector, "_normalize_detail_flight", mutant):
            with self.assertRaisesRegex(AssertionError, "computed_zero_preserved"):
                self._assert_zero_normalization(True)
            self._assert_zero_normalization(False)


if __name__ == "__main__":
    unittest.main()
