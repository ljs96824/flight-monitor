"""观测库的只读描述统计与双源一致度报告。"""

from __future__ import annotations

import argparse
import csv
import math
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from airports import get_airport_city


DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "observations.sqlite3"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "out"
MIN_CELL_N = 20
PRICE_CALIBER = "单人单程CNY含税"

OBSERVATION_COLUMNS = (
    "id",
    "observed_at",
    "round_id",
    "route_type",
    "origin_airport",
    "dest_airport",
    "depart_date",
    "days_to_departure",
    "cabin_class",
    "source",
    "flight_combo",
    "airline",
    "stops",
    "duration_min",
    "price_cny",
    "method_version",
)

LAYER_ORDER = (
    ("direct", "outbound"),
    ("direct", "return"),
    ("transfer", "outbound"),
    ("transfer", "return"),
)

STOP_LABELS = {"direct": "直飞", "transfer": "中转"}
DIRECTION_LABELS = {"outbound": "去程", "return": "返程"}


def _clean_number(value, digits=2):
    number = round(float(value), digits)
    return int(number) if number.is_integer() else number


def _observed_day(value) -> str:
    text = str(value or "").strip()
    if len(text) < 10:
        raise ValueError(f"observed_at不是有效日期时间: {value!r}")
    date.fromisoformat(text[:10])
    return text[:10]


def _readonly_connection(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"观测库不存在: {path}")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def load_observations(db_path: str | Path = DEFAULT_DB_PATH) -> list[dict]:
    """从只读连接加载观测行，不创建数据库或修改任何元数据。"""
    connection = _readonly_connection(db_path)
    try:
        available = {
            row[1]
            for row in connection.execute("PRAGMA table_info(observations)").fetchall()
        }
        required = set(OBSERVATION_COLUMNS) - {"duration_min"}
        missing = sorted(required - available)
        if missing:
            raise RuntimeError(f"observations缺少字段: {', '.join(missing)}")
        select_columns = [
            column if column in available else f"NULL AS {column}"
            for column in OBSERVATION_COLUMNS
        ]
        rows = connection.execute(
            f"SELECT {', '.join(select_columns)} FROM observations "
            "WHERE LOWER(cabin_class)='economy' ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def build_overview(rows: list[dict]) -> dict:
    source_rows = Counter(str(row.get("source") or "unknown") for row in rows)
    return {
        "total_rows": len(rows),
        "depart_dates": len({str(row.get("depart_date")) for row in rows}),
        "observed_days": len({_observed_day(row.get("observed_at")) for row in rows}),
        "rounds": len({str(row.get("round_id")) for row in rows}),
        "source_rows": dict(sorted(source_rows.items())),
        "price_caliber": PRICE_CALIBER,
    }


def fold_daily_cells(rows: list[dict]) -> list[dict]:
    """折叠为城市对、出发日、观测日单元，吸收机场键和同日多轮。"""
    grouped: dict[tuple, dict] = {}
    for row in rows:
        observed_day = _observed_day(row.get("observed_at"))
        depart_date = str(row.get("depart_date") or "")
        departure_day = date.fromisoformat(depart_date)
        origin_city = get_airport_city(row.get("origin_airport"))
        dest_city = get_airport_city(row.get("dest_airport"))
        key = (origin_city, dest_city, depart_date, observed_day)
        cell = grouped.setdefault(
            key,
            {
                "prices": [],
                "round_ids": set(),
                "route_types": set(),
            },
        )
        price = float(row.get("price_cny"))
        if price <= 0:
            continue
        cell["prices"].append(price)
        cell["round_ids"].add(str(row.get("round_id") or ""))
        cell["route_types"].add(str(row.get("route_type") or "unknown"))

    cells = []
    for (origin_city, dest_city, depart_date, observed_day), values in grouped.items():
        prices = values["prices"]
        if not prices:
            continue
        cells.append(
            {
                "origin_city": origin_city,
                "dest_city": dest_city,
                "depart_date": depart_date,
                "observed_day": observed_day,
                "days_to_departure": (
                    date.fromisoformat(depart_date) - date.fromisoformat(observed_day)
                ).days,
                "min_price": _clean_number(min(prices)),
                "median_price": _clean_number(statistics.median(prices)),
                "obs_rows": len(prices),
                "rounds": len(values["round_ids"]),
                "route_types": "+".join(sorted(values["route_types"])),
            }
        )
    return sorted(
        cells,
        key=lambda item: (
            item["origin_city"],
            item["dest_city"],
            item["depart_date"],
            item["observed_day"],
        ),
    )


def build_reference_rows(cells: list[dict]) -> list[dict]:
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for cell in cells:
        grouped[
            (cell["origin_city"], cell["dest_city"], cell["depart_date"])
        ].append(cell)

    references = []
    for (origin_city, dest_city, depart_date), series in grouped.items():
        ordered = sorted(series, key=lambda item: item["observed_day"])
        current_day = date.fromisoformat(ordered[-1]["observed_day"])
        recent_cutoff = current_day - timedelta(days=6)
        recent = [
            item
            for item in ordered
            if date.fromisoformat(item["observed_day"]) >= recent_cutoff
        ]
        current = [item for item in ordered if item["observed_day"] == ordered[-1]["observed_day"]]
        references.append(
            {
                "origin_city": origin_city,
                "dest_city": dest_city,
                "depart_date": depart_date,
                "historical_low": _clean_number(min(item["min_price"] for item in ordered)),
                "recent_7d_low": _clean_number(min(item["min_price"] for item in recent)),
                "current_low": _clean_number(min(item["min_price"] for item in current)),
                "current_median": _clean_number(
                    statistics.median(item["median_price"] for item in current)
                ),
                "current_observed_day": ordered[-1]["observed_day"],
                "observed_day_count": len({item["observed_day"] for item in ordered}),
            }
        )
    return sorted(
        references,
        key=lambda item: (item["origin_city"], item["dest_city"], item["depart_date"]),
    )


def _primary_orientations(rows: list[dict]) -> dict[tuple[str, str], tuple[str, str]]:
    unique_units = set()
    for row in rows:
        origin_city = get_airport_city(row.get("origin_airport"))
        dest_city = get_airport_city(row.get("dest_airport"))
        unique_units.add(
            (
                str(row.get("round_id") or ""),
                origin_city,
                dest_city,
                str(row.get("depart_date") or ""),
                str(row.get("flight_combo") or ""),
            )
        )
    counts = Counter((tuple(sorted((origin, dest))), (origin, dest)) for _, origin, dest, _, _ in unique_units)
    baselines = {}
    for unordered, _orientation in counts:
        choices = [
            (count, orientation)
            for (pair, orientation), count in counts.items()
            if pair == unordered
        ]
        baselines[unordered] = max(choices, key=lambda item: (item[0], item[1]))[1]
    return baselines


def build_source_pairs(rows: list[dict]) -> list[dict]:
    """配对同轮、同城市方向、同出发日、同舱等、同组合的双源价格。"""
    baselines = _primary_orientations(rows)
    grouped: dict[tuple, dict[str, dict]] = defaultdict(dict)
    for row in rows:
        source = str(row.get("source") or "").lower()
        if source not in {"hasdata", "juhe"}:
            continue
        origin_city = get_airport_city(row.get("origin_airport"))
        dest_city = get_airport_city(row.get("dest_airport"))
        key = (
            str(row.get("round_id") or ""),
            origin_city,
            dest_city,
            str(row.get("depart_date") or ""),
            str(row.get("cabin_class") or ""),
            str(row.get("flight_combo") or ""),
        )
        price = float(row.get("price_cny"))
        stops = row.get("stops")
        current = grouped[key].get(source)
        if current is None or price < current["price"]:
            grouped[key][source] = {"price": price, "stops": stops}
        elif stops is not None:
            current_stops = current.get("stops")
            current["stops"] = max(int(stops), int(current_stops or 0))

    pairs = []
    for key, sources in grouped.items():
        if "hasdata" not in sources or "juhe" not in sources:
            continue
        round_id, origin_city, dest_city, depart_date, cabin_class, combo = key
        hasdata_price = sources["hasdata"]["price"]
        juhe_price = sources["juhe"]["price"]
        lower = min(hasdata_price, juhe_price)
        if lower <= 0:
            continue
        unordered = tuple(sorted((origin_city, dest_city)))
        direction = (
            "outbound"
            if (origin_city, dest_city) == baselines.get(unordered, (origin_city, dest_city))
            else "return"
        )
        stops_values = [
            int(item["stops"])
            for item in sources.values()
            if item.get("stops") is not None
        ]
        inferred_transfer = "+" in combo
        stop_kind = "transfer" if inferred_transfer or any(value > 0 for value in stops_values) else "direct"
        if hasdata_price > juhe_price:
            price_direction = "hasdata_high"
        elif juhe_price > hasdata_price:
            price_direction = "juhe_high"
        else:
            price_direction = "equal"
        pairs.append(
            {
                "round_id": round_id,
                "origin_city": origin_city,
                "dest_city": dest_city,
                "depart_date": depart_date,
                "cabin_class": cabin_class,
                "flight_combo": combo,
                "stop_kind": stop_kind,
                "direction": direction,
                "hasdata_price": _clean_number(hasdata_price),
                "juhe_price": _clean_number(juhe_price),
                "gap_pct": abs(hasdata_price - juhe_price) / lower * 100,
                "price_direction": price_direction,
            }
        )
    return sorted(
        pairs,
        key=lambda item: (
            item["stop_kind"],
            item["direction"],
            item["round_id"],
            item["flight_combo"],
        ),
    )


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def summarize_source_consistency(pairs: list[dict], min_n: int = MIN_CELL_N) -> list[dict]:
    if min_n < 1:
        raise ValueError("min_n必须大于0")
    result = []
    for stop_kind, direction in LAYER_ORDER:
        layer = [
            item
            for item in pairs
            if item["stop_kind"] == stop_kind and item["direction"] == direction
        ]
        pair_count = len(layer)
        sufficient = pair_count >= min_n
        direction_counts = Counter(item["price_direction"] for item in layer)
        gaps = [float(item["gap_pct"]) for item in layer]
        result.append(
            {
                "stop_kind": stop_kind,
                "direction": direction,
                "pair_count": pair_count,
                "min_n": min_n,
                "sufficient": sufficient,
                "median_gap_pct": (
                    round(statistics.median(gaps), 4) if sufficient else None
                ),
                "p90_gap_pct": (
                    round(_nearest_rank(gaps, 0.90), 4) if sufficient else None
                ),
                "hasdata_high_count": direction_counts["hasdata_high"],
                "juhe_high_count": direction_counts["juhe_high"],
                "equal_count": direction_counts["equal"],
                "hasdata_high_pct": (
                    round(direction_counts["hasdata_high"] / pair_count * 100, 2)
                    if sufficient
                    else None
                ),
                "juhe_high_pct": (
                    round(direction_counts["juhe_high"] / pair_count * 100, 2)
                    if sufficient
                    else None
                ),
                "equal_pct": (
                    round(direction_counts["equal"] / pair_count * 100, 2)
                    if sufficient
                    else None
                ),
            }
        )
    return result


def _format_price(value) -> str:
    return f"CNY{float(value):,.0f}"


def render_consistency_block(stats: list[dict], min_n: int = MIN_CELL_N) -> str:
    lines = ["【双源一致度】", "差价定义=|hasdata-juhe|/两者较低价；P90采用nearest-rank。"]
    for item in stats:
        label = f"{STOP_LABELS[item['stop_kind']]}/{DIRECTION_LABELS[item['direction']]}"
        if not item["sufficient"]:
            lines.append(f"{label}: 数据不足(n={item['pair_count']}) | 门槛={min_n}")
            continue
        lines.append(
            f"{label}: 配对数={item['pair_count']} | "
            f"中位差={item['median_gap_pct']:.2f}% | P90差={item['p90_gap_pct']:.2f}% | "
            f"方向分布=hasdata高 {item['hasdata_high_pct']:.2f}%({item['hasdata_high_count']}) / "
            f"juhe高 {item['juhe_high_pct']:.2f}%({item['juhe_high_count']}) / "
            f"平 {item['equal_pct']:.2f}%({item['equal_count']})"
        )
    return "\n".join(lines)


def render_report(
    overview: dict,
    cells: list[dict],
    references: list[dict],
    consistency_stats: list[dict],
    min_n: int,
) -> str:
    lines = [
        "观测描述统计报告",
        f"口径={overview['price_caliber']}",
        "折叠键=(origin_city,dest_city,depart_date,observed_day)，同日多轮不重复计天。",
        "去返定义=同一无向城市对中观测单元较多的有向城市对记为去程，反向记为返程。",
        "",
        "【总览】",
        f"总行数={overview['total_rows']}",
        f"出发日数={overview['depart_dates']}",
        f"观测日数={overview['observed_days']}",
        f"轮数={overview['rounds']}",
        "分源行数=" + " / ".join(
            f"{source}:{count}" for source, count in overview["source_rows"].items()
        ),
        "",
        "【纵向序列与参考价数据版v1】",
    ]
    reference_map = {
        (item["origin_city"], item["dest_city"], item["depart_date"]): item
        for item in references
    }
    grouped_cells: dict[tuple, list[dict]] = defaultdict(list)
    for cell in cells:
        grouped_cells[
            (cell["origin_city"], cell["dest_city"], cell["depart_date"])
        ].append(cell)
    for key in sorted(grouped_cells):
        origin_city, dest_city, depart_date = key
        series = sorted(grouped_cells[key], key=lambda item: item["observed_day"])
        lines.append(f"{origin_city}→{dest_city} | 出发日={depart_date}")
        for cell in series:
            lines.append(
                f"  {cell['observed_day']}(D-{cell['days_to_departure']}): "
                f"min={_format_price(cell['min_price'])}, "
                f"median={_format_price(cell['median_price'])}, "
                f"rows={cell['obs_rows']}, rounds={cell['rounds']}"
            )
        reference = reference_map[key]
        lines.extend(
            [
                f"  [无条件历史最低] {_format_price(reference['historical_low'])}",
                f"  [近7日最低] {_format_price(reference['recent_7d_low'])}",
                f"  [当前最低] {_format_price(reference['current_low'])}",
                f"  [当前中位] {_format_price(reference['current_median'])}",
            ]
        )
    lines.extend(["", render_consistency_block(consistency_stats, min_n=min_n)])
    return "\n".join(lines)


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_csv_reports(
    out_dir: str | Path,
    overview: dict,
    cells: list[dict],
    references: list[dict],
    consistency_stats: list[dict],
) -> list[Path]:
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    overview_rows = [
        {"metric": "total_rows", "value": overview["total_rows"]},
        {"metric": "depart_dates", "value": overview["depart_dates"]},
        {"metric": "observed_days", "value": overview["observed_days"]},
        {"metric": "rounds", "value": overview["rounds"]},
        {"metric": "price_caliber", "value": overview["price_caliber"]},
    ]
    overview_rows.extend(
        {"metric": f"source_{source}_rows", "value": count}
        for source, count in overview["source_rows"].items()
    )
    consistency_rows = []
    for item in consistency_stats:
        row = dict(item)
        row["status"] = "ok" if item["sufficient"] else f"数据不足(n={item['pair_count']})"
        if not item["sufficient"]:
            for key in (
                "median_gap_pct",
                "p90_gap_pct",
                "hasdata_high_pct",
                "juhe_high_pct",
                "equal_pct",
            ):
                row[key] = ""
        consistency_rows.append(row)

    paths = [
        target / "overview.csv",
        target / "daily_cells.csv",
        target / "reference_prices.csv",
        target / "source_consistency.csv",
    ]
    _write_csv(paths[0], overview_rows, ["metric", "value"])
    _write_csv(
        paths[1],
        cells,
        [
            "origin_city",
            "dest_city",
            "depart_date",
            "observed_day",
            "days_to_departure",
            "min_price",
            "median_price",
            "obs_rows",
            "rounds",
            "route_types",
        ],
    )
    _write_csv(
        paths[2],
        references,
        [
            "origin_city",
            "dest_city",
            "depart_date",
            "historical_low",
            "recent_7d_low",
            "current_low",
            "current_median",
            "current_observed_day",
            "observed_day_count",
        ],
    )
    _write_csv(
        paths[3],
        consistency_rows,
        [
            "stop_kind",
            "direction",
            "pair_count",
            "min_n",
            "status",
            "median_gap_pct",
            "p90_gap_pct",
            "hasdata_high_count",
            "hasdata_high_pct",
            "juhe_high_count",
            "juhe_high_pct",
            "equal_count",
            "equal_pct",
        ],
    )
    return paths


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须大于0")
    return number


def run_cli(argv=None) -> int:
    parser = argparse.ArgumentParser(description="观测库只读描述统计报告")
    parser.add_argument("--csv", action="store_true", help="同步输出CSV到analytics/out")
    parser.add_argument("--min-n", type=_positive_int, default=MIN_CELL_N, help="双源统计最小样本数")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="只读观测库路径")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        rows = load_observations(args.db)
        overview = build_overview(rows)
        cells = fold_daily_cells(rows)
        references = build_reference_rows(cells)
        pairs = build_source_pairs(rows)
        consistency_stats = summarize_source_consistency(pairs, min_n=args.min_n)
        print(render_report(overview, cells, references, consistency_stats, args.min_n))
        if args.csv:
            paths = write_csv_reports(
                DEFAULT_OUT_DIR,
                overview,
                cells,
                references,
                consistency_stats,
            )
            print("\nCSV输出=" + " / ".join(str(path) for path in paths))
        return 0
    except (FileNotFoundError, RuntimeError, sqlite3.Error, ValueError) as exc:
        print(f"报告失败: {exc}", file=sys.stderr)
        return 1
