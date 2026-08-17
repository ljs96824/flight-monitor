"""订阅稳定身份的单一入口。"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID, uuid4


def subscription_id(subscription: dict) -> str:
    """读取规范身份；兼容历史 `id` 字段。"""
    return str(
        subscription.get("subscription_id")
        or subscription.get("id")
        or ""
    ).strip()


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