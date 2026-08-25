import hashlib
import json
import multiprocessing
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


def _feedback_append_worker(path_text, record, barrier, result_queue):
    """把旧实现的锁外read同步到同一时刻，稳定复现丢更新。"""

    try:
        import web_form

        path = Path(path_text)
        web_form.FEEDBACK_PATH = path
        barrier.wait(timeout=10)
        web_form.save_feedback(record)
        result_queue.put(None)
    except BaseException as exc:  # pragma: no cover - 子进程错误由父进程断言
        result_queue.put(f"{type(exc).__name__}: {exc}")


def _subscription_edit_worker(
    path_text,
    index,
    replacement,
    barrier,
    result_queue,
):
    """把旧实现的两次订阅读取对齐，稳定暴露最后写入者覆盖。"""

    try:
        import web_form

        path = Path(path_text)
        web_form.SUBSCRIPTIONS_PATH = path
        barrier.wait(timeout=10)
        web_form.save_subscription(replacement, index)
        result_queue.put(None)
    except BaseException as exc:  # pragma: no cover - 子进程错误由父进程断言
        result_queue.put(f"{type(exc).__name__}: {exc}")


def _run_processes(target, args_list):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(len(args_list))
    result_queue = context.Queue()
    processes = [
        context.Process(target=target, args=(*args, barrier, result_queue))
        for args in args_list
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
    alive = [process for process in processes if process.is_alive()]
    for process in alive:
        process.terminate()
        process.join(timeout=5)
    results = [result_queue.get(timeout=2) for _ in processes]
    errors = [result for result in results if result]
    return processes, alive, errors


def _subscription(identifier, budget):
    return {
        "id": identifier,
        "subscription_id": identifier,
        "origin": "PVG",
        "destination": "KIX",
        "depart_date": "2026-10-01",
        "return_date": "2026-10-06",
        "round_trip": True,
        "status": "active",
        "budget_scope": "per_person",
        "max_budget_scope": "per_person",
        "target_price_scope": "per_person",
        "lcc_policy": "any",
        "hard_constraints": {
            "max_budget": budget,
            "budget_scope": "per_person",
            "max_budget_scope": "per_person",
            "target_price_scope": "per_person",
            "lcc_policy": "any",
        },
    }


class AtomicJsonStoreTest(unittest.TestCase):
    def test_replace_failure_keeps_original_bytes_and_removes_temp_file(self):
        from atomic_json_store import update_json

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text('{"items":[1]}\n', encoding="utf-8")
            original = path.read_bytes()

            with patch(
                "atomic_json_store.os.replace",
                side_effect=OSError("simulated crash before replace"),
            ):
                with self.assertRaisesRegex(OSError, "simulated crash"):
                    update_json(
                        path,
                        lambda payload: {"items": [*payload["items"], 2]},
                    )

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"items": [1]})
            self.assertEqual(list(path.parent.glob("state.json.*.tmp")), [])

    def test_read_error_is_raised_and_logged_instead_of_becoming_empty(self):
        from atomic_json_store import JsonStoreReadError, read_json

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "broken.json"
            path.write_text("{broken", encoding="utf-8")
            with patch("atomic_json_store.safe_log") as log_mock:
                with self.assertRaises(JsonStoreReadError):
                    read_json(path)

        self.assertTrue(
            any("[JSON存储] 读取失败" in str(call.args[0]) for call in log_mock.call_args_list)
        )

    def test_two_processes_append_feedback_without_lost_update(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "feedback.json"
            path.write_text("[]\n", encoding="utf-8")
            processes, alive, errors = _run_processes(
                _feedback_append_worker,
                [
                    (str(path), {"id": "feedback-a"}),
                    (str(path), {"id": "feedback-b"}),
                ],
            )
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(alive, [])
        self.assertEqual([process.exitcode for process in processes], [0, 0])
        self.assertEqual(errors, [])
        self.assertEqual({item["id"] for item in saved}, {"feedback-a", "feedback-b"})

    def test_two_processes_edit_different_subscriptions_without_lost_update(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            path.write_text(
                json.dumps(
                    [_subscription("sub-a", 1000), _subscription("sub-b", 2000)],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            processes, alive, errors = _run_processes(
                _subscription_edit_worker,
                [
                    (str(path), 0, _subscription("replacement-a", 3000)),
                    (str(path), 1, _subscription("replacement-b", 4000)),
                ],
            )
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(alive, [])
        self.assertEqual([process.exitcode for process in processes], [0, 0])
        self.assertEqual(errors, [])
        self.assertEqual(saved[0]["hard_constraints"]["max_budget"], 3000)
        self.assertEqual(saved[1]["hard_constraints"]["max_budget"], 4000)


class SubscriptionDefaultsMigrationTest(unittest.TestCase):
    def test_load_migrates_in_memory_without_changing_file_hash(self):
        import web_form

        legacy = [
            {
                "id": "legacy",
                "origin": "PVG",
                "destination": "KIX",
                "hard_constraints": {},
            }
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
            before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch.object(web_form, "SUBSCRIPTIONS_PATH", path):
                loaded = web_form.load_subscriptions()
            after_hash = hashlib.sha256(path.read_bytes()).hexdigest()

        self.assertEqual(before_hash, after_hash)
        self.assertEqual(loaded[0]["max_budget_scope"], "per_person")
        self.assertEqual(loaded[0]["lcc_policy"], "any")

    def test_explicit_migration_write_is_backed_up_and_idempotent(self):
        from scripts.migrate_subscription_defaults import run

        legacy = [
            {
                "id": "legacy",
                "origin": "PVG",
                "destination": "KIX",
                "hard_constraints": {},
            }
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            original = json.dumps(legacy, ensure_ascii=False, indent=2).encode("utf-8")
            path.write_bytes(original)

            dry_run = run(path, write=False)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(dry_run["changed"], 1)

            first = run(
                path,
                write=True,
                now=datetime(2026, 8, 25, 12, 0, 0),
            )
            migrated_bytes = path.read_bytes()
            second = run(
                path,
                write=True,
                now=datetime(2026, 8, 25, 12, 1, 0),
            )

            backup = Path(first["backup_path"])
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(first["changed"], 1)
            self.assertTrue(first["written"])
            self.assertEqual(second["changed"], 0)
            self.assertFalse(second["written"])
            self.assertIsNone(second["backup_path"])
            self.assertEqual(path.read_bytes(), migrated_bytes)


class JsonLockOrderingTest(unittest.TestCase):
    def test_web_save_releases_json_lock_before_background_collection(self):
        import web_form
        from local_file_lock import file_lock

        subscription = _subscription("saved-sub", 3000)
        lock_observations = []
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subscriptions.json"
            path.write_text("[]\n", encoding="utf-8")

            def start_after_save(_subscription):
                with file_lock(path, timeout=0):
                    lock_observations.append("released")

            with (
                patch.object(web_form, "SUBSCRIPTIONS_PATH", path),
                patch.object(web_form, "build_subscription", return_value=subscription),
                patch.object(
                    web_form,
                    "start_background_collection",
                    side_effect=start_after_save,
                ),
            ):
                response = web_form.app.test_client().post(
                    "/subscribe",
                    data={"form_page": "full"},
                )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(lock_observations, ["released"])


if __name__ == "__main__":
    unittest.main()
