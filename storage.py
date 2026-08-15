"""SQLite storage for flight price snapshots."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator


BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "prices.db"


SNAPSHOT_COLUMNS = [
    "route",
    "flight_combo",
    "airline",
    "price",
    "stopover_city",
    "duration_hours",
    "depart_date",
    "snapshot_time",
    "days_before_dept",
    "is_target",
    "data_source",
]

FLIGHT_DETAIL_COLUMNS = [
    "route",
    "depart_date",
    "snapshot_time",
    "flight_combo",
    "airline_summary",
    "price",
    "total_duration_min",
    "stops",
    "route_summary",
    "layover_summary",
    "segments_json",
    "data_source",
    "price_source",
    "constraint_fingerprint",
]


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]


def init_db() -> None:
    """Create the data directory and price snapshots table."""
    DB_PATH.parent.mkdir(exist_ok=True)

    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS price_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route TEXT,
                flight_combo TEXT,
                airline TEXT,
                price REAL,
                stopover_city TEXT,
                duration_hours REAL,
                depart_date TEXT,
                snapshot_time TEXT,
                days_before_dept INTEGER,
                is_target INTEGER,
                data_source TEXT
            )
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(price_snapshots)")
        }
        if "data_source" not in columns:
            connection.execute(
                "ALTER TABLE price_snapshots ADD COLUMN data_source TEXT"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS flight_details (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route TEXT,
                depart_date TEXT,
                snapshot_time TEXT,
                flight_combo TEXT,
                airline_summary TEXT,
                price REAL,
                total_duration_min INTEGER,
                stops INTEGER,
                route_summary TEXT,
                layover_summary TEXT,
                segments_json TEXT,
                data_source TEXT,
                price_source TEXT,
                constraint_fingerprint TEXT
            )
            """
        )
        flight_detail_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(flight_details)")
        }
        if "constraint_fingerprint" not in flight_detail_columns:
            connection.execute(
                "ALTER TABLE flight_details ADD COLUMN constraint_fingerprint TEXT"
            )
        if "price_source" not in flight_detail_columns:
            connection.execute(
                "ALTER TABLE flight_details ADD COLUMN price_source TEXT"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS roundtrip_price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route TEXT,
                depart_date TEXT,
                return_date TEXT,
                snapshot_time TEXT,
                outbound_lowest REAL,
                return_lowest REAL,
                roundtrip_lowest REAL,
                constraint_fingerprint TEXT,
                sources_json TEXT
            )
            """
        )
        roundtrip_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(roundtrip_price_history)")
        }
        if "constraint_fingerprint" not in roundtrip_columns:
            connection.execute(
                "ALTER TABLE roundtrip_price_history ADD COLUMN constraint_fingerprint TEXT"
            )
        if "sources_json" not in roundtrip_columns:
            connection.execute(
                "ALTER TABLE roundtrip_price_history ADD COLUMN sources_json TEXT"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS last_push_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_key TEXT UNIQUE,
                route TEXT,
                depart_date TEXT,
                return_date TEXT,
                price REAL,
                push_type TEXT,
                pushed_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS push_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_key TEXT,
                route TEXT,
                depart_date TEXT,
                return_date TEXT,
                pushed_at TEXT,
                price REAL,
                confidence TEXT,
                channels TEXT,
                fare_status TEXT,
                push_type TEXT,
                constraint_fingerprint TEXT,
                constraint_sample_n INTEGER
            )
            """
        )
        push_snapshot_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(push_snapshots)")
        }
        if "constraint_fingerprint" not in push_snapshot_columns:
            connection.execute(
                "ALTER TABLE push_snapshots ADD COLUMN constraint_fingerprint TEXT"
            )
        if "constraint_sample_n" not in push_snapshot_columns:
            connection.execute(
                "ALTER TABLE push_snapshots ADD COLUMN constraint_sample_n INTEGER"
            )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_flight_details_constraint_history
            ON flight_details (
                route, depart_date, constraint_fingerprint, snapshot_time
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_roundtrip_constraint_history
            ON roundtrip_price_history (
                route, depart_date, return_date,
                constraint_fingerprint, snapshot_time
            )
            """
        )


def save_snapshots(records: list[dict]) -> None:
    """Batch insert price snapshot records."""
    if not records:
        return

    init_db()
    placeholders = ", ".join(["?"] * len(SNAPSHOT_COLUMNS))
    columns = ", ".join(SNAPSHOT_COLUMNS)
    values = [
        tuple(record.get(column) for column in SNAPSHOT_COLUMNS)
        for record in records
    ]

    with _connect() as connection:
        connection.executemany(
            f"INSERT INTO price_snapshots ({columns}) VALUES ({placeholders})",
            values,
        )


def get_target_history(route: str, depart_date: str, target_combo: str) -> list[dict]:
    """Get historical records for the configured target flight."""
    init_db()

    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM price_snapshots
            WHERE route = ?
              AND depart_date = ?
              AND flight_combo = ?
              AND is_target = 1
            ORDER BY snapshot_time ASC, id ASC
            """,
            (route, depart_date, target_combo),
        ).fetchall()

    return _rows_to_dicts(rows)


def get_latest_alternatives(
    route: str, depart_date: str, target_combo: str
) -> list[dict]:
    """Get the latest non-target alternatives for a route and departure date."""
    init_db()

    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM price_snapshots
            WHERE route = ?
              AND depart_date = ?
              AND flight_combo != ?
              AND snapshot_time = (
                  SELECT MAX(snapshot_time)
                  FROM price_snapshots
                  WHERE route = ?
                    AND depart_date = ?
              )
            ORDER BY price ASC, id ASC
            """,
            (route, depart_date, target_combo, route, depart_date),
        ).fetchall()

    return _rows_to_dicts(rows)


def get_all_history(route: str, depart_date: str) -> list[dict]:
    """Get all historical records for a route and departure date."""
    init_db()

    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM price_snapshots
            WHERE route = ?
              AND depart_date = ?
            ORDER BY snapshot_time ASC, price ASC, id ASC
            """,
            (route, depart_date),
        ).fetchall()

    return _rows_to_dicts(rows)


def save_flight_details(
    route: str,
    depart_date: str,
    flights: list[dict],
    *,
    constraint_fingerprint: str | None = None,
) -> None:
    """Save detailed flight options for one route/date snapshot."""
    if not flights:
        return

    init_db()
    snapshot_time = datetime.now().isoformat(timespec="seconds")
    records = []
    for flight in flights:
        records.append(
            {
                "route": route,
                "depart_date": depart_date,
                "snapshot_time": snapshot_time,
                "flight_combo": flight.get("flight_combo"),
                "airline_summary": flight.get("airline_summary"),
                "price": flight.get("price"),
                "total_duration_min": flight.get("total_duration_min"),
                "stops": flight.get("stops"),
                "route_summary": flight.get("route_summary"),
                "layover_summary": flight.get("layover_summary"),
                "segments_json": json.dumps(
                    flight.get("segments", []), ensure_ascii=False
                ),
                "data_source": flight.get("data_source"),
                "price_source": flight.get("price_source"),
                "constraint_fingerprint": constraint_fingerprint,
            }
        )

    placeholders = ", ".join(["?"] * len(FLIGHT_DETAIL_COLUMNS))
    columns = ", ".join(FLIGHT_DETAIL_COLUMNS)
    values = [
        tuple(record.get(column) for column in FLIGHT_DETAIL_COLUMNS)
        for record in records
    ]

    with _connect() as connection:
        connection.executemany(
            f"INSERT INTO flight_details ({columns}) VALUES ({placeholders})",
            values,
        )


def _decode_segments(row: dict) -> dict:
    segments_json = row.get("segments_json")
    if segments_json:
        try:
            row["segments"] = json.loads(segments_json)
        except json.JSONDecodeError:
            row["segments"] = []
    else:
        row["segments"] = []
    return row


def get_latest_flights(route: str, depart_date: str) -> list[dict]:
    """Get latest detailed flight options for one route/date."""
    init_db()

    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM flight_details
            WHERE route = ?
              AND depart_date = ?
              AND snapshot_time = (
                  SELECT MAX(snapshot_time)
                  FROM flight_details
                  WHERE route = ?
                    AND depart_date = ?
              )
            ORDER BY price ASC, total_duration_min ASC, id ASC
            """,
            (route, depart_date, route, depart_date),
        ).fetchall()

    return [_decode_segments(row) for row in _rows_to_dicts(rows)]


def get_previous_snapshot_prices(
    route: str,
    depart_date: str,
    constraint_fingerprint: str | None = None,
) -> dict:
    """Get flight prices from the latest completed collection snapshot."""
    init_db()

    with _connect() as connection:
        fingerprint_clause = ""
        parameters: list = [route, depart_date]
        if constraint_fingerprint is not None:
            fingerprint_clause = " AND constraint_fingerprint = ?"
            parameters.append(constraint_fingerprint)
        snapshot_row = connection.execute(
            f"""
            SELECT snapshot_time
            FROM flight_details
            WHERE route = ?
              AND depart_date = ?
              {fingerprint_clause}
            GROUP BY snapshot_time
            ORDER BY snapshot_time DESC
            LIMIT 1
            """,
            parameters,
        ).fetchone()

        if snapshot_row is None:
            return {}

        row_parameters: list = [route, depart_date, snapshot_row["snapshot_time"]]
        if constraint_fingerprint is not None:
            row_parameters.append(constraint_fingerprint)
        rows = connection.execute(
            f"""
            SELECT flight_combo, price
            FROM flight_details
            WHERE route = ?
              AND depart_date = ?
              AND snapshot_time = ?
              {fingerprint_clause}
              AND flight_combo IS NOT NULL
            """,
            row_parameters,
        ).fetchall()

    return {
        row["flight_combo"]: row["price"]
        for row in rows
        if row["flight_combo"] and row["price"] is not None
    }


def _source_names(value) -> set[str]:
    return {
        item.strip().lower()
        for item in str(value or "").replace("|", "+").split("+")
        if item.strip()
    }


def get_lowest_price_history(
    route: str,
    depart_date: str,
    limit: int = 14,
    constraint_fingerprint: str | None = None,
    *,
    include_metadata: bool = False,
    since: str | None = None,
) -> list:
    """Get the lowest flight price for each recent collection snapshot."""
    init_db()

    with _connect() as connection:
        fingerprint_clause = ""
        since_clause = ""
        parameters: list = [route, depart_date]
        if constraint_fingerprint is not None:
            fingerprint_clause = " AND constraint_fingerprint = ?"
            parameters.append(constraint_fingerprint)
        if since:
            since_clause = " AND snapshot_time > ?"
            parameters.append(str(since))
        rows = connection.execute(
            f"""
            SELECT
                snapshot_time, price, data_source, price_source,
                constraint_fingerprint, id
            FROM flight_details
            WHERE route = ?
              AND depart_date = ?
              {fingerprint_clause}
              {since_clause}
              AND price IS NOT NULL
            ORDER BY snapshot_time DESC, id ASC
            """,
            parameters,
        ).fetchall()

    snapshots: dict[str, dict] = {}
    for row in rows:
        snapshot_time = row["snapshot_time"]
        price = row["price"]
        if not snapshot_time or price is None:
            continue
        if snapshot_time not in snapshots:
            if len(snapshots) >= limit:
                continue
            snapshots[snapshot_time] = {
                "price": float(price),
                "sources": set(
                    _source_names(row["price_source"] or row["data_source"])
                ),
                "constraint_fingerprint": row["constraint_fingerprint"],
            }
            continue
        current = snapshots[snapshot_time]
        numeric_price = float(price)
        if numeric_price < current["price"]:
            current["price"] = numeric_price
            current["sources"] = set(
                _source_names(row["price_source"] or row["data_source"])
            )
        elif numeric_price == current["price"]:
            current["sources"].update(
                _source_names(row["price_source"] or row["data_source"])
            )

    history = []
    for snapshot_time, item in reversed(list(snapshots.items())):
        if include_metadata:
            history.append(
                {
                    "date": snapshot_time[:10],
                    "timestamp": snapshot_time,
                    "price": item["price"],
                    "min_price": item["price"],
                    "sources": sorted(item["sources"]),
                    "constraint_fingerprint": item["constraint_fingerprint"],
                }
            )
        else:
            history.append((snapshot_time, item["price"]))
    return history


def get_flight_price_history(
    route: str, depart_date: str, flight_combo: str
) -> list[dict]:
    """Get historical prices for one flight option."""
    init_db()

    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM flight_details
            WHERE route = ?
              AND depart_date = ?
              AND flight_combo = ?
            ORDER BY snapshot_time ASC, id ASC
            """,
            (route, depart_date, flight_combo),
        ).fetchall()

    return [_decode_segments(row) for row in _rows_to_dicts(rows)]


def save_roundtrip_price_history(
    route: str,
    depart_date: str,
    return_date: str,
    outbound_lowest,
    return_lowest,
    roundtrip_lowest,
    constraint_fingerprint: str | None = None,
    sources: list[str] | None = None,
) -> None:
    """Save one round-trip lowest-price snapshot."""
    save_roundtrip_snapshot(
        route,
        depart_date,
        return_date,
        outbound_lowest,
        return_lowest,
        roundtrip_lowest,
        datetime.now().isoformat(),
        constraint_fingerprint=constraint_fingerprint,
        sources=sources,
    )


def save_roundtrip_snapshot(
    route: str,
    depart_date: str,
    return_date: str,
    outbound_lowest,
    return_lowest,
    roundtrip_total,
    collected_at,
    *,
    constraint_fingerprint: str | None = None,
    sources: list[str] | None = None,
) -> None:
    """Save one normalized round-trip lowest-price snapshot."""
    roundtrip_lowest = roundtrip_total
    if not route or not depart_date or not return_date or roundtrip_lowest is None:
        return
    init_db()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO roundtrip_price_history (
                route, depart_date, return_date, snapshot_time,
                outbound_lowest, return_lowest, roundtrip_lowest,
                constraint_fingerprint, sources_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                route,
                depart_date,
                return_date,
                collected_at or datetime.now().isoformat(),
                outbound_lowest,
                return_lowest,
                roundtrip_lowest,
                constraint_fingerprint,
                json.dumps(sorted(set(sources or [])), ensure_ascii=False),
            ),
        )


def get_roundtrip_price_history(
    route: str,
    depart_date: str,
    return_date: str,
    limit: int = 14,
    constraint_fingerprint: str | None = None,
    *,
    since: str | None = None,
) -> list[dict]:
    """Get recent round-trip lowest-price snapshots."""
    init_db()
    with _connect() as connection:
        fingerprint_clause = ""
        since_clause = ""
        parameters: list = [route, depart_date, return_date]
        if constraint_fingerprint is not None:
            fingerprint_clause = " AND constraint_fingerprint = ?"
            parameters.append(constraint_fingerprint)
        if since:
            since_clause = " AND snapshot_time > ?"
            parameters.append(str(since))
        parameters.append(limit)
        rows = connection.execute(
            f"""
            SELECT *
            FROM roundtrip_price_history
            WHERE route = ?
              AND depart_date = ?
              AND return_date = ?
              {fingerprint_clause}
              {since_clause}
            ORDER BY snapshot_time DESC
            LIMIT ?
            """,
            parameters,
        ).fetchall()
    normalized = []
    for row in reversed(_rows_to_dicts(rows)):
        snapshot_time = row.get("snapshot_time") or ""
        try:
            sources = json.loads(row.get("sources_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            sources = []
        normalized.append(
            {
                "date": snapshot_time[:10],
                "timestamp": snapshot_time,
                "outbound": row.get("outbound_lowest"),
                "return": row.get("return_lowest"),
                "total": row.get("roundtrip_lowest"),
                "outbound_lowest": row.get("outbound_lowest"),
                "return_lowest": row.get("return_lowest"),
                "roundtrip_lowest": row.get("roundtrip_lowest"),
                "sources": sorted(
                    {
                        source
                        for value in (sources if isinstance(sources, list) else [sources])
                        for source in _source_names(value)
                    }
                ),
                "constraint_fingerprint": row.get("constraint_fingerprint"),
            }
        )
    return normalized


def _last_push_key(
    route: str,
    depart_date: str,
    return_date: str | None = None,
    subscription_id: str | None = None,
) -> str:
    """Build a stable key for the latest pushed price of one subscription."""
    parts = [route or "", depart_date or "", return_date or ""]
    if subscription_id is not None and str(subscription_id).strip():
        parts.insert(0, f"subscription:{subscription_id}")
    return "|".join(parts)


def get_last_push_price(
    route: str,
    depart_date: str,
    return_date: str | None = None,
    *,
    subscription_id: str | None = None,
) -> dict | None:
    """Get the most recent pushed price for one subscription."""
    if not route or not depart_date:
        return None
    init_db()
    key = _last_push_key(route, depart_date, return_date, subscription_id)
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM last_push_prices
            WHERE subscription_key = ?
            LIMIT 1
            """,
            (key,),
        ).fetchone()
    return dict(row) if row else None


def save_last_push_price(
    route: str,
    depart_date: str,
    return_date: str | None,
    price,
    push_type: str | None = None,
    pushed_at: str | None = None,
    *,
    subscription_id: str | None = None,
) -> None:
    """Persist the price used in the latest notification."""
    if not route or not depart_date or price is None:
        return
    try:
        price_value = float(price)
    except (TypeError, ValueError):
        return
    if price_value <= 0:
        return

    init_db()
    key = _last_push_key(route, depart_date, return_date, subscription_id)
    pushed_at = pushed_at or datetime.now().isoformat(timespec="seconds")
    with _connect() as connection:
        existing = connection.execute(
            "SELECT id FROM last_push_prices WHERE subscription_key = ?",
            (key,),
        ).fetchone()
        if existing:
            connection.execute(
                """
                UPDATE last_push_prices
                SET route = ?,
                    depart_date = ?,
                    return_date = ?,
                    price = ?,
                    push_type = ?,
                    pushed_at = ?
                WHERE subscription_key = ?
                """,
                (route, depart_date, return_date, price_value, push_type, pushed_at, key),
            )
        else:
            connection.execute(
                """
                INSERT INTO last_push_prices (
                    subscription_key, route, depart_date, return_date,
                    price, push_type, pushed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (key, route, depart_date, return_date, price_value, push_type, pushed_at),
            )


def get_last_push_snapshot(
    route: str,
    depart_date: str,
    return_date: str | None = None,
    *,
    subscription_id: str | None = None,
) -> dict | None:
    """Get the latest notification snapshot for one subscription."""
    if not route or not depart_date:
        return None
    init_db()
    key = _last_push_key(route, depart_date, return_date, subscription_id)
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM push_snapshots
            WHERE subscription_key = ?
            ORDER BY pushed_at DESC, id DESC
            LIMIT 1
            """,
            (key,),
        ).fetchone()
    return dict(row) if row else None


def get_constraint_epoch_boundary(
    route: str,
    depart_date: str,
    return_date: str | None,
    current_fingerprint: str | None,
    *,
    subscription_id: str | None = None,
) -> str | None:
    """返回本订阅最近一次不同约束指纹的已推送时间边界。"""
    current = str(current_fingerprint or "").strip()
    if not route or not depart_date or not current:
        return None
    init_db()
    key = _last_push_key(route, depart_date, return_date, subscription_id)
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT pushed_at, constraint_fingerprint
            FROM push_snapshots
            WHERE subscription_key = ?
            ORDER BY pushed_at DESC, id DESC
            """,
            (key,),
        ).fetchall()
    for row in rows:
        previous = str(row["constraint_fingerprint"] or "").strip()
        pushed_at = str(row["pushed_at"] or "").strip()
        if previous and pushed_at and previous != current:
            return pushed_at
    return None


def get_constraint_history_limit(
    route: str,
    depart_date: str,
    return_date: str | None,
    current_fingerprint: str | None,
    *,
    subscription_id: str | None = None,
    default_limit: int = 14,
) -> int:
    """按本订阅连续约束口径限制历史样本数。"""
    try:
        limit = max(1, int(default_limit))
    except (TypeError, ValueError):
        limit = 14
    current = str(current_fingerprint or "").strip()
    if not current:
        return limit
    last_snapshot = get_last_push_snapshot(
        route,
        depart_date,
        return_date,
        subscription_id=subscription_id,
    )
    previous = str((last_snapshot or {}).get("constraint_fingerprint") or "").strip()
    if not previous:
        return limit
    if previous != current:
        return 1
    try:
        previous_n = int((last_snapshot or {}).get("constraint_sample_n") or 0)
    except (TypeError, ValueError):
        previous_n = 0
    return min(limit, previous_n + 1) if previous_n > 0 else limit


def save_push_snapshot(
    route: str,
    depart_date: str,
    return_date: str | None,
    price,
    confidence: str | None = None,
    channels: list[str] | None = None,
    fare_status: str | None = None,
    push_type: str | None = None,
    pushed_at: str | None = None,
    constraint_fingerprint: str | None = None,
    constraint_sample_n: int | None = None,
    subscription_id: str | None = None,
) -> None:
    """Save one notification snapshot for comparing with the next push."""
    if not route or not depart_date:
        return
    price_value = None
    if price is not None:
        try:
            price_value = float(price)
        except (TypeError, ValueError):
            return
        if price_value <= 0:
            return

    init_db()
    key = _last_push_key(route, depart_date, return_date, subscription_id)
    pushed_at = pushed_at or datetime.now().isoformat(timespec="seconds")
    channels_text = json.dumps(channels or [], ensure_ascii=False)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO push_snapshots (
                subscription_key, route, depart_date, return_date,
                pushed_at, price, confidence, channels, fare_status, push_type,
                constraint_fingerprint, constraint_sample_n
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                route,
                depart_date,
                return_date,
                pushed_at,
                price_value,
                confidence,
                channels_text,
                fare_status,
                push_type,
                constraint_fingerprint,
                constraint_sample_n,
            ),
        )
