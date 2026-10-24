"""R18：舱位与机场列表字段类型不对的单条订阅记为加载失败，不再中断整批采集。

基线(R17 合并后的 main)：main._normalize_subscription 把原始 cabin_classes 原样带进规范化结果(main.py:1042)；地点无法解析的
订阅由 _invalid_location_subscription 复制整条原始记录(main.py:720)，原始的 origin_airports(_active)、destination_airports(_active)
也原样留下。批次里被前置校验跳过的订阅在记跳过行时由 _estimated_saved_api_calls(main.py:173–192)对这些字段取 len；
未被跳过的订阅在 collection_plan._cabin_classes(collection_plan.py:658–662)里被逐项迭代。字段是整数、布尔、浮点等非列表值时
抛 TypeError：前者发生在开轮档与主体 try 之前，后者发生在构建采集计划时，两种都让整批中断、正常订阅一条也不处理。
口径(用户 2026-10-24 开工指令，按建议口径)：规范化入口先检查 cabin_classes 与各机场列表字段(顶层与 basic 内)：空值照旧按缺省处理；
字符串与"全部由字符串组成的列表"照旧接受；其余类型(含列表里夹非字符串项)抛 ValueError，只报字段名与类型名、不带原值，由加载器
按既有的规范化失败计入加载失败，整批照常。不改写订阅文件，不自动纠正取值，不校验舱位或机场代码本身是否存在。
独立复核补充(同一裁定内)：basic 非空却不是对象(例如键值对数组)时同样报类型无效——迁移旧格式会把键值对数组转成对象，
其中的机场字段随后被复制到顶层，原先会绕过检查。
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


CANARY = "RAW-CONTENT-CANARY-R18"
GOOD = {"id": "good", "origin": "PVG", "destination": "PEK", "depart_date": "2026-06-10", "status": "active"}
TODAY = date(2026, 1, 1)


def bad(**fields):
    return {"id": "bad", "origin": "PVG", "destination": "PEK", "depart_date": "2026-06-10", "status": "active", **fields}


def load(records):
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "subscriptions.json"
        raw = json.dumps(records, ensure_ascii=False).encode("utf-8")
        path.write_bytes(raw)
        output = io.StringIO()
        failures = []
        with patch.object(main, "SUBSCRIPTIONS_PATH", path), redirect_stdout(output), \
                patch.object(main.logging, "warning"):
            loaded = main.load_file_subscriptions(failures=failures)
        unchanged = path.read_bytes() == raw
    return loaded, failures, output.getvalue(), unchanged


class ListFieldShapeLoaderTest(unittest.TestCase):
    WRONG = (
        ("cabin_int", {"cabin_classes": 1}, "cabin_classes 类型无效(int)"),
        ("cabin_float", {"cabin_classes": 2.5}, "cabin_classes 类型无效(float)"),
        ("cabin_bool", {"cabin_classes": True}, "cabin_classes 类型无效(bool)"),
        ("cabin_dict", {"cabin_classes": {CANARY: 1}}, "cabin_classes 类型无效(dict)"),
        ("cabin_list_with_int", {"cabin_classes": ["economy", 1]}, "cabin_classes 类型无效(list含int)"),
        ("origin_active_int", {"origin_airports_active": 5}, "origin_airports_active 类型无效(int)"),
        ("destination_list_with_int", {"destination_airports": [1]}, "destination_airports 类型无效(list含int)"),
        ("basic_dest_active_bool", {"basic": {"dest_airports_active": True}}, "basic.dest_airports_active 类型无效(bool)"),
        ("basic_origin_dict", {"basic": {"origin_airports": {CANARY: "PVG"}}}, "basic.origin_airports 类型无效(dict)"),
        ("basic_pair_list", {"basic": [["origin_airports_active", 5]]}, "basic 类型无效(list)"),
        ("basic_string", {"basic": CANARY}, "basic 类型无效(str)"),
    )

    def test_wrong_shapes_become_load_failures_in_either_order(self):
        for name, fields, error in self.WRONG:
            for order in ("bad_first", "good_first"):
                with self.subTest(case=name, order=order):
                    records = [bad(**fields), GOOD] if order == "bad_first" else [GOOD, bad(**fields)]
                    loaded, failures, output, unchanged = load(records)
                    self.assertEqual([sub["id"] for sub in loaded], ["good"], "GOOD_STILL_LOADED")
                    self.assertEqual(failures, [{"id": "bad", "error": error}])
                    self.assertIn(f"[订阅跳过] 订阅bad 规范化失败: {error}", output)
                    self.assertNotIn(CANARY, output, "RAW_VALUE_NOT_PRINTED")
                    self.assertTrue(unchanged, "FILE_NOT_REWRITTEN")

    def test_accepted_shapes_load_as_before(self):
        cases = (
            ("cabin_string", {"cabin_classes": "economy"}, "cabin_classes", "economy"),
            ("cabin_list", {"cabin_classes": ["economy", "business"]}, "cabin_classes", ["economy", "business"]),
            ("cabin_missing", {}, "cabin_classes", ["economy"]),
            ("cabin_none", {"cabin_classes": None}, "cabin_classes", ["economy"]),
            ("cabin_empty_list", {"cabin_classes": []}, "cabin_classes", ["economy"]),
            ("cabin_zero", {"cabin_classes": 0}, "cabin_classes", ["economy"]),
            ("origin_active_string", {"origin_airports_active": "PVG"}, "origin_airports_active", ["PVG"]),
            ("origin_active_list", {"origin_airports_active": ["PVG"]}, "origin_airports_active", ["PVG"]),
            ("basic_empty_list", {"basic": []}, "origin", "PVG"),
            ("basic_empty_string", {"basic": ""}, "origin", "PVG"),
        )
        for name, fields, key, expected in cases:
            with self.subTest(case=name):
                loaded, failures, _output, _unchanged = load([bad(**fields)])
                self.assertEqual(failures, [])
                self.assertEqual(len(loaded), 1)
                self.assertEqual(loaded[0][key], expected)

    def test_unresolvable_location_without_wrong_shapes_stays_an_invalid_record(self):
        loaded, failures, _output, _unchanged = load([bad(origin="NOWHERE-R18")])
        self.assertEqual(failures, [])
        self.assertEqual([(sub["id"], sub["validation_status"]) for sub in loaded], [("bad", "invalid")])

    def test_unresolvable_location_with_wrong_shape_is_a_load_failure(self):
        loaded, failures, _output, _unchanged = load([bad(origin="NOWHERE-R18", origin_airports_active=5), GOOD])
        self.assertEqual([sub["id"] for sub in loaded], ["good"])
        self.assertEqual(failures, [{"id": "bad", "error": "origin_airports_active 类型无效(int)"}])


class ListFieldShapeBatchTest(unittest.TestCase):
    """真实加载器、真实前置校验与真实跳过日志接入 _run_locked；只替换采集计划与逐条处理。"""

    def run_batch(self, records):
        logs, processed, planned = [], [], []
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
                         "_log_subscription_failure", "_run_basket_sentinel_for_main",
                         "start_round_log_archive", "end_round_log_archive", "reset_current_round"):
                stack.enter_context(patch.object(main, name))
            stack.enter_context(patch.object(main, "set_current_round", return_value=object()))
            for level in ("error", "info", "warning"):
                stack.enter_context(patch.object(main.logging, level))
            stack.enter_context(patch.object(main, "safe_log", side_effect=logs.append))
            stack.enter_context(patch.object(main, "_shanghai_today", return_value=TODAY))
            stack.enter_context(patch.object(main, "_collection_plan_log_options", return_value={}))
            stack.enter_context(patch.object(
                main, "build_collection_plan",
                side_effect=lambda **kwargs: planned.extend(sub["id"] for sub in kwargs["subscriptions"]) or plan,
            ))
            stack.enter_context(patch.object(
                main, "_process_scheduled_subscription",
                side_effect=lambda sub, **kwargs: processed.append(sub["id"]) or True,
            ))
            try:
                main._run_locked(sync_remote=False, round_id="collection_synthetic")
            except Exception as exc:
                caught = exc
        return logs, processed, planned, caught

    def test_one_wrong_shaped_record_no_longer_stops_the_batch(self):
        cases = (
            ("expired_with_int_cabin", bad(id="bad", depart_date="2025-12-01", cabin_classes=1)),
            ("unresolvable_with_int_airports", bad(origin="NOWHERE-R18", origin_airports_active=5)),
            ("future_with_int_cabin", bad(cabin_classes=1)),
            ("unresolvable_with_pair_list_basic", bad(origin="NOWHERE-R18", basic=[["origin_airports_active", 5]])),
        )
        for name, record in cases:
            for order in ("bad_first", "good_first"):
                with self.subTest(case=name, order=order):
                    records = [record, GOOD] if order == "bad_first" else [GOOD, record]
                    logs, processed, planned, caught = self.run_batch(records)
                    self.assertIsNone(caught, "BATCH_NOT_ABORTED")
                    self.assertEqual((processed, planned), (["good"], ["good"]), "ONLY_GOOD_REACHES_THE_PLAN")
                    self.assertIn("[批次结果] 订阅处理 成功=1 失败=0(其中异常=0) 前置跳过=0 预检失败=1 status=partial",
                                  logs)


if __name__ == "__main__":
    unittest.main()
