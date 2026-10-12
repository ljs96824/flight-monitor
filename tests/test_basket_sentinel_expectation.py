"""F6：篮子哨兵按配置判断今日是否应有篮子台账。

口径与 basket_collect.run_basket 的配置/运行态跳过条件一致：
RESEARCH_BASKET_ENABLED=false → 不期待；cohort_v2 且研究运行态停用 → 不期待；其余（含配置或状态不可读）→ 期待，哨兵照常检查。
硬门、配额守卫、单飞锁等运行期阻断不在"按配置"范围内，仍按缺勤告警。
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import basket_collect
import main
from basket_sentinel import basket_run_expectation
from research_state_store import initialize_research_state


def _settings(enabled, strategy="cohort_v2"):
    return {"research_basket_enabled": enabled, "research_basket_strategy": strategy}


def _state(runtime_enabled):
    state = basket_collect.build_initial_state(date(2026, 10, 11))
    if runtime_enabled is not None:
        state["research_cohort_v2"] = {"runtime_enabled": runtime_enabled}
    return state


class Proceeded(Exception):
    pass


class BasketRunExpectationTest(unittest.TestCase):
    def test_predicate_matrix(self):
        cases = [
            (None, None, True, "settings_unavailable"),
            (_settings(False), _state(True), False, "research_basket_disabled"),
            (_settings(False, "legacy"), None, False, "research_basket_disabled"),
            (_settings(True), None, True, ""),
            (_settings(True), _state(None), True, ""),
            (_settings(True), _state(True), True, ""),
            (_settings(True), _state(False), False, "research_runtime_disabled"),
            (_settings(True, "legacy"), _state(False), True, ""),
        ]
        for settings, state, expected, reason in cases:
            with self.subTest(settings=settings, runtime=None if state is None else state.get("research_cohort_v2")):
                self.assertEqual(basket_run_expectation(settings, state), {"expected": expected, "reason": reason})

    def test_predicate_matches_run_basket_skip_gates(self):
        for enabled in (True, False):
            for strategy in ("cohort_v2", "legacy"):
                for runtime_enabled in (True, False, None):
                    with self.subTest(enabled=enabled, strategy=strategy, runtime=runtime_enabled):
                        with tempfile.TemporaryDirectory() as tmp:
                            root = Path(tmp)
                            state_path = root / "data" / "basket_state.json"
                            state_path.parent.mkdir()
                            initialize_research_state(state_path, _state(runtime_enabled))
                            settings = _settings(enabled, strategy)
                            stdout = io.StringIO()
                            with (
                                patch.object(basket_collect, "load_collection_settings", return_value=settings),
                                patch.object(basket_collect, "_prepare_research_basket", side_effect=Proceeded),
                                patch.object(basket_collect, "_basket_requests", side_effect=Proceeded),
                                patch.object(basket_collect, "renew_expired_queues", return_value=[]),
                                patch.object(basket_collect, "start_round_log_archive"),
                                patch.object(basket_collect, "end_round_log_archive"),
                                redirect_stdout(stdout),
                            ):
                                try:
                                    result = basket_collect.run_basket(
                                        today=date(2026, 10, 11), now=datetime(2026, 10, 11, 9, 30),
                                        state_path=state_path, db_path=root / "observations.sqlite3",
                                        usage_path=root / "api_usage.json",
                                        singleflight_lock_path=root / "collection.lock",
                                    )
                                    proceeded = False
                                except Proceeded:
                                    proceeded = True
                            if not proceeded:
                                self.assertIn(result["status"], {"disabled", "blocked"})
                            expectation = basket_run_expectation(settings, json.loads(state_path.read_text(encoding="utf-8")))
                            self.assertIs(expectation["expected"], proceeded)


class MainSentinelExpectationTest(unittest.TestCase):
    def test_paths_and_config_sources_match_basket(self):
        self.assertEqual(main.BASKET_STATE_PATH.resolve(), basket_collect.DEFAULT_STATE_PATH.resolve())
        self.assertEqual(Path(main.CONFIG_PATH).resolve(), Path(basket_collect.CONFIG_PATH).resolve())
        self.assertEqual(Path(main.RUNTIME_SETTINGS_PATH).resolve(), Path(basket_collect.RUNTIME_SETTINGS_PATH).resolve())

    def expectation_with(self, *, settings=None, settings_error=None, state=None, state_text=None):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "basket_state.json"
            if state is not None:
                initialize_research_state(state_path, state)
            elif state_text is not None:
                state_path.write_text(state_text, encoding="utf-8")
            load_kwargs = {"side_effect": settings_error} if settings_error else {"return_value": settings}
            stdout = io.StringIO()
            with (
                patch.object(main, "load_collection_settings", **load_kwargs),
                patch.object(main, "BASKET_STATE_PATH", state_path),
                redirect_stdout(stdout),
            ):
                return main._basket_run_expectation_for_main(), stdout.getvalue()

    def test_main_expectation_reads_config_then_state(self):
        result, log = self.expectation_with(settings_error=ValueError("bad yaml"), state=_state(False))
        self.assertEqual(result, {"expected": True, "reason": "settings_unavailable"})
        self.assertIn("[篮子哨兵] 篮子配置不可读,按应运行检查", log)
        result, _log = self.expectation_with(settings=_settings(False), state_text="{broken")
        self.assertEqual(result, {"expected": False, "reason": "research_basket_disabled"})
        result, log = self.expectation_with(settings=_settings(True), state_text="{broken")
        self.assertEqual(result, {"expected": True, "reason": ""})
        self.assertIn("[篮子哨兵] 篮子状态不可读,按应运行检查", log)
        result, _log = self.expectation_with(settings=_settings(True), state=_state(False))
        self.assertEqual(result, {"expected": False, "reason": "research_runtime_disabled"})
        result, _log = self.expectation_with(settings=_settings(True))
        self.assertEqual(result, {"expected": True, "reason": ""})

    def test_not_expected_skips_sentinel_and_notification(self):
        stdout = io.StringIO()
        with (
            patch.object(main, "_basket_run_expectation_for_main",
                         return_value={"expected": False, "reason": "research_basket_disabled"}),
            patch.object(main, "run_basket_sentinel") as sentinel,
            patch.object(main, "_notify_system_alert") as alert,
            redirect_stdout(stdout),
        ):
            result = main._run_basket_sentinel_for_main([{"id": "x"}], now=datetime(2026, 10, 11, 21, 0))
        self.assertEqual(result, {"due": False, "notified": False, "status": "not_expected",
                                  "reason": "research_basket_disabled"})
        sentinel.assert_not_called()
        alert.assert_not_called()
        self.assertIn("[篮子哨兵] 今日不要求篮子运行 原因=research_basket_disabled", stdout.getvalue())

    def test_expected_keeps_existing_sentinel(self):
        with (
            patch.object(main, "_basket_run_expectation_for_main", return_value={"expected": True, "reason": ""}),
            patch.object(main, "load_usage_strict", return_value={"entries": []}),
            patch.object(main, "run_basket_sentinel", return_value={"due": True, "notified": True}) as sentinel,
        ):
            result = main._run_basket_sentinel_for_main([], now=datetime(2026, 10, 11, 21, 0))
        self.assertEqual(result, {"due": True, "notified": True})
        sentinel.assert_called_once()
        self.assertEqual(sentinel.call_args.kwargs["state_path"], main.BASKET_SENTINEL_STATE_PATH)


if __name__ == "__main__":
    unittest.main()
