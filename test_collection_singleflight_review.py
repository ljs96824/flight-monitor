import json
import multiprocessing
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID


def _hold_stale_collection_lock(lock_path, ready, result_queue):
    from collection_singleflight import acquire_collection_singleflight, _write_holder

    gate = acquire_collection_singleflight(
        "round-stale-holder",
        lock_path=lock_path,
        heartbeat_interval_seconds=0,
    )
    metadata = gate._metadata()
    metadata["heartbeat_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()
    _write_holder(gate._lock_file, metadata)
    result_queue.put({"acquired": gate.acquired, "pid": gate.pid})
    ready.set()
    while True:
        time.sleep(1)


class CollectionSingleflightSafetyReviewTest(unittest.TestCase):
    def test_active_os_lock_stays_busy_when_heartbeat_is_stale_then_recovers_after_kill(self):
        from collection_singleflight import acquire_collection_singleflight

        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            lock_path = Path(tmp) / "collection.lock"
            ready = context.Event()
            result_queue = context.Queue()
            holder = context.Process(
                target=_hold_stale_collection_lock,
                args=(str(lock_path), ready, result_queue),
            )
            holder.start()
            self.addCleanup(lambda: holder.is_alive() and holder.terminate())
            self.assertTrue(ready.wait(10))
            self.assertTrue(result_queue.get(timeout=5)["acquired"])

            contender = acquire_collection_singleflight(
                "round-contender",
                lock_path=lock_path,
                stale_after_seconds=30 * 60,
                heartbeat_interval_seconds=0,
            )
            self.assertFalse(contender.acquired)
            self.assertEqual(contender.holder.get("round_id"), "round-stale-holder")

            holder.terminate()
            holder.join(10)
            self.assertFalse(holder.is_alive())

            output = StringIO()
            replacement = None
            deadline = time.monotonic() + 5
            with redirect_stdout(output):
                while time.monotonic() < deadline:
                    replacement = acquire_collection_singleflight(
                        "round-after-kill",
                        lock_path=lock_path,
                        stale_after_seconds=30 * 60,
                        heartbeat_interval_seconds=0,
                    )
                    if replacement.acquired:
                        break
                    time.sleep(0.05)
            self.assertIsNotNone(replacement)
            self.assertTrue(replacement.acquired)
            replacement.release()

        self.assertIn("[采集] 陈旧锁接管", output.getvalue())

    def test_lease_mismatch_prevents_heartbeat_and_release_from_overwriting_holder(self):
        from collection_singleflight import (
            _read_holder_path,
            _write_holder,
            acquire_collection_singleflight,
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            lock_path = Path(tmp) / "collection.lock"
            gate = acquire_collection_singleflight(
                "round-lease-owner",
                lock_path=lock_path,
                heartbeat_interval_seconds=0,
            )
            original = _read_holder_path(lock_path)
            UUID(original["lease_id"])
            self.assertTrue(original["hostname"])

            foreign = dict(original)
            foreign.update(
                {
                    "lease_id": "foreign-lease",
                    "pid": 99999,
                    "round_id": "foreign-round",
                    "state": "running",
                }
            )
            _write_holder(gate._lock_file, foreign)

            output = StringIO()
            with redirect_stdout(output):
                self.assertFalse(gate.heartbeat())
                gate.release()

            after_release = _read_holder_path(lock_path)

        self.assertEqual(after_release["lease_id"], "foreign-lease")
        self.assertEqual(after_release["state"], "running")
        self.assertIn("租约不匹配", output.getvalue())

    def test_heartbeat_updates_the_held_fd_without_replacing_lock_path(self):
        from collection_singleflight import acquire_collection_singleflight

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            lock_path = Path(tmp) / "collection.lock"
            gate = acquire_collection_singleflight(
                "round-in-place",
                lock_path=lock_path,
                heartbeat_interval_seconds=0,
            )
            with patch("collection_singleflight.os.replace") as replace:
                self.assertTrue(gate.heartbeat())
                replace.assert_not_called()
            gate.release()

    def test_collection_lock_path_honors_config_and_primary_worktree(self):
        from collection_singleflight import resolve_collection_lock_path

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            primary = root / "primary"
            linked = root / "linked"
            git_dir = primary / ".git"
            worktree_git_dir = git_dir / "worktrees" / "review"
            worktree_git_dir.mkdir(parents=True)
            linked.mkdir()
            (linked / ".git").write_text(
                f"gitdir: {worktree_git_dir}\n",
                encoding="utf-8",
            )

            expected_default = (primary / "data" / "collection_singleflight.lock").resolve()
            self.assertEqual(
                resolve_collection_lock_path(base_dir=linked, environ={}),
                expected_default,
            )

            with patch.dict(os.environ, {}, clear=True), patch(
                "collection_singleflight.load_dotenv"
            ) as load_dotenv:
                self.assertEqual(
                    resolve_collection_lock_path(base_dir=linked),
                    expected_default,
                )
            load_dotenv.assert_called_once_with(
                primary / ".env",
                encoding="utf-8",
            )

            configured = root / "runtime" / "collection.lock"
            self.assertEqual(
                resolve_collection_lock_path(
                    base_dir=linked,
                    environ={"COLLECTION_LOCK_PATH": str(configured)},
                ),
                configured.resolve(),
            )


class CollectionBusySideEffectContractTest(unittest.TestCase):
    @staticmethod
    def _busy_gate():
        return SimpleNamespace(
            acquired=False,
            holder={
                "pid": 4321,
                "round_id": "holder-round",
                "heartbeat_at": "2099-01-02T03:04:05+00:00",
            },
        )

    def test_single_subscription_busy_is_distinct_and_has_zero_side_effects(self):
        import main

        effects = {
            "init_db": Mock(),
            "request_round": Mock(),
            "activate_plan": Mock(),
            "round_context": Mock(),
            "round_archive": Mock(),
            "collect": Mock(),
            "price_snapshot": Mock(),
            "flight_details": Mock(),
            "api_usage": Mock(),
            "observations": Mock(),
        }
        output = StringIO()
        with (
            redirect_stdout(output),
            patch("main.evaluate_subscription_preflight", return_value={"skip": False}),
            patch("main._make_round_id", return_value="contender-round"),
            patch("main.acquire_collection_singleflight", return_value=self._busy_gate()),
            patch("main.init_db", effects["init_db"]),
            patch("main.start_request_cache_round", effects["request_round"]),
            patch("main.activate_collection_plan", effects["activate_plan"]),
            patch("main.set_current_round", effects["round_context"]),
            patch("main.start_round_log_archive", effects["round_archive"]),
            patch("main.collect_for_airport_matrix", effects["collect"]),
            patch("main.save_roundtrip_snapshot", effects["price_snapshot"]),
            patch("main.save_flight_details", effects["flight_details"]),
            patch("api_usage.record_actual_requests", effects["api_usage"]),
            patch("observations_store.append_observations", effects["observations"]),
        ):
            result = main.process_subscription(
                {
                    "origin": "PVG",
                    "destination": "KIX",
                    "depart_date": "2099-10-01",
                    "round_trip": False,
                },
            )

        self.assertEqual(result["status"], "busy")
        self.assertEqual(result["entrypoint"], "single_subscription")
        for effect in effects.values():
            effect.assert_not_called()
        status_line = output.getvalue()
        self.assertIn("status=busy", status_line)
        self.assertIn("holder_pid=4321", status_line)
        self.assertIn("holder_round_id=holder-round", status_line)
        self.assertIn("holder_heartbeat_at=2099-01-02T03:04:05+00:00", status_line)
        self.assertIn("entrypoint=single_subscription", status_line)

    def test_batch_busy_stops_before_all_round_side_effects(self):
        import main

        effects = {
            "init_db": Mock(),
            "request_round": Mock(),
            "activate_plan": Mock(),
            "round_context": Mock(),
            "round_archive": Mock(),
            "build_plan": Mock(),
            "price_snapshot": Mock(),
            "flight_details": Mock(),
            "api_usage": Mock(),
            "observations": Mock(),
        }
        output = StringIO()
        with (
            redirect_stdout(output),
            patch("main._make_collection_round_id", return_value="batch-contender"),
            patch("main.acquire_collection_singleflight", return_value=self._busy_gate()),
            patch("main.init_db", effects["init_db"]),
            patch("main.start_request_cache_round", effects["request_round"]),
            patch("main.activate_collection_plan", effects["activate_plan"]),
            patch("main.set_current_round", effects["round_context"]),
            patch("main.start_round_log_archive", effects["round_archive"]),
            patch("main.build_collection_plan", effects["build_plan"]),
            patch("main.save_roundtrip_snapshot", effects["price_snapshot"]),
            patch("main.save_flight_details", effects["flight_details"]),
            patch("api_usage.record_actual_requests", effects["api_usage"]),
            patch("observations_store.append_observations", effects["observations"]),
        ):
            result = main.run(sync_remote=False)

        for effect in effects.values():
            effect.assert_not_called()
        self.assertEqual(result["status"], "busy")
        self.assertEqual(result["entrypoint"], "batch")
        self.assertIn("status=busy", output.getvalue())

    def test_basket_busy_does_not_mark_state_or_round_complete(self):
        import basket_collect

        effects = {
            "state_read": Mock(),
            "state_write": Mock(),
            "request_round": Mock(),
            "activate_plan": Mock(),
            "round_context": Mock(),
            "round_archive": Mock(),
            "round_end": Mock(),
            "api_usage": Mock(),
            "observations": Mock(),
        }
        output = StringIO()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            with (
                redirect_stdout(output),
                patch(
                    "basket_collect.load_collection_settings",
                    return_value={
                        "research_basket_enabled": True,
                        "research_basket_strategy": "cohort_v2",
                    },
                ),
                patch(
                    "basket_collect.acquire_collection_singleflight",
                    return_value=self._busy_gate(),
                ),
                patch("basket_collect.load_or_create_state", effects["state_read"]),
                patch("basket_collect._persist_state", effects["state_write"]),
                patch("basket_collect.start_request_cache_round", effects["request_round"]),
                patch("basket_collect.activate_collection_plan", effects["activate_plan"]),
                patch("basket_collect.set_current_round", effects["round_context"]),
                patch("basket_collect.start_round_log_archive", effects["round_archive"]),
                patch("basket_collect.end_round_log_archive", effects["round_end"]),
                patch("api_usage.record_actual_requests", effects["api_usage"]),
                patch("observations_store.append_observations", effects["observations"]),
            ):
                result = basket_collect.run_basket(
                    today=datetime(2099, 1, 1).date(),
                    now=datetime(2099, 1, 1, 9, 30),
                    state_path=root / "basket_state.json",
                    db_path=root / "observations.sqlite3",
                    usage_path=root / "api_usage.json",
                    singleflight_lock_path=root / "collection.lock",
                )

        for effect in effects.values():
            effect.assert_not_called()
        self.assertEqual(result["status"], "busy")
        self.assertEqual(result["entrypoint"], "basket")
        self.assertEqual(result["success"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertNotIn("[篮子完成]", output.getvalue())


if __name__ == "__main__":
    unittest.main()
