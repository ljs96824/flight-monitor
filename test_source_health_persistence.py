import json
import multiprocessing
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


def _source_health_worker(path_text, source, barrier, result_queue):
    try:
        import health_check

        path = Path(path_text)
        health_check.SOURCE_HEALTH_PATH = path
        original_load = health_check._load_source_health

        def synchronized_load():
            current = original_load()
            barrier.wait(timeout=10)
            return current

        barrier.wait(timeout=10)
        with patch.object(health_check, "_load_source_health", synchronized_load):
            health_check.system_health_check(
                source_stats={source: {"status": "failed", "count": 0}},
                flights=[],
            )
        result_queue.put(None)
    except BaseException as exc:  # pragma: no cover - asserted in parent process
        result_queue.put(f"{type(exc).__name__}: {exc}")


class _FixedDateTime:
    @classmethod
    def now(cls):
        return datetime(2026, 8, 28, 12, 0, 0)


class SourceHealthPersistenceTest(unittest.TestCase):
    def test_two_processes_update_different_sources_without_lost_failures(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "source_health.json"
            path.write_text("{}\n", encoding="utf-8")
            barrier = context.Barrier(2)
            result_queue = context.Queue()
            processes = [
                context.Process(
                    target=_source_health_worker,
                    args=(str(path), source, barrier, result_queue),
                )
                for source in ("juhe", "serpapi")
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=20)
            alive = [process for process in processes if process.is_alive()]
            for process in alive:
                process.terminate()
                process.join(timeout=5)
            errors = [result_queue.get(timeout=2) for _ in processes]
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(alive, [])
        self.assertEqual([process.exitcode for process in processes], [0, 0])
        self.assertEqual(errors, [None, None])
        self.assertEqual(set(saved), {"juhe", "serpapi"})
        self.assertEqual(saved["juhe"]["consecutive_failures"], 1)
        self.assertEqual(saved["serpapi"]["consecutive_failures"], 1)

    def test_corrupt_history_is_not_overwritten_and_current_stats_still_score(self):
        import health_check

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "source_health.json"
            path.write_bytes(b"{broken")
            original = path.read_bytes()
            with (
                patch.object(health_check, "SOURCE_HEALTH_PATH", path),
                patch.object(health_check, "safe_log") as log_mock,
                patch.object(health_check, "datetime", _FixedDateTime),
            ):
                result = health_check.system_health_check(
                    source_stats={"juhe": {"status": "success", "count": 3}},
                    flights=[{"price": 100}, {"price": 120}],
                )

            self.assertEqual(path.read_bytes(), original)

        self.assertEqual(result["active_sources"], 1)
        self.assertEqual(result["source_history"]["juhe"]["last_count"], 3)
        self.assertTrue(
            any(
                "source_health_state_degraded" in str(call.args[0])
                for call in log_mock.call_args_list
            )
        )

    def test_no_source_updates_do_not_rewrite_healthy_history(self):
        import health_check

        payload = {
            "juhe": {
                "consecutive_failures": 0,
                "last_status": "success",
                "last_count": 4,
                "updated_at": "2026-08-28T08:00:00",
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "source_health.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            before = path.read_bytes()
            with (
                patch.object(health_check, "SOURCE_HEALTH_PATH", path),
                patch.object(health_check, "datetime", _FixedDateTime),
            ):
                result = health_check.system_health_check(
                    source_stats={"after_dedup": 0},
                    flights=[],
                )
            after = path.read_bytes()

        self.assertEqual(after, before)
        self.assertEqual(result["source_history"], payload)

    def test_healthy_result_shape_and_score_match_legacy_behavior(self):
        import health_check

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "source_health.json"
            with (
                patch.object(health_check, "SOURCE_HEALTH_PATH", path),
                patch.object(health_check, "datetime", _FixedDateTime),
            ):
                result = health_check.system_health_check(
                    source_stats={
                        "juhe": {"status": "success", "count": 3},
                        "duffel": {"status": "failed", "count": 0},
                        "after_dedup": 6,
                    },
                    flights=[{"price": 100}, {"price": 120}],
                )

        self.assertEqual(
            result,
            {
                "score": 42,
                "level": "低",
                "emoji": "🔴",
                "warnings": ["数据覆盖不足"],
                "active_sources": 1,
                "option_count": 6,
                "source_history": {
                    "juhe": {
                        "consecutive_failures": 0,
                        "last_status": "success",
                        "last_count": 3,
                        "updated_at": "2026-08-28T12:00:00",
                    },
                    "duffel": {
                        "consecutive_failures": 1,
                        "last_status": "failed",
                        "last_count": 0,
                        "updated_at": "2026-08-28T12:00:00",
                    },
                },
                "checked_at": "2026-08-28T12:00:00",
            },
        )


if __name__ == "__main__":
    unittest.main()
