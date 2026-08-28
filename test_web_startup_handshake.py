import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import main
import web_form
from local_file_lock import file_lock
from subscription_attempts import attempt_time, record_subscription_attempt


TEST_SUBSCRIPTION_ID = "123e4567-e89b-12d3-a456-426614174088"


def _subscription():
    return {
        "subscription_id": TEST_SUBSCRIPTION_ID,
        "origin": "PVG",
        "destination": "KIX",
        "depart_date": "2099-10-01",
        "round_trip": False,
    }


class MainStartupCallbackContractTest(unittest.TestCase):
    def test_busy_reports_holder_before_any_collection_side_effect(self):
        callback = Mock()
        gate = SimpleNamespace(
            acquired=False,
            holder={
                "pid": 4321,
                "round_id": "holder-round",
                "heartbeat_at": "2099-01-02T03:04:05+00:00",
            },
        )
        start_round = Mock()

        with (
            patch("main.evaluate_subscription_preflight", return_value={"skip": False}),
            patch("main._make_round_id", return_value="contender-round"),
            patch("main.acquire_collection_singleflight", return_value=gate),
            patch("main.start_request_cache_round", start_round),
        ):
            result = main.process_subscription(
                _subscription(),
                startup_callback=callback,
            )

        self.assertEqual(result["status"], "busy")
        callback.assert_called_once()
        reported = callback.call_args.args[0]
        self.assertEqual(reported["status"], "busy")
        self.assertEqual(reported["holder_round_id"], "holder-round")
        start_round.assert_not_called()

    def test_acquired_lock_reports_started_before_locked_processing(self):
        events = []
        gate = SimpleNamespace(acquired=True, holder={}, release=lambda: events.append("release"))

        with (
            patch("main.evaluate_subscription_preflight", return_value={"skip": False}),
            patch("main._make_round_id", return_value="started-round"),
            patch("main.acquire_collection_singleflight", return_value=gate),
            patch(
                "main._process_subscription_locked",
                side_effect=lambda *args, **kwargs: events.append("process") or True,
            ),
        ):
            result = main.process_subscription(
                _subscription(),
                startup_callback=lambda payload: events.append(payload["status"]),
            )

        self.assertTrue(result)
        self.assertEqual(events, ["started", "process", "release"])


class ScheduledAttemptRecoveryContractTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmpdir.name) / "subscriptions.json"
        self.old_main_path = main.SUBSCRIPTIONS_PATH
        main.SUBSCRIPTIONS_PATH = self.path
        self.path.write_text(
            json.dumps(
                [
                    {
                        **_subscription(),
                        "last_attempt": {
                            "status": "busy",
                            "at": "2099-01-01T00:00:00+00:00",
                            "holder_round_id": "old-round",
                            "entrypoint": "web",
                        },
                    }
                ]
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        main.SUBSCRIPTIONS_PATH = self.old_main_path
        self.tmpdir.cleanup()

    def test_scheduled_success_overwrites_previous_busy_status(self):
        def fake_process(_subscription, **kwargs):
            kwargs["startup_callback"](
                {
                    "status": "started",
                    "holder_round_id": "scheduled-round",
                    "entrypoint": "batch",
                }
            )
            return True

        with patch.object(main, "process_subscription", side_effect=fake_process):
            result = main._process_scheduled_subscription(
                _subscription(),
                preflight={"skip": False},
                round_id="scheduled-round",
            )

        stored = json.loads(self.path.read_text(encoding="utf-8"))[0]["last_attempt"]
        self.assertTrue(result)
        self.assertEqual(stored["status"], "success")
        self.assertEqual(stored["holder_round_id"], "scheduled-round")
        self.assertEqual(stored["entrypoint"], "batch")

    def test_older_busy_cannot_overwrite_newer_success(self):
        self.assertTrue(
            record_subscription_attempt(
                _subscription(),
                status="success",
                holder_round_id="new-round",
                entrypoint="batch",
                at="2099-01-02T00:00:00+00:00",
                path=self.path,
            )
        )

        updated = record_subscription_attempt(
            _subscription(),
            status="busy",
            holder_round_id="old-round",
            entrypoint="web",
            at="2099-01-01T00:00:00+00:00",
            path=self.path,
        )

        stored = json.loads(self.path.read_text(encoding="utf-8"))[0]["last_attempt"]
        self.assertFalse(updated)
        self.assertEqual(stored["status"], "success")
        self.assertEqual(stored["at"], "2099-01-02T00:00:00.000000+00:00")
        self.assertEqual(stored["holder_round_id"], "new-round")

    def test_naive_attempt_timestamp_is_interpreted_as_utc(self):
        self.assertEqual(
            attempt_time("2099-01-01T08:00:00"),
            "2099-01-01T08:00:00.000000+00:00",
        )

    def test_batch_status_store_failure_does_not_change_successful_result(self):
        def fake_process(_subscription, **kwargs):
            main._report_collection_startup(
                kwargs["startup_callback"],
                {
                    "status": "started",
                    "holder_round_id": "scheduled-round",
                    "entrypoint": "batch",
                },
            )
            return True

        with (
            patch.object(main, "process_subscription", side_effect=fake_process),
            patch.object(
                main,
                "record_subscription_attempt",
                side_effect=RuntimeError("status-store-failed"),
            ),
        ):
            result = main._process_scheduled_subscription(
                _subscription(),
                preflight={"skip": False},
                round_id="scheduled-round",
            )

        self.assertTrue(result)


class WebStartupHandshakeContractTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmpdir.name) / "subscriptions.json"
        self.old_path = web_form.SUBSCRIPTIONS_PATH
        web_form.SUBSCRIPTIONS_PATH = self.path
        self.path.write_text(json.dumps([_subscription()]), encoding="utf-8")

    def tearDown(self):
        web_form.SUBSCRIPTIONS_PATH = self.old_path
        self.tmpdir.cleanup()

    def _last_attempt(self):
        return web_form.load_subscriptions()[0]["last_attempt"]

    def test_background_busy_is_reported_and_persisted_without_success_failure_alias(self):
        callback_result = {
            "status": "busy",
            "holder_round_id": "holder-round",
            "entrypoint": "single_subscription",
        }

        def fake_process(_subscription, **kwargs):
            kwargs["startup_callback"](callback_result)
            return {"status": "busy"}

        with (
            patch.object(main, "_normalize_subscription", side_effect=lambda value: value),
            patch.object(main, "process_subscription", side_effect=fake_process),
        ):
            result = web_form.start_background_collection(
                _subscription(),
                timeout_seconds=1,
            )

        self.assertEqual(result["status"], "busy")
        deadline = time.monotonic() + 2
        attempt = {}
        while time.monotonic() < deadline:
            attempt = web_form.load_subscriptions()[0].get("last_attempt") or {}
            if attempt.get("status") == "busy":
                break
            time.sleep(0.01)
        self.assertEqual(attempt["status"], "busy")
        self.assertEqual(attempt["holder_round_id"], "holder-round")
        self.assertNotIn(attempt["status"], {"success", "failed"})

    def test_started_then_completed_overwrites_previous_busy_status(self):
        self.path.write_text(
            json.dumps(
                [
                    {
                        **_subscription(),
                        "last_attempt": {
                            "status": "busy",
                            "at": "2099-01-01T00:00:00+00:00",
                            "holder_round_id": "old-round",
                            "entrypoint": "web",
                        },
                    }
                ]
            ),
            encoding="utf-8",
        )

        def fake_process(_subscription, **kwargs):
            kwargs["startup_callback"](
                {
                    "status": "started",
                    "holder_round_id": "new-round",
                    "entrypoint": "single_subscription",
                }
            )
            return True

        with (
            patch.object(main, "_normalize_subscription", side_effect=lambda value: value),
            patch.object(main, "process_subscription", side_effect=fake_process),
        ):
            result = web_form.start_background_collection(
                _subscription(),
                timeout_seconds=1,
            )
            deadline = time.monotonic() + 2
            while self._last_attempt()["status"] != "success" and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertEqual(result["status"], "started")
        self.assertEqual(self._last_attempt()["status"], "success")
        self.assertEqual(self._last_attempt()["holder_round_id"], "new-round")

    def test_web_status_store_failure_does_not_block_started_result_or_success(self):
        startup_results = queue.Queue(maxsize=1)

        def fake_process(_subscription, **kwargs):
            main._report_collection_startup(
                kwargs["startup_callback"],
                {
                    "status": "started",
                    "holder_round_id": "new-round",
                    "entrypoint": "single_subscription",
                },
            )
            return True

        with (
            patch.object(main, "_normalize_subscription", side_effect=lambda value: value),
            patch.object(main, "process_subscription", side_effect=fake_process),
            patch.object(
                web_form,
                "record_last_attempt",
                side_effect=RuntimeError("status-store-failed"),
            ),
        ):
            web_form.run_single_subscription(_subscription(), startup_results)

        result = startup_results.get_nowait()
        self.assertEqual(result["status"], "started")
        self.assertFalse(result["persisted"])

    def test_slow_status_store_cannot_delay_started_queue(self):
        startup_results = queue.Queue(maxsize=1)
        store_entered = threading.Event()
        release_store = threading.Event()

        def fake_process(_subscription, **kwargs):
            kwargs["startup_callback"](
                {
                    "status": "started",
                    "holder_round_id": "new-round",
                    "entrypoint": "single_subscription",
                }
            )
            return True

        def slow_store(*_args, **_kwargs):
            store_entered.set()
            release_store.wait(timeout=1)
            return True

        worker = threading.Thread(
            target=web_form.run_single_subscription,
            args=(_subscription(), startup_results),
        )
        try:
            with (
                patch.object(main, "_normalize_subscription", side_effect=lambda value: value),
                patch.object(main, "process_subscription", side_effect=fake_process),
                patch.object(web_form, "record_last_attempt", side_effect=slow_store),
            ):
                worker.start()
                self.assertTrue(store_entered.wait(timeout=1))
                result = startup_results.get(timeout=0.05)
        finally:
            release_store.set()
            worker.join(timeout=2)

        self.assertEqual(result["status"], "started")
    def test_timeout_persists_confirming_without_false_time_promise(self):
        release = threading.Event()

        def slow_runner(_subscription, startup_queue=None):
            release.wait(1)

        try:
            with patch.object(web_form, "run_single_subscription", side_effect=slow_runner):
                result = web_form.start_background_collection(
                    _subscription(),
                    timeout_seconds=0.01,
                )
            self.assertEqual(result["status"], "confirming")
            self.assertEqual(self._last_attempt()["status"], "confirming")
            text = web_form.startup_status_message("confirming")
            self.assertIn("状态正在确认", text)
            self.assertNotIn("1-2分钟", text)
        finally:
            release.set()

    def test_thread_start_failure_persists_startup_error(self):
        with patch.object(web_form.threading.Thread, "start", side_effect=RuntimeError("boom")):
            result = web_form.start_background_collection(_subscription(), timeout_seconds=0.01)

        self.assertEqual(result["status"], "startup_error")
        self.assertEqual(self._last_attempt()["status"], "startup_error")
        self.assertNotIn("1-2分钟", web_form.startup_status_message("startup_error"))

    def test_json_save_finishes_before_thread_start_and_singleflight_attempt(self):
        lifecycle = Mock()
        subscription = _subscription()
        web_form.app.config.update(TESTING=True)
        client = web_form.app.test_client()

        with client.get("/") as response:
            body = response.get_data(as_text=True)
        token = body.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]

        def start(item):
            with file_lock(self.path, timeout=0):
                lifecycle.json_lock_reacquired()
                lifecycle.thread_started(item)
                lifecycle.singleflight_attempted()
            return {"status": "started", "entrypoint": "web"}

        with (
            patch.object(web_form, "build_subscription", return_value=subscription),
            patch.object(web_form, "start_background_collection", side_effect=start),
            patch.object(web_form, "record_last_attempt"),
        ):
            response = client.post(
                "/subscribe",
                data={
                    "csrf_token": token,
                    "form_page": "full",
                    "subscription_index": TEST_SUBSCRIPTION_ID,
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            lifecycle.mock_calls,
            [
                call.json_lock_reacquired(),
                call.thread_started({**subscription, "_index": 0}),
                call.singleflight_attempted(),
            ],
        )


class StartupPageContractTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmpdir.name) / "subscriptions.json"
        self.old_path = web_form.SUBSCRIPTIONS_PATH
        web_form.SUBSCRIPTIONS_PATH = self.path
        self.path.write_text(
            json.dumps(
                [
                    {
                        **_subscription(),
                        "notification_goals": {
                            "method": "email",
                            "email": "status@example.com",
                        },
                    }
                ]
            ),
            encoding="utf-8",
        )
        web_form.app.config.update(TESTING=True)
        self.client = web_form.app.test_client()

    def tearDown(self):
        web_form.SUBSCRIPTIONS_PATH = self.old_path
        self.tmpdir.cleanup()

    def test_non_started_pages_show_truthful_status_without_timing_promise(self):
        expected = {
            "busy": "已有采集轮正在执行",
            "startup_error": "首次采集未能启动",
            "confirming": "状态正在确认",
        }
        for status, phrase in expected.items():
            with self.subTest(status=status):
                with self.client.session_transaction() as signed_session:
                    signed_session[web_form.STARTUP_HANDSHAKE_SESSION_KEY] = {
                        TEST_SUBSCRIPTION_ID: {
                            "status": status,
                            "at": "2099-01-02T00:00:00+00:00",
                            "holder_round_id": "current-round",
                            "entrypoint": "web",
                        }
                    }
                response = self.client.get("/success?index=0")
                body = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn(f'data-startup-status="{status}"', body)
                self.assertIn(phrase, body)
                self.assertIn("status@example.com", body)
                self.assertNotIn("1-2分钟", body)

    def test_started_page_preserves_existing_normal_copy(self):
        with self.client.session_transaction() as signed_session:
            signed_session[web_form.STARTUP_HANDSHAKE_SESSION_KEY] = {
                TEST_SUBSCRIPTION_ID: {
                    "status": "started",
                    "at": "2099-01-02T00:00:00+00:00",
                    "holder_round_id": "current-round",
                    "entrypoint": "web",
                }
            }
        body = self.client.get("/success?index=0").get_data(as_text=True)

        self.assertIn("1-2分钟", body)
        self.assertIn("status@example.com", body)

    def test_persisted_status_overrides_query_parameter_and_success_stays_completed(self):
        subscriptions = json.loads(self.path.read_text(encoding="utf-8"))
        subscriptions[0]["last_attempt"] = {
            "status": "busy",
            "at": "2099-01-01T00:00:00+00:00",
            "holder_round_id": "holder-round",
            "entrypoint": "web",
        }
        self.path.write_text(json.dumps(subscriptions), encoding="utf-8")

        busy_body = self.client.get(
            "/success?index=0&startup=started"
        ).get_data(as_text=True)
        self.assertIn('data-startup-status="busy"', busy_body)
        self.assertNotIn("1-2分钟", busy_body)

        subscriptions[0]["last_attempt"]["status"] = "success"
        self.path.write_text(json.dumps(subscriptions), encoding="utf-8")
        success_body = self.client.get("/success?index=0").get_data(as_text=True)
        self.assertIn('data-startup-status="success"', success_body)
        self.assertIn("首次采集已完成", success_body)
        self.assertNotIn("1-2分钟", success_body)

    def test_signed_session_handshake_is_one_time_and_query_cannot_override_storage(self):
        subscriptions = json.loads(self.path.read_text(encoding="utf-8"))
        subscriptions[0]["last_attempt"] = {
            "status": "busy",
            "at": "2099-01-01T00:00:00+00:00",
            "holder_round_id": "old-round",
            "entrypoint": "web",
        }
        self.path.write_text(json.dumps(subscriptions), encoding="utf-8")
        with self.client.session_transaction() as signed_session:
            signed_session[web_form.STARTUP_HANDSHAKE_SESSION_KEY] = {
                TEST_SUBSCRIPTION_ID: {
                    "status": "started",
                    "at": "2099-01-02T00:00:00+00:00",
                    "holder_round_id": "new-round",
                    "entrypoint": "web",
                }
            }

        old_query = "/success?index=0&startup=started&startup_persisted=0"
        body = self.client.get(old_query).get_data(as_text=True)

        self.assertIn("1-2分钟", body)
        self.assertNotIn('data-startup-status="busy"', body)

        second_body = self.client.get(old_query).get_data(as_text=True)
        self.assertIn('data-startup-status="busy"', second_body)
        self.assertNotIn("1-2分钟", second_body)


class StartupCopyContractTest(unittest.TestCase):
    def test_only_started_copy_keeps_existing_timing_promise(self):
        self.assertIn("1-2分钟", web_form.startup_status_message("started"))
        for status in ("busy", "startup_error", "confirming"):
            with self.subTest(status=status):
                self.assertNotIn("1-2分钟", web_form.startup_status_message(status))


if __name__ == "__main__":
    unittest.main()
