import hashlib
import json
import multiprocessing
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from atomic_json_store import JsonStoreReadError
from subscription_identity import mask_subscription_id, persisted_subscription_id
from subscription_repository import (
    LOCAL_OWNER_ID,
    DuplicateSubscriptionIdError,
    SubscriptionIdentityMigrationRequired,
    SubscriptionOwnerScopeError,
    SubscriptionRepository,
    _validated_id_index,
)


SUB_A = "123e4567-e89b-12d3-a456-426614174201"
SUB_B = "223e4567-e89b-12d3-a456-426614174202"


def _subscription(subscription_id: str, *, budget: int = 1000) -> dict:
    return {
        "subscription_id": subscription_id,
        "origin": "PVG",
        "destination": "KIX",
        "depart_date": "2026-10-01",
        "hard_constraints": {"max_budget": budget},
    }


def _create_same_id_worker(path_text: str, barrier, results) -> None:
    repository = SubscriptionRepository(Path(path_text))
    try:
        barrier.wait(timeout=10)
        created = repository.create(
            LOCAL_OWNER_ID,
            _subscription(SUB_A),
        )
        results.put(("created", created["subscription_id"]))
    except DuplicateSubscriptionIdError as exc:
        results.put(("duplicate", str(exc)))
    except BaseException as exc:  # pragma: no cover - 子进程错误由父进程断言
        results.put(("unexpected", f"{type(exc).__name__}: {exc}"))


class SubscriptionIdentityUniquenessTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = Path(self.tmpdir.name) / "subscriptions.json"
        self.repository = SubscriptionRepository(self.path)

    def _write(self, payload) -> bytes:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.path.write_bytes(raw)
        return raw

    def _local_operations(self):
        return {
            "list": lambda: self.repository.list_for_owner(LOCAL_OWNER_ID),
            "get": lambda: self.repository.get(LOCAL_OWNER_ID, SUB_A),
            "create": lambda: self.repository.create(
                LOCAL_OWNER_ID,
                _subscription(SUB_B),
            ),
            "update": lambda: self.repository.update(
                LOCAL_OWNER_ID,
                SUB_A,
                {"hard_constraints": {"max_budget": 2000}},
            ),
            "mutate": lambda: self.repository.mutate(
                LOCAL_OWNER_ID,
                SUB_A,
                lambda current: {**current, "status": "paused"},
            ),
            "delete": lambda: self.repository.delete(LOCAL_OWNER_ID, SUB_A),
            "resolve": lambda: self.repository.resolve_legacy_index(
                LOCAL_OWNER_ID,
                0,
            ),
        }

    def test_shared_identity_and_mask_helpers_keep_exact_m0_semantics(self):
        self.assertEqual(
            persisted_subscription_id({"subscription_id": "  Mixed-Case-ID  "}),
            "Mixed-Case-ID",
        )
        self.assertEqual(persisted_subscription_id({"subscription_id": 123}), "")
        self.assertEqual(mask_subscription_id(SUB_A), "123e4567********")
        self.assertRegex(
            mask_subscription_id("person@example.com"),
            r"^sha256:[0-9a-f]{8}\*{8}$",
        )

    def test_validated_index_rejects_single_and_multiple_duplicate_groups(self):
        cases = (
            ([_subscription(SUB_A), _subscription(SUB_A)], 0, 1),
            (
                [
                    _subscription(SUB_A),
                    _subscription(SUB_B),
                    _subscription(SUB_A),
                    _subscription(SUB_B),
                ],
                0,
                2,
            ),
        )
        for subscriptions, first_index, second_index in cases:
            with self.subTest(count=len(subscriptions)):
                with self.assertRaises(DuplicateSubscriptionIdError) as raised:
                    _validated_id_index(subscriptions)
                self.assertEqual(raised.exception.first_index, first_index)
                self.assertEqual(raised.exception.second_index, second_index)

    def test_migration_error_has_priority_over_duplicate_error(self):
        subscriptions = [
            _subscription(SUB_A),
            _subscription(SUB_A),
            {"origin": "SHA", "destination": "KIX"},
        ]

        with self.assertRaises(SubscriptionIdentityMigrationRequired):
            _validated_id_index(subscriptions)

    def test_duplicate_error_contains_only_masked_identity_and_indexes(self):
        private_id = "person@example.com"
        subscriptions = [
            {
                "subscription_id": private_id,
                "email": "private-owner@example.com",
                "route": "private-route",
            },
            {"subscription_id": private_id},
        ]

        with self.assertRaises(DuplicateSubscriptionIdError) as raised:
            _validated_id_index(subscriptions)

        error = raised.exception
        rendered = "\n".join(
            (str(error), repr(error), repr(error.__dict__), repr(error.args))
        )
        self.assertEqual(error.first_index, 0)
        self.assertEqual(error.second_index, 1)
        self.assertRegex(error.masked_id, r"^sha256:[0-9a-f]{8}\*{8}$")
        for forbidden in (
            private_id,
            "private-owner@example.com",
            "private-route",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_all_seven_local_owner_methods_fail_closed_on_duplicates(self):
        duplicate_payload = [_subscription(SUB_A), _subscription(SUB_A)]
        for name, operation in self._local_operations().items():
            with self.subTest(operation=name):
                original = self._write(duplicate_payload)
                before_hash = hashlib.sha256(original).hexdigest()

                with self.assertRaises(DuplicateSubscriptionIdError):
                    operation()

                after = self.path.read_bytes()
                self.assertEqual(after, original)
                self.assertEqual(hashlib.sha256(after).hexdigest(), before_hash)

    def test_create_rejects_caller_supplied_existing_id_without_writing(self):
        original = self._write([_subscription(SUB_A)])

        with self.assertRaises(DuplicateSubscriptionIdError) as raised:
            self.repository.create(LOCAL_OWNER_ID, _subscription(SUB_A))

        self.assertEqual(raised.exception.first_index, 0)
        self.assertEqual(raised.exception.second_index, 1)
        self.assertEqual(self.path.read_bytes(), original)

    def test_missing_file_and_valid_empty_array_follow_empty_table_contract(self):
        expected = {
            "list": [],
            "get": None,
            "update": None,
            "mutate": None,
            "delete": False,
            "resolve": None,
        }
        operations = self._local_operations()
        for state in ("missing", "empty"):
            for name, value in expected.items():
                with self.subTest(state=state, operation=name):
                    self.path.unlink(missing_ok=True)
                    if state == "empty":
                        self.path.write_bytes(b"[]")
                    before = self.path.read_bytes() if self.path.exists() else None

                    self.assertEqual(operations[name](), value)

                    after = self.path.read_bytes() if self.path.exists() else None
                    self.assertEqual(after, before)

        self.path.unlink(missing_ok=True)
        created = self.repository.create(LOCAL_OWNER_ID, _subscription(SUB_A))
        self.assertEqual(created["subscription_id"], SUB_A)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), [created])

    def test_invalid_json_states_fail_closed_without_overwriting_file(self):
        invalid_states = (
            ("zero-byte", b"", JsonStoreReadError),
            ("malformed", b'{"broken"', JsonStoreReadError),
            ("non-array-root", b'{"subscription_id":"x"}', ValueError),
        )
        for state, raw, error_type in invalid_states:
            for name, operation in self._local_operations().items():
                with self.subTest(state=state, operation=name):
                    self.path.write_bytes(raw)

                    with self.assertRaises(error_type):
                        operation()

                    self.assertEqual(self.path.read_bytes(), raw)

    def test_missing_persisted_identity_blocks_all_methods_without_writing(self):
        legacy = _subscription(SUB_A)
        legacy["id"] = legacy.pop("subscription_id")
        for name, operation in self._local_operations().items():
            with self.subTest(operation=name):
                original = self._write([legacy])

                with self.assertRaises(SubscriptionIdentityMigrationRequired):
                    operation()

                self.assertEqual(self.path.read_bytes(), original)

    def test_list_get_and_legacy_resolution_validate_inside_file_lock(self):
        payload = [_subscription(SUB_A)]
        self._write(payload)
        import subscription_repository as repository_module

        real_validate = repository_module._validated_id_index
        operations = (
            lambda: self.repository.list_for_owner(LOCAL_OWNER_ID),
            lambda: self.repository.get(LOCAL_OWNER_ID, SUB_A),
            lambda: self.repository.resolve_legacy_index(LOCAL_OWNER_ID, 0),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                lock_entered = False

                @contextmanager
                def guarded_lock(path):
                    nonlocal lock_entered
                    self.assertEqual(Path(path), self.path)
                    lock_entered = True
                    try:
                        yield
                    finally:
                        lock_entered = False

                def guarded_read(path):
                    self.assertTrue(lock_entered)
                    return payload

                def guarded_validate(subscriptions):
                    self.assertTrue(lock_entered)
                    return real_validate(subscriptions)

                with (
                    patch.object(repository_module, "file_lock", guarded_lock),
                    patch.object(repository_module, "read_json", guarded_read),
                    patch.object(
                        repository_module,
                        "_validated_id_index",
                        guarded_validate,
                    ),
                ):
                    operation()

    def test_nonlocal_owner_does_not_leak_duplicate_local_state(self):
        original = self._write([_subscription(SUB_A), _subscription(SUB_A)])
        mutator = Mock()

        self.assertEqual(self.repository.list_for_owner("other-owner"), [])
        self.assertIsNone(self.repository.get("other-owner", SUB_A))
        self.assertIsNone(
            self.repository.update("other-owner", SUB_A, {"status": "paused"})
        )
        self.assertIsNone(self.repository.mutate("other-owner", SUB_A, mutator))
        self.assertFalse(self.repository.delete("other-owner", SUB_A))
        self.assertIsNone(self.repository.resolve_legacy_index("other-owner", 0))
        with self.assertRaises(SubscriptionOwnerScopeError) as raised:
            self.repository.create("other-owner", _subscription(SUB_B))

        self.assertNotIn("duplicate", str(raised.exception).lower())
        mutator.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)

    def test_two_processes_creating_same_id_allow_exactly_one_record(self):
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        results = context.Queue()
        processes = [
            context.Process(
                target=_create_same_id_worker,
                args=(str(self.path), barrier, results),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)

        self.assertEqual([process.exitcode for process in processes], [0, 0])
        outcomes = [results.get(timeout=2) for _ in processes]
        self.assertEqual([item[0] for item in outcomes].count("created"), 1)
        self.assertEqual([item[0] for item in outcomes].count("duplicate"), 1)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["subscription_id"], SUB_A)

    def test_update_and_mutate_cannot_change_primary_identity(self):
        self._write([_subscription(SUB_A)])

        updated = self.repository.update(
            LOCAL_OWNER_ID,
            SUB_A,
            {"subscription_id": SUB_B, "hard_constraints": {"max_budget": 2500}},
        )
        mutated = self.repository.mutate(
            LOCAL_OWNER_ID,
            SUB_A,
            lambda current: {
                **current,
                "subscription_id": SUB_B,
                "status": "paused",
            },
        )

        self.assertEqual(updated["subscription_id"], SUB_A)
        self.assertEqual(mutated["subscription_id"], SUB_A)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["subscription_id"], SUB_A)
        self.assertEqual(saved[0]["status"], "paused")


if __name__ == "__main__":
    unittest.main()
