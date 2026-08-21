"""通知渠道配置的单一真值与兼容规范化。"""

from __future__ import annotations

from collections.abc import Callable, Mapping


DEFAULT_NOTIFICATION_METHOD = "both"
FORM_NOTIFICATION_METHODS = ("email", "pushplus", "both")
LEGACY_NOTIFICATION_METHODS = ("page_only",)
VALID_NOTIFICATION_METHODS = frozenset(
    FORM_NOTIFICATION_METHODS + LEGACY_NOTIFICATION_METHODS
)
DEFAULT_NOTIFICATION_PRIVACY_LEVEL = "full"
VALID_NOTIFICATION_PRIVACY_LEVELS = frozenset({"full", "redacted", "minimal"})


def resolve_notification_privacy_level(goals: Mapping | None) -> str:
    """解析通知隐私等级；缺失或非法值一律保持兼容的 full。"""
    raw = goals if isinstance(goals, Mapping) else {}
    level = str(
        raw.get("privacy_level")
        or raw.get("notification_privacy_level")
        or DEFAULT_NOTIFICATION_PRIVACY_LEVEL
    ).strip().lower()
    if level not in VALID_NOTIFICATION_PRIVACY_LEVELS:
        return DEFAULT_NOTIFICATION_PRIVACY_LEVEL
    return level


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
    privacy_level = resolve_notification_privacy_level(normalized)
    normalized.pop("notification_privacy_level", None)
    if privacy_level == DEFAULT_NOTIFICATION_PRIVACY_LEVEL:
        normalized.pop("privacy_level", None)
    else:
        normalized["privacy_level"] = privacy_level
    if logger is not None:
        logger(f"[通知配置] method={method} email={'有' if email else '无'}")
    return normalized
