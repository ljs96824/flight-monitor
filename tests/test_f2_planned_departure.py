"""F2：计划起飞时刻已过的航班不得作为首选或可选方案；按出发机场当地时区比较，不推断实际起飞。"""

from __future__ import annotations

import ast
import pathlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

import departure_guard
import main
import notifier
from airports import AIRPORT_IANA_TIMEZONE
from analyzer import analyze_all_flights
from departure_guard import (
    PASSED_REASON,
    PASSED_REASON_CODE,
    STATUS_KEY,
    STATUS_PASSED,
    STATUS_UNKNOWN,
    STATUS_UPCOMING,
    UNKNOWN_NOTE,
    planned_departure_status,
    resolve_timezone,
    split_by_planned_departure,
)
from project_time import SHANGHAI_TZ

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHANGHAI_1500 = datetime(2026, 10, 1, 15, 0, tzinfo=timezone(timedelta(hours=8)))
SHANGHAI_1500_OCT6 = datetime(2026, 10, 6, 15, 0, tzinfo=timezone(timedelta(hours=8)))


def _flight(combo, dep, arr, *, origin="PVG", dest="KIX", price=1000.0, segments=None):
    return {
        "flight_no": combo,
        "flight_combo": combo,
        "airline": "测试航空",
        "airline_summary": "测试航空",
        "airlines": ["测试航空"],
        "price": price,
        "stops": max(len(segments or []) - 1, 0),
        "total_duration_min": 150,
        "route_summary": combo,
        "departure_time": dep,
        "arrival_time": arr,
        "departure_airport": origin,
        "arrival_airport": dest,
        "segments": segments
        or [
            {
                "flight_no": combo,
                "airline": "测试航空",
                "dep_airport": origin,
                "arr_airport": dest,
                "dep_time": dep,
                "arr_time": arr,
            }
        ],
    }


def _combos(analysis, key):
    return {item.get("flight_combo") for item in analysis.get(key) or []}


class TimezoneResolutionTest(unittest.TestCase):
    def test_every_airport_timezone_resolves(self):
        unresolved = sorted({name for name in AIRPORT_IANA_TIMEZONE.values() if resolve_timezone(name) is None})
        self.assertEqual(unresolved, [])

    def test_fallback_without_system_zoneinfo_keeps_offsets(self):
        with patch.object(departure_guard, "ZoneInfo", side_effect=ZoneInfoNotFoundError("Asia/Tokyo")):
            tokyo = resolve_timezone("Asia/Tokyo")
            new_york = resolve_timezone("America/New_York")
        self.assertEqual(datetime(2026, 10, 1, 12, tzinfo=tokyo).utcoffset(), timedelta(hours=9))
        self.assertEqual(datetime(2026, 7, 1, 12, tzinfo=new_york).utcoffset(), timedelta(hours=-4))
        self.assertEqual(datetime(2026, 1, 15, 12, tzinfo=new_york).utcoffset(), timedelta(hours=-5))

    def test_unknown_name_returns_none(self):
        self.assertIsNone(resolve_timezone(""))
        self.assertIsNone(resolve_timezone(None))
        self.assertIsNone(resolve_timezone("Mars/Olympus_Mons"))


class PlannedDepartureStatusTest(unittest.TestCase):
    def test_fm821_departed_in_shanghai_is_passed(self):
        flight = _flight("FM821", "2026-10-01 14:45", "2026-10-01 18:00")
        self.assertEqual(planned_departure_status(flight, SHANGHAI_1500), STATUS_PASSED)

    def test_departure_airport_local_time_is_used(self):
        # 关西 15:30 = 上海 14:30，上海 15:00 时已过；若误按上海时间解读会判为未过。
        flight = _flight("JL892", "2026-10-06 15:30", "2026-10-06 17:30", origin="KIX", dest="PVG")
        self.assertEqual(planned_departure_status(flight, SHANGHAI_1500_OCT6), STATUS_PASSED)
        later = _flight("JL894", "2026-10-06 16:30", "2026-10-06 18:30", origin="KIX", dest="PVG")
        self.assertEqual(planned_departure_status(later, SHANGHAI_1500_OCT6), STATUS_UPCOMING)

    def test_boundary_equal_counts_as_passed(self):
        flight = _flight("MU1", "2026-10-01 15:00", "2026-10-01 18:00")
        self.assertEqual(planned_departure_status(flight, SHANGHAI_1500), STATUS_PASSED)

    def test_transfer_uses_first_segment(self):
        segments = [
            {"flight_no": "CI157", "dep_airport": "KIX", "arr_airport": "TPE", "dep_time": "2026-10-06 13:10", "arr_time": "2026-10-06 15:00"},
            {"flight_no": "CI503", "dep_airport": "TPE", "arr_airport": "PVG", "dep_time": "2026-10-06 16:30", "arr_time": "2026-10-06 18:30"},
        ]
        flight = _flight("CI157+CI503", "2026-10-06 13:10", "2026-10-06 18:30", origin="KIX", dest="PVG", segments=segments)
        self.assertEqual(planned_departure_status(flight, SHANGHAI_1500_OCT6), STATUS_PASSED)

    def test_explicit_offset_is_respected(self):
        flight = _flight("X1", "2026-10-01T15:30:00+09:00", "2026-10-01T18:00:00+08:00")
        self.assertEqual(planned_departure_status(flight, SHANGHAI_1500), STATUS_PASSED)

    def test_unparseable_time_or_unknown_airport_is_unknown(self):
        bad_time = _flight("X2", "明天上午", "2026-10-01 18:00")
        self.assertEqual(planned_departure_status(bad_time, SHANGHAI_1500), STATUS_UNKNOWN)
        unknown_airport = _flight("X3", "2026-10-01 10:00", "2026-10-01 12:00", origin="ZZZ")
        self.assertEqual(planned_departure_status(unknown_airport, SHANGHAI_1500), STATUS_UNKNOWN)

    def test_split_marks_and_keeps_objects(self):
        passed = _flight("FM821", "2026-10-01 14:45", "2026-10-01 18:00")
        upcoming = _flight("MU2", "2026-10-01 18:00", "2026-10-01 21:00")
        unknown = _flight("X3", "2026-10-01 10:00", "2026-10-01 12:00", origin="ZZZ")
        kept, excluded = split_by_planned_departure([passed, upcoming, unknown], SHANGHAI_1500)
        self.assertEqual([item["flight_combo"] for item in excluded], ["FM821"])
        self.assertEqual(excluded[0]["exclude_reason"], PASSED_REASON)
        self.assertEqual(excluded[0][STATUS_KEY], STATUS_PASSED)
        self.assertIs(kept[0], upcoming)
        self.assertEqual(kept[1][STATUS_KEY], STATUS_UNKNOWN)
        self.assertNotIn(STATUS_KEY, passed)

    def test_naive_reference_now_is_rejected(self):
        with self.assertRaises(ValueError):
            split_by_planned_departure([], datetime(2026, 10, 1, 15, 0))


class AnalyzeAllFlightsDepartureTest(unittest.TestCase):
    def setUp(self):
        self.round = patch("analyzer.get_current_round", return_value=("f2-test", None))
        self.round.start()
        self.addCleanup(self.round.stop)

    def test_departed_flight_is_excluded_from_recommendations(self):
        flights = [
            _flight("FM821", "2026-10-01 14:45", "2026-10-01 18:00", price=800.0),
            _flight("MU2", "2026-10-01 18:00", "2026-10-01 21:00", price=1200.0),
        ]
        analysis = analyze_all_flights(flights, departure_reference_now=SHANGHAI_1500)
        self.assertNotIn("FM821", _combos(analysis, "all_flights"))
        recommended = {(item.get("flight") or {}).get("flight_combo") for item in analysis.get("recommendations") or []}
        self.assertNotIn("FM821", recommended)
        excluded = {item.get("flight_combo"): item for item in analysis.get("excluded_flights") or []}
        self.assertEqual(excluded["FM821"]["reason"], PASSED_REASON)
        self.assertEqual(excluded["FM821"]["flight"]["filter_reason_code"], PASSED_REASON_CODE)

    def test_without_reference_now_behavior_is_unchanged(self):
        flights = [
            _flight("FM821", "2026-10-01 14:45", "2026-10-01 18:00", price=800.0),
            _flight("MU2", "2026-10-01 18:00", "2026-10-01 21:00", price=1200.0),
        ]
        analysis = analyze_all_flights(flights)
        self.assertIn("FM821", _combos(analysis, "all_flights"))

    def test_all_departed_returns_no_flights_with_reasons(self):
        flights = [_flight("FM821", "2026-10-01 14:45", "2026-10-01 18:00")]
        analysis = analyze_all_flights(flights, departure_reference_now=SHANGHAI_1500)
        self.assertEqual(analysis.get("error"), "no_flights")
        reasons = [item.get("reason") for item in analysis.get("excluded_flights") or []]
        self.assertEqual(reasons, [PASSED_REASON])

    def test_unknown_time_flight_stays_selectable_and_marked(self):
        flights = [_flight("X3", "2026-10-01 10:00", "2026-10-01 12:00", origin="ZZZ")]
        analysis = analyze_all_flights(flights, departure_reference_now=SHANGHAI_1500)
        marked = [item for item in analysis.get("all_flights") or [] if item.get("flight_combo") == "X3"]
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0][STATUS_KEY], STATUS_UNKNOWN)


class MainWiringContractTest(unittest.TestCase):
    def test_every_main_analysis_call_passes_reference_now(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "analyze_all_flights"
        ]
        self.assertEqual(len(calls), 3)
        for call in calls:
            keywords = {kw.arg: kw.value for kw in call.keywords}
            self.assertIn("departure_reference_now", keywords)
            value = keywords["departure_reference_now"]
            self.assertEqual(ast.unparse(value), "_departure_reference_now()")

    def test_reference_now_is_shanghai_aware(self):
        value = main._departure_reference_now()
        self.assertIsNotNone(value.utcoffset())
        self.assertEqual(value.utcoffset(), datetime(2026, 10, 1, tzinfo=SHANGHAI_TZ).utcoffset())


class NotifierDisplayTest(unittest.TestCase):
    def test_reason_label_is_registered(self):
        label = notifier._FILTER_REASON_LABELS.get(PASSED_REASON_CODE, "")
        self.assertTrue(label.startswith("计划起飞时刻已过"), label)

    def test_departed_items_are_not_shown_as_cheaper_comparisons(self):
        departed = _flight("CI157+CI503", "2026-10-06 13:10", "2026-10-06 18:30", origin="KIX", dest="PVG", price=3000.0)
        departed.update({STATUS_KEY: STATUS_PASSED, "filter_reason_code": PASSED_REASON_CODE})
        other = _flight("HX619+HX234", "2026-10-06 17:00", "2026-10-06 23:00", origin="KIX", dest="PVG", price=3500.0)
        payload = {
            "current_price": 7769.0,
            "is_roundtrip": False,
            "excluded_plans": [
                {"flight": departed, "price": 3000.0, "reason": PASSED_REASON, "flight_combo": "CI157+CI503", "filter_reason_code": PASSED_REASON_CODE},
                {"flight": other, "price": 3500.0, "reason": "中转时间低于安全下限", "flight_combo": "HX619+HX234"},
            ],
        }
        body = notifier._email_excluded_compact_body(payload)
        self.assertFalse("CI157" in body, "计划起飞时刻已过的条目仍作为更低价对照展示")
        self.assertTrue("HX619" in body, "其他排除对照不应受影响")
        only_departed = dict(payload, excluded_plans=payload["excluded_plans"][:1])
        self.assertTrue("暂无被排除的更低价方案" in notifier._email_excluded_compact_body(only_departed))

    def test_unknown_note_on_plan(self):
        unknown = _flight("X3", "2026-10-01 10:00", "2026-10-01 12:00", origin="ZZZ")
        unknown[STATUS_KEY] = STATUS_UNKNOWN
        self.assertEqual(notifier._plan_departure_unknown_note({"main_flight": unknown}), UNKNOWN_NOTE)
        known = _flight("MU2", "2026-10-01 18:00", "2026-10-01 21:00")
        self.assertEqual(notifier._plan_departure_unknown_note({"main_flight": known}), "")


if __name__ == "__main__":
    unittest.main()
