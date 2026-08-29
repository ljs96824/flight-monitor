"""订阅稳定身份的单一入口。"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
from uuid import UUID, uuid4


def subscription_id(subscription: dict) -> str:
    """读取规范身份；兼容历史 `id` 字段。"""
    return str(
        subscription.get("subscription_id")
        or subscription.get("id")
        or ""
    ).strip()


def persisted_subscription_id(subscription) -> str:
    """Return only the persisted M0 identity using exact stripped text."""

    if not isinstance(subscription, dict):
        return ""
    value = subscription.get("subscription_id")
    if not isinstance(value, str):
        return ""
    return value.strip()


def mask_subscription_id(value) -> str:
    """Render a stable identifier without retaining or exposing its raw value."""

    stable_id = str(value or "").strip()
    try:
        canonical_uuid = str(UUID(stable_id))
    except ValueError:
        canonical_uuid = ""
    if stable_id.lower() == canonical_uuid:
        prefix = stable_id[:8]
    else:
        digest = hashlib.sha256(stable_id.encode("utf-8")).hexdigest()[:8]
        prefix = f"sha256:{digest}"
    return f"{prefix}********"


def ensure_subscription_id(
    subscription: dict,
    *,
    id_factory: Callable[[], UUID | str] = uuid4,
) -> tuple[str, bool]:
    """确保记录落有 `subscription_id`，返回（身份，是否补发）。"""
    current = subscription_id(subscription)
    if current:
        if not str(subscription.get("subscription_id") or "").strip():
            subscription["subscription_id"] = current
            return current, True
        return current, False

    generated = str(id_factory())
    subscription["subscription_id"] = generated
    return generated, True
