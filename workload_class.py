"""Explicit workload classes for real external API usage."""

from __future__ import annotations


SCHEDULED_USER_MONITOR = "scheduled_user_monitor"
RESEARCH_COHORT = "research_cohort"
MANUAL_LIVE = "manual_live"
CANARY = "canary"
UNKNOWN = "unknown"

WORKLOAD_CLASSES = frozenset(
    {
        SCHEDULED_USER_MONITOR,
        RESEARCH_COHORT,
        MANUAL_LIVE,
        CANARY,
        UNKNOWN,
    }
)


def normalize_workload_class(value) -> str:
    """Return a registered class, conservatively falling back to unknown."""

    normalized = str(value or "").strip().lower()
    return normalized if normalized in WORKLOAD_CLASSES else UNKNOWN
