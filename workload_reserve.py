"""Workload-aware monitoring reserve calculations."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math
from typing import Mapping

from project_time import SHANGHAI_TZ
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


def _parse_timestamp(value) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.astimezone(timezone.utc)


def _entry_is_in_epoch(entry: Mapping, epoch_started_at: datetime | None) -> bool:
    if epoch_started_at is None:
        return True
    try:
        recorded_at = _parse_timestamp(entry.get("recorded_at"))
    except ValueError:
        recorded_at = None
    if recorded_at is not None:
        return recorded_at >= epoch_started_at

    # Legacy entries can lack a timestamp. A day strictly before the epoch is
    # safely excluded; same-day or unknown rows stay in-epoch conservatively.
    day_key = _entry_day(entry)
    epoch_day = epoch_started_at.astimezone(SHANGHAI_TZ).date().isoformat()
    return not day_key or day_key >= epoch_day


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
            "entry_total": 0,
            "classified_entry_total": 0,
        }
        for day_key in day_keys
    }
    entry_totals = {day_key: 0 for day_key in day_keys}
    epoch_started_at_raw = str(reserve_config.get("epoch_started_at") or "").strip()
    epoch_started_at = _parse_timestamp(epoch_started_at_raw)
    manual_live_lifetime = 0
    manual_live_in_epoch = 0
    canary_lifetime = 0
    canary_in_epoch = 0

    for entry in (usage_payload or {}).get("entries") or []:
        if not isinstance(entry, Mapping):
            continue
        count = _source_count(entry, source)
        if not count:
            continue
        workload = normalize_workload_class(entry.get("workload_class"))
        if workload == MANUAL_LIVE:
            manual_live_lifetime += count
            if _entry_is_in_epoch(entry, epoch_started_at):
                manual_live_in_epoch += count
        elif workload == CANARY:
            canary_lifetime += count
            if _entry_is_in_epoch(entry, epoch_started_at):
                canary_in_epoch += count
        day_key = _entry_day(entry)
        if day_key not in rows:
            continue
        rows[day_key][workload] += count
        rows[day_key]["entry_total"] += count
        if workload != UNKNOWN:
            rows[day_key]["classified_entry_total"] += count
        entry_totals[day_key] += count

    aggregate_dates = (usage_payload or {}).get("dates") or {}
    for day_key in day_keys:
        aggregate_count = max(
            0,
            int(((aggregate_dates.get(day_key) or {}).get(source, 0)) or 0),
        )
        # Aggregate calls without an entry cannot be classified. Pure legacy
        # days are estimated at the floor; mixed days keep the unknown amount.
        rows[day_key][UNKNOWN] += max(0, aggregate_count - entry_totals[day_key])
        rows[day_key]["aggregate_total"] = aggregate_count
        rows[day_key]["telemetry_consistent"] = (
            aggregate_count == entry_totals[day_key]
        )

    scheduled_floor = max(0, int(reserve_config.get("minimum_daily_p90") or 10))
    for day_key in day_keys:
        row = rows[day_key]
        has_records = row["entry_total"] > 0 or row["aggregate_total"] > 0
        classified_total = row["classified_entry_total"]
        unknown_total = row[UNKNOWN]
        if not has_records:
            day_type = "telemetry_missing"
            sample_value = scheduled_floor
        elif classified_total == 0 and unknown_total > 0:
            day_type = "pure_unknown"
            sample_value = scheduled_floor
        elif classified_total > 0 and unknown_total == 0:
            day_type = "fully_classified"
            sample_value = row[SCHEDULED_USER_MONITOR]
        else:
            day_type = "mixed"
            sample_value = max(
                row[SCHEDULED_USER_MONITOR] + unknown_total,
                scheduled_floor,
            )
        row["day_type"] = day_type
        row["telemetry_missing"] = day_type == "telemetry_missing"
        row["sample_value"] = sample_value
        # Compatibility alias retained for existing reports and tests.
        row["reserve_basis"] = sample_value

    observed_raw_p90 = _nearest_rank(
        [rows[day_key]["sample_value"] for day_key in day_keys],
        0.90,
    )
    effective_scheduled_p90 = max(observed_raw_p90, scheduled_floor)
    fully_classified_days = [
        day_key
        for day_key in day_keys
        if rows[day_key]["day_type"] == "fully_classified"
    ]
    pure_unknown_days = [
        day_key
        for day_key in day_keys
        if rows[day_key]["day_type"] == "pure_unknown"
    ]
    mixed_days = [
        day_key for day_key in day_keys if rows[day_key]["day_type"] == "mixed"
    ]
    telemetry_missing_days = [
        day_key
        for day_key in day_keys
        if rows[day_key]["day_type"] == "telemetry_missing"
    ]
    cold_start_active = len(fully_classified_days) != window_days
    trailing_fully_classified = 0
    for day_key in reversed(day_keys):
        if rows[day_key]["day_type"] != "fully_classified":
            break
        trailing_fully_classified += 1
    days_until_exit = max(0, window_days - trailing_fully_classified)
    cold_start_expected_exit_at = (
        as_of if not cold_start_active else as_of + timedelta(days=days_until_exit)
    ).isoformat()

    target_date = date.fromisoformat(str(reserve_config.get("target_date")))
    days_remaining = max(0, (target_date - as_of).days)
    multiplier = max(0.0, float(reserve_config.get("safety_multiplier") or 1.2))
    manual_buffer = max(0, int(reserve_config.get("manual_live_buffer") or 30))
    canary_buffer = max(0, int(reserve_config.get("canary_buffer") or 12))
    monitoring_reserve = max(
        0,
        math.ceil(effective_scheduled_p90 * days_remaining * multiplier)
        + manual_buffer,
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
        "reserve_window_days": window_days,
        "fully_classified_days": fully_classified_days,
        "pure_unknown_days": pure_unknown_days,
        "mixed_days": mixed_days,
        "telemetry_missing_days": telemetry_missing_days,
        "observed_raw_p90": observed_raw_p90,
        "effective_scheduled_p90": effective_scheduled_p90,
        "scheduled_daily_floor": scheduled_floor,
        "cold_start_active": cold_start_active,
        "cold_start_reason": (
            "window_contains_unclassified_days"
            if cold_start_active
            else "seven_fully_classified_days"
        ),
        "cold_start_estimated": cold_start_active,
        "cold_start_exit_condition": "最近7个完整上海日全部为完全分类日",
        "cold_start_expected_exit_at": cold_start_expected_exit_at,
        # Compatibility aliases for consumers introduced before cold-start
        # day typing became explicit.
        "raw_scheduled_daily_p90": observed_raw_p90,
        "scheduled_daily_p90": effective_scheduled_p90,
        "minimum_daily_p90": scheduled_floor,
        "minimum_floor_applied": effective_scheduled_p90 != observed_raw_p90,
        "days_remaining": days_remaining,
        "target_date": target_date.isoformat(),
        "safety_multiplier": multiplier,
        "reserve_epoch_started_at": epoch_started_at_raw or None,
        "manual_live_buffer": manual_buffer,
        "manual_live_lifetime": manual_live_lifetime,
        "manual_live_in_epoch": manual_live_in_epoch,
        "manual_live_buffer_remaining": max(0, manual_buffer - manual_live_in_epoch),
        "canary_buffer": canary_buffer,
        "canary_lifetime": canary_lifetime,
        "canary_in_epoch": canary_in_epoch,
        "canary_buffer_remaining": max(0, canary_buffer - canary_in_epoch),
        # Compatibility aliases now carry the only value safe for guards.
        "manual_live_used": manual_live_in_epoch,
        "canary_used": canary_in_epoch,
        "research_batch_calls": research_batch_calls,
        "scheduled_anomaly_threshold": anomaly_threshold,
        "scheduled_anomaly_consecutive_days": anomaly_days_required,
        "scheduled_anomaly_days": [row["day"] for row in latest_rows],
        "scheduled_anomaly": scheduled_anomaly,
        "monitoring_reserve": monitoring_reserve,
    }
