"""Offline contracts for the attempt writer, not subscription reconstruction."""

import ast
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import atomic_json_store
import subscription_attempts as attempts


SUB_ID = "123e4567-e89b-12d3-a456-426614174011"
OTHER_ID = "123e4567-e89b-12d3-a456-426614174012"
FIXED_NOW = datetime(2026, 9, 16, 0, 0, tzinfo=timezone.utc)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW.astimezone(tz) if tz is not None else FIXED_NOW.replace(tzinfo=None)


def _bytes(payload):
    return json.dumps(payload, indent=1).encode("utf-8") + b"\n"


def _mutation(kind):
    source = inspect.getsource(attempts.record_subscription_attempt)
    tree = ast.parse(source)
    original = ast.dump(tree)
    function = tree.body[0]
    mutate = next(node for node in function.body if isinstance(node, ast.FunctionDef)
                  and node.name == "mutate")
    hits = 0
    if kind == "none_to_empty":
        for node in mutate.body:
            if isinstance(node, ast.If) and ast.unparse(node.test) == "payload is None":
                node.body = ast.parse("payload = []").body
                hits += 1
    elif kind == "remove_identity_validation":
        for node in list(mutate.body):
            if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "_validated_id_index"):
                mutate.body.remove(node)
                hits += 1
    elif kind == "validation_outside_lock":
        for node in list(mutate.body):
            if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "_validated_id_index"):
                mutate.body.remove(node)
                hits += 1
        # Model a check-before-use bug using a real unlocked file read.
        function.body.insert(function.body.index(mutate), ast.parse(
            "_validated_id_index(__import__('atomic_json_store').read_json(path))"
        ).body[0])
    else:
        raise AssertionError("unknown_mutation")
    if hits != 1:
        raise AssertionError(f"mutation_match_count:{kind}:{hits}")
    ast.fix_missing_locations(tree)
    if ast.dump(tree) == original:
        raise AssertionError("mutation_source_unchanged")
    changed = ast.unparse(tree)
    code = compile(tree, "<attempt-write-guard-mutation>", "exec")
    namespace = dict(attempts.__dict__)
    exec(code, namespace)
    return namespace[function.name], {"matches": hits, "changed": changed != source,
                                       "compiled": True}


class SubscriptionAttemptWriteGuardsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "subscriptions.json"
        self.logs = []
        self.clock = patch.object(attempts, "datetime", FixedDateTime)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def _record(self, writer=None, **overrides):
        values = dict(status="success", holder_round_id="synthetic-round",
                      entrypoint="batch", at=FIXED_NOW.isoformat(),
                      path=self.path, logger=self.logs.append)
        values.update(overrides)
        return (writer or attempts.record_subscription_attempt)(SUB_ID, **values)

    def _assert_rejected(self, raw, reason, writer=None):
        self.path.unlink(missing_ok=True)
        if raw is not None:
            self.path.write_bytes(raw)
        before = self.path.read_bytes() if self.path.exists() else None
        self.logs.clear()
        result = self._record(writer)
        after = self.path.read_bytes() if self.path.exists() else None
        self.assertIs(result, False, "rejection_return")
        self.assertEqual(after, before, "rejection_bytes")
        self.assertTrue(any(reason in message for message in self.logs), "rejection_reason")

    def test_missing_file_is_not_created(self):
        self._assert_rejected(None, "订阅文件缺失或为 JSON null")

    def test_json_null_is_not_rewritten(self):
        self._assert_rejected(b" \n null\t\n", "订阅文件缺失或为 JSON null")

    def test_duplicate_persisted_id_is_not_written(self):
        for records in (
            [{"subscription_id": SUB_ID}, {"subscription_id": " " + SUB_ID + " "}],
            [{"subscription_id": SUB_ID}, {"subscription_id": OTHER_ID},
             {"subscription_id": OTHER_ID}],
        ):
            with self.subTest(records=len(records)):
                self._assert_rejected(_bytes(records), "DuplicateSubscriptionIdError")

    def test_missing_persisted_identity_is_not_repaired(self):
        self._assert_rejected(_bytes([{"id": SUB_ID}]), "SubscriptionIdentityMigrationRequired")

    def test_unique_identity_records_attempt(self):
        self.path.write_bytes(_bytes([{"subscription_id": SUB_ID, "enabled": True}]))
        self.assertIs(self._record(), True)
        row = json.loads(self.path.read_bytes())[0]
        self.assertEqual(row, {"subscription_id": SUB_ID, "enabled": True,
                              "last_attempt": {"status": "success", "holder_round_id": "synthetic-round",
                                               "entrypoint": "batch", "at": "2026-09-16T00:00:00.000000+00:00"}})
        self.assertEqual(self.logs, [])

    def test_non_array_keeps_value_error_without_writing(self):
        for payload in ({}, "invalid", 7, False):
            with self.subTest(payload=payload):
                raw = _bytes(payload)
                self.path.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, "应为订阅数组"):
                    self._record()
                self.assertEqual(self.path.read_bytes(), raw)

    def test_missing_subscription_entry_is_not_recreated(self):
        raw = _bytes([{"subscription_id": OTHER_ID}])
        self.path.write_bytes(raw)
        self.assertIs(self._record(), False)
        self.assertEqual(self.path.read_bytes(), raw)

    def test_older_state_does_not_overwrite_newer(self):
        raw = _bytes([{"subscription_id": SUB_ID, "last_attempt": {
            "status": "success", "at": FIXED_NOW.isoformat()}}])
        self.path.write_bytes(raw)
        self.assertIs(self._record(at=(FIXED_NOW - timedelta(minutes=1)).isoformat()), False)
        self.assertEqual(self.path.read_bytes(), raw)

    def test_anomalous_future_recovery_boundary_is_unchanged(self):
        for seconds, recover in ((300, False), (301, True)):
            with self.subTest(seconds=seconds):
                raw = _bytes([{"subscription_id": SUB_ID, "last_attempt": {
                    "status": "busy", "at": (FIXED_NOW + timedelta(seconds=seconds)).isoformat()}}])
                self.path.write_bytes(raw)
                self.assertIs(self._record(), recover)
                if recover:
                    self.assertEqual(json.loads(self.path.read_bytes())[0]["last_attempt"]["status"], "success")
                else:
                    self.assertEqual(self.path.read_bytes(), raw)

    def _assert_locked_recheck(self, scenario, writer=None):
        initial = _bytes([{"subscription_id": SUB_ID}])
        replacement = {
            "missing": None,
            "null": b"null\n",
            "duplicate": _bytes([{"subscription_id": SUB_ID}, {"subscription_id": SUB_ID}]),
        }[scenario]
        self.path.write_bytes(initial)
        real_lock = atomic_json_store.file_lock
        events = []

        @contextmanager
        def change_on_lock(target):
            events.append("lock_requested")
            # The competing update finishes after the public call starts,
            # but before its real locked read. No timing/scheduling assumption.
            with real_lock(target):
                if replacement is None:
                    self.path.unlink()
                else:
                    self.path.write_bytes(replacement)
            with real_lock(target):
                events.append("lock_acquired")
                try:
                    yield
                finally:
                    events.append("lock_released")

        self.logs.clear()
        with patch.object(atomic_json_store, "file_lock", change_on_lock):
            result = self._record(writer)
        after = self.path.read_bytes() if self.path.exists() else None
        self.assertEqual(events, ["lock_requested", "lock_acquired", "lock_released"])
        self.assertIs(result, False, "locked_recheck_return")
        self.assertEqual(after, replacement, "locked_recheck_bytes")
        self.assertTrue(self.logs, "locked_recheck_reason")

    def test_state_changes_before_locked_read_are_rejected(self):
        for scenario in ("missing", "null", "duplicate"):
            with self.subTest(scenario=scenario):
                self._assert_locked_recheck(scenario)

    def test_mutation_none_to_empty_is_rejected(self):
        writer, evidence = _mutation("none_to_empty")
        self.assertEqual(evidence, {"matches": 1, "changed": True, "compiled": True})
        with self.assertRaisesRegex(AssertionError, "rejection_bytes"):
            self._assert_rejected(None, "订阅文件缺失或为 JSON null", writer)

    def test_mutation_duplicate_validation_removed_is_rejected(self):
        writer, evidence = _mutation("remove_identity_validation")
        self.assertEqual(evidence, {"matches": 1, "changed": True, "compiled": True})
        with self.assertRaisesRegex(AssertionError, "rejection_return"):
            self._assert_rejected(_bytes([{"subscription_id": SUB_ID}] * 2),
                                  "DuplicateSubscriptionIdError", writer)

    def test_mutation_validation_outside_lock_is_rejected(self):
        writer, evidence = _mutation("validation_outside_lock")
        self.assertEqual(evidence, {"matches": 1, "changed": True, "compiled": True})
        with self.assertRaisesRegex(AssertionError, "locked_recheck_return"):
            self._assert_locked_recheck("duplicate", writer)


if __name__ == "__main__":
    unittest.main()
