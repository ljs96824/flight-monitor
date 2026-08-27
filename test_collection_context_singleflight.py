import json
import multiprocessing
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch


def _hold_collection_lock(lock_path, ready, release, result_queue):
    from collection_singleflight import acquire_collection_singleflight

    gate = acquire_collection_singleflight(
        "round-A",
        lock_path=lock_path,
        heartbeat_interval_seconds=0,
    )
    result_queue.put({"acquired": gate.acquired, "pid": gate.pid})
    ready.set()
    release.wait(10)
    gate.release()


class ObservationRoundContextTest(unittest.TestCase):
    def tearDown(self):
        from observations_store import clear_current_round

        clear_current_round()

    def test_interleaved_threads_write_to_their_own_round_context(self):
        from observations_store import (
            append_observations,
            get_current_round,
            reset_current_round,
            set_current_round,
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "observations.sqlite3"
            barrier = threading.Barrier(2)
            a_is_set = threading.Event()
            a_has_written = threading.Event()
            errors = []

            def write_current(combo):
                round_id, current_db_path = get_current_round()
                append_observations(
                    [{"flight_combo": combo, "price": 1000}],
                    round_id=round_id,
                    route_type="international",
                    origin_airport="PVG",
                    dest_airport="KIX",
                    depart_date="2099-10-01",
                    cabin_class="economy",
                    source="juhe",
                    observed_at="2099-09-01T10:00:00",
                    db_path=current_db_path,
                )

            def worker_a():
                tokens = set_current_round("round-A", db_path)
                try:
                    a_is_set.set()
                    barrier.wait(5)
                    write_current("MU1")
                    a_has_written.set()
                except Exception as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)
                    a_has_written.set()
                finally:
                    reset_current_round(tokens)

            def worker_b():
                a_is_set.wait(5)
                tokens = set_current_round("round-B", db_path)
                try:
                    barrier.wait(5)
                    a_has_written.wait(5)
                    write_current("MU2")
                except Exception as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)
                finally:
                    reset_current_round(tokens)

            threads = [threading.Thread(target=worker_a), threading.Thread(target=worker_b)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)

            self.assertFalse(errors)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            with closing(sqlite3.connect(db_path)) as connection:
                rows = connection.execute(
                    "SELECT flight_combo, round_id FROM observations ORDER BY flight_combo"
                ).fetchall()

        self.assertEqual(rows, [("MU1", "round-A"), ("MU2", "round-B")])

    def test_nested_token_reset_restores_outer_round(self):
        from observations_store import (
            DEFAULT_DB_PATH,
            get_current_round,
            reset_current_round,
            set_current_round,
        )

        outer = set_current_round("outer-round", Path("outer.sqlite3"))
        try:
            inner = set_current_round("inner-round", Path("inner.sqlite3"))
            reset_current_round(inner)
            self.assertEqual(get_current_round(), ("outer-round", Path("outer.sqlite3")))
        finally:
            reset_current_round(outer)

        self.assertEqual(get_current_round(), (None, DEFAULT_DB_PATH))


class CollectionSingleflightTest(unittest.TestCase):
    def test_busy_process_skips_before_request_round_and_usage_write(self):
        from collection_singleflight import acquire_collection_singleflight
        import main

        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            lock_path = root / "collection.lock"
            usage_path = root / "api_usage.json"
            usage_path.write_text(
                json.dumps({"version": 1, "dates": {}, "entries": []}),
                encoding="utf-8",
            )
            before_usage = usage_path.read_bytes()
            ready = context.Event()
            release = context.Event()
            result_queue = context.Queue()
            holder = context.Process(
                target=_hold_collection_lock,
                args=(str(lock_path), ready, release, result_queue),
            )
            holder.start()
            self.addCleanup(lambda: holder.is_alive() and holder.terminate())
            self.assertTrue(ready.wait(10))
            self.assertTrue(result_queue.get(timeout=5)["acquired"])

            start_round = Mock()
            collect = Mock()
            output = StringIO()
            with (
                redirect_stdout(output),
                patch("main.evaluate_subscription_preflight", return_value={"skip": False}),
                patch("main._make_round_id", return_value="round-B"),
                patch(
                    "main.acquire_collection_singleflight",
                    side_effect=lambda round_id: acquire_collection_singleflight(
                        round_id,
                        lock_path=lock_path,
                        heartbeat_interval_seconds=0,
                    ),
                ),
                patch("main.start_request_cache_round", start_round),
                patch("main.collect_for_airport_matrix", collect),
            ):
                ok = main.process_subscription(
                    {
                        "origin": "PVG",
                        "destination": "KIX",
                        "depart_date": "2099-10-01",
                        "round_trip": False,
                    },
                    ensure_db=False,
                )

            release.set()
            holder.join(10)

            self.assertEqual(ok["status"], "busy")
            self.assertEqual(ok["entrypoint"], "single_subscription")
            start_round.assert_not_called()
            collect.assert_not_called()
            self.assertEqual(usage_path.read_bytes(), before_usage)
            self.assertIn("[采集] 已有采集在运行,本次跳过(holder=", output.getvalue())
            self.assertIn("/round-A)", output.getvalue())

    def test_batch_entry_busy_skips_plan_and_request_round(self):
        import main

        busy_gate = Mock(acquired=False)
        start_round = Mock()
        build_plan = Mock()
        subscription = {
            "_index": 1,
            "origin": "PVG",
            "destination": "KIX",
            "depart_date": "2099-10-01",
            "round_trip": False,
        }
        with (
            patch("main.init_db"),
            patch("main.load_file_subscriptions", return_value=[subscription]),
            patch(
                "main.evaluate_subscription_preflight",
                return_value={
                    "skip": False,
                    "collection_dates": [datetime(2099, 10, 1).date()],
                },
            ),
            patch("main._make_collection_round_id", return_value="batch-B"),
            patch("main.acquire_collection_singleflight", return_value=busy_gate),
            patch("main.start_request_cache_round", start_round),
            patch("main.build_collection_plan", build_plan),
            patch("main._run_basket_sentinel_for_main"),
        ):
            result = main.run(sync_remote=False)

        self.assertEqual(result["status"], "busy")
        self.assertEqual(result["entrypoint"], "batch")
        start_round.assert_not_called()
        build_plan.assert_not_called()
        busy_gate.release.assert_not_called()

    def test_basket_entry_busy_skips_state_and_request_round(self):
        import basket_collect

        busy_gate = Mock(acquired=False)
        start_round = Mock()
        load_state = Mock()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            usage_path = root / "api_usage.json"
            usage_path.write_text(
                json.dumps({"version": 1, "dates": {}, "entries": []}),
                encoding="utf-8",
            )
            before_usage = usage_path.read_bytes()
            with (
                patch(
                    "basket_collect.load_collection_settings",
                    return_value={
                        "research_basket_enabled": True,
                        "research_basket_strategy": "cohort_v2",
                    },
                ),
                patch(
                    "basket_collect.acquire_collection_singleflight",
                    return_value=busy_gate,
                ),
                patch("basket_collect.start_request_cache_round", start_round),
                patch("basket_collect.load_or_create_state", load_state),
            ):
                summary = basket_collect.run_basket(
                    today=datetime(2099, 9, 1).date(),
                    now=datetime(2099, 9, 1, 9, 30),
                    state_path=root / "basket_state.json",
                    db_path=root / "observations.sqlite3",
                    usage_path=usage_path,
                    singleflight_lock_path=root / "collection.lock",
                )

            self.assertEqual(usage_path.read_bytes(), before_usage)

        self.assertTrue(summary["skipped"])
        self.assertEqual(summary["reason"], "singleflight_busy")
    def test_stale_unlocked_metadata_is_taken_over_and_logged(self):
        from collection_singleflight import acquire_collection_singleflight

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            lock_path = Path(tmp) / "collection.lock"
            stale_at = datetime.now(timezone.utc) - timedelta(minutes=31)
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": 123,
                        "round_id": "stale-round",
                        "heartbeat_at": stale_at.isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                gate = acquire_collection_singleflight(
                    "fresh-round",
                    lock_path=lock_path,
                    stale_after_seconds=1800,
                    heartbeat_interval_seconds=0,
                )
                try:
                    self.assertTrue(gate.acquired)
                finally:
                    gate.release()

        self.assertIn("[采集] 陈旧锁接管", output.getvalue())
        self.assertIn("stale-round", output.getvalue())


    def test_released_metadata_is_not_reported_as_stale_takeover(self):
        from collection_singleflight import acquire_collection_singleflight

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            lock_path = Path(tmp) / "collection.lock"
            released_at = datetime.now(timezone.utc) - timedelta(hours=2)
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": 123,
                        "round_id": "completed-round",
                        "heartbeat_at": released_at.isoformat(),
                        "released_at": released_at.isoformat(),
                        "state": "released",
                    }
                ),
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                gate = acquire_collection_singleflight(
                    "next-round",
                    lock_path=lock_path,
                    stale_after_seconds=1800,
                    heartbeat_interval_seconds=0,
                )
                self.assertTrue(gate.acquired)
                gate.release()

        self.assertNotIn("[采集] 陈旧锁接管", output.getvalue())
if __name__ == "__main__":
    unittest.main()
