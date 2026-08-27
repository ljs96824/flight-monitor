"""统计依据信封与只读双源历史一致度。"""

from __future__ import annotations

import os
import re
import sqlite3
import statistics
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator

from airports import get_airport_city
from flight_combo_utils import normalize_combo
from method_registry import METHOD_VERSIONS, method_version, method_version_for_stat
from project_time import SHANGHAI_TZ as PROJECT_TIMEZONE
from source_profiles import expected_listing_sources, normalize_route_type
from subscription_preflight import shanghai_today


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "observations.sqlite3"


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


AGREEMENT_WINDOW_DAYS = _positive_int_env("AGREEMENT_WINDOW_DAYS", 30)
MIN_PAIRS_FOR_AGREEMENT = _positive_int_env("MIN_PAIRS_FOR_AGREEMENT", 10)


@contextmanager
def readonly_connection(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    timeout: float = 3.0,
) -> Iterator[sqlite3.Connection]:
    """以只读 URI 打开观测库，绝不创建文件或改 schema。"""
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"观测库不存在: {path}")
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
        timeout=timeout,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def split_route(route: str) -> tuple[str, str]:
    text = str(route or "").strip()
    for separator in ("->", "→", "—", "-"):
        if separator not in text:
            continue
        origin, dest = (part.strip() for part in text.split(separator, 1))
        if origin and dest:
            return origin, dest
    raise ValueError(f"航线格式无效，应为'出发地-目的地': {route!r}")


def _normalize_pair(airport_pair) -> tuple[str, str] | None:
    if airport_pair in (None, ""):
        return None
    if isinstance(airport_pair, str):
        origin, dest = split_route(airport_pair)
    else:
        try:
            origin, dest = airport_pair
        except (TypeError, ValueError) as exc:
            raise ValueError("机场对必须为'PVG-KIX'或两个IATA值") from exc
    origin = str(origin or "").strip().upper()
    dest = str(dest or "").strip().upper()
    if not origin or not dest:
        raise ValueError("机场对不能为空")
    return origin, dest


def expected_search_sources(
    route_type: str | None,
    observed_day: str | date | None = None,
) -> set[str]:
    return expected_listing_sources(route_type, observed_day=observed_day)


def load_route_observations(
    db_path: str | Path,
    *,
    route: str,
    airport_pair=None,
    timeout: float = 3.0,
) -> list[dict]:
    """读取同一有向城市航线的分源观测行。"""
    origin_city, dest_city = split_route(route)
    pair = _normalize_pair(airport_pair)
    columns = (
        "observed_at, round_id, route_type, origin_airport, dest_airport, "
        "depart_date, days_to_departure, cabin_class, source, flight_combo, "
        "airline, stops, duration_min, price_cny, method_version"
    )
    with readonly_connection(db_path, timeout=timeout) as connection:
        available = {
            row[1]
            for row in connection.execute("PRAGMA table_info(observations)").fetchall()
        }
        required = {
            "observed_at",
            "round_id",
            "route_type",
            "origin_airport",
            "dest_airport",
            "depart_date",
            "cabin_class",
            "source",
            "flight_combo",
            "price_cny",
        }
        missing = sorted(required - available)
        if missing:
            raise RuntimeError(f"observations缺少字段: {', '.join(missing)}")
        if pair:
            raw_rows = connection.execute(
                f"SELECT {columns} FROM observations "
                "WHERE price_cny > 0 AND LOWER(cabin_class)='economy' "
                "AND UPPER(origin_airport)=? AND UPPER(dest_airport)=?",
                pair,
            ).fetchall()
        else:
            raw_rows = connection.execute(
                f"SELECT {columns} FROM observations "
                "WHERE price_cny > 0 AND LOWER(cabin_class)='economy'"
            ).fetchall()

    rows = []
    for row in raw_rows:
        item = dict(row)
        if get_airport_city(item.get("origin_airport")) != origin_city:
            continue
        if get_airport_city(item.get("dest_airport")) != dest_city:
            continue
        item["flight_combo"] = normalize_combo(item.get("flight_combo"))
        rows.append(item)
    return rows


def _window_bounds(as_of_date: date, window_days: int) -> tuple[date, date]:
    if window_days < 1:
        raise ValueError("window_days必须大于0")
    return as_of_date - timedelta(days=window_days - 1), as_of_date


def _rows_in_window(rows: list[dict], start: date, end: date) -> list[dict]:
    selected = []
    for row in rows:
        try:
            observed_day = date.fromisoformat(str(row.get("observed_at") or "")[:10])
        except ValueError:
            continue
        if start <= observed_day <= end:
            selected.append(row)
    return selected


def _daily_min_cells(rows: list[dict], start: date, end: date) -> list[dict]:
    """按城市航线、出发日、观测日折叠，并保留最低价的真实来源。"""
    grouped: dict[tuple[str, str], list[tuple[float, str]]] = {}
    for row in _rows_in_window(rows, start, end):
        observed_day = str(row.get("observed_at") or "")[:10]
        depart_date = str(row.get("depart_date") or "")[:10]
        source = str(row.get("source") or "").strip().lower()
        try:
            price = float(row.get("price_cny"))
        except (TypeError, ValueError):
            continue
        if len(observed_day) != 10 or len(depart_date) != 10 or price <= 0:
            continue
        grouped.setdefault((depart_date, observed_day), []).append((price, source))

    cells = []
    for (depart_date, observed_day), values in sorted(grouped.items()):
        minimum = min(price for price, _source in values)
        cells.append(
            {
                "depart_date": depart_date,
                "observed_day": observed_day,
                "min_price": minimum,
                "sources": sorted(
                    {
                        source
                        for price, source in values
                        if price == minimum and source
                    }
                ),
            }
        )
    return cells


def _clean_number(value: float, digits: int = 2):
    number = round(float(value), digits)
    return int(number) if number.is_integer() else number


def _agreement_from_rows(
    rows: list[dict],
    *,
    start: date,
    end: date,
    min_pairs: int,
    computed_at: str,
) -> dict:
    grouped: dict[tuple[str, str, str, str, str, str], dict[str, float]] = {}
    actual_sources = set()
    for row in _rows_in_window(rows, start, end):
        source = str(row.get("source") or "").strip().lower()
        combo = normalize_combo(row.get("flight_combo"))
        cabin = str(row.get("cabin_class") or "economy").strip().lower()
        observed_day = str(row.get("observed_at") or "")[:10]
        depart_date = str(row.get("depart_date") or "")[:10]
        try:
            price = float(row.get("price_cny"))
        except (TypeError, ValueError):
            continue
        if source not in {"hasdata", "juhe"} or not combo or price <= 0:
            continue
        actual_sources.add(source)
        key = (
            get_airport_city(row.get("origin_airport")),
            get_airport_city(row.get("dest_airport")),
            depart_date,
            observed_day,
            combo,
            cabin,
        )
        source_prices = grouped.setdefault(key, {})
        current = source_prices.get(source)
        if current is None or price < current:
            source_prices[source] = price

    gaps = []
    for source_prices in grouped.values():
        hasdata_price = source_prices.get("hasdata")
        juhe_price = source_prices.get("juhe")
        if not hasdata_price or not juhe_price:
            continue
        denominator = min(hasdata_price, juhe_price)
        if denominator <= 0:
            continue
        gaps.append(abs(hasdata_price - juhe_price) / denominator * 100)

    sample_n = len(gaps)
    result = {
        "method_version": method_version("dual_source_agreement"),
        "status": "ok" if sample_n >= min_pairs else "insufficient",
        "sample_n": sample_n,
        "median_abs_diff_pct": None,
        "within_5pct_pct": None,
        "window": [start.isoformat(), end.isoformat()],
        "sources": sorted(actual_sources),
        "computed_at": computed_at,
        "relative_difference_basis": "abs(hasdata-juhe)/min(hasdata,juhe)",
    }
    if sample_n < min_pairs:
        result["summary"] = f"样本不足(n={sample_n})"
        return result

    median_gap = round(statistics.median(gaps), 2)
    within_5 = round(sum(1 for gap in gaps if gap <= 5) / sample_n * 100, 2)
    result.update(
        {
            "median_abs_diff_pct": median_gap,
            "within_5pct_pct": within_5,
            "summary": (
                f"n={sample_n},中位相对差{median_gap:.2f}%,"
                f"差≤5%占比{within_5:.2f}%"
            ),
        }
    )
    return result


def compute_dual_source_agreement(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    route: str,
    airport_pair=None,
    window_days: int = AGREEMENT_WINDOW_DAYS,
    min_pairs: int = MIN_PAIRS_FOR_AGREEMENT,
    as_of_date: date | None = None,
    timeout: float = 3.0,
) -> dict:
    """从面板分源行计算近窗口双源历史一致度。"""
    end = as_of_date or shanghai_today()
    start, end = _window_bounds(end, window_days)
    rows = load_route_observations(
        db_path,
        route=route,
        airport_pair=airport_pair,
        timeout=timeout,
    )
    return _agreement_from_rows(
        rows,
        start=start,
        end=end,
        min_pairs=min_pairs,
        computed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )


def build_route_provenance_context(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    route: str,
    airport_pair=None,
    window_days: int = AGREEMENT_WINDOW_DAYS,
    min_pairs: int = MIN_PAIRS_FOR_AGREEMENT,
    as_of_date: date | None = None,
    timeout: float = 3.0,
) -> dict:
    """一次只读查询生成通知所需的观测窗口、源覆盖和一致度。"""
    end = as_of_date or shanghai_today()
    start, end = _window_bounds(end, window_days)
    computed_at = datetime.now().astimezone().isoformat(timespec="seconds")
    rows = load_route_observations(
        db_path,
        route=route,
        airport_pair=airport_pair,
        timeout=timeout,
    )
    window_rows = _rows_in_window(rows, start, end)
    sources = sorted(
        {
            str(row.get("source") or "").strip().lower()
            for row in window_rows
            if str(row.get("source") or "").strip()
        }
    )

    daily_sources: dict[tuple[str, str], dict[str, set[str]]] = {}
    for row in window_rows:
        observed_day = str(row.get("observed_at") or "")[:10]
        depart_date = str(row.get("depart_date") or "")[:10]
        cell = daily_sources.setdefault(
            (depart_date, observed_day),
            {"sources": set(), "route_types": set()},
        )
        source = str(row.get("source") or "").strip().lower()
        if source:
            cell["sources"].add(source)
        route_type = normalize_route_type(row.get("route_type")) or str(
            row.get("route_type") or "international"
        ).strip().lower()
        cell["route_types"].add(route_type)

    degraded = 0
    for (_depart_date, observed_day), cell in daily_sources.items():
        expected = set()
        for route_type in cell["route_types"]:
            expected.update(expected_search_sources(route_type, observed_day))
        if expected and not expected.issubset(cell["sources"]):
            degraded += 1

    agreement = _agreement_from_rows(
        rows,
        start=start,
        end=end,
        min_pairs=min_pairs,
        computed_at=computed_at,
    )
    return {
        "route": route,
        "window": [start.isoformat(), end.isoformat()],
        "sources": sources,
        "degraded_excluded": degraded,
        "dual_source_agreement": agreement,
        # 参考价可能引用30天窗口外的历史最低，来源匹配覆盖完整面板历史。
        "price_cells": _daily_min_cells(rows, date.min, date.max),
        "computed_at": computed_at,
    }


def route_cities_from_info(route_info: dict) -> tuple[str, str]:
    route_info = route_info or {}

    def city_for(value, active_values):
        candidates = active_values if isinstance(active_values, list) else []
        candidate = next((item for item in candidates if item), None) or value
        return get_airport_city(candidate)

    origin = city_for(
        route_info.get("origin_city") or route_info.get("origin"),
        route_info.get("origin_airports_active") or route_info.get("origin_airports"),
    )
    dest = city_for(
        route_info.get("destination_city") or route_info.get("destination"),
        route_info.get("destination_airports_active")
        or route_info.get("destination_airports"),
    )
    if not origin or not dest:
        raise ValueError("通知路由缺少可识别的出发地或目的地")
    return origin, dest


def build_route_provenance_context_from_info(
    route_info: dict,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    window_days: int = AGREEMENT_WINDOW_DAYS,
    min_pairs: int = MIN_PAIRS_FOR_AGREEMENT,
    as_of_date: date | None = None,
) -> dict:
    origin_city, dest_city = route_cities_from_info(route_info)
    outbound = build_route_provenance_context(
        db_path,
        route=f"{origin_city}-{dest_city}",
        window_days=window_days,
        min_pairs=min_pairs,
        as_of_date=as_of_date,
    )
    is_roundtrip = bool(
        route_info.get("round_trip")
        or route_info.get("is_roundtrip")
        or route_info.get("return_date")
    )
    if not is_roundtrip:
        outbound["agreements"] = {"outbound": outbound["dual_source_agreement"]}
        return outbound

    return_context = build_route_provenance_context(
        db_path,
        route=f"{dest_city}-{origin_city}",
        window_days=window_days,
        min_pairs=min_pairs,
        as_of_date=as_of_date,
    )
    roundtrip_agreement = _combine_directional_agreements(
        outbound["dual_source_agreement"],
        return_context["dual_source_agreement"],
    )
    roundtrip_cells = _combine_roundtrip_cells(
        outbound.get("price_cells") or [],
        return_context.get("price_cells") or [],
        str(route_info.get("depart_date") or "")[:10],
        str(route_info.get("return_date") or "")[:10],
    )
    return {
        **outbound,
        "sources": sorted(set(outbound.get("sources") or []) | set(return_context.get("sources") or [])),
        "degraded_excluded": int(outbound.get("degraded_excluded") or 0)
        + int(return_context.get("degraded_excluded") or 0),
        "dual_source_agreement": roundtrip_agreement,
        "agreements": {
            "outbound": outbound["dual_source_agreement"],
            "return": return_context["dual_source_agreement"],
            "roundtrip": roundtrip_agreement,
        },
        "return_price_cells": return_context.get("price_cells") or [],
        "roundtrip_price_cells": roundtrip_cells,
    }


def _combine_directional_agreements(outbound: dict, return_: dict) -> dict:
    """往返统计不伪装成单一方向指标，明确保留两方向事实。"""
    outbound = dict(outbound or {})
    return_ = dict(return_ or {})
    sample_n = int(outbound.get("sample_n") or 0) + int(return_.get("sample_n") or 0)
    status = "ok" if outbound.get("status") == return_.get("status") == "ok" else "insufficient"
    windows = [
        value
        for agreement in (outbound, return_)
        for value in (agreement.get("window") or [])
        if value
    ]
    summary = (
        f"去程:{format_dual_source_agreement(outbound)};"
        f"返程:{format_dual_source_agreement(return_)}"
    )
    return {
        "method_version": method_version("dual_source_agreement"),
        "scope": "roundtrip",
        "status": status,
        "sample_n": sample_n,
        "median_abs_diff_pct": None,
        "within_5pct_pct": None,
        "window": [min(windows), max(windows)] if windows else [None, None],
        "sources": sorted(set(outbound.get("sources") or []) | set(return_.get("sources") or [])),
        "summary": summary,
        "outbound": outbound,
        "return": return_,
        "computed_at": outbound.get("computed_at") or return_.get("computed_at"),
    }


def _combine_roundtrip_cells(
    outbound_cells: list[dict],
    return_cells: list[dict],
    depart_date: str,
    return_date: str,
) -> list[dict]:
    if len(depart_date) != 10 or len(return_date) != 10:
        return []
    outbound_by_day = {
        cell["observed_day"]: cell
        for cell in outbound_cells
        if cell.get("depart_date") == depart_date
    }
    return_by_day = {
        cell["observed_day"]: cell
        for cell in return_cells
        if cell.get("depart_date") == return_date
    }
    cells = []
    for observed_day in sorted(set(outbound_by_day) & set(return_by_day)):
        outbound = outbound_by_day[observed_day]
        return_ = return_by_day[observed_day]
        cells.append(
            {
                "depart_date": depart_date,
                "return_date": return_date,
                "observed_day": observed_day,
                "min_price": float(outbound["min_price"]) + float(return_["min_price"]),
                "sources": sorted(set(outbound.get("sources") or []) | set(return_.get("sources") or [])),
            }
        )
    return cells


def build_envelope(
    stat_key: str,
    *,
    sample_n,
    window,
    sources,
    degraded_excluded,
    bucket,
    dual_source_agreement=None,
    computed_at: str | None = None,
) -> dict:
    """构建标准依据信封，未知统计键直接失败。"""
    version = method_version_for_stat(stat_key)
    normalized_window = list(window or [None, None])[:2]
    while len(normalized_window) < 2:
        normalized_window.append(None)
    normalized_sources = sorted(
        {
            str(source).strip()
            for source in (sources or [])
            if str(source).strip()
        }
    )
    try:
        normalized_n = int(sample_n) if sample_n is not None else 0
    except (TypeError, ValueError):
        normalized_n = 0
    try:
        excluded = int(degraded_excluded or 0)
    except (TypeError, ValueError):
        excluded = 0
    return {
        "stat_key": str(stat_key),
        "method_version": version,
        "sample_n": normalized_n,
        "window": normalized_window,
        "sources": normalized_sources,
        "degraded_excluded": excluded,
        "bucket": str(bucket or ""),
        "dual_source_agreement": dict(dual_source_agreement or {}),
        "computed_at": computed_at
        or datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def format_micro_provenance(envelope: dict | None) -> str:
    envelope = envelope or {}
    sample_n = int(envelope.get("sample_n") or 0)
    window = envelope.get("window") or [None, None]
    start = window[0] if len(window) > 0 else None
    end = window[1] if len(window) > 1 else None
    if start and end:
        window_text = str(start) if start == end else f"{start}~{end}"
    else:
        window_text = "未标明"
    return f"（n={sample_n}·窗口={window_text}）"


def format_dual_source_agreement(agreement: dict | None) -> str:
    agreement = agreement or {}
    if agreement.get("scope") == "roundtrip":
        return str(
            agreement.get("summary")
            or (
                f"去程:{format_dual_source_agreement(agreement.get('outbound'))};"
                f"返程:{format_dual_source_agreement(agreement.get('return'))}"
            )
        )
    if agreement.get("status") != "ok":
        return str(agreement.get("summary") or f"样本不足(n={int(agreement.get('sample_n') or 0)})")
    return str(
        agreement.get("summary")
        or (
            f"n={int(agreement.get('sample_n') or 0)},"
            f"中位相对差{float(agreement.get('median_abs_diff_pct') or 0):.2f}%,"
            f"差≤5%占比{float(agreement.get('within_5pct_pct') or 0):.2f}%"
        )
    )


def _valid_price(value) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _item_date(item) -> str | None:
    if isinstance(item, (list, tuple)) and item:
        value = item[0]
    elif isinstance(item, dict):
        value = (
            item.get("date")
            or item.get("observed_day")
            or item.get("timestamp")
            or item.get("snapshot_time")
            or item.get("label")
        )
    else:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=PROJECT_TIMEZONE)
        return value.astimezone(PROJECT_TIMEZONE).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=PROJECT_TIMEZONE).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value or "").strip()
    if len(text) >= 10:
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(float(text), tz=PROJECT_TIMEZONE).date().isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _items_window(items, fallback) -> list:
    dates = sorted({item_date for item in (items or []) if (item_date := _item_date(item))})
    if dates:
        return [dates[0], dates[-1]]
    return list(fallback or [None, None])


def history_observation_window(items) -> list:
    """公开的历史窗口规范化入口；未知日期保持未标明。"""
    return _items_window(items, [None, None])


def _observation_window(items, fallback) -> list:
    dates = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        for value in item.get("observation_window") or []:
            text = str(value or "").strip()
            if len(text) < 10:
                continue
            try:
                dates.add(date.fromisoformat(text[:10]).isoformat())
            except ValueError:
                continue
        for key in ("observed_at", "observed_day", "updated_at", "collected_at"):
            text = str(item.get(key) or "").strip()
            if len(text) < 10:
                continue
            try:
                dates.add(date.fromisoformat(text[:10]).isoformat())
            except ValueError:
                continue
            break
    if dates:
        ordered = sorted(dates)
        return [ordered[0], ordered[-1]]
    return list(fallback or [None, None])


def _source_tokens(value) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = str(value or "").replace("|", "+").split("+")
    return {
        str(source).strip().lower()
        for source in values
        if str(source).strip()
    }


def _sources_from_rows(rows) -> list[str]:
    sources = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        sources.update(_source_tokens(row.get("sources")))
        sources.update(_source_tokens(row.get("source")))
        sources.update(_source_tokens(row.get("price_source")))
        sources.update(_source_tokens(row.get("data_source")))
    return sorted(sources)


def _source_names_from_payload(payload: dict) -> list[str]:
    names = set()
    for name, stat in (payload.get("source_stats") or {}).items():
        source = str(name or "").strip().lower()
        if source not in {"hasdata", "juhe", "serpapi", "searchapi"}:
            continue
        if not isinstance(stat, dict):
            continue
        try:
            count = int(stat.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            names.add(source)
    return sorted(names)


def _base_bucket(payload: dict) -> str:
    parts = [f"航线={str(payload.get('route') or '未标明').replace(' → ', '-')}" ]
    if payload.get("depart_date"):
        parts.append(f"出发日={payload.get('depart_date')}")
    if payload.get("return_date"):
        parts.append(f"返程日={payload.get('return_date')}")
    constraint_short = str(
        payload.get("constraint_fingerprint_short")
        or payload.get("constraint_fingerprint")
        or ""
    ).strip()[:8]
    if constraint_short:
        parts.append(f"约束={constraint_short}")
    return "·".join(parts)


def _add_stat(statistics: dict, value, envelope: dict) -> dict:
    entry = {"value": value, **envelope}
    statistics[envelope["stat_key"]] = entry
    return entry


def _agreement_for_scope(context: dict, *, roundtrip: bool) -> dict:
    agreements = context.get("agreements") or {}
    if roundtrip:
        return dict(
            agreements.get("roundtrip")
            or context.get("dual_source_agreement")
            or {}
        )
    return dict(
        agreements.get("outbound")
        or context.get("dual_source_agreement")
        or {}
    )


def _price_cells_for_scope(context: dict, *, roundtrip: bool) -> list[dict]:
    key = "roundtrip_price_cells" if roundtrip else "price_cells"
    return [cell for cell in (context.get(key) or []) if isinstance(cell, dict)]


def _sources_for_value(cells: list[dict], value) -> list[str]:
    price = _valid_price(value)
    if price is None:
        return []
    return sorted(
        {
            source
            for cell in cells
            if _valid_price(cell.get("min_price")) == price
            for source in _source_tokens(cell.get("sources"))
        }
    )


def _calendar_cell_for_row(
    cells: list[dict],
    row_date: str,
    value,
) -> dict | None:
    """匹配日历实际引用的日格，优先使用最近观测日。"""
    price = _valid_price(value)
    if len(str(row_date or "")) != 10 or price is None:
        return None
    matches = [
        cell
        for cell in cells
        if str(cell.get("depart_date") or "")[:10] == row_date
        and _valid_price(cell.get("min_price")) == price
    ]
    if not matches:
        return None
    return max(matches, key=lambda cell: str(cell.get("observed_day") or ""))


def _history_item_price(item):
    if isinstance(item, dict):
        return _valid_price(
            item.get("price")
            or item.get("total")
            or item.get("min_price")
            or item.get("value")
        )
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        return _valid_price(item[1])
    return _valid_price(item)


def _sources_for_history(cells: list[dict], history) -> list[str]:
    prices = {
        price
        for item in (history or [])
        if (price := _history_item_price(item)) is not None
    }
    return sorted(
        {
            source
            for cell in cells
            if _valid_price(cell.get("min_price")) in prices
            for source in _source_tokens(cell.get("sources"))
        }
    )


_MICRO_SUFFIX_RE = re.compile(r"（n=\d+(?:·窗口=[^）]*)?）")


def replace_micro_provenance(text: str, envelope: dict | None) -> str:
    """把旧的次数推测括注替换为标准信封中的真实日期窗口。"""
    value = str(text or "")
    note = format_micro_provenance(envelope)
    if _MICRO_SUFFIX_RE.search(value):
        return _MICRO_SUFFIX_RE.sub(note, value)
    return f"{value}{note}" if value else note


def attach_payload_provenance(
    payload: dict,
    *,
    context: dict | None = None,
    computed_at: str | None = None,
) -> dict:
    """给现有 payload 增量附着五族信封，不重算其统计值。"""
    payload = payload or {}
    context = context or payload.get("provenance_context") or {}
    computed_at = (
        computed_at
        or context.get("computed_at")
        or payload.get("collected_at")
        or datetime.now().astimezone().isoformat(timespec="seconds")
    )
    history = payload.get("price_history") or []
    collected_day = str(payload.get("collected_at") or "")[:10]
    fallback_window = context.get("window") or (
        [collected_day, collected_day] if len(collected_day) == 10 else [None, None]
    )
    history_window = _items_window(history, fallback_window)
    available_degraded_cells = int(context.get("degraded_excluded") or 0)
    is_roundtrip = bool(payload.get("is_roundtrip") or payload.get("return_date"))
    outbound_agreement = _agreement_for_scope(context, roundtrip=False)
    roundtrip_agreement = _agreement_for_scope(context, roundtrip=True)
    payload_agreement = roundtrip_agreement if is_roundtrip else outbound_agreement
    reference_cells = _price_cells_for_scope(context, roundtrip=is_roundtrip)
    base_bucket = _base_bucket(payload)
    statistics = {}

    references = payload.get("price_references") or {}
    history_n = len(history)
    for name, reference in references.items():
        if not isinstance(reference, dict):
            continue
        price = _valid_price(reference.get("price"))
        if price is None:
            continue
        stat_key = f"reftier.{name}"
        sample_n = reference.get("sample_size")
        if sample_n is None:
            sample_n = len(payload.get("recommended_plans") or []) if name in {"current", "current_min"} else history_n
        bucket = base_bucket
        if name == "conditional_min":
            days_to_dept = payload.get("days_to_dept")
            bucket += f"·提前购买天数={days_to_dept}±7" if days_to_dept is not None else "·提前购买天数=±7天同档"
        reference_sources = (
            reference.get("sources")
            or _sources_for_value(reference_cells, price)
        )
        reference_window = reference.get("window") or (
            fallback_window
            if name in {"current", "current_min", "current_median"}
            else history_window
        )
        envelope = build_envelope(
            stat_key,
            sample_n=sample_n,
            window=reference_window,
            sources=reference_sources,
            degraded_excluded=reference.get("degraded_excluded", 0),
            bucket=bucket,
            dual_source_agreement=roundtrip_agreement if is_roundtrip else outbound_agreement,
            computed_at=computed_at,
        )
        reference["provenance"] = envelope
        _add_stat(statistics, price, envelope)

    calendar = payload.get("price_calendar") or {}
    calendar_provenance = {}
    calendar_scope = str(calendar.get("scope") or "").lower()
    calendar_cells = _price_cells_for_scope(
        context,
        roundtrip=calendar_scope == "roundtrip",
    )
    for row in calendar.get("rows") or []:
        if not isinstance(row, dict):
            continue
        if not row.get("eligible_for_recommendation", True):
            continue
        row_date = str(row.get("date") or "")[:10]
        price = _valid_price(row.get("min_price") or row.get("value"))
        if len(row_date) != 10 or price is None:
            continue
        stat_key = f"calendar.{row_date}.min"
        matching_cell = _calendar_cell_for_row(calendar_cells, row_date, price)
        row_sources = (
            row.get("sources")
            or _sources_from_rows([row])
            or _sources_from_rows([matching_cell] if matching_cell else [])
        )
        row_sample_n = row.get("sample_n")
        if row_sample_n is None:
            row_sample_n = row.get("count")
        if row_sample_n is None and matching_cell:
            row_sample_n = 1
        row_window = _observation_window([row], [None, None])
        if row_window == [None, None] and matching_cell:
            observed_day = str(matching_cell.get("observed_day") or "")[:10]
            if len(observed_day) == 10:
                row_window = [observed_day, observed_day]
        envelope = build_envelope(
            stat_key,
            sample_n=row_sample_n,
            window=row_window,
            sources=row_sources,
            degraded_excluded=row.get("degraded_excluded", 0),
            bucket=f"{base_bucket}·日历日期={row_date}·口径={calendar.get('scope') or 'oneway'}",
            dual_source_agreement=(
                roundtrip_agreement
                if str(calendar.get("scope") or "").lower() == "roundtrip"
                else outbound_agreement
            ),
            computed_at=computed_at,
        )
        row["provenance"] = envelope
        calendar_provenance[stat_key] = envelope
        _add_stat(statistics, price, envelope)

    weekday = calendar.get("weekday_pattern") or {}
    if isinstance(weekday, dict):
        weekday["method_version"] = method_version("weekday")
        weekday_provenance = {}
        calendar_rows = [row for row in (calendar.get("rows") or []) if isinstance(row, dict)]
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        medians = weekday.get("median_by_weekday") or weekday.get("by_weekday") or {}
        counts = weekday.get("sample_count_by_weekday") or {}
        for weekday_name, median_price in medians.items():
            price = _valid_price(median_price)
            if price is None:
                continue
            matching_rows = []
            for row in calendar_rows:
                try:
                    row_weekday = weekday_names[date.fromisoformat(str(row.get("date"))[:10]).weekday()]
                except (TypeError, ValueError):
                    continue
                if row_weekday == weekday_name:
                    matching_rows.append(row)
            stat_key = f"weekday.{weekday_name}.median"
            envelope = build_envelope(
                stat_key,
                sample_n=counts.get(weekday_name) or weekday.get("sample_count") or 0,
                window=_observation_window(matching_rows, fallback_window),
                sources=_sources_from_rows(matching_rows),
                degraded_excluded=weekday.get("degraded_excluded", 0),
                bucket=f"{base_bucket}·星期={weekday_name}",
                dual_source_agreement=(
                    roundtrip_agreement
                    if str(calendar.get("scope") or "").lower() == "roundtrip"
                    else outbound_agreement
                ),
                computed_at=computed_at,
            )
            weekday_provenance[stat_key] = envelope
            _add_stat(statistics, price, envelope)
        minimum_price = _valid_price(weekday.get("min_price"))
        if minimum_price is not None:
            minimum_rows = [
                row
                for row in calendar_rows
                if str(row.get("date") or "")[:10] == str(weekday.get("min_date") or "")[:10]
                and _valid_price(row.get("min_price") or row.get("value")) == minimum_price
            ]
            stat_key = "weekday.minimum"
            envelope = build_envelope(
                stat_key,
                sample_n=weekday.get("sample_count") or sum(int(value or 0) for value in counts.values()),
                window=_observation_window(calendar_rows, fallback_window),
                sources=_sources_from_rows(minimum_rows),
                degraded_excluded=weekday.get("degraded_excluded", 0),
                bucket=f"{base_bucket}·最低价日期={weekday.get('min_date') or '未标明'}",
                dual_source_agreement=(
                    roundtrip_agreement
                    if str(calendar.get("scope") or "").lower() == "roundtrip"
                    else outbound_agreement
                ),
                computed_at=computed_at,
            )
            weekday_provenance[stat_key] = envelope
            _add_stat(
                statistics,
                {
                    "date": weekday.get("min_date"),
                    "weekday": weekday.get("min_weekday"),
                    "price": minimum_price,
                },
                envelope,
            )
        if weekday_provenance:
            weekday["provenance"] = weekday_provenance
    if calendar_provenance:
        calendar["provenance"] = calendar_provenance

    price_signal = payload.get("price_signal") or {}
    signal_sample_n = price_signal.get("sample_n")
    if signal_sample_n is None:
        signal_sample_n = history_n
    if isinstance(price_signal, dict) and (
        price_signal.get("percentile") is not None or signal_sample_n
    ):
        stat_key = "price_signal.history_position"
        signal_window = price_signal.get("window") or history_window
        signal_sources = (
            price_signal.get("sources")
            or _sources_for_history(
                reference_cells,
                price_signal.get("_provenance_history")
                or price_signal.get("history")
                or history,
            )
        )
        envelope = build_envelope(
            stat_key,
            sample_n=signal_sample_n,
            window=signal_window,
            sources=signal_sources,
            degraded_excluded=price_signal.get("degraded_excluded", 0),
            bucket=base_bucket,
            dual_source_agreement=roundtrip_agreement if is_roundtrip else outbound_agreement,
            computed_at=computed_at,
        )
        price_signal["provenance"] = envelope
        price_signal.pop("_provenance_history", None)
        if price_signal.get("summary"):
            price_signal["summary"] = replace_micro_provenance(
                price_signal["summary"],
                envelope,
            )
        rewritten_reasons = []
        for reason in payload.get("trigger_reason") or []:
            reason_text = str(reason)
            if any(marker in reason_text for marker in ("历史样本", "近期低位", "同条件样本")):
                reason_text = replace_micro_provenance(reason_text, envelope)
            rewritten_reasons.append(reason_text)
        if rewritten_reasons:
            payload["trigger_reason"] = rewritten_reasons
        _add_stat(
            statistics,
            price_signal.get("percentile")
            if price_signal.get("percentile") is not None
            else price_signal.get("summary"),
            envelope,
        )

    curve = payload.get("tcurve") or {}
    for point in curve.get("points") or []:
        if not isinstance(point, dict):
            continue
        t_value = int(point.get("t"))
        stat_key = f"tcurve.T{t_value}.median"
        existing = point.get("provenance") or {}
        envelope = build_envelope(
            stat_key,
            sample_n=point.get("n") or existing.get("sample_n") or 0,
            window=existing.get("window") or fallback_window,
            sources=existing.get("sources") or [],
            degraded_excluded=existing.get(
                "degraded_excluded",
                curve.get("degraded_excluded_count", 0),
            ),
            bucket=existing.get("bucket") or f"{base_bucket}·T={t_value}天",
            dual_source_agreement=outbound_agreement,
            computed_at=computed_at,
        )
        point["provenance"] = envelope
        if point.get("median") is not None:
            _add_stat(statistics, point.get("median"), envelope)

    forecast = payload.get("forecast") or {}
    if isinstance(forecast, dict) and forecast.get("eligible"):
        envelope = forecast.get("provenance") or {}
        for item in forecast.get("predictions") or []:
            if item.get("median") is not None:
                _add_stat(statistics, item.get("median"), envelope)

    payload_versions = dict(METHOD_VERSIONS)
    if not (isinstance(forecast, dict) and forecast.get("eligible")):
        payload_versions.pop("forecast", None)
    if not payload.get("patterns"):
        payload_versions.pop("patterns", None)
    payload["versions"] = payload_versions
    payload["dual_source_agreement"] = dict(payload_agreement)
    payload["provenance"] = {
        "method_version": method_version("provenance"),
        "computed_at": computed_at,
        "available_degraded_cells": available_degraded_cells,
        "statistics": statistics,
        "referenced_stat_keys": [],
    }
    return payload


def build_panel_report_payload(
    db_path: str | Path,
    *,
    route: str,
    airport_pair=None,
    as_of_date: date | None = None,
    window_days: int = AGREEMENT_WINDOW_DAYS,
    min_pairs: int = MIN_PAIRS_FOR_AGREEMENT,
    min_tcurve_sample: int = 5,
) -> dict | None:
    """把面板只读统计组装成与通知一致的五族信封。"""
    rows = load_route_observations(db_path, route=route, airport_pair=airport_pair)
    if not rows:
        return None
    origin_city, dest_city = split_route(route)
    grouped: dict[tuple[str, str], dict] = {}
    for row in rows:
        observed_day = str(row.get("observed_at") or "")[:10]
        depart_date = str(row.get("depart_date") or "")[:10]
        price = _valid_price(row.get("price_cny"))
        if not observed_day or not depart_date or price is None:
            continue
        cell = grouped.setdefault(
            (depart_date, observed_day),
            {"prices": [], "priced_sources": []},
        )
        cell["prices"].append(price)
        source = str(row.get("source") or "").strip().lower()
        if source:
            cell["priced_sources"].append((price, source))
    if not grouped:
        return None

    cells = []
    for (depart_date, observed_day), values in grouped.items():
        min_price = min(values["prices"])
        cells.append(
            {
                "depart_date": depart_date,
                "observed_day": observed_day,
                "min_price": min_price,
                "median_price": statistics.median(values["prices"]),
                "sources": sorted(
                    {
                        source
                        for price, source in values["priced_sources"]
                        if price == min_price
                    }
                ),
            }
        )
    cells.sort(key=lambda item: (item["observed_day"], item["depart_date"]))
    latest_day = max(item["observed_day"] for item in cells)
    latest_date = date.fromisoformat(latest_day)
    recent_start = (latest_date - timedelta(days=6)).isoformat()
    latest_cells = [item for item in cells if item["observed_day"] == latest_day]
    recent_cells = [item for item in cells if item["observed_day"] >= recent_start]
    all_prices = [item["min_price"] for item in cells]
    current_prices = [item["min_price"] for item in latest_cells]

    def contributing_sources(items: list[dict], selected_price=None) -> list[str]:
        selected = (
            [item for item in items if item["min_price"] == selected_price]
            if selected_price is not None
            else items
        )
        return sorted({source for item in selected for source in item["sources"]})

    latest_by_depart = {}
    for cell in cells:
        current = latest_by_depart.get(cell["depart_date"])
        if current is None or cell["observed_day"] > current["observed_day"]:
            latest_by_depart[cell["depart_date"]] = cell
    calendar_rows = [
        {
            "date": depart_date,
            "min_price": cell["min_price"],
            "sources": cell["sources"],
            "observed_at": cell["observed_day"],
            # 该值是“最新观测日”的一个日格最低价，不混入旧观测日样本数。
            "sample_n": 1,
        }
        for depart_date, cell in sorted(latest_by_depart.items())
    ]

    from price_calendar import analyze_weekday_pattern
    from tcurve import build_tcurve

    weekday_pattern = analyze_weekday_pattern(
        {
            "route": route,
            "dates": {
                row["date"]: {"min_price": row["min_price"]}
                for row in calendar_rows
            },
        },
        min_samples=1,
    ) or {"data_insufficient": True}
    below = sum(1 for value in all_prices if value < min(current_prices))
    percentile = round(below / len(all_prices) * 100) if all_prices else None
    curve = build_tcurve(
        db_path,
        route=route,
        airport_pair=airport_pair,
        min_sample=min_tcurve_sample,
        as_of_date=as_of_date,
    )
    context = build_route_provenance_context(
        db_path,
        route=route,
        airport_pair=airport_pair,
        as_of_date=as_of_date,
        window_days=window_days,
        min_pairs=min_pairs,
    )
    absolute_min = min(all_prices)
    recent_min = min(item["min_price"] for item in recent_cells)
    current_min = min(current_prices)
    all_sources = contributing_sources(cells)
    absolute_min_sources = contributing_sources(cells, absolute_min)
    recent_min_sources = contributing_sources(recent_cells, recent_min)
    current_min_sources = contributing_sources(latest_cells, current_min)
    current_sources = contributing_sources(latest_cells)
    all_window = [min(item["observed_day"] for item in cells), max(item["observed_day"] for item in cells)]
    recent_window = [min(item["observed_day"] for item in recent_cells), max(item["observed_day"] for item in recent_cells)]
    current_window = [latest_day, latest_day]
    payload = {
        "route": f"{origin_city} → {dest_city}",
        "price_references": {
            "absolute_min": {
                "price": absolute_min,
                "sample_size": len(all_prices),
                "window": all_window,
                "sources": absolute_min_sources,
            },
            "recent_min": {
                "price": recent_min,
                "sample_size": len(recent_cells),
                "window": recent_window,
                "sources": recent_min_sources,
            },
            "current_min": {
                "price": current_min,
                "sample_size": len(current_prices),
                "window": current_window,
                "sources": current_min_sources,
            },
            "current_median": {
                "price": statistics.median(current_prices),
                "sample_size": len(current_prices),
                "window": current_window,
                "sources": current_sources,
            },
        },
        "price_history": [
            {"date": item["observed_day"], "price": item["min_price"]}
            for item in cells
        ],
        "price_calendar": {
            "scope": "oneway",
            "rows": calendar_rows,
            "weekday_pattern": weekday_pattern,
        },
        "price_signal": {
            "label": "历史位置",
            "summary": "当前最低价在同航线面板日格中的位置",
            "percentile": percentile,
            "sample_n": len(all_prices),
            "sources": all_sources,
        },
        "tcurve": curve,
    }
    return attach_payload_provenance(payload, context=context)
