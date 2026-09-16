"""Offline contracts for disk-to-memory age preservation, not round policy."""

import ast
from datetime import datetime, timedelta, timezone
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import request_cache as cache


FETCHED = datetime(2026, 3, 1, 12)
TTL = 60


class ControlledDateTime(datetime):
    current = FETCHED

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.current
        return cls.current.replace(tzinfo=timezone.utc).astimezone(tz)

    def astimezone(self, tz=None):
        return super().astimezone(timezone.utc if tz is None else tz)


class SyntheticSource:
    name = "age_preservation_synthetic"

    def __init__(self):
        self.calls = 0

    def fetch(self, origin, dest, date_str, cabin_class):
        self.calls += 1
        return {"source": self.name, "flights": [{"flight_combo": "SYNTH1", "price": 200}]}


def _mutated_function(kind):
    original = cache._read_persistent if kind == "freshness_guard" else cache.cached_fetch
    tree = ast.parse(inspect.getsource(original))
    before = ast.dump(tree)
    hits = 0
    if kind == "freshness_guard":
        function = tree.body[0]
        for node in list(function.body):
            if (isinstance(node, ast.If)
                    and ast.unparse(node.test) == "not _fresh(payload.get('fetched_at'), ttl_seconds)"):
                function.body.remove(node)
                hits += 1
    else:
        for node in ast.walk(tree):
            if not isinstance(node, ast.If) or ast.unparse(node.test) != "persisted is not None":
                continue
            for statement in node.body:
                if (not isinstance(statement, ast.Assign)
                        or ast.unparse(statement.targets[0]) != "_request_cache[key]"
                        or not isinstance(statement.value, ast.Dict)):
                    continue
                for index, key in enumerate(statement.value.keys):
                    if isinstance(key, ast.Constant) and key.value == "fetched_at":
                        if ast.unparse(statement.value.values[index]) != "fetched_at":
                            raise AssertionError("mutation_promotion_shape")
                        replacement = (
                            ast.parse('datetime.now().isoformat(timespec="seconds")', mode="eval").body
                            if kind == "promotion_now" else ast.Constant("2000-01-01T00:00:00")
                        )
                        statement.value.values[index] = replacement
                        hits += 1
    if hits != 1:
        raise AssertionError(f"mutation_match_count:{kind}:{hits}")
    ast.fix_missing_locations(tree)
    if ast.dump(tree) == before:
        raise AssertionError("mutation_unchanged")
    code = compile(tree, "<persistent-age-mutation>", "exec")
    namespace = dict(cache.__dict__)
    exec(code, namespace)
    return namespace[original.__name__], {"matches": hits, "changed": True, "compiled": True}


class PersistentCacheAgePreservationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        cache.reset_for_tests(self.root / "cache")
        self.addCleanup(cache.reset_for_tests, None)
        self.enterContext(patch.object(cache, "datetime", ControlledDateTime))
        self.enterContext(patch.object(ControlledDateTime, "current", FETCHED + timedelta(seconds=59)))
        for name in ("safe_log", "_record_observations_after_fetch", "_archive_listing_result",
                     "_persist_api_usage_attempt"):
            self.enterContext(patch.object(cache, name))
        self.source = SyntheticSource()
        self.key = cache.cache_key(self.source, "PVG", "KIX", "2026-10-01")
        self.path = cache._cache_path(self.key)

    def _seed(self, *, timestamp=None, producer=None, result=None):
        original = FETCHED.isoformat(timespec="seconds") if timestamp is None else timestamp
        payload = {
            "fetched_at": original,
            "key": list(self.key),
            "producer_class": cache._source_producer(self.source) if producer is None else producer,
            "result": result if result is not None else {
                "source": self.source.name, "flights": [{"flight_combo": "SYNTH1", "price": 100}]},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        return original

    def _fetch(self, function=None, **kwargs):
        return (function or cache.cached_fetch)(
            self.source, "PVG", "KIX", "2026-10-01", ttl_seconds=TTL,
            include_cache_details=True, **kwargs)

    def _assert_original(self, function=None, timestamp=None):
        original = self._seed(timestamp=timestamp)
        disk_before = self.path.read_bytes()
        self.assertIsNone(cache._current_stats_round_id)
        result, status, reuse = self._fetch(function)
        self.assertEqual((status, reuse, self.source.calls), ("cache", "persistent_cache", 0))
        self.assertEqual(result["flights"][0]["price"], 100)
        self.assertEqual(cache._request_cache[self.key]["fetched_at"], original, "disk_original")
        self.assertEqual(self.path.read_bytes(), disk_before)

    def test_promotion_preserves_disk_timestamp(self):
        for timestamp in ("2026-03-01T12:00:00", "2026-03-01T20:00:00+08:00"):
            with self.subTest(timestamp=timestamp):
                cache._request_cache.clear()
                self._assert_original(timestamp=timestamp)

    def _assert_boundary(self, function=None):
        self._seed()
        self.assertIsNone(cache._current_stats_round_id)
        _, status, _ = self._fetch(function)
        self.assertEqual(status, "cache")
        self.assertEqual(self.source.calls, 0)
        ControlledDateTime.current = FETCHED + timedelta(seconds=TTL)
        result, status, _ = self._fetch(function)
        self.assertEqual(status, "fresh", "post_promotion_ttl")
        self.assertEqual(self.source.calls, 1)
        self.assertEqual(result["flights"][0]["price"], 200)

    def test_promoted_memory_expires_at_original_deadline(self):
        self._assert_boundary()

    def test_repeated_promotions_keep_one_age(self):
        original = self._seed()
        before = self.path.read_bytes()
        promoted = []
        for age in (30, 59):
            cache._request_cache.clear()
            ControlledDateTime.current = FETCHED + timedelta(seconds=age)
            _, status, _ = self._fetch()
            self.assertEqual(status, "cache")
            promoted.append(cache._request_cache[self.key]["fetched_at"])
        self.assertEqual(promoted, [original, original], "repeated_original")
        self.assertEqual(self.source.calls, 0)
        self.assertEqual(self.path.read_bytes(), before)

    def _assert_stale_disk(self):
        self._seed()
        ControlledDateTime.current = FETCHED + timedelta(seconds=TTL)
        _, status, _ = self._fetch()
        self.assertEqual(status, "fresh", "stale_disk_not_promoted")
        self.assertEqual(self.source.calls, 1)

    def test_stale_disk_is_not_promoted(self):
        self._assert_stale_disk()

    def test_mismatched_source_is_not_promoted(self):
        self._seed(producer="other.synthetic.Implementation")
        _, status, _ = self._fetch()
        self.assertEqual((status, self.source.calls), ("fresh", 1))

    def test_nonpersistent_results_not_promoted(self):
        for result in ({"flights": []}, {"flights": [{"price": 100}], "source_status": "failed"}):
            with self.subTest(result=result):
                cache._request_cache.clear()
                self._seed(result=result)
                calls_before = self.source.calls
                _, status, _ = self._fetch()
                self.assertEqual((status, self.source.calls - calls_before), ("fresh", 1))

    def test_invalid_payload_is_not_promoted(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for serialized in ("null", "[]", "{invalid-json"):
            with self.subTest(serialized=serialized):
                cache._request_cache.clear()
                self.path.write_text(serialized, encoding="utf-8")
                calls_before = self.source.calls
                _, status, _ = self._fetch()
                self.assertEqual((status, self.source.calls - calls_before), ("fresh", 1))

    def test_force_fresh_bypasses_caches(self):
        original = self._seed()
        cache._request_cache[self.key] = {"fetched_at": original, "result": {"flights": [{"price": 100}]}}
        with patch.object(cache, "_read_persistent", side_effect=AssertionError("disk_read_forbidden")):
            _, status, _ = self._fetch(force_fresh=True)
        self.assertEqual((status, self.source.calls), ("fresh", 1))

    def test_round_pins_result_then_next_round_rechecks_age(self):
        self._seed()
        cache.start_request_cache_round("synthetic-round-one")
        _, status, reuse = self._fetch()
        self.assertEqual((status, reuse), ("cache", "persistent_cache"))
        self.assertIn(self.key, cache._resolved_request_keys_this_round)
        ControlledDateTime.current = FETCHED + timedelta(seconds=61)
        with patch.object(cache, "_fresh", side_effect=AssertionError("same_round_ttl_forbidden")):
            _, status, reuse = self._fetch(force_fresh=True)
        self.assertEqual((status, reuse, self.source.calls), ("cache", "in_round_cache", 0))
        cache.start_request_cache_round("synthetic-round-two")
        self.assertNotIn(self.key, cache._resolved_request_keys_this_round)
        _, status, _ = self._fetch()
        self.assertEqual(status, "fresh", "next_round_original_age")
        self.assertEqual(self.source.calls, 1)

    def test_memory_hit_keeps_timestamp(self):
        original = self._seed()
        cache._request_cache[self.key] = {"fetched_at": original, "result": {"flights": [{"price": 100}]}}
        with patch.object(cache, "_read_persistent", side_effect=AssertionError("disk_read_forbidden")):
            _, status, _ = self._fetch()
        self.assertEqual((status, self.source.calls), ("cache", 0))
        self.assertEqual(cache._request_cache[self.key]["fetched_at"], original)

    def test_panel_only_skip_uses_current_time(self):
        _, status, _ = self._fetch(panel_only=True)
        self.assertEqual((status, self.source.calls), ("skipped", 0))
        self.assertEqual(cache._request_cache[self.key]["fetched_at"], "2026-03-01T12:00:59")
        self.assertFalse(self.path.exists())

    def test_real_fetch_result_uses_current_time(self):
        self._seed()
        with patch.object(cache, "_read_persistent", side_effect=AssertionError("disk_read_forbidden")):
            _, status, _ = self._fetch(persist=False)
        self.assertEqual((status, self.source.calls), ("fresh", 1))
        self.assertEqual(cache._request_cache[self.key]["fetched_at"], "2026-03-01T12:00:59")

    def test_read_validation_order(self):
        for case, expected in (
            ("invalid", ["payload"]),
            ("stale", ["payload", "fresh"]),
            ("producer", ["payload", "fresh", "producer"]),
            ("status", ["payload", "fresh", "producer", "status"]),
            ("valid", ["payload", "fresh", "producer", "status"]),
        ):
            with self.subTest(case=case):
                self._seed(producer="wrong" if case == "producer" else None,
                           result={"flights": []} if case == "status" else None)
                if case == "invalid":
                    self.path.write_text("[]", encoding="utf-8")
                ControlledDateTime.current = FETCHED + timedelta(seconds=TTL if case == "stale" else 59)
                calls = []

                def recording(name, function):
                    def invoke(*args, **kwargs):
                        calls.append(name)
                        return function(*args, **kwargs)
                    return invoke

                with (
                    patch.object(cache, "_read_persistent_payload", side_effect=recording("payload", cache._read_persistent_payload)),
                    patch.object(cache, "_fresh", side_effect=recording("fresh", cache._fresh)),
                    patch.object(cache, "_persistent_payload_matches_source", side_effect=recording("producer", cache._persistent_payload_matches_source)),
                    patch.object(cache, "_result_cache_status", side_effect=recording("status", cache._result_cache_status)),
                ):
                    result = cache._read_persistent(self.key, TTL, source=self.source)
                self.assertEqual(calls, expected)
                self.assertEqual(result is not None, case == "valid")

    def test_mutation_promotion_now_is_rejected(self):
        function, evidence = _mutated_function("promotion_now")
        self.assertEqual(evidence, {"matches": 1, "changed": True, "compiled": True})
        with self.assertRaisesRegex(AssertionError, "post_promotion_ttl"):
            self._assert_boundary(function)

    def test_mutation_freshness_guard_removed_is_rejected(self):
        function, evidence = _mutated_function("freshness_guard")
        self.assertEqual(evidence, {"matches": 1, "changed": True, "compiled": True})
        with patch.object(cache, "_read_persistent", function):
            with self.assertRaisesRegex(AssertionError, "stale_disk_not_promoted"):
                self._assert_stale_disk()

    def test_mutation_constant_past_time_is_rejected(self):
        function, evidence = _mutated_function("constant_past")
        self.assertEqual(evidence, {"matches": 1, "changed": True, "compiled": True})
        with self.assertRaisesRegex(AssertionError, "disk_original"):
            self._assert_original(function)


if __name__ == "__main__":
    unittest.main()
