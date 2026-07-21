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
                data_source TEXT
            )
            """
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
                roundtrip_lowest REAL
            )
            """
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
                push_type TEXT
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


def save_flight_details(route: str, depart_date: str, flights: list[dict]) -> None:
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


def get_previous_snapshot_prices(route: str, depart_date: str) -> dict:
    """Get flight prices from the previous collection snapshot."""
    init_db()

    with _connect() as connection:
        snapshot_row = connection.execute(
            """
            SELECT snapshot_time
            FROM flight_details
            WHERE route = ?
              AND depart_date = ?
            GROUP BY snapshot_time
            ORDER BY snapshot_time DESC
            LIMIT 1 OFFSET 1
            """,
            (route, depart_date),
        ).fetchone()

        if snapshot_row is None:
            return {}

        rows = connection.execute(
            """
            SELECT flight_combo, price
            FROM flight_details
            WHERE route = ?
              AND depart_date = ?
              AND snapshot_time = ?
              AND flight_combo IS NOT NULL
            """,
            (route, depart_date, snapshot_row["snapshot_time"]),
        ).fetchall()

    return {
        row["flight_combo"]: row["price"]
        for row in rows
        if row["flight_combo"] and row["price"] is not None
    }


def get_lowest_price_history(
    route: str, depart_date: str, limit: int = 14
) -> list[tuple[str, float]]:
    """Get the lowest flight price for each recent collection snapshot."""
    init_db()

    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT snapshot_time, MIN(price) AS min_price
            FROM flight_details
            WHERE route = ?
              AND depart_date = ?
              AND price IS NOT NULL
            GROUP BY snapshot_time
            ORDER BY snapshot_time DESC
            LIMIT ?
            """,
            (route, depart_date, limit),
        ).fetchall()

    history = [
        (row["snapshot_time"], row["min_price"])
        for row in rows
        if row["snapshot_time"] and row["min_price"] is not None
    ]
    return list(reversed(history))


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
    )


def save_roundtrip_snapshot(
    route: str,
    depart_date: str,
    return_date: str,
    outbound_lowest,
    return_lowest,
    roundtrip_total,
    collected_at,
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
                outbound_lowest, return_lowest, roundtrip_lowest
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                route,
                depart_date,
                return_date,
                collected_at or datetime.now().isoformat(),
                outbound_lowest,
                return_lowest,
                roundtrip_lowest,
            ),
        )


def get_roundtrip_price_history(
    route: str, depart_date: str, return_date: str, limit: int = 14
) -> list[dict]:
    """Get recent round-trip lowest-price snapshots."""
    init_db()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM roundtrip_price_history
            WHERE route = ?
              AND depart_date = ?
              AND return_date = ?
            ORDER BY snapshot_time DESC
            LIMIT ?
            """,
            (route, depart_date, return_date, limit),
        ).fetchall()
    normalized = []
    for row in reversed(_rows_to_dicts(rows)):
        snapshot_time = row.get("snapshot_time") or ""
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
            }
        )
    return normalized


def _last_push_key(route: str, depart_date: str, return_date: str | None = None) -> str:
    """Build a stable key for the latest pushed price of one subscription."""
    return "|".join([route or "", depart_date or "", return_date or ""])


def get_last_push_price(
    route: str, depart_date: str, return_date: str | None = None
) -> dict | None:
    """Get the most recent pushed price for one subscription."""
    if not route or not depart_date:
        return None
    init_db()
    key = _last_push_key(route, depart_date, return_date)
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
    key = _last_push_key(route, depart_date, return_date)
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
    route: str, depart_date: str, return_date: str | None = None
) -> dict | None:
    """Get the latest notification snapshot for one subscription."""
    if not route or not depart_date:
        return None
    init_db()
    key = _last_push_key(route, depart_date, return_date)
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
) -> None:
    """Save one notification snapshot for comparing with the next push."""
    if not route or not depart_date or price is None:
        return
    try:
        price_value = float(price)
    except (TypeError, ValueError):
        return
    if price_value <= 0:
        return

    init_db()
    key = _last_push_key(route, depart_date, return_date)
    pushed_at = pushed_at or datetime.now().isoformat(timespec="seconds")
    channels_text = json.dumps(channels or [], ensure_ascii=False)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO push_snapshots (
                subscription_key, route, depart_date, return_date,
                pushed_at, price, confidence, channels, fare_status, push_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
