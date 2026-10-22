"""R15：暂停订阅跳过行的格式化不得中断整批加载。

基线(R12 合并后的 main)：main.load_file_subscriptions 对非 active 记录先拼航线再打印跳过行，拼航线时对 basic 直接调用 .get
(main.py:1435)，这一段在逐条规范化的 try 之外。顶层缺航线端点、basic 又是非空字符串或列表等非对象值时抛 AttributeError，
整批加载不返回，排在前面或后面的正常订阅都进不了处理；_run_locked 在开轮档与主体 try 之前调用加载器，异常直接穿出 run_cli。
口径(用户裁定 2026-10-22，采纳建议)：暂停记录仍不采集、不计失败、不自动启用；跳过行的航线端点只取非空字符串，先看顶层、
再看对象型 basic，basic 不是对象时不读取，取不到的端点记"未知"；不打印整条记录或损坏字段，不改写文件。
"""

from contextlib import ExitStack, redirect_stdout
from datetime import date
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import main


CANARY = "RAW-CONTENT-CANARY-R15"
GOOD = {"id": "good", "origin": "PVG", "destination": "PEK", "depart_date": "2026-06-10", "status": "active"}
BAD_PAUSED = {"id": "synthetic-paused", "status": "paused", "basic": CANARY}
OK_PAUSED = {"id": "ok-paused", "origin": "PVG", "destination": "KIX", "status": "paused"}


def load(records, *, with_failures=True):
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "subscriptions.json"
        raw = json.dumps(records, ensure_ascii=False).encode("utf-8")
        path.write_bytes(raw)
        output = io.StringIO()
        failures = [] if with_failures else None
        with patch.object(main, "SUBSCRIPTIONS_PATH", path), redirect_stdout(output), \
                patch.object(main.logging, "warning"):
            loaded = main.load_file_subscriptions(failures=failures)
        unchanged = path.read_bytes() == raw
    return loaded, failures, output.getvalue(), unchanged


class PausedLabelLoaderTest(unittest.TestCase):
    def test_bad_paused_record_does_not_stop_loading_in_either_order(self):
        for order in ("bad_first", "good_first"):
            with self.subTest(order=order):
                records = [BAD_PAUSED, GOOD] if order == "bad_first" else [GOOD, BAD_PAUSED]
                loaded, failures, output, unchanged = load(records)
                self.assertEqual([sub["id"] for sub in loaded], ["good"], "GOOD_STILL_LOADED")
                self.assertEqual(failures, [], "PAUSED_IS_NOT_A_FAILURE")
                self.assertIn("[跳过] 订阅已暂停: synthetic-paused 未知→未知", output)
                self.assertNotIn(CANARY, output, "DAMAGED_FIELD_NOT_PRINTED")
                self.assertTrue(unchanged, "FILE_NOT_REWRITTEN")

    def test_every_non_object_basic_is_left_unread(self):
        for basic in (CANARY, [CANARY], 7, 2.5, True):
            with self.subTest(basic=type(basic).__name__):
                record = {"id": "p", "status": "paused", "basic": basic}
                loaded, failures, output, _unchanged = load([record, GOOD])
                self.assertEqual([sub["id"] for sub in loaded], ["good"])
                self.assertEqual(failures, [])
                self.assertIn("[跳过] 订阅已暂停: p 未知→未知", output)
                self.assertNotIn(CANARY, output)

    def test_route_label_uses_string_endpoints_from_top_level_then_object_basic(self):
        cases = (
            ("top_level", {"origin": "PVG", "destination": "KIX"}, "PVG→KIX"),
            ("object_basic", {"basic": {"origin": "PVG", "destination": "KIX"}}, "PVG→KIX"),
            ("mixed", {"origin": "PVG", "basic": {"destination": "KIX"}}, "PVG→KIX"),
            ("top_level_wins", {"origin": "SHA", "basic": {"origin": "PVG", "destination": "KIX"}}, "SHA→KIX"),
            ("string_basic", {"origin": "PVG", "basic": CANARY}, "PVG→未知"),
            ("non_string_endpoints", {"origin": {"raw": CANARY}, "destination": 123,
                                      "basic": {"origin": [CANARY], "destination": None}}, "未知→未知"),
            ("nothing", {}, "未知→未知"),
        )
        for name, fields, label in cases:
            with self.subTest(case=name):
                record = {"id": "p", "status": "paused", **fields}
                loaded, failures, output, _unchanged = load([record, GOOD])
                self.assertEqual([sub["id"] for sub in loaded], ["good"])
                self.assertEqual(failures, [])
                self.assertIn(f"[跳过] 订阅已暂停: p {label}\n", output)
                self.assertNotIn(CANARY, output)

    def test_paused_records_are_skipped_the_same_with_or_without_a_failures_list(self):
        records = [BAD_PAUSED, OK_PAUSED, GOOD]
        with_list, failures, output, unchanged = load(records)
        without_list, _none, _output, _unchanged = load(records, with_failures=False)
        self.assertEqual(with_list, without_list)
        self.assertEqual([sub["id"] for sub in with_list], ["good"])
        self.assertEqual(failures, [])
        self.assertIn("[跳过] 订阅已暂停: ok-paused PVG→KIX", output)
        self.assertNotIn("[订阅汇总]", output, "NO_SKIP_SUMMARY_FOR_PAUSED")
        self.assertTrue(unchanged)


class PausedLabelBatchTest(unittest.TestCase):
    """真实 load_file_subscriptions 接入 _run_locked：坏暂停记录不影响正常订阅。"""

    def run_batch(self, records):
        logs, processed, ends = [], [], []
        output = io.StringIO()
        plan = Mock(request_keys=set(), panel_only_keys=set(), freshness_hours=6, fresh_scope="primary_only")
        caught = None
        with tempfile.TemporaryDirectory() as tmpdir, ExitStack() as stack:
            path = Path(tmpdir) / "subscriptions.json"
            path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(patch.object(main, "SUBSCRIPTIONS_PATH", path))
            for name in ("init_db", "sync_subscriptions", "activate_collection_plan", "deactivate_collection_plan",
                         "start_request_cache_round", "print_request_cache_stats", "log_retention_dry_run",
                         "_log_preflight_skip", "_log_subscription_failure", "_run_basket_sentinel_for_main",
                         "start_round_log_archive", "reset_current_round"):
                stack.enter_context(patch.object(main, name))
            stack.enter_context(patch.object(main, "set_current_round", return_value=object()))
            stack.enter_context(patch.object(main.logging, "error"))
            stack.enter_context(patch.object(main.logging, "info"))
            stack.enter_context(patch.object(main.logging, "warning"))
            stack.enter_context(patch.object(main, "safe_log", side_effect=logs.append))
            stack.enter_context(patch.object(main, "_shanghai_today", return_value=date(2026, 1, 1)))
            stack.enter_context(patch.object(main, "evaluate_subscription_preflight", return_value={"skip": False}))
            stack.enter_context(patch.object(main, "_collection_plan_log_options", return_value={}))
            stack.enter_context(patch.object(main, "build_collection_plan", return_value=plan))
            stack.enter_context(patch.object(
                main, "_process_scheduled_subscription",
                side_effect=lambda sub, **kwargs: processed.append(sub["id"]) or True,
            ))
            stack.enter_context(patch.object(main, "end_round_log_archive",
                                            side_effect=lambda **kw: ends.append(kw["status"])))
            try:
                main._run_locked(sync_remote=False, round_id="collection_synthetic")
            except Exception as exc:
                caught = exc
        return logs, processed, ends, output.getvalue(), caught

    def test_bad_paused_record_before_or_after_good_one_processes_good_once(self):
        for order in ("bad_first", "good_first"):
            with self.subTest(order=order):
                records = [BAD_PAUSED, GOOD] if order == "bad_first" else [GOOD, BAD_PAUSED]
                logs, processed, ends, output, caught = self.run_batch(records)
                self.assertIsNone(caught, "LOADER_DID_NOT_ABORT_THE_BATCH")
                self.assertEqual(processed, ["good"], "GOOD_PROCESSED_ONCE")
                self.assertIn("[批次结果] 订阅处理 成功=1 失败=0(其中异常=0) 前置跳过=0 status=ok", logs)
                self.assertEqual(ends, ["ok"])
                self.assertNotIn(CANARY, output)

    def test_only_a_bad_paused_record_still_means_no_subscriptions(self):
        logs, processed, ends, output, caught = self.run_batch([BAD_PAUSED])
        self.assertIsNone(caught)
        self.assertIn("暂无订阅，请通过表单添加", output)
        self.assertIn("[订阅前置校验] 本轮检查=0 跳过=0", logs)
        self.assertEqual((processed, ends), ([], []))


if __name__ == "__main__":
    unittest.main()
