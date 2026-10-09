"""Offline contracts for readable degraded-source tracking, with fixed clocks."""

import ast
import copy
import hashlib
import importlib
import io
import json
import re
import tempfile
import types
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch


SUB_ID = "00000000-0000-4000-8000-000000000050"
DEGRADED = {"active": True, "source": "juhe", "source_label": "OTA源"}
EXPECTED_RED_TEST_IDS = (
    "test_c1_single_message", "test_c2_roundtrip_message", "test_c3_default_label",
    "test_c4_missing_single_price", "test_c5_neutral_messages",
    "test_c8_no_question_literals", "test_c9a_email_messages", "test_c9b_return_pushplus_message",
)
# Serialized synthetic outputs captured from de60cc0, not function/source pins.
BASELINE_DIGESTS = {
    "fields-False-True": "088e6294438ab90c90fd7a118f26b7affc61f77814480303dba5e6eeceb8cf12",
    "fields-False-False": "1e9595f4678cda0689fc11bd351d73d9d0352b245b962ea5bf0716749eff70c6",
    "healthy-False-5000-False": "692ab962cbc3a112b483eeeadabb917bc2380ae605c7d95b81a13e8a40b770c4",
    "healthy-False-1000-False": "a66f6735842ed97a5aad5ac80e8cfffa5564bf13b30b77dd7ed0275b0c6ab901",
    "healthy-False-2000-False": "488892fe0720816c311e15bd2b67998e39a202829a5fb43dc1fc1be4c1211ad9",
    "healthy-False-1600-True": "a5136a0ade31e956217fd202fefc64fe02d3bee2583f6fb3b8eea7c2ed878e7a",
    "fields-True-True": "3d688b509c14eb7cacc26d35f76d92918278a6f88ca7808a643779f4f03266c0",
    "fields-True-False": "513dc3d5cd07c06fadd46727c1a42eece40bb1225afed09953ff76ea48f8b6be",
    "healthy-True-5000-False": "c56cbc418a07ede657d3170deb171d146b90dd6c87ba3807f697f3912c966220",
    "healthy-True-1000-False": "781678ad7803a12d22b1496d34370741dda800d9f12b5c2d9246511b2f6af2ca",
    "healthy-True-2760-False": "2f220dac96637a2b39750d05298666eb80973f1576045fd733a1cd6d3c23510e",
    "healthy-True-1600-True": "2d44e69777902d8bf6a64a6483fb91b017a120c5172aa7478d783c35cdbffd5e",
}
BASELINE_PUSHPLUS_DIGESTS = {
    "False": "28a2eb3f16eed5f3bbae606ecd8c2f121f2d132c5c0e67067f36a685fa1b3f3b",
    "True": "121c8089831572d2f9a1312edd05e62fd8ef618ff3bd3d8972603e634588143e",
}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


def tracker_source():
    return Path(importlib.import_module("plan_tracker").__file__).read_text(encoding="utf-8")


class ScenarioDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 2, 16)
        return value if tz is None else value.replace(tzinfo=tz)


def flight(number="JL891", price=1600):
    return {
        "flight_no": number, "flight_combo": number, "price": price, "currency": "CNY",
        "source": "juhe", "data_source": "juhe", "cabin_class": "economy",
        "stops": 0, "total_duration_min": 150,
        "departure_airport": "KIX", "arrival_airport": "PVG",
        "departure_date": "2026-10-06", "departure_time": "13:00", "arrival_time": "15:30",
        "segments": [{"flight_no": number, "dep_airport": "KIX", "arr_airport": "PVG",
                      "dep_time": "2026-10-06 13:00", "arr_time": "2026-10-06 15:30"}],
        "layovers": [], "collected_at": "2026-10-02T16:00:00",
    }


def expected_message(roundtrip=False, label="OTA源", price=1600):
    current = "暂无报价" if price is None else f"¥{price:,.0f}"
    if roundtrip:
        return ("上次推荐:MU5099去+CA1589回,单人往返¥2,760。"
                f"本轮同组合:单人往返{current}。"
                f"{label}本轮不可用,报价覆盖不完整,暂不作涨跌比较。")
    return (f"上次推荐的JL891:上次¥2,000,本轮{current}。"
            f"{label}本轮不可用,报价覆盖不完整,暂不作涨跌比较,建议在渠道核实。")


MUTATION_TARGETS = {
    "m1": (("test_c1_single_message", "SINGLE_APPROVED_TEXT"),
           ("test_c5_neutral_messages", "NEUTRAL_TEXT"),
           ("test_c8_no_question_literals", "NO_QUESTION_LITERALS")),
    "m2": (("test_c2_roundtrip_message", "ROUNDTRIP_APPROVED_TEXT"),
           ("test_c5_neutral_messages", "NEUTRAL_TEXT"),
           ("test_c8_no_question_literals", "NO_QUESTION_LITERALS")),
    "m3": (("test_c3_default_label", "DEFAULT_SOURCE_LABEL"),),
    "m4": (("test_c5_neutral_messages", "NEUTRAL_TEXT"),),
}


def mutated_tracker(module, kind):
    tree = ast.parse(tracker_source())
    before = ast.dump(tree, include_attributes=False)
    name = "_track_roundtrip_plan" if kind == "m2" else "track_plan_status"
    function, = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    # Only the matched-quote degradation branch has both its own label and message.
    branch, = [n for n in function.body if isinstance(n, ast.If)
               and ast.unparse(n.test) == "(source_degradation or {}).get('active')"]
    if kind == "m3":
        label, = [n for n in branch.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "label" for t in n.targets)]
        value, = [n for n in ast.walk(label) if isinstance(n, ast.Constant) and n.value in {"缺失数据源", "?????"}]
        value.value = ""
    else:
        if kind == "m2":
            assignment, = [n for n in branch.body if isinstance(n, ast.Assign)
                            and any(isinstance(t, ast.Name) and t.id == "msg" for t in n.targets)]
            assignment.value = ast.Constant(value="????:?????,??????????")
        else:
            mapping, = [n.value for n in branch.body if isinstance(n, ast.Return)]
            index, = [i for i, key in enumerate(mapping.keys) if isinstance(key, ast.Constant) and key.value == "msg"]
            mapping.values[index] = (ast.Constant(value="?????,??????????") if kind == "m1" else
                                     ast.BinOp(left=mapping.values[index], op=ast.Add(), right=ast.Constant(value="涨价")))
    assert ast.dump(tree, include_attributes=False) != before, "MUTATION_UNCHANGED"
    source = ast.unparse(ast.fix_missing_locations(tree))
    namespace = dict(vars(module))
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<degraded-text-mutant>", "exec"), namespace)
    compiled = namespace[name]
    mutant = types.FunctionType(compiled.__code__, vars(module), name, compiled.__defaults__)
    return name, mutant, source


class PlanTrackerDegradedTextTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.tracker = importlib.import_module("plan_tracker")
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(self.tracker, "DEFAULT_DATA_DIR", self.root))
        self.stack.enter_context(patch.object(self.tracker, "DEFAULT_FEEDBACK_PATH", self.root / "feedback.json"))
        self.stack.enter_context(patch.object(self.tracker, "datetime", ScenarioDatetime))

    def inputs(self, roundtrip=False, price=1600, directory=None):
        if roundtrip:
            previous = {"label": "方案A", "is_roundtrip": True,
                        "outbound_flight": flight("MU5099", 1410),
                        "return_flight": flight("CA1589", 1350),
                        "price_tiers": {"unit_roundtrip": 2760}, "price": 2760}
            items = [{"is_roundtrip": True, "outbound": flight("MU5099", price / 2 if price else None),
                      "return": flight("CA1589", price / 2 if price else None),
                      "price_tiers": {"unit_roundtrip": price}}]
        else:
            previous = {"label": "方案A", "main_flight": flight(price=2000), "price": 2000}
            items = [flight(price=price)]
        self.tracker.save_pushed_plans(SUB_ID, [previous], data_dir=directory or self.root)
        return items

    def track(self, roundtrip=False, price=1600, degraded=None, missing=False):
        items = self.inputs(roundtrip, price)
        return self.tracker.track_plan_status(
            SUB_ID, [] if missing else items, data_dir=self.root,
            source_degradation=copy.deepcopy(DEGRADED if degraded is None else degraded),
        )

    def test_c1_single_message(self):
        self.assertEqual(self.track()["msg"], expected_message(), "SINGLE_APPROVED_TEXT")

    def test_c2_roundtrip_message(self):
        self.assertEqual(self.track(True, 6000)["msg"], expected_message(True, price=6000), "ROUNDTRIP_APPROVED_TEXT")

    def test_c3_default_label(self):
        for roundtrip in (False, True):
            with self.subTest(roundtrip=roundtrip):
                result = self.track(roundtrip, degraded={"active": True})
                self.assertEqual(result["msg"], expected_message(roundtrip, "缺失数据源"), "DEFAULT_SOURCE_LABEL")

    def test_c4_missing_single_price(self):
        result = self.track(price=None)
        self.assertEqual(result["status"], "source_unavailable")
        self.assertIsNone(result["current_price"])
        self.assertEqual(result["msg"], expected_message(price=None), "MISSING_PRICE_TEXT")

    def test_c5_neutral_messages(self):
        for roundtrip in (False, True):
            with self.subTest(roundtrip=roundtrip):
                self.assertIsNone(re.search(r"售罄|停飞|涨价|降价|\?{4,}", self.track(roundtrip)["msg"]), "NEUTRAL_TEXT")

    def test_c6_other_fields_match_baseline(self):
        for roundtrip in (False, True):
            for degraded in (DEGRADED, {"active": True}):
                with self.subTest(roundtrip=roundtrip, label=degraded.get("source_label")):
                    result = self.track(roundtrip, degraded=degraded)
                    result.pop("msg")
                    key = f"fields-{roundtrip}-{bool(degraded.get('source_label'))}"
                    self.assertEqual(hashlib.sha256(encoded(result)).hexdigest(), BASELINE_DIGESTS[key], "OTHER_FIELDS_BASELINE")
                    self.assertEqual(result["status"], "source_unavailable")
                    self.assertIsNone(result["price_diff"])

    def test_c7_healthy_results_match_baseline(self):
        for roundtrip in (False, True):
            for price, missing in ((5000, False), (1000, False), (2760 if roundtrip else 2000, False), (1600, True)):
                with self.subTest(roundtrip=roundtrip, price=price, missing=missing):
                    result = self.track(roundtrip, price, degraded={}, missing=missing)
                    key = f"healthy-{roundtrip}-{price}-{missing}"
                    self.assertEqual(hashlib.sha256(encoded(result)).hexdigest(), BASELINE_DIGESTS[key], "HEALTHY_BASELINE")

    def test_c8_no_question_literals(self):
        bad = [(n.lineno, n.value) for n in ast.walk(ast.parse(tracker_source()))
               if isinstance(n, ast.Constant) and isinstance(n.value, str) and re.search(r"\?{4,}", n.value)]
        self.assertEqual(bad, [], "NO_QUESTION_LITERALS")

    def payload(self, roundtrip=False, return_phase=False):
        notifier = importlib.import_module("notifier")
        for name in ("notifier", "price_calendar", "analyzer"):
            self.stack.enter_context(patch.object(importlib.import_module(name), "shanghai_today", return_value=date(2026, 10, 2)))
        for name in ("notifier", "price_calendar"):
            self.stack.enter_context(patch.object(importlib.import_module(name), "datetime", ScenarioDatetime))
        self.stack.enter_context(patch.object(notifier, "get_last_push_price", return_value=None))
        self.stack.enter_context(patch.object(notifier, "get_last_push_snapshot", return_value=None))
        self.stack.enter_context(patch.object(notifier, "_quota_overview_text", return_value="synthetic quota"))
        self.stack.enter_context(patch.object(notifier, "_build_source_degradation_context", return_value=copy.deepcopy(DEGRADED)))
        directory = self.root / "return_only" if return_phase else self.root
        items = self.inputs(roundtrip, 6000 if roundtrip else 1600, directory=directory)
        analysis = {"all_flights": items, "recommendations": [{"flight": items[0]}],
                    "economy_recommendations": [{"flight": items[0]}]}
        if roundtrip:
            analysis["round_trip_analysis"] = {"combinations": items}
        route = {"subscription_id": SUB_ID, "origin": "KIX", "destination": "PVG",
                 "origin_city": "大阪", "destination_city": "上海", "route_type": "international",
                 "depart_date": "2026-10-06", "round_trip": roundtrip}
        if roundtrip:
            route["return_date"] = "2026-10-10"
        if return_phase:
            route.update(processing_phase="return_only", original_outbound_date="2026-10-01",
                         phase_label="返程(原去程计划出发日 10-01 已过)")
        return notifier.build_notification_payload(analysis, route_info=route,
                                                   subscription={"id": SUB_ID})

    def test_c9a_email_messages(self):
        notifier = importlib.import_module("notifier")
        for roundtrip in (False, True):
            with self.subTest(roundtrip=roundtrip):
                payload = self.payload(roundtrip)
                wanted = expected_message(roundtrip, price=6000 if roundtrip else 1600)
                self.assertEqual(payload["plan_status_change"]["msg"], wanted, "PAYLOAD_APPROVED_TEXT")
                email = notifier.render_email(payload)[1]
                self.assertIn(wanted, email, "EMAIL_APPROVED_TEXT")
                self.assertIsNone(re.search(r"\?{4,}", email), "EMAIL_NO_QUESTIONS")

    def test_c9b_return_pushplus_message(self):
        from pushplus_sections import prepare_push_render

        notifier = importlib.import_module("notifier")
        payload = self.payload(return_phase=True)
        self.assertEqual(payload["plan_status_change"]["status"], "source_unavailable")
        self.assertEqual(payload["plan_status_change"]["flight_no"], "JL891")
        render = notifier.render_pushplus_sections(payload)
        sections = [section for section in render.sections if section.section_id == "plan_tracking"]
        self.assertEqual(len(sections), 1, "RETURN_TRACKING_SECTION")
        self.assertIn(expected_message(), sections[0].html, "RETURN_PUSHPLUS_APPROVED_TEXT")
        text = prepare_push_render(render).content
        self.assertIn(expected_message(), text, "RETURN_PUSHPLUS_APPROVED_TEXT")
        self.assertIsNone(re.search(r"\?{4,}", text), "RETURN_PUSHPLUS_NO_QUESTIONS")

    def test_c9c_regular_pushplus_matches_baseline(self):
        from pushplus_sections import prepare_push_render

        notifier = importlib.import_module("notifier")
        for roundtrip in (False, True):
            with self.subTest(roundtrip=roundtrip):
                render = notifier.render_pushplus_sections(self.payload(roundtrip))
                self.assertNotIn("plan_tracking", [section.section_id for section in render.sections])
                text = prepare_push_render(render).content
                self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(),
                                 BASELINE_PUSHPLUS_DIGESTS[str(roundtrip)], "REGULAR_PUSHPLUS_BASELINE")
                self.assertIsNone(re.search(r"\?{4,}", text), "REGULAR_PUSHPLUS_NO_QUESTIONS")

    def test_mutations_hit_target_assertions(self):
        for kind, targets in MUTATION_TARGETS.items():
            name, mutant, source = mutated_tracker(self.tracker, kind)
            for method, marker in targets:
                with self.subTest(mutation=kind, method=method):
                    with patch.object(self.tracker, name, mutant), patch(__name__ + ".tracker_source", return_value=source):
                        result = unittest.TestResult()
                        type(self)(method).run(result)
                    self.assertEqual(result.errors, [], "MUTATION_NO_UNRELATED_ERROR")
                    self.assertTrue(result.failures, "MUTATION_SURVIVED")
                    self.assertTrue(all(marker in trace for _, trace in result.failures), "TARGET_ASSERTION_REQUIRED")


if __name__ == "__main__":
    unittest.main()
