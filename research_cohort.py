"""State machine for the quota-bounded PVG-KIX research cohort.

This module only plans and accounts for research cells. It never calls a
flight source directly; execution remains owned by ``CollectionPlan``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
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
    other_scheduled_calls: int,
    quota_remaining: int,
    retries_per_request: int = 1,
) -> dict:
    """Model whole-system daily Juhe use and the research-basket retry ceiling."""
    basket = set(basket_keys)
    subscriptions = set(subscription_keys)
    other = max(0, int(other_scheduled_calls or 0))
    retries = max(0, int(retries_per_request or 0))
    # The basket is a separate force-fresh process, so an equal subscription
    # key is still another real request. Worst case adds the one permitted
    # OSError retry for each research-basket key to the whole-system baseline.
    expected = len(basket) + len(subscriptions) + other
    worst = expected + len(basket) * retries
    remaining = max(0, int(quota_remaining or 0))
    return {
        "basket_planned_unique": len(basket),
        "basket_normal_actual": len(basket),
        "basket_retry_ceiling": len(basket) * (1 + retries),
        "subscription_planned_unique": len(subscriptions),
        "other_scheduled_calls": other,
        "combined_daily_expected": expected,
        "combined_daily_worst_case": worst,
        "estimated_days_remaining": remaining // expected if expected else None,
        "complete": True,
    }


def evaluate_research_hard_gates(
    *,
    off_disk_copy: bool,
    quota_simulation: dict,
    migration_status: dict,
) -> dict:
    checks = {
        "off_disk_copy": bool(off_disk_copy),
        "quota_simulation": bool((quota_simulation or {}).get("complete")),
        "timestamp_migration": bool((migration_status or {}).get("timestamp_ready")),
        "lineage_migration": bool((migration_status or {}).get("lineage_ready")),
        "old_data_readable": bool((migration_status or {}).get("old_data_readable")),
    }
    missing = [name for name, ready in checks.items() if not ready]
    return {"ready": not missing, "missing": missing, "checks": checks}


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
