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
from subscription_identity import ensure_subscription_id


LOCAL_OWNER_ID = "local-owner"


class SubscriptionOwnerScopeError(ValueError):
    """A caller attempted to create data outside this repository's owner scope."""


class SubscriptionIdentityMigrationRequired(RuntimeError):
    """Existing records must receive a persisted subscription_id before M0."""


def _subscription_array(payload, *, allow_missing: bool = False) -> list[dict]:
    if payload is None and allow_missing:
        return []
    if not isinstance(payload, list):
        raise ValueError("subscriptions.json 格式错误，应为订阅数组")
    return payload


def _persisted_subscription_id(subscription: dict) -> str:
    return str(subscription.get("subscription_id") or "").strip()


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

    def list_for_owner(self, owner_id: str) -> list[dict]:
        if not self._owns(owner_id):
            return []
        if not self.path.exists():
            return []
        with file_lock(self.path):
            if not self.path.exists():
                return []
            return deepcopy(_subscription_array(read_json(self.path)))

    def get(self, owner_id: str, subscription_id_value: str) -> dict | None:
        if not self._owns(owner_id):
            return None
        target = str(subscription_id_value)
        subscriptions = self.list_for_owner(owner_id)
        _require_persisted_identities(subscriptions)
        return next(
            (
                deepcopy(item)
                for item in subscriptions
                if isinstance(item, dict)
                and _persisted_subscription_id(item) == target
            ),
            None,
        )

    def resolve_legacy_index(self, owner_id: str, index: int) -> dict | None:
        """M0 only: resolve a full-table position without mutating storage."""

        if not self._owns(owner_id):
            return None
        subscriptions = self.list_for_owner(owner_id)
        _require_persisted_identities(subscriptions)
        if not 0 <= index < len(subscriptions):
            return None
        candidate = subscriptions[index]
        if not isinstance(candidate, dict):
            return None
        return deepcopy(candidate)

    def create(self, owner_id: str, subscription: dict) -> dict:
        if not self._owns(owner_id):
            raise SubscriptionOwnerScopeError(
                f"owner scope unavailable: {owner_id!r}"
            )
        candidate = deepcopy(subscription)
        saved: dict = {}

        def mutate(payload):
            subscriptions = _subscription_array(payload, allow_missing=True)
            _require_persisted_identities(subscriptions)
            ensure_subscription_id(candidate)
            subscriptions.append(candidate)
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
            subscriptions = _subscription_array(payload, allow_missing=True)
            _require_persisted_identities(subscriptions)
            for index, existing in enumerate(subscriptions):
                if (
                    not isinstance(existing, dict)
                    or _persisted_subscription_id(existing) != target
                ):
                    continue
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
                saved.update(deepcopy(replacement))
                matched = True
                break
            return subscriptions

        update_json(self.path, mutate_payload)
        return saved if matched else None

    def delete(self, owner_id: str, subscription_id_value: str) -> bool:
        if not self._owns(owner_id):
            return False
        target = str(subscription_id_value)
        deleted = False

        def mutate(payload):
            nonlocal deleted
            subscriptions = _subscription_array(payload, allow_missing=True)
            _require_persisted_identities(subscriptions)
            retained = []
            for item in subscriptions:
                if (
                    not deleted
                    and isinstance(item, dict)
                    and _persisted_subscription_id(item) == target
                ):
                    deleted = True
                    continue
                retained.append(item)
            return retained

        update_json(self.path, mutate)
        return deleted
