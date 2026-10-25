"""R19：采集计划要用的去程或返程日期展开不了的单条订阅记为加载失败，不再让整批在构建采集计划时中断。

基线(G19 合并后的 main，f077f5e9)：subscription_preflight._parse_date 解析失败返回 None，不据此判输入无效，坏日期订阅照常进入采集计划。
国内航线、有非商务舱且有支持该舱位的搜索源时，collection_plan._calendar_dates → price_calendar._query_dates 先按前 10 个字符解析
日期(2026/06/12、2026-02-30、对象等抛 ValueError)，再取目标日前后 14 天(9999-12-25 这类临近可表示范围边界的日期抛 OverflowError)；
build_collection_plan 在 main._run_locked 的主体 try 里、逐条处理之前调用，整批中断，正常订阅一条也不处理。国际航线或纯商务舱不崩，
但坏日期作为主行程请求进入计划。日期弹性为 ±Infinity 时，预检 _as_nonnegative_int 抛 OverflowError，由 _run_locked 逐条捕获计为预检失败。
口径(用户 2026-10-25"开工r19"，按建议口径；独立复核后按同一主题补上临近边界的日期)：规范化入口取采集计划实际要用的去程与返程日期
(collection_plan.planned_trip_dates，与 build_collection_plan 取值一致)，按低价日历同一函数展开(collection_plan.unplannable_trip_date)；
展开不了抛 ValueError，只报字段名、不带原值，由加载器计入加载失败，整批照常。原来能展开的写法照旧接受；顶层去程为空的记录照旧由加载器
剔除、不计失败；不改预检，研究入口不在本笔；Infinity 弹性不改代码，只用完整入口测试固定顶层写法下的现有隔离行为。
已过期或 lcc_policy 无效、同时带坏日期的记录，原先被前置校验跳过，现在在加载时计为失败(下方批次测试固定这一变化)。
"""

from contextlib import ExitStack, redirect_stdout
from datetime import date
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import collection_plan
import main
import price_calendar


CANARY = "RAW-CONTENT-CANARY-R19"
GOOD = {"id": "good", "origin": "PVG", "destination": "PEK", "depart_date": "2026-06-10", "status": "active"}
TODAY = date(2026, 1, 1)
PARTIAL_LINE = "[批次结果] 订阅处理 成功=1 失败=0(其中异常=0) 前置跳过=0 预检失败=1 status=partial"


def bad(**fields):
    return {"id": "bad", "origin": "PVG", "destination": "PEK", "depart_date": "2026-06-12", "status": "active", **fields}


class _FakeSource:
    """不声明 supported_cabins，即支持全部舱位；只用于构建计划，不会被执行。"""

    name = "juhe"


def fake_sources(origin, dest, route_type=None):
    return [_FakeSource()], []


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


class TripDateLoaderTest(unittest.TestCase):
    WRONG = (
        ("depart_slash", {"depart_date": "2026/06/12"}, "depart_date 日期无效"),
        ("depart_feb30", {"depart_date": "2026-02-30"}, "depart_date 日期无效"),
        ("depart_object", {"depart_date": {CANARY: 1}}, "depart_date 日期无效"),
        ("depart_word", {"depart_date": CANARY}, "depart_date 日期无效"),
        ("depart_beyond_calendar_window", {"depart_date": "9999-12-25"}, "depart_date 日期无效"),
        ("return_slash_round_trip", {"round_trip": True, "return_date": "2026/06/15"}, "return_date 日期无效"),
        ("return_object_same_day", {"same_day_round_trip": True, "return_date": {CANARY: 1}}, "return_date 日期无效"),
        ("return_in_section_round_trip", {"round_trip": True, "preferences": {"return_date": "2026/06/15"}},
         "return_date 日期无效"),
    )

    def test_unplannable_dates_become_load_failures_in_either_order(self):
        for name, fields, error in self.WRONG:
            for order in ("bad_first", "good_first"):
                with self.subTest(case=name, order=order):
                    records = [bad(**fields), GOOD] if order == "bad_first" else [GOOD, bad(**fields)]
                    loaded, failures, output, unchanged = load(records)
                    self.assertEqual([sub["id"] for sub in loaded], ["good"], "ONLY_GOOD_LOADED")
                    self.assertEqual(failures, [{"id": "bad", "error": error}])
                    self.assertIn(f"[订阅跳过] 订阅bad 规范化失败: {error}", output)
                    self.assertNotIn(CANARY, output, "RAW_VALUE_NOT_PRINTED")
                    self.assertTrue(unchanged, "FILE_NOT_REWRITTEN")

    def test_expandable_or_unused_dates_load_as_before(self):
        cases = (
            ("iso_date", {}, "depart_date", "2026-06-12"),
            ("iso_prefix_with_time", {"depart_date": "2026-06-12T08:00"}, "depart_date", "2026-06-12T08:00"),
            ("round_trip_valid_return", {"round_trip": True, "return_date": "2026-06-15"}, "return_date", "2026-06-15"),
            ("one_way_ignores_return", {"round_trip": False, "return_date": "2026/06/15"}, "return_date", "2026/06/15"),
            ("same_day_without_return", {"same_day_round_trip": True}, "return_date", "2026-06-12"),
        )
        for name, fields, key, expected in cases:
            with self.subTest(case=name):
                loaded, failures, _output, _unchanged = load([bad(**fields)])
                self.assertEqual(failures, [])
                self.assertEqual(len(loaded), 1)
                self.assertEqual(loaded[0][key], expected)

    def test_empty_top_level_depart_is_still_dropped_without_failure(self):
        shapes = (
            ("no_section", {}),
            ("preferences", {"preferences": {"depart_date": "2026/06/12"}}),
            ("hard_constraints", {"hard_constraints": {"depart_date": "2026/06/12"}}),
            ("basic", {"basic": {"depart_date": "2026/06/12"}}),
        )
        for name, fields in shapes:
            with self.subTest(shape=name):
                loaded, failures, _output, _unchanged = load([bad(depart_date="", **fields), GOOD])
                self.assertEqual(([sub["id"] for sub in loaded], failures), (["good"], []))


class UnplannableTripDateTest(unittest.TestCase):
    """展开口径就是低价日历的 _query_dates：前 10 个字符的 ISO 日期，且目标日前后 14 天都在可表示范围内。"""

    def test_calendar_window_bounds(self):
        from collection_plan import unplannable_trip_date

        cases = (
            ("latest_expandable", {"depart_date": "9999-12-17"}, ""),
            ("one_day_too_late", {"depart_date": "9999-12-18"}, "depart_date"),
            ("earliest_expandable", {"depart_date": "0001-01-15"}, ""),
            ("one_day_too_early", {"depart_date": "0001-01-14"}, "depart_date"),
            ("prefix_with_time", {"depart_date": "2026-06-12T08:00"}, ""),
            ("unparseable_depart", {"depart_date": "2026/06/12"}, "depart_date"),
            ("return_too_late", {"depart_date": "2026-06-12", "round_trip": True, "return_date": "9999-12-31"},
             "return_date"),
            ("unused_return", {"depart_date": "2026-06-12", "round_trip": False, "return_date": "9999-12-31"}, ""),
            ("no_depart", {"depart_date": ""}, ""),
        )
        for name, fields, expected in cases:
            with self.subTest(case=name):
                self.assertEqual(unplannable_trip_date(fields), expected)


class PlannedTripDatesContractTest(unittest.TestCase):
    """planned_trip_dates 与 build_collection_plan 实际交给去程、返程请求的日期逐条一致(含字段在分节里的写法)。"""

    SHAPES = (
        ("one_way", {"depart_date": "2026-06-12"}),
        ("round_trip", {"depart_date": "2026-06-12", "round_trip": True, "return_date": "2026-06-15"}),
        ("round_trip_text_flag", {"depart_date": "2026-06-12", "round_trip": "yes", "return_date": "2026-06-15"}),
        ("one_way_with_return", {"depart_date": "2026-06-12", "round_trip": False, "return_date": "2026-06-15"}),
        ("same_day_without_return", {"depart_date": "2026-06-12", "same_day_round_trip": True}),
        ("same_day_in_section", {"depart_date": "2026-06-12", "hard_constraints": {"same_day_round_trip": True}}),
        ("depart_in_section", {"depart_date": "", "basic": {"depart_date": "2026-06-12"}}),
        ("return_in_section", {"depart_date": "2026-06-12", "round_trip": True, "preferences": {"return_date": "2026-06-15"}}),
        ("no_depart", {"depart_date": ""}),
        ("unparseable_kept_verbatim", {"depart_date": "2026/06/12", "round_trip": True, "return_date": "2026-02-30"}),
    )

    def test_dates_match_what_the_plan_builder_uses(self):
        from collection_plan import planned_trip_dates

        direction = {"depart_date": "outbound", "return_date": "return"}
        for name, fields in self.SHAPES:
            with self.subTest(shape=name):
                sub = {"id": name, "origin": "PVG", "destination": "KIX", "route_type": "international", **fields}
                calls = []
                with patch.object(
                    collection_plan,
                    "_add_direction_requests",
                    side_effect=lambda plan, **kwargs: calls.append(
                        (kwargs["group_prefix"].rsplit(":", 1)[1], kwargs["depart_date"])
                    ),
                ):
                    collection_plan.build_collection_plan(
                        subscriptions=[sub], basket_requests=[], source_builder=fake_sources
                    )
                self.assertEqual(calls, [(direction[field], text) for field, text in planned_trip_dates(sub)])


class TripDateBatchTest(unittest.TestCase):
    """真实加载器、真实前置校验与真实 build_collection_plan(注入假搜索源)接入 _run_locked；只替换计划执行与逐条处理。"""

    def run_batch(self, records):
        logs, processed, planned, failures = [], [], [], []
        output = io.StringIO()
        real_build = collection_plan.build_collection_plan

        def build(**kwargs):
            real_build(**kwargs, source_builder=fake_sources)
            planned.extend(sub["id"] for sub in kwargs["subscriptions"])
            return Mock(request_keys=set(), panel_only_keys=set(), freshness_hours=6, fresh_scope="primary_only")

        caught = None
        with tempfile.TemporaryDirectory() as tmpdir, ExitStack() as stack:
            path = Path(tmpdir) / "subscriptions.json"
            path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(patch.object(main, "SUBSCRIPTIONS_PATH", path))
            for name in ("init_db", "sync_subscriptions", "activate_collection_plan", "deactivate_collection_plan",
                         "start_request_cache_round", "print_request_cache_stats", "log_retention_dry_run",
                         "_run_basket_sentinel_for_main", "start_round_log_archive", "end_round_log_archive",
                         "reset_current_round"):
                stack.enter_context(patch.object(main, name))
            stack.enter_context(patch.object(main, "set_current_round", return_value=object()))
            for level in ("error", "info", "warning"):
                stack.enter_context(patch.object(main.logging, level))
            stack.enter_context(patch.object(main, "safe_log", side_effect=logs.append))
            stack.enter_context(patch.object(
                main, "_log_subscription_failure",
                side_effect=lambda sub, **kwargs: failures.append((sub.get("id"), str(kwargs.get("reason", "")))),
            ))
            stack.enter_context(patch.object(main, "_shanghai_today", return_value=TODAY))
            stack.enter_context(patch.object(main, "_collection_plan_log_options", return_value={}))
            stack.enter_context(patch.object(price_calendar, "load_calendar", return_value={}))
            stack.enter_context(patch.object(main, "build_collection_plan", side_effect=build))
            stack.enter_context(patch.object(
                main, "_process_scheduled_subscription",
                side_effect=lambda sub, **kwargs: processed.append(sub["id"]) or True,
            ))
            try:
                main._run_locked(sync_remote=False, round_id="collection_synthetic")
            except Exception as exc:
                caught = exc
        return logs, processed, planned, failures, caught

    def test_one_unplannable_date_no_longer_stops_the_batch(self):
        cases = (
            ("depart_slash", bad(depart_date="2026/06/12")),
            ("depart_feb30", bad(depart_date="2026-02-30")),
            ("depart_beyond_calendar_window", bad(depart_date="9999-12-25", date_flexibility=1)),
            ("return_slash_round_trip", bad(round_trip=True, return_date="2026/06/15")),
        )
        for name, record in cases:
            for order in ("bad_first", "good_first"):
                with self.subTest(case=name, order=order):
                    records = [record, GOOD] if order == "bad_first" else [GOOD, record]
                    logs, processed, planned, _failures, caught = self.run_batch(records)
                    self.assertIsNone(caught, "BATCH_NOT_ABORTED")
                    self.assertEqual((processed, planned), (["good"], ["good"]), "ONLY_GOOD_REACHES_THE_PLAN")
                    self.assertIn(PARTIAL_LINE, logs)

    def test_expired_trip_with_unparseable_return_now_counts_as_failure(self):
        # 原先被前置校验按已过期跳过(批次 ok)；现在在加载时计为失败(批次 partial)，属本笔口径的既定后果。
        record = bad(depart_date="2025-12-01", round_trip=True, return_date="2025/12/05")
        for order in ("bad_first", "good_first"):
            with self.subTest(order=order):
                records = [record, GOOD] if order == "bad_first" else [GOOD, record]
                logs, processed, planned, _failures, caught = self.run_batch(records)
                self.assertIsNone(caught)
                self.assertEqual((processed, planned), (["good"], ["good"]))
                self.assertIn(PARTIAL_LINE, logs)

    def test_infinite_flexibility_stays_isolated_by_preflight(self):
        cases = (
            ("depart_flexibility", bad(date_flexibility=float("inf"))),
            ("return_flexibility", bad(round_trip=True, return_date="2026-06-15", return_date_flexibility=float("-inf"))),
        )
        for name, record in cases:
            for order in ("bad_first", "good_first"):
                with self.subTest(case=name, order=order):
                    records = [record, GOOD] if order == "bad_first" else [GOOD, record]
                    logs, processed, planned, failures, caught = self.run_batch(records)
                    self.assertIsNone(caught, "BATCH_NOT_ABORTED")
                    self.assertEqual((processed, planned), (["good"], ["good"]))
                    self.assertEqual([item[0] for item in failures], ["bad"])
                    self.assertTrue(failures[0][1].startswith("前置校验失败: OverflowError"), failures)
                    self.assertIn(PARTIAL_LINE, logs)


if __name__ == "__main__":
    unittest.main()
