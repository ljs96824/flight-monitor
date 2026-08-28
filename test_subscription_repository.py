import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from subscription_repository import (
    LOCAL_OWNER_ID,
    SubscriptionOwnerScopeError,
    SubscriptionRepository,
)


SUB_A = "123e4567-e89b-12d3-a456-426614174101"
SUB_B = "123e4567-e89b-12d3-a456-426614174102"


def _subscription(subscription_id: str, budget: int) -> dict:
    return {
        "subscription_id": subscription_id,
        "origin": "PVG",
        "destination": "KIX",
        "depart_date": "2026-10-01",
        "status": "active",
        "hard_constraints": {"max_budget": budget},
    }


def _repository_update_worker(
    path_text: str,
    subscription_id: str,
    budget: int,
    barrier,
    results,
) -> None:
    try:
        repository = SubscriptionRepository(Path(path_text))
        current = repository.get(LOCAL_OWNER_ID, subscription_id)
        current["hard_constraints"]["max_budget"] = budget
        barrier.wait(timeout=10)
        saved = repository.update(LOCAL_OWNER_ID, subscription_id, current)
        results.put(saved["hard_constraints"]["max_budget"] if saved else None)
    except BaseException as exc:  # pragma: no cover - 子进程错误由父进程断言
        results.put(f"{type(exc).__name__}: {exc}")


def _delete_then_update_worker(
    path_text: str,
    role: str,
    deleted,
    results,
) -> None:
    try:
        repository = SubscriptionRepository(Path(path_text))
        if role == "delete":
            results.put(repository.delete(LOCAL_OWNER_ID, SUB_A))
            deleted.set()
            return
        deleted.wait(timeout=10)
        results.put(
            repository.update(
                LOCAL_OWNER_ID,
                SUB_A,
                _subscription(SUB_A, 9999),
            )
        )
    except BaseException as exc:  # pragma: no cover - 子进程错误由父进程断言
        results.put(f"{type(exc).__name__}: {exc}")


def _repository_mutate_worker(
    path_text: str,
    field: str,
    value,
    barrier,
    results,
) -> None:
    try:
        repository = SubscriptionRepository(Path(path_text))
        barrier.wait(timeout=10)

        def mutate(current):
            current[field] = value
            return current

        saved = repository.mutate(LOCAL_OWNER_ID, SUB_A, mutate)
        results.put(saved.get(field) if saved else None)
    except BaseException as exc:  # pragma: no cover - 子进程错误由父进程断言
        results.put(f"{type(exc).__name__}: {exc}")


class SubscriptionRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "subscriptions.json"
        self.repository = SubscriptionRepository(self.path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_five_methods_preserve_json_array_shape_and_identity(self):
        created = self.repository.create(
            LOCAL_OWNER_ID,
            _subscription(SUB_A, 1000),
        )

        self.assertEqual(created["subscription_id"], SUB_A)
        self.assertEqual(
            self.repository.list_for_owner(LOCAL_OWNER_ID),
            [created],
        )
        self.assertEqual(self.repository.get(LOCAL_OWNER_ID, SUB_A), created)

        replacement = _subscription("replacement-id-must-not-win", 2500)
        updated = self.repository.update(LOCAL_OWNER_ID, SUB_A, replacement)
        self.assertEqual(updated["subscription_id"], SUB_A)
        self.assertEqual(updated["hard_constraints"]["max_budget"], 2500)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), [updated])

        self.assertTrue(self.repository.delete(LOCAL_OWNER_ID, SUB_A))
        self.assertEqual(self.repository.list_for_owner(LOCAL_OWNER_ID), [])

    def test_missing_id_has_explicit_nonexceptional_results(self):
        self.assertIsNone(self.repository.get(LOCAL_OWNER_ID, "missing"))
        self.assertIsNone(
            self.repository.update(
                LOCAL_OWNER_ID,
                "missing",
                _subscription("missing", 2000),
            )
        )
        self.assertFalse(self.repository.delete(LOCAL_OWNER_ID, "missing"))

    def test_m0_legacy_index_resolution_persists_stable_identity_once(self):
        legacy = _subscription("", 1000)
        legacy.pop("subscription_id")
        self.path.write_text(
            json.dumps([legacy], ensure_ascii=False),
            encoding="utf-8",
        )

        first = self.repository.resolve_legacy_index(LOCAL_OWNER_ID, 0)
        first_bytes = self.path.read_bytes()
        second = self.repository.resolve_legacy_index(LOCAL_OWNER_ID, 0)

        self.assertTrue(first["subscription_id"])
        self.assertEqual(second["subscription_id"], first["subscription_id"])
        self.assertEqual(self.path.read_bytes(), first_bytes)

    def test_update_promotes_legacy_id_to_subscription_id_like_old_save_path(self):
        legacy = _subscription(SUB_A, 1000)
        legacy["id"] = legacy.pop("subscription_id")
        self.path.write_text(
            json.dumps([legacy], ensure_ascii=False),
            encoding="utf-8",
        )
        replacement = _subscription("replacement-must-not-win", 2500)

        saved = self.repository.update(LOCAL_OWNER_ID, SUB_A, replacement)

        self.assertEqual(saved["id"], SUB_A)
        self.assertEqual(saved["subscription_id"], SUB_A)
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))[0],
            saved,
        )

    def test_nonlocal_owner_cannot_observe_or_mutate_local_records(self):
        self.repository.create(LOCAL_OWNER_ID, _subscription(SUB_A, 1000))

        self.assertEqual(self.repository.list_for_owner("other-owner"), [])
        self.assertIsNone(self.repository.get("other-owner", SUB_A))
        self.assertIsNone(
            self.repository.update(
                "other-owner",
                SUB_A,
                _subscription(SUB_A, 9000),
            )
        )
        self.assertFalse(self.repository.delete("other-owner", SUB_A))
        with self.assertRaises(SubscriptionOwnerScopeError):
            self.repository.create("other-owner", _subscription(SUB_B, 2000))
        self.assertEqual(
            self.repository.get(LOCAL_OWNER_ID, SUB_A)["hard_constraints"]["max_budget"],
            1000,
        )

    def test_two_processes_update_different_subscriptions_without_lost_update(self):
        self.path.write_text(
            json.dumps(
                [_subscription(SUB_A, 1000), _subscription(SUB_B, 2000)],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        results = context.Queue()
        processes = [
            context.Process(
                target=_repository_update_worker,
                args=(str(self.path), SUB_A, 3000, barrier, results),
            ),
            context.Process(
                target=_repository_update_worker,
                args=(str(self.path), SUB_B, 4000, barrier, results),
            ),
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)

        self.assertEqual([process.exitcode for process in processes], [0, 0])
        self.assertEqual({results.get(timeout=2) for _ in processes}, {3000, 4000})
        saved = {item["subscription_id"]: item for item in json.loads(self.path.read_text(encoding="utf-8"))}
        self.assertEqual(saved[SUB_A]["hard_constraints"]["max_budget"], 3000)
        self.assertEqual(saved[SUB_B]["hard_constraints"]["max_budget"], 4000)

    def test_update_losing_delete_race_returns_none_instead_of_crashing(self):
        self.path.write_text(
            json.dumps([_subscription(SUB_A, 1000)], ensure_ascii=False),
            encoding="utf-8",
        )
        context = multiprocessing.get_context("spawn")
        deleted = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=_delete_then_update_worker,
                args=(str(self.path), "delete", deleted, results),
            ),
            context.Process(
                target=_delete_then_update_worker,
                args=(str(self.path), "update", deleted, results),
            ),
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)

        self.assertEqual([process.exitcode for process in processes], [0, 0])
        values = [results.get(timeout=2) for _ in processes]
        self.assertIn(True, values)
        self.assertIn(None, values)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), [])

    def test_same_subscription_mutations_are_one_locked_rmw(self):
        self.path.write_text(
            json.dumps([_subscription(SUB_A, 1000)], ensure_ascii=False),
            encoding="utf-8",
        )
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        results = context.Queue()
        expected_attempt = {
            "status": "started",
            "at": "2099-01-01T00:00:00+00:00",
        }
        processes = [
            context.Process(
                target=_repository_mutate_worker,
                args=(str(self.path), "status", "paused", barrier, results),
            ),
            context.Process(
                target=_repository_mutate_worker,
                args=(
                    str(self.path),
                    "last_attempt",
                    expected_attempt,
                    barrier,
                    results,
                ),
            ),
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)

        self.assertEqual([process.exitcode for process in processes], [0, 0])
        values = [results.get(timeout=2) for _ in processes]
        self.assertIn("paused", values)
        self.assertIn(expected_attempt, values)
        saved = json.loads(self.path.read_text(encoding="utf-8"))[0]
        self.assertEqual(saved["status"], "paused")
        self.assertEqual(saved["last_attempt"], expected_attempt)


if __name__ == "__main__":
    unittest.main()
