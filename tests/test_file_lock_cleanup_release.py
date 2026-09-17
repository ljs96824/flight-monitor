"""Cleanup errors must not retain the registry's real in-process lock."""

import ast
from contextlib import ExitStack
import inspect
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import local_file_lock as locks


class ClosingFile:
    def __init__(self, stream, error=None):
        self.stream = stream
        self.error = error
        self.close_calls = 0

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def close(self):
        self.close_calls += 1
        if self.error is not None:
            raise self.error
        self.stream.close()


class FileLockCleanupReleaseTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        self.target = self.root / "target.json"
        self.lock_path = self.root / "unresolved-parent" / ".." / "explicit.lock"
        self.assertNotEqual(self.lock_path, self.lock_path.resolve())
        self.thread_lock = locks._thread_lock_for(self.target)
        self.registry_key = str(self.target.resolve())
        self.assertIs(locks._thread_locks[self.registry_key], self.thread_lock)
        self.assertNotEqual(self.registry_key, str(self.lock_path.resolve()))
        self.opened = []
        self.stack.callback(self._clean_synthetic_resources)
        self.backend = locks.FileLockBackend("synthetic", Mock(return_value=True), Mock())
        self.stack.enter_context(patch.object(locks, "LOCK_BACKEND", self.backend))
        self.denials = [self.stack.enter_context(patch(name, side_effect=AssertionError(name)))
                        for name in ("socket.socket.connect", "socket.create_connection",
                                     "socket.socket.sendto", "smtplib.SMTP", "smtplib.SMTP_SSL")]
        self.addCleanup(self._assert_no_network)

    def _assert_no_network(self):
        for mock in self.denials:
            self.assertEqual(mock.call_count, 0)

    def _clean_synthetic_resources(self):
        for wrapper in self.opened:
            wrapper.stream.close()
        if self.thread_lock.locked():
            self.thread_lock.release()
        with locks._thread_locks_guard:
            self.assertIs(locks._thread_locks.pop(self.registry_key), self.thread_lock)

    def _inject_open(self, close_error=None):
        original_open = Path.open

        def open_exact(path, *args, **kwargs):
            # Production passes this unresolved path to open; do not resolve it.
            self.assertEqual(path, self.lock_path, "injection_path")
            self.assertEqual(args, ("a+b",), "injection_mode")
            stream = original_open(path, *args, **kwargs)
            wrapper = ClosingFile(stream, close_error if not self.opened else None)
            self.opened.append(wrapper)
            return wrapper

        return patch.object(Path, "open", autospec=True, side_effect=open_exact)

    def _observe_exit(self, function, kind):
        close_error = OSError("synthetic close failure") if kind == "close" else None
        business_error = ValueError("synthetic business failure") if kind == "business" else None
        unlock_error = (RuntimeError("synthetic unlock failure") if kind == "unlock_non_oserror"
                        else OSError("synthetic unlock os failure") if kind == "unlock_oserror" else None)
        self.backend.unlock.side_effect = [unlock_error, None]
        first_error = None
        second_error = None
        first_entered = False
        second_entered = False
        with self._inject_open(close_error) as opener:
            try:
                with function(self.target, lock_path=self.lock_path, timeout=0):
                    first_entered = True
                    if business_error is not None:
                        raise business_error
            except Exception as exc:
                first_error = exc
            registered_locked = locks._thread_locks[self.registry_key].locked()
            try:
                with function(self.target, lock_path=self.lock_path, timeout=0):
                    second_entered = True
            except Exception as exc:
                second_error = exc
            open_calls = opener.call_count
        return dict(kind=kind, first_error=first_error, second_error=second_error,
                    first_entered=first_entered, second_entered=second_entered,
                    registered_locked=registered_locked, open_calls=open_calls,
                    expected_error=close_error or business_error or
                    (unlock_error if kind == "unlock_non_oserror" else None))

    def _assert_exit(self, function, kind):
        observed = self._observe_exit(function, kind)
        self.assertTrue(observed["first_entered"], "first_context_not_entered")
        self.assertIs(observed["first_error"], observed["expected_error"], "original_error_not_preserved")
        self.assertFalse(observed["registered_locked"], "registry_lock_retained")
        self.assertIsNone(observed["second_error"], "second_acquire_failed")
        self.assertTrue(observed["second_entered"], "second_context_not_entered")
        self.assertEqual(observed["open_calls"], 2, "injection_not_hit_twice")
        self.assertFalse(self.thread_lock.locked())
        self.assertEqual(self.backend.try_lock.call_count, 2)
        self.assertEqual(self.backend.unlock.call_count, 2)
        self.assertIs(self.backend.try_lock.call_args_list[0].args[0], self.opened[0])
        self.assertIs(self.backend.unlock.call_args_list[0].args[0], self.opened[0])
        self.assertEqual(self.opened[0].close_calls, int(kind != "unlock_non_oserror"))
        self.assertEqual(self.opened[1].close_calls, 1)
        if kind in {"close", "unlock_non_oserror"}:
            self.assertFalse(self.opened[0].stream.closed, "do_not_claim_resource_closed")

    def test_normal_exit_releases_lock(self):
        self._assert_exit(locks.file_lock, "normal")

    def test_business_error_releases_lock(self):
        self._assert_exit(locks.file_lock, "business")

    def test_close_error_releases_lock_and_propagates(self):
        self._assert_exit(locks.file_lock, "close")

    def test_non_oserror_unlock_releases_lock_and_propagates(self):
        self._assert_exit(locks.file_lock, "unlock_non_oserror")

    def test_oserror_unlock_is_still_ignored(self):
        self._assert_exit(locks.file_lock, "unlock_oserror")

    def _assert_acquire_failure(self, function):
        # This test owns the existing lock; the attempted context is a second user.
        self.assertTrue(self.thread_lock.acquire(blocking=False))
        try:
            with self._inject_open() as opener:
                with self.assertRaises(locks.FileLockTimeout):
                    with function(self.target, lock_path=self.lock_path, timeout=0):
                        self.fail("busy_context_entered")
                self.assertEqual(opener.call_count, 0, "acquire_failure_opened_file")
            self.backend.try_lock.assert_not_called()
            self.backend.unlock.assert_not_called()
            self.assertEqual(self.opened, [])
            self.assertTrue(self.thread_lock.locked(), "holder_lock_released")
        finally:
            if self.thread_lock.locked():
                self.thread_lock.release()

    def test_acquire_failure_does_not_enter_cleanup(self):
        self._assert_acquire_failure(locks.file_lock)

    def _mutated_lock(self, kind):
        tree = ast.parse(inspect.getsource(locks.file_lock))
        function = tree.body[0]
        original = ast.unparse(tree)
        outer = [node for node in function.body if isinstance(node, ast.Try) and node.finalbody]
        self.assertEqual(len(outer), 1, "mutation_outer_match")
        outer = outer[0]
        self.assertEqual(len(outer.finalbody), 1, "mutation_guard_match")
        guard = outer.finalbody[0]
        self.assertIsInstance(guard, ast.Try)
        self.assertEqual([ast.unparse(n) for n in guard.finalbody], ["thread_lock.release()"])
        if kind == "old_order":
            outer.finalbody = guard.body + guard.finalbody
        elif kind == "swallow_close":
            closes = [node for node in ast.walk(guard) if isinstance(node, ast.If)
                      and ast.unparse(node.test) == "lock_file is not None"]
            self.assertEqual(len(closes), 1, "mutation_file_guard_match")
            block = closes[0].body
            matches = [i for i, node in enumerate(block) if ast.unparse(node) == "lock_file.close()"]
            self.assertEqual(len(matches), 1, "mutation_close_match")
            replacement = ast.parse("try:\n    lock_file.close()\nexcept OSError:\n    pass").body[0]
            block[matches[0]] = replacement
        elif kind == "release_without_acquire":
            indices = [i for i, node in enumerate(function.body) if isinstance(node, ast.If)
                       and ast.unparse(node.test) == "not thread_lock.acquire(timeout=wait_seconds)"]
            self.assertEqual(len(indices), 1, "mutation_acquire_match")
            outer.finalbody = guard.body
            start = indices[0]
            wrapper = ast.Try(body=function.body[start:], handlers=[], orelse=[], finalbody=guard.finalbody)
            function.body[start:] = [wrapper]
        else:
            self.fail("unknown_mutation")
        self.assertNotEqual(ast.unparse(tree), original, "mutation_source_unchanged")
        code = compile(ast.fix_missing_locations(tree), f"<file-lock-{kind}>", "exec")
        namespace = dict(vars(locks))
        exec(code, namespace)
        self.assertTrue(callable(namespace["file_lock"]))
        return namespace["file_lock"]

    def test_mutation_original_cleanup_order_is_rejected(self):
        mutant = self._mutated_lock("old_order")
        with self.assertRaisesRegex(AssertionError, "registry_lock_retained"):
            self._assert_exit(mutant, "close")

    def test_mutation_swallowed_close_error_is_rejected(self):
        mutant = self._mutated_lock("swallow_close")
        with self.assertRaisesRegex(AssertionError, "original_error_not_preserved"):
            self._assert_exit(mutant, "close")

    def test_mutation_cleanup_after_failed_acquire_is_rejected(self):
        mutant = self._mutated_lock("release_without_acquire")
        with self.assertRaisesRegex(AssertionError, "holder_lock_released"):
            self._assert_acquire_failure(mutant)


if __name__ == "__main__":
    unittest.main()
