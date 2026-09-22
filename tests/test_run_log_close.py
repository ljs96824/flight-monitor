"""Explicit run-log cleanup calls; no real streams or files are replaced."""

import ast
import inspect
import unittest

import log_utils


class _RecordingFile:
    def __init__(self, flush_fails=False, close_fails=False):
        self.flush_fails = flush_fails
        self.close_fails = close_fails
        self.calls = []

    def flush(self):
        self.calls.append("flush")
        if self.flush_fails:
            raise OSError("synthetic flush failure")

    def close(self):
        self.calls.append("close")
        if self.close_fails:
            raise OSError("synthetic close failure")


def _observe(close_run_log, *, flush_fails=False, close_fails=False):
    namespace = close_run_log.__globals__
    original_state = namespace["_run_log_state"]
    file = _RecordingFile(flush_fails, close_fails)
    namespace["_run_log_state"] = {"file": file}
    returned = object()
    escaped = None
    try:
        try:
            returned = close_run_log()
        except Exception as exc:
            escaped = (type(exc).__name__, str(exc))
        return {
            "sequence": file.calls,
            "flush_count": file.calls.count("flush"),
            "close_count": file.calls.count("close"),
            "state_cleared": namespace["_run_log_state"] is None,
            "returned_none": returned is None,
            "escaped": escaped,
        }
    finally:
        namespace["_run_log_state"] = original_state


_EXPECTED = {
    "sequence": ["flush", "close"],
    "flush_count": 1,
    "close_count": 1,
    "state_cleared": True,
    "returned_none": True,
    "escaped": None,
}


def _assert_field(case, observed, field):
    case.assertEqual(observed[field], _EXPECTED[field], field)


def _mutated_close(case, mutation):
    source = inspect.getsource(log_utils._close_run_log)
    tree = ast.parse(source)
    original = ast.dump(tree)
    case.assertEqual(len(tree.body), 1)
    function = tree.body[0]
    case.assertIsInstance(function, ast.FunctionDef)
    case.assertEqual(function.name, "_close_run_log")
    tries = [node for node in function.body if isinstance(node, ast.Try)]
    case.assertEqual(len(tries), 2, "exactly two explicit cleanup try blocks")
    flush, close = tries
    case.assertEqual([ast.unparse(n) for n in flush.body], ["state['file'].flush()"])
    case.assertEqual([ast.unparse(n) for n in close.body], ["state['file'].close()"])
    clears = [node for node in function.body
              if isinstance(node, ast.Assign)
              and ast.unparse(node) == "_run_log_state = None"]
    case.assertEqual(len(clears), 1, "unique state reset")
    if mutation == "shared_try":
        flush.body.extend(close.body)
        function.body.remove(close)
    elif mutation == "conditional_state_reset":
        function.body.remove(clears[0])
        flush.body.append(clears[0])
    else:
        raise AssertionError("unknown mutation")
    ast.fix_missing_locations(tree)
    case.assertNotEqual(ast.dump(tree), original, "mutation must change real AST")
    changed_source = ast.unparse(tree)
    case.assertNotEqual(changed_source, ast.unparse(ast.parse(source)))
    code = compile(tree, "<run-log-close-mutation>", "exec")
    namespace = {"_run_log_state": None}
    exec(code, namespace)
    return namespace["_close_run_log"]


class RunLogCloseTest(unittest.TestCase):
    def _check(self, **faults):
        original_state = log_utils._run_log_state
        observed = _observe(log_utils._close_run_log, **faults)
        self.assertIs(log_utils._run_log_state, original_state)
        for field in _EXPECTED:
            with self.subTest(invariant=field):
                _assert_field(self, observed, field)

    def test_normal_close(self):
        self._check()

    def test_flush_failure_still_attempts_close(self):
        self._check(flush_fails=True)

    def test_flush_and_close_failure_still_attempts_close(self):
        self._check(flush_fails=True, close_fails=True)

    def test_close_failure_keeps_suppression_policy(self):
        self._check(close_fails=True)

    def _check_mutation(self, mutation, expected_failures):
        close_run_log = _mutated_close(self, mutation)
        for close_fails in (False, True):
            with self.subTest(close_fails=close_fails):
                observed = _observe(close_run_log, flush_fails=True, close_fails=close_fails)
                failures = []
                for field in _EXPECTED:
                    try:
                        _assert_field(self, observed, field)
                    except AssertionError:
                        failures.append(field)
                self.assertEqual(failures, expected_failures)

    def test_mutation_shared_try_is_rejected(self):
        self._check_mutation("shared_try", ["sequence", "close_count"])

    def test_mutation_conditional_state_reset_is_rejected(self):
        self._check_mutation("conditional_state_reset", ["state_cleared"])


if __name__ == "__main__":
    unittest.main()
