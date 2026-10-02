"""Read-only, fixed-departure descriptions from an identified snapshot."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from observation_time import resolve_observed_day_shanghai
from readonly_snapshot import resolve_observations_db, resolve_snapshot_member
from tcurve import expected_search_sources, load_tcurve_daily_cells, readonly_connection


# Only this audited anchor day has an established missing-role expectation.
EXPECTED_ANCHOR_DAYS = frozenset({("2026-10-01", "2026-09-13")})


def _snapshot_db(db_path, expected_sha):
    candidate = Path(db_path)
    manifest = resolve_snapshot_member(candidate, "snapshot_manifest.json")
    if manifest is None:
        raise ValueError("snapshot_manifest.json 缺失；必须使用固化快照")
    supplied = str(expected_sha).lower()
    if len(supplied) != 64 or any(c not in "0123456789abcdef" for c in supplied):
        raise ValueError("manifest SHA-256 格式无效")
    manifest_bytes = manifest.read_bytes()
    actual = hashlib.sha256(manifest_bytes).hexdigest()
    if actual != supplied:
        raise ValueError("snapshot manifest SHA-256 不匹配")
    metadata = json.loads(manifest_bytes)
    hashes = metadata.get("snapshot_sha256") if isinstance(metadata, dict) else None
    expected_db = hashes.get("observations.sqlite3") if isinstance(hashes, dict) else None
    if (not isinstance(expected_db, str) or len(expected_db) != 64
            or any(c not in "0123456789abcdef" for c in expected_db.lower())):
        raise ValueError("snapshot database SHA-256 missing or invalid: observations.sqlite3")
    db = resolve_observations_db(candidate)
    database_sha = hashlib.sha256(db.read_bytes()).hexdigest()
    if database_sha != expected_db.lower():
        raise ValueError("snapshot database SHA-256 mismatch: observations.sqlite3")
    return db


def _load_evidence(db, departures):
    observation_columns = (
        "id", "observed_at", "observed_at_utc", "observed_day_shanghai",
        "legacy_time_ambiguous", "round_id", "route_type", "origin_airport",
        "dest_airport", "depart_date", "cabin_class", "source", "flight_combo", "price_cny",
    )
    ledger_columns = (
        "round_id", "request_fingerprint", "source", "route_type", "depart_date",
        "observed_day_shanghai", "execution_status", "sample_role", "raw_result_count",
        "valid_result_count",
    )
    placeholders = ",".join("?" for _ in departures)
    where = (" WHERE UPPER(origin_airport)='PVG' AND UPPER(dest_airport)='KIX' "
             "AND LOWER(cabin_class)='economy' AND depart_date IN (" + placeholders + ")")
    with readonly_connection(db) as connection:
        available = {row[1] for row in connection.execute("PRAGMA table_info(observations)")}
        columns = ",".join(name if name in available else "NULL AS " + name
                           for name in observation_columns)
        observations = [dict(row) for row in connection.execute("SELECT " + columns + " FROM observations" + where, departures)]
        has_ledger = connection.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='collection_cells'").fetchone()
        ledger = [dict(row) for row in connection.execute("SELECT " + ",".join(ledger_columns) + " FROM collection_cells" + where, departures)] if has_ledger else []
    groups = defaultdict(list)
    unresolved = []
    for row in observations:
        day, time_evidence = resolve_observed_day_shanghai(row)
        if day is None:
            unresolved.append({"id": row["id"], "reason": time_evidence})
            continue
        groups[(row["depart_date"], day)].append(row)
    ledger_groups = defaultdict(list)
    unassigned = 0
    for row in ledger:
        try:
            day = date.fromisoformat(str(row["observed_day_shanghai"])).isoformat()
        except ValueError:
            unassigned += 1
            continue
        ledger_groups[(row["depart_date"], day)].append(row)
    return groups, ledger_groups, ledger, unresolved, unassigned


def _positive_rows(rows):
    result = []
    for row in rows:
        try:
            value = float(row["price_cny"])
        except (ValueError, TypeError):
            continue
        if math.isfinite(value) and value > 0:
            result.append(row)
    return result


def _minimum_evidence(raw_rows, cell):
    priced = _positive_rows(raw_rows)
    if cell is None or not priced:
        return [], [], "证据不足"
    minimum = min(float(row["price_cny"]) for row in priced)
    if round(minimum, 2) != cell["min_price"]:
        return [], [], "证据不足：原始最低价与日格显示值不一致"
    minima = [row for row in priced if float(row["price_cny"]) == minimum]
    ids = [row["id"] for row in minima]
    if any(value is None for value in ids) or len(ids) != len(set(ids)):
        return [], [], "证据不足：原始行身份缺失或不唯一"
    if any(not row.get(key) for row in minima for key in ("source", "round_id", "flight_combo")):
        return [], ids, "证据不足：最低价来源身份缺项"
    tuples = sorted({(row["source"], row["round_id"], row["flight_combo"]) for row in minima})
    return tuples, ids, "原始报价精确并列；显示值仅用于核对"


def _build_row(dep, day, cell, raw_rows, ledger_rows):
    route_types = sorted({str(row.get("route_type") or "") for row in raw_rows + ledger_rows})
    unit = ("PVG", "KIX", dep, day, "economy", route_types[0]) if len(route_types) == 1 and route_types[0] else None
    strict = cell is not None and (cell["collection_state"] == "valid" or (cell["collection_state"] == "legacy" and not cell["degraded"]))
    strict_price = cell["min_price"] if strict else None
    tuples, ids, provenance = _minimum_evidence(raw_rows, cell)
    supplementary_price = cell["min_price"] if cell and not strict and tuples else None
    expected = cell["expected_sources"] if cell else sorted(set().union(*(expected_search_sources(kind, day) for kind in route_types)))
    priced_sources = sorted({row["source"] for row in _positive_rows(raw_rows) if row["source"]})
    roles = sorted(set((cell or {}).get("sample_roles", [])) | {row["sample_role"] for row in ledger_rows if row["sample_role"]})
    successes = sorted({(row["source"], row["round_id"]) for row in ledger_rows if row["execution_status"] == "success"})
    gaps = []
    if cell is None:
        gaps.append("无价格")
    if not strict:
        gaps.append("质量排除")
    if any(row["execution_status"] in {"planned", "running"} for row in ledger_rows):
        gaps.append("终态未闭合")
    if (dep, day) in EXPECTED_ANCHOR_DAYS and "trajectory_anchor" not in roles:
        gaps.append("角色缺项")
    if unit is None:
        gaps.append("route_type不一致" if len(route_types) > 1 else "字段缺失")
    if cell and not tuples:
        gaps.append("证据不足")
    return {
        "depart_date": dep, "observed_day": day,
        "t": (date.fromisoformat(dep) - date.fromisoformat(day)).days,
        "unit": unit, "route_types": route_types,
        "strict_price": strict_price, "supplementary_price": supplementary_price,
        "collection_state": cell["collection_state"] if cell else "无有价日格",
        "degraded": cell["degraded"] if cell else None,
        "expected_sources": expected, "priced_sources": priced_sources,
        "successes": successes, "success_evidence": "原始execution_status=success" if ledger_rows else "未记录",
        "minimum_tuples": tuples, "minimum_ids": ids, "minimum_evidence": provenance,
        "roles": roles, "gaps": gaps, "switches": [],
        "execution_statuses": dict(sorted(Counter(row["execution_status"] for row in ledger_rows).items())),
        "round_ids": sorted({row["round_id"] for row in raw_rows + ledger_rows if row["round_id"]}),
        "has_ledger": bool(ledger_rows),
    }


def _trajectory_rows(dep, cells, observations, ledger):
    days = sorted({day for d, day in cells if d == dep} | {day for d, day in observations if d == dep} | {day for d, day in ledger if d == dep})
    rows = []
    previous = None
    for day in days:
        key = (dep, day)
        row = _build_row(dep, day, cells.get(key), observations.get(key, []), ledger.get(key, []))
        if row["minimum_tuples"]:
            if previous is not None and previous["minimum_tuples"]:
                if {t[0] for t in row["minimum_tuples"]} != {t[0] for t in previous["minimum_tuples"]}:
                    row["switches"].append("来源切换")
                if {t[2] for t in row["minimum_tuples"]} != {t[2] for t in previous["minimum_tuples"]}:
                    row["switches"].append("组合切换")
        previous = row
        rows.append(row)
    return rows


def _counts_by_t(rows):
    grouped = defaultdict(list)
    for row in rows:
        if row["unit"] and (row["strict_price"] is not None or row["supplementary_price"] is not None):
            grouped[row["t"]].append(row)
    result = {}
    for t, items in sorted(grouped.items()):
        n = len({row["depart_date"] for row in items})
        result[t] = n
    return result


def _ledger_counts(rows):
    keys = {(row["round_id"], row["request_fingerprint"]) for row in rows if row["round_id"] and row["request_fingerprint"]}
    returned = {}
    for field in ("raw_result_count", "valid_result_count"):
        recorded = [int(row[field]) for row in rows if row[field] is not None]
        returned[field] = {"recorded_sum": sum(recorded) if recorded else None, "unrecorded_rows": len(rows) - len(recorded)}
    return {"request_keys": len(keys), "missing_key_rows": sum(not r["round_id"] or not r["request_fingerprint"] for r in rows),
            "statuses": dict(sorted(Counter(row["execution_status"] for row in rows).items())),
            "actual_sent": None, "retries": None, "billed": None, "returned": returned}


def _comparison_lines(trajectories):
    lines = ["轨迹并列描述"]
    for trajectory in trajectories:
        rows = trajectory["rows"]
        days = [row["observed_day"] for row in rows]
        lines.append(f"{trajectory['depart_date']}：观测记录范围 {days[0] if days else '未记录'} 至 {days[-1] if days else '未记录'}；严格列 {sum(r['strict_price'] is not None for r in rows)} 格；补充列 {sum(r['supplementary_price'] is not None for r in rows)} 格。")
    lines.append("只描述上述独立轨迹的覆盖与记录差异，不作因果解释；不同报价不保证对应同一产品。")
    return lines


def _text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _price(value):
    return "" if value is None else f"{value:.2f}"


def _render(data):
    lines = ["固定出发日单轨迹描述：上海→大阪 / PVG→KIX / 经济舱",
             "价格口径：单人单程CNY含税；严格/补充分列，不改历史质量状态。",
             "日格来自现行加载器；六维为出发机场、到达机场、出发日、上海观测日、舱位、route_type。",
             "缺省不生成不存在的日格；只列有报价归属或台账日期的日，不插值。切换相对上一观测日，任一侧归属不足则不判切换。",
             "角色缺项仅据已核实的10-01出发、09-13观测(T=18)锚点职责；不追判启用前legacy。"]
    for trajectory in data["trajectories"]:
        dep = trajectory["depart_date"]
        lines += ["", f"出发日 {dep}", "观测日 | T | 严格列价格 | 补充列价格 | 原日格状态 | 当时期望源 | 实际有价来源 | 台账确认成功来源/轮次 | 最低价(source,round_id,flight_combo)与id | 角色集合 | 切换标记 | 缺口标记"]
        for row in trajectory["rows"]:
            success = _text(row["successes"]) if row["has_ledger"] else "未记录"
            minimum = _text(row["minimum_tuples"]) + " id=" + _text(row["minimum_ids"]) if row["minimum_tuples"] else row["minimum_evidence"]
            gaps = list(row["gaps"])
            if row["supplementary_price"] is not None:
                coverage = "当时期望双源、实际单源；" if len(row["expected_sources"]) == 2 and len(row["priced_sources"]) == 1 else ""
                gaps.append(coverage + "当时期望" + "+".join(row["expected_sources"]) + "、实际" + "+".join(row["priced_sources"]) + "；补充展示，不追认为valid")
            lines.append(" | ".join([row["observed_day"], str(row["t"]), _price(row["strict_price"]), _price(row["supplementary_price"]), row["collection_state"], _text(row["expected_sources"]), _text(row["priced_sources"]), success, minimum, _text(row["roles"]), "、".join(row["switches"]), "、".join(gaps)]))
        lines.append("本轨迹日格清点(现行加载器)：" + _text(trajectory["counts"]))
    counts = data["ledger_counts"]
    all_rows = [row for t in data["trajectories"] for row in t["rows"]]
    lines += ["", f"台账请求键数：{counts['request_keys']}；身份缺项行={counts['missing_key_rows']}（来源：原始collection_cells，round_id+request_fingerprint去重）",
              "各执行状态记录数：" + _text(counts["statuses"]) + "（来源：原始execution_status，不按日格valid反推）",
              "实际发送/重试/计费次数：未记录/不能确定（来源：本报告读取的快照字段无明确事件计数证据）",
              "返回条数：" + _text(counts["returned"]) + "（来源：raw_result_count/valid_result_count；已记录值相加，缺失不填零，不等于请求数）",
              f"六维日格数：{sum(r['unit'] is not None for r in all_rows)}；字段缺失/route_type不一致未定维度格={sum(r['unit'] is None for r in all_rows)}；轨迹数={len(data['trajectories'])}",
              "每个T的n：" + _text(data["n_by_t"]) + "（有显示价格且六维身份完整的出发日数，含严格或补充，每个出发日每T最多一次）",
              f"时间归属证据不足原始行={len(data['unresolved_time_rows'])}；台账观测日不可归属行={data['unassigned_ledger_rows']}",
              "", *_comparison_lines(data["trajectories"])]
    return "\n".join(lines)


def generate_report(*, db_path, expect_manifest_sha, route, airport_pair, departures):
    if route != "上海-大阪" or airport_pair != "PVG-KIX":
        raise ValueError("本报告限定上海-大阪、PVG-KIX、经济舱")
    departures = sorted(set(departures))
    if not 1 <= len(departures) <= 2:
        raise ValueError("须指定一至两个独立出发日")
    for value in departures:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("出发日须为YYYY-MM-DD")
    db = _snapshot_db(db_path, expect_manifest_sha)
    cells = {(c["depart_date"], c["observed_day"]): c for c in load_tcurve_daily_cells(db, route=route, airport_pair=airport_pair) if c["depart_date"] in departures}
    observations, ledger, ledger_rows, unresolved, unassigned = _load_evidence(db, departures)
    trajectories = []
    for dep in departures:
        selected = [c for (d, _), c in cells.items() if d == dep]
        rows = _trajectory_rows(dep, cells, observations, ledger)
        counts = {"有价日格": len(selected), "严格": sum(not c["degraded"] for c in selected), "退化": sum(c["degraded"] for c in selected),
                  "legacy": sum(c["collection_state"] == "legacy" for c in selected), "有价无台账": sum(not ledger.get((dep, c["observed_day"])) for c in selected),
                  "台账valid有价日格": sum(c["collection_state"] == "valid" for c in selected), "角色": dict(sorted(Counter(r for c in selected for r in c["sample_roles"]).items()))}
        trajectories.append({"depart_date": dep, "rows": rows, "counts": counts})
    data = {"trajectories": trajectories, "n_by_t": _counts_by_t([r for t in trajectories for r in t["rows"]]),
            "ledger_counts": _ledger_counts(ledger_rows), "unresolved_time_rows": unresolved, "unassigned_ledger_rows": unassigned}
    return _render(data), data


def main(argv=None):
    parser = argparse.ArgumentParser(description="固定出发日只读描述性报告")
    parser.add_argument("--route", required=True)
    parser.add_argument("--pair", required=True)
    parser.add_argument("--depart", required=True, action="append")
    parser.add_argument("--db", required=True)
    parser.add_argument("--expect-manifest-sha", required=True)
    args = parser.parse_args(argv)
    try:
        text, _ = generate_report(db_path=args.db, expect_manifest_sha=args.expect_manifest_sha,
                                  route=args.route, airport_pair=args.pair, departures=args.depart)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"轨迹报告失败：{exc}")
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
