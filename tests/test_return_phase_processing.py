"""Offline contracts for the return-only subscription processing phase."""

import ast
import copy
import hashlib
import importlib
import inspect
import io
import json
import logging
import sys
import tempfile
import textwrap
import types
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch


SUB_ID = "00000000-0000-4000-8000-000000000048"
PHASE_LABEL = "返程(原去程计划出发日 10-01 已过)"
NOT_APPLICABLE = "不适用(原预算为往返口径)"
# Captured from 357e7a6 at 2026-09-30 16:00: payload, all carriers and saved plans.
BASELINE_NORMAL_SHA = "e084415d3d19bca88beb9595e52e2974c70a6e36fe557f986cd3ee0298e2f4bf"
EXPECTED_RED_TEST_IDS = (
    "test_return_new_collection",
    "test_return_panel_reuse",
    "test_return_departure_today",
    "test_return_failure_is_reached",
    "test_return_tracking_rounds",
    "test_low_price_policy_cannot_silence_return",
)


class FixedDatetime(datetime):
    today = date(2026, 10, 2)

    @classmethod
    def now(cls, tz=None):
        value = cls(cls.today.year, cls.today.month, cls.today.day, 16, 0, 0)
        return value if tz is None else value.replace(tzinfo=tz)


def subscription():
    return {
        "id": SUB_ID, "origin": "SHA", "destination": "OSA",
        "origin_type": "city", "destination_type": "city",
        "origin_airports": ["PVG"], "origin_airports_active": ["PVG"],
        "destination_airports": ["KIX"], "destination_airports_active": ["KIX"],
        "depart_date": "2026-10-01", "return_date": "2026-10-06",
        "round_trip": True, "date_flexibility": 0, "return_date_flexibility": 0,
        "route_type": "international", "cabin_classes": ["economy"],
        "mode": "balanced", "passenger_count": 1,
        "return_departure_slots": ["afternoon"], "return_arrival_slots": ["afternoon"],
        "hard_constraints": {"round_trip": True, "return_date": "2026-10-06"},
        "soft_preferences": {}, "preferences": {"passengers": {"adult": 1}},
        "notification_goals": {"method": "both", "email": "offline@example.invalid"},
    }


def flight(outbound=False, price=1200):
    day = "2026-10-01" if outbound else "2026-10-06"
    origin, destination = ("PVG", "KIX") if outbound else ("KIX", "PVG")
    number = "MU101" if outbound else "MU102"
    return {
        "flight_combo": number, "airline": "China Eastern", "price": price,
        "currency": "CNY", "source": "juhe", "data_source": "juhe",
        "cabin_class": "economy", "stops": 0, "total_duration_min": 150,
        "departure_airport": origin, "arrival_airport": destination,
        "departure_date": day, "departure_time": "13:00", "arrival_time": "15:30",
        "segments": [{"flight_no": number, "airline": "China Eastern",
                      "dep_airport": origin, "arr_airport": destination,
                      "dep_time": day + " 13:00", "arr_time": day + " 15:30",
                      "duration_min": 150}],
        "layovers": [], "collected_at": "2026-10-02T16:00:00",
    }


def mutated_function(module, name, kind):
    source = textwrap.dedent(inspect.getsource(getattr(module, name)))
    tree = ast.parse(source)
    function = tree.body[0]
    matched = 0

    if kind == "past_outbound":
        for node in ast.walk(function):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "collect_for_airport_matrix":
                node.args[1:4] = [ast.parse(text, mode="eval").body for text in
                                  ('view["destination_airports_active"]', 'view["origin_airports_active"]', 'context["original_outbound_date"]')]
                matched += 1
    elif kind == "roundtrip_write":
        for index, node in enumerate(function.body):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "delivered" for t in node.targets):
                function.body.insert(index, ast.parse('save_roundtrip_snapshot(route, depart_date, depart_date, [])').body[0])
                matched += 1
                break
    elif kind == "only_remove_false":
        for node in ast.walk(function):
            if not isinstance(node, ast.Try):
                continue
            dispatch = [item for item in node.body if isinstance(item, ast.If) and ast.unparse(item.test) == "return_context is not None"]
            empty = [item for item in node.body if isinstance(item, ast.If) and ast.unparse(item.test).startswith("data is None or")]
            if dispatch and empty:
                assert len(dispatch) == len(empty) == 1
                node.body.remove(dispatch[0])
                returns = [item for item in empty[0].body if isinstance(item, ast.Return) and isinstance(item.value, ast.Constant) and item.value.value is False]
                assert len(returns) == 1
                empty[0].body.remove(returns[0])
                matched += 1
    elif kind == "drop_panel":
        for index, node in enumerate(function.body):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "source_errors" for t in node.targets):
                function.body.insert(index, ast.parse('if any(item.get("cache_status") == "panel_reuse" for item in (data or {}).get("collection_freshness", [])):\n    data = None').body[0])
                matched += 1
                break
    elif kind == "empty_budget_only":
        for node in ast.walk(function):
            if isinstance(node, ast.Dict):
                for index, key in enumerate(node.keys):
                    if isinstance(key, ast.Constant) and key.value == "purchase_budget_decision":
                        node.values[index] = ast.parse('{"status": evaluate_purchase_budget(1200, None, None)["status"], "reason": reason}', mode="eval").body
                        matched += 1
    elif kind == "title_without_phase":
        for node in ast.walk(function):
            if isinstance(node, ast.keyword) and node.arg == "title" and isinstance(node.value, ast.BinOp):
                node.value = node.value.left
                matched += 1
    elif kind == "track_roundtrip":
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "tracking_dir" for t in node.targets):
                node.value = ast.Name(id="DEFAULT_DATA_DIR", ctx=ast.Load())
                matched += 1
    elif kind == "write_roundtrip":
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and any(ast.unparse(t) == "tracking_options['data_dir']" for t in node.targets):
                node.value = ast.Name(id="DEFAULT_DATA_DIR", ctx=ast.Load())
                matched += 1
    elif kind == "read_only_namespace":
        for node in ast.walk(function):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "save_pushed_plans":
                keywords = [key for key in node.keywords if key.arg is None and ast.unparse(key.value) == "tracking_options"]
                assert len(keywords) == 1
                node.keywords.remove(keywords[0])
                matched += 1
    else:
        raise AssertionError("unknown mutation")
    assert matched == (2 if kind == "title_without_phase" else 1), (kind, matched)
    ast.fix_missing_locations(tree)
    assert ast.dump(tree, include_attributes=False) != ast.dump(ast.parse(source), include_attributes=False), "MUTATION_NO_CHANGE"
    namespace = dict(vars(module))
    code = compile(tree, inspect.getsourcefile(module) or "<mutant>", "exec")
    exec(code, namespace)
    compiled = namespace[name]
    result = types.FunctionType(compiled.__code__, vars(module), name, compiled.__defaults__, compiled.__closure__)
    result.__kwdefaults__ = compiled.__kwdefaults__
    return result


class ReturnPhaseProcessingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dotenv = types.ModuleType("dotenv")
        dotenv.load_dotenv = lambda *a, **k: False
        dotenv.dotenv_values = lambda *a, **k: {}
        previous_dotenv = sys.modules.get("dotenv")
        sys.modules["dotenv"] = dotenv
        try:
            with patch.object(logging, "basicConfig"):
                cls.main = importlib.import_module("main")
                cls.notifier = importlib.import_module("notifier")
                cls.tracker = importlib.import_module("plan_tracker")
                cls.analyzer = importlib.import_module("analyzer")
        finally:
            if previous_dotenv is None:
                sys.modules.pop("dotenv", None)
            else:
                sys.modules["dotenv"] = previous_dotenv

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.tracking = self.root / "pushed_plans"
        self.clock = type("ScenarioDatetime", (FixedDatetime,), {"today": date(2026, 10, 2)})
        self.stack.enter_context(patch.object(self.tracker, "DEFAULT_DATA_DIR", self.tracking))
        self.stack.enter_context(patch.object(self.tracker, "DEFAULT_FEEDBACK_PATH", self.root / "feedback.json"))
        self.stack.enter_context(patch.object(self.tracker, "datetime", self.clock))
        self.stack.enter_context(patch.object(self.notifier, "datetime", self.clock))
        self.stack.enter_context(patch.object(self.main, "datetime", self.clock))
        self.stack.enter_context(patch.object(self.analyzer, "datetime", self.clock))
        # These consumers hold separate imports; freezing the definition alone misses them.
        for name in ("analyzer", "collection_plan", "subscription_preflight",
                     "sources.juhe_source", "notifier", "price_calendar"):
            module = importlib.import_module(name)
            self.stack.enter_context(patch.object(module, "shanghai_today", side_effect=lambda: self.clock.today))
            if name in ("subscription_preflight", "sources.juhe_source", "price_calendar"):
                self.stack.enter_context(patch.object(module, "datetime", self.clock))

    def run_case(self, *, today=date(2026, 10, 2), panel=False, fail_return=False,
                 sub=None, price=1200, accepted=True):
        self.clock.today = today
        main, notifier = self.main, self.notifier
        sub = copy.deepcopy(sub if sub is not None else subscription())
        original = copy.deepcopy(sub)
        calls, payloads, pages, emails, pushes, events = [], [], [], [], [], []
        responses = []
        stdout = io.StringIO()

        def collect(agg, origins, destinations, day, **kwargs):
            calls.append((list(origins), list(destinations), day, copy.deepcopy(kwargs)))
            if day < today.isoformat() or (fail_return and origins == ["KIX"]):
                return {"flights": [], "source_errors": [{"source": "juhe", "error": "synthetic unavailable"}]}
            response = {
                "flights": [flight(origins == ["PVG"], price)],
                "collected_at": "2026-10-02T16:00:00", "price_insights": {},
                "source_stats": {}, "source_errors": [],
                "collection_freshness": [{"source": "juhe", "cache_status": "panel_reuse" if panel else "fresh"}],
            }
            responses.append((response, copy.deepcopy(response)))
            return response

        def save_page(detail_id, html, payload):
            payloads.append(copy.deepcopy(payload))
            pages.append((detail_id, html))
            events.append("page")
            return True

        def email(*args):
            emails.append(args)
            events.append("email-accepted" if accepted else "email-rejected")
            return accepted

        def push(content, title):
            pushes.append((title, str(content)))
            events.append("push-accepted" if accepted else "push-rejected")
            return accepted

        real_save_plans = notifier.save_pushed_plans

        def save_plans(*args, **kwargs):
            events.append("plans-saved")
            return real_save_plans(*args, **kwargs)

        with ExitStack() as stack, redirect_stdout(stdout):
            patches = {
                "_shanghai_today": Mock(return_value=today),
                "build_default_sources": Mock(return_value=([], [])),
                "FlightAggregator": Mock(return_value=types.SimpleNamespace(last_source_errors=[])),
                "collect_for_airport_matrix": collect,
                "get_constraint_epoch_boundary": Mock(return_value=None),
                "get_constraint_history_limit": Mock(return_value=14),
                "get_previous_snapshot_prices": Mock(return_value={}),
                "get_lowest_price_history": Mock(return_value=[]),
                "save_raw_response": Mock(), "save_flight_details": Mock(),
                "save_roundtrip_snapshot": Mock(), "get_roundtrip_price_history": Mock(return_value=[]),
                "collect_nearby_dates": Mock(return_value=[]),
                "_notification_tcurve": Mock(return_value={}),
                "_notification_forecast": Mock(return_value={}),
                "_notification_provenance_context": Mock(return_value={}),
                "feedback_acknowledgement": Mock(return_value=None),
                "delivery_payload_with_detail_token": copy.deepcopy,
                "_save_result_for_page": save_page,
                "send_email": email, "send": push,
                "render_email": notifier.render_email,
            }
            for key, value in patches.items():
                stack.enter_context(patch.object(main, key, value))
            for key in ("get_last_push_price", "get_last_push_snapshot"):
                stack.enter_context(patch.object(notifier, key, return_value=None))
            save_price = stack.enter_context(patch.object(notifier, "save_last_push_price"))
            save_snapshot = stack.enter_context(patch.object(notifier, "save_push_snapshot"))
            plan_writer = stack.enter_context(patch.object(notifier, "save_pushed_plans", side_effect=save_plans))
            plan_reader = stack.enter_context(patch.object(notifier, "track_plan_status", wraps=notifier.track_plan_status))
            plan_loader = stack.enter_context(patch.object(self.tracker, "load_pushed_plans", wraps=self.tracker.load_pushed_plans))
            analyzed = stack.enter_context(patch.object(main, "analyze_all_flights", wraps=main.analyze_all_flights))
            skipped = stack.enter_context(patch.object(main, "_should_skip_low_price_alert", wraps=main._should_skip_low_price_alert))
            ok = main._process_subscription_locked(sub, ensure_db=False, manage_collection_round=False,
                                                   collection_round_id="synthetic-return-phase")
        self.assertEqual(sub, original, "subscription_input_mutated")
        return {
            "ok": ok, "calls": calls, "payloads": payloads, "pages": pages, "emails": emails,
            "pushes": pushes, "events": events, "stdout": stdout.getvalue(), "responses": responses,
            "writes": patches["save_flight_details"], "roundtrip_writes": patches["save_roundtrip_snapshot"],
            "roundtrip_reads": patches["get_roundtrip_price_history"], "analyzed": analyzed,
            "plan_writer": plan_writer, "plan_reader": plan_reader, "skip": skipped,
            "plan_loader": plan_loader,
            "save_price": save_price, "save_snapshot": save_snapshot,
        }

    def assert_return(self, result):
        self.assertTrue(result["ok"], "return_processing_completed\n" + result["stdout"])
        self.assertEqual(len(result["calls"]), 1, "one_return_consumer")
        self.assertEqual(result["calls"][0][:3], (["KIX"], ["PVG"], "2026-10-06"), "no_past_outbound_consumer")
        self.assertEqual(result["analyzed"].call_count, 1)
        self.assertEqual(result["analyzed"].call_args.args[0][0]["flight_combo"], "MU102", "return_result_consumed")
        result["roundtrip_writes"].assert_not_called()
        result["roundtrip_reads"].assert_not_called()
        payload = result["payloads"][0]
        self.assertEqual(payload["subscription_id"], SUB_ID)
        self.assertEqual(result["pages"][0][0], SUB_ID)
        self.assertIn("大阪", payload["route"])
        self.assertIn("上海", payload["route"])
        self.assertEqual(payload["processing_phase"], "return_only")
        self.assertEqual(payload["purchase_budget_decision"]["status"], "not_applicable", "explicit_budget_na")
        self.assertEqual(payload["purchase_budget_decision"]["reason"], NOT_APPLICABLE)
        self.assertEqual(payload["cross_leg_constraints"]["status"], "not_applicable")
        self.assertIsNone(payload.get("verify_price"))
        self.assertFalse(payload["is_roundtrip"])
        for text in (result["emails"][0][1], result["emails"][0][2], result["pages"][0][1], *result["pushes"][0]):
            self.assertIn(PHASE_LABEL, text, "phase_visible_in_delivery")
            for forbidden in ("可以购买前验证", "满足购买条件", "低于目标"):
                self.assertNotIn(forbidden, text, "no_purchase_judgment")
        payload_text = json.dumps(payload, ensure_ascii=False)
        for forbidden in ("可以购买前验证", "满足购买条件", "低于目标"):
            self.assertNotIn(forbidden, payload_text)
        for plan in payload["recommended_plans"]:
            self.assertEqual(plan["direction"], "return")
            self.assertNotIn("去程", plan["summary"])
            self.assertNotIn("去程", plan["main_push_line"])
            self.assertEqual(plan["price"], 1200)
        self.assertEqual(payload["snapshot"]["route"], "OSA-SHA")
        self.assertEqual(payload["snapshot"]["depart_date"], "2026-10-06")
        self.assertIsNone(payload["snapshot"].get("return_date"))
        fp = self.main.constraint_fingerprint(subscription())
        for call in result["writes"].call_args_list:
            self.assertEqual(call.args[:2], ("OSA-SHA", "2026-10-06"))
            self.assertEqual(call.kwargs["constraint_fingerprint"], fp)
        self.assertLess(result["events"].index("push-accepted"), result["events"].index("plans-saved"))
        for actual, before in result["responses"]:
            self.assertEqual(actual, before, "source_response_not_mutated")

    def test_normal_roundtrip_baseline(self):
        result = self.run_case(today=date(2026, 9, 30))
        self.assertTrue(result["ok"], result["stdout"])
        self.assertEqual(result["roundtrip_writes"].call_count, 1)
        self.assertEqual(result["plan_writer"].call_args.kwargs, {})
        self.assertEqual(result["events"], ["page", "email-accepted", "push-accepted", "plans-saved"])
        record = {key: result[key] for key in ("payloads", "pages", "emails", "pushes", "events")}
        record["saved_plans"] = self.tracker._storage_path(SUB_ID).read_text(encoding="utf-8")
        body = json.dumps(record,
                          ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        self.assertEqual(hashlib.sha256(body).hexdigest(), BASELINE_NORMAL_SHA)

    def test_return_new_collection(self):
        self.assert_return(self.run_case())

    def test_return_panel_reuse(self):
        result = self.run_case(panel=True)
        self.assert_return(result)
        self.assertEqual(result["payloads"][0]["data_freshness"]["legs"][0]["cache_status"], "panel_reuse")

    def test_return_departure_today(self):
        self.assert_return(self.run_case(today=date(2026, 10, 6)))

    def test_all_expired_skips(self):
        result = self.run_case(today=date(2026, 10, 7))
        self.assertTrue(result["ok"])
        self.assertEqual(result["calls"], [])
        self.assertEqual(result["payloads"], [])
        result["roundtrip_writes"].assert_not_called()

    def test_return_failure_is_reached(self):
        result = self.run_case(fail_return=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["calls"][0][:3], (["KIX"], ["PVG"], "2026-10-06"), "return_failure_reached")
        self.assertIn("返程采集未返回有效航班", result["stdout"])
        self.assertEqual(result["pushes"], [])
        result["roundtrip_writes"].assert_not_called()

    def test_low_price_policy_cannot_silence_return(self):
        sub = subscription()
        sub["hard_constraints"].update(budget_strategy="low_price_alert", target_price=10, max_budget=20)
        sub["constraints"] = {"target_price": 10, "budget": 20, "max_price": 20}
        sub["preferences"].update(target_price_mode="auto", budget=20)
        result = self.run_case(sub=sub)
        self.assertTrue(result["ok"], result["stdout"])
        result["skip"].assert_not_called()
        self.assertEqual(result["payloads"][0]["purchase_budget_decision"]["status"], "not_applicable")
        self.assertEqual(len(result["pushes"]), 1)

    def test_return_tracking_rounds(self):
        self.tracker.save_pushed_plans(SUB_ID, [{"is_roundtrip": True, "label": "方案A", "price": 9999,
                                               "outbound_flight": flight(True), "return_flight": flight()}])
        old = self.tracker._storage_path(SUB_ID)
        before = old.read_bytes()
        first = self.run_case()
        self.assertTrue(first["ok"], first["stdout"])
        self.assertEqual(old.read_bytes(), before, "old_roundtrip_record_unchanged")
        new = self.tracker._storage_path(SUB_ID, self.tracking / "return_only")
        self.assertTrue(new.is_file(), "return_record_persisted")
        self.assertEqual(json.loads(new.read_text(encoding="utf-8"))["subscription_id"], SUB_ID)
        self.assertEqual(first["plan_writer"].call_args.kwargs["data_dir"], self.tracking / "return_only")
        for call in first["plan_loader"].call_args_list:
            location = call.kwargs.get("data_dir") if len(call.args) < 2 else call.args[1]
            self.assertEqual(location, self.tracking / "return_only", "old_roundtrip_not_read")
        self.assertIn("口径切换(往返→返程)", json.dumps(first["payloads"][0], ensure_ascii=False))
        second = self.run_case(price=1300)
        self.assertTrue(second["ok"], second["stdout"])
        self.assertEqual(second["payloads"][0]["plan_status_change"]["previous_price"], 1200, "compared_return_record")
        self.assertNotIn("口径切换", json.dumps(second["payloads"][0], ensure_ascii=False))
        for text in (first["emails"][0][2], first["pages"][0][1], first["pushes"][0][1]):
            self.assertIn("口径切换(往返→返程)", text)
        for text in (second["emails"][0][2], second["pages"][0][1], second["pushes"][0][1]):
            self.assertNotIn("口径切换", text)
        self.assertEqual(old.read_bytes(), before)

    def test_return_tracking_requires_channel_acceptance(self):
        result = self.run_case(accepted=False)
        self.assertFalse(result["ok"])
        result["plan_writer"].assert_not_called()
        result["save_price"].assert_not_called()
        result["save_snapshot"].assert_not_called()
        self.assertFalse(self.tracking.exists())

    def test_return_cabin_quote_is_not_a_roundtrip_or_party_total(self):
        sub = subscription()
        sub["passenger_count"] = 3
        sub["preferences"]["passengers"] = {"adult": 2, "child": 1}
        sub["constraints"] = {"cabin_allocation": {"business": 1, "economy": 2}}
        result = self.run_case(sub=sub)
        self.assertTrue(result["ok"], result["stdout"])
        payload = result["payloads"][0]
        self.assertEqual(payload["current_price"], 1200)
        self.assertEqual(payload["snapshot"]["subscription_id"], SUB_ID)
        for plan in payload["recommended_plans"]:
            self.assertEqual(plan["price"], 1200)
            self.assertNotIn("passenger_pricing", plan)
            self.assertFalse(plan["is_roundtrip"])
        for text in (result["emails"][0][2], result["pages"][0][1], result["pushes"][0][1]):
            for label in ("单人往返", "全员往返", "往返总价", "往返票价", "往返¥", "去程票价"):
                self.assertNotIn(label, text)
            self.assertIn("返程舱位单程", text)

    def test_privacy_carriers_keep_phase(self):
        for level in ("minimal", "redacted"):
            with self.subTest(level=level):
                sub = subscription()
                sub["notification_goals"]["privacy_level"] = level
                result = self.run_case(sub=sub)
                self.assert_return(result)

    def test_city_aliases_follow_return_direction(self):
        sub = subscription()
        sub.update(origin_city="上海", destination_city="大阪")
        result = self.run_case(sub=sub)
        self.assert_return(result)
        route = result["payloads"][0]["route"]
        self.assertLess(route.index("大阪"), route.index("上海"))

    def test_return_consumer_key_matches_planned_primary_requests(self):
        from collection_plan import build_collection_plan
        from request_cache import cache_key, passenger_signature

        sub = subscription()
        passengers = {"adult": 2, "child": 1, "elderly": 0, "infant": 0}
        sub["passengers"] = passengers
        sub["preferences"]["passengers"] = passengers
        result = self.run_case(sub=sub)
        self.assertTrue(result["ok"], result["stdout"])
        origins, destinations, day, options = result["calls"][0]
        self.assertEqual(options["passengers"], passengers)
        for uses_count in (False, True):
            with self.subTest(uses_passenger_count=uses_count):
                source = types.SimpleNamespace(name="synthetic", uses_passenger_count=uses_count)
                plan = build_collection_plan(
                    subscriptions=[sub], include_calendars=False,
                    source_builder=lambda *a, **k: ([source], []),
                )
                planned = [request for request in plan._requests.values()
                           if request.origin == origins[0] and request.dest == destinations[0]
                           and request.date_str == day and request.conditional is None]
                self.assertEqual(len(planned), 1)
                actual = cache_key(source, origins[0], destinations[0], day,
                                   options["passengers"], options["cabin_classes"][0])
                self.assertEqual(actual, planned[0].key)
                self.assertIn(actual, plan.request_keys, "no_unplanned_primary_request")
                self.assertEqual(actual[4], passenger_signature(passengers if uses_count else {"adult": 1}))

    def test_incomplete_private_carriers_keep_phase_without_exact_data(self):
        from pushplus_sections import prepare_push_render

        payload = self.run_case()["payloads"][0]
        payload["source_degradation"] = {"data_incomplete": True, "reason": "synthetic failure"}
        payload["detail_url"] = "https://example.invalid/detail?token=synthetic-private-canary"
        for level in ("minimal", "redacted"):
            with self.subTest(level=level):
                payload["notification_privacy_level"] = level
                subject, body = self.notifier.render_email(payload)
                render = self.notifier.render_pushplus_sections(payload)
                push = prepare_push_render(render).content
                for text in (subject, body, render.title, push):
                    self.assertIn(PHASE_LABEL, text)
                    self.assertNotIn("MU102", text)
                    self.assertNotIn("1200", text)
                    self.assertNotIn("synthetic-private-canary", text)
                for text in (body, push):
                    self.assertIn("本轮结论不可用", text)

    def test_nested_budget_and_cross_leg_constraints_not_applicable(self):
        sub = subscription()
        for key in ("constraints", "hard_constraints", "soft_preferences", "preferences", "basic", "advanced_rules"):
            sub.setdefault(key, {}).update(budget=1, max_price=2, max_budget=3, target_price=1,
                                            ideal_price=1, target_price_mode="auto", budget_strategy="low_price_alert")
        result = self.run_case(sub=sub)
        self.assertTrue(result["ok"], result["stdout"])
        payload = result["payloads"][0]
        self.assertEqual(payload["purchase_budget_decision"], {"status": "not_applicable", "reason": NOT_APPLICABLE})
        self.assertEqual(payload["cross_leg_constraints"]["status"], "not_applicable")
        self.assertIsNone(payload.get("verify_price"))
        for text in (json.dumps(payload, ensure_ascii=False), result["emails"][0][2], result["pages"][0][1], result["pushes"][0][1]):
            for forbidden in ("可以购买前验证", "满足购买条件", "低于目标"):
                self.assertNotIn(forbidden, text)
        result["skip"].assert_not_called()

    def test_fixed_scope_and_return_direction_preferences(self):
        sub = subscription()
        today = date(2026, 10, 2)
        before = copy.deepcopy(sub)
        context = self.main._return_phase_context(sub, today)
        self.assertIsNotNone(context)
        self.assertEqual(sub, before)
        for overrides in ({"date_flexibility": 1}, {"round_trip": False}, {"same_day_round_trip": True}):
            with self.subTest(overrides=overrides):
                self.assertIsNone(self.main._return_phase_context({**sub, **overrides}, today))
        sub["outbound_departure_slots"] = ["morning"]
        sub["soft_preferences"]["return_departure_time_windows"] = ["13:00-16:00"]
        sub["hard_constraints"]["return_departure_time_windows"] = ["14:00-16:00"]
        mapped = self.main._return_phase_preferences(sub, context)
        self.assertEqual(mapped["preferences"]["direction"], "return")
        self.assertEqual(mapped["preferences"]["departure_slots"], sub["return_departure_slots"])
        self.assertEqual(mapped["preferences"]["return_departure_time_windows"], ["13:00-16:00"])
        self.assertEqual(mapped["constraints"]["return_departure_time_windows"], ["14:00-16:00"])
        self.assertNotEqual(self.main.constraint_fingerprint(mapped["view"]), self.main.constraint_fingerprint(sub))

    def test_return_departure_feasibility_uses_return_set_off(self):
        sub = subscription()
        sub["hard_constraints"].update(outbound_set_off="23:50", return_set_off="08:00")
        with patch.object(self.notifier, "analyze_departure_feasibility", wraps=self.notifier.analyze_departure_feasibility) as probe:
            result = self.run_case(sub=sub)
        self.assertTrue(result["ok"], result["stdout"])
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(probe.call_args.args[0], "08:00")
        self.assertEqual(probe.call_args.args[-1], "2026-10-06")
        plan = result["payloads"][0]["recommended_plans"][0]
        self.assertIn("return", plan["feasibility"])
        self.assertNotIn("outbound", plan["feasibility"])

    def seed_roundtrip(self):
        self.tracker.save_pushed_plans(SUB_ID, [{"is_roundtrip": True, "label": "方案A", "price": 9999,
                                               "outbound_flight": flight(True), "return_flight": flight()}])
        path = self.tracker._storage_path(SUB_ID)
        return path, path.read_bytes()

    def test_m1_past_outbound_rejected_by_consumer_arguments(self):
        mutant = mutated_function(self.main, "_process_return_phase", "past_outbound")
        with patch.object(self.main, "_process_return_phase", mutant):
            result = self.run_case()
        with self.assertRaisesRegex(AssertionError, "no_past_outbound_consumer"):
            self.assertEqual(result["calls"][0][:3], (["KIX"], ["PVG"], "2026-10-06"), "no_past_outbound_consumer")

    def test_m2_roundtrip_write_rejected(self):
        mutant = mutated_function(self.main, "_process_return_phase", "roundtrip_write")
        with patch.object(self.main, "_process_return_phase", mutant):
            result = self.run_case()
        self.assertTrue(result["ok"], result["stdout"])
        with self.assertRaisesRegex(AssertionError, "roundtrip_history_zero_writes"):
            self.assertEqual(result["roundtrip_writes"].call_count, 0, "roundtrip_history_zero_writes")

    def test_m3_removing_only_false_does_not_complete_return(self):
        mutant = mutated_function(self.main, "_process_subscription_locked", "only_remove_false")
        with patch.object(self.main, "_process_subscription_locked", mutant):
            result = self.run_case()
        self.assertNotIn("[处理失败]", result["stdout"])
        with self.assertRaisesRegex(AssertionError, "return_phase_completed"):
            self.assertEqual([payload.get("processing_phase") for payload in result["payloads"]], ["return_only"], "return_phase_completed")

    def test_m4_panel_results_must_be_consumed(self):
        mutant = mutated_function(self.main, "_process_return_phase", "drop_panel")
        with patch.object(self.main, "_process_return_phase", mutant):
            result = self.run_case(panel=True)
        with self.assertRaisesRegex(AssertionError, "return_processing_completed"):
            self.assertTrue(result["ok"], "return_processing_completed")

    def test_m5_empty_budget_is_not_the_explicit_na_branch(self):
        mutant = mutated_function(self.notifier, "_return_phase_price_context", "empty_budget_only")
        with patch.object(self.notifier, "_return_phase_price_context", mutant):
            result = self.run_case()
        self.assertTrue(result["ok"], result["stdout"])
        with self.assertRaisesRegex(AssertionError, "explicit_budget_na"):
            self.assertEqual(result["payloads"][0]["purchase_budget_decision"]["status"], "not_applicable", "explicit_budget_na")

    def test_m6_actual_push_title_requires_phase(self):
        mutant = mutated_function(self.main, "_deliver_notification", "title_without_phase")
        with patch.object(self.main, "_deliver_notification", mutant):
            result = self.run_case()
        self.assertTrue(result["ok"], result["stdout"])
        with self.assertRaisesRegex(AssertionError, "phase_visible_in_delivery"):
            self.assertIn(PHASE_LABEL, result["pushes"][0][0], "phase_visible_in_delivery")

    def test_m7_roundtrip_comparison_rejected(self):
        self.seed_roundtrip()
        mutant = mutated_function(self.notifier, "build_notification_payload", "track_roundtrip")
        with patch.object(self.main, "build_notification_payload", mutant):
            result = self.run_case()
        self.assertTrue(result["ok"], result["stdout"])
        with self.assertRaisesRegex(AssertionError, "first_return_scope_change"):
            self.assertEqual((result["payloads"][0].get("plan_status_change") or {}).get("status"), "scope_changed", "first_return_scope_change")

    def test_m8_roundtrip_record_overwrite_rejected(self):
        old, before = self.seed_roundtrip()
        mutant = mutated_function(self.notifier, "persist_notification_payload", "write_roundtrip")
        with patch.object(self.main, "persist_notification_payload", mutant):
            result = self.run_case()
        self.assertTrue(result["ok"], result["stdout"])
        with self.assertRaisesRegex(AssertionError, "old_roundtrip_record_unchanged"):
            self.assertEqual(old.read_bytes(), before, "old_roundtrip_record_unchanged")

    def test_m9_read_only_namespace_does_not_supply_second_round(self):
        self.seed_roundtrip()
        mutant = mutated_function(self.notifier, "persist_notification_payload", "read_only_namespace")
        with patch.object(self.main, "persist_notification_payload", mutant):
            first = self.run_case()
            second = self.run_case(price=1300)
        self.assertTrue(first["ok"], first["stdout"])
        self.assertTrue(second["ok"], second["stdout"])
        with self.assertRaisesRegex(AssertionError, "compared_return_record"):
            self.assertEqual((second["payloads"][0].get("plan_status_change") or {}).get("previous_price"), 1200, "compared_return_record")


if __name__ == "__main__":
    unittest.main()
