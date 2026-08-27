import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class _ImmediateUsageSource:
    name = "fake"

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        return {
            "flights": [
                {
                    "flight_combo": f"{origin}{dest}{date_str}{cabin_class}",
                    "price": 100,
                }
            ],
            "source": self.name,
        }


class _KeyboardInterruptSource:
    name = "fake"

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        raise KeyboardInterrupt


def _run_concurrent_round(
    usage_path: str,
    cache_dir: str,
    round_id: str,
    start_event,
    request_count: int,
) -> None:
    from request_cache import cached_fetch, reset_for_tests, start_request_cache_round

    reset_for_tests(cache_dir)
    start_request_cache_round(
        round_id,
        track_usage=True,
        usage_path=usage_path,
    )
    start_event.wait(timeout=10)
    source = _ImmediateUsageSource()
    for index in range(request_count):
        cached_fetch(
            source,
            "SHA",
            "PEK",
            f"2099-09-{index + 1:02d}",
            persist=False,
            force_fresh=True,
        )


def _run_until_terminated(
    usage_path: str,
    cache_dir: str,
    ready_event,
) -> None:
    from request_cache import cached_fetch, reset_for_tests, start_request_cache_round

    reset_for_tests(cache_dir)
    start_request_cache_round(
        "terminated-round",
        track_usage=True,
        usage_path=usage_path,
    )
    cached_fetch(
        _ImmediateUsageSource(),
        "SHA",
        "PEK",
        "2099-10-01",
        persist=False,
        force_fresh=True,
    )
    ready_event.set()
    time.sleep(30)


class ImmediateApiUsageLedgerTest(unittest.TestCase):
    def test_completed_call_is_durable_before_keyboard_interrupt_and_round_end(self):
        from api_usage import load_usage
        from request_cache import (
            cached_fetch,
            reset_for_tests,
            start_request_cache_round,
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            usage_path = root / "api_usage.json"
            reset_for_tests(root / "cache")
            self.addCleanup(reset_for_tests, None)
            start_request_cache_round(
                "keyboard-interrupt-round",
                track_usage=True,
                usage_path=usage_path,
            )

            with self.assertRaises(KeyboardInterrupt):
                cached_fetch(
                    _KeyboardInterruptSource(),
                    "SHA",
                    "PEK",
                    "2099-10-01",
                    persist=False,
                    force_fresh=True,
                )

            payload = load_usage(usage_path)

        self.assertEqual(payload["dates"][next(iter(payload["dates"]))]["fake"], 1)
        self.assertEqual(len(payload["entries"]), 1)
        self.assertEqual(payload["entries"][0]["round_id"], "keyboard-interrupt-round")


    def test_retry_attempts_are_each_durable_before_round_end(self):
        from api_usage import load_usage
        import request_cache

        class RetryThenSuccessSource(_ImmediateUsageSource):
            def __init__(self):
                self.calls = 0

            def fetch(self, origin, dest, date_str, cabin_class="economy"):
                self.calls += 1
                if self.calls == 1:
                    raise OSError("retry once")
                return super().fetch(origin, dest, date_str, cabin_class)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            usage_path = root / "api_usage.json"
            request_cache.reset_for_tests(root / "cache")
            self.addCleanup(request_cache.reset_for_tests, None)
            request_cache.start_request_cache_round(
                "retry-round",
                track_usage=True,
                usage_path=usage_path,
            )
            with patch.object(request_cache, "SOURCE_FETCH_IO_RETRY_DELAY_SECONDS", 0):
                request_cache.cached_fetch(
                    RetryThenSuccessSource(),
                    "SHA",
                    "PEK",
                    "2099-10-01",
                    persist=False,
                    force_fresh=True,
                )
            payload = load_usage(usage_path)

        self.assertEqual(payload["dates"][next(iter(payload["dates"]))]["fake"], 2)
        self.assertEqual(len(payload["entries"]), 2)
        self.assertEqual(
            {entry["round_id"] for entry in payload["entries"]},
            {"retry-round"},
        )
    def test_completed_call_survives_process_termination_without_round_end(self):
        from api_usage import load_usage

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            usage_path = root / "api_usage.json"
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            process = context.Process(
                target=_run_until_terminated,
                args=(str(usage_path), str(root / "cache"), ready),
            )
            process.start()
            self.assertTrue(ready.wait(timeout=15), "子进程未完成首个模拟调用")
            process.terminate()
            process.join(timeout=10)
            self.assertFalse(process.is_alive(), "被终止的子进程仍在运行")

            payload = load_usage(usage_path)

        self.assertEqual(payload["dates"][next(iter(payload["dates"]))]["fake"], 1)
        self.assertEqual(len(payload["entries"]), 1)
        self.assertEqual(payload["entries"][0]["round_id"], "terminated-round")

    def test_concurrent_rounds_append_each_attempt_without_loss(self):
        from api_usage import load_usage

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            usage_path = root / "api_usage.json"
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            processes = [
                context.Process(
                    target=_run_concurrent_round,
                    args=(
                        str(usage_path),
                        str(root / f"cache-{label}"),
                        f"concurrent-{label}",
                        start,
                        8,
                    ),
                )
                for label in ("a", "b")
            ]
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(timeout=20)

            payload = load_usage(usage_path)

        self.assertTrue(all(not process.is_alive() for process in processes))
        self.assertEqual([process.exitcode for process in processes], [0, 0])
        self.assertEqual(payload["dates"][next(iter(payload["dates"]))]["fake"], 16)
        self.assertEqual(len(payload["entries"]), 16)
        self.assertEqual(
            {entry["round_id"] for entry in payload["entries"]},
            {"concurrent-a", "concurrent-b"},
        )

    def test_round_end_only_reconciles_and_never_writes_a_second_entry(self):
        from api_usage import load_usage
        import request_cache

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            usage_path = root / "api_usage.json"
            request_cache.reset_for_tests(root / "cache")
            self.addCleanup(request_cache.reset_for_tests, None)
            request_cache.start_request_cache_round(
                "reconcile-round",
                track_usage=True,
                usage_path=usage_path,
            )
            request_cache.cached_fetch(
                _ImmediateUsageSource(),
                "SHA",
                "PEK",
                "2099-10-01",
                persist=False,
                force_fresh=True,
            )
            before = load_usage(usage_path)
            logs = []
            with patch.object(request_cache, "safe_log", side_effect=logs.append):
                request_cache.print_request_cache_stats()
                request_cache.print_request_cache_stats()
            after = load_usage(usage_path)

        self.assertEqual(len(before["entries"]), 1)
        self.assertEqual(after, before)
        reconciliation = [
            line for line in logs if line.startswith("[配额恒等式]")
        ]
        self.assertEqual(len(reconciliation), 1)
        self.assertIn("一致=True", reconciliation[0])


if __name__ == "__main__":
    unittest.main()
