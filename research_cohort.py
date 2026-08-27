"""State machine for the quota-bounded PVG-KIX research cohort.

This module only plans and accounts for research cells. It never calls a
flight source directly; execution remains owned by ``CollectionPlan``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import sqlite3
from typing import Callable, Iterable


RESEARCH_COHORT_ID = "pvg-kix-tcurve-v2"
RESEARCH_ROUTE = {
    "route": "PVG->KIX",
    "origin": "PVG",
    "dest": "KIX",
    "route_type": "international",
    "sources": ("juhe",),
}
ANCHOR_DATES = {
    "anchor_normal": "2026-09-08",
    "anchor_holiday": "2026-10-01",
}
PROBE_TARGET_SEQUENCES = {
    "probe_1": (7, 10, 3, 5),
    "probe_2": (14, 21, 17, 24),
    "probe_3": (28, 35, 49, 63),
    "probe_4": (42, 56, 70, 84),
}
PROBE_MIN_VALID_N = 5


@dataclass(frozen=True)
class ResearchSchedule:
    requests: list[dict]
    events: list[dict]


def _new_cohort_state() -> dict:
    return {
        "version": "research_cohort_v2",
        "cohort_id": RESEARCH_COHORT_ID,
        "anchors": {
            slot: {"depart_date": depart_date, "status": "active"}
            for slot, depart_date in ANCHOR_DATES.items()
        },
        "probes": {
            slot: {
                "target_sequence": list(sequence),
                "target_index": 0,
                "target_t": sequence[0],
                "probe_valid_n": 0,
                "status": "active",
            }
            for slot, sequence in PROBE_TARGET_SEQUENCES.items()
        },
        "events": [],
    }


def _cohort_state(state: dict) -> dict:
    cohort = state.get("research_cohort_v2")
    if not isinstance(cohort, dict):
        cohort = _new_cohort_state()
        state["research_cohort_v2"] = cohort
    return cohort


def _request(
    *,
    slot: str,
    depart_date: str,
    sample_role: str,
    target_t: int | None = None,
) -> dict:
    item = {
        **RESEARCH_ROUTE,
        "depart_date": str(depart_date),
        "queue": f"{RESEARCH_COHORT_ID}:{slot}",
        "cabin_class": "economy",
        "cohort_id": RESEARCH_COHORT_ID,
        "sample_role": sample_role,
        "slot": slot,
    }
    if target_t is not None:
        item["target_t"] = int(target_t)
    return item


def _advance_probe(probe: dict) -> None:
    sequence = [int(value) for value in (probe.get("target_sequence") or [])]
    next_index = int(probe.get("target_index") or 0) + 1
    probe["probe_valid_n"] = 0
    if next_index >= len(sequence):
        probe["target_index"] = len(sequence)
        probe["target_t"] = None
        probe["status"] = "completed"
        return
    probe["target_index"] = next_index
    probe["target_t"] = sequence[next_index]
    probe["status"] = "active"


def _record_event(cohort: dict, events: list[dict], event: dict) -> None:
    events.append(event)
    cohort.setdefault("events", []).append(event)


def prepare_research_requests(
    state: dict,
    *,
    today: date,
    user_monitor_dates: set[str],
) -> ResearchSchedule:
    """Return the six-slot schedule, honoring anchor/user/probe priority."""
    cohort = _cohort_state(state)
    requests: list[dict] = []
    events: list[dict] = []
    active_anchor_dates: set[str] = set()

    for slot in ANCHOR_DATES:
        anchor = cohort["anchors"][slot]
        depart_text = str(anchor["depart_date"])
        depart_day = date.fromisoformat(depart_text)
        if depart_day < today:
            anchor["status"] = "completed"
            continue
        if anchor.get("status") == "completed":
            continue
        active_anchor_dates.add(depart_text)
        requests.append(
            _request(
                slot=slot,
                depart_date=depart_text,
                sample_role="trajectory_anchor",
            )
        )

    for slot in PROBE_TARGET_SEQUENCES:
        probe = cohort["probes"][slot]
        while probe.get("status") != "completed":
            target_t = probe.get("target_t")
            if target_t is None:
                probe["status"] = "completed"
                break
            depart_text = (today + timedelta(days=int(target_t))).isoformat()
            if depart_text in active_anchor_dates:
                _record_event(
                    cohort,
                    events,
                    {
                        "kind": "deduped_with_anchor",
                        "slot": slot,
                        "depart_date": depart_text,
                        "target_t": int(target_t),
                    },
                )
                _advance_probe(probe)
                continue
            if depart_text in user_monitor_dates:
                _record_event(
                    cohort,
                    events,
                    {
                        "kind": "deduped_with_user_monitor",
                        "slot": slot,
                        "depart_date": depart_text,
                        "target_t": int(target_t),
                    },
                )
                _advance_probe(probe)
                continue
            requests.append(
                _request(
                    slot=slot,
                    depart_date=depart_text,
                    sample_role="cross_sectional_probe",
                    target_t=int(target_t),
                )
            )
            break

    return ResearchSchedule(requests=requests, events=events)


def apply_research_round_outcomes(
    state: dict,
    *,
    requests: Iterable[dict],
    round_id: str,
    today: date,
    db_path: str | Path,
    cell_state_loader: Callable | None = None,
) -> list[dict]:
    """Advance probes only for valid cells and retire an anchor after T=0."""
    if cell_state_loader is None:
        from collection_ledger import load_daily_collection_state

        cell_state_loader = load_daily_collection_state

    cohort = _cohort_state(state)
    outcomes: list[dict] = []
    for item in requests:
        slot = str(item.get("slot") or "")
        if not slot:
            continue
        result = cell_state_loader(
            round_id=round_id,
            origin_airport=item["origin"],
            dest_airport=item["dest"],
            depart_date=item["depart_date"],
            cabin_class=item.get("cabin_class") or "economy",
            route_type=item.get("route_type") or "international",
            observed_day_shanghai=today.isoformat(),
            db_path=db_path,
        )
        cell_state = str((result or {}).get("state") or "missing")
        outcomes.append({"slot": slot, "state": cell_state})
        if slot.startswith("anchor_"):
            if date.fromisoformat(str(item["depart_date"])) <= today:
                cohort["anchors"][slot]["status"] = "completed"
            continue
        probe = cohort["probes"][slot]
        if cell_state != "valid":
            continue
        probe["probe_valid_n"] = int(probe.get("probe_valid_n") or 0) + 1
        if probe["probe_valid_n"] >= PROBE_MIN_VALID_N:
            _advance_probe(probe)
    return outcomes


def record_research_ledger_degraded(
    state: dict,
    *,
    round_id: str,
    today: date,
    actual_requests: int,
) -> dict:
    """Record an evidence-degraded round without advancing research state."""
    cohort = _cohort_state(state)
    record = {
        "round_id": str(round_id),
        "observed_day_shanghai": today.isoformat(),
        "status": "ledger_degraded",
        "ledger_degraded": True,
        "research_progress_applied": False,
        "valid_research_day": False,
        "plan_actual_requests": max(0, int(actual_requests or 0)),
    }
    cohort["last_round"] = record
    cohort.setdefault("events", []).append(
        {
            "kind": "ledger_degraded",
            **record,
        }
    )
    return record


def _subscription_value(subscription: dict, key: str):
    value = subscription.get(key)
    if value not in (None, ""):
        return value
    for section_name in ("basic", "preferences", "hard_constraints", "constraints"):
        section = subscription.get(section_name)
        if isinstance(section, dict) and section.get(key) not in (None, ""):
            return section[key]
    return None


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _airport_values(subscription: dict, active: str, all_key: str, fallback: str) -> set[str]:
    value = (
        _subscription_value(subscription, active)
        or _subscription_value(subscription, all_key)
        or _subscription_value(subscription, fallback)
    )
    if isinstance(value, str):
        value = [part.strip() for part in value.replace("、", ",").split(",")]
    return {str(part).strip().upper() for part in (value or []) if str(part).strip()}


def active_user_monitor_dates(
    subscriptions: Iterable[dict],
    *,
    origin: str,
    dest: str,
) -> set[str]:
    """Return dates already covered by active user monitors in one direction."""
    origin = str(origin).upper()
    dest = str(dest).upper()
    dates: set[str] = set()
    for subscription in subscriptions:
        if str(subscription.get("status") or "active").lower() not in {"active", "enabled"}:
            continue
        origins = _airport_values(
            subscription, "origin_airports_active", "origin_airports_all", "origin"
        )
        destinations = _airport_values(
            subscription,
            "destination_airports_active",
            "destination_airports_all",
            "destination",
        )
        depart_date = str(_subscription_value(subscription, "depart_date") or "")
        if origin in origins and dest in destinations and depart_date:
            dates.add(depart_date)
        if (
            _as_bool(_subscription_value(subscription, "round_trip"))
            and origin in destinations
            and dest in origins
            and _subscription_value(subscription, "return_date")
        ):
            dates.add(str(_subscription_value(subscription, "return_date")))
    return dates


def simulate_research_quota(
    *,
    basket_keys: set,
    subscription_keys: set,
    scheduled_subscription_runs_per_day: int,
    other_non_subscription_calls_per_day: int,
    quota_remaining: int,
    retries_per_request: int = 1,
    monitoring_reserve: int = 0,
) -> dict:
    """Model daily Juhe use across the basket and repeated subscription rounds."""
    basket = set(basket_keys)
    subscriptions = set(subscription_keys)
    subscription_runs = max(0, int(scheduled_subscription_runs_per_day or 0))
    other = max(0, int(other_non_subscription_calls_per_day or 0))
    retries = max(0, int(retries_per_request or 0))
    # The basket and each scheduled subscription round are separate force-fresh
    # processes. Equal keys therefore remain separate real-request costs.
    basket_retry_ceiling = len(basket) * (1 + retries)
    subscription_daily_expected = len(subscriptions) * subscription_runs
    subscription_daily_worst_case = subscription_daily_expected * (1 + retries)
    other_daily_worst_case = other * (1 + retries)
    expected = len(basket) + subscription_daily_expected + other
    worst = (
        basket_retry_ceiling
        + subscription_daily_worst_case
        + other_daily_worst_case
    )
    remaining = max(0, int(quota_remaining or 0))
    reserve_value = max(0, int(monitoring_reserve or 0))
    expected_days = remaining // expected if expected else None
    worst_days = remaining // worst if worst else None
    return {
        "basket_planned_unique": len(basket),
        "basket_normal_actual": len(basket),
        "basket_retry_ceiling": basket_retry_ceiling,
        "subscription_planned_unique": len(subscriptions),
        "scheduled_subscription_runs_per_day": subscription_runs,
        "subscription_daily_expected": subscription_daily_expected,
        "subscription_daily_worst_case": subscription_daily_worst_case,
        "other_non_subscription_calls_per_day": other,
        "other_non_subscription_worst_case": other_daily_worst_case,
        "combined_daily_expected": expected,
        "combined_daily_worst_case": worst,
        "estimated_days_remaining": expected_days,
        "expected_days_remaining": expected_days,
        "worst_case_days_remaining": worst_days,
        "quota_remaining": remaining,
        "monitoring_reserve": reserve_value,
        "research_available": max(0, remaining - reserve_value),
        "remaining_after_research": remaining - basket_retry_ceiling,
        "complete": True,
    }


def evaluate_research_hard_gates(
    *,
    backup_evidence: dict,
    quota_simulation: dict,
    migration_status: dict,
    minimum_expected_days: int = 30,
    minimum_worst_case_days: int = 20,
) -> dict:
    quota = quota_simulation or {}
    expected_days = quota.get("expected_days_remaining")
    worst_days = quota.get("worst_case_days_remaining")
    remaining_after = quota.get("remaining_after_research")
    reserve_value = quota.get("monitoring_reserve")
    quota_complete = bool(quota.get("complete"))
    research_available_raw = quota.get("research_available")
    research_available_value = int(research_available_raw or 0)
    research_batch_calls = max(1, int(quota.get("research_batch_calls") or 30))
    scheduled_anomaly = bool(quota.get("scheduled_anomaly"))
    manual_live_used = max(
        0,
        int(
            quota.get("manual_live_in_epoch")
            if "manual_live_in_epoch" in quota
            else quota.get("manual_live_used") or 0
        ),
    )
    manual_live_buffer = max(0, int(quota.get("manual_live_buffer") or 30))
    canary_used = max(
        0,
        int(
            quota.get("canary_in_epoch")
            if "canary_in_epoch" in quota
            else quota.get("canary_used") or 0
        ),
    )
    canary_buffer = max(0, int(quota.get("canary_buffer") or 12))
    manual_counter_present = (
        "manual_live_in_epoch" in quota or "manual_live_used" in quota
    )
    canary_counter_present = "canary_in_epoch" in quota or "canary_used" in quota
    manual_buffer_ok = (
        not manual_counter_present or manual_live_used <= manual_live_buffer
    )
    canary_buffer_ok = not canary_counter_present or canary_used <= canary_buffer
    quota_ledger_healthy = bool(quota.get("quota_ledger_healthy", True))
    batch_guard_ok = (
        research_available_raw is None
        or research_available_value >= research_batch_calls
    )
    backup = backup_evidence or {}
    backup_checks = backup.get("checks") or {}
    migration = migration_status or {}
    checks = {
        "quota_ledger_healthy": quota_ledger_healthy,
        "expected_days_remaining": (
            quota_complete
            and expected_days is not None
            and int(expected_days) >= max(0, int(minimum_expected_days))
        ),
        "worst_case_days_remaining": (
            quota_complete
            and worst_days is not None
            and int(worst_days) >= max(0, int(minimum_worst_case_days))
        ),
        "monitoring_reserve": (
            quota_complete
            and remaining_after is not None
            and reserve_value is not None
            and int(remaining_after) >= int(reserve_value)
            and batch_guard_ok
            and ("scheduled_anomaly" not in quota or not scheduled_anomaly)
            and manual_buffer_ok
            and canary_buffer_ok
        ),
        "backup_restore_verified": bool(
            backup_checks.get("backup_restore_verified")
        ),
        "off_disk_copy_verified": bool(
            backup_checks.get("off_disk_copy_verified")
        ),
        "different_device_verified": bool(
            backup_checks.get("different_device_verified")
        ),
        "off_disk_copy_fresh": bool(backup_checks.get("off_disk_copy_fresh")),
        "timestamp_migration": bool(migration.get("timestamp_ready")),
        "lineage_migration": bool(migration.get("lineage_ready")),
        "old_data_readable": bool(migration.get("old_data_readable")),
    }
    missing = [name for name, ready in checks.items() if not ready]
    current = {
        "quota_ledger_healthy": {
            "healthy": quota_ledger_healthy,
            "pending_reconciliation_count": int(
                quota.get("pending_reconciliation_count") or 0
            ),
            "error": quota.get("quota_ledger_error"),
        },
        "expected_days_remaining": expected_days,
        "worst_case_days_remaining": worst_days,
        "monitoring_reserve": {
            "remaining_after_research": remaining_after,
            "required_reserve": reserve_value,
            "reserve_details": quota.get("reserve_details") or {},
        },
        **(backup.get("current") or {}),
        "backup_restore_verified": (backup.get("current") or {}).get(
            "verified_restore_at"
        ),
        "off_disk_copy_verified": (backup.get("current") or {}).get(
            "off_disk_copy_verified"
        ),
        "different_device_verified": (backup.get("current") or {}).get(
            "different_device_verified"
        ),
        "off_disk_copy_fresh": {
            "verified_at": (backup.get("current") or {}).get(
                "off_disk_copy_verified_at"
            ),
            "age_days": (backup.get("current") or {}).get(
                "off_disk_copy_age_days"
            ),
        },
        "timestamp_migration": bool(migration.get("timestamp_ready")),
        "lineage_migration": bool(migration.get("lineage_ready")),
        "old_data_readable": bool(migration.get("old_data_readable")),
    }
    reasons = dict(backup.get("reasons") or {})
    if not quota_ledger_healthy:
        reasons["quota_ledger_healthy"] = (
            quota.get("quota_ledger_error")
            or "配额台账损坏或存在待人工对账记录"
        )
    if not quota_complete:
        for name in (
            "expected_days_remaining",
            "worst_case_days_remaining",
            "monitoring_reserve",
        ):
            reasons[name] = "全系统配额模拟不完整"
    else:
        if not checks["expected_days_remaining"]:
            reasons["expected_days_remaining"] = "预计可运行天数不足"
        if not checks["worst_case_days_remaining"]:
            reasons["worst_case_days_remaining"] = "最坏情形可运行天数不足"
        if not checks["monitoring_reserve"]:
            reasons["monitoring_reserve"] = "储备、下一批额度或异常用量守卫未通过"
    for check_name, source_name in (
        ("timestamp_migration", "timestamp_ready"),
        ("lineage_migration", "lineage_ready"),
        ("old_data_readable", "old_data_readable"),
    ):
        if not checks[check_name]:
            reasons[check_name] = f"迁移证据未就绪:{source_name}"
    requirements = {
        "minimum_expected_days": max(0, int(minimum_expected_days)),
        "minimum_worst_case_days": max(0, int(minimum_worst_case_days)),
        **(backup.get("requirements") or {}),
    }
    return {
        "ready": not missing,
        "missing": missing,
        "checks": checks,
        "current": current,
        "requirements": requirements,
        "reasons": reasons,
    }


def apply_research_quota_guard(
    state: dict,
    quota_simulation: dict,
    *,
    notifier=None,
    now: str | None = None,
) -> dict:
    """Disable research only when a workload-aware quota guard is tripped."""
    quota = quota_simulation or {}
    required = {"quota_remaining", "monitoring_reserve", "research_available"}
    if not required.issubset(quota):
        return {"triggered": False, "notified": False, "reason_codes": []}
    remaining_value = max(0, int(quota.get("quota_remaining") or 0))
    reserve_value = max(0, int(quota.get("monitoring_reserve") or 0))
    available_value = int(quota.get("research_available") or 0)
    batch_calls = max(1, int(quota.get("research_batch_calls") or 30))
    manual_used = max(
        0,
        int(
            quota.get("manual_live_in_epoch")
            if "manual_live_in_epoch" in quota
            else quota.get("manual_live_used") or 0
        ),
    )
    manual_buffer = max(0, int(quota.get("manual_live_buffer") or 30))
    canary_used = max(
        0,
        int(
            quota.get("canary_in_epoch")
            if "canary_in_epoch" in quota
            else quota.get("canary_used") or 0
        ),
    )
    canary_buffer = max(0, int(quota.get("canary_buffer") or 12))
    reason_codes = []
    if remaining_value <= reserve_value:
        reason_codes.append("monitoring_reserve_reached")
    if available_value < batch_calls:
        reason_codes.append("research_batch_budget_insufficient")
    if bool(quota.get("scheduled_anomaly")):
        reason_codes.append("scheduled_usage_anomaly")
    if manual_used > manual_buffer:
        reason_codes.append("manual_live_buffer_exceeded")
    if canary_used > canary_buffer:
        reason_codes.append("canary_buffer_exceeded")
    if not reason_codes:
        return {"triggered": False, "notified": False, "reason_codes": []}

    cohort = _cohort_state(state)
    cohort["runtime_enabled"] = False
    cohort["user_monitoring_enabled"] = True
    guard = cohort.setdefault("quota_guard", {})
    guard.update(
        {
            "triggered": True,
            "disabled_at": str(now or datetime.now().astimezone().isoformat(timespec="seconds")),
            "remaining": remaining_value,
            "reserve": reserve_value,
            "research_available": available_value,
            "reason_codes": list(reason_codes),
        }
    )
    notified = False
    if not guard.get("notification_attempted"):
        title = "[配额守卫] 研究采样已停用"
        content = (
            "[配额守卫] 研究采样已停用,用户监控继续,"
            f"余量={remaining_value} 储备={reserve_value} "
            f"原因={','.join(reason_codes)}"
        )
        guard["notification_attempted"] = True
        if notifier is not None:
            try:
                notified = bool(notifier(title, content))
            except Exception as exc:
                guard["notification_error"] = f"{type(exc).__name__}:{exc}"
        guard["notified"] = notified
    return {"triggered": True, "notified": notified, "reason_codes": reason_codes}


def research_runtime_enabled(state: dict, configured: bool) -> bool:
    if not configured:
        return False
    cohort = state.get("research_cohort_v2")
    if not isinstance(cohort, dict):
        return True
    return bool(cohort.get("runtime_enabled", True))


def load_research_round_ids(db_path: str | Path) -> set[str]:
    """Read round ids already attributed to the research cohort."""
    from tcurve import readonly_connection

    try:
        with readonly_connection(db_path) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table'"
                )
            }
            if "collection_cells" not in tables:
                return set()
            return {
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT round_id FROM collection_cells "
                    "WHERE cohort_id = ? AND round_id IS NOT NULL",
                    (RESEARCH_COHORT_ID,),
                )
                if row[0]
            }
    except (OSError, sqlite3.Error):
        return set()


def _table_columns(connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def inspect_research_migrations(
    observations_path: str | Path,
    prices_path: str | Path,
) -> dict:
    """Inspect both explicit migrations using read-only connections."""
    from tcurve import readonly_connection

    timestamp_ready = False
    lineage_ready = False
    old_data_readable = False
    try:
        with readonly_connection(observations_path) as connection:
            observation_columns = _table_columns(connection, "observations")
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table'"
                )
            }
            timestamp_ready = {
                "observed_at_utc",
                "observed_day_shanghai",
                "legacy_time_ambiguous",
            }.issubset(observation_columns)
            collection_lineage_ready = "collection_cells" in tables
            connection.execute("SELECT COUNT(*) FROM observations").fetchone()
        with readonly_connection(prices_path) as connection:
            price_lineage_ready = all(
                "round_id" in _table_columns(connection, table)
                for table in (
                    "flight_details",
                    "roundtrip_price_history",
                    "push_snapshots",
                )
            )
            for table in (
                "flight_details",
                "roundtrip_price_history",
                "push_snapshots",
            ):
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        lineage_ready = collection_lineage_ready and price_lineage_ready
        old_data_readable = True
    except (OSError, ValueError, sqlite3.Error):
        pass
    return {
        "timestamp_ready": timestamp_ready,
        "lineage_ready": lineage_ready,
        "old_data_readable": old_data_readable,
    }
