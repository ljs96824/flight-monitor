"""Owner-scoped subscription persistence over the existing JSON array.

M0 keeps the on-disk schema unchanged. The owner boundary is enforced by the
server-side repository instance; owner identity is never read from a request.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

from atomic_json_store import read_json, update_json
from local_file_lock import file_lock
from subscription_identity import (
    ensure_subscription_id,
    mask_subscription_id,
    persisted_subscription_id,
)


LOCAL_OWNER_ID = "local-owner"


class SubscriptionOwnerScopeError(ValueError):
    """A caller attempted to create data outside this repository's owner scope."""


class SubscriptionIdentityMigrationRequired(RuntimeError):
    """Existing records must receive a persisted subscription_id before M0."""


class DuplicateSubscriptionIdError(RuntimeError):
    """The persisted subscription array contains a duplicate identity."""

    def __init__(
        self,
        masked_id: str,
        first_index: int,
        second_index: int,
    ) -> None:
        self.masked_id = str(masked_id)
        self.first_index = int(first_index)
        self.second_index = int(second_index)
        super().__init__(
            "订阅 subscription_id 重复: "
            f"id={self.masked_id} "
            f"indexes=[{self.first_index}, {self.second_index}]"
        )


class _SubscriptionFileMissing(RuntimeError):
    """Abort a write RMW without creating a missing subscription file."""


def _subscription_array(payload, *, allow_missing: bool = False) -> list[dict]:
    if payload is None and allow_missing:
        return []
    if not isinstance(payload, list):
        raise ValueError("subscriptions.json 格式错误，应为订阅数组")
    return payload


_persisted_subscription_id = persisted_subscription_id


def _require_persisted_identities(subscriptions: list[dict]) -> None:
    missing = [
        index
        for index, item in enumerate(subscriptions)
        if not isinstance(item, dict) or not _persisted_subscription_id(item)
    ]
    if missing:
        raise SubscriptionIdentityMigrationRequired(
            "订阅缺少持久 subscription_id；"
            "请先运行 scripts/migrate_subscription_ids.py --write "
            f"(indexes={missing})"
        )


def _validated_id_index(subscriptions: list[dict]) -> dict[str, int]:
    """Validate migration first, then build the unique full-array ID index."""

    _require_persisted_identities(subscriptions)
    positions: dict[str, int] = {}
    for index, subscription in enumerate(subscriptions):
        stable_id = persisted_subscription_id(subscription)
        first_index = positions.get(stable_id)
        if first_index is not None:
            raise DuplicateSubscriptionIdError(
                mask_subscription_id(stable_id),
                first_index,
                index,
            )
        positions[stable_id] = index
    return positions


def _merge_subscription_patch(current: dict, patch: dict) -> dict:
    """Merge a field patch into the record read inside the JSON file lock."""

    merged = deepcopy(current)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_subscription_patch(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


class SubscriptionRepository:
    """Persist subscriptions for one server-owned owner scope."""

    def __init__(
        self,
        path: str | Path,
        *,
        local_owner_id: str = LOCAL_OWNER_ID,
    ) -> None:
        self.path = Path(path)
        self.local_owner_id = str(local_owner_id)

    def _owns(self, owner_id: str) -> bool:
        return str(owner_id) == self.local_owner_id

    def _load_validated_locked(self) -> tuple[list[dict], dict[str, int]]:
        """Read and validate while the caller holds this repository's file lock."""

        if not self.path.exists():
            return [], {}
        subscriptions = _subscription_array(read_json(self.path))
        return subscriptions, _validated_id_index(subscriptions)

    def list_for_owner(self, owner_id: str) -> list[dict]:
        if not self._owns(owner_id):
            return []
        with file_lock(self.path):
            subscriptions, _positions = self._load_validated_locked()
            return deepcopy(subscriptions)

    def get(self, owner_id: str, subscription_id_value: str) -> dict | None:
        if not self._owns(owner_id):
            return None
        target = str(subscription_id_value)
        with file_lock(self.path):
            subscriptions, positions = self._load_validated_locked()
            index = positions.get(target)
            return deepcopy(subscriptions[index]) if index is not None else None

    def resolve_legacy_index(self, owner_id: str, index: int) -> dict | None:
        """M0 only: resolve a full-table position without mutating storage."""

        if not self._owns(owner_id):
            return None
        with file_lock(self.path):
            subscriptions, _positions = self._load_validated_locked()
            if not 0 <= index < len(subscriptions):
                return None
            return deepcopy(subscriptions[index])

    def create(self, owner_id: str, subscription: dict) -> dict:
        if not self._owns(owner_id):
            raise SubscriptionOwnerScopeError(
                f"owner scope unavailable: {owner_id!r}"
            )
        candidate = deepcopy(subscription)
        saved: dict = {}

        def mutate(payload):
            subscriptions = _subscription_array(payload, allow_missing=True)
            positions = _validated_id_index(subscriptions)
            ensure_subscription_id(candidate)
            candidate_id = persisted_subscription_id(candidate)
            if candidate_id in positions:
                raise DuplicateSubscriptionIdError(
                    mask_subscription_id(candidate_id),
                    positions[candidate_id],
                    len(subscriptions),
                )
            subscriptions.append(candidate)
            _validated_id_index(subscriptions)
            saved.update(deepcopy(candidate))
            return subscriptions

        update_json(self.path, mutate)
        return saved

    def update(
        self,
        owner_id: str,
        subscription_id_value: str,
        subscription: dict,
    ) -> dict | None:
        patch = deepcopy(subscription)
        return self.mutate(
            owner_id,
            subscription_id_value,
            lambda current: _merge_subscription_patch(current, patch),
        )

    def mutate(
        self,
        owner_id: str,
        subscription_id_value: str,
        mutator: Callable[[dict], dict],
    ) -> dict | None:
        """Mutate one record inside the repository's locked JSON RMW."""

        if not self._owns(owner_id):
            return None
        target = str(subscription_id_value)
        saved: dict = {}
        matched = False

        def mutate_payload(payload):
            nonlocal matched
            if payload is None:
                raise _SubscriptionFileMissing
            subscriptions = _subscription_array(payload)
            positions = _validated_id_index(subscriptions)
            index = positions.get(target)
            if index is None:
                return subscriptions
            existing = subscriptions[index]
            replacement = mutator(deepcopy(existing))
            if not isinstance(replacement, dict):
                raise TypeError("subscription mutator 必须返回 dict")
            for identity_field in ("id", "subscription_id", "created_at"):
                if identity_field in existing:
                    replacement[identity_field] = existing[identity_field]
                else:
                    replacement.pop(identity_field, None)
            ensure_subscription_id(replacement)
            subscriptions[index] = replacement
            _validated_id_index(subscriptions)
            saved.update(deepcopy(replacement))
            matched = True
            return subscriptions

        try:
            update_json(self.path, mutate_payload)
        except _SubscriptionFileMissing:
            return None
        return saved if matched else None

    def delete(self, owner_id: str, subscription_id_value: str) -> bool:
        if not self._owns(owner_id):
            return False
        target = str(subscription_id_value)
        deleted = False

        def mutate(payload):
            nonlocal deleted
            if payload is None:
                raise _SubscriptionFileMissing
            subscriptions = _subscription_array(payload)
            positions = _validated_id_index(subscriptions)
            index = positions.get(target)
            if index is None:
                return subscriptions
            retained = subscriptions[:index] + subscriptions[index + 1 :]
            _validated_id_index(retained)
            deleted = True
            return retained

        try:
            update_json(self.path, mutate)
        except _SubscriptionFileMissing:
            return False
        return deleted
