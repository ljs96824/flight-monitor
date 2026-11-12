"""SQLite storage for flight price snapshots."""

from __future__ import annotations

import json
import math
import re
from decimal import Decimal
from datetime import timezone
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from log_utils import safe_log


BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "prices.db"
_lineage_degraded_logged = False


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
    "round_id",
]


@contextmanager
def _connect(path: str | Path | None = None, *, uri: bool = False) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path if path is not None else DB_PATH, uri=uri)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]


def _current_round_id() -> str | None:
    """Read active collection lineage without inferring it from timestamps."""
    global _lineage_degraded_logged
    try:
        from observations_store import get_current_round

        round_id, _ = get_current_round()
    except Exception:
        round_id = None
    if round_id:
        return str(round_id)
    if not _lineage_degraded_logged:
        _lineage_degraded_logged = True
        safe_log("[价格lineage降级] round_id不可用,本记录保持NULL")
    return None


LOW_PRICE_ALERT_METHOD_VERSION = "low_price_alert_v1"
_LP_OBS = "low_price_alert_observations_v1"
_LP_CLAIMS = "low_price_alert_component_claims_v1"
_LP_EVAL = "low_price_alert_evaluations_v1"
_LP_DDL = """
CREATE TABLE low_price_alert_observations_v1 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version TEXT NOT NULL CHECK (schema_version = 'v1'),
    method_version TEXT NOT NULL,
    bucket_key TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    trip_type TEXT NOT NULL CHECK (trip_type IN ('one_way', 'round_trip')),
    route TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT NOT NULL DEFAULT '',
    cabin_scope TEXT NOT NULL,
    price_scope TEXT NOT NULL,
    passenger_signature TEXT NOT NULL,
    route_rate_key TEXT NOT NULL,
    constraint_fingerprint TEXT NOT NULL,
    coverage_policy_json TEXT NOT NULL,
    amount_cny REAL NOT NULL CHECK (amount_cny > 0),
    amount_raw_text TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    observed_at_shanghai TEXT NOT NULL,
    component_slots_json TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    UNIQUE(bucket_key, evidence_id)
);

CREATE INDEX idx_lpa_obs_v1_bucket_obs_rec
    ON low_price_alert_observations_v1(bucket_key, observed_at_utc, recorded_at_utc, id);

CREATE TABLE low_price_alert_component_claims_v1 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version TEXT NOT NULL CHECK (schema_version = 'v1'),
    bucket_key TEXT NOT NULL,
    slot_key TEXT NOT NULL,
    fetch_unit_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    unit_observed_at_utc TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    UNIQUE(bucket_key, slot_key, fetch_unit_id)
);

CREATE INDEX idx_lpa_claims_v1_bucket_slot
    ON low_price_alert_component_claims_v1(bucket_key, slot_key);

CREATE TABLE low_price_alert_evaluations_v1 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version TEXT NOT NULL CHECK (schema_version = 'v1'),
    method_version TEXT NOT NULL,
    bucket_key TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    since_utc TEXT NOT NULL DEFAULT '',
    evaluated_amount_cny REAL NOT NULL CHECK (evaluated_amount_cny > 0),
    evaluated_amount_raw_text TEXT NOT NULL,
    cutoff_observed_at_utc TEXT NOT NULL,
    first_evaluated_at_utc TEXT NOT NULL,
    first_max_observation_id INTEGER NOT NULL CHECK (first_max_observation_id >= 0),
    prior_n INTEGER NOT NULL CHECK (prior_n >= 0),
    prior_evidence_ids_json TEXT NOT NULL,
    prior_amounts_raw_json TEXT NOT NULL,
    threshold_cny REAL,
    threshold_raw_text TEXT,
    state TEXT NOT NULL CHECK (state IN ('triggered', 'not_triggered', 'unknown')),
    reason TEXT NOT NULL,
    can_append_sample INTEGER NOT NULL CHECK (can_append_sample IN (0, 1)),
    sample_write_status TEXT NOT NULL CHECK (
        sample_write_status IN ('inserted', 'skipped_partial_reuse', 'existing_evidence_noop')
    ),
    UNIQUE(bucket_key, evidence_id, since_utc)
);

CREATE INDEX idx_lpa_eval_v1_bucket_evidence
    ON low_price_alert_evaluations_v1(bucket_key, evidence_id, since_utc);
"""


class LowPriceEvidenceConflictError(RuntimeError):
    pass


class LowPriceInvalidAmountError(ValueError):
    pass


class LowPriceStorageSchemaError(RuntimeError):
    pass


class LowPriceStorageReadError(RuntimeError):
    pass


def canonical_decimal_text_from_plain(raw_text: str) -> str:
    if not isinstance(raw_text, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw_text):
        raise LowPriceInvalidAmountError("expected plain ASCII decimal")
    integer, _, fraction = raw_text.partition(".")
    integer = integer.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    return integer + "." + fraction if fraction else integer


def validate_low_price_amount_and_scope(amount_cny, amount_raw_text, *, trip_type, cabin_scope, price_scope):
    raw = canonical_decimal_text_from_plain(amount_raw_text)
    exact = Decimal(raw)
    if isinstance(amount_cny, bool) or not isinstance(amount_cny, (float, int, Decimal)):
        raise LowPriceInvalidAmountError("non-numeric amount")
    numeric = Decimal(str(amount_cny)) if isinstance(amount_cny, float) else Decimal(amount_cny)
    projection = float(exact)
    if not numeric.is_finite() or numeric != exact or not math.isfinite(projection) or projection <= 0:
        raise LowPriceInvalidAmountError("non-positive, non-finite or inconsistent amount")
    suffix = {"one_way": "oneway", "round_trip": "roundtrip"}.get(trip_type)
    mixed = cabin_scope.startswith("mixed:")
    if (not suffix or not (mixed or cabin_scope in {"economy", "business"})
            or price_scope not in {"all_passengers_" + suffix, "per_person_" + suffix}
            or (mixed and not price_scope.startswith("all_passengers_"))):
        raise LowPriceInvalidAmountError("incompatible price scope")
    return projection, raw, exact


def _claim_pairs(slots, trip, cabin):
    cabins = {cabin}
    if cabin.startswith("mixed:"):
        allocation = json.loads(cabin.partition(":")[2])
        cabins = {c for c, counts in allocation.items() if any(counts.values())}
    legs = ["outbound", "return"] if trip == "round_trip" else ["outbound"]
    if not cabins or set(slots) != {f"{leg}:{c}" for leg in legs for c in cabins}:
        raise LowPriceStorageSchemaError("incomplete component slots")
    pairs = set()
    for key, slot in slots.items():
        ids = slot["fetch_unit_ids"]
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
            raise LowPriceStorageSchemaError("invalid component identity")
        pairs.update((key, i) for i in ids)
    return pairs


def _low_price_schema(conn, create):
    tables = (_LP_OBS, _LP_CLAIMS, _LP_EVAL)
    objects = dict(conn.execute("SELECT name,type FROM sqlite_master WHERE name IN (?,?,?)", tables))
    if not objects:
        if not create:
            return False
        for sql in _LP_DDL.split(";"):
            if sql.strip():
                conn.execute(sql)
    elif objects != dict.fromkeys(tables, "table"):
        raise LowPriceStorageSchemaError("partial or incompatible low-price schema")
    # Compare table definitions, not index names alone. Never repair a partial ledger.
    for table in tables:
        actual = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
        expected = _LP_DDL.split("CREATE TABLE " + table, 1)[1].split(";", 1)[0]
        if " ".join(actual.split()) != " ".join(("CREATE TABLE " + table + expected).split()):
            raise LowPriceStorageSchemaError("incompatible schema: " + table)
    expected, inserted, obs_keys = set(), set(), set()
    try:
        for row in conn.execute(f"SELECT * FROM {_LP_OBS}"):
            _verify_projection(row)
            json.loads(row["coverage_policy_json"])
            if row["schema_version"] != "v1":
                raise LowPriceStorageSchemaError("unexpected observation version")
            key = row["bucket_key"], row["evidence_id"]
            obs_keys.add(key)
            pairs = _claim_pairs(json.loads(row["component_slots_json"]), row["trip_type"], row["cabin_scope"])
            expected.update((*key, slot, unit) for slot, unit in pairs)
        for row in conn.execute(f"SELECT * FROM {_LP_EVAL}"):
            if row["schema_version"] != "v1":
                raise LowPriceStorageSchemaError("unexpected evaluation version")
            ids, amounts = json.loads(row["prior_evidence_ids_json"]), json.loads(row["prior_amounts_raw_json"])
            if not isinstance(ids, list) or not isinstance(amounts, list) or len(ids) != len(amounts) or len(ids) != row["prior_n"]:
                raise LowPriceStorageSchemaError("invalid frozen observations")
            if row["sample_write_status"] == "inserted" and row["can_append_sample"] == 1:
                inserted.add((row["bucket_key"], row["evidence_id"]))
        actual = list(conn.execute(f"SELECT bucket_key,evidence_id,slot_key,fetch_unit_id,schema_version FROM {_LP_CLAIMS}"))
        if (len(actual) != len(expected) or {tuple(r)[:4] for r in actual} != expected
                or any(r[4] != "v1" for r in actual) or inserted != obs_keys):
            raise LowPriceStorageSchemaError("inconsistent observation/claim/evaluation ledger")
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError) as exc:
        raise LowPriceStorageSchemaError("invalid low-price ledger payload") from exc
    return True


def _verify_projection(row):
    exact = Decimal(row["amount_raw_text"])
    value, _, _ = validate_low_price_amount_and_scope(exact, row["amount_raw_text"],
        trip_type=row["trip_type"], cabin_scope=row["cabin_scope"], price_scope=row["price_scope"])
    if value != row["amount_cny"]:
        raise LowPriceStorageSchemaError("invalid stored REAL projection")


@contextmanager
def _low_price_connection(db_path, write):
    path = Path(db_path or DB_PATH).resolve()
    try:
        try:
            with path.open("rb") as header:
                if header.read(16) != b"SQLite format 3\x00":
                    raise LowPriceStorageReadError("invalid SQLite header")
        except FileNotFoundError:
            if not write:
                yield None
                return
            path.parent.mkdir(parents=True, exist_ok=True)
        uri = path.as_uri() + ("?mode=rwc" if write else "?mode=ro")
        with _connect(uri, uri=True) as conn:
            with conn:
                if not write:
                    conn.execute("PRAGMA query_only=ON")
                conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield conn if _low_price_schema(conn, write) else None
    except (sqlite3.DatabaseError, OSError) as exc:
        raise LowPriceStorageReadError("low-price history database unavailable") from exc


def init_low_price_alert_schema(db_path=None):
    with _low_price_connection(db_path, True):
        return Path(db_path or DB_PATH)


def _low_price_input(conn, bucket, method, eid, cutoff, slots, since, limit, as_of, max_id):
    frozen = conn.execute(f"SELECT * FROM {_LP_EVAL} WHERE bucket_key=? AND evidence_id=? AND since_utc=?",
                          (bucket, eid, since)).fetchone()
    if frozen:
        return {"snapshot_hit": True, "snapshot": dict(frozen), "prior_rows": []}
    prior = []
    for row in conn.execute(f"""SELECT * FROM {_LP_OBS}
            WHERE bucket_key=? AND method_version=? AND evidence_id!=? AND observed_at_utc<=?
            AND observed_at_utc>=? AND recorded_at_utc<=? AND id<=?
            ORDER BY observed_at_utc DESC,id DESC""", (bucket, method, eid, cutoff, since, as_of, max_id)):
        old = json.loads(row["component_slots_json"])
        if any(set(units).intersection(old.get(k, {}).get("fetch_unit_ids", ())) for k, units in slots.items()):
            continue
        prior.append(dict(row))
        if len(prior) >= limit:
            break
    return {"snapshot_hit": False, "prior_rows": prior}


def read_low_price_alert_evaluation_input(bucket_key, *, method_version, evidence_id, cutoff_observed_at_utc,
                                         slot_fetch_unit_map, since_utc="", limit=14,
                                         as_of_recorded_at_utc=None, db_path=None):
    with _low_price_connection(db_path, False) as conn:
        if conn is None:
            return {"snapshot_hit": False, "prior_rows": []}
        return _low_price_input(conn, bucket_key, method_version, evidence_id, cutoff_observed_at_utc,
                                slot_fetch_unit_map, since_utc, limit, as_of_recorded_at_utc or "9999", 2**63-1)


def _lp_insert(conn, table, data):
    conn.execute(f"INSERT INTO {table} ({','.join(data)}) VALUES ({','.join('?' for _ in data)})", tuple(data.values()))


def _frozen_result(snapshot, replay):
    result = dict(snapshot)
    first = result["sample_write_status"]
    result.update(evaluated_evidence_id=result["evidence_id"], snapshot_reused=replay,
                  first_sample_write_status=first, first_can_append_sample=bool(result["can_append_sample"]),
                  can_append_sample=not replay and first == "inserted",
                  sample_write_status="replayed_noop" if replay else first,
                  sample_delta=int(not replay and first == "inserted"),
                  prior_amounts_raw=json.loads(result["prior_amounts_raw_json"]),
                  prior_evidence_ids=json.loads(result["prior_evidence_ids_json"]))
    return result


def evaluate_and_persist_low_price_alert(offer, *, since_utc="", limit=14, db_path=None, recorded_at_utc=None):
    if not offer["can_evaluate_current"]:
        raise ValueError("unqualified low-price evidence")
    price, raw, exact = validate_low_price_amount_and_scope(offer["amount_cny"], offer["amount_raw_text"],
        trip_type=offer["trip_type"], cabin_scope=offer["cabin_scope"], price_scope=offer["price_scope"])
    slots = offer["component_slots"]
    pairs = sorted(_claim_pairs(slots, offer["trip_type"], offer["cabin_scope"]))
    bucket, eid = offer["bucket_key"], offer["evidence_id"]
    components = json.dumps(slots, sort_keys=True, ensure_ascii=False)
    method, cutoff = offer["method_version"], offer["observed_at_utc"]
    with _low_price_connection(db_path, True) as conn:
        now = recorded_at_utc or datetime.now(timezone.utc).isoformat(timespec="seconds")
        maximum = conn.execute(f"SELECT COALESCE(MAX(id),0) FROM {_LP_OBS} WHERE bucket_key=?", (bucket,)).fetchone()[0]
        old = conn.execute(f"SELECT * FROM {_LP_OBS} WHERE bucket_key=? AND evidence_id=?", (bucket, eid)).fetchone()
        if old and (old["amount_raw_text"] != raw or old["component_slots_json"] != components
                    or any(old[k] != offer[k] for k in ("method_version","trip_type","cabin_scope","price_scope"))):
            raise LowPriceEvidenceConflictError("same evidence has different offer")
        data = _low_price_input(conn, bucket, method, eid, cutoff,
                                {k: v["fetch_unit_ids"] for k,v in slots.items()}, since_utc, limit, now, maximum)
        if data["snapshot_hit"]:
            if data["snapshot"]["evaluated_amount_raw_text"] != raw:
                raise LowPriceEvidenceConflictError("same evaluation has different amount")
            return _frozen_result(data["snapshot"], True)
        prior = data["prior_rows"]
        amounts = [r["amount_raw_text"] for r in prior]
        n = len(prior)
        threshold = sorted(amounts, key=Decimal)[round((n-1)*0.30)] if n >= 5 else None
        if threshold is None:
            state, reason = "unknown", "history_insufficient" if n else "history_unavailable"
        elif exact <= Decimal(threshold):
            state, reason = "triggered", "entered_low_zone"
        else:
            state, reason = "not_triggered", "above_threshold"
        common = dict(schema_version="v1", bucket_key=bucket, evidence_id=eid)
        if old:
            status = "existing_evidence_noop"
        elif not offer.get("can_append_sample", True) or any(conn.execute(
                f"SELECT 1 FROM {_LP_CLAIMS} WHERE bucket_key=? AND slot_key=? AND fetch_unit_id=?",
                (bucket,k,u)).fetchone() for k,u in pairs):
            status = "skipped_partial_reuse"
        else:
            status = "inserted"
            obs = {k: offer[k] for k in ("trip_type","route","depart_date","return_date","cabin_scope",
                                         "price_scope","passenger_signature","route_rate_key","constraint_fingerprint",
                                         "observed_at_utc","observed_at_shanghai")}
            _lp_insert(conn, _LP_OBS, dict(**common, **obs, method_version=method, amount_cny=price, amount_raw_text=raw,
                       recorded_at_utc=now, coverage_policy_json=json.dumps(offer["coverage_policy"], sort_keys=True),
                       component_slots_json=components))
            for k,u in pairs:
                _lp_insert(conn, _LP_CLAIMS, dict(**common, slot_key=k, fetch_unit_id=u,
                           unit_observed_at_utc=slots[k]["unit_times"][u], recorded_at_utc=now))
        snapshot = dict(**common, method_version=method,since_utc=since_utc,evaluated_amount_cny=price,
                        evaluated_amount_raw_text=raw,cutoff_observed_at_utc=cutoff,first_evaluated_at_utc=now,
                        first_max_observation_id=maximum,prior_n=n,prior_evidence_ids_json=json.dumps([r["evidence_id"] for r in prior]),
                        prior_amounts_raw_json=json.dumps(amounts),threshold_cny=float(threshold) if threshold else None,
                        threshold_raw_text=threshold,state=state,reason=reason,can_append_sample=int(status=="inserted"),
                        sample_write_status=status)
        _lp_insert(conn, _LP_EVAL, snapshot)
        return _frozen_result(snapshot, False)


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
                constraint_fingerprint TEXT,
                round_id TEXT
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
                sources_json TEXT,
                round_id TEXT
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
                constraint_sample_n INTEGER,
                round_id TEXT
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


def migrate_round_lineage_schema() -> None:
    """Apply the explicit nullable round_id migration to existing price tables."""
    init_db()
    with _connect() as connection:
        for table in (
            "flight_details",
            "roundtrip_price_history",
            "push_snapshots",
        ):
            columns = {
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if "round_id" not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN round_id TEXT")


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
    round_id = _current_round_id()
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
                "round_id": round_id,
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
                constraint_fingerprint, sources_json, round_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                _current_round_id(),
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
                constraint_fingerprint, constraint_sample_n, round_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                _current_round_id(),
            ),
        )
