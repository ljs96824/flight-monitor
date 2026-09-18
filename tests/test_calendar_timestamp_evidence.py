import ast
import copy
import inspect
import json
import socket
import tempfile
import textwrap
import unittest
from contextlib import ExitStack
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from project_time import SHANGHAI_TZ


INVALID_DEADLINES = ("not-a-time", "", "   ", 0, False)
ANOMALY_TEXT = "\u65f6\u95f4\u8bc1\u636e\u5f02\u5e38\uff0c\u5f53\u524d\u4e0d\u53ef\u7528\u4e8e\u63a8\u8350"
EXPIRED_TEXT = "\u5386\u53f2\u53c2\u8003(\u5df2\u8fc7\u671f)"


def _mutated(function, kind):
    source = textwrap.dedent(inspect.getsource(function))
    tree = ast.parse(source)
    before = ast.dump(tree)
    hits = 0

    class Mutation(ast.NodeTransformer):
        def visit_Assign(self, node):
            nonlocal hits
            if (kind in {"skip_invalid", "reject_legacy"}
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "explicit_invalid"):
                hits += 1
                if kind == "skip_invalid":
                    node.value = ast.Constant(False)
                else:
                    node.value = ast.BoolOp(ast.Or(), [
                        ast.Compare(ast.Name("raw_deadline", ast.Load()),
                                    [ast.Is()], [ast.Constant(None)]), node.value])
            return self.generic_visit(node)

        def visit_Compare(self, node):
            nonlocal hits
            if (kind in {"skip_future", "reject_future_deadline"}
                    and isinstance(node.left, ast.Name)
                    and node.left.id == "success_time"
                    and len(node.ops) == 1 and isinstance(node.ops[0], ast.Gt)):
                hits += 1
                if kind == "skip_future":
                    return ast.Constant(False)
                node.left = ast.Name("deadline", ast.Load())
            return self.generic_visit(node)

        def visit_Constant(self, node):
            nonlocal hits
            if kind == "wrong_text" and node.value == ANOMALY_TEXT:
                hits += 1
                return ast.Constant(EXPIRED_TEXT)
            return node

    tree = Mutation().visit(tree)
    if hits != 1:
        raise RuntimeError(f"MUTATION_MATCH_COUNT:{kind}:{hits}")
    ast.fix_missing_locations(tree)
    if ast.dump(tree) == before:
        raise RuntimeError("MUTATION_NO_CHANGE")
    namespace = dict(function.__globals__)
    exec(compile(tree, "<calendar-evidence-mutation>", "exec"), namespace)
    return namespace[function.__name__]


class CalendarTimestampEvidenceTest(unittest.TestCase):
    NOW = datetime(2026, 9, 17, 12, tzinfo=SHANGHAI_TZ)
    DAY = date(2026, 9, 20)

    def setUp(self):
        import price_calendar

        self.calendar = price_calendar
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        self.stack.enter_context(patch.object(price_calendar, "_shanghai_now", return_value=self.NOW))
        self.stack.enter_context(patch.object(price_calendar, "shanghai_today", return_value=self.NOW.date()))
        self.network = self.stack.enter_context(patch.object(
            socket.socket, "connect", side_effect=AssertionError("UNEXPECTED_NETWORK")))
        self.fetch = self.stack.enter_context(patch.object(
            price_calendar, "cached_fetch", side_effect=AssertionError("UNEXPECTED_CACHED_FETCH")))

    def tearDown(self):
        self.network.assert_not_called()
        self.fetch.assert_not_called()

    def _record(self, **changes):
        record = {"status": "success", "min_price": 500,
                  "last_success_at": "2026-09-17T11:00:00+08:00",
                  "stale_after": "2026-09-17T17:00:00+08:00"}
        record.update(changes)
        return record

    def _write(self, record):
        path = self.calendar.calendar_path("PVG-KIX", self.root)
        path.write_text(json.dumps({"route": "PVG-KIX", "dates": {
            self.DAY.isoformat(): record}}, ensure_ascii=False), encoding="utf-8")
        return path

    def _assert_anomaly(self, normalize, record, reason):
        original = copy.deepcopy(record)
        result = normalize(record, now=self.NOW)
        self.assertEqual(result["status"], "stale", "time_anomaly_admitted")
        self.assertEqual(result["error_type"], reason, "time_reason_lost")
        self.assertFalse(self.calendar.calendar_record_is_eligible(result, now=self.NOW))
        self.assertEqual(self.calendar._status_text(result), ANOMALY_TEXT)
        self.assertEqual(record, original, "input_mutated")
        for field in ("stale_after", "last_success_at", "updated_at"):
            if field in original:
                self.assertEqual(result[field], original[field], "raw_time_changed")
        return result

    def _assert_fresh(self, normalize, record):
        result = normalize(record, now=self.NOW)
        self.assertEqual(result["status"], "success", "normal_fresh_rejected")
        self.assertTrue(self.calendar.calendar_record_is_eligible(result, now=self.NOW))
        return result

    def test_explicit_invalid_deadlines_are_quarantined(self):
        for value in INVALID_DEADLINES:
            with self.subTest(deadline=repr(value)):
                self.assertIsNone(self.calendar._parse_timestamp(value))
                self._assert_anomaly(self.calendar._normalized_calendar_record,
                                     self._record(stale_after=value, last_success_at="2016-09-17T11:00:00+08:00"),
                                     "CalendarInvalidDeadline")

    def test_future_success_is_quarantined(self):
        for legacy in (False, True):
            for deadline in (None, "2036-09-18T12:00:00+08:00"):
                with self.subTest(legacy=legacy, deadline=deadline):
                    record = self._record(last_success_at="2036-09-17T12:00:00+08:00")
                    if legacy:
                        record["updated_at"] = record.pop("last_success_at")
                    if deadline is None:
                        del record["stale_after"]
                    else:
                        record["stale_after"] = deadline
                    self._assert_anomaly(self.calendar._normalized_calendar_record, record,
                                         "CalendarFutureSuccess")

    def test_normal_fresh_and_equal_success_remain_eligible(self):
        for success in ("2026-09-17T11:00:00+08:00", "2026-09-17T12:00:00+08:00"):
            with self.subTest(success=success):
                self._assert_fresh(self.calendar._normalized_calendar_record, self._record(last_success_at=success))

    def test_equal_deadline_remains_ordinary_expiry(self):
        result = self.calendar._normalized_calendar_record(
            self._record(stale_after=self.NOW.isoformat()), now=self.NOW)
        self.assertEqual(result["status"], "stale")
        self.assertIsNone(result["error_type"])
        self.assertEqual(self.calendar._status_text(result), EXPIRED_TEXT)
        self.assertFalse(self.calendar.calendar_record_is_eligible(result, now=self.NOW))

    def test_offset_representations_agree(self):
        cases = (
            ("2026-09-17T11:00:00+08:00", "2026-09-17T17:00:00+08:00", "success"),
            ("2026-09-17T03:00:00Z", "2026-09-17T09:00:00+00:00", "success"),
            ("2026-09-16T22:00:00-05:00", "2026-09-17T04:00:00-05:00", "success"),
            ("2026-09-17T12:00:01+08:00", "2026-09-17T17:00:00+08:00", "stale"),
            ("2026-09-17T04:00:01Z", "2026-09-17T09:00:00Z", "stale"),
            ("2026-09-16T23:00:01-05:00", "2026-09-17T04:00:00-05:00", "stale"),
        )
        for success, deadline, expected in cases:
            with self.subTest(success=success):
                result = self.calendar._normalized_calendar_record(
                    self._record(last_success_at=success, stale_after=deadline), now=self.NOW)
                self.assertEqual(result["status"], expected, "offset_time_anomaly_admitted")
                self.assertEqual(self.calendar.calendar_record_is_eligible(result, now=self.NOW), expected == "success")

    def test_legacy_none_and_unparseable_success_compatibility(self):
        for record in ({"min_price": 500}, {"min_price": 500, "stale_after": None},
                       {"min_price": 500, "last_success_at": "bad-success"},
                       {"min_price": 500, "updated_at": "bad-success"}):
            with self.subTest(record=record):
                first = self._assert_fresh(self.calendar._normalized_calendar_record, record)
                second = self._assert_fresh(self.calendar._normalized_calendar_record, first)
                self.assertEqual(first, second)
                self.assertIsNone(second["stale_after"])
        for explicit_none in (False, True):
            with self.subTest(explicit_none=explicit_none):
                record = {"min_price": 500, "updated_at": "2026-09-17T11:00:00+08:00"}
                if explicit_none:
                    record["stale_after"] = None
                result = self._assert_fresh(self.calendar._normalized_calendar_record, record)
                self.assertEqual(result["stale_after"], "2026-09-17T17:00:00+08:00")

    def test_repeated_normalization_preserves_anomalies(self):
        for record, reason in ((self._record(stale_after="broken"), "CalendarInvalidDeadline"),
                               (self._record(last_success_at="2036-09-17T12:00:00+08:00"), "CalendarFutureSuccess")):
            with self.subTest(reason=reason):
                first = self._assert_anomaly(self.calendar._normalized_calendar_record, record, reason)
                second = self._assert_anomaly(self.calendar._normalized_calendar_record, first, reason)
                self.assertEqual(first, second)

    def test_failed_and_empty_preserve_collection_evidence(self):
        for status in ("failed", "empty"):
            for reason in (None, "PermissionError"):
                with self.subTest(status=status, reason=reason):
                    record = self._record(status=status, error_type=reason, stale_after="broken",
                                          last_success_at="2036-09-17T12:00:00+08:00")
                    result = self.calendar._normalized_calendar_record(record, now=self.NOW)
                    self.assertEqual(result["status"], status)
                    self.assertEqual(result["error_type"], reason)
                    self.assertEqual(self.calendar._status_text(result), self.calendar._status_text(record))
                    self.assertFalse(self.calendar.calendar_record_is_eligible(result, now=self.NOW))

    def test_success_source_priority_and_attempt_not_used(self):
        self._assert_fresh(self.calendar._normalized_calendar_record, self._record(
            updated_at="2036-09-17T12:00:00+08:00", last_attempt_at="2036-09-17T12:00:00+08:00"))
        self._assert_fresh(self.calendar._normalized_calendar_record,
                           {"min_price": 500, "last_attempt_at": "2036-09-17T12:00:00+08:00"})

    def test_real_read_pipeline_preserves_bytes_and_input(self):
        record = self._record(stale_after="broken")
        path = self._write(record)
        before = path.read_bytes()
        loaded = self.calendar.load_calendar("PVG-KIX", self.root)
        original = copy.deepcopy(loaded)
        rows = self.calendar.calendar_rows(loaded, self.DAY.isoformat())
        self.assertFalse(self.calendar.calendar_record_is_eligible(loaded["dates"][self.DAY.isoformat()]),
                         "read_pipeline_anomaly_admitted")
        self.assertEqual(rows[0]["status_text"], ANOMALY_TEXT, "read_pipeline_reason_missing")
        self.assertFalse(rows[0]["eligible_for_recommendation"])
        self.assertEqual(rows[0]["stale_after"], "broken")
        self.assertEqual(loaded, original)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(record, self._record(stale_after="broken"))

    def test_refresh_plan_includes_anomalous_date(self):
        from collection_plan import _calendar_dates

        read = self.calendar.load_calendar
        for record in (self._record(stale_after="broken"),
                       self._record(last_success_at="2036-09-17T12:00:00+08:00")):
            with self.subTest(record=record):
                path = self._write(record)
                before = path.read_bytes()
                with patch.object(self.calendar, "_query_dates", return_value=[self.DAY]), patch.object(
                    self.calendar, "load_calendar", side_effect=lambda route, **kw: read(route, self.root, **kw)):
                    self.assertEqual(_calendar_dates(self.DAY.isoformat(), "PVG", "KIX"),
                                     [self.DAY.isoformat()], "anomaly_skipped_by_plan")
                self.assertEqual(path.read_bytes(), before)

    def test_successful_refresh_clears_time_reason(self):
        for record in (self._record(stale_after="broken"),
                       self._record(last_success_at="2036-09-17T12:00:00+08:00")):
            with self.subTest(record=record):
                self._write(record)
                source = Mock(name="synthetic_source")
                with patch.object(self.calendar, "_query_dates", return_value=[self.DAY]), patch.object(
                    self.calendar, "_source_fetch", return_value=[{"price": 450, "source": "synthetic"}]) as fetch:
                    result = self.calendar.update_calendar("PVG-KIX", "PVG", "KIX", self.DAY.isoformat(),
                                                          source, data_dir=self.root, sleep_seconds=0)
                self.assertEqual(fetch.call_count, 1, "anomaly_skipped_by_refresh")
                source.fetch.assert_not_called()
                refreshed = result["dates"][self.DAY.isoformat()]
                self.assertEqual(refreshed["status"], "success")
                self.assertIsNone(refreshed["error_type"])
                self.assertEqual(refreshed["last_success_at"], self.NOW.isoformat())
                self.assertTrue(self.calendar.calendar_record_is_eligible(refreshed))
                self.assertEqual(self.calendar.load_calendar("PVG-KIX", self.root)["dates"][self.DAY.isoformat()], refreshed)

    def test_time_anomaly_text_is_not_ttl_expiry(self):
        for reason in ("CalendarInvalidDeadline", "CalendarFutureSuccess"):
            with self.subTest(reason=reason):
                self.assertEqual(self.calendar._status_text({"status": "stale", "error_type": reason}),
                                 ANOMALY_TEXT, "wrong_time_anomaly_text")
        self.assertEqual(self.calendar._status_text({"status": "stale"}), EXPIRED_TEXT)

    def test_mutation_invalid_deadline_skipped_is_rejected(self):
        mutant = _mutated(self.calendar._normalized_calendar_record, "skip_invalid")
        with self.assertRaisesRegex(AssertionError, "time_anomaly_admitted"):
            self._assert_anomaly(mutant, self._record(stale_after="broken"), "CalendarInvalidDeadline")

    def test_mutation_future_success_skipped_is_rejected(self):
        mutant = _mutated(self.calendar._normalized_calendar_record, "skip_future")
        with self.assertRaisesRegex(AssertionError, "time_anomaly_admitted"):
            self._assert_anomaly(mutant, self._record(last_success_at="2036-09-17T12:00:00+08:00"), "CalendarFutureSuccess")

    def test_mutation_future_deadline_rejection_is_rejected(self):
        mutant = _mutated(self.calendar._normalized_calendar_record, "reject_future_deadline")
        with self.assertRaisesRegex(AssertionError, "normal_fresh_rejected"):
            self._assert_fresh(mutant, self._record())

    def test_mutation_legacy_rejection_is_rejected(self):
        mutant = _mutated(self.calendar._normalized_calendar_record, "reject_legacy")
        with self.assertRaisesRegex(AssertionError, "normal_fresh_rejected"):
            self._assert_fresh(mutant, {"min_price": 500})

    def test_mutation_wrong_anomaly_text_is_rejected(self):
        mutant = _mutated(self.calendar._status_text, "wrong_text")
        with self.assertRaisesRegex(AssertionError, "wrong_time_anomaly_text"):
            self.assertEqual(mutant({"status": "stale", "error_type": "CalendarInvalidDeadline"}),
                             ANOMALY_TEXT, "wrong_time_anomaly_text")


if __name__ == "__main__":
    unittest.main()
