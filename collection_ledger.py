"""Per-request collection outcome ledger stored beside flight observations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from log_utils import append_round_evidence, safe_log
from method_registry import method_version
from observation_time import canonicalize_observed_at
from observations_store import (
    count_observations_for_request,
    init_observations_db,
    managed_observation_connection,
)
from source_profiles import expected_listing_sources


METHOD_VERSION = method_version("collection_ledger")
EXECUTION_STATUSES = frozenset(
    {"planned", "running", "success", "empty", "failed", "skipped", "reused", "interrupted"}
)
SAMPLE_ROLES = frozenset(
    {"trajectory_anchor", "cross_sectional_probe", "user_monitor", "legacy"}
)
REUSE_KINDS = frozenset({"in_round_cache", "persistent_cache", "panel"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_cells (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  round_id TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  planned_at_utc TEXT NOT NULL,
  started_at_utc TEXT,
  finished_at_utc TEXT,
  observed_day_shanghai TEXT,
  cohort_id TEXT,
  sample_role TEXT NOT NULL DEFAULT 'legacy'
    CHECK(sample_role IN ('trajectory_anchor','cross_sectional_probe','user_monitor','legacy')),
  route_type TEXT NOT NULL,
  origin_airport TEXT NOT NULL,
  dest_airport TEXT NOT NULL,
  depart_date TEXT NOT NULL,
  cabin_class TEXT NOT NULL,
  source TEXT NOT NULL,
  execution_status TEXT NOT NULL
    CHECK(execution_status IN ('planned','running','success','empty','failed','skipped','reused','interrupted')),
  reuse_kind TEXT
    CHECK(reuse_kind IS NULL OR reuse_kind IN ('in_round_cache','persistent_cache','panel')),
  skip_reason_code TEXT,
  error_type TEXT,
  error_code TEXT,
  raw_result_count INTEGER,
  valid_result_count INTEGER,
  written_count INTEGER,
  cache_status TEXT,
  quota_status TEXT,
  method_version TEXT NOT NULL,
  UNIQUE(round_id, request_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_collection_cells_daily
ON collection_cells (
  origin_airport, dest_airport, depart_date,
  observed_day_shanghai, cabin_class, source
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def request_fingerprint(request) -> str:
    payload = json.dumps(
        list(request.key), ensure_ascii=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def init_collection_ledger(db_path: str | Path) -> Path:
    path = init_observations_db(db_path)
    with managed_observation_connection(path) as connection:
        connection.executescript(SCHEMA)
    return path


def _source_name(request) -> str:
    return str(getattr(request.source, "name", type(request.source).__name__)).lower()


def _route_type(request) -> str:
    return str(
        getattr(request, "route_type", None)
        or getattr(request.source, "route_type", None)
        or "unknown"
    )


def _positive_count(result) -> int:
    count = 0
    for flight in (result or {}).get("flights") or []:
        if not isinstance(flight, dict):
            continue
        try:
            if float(flight.get("price") or 0) > 0:
                count += 1
        except (TypeError, ValueError):
            continue
    return count


def _result_counts(result) -> tuple[int, int]:
    flights = list((result or {}).get("flights") or [])
    raw = (result or {}).get("raw_result_count")
    try:
        raw_count = int(raw)
    except (TypeError, ValueError):
        raw_count = len(flights)
    return raw_count, _positive_count(result)


def _skip_reason(source_status: str, cache_status: str, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    if "panel_only" in source_status or source_status == "panel_missing":
        return "panel_only"
    if "source_disabled" in source_status:
        return "source_disabled"
    if cache_status == "skipped" or source_status.startswith("skipped"):
        return "preflight"
    if source_status == "not_configured":
        return "source_disabled"
    return None


def classify_collection_result(
    result,
    *,
    cache_status: str,
    reuse_kind: str | None = None,
    skip_reason_code: str | None = None,
) -> dict:
    result = result if isinstance(result, dict) else {}
    source_status = str(result.get("source_status") or "").strip().lower()
    raw_count, valid_count = _result_counts(result)
    reason_code = _skip_reason(source_status, cache_status, skip_reason_code)
    error_type = result.get("error_type")
    error_code = (
        result.get("error_code")
        or result.get("quota_code")
        or result.get("resultcode")
    )
    quota_status = None
    if reason_code == "quota":
        quota_status = "protected"
    elif "quota" in source_status or error_code in (112, "112", 10012, "10012"):
        quota_status = "exhausted"

    if cache_status == "panel":
        status = "reused"
        reuse_kind = "panel"
    elif cache_status == "cache":
        status = "reused"
        reuse_kind = reuse_kind or "persistent_cache"
    elif cache_status == "round_empty":
        status = "empty"
    elif cache_status == "round_failed":
        status = "failed"
    elif reason_code:
        status = "skipped"
    elif (
        source_status.startswith("failed")
        or source_status.startswith("error")
        or result.get("error")
    ):
        status = "failed"
    elif valid_count:
        status = "success"
    else:
        status = "empty"

    return {
        "execution_status": status,
        "reuse_kind": reuse_kind,
        "skip_reason_code": reason_code,
        "error_type": str(error_type) if error_type else None,
        "error_code": str(error_code) if error_code not in (None, "") else None,
        "raw_result_count": raw_count,
        "valid_result_count": valid_count,
        "cache_status": str(cache_status or ""),
        "quota_status": quota_status,
    }


_terminal_values = classify_collection_result


class CollectionLedgerSession:
    """Best-effort ledger session; failures degrade evidence, never collection."""

    def __init__(self, *, round_id: str, db_path: str | Path):
        self.round_id = str(round_id)
        self.db_path = Path(db_path)
        self.degraded = False
        self._degraded_logged = False

    def _degrade(self, phase: str, exc: BaseException) -> None:
        self.degraded = True
        if self._degraded_logged:
            return
        self._degraded_logged = True
        payload = {
            "round_id": self.round_id,
            "phase": phase,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "ledger_degraded": True,
        }
        safe_log(
            f"[采集台账降级] round={self.round_id} 阶段={phase} "
            f"原因={type(exc).__name__}: {exc}"
        )
        try:
            append_round_evidence("[采集台账降级] ", payload)
        except Exception as evidence_exc:
            safe_log(
                f"[采集台账证据失败] round={self.round_id} "
                f"原因={type(evidence_exc).__name__}: {evidence_exc}"
            )

    def plan(self, requests: Iterable) -> None:
        if self.degraded:
            return
        try:
            init_collection_ledger(self.db_path)
            planned_at = _utc_now()
            observed_day = canonicalize_observed_at(None).observed_day_shanghai
            rows = []
            for request in requests:
                role = str(getattr(request, "sample_role", None) or "legacy")
                if role not in SAMPLE_ROLES:
                    raise ValueError(f"unknown sample_role: {role}")
                rows.append(
                    (
                        self.round_id,
                        request_fingerprint(request),
                        planned_at,
                        observed_day,
                        getattr(request, "cohort_id", None),
                        role,
                        _route_type(request),
                        str(request.origin).upper(),
                        str(request.dest).upper(),
                        str(request.date_str),
                        str(request.cabin_class or "economy"),
                        _source_name(request),
                        "planned",
                        METHOD_VERSION,
                    )
                )
            with managed_observation_connection(self.db_path) as connection:
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO collection_cells (
                        round_id, request_fingerprint, planned_at_utc,
                        observed_day_shanghai, cohort_id, sample_role,
                        route_type, origin_airport, dest_airport, depart_date,
                        cabin_class, source, execution_status, method_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
        except Exception as exc:
            self._degrade("planned", exc)

    def start(self, request) -> None:
        if self.degraded:
            return
        try:
            with managed_observation_connection(self.db_path) as connection:
                connection.execute(
                    """
                    UPDATE collection_cells
                    SET execution_status='running', started_at_utc=?
                    WHERE round_id=? AND request_fingerprint=?
                      AND execution_status='planned'
                    """,
                    (_utc_now(), self.round_id, request_fingerprint(request)),
                )
        except Exception as exc:
            self._degrade("running", exc)

    def finish(
        self,
        request,
        result,
        *,
        cache_status: str,
        reuse_kind: str | None = None,
        skip_reason_code: str | None = None,
    ) -> None:
        if self.degraded:
            return
        try:
            values = classify_collection_result(
                result,
                cache_status=cache_status,
                reuse_kind=reuse_kind,
                skip_reason_code=skip_reason_code,
            )
            written_count = 0
            if values["execution_status"] in {"success", "empty", "failed"}:
                written_count = count_observations_for_request(
                    round_id=self.round_id,
                    source=_source_name(request),
                    origin_airport=request.origin,
                    dest_airport=request.dest,
                    depart_date=request.date_str,
                    cabin_class=request.cabin_class,
                    db_path=self.db_path,
                )
            with managed_observation_connection(self.db_path) as connection:
                connection.execute(
                    """
                    UPDATE collection_cells
                    SET execution_status=?, finished_at_utc=?, reuse_kind=?,
                        skip_reason_code=?, error_type=?, error_code=?,
                        raw_result_count=?, valid_result_count=?, written_count=?,
                        cache_status=?, quota_status=?
                    WHERE round_id=? AND request_fingerprint=?
                    """,
                    (
                        values["execution_status"],
                        _utc_now(),
                        values["reuse_kind"],
                        values["skip_reason_code"],
                        values["error_type"],
                        values["error_code"],
                        values["raw_result_count"],
                        values["valid_result_count"],
                        written_count,
                        values["cache_status"],
                        values["quota_status"],
                        self.round_id,
                        request_fingerprint(request),
                    ),
                )
        except Exception as exc:
            self._degrade("terminal", exc)

    def fail_exception(self, request, exc: BaseException) -> None:
        self.finish(
            request,
            {
                "flights": [],
                "source_status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
            cache_status="round_failed",
        )

    def finalize(self) -> None:
        if self.degraded:
            return
        try:
            with managed_observation_connection(self.db_path) as connection:
                connection.execute(
                    """
                    UPDATE collection_cells
                    SET execution_status='interrupted', finished_at_utc=?,
                        error_type='ProcessInterrupted', cache_status='interrupted'
                    WHERE round_id=? AND execution_status IN ('planned', 'running')
                    """,
                    (_utc_now(), self.round_id),
                )
        except Exception as exc:
            self._degrade("finalize", exc)


def derive_daily_cell_state(rows: list[dict], expected_sources: set[str]) -> str:
    """Collapse request outcomes to missing/failed/empty/degraded/valid."""
    if not rows:
        return "missing"
    expected = {str(source).lower() for source in expected_sources or set()}
    valid_rows = [row for row in rows if int(row.get("valid_result_count") or 0) > 0]
    successful_sources = {
        str(row.get("source") or "").lower()
        for row in rows
        if row.get("execution_status") in {"success", "empty", "reused"}
    }
    if valid_rows:
        return "valid" if expected.issubset(successful_sources) else "degraded"
    if any(
        row.get("execution_status") in {"failed", "interrupted"} for row in rows
    ):
        return "failed"
    if expected and expected.issubset(successful_sources):
        return "empty"
    # A planned but unavailable cell (quota/preflight/conditional skip) is not
    # missing; it is operationally failed with the exact skip reason retained.
    return "failed"


def load_daily_collection_state(
    *,
    round_id: str,
    origin_airport: str,
    dest_airport: str,
    depart_date: str,
    cabin_class: str,
    route_type: str,
    observed_day_shanghai: str | None = None,
    db_path: str | Path,
) -> dict:
    """Read one planned day cell and derive its five-state outcome."""
    path = Path(db_path)
    if not path.is_file():
        return {"state": "missing", "rows": [], "expected_sources": []}
    with managed_observation_connection(path) as connection:
        connection.row_factory = sqlite3.Row
        exists = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='collection_cells'"
        ).fetchone()
        if not exists:
            return {"state": "missing", "rows": [], "expected_sources": []}
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM collection_cells
                WHERE round_id=? AND origin_airport=? AND dest_airport=?
                  AND depart_date=? AND cabin_class=?
                ORDER BY source, request_fingerprint
                """,
                (
                    str(round_id),
                    str(origin_airport).upper(),
                    str(dest_airport).upper(),
                    str(depart_date),
                    str(cabin_class or "economy"),
                ),
            ).fetchall()
        ]
    observed_day = observed_day_shanghai or next(
        (row.get("observed_day_shanghai") for row in rows if row.get("observed_day_shanghai")),
        None,
    )
    expected = expected_listing_sources(
        route_type,
        observed_day=observed_day,
        cabin_class=cabin_class,
    )
    return {
        "state": derive_daily_cell_state(rows, expected),
        "rows": rows,
        "expected_sources": sorted(expected),
    }
