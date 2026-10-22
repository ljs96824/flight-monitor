"""R11：采集轮收尾逐步保护——任一收尾步骤失败，后续清理照常执行、轮档照常关闭。

基线(R10 合并后的 main)：三处收尾链都是顺序调用、互不保护——main._run_locked 的 finally(main.py:3185 起)、
main._process_subscription_locked 的 finally(main.py:2947 起)、basket_collect._run_basket_locked 的 finally(basket_collect.py:918 起)。
print_request_cache_stats() 末尾经 _flush_api_usage_ledger() 严格重读用量台账(request_cache.py:1115 起)，台账不可读即抛错；
它一抛错，哨兵、解除采集计划、恢复轮次上下文、日志保留演练与关闭轮档全部被跳过，轮档停在未关闭状态。
口径(用户裁定 2026-10-21，采纳建议)：每个收尾步骤单独保护，失败记一行 `[收尾失败] round_id=… 步骤=… 原因=类型:信息`，后续步骤照常执行；
关闭轮档时 ok 降为 partial(其余状态不变)；主体未抛错时，全部收尾做完后把第一个收尾异常原样抛出，保持原有失败出口与退出码；
主体已抛错时(含 KeyboardInterrupt、SystemExit，以及单条订阅的失败处理本身再抛错)原始异常照常传播，收尾异常只记日志、不覆盖它。
不重跑业务，不改准入与配额。
"""

from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from datetime import date, datetime
import io
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import round_cleanup
from round_cleanup import RoundCleanup


class RoundCleanupUnitTest(unittest.TestCase):
    def test_failed_step_is_logged_and_later_steps_still_run(self):
        calls = []
        lines = []
        cleanup = RoundCleanup("round-x")
        boom = OSError("ledger unreadable")
        with patch.object(round_cleanup, "safe_log", side_effect=lines.append):
            cleanup.step("第一步", Mock(side_effect=boom))
            result = cleanup.step("第二步", lambda value: calls.append(value) or "done", 7)
        self.assertEqual(result, "done")
        self.assertEqual(calls, [7])
        self.assertEqual(cleanup.failures, [("第一步", boom)])
        self.assertEqual(lines, ["[收尾失败] round_id=round-x 步骤=第一步 原因=OSError:ledger unreadable"])

    def test_status_downgrades_only_ok(self):
        clean, dirty = RoundCleanup("r"), RoundCleanup("r")
        with patch.object(round_cleanup, "safe_log"):
            dirty.step("x", Mock(side_effect=ValueError("bad")))
        for status in ("ok", "partial", "failed", "interrupted", "blocked"):
            with self.subTest(status=status):
                self.assertEqual(clean.status(status), status)
                self.assertEqual(dirty.status(status), "partial" if status == "ok" else status)

    def test_first_failure_is_raised_unchanged_and_only_when_present(self):
        cleanup = RoundCleanup("r")
        cleanup.raise_first_failure()
        first, second = OSError("first"), RuntimeError("second")
        with patch.object(round_cleanup, "safe_log"):
            cleanup.step("a", Mock(side_effect=first))
            cleanup.step("b", Mock(side_effect=second))
        with self.assertRaises(OSError) as caught:
            cleanup.raise_first_failure()
        self.assertIs(caught.exception, first)

    def test_broken_logger_does_not_break_cleanup_and_base_exceptions_pass_through(self):
        cleanup = RoundCleanup(None)
        with patch.object(round_cleanup, "safe_log", side_effect=RuntimeError("log down")):
            self.assertIsNone(cleanup.step("a", Mock(side_effect=OSError("x"))))
        self.assertEqual(len(cleanup.failures), 1)
        with self.assertRaises(KeyboardInterrupt):
            cleanup.step("b", Mock(side_effect=KeyboardInterrupt()))


class BatchRoundCleanupTest(unittest.TestCase):
    """main._run_locked：批量轮。"""

    STEPS = ("reset_current_round", "print_request_cache_stats", "_run_basket_sentinel_for_main",
             "deactivate_collection_plan", "log_retention_dry_run")

    def setUp(self):
        with patch("logging.basicConfig"), patch("dotenv.load_dotenv"):
            import main
        self.main = main

    def run_batch(self, *, failing=(), plan_error=None):
        events = []
        output = io.StringIO()
        plan = Mock(request_keys=set(), panel_only_keys=set(), freshness_hours=6, fresh_scope="primary_only")
        plan.execute.side_effect = plan_error
        errors = {name: OSError(f"synthetic {name}") for name in failing}

        def recorder(name):
            def record(*args, **kwargs):
                events.append(name)
                if name in errors:
                    raise errors[name]
            return record

        caught = None
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(output))
            for name in ("init_db", "sync_subscriptions", "activate_collection_plan", "start_request_cache_round",
                         "_log_subscription_failure", "_log_preflight_skip"):
                stack.enter_context(patch.object(self.main, name))
            stack.enter_context(patch.object(self.main, "set_current_round", return_value=object()))
            for name in self.STEPS:
                stack.enter_context(patch.object(self.main, name, side_effect=recorder(name)))
            stack.enter_context(patch.object(self.main.logging, "error"))
            stack.enter_context(patch.object(self.main.logging, "info"))
            stack.enter_context(patch.object(self.main, "_shanghai_today", return_value=date(2026, 1, 1)))
            stack.enter_context(patch.object(self.main, "load_file_subscriptions",
                                            return_value=[{"id": "synthetic-0"}]))
            stack.enter_context(patch.object(self.main, "evaluate_subscription_preflight", return_value={"skip": False}))
            stack.enter_context(patch.object(self.main, "_collection_plan_log_options", return_value={}))
            stack.enter_context(patch.object(self.main, "build_collection_plan", return_value=plan))
            stack.enter_context(patch.object(self.main, "_process_scheduled_subscription", return_value=True))
            stack.enter_context(patch.object(self.main, "start_round_log_archive"))
            stack.enter_context(patch.object(self.main, "end_round_log_archive",
                                            side_effect=lambda **kw: events.append(("end", kw["status"]))))
            try:
                self.main._run_locked(sync_remote=False, round_id="collection_synthetic")
            except BaseException as exc:
                caught = exc
        return events, output.getvalue(), caught, errors

    def test_clean_round_closes_ok_without_cleanup_failure(self):
        events, output, caught, _errors = self.run_batch()
        self.assertIsNone(caught)
        self.assertEqual(events, [*self.STEPS, ("end", "ok")])
        self.assertNotIn("[收尾失败]", output)

    def test_any_failed_step_still_runs_the_rest_and_resurfaces_after_closing(self):
        for name in self.STEPS:
            with self.subTest(step=name):
                events, output, caught, errors = self.run_batch(failing=(name,))
                self.assertEqual(events, [*self.STEPS, ("end", "partial")], "ALL_STEPS_AND_ARCHIVE_CLOSE")
                self.assertIs(caught, errors[name], "ORIGINAL_EXIT_PRESERVED")
                self.assertIn("[收尾失败] round_id=collection_synthetic", output)

    def test_ledger_failure_reports_step_name(self):
        _events, output, _caught, _errors = self.run_batch(failing=("print_request_cache_stats",))
        self.assertIn(
            "[收尾失败] round_id=collection_synthetic 步骤=请求统计与台账核对 原因=OSError:synthetic print_request_cache_stats",
            output,
        )

    def test_body_failure_keeps_original_exception_over_cleanup_failure(self):
        for primary in (RuntimeError("synthetic plan failure"), KeyboardInterrupt(), SystemExit(3)):
            with self.subTest(primary=type(primary).__name__):
                events, output, caught, _errors = self.run_batch(
                    failing=("print_request_cache_stats",), plan_error=primary)
                self.assertIs(caught, primary, "PRIMARY_EXCEPTION_WINS")
                self.assertEqual(events, [*self.STEPS, ("end", "failed")])
                self.assertIn("[收尾失败]", output)


class SingleSubscriptionCleanupTest(unittest.TestCase):
    """main._process_subscription_locked：单条订阅自管轮次。"""

    STEPS = ("print_request_cache_stats", "deactivate_collection_plan", "reset_current_round", "log_retention_dry_run")
    SUB = {"id": "00000000-0000-4000-8000-000000000111", "origin": "SHA", "destination": "OSA",
           "depart_date": "2026-12-01", "origin_airports": ["PVG"], "destination_airports": ["KIX"]}

    def setUp(self):
        with patch("logging.basicConfig"), patch("dotenv.load_dotenv"):
            import main
        self.main = main

    def run_single(self, *, failing=(), phase=True, handler_error=None):
        events = []
        output = io.StringIO()
        plan = Mock(request_keys=set(), panel_only_keys=set(), freshness_hours=6, fresh_scope="primary_only")
        errors = {name: OSError(f"synthetic {name}") for name in failing}

        def recorder(name):
            def record(*args, **kwargs):
                events.append(name)
                if name in errors:
                    raise errors[name]
            return record

        caught, returned = None, None
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(output))
            for name in ("get_constraint_epoch_boundary", "activate_collection_plan", "start_request_cache_round",
                         "start_round_log_archive", "FlightAggregator"):
                stack.enter_context(patch.object(self.main, name))
            stack.enter_context(patch.object(self.main, "_log_subscription_failure", side_effect=handler_error))
            stack.enter_context(patch.object(self.main, "get_constraint_history_limit", return_value=14))
            stack.enter_context(patch.object(self.main, "constraint_fingerprint", return_value="f" * 16))
            stack.enter_context(patch.object(self.main, "set_current_round", return_value=object()))
            stack.enter_context(patch.object(self.main, "_collection_plan_log_options", return_value={}))
            stack.enter_context(patch.object(self.main, "build_collection_plan", return_value=plan))
            stack.enter_context(patch.object(self.main, "build_default_sources", return_value=([], [])))
            stack.enter_context(patch.object(self.main, "_return_phase_context", return_value=object()))
            stack.enter_context(patch.object(
                self.main, "_process_return_phase",
                return_value=True if phase is True else None,
                side_effect=None if phase is True else phase,
            ))
            for name in self.STEPS:
                stack.enter_context(patch.object(self.main, name, side_effect=recorder(name)))
            stack.enter_context(patch.object(self.main, "end_round_log_archive",
                                            side_effect=lambda **kw: events.append(("end", kw["status"]))))
            stack.enter_context(patch.object(self.main.logging, "error"))
            stack.enter_context(patch.object(self.main.logging, "info"))
            try:
                returned = self.main._process_subscription_locked(
                    dict(self.SUB),
                    ensure_db=False,
                    preflight_result={"skip": False},
                    collection_round_id="single_synthetic",
                )
            except BaseException as exc:
                caught = exc
        return events, output.getvalue(), caught, returned, errors

    def test_clean_round_returns_result_and_closes_ok(self):
        events, output, caught, returned, _errors = self.run_single()
        self.assertIsNone(caught)
        self.assertIs(returned, True)
        self.assertEqual(events, [*self.STEPS, ("end", "ok")])
        self.assertNotIn("[收尾失败]", output)

    def test_failed_step_still_runs_the_rest_and_resurfaces_after_closing(self):
        for name in self.STEPS:
            with self.subTest(step=name):
                events, output, caught, _returned, errors = self.run_single(failing=(name,))
                self.assertEqual(events, [*self.STEPS, ("end", "partial")], "ALL_STEPS_AND_ARCHIVE_CLOSE")
                self.assertIs(caught, errors[name], "ORIGINAL_EXIT_PRESERVED")
                self.assertIn("[收尾失败] round_id=single_synthetic", output)

    def test_handled_body_failure_keeps_failed_status(self):
        events, _output, caught, _returned, errors = self.run_single(
            failing=("print_request_cache_stats",), phase=RuntimeError("synthetic phase failure"),
        )
        self.assertEqual(events, [*self.STEPS, ("end", "failed")])
        self.assertIs(caught, errors["print_request_cache_stats"])

    def test_exception_escaping_the_failure_handler_wins_over_cleanup_failure(self):
        for escaped in (KeyboardInterrupt(), OSError("synthetic failure-log error")):
            with self.subTest(escaped=type(escaped).__name__):
                events, output, caught, returned, _errors = self.run_single(
                    failing=("print_request_cache_stats",), phase=RuntimeError("synthetic phase failure"),
                    handler_error=escaped,
                )
                self.assertIs(caught, escaped, "HANDLER_EXCEPTION_WINS")
                self.assertIsNone(returned)
                self.assertEqual(events, [*self.STEPS, ("end", "failed")])
                self.assertIn("[收尾失败] round_id=single_synthetic", output)


class BasketRoundCleanupTest(unittest.TestCase):
    """basket_collect._run_basket_locked：篮子轮(隔离夹具沿用 tests/test_basket_interrupted_status.py 的做法)。"""

    def run_case(self, *, stats_error=None, execute_error=None, entry="locked"):
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"NO_LIVE_API": "1", "PYTHON_DOTENV_DISABLED": "1"}, clear=True))
            stack.enter_context(patch.dict(sys.modules, {"dotenv": SimpleNamespace(
                load_dotenv=lambda *a, **k: False, dotenv_values=lambda *a, **k: {})}))
            forbidden = Mock(side_effect=AssertionError("EXTERNAL_NETWORK_FORBIDDEN"))
            stack.enter_context(patch.object(socket.socket, "connect", forbidden))
            stack.enter_context(patch.object(socket.socket, "connect_ex", forbidden))
            import basket_collect as basket
            import log_utils

            root = Path(stack.enter_context(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
            console = io.StringIO()
            private_sys = SimpleNamespace(stdout=console, stderr=io.StringIO())
            stack.enter_context(patch.object(log_utils, "sys", private_sys))
            stack.enter_context(patch.object(log_utils, "_round_log_state", None))
            stack.enter_context(patch.object(
                log_utils, "print", lambda *a, **k: print(*a, file=private_sys.stdout, **k), create=True))
            settings = {"research_basket_enabled": True, "research_basket_strategy": "legacy",
                        "source_quota_budget": {"juhe": 100}, "source_quota_low_remaining_threshold": 1,
                        "freshness_hours": 6, "sub_round_fresh_scope": "primary_only"}
            state = {"revision": 1}
            route = {"route": "PVG->KIX", "origin": "PVG", "dest": "KIX", "route_type": "international",
                     "sources": ("juhe",)}
            requests = [{**route, "depart_date": "2026-10-01", "queue": queue, "cabin_class": "economy"}
                        for queue in ("A", "B")]
            report = SimpleNamespace(ledger_degraded=False, actual_requests=0, outcomes=())
            execute = Mock(side_effect=execute_error) if execute_error else Mock(return_value=report)
            plan = SimpleNamespace(request_keys=(), panel_only_keys=(), freshness_hours=6, fresh_scope="primary_only",
                                   log_summary=Mock(), execute=execute)
            aggregator = SimpleNamespace(last_outcome_reads=0, collect_from_outcomes=Mock(
                return_value={"flights": [{"flight_combo": "SYNTHETIC"}]}))
            gate = SimpleNamespace(acquired=True, holder={}, release=Mock())

            def persist(path, value, revision):
                return {**deepcopy(value), "revision": revision + 1}

            replacements = {
                "load_collection_settings": Mock(return_value=settings),
                "load_or_create_state": Mock(return_value=state),
                "research_runtime_enabled": Mock(return_value=True),
                "_prepare_research_basket": Mock(return_value=(state, requests, {"ready": True}, {})),
                "_persist_state": Mock(side_effect=persist), "apply_research_round_outcomes": Mock(return_value=[]),
                "renew_expired_queues": Mock(return_value=[]), "_basket_requests": Mock(return_value=requests),
                "reset_request_cache": Mock(), "start_request_cache_round": Mock(),
                "set_current_round": Mock(return_value=object()), "reset_current_round": Mock(),
                "build_collection_plan": Mock(return_value=plan), "activate_collection_plan": Mock(),
                "deactivate_collection_plan": Mock(), "load_usage_strict": Mock(return_value={}),
                "usage_snapshot": Mock(return_value={}), "_build_route_aggregator": Mock(return_value=aggregator),
                "count_observations_for_round": Mock(return_value=0),
                "print_request_cache_stats": Mock(side_effect=stats_error), "log_retention_dry_run": Mock(),
                "acquire_collection_singleflight": Mock(return_value=gate), "_load_environment": Mock(),
                "configure_stdio_utf8": Mock(),
            }
            for name, replacement in replacements.items():
                stack.enter_context(patch.object(basket, name, replacement))
            stack.enter_context(patch.object(basket, "BASKET_ROUTES", (route,)))
            end = stack.enter_context(patch.object(basket, "end_round_log_archive",
                                                  wraps=log_utils.end_round_log_archive))
            factory = Mock(side_effect=AssertionError("REAL_SOURCE_FACTORY_FORBIDDEN"))
            kwargs = dict(today=date(2026, 9, 21), now=datetime(2026, 9, 21, 12), state_path=root / "basket_state.json",
                          db_path=root / "observations.sqlite3", usage_path=root / "api_usage.json",
                          source_builder=factory, aggregator_factory=factory, quota_guard_notifier=factory)
            result, error = None, None
            try:
                if entry == "cli":
                    original_run = basket.run_basket
                    stack.enter_context(patch.object(
                        basket, "run_basket", side_effect=lambda **options: original_run(**kwargs, **options)))
                    result = basket.main([])
                else:
                    result = basket._run_basket_locked(**kwargs, settings=settings)
            except BaseException as exc:
                error = exc
            archive_path = root / "logs" / "rounds" / "20260921.log"
            archive = archive_path.read_text(encoding="utf-8") if archive_path.exists() else ""
            observed = dict(
                result=result, error=error, archive=archive, output=console.getvalue(),
                end_statuses=[item.kwargs["status"] for item in end.call_args_list],
                deactivate_calls=replacements["deactivate_collection_plan"].call_count,
                reset_calls=replacements["reset_current_round"].call_count,
                retention_calls=replacements["log_retention_dry_run"].call_count,
                archive_closed=log_utils._round_log_state is None,
            )
            forbidden.assert_not_called()
            factory.assert_not_called()
            return observed

    def test_stats_failure_still_closes_the_archive_as_partial(self):
        failure = OSError("synthetic ledger unreadable")
        row = self.run_case(stats_error=failure)
        self.assertIs(row["error"], failure, "ORIGINAL_EXIT_PRESERVED")
        self.assertEqual(row["end_statuses"], ["partial"])
        self.assertIn("status=partial =====", row["archive"])
        self.assertTrue(row["archive_closed"])
        self.assertEqual((row["deactivate_calls"], row["reset_calls"], row["retention_calls"]), (1, 1, 1))
        self.assertIn("[篮子完成] 队列=2 成功=2 失败=0 总写入=0", row["output"])
        self.assertIn("[收尾失败] round_id=basket_20260921T120000 步骤=请求统计与台账核对 原因=OSError:synthetic ledger unreadable",
                      row["output"])

    def test_interrupted_body_keeps_original_error_and_status(self):
        for primary in (RuntimeError("SYNTHETIC_EXECUTE_ERROR"), KeyboardInterrupt()):
            with self.subTest(primary=type(primary).__name__):
                row = self.run_case(stats_error=OSError("synthetic ledger unreadable"), execute_error=primary)
                self.assertIs(row["error"], primary, "PRIMARY_EXCEPTION_WINS")
                self.assertEqual(row["end_statuses"], ["interrupted"])
                self.assertTrue(row["archive_closed"])
                self.assertIn("[收尾失败]", row["output"])

    def test_cli_exit_code_is_unchanged(self):
        row = self.run_case(stats_error=OSError("synthetic ledger unreadable"), entry="cli")
        self.assertIsNone(row["error"])
        self.assertEqual(row["result"], 1, "CLI_NONZERO_AS_BEFORE")
        self.assertIn("[篮子失败] route=bootstrap 原因=synthetic ledger unreadable", row["output"])
        self.assertEqual(row["end_statuses"], ["partial"])
        self.assertTrue(row["archive_closed"])

    def test_clean_round_is_unchanged(self):
        row = self.run_case()
        self.assertIsNone(row["error"])
        self.assertEqual(row["end_statuses"], ["ok"])
        self.assertNotIn("[收尾失败]", row["output"])
        self.assertEqual(row["result"]["failed"], 0)


if __name__ == "__main__":
    unittest.main()
