import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


# 两个写入线程合计的等待预算(G16：Windows CI 上原先每线程 15 秒曾不够；死锁仍会在预算内被报出)。
JOIN_TIMEOUT_SECONDS = 60


class ApiUsageConcurrencyTest(unittest.TestCase):
    def test_platform_backends_share_retry_and_conflict_audit_path(self):
        import api_usage
        import local_file_lock

        self.assertTrue(
            hasattr(local_file_lock, "build_lock_backend"),
            "local_file_lock 尚未提供跨平台锁后端工厂",
        )

        class FakeMsvcrt:
            LK_NBLCK = 1
            LK_UNLCK = 2

            def __init__(self):
                self.calls = []

            def locking(self, _fileno, mode, _length):
                self.calls.append(mode)
                raise OSError("windows lock busy")

        class FakeFcntl:
            LOCK_EX = 1
            LOCK_NB = 2
            LOCK_UN = 4

            def __init__(self):
                self.calls = []

            def flock(self, _fileno, operation):
                self.calls.append(operation)
                raise BlockingIOError("posix lock busy")

        cases = [
            ("windows", FakeMsvcrt(), None),
            ("posix", None, FakeFcntl()),
        ]
        for platform_name, msvcrt_module, fcntl_module in cases:
            with self.subTest(platform=platform_name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    usage_path = root / "api_usage.json"
                    conflict_path = root / "api_usage_conflict.log"
                    api_usage.initialize_usage_ledger(usage_path)
                    backend = local_file_lock.build_lock_backend(
                        msvcrt_module=msvcrt_module,
                        fcntl_module=fcntl_module,
                    )
                    with (
                        patch("local_file_lock.LOCK_BACKEND", backend),
                        patch("api_usage.safe_log") as log_mock,
                    ):
                        payload = api_usage.record_actual_requests(
                            {"juhe": 1},
                            path=usage_path,
                            day="2026-07-24",
                            round_id=f"{platform_name}-conflict",
                            lock_timeout=0,
                            lock_retries=1,
                            conflict_log_path=conflict_path,
                        )

                    rows = [
                        json.loads(line)
                        for line in conflict_path.read_text(
                            encoding="utf-8"
                        ).splitlines()
                        if line.strip()
                    ]
                    platform_calls = (
                        msvcrt_module.calls
                        if msvcrt_module is not None
                        else fcntl_module.calls
                    )

                self.assertEqual(payload["dates"], {})
                self.assertEqual(len(platform_calls), 2)
                self.assertEqual(rows[-1]["round_id"], f"{platform_name}-conflict")
                self.assertEqual(rows[-1]["status"], "pending_reconciliation")
                self.assertTrue(
                    any(
                        "[配额台账] 写入冲突" in str(call.args[0])
                        for call in log_mock.call_args_list
                    )
                )

    def test_two_threads_record_one_hundred_entries_each_without_loss(self):
        from api_usage import initialize_usage_ledger, load_usage_strict, record_actual_requests

        # Windows 上被拒替换后残留的 .tmp 可能仍被其他进程占用；清理失败不影响本用例的计数断言(G15)。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            usage_path = Path(tmp) / "api_usage.json"
            initialize_usage_ledger(usage_path)
            errors = []
            progress = {"a": 0, "b": 0}

            def worker(prefix):
                try:
                    for index in range(100):
                        record_actual_requests(
                            {"juhe": 1},
                            path=usage_path,
                            day="2026-07-22",
                            round_id=f"{prefix}-{index}",
                        )
                        progress[prefix] += 1
                except Exception as exc:  # pragma: no cover - 由断言报告线程异常
                    errors.append(exc)

            threads = [
                threading.Thread(target=worker, args=("a",), name="ledger-a"),
                threading.Thread(target=worker, args=("b",), name="ledger-b"),
            ]
            started = time.monotonic()
            for thread in threads:
                thread.start()
            deadline = started + JOIN_TIMEOUT_SECONDS
            for thread in threads:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
            elapsed = time.monotonic() - started
            alive = [thread.name for thread in threads if thread.is_alive()]
            # 超时时写明已用时间与各线程已写入条数，区分"慢"与"卡住"。
            self.assertEqual(
                alive,
                [],
                f"threads_still_running={alive} elapsed={elapsed:.1f}s "
                f"budget={JOIN_TIMEOUT_SECONDS}s progress={dict(progress)}",
            )

            payload = load_usage_strict(usage_path)

        self.assertEqual(errors, [])
        self.assertEqual(payload["dates"]["2026-07-22"]["juhe"], 200)
        self.assertEqual(len(payload["entries"]), 200)

    def test_lock_timeout_is_audited_without_raising(self):
        from api_usage import (
            _usage_lock,
            initialize_usage_ledger,
            load_usage_strict,
            record_actual_requests,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            usage_path = root / "api_usage.json"
            conflict_path = root / "api_usage_conflict.log"
            initialize_usage_ledger(usage_path)
            record_actual_requests(
                {"juhe": 7},
                path=usage_path,
                day="2026-07-22",
                round_id="seed-round",
            )
            acquired = threading.Event()
            release = threading.Event()

            def hold_lock():
                with _usage_lock(usage_path, timeout=1.0):
                    acquired.set()
                    release.wait(timeout=5)

            holder = threading.Thread(target=hold_lock)
            holder.start()
            self.assertTrue(acquired.wait(timeout=2))
            try:
                with patch("api_usage.safe_log") as log_mock:
                    payload = record_actual_requests(
                        {"juhe": 1},
                        path=usage_path,
                        day="2026-07-22",
                        round_id="conflict-round",
                        lock_timeout=0.02,
                        lock_retries=1,
                        conflict_log_path=conflict_path,
                    )
            finally:
                release.set()
                holder.join(timeout=2)

            audit_rows = [
                json.loads(line)
                for line in conflict_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            final_payload = load_usage_strict(usage_path)

        self.assertEqual(final_payload["dates"]["2026-07-22"]["juhe"], 7)
        self.assertEqual(payload["dates"]["2026-07-22"]["juhe"], 7)
        self.assertEqual(audit_rows[-1]["round_id"], "conflict-round")
        self.assertEqual(audit_rows[-1]["status"], "pending_reconciliation")
        self.assertTrue(
            any("[配额台账] 写入冲突" in str(call.args[0]) for call in log_mock.call_args_list)
        )

    def test_atomic_replace_failure_keeps_original_ledger_intact(self):
        from api_usage import initialize_usage_ledger, load_usage_strict, record_actual_requests

        with tempfile.TemporaryDirectory() as tmp:
            usage_path = Path(tmp) / "api_usage.json"
            initialize_usage_ledger(usage_path)
            record_actual_requests(
                {"juhe": 1},
                path=usage_path,
                day="2026-07-22",
                round_id="seed-round",
            )
            original = usage_path.read_bytes()

            with patch("api_usage.os.replace", side_effect=OSError("simulated replace failure")):
                with self.assertRaisesRegex(OSError, "simulated replace failure"):
                    record_actual_requests(
                        {"juhe": 1},
                        path=usage_path,
                        day="2026-07-22",
                        round_id="failed-round",
                    )

            payload = load_usage_strict(usage_path)
            temp_files = list(usage_path.parent.glob("api_usage.json.*.tmp"))
            final = usage_path.read_bytes()

        self.assertEqual(final, original)
        self.assertEqual(payload["dates"]["2026-07-22"]["juhe"], 1)
        self.assertEqual(len(payload["entries"]), 1)
        self.assertEqual(temp_files, [])


if __name__ == "__main__":
    unittest.main()
