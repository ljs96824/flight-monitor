"""Offline contracts for rejecting undecodable derived cache bytes."""

import ast
from datetime import datetime
import inspect
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


BYTE_CASES = (
    ("valid", b'{"ok": true}', {"ok": True}),
    ("empty_object", b'{}', {}),
    ("truncated", b'{"ok":', None),
    ("invalid_utf8", b'\xff', None),
    ("bom", b'\xef\xbb\xbf{}', None),
    ("list", b'[]', None),
    ("null", b'null', None),
    ("string", b'"text"', None),
    ("number", b'42', None),
)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 9, 21, 12)
        return value if tz is None else value.replace(tzinfo=tz)


class PersistentCacheDecodeTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"NO_LIVE_API": "1", "PYTHON_DOTENV_DISABLED": "1"}, clear=True))
        self.enterContext(patch.dict(sys.modules, {"dotenv": SimpleNamespace(
            load_dotenv=lambda *a, **k: False, dotenv_values=lambda *a, **k: {})}))
        self.network = Mock(side_effect=AssertionError("EXTERNAL_NETWORK_FORBIDDEN"))
        self.enterContext(patch.object(socket.socket, "connect", self.network))
        self.enterContext(patch.object(socket.socket, "connect_ex", self.network))
        self.addCleanup(self.network.assert_not_called)
        import request_cache
        self.cache = request_cache
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        request_cache.reset_for_tests(self.root / "cache")
        self.addCleanup(request_cache.reset_for_tests, None)
        self.enterContext(patch.object(request_cache, "datetime", FixedDateTime))
        for name in ("safe_log", "_record_observations_after_fetch", "_archive_listing_result", "_persist_api_usage_attempt"):
            self.enterContext(patch.object(request_cache, name))
        self.snapshot = self.enterContext(patch.object(request_cache, "_panel_snapshot_for_key", return_value=None))
        self.source = SimpleNamespace(name="juhe", fetch=Mock(return_value={"source": "juhe", "flights": []}))
        self.key = request_cache.cache_key(self.source, "PVG", "KIX", "2026-10-01")
        self.path = request_cache._cache_path(self.key)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def assert_payload(self, raw, expected, function=None):
        self.path.write_bytes(raw)
        try:
            try:
                actual = (function or self.cache._read_persistent_payload)(self.key)
            except UnicodeDecodeError:
                self.fail("DECODE_MISS: UnicodeDecodeError escaped")
            self.assertEqual(actual, expected, "PAYLOAD_REJECTED")
        finally:
            self.assertEqual(self.path.read_bytes(), raw, "CACHE_BYTES_UNCHANGED")

    def fetch(self, **kwargs):
        return self.cache.cached_fetch(self.source, "PVG", "KIX", "2026-10-01", include_cache_details=True, **kwargs)

    def test_payload_byte_matrix(self):
        for label, raw, expected in BYTE_CASES:
            with self.subTest(case=label):
                self.assert_payload(raw, expected)

    def test_replacement_would_form_valid_json_but_must_be_rejected(self):
        raw = b'{"note":"\xff"}'
        self.assertEqual(json.loads(raw.decode("utf-8", errors="replace")), {"note": "\ufffd"})
        self.assert_payload(raw, None)

    def test_missing_cache_is_not_created(self):
        self.assertIsNone(self.cache._read_persistent_payload(self.key))
        self.assertFalse(self.path.exists())

    def assert_unexpected_error(self, function=None):
        self.path.write_bytes(b'{}')
        failure = RuntimeError("UNEXPECTED_READ_FAILURE")
        with patch.object(Path, "read_text", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "UNEXPECTED_READ_FAILURE") as caught:
                (function or self.cache._read_persistent_payload)(self.key)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.path.read_bytes(), b'{}')

    def test_unexpected_read_error_still_propagates(self):
        self.assert_unexpected_error()

    def test_os_error_remains_a_cache_miss(self):
        self.path.write_bytes(b'{}')
        with patch.object(Path, "read_text", side_effect=PermissionError("SYNTHETIC_DENIAL")):
            self.assertIsNone(self.cache._read_persistent_payload(self.key))
        self.assertEqual(self.path.read_bytes(), b'{}')

    def test_cached_fetch_refreshes_without_altering_bad_bytes(self):
        self.path.write_bytes(b'\xff')
        self.assertIsNone(self.cache._current_stats_round_id)
        self.assertEqual(self.cache._request_cache, {})
        with patch.object(self.cache, "_read_persistent_payload", wraps=self.cache._read_persistent_payload) as reader:
            try:
                result, status, reuse = self.fetch()
            except UnicodeDecodeError:
                self.fail("CACHED_FETCH_DECODE: decoding interrupted refresh")
        self.assertEqual((result["flights"], status, reuse), ([], "fresh", None))
        self.source.fetch.assert_called_once()
        reader.assert_called_once_with(self.key, None)
        self.assertEqual(self.path.read_bytes(), b'\xff')
        self.assertEqual(self.cache.get_request_cache_stats()["actual"], 1)

    def test_bad_panel_detail_does_not_bypass_read_only(self):
        self.path.write_bytes(b'\xff')
        self.snapshot.return_value = {"observed_at": "2026-09-21T12:00:00", "flights": []}
        try:
            result, status, reuse = self.fetch(panel_only=True)
        except UnicodeDecodeError:
            self.fail("PANEL_DECODE: decoding interrupted read-only fallback")
        self.assertEqual((result["source_status"], status, reuse), ("skipped_panel_only", "skipped", None))
        self.source.fetch.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b'\xff')

    def test_source_preflight_still_precedes_cache_read(self):
        self.path.write_bytes(b'\xff')
        self.source.preflight_skip = Mock(return_value={"source": "juhe", "flights": [], "source_status": "synthetic_skip"})
        with patch.object(self.cache, "_read_persistent_payload", side_effect=AssertionError("UNREACHABLE_READ")):
            result, status, _ = self.fetch()
        self.assertEqual((result["source_status"], status), ("synthetic_skip", "skipped"))
        self.source.fetch.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b'\xff')

    def test_refreshed_empty_result_stays_pinned_for_round(self):
        self.path.write_bytes(b'\xff')
        self.cache.start_request_cache_round("synthetic-decode-round")
        try:
            first, _, _ = self.fetch()
        except UnicodeDecodeError:
            self.fail("ROUND_DECODE: decoding prevented round resolution")
        self.assertIn(self.key, self.cache._resolved_request_keys_this_round)
        with patch.object(self.cache, "_read_persistent_payload", side_effect=AssertionError("ROUND_READ_FORBIDDEN")):
            second, _, reuse = self.fetch(force_fresh=True)
        self.assertEqual(first, second)
        self.assertEqual(reuse, "in_round_cache")
        self.source.fetch.assert_called_once()
        self.assertEqual(self.path.read_bytes(), b'\xff')

    def test_monthly_plan_quota_protection_still_skips(self):
        import collection_plan
        self.path.write_bytes(b'\xff')
        plan = collection_plan.CollectionPlan(subscription_count=1)
        plan.add_request(self.source, "PVG", "KIX", "2026-10-01")
        with patch.object(collection_plan, "safe_log"):
            plan.log_summary(quota_budgets={"juhe": {"kind": "monthly", "monthly": 1}},
                             usage_snapshot={"month": {"juhe": 1}})
        self.assertIn(self.key, plan._quota_protected_keys)
        report = plan.execute()
        self.assertEqual(report.actual_requests, 0)
        self.assertEqual(report.outcomes[0].skip_reason_code, "quota")
        self.source.fetch.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b'\xff')

    def mutated(self, kind):
        source = inspect.getsource(self.cache._read_persistent_payload)
        tree = ast.parse(source)
        original = ast.dump(tree)
        handlers = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
        self.assertEqual(len(handlers), 1, "MUTATION_HANDLER_MATCH")
        handler = handlers[0]
        self.assertEqual(ast.unparse(handler.type), "(OSError, json.JSONDecodeError, UnicodeDecodeError)")
        if kind == "decode_removed":
            handler.type.elts.pop()
        elif kind == "replace":
            reads = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute) and ast.unparse(node.func) == "path.read_text"]
            self.assertEqual(len(reads), 1, "MUTATION_READ_MATCH")
            self.assertEqual([(item.arg, ast.literal_eval(item.value)) for item in reads[0].keywords], [("encoding", "utf-8")])
            reads[0].keywords.append(ast.keyword(arg="errors", value=ast.Constant("replace")))
        elif kind == "broad_exception":
            handler.type = ast.Name(id="Exception", ctx=ast.Load())
        else:
            self.fail("UNKNOWN_MUTATION")
        self.assertNotEqual(ast.dump(tree), original, "MUTATION_AST_CHANGED")
        changed = ast.unparse(ast.fix_missing_locations(tree))
        self.assertNotEqual(changed, ast.unparse(ast.parse(source)), "MUTATION_SOURCE_CHANGED")
        code = compile(changed, "<persistent-decode-mutation>", "exec")
        namespace = dict(self.cache.__dict__)
        exec(code, namespace)
        self.mutation_evidence = {"kind": kind, "matches": 1, "changed": True, "compiled": True}
        return namespace["_read_persistent_payload"]

    def test_mutation_decode_catch_removed_is_rejected(self):
        function = self.mutated("decode_removed")
        with self.assertRaisesRegex(AssertionError, "DECODE_MISS"):
            self.assert_payload(b'\xff', None, function)

    def test_mutation_replacement_decode_is_rejected(self):
        function = self.mutated("replace")
        with self.assertRaisesRegex(AssertionError, "PAYLOAD_REJECTED"):
            self.assert_payload(b'{"note":"\xff"}', None, function)

    def test_mutation_catch_all_is_rejected(self):
        function = self.mutated("broad_exception")
        with self.assertRaisesRegex(AssertionError, "RuntimeError not raised"):
            self.assert_unexpected_error(function)


if __name__ == "__main__":
    unittest.main()
