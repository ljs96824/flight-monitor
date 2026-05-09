"""SQLite storage for flight price snapshots."""

from __future__ import annotations

import sqlite3
from pathlib import Path


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
]


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


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
                is_target INTEGER
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
