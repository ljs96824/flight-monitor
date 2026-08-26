"""Append-only per-source flight observation storage."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator

from flight_combo_utils import normalize_combo
from log_utils import safe_log
from method_registry import method_version
from observation_time import canonicalize_observed_at, timestamp_kind


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "observations.sqlite3"
METHOD_VERSION = method_version("obs_store")

_current_round_id: ContextVar[str | None] = ContextVar(
    "observations_current_round_id", default=None
)
_current_db_path: ContextVar[Path] = ContextVar(
    "observations_current_db_path", default=DEFAULT_DB_PATH
)
_duration_missing_logged: set[str] = set()


@contextmanager
def _managed_connection(path: str | Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def set_current_round(
    round_id: str,
    db_path: str | Path | None = None,
) -> tuple[Token, Token]:
    round_token = _current_round_id.set(str(round_id) if round_id else None)
    current_db_path = _current_db_path.get()
    db_token = _current_db_path.set(
        Path(db_path) if db_path is not None else current_db_path
    )
    return round_token, db_token


def reset_current_round(tokens: tuple[Token, Token]) -> None:
    round_token, db_token = tokens
    _current_db_path.reset(db_token)
    _current_round_id.reset(round_token)


def clear_current_round() -> None:
    _current_round_id.set(None)
    _current_db_path.set(DEFAULT_DB_PATH)


def get_current_round() -> tuple[str | None, Path]:
    return _current_round_id.get(), _current_db_path.get()


def _parse_observed_at(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _observation_age(latest: datetime, now: datetime | None = None) -> timedelta:
    if latest.tzinfo is not None:
        current = now or datetime.now(latest.tzinfo)
        if current.tzinfo is None:
            current = current.replace(tzinfo=latest.tzinfo)
        else:
            current = current.astimezone(latest.tzinfo)
        return current - latest
    current = now or datetime.now()
    if current.tzinfo is not None:
        current = current.astimezone().replace(tzinfo=None)
    return current - latest


def load_fresh_observation_snapshot(
    *,
    source: str,
    origin_airport: str,
    dest_airport: str,
    depart_date: str,
    cabin_class: str,
    freshness_hours: float = 6,
    now: datetime | None = None,
    db_path: str | Path | None = None,
) -> dict | None:
    """只读返回某请求键最近一批面板观测；不建库、不写库。"""
    path = Path(db_path or _current_db_path.get())
    if not path.exists() or float(freshness_hours or 0) <= 0:
        return None
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
    except sqlite3.Error:
        return None
    try:
        connection.execute("PRAGMA query_only=ON")
        key = (
            str(source or "").lower(),
            str(origin_airport or "").upper(),
            str(dest_airport or "").upper(),
            str(depart_date or ""),
            str(cabin_class or "economy"),
        )
        latest_row = connection.execute(
            """
            SELECT observed_at, round_id
            FROM observations
            WHERE source=? AND origin_airport=? AND dest_airport=?
              AND depart_date=? AND cabin_class=?
            ORDER BY observed_at DESC, id DESC
            LIMIT 1
            """,
            key,
        ).fetchone()
        if not latest_row:
            return None
        latest = _parse_observed_at(latest_row[0])
        if latest is None or _observation_age(latest, now) > timedelta(
            hours=float(freshness_hours)
        ):
            return None
        rows = connection.execute(
            """
            SELECT flight_combo, airline, stops, duration_min, price_cny,
                   method_version
            FROM observations
            WHERE source=? AND origin_airport=? AND dest_airport=?
              AND depart_date=? AND cabin_class=?
              AND observed_at=? AND round_id=?
            ORDER BY price_cny, flight_combo
            """,
            (*key, latest_row[0], latest_row[1]),
        ).fetchall()
        if not rows:
            return None
        return {
            "source": key[0],
            "origin_airport": key[1],
            "dest_airport": key[2],
            "depart_date": key[3],
            "cabin_class": key[4],
            "observed_at": str(latest_row[0]),
            "round_id": str(latest_row[1] or ""),
            "rows": [
                {
                    "flight_combo": str(row[0]),
                    "airline": row[1],
                    "stops": row[2],
                    "duration_min": row[3],
                    "price_cny": float(row[4]),
                    "method_version": row[5],
                }
                for row in rows
            ],
        }
    except sqlite3.Error:
        return None
    finally:
        connection.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  observed_at TEXT NOT NULL,
  observed_at_utc TEXT,
  observed_day_shanghai TEXT,
  legacy_time_ambiguous INTEGER NOT NULL DEFAULT 0,
  round_id TEXT NOT NULL,
  route_type TEXT NOT NULL,
  origin_airport TEXT NOT NULL,
  dest_airport TEXT NOT NULL,
  depart_date TEXT NOT NULL,
  days_to_departure INTEGER NOT NULL,
  cabin_class TEXT NOT NULL,
  source TEXT NOT NULL,
  flight_combo TEXT NOT NULL,
  airline TEXT,
  stops INTEGER,
  duration_min INTEGER,
  price_cny REAL NOT NULL,
  method_version TEXT NOT NULL,
  UNIQUE(round_id, source, origin_airport, dest_airport,
         depart_date, flight_combo, cabin_class)
);
"""



def _ensure_duration_column(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(observations)").fetchall()}
    if "duration_min" not in columns:
        conn.execute("ALTER TABLE observations ADD COLUMN duration_min INTEGER")


def _ensure_observation_time_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(observations)").fetchall()}
    if "observed_at_utc" not in columns:
        conn.execute("ALTER TABLE observations ADD COLUMN observed_at_utc TEXT")
    if "observed_day_shanghai" not in columns:
        conn.execute("ALTER TABLE observations ADD COLUMN observed_day_shanghai TEXT")
    if "legacy_time_ambiguous" not in columns:
        conn.execute(
            "ALTER TABLE observations "
            "ADD COLUMN legacy_time_ambiguous INTEGER NOT NULL DEFAULT 0"
        )


def init_observations_db(db_path: str | Path = DEFAULT_DB_PATH) -> Path:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _managed_connection(path) as conn:
        conn.execute(SCHEMA)
        _ensure_duration_column(conn)
        _ensure_observation_time_columns(conn)
    return path


def _days_to_departure(depart_date: str, observed_day_shanghai: str) -> int:
    return (
        date.fromisoformat(str(depart_date))
        - date.fromisoformat(str(observed_day_shanghai))
    ).days


def audit_observation_timestamps(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    assume_naive_shanghai: bool = False,
) -> dict:
    """Classify legacy timestamps without changing schema, rows, or file metadata."""
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT id, observed_at, depart_date, days_to_departure FROM observations"
        ).fetchall()
    finally:
        connection.close()

    counts = Counter(timestamp_kind(row["observed_at"]) for row in rows)
    changes = []
    for row in rows:
        kind = timestamp_kind(row["observed_at"])
        if kind == "invalid" or (kind == "naive" and not assume_naive_shanghai):
            continue
        canonical = canonicalize_observed_at(
            row["observed_at"],
            assume_naive_shanghai=assume_naive_shanghai,
        )
        computed = _days_to_departure(
            row["depart_date"],
            canonical.observed_day_shanghai,
        )
        stored = int(row["days_to_departure"])
        if computed != stored:
            changes.append({"id": int(row["id"]), "stored_t": stored, "canonical_t": computed})
    ordered_counts = {
        kind: int(counts[kind])
        for kind in ("aware", "naive", "invalid")
        if counts[kind]
    }
    return {
        "classification_counts": ordered_counts,
        "would_be_ambiguous": int(counts["invalid"])
        + (0 if assume_naive_shanghai else int(counts["naive"])),
        "t_assignment_changes": changes,
    }


def migrate_observation_timestamps(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    assume_naive_shanghai: bool = False,
) -> dict:
    """Backfill canonical fields; naive interpretation is always explicit."""
    audit = audit_observation_timestamps(
        db_path,
        assume_naive_shanghai=assume_naive_shanghai,
    )
    if audit["classification_counts"].get("naive") and not assume_naive_shanghai:
        raise ValueError(
            "naive legacy rows require assume_naive_shanghai=True after provenance audit"
        )
    path = init_observations_db(db_path)
    migrated_aware = 0
    migrated_naive = 0
    ambiguous = 0
    with _managed_connection(path) as conn:
        rows = conn.execute("SELECT id, observed_at, depart_date FROM observations").fetchall()
        for row_id, observed_at, depart_date in rows:
            kind = timestamp_kind(observed_at)
            if kind == "invalid":
                conn.execute(
                    "UPDATE observations SET observed_at_utc=NULL, "
                    "observed_day_shanghai=NULL, legacy_time_ambiguous=1 WHERE id=?",
                    (row_id,),
                )
                ambiguous += 1
                continue
            canonical = canonicalize_observed_at(
                observed_at,
                assume_naive_shanghai=assume_naive_shanghai,
            )
            days_to_departure = _days_to_departure(
                depart_date,
                canonical.observed_day_shanghai,
            )
            conn.execute(
                "UPDATE observations SET observed_at_utc=?, "
                "observed_day_shanghai=?, legacy_time_ambiguous=0, "
                "days_to_departure=? WHERE id=?",
                (
                    canonical.observed_at_utc,
                    canonical.observed_day_shanghai,
                    days_to_departure,
                    row_id,
                ),
            )
            if kind == "aware":
                migrated_aware += 1
            else:
                migrated_naive += 1
    return {
        **audit,
        "migrated_aware": migrated_aware,
        "migrated_naive_shanghai": migrated_naive,
        "legacy_time_ambiguous": ambiguous,
    }


def _to_int_or_none(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_price(value) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _parse_duration_minutes(value) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        minutes = int(value)
        return minutes if minutes > 0 else None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        minutes = int(text)
        return minutes if minutes > 0 else None
    colon = re.match(r"^(\d{1,2}):(\d{1,2})$", text)
    if colon:
        return int(colon.group(1)) * 60 + int(colon.group(2))
    hours = 0
    minutes = 0
    hour_match = re.search(r"(\d+)\s*(?:h|hr|hour|hours|\u5c0f\u65f6|\u5c0f\u6642)", text, re.IGNORECASE)
    minute_match = re.search(r"(\d+)\s*(?:m|min|minute|minutes|\u5206\u949f|\u5206\u9418)", text, re.IGNORECASE)
    if hour_match:
        hours = int(hour_match.group(1))
    if minute_match:
        minutes = int(minute_match.group(1))
    total = hours * 60 + minutes
    return total if total > 0 else None


def _flight_duration_min(flight: dict, source: str) -> int | None:
    for key in ("duration_min", "total_duration_min", "duration_minutes", "duration"):
        minutes = _parse_duration_minutes(flight.get(key))
        if minutes is not None:
            return minutes
    minutes = _parse_duration_minutes(flight.get("duration_str") or flight.get("duration_text"))
    if minutes is not None:
        return minutes
    source_name = str(source or "unknown").lower()
    if source_name not in _duration_missing_logged:
        _duration_missing_logged.add(source_name)
        safe_log(f"[\u65f6\u957f\u7f3a\u5931] \u6e90={source_name} \u5b57\u6bb5\u4e0d\u53ef\u5f97")
    return None


def append_observations(
    flights: Iterable[dict],
    *,
    round_id: str,
    route_type: str,
    origin_airport: str,
    dest_airport: str,
    depart_date: str,
    cabin_class: str,
    source: str,
    observed_at: str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, int]:
    """Append valid per-source flight prices and return write/skip counts."""
    canonical_time = canonicalize_observed_at(observed_at)
    observed_at = canonical_time.observed_at_shanghai
    db_path = init_observations_db(db_path)
    days_to_departure = _days_to_departure(
        depart_date,
        canonical_time.observed_day_shanghai,
    )
    rows = []
    for flight in flights:
        price = _to_price(flight.get("price"))
        combo = normalize_combo(flight.get("flight_combo") or flight.get("flight_no"))
        if price is None or not combo:
            continue
        rows.append(
            (
                observed_at,
                canonical_time.observed_at_utc,
                canonical_time.observed_day_shanghai,
                0,
                str(round_id),
                str(route_type or "unknown"),
                str(origin_airport),
                str(dest_airport),
                str(depart_date),
                days_to_departure,
                str(flight.get("cabin_class") or cabin_class or "economy"),
                str(source).lower(),
                combo,
                flight.get("airline"),
                _to_int_or_none(flight.get("stops")),
                _flight_duration_min(flight, str(source).lower()),
                price,
                METHOD_VERSION,
            )
        )

    written = 0
    with _managed_connection(db_path) as conn:
        for row in rows:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO observations (
                    observed_at, observed_at_utc, observed_day_shanghai,
                    legacy_time_ambiguous, round_id, route_type, origin_airport, dest_airport,
                    depart_date, days_to_departure, cabin_class, source, flight_combo,
                    airline, stops, duration_min, price_cny, method_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            written += cursor.rowcount
    return {"written": written, "skipped": len(rows) - written}


def migrate_normalized_combos(db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, int]:
    """Normalize historical observation combo keys and merge format twins.

    If multiple rows collapse onto the same UNIQUE key, keep the lower
    price_cny row and delete the others.
    """
    path = init_observations_db(db_path)
    with _managed_connection(path) as conn:
        rows = conn.execute(
            """
            SELECT id, round_id, source, origin_airport, dest_airport, depart_date,
                   cabin_class, flight_combo, price_cny
            FROM observations
            """
        ).fetchall()
        groups: dict[tuple, list[dict]] = {}
        for row in rows:
            row_id, round_id, source, origin, dest, depart_date, cabin_class, combo, price = row
            normalized = normalize_combo(combo)
            if not normalized:
                continue
            key = (round_id, source, origin, dest, depart_date, cabin_class, normalized)
            groups.setdefault(key, []).append(
                {"id": row_id, "combo": combo, "price": float(price or 0), "normalized": normalized}
            )

        merged = 0
        updated = 0
        for key, items in groups.items():
            normalized = key[-1]
            keep = min(items, key=lambda item: (item["price"] if item["price"] > 0 else float("inf"), item["id"]))
            delete_ids = [item["id"] for item in items if item["id"] != keep["id"]]
            for row_id in delete_ids:
                conn.execute("DELETE FROM observations WHERE id=?", (row_id,))
            if delete_ids:
                merged += len(delete_ids)
            if keep["combo"] != normalized:
                conn.execute("UPDATE observations SET flight_combo=? WHERE id=?", (normalized, keep["id"]))
                updated += 1

        remaining = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE instr(flight_combo, char(124)) > 0"
        ).fetchone()[0]
        conn.commit()
    safe_log(f"[\u5f52\u4e00\u8fc1\u79fb] \u5408\u5e76{merged}\u884c \u66f4\u65b0{updated}\u884c \u5269\u4f59\u542b\u7ad6\u7ebf={remaining}")
    return {"merged": merged, "updated": updated, "remaining_pipe": int(remaining)}


def count_observations(db_path: str | Path = DEFAULT_DB_PATH) -> int:
    path = Path(db_path)
    if not path.exists():
        return 0
    with _managed_connection(path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0])


def count_observations_for_round(
    round_id: str,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> int:
    path = Path(db_path)
    if not path.exists():
        return 0
    with _managed_connection(path) as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM observations WHERE round_id = ?",
                (str(round_id),),
            ).fetchone()[0]
        )
