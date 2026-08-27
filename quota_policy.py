"""Central quota semantics for purchased packs and monthly providers."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import math
from typing import Mapping

from project_time import SHANGHAI_TZ
from workload_reserve import calculate_workload_reserve


PURCHASED_PACKS = "purchased_packs"
MONTHLY = "monthly"
LEGACY_TOTAL = "legacy_total"
WORKLOAD_P90 = "workload_p90"


def policy_kind(policy) -> str:
    if isinstance(policy, Mapping):
        declared = str(policy.get("kind") or "").strip().lower()
        if declared:
            return declared
        if "monthly" in policy:
            return MONTHLY
        if "packs" in policy:
            return PURCHASED_PACKS
    return LEGACY_TOTAL


def total_limit(policy) -> int:
    """Return the configured provider limit without inspecting usage."""
    kind = policy_kind(policy)
    if kind == PURCHASED_PACKS:
        return sum(
            max(0, int((item or {}).get("added") or 0))
            for item in (policy.get("packs") or [])
            if isinstance(item, Mapping)
        )
    if kind == MONTHLY:
        return max(0, int((policy or {}).get("monthly") or 0))
    if isinstance(policy, Mapping):
        return max(0, int(policy.get("total") or policy.get("limit") or 0))
    return max(0, int(policy or 0))


def used(policy, snapshot: Mapping | None, source: str) -> int:
    """Return usage in the policy's epoch (cumulative or current month)."""
    bucket = "month" if policy_kind(policy) == MONTHLY else "cumulative"
    values = (snapshot or {}).get(bucket) or {}
    ledger_used = max(0, int(values.get(str(source), 0) or 0))
    if policy_kind(policy) != PURCHASED_PACKS or not isinstance(policy, Mapping):
        return ledger_used
    reconciliation = policy.get("reconciliation") or {}
    adjustment = (
        int(reconciliation.get("unrecorded_usage_adjustment") or 0)
        if isinstance(reconciliation, Mapping)
        else 0
    )
    return max(0, ledger_used + adjustment)


def remaining(policy, snapshot: Mapping | None, source: str) -> int:
    return max(0, total_limit(policy) - used(policy, snapshot, source))


def _as_date(value: date | str | None) -> date:
    if isinstance(value, date):
        return value
    if value:
        return date.fromisoformat(str(value))
    return datetime.now(SHANGHAI_TZ).date()


def _nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    rank = max(1, math.ceil(float(percentile) * len(ordered)))
    return ordered[min(rank - 1, len(ordered) - 1)]


def recent_non_research_daily_usage(
    usage_payload: Mapping | None,
    *,
    source: str,
    as_of: date | str | None,
    window_days: int,
    research_round_ids: set[str] | None = None,
) -> list[int]:
    """Return calendar-day actual counts, subtracting identified research rounds."""
    current = _as_date(as_of)
    window = max(1, int(window_days or 1))
    start = current - timedelta(days=window - 1)
    dates = (usage_payload or {}).get("dates") or {}
    counts = {
        (start + timedelta(days=offset)).isoformat(): max(
            0,
            int(
                ((dates.get((start + timedelta(days=offset)).isoformat()) or {}).get(source, 0))
                or 0
            ),
        )
        for offset in range(window)
    }
    excluded = {str(value) for value in (research_round_ids or set())}
    if excluded:
        for entry in (usage_payload or {}).get("entries") or []:
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("round_id") or "") not in excluded:
                continue
            day_key = str(entry.get("day") or entry.get("recorded_at") or "")[:10]
            if day_key not in counts:
                continue
            research_count = int(((entry.get("counts") or {}).get(source, 0)) or 0)
            counts[day_key] = max(0, counts[day_key] - research_count)
    return [counts[key] for key in sorted(counts)]


def workload_reserve_details(
    policy,
    *,
    usage_payload: Mapping | None,
    source: str,
    as_of: date | str | None = None,
) -> dict:
    """Return the auditable derivation for a workload-aware reserve."""

    if not isinstance(policy, Mapping):
        raise ValueError("workload reserve requires a mapping policy")
    raw = policy.get("reserve") or {}
    if not isinstance(raw, Mapping) or str(raw.get("kind") or "").lower() != WORKLOAD_P90:
        raise ValueError("policy does not use workload_p90 reserve")
    return calculate_workload_reserve(
        raw,
        usage_payload=usage_payload,
        source=str(source),
        as_of=_as_date(as_of),
    )


def reserve(
    policy,
    *,
    usage_payload: Mapping | None = None,
    source: str,
    as_of: date | str | None = None,
    research_round_ids: set[str] | None = None,
) -> int:
    """Return the monitoring reserve defined by the provider policy."""
    if not isinstance(policy, Mapping):
        return 0
    raw = policy.get("reserve") or 0
    if not isinstance(raw, Mapping):
        return max(0, int(raw or 0))
    reserve_kind = str(raw.get("kind") or "").strip().lower()
    if reserve_kind == WORKLOAD_P90:
        return workload_reserve_details(
            policy,
            usage_payload=usage_payload,
            source=source,
            as_of=as_of,
        )["monitoring_reserve"]
    if reserve_kind != "monitoring_p90":
        return max(0, int(raw.get("amount") or 0))

    current = _as_date(as_of)
    target = _as_date(raw.get("target_date"))
    days_remaining = max(0, (target - current).days)
    daily = recent_non_research_daily_usage(
        usage_payload,
        source=source,
        as_of=current,
        window_days=int(raw.get("window_days") or 7),
        research_round_ids=research_round_ids,
    )
    if any(daily):
        daily_p90 = _nearest_rank(daily, 0.90)
    else:
        daily_p90 = max(0.0, float(raw.get("fallback_daily_p90") or 11.5))
    multiplier = max(0.0, float(raw.get("safety_multiplier") or 1.2))
    emergency = max(0, int(raw.get("emergency_calls") or 20))
    return max(0, math.ceil(daily_p90 * days_remaining * multiplier + emergency))


def research_available(
    policy,
    snapshot: Mapping | None,
    source: str,
    *,
    usage_payload: Mapping | None = None,
    as_of: date | str | None = None,
    research_round_ids: set[str] | None = None,
) -> int:
    available = remaining(policy, snapshot, source) - reserve(
        policy,
        usage_payload=usage_payload,
        source=source,
        as_of=as_of,
        research_round_ids=research_round_ids,
    )
    raw = policy.get("reserve") if isinstance(policy, Mapping) else None
    if isinstance(raw, Mapping) and str(raw.get("kind") or "").lower() == WORKLOAD_P90:
        return available
    return max(0, available)


def metrics(
    policy,
    snapshot: Mapping | None,
    source: str,
    *,
    usage_payload: Mapping | None = None,
    as_of: date | str | None = None,
    research_round_ids: set[str] | None = None,
) -> dict:
    policy_reserve = reserve(
        policy,
        usage_payload=usage_payload,
        source=source,
        as_of=as_of,
        research_round_ids=research_round_ids,
    )
    policy_remaining = remaining(policy, snapshot, source)
    raw_reserve = policy.get("reserve") if isinstance(policy, Mapping) else None
    workload_aware = (
        isinstance(raw_reserve, Mapping)
        and str(raw_reserve.get("kind") or "").lower() == WORKLOAD_P90
    )
    available = policy_remaining - policy_reserve
    if not workload_aware:
        available = max(0, available)
    result = {
        "kind": policy_kind(policy),
        "total_limit": total_limit(policy),
        "used": used(policy, snapshot, source),
        "remaining": policy_remaining,
        "reserve": policy_reserve,
        "research_available": available,
    }
    if workload_aware:
        details = workload_reserve_details(
            policy,
            usage_payload=usage_payload,
            source=source,
            as_of=as_of,
        )
        details["research_available"] = available
        details["next_batch_can_start"] = (
            available >= details["research_batch_calls"]
        )
        result["reserve_details"] = details
    return result
