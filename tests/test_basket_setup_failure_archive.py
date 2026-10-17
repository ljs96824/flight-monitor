"""R4：篮子轮次在进入采集之前抛错时，轮档同样要收尾。

基线(7aa9742)的 _run_basket_locked 先开轮档(把 stdout/stderr 换成同时写轮档的流)，之后的读取配置、
读取篮子状态、研究准备中的非冲突异常、开启请求缓存轮次都不在任何收尾保护之内：任一步抛错，轮档不关闭、
标准流不恢复；命令行入口记一行 bootstrap 失败后返回 1，进程退出时 atexit 再以默认 status=completed
关闭轮档，失败的轮次被记成 completed。
口径：上述任一步抛错 → 先在轮档写一行 [篮子中断] 阶段=准备 原因=…，再以 interrupted 关闭轮档并恢复标准流，
原异常原样抛出；收尾本身失败只记一行，不覆盖原异常；不重建坏状态、不写状态文件、不进入采集。
"""

from contextlib import ExitStack
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


ROUND_ID = "basket_20260921T120000"
SETTINGS = {"research_basket_enabled": True, "research_basket_strategy": "cohort_v2",
            "source_quota_budget": {"juhe": 100}, "source_quota_low_remaining_threshold": 1,
            "freshness_hours": 6, "sub_round_fresh_scope": "primary_only"}
INTERRUPT_PREFIX = f"[篮子中断] round_id={ROUND_ID} 阶段=准备 原因="


def run_case(*, state_text=None, failures=None, pass_settings=True, entry="locked",
             close_error=None, start_error=None):
    """state_text 不为 None 时写入真实状态文件并走真实读取；failures 为 {被替换的名字: 要抛的异常}。"""
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
        state_path = root / "basket_state.json"
        console, errors = io.StringIO(), io.StringIO()
        real_streams = (sys.stdout, sys.stderr)
        private_sys = SimpleNamespace(stdout=console, stderr=errors)
        stack.enter_context(patch.object(log_utils, "sys", private_sys))
        stack.enter_context(patch.object(log_utils, "_round_log_state", None))
        stack.enter_context(patch.object(log_utils, "print",
                                         lambda *a, **k: print(*a, file=private_sys.stdout, **k), create=True))
        state = {"revision": 1}
        route = {"route": "PVG->KIX", "origin": "PVG", "dest": "KIX", "route_type": "international", "sources": ("juhe",)}
        requests = [{**route, "depart_date": "2026-10-01", "queue": "A", "cabin_class": "economy"}]
        execute = Mock(side_effect=AssertionError("BODY_MUST_NOT_RUN"))
        plan = SimpleNamespace(request_keys=(), panel_only_keys=(), freshness_hours=6, fresh_scope="primary_only",
                               log_summary=Mock(), execute=execute)
        gate = SimpleNamespace(acquired=True, holder={}, release=Mock())
        replacements = {
            "load_collection_settings": Mock(return_value=SETTINGS),
            "research_runtime_enabled": Mock(return_value=True),
            "_prepare_research_basket": Mock(return_value=(state, requests, {"ready": True}, {})),
            "_persist_state": Mock(side_effect=lambda path, value, revision: {**value, "revision": revision + 1}),
            "reset_request_cache": Mock(), "start_request_cache_round": Mock(),
            "set_current_round": Mock(return_value=object()), "reset_current_round": Mock(),
            "build_collection_plan": Mock(return_value=plan), "activate_collection_plan": Mock(),
            "deactivate_collection_plan": Mock(), "count_observations_for_round": Mock(return_value=0),
            "print_request_cache_stats": Mock(), "log_retention_dry_run": Mock(),
            "acquire_collection_singleflight": Mock(return_value=gate), "_load_environment": Mock(),
            "configure_stdio_utf8": Mock(),
        }
        if state_text is None:
            replacements["load_or_create_state"] = Mock(return_value=state)
        else:
            state_path.write_text(state_text, encoding="utf-8")
        for name, error in (failures or {}).items():
            replacements[name] = Mock(side_effect=error)
        for name, replacement in replacements.items():
            stack.enter_context(patch.object(basket, name, replacement))

        def start_archive(*args, **kwargs):
            if start_error is not None:
                raise start_error
            return log_utils.start_round_log_archive(*args, **kwargs)

        def end_archive(**kwargs):
            log_utils.end_round_log_archive(**kwargs)
            if close_error is not None:
                raise close_error

        stack.enter_context(patch.object(basket, "start_round_log_archive", side_effect=start_archive))
        end = stack.enter_context(patch.object(basket, "end_round_log_archive", side_effect=end_archive))
        factory = Mock(side_effect=AssertionError("REAL_SOURCE_FACTORY_FORBIDDEN"))
        kwargs = dict(today=date(2026, 9, 21), now=datetime(2026, 9, 21, 12), state_path=state_path,
                      db_path=root / "observations.sqlite3", usage_path=root / "api_usage.json",
                      source_builder=factory, aggregator_factory=factory, quota_guard_notifier=factory)
        if pass_settings:
            kwargs["settings"] = SETTINGS
        before = state_path.read_bytes() if state_path.exists() else None
        result, error = None, None
        try:
            try:
                if entry == "cli":
                    original_run = basket.run_basket
                    stack.enter_context(patch.object(basket, "run_basket",
                                                     side_effect=lambda **options: original_run(**kwargs, **options)))
                    result = basket.main([])
                else:
                    result = basket._run_basket_locked(**kwargs)
            except Exception as exc:
                error = exc
            # 先观察生产代码的收尾，再做测试自身的兜底清理。
            archive_path = root / "logs" / "rounds" / "20260921.log"
            row = dict(
                result=result, error=error, output=console.getvalue(),
                archive=archive_path.read_text(encoding="utf-8") if archive_path.exists() else "",
                end_statuses=[call.kwargs["status"] for call in end.call_args_list],
                closed=log_utils._round_log_state is None,
                streams_restored=(private_sys.stdout is console, private_sys.stderr is errors),
                before=before, after=state_path.read_bytes() if state_path.exists() else None,
                persist_calls=replacements["_persist_state"].call_count,
                build_calls=replacements["build_collection_plan"].call_count,
                execute_calls=execute.call_count, release_calls=gate.release.call_count,
            )
        finally:
            if log_utils._round_log_state is not None:
                log_utils.end_round_log_archive(status="test_cleanup")
        assert (sys.stdout, sys.stderr) == real_streams, "REAL_STREAMS_UNCHANGED"
        forbidden.assert_not_called()
        factory.assert_not_called()
        return row


class BasketSetupFailureArchiveTest(unittest.TestCase):
    def assert_closed_interrupted(self, row, error_type):
        self.assertIsInstance(row["error"], error_type, "ORIGINAL_ERROR_PROPAGATES")
        self.assertTrue(row["closed"], "ARCHIVE_CLOSED")
        self.assertEqual(row["streams_restored"], (True, True), "STDIO_RESTORED")
        self.assertEqual(row["end_statuses"], ["interrupted"], "END_STATUS")
        self.assertIn(f"{INTERRUPT_PREFIX}{type(row['error']).__name__}:{row['error']}", row["archive"],
                      "INTERRUPT_REASON_IN_ARCHIVE")
        self.assertTrue(row["archive"].rstrip().endswith("status=interrupted ====="), "WRITTEN_END_STATUS")
        self.assertEqual((row["build_calls"], row["execute_calls"]), (0, 0), "NO_COLLECTION")
        self.assertNotIn("[篮子完成]", row["output"])

    def test_mocked_setup_failures_close_archive(self):
        cases = (
            ("settings_unreadable", {"load_collection_settings": OSError("synthetic settings read failure")}, False),
            ("prepare_non_conflict_error", {"_prepare_research_basket": RuntimeError("synthetic prepare failure")}, True),
            ("cache_round_error", {"start_request_cache_round": OSError("synthetic usage ledger failure")}, True),
        )
        for name, failures, pass_settings in cases:
            with self.subTest(case=name):
                expected = next(iter(failures.values()))
                row = run_case(failures=failures, pass_settings=pass_settings)
                self.assertIs(row["error"], expected, "SAME_EXCEPTION_OBJECT")
                self.assert_closed_interrupted(row, type(expected))

    def test_unreadable_state_file_closes_archive_without_rewriting(self):
        from atomic_json_store import JsonStoreReadError

        cases = (
            ("not_an_object", "[]", JsonStoreReadError),
            ("invalid_revision", '{"version": 2, "revision": "abc", "routes": {}}', JsonStoreReadError),
            ("invalid_json", "{bad json", JsonStoreReadError),
            ("missing_routes", '{"revision": 1}', RuntimeError),
        )
        for name, text, error_type in cases:
            with self.subTest(case=name):
                row = run_case(state_text=text)
                self.assert_closed_interrupted(row, error_type)
                self.assertEqual(row["after"], row["before"], "STATE_FILE_UNCHANGED")
                self.assertEqual(row["persist_calls"], 0, "STATE_NOT_REBUILT")

    def test_close_failure_does_not_mask_original_error(self):
        original = RuntimeError("synthetic prepare failure")
        row = run_case(failures={"_prepare_research_basket": original}, close_error=RuntimeError("synthetic close failure"))
        self.assertIs(row["error"], original, "ORIGINAL_ERROR_KEPT")
        self.assertTrue(row["closed"])
        self.assertEqual(row["streams_restored"], (True, True))
        self.assertIn(f"[轮档失败] round_id={ROUND_ID} 关闭失败=RuntimeError:synthetic close failure", row["output"])

    def test_archive_not_started_skips_close(self):
        original = OSError("synthetic settings read failure")
        row = run_case(failures={"load_collection_settings": original}, pass_settings=False,
                       start_error=OSError("synthetic archive start failure"))
        self.assertIs(row["error"], original)
        self.assertEqual(row["end_statuses"], [], "NOTHING_TO_CLOSE")
        self.assertIn(f"[轮档失败] round_id={ROUND_ID} 原因=OSError:synthetic archive start failure", row["output"])
        self.assertNotIn(INTERRUPT_PREFIX, row["output"])

    def test_cli_closes_archive_as_interrupted_before_returning(self):
        row = run_case(state_text="[]", entry="cli", pass_settings=False)
        self.assertIsNone(row["error"])
        self.assertEqual(row["result"], 1, "CLI_NONZERO")
        self.assertIn("[篮子失败] route=bootstrap 原因=研究篮子状态根节点不是对象", row["output"])
        self.assertTrue(row["closed"], "CLOSED_BEFORE_EXIT_NOT_BY_ATEXIT")
        self.assertEqual(row["end_statuses"], ["interrupted"])
        self.assertEqual(row["release_calls"], 1)


if __name__ == "__main__":
    unittest.main()
