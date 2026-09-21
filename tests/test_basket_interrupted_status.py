import ast
from contextlib import ExitStack
from copy import deepcopy
from datetime import date, datetime
import inspect
import io
import os
from pathlib import Path
import socket
import sys
import tempfile
import textwrap
from types import FunctionType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class BasketInterruptedStatusTest(unittest.TestCase):
    def run_case(self, scenario="normal", *, strategy="legacy", entry="locked", early=None, function=None):
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
            console, errors = io.StringIO(), io.StringIO()
            real_streams = (sys.stdout, sys.stderr)
            private_sys = SimpleNamespace(stdout=console, stderr=errors)
            stack.enter_context(patch.object(log_utils, "sys", private_sys))
            stack.enter_context(patch.object(log_utils, "_round_log_state", None))
            stack.enter_context(patch.object(log_utils, "print", lambda *a, **k: print(*a, file=private_sys.stdout, **k), create=True))
            failure = RuntimeError("SYNTHETIC_" + scenario.upper())
            settings = {"research_basket_enabled": early != "disabled", "research_basket_strategy": strategy,
                        "source_quota_budget": {"juhe": 100}, "source_quota_low_remaining_threshold": 1,
                        "freshness_hours": 6, "sub_round_fresh_scope": "primary_only"}
            state = {"revision": 1}
            route = {"route": "PVG->KIX", "origin": "PVG", "dest": "KIX", "route_type": "international", "sources": ("juhe",)}
            requests = [{**route, "depart_date": "2026-10-01", "queue": queue, "cabin_class": "economy"} for queue in ("A", "B")]
            report = SimpleNamespace(ledger_degraded=False, actual_requests=0, outcomes=())
            execute = Mock(side_effect=failure) if scenario == "execute_error" else Mock(return_value=report)
            plan = SimpleNamespace(request_keys=(), panel_only_keys=(), freshness_hours=6, fresh_scope="primary_only",
                                   log_summary=Mock(), execute=execute)
            aggregator = SimpleNamespace(last_outcome_reads=0, collect_from_outcomes=Mock(side_effect=[
                {"flights": [{"flight_combo": "SYNTHETIC"}]},
                {} if scenario in {"partial", "late_error"} else {"flights": [{"flight_combo": "SYNTHETIC"}]},
            ]))
            gate = SimpleNamespace(acquired=early != "busy", holder={"pid": 123, "round_id": "synthetic-holder", "heartbeat_at": "2026-09-21T12:00:00"}, release=Mock())
            persisted = []

            def persist(path, value, revision):
                self.assertEqual(path, root / "basket_state.json")
                persisted.append(deepcopy(value))
                return {**deepcopy(value), "revision": revision + 1}

            def safe_log(message):
                log_utils.safe_log(message)
                if scenario == "late_error" and message.startswith("[篮子结果复用]"):
                    raise failure

            replacements = {
                "load_collection_settings": Mock(return_value=settings), "load_or_create_state": Mock(return_value=state),
                "research_runtime_enabled": Mock(return_value=early != "runtime_blocked"),
                "_prepare_research_basket": Mock(return_value=(state, requests, {"ready": early != "gate_blocked"}, {})),
                "_persist_state": Mock(side_effect=persist), "apply_research_round_outcomes": Mock(return_value=[]),
                "renew_expired_queues": Mock(return_value=[]), "_basket_requests": Mock(return_value=requests),
                "reset_request_cache": Mock(), "start_request_cache_round": Mock(), "set_current_round": Mock(return_value=object()),
                "reset_current_round": Mock(), "build_collection_plan": Mock(return_value=plan),
                "activate_collection_plan": Mock(), "deactivate_collection_plan": Mock(),
                "load_usage_strict": Mock(return_value={}), "usage_snapshot": Mock(return_value={}),
                "_build_route_aggregator": Mock(return_value=aggregator), "count_observations_for_round": Mock(return_value=0),
                "print_request_cache_stats": Mock(), "log_retention_dry_run": Mock(),
                "acquire_collection_singleflight": Mock(return_value=gate), "_load_environment": Mock(),
                "configure_stdio_utf8": Mock(), "safe_log": safe_log,
            }
            for name, replacement in replacements.items():
                stack.enter_context(patch.object(basket, name, replacement))
            stack.enter_context(patch.object(basket, "BASKET_ROUTES", (route,)))
            start = stack.enter_context(patch.object(basket, "start_round_log_archive", wraps=log_utils.start_round_log_archive))
            end = stack.enter_context(patch.object(basket, "end_round_log_archive", wraps=log_utils.end_round_log_archive))
            factory = Mock(side_effect=AssertionError("REAL_SOURCE_FACTORY_FORBIDDEN"))
            kwargs = dict(today=date(2026, 9, 21), now=datetime(2026, 9, 21, 12), state_path=root / "basket_state.json",
                          db_path=root / "observations.sqlite3", usage_path=root / "api_usage.json",
                          source_builder=factory, aggregator_factory=factory, quota_guard_notifier=factory)
            result, error = None, None
            try:
                if entry == "cli":
                    original_run = basket.run_basket
                    stack.enter_context(patch.object(basket, "run_basket", side_effect=lambda **options: original_run(**kwargs, **options)))
                    result = basket.main([])
                elif entry == "public":
                    result = basket.run_basket(**kwargs)
                else:
                    result = (function or basket._run_basket_locked)(**kwargs, settings=settings)
            except Exception as exc:
                error = exc
            archive_path = root / "logs" / "rounds" / "20260921.log"
            archive = archive_path.read_text(encoding="utf-8") if archive_path.exists() else ""
            text = console.getvalue()
            observed = dict(result=result, error=error, expected_error=failure, archive=archive, output=text,
                end_statuses=[call.kwargs["status"] for call in end.call_args_list], start_calls=start.call_count,
                execute_calls=execute.call_count, collect_calls=aggregator.collect_from_outcomes.call_count,
                completed=[line for line in text.splitlines() if line.startswith("[篮子完成]")],
                state_reads=replacements["load_or_create_state"].call_count, persisted=persisted,
                release_calls=gate.release.call_count, acquire_calls=replacements["acquire_collection_singleflight"].call_count)
            self.assertIsNone(log_utils._round_log_state, "ARCHIVE_CLOSED")
            self.assertIs(sys.stdout, real_streams[0], "REAL_STDOUT_UNCHANGED")
            self.assertIs(sys.stderr, real_streams[1], "REAL_STDERR_UNCHANGED")
            forbidden.assert_not_called()
            factory.assert_not_called()
            console.close()
            errors.close()
            return observed

    def assert_case(self, scenario, *, strategy="legacy", function=None, check_status=True):
        row = self.run_case(scenario, strategy=strategy, function=function)
        interrupted = scenario in {"execute_error", "late_error"}
        self.assertIs(row["error"], row["expected_error"] if interrupted else None, "ORIGINAL_ERROR_PROPAGATES")
        if interrupted:
            self.assertEqual(type(row["error"]), RuntimeError)
            self.assertEqual(str(row["error"]), "SYNTHETIC_" + scenario.upper())
        expected = "interrupted" if interrupted else "partial" if scenario == "partial" else "ok"
        if check_status:
            self.assertEqual(row["end_statuses"], [expected], "END_STATUS")
            self.assertIn("status=" + expected + " =====", row["archive"], "WRITTEN_END_STATUS")
        queues, success, failed = (0, 0, 0) if scenario == "execute_error" else (2, 1, 1) if scenario in {"partial", "late_error"} else (2, 2, 0)
        self.assertEqual(row["completed"], [f"[篮子完成] 队列={queues} 成功={success} 失败={failed} 总写入=0"], "FAILED_COUNT")
        self.assertEqual(row["execute_calls"], 1)
        self.assertEqual(row["collect_calls"], queues)
        if not interrupted:
            self.assertEqual((row["result"]["queues"], row["result"]["success"], row["result"]["failed"]), (queues, success, failed))
            if strategy == "cohort_v2":
                self.assertEqual(row["result"]["status"], expected)
        return row

    def test_normal_body_finishes_ok(self):
        for strategy in ("legacy", "cohort_v2"):
            with self.subTest(strategy=strategy):
                self.assert_case("normal", strategy=strategy)

    def test_completed_partial_keeps_partial(self):
        for strategy in ("legacy", "cohort_v2"):
            with self.subTest(strategy=strategy):
                self.assert_case("partial", strategy=strategy)

    def test_execute_error_is_interrupted(self):
        for strategy in ("legacy", "cohort_v2"):
            with self.subTest(strategy=strategy):
                self.assert_case("execute_error", strategy=strategy)

    def test_late_error_is_interrupted(self):
        for strategy in ("legacy", "cohort_v2"):
            with self.subTest(strategy=strategy):
                self.assert_case("late_error", strategy=strategy)

    def test_interruption_does_not_invent_failed_queues(self):
        for scenario in ("execute_error", "late_error"):
            with self.subTest(scenario=scenario):
                self.assert_case(scenario, check_status=False)

    def test_real_cli_returns_one_for_original_errors(self):
        for scenario in ("execute_error", "late_error"):
            with self.subTest(scenario=scenario):
                row = self.run_case(scenario, entry="cli")
                self.assertIsNone(row["error"])
                self.assertEqual(row["result"], 1, "CLI_NONZERO")
                self.assertIn("[篮子失败] route=bootstrap 原因=SYNTHETIC_" + scenario.upper(), row["output"])
                self.assertEqual(row["execute_calls"], 1)
                self.assertEqual(row["release_calls"], 1)

    def test_real_cli_normal_and_partial_exit_codes(self):
        for scenario, expected in (("normal", 0), ("partial", 1)):
            with self.subTest(scenario=scenario):
                row = self.run_case(scenario, entry="cli")
                self.assertIsNone(row["error"])
                self.assertEqual(row["result"], expected, "CLI_COMPLETED_RESULT")
                self.assertEqual(row["execute_calls"], 1)
                self.assertEqual(row["release_calls"], 1)

    def test_runtime_blocked_keeps_early_output(self):
        row = self.run_case(strategy="cohort_v2", early="runtime_blocked")
        self.assertEqual(row["result"], {"status": "blocked", "reason": "research_runtime_disabled", "user_monitoring_enabled": True,
                         "round_id": "basket_20260921T120000", "queues": 0, "success": 0, "failed": 0, "written": 0})
        self.assertEqual(row["end_statuses"], ["blocked"])
        self.assertIn("[篮子跳过] 原因=研究采样运行态已停用,用户监控继续", row["output"])
        self.assertEqual((row["execute_calls"], row["completed"]), (0, []))

    def test_hard_gate_blocked_keeps_early_output(self):
        row = self.run_case(strategy="cohort_v2", early="gate_blocked")
        self.assertEqual(row["result"], {"status": "blocked", "reason": "research_hard_gate", "user_monitoring_enabled": True,
                         "round_id": "basket_20260921T120000", "queues": 0, "success": 0, "failed": 0, "written": 0})
        self.assertEqual(row["end_statuses"], ["blocked"])
        self.assertIn("[篮子跳过] 原因=研究采样硬门未通过", row["output"])
        self.assertEqual((row["execute_calls"], row["completed"]), (0, []))

    def test_busy_keeps_outer_early_output(self):
        row = self.run_case(entry="public", early="busy")
        self.assertEqual(row["result"], {"status": "busy", "holder_pid": 123, "holder_round_id": "synthetic-holder",
            "holder_heartbeat_at": "2026-09-21T12:00:00", "entrypoint": "basket", "round_id": "basket_20260921T120000",
            "queues": 0, "success": 0, "failed": 0, "written": 0, "skipped": True, "reason": "singleflight_busy"})
        self.assertIn("[采集状态] status=busy holder_pid=123 holder_round_id=synthetic-holder holder_heartbeat_at=2026-09-21T12:00:00 entrypoint=basket", row["output"])
        self.assertEqual((row["state_reads"], row["start_calls"], row["execute_calls"], row["release_calls"]), (0, 0, 0, 0))
        self.assertEqual((row["end_statuses"], row["completed"]), ([], []))

    def test_disabled_keeps_outer_early_output(self):
        row = self.run_case(entry="public", early="disabled")
        self.assertEqual(row["result"], {"status": "disabled", "reason": "research_basket_disabled", "actual_requests": 0,
            "round_id": "basket_20260921T120000", "queues": 0, "success": 0, "failed": 0, "written": 0, "skipped": True})
        self.assertIn("[篮子跳过] 原因=研究篮子已停用", row["output"])
        self.assertEqual((row["acquire_calls"], row["state_reads"], row["start_calls"], row["execute_calls"]), (0, 0, 0, 0))
        self.assertEqual((row["end_statuses"], row["completed"]), ([], []))

    def mutated(self, kind):
        import basket_collect
        source = textwrap.dedent(inspect.getsource(basket_collect._run_basket_locked))
        tree = ast.parse(source)
        original = ast.dump(tree)
        blocks = [node for node in tree.body[0].body if isinstance(node, ast.Try) and any(
            isinstance(child, ast.Assign) and ast.unparse(child.targets[0]) == "round_status" for child in node.finalbody)]
        self.assertEqual(len(blocks), 1, "MUTATION_FINALLY_MATCH")
        block = blocks[0]
        targets = [node for node in block.finalbody if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "round_status"]
        self.assertEqual(len(targets), 1, "MUTATION_STATUS_MATCH")
        target = targets[0]
        self.assertIsInstance(target.value, ast.IfExp)
        self.assertEqual(ast.unparse(target.value.test), "not body_completed", "MUTATION_GUARD_MATCH")
        self.assertEqual(target.value.body.value, "interrupted")
        if kind == "missing_completion":
            target.value = target.value.orelse
        elif kind == "invent_failure":
            block.finalbody.insert(0, ast.parse("if not body_completed:\n    failed += 1").body[0])
        elif kind == "interrupted_ok":
            target.value.body = ast.Constant(value="ok")
        else:
            self.fail("UNKNOWN_MUTATION")
        self.assertNotEqual(ast.dump(tree), original, "MUTATION_AST_CHANGED")
        changed = ast.unparse(ast.fix_missing_locations(tree))
        self.assertNotEqual(changed, ast.unparse(ast.parse(source)), "MUTATION_SOURCE_CHANGED")
        compiled = compile(changed, "<basket-status-mutation>", "exec")
        namespace = dict(basket_collect.__dict__)
        exec(compiled, namespace)
        original_function = namespace["_run_basket_locked"]
        result = FunctionType(original_function.__code__, basket_collect.__dict__, original_function.__name__)
        result.__kwdefaults__ = original_function.__kwdefaults__
        self.mutation_evidence = {"kind": kind, "matches": 1, "source_changed": True, "compiled": True}
        return result

    def test_mutation_missing_completion_is_rejected(self):
        changed = self.mutated("missing_completion")
        with self.assertRaisesRegex(AssertionError, "END_STATUS"):
            self.assert_case("execute_error", function=changed)

    def test_mutation_invented_failure_is_rejected(self):
        changed = self.mutated("invent_failure")
        with self.assertRaisesRegex(AssertionError, "FAILED_COUNT"):
            self.assert_case("execute_error", function=changed)

    def test_mutation_interrupted_ok_is_rejected(self):
        changed = self.mutated("interrupted_ok")
        for scenario in ("execute_error", "late_error"):
            with self.subTest(scenario=scenario):
                with self.assertRaisesRegex(AssertionError, "END_STATUS"):
                    self.assert_case(scenario, function=changed)


if __name__ == "__main__":
    unittest.main()
