"""通知渠道配置的单一真值与兼容规范化。"""

from __future__ import annotations

from collections.abc import Callable, Mapping


DEFAULT_NOTIFICATION_METHOD = "both"
FORM_NOTIFICATION_METHODS = ("email", "pushplus", "both")
LEGACY_NOTIFICATION_METHODS = ("page_only",)
VALID_NOTIFICATION_METHODS = frozenset(
    FORM_NOTIFICATION_METHODS + LEGACY_NOTIFICATION_METHODS
)


def normalize_notification_goals(
    goals: Mapping | None,
    *,
    logger: Callable[[str], object] | None = None,
) -> dict:
    """补齐渠道缺省并保留其余通知字段。"""

    normalized = dict(goals or {})
    method = str(normalized.get("method") or "").strip().lower()
    if method not in VALID_NOTIFICATION_METHODS:
        method = DEFAULT_NOTIFICATION_METHOD
    email = str(normalized.get("email") or "").strip()
    normalized["method"] = method
    normalized["email"] = email
    if logger is not None:
        logger(f"[通知配置] method={method} email={'有' if email else '无'}")
    return normalized
