import ast
from contextlib import ExitStack, redirect_stdout
from datetime import date
import inspect
import io
from pathlib import Path
import unittest
from unittest.mock import Mock, call, patch


class BatchRoundStatusTest(unittest.TestCase):
    def setUp(self):
        with patch("logging.basicConfig"), patch("dotenv.load_dotenv"):
            import main
        self.main = main

    def run_batch(self, outcomes, *, skipped=0, plan_error=None):
        events = []
        output = io.StringIO()
        subscriptions = [{"id": f"synthetic-{i}", "skip": i >= len(outcomes)}
                         for i in range(len(outcomes) + skipped)]
        by_id = {sub["id"]: outcome for sub, outcome in zip(subscriptions, outcomes)}

        def process(sub, **kwargs):
            events.append(("process", sub["id"]))
            outcome = by_id[sub["id"]]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def log(message):
            events.append(("log", message))

        plan = Mock(request_keys=set(), panel_only_keys=set(), freshness_hours=6,
                    fresh_scope="primary_only")
        plan.execute.side_effect = plan_error
        caught = None
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(output))
            for name in ("init_db", "sync_subscriptions", "activate_collection_plan",
                         "deactivate_collection_plan", "set_current_round", "reset_current_round",
                         "start_request_cache_round", "print_request_cache_stats",
                         "log_retention_dry_run", "_log_subscription_failure", "_log_preflight_skip"):
                stack.enter_context(patch.object(self.main, name))
            stack.enter_context(patch.object(self.main.logging, "error"))
            stack.enter_context(patch.object(self.main.logging, "info"))
            stack.enter_context(patch.object(self.main, "safe_log", side_effect=log))
            stack.enter_context(patch.object(self.main, "_shanghai_today", return_value=date(2026, 1, 1)))
            stack.enter_context(patch.object(self.main, "load_file_subscriptions", return_value=subscriptions))
            stack.enter_context(patch.object(self.main, "evaluate_subscription_preflight",
                                            side_effect=lambda sub, **kw: {"skip": sub["skip"]}))
            stack.enter_context(patch.object(self.main, "_collection_plan_log_options", return_value={}))
            build = stack.enter_context(patch.object(self.main, "build_collection_plan", return_value=plan))
            process_mock = stack.enter_context(patch.object(self.main, "_process_scheduled_subscription",
                                                            side_effect=process))
            start = stack.enter_context(patch.object(self.main, "start_round_log_archive",
                                                    side_effect=lambda *a, **k: events.append(("start", None))))
            end = stack.enter_context(patch.object(self.main, "end_round_log_archive",
                                                  side_effect=lambda **kw: events.append(("end", kw["status"]))))
            sentinel = stack.enter_context(patch.object(self.main, "_run_basket_sentinel_for_main"))
            try:
                returned = self.main._run_locked(sync_remote=False, round_id="collection_synthetic")
            except Exception as exc:
                caught = exc
                returned = None
        return dict(events=events, output=output.getvalue(), error=caught, returned=returned,
                    subscriptions=subscriptions, process=process_mock, start=start, end=end,
                    sentinel=sentinel, build=build, plan=plan)

    def assert_status(self, result, status):
        self.assertIsNone(result["error"], "UNEXPECTED_BATCH_EXCEPTION")
        self.assertEqual(result["end"].call_args, call(status=status), "ROUND_STATUS")
        self.assertIsNone(result["returned"], "BATCH_RETURN_UNCHANGED")

    def test_c1_all_true_ok(self):
        self.assert_status(self.run_batch([True, True]), "ok")

    def test_c2_mixed_returns_partial(self):
        self.assert_status(self.run_batch([True, False]), "partial")

    def test_c3_all_false_failed(self):
        self.assert_status(self.run_batch([False, False]), "failed")

    def test_c4_exception_continues_partial(self):
        result = self.run_batch([RuntimeError("synthetic processing failure"), True])
        self.assertEqual([c.args[0]["id"] for c in result["process"].call_args_list],
                         ["synthetic-0", "synthetic-1"], "CONTINUES_AFTER_EXCEPTION")
        self.assert_status(result, "partial")

    def test_c5_non_boolean_results_failed(self):
        self.assert_status(self.run_batch([{"status": "busy"}, None]), "failed")

    def test_c6_each_subscription_once(self):
        result = self.run_batch([True, False, RuntimeError("synthetic"), {"status": "busy"}, None])
        self.assertIsNone(result["error"], "UNEXPECTED_BATCH_EXCEPTION")
        self.assertEqual([c.args[0]["id"] for c in result["process"].call_args_list],
                         [f"synthetic-{i}" for i in range(5)], "EXACTLY_ONCE_NO_RETRY")
        for invocation in result["process"].call_args_list:
            self.assertEqual(invocation.kwargs,
                             {"preflight": {"skip": False}, "round_id": "collection_synthetic"})

    def test_c7_plan_exception_preserved(self):
        original = RuntimeError("synthetic plan failure")
        result = self.run_batch([True], plan_error=original)
        self.assertIs(result["error"], original)
        result["end"].assert_called_once_with(status="failed")
        result["process"].assert_not_called()
        result["sentinel"].assert_called_once_with(result["subscriptions"])

    def test_c8_preflight_skips_excluded(self):
        result = self.run_batch([True], skipped=2)
        self.assert_status(result, "ok")
        self.assertEqual(result["process"].call_count, 1)
        self.assertEqual(result["build"].call_args.kwargs["subscriptions"], result["subscriptions"][:1])
        empty = self.run_batch([], skipped=2)
        self.assertIsNone(empty["error"])
        empty["process"].assert_not_called()
        empty["start"].assert_not_called()
        empty["end"].assert_not_called()
        empty["build"].assert_not_called()
        empty["sentinel"].assert_called_once_with(empty["subscriptions"])
        self.assertIn("[订阅前置校验] 本轮检查=2 跳过=2", [e[1] for e in empty["events"] if e[0] == "log"])

    def test_c9_summary_before_close(self):
        for outcomes, skipped, counts, status in (
            ([True, True], 0, (2, 0, 0), "ok"),
            ([False, False], 0, (0, 2, 0), "failed"),
            ([True, False, RuntimeError("synthetic"), None, {"status": "busy"}], 2, (1, 4, 1), "partial"),
        ):
            with self.subTest(status=status):
                result = self.run_batch(outcomes, skipped=skipped)
                success, failed, exceptions = counts
                expected = (f"[批次结果] 订阅处理 成功={success} 失败={failed}"
                            f"(其中异常={exceptions}) 前置跳过={skipped} status={status}")
                summaries = [e for e in result["events"] if e[0] == "log" and e[1].startswith("[批次结果]")]
                self.assertEqual(summaries, [("log", expected)], "SUMMARY_COUNTS")
                self.assertLess(result["events"].index(("start", None)), result["events"].index(summaries[0]))
                self.assertLess(result["events"].index(summaries[0]), result["events"].index(("end", status)))
                self.assertNotIn("通知", summaries[0][1])
                self.assertNotIn("推送", summaries[0][1])

    def test_c10_sentinel_and_close_once(self):
        for outcomes in ([True], [False], [RuntimeError("synthetic"), True], [None]):
            with self.subTest(outcomes=outcomes):
                result = self.run_batch(outcomes)
                result["start"].assert_called_once()
                result["end"].assert_called_once()
                result["sentinel"].assert_called_once_with(result["subscriptions"])


MUTATION_TARGETS = {
    "m1": [("test_c2_mixed_returns_partial", "ROUND_STATUS"), ("test_c3_all_false_failed", "ROUND_STATUS")],
    "m2": [("test_c4_exception_continues_partial", "ROUND_STATUS")],
    "m3": [("test_c2_mixed_returns_partial", "ROUND_STATUS")],
    "m4": [("test_c6_each_subscription_once", "EXACTLY_ONCE_NO_RETRY")],
    "m5": [("test_c5_non_boolean_results_failed", "ROUND_STATUS")],
}


def _mutated_run(main, kind):
    source = inspect.getsource(main._run_locked)
    tree = ast.parse(source)
    original = ast.dump(tree, include_attributes=False)
    matches = 0
    for node in list(ast.walk(tree)):
        if kind in {"m1", "m3"} and isinstance(node, ast.Assign):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "round_status" and isinstance(node.value, ast.IfExp):
                matches += 1
                node.value = ast.parse("'ok'" if kind == "m1" else "'failed' if processed_failed else 'ok'", mode="eval").body
        elif kind == "m2" and isinstance(node, ast.ExceptHandler):
            failures = [n for n in node.body if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and n.target.id == "processed_failed"]
            for assignment in failures:
                matches += 1
                assignment.target.id = "processed_success"
        elif kind in {"m4", "m5"} and isinstance(node, ast.If) and ast.unparse(node.test) == "result is True":
            matches += 1
            if kind == "m5":
                node.test = ast.Name(id="result", ctx=ast.Load())
            else:
                node.orelse.append(ast.parse("_process_scheduled_subscription(sub, preflight=preflight, round_id=round_id)").body[0])
    if matches != 1:
        raise AssertionError(f"MUTATION_NODE_MATCH:{kind}:{matches}")
    if ast.dump(tree, include_attributes=False) == original:
        raise AssertionError("MUTATION_SOURCE_UNCHANGED")
    code = compile(ast.fix_missing_locations(tree), str(Path(main.__file__)), "exec")
    namespace = dict(main.__dict__)
    exec(code, namespace)
    # Use live module bindings so the same isolated processing-chain stubs apply.
    import types
    return types.FunctionType(namespace["_run_locked"].__code__, main.__dict__)


class BatchRoundStatusMutationTest(unittest.TestCase):
    def test_c_mutations_hit_target_assertions(self):
        with patch("logging.basicConfig"), patch("dotenv.load_dotenv"):
            import main
        for kind, targets in MUTATION_TARGETS.items():
            with self.subTest(mutation=kind):
                mutated = _mutated_run(main, kind)
                for method, marker in targets:
                    with patch.object(main, "_run_locked", mutated):
                        result = unittest.TestResult()
                        BatchRoundStatusTest(method).run(result)
                    self.assertEqual(result.testsRun, 1)
                    self.assertEqual(result.errors, [], "MUTATION_UNRELATED_ERROR")
                    self.assertEqual(len(result.failures), 1, "MUTATION_TARGET_NOT_KILLED")
                    self.assertIn(marker, result.failures[0][1], "MUTATION_WRONG_ASSERTION")
                    self.assertEqual(result.failures[0][0]._testMethodName, method)


if __name__ == "__main__":
    unittest.main()
