"""Versioned compare-and-swap persistence for the research basket state."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Callable

from atomic_json_store import JsonStoreReadError, read_json, update_json


STATE_VERSION = 2


class ResearchStateConflict(RuntimeError):
    """The caller tried to persist a stale research-state snapshot."""


def _normalized_state(payload, *, path: Path) -> dict:
    if not isinstance(payload, dict):
        raise JsonStoreReadError(f"研究篮子状态根节点不是对象: {path}")
    normalized = deepcopy(payload)
    try:
        version = int(normalized.get("version") or 1)
        revision = int(normalized.get("revision") or 0)
    except (TypeError, ValueError) as exc:
        raise JsonStoreReadError(f"研究篮子状态版本字段无效: {path}") from exc
    if version not in {1, STATE_VERSION}:
        raise JsonStoreReadError(f"不支持的研究篮子状态版本={version}: {path}")
    if revision < 0:
        raise JsonStoreReadError(f"研究篮子状态revision不能为负数: {path}")
    normalized["version"] = STATE_VERSION
    normalized["revision"] = revision
    return normalized


def load_research_state(path: str | Path) -> dict:
    """Strictly read state; legacy v1 payloads are upgraded in memory only."""

    target = Path(path)
    if not target.exists():
        raise JsonStoreReadError(f"研究篮子状态不存在: {target}")
    return _normalized_state(read_json(target), path=target)


def initialize_research_state(path: str | Path, initial_state: dict) -> dict:
    """Create state at revision zero without replacing an existing file."""

    target = Path(path)

    def initialize(current):
        if current is not None:
            return _normalized_state(current, path=target)
        created = _normalized_state(initial_state, path=target)
        created["revision"] = 0
        return created

    return update_json(target, initialize)


def update_research_state(
    path: str | Path,
    expected_revision: int,
    mutator: Callable[[dict], dict],
) -> dict:
    """Atomically update state only when the persisted revision still matches."""

    target = Path(path)
    expected = int(expected_revision)

    def compare_and_swap(current):
        if current is None:
            raise JsonStoreReadError(f"研究篮子状态不存在: {target}")
        normalized = _normalized_state(current, path=target)
        actual = int(normalized["revision"])
        if actual != expected:
            raise ResearchStateConflict(
                f"研究篮子状态冲突: expected_revision={expected} actual_revision={actual}"
            )
        updated = mutator(deepcopy(normalized))
        if not isinstance(updated, dict):
            raise ValueError("研究篮子状态mutator必须返回对象")
        updated = _normalized_state(updated, path=target)
        updated["version"] = STATE_VERSION
        updated["revision"] = actual + 1
        return updated

    return update_json(target, compare_and_swap)
