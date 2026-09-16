"""Offline freshness contracts with an explicit, host-independent local zone."""

import ast
from datetime import datetime, timedelta, timezone
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import request_cache as cache


NOW_UTC = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
REPRESENTATIONS = {"naive": None, "utc": 0, "positive": 9, "negative": -5}
CALLER_CASES = (
    (0, -1, "naive", "fresh"),
    (8, 30, "utc", "cache"),
    (-5, 61, "utc", "fresh"),
    (0, 30, "naive", "cache"),
)


class ControlledDateTime(datetime):
    local_zone = timezone.utc

    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return NOW_UTC.astimezone(tz)
        return NOW_UTC.astimezone(cls.local_zone).replace(tzinfo=None)

    def astimezone(self, tz=None):
        # Only the default local zone is injected; conversion uses datetime.
        return super().astimezone(self.local_zone if tz is None else tz)


def _timestamp(age, representation, local_hours=0):
    instant = NOW_UTC - timedelta(seconds=age)
    hours = REPRESENTATIONS[representation]
    zone = timezone(timedelta(hours=local_hours if hours is None else hours))
    value = instant.astimezone(zone)
    if hours is None:
        value = value.replace(tzinfo=None)
    return value.isoformat()


def _mutated_fresh(kind):
    source = inspect.getsource(cache._fresh)
    tree = ast.parse(source)
    before = ast.dump(tree)
    hits = 0
    for node in ast.walk(tree):
        if (kind == "lower_bound" and isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Call) and ast.unparse(node.left) == "timedelta(0)"
                and len(node.ops) == 2):
            node.left = node.comparators[0]
            node.ops = node.ops[1:]
            node.comparators = node.comparators[1:]
            hits += 1
        elif (kind == "offset" and isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute) and node.func.attr == "replace"
              and isinstance(node.func.value, ast.Call)
              and ast.unparse(node.func.value) == "dt.astimezone()"):
            node.func.value = ast.Name(id="dt", ctx=ast.Load())
            hits += 1
        elif (kind == "ttl" and isinstance(node, ast.If)
              and ast.unparse(node.test) == "ttl_seconds <= 0"):
            if len(node.body) != 1 or not isinstance(node.body[0], ast.Return):
                raise AssertionError("mutation_ttl_shape")
            node.body[0].value = ast.Constant(True)
            hits += 1
    if hits != 1:
        raise AssertionError(f"mutation_match_count:{kind}:{hits}")
    ast.fix_missing_locations(tree)
    if ast.dump(tree) == before:
        raise AssertionError("mutation_unchanged")
    code = compile(tree, "<freshness-mutation>", "exec")
    namespace = dict(cache.__dict__)
    exec(code, namespace)
    return namespace["_fresh"], {"matches": hits, "changed": True, "compiled": True}


class SyntheticSource:
    name = "freshness_synthetic"

    def __init__(self):
        self.calls = 0

    def fetch(self, origin, dest, date_str, cabin_class):
        self.calls += 1
        return {"source": self.name, "flights": [{"flight_combo": "SYNTH1", "price": 200}]}


class CacheFreshnessBoundsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        cache.reset_for_tests(self.root / "cache")
        self.addCleanup(cache.reset_for_tests, None)
        self.enterContext(patch.object(cache, "datetime", ControlledDateTime))
        self.enterContext(patch.object(ControlledDateTime, "local_zone", timezone.utc))
        self.enterContext(patch.object(cache, "safe_log"))
        self.enterContext(patch.object(cache, "_record_observations_after_fetch"))
        self.enterContext(patch.object(cache, "_archive_listing_result"))
        self.enterContext(patch.object(cache, "_persist_api_usage_attempt"))

    def _assert_age(self, age, expected, fresh=None):
        value = _timestamp(age, "naive")
        self.assertIs((fresh or cache._fresh)(value, 60), expected, "age_bound")

    def test_age_bounds(self):
        for age, expected in ((0, True), (30, True), (59, True), (60, False),
                              (61, False), (-1, False), (-86400, False), (-315360000, False)):
            with self.subTest(age=age):
                self._assert_age(age, expected)

    def _assert_representation(self, local, age, representation, fresh=None):
        zone = timezone(timedelta(hours=local))
        with patch.object(ControlledDateTime, "local_zone", zone):
            value = _timestamp(age, representation, local)
            parsed = cache._parse_cache_time(value)
            expected_local = (NOW_UTC - timedelta(seconds=age)).astimezone(zone).replace(tzinfo=None)
            self.assertEqual(parsed, expected_local, "local_projection")
            self.assertIsNone(parsed.tzinfo)
            parsed_age = ControlledDateTime.now() - parsed
            self.assertEqual(parsed_age, timedelta(seconds=age), "parsed_age")
            expected = 0 <= age < 60
            self.assertIs((fresh or cache._fresh)(value, 60), expected, "timezone_freshness")

    def test_timezone_representation_consistency(self):
        for local in (-5, 0, 8):
            for age in (30, 61):
                for representation in REPRESENTATIONS:
                    with self.subTest(local=local, age=age, representation=representation):
                        self._assert_representation(local, age, representation)

    def _assert_disabled_ttl(self, ttl, fresh=None):
        self.assertIs((fresh or cache._fresh)(_timestamp(30, "utc"), ttl), False, "disabled_ttl")

    def test_nonpositive_ttl(self):
        for ttl in (0, -1, -60):
            with self.subTest(ttl=ttl):
                self._assert_disabled_ttl(ttl)

    def test_invalid_timestamps(self):
        for value in (None, "", "not-a-time", "2026-13-01T00:00:00", "2026-03-01T12:00+99:00"):
            with self.subTest(value=value):
                self.assertIs(cache._fresh(value, 60), False)

    def _seed(self, source, value, persistent):
        key = cache.cache_key(source, "PVG", "KIX", "2026-10-01")
        payload = {"fetched_at": value, "key": list(key),
                   "producer_class": cache._source_producer(source),
                   "result": {"source": source.name, "flights": [{"flight_combo": "SYNTH1", "price": 100}]}}
        if persistent:
            path = cache._cache_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        else:
            cache._request_cache[key] = payload
        return key

    def _assert_caller(self, persistent):
        for local, age, representation, expected_status in CALLER_CASES:
            with self.subTest(local=local, age=age, representation=representation):
                cache.reset_for_tests(self.root / "cache")
                source = SyntheticSource()
                with patch.object(ControlledDateTime, "local_zone", timezone(timedelta(hours=local))):
                    self._seed(source, _timestamp(age, representation, local), persistent)
                    result, status = cache.cached_fetch(source, "PVG", "KIX", "2026-10-01",
                                                        ttl_seconds=60, persist=persistent,
                                                        include_cache_status=True)
                self.assertEqual(status, expected_status, "caller_reuse_status")
                self.assertEqual(source.calls, int(expected_status == "fresh"), "synthetic_fetch_count")
                self.assertEqual(result["flights"][0]["price"], 200 if expected_status == "fresh" else 100)

    def test_memory_call_path(self):
        self._assert_caller(False)

    def test_persistent_call_path(self):
        self._assert_caller(True)

    def test_panel_future_rejected(self):
        source = SyntheticSource()
        key = self._seed(source, _timestamp(-1, "naive"), True)
        snapshot = {"observed_at": _timestamp(30, "positive"), "round_id": "synthetic",
                    "rows": [{"flight_combo": "SYNTH1", "price_cny": 150}]}
        self.assertIsNone(cache._rebuild_panel_result(key, snapshot, source=source, freshness_hours=1),
                          "panel_future_reuse")
        self.assertEqual(source.calls, 0)

    def test_mutation_lower_bound_removed_is_rejected(self):
        fresh, evidence = _mutated_fresh("lower_bound")
        self.assertEqual(evidence, {"matches": 1, "changed": True, "compiled": True})
        for age in (-1, -86400, -315360000):
            with self.subTest(age=age), self.assertRaisesRegex(AssertionError, "age_bound"):
                self._assert_age(age, False, fresh)

    def test_mutation_offset_discarded_is_rejected(self):
        fresh, evidence = _mutated_fresh("offset")
        self.assertEqual(evidence, {"matches": 1, "changed": True, "compiled": True})
        for local, representation in ((8, "utc"), (-5, "utc"), (0, "positive"), (0, "negative")):
            with self.subTest(local=local, representation=representation):
                with self.assertRaisesRegex(AssertionError, "timezone_freshness"):
                    self._assert_representation(local, 30, representation, fresh)

    def test_mutation_nonpositive_ttl_enabled_is_rejected(self):
        fresh, evidence = _mutated_fresh("ttl")
        self.assertEqual(evidence, {"matches": 1, "changed": True, "compiled": True})
        for ttl in (0, -60):
            with self.subTest(ttl=ttl), self.assertRaisesRegex(AssertionError, "disabled_ttl"):
                self._assert_disabled_ttl(ttl, fresh)


if __name__ == "__main__":
    unittest.main()
