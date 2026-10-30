"""R20：研究入口遇到无法估算监控负载的订阅时，研究不准入(按订阅输入不可用处理)，不再以 bootstrap 失败结束，也不跳过坏记录。

基线(R19 合并后的 main)：basket_collect._load_active_subscriptions_for_research 只滤掉非对象、未启用与已过期的订阅，调用前置校验时
没有异常保护(日期弹性为 ±Infinity 时 OverflowError 穿出)；随后 _prepare_research_basket 把原始订阅交给
research_cohort.active_user_monitor_dates(启用机场字段为整数时 TypeError)与 _simulate_runtime_quota 里的 build_collection_plan
(舱位或机场为整数时 TypeError；国内航线的坏日期 ValueError、临近可表示范围边界的日期 OverflowError)。这些异常都不是
ResearchSubscriptionInputUnavailable，篮子入口以"[篮子失败] route=bootstrap"、退出码 1 结束。
口径(用户 2026-10-25"开工下一件"，按建议口径)：加载器对每条在用订阅按研究准备真正要做的三步各试算一次——前置校验、用户监控出发日、
订阅采集计划；任一步抛出 TypeError、ValueError 或 OverflowError 时，抛 ResearchSubscriptionInputUnavailable(只写文件、数组位置、
规范编号(subscription_id，兼容旧 id，repr 转义)、步骤与异常类型，不带原值)，沿用现有"订阅输入不可用"返回：研究门 ready=False、missing 为 subscription_input、complete=False、
guard_triggered=False、状态不推进，本轮以 blocked 结束。不跳过坏记录(跳过会低估用户监控负载)；能算出结果的写法照旧接受；
用户监控入口不在本笔。研究配额模拟脚本把它自己的 source_builder 交给加载器，试算与模拟用同一组搜索源(独立复核后补)。
"""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import date, datetime
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


TODAY = date(2026, 1, 1)
GOOD = {"id": "good", "status": "active", "origin": "PVG", "destination": "KIX", "depart_date": "2026-12-10"}
LATER_STEPS = (
    ("prepare_research_requests", SimpleNamespace(requests=[], events=[])),
    ("_simulate_runtime_quota", {"complete": True}),
    ("apply_research_quota_guard", {"triggered": False}),
    ("inspect_research_migrations", {}),
    ("load_backup_evidence", {}),
    ("evaluate_research_hard_gates", {"ready": True, "checks": {}, "missing": []}),
)


def bad(**fields):
    return {"id": "bad", "status": "active", "origin": "PVG", "destination": "KIX", "depart_date": "2026-12-12", **fields}


class _FakeSource:
    """不声明 supported_cabins，即支持全部舱位；只用于构建计划，不会被执行。"""

    name = "juhe"


def fake_sources(origin, dest, route_type=None):
    return [_FakeSource()], []


class _ResearchGuardCase(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"NO_LIVE_API": "1", "PYTHON_DOTENV_DISABLED": "1"}, clear=True))
        self.stack.enter_context(patch.dict(sys.modules, {"dotenv": SimpleNamespace(
            load_dotenv=lambda *a, **k: False, dotenv_values=lambda *a, **k: {})}))
        self.network = Mock(side_effect=AssertionError("EXTERNAL_NETWORK_FORBIDDEN"))
        self.stack.enter_context(patch.object(socket.socket, "connect", self.network))
        self.stack.enter_context(patch.object(socket.socket, "connect_ex", self.network))
        import basket_collect
        import log_utils
        import price_calendar
        self.basket = basket_collect
        self.logs = log_utils
        self.stack.enter_context(patch.object(price_calendar, "load_calendar", return_value={}))
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        self.path = self.root / "subscriptions.json"

    def tearDown(self):
        self.network.assert_not_called()

    def write(self, records):
        raw = json.dumps(records, ensure_ascii=False).encode("utf-8")
        self.path.write_bytes(raw)
        return raw


class ResearchLoadGuardLoaderTest(_ResearchGuardCase):
    WRONG = (
        ("cabin_int", {"cabin_classes": 1}, "订阅采集计划", TypeError),
        ("origin_active_int", {"origin_airports_active": 5}, "用户监控日期", TypeError),
        ("origin_all_int", {"origin_airports": 5}, "订阅采集计划", TypeError),
        ("infinite_flexibility", {"date_flexibility": float("inf")}, "前置校验", OverflowError),
        ("domestic_unparseable_date",
         {"destination": "PEK", "route_type": "domestic", "depart_date": "2026/12/12"}, "订阅采集计划", ValueError),
        ("domestic_date_beyond_range",
         {"destination": "PEK", "route_type": "domestic", "depart_date": "9999-12-25"}, "订阅采集计划", OverflowError),
    )

    def setUp(self):
        super().setUp()
        import sources.aggregator
        # 按加载器的缺省调用(不传 source_builder)验证；采集计划缺省取 build_default_sources，这里换成假搜索源以免依赖本机密钥。
        self.stack.enter_context(patch.object(sources.aggregator, "build_default_sources", side_effect=fake_sources))

    def load(self, records):
        raw = self.write(records)
        value, error = None, None
        with redirect_stdout(io.StringIO()):
            try:
                value = self.basket._load_active_subscriptions_for_research(self.path, today=TODAY)
            except Exception as exc:
                error = exc
        return value, error, self.path.read_bytes() == raw

    def test_unloadable_subscription_makes_input_unavailable_in_either_order(self):
        for name, fields, step, cause in self.WRONG:
            for order in ("bad_first", "good_first"):
                with self.subTest(case=name, order=order):
                    records = [bad(**fields), GOOD] if order == "bad_first" else [GOOD, bad(**fields)]
                    index = 0 if order == "bad_first" else 1
                    value, error, unchanged = self.load(records)
                    self.assertIsNone(value)
                    self.assertIsInstance(error, self.basket.ResearchSubscriptionInputUnavailable, "DEDICATED_EXCEPTION_TYPE")
                    self.assertIsInstance(error.__cause__, cause, "UNDERLYING_CAUSE")
                    message = str(error)
                    self.assertIn(str(self.path), message, "ERROR_PATH")
                    self.assertIn(f"订阅#{index} id='bad' 步骤={step} 原因={cause.__name__}", message)
                    self.assertNotIn("2026/12/12", message, "RAW_VALUE_NOT_IN_REASON")
                    self.assertTrue(unchanged, "INPUT_BYTES_UNCHANGED")

    def test_reason_uses_file_position_and_canonical_id(self):
        # 位置按订阅文件数组计(前面的暂停记录与非对象项照算)；编号取 subscription_id，旧记录才退回 id。
        paused = {**GOOD, "id": "paused", "status": "paused"}
        current = {key: value for key, value in bad(cabin_classes=1).items() if key != "id"}
        current["subscription_id"] = "sub-uuid-1"
        value, error, _unchanged = self.load([paused, 7, GOOD, current])
        self.assertIsNone(value)
        self.assertIn("订阅#3 id='sub-uuid-1' 步骤=订阅采集计划 原因=TypeError", str(error))

    def test_reason_escapes_control_characters_in_id(self):
        record = bad(cabin_classes=1, id="evil\n===== [轮档结束] round_id=forged status=ok =====")
        value, error, _unchanged = self.load([GOOD, record])
        message = str(error)
        self.assertIsNone(value)
        self.assertNotIn("\n", message, "NO_RAW_NEWLINE_IN_REASON")
        self.assertIn("id='evil\\n===== [轮档结束]", message)

    def test_computable_inputs_load_as_before(self):
        cases = (
            ("cabin_zero_falls_back", [bad(cabin_classes=0), GOOD], ["bad", "good"]),
            ("cabin_list_with_int", [bad(cabin_classes=["economy", 1]), GOOD], ["bad", "good"]),
            ("international_unparseable_date", [bad(depart_date="2026/12/12"), GOOD], ["bad", "good"]),
            ("paused_with_int_cabin", [bad(status="paused", cabin_classes=1), GOOD], ["good"]),
            ("expired_with_int_cabin", [bad(depart_date="2025-12-01", cabin_classes=1), GOOD], ["good"]),
            ("non_object_entry", [7, GOOD], ["good"]),
        )
        for name, records, expected in cases:
            with self.subTest(case=name):
                value, error, unchanged = self.load(records)
                self.assertIsNone(error)
                self.assertEqual([item["id"] for item in value], expected)
                self.assertTrue(unchanged)


class ResearchSimulationScriptSourceBuilderTest(_ResearchGuardCase):
    def test_simulation_hands_its_source_builder_to_the_loader(self):
        import scripts.research_quota_simulation as simulation

        class _Stop(Exception):
            pass

        loader = Mock(side_effect=_Stop)
        with patch.object(simulation, "load_collection_settings", return_value={}), \
                patch.object(simulation, "_load_active_subscriptions_for_research", loader):
            with self.assertRaises(_Stop):
                simulation._build_report_inputs(
                    today=TODAY, config_path=self.root / "config.yaml", state_path=self.root / "basket_state.json",
                    subscriptions_path=self.path, observations_path=self.root / "observations.sqlite3",
                    prices_path=self.root / "prices.db", usage_path=self.root / "api_usage.json",
                    source_builder=fake_sources,
                )
        self.assertEqual(loader.call_count, 1)
        self.assertIs(loader.call_args.kwargs.get("source_builder"), fake_sources, "SAME_SOURCES_FOR_PROBE_AND_SIMULATION")


class ResearchLoadGuardPreparationTest(_ResearchGuardCase):
    def test_unloadable_input_blocks_admission_without_later_steps(self):
        cases = (
            ("cabin_int", bad(cabin_classes=1), "订阅采集计划"),
            ("origin_active_int", bad(origin_airports_active=5), "用户监控日期"),
            ("infinite_flexibility", bad(date_flexibility=float("inf")), "前置校验"),
            # 只有用上传入的 source_builder 才会走到国内低价日历，借此确认准备阶段把它交给了加载器。
            ("domestic_unparseable_date",
             bad(destination="PEK", route_type="domestic", depart_date="2026/12/12"), "订阅采集计划"),
        )
        for name, record, step in cases:
            with self.subTest(case=name):
                self.write([GOOD, record])
                state = self.basket.build_initial_state(TODAY)
                before = deepcopy(state)
                output = io.StringIO()
                result, error = None, None
                with ExitStack() as stack:
                    spies = {step_name: stack.enter_context(patch.object(self.basket, step_name, return_value=value))
                             for step_name, value in LATER_STEPS}
                    stack.enter_context(redirect_stdout(output))
                    try:
                        result = self.basket._prepare_research_basket(
                            state, today=TODAY, state_path=self.root / "basket_state.json",
                            db_path=self.root / "observations.sqlite3", usage_path=self.root / "api_usage.json",
                            settings={}, source_builder=fake_sources,
                        )
                    except Exception as exc:
                        error = exc
                    counts = {step_name: spy.call_count for step_name, spy in spies.items()}
                self.assertIsNone(error, "NO_ESCAPE")
                returned_state, requests, gate, quota = result
                self.assertIs(gate["ready"], False, "INPUT_GATE")
                self.assertEqual(gate["missing"], ["subscription_input"])
                self.assertEqual(gate["checks"], {"subscription_input": False})
                self.assertIn(f"订阅#1 id='bad' 步骤={step}", gate["reasons"]["subscription_input"])
                self.assertEqual(quota, {"complete": False, "guard_triggered": False})
                self.assertIs(returned_state, state)
                self.assertEqual(state, before, "STATE_NOT_ADVANCED")
                self.assertEqual(requests, [])
                self.assertEqual(counts, dict.fromkeys(counts, 0), "NO_LATER_STEPS")
                self.assertEqual(sum("[研究输入缺失]" in line for line in output.getvalue().splitlines()), 1)


class ResearchLoadGuardLifecycleTest(_ResearchGuardCase):
    """真实 _run_basket_locked：坏输入让本轮以 blocked 结束并关闭轮档，状态文件字节不变，不执行采集。"""

    def test_unloadable_input_blocks_the_round_instead_of_failing_bootstrap(self):
        self.write([GOOD, bad(date_flexibility=float("inf"))])
        path = self.root / "basket_state.json"
        path.write_text(json.dumps(self.basket.build_initial_state(TODAY)), encoding="utf-8")
        before = path.read_bytes()
        console = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(console))
            stack.enter_context(redirect_stderr(io.StringIO()))
            stack.enter_context(patch.object(self.logs, "_round_log_state", None))
            for step_name, value in LATER_STEPS:
                stack.enter_context(patch.object(self.basket, step_name, return_value=value))
            execute = Mock(return_value=SimpleNamespace(ledger_degraded=False, actual_requests=0, outcomes=()))
            plan = SimpleNamespace(request_keys=(), panel_only_keys=(), freshness_hours=6,
                                   fresh_scope="primary_only", log_summary=Mock(), execute=execute)
            replacements = {
                "reset_request_cache": Mock(), "start_request_cache_round": Mock(),
                "set_current_round": Mock(), "reset_current_round": Mock(),
                "build_collection_plan": Mock(return_value=plan), "activate_collection_plan": Mock(),
                "deactivate_collection_plan": Mock(), "load_usage_strict": Mock(return_value={}),
                "usage_snapshot": Mock(return_value={}), "apply_research_round_outcomes": Mock(return_value=[]),
                "_build_route_aggregator": Mock(), "count_observations_for_round": Mock(return_value=0),
                "print_request_cache_stats": Mock(), "log_retention_dry_run": Mock(),
            }
            for name, replacement in replacements.items():
                stack.enter_context(patch.object(self.basket, name, replacement))
            forbidden = Mock(side_effect=AssertionError("SOURCE_OR_NOTIFICATION_FORBIDDEN"))
            result, error = None, None
            try:
                try:
                    result = self.basket._run_basket_locked(
                        today=TODAY, now=datetime(2026, 1, 1, 12), state_path=path,
                        db_path=self.root / "observations.sqlite3", usage_path=self.root / "api_usage.json",
                        source_builder=forbidden, aggregator_factory=forbidden, quota_guard_notifier=forbidden,
                        settings={"research_basket_strategy": "cohort_v2", "source_quota_budget": {"juhe": 1000},
                                  "source_quota_low_remaining_threshold": 1},
                    )
                except Exception as exc:
                    error = exc
                archive = (self.root / "logs" / "rounds" / "20260101.log").read_text(encoding="utf-8")
            finally:
                if self.logs._round_log_state is not None:
                    self.logs.end_round_log_archive(status="test_cleanup")
            forbidden.assert_not_called()
        checks = {
            "no_escape": error is None,
            "blocked": (result or {}).get("status") == "blocked",
            "input_log": "[研究输入缺失]" in console.getvalue() and "步骤=前置校验" in console.getvalue(),
            "end_marker": "status=blocked =====" in archive,
            "state_bytes": path.read_bytes() == before,
            "no_execute": execute.call_count == 0,
        }
        for name, passed in checks.items():
            with self.subTest(check=name):
                self.assertTrue(passed, name)


if __name__ == "__main__":
    unittest.main()
