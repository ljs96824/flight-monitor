#!/usr/bin/env python3
"""只读审计 PermissionError 受影响轮次对观测面板与统计层的影响。"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from airports import get_airport_city  # noqa: E402
from tcurve import fold_tcurve_daily_cells  # noqa: E402


DEFAULT_OBSERVATIONS_DB = ROOT / "data" / "observations.sqlite3"
DEFAULT_PRICES_DB = ROOT / "data" / "prices.db"
DEFAULT_LOGS_DIR = ROOT / "data" / "logs" / "rounds"

AFFECTED_ROUND_IDS = (
    "collection_20260813T150004945725",
    "collection_20260813T210009452471",
    "collection_20260814T150016705469",
    "collection_20260814T210010511289",
    "collection_20260815T150004143251",
    "collection_20260817T210013799318",
    "collection_20260819T210010346027",
    "collection_20260821T150007460343",
    "collection_20260821T210009191769",
    "collection_20260823T210014035475",
)

ROUND_START_RE = re.compile(r"\[轮档开始\]\s+round_id=([^\s]+)")
ROUND_END_RE = re.compile(r"\[轮档结束\]\s+round_id=([^\s]+)")
FAILURE_RE = re.compile(
    r"\[采集失败入池\]\s+源=(?P<source>[^\s]+)\s+"
    r"航线=(?P<origin>[A-Z]{3})->(?P<destination>[A-Z]{3})\s+"
    r"日期=(?P<depart_date>\d{4}-\d{2}-\d{2})\s+"
    r"原因=PermissionError(?P<reason>.*?)(?=(?:\\n|\n|$))"
)
ROUND_TIME_RE = re.compile(r"^[^_]+_(\d{8}T\d{6})")

EPOCH_TABLES = {
    "flight_details": "snapshot_time",
    "roundtrip_price_history": "snapshot_time",
    "push_snapshots": "pushed_at",
}
EPOCH_FIRST_EVENT_MAX_DELAY = timedelta(minutes=5)
EPOCH_CHAIN_MAX_GAP = timedelta(minutes=5)
EPOCH_MAX_HORIZON = timedelta(minutes=90)


@contextmanager
def readonly_connection(path: str | Path):
    """以 SQLite mode=ro 打开数据库，保证不存在时也不会创建文件。"""
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"数据库不存在: {resolved}")
    connection = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro",
        uri=True,
        timeout=3,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def parse_round_start(round_id: str) -> datetime | None:
    match = ROUND_TIME_RE.match(str(round_id or ""))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S")
    except ValueError:
        return None


def _iter_log_files(logs_dir: str | Path):
    path = Path(logs_dir)
    if not path.is_dir():
        raise FileNotFoundError(f"轮档目录不存在: {path.resolve()}")
    yield from sorted(path.glob("*.log"))


def load_failure_requests(
    logs_dir: str | Path,
    round_ids: list[str] | tuple[str, ...],
) -> list[dict]:
    """从轮档边界内提取指定轮次的 PermissionError 请求。"""
    targets = set(round_ids)
    rows = []
    for path in _iter_log_files(logs_dir):
        current_round = None
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8", errors="strict").splitlines(),
            start=1,
        ):
            start_match = ROUND_START_RE.search(line)
            if start_match:
                current_round = start_match.group(1)
            if current_round in targets:
                for match in FAILURE_RE.finditer(line):
                    rows.append(
                        {
                            "round_id": current_round,
                            "observed_day": _round_observed_day(current_round),
                            "source": match.group("source").strip().lower(),
                            "origin": match.group("origin"),
                            "destination": match.group("destination"),
                            "depart_date": match.group("depart_date"),
                            "reason": "PermissionError" + match.group("reason").strip(),
                            "log_file": path.name,
                            "line": line_number,
                        }
                    )
            end_match = ROUND_END_RE.search(line)
            if end_match and end_match.group(1) == current_round:
                current_round = None
    return rows


def _round_observed_day(round_id: str) -> str:
    started = parse_round_start(round_id)
    return started.date().isoformat() if started else ""


def _placeholders(values) -> str:
    return ",".join("?" for _ in values)


def _load_exact_observations(
    observations_db: str | Path,
    round_ids: list[str] | tuple[str, ...],
) -> list[dict]:
    if not round_ids:
        return []
    columns = (
        "observed_at, round_id, route_type, origin_airport, dest_airport, "
        "depart_date, days_to_departure, cabin_class, source, flight_combo, "
        "price_cny"
    )
    with readonly_connection(observations_db) as connection:
        rows = connection.execute(
            f"SELECT {columns} FROM observations "
            f"WHERE round_id IN ({_placeholders(round_ids)}) "
            "ORDER BY round_id, origin_airport, dest_airport, depart_date, "
            "cabin_class, source, price_cny",
            tuple(round_ids),
        ).fetchall()
    return [dict(row) for row in rows]


def _load_relevant_economy_rows(
    observations_db: str | Path,
    failures: list[dict],
) -> list[dict]:
    observed_days = sorted({row["observed_day"] for row in failures if row["observed_day"]})
    depart_dates = sorted({row["depart_date"] for row in failures if row["depart_date"]})
    if not observed_days or not depart_dates:
        return []
    columns = (
        "observed_at, round_id, route_type, origin_airport, dest_airport, "
        "depart_date, days_to_departure, cabin_class, source, flight_combo, "
        "price_cny"
    )
    params = tuple(observed_days) + tuple(depart_dates)
    with readonly_connection(observations_db) as connection:
        rows = connection.execute(
            f"SELECT {columns} FROM observations "
            "WHERE LOWER(cabin_class)='economy' AND price_cny>0 "
            f"AND substr(observed_at,1,10) IN ({_placeholders(observed_days)}) "
            f"AND depart_date IN ({_placeholders(depart_dates)})",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def _failure_cell_key(row: dict) -> tuple[str, str, str, str]:
    return (
        get_airport_city(row["origin"]),
        get_airport_city(row["destination"]),
        row["depart_date"],
        row["observed_day"],
    )


def _row_cell_key(row: dict) -> tuple[str, str, str, str]:
    return (
        get_airport_city(row["origin_airport"]),
        get_airport_city(row["dest_airport"]),
        str(row["depart_date"]),
        str(row["observed_at"])[:10],
    )


def _direction(origin_city: str, destination_city: str) -> str:
    if origin_city == "上海" and destination_city == "大阪":
        return "去程"
    if origin_city == "大阪" and destination_city == "上海":
        return "返程"
    return f"{origin_city}->{destination_city}"


def _build_affected_cells(
    failures: list[dict],
    economy_rows: list[dict],
    affected_round_ids: set[str],
) -> list[dict]:
    failures_by_cell = defaultdict(list)
    for failure in failures:
        failures_by_cell[_failure_cell_key(failure)].append(failure)

    rows_by_cell = defaultdict(list)
    for row in economy_rows:
        key = _row_cell_key(row)
        if key in failures_by_cell:
            rows_by_cell[key].append(row)

    folded_by_cell = {
        (
            cell["origin_city"],
            cell["dest_city"],
            cell["depart_date"],
            cell["observed_day"],
        ): cell
        for cell in fold_tcurve_daily_cells(
            [row for key in failures_by_cell for row in rows_by_cell.get(key, [])]
        )
    }

    cells = []
    for key in sorted(failures_by_cell):
        origin_city, destination_city, depart_date, observed_day = key
        failure_rows = failures_by_cell[key]
        all_rows = rows_by_cell.get(key, [])
        affected_rows = [
            row for row in all_rows if str(row.get("round_id")) in affected_round_ids
        ]
        folded = folded_by_cell.get(key)
        degraded = bool(folded and folded.get("degraded"))
        entered_tcurve = bool(folded and not degraded)
        affected_contributed = bool(affected_rows and entered_tcurve)
        if not all_rows:
            bias = "该日格缺失，不进入global_min；影响是样本n减少，不是已存最低价被抬高"
        elif not affected_rows:
            bias = (
                "global_min来自同日其他轮；受影响轮未直接贡献。失败请求若有更低新价，"
                "其反事实不可知"
            )
        elif degraded:
            bias = "受影响轮有经济舱行，但该日格被degraded门控剔除"
        else:
            bias = "受影响轮经济舱行进入global_min且未被degraded标记，属于确认污染"
        cells.append(
            {
                "observed_day": observed_day,
                "origin_city": origin_city,
                "dest_city": destination_city,
                "direction": _direction(origin_city, destination_city),
                "depart_date": depart_date,
                "t": (date.fromisoformat(depart_date) - date.fromisoformat(observed_day)).days,
                "failure_events": len(failure_rows),
                "failed_sources": sorted({row["source"] for row in failure_rows}),
                "failed_airport_pairs": sorted(
                    {f'{row["origin"]}->{row["destination"]}' for row in failure_rows}
                ),
                "affected_round_economy_rows": len(affected_rows),
                "all_day_row_count": len(all_rows),
                "contributing_rounds": sorted(
                    {str(row.get("round_id") or "") for row in all_rows}
                ),
                "global_min": folded.get("min_price") if folded else None,
                "min_sources": folded.get("min_sources", []) if folded else [],
                "source_coverage": folded.get("source_coverage", []) if folded else [],
                "expected_sources": folded.get("expected_sources", []) if folded else [],
                "degraded": degraded if folded else None,
                "entered_tcurve": entered_tcurve,
                "affected_round_contributed": affected_contributed,
                "global_min_bias_assessment": bias,
            }
        )
    return cells


def _build_exact_groups(exact_rows: list[dict], affected_cells: list[dict]) -> list[dict]:
    cell_lookup = {
        (
            cell["origin_city"],
            cell["dest_city"],
            cell["depart_date"],
            cell["observed_day"],
        ): cell
        for cell in affected_cells
    }
    grouped = defaultdict(list)
    for row in exact_rows:
        grouped[
            (
                str(row["round_id"]),
                str(row["origin_airport"]),
                str(row["dest_airport"]),
                str(row["depart_date"]),
                str(row["cabin_class"]).lower(),
                str(row["source"]).lower(),
            )
        ].append(row)
    results = []
    for key, rows in sorted(grouped.items()):
        round_id, origin, destination, depart_date, cabin, source = key
        observed_day = str(rows[0]["observed_at"])[:10]
        city_key = (
            get_airport_city(origin),
            get_airport_city(destination),
            depart_date,
            observed_day,
        )
        cell = cell_lookup.get(city_key)
        results.append(
            {
                "round_id": round_id,
                "route": f"{origin}->{destination}",
                "city_route": f"{city_key[0]}->{city_key[1]}",
                "direction": _direction(city_key[0], city_key[1]),
                "depart_date": depart_date,
                "cabin_class": cabin,
                "source": source,
                "rows": len(rows),
                "min_price": min(float(row["price_cny"]) for row in rows),
                "max_price": max(float(row["price_cny"]) for row in rows),
                "degraded": (
                    cell.get("degraded") if cabin == "economy" and cell else "不适用"
                ),
                "entered_tcurve": bool(
                    cabin == "economy" and cell and cell.get("entered_tcurve")
                ),
            }
        )
    return results


def _count_affected_agreement_pairs(exact_rows: list[dict]) -> int:
    grouped = defaultdict(set)
    for row in exact_rows:
        if str(row.get("cabin_class") or "").lower() != "economy":
            continue
        key = (
            str(row.get("round_id") or ""),
            get_airport_city(row.get("origin_airport")),
            get_airport_city(row.get("dest_airport")),
            str(row.get("depart_date") or ""),
            str(row.get("observed_at") or "")[:10],
            str(row.get("flight_combo") or ""),
            "economy",
        )
        grouped[key].add(str(row.get("source") or "").lower())
    return sum(1 for sources in grouped.values() if {"hasdata", "juhe"}.issubset(sources))


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _load_epoch_events(prices_db: str | Path) -> tuple[list[dict], dict]:
    events = []
    schema = {}
    with readonly_connection(prices_db) as connection:
        for table, time_column in EPOCH_TABLES.items():
            columns = _table_columns(connection, table)
            schema[table] = {
                "columns": sorted(columns),
                "has_round_id": "round_id" in columns,
            }
            if time_column not in columns:
                continue
            fingerprint_expr = (
                "constraint_fingerprint" if "constraint_fingerprint" in columns else "NULL"
            )
            for row in connection.execute(
                f"SELECT {time_column} AS event_time, "
                f"{fingerprint_expr} AS fingerprint FROM {table} "
                f"WHERE {time_column} IS NOT NULL ORDER BY {time_column}"
            ):
                try:
                    parsed = datetime.fromisoformat(str(row["event_time"]))
                except ValueError:
                    continue
                events.append(
                    {
                        "table": table,
                        "event_time": parsed,
                        "fingerprint": str(row["fingerprint"] or ""),
                    }
                )
    return sorted(events, key=lambda item: item["event_time"]), schema


def infer_epoch_candidates(
    prices_db: str | Path,
    round_ids: list[str] | tuple[str, ...],
) -> list[dict]:
    """按时间链给出候选，明确不把无 round_id 的表伪装成精确归因。"""
    events, schema = _load_epoch_events(prices_db)
    results = []
    for round_id in round_ids:
        started = parse_round_start(round_id)
        selected = []
        if started:
            candidates = [
                event
                for event in events
                if started <= event["event_time"] <= started + EPOCH_MAX_HORIZON
            ]
            if candidates and candidates[0]["event_time"] - started <= EPOCH_FIRST_EVENT_MAX_DELAY:
                selected.append(candidates[0])
                for event in candidates[1:]:
                    if event["event_time"] - selected[-1]["event_time"] > EPOCH_CHAIN_MAX_GAP:
                        break
                    selected.append(event)
        counts = Counter(event["table"] for event in selected)
        results.append(
            {
                "round_id": round_id,
                "evidence_level": "时间链候选",
                "direct_attribution": False,
                "reason": "价格库三张表均无round_id，只能按轮次开始后的连续事件链圈定候选",
                "window": [
                    selected[0]["event_time"].isoformat(timespec="seconds"),
                    selected[-1]["event_time"].isoformat(timespec="seconds"),
                ] if selected else [None, None],
                "counts": {table: int(counts.get(table, 0)) for table in EPOCH_TABLES},
                "fingerprints": sorted(
                    {event["fingerprint"] for event in selected if event["fingerprint"]}
                ),
                "schema_has_round_id": {
                    table: bool(info["has_round_id"]) for table, info in schema.items()
                },
            }
        )
    return results


def build_audit(
    *,
    observations_db: str | Path,
    prices_db: str | Path,
    logs_dir: str | Path,
    round_ids: list[str] | tuple[str, ...] = AFFECTED_ROUND_IDS,
) -> dict:
    round_ids = tuple(round_ids)
    failures = load_failure_requests(logs_dir, round_ids)
    exact_rows = _load_exact_observations(observations_db, round_ids)
    economy_rows = _load_relevant_economy_rows(observations_db, failures)
    cells = _build_affected_cells(failures, economy_rows, set(round_ids))
    exact_groups = _build_exact_groups(exact_rows, cells)
    affected_by_t = Counter(
        cell["t"] for cell in cells if cell["affected_round_contributed"]
    )
    epoch_candidates = infer_epoch_candidates(prices_db, round_ids)
    unmarked = [
        cell
        for cell in cells
        if cell["affected_round_economy_rows"] > 0
        and cell["entered_tcurve"]
        and not cell["degraded"]
    ]
    exact_rounds = sorted({str(row["round_id"]) for row in exact_rows})
    failure_rounds = sorted({row["round_id"] for row in failures})
    return {
        "scope": {
            "round_ids": list(round_ids),
            "round_count": len(round_ids),
            "failure_rounds_found": failure_rounds,
            "failure_event_count": len(failures),
        },
        "exact_observations": {
            "row_count": len(exact_rows),
            "economy_row_count": sum(
                1
                for row in exact_rows
                if str(row.get("cabin_class") or "").lower() == "economy"
            ),
            "rounds_with_rows": exact_rounds,
            "groups": exact_groups,
        },
        "affected_cells": cells,
        "tcurve_impact": {
            "affected_n_total": sum(affected_by_t.values()),
            "affected_n_by_t": {str(key): affected_by_t[key] for key in sorted(affected_by_t)},
            "missing_cell_count": sum(
                1 for cell in cells if cell["all_day_row_count"] == 0
            ),
            "missing_cell_t_values": sorted(
                {cell["t"] for cell in cells if cell["all_day_row_count"] == 0}
            ),
            "degraded_cell_t_values": sorted(
                {cell["t"] for cell in cells if cell["degraded"] is True}
            ),
            "same_day_other_round_t_values": sorted(
                {
                    cell["t"]
                    for cell in cells
                    if cell["all_day_row_count"] > 0
                    and cell["affected_round_economy_rows"] == 0
                }
            ),
            "note": "仅经济舱、非degraded日格会进入tcurve；按日格每个T贡献n=1",
        },
        "agreement_impact": {
            "affected_pair_count": _count_affected_agreement_pairs(exact_rows),
            "note": "现行一致度仅配对同轮同组合的hasdata/juhe经济舱行",
        },
        "epoch_impact": {
            "attribution": "无法精确归因",
            "candidates": epoch_candidates,
        },
        "conclusion": {
            "unmarked_pollution_confirmed": bool(unmarked),
            "unmarked_cells": unmarked,
            "direct_global_min_bias_confirmed": any(
                cell["affected_round_contributed"] for cell in cells
            ),
            "counterfactual_unknown": bool(
                any(cell["failure_events"] for cell in cells)
            ),
        },
    }


def _money(value) -> str:
    if value is None:
        return "-"
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.2f}"


def render_report(audit: dict) -> str:
    scope = audit["scope"]
    exact = audit["exact_observations"]
    tcurve = audit["tcurve_impact"]
    agreement = audit["agreement_impact"]
    conclusion = audit["conclusion"]
    lines = [
        "# PermissionError 污染轮观测体检",
        "",
        "## 1. 结论",
        "",
    ]
    if conclusion["unmarked_pollution_confirmed"]:
        lines.append(
            "确认存在受影响轮经济舱样本进入 T 曲线且未被 degraded 标记，需人工选择处置。"
        )
    else:
        lines.append(
            "未确认受影响轮的经济舱观测污染进入 T 曲线或双源一致度；不建议修改面板数据。"
        )
    lines.extend(
        [
            "",
            f"- 诊断轮次：{scope['round_count']}；轮档命中：{len(scope['failure_rounds_found'])}；"
            f"PermissionError 事件：{scope['failure_event_count']}。",
            f"- 精确按 `round_id` 入库：{exact['row_count']} 行，其中 economy="
            f"{exact['economy_row_count']}。",
            f"- T 曲线中含受影响轮污染样本的 n：{tcurve['affected_n_total']}；另有 "
            f"{tcurve['missing_cell_count']} 个日格因失败未形成；历史一致度受影响配对："
            f"{agreement['affected_pair_count']}。",
            "- 去返方向缺失不会直接把另一个方向的 `global_min` 抬高；它会造成该方向日格缺失或"
            "退化。若同日其他轮仍有格，失败请求本可返回的价格属于不可观测反事实。",
            "",
            "## 2. 审计口径",
            "",
            "- `observations.sqlite3`：以 `round_id` 精确归因；degraded 按生产源策略动态计算，"
            "不是库内持久字段。",
            "- T 曲线：仅 economy 日格；城市级 `(航线, depart_date, observed_day)` 取跨源"
            " `global_min`；degraded 默认剔除。",
            "- 双源一致度：仅 economy，同轮同组合的 HasData/Juhe 配对。",
            "- 约束纪元：`prices.db` 三张相关表没有 `round_id`，只能输出时间链候选，"
            "不得视作精确污染证据。",
            "",
            "## 3. 精确入库清单",
            "",
        ]
    )
    if not exact["groups"]:
        lines.append("指定轮次没有写入任何观测行。")
    else:
        lines.extend(
            [
                "| round_id | 航线 | 方向 | 日期 | 舱位 | 源 | 行数 | 价格范围 | degraded | 入T曲线 |",
                "|---|---|---|---|---|---:|---:|---:|---|---|",
            ]
        )
        for row in exact["groups"]:
            lines.append(
                f"| `{row['round_id']}` | {row['route']} | {row['direction']} | "
                f"{row['depart_date']} | {row['cabin_class']} | {row['source']} | "
                f"{row['rows']} | CNY{_money(row['min_price'])}-CNY{_money(row['max_price'])} | "
                f"{row['degraded']} | {row['entered_tcurve']} |"
            )
    lines.extend(
        [
            "",
            "## 4. 受影响日格",
            "",
            "| 观测日 | 方向 | depart_date | T | 失败请求 | 当日economy行 | 受影响轮economy行 | 覆盖/期望源 | degraded | 入T | global_min | 影响判断 |",
            "|---|---|---:|---:|---:|---:|---:|---|---|---|---:|---|",
        ]
    )
    for cell in audit["affected_cells"]:
        coverage = ",".join(cell["source_coverage"]) or "-"
        expected = ",".join(cell["expected_sources"]) or "-"
        lines.append(
            f"| {cell['observed_day']} | {cell['direction']} | {cell['depart_date']} | "
            f"{cell['t']} | {cell['failure_events']} | {cell['all_day_row_count']} | "
            f"{cell['affected_round_economy_rows']} | {coverage}/{expected} | "
            f"{cell['degraded']} | {cell['entered_tcurve']} | "
            f"CNY{_money(cell['global_min'])} | {cell['global_min_bias_assessment']} |"
        )
    lines.extend(
        [
            "",
            "### T 格 n 影响",
            "",
            f"- 受影响轮直接进入的 T 格：{json.dumps(tcurve['affected_n_by_t'], ensure_ascii=False)}。",
            f"- 总受影响 n={tcurve['affected_n_total']}。",
            f"- 因整格缺失而少一个日格样本的 T：{tcurve['missing_cell_t_values']}。",
            f"- 有同日其他轮数据、但受影响轮未贡献的 T："
            f"{tcurve['same_day_other_round_t_values']}。",
            f"- 被 degraded 门控剔除的受影响日格 T：{tcurve['degraded_cell_t_values']}。",
            "",
            "### 双源一致度影响",
            "",
            f"- 受影响配对数={agreement['affected_pair_count']}。{agreement['note']}。",
            "",
            "## 5. 约束纪元样本",
            "",
            "> 证据等级：时间链候选。价格库没有 `round_id`，下表不能证明某条纪元样本由"
            "指定 collection 轮写入。",
            "",
            "| round_id | 候选窗口 | flight_details | roundtrip_history | push_snapshots | 指纹数 |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for row in audit["epoch_impact"]["candidates"]:
        window = "-" if row["window"][0] is None else f"{row['window'][0]}~{row['window'][1]}"
        lines.append(
            f"| `{row['round_id']}` | {window} | {row['counts']['flight_details']} | "
            f"{row['counts']['roundtrip_price_history']} | {row['counts']['push_snapshots']} | "
            f"{len(row['fingerprints'])} |"
        )
    lines.extend(
        [
            "",
            "## 6. 处置选项",
            "",
            "本次精确证据不支持修改 `observations`。若未来通过新增 lineage 证实存在未标记污染，可选：",
            "",
            "1. 标记为 degraded：统计层默认剔除，代价是需要新增持久质量标记或旁表，"
            "并重算历史信封；可追溯性最好。",
            "2. 保留并在信封披露：不改变历史数值，代价是所有消费方都必须识别披露，"
            "污染仍留在统计量中。",
            "3. 不动：零迁移成本，但已确认污染会持续影响 n 与分位；仅适用于影响可证明为零。",
            "",
            "本轮建议：选项3（不动数据），同时把 `round_id` 补入未来纪元写入的 lineage"
            " 作为独立后续任务。",
            "",
            "## 7. 只读保证",
            "",
            "脚本仅以 SQLite `mode=ro` + `PRAGMA query_only=ON` 读取两库；不导入采集器，"
            "不调用任何外部 API，不修改观测、价格或订阅数据。",
            "",
            "## 8. 待办登记：价格历史 `round_id` lineage",
            "",
            "- 状态：待办，本轮不实现。",
            "- 目标：为 `prices.db` 的纪元/价格历史写入补充 `round_id`，使未来能够精确回答"
            "‘某轮是否污染某序列’，不再依赖时间链候选。",
            "- 预计范围：`flight_details`、`roundtrip_price_history`、`push_snapshots` 的 schema、"
            "全部写入点、读取与信封 lineage；历史记录不得按时间猜测回填。",
            "- 常规触发：下次修改价格历史 schema 时顺带实施。",
            "- 提前触发：若再发生一次因缺少 `round_id` 而无法精确归因的事故，立即提前实施。",
            "- 推送前触发：在任何基于轮次/约束纪元的预测或自动建议进入用户推送之前，必须先实现 `round_id` lineage。",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="只读审计 PermissionError 污染轮")
    parser.add_argument("--observations-db", default=str(DEFAULT_OBSERVATIONS_DB))
    parser.add_argument("--prices-db", default=str(DEFAULT_PRICES_DB))
    parser.add_argument("--logs-dir", default=str(DEFAULT_LOGS_DIR))
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非 Markdown")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="要求10个轮次均在轮档命中且PermissionError事件数为71",
    )
    args = parser.parse_args(argv)
    audit = build_audit(
        observations_db=args.observations_db,
        prices_db=args.prices_db,
        logs_dir=args.logs_dir,
        round_ids=AFFECTED_ROUND_IDS,
    )
    if args.strict:
        scope = audit["scope"]
        if len(scope["failure_rounds_found"]) != len(AFFECTED_ROUND_IDS):
            raise SystemExit(
                f"轮次命中不完整: {len(scope['failure_rounds_found'])}/{len(AFFECTED_ROUND_IDS)}"
            )
        if scope["failure_event_count"] != 71:
            raise SystemExit(f"PermissionError事件数异常: {scope['failure_event_count']} != 71")
    if args.json:
        print(json.dumps(audit, ensure_ascii=False, indent=2))
    else:
        print(render_report(audit), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
