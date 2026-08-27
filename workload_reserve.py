"""Workload-aware monitoring reserve calculations."""

from __future__ import annotations

from datetime import date, timedelta
import math
from typing import Mapping

from workload_class import (
    CANARY,
    MANUAL_LIVE,
    RESEARCH_COHORT,
    SCHEDULED_USER_MONITOR,
    UNKNOWN,
    normalize_workload_class,
)


def _entry_day(entry: Mapping) -> str:
    return str(entry.get("day") or entry.get("recorded_at") or "")[:10]


def _source_count(entry: Mapping, source: str) -> int:
    return max(0, int(((entry.get("counts") or {}).get(source, 0)) or 0))


def _nearest_rank(values: list[int], percentile: float) -> int:
    ordered = sorted(max(0, int(value or 0)) for value in values)
    if not ordered:
        return 0
    rank = max(1, math.ceil(float(percentile) * len(ordered)))
    return ordered[min(rank - 1, len(ordered) - 1)]


def calculate_workload_reserve(
    reserve_config: Mapping,
    *,
    usage_payload: Mapping | None,
    source: str,
    as_of: date,
) -> dict:
    """Return reserve inputs without mutating historical usage rows."""

    window_days = max(1, int(reserve_config.get("window_complete_days") or 7))
    first_day = as_of - timedelta(days=window_days)
    day_keys = [
        (first_day + timedelta(days=offset)).isoformat()
        for offset in range(window_days)
    ]
    rows = {
        day_key: {
            "day": day_key,
            SCHEDULED_USER_MONITOR: 0,
            RESEARCH_COHORT: 0,
            MANUAL_LIVE: 0,
            CANARY: 0,
            UNKNOWN: 0,
        }
        for day_key in day_keys
    }
    entry_totals = {day_key: 0 for day_key in day_keys}
    manual_live_used = 0
    canary_used = 0

    for entry in (usage_payload or {}).get("entries") or []:
        if not isinstance(entry, Mapping):
            continue
        count = _source_count(entry, source)
        if not count:
            continue
        workload = normalize_workload_class(entry.get("workload_class"))
        if workload == MANUAL_LIVE:
            manual_live_used += count
        elif workload == CANARY:
            canary_used += count
        day_key = _entry_day(entry)
        if day_key not in rows:
            continue
        rows[day_key][workload] += count
        entry_totals[day_key] += count

    aggregate_dates = (usage_payload or {}).get("dates") or {}
    for day_key in day_keys:
        aggregate_count = max(
            0,
            int(((aggregate_dates.get(day_key) or {}).get(source, 0)) or 0),
        )
        # Old ledgers may have only daily aggregates. Their unclassified
        # remainder is conservatively treated as monitoring demand.
        rows[day_key][UNKNOWN] += max(0, aggregate_count - entry_totals[day_key])
        rows[day_key]["reserve_basis"] = (
            rows[day_key][SCHEDULED_USER_MONITOR] + rows[day_key][UNKNOWN]
        )

    raw_p90 = _nearest_rank(
        [rows[day_key]["reserve_basis"] for day_key in day_keys],
        0.90,
    )
    minimum_p90 = max(0, int(reserve_config.get("minimum_daily_p90") or 10))
    scheduled_p90 = max(raw_p90, minimum_p90)
    target_date = date.fromisoformat(str(reserve_config.get("target_date")))
    days_remaining = max(0, (target_date - as_of).days)
    multiplier = max(0.0, float(reserve_config.get("safety_multiplier") or 1.2))
    manual_buffer = max(0, int(reserve_config.get("manual_live_buffer") or 30))
    monitoring_reserve = max(
        0,
        math.ceil(scheduled_p90 * days_remaining * multiplier) + manual_buffer,
    )
    anomaly_threshold = max(
        0,
        int(reserve_config.get("scheduled_anomaly_threshold") or 12),
    )
    anomaly_days_required = max(
        1,
        int(reserve_config.get("scheduled_anomaly_consecutive_days") or 2),
    )
    latest_rows = [rows[day_key] for day_key in day_keys[-anomaly_days_required:]]
    scheduled_anomaly = len(latest_rows) == anomaly_days_required and all(
        row[SCHEDULED_USER_MONITOR] > anomaly_threshold for row in latest_rows
    )
    research_batch_calls = max(
        1,
        int(reserve_config.get("research_batch_calls") or 30),
    )
    return {
        "daily_counts": [rows[day_key] for day_key in day_keys],
        "raw_scheduled_daily_p90": raw_p90,
        "scheduled_daily_p90": scheduled_p90,
        "minimum_daily_p90": minimum_p90,
        "minimum_floor_applied": scheduled_p90 != raw_p90,
        "days_remaining": days_remaining,
        "target_date": target_date.isoformat(),
        "safety_multiplier": multiplier,
        "manual_live_buffer": manual_buffer,
        "manual_live_used": manual_live_used,
        "canary_used": canary_used,
        "research_batch_calls": research_batch_calls,
        "scheduled_anomaly_threshold": anomaly_threshold,
        "scheduled_anomaly_consecutive_days": anomaly_days_required,
        "scheduled_anomaly_days": [row["day"] for row in latest_rows],
        "scheduled_anomaly": scheduled_anomaly,
        "monitoring_reserve": monitoring_reserve,
    }
