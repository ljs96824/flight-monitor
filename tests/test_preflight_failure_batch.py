"""R2：预检抛异常、加载时规范化失败的订阅计入批次结果，不再被排除在整轮结论之外。

口径：本轮检查数 = 前置跳过 + 预检失败 + 进入处理。有预检失败时整轮不再是 ok：
另有订阅处理成功记 partial，否则记 failed；全部订阅都没进入处理时同样输出 [批次结果]。
正常跳过(过期、地点无效等)仍不算失败；没有预检失败时两行汇总与原格式逐字相同；
已进入处理的订阅只处理一次，预检失败的订阅不处理、不重试。
"""

from contextlib import ExitStack, redirect_stdout
from datetime import date
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

import main


SUMMARY = re.compile(r"^\[订阅前置校验\] 本轮检查=(\d+) 跳过=(\d+)(?: 预检失败=(\d+))?$")


def run_batch(specs, *, load_failures=()):
    """specs 每项为 ("ready", 处理结果) / ("skip", None) / ("raise", 异常)。"""
    events = []
    output = io.StringIO()
    subscriptions = [{"id": f"synthetic-{i}", "spec": spec} for i, spec in enumerate(specs)]

    def fake_load(*, failures=None):
        if failures is not None:
            failures.extend({"id": f"broken-{i}", "error": "synthetic"} for i in range(len(load_failures)))
        return list(subscriptions)

    def preflight(sub, **kwargs):
        kind, value = sub["spec"]
        if kind == "raise":
            raise value
        return {"skip": kind == "skip"}

    def process(sub, **kwargs):
        events.append(("process", sub["id"]))
        return sub["spec"][1]

    plan = Mock(request_keys=set(), panel_only_keys=set(), freshness_hours=6, fresh_scope="primary_only")
    with ExitStack() as stack:
        stack.enter_context(redirect_stdout(output))
        for name in ("init_db", "sync_subscriptions", "activate_collection_plan", "deactivate_collection_plan",
                     "set_current_round", "reset_current_round", "start_request_cache_round",
                     "print_request_cache_stats", "log_retention_dry_run", "_log_preflight_skip"):
            stack.enter_context(patch.object(main, name))
        failure_log = stack.enter_context(patch.object(main, "_log_subscription_failure"))
        stack.enter_context(patch.object(main.logging, "error"))
        stack.enter_context(patch.object(main.logging, "info"))
        stack.enter_context(patch.object(main, "safe_log", side_effect=lambda message: events.append(("log", message))))
        stack.enter_context(patch.object(main, "_shanghai_today", return_value=date(2026, 1, 1)))
        stack.enter_context(patch.object(main, "load_file_subscriptions", side_effect=fake_load))
        stack.enter_context(patch.object(main, "evaluate_subscription_preflight", side_effect=preflight))
        stack.enter_context(patch.object(main, "_collection_plan_log_options", return_value={}))
        build = stack.enter_context(patch.object(main, "build_collection_plan", return_value=plan))
        stack.enter_context(patch.object(main, "_process_scheduled_subscription", side_effect=process))
        start = stack.enter_context(patch.object(main, "start_round_log_archive"))
        end = stack.enter_context(patch.object(main, "end_round_log_archive",
                                               side_effect=lambda **kw: events.append(("end", kw["status"]))))
        sentinel = stack.enter_context(patch.object(main, "_run_basket_sentinel_for_main"))
        returned = main._run_locked(sync_remote=False, round_id="collection_synthetic")
    logs = [message for kind, message in events if kind == "log"]
    return {
        "returned": returned,
        "output": output.getvalue(),
        "batch": [line for line in logs if line.startswith("[批次结果]")],
        "summary": [line for line in logs if line.startswith("[订阅前置校验]")],
        "processed": [value for kind, value in events if kind == "process"],
        "end": [value for kind, value in events if kind == "end"],
        "start": start,
        "build": build,
        "sentinel": sentinel,
        "failure_log": failure_log,
        "subscriptions": subscriptions,
    }


def batch_line(success, failed, exceptions, skipped, preflight_failed, status):
    return (f"[批次结果] 订阅处理 成功={success} 失败={failed}(其中异常={exceptions}) 前置跳过={skipped} "
            f"预检失败={preflight_failed} status={status}")


class PreflightFailureBatchTest(unittest.TestCase):
    def assert_invariant(self, result):
        self.assertEqual(len(result["summary"]), 1, "one_preflight_summary")
        checked, skipped, failed = SUMMARY.match(result["summary"][0]).groups()
        self.assertEqual(int(checked), int(skipped) + int(failed or 0) + len(result["processed"]),
                         "checked_equals_skipped_plus_failed_plus_processed")

    def test_preflight_failures_counted_when_some_subscriptions_processed(self):
        boom = OverflowError("date value out of range")
        cases = (
            # 名称, 订阅, 加载失败数, 期望状态, 期望[批次结果], 期望[订阅前置校验]
            ("exception_plus_success", [("ready", True), ("raise", boom)], 0, "partial",
             batch_line(1, 0, 0, 0, 1, "partial"), "[订阅前置校验] 本轮检查=2 跳过=0 预检失败=1"),
            ("exception_plus_processing_failure", [("ready", False), ("raise", boom)], 0, "failed",
             batch_line(0, 1, 0, 0, 1, "failed"), "[订阅前置校验] 本轮检查=2 跳过=0 预检失败=1"),
            ("exception_skip_and_success", [("ready", True), ("skip", None), ("raise", boom)], 0, "partial",
             batch_line(1, 0, 0, 1, 1, "partial"), "[订阅前置校验] 本轮检查=3 跳过=1 预检失败=1"),
            ("load_failure_plus_success", [("ready", True)], 1, "partial",
             batch_line(1, 0, 0, 0, 1, "partial"), "[订阅前置校验] 本轮检查=2 跳过=0 预检失败=1"),
        )
        for name, specs, load_failures, status, batch, summary in cases:
            with self.subTest(case=name):
                result = run_batch(specs, load_failures=range(load_failures))
                self.assertIsNone(result["returned"])
                self.assertEqual(result["end"], [status], "round_archive_status")
                self.assertEqual(result["batch"], [batch])
                self.assertEqual(result["summary"], [summary])
                ready_ids = [sub["id"] for sub in result["subscriptions"] if sub["spec"][0] == "ready"]
                self.assertEqual(result["processed"], ready_ids, "only_ready_processed_once")
                self.assertEqual(result["build"].call_args.kwargs["subscriptions"],
                                 [sub for sub in result["subscriptions"] if sub["spec"][0] == "ready"])
                result["sentinel"].assert_called_once_with(result["subscriptions"])
                self.assert_invariant(result)

    def test_no_ready_subscription_still_reports_failed_batch(self):
        boom = OverflowError("date value out of range")
        cases = (
            ("all_exceptions", [("raise", boom), ("raise", boom)], 0,
             batch_line(0, 0, 0, 0, 2, "failed"), "[订阅前置校验] 本轮检查=2 跳过=0 预检失败=2"),
            ("skip_and_exception", [("skip", None), ("raise", boom)], 0,
             batch_line(0, 0, 0, 1, 1, "failed"), "[订阅前置校验] 本轮检查=2 跳过=1 预检失败=1"),
            ("all_load_failures", [], 2,
             batch_line(0, 0, 0, 0, 2, "failed"), "[订阅前置校验] 本轮检查=2 跳过=0 预检失败=2"),
        )
        for name, specs, load_failures, batch, summary in cases:
            with self.subTest(case=name):
                result = run_batch(specs, load_failures=range(load_failures))
                self.assertIsNone(result["returned"])
                self.assertEqual(result["batch"], [batch])
                self.assertEqual(result["summary"], [summary])
                self.assertEqual(result["processed"], [])
                result["start"].assert_not_called()
                result["build"].assert_not_called()
                self.assertEqual(result["end"], [], "no_round_archive_without_ready")
                result["sentinel"].assert_called_once_with(result["subscriptions"])
                self.assertNotIn("暂无订阅", result["output"])
                self.assert_invariant(result)

    def test_preflight_exception_still_logged_per_subscription(self):
        result = run_batch([("ready", True), ("raise", OverflowError("date value out of range"))])
        result["failure_log"].assert_called_once_with(
            result["subscriptions"][1], reason="前置校验失败: OverflowError: date value out of range")

    def test_without_preflight_failures_lines_are_unchanged(self):
        cases = (
            ("success_and_skip", [("ready", True), ("skip", None)],
             ["[批次结果] 订阅处理 成功=1 失败=0(其中异常=0) 前置跳过=1 status=ok"], "[订阅前置校验] 本轮检查=2 跳过=1"),
            ("only_skips", [("skip", None), ("skip", None)], [], "[订阅前置校验] 本轮检查=2 跳过=2"),
            ("no_subscriptions", [], [], "[订阅前置校验] 本轮检查=0 跳过=0"),
        )
        for name, specs, batch, summary in cases:
            with self.subTest(case=name):
                result = run_batch(specs)
                self.assertEqual(result["batch"], batch)
                self.assertEqual(result["summary"], [summary])
                self.assertIs("暂无订阅" in result["output"], not specs)


class LoaderFailureReportTest(unittest.TestCase):
    def test_normalization_failures_reported_without_changing_result(self):
        records = [
            {"id": "good", "origin": "PVG", "destination": "PEK", "depart_date": "2026-06-10", "status": "active"},
            {"id": "broken", "origin": "PVG", "destination": "PEK", "depart_date": "2026-06-10", "status": "active"},
            {"id": "paused", "origin": "PVG", "destination": "PEK", "depart_date": "2026-06-10", "status": "paused"},
        ]
        real_normalize = main._normalize_subscription

        def normalize(item):
            if item.get("id") == "broken":
                raise ValueError("synthetic normalization failure")
            return real_normalize(item)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
            with (
                patch.object(main, "SUBSCRIPTIONS_PATH", path),
                patch.object(main, "_normalize_subscription", side_effect=normalize),
                redirect_stdout(io.StringIO()),
            ):
                plain = main.load_file_subscriptions()
                failures = []
                reported = main.load_file_subscriptions(failures=failures)
        self.assertEqual([sub["id"] for sub in plain], ["good"])
        self.assertEqual(reported, plain, "result_unchanged")
        self.assertEqual(failures, [{"id": "broken", "error": "synthetic normalization failure"}],
                         "paused_is_not_a_failure")


if __name__ == "__main__":
    unittest.main()
