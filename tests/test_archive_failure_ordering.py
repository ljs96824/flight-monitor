"""Store-before-archive contracts using synthetic sources and temporary state."""

import ast
from contextlib import closing, contextmanager, ExitStack, redirect_stdout
import copy
from datetime import datetime
import inspect
import io
import itertools
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import patch

import api_usage
import collection_ledger
from collection_plan import CollectionPlan
import log_utils
import observations_store
import request_cache as cache


ROUND = "synthetic-archive-ordering"
DEPARTURE = "2026-10-01"
NOW = datetime(2026, 9, 20, 12)
SOURCE_NAMES = ("juhe", "hasdata", "serpapi", "duffel")
OUTCOMES = ("failed", "empty", "positive", "exception", "quota")
FAULTS = ("none", "write", "flush")


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.replace(tzinfo=tz)


class SyntheticSource:
    route_type = "international"
    supported_cabins = ["economy"]

    def __init__(self, name, outcome):
        self.name, self.outcome, self.calls = name, outcome, 0

    def response(self):
        result = {"source": self.name, "flights": [], "source_status": "success",
                  "raw": {"synthetic": True, "outcome": self.outcome}}
        if self.outcome == "failed":
            result.update(source_status="failed", error="SYNTHETIC_FAILED")
        elif self.outcome == "quota":
            result.update(source_status="failed_quota", error="SYNTHETIC_QUOTA", quota_code="112")
        elif self.outcome == "positive":
            result["flights"] = [{"flight_combo": "ZZ100", "price": 100, "cabin_class": "economy"}]
        return result

    def fetch(self, origin, dest, date_str, cabin_class):
        self.calls += 1
        if self.outcome == "exception":
            raise ValueError("SYNTHETIC_SOURCE_ERROR")
        if self.outcome == "io_always" or (self.outcome == "io_once" and self.calls == 1):
            raise OSError("SYNTHETIC_SOURCE_IO")
        return self.response()


class FaultStream:
    def __init__(self, stream, fault, error):
        self.stream, self.fault, self.error = stream, fault, error
        self.writes = self.flushes = 0

    def write(self, text):
        self.writes += 1
        if self.fault == "write":
            raise self.error
        return self.stream.write(text)

    def flush(self):
        self.flushes += 1
        if self.fault == "flush":
            raise self.error
        return self.stream.flush()


def _is_call(statement, name):
    return (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name) and statement.value.func.id == name)


def _mutated_function(kind):
    original = cache._archive_listing_result if kind == "persistent_gate" else cache.cached_fetch
    tree = ast.parse(inspect.getsource(original))
    before = ast.dump(tree)
    function = tree.body[0]
    matches = 0
    if kind in {"exception_order", "quota_order"}:
        containers = [n for n in ast.walk(function)
                      if ((kind == "exception_order" and isinstance(n, ast.ExceptHandler)
                           and n.name == "exc") or
                          (kind == "quota_order" and isinstance(n, ast.If)
                           and ast.unparse(n.test) == "quota_reason"))]
        for container in containers:
            for i, node in enumerate(container.body):
                if _is_call(node, "_archive_listing_result"):
                    assert i > 0 and _is_call(container.body[i - 1], "_store_round_only_result"), "mutation_order_shape"
                    container.body[i - 1:i + 1] = [node, container.body[i - 1]]
                    matches += 1
    elif kind in {"public_order", "swallow"}:
        for i, node in enumerate(function.body):
            if _is_call(node, "_archive_listing_result"):
                if kind == "public_order":
                    assert isinstance(function.body[i - 1], ast.If), "mutation_public_shape"
                    assert ast.unparse(function.body[i - 1].test) == "cache_status == 'persistent'"
                    function.body[i - 1:i + 1] = [node, function.body[i - 1]]
                else:
                    function.body[i] = ast.Try(body=[node], handlers=[ast.ExceptHandler(
                        type=ast.Name(id="Exception", ctx=ast.Load()), name=None, body=[ast.Pass()])],
                        orelse=[], finalbody=[])
                matches += 1
    elif kind == "persistent_gate":
        for node in list(function.body):
            if isinstance(node, ast.If) and ast.unparse(node.test) == "cache_status == 'persistent'":
                assert len(node.body) == 1 and isinstance(node.body[0], ast.Return)
                function.body.remove(node)
                matches += 1
    assert matches == 1, f"mutation_match_count:{kind}:{matches}"
    ast.fix_missing_locations(tree)
    assert ast.dump(tree) != before, "mutation_unchanged"
    code = compile(tree, "<archive-ordering-mutation>", "exec")
    namespace = dict(cache.__dict__)
    exec(code, namespace)
    candidate = namespace[original.__name__]
    # Keep runtime globals live so round setup and the local append binding apply.
    candidate = types.FunctionType(candidate.__code__, cache.__dict__, candidate.__name__, candidate.__defaults__)
    candidate.__kwdefaults__ = namespace[original.__name__].__kwdefaults__
    return candidate, {"matches": matches, "changed": True, "compiled": True}


class ArchiveFailureOrderingTest(unittest.TestCase):
    @contextmanager
    def scenario(self, name="juhe", outcome="empty", fault="write", *, round_id=ROUND, active=True):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp, ExitStack() as stack:
            root = Path(tmp)
            source = SyntheticSource(name, outcome)
            cache.reset_for_tests(root / "cache")
            stack.callback(cache.reset_for_tests, None)
            observations_store.set_current_round(round_id or "", root / "observations.sqlite3")
            collection_ledger.init_collection_ledger(root / "observations.sqlite3")
            api_usage.initialize_usage_ledger(root / "usage.json")
            cache.start_request_cache_round(round_id or "", track_usage=True,
                                            usage_path=root / "usage.json", entrypoint="synthetic_test")
            logs = io.StringIO()
            stack.enter_context(redirect_stdout(logs))
            stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("NO_NETWORK")))
            stack.enter_context(patch.object(cache, "datetime", FixedDateTime))
            sleep = stack.enter_context(patch.object(cache.time, "sleep"))
            key = cache.cache_key(source, "PVG", "KIX", DEPARTURE)
            self.assertEqual(cache._request_cache, {}, "CLEAN_POOL")
            self.assertEqual(cache._round_only_result_keys, set())
            self.assertEqual(cache._resolved_request_keys_this_round, set())
            self.assertEqual(cache._source_circuit_breakers, {})
            error = OSError("SYNTHETIC_ARCHIVE_" + fault.upper())
            path = original = None
            if active:
                path = log_utils.start_round_log_archive(ROUND, root_dir=root / "rounds", now=NOW)
                original = log_utils._round_log_state["file"]
            stream = FaultStream(original or io.StringIO(), fault, error)
            if active:
                log_utils._round_log_state["file"] = stream
            env = types.SimpleNamespace(root=root, source=source, key=key, logs=logs, path=path,
                                        stream=stream, error=error, sleep=sleep, observed=[], active=active)
            real_append = log_utils.append_round_evidence

            def append(prefix, payload):
                self.assertNotEqual(source.outcome, "positive", "PERSISTENT_MUST_SKIP_ARCHIVE")
                self.assert_pool(env)
                env.observed.append(copy.deepcopy(cache._request_cache[key]))
                return real_append(prefix, payload)

            stack.enter_context(patch.object(cache, "append_round_evidence", side_effect=append))
            try:
                yield env
            finally:
                if active:
                    log_utils._round_log_state["file"] = original
                    log_utils.end_round_log_archive(status="synthetic_cleanup")

    def expected_result(self, source):
        if source.outcome in {"exception", "io_always"}:
            kind = "ValueError" if source.outcome == "exception" else "OSError"
            text = "SYNTHETIC_SOURCE_ERROR" if kind == "ValueError" else "SYNTHETIC_SOURCE_IO"
            return {"flights": [], "source": source.name, "raw": {}, "source_status": "failed",
                    "retry_count": int(source.outcome == "io_always"), "error": kind + ":" + text,
                    "error_type": kind, "errno": None, "path": None}
        result = source.response()
        if source.outcome != "quota":
            fields = {"collected_at": "2026-09-20T12:00:00", "collection_state": "fresh",
                      "collection_label": "实时采集12:00"}
            result.update(fields)
            for flight in result["flights"]:
                flight.update(fields)
        if source.outcome == "io_once":
            result["retry_count"] = 1
        return result

    def pool_status(self, outcome):
        if outcome == "positive":
            return "fresh"
        return "round_failed" if outcome in {"failed", "exception", "quota", "io_always"} else "round_empty"

    def assert_pool(self, env):
        key = env.key
        self.assertIn(key, cache._request_cache, "POOL_BEFORE_ARCHIVE")
        self.assertEqual(key in cache._round_only_result_keys, env.source.outcome != "positive", "ROUND_ONLY_REGISTERED")
        self.assertEqual(key in cache._resolved_request_keys_this_round, bool(cache._current_stats_round_id), "RESOLVED_REGISTERED")
        entry = cache._request_cache[key]
        self.assertEqual(entry["cache_status"], self.pool_status(env.source.outcome), "POOL_CLASSIFICATION")
        self.assertEqual(entry["result"], self.expected_result(env.source), "NORMALIZED_RESULT_PRESERVED")
        if env.source.outcome == "quota":
            self.assertEqual(cache._source_circuit_breakers.get(env.source.name), "SYNTHETIC_QUOTA", "BREAKER_BEFORE_ARCHIVE")

    def fetch(self, env, function=None, **kwargs):
        return (function or cache.cached_fetch)(env.source, "PVG", "KIX", DEPARTURE,
            cache_dir=env.root / "cache", include_cache_details=True, **kwargs)

    def assert_case(self, env, fault, function=None):
        archival = env.source.name in cache.LISTING_OBSERVATION_SOURCES and env.source.outcome != "positive"
        broken = archival and fault != "none" and env.active
        if broken:
            with self.assertRaises(OSError, msg="ARCHIVE_ERROR_MUST_PROPAGATE") as raised:
                self.fetch(env, function)
            self.assertIs(raised.exception, env.error, "ORIGINAL_ARCHIVE_EXCEPTION")
        else:
            result, status, reuse = self.fetch(env, function)
            self.assertEqual((result, status, reuse), (self.expected_result(env.source), "fresh", None))
        self.assert_pool(env)
        first_calls = env.source.calls
        self.assertEqual(first_calls, 2 if env.source.outcome in {"io_once", "io_always"} else 1)
        self.assertEqual(len(env.observed), int(archival))
        self.assertEqual(env.stream.writes, int(archival and env.active))
        self.assertEqual(env.stream.flushes, int(archival and env.active and fault != "write"))
        if env.path is not None:
            lines = env.path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(sum(s.startswith("[源响应证据]") for s in lines), int(archival and fault != "write"))
        self.assertEqual(cache._cache_path(env.key, env.root / "cache").exists(), env.source.outcome == "positive")
        self.assertNotIn("SYNTHETIC_ARCHIVE_", env.logs.getvalue())
        saved = copy.deepcopy(cache._request_cache[env.key])
        result, status, reuse = self.fetch(env, function)
        self.assertEqual(env.source.calls, first_calls, "REENTRY_NO_NEW_SOURCE_CALL")
        self.assertEqual(reuse, "in_round_cache")
        self.assertEqual(status, "cache" if env.source.outcome == "positive" else self.pool_status(env.source.outcome))
        self.assertEqual(result, self.expected_result(env.source), "ORIGINAL_RESULT_ON_REENTRY")
        result["raw"]["caller_only"] = True
        result["flights"].append({"price": 1})
        self.assertEqual(cache._request_cache[env.key], saved, "RETURN_COPY_ISOLATED")
        self.assertEqual(len(env.observed), int(archival), "CACHE_HIT_IS_NOT_ARCHIVE_RETRY")
        usage = api_usage.load_usage_strict(env.root / "usage.json")
        self.assertEqual(api_usage.round_actual_counts(usage, ROUND), {env.source.name: first_calls})
        self.assertTrue(all("success" not in row for row in usage["entries"]))
        if env.source.outcome == "quota":
            next_result, _, _ = cache.cached_fetch(env.source, "PVG", "KIX", "2026-10-02",
                cache_dir=env.root / "cache", include_cache_details=True)
            self.assertEqual(next_result["source_status"], "skipped_source_disabled")
            self.assertEqual(env.source.calls, first_calls)

    def matrix(self, outcome):
        for name, fault in itertools.product(SOURCE_NAMES, FAULTS):
            with self.subTest(source=name, outcome=outcome, fault=fault):
                with self.scenario(name, outcome, fault) as env:
                    self.assert_case(env, fault)

    def test_failed_result_matrix(self):
        self.matrix("failed")

    def test_empty_result_matrix(self):
        self.matrix("empty")

    def test_persistent_result_matrix(self):
        self.matrix("positive")

    def test_source_exception_matrix(self):
        self.matrix("exception")

    def test_quota_result_matrix(self):
        self.matrix("quota")

    def test_plan_abort_matrix(self):
        for outcome, fault in itertools.product(OUTCOMES, FAULTS):
            with self.subTest(outcome=outcome, fault=fault):
                with self.scenario(outcome=outcome, fault=fault) as env:
                    other = SyntheticSource("serpapi", "positive")
                    plan = CollectionPlan()
                    first = plan.add_request(env.source, "PVG", "KIX", DEPARTURE, consumer="synthetic_a")
                    duplicate = plan.add_request(env.source, "PVG", "KIX", DEPARTURE, consumer="synthetic_b")
                    plan.add_request(other, "PVG", "KIX", "2026-10-02")
                    self.assertIs(first, duplicate)
                    self.assertEqual(plan.unique_count, 2)
                    broken = outcome != "positive" and fault != "none"
                    if broken:
                        with self.assertRaises(OSError) as raised:
                            plan.execute()
                        self.assertIs(raised.exception, env.error)
                        self.assertEqual(plan._results, {}, "NO_NORMAL_PLAN_RESULT")
                    else:
                        report = plan.execute()
                        self.assertFalse(report.ledger_degraded)
                    self.assertEqual((env.source.calls, other.calls), (1, 0 if broken else 1))
                    self.assert_pool(env)
                    with closing(sqlite3.connect(env.root / "observations.sqlite3")) as db:
                        rows = db.execute("SELECT execution_status,error_type FROM collection_cells ORDER BY depart_date").fetchall()
                    expected = ("success" if outcome == "positive" else "empty" if outcome == "empty" else "failed")
                    if broken:
                        self.assertEqual(rows, [("failed", "OSError"), ("interrupted", "ProcessInterrupted")])
                    else:
                        self.assertEqual([row[0] for row in rows], [expected, "success"])
                    usage = api_usage.round_actual_counts(api_usage.load_usage_strict(env.root / "usage.json"), ROUND)
                    self.assertEqual(usage, {"juhe": 1} if broken else {"juhe": 1, "serpapi": 1})

    def test_ledger_degradation_matrix(self):
        for fault in ("write", "flush"):
            with self.subTest(fault=fault), self.scenario(fault=fault) as env:
                session = collection_ledger.CollectionLedgerSession(round_id=ROUND, db_path=env.root / "observations.sqlite3")
                session._degrade("terminal", OSError("SYNTHETIC_LEDGER_ERROR"))
                self.assertTrue(session.degraded)
                self.assertIn("[采集台账降级]", env.logs.getvalue())
                self.assertIn("[采集台账证据失败]", env.logs.getvalue())
                self.assertIn(str(env.error), env.logs.getvalue())

    def test_no_active_archive(self):
        with self.scenario(active=False) as env:
            self.assertFalse(log_utils.append_round_evidence("[synthetic] ", {}))
            self.assert_case(env, "write")

    def test_source_io_retry_boundary(self):
        for outcome in ("io_once", "io_always"):
            with self.subTest(outcome=outcome), self.scenario(outcome=outcome) as env:
                self.assert_case(env, "write")
                env.sleep.assert_called_once_with(cache.SOURCE_FETCH_IO_RETRY_DELAY_SECONDS)
                self.assertEqual(cache.get_request_cache_stats()["retries"], 1)

    def test_no_round_does_not_register_resolved(self):
        with self.scenario(round_id=None) as env:
            with self.assertRaises(OSError) as raised:
                self.fetch(env)
            self.assertIs(raised.exception, env.error)
            self.assert_pool(env)
            self.assertNotIn(env.key, cache._resolved_request_keys_this_round)

    def test_new_round_ordinary_results_are_cleared(self):
        for outcome in ("failed", "empty", "exception"):
            with self.subTest(outcome=outcome), self.scenario(outcome=outcome) as env:
                self.assert_case(env, "write")
                cache.start_request_cache_round("synthetic-next", track_usage=True, usage_path=env.root / "usage.json")
                self.assertNotIn(env.key, cache._request_cache)
                self.assertNotIn(env.key, cache._round_only_result_keys)
                self.assertNotIn(env.key, cache._resolved_request_keys_this_round)
                with self.assertRaises(OSError) as raised:
                    self.fetch(env)
                self.assertIs(raised.exception, env.error)
                self.assertEqual(env.source.calls, 2)

    def test_new_round_preserves_quota_breaker(self):
        with self.scenario(outcome="quota") as env:
            self.assert_case(env, "write")
            cache.start_request_cache_round("synthetic-next")
            self.assertNotIn(env.key, cache._request_cache)
            self.assertNotIn(env.key, cache._round_only_result_keys)
            self.assertNotIn(env.key, cache._resolved_request_keys_this_round)
            self.assertEqual(cache._source_circuit_breakers["juhe"], "SYNTHETIC_QUOTA")
            result, status, _ = self.fetch(env)
            self.assertEqual((result["source_status"], status, env.source.calls), ("skipped_source_disabled", "skipped", 1))

    def test_new_round_preserves_persistent_age(self):
        with self.scenario(outcome="positive") as env:
            self.assert_case(env, "write")
            before = copy.deepcopy(cache._request_cache[env.key])
            cache.start_request_cache_round("synthetic-next")
            self.assertEqual(cache._request_cache[env.key], before)
            _, status, _ = self.fetch(env)
            self.assertEqual((status, env.source.calls), ("cache", 1))
            self.assertEqual(cache._request_cache[env.key], before)
            self.assertEqual(env.observed, [])

    def test_persistent_write_caught_errors(self):
        for error in (TypeError("SYNTHETIC_SERIALIZE"), OSError("SYNTHETIC_WRITE")):
            with self.subTest(error=type(error).__name__), self.scenario(outcome="positive") as env:
                real_dumps = json.dumps
                serialization_hits = []

                def dumps(value, *args, **kwargs):
                    if (isinstance(value, dict) and set(value) == {"fetched_at", "key", "producer_class", "result"}
                            and value["key"] == list(env.key)):
                        serialization_hits.append(True)
                        raise error
                    return real_dumps(value, *args, **kwargs)

                point = patch.object(cache.json, "dumps", side_effect=dumps) if isinstance(error, TypeError) else patch.object(Path, "write_text", side_effect=error)
                with patch.object(cache, "_record_observations_after_fetch"), patch.object(cache, "safe_log") as log, point:
                    result, status, _ = self.fetch(env)
                disabled = str(cache._cache_path(env.key, env.root / "cache").parent)
                self.assertIn(disabled, cache._disabled_persistent_dirs)
                self.assertEqual((result["source_status"], status), ("success", "fresh"))
                self.assertTrue(any("持久化失败" in str(c.args[0]) and str(error) in str(c.args[0]) for c in log.call_args_list))
                self.assertEqual(env.observed, [])
                self.assertEqual(len(serialization_hits), int(isinstance(error, TypeError)))
                self.assertEqual(api_usage.round_actual_counts(api_usage.load_usage_strict(env.root / "usage.json"), ROUND), {"juhe": 1})
                with patch.object(Path, "write_text") as write:
                    cache._write_persistent(env.key, result, env.root / "cache", source=env.source)
                write.assert_not_called()

    def test_persistent_write_uncaught_error(self):
        with self.scenario(outcome="positive") as env:
            error = RuntimeError("SYNTHETIC_UNCAUGHT_WRITE")
            with patch.object(cache, "safe_log"), patch.object(Path, "write_text", side_effect=error):
                with self.assertRaises(RuntimeError) as raised:
                    self.fetch(env)
            self.assertIs(raised.exception, error)
            self.assertEqual(cache._disabled_persistent_dirs, set())
            self.assertEqual(env.observed, [])

    def test_persistent_error_logger_failure_propagates(self):
        with self.scenario(outcome="positive") as env:
            log_error = RuntimeError("SYNTHETIC_LOG_FAILURE")

            def log(message):
                if "持久化失败" in message:
                    raise log_error

            with patch.object(cache, "safe_log", side_effect=log), patch.object(Path, "write_text", side_effect=OSError("SYNTHETIC_WRITE")):
                with self.assertRaises(RuntimeError) as raised:
                    self.fetch(env)
            self.assertIs(raised.exception, log_error)
            self.assertIn(str(cache._cache_path(env.key).parent), cache._disabled_persistent_dirs)

    def test_persist_false_skips_writer(self):
        with self.scenario(outcome="positive") as env, patch.object(cache, "_write_persistent", wraps=cache._write_persistent) as write:
            result, status, _ = self.fetch(env, persist=False)
            self.assertEqual((result["source_status"], status), ("success", "fresh"))
            write.assert_not_called()
            self.assertEqual(env.observed, [])

    def assert_mutation(self, kind, outcome, reason):
        mutant, evidence = _mutated_function(kind)
        self.assertEqual(evidence, {"matches": 1, "changed": True, "compiled": True})
        with self.scenario(outcome=outcome) as env:
            target = "_archive_listing_result" if kind == "persistent_gate" else "cached_fetch"
            with patch.object(cache, target, mutant):
                with self.assertRaisesRegex(AssertionError, reason):
                    self.assert_case(env, "write")

    def test_mutation_exception_order(self):
        self.assert_mutation("exception_order", "exception", "POOL_BEFORE_ARCHIVE")

    def test_mutation_quota_order(self):
        self.assert_mutation("quota_order", "quota", "POOL_BEFORE_ARCHIVE")

    def test_mutation_public_order(self):
        self.assert_mutation("public_order", "empty", "POOL_BEFORE_ARCHIVE")

    def test_mutation_swallowed_archive_error(self):
        self.assert_mutation("swallow", "empty", "ARCHIVE_ERROR_MUST_PROPAGATE")

    def test_mutation_persistent_gate(self):
        self.assert_mutation("persistent_gate", "positive", "PERSISTENT_MUST_SKIP_ARCHIVE")


if __name__ == "__main__":
    unittest.main()
