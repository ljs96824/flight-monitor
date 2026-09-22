"""Synthetic subscription inputs through the real research preparation chain."""
import ast
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import date, datetime
import inspect
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import FunctionType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


TODAY = date(2026, 8, 26)
DOWNSTREAM = (
    "active_user_monitor_dates", "prepare_research_requests", "_simulate_runtime_quota",
    "apply_research_quota_guard", "inspect_research_migrations", "load_backup_evidence",
)


class ResearchSubscriptionInputTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "NO_LIVE_API": "1", "PYTHON_DOTENV_DISABLED": "1",
        }, clear=True))
        self.stack.enter_context(patch.dict(sys.modules, {"dotenv": SimpleNamespace(
            load_dotenv=lambda *a, **k: False, dotenv_values=lambda *a, **k: {})}))
        self.network = Mock(side_effect=AssertionError("EXTERNAL_NETWORK_FORBIDDEN"))
        self.stack.enter_context(patch.object(socket.socket, "connect", self.network))
        self.stack.enter_context(patch.object(socket.socket, "connect_ex", self.network))
        import basket_collect
        import log_utils
        self.basket = basket_collect
        self.logs = log_utils
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))

    def tearDown(self):
        self.network.assert_not_called()

    def _input(self, kind):
        path = self.root / "subscriptions.json"
        if kind == "directory":
            path.mkdir()
        elif kind != "missing":
            raw = {"empty": b"[]", "null": b"null", "object": b"{}",
                   "invalid_json": b'{"broken":', "encoding": b"\xff"}.get(kind)
            if raw is None:
                raw = json.dumps([
                    {"status": "Enabled", "origin": "PVG", "destination": "KIX", "depart_date": "2026-10-01"},
                    {"status": "active", "origin": "PVG", "destination": "KIX", "depart_date": "2026-08-01"},
                    {"status": "paused", "origin": "PVG", "destination": "KIX", "depart_date": "2026-10-01"},
                    None, 7,
                ]).encode("utf-8")
            path.write_bytes(raw)
        return path

    def loader_observation(self, kind, reader_error=None):
        path = self._input(kind)
        before = path.read_bytes() if path.is_file() else None
        value, error = None, None
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(io.StringIO()))
            if reader_error is not None:
                stack.enter_context(patch.object(self.basket, "read_json", side_effect=reader_error))
            try:
                value = self.basket._load_active_subscriptions_for_research(path, today=TODAY)
            except Exception as exc:
                error = exc
        after = path.read_bytes() if path.is_file() else None
        self.assertEqual(after, before, "INPUT_BYTES_UNCHANGED")
        if kind == "missing":
            self.assertFalse(path.exists(), "MISSING_NOT_CREATED")
        return value, error

    def assert_unavailable(self, kind, cause=None, reader_error=None):
        value, error = self.loader_observation(kind, reader_error)
        expected = getattr(self.basket, "ResearchSubscriptionInputUnavailable", None)
        self.assertIsNotNone(expected, "DEDICATED_EXCEPTION_EXISTS")
        self.assertIsInstance(error, expected, "DEDICATED_EXCEPTION_TYPE")
        self.assertIsNone(value)
        self.assertIn(str(self.root / "subscriptions.json"), str(error), "ERROR_PATH")
        if cause is None:
            self.assertIsNone(error.__cause__, "NO_FABRICATED_CAUSE")
        else:
            self.assertIsInstance(error.__cause__, cause, "UNDERLYING_CAUSE")
        return error

    def test_missing_file_is_unavailable(self):
        self.assert_unavailable("missing")

    def test_directory_is_unavailable(self):
        self.assert_unavailable("directory")

    def test_empty_array_is_valid(self):
        value, error = self.loader_observation("empty")
        self.assertIsNone(error)
        self.assertEqual(value, [])

    def test_normal_array_preserves_filtering(self):
        value, error = self.loader_observation("normal")
        self.assertIsNone(error)
        self.assertEqual(value, [{"status": "Enabled", "origin": "PVG", "destination": "KIX", "depart_date": "2026-10-01"}])

    def test_invalid_json_preserves_parse_cause(self):
        self.assert_unavailable("invalid_json", json.JSONDecodeError)

    def test_invalid_encoding_preserves_unicode_cause(self):
        self.assert_unavailable("encoding", UnicodeError)

    def test_null_is_unavailable_without_fake_parse_cause(self):
        error = self.assert_unavailable("null")
        self.assertNotIsInstance(error.__cause__, json.JSONDecodeError)

    def test_object_is_unavailable(self):
        self.assert_unavailable("object")

    def test_wrapped_os_error_preserves_underlying_cause(self):
        original = PermissionError("synthetic unreadable file")
        wrapped = self.basket.JsonStoreReadError("synthetic wrapped read error")
        wrapped.__cause__ = original
        error = self.assert_unavailable("empty", OSError, wrapped)
        self.assertIs(error.__cause__, original)

    def test_wrapper_without_cause_is_retained(self):
        wrapped = self.basket.JsonStoreReadError("synthetic wrapper without cause")
        error = self.assert_unavailable("empty", self.basket.JsonStoreReadError, wrapped)
        self.assertIs(error.__cause__, wrapped)

    def _downstream(self, stack):
        values = (set(), SimpleNamespace(requests=[], events=[]), {"complete": True},
                  {"triggered": False}, {}, {})
        spies = {}
        for name, value in zip(DOWNSTREAM, values):
            spies[name] = stack.enter_context(patch.object(self.basket, name, return_value=value))
        stack.enter_context(patch.object(self.basket, "evaluate_research_hard_gates",
                                         return_value={"ready": True, "checks": {}, "missing": []}))
        return spies

    def preparation_observation(self, kind):
        self._input(kind)
        state = self.basket.build_initial_state(TODAY)
        before = deepcopy(state)
        output = io.StringIO()
        with ExitStack() as stack:
            spies = self._downstream(stack)
            stack.enter_context(redirect_stdout(output))
            result = self.basket._prepare_research_basket(
                state, today=TODAY, state_path=self.root / "basket_state.json",
                db_path=self.root / "observations.sqlite3", usage_path=self.root / "api_usage.json",
                settings={}, source_builder=Mock(side_effect=AssertionError("SOURCE_FORBIDDEN")),
            )
            counts = {name: spy.call_count for name, spy in spies.items()}
        return dict(result=result, input_state=state, before=before, counts=counts, output=output.getvalue())

    def assert_preparation_missing(self):
        row = self.preparation_observation("missing")
        state, requests, gate, quota = row["result"]
        with self.subTest(check="gate"):
            self.assertIs(gate["ready"], False, "INPUT_GATE")
        with self.subTest(check="missing"):
            self.assertEqual(gate["missing"][:1], ["subscription_input"], "INPUT_MISSING_FIRST")
        with self.subTest(check="counts"):
            self.assertEqual(row["counts"], dict.fromkeys(DOWNSTREAM, 0), "NO_DOWNSTREAM_CALLS")
        self.assertEqual(gate["checks"].get("subscription_input"), False)
        self.assertIs(quota["complete"], False)
        self.assertIs(quota["guard_triggered"], False)
        self.assertIs(state, row["input_state"])
        self.assertEqual(state, row["before"])
        self.assertEqual(requests, [])
        self.assertEqual(sum("[研究输入缺失]" in line for line in row["output"].splitlines()), 1)

    def test_missing_input_short_circuits_preparation(self):
        self.assert_preparation_missing()

    def test_empty_input_runs_all_preparation_steps(self):
        row = self.preparation_observation("empty")
        self.assertNotIn("subscription_input", row["result"][2]["missing"])
        self.assertTrue(row["result"][2]["ready"])
        self.assertEqual(row["counts"], dict.fromkeys(DOWNSTREAM, 1), "ALL_SIX_STEPS")

    def lifecycle_observation(self, kind):
        self._input(kind)
        path = self.root / "basket_state.json"
        initial = self.basket.build_initial_state(TODAY)
        path.write_text(json.dumps(initial), encoding="utf-8")
        before = path.read_bytes()
        console, errors = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(console))
            stack.enter_context(redirect_stderr(errors))
            entry_streams = (sys.stdout, sys.stderr)
            stack.enter_context(patch.object(self.logs, "_round_log_state", None))
            self._downstream(stack)
            execute = Mock(return_value=SimpleNamespace(ledger_degraded=False, actual_requests=0, outcomes=()))
            plan = SimpleNamespace(request_keys=(), panel_only_keys=(), freshness_hours=6,
                                   fresh_scope="primary_only", log_summary=Mock(), execute=execute)
            replacements = {
                "reset_request_cache": Mock(), "start_request_cache_round": Mock(),
                "set_current_round": Mock(), "reset_current_round": Mock(),
                "build_collection_plan": Mock(return_value=plan), "activate_collection_plan": Mock(),
                "deactivate_collection_plan": Mock(), "load_usage_strict": Mock(return_value={}),
                "usage_snapshot": Mock(return_value={}), "apply_research_round_outcomes": Mock(return_value=[]),
                "_build_route_aggregator": Mock(), "count_observations_for_round": Mock(return_value=0),
                "print_request_cache_stats": Mock(), "log_retention_dry_run": Mock(),
            }
            for name, replacement in replacements.items():
                stack.enter_context(patch.object(self.basket, name, replacement))
            source = Mock(side_effect=AssertionError("SOURCE_OR_NOTIFICATION_FORBIDDEN"))
            result, error = None, None
            try:
                try:
                    result = self.basket._run_basket_locked(
                        today=TODAY, now=datetime(2026, 8, 26, 12), state_path=path,
                        db_path=self.root / "observations.sqlite3", usage_path=self.root / "api_usage.json",
                        source_builder=source, aggregator_factory=source, quota_guard_notifier=source,
                        settings={"research_basket_strategy": "cohort_v2", "source_quota_budget": {"juhe": 1000},
                                  "source_quota_low_remaining_threshold": 1},
                    )
                except Exception as exc:
                    error = exc
                # Observe production cleanup before the test's emergency cleanup.
                row = dict(result=result, error=error, output=console.getvalue(),
                    stdout_restored=sys.stdout is entry_streams[0], stderr_restored=sys.stderr is entry_streams[1],
                    state_cleared=self.logs._round_log_state is None, before=before, after=path.read_bytes(),
                    archive=(self.root / "logs" / "rounds" / "20260826.log").read_text(encoding="utf-8"),
                    execute_calls=execute.call_count)
            finally:
                if self.logs._round_log_state is not None:
                    self.logs.end_round_log_archive(status="test_cleanup")
            source.assert_not_called()
        return row

    def assert_lifecycle(self, kind):
        row = self.lifecycle_observation(kind)
        checks = {
            "no_escape": row["error"] is None,
            "blocked": (row["result"] or {}).get("status") == "blocked",
            "input_log": "[研究输入缺失]" in row["output"],
            "end_marker": "status=blocked =====" in row["archive"],
            "stdout_restored": row["stdout_restored"], "stderr_restored": row["stderr_restored"],
            "state_cleared": row["state_cleared"], "state_bytes": row["before"] == row["after"],
            "no_execute": row["execute_calls"] == 0,
        }
        for name, passed in checks.items():
            with self.subTest(check=name):
                self.assertTrue(passed, name)

    def test_missing_input_blocks_and_closes_archive(self):
        self.assert_lifecycle("missing")

    def test_null_input_blocks_and_closes_archive(self):
        self.assert_lifecycle("null")

    def _mutate(self, kind):
        basket = self.basket
        name = "_prepare_research_basket" if kind in {"swallow", "reraise"} else "_load_active_subscriptions_for_research"
        source = inspect.getsource(getattr(basket, name))
        tree = ast.parse(source)
        original = ast.dump(tree)
        fn = tree.body[0]
        if kind == "missing_empty":
            targets = [n for n in fn.body if isinstance(n, ast.If) and ast.unparse(n.test) == "not target.is_file()"]
            self.assertEqual(len(targets), 1, "MUTATION_MATCH")
            self.assertIsInstance(targets[0].body[0], ast.Raise)
            targets[0].body = [ast.Return(value=ast.List(elts=[], ctx=ast.Load()))]
        elif kind in {"swallow", "reraise"}:
            handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)
                        and ast.unparse(n.type) == "ResearchSubscriptionInputUnavailable"]
            self.assertEqual(len(handlers), 1, "MUTATION_MATCH")
            handlers[0].body = ast.parse("subscriptions = []" if kind == "swallow" else "raise").body
        elif kind == "reject_empty":
            targets = [n for n in fn.body if isinstance(n, ast.If)
                       and ast.unparse(n.test) == "not isinstance(payload, list)"]
            self.assertEqual(len(targets), 1, "MUTATION_MATCH")
            targets[0].test = ast.parse("not isinstance(payload, list) or not payload", mode="eval").body
        elif kind == "wrong_catch":
            handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)
                        and ast.unparse(n.type) == "JsonStoreReadError"]
            self.assertEqual(len(handlers), 1, "MUTATION_MATCH")
            handlers[0].type = ast.parse("(OSError, UnicodeError, __import__('json').JSONDecodeError)", mode="eval").body
        else:
            self.fail("UNKNOWN_MUTATION")
        ast.fix_missing_locations(tree)
        self.assertNotEqual(ast.dump(tree), original, "MUTATION_AST_CHANGED")
        changed = ast.unparse(tree)
        self.assertNotEqual(changed, ast.unparse(ast.parse(source)), "MUTATION_SOURCE_CHANGED")
        namespace = dict(basket.__dict__)
        exec(compile(tree, "<research-input-mutation>", "exec"), namespace)
        compiled = namespace[name]
        function = FunctionType(compiled.__code__, basket.__dict__, name, compiled.__defaults__)
        function.__kwdefaults__ = compiled.__kwdefaults__
        return name, function

    def _assert_mutation(self, kind, targets):
        name, function = self._mutate(kind)
        with patch.object(self.basket, name, function):
            for method, expected_markers in targets.items():
                with self.subTest(method=method):
                    case = type(self)(method)
                    result = unittest.TestResult()
                    case.run(result)
                    self.assertEqual(result.errors, [], "MUTATION_NO_UNRELATED_ERRORS")
                    messages = "\n".join(message for _, message in result.failures)
                    self.assertTrue(result.failures, "MUTATION_BEHAVIOR_REJECTED")
                    for marker in expected_markers:
                        self.assertIn(marker, messages)

    def test_mutation_missing_as_empty_is_rejected(self):
        self._assert_mutation("missing_empty", {
            "test_missing_file_is_unavailable": ["DEDICATED_EXCEPTION_TYPE"],
            "test_directory_is_unavailable": ["DEDICATED_EXCEPTION_TYPE"],
            "test_missing_input_short_circuits_preparation": ["NO_DOWNSTREAM_CALLS", "INPUT_GATE", "INPUT_MISSING_FIRST"],
            "test_missing_input_blocks_and_closes_archive": ["input_log"],
        })

    def test_mutation_swallowed_input_is_rejected(self):
        self._assert_mutation("swallow", {"test_missing_input_short_circuits_preparation": [
            "NO_DOWNSTREAM_CALLS", "INPUT_GATE", "INPUT_MISSING_FIRST"]})

    def test_mutation_reraise_is_rejected(self):
        self._assert_mutation("reraise", {"test_null_input_blocks_and_closes_archive": [
            "no_escape", "end_marker", "stdout_restored", "stderr_restored", "state_cleared"]})

    def test_mutation_empty_rejected_is_rejected(self):
        self._assert_mutation("reject_empty", {
            "test_empty_array_is_valid": ["is not None"],
            "test_empty_input_runs_all_preparation_steps": ["unexpectedly found"],
        })

    def test_mutation_wrong_exception_catch_is_rejected(self):
        self._assert_mutation("wrong_catch", {
            "test_invalid_json_preserves_parse_cause": ["DEDICATED_EXCEPTION_TYPE"],
            "test_invalid_encoding_preserves_unicode_cause": ["DEDICATED_EXCEPTION_TYPE"],
        })


if __name__ == "__main__":
    unittest.main()
