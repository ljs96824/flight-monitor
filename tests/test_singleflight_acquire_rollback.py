"""Synthetic single-use release contracts; never open the default lock path."""

import ast
from contextlib import ExitStack
import importlib
import inspect
from pathlib import Path
import sys
import tempfile
import textwrap
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class CountedLock:
    """Count calls while all locking is performed by the real registered mutex."""

    def __init__(self, raw):
        self.raw = raw
        self.release_calls = 0
        self.release_successes = 0

    def acquire(self, *args, **kwargs):
        return self.raw.acquire(*args, **kwargs)

    def release(self):
        self.release_calls += 1
        self.raw.release()
        self.release_successes += 1


class CloseFaultStream:
    def __init__(self, raw, errors):
        self.raw = raw
        self.errors = list(errors)
        self.close_calls = 0

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def close(self):
        self.close_calls += 1
        if self.errors:
            raise self.errors.pop(0)
        self.raw.close()


class AcquireHarness:
    def __init__(self, module, *, os_busy=False, close_errors=()):
        self.module = module
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = (Path(self.tmp.name) / "singleflight.lock").resolve()
        self.path.write_bytes(b"\0{}")
        self.key = str(self.path)
        self.raw = module._thread_lock_for(self.path)
        self.counted = CountedLock(self.raw)
        self.close_errors = list(close_errors)
        self.streams = []
        self.gates = []
        self.open_error = None
        self.backend = SimpleNamespace(try_lock=Mock(return_value=not os_busy), unlock=Mock())
        self.stack = ExitStack()

    def __enter__(self):
        module = self.module
        original_open = Path.open

        def open_stream(path, mode="r", *args, **kwargs):
            if path == self.path and mode == "r+b":
                if self.open_error is not None:
                    raise self.open_error
                stream = CloseFaultStream(original_open(path, mode, *args, **kwargs), self.close_errors)
                self.streams.append(stream)
                return stream
            return original_open(path, mode, *args, **kwargs)

        def registered_lock(path):
            if path != self.path or module._thread_locks[self.key] is not self.raw:
                raise AssertionError("REGISTERED_LOCK_IDENTITY_CHANGED")
            return self.counted

        for name, value in {
            "_thread_lock_for": registered_lock,
            "LOCK_BACKEND": self.backend,
            "_read_holder_stream": Mock(return_value={"lease_id": "synthetic-lease", "state": "released"}),
            "_read_holder_path": Mock(return_value={"round_id": "synthetic-holder"}),
            "_write_holder": Mock(),
            "safe_log": Mock(),
            "uuid4": Mock(return_value="synthetic-lease"),
        }.items():
            self.stack.enter_context(patch.object(module, name, value))
        self.stack.enter_context(patch.object(Path, "open", open_stream))
        self.heartbeat = self.stack.enter_context(patch.object(module.CollectionSingleflightGate, "start_heartbeat"))
        self.stack.enter_context(patch.object(module.socket, "gethostname", return_value="synthetic-host"))
        return self

    def disarm(self):
        self.close_errors = []
        self.open_error = None
        self.backend.try_lock.return_value = True
        self.module._write_holder.side_effect = None
        self.heartbeat.side_effect = None
        self.module.safe_log.side_effect = None

    def call(self, function, round_id="synthetic-round"):
        # An isolated AST copy must consume the same patched dependencies as production.
        names = ("_thread_lock_for", "LOCK_BACKEND", "_read_holder_stream", "_read_holder_path",
                 "_write_holder", "safe_log", "uuid4")
        with patch.dict(function.__globals__, {name: getattr(self.module, name) for name in names}):
            try:
                gate = function(round_id, lock_path=self.path, heartbeat_interval_seconds=0)
            except Exception as exc:
                return None, exc
        self.gates.append(gate)
        return gate, None

    def snapshot(self):
        return dict(locked=self.raw.locked(), release_calls=self.counted.release_calls,
                    release_successes=self.counted.release_successes,
                    close_calls=sum(s.close_calls for s in self.streams),
                    same_registered_lock=self.module._thread_locks[self.key] is self.raw)

    def __exit__(self, *args):
        # Teardown runs only after observations/reacquisition; never reset the registry first.
        self.disarm()
        try:
            for gate in reversed(self.gates):
                if gate.acquired and not gate._released:
                    try:
                        gate.release()
                    except (OSError, RuntimeError):
                        pass
            for stream in self.streams:
                stream.raw.close()
            if self.raw.locked():
                self.raw.release()
            with self.module._thread_locks_guard:
                if self.module._thread_locks.get(self.key) is self.raw:
                    del self.module._thread_locks[self.key]
        finally:
            self.stack.close()
            self.tmp.cleanup()


class SingleflightAcquireRollbackTest(unittest.TestCase):
    def setUp(self):
        dotenv = SimpleNamespace(load_dotenv=Mock(return_value=False), dotenv_values=Mock(return_value={}))
        with patch.dict(sys.modules, {"dotenv": dotenv}):
            self.module = importlib.import_module("collection_singleflight")
        self.last_observation = None

    def observe_fault(self, function, stage):
        first_close = OSError("first-close-failed")
        rollback_close = OSError("rollback-close-failed")
        original = ValueError(stage + "-failed")
        errors = ([first_close] if stage == "first_close" else
                  [first_close, rollback_close] if stage == "both_closes" else [rollback_close])
        with AcquireHarness(self.module, os_busy=stage in {"first_close", "both_closes"}, close_errors=errors) as h:
            if stage == "metadata":
                self.module._write_holder.side_effect = original
            elif stage == "heartbeat":
                h.heartbeat.side_effect = original
            gate, error = h.call(function)
            first = h.snapshot()
            h.disarm()
            following, next_error = h.call(function, "following-round")
            observation = dict(stage=stage, first=first, returned_gate=gate is not None,
                               exception_type=type(error).__name__ if error else None,
                               exception_text=str(error) if error else None,
                               next_acquired=bool(following and following.acquired),
                               next_error=type(next_error).__name__ if next_error else None,
                               same_registered_lock=h.module._thread_locks[h.key] is h.raw)
            self.last_observation = observation
            self.assertIsNotNone(error, "close_error_missing")
            self.assertIsNone(gate)
            self.assertFalse(first["locked"], "rollback_lock_retained")
            self.assertEqual(first["release_calls"], 1, "rollback_release_count")
            self.assertEqual(first["release_successes"], 1)
            self.assertEqual(first["close_calls"], 2 if stage in {"first_close", "both_closes"} else 1)
            self.assertTrue(first["same_registered_lock"] and observation["same_registered_lock"])
            self.assertIsNone(next_error)
            self.assertTrue(observation["next_acquired"], "same_lock_did_not_recover")
            if stage == "first_close":
                self.assertIs(error, first_close, "first_close_error_obscured")
            else:
                self.assertIs(error, rollback_close, "rollback_close_error_obscured")
                self.assertIs(error.__context__, first_close if stage == "both_closes" else original,
                              "original_failure_not_traceable")
            return observation

    def observe_busy_report(self, function, successor=False):
        original = ValueError("busy-report-failed")
        enter = threading.Event()
        acquired = threading.Event()
        finish = threading.Event()
        worker_state = {}
        with AcquireHarness(self.module, os_busy=True) as h:
            def hold_next():
                if not enter.wait(10):
                    worker_state["error"] = "HANDOFF_NOT_REQUESTED"
                    acquired.set()
                    return
                worker_state["acquired"] = h.raw.acquire(blocking=False)
                acquired.set()
                if not finish.wait(10):
                    worker_state["error"] = "OWNER_NOT_RELEASED_BY_TEST"
                if worker_state["acquired"]:
                    try:
                        h.raw.release()
                    except RuntimeError as exc:
                        worker_state["release_error"] = str(exc)

            def report_failure(*_args, **_kwargs):
                if successor:
                    enter.set()
                    if not acquired.wait(10) or not worker_state.get("acquired"):
                        raise AssertionError("SUCCESSOR_SYNCHRONIZATION_FAILED")
                raise original

            self.module.safe_log.side_effect = report_failure
            thread = threading.Thread(target=hold_next) if successor else None
            if thread:
                thread.start()
            try:
                gate, error = h.call(function)
                first = h.snapshot()
                self.last_observation = dict(stage="busy_report", successor=successor, first=first,
                    successor_acquired=worker_state.get("acquired"), returned_gate=gate is not None,
                    exception_type=type(error).__name__ if error else None, exception_text=str(error) if error else None)
                if successor:
                    self.assertTrue(worker_state.get("acquired"), "successor_not_acquired")
                    self.assertTrue(first["locked"], "successor_lock_released")
                else:
                    self.assertFalse(first["locked"])
                self.assertEqual(first["release_calls"], 1, "release_responsibility_reused")
                self.assertEqual(first["release_successes"], 1)
                self.assertTrue(first["same_registered_lock"])
                self.assertIs(error, original, "busy_report_error_obscured")
                self.assertIsNone(gate)
            finally:
                if thread:
                    finish.set()
                    enter.set()
                    thread.join(10)
                    self.assertFalse(thread.is_alive(), "SUCCESSOR_THREAD_NOT_FINISHED")
            self.assertNotIn("error", worker_state)
            self.assertNotIn("release_error", worker_state)
            h.disarm()
            following, error = h.call(function, "following-round")
            self.assertIsNone(error)
            self.assertTrue(following.acquired)
            return self.last_observation

    def assert_gate_handoff(self, function):
        with AcquireHarness(self.module) as h:
            gate, error = h.call(function)
            self.last_observation = dict(stage="success", first=h.snapshot())
            self.assertIsNone(error)
            self.assertTrue(gate.acquired)
            self.assertIs(gate._thread_lock, h.counted)
            self.assertIs(h.counted.raw, h.module._thread_locks[h.key])
            self.assertEqual(h.counted.release_calls, 0, "success_gate_released_by_acquirer")
            self.assertTrue(h.raw.locked(), "success_gate_lock_not_held")
            busy, error = h.call(function, "contender-round")
            self.assertIsNone(error)
            self.assertFalse(busy.acquired, "gate_did_not_exclude_contender")
            self.assertEqual(h.counted.release_calls, 0)
            gate.release()
            self.assertEqual(h.counted.release_calls, 1)
            self.assertFalse(h.raw.locked())
            following, error = h.call(function, "following-round")
            self.assertIsNone(error)
            self.assertTrue(following.acquired)
            self.assertEqual(h.counted.release_calls, 1)
            following.release()
            self.assertEqual(h.counted.release_calls, 2)

    def test_first_close_failure_then_rollback_success(self):
        self.observe_fault(self.module.acquire_collection_singleflight, "first_close")

    def test_both_busy_closes_fail(self):
        self.observe_fault(self.module.acquire_collection_singleflight, "both_closes")

    def test_busy_report_failure_does_not_release_twice(self):
        self.observe_busy_report(self.module.acquire_collection_singleflight)

    def test_busy_report_failure_preserves_successor_lock(self):
        self.observe_busy_report(self.module.acquire_collection_singleflight, successor=True)

    def test_success_transfers_responsibility_to_gate(self):
        self.assert_gate_handoff(self.module.acquire_collection_singleflight)

    def test_metadata_failure_and_rollback_close_failure(self):
        self.observe_fault(self.module.acquire_collection_singleflight, "metadata")

    def test_heartbeat_failure_and_rollback_close_failure(self):
        self.observe_fault(self.module.acquire_collection_singleflight, "heartbeat")

    def test_thread_busy_never_assumes_release_responsibility(self):
        with AcquireHarness(self.module) as h:
            self.assertTrue(h.raw.acquire(blocking=False))
            gate, error = h.call(self.module.acquire_collection_singleflight)
            self.assertIsNone(error)
            self.assertFalse(gate.acquired)
            self.assertTrue(h.raw.locked())
            self.assertEqual(h.counted.release_calls, 0)
            self.assertEqual(h.streams, [])
            h.backend.try_lock.assert_not_called()
            self.assertIs(h.module._thread_locks[h.key], h.raw)

    def test_os_busy_releases_once(self):
        with AcquireHarness(self.module, os_busy=True) as h:
            gate, error = h.call(self.module.acquire_collection_singleflight)
            self.assertIsNone(error)
            self.assertFalse(gate.acquired)
            self.assertEqual(h.counted.release_calls, 1)
            self.assertFalse(h.raw.locked())
            h.disarm()
            following, error = h.call(self.module.acquire_collection_singleflight)
            self.assertIsNone(error)
            self.assertTrue(following.acquired)

    def test_file_open_failure_releases_acquired_thread_lock(self):
        with AcquireHarness(self.module) as h:
            original = OSError("file-open-failed")
            h.open_error = original
            gate, error = h.call(self.module.acquire_collection_singleflight)
            self.assertIsNone(gate)
            self.assertIs(error, original)
            self.assertEqual(h.streams, [])
            self.assertEqual(h.counted.release_calls, 1)
            self.assertFalse(h.raw.locked())
            h.disarm()
            following, error = h.call(self.module.acquire_collection_singleflight)
            self.assertIsNone(error)
            self.assertTrue(following.acquired)

    def mutated(self, kind):
        tree = ast.parse(textwrap.dedent(inspect.getsource(self.module.acquire_collection_singleflight)))
        original = ast.dump(tree, include_attributes=False)
        function = tree.body[0]
        self.assertIsInstance(function, ast.FunctionDef)
        self.assertEqual(function.name, "acquire_collection_singleflight")
        outer = [n for n in function.body if isinstance(n, ast.Try) and n.handlers]
        self.assertEqual(len(outer), 1, "mutation_outer_try_match")
        outer = outer[0]
        handler = outer.handlers[0]
        self.assertEqual(ast.unparse(handler.type), "Exception")
        matches = 0
        if kind in {"rollback_order", "repeat_release"}:
            cleanup = handler.body[0]
            self.assertIsInstance(cleanup, ast.Try)
            responsibility = cleanup.finalbody[0]
            self.assertIsInstance(responsibility, ast.If)
            self.assertEqual(ast.unparse(responsibility.test), "release_pending")
            if kind == "rollback_order":
                handler.body = cleanup.body + cleanup.finalbody + handler.body[1:]
            else:
                cleanup.finalbody = responsibility.body
            matches = 1
        elif kind == "swallow_close":
            class Swallow(ast.NodeTransformer):
                def visit_Expr(self, node):
                    nonlocal matches
                    if isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == "lock_file.close":
                        matches += 1
                        return ast.Try(body=[node], handlers=[ast.ExceptHandler(
                            type=ast.Name(id="OSError", ctx=ast.Load()), body=[ast.Pass()])], orelse=[], finalbody=[])
                    return self.generic_visit(node)
            tree = Swallow().visit(tree)
        elif kind == "release_success":
            for i, node in enumerate(outer.body[:-1]):
                if (isinstance(node, ast.Assign) and ast.unparse(node) == "release_pending = False"
                        and isinstance(outer.body[i + 1], ast.Return)
                        and ast.unparse(outer.body[i + 1].value) == "gate"):
                    outer.body[i:i + 1] = ast.parse("thread_lock.release()").body + [node]
                    matches += 1
                    break
        self.assertEqual(matches, 2 if kind == "swallow_close" else 1, "mutation_exact_match")
        self.assertNotEqual(ast.dump(tree, include_attributes=False), original, "mutation_changed")
        compiled = compile(ast.fix_missing_locations(tree), "<singleflight-mutation>", "exec")
        namespace = dict(self.module.acquire_collection_singleflight.__globals__)
        exec(compiled, namespace)
        return namespace["acquire_collection_singleflight"]

    def test_mutation_rollback_order_is_rejected(self):
        changed = self.mutated("rollback_order")
        for stage in ("both_closes", "metadata"):
            with self.subTest(stage=stage):
                with self.assertRaisesRegex(AssertionError, "rollback_lock_retained"):
                    self.observe_fault(changed, stage)

    def test_mutation_swallowed_close_is_rejected(self):
        changed = self.mutated("swallow_close")
        with self.assertRaisesRegex(AssertionError, "close_error_missing"):
            self.observe_fault(changed, "both_closes")

    def test_mutation_repeated_release_is_rejected(self):
        changed = self.mutated("repeat_release")
        for successor, marker in ((False, "release_responsibility_reused"), (True, "successor_lock_released")):
            with self.subTest(successor=successor):
                with self.assertRaisesRegex(AssertionError, marker):
                    self.observe_busy_report(changed, successor=successor)

    def test_mutation_success_release_is_rejected(self):
        changed = self.mutated("release_success")
        with self.assertRaisesRegex(AssertionError, "success_gate_released_by_acquirer"):
            self.assert_gate_handoff(changed)


if __name__ == "__main__":
    unittest.main()
