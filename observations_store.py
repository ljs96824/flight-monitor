"""Append-only per-source flight observation storage."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Iterable


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "observations.sqlite3"
METHOD_VERSION = "v1"


SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  observed_at TEXT NOT NULL,
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
  price_cny REAL NOT NULL,
  method_version TEXT NOT NULL,
  UNIQUE(round_id, source, origin_airport, dest_airport,
         depart_date, flight_combo, cabin_class)
);
"""


def normalize_combo(combo: str | None) -> str:
    return str(combo or "").replace(" ", "").upper()


def init_observations_db(db_path: str | Path = DEFAULT_DB_PATH) -> Path:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(SCHEMA)
    return path


def _observed_date(observed_at: str) -> date:
    value = str(observed_at or "").strip()
    if not value:
        return date.today()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return date.fromisoformat(value[:10])


def _days_to_departure(depart_date: str, observed_at: str) -> int:
    return (date.fromisoformat(str(depart_date)) - _observed_date(observed_at)).days


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
    observed_at = observed_at or datetime.now().isoformat(timespec="seconds")
    db_path = init_observations_db(db_path)
    days_to_departure = _days_to_departure(depart_date, observed_at)
    rows = []
    for flight in flights:
        price = _to_price(flight.get("price"))
        combo = normalize_combo(flight.get("flight_combo") or flight.get("flight_no"))
        if price is None or not combo:
            continue
        rows.append(
            (
                observed_at,
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
                price,
                METHOD_VERSION,
            )
        )

    written = 0
    with sqlite3.connect(db_path) as conn:
        for row in rows:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO observations (
                    observed_at, round_id, route_type, origin_airport, dest_airport,
                    depart_date, days_to_departure, cabin_class, source, flight_combo,
                    airline, stops, price_cny, method_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            written += cursor.rowcount
    return {"written": written, "skipped": len(rows) - written}


def count_observations(db_path: str | Path = DEFAULT_DB_PATH) -> int:
    path = Path(db_path)
    if not path.exists():
        return 0
    with sqlite3.connect(path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0])
