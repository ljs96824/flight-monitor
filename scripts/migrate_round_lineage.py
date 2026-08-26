"""Audit or apply the two independent round-lineage schema migrations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import storage
from collection_ledger import init_collection_ledger
from observations_store import DEFAULT_DB_PATH as DEFAULT_OBSERVATIONS_DB
from tcurve import readonly_connection


DEFAULT_PRICES_DB = ROOT / "data" / "prices.db"
PRICE_TABLES = ("flight_details", "roundtrip_price_history", "push_snapshots")


def _table_columns(path: Path, table: str) -> set[str]:
    if not path.is_file():
        return set()
    with readonly_connection(path) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            return set()
        return {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }


def audit_migrations(*, observations_db: Path, prices_db: Path) -> dict:
    return {
        "collection_cells": bool(_table_columns(observations_db, "collection_cells")),
        "price_lineage": {
            table: "round_id" in _table_columns(prices_db, table)
            for table in PRICE_TABLES
        },
    }


def migrate_sections(
    *,
    observations_db: Path,
    prices_db: Path,
    section: str = "all",
    write: bool = False,
) -> dict:
    before = audit_migrations(
        observations_db=observations_db,
        prices_db=prices_db,
    )
    if write and section in {"all", "collection_cells"}:
        init_collection_ledger(observations_db)
    if write and section in {"all", "price_lineage"}:
        original_path = storage.DB_PATH
        try:
            storage.DB_PATH = prices_db
            storage.migrate_round_lineage_schema()
        finally:
            storage.DB_PATH = original_path
    after = audit_migrations(
        observations_db=observations_db,
        prices_db=prices_db,
    )
    return {
        "mode": "write" if write else "dry-run",
        "section": section,
        "before": before,
        "after": after,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="审计或执行collection_cells与prices round_id两段独立迁移"
    )
    parser.add_argument(
        "--observations-db", default=str(DEFAULT_OBSERVATIONS_DB), type=Path
    )
    parser.add_argument("--prices-db", default=str(DEFAULT_PRICES_DB), type=Path)
    parser.add_argument(
        "--section",
        choices=("all", "collection_cells", "price_lineage"),
        default="all",
    )
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    result = migrate_sections(
        observations_db=args.observations_db.resolve(),
        prices_db=args.prices_db.resolve(),
        section=args.section,
        write=args.write,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
