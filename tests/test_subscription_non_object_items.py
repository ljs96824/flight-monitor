"""R12：订阅数组里的非对象条目计入加载失败，不再被静默跳过。

基线(R11 合并后的 main)：main.load_file_subscriptions 遍历数组时遇到非对象条目直接 continue，既不进 failures 也不打印；
_run_locked 只在"没有订阅且没有加载失败"时走"暂无订阅"分支，于是数组里全是 null / 数字 / 字符串时，本轮按"暂无订阅"
正常结束，坏数据无人知晓；一好一坏时坏条目也不计入批次失败。
口径(用户裁定 2026-10-21，采纳建议)：非对象条目仍不参与采集，但按数组位置计入加载失败，标识为 `#<位置>`、原因为
`not_object:<类型名>`；不打印原内容，不伪造订阅编号，不删除或改写文件。合法 [] 仍为暂无订阅；全部非法时走已有的
"批次失败"分支；一好一坏时正常订阅只处理一次、坏条目计一次失败；暂停订阅仍不算失败。
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


CANARY = "RAW-CONTENT-CANARY-R12"
GOOD = {"id": "good", "origin": "PVG", "destination": "PEK", "depart_date": "2026-06-10", "status": "active"}
PAUSED = {"id": "paused", "origin": "PVG", "destination": "PEK", "depart_date": "2026-06-10", "status": "paused"}


def load(records):
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "subscriptions.json"
        path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        output = io.StringIO()
        failures = []
        with patch.object(main, "SUBSCRIPTIONS_PATH", path), redirect_stdout(output), \
                patch.object(main.logging, "warning"):
            loaded = main.load_file_subscriptions(failures=failures)
    return loaded, failures, output.getvalue()


class LoaderNonObjectItemTest(unittest.TestCase):
    def test_empty_array_is_still_no_subscriptions(self):
        loaded, failures, _output = load([])
        self.assertEqual((loaded, failures), ([], []))

    def test_every_non_object_item_is_a_load_failure_by_position(self):
        loaded, failures, output = load([None, 7, CANARY, [1], 2.5, True])
        self.assertEqual(loaded, [])
        self.assertEqual(
            failures,
            [
                {"id": "#0", "error": "not_object:NoneType"},
                {"id": "#1", "error": "not_object:int"},
                {"id": "#2", "error": "not_object:str"},
                {"id": "#3", "error": "not_object:list"},
                {"id": "#4", "error": "not_object:float"},
                {"id": "#5", "error": "not_object:bool"},
            ],
        )
        self.assertNotIn(CANARY, output, "RAW_CONTENT_NOT_PRINTED")
        self.assertIn("[订阅跳过] 订阅#2 不是对象(str)，已计入加载失败", output)
        self.assertIn("[订阅汇总] 跳过6条无效订阅,正常处理0条", output)

    def test_good_paused_and_bad_items_together(self):
        loaded, failures, _output = load([GOOD, None, PAUSED])
        self.assertEqual([sub["id"] for sub in loaded], ["good"])
        self.assertEqual(failures, [{"id": "#1", "error": "not_object:NoneType"}], "PAUSED_IS_NOT_A_FAILURE")

    def test_result_is_the_same_with_or_without_a_failures_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            path.write_text(json.dumps([GOOD, 7]), encoding="utf-8")
            with patch.object(main, "SUBSCRIPTIONS_PATH", path), redirect_stdout(io.StringIO()), \
                    patch.object(main.logging, "warning"):
                plain = main.load_file_subscriptions()
                reported = main.load_file_subscriptions(failures=[])
        self.assertEqual(plain, reported)
        self.assertEqual([sub["id"] for sub in plain], ["good"])

    def test_file_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            raw = json.dumps([None, GOOD], ensure_ascii=False).encode("utf-8")
            path.write_bytes(raw)
            with patch.object(main, "SUBSCRIPTIONS_PATH", path), redirect_stdout(io.StringIO()), \
                    patch.object(main.logging, "warning"):
                main.load_file_subscriptions(failures=[])
            self.assertEqual(path.read_bytes(), raw)


class BatchNonObjectItemTest(unittest.TestCase):
    """真实 load_file_subscriptions 接入 _run_locked：批次结论与处理次数。"""

    def run_batch(self, records):
        logs, processed, ends = [], [], []
        output = io.StringIO()
        plan = Mock(request_keys=set(), panel_only_keys=set(), freshness_hours=6, fresh_scope="primary_only")
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
            main._run_locked(sync_remote=False, round_id="collection_synthetic")
        return logs, processed, ends, output.getvalue()

    def test_all_invalid_items_fail_the_batch_instead_of_no_subscriptions(self):
        logs, processed, ends, output = self.run_batch([None, 7, "broken"])
        self.assertNotIn("暂无订阅", output)
        self.assertIn("[批次结果] 订阅处理 成功=0 失败=0(其中异常=0) 前置跳过=0 预检失败=3 status=failed", logs)
        self.assertIn("[订阅前置校验] 本轮检查=3 跳过=0 预检失败=3", logs)
        self.assertEqual((processed, ends), ([], []))

    def test_one_good_one_bad_processes_good_once_and_counts_bad(self):
        logs, processed, ends, _output = self.run_batch([GOOD, None, PAUSED])
        self.assertEqual(processed, ["good"], "GOOD_PROCESSED_ONCE")
        self.assertIn("[批次结果] 订阅处理 成功=1 失败=0(其中异常=0) 前置跳过=0 预检失败=1 status=partial", logs)
        self.assertEqual(ends, ["partial"])

    def test_empty_array_still_reports_no_subscriptions(self):
        logs, processed, ends, output = self.run_batch([])
        self.assertIn("暂无订阅，请通过表单添加", output)
        self.assertIn("[订阅前置校验] 本轮检查=0 跳过=0", logs)
        self.assertEqual((processed, ends), ([], []))


if __name__ == "__main__":
    unittest.main()
