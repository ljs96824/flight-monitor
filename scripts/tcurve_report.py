"""只读输出同航线提前购买曲线。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from readonly_snapshot import resolve_observations_db, resolve_snapshot_member
from tcurve import DEFAULT_DB_PATH, MIN_SAMPLE_FOR_TCURVE, build_tcurve


def _load_default_quality_cells(db_path):
    """仅对真实面板叠加现有 PermissionError 审计，不改面板数据。"""
    supplied = Path(db_path)
    if supplied.is_dir():
        required = [
            supplied / "observations.sqlite3",
            supplied / "prices.db",
            supplied / "api_usage.json",
            supplied / "snapshot_manifest.json",
        ]
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise FileNotFoundError("只读快照缺少文件: " + ", ".join(missing))
        manifest_path = resolve_snapshot_member(
            supplied,
            "snapshot_manifest.json",
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        quality_cells = manifest.get("permission_quality_cells")
        if not isinstance(quality_cells, list):
            raise ValueError("只读快照未冻结permission_quality_cells")
        return quality_cells

    try:
        observations_db = resolve_observations_db(supplied)
        if observations_db.resolve() != Path(DEFAULT_DB_PATH).resolve():
            return []
        from scripts.audit_permission_pollution import (
            AFFECTED_ROUND_IDS,
            DEFAULT_LOGS_DIR,
            DEFAULT_PRICES_DB,
            build_audit,
        )

        audit = build_audit(
            observations_db=observations_db,
            prices_db=DEFAULT_PRICES_DB,
            logs_dir=DEFAULT_LOGS_DIR,
            round_ids=AFFECTED_ROUND_IDS,
        )
        return list(audit.get("affected_cells") or [])
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        return []


def _quality_key(item):
    return (
        str(item.get("origin_city") or ""),
        str(item.get("dest_city") or ""),
        str(item.get("depart_date") or ""),
        str(item.get("observed_day") or ""),
        int(item.get("t", item.get("days_to_departure", 0)) or 0),
    )


def _quality_line(item):
    origin, destination, depart, observed, t_value = _quality_key(item)
    return (
        f"{observed} {origin}→{destination} depart={depart} T={t_value} "
        f"覆盖={','.join(item.get('source_coverage') or []) or '-'} "
        f"期望={','.join(item.get('expected_sources') or []) or '-'}"
    )


def _price(value) -> str:
    if value is None:
        return "-"
    return f"CNY{float(value):,.2f}".replace(".00", "")


def generate_report(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    route: str,
    airport_pair=None,
    include_degraded: bool = False,
    min_sample: int = MIN_SAMPLE_FOR_TCURVE,
    quality_cells=None,
) -> str:
    supplied_db_path = db_path
    db_path = resolve_observations_db(db_path)
    curve = build_tcurve(
        db_path,
        route=route,
        airport_pair=airport_pair,
        include_degraded=include_degraded,
        min_sample=min_sample,
    )
    route_text = f"{curve['origin_city']}→{curve['dest_city']}"
    if quality_cells is None:
        quality_cells = _load_default_quality_cells(supplied_db_path)
    route_city_set = {curve["origin_city"], curve["dest_city"]}
    relevant_quality = [
        item
        for item in quality_cells or []
        if {str(item.get("origin_city") or ""), str(item.get("dest_city") or "")}
        == route_city_set
    ]
    missing_cells = [
        item for item in relevant_quality if int(item.get("all_day_row_count") or 0) == 0
    ]
    degraded_cells = [
        item for item in curve.get("daily_cells") or [] if item.get("degraded")
    ]
    degraded_cells.extend(
        item for item in relevant_quality if item.get("degraded") is True
    )
    degraded_by_key = {_quality_key(item): item for item in degraded_cells}
    lines = [
        f"提前购买曲线: {route_text}",
        f"口径: {curve['price_caliber']}",
        (
            f"方法: {curve['method_version']}；每个日格采用跨源最低价"
            "(global_min市场最低参考价·与用户筛选无关)"
        ),
    ]
    if curve.get("airport_pair"):
        lines.append(f"机场对细分: {curve['airport_pair']}")
    if not curve["daily_cell_count"]:
        lines.append("无数据: 该航线暂无有效观测日格。")
        return "\n".join(lines)

    coverage = curve.get("coverage") or {}
    lines.extend(
        [
            "参与出发日: " + (", ".join(curve.get("included_depart_dates") or []) or "无"),
            (
                f"源退化日格: {curve['degraded_count']}；"
                + (
                    "本次已包含，并在解读时保留源覆盖不完整标记。"
                    if include_degraded
                    else f"默认剔除{curve['degraded_excluded_count']}个。"
                )
            ),
            (
                "观测时间归属: "
                f"ambiguous剔除={curve.get('ambiguous_excluded_count', 0)}；"
                f"legacy fallback行={curve.get('legacy_fallback_row_count', 0)}。"
            ),
            "样本角色构成: "
            + (
                " / ".join(
                    f"{role}={count}"
                    for role, count in sorted(
                        (curve.get("sample_role_counts") or {}).items()
                    )
                )
                or "无"
            ),
            "采集日格状态: "
            + (
                " / ".join(
                    f"{state}={count}"
                    for state, count in sorted(
                        (curve.get("collection_state_counts") or {}).items()
                    )
                )
                or "无"
            ),
            f"覆盖范围: T={coverage.get('t_min')} 至 T={coverage.get('t_max')} 天；禁止外推范围外数据。",
            "",
            "缺失格清单:",
            *(
                [f"- {_quality_line(item)}" for item in sorted(missing_cells, key=_quality_key)]
                if missing_cells
                else ["- 无"]
            ),
            "缺失不参与趋势判断。",
            "degraded格清单:",
            *(
                [
                    f"- {_quality_line(item)}"
                    for item in sorted(degraded_by_key.values(), key=_quality_key)
                ]
                if degraded_by_key
                else ["- 无"]
            ),
            "",
            "T(天) | n | 中位数 | IQR(P25-P75) | 状态",
        ]
    )
    for point in curve.get("points") or []:
        if point.get("sufficient"):
            median = _price(point.get("median"))
            iqr = f"{_price(point.get('p25'))}-{_price(point.get('p75'))}"
        else:
            median = "-"
            iqr = "-"
        lines.append(
            f"{point.get('t')} | {point.get('n')} | {median} | {iqr} | {point.get('status')}"
        )
    if curve.get("lowest_median_t_ranges"):
        lines.extend(
            [
                "",
                "观测覆盖范围内中位最低的T区段: "
                + "、".join(curve["lowest_median_t_ranges"])
                + f"，中位数{_price(curve.get('lowest_median'))}。",
                "该描述仅对应现有观测覆盖，不代表未观测区间，也不构成购买时点建议。",
            ]
        )
    else:
        lines.extend(["", f"合格T格不足：每格需n≥{curve['min_sample']}，暂不输出位置描述。"])
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="输出只读提前购买曲线报告")
    parser.add_argument("--route", required=True, help="城市航线，如 上海-大阪")
    parser.add_argument("--pair", help="可选机场对细分，如 PVG-KIX")
    parser.add_argument("--include-degraded", action="store_true", help="包含源覆盖不完整日格")
    parser.add_argument("--min-n", type=int, default=MIN_SAMPLE_FOR_TCURVE, help="T格最小样本数")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="观测库路径或只读快照目录")
    args = parser.parse_args(argv)
    try:
        report = generate_report(
            db_path=args.db,
            route=args.route,
            airport_pair=args.pair,
            include_degraded=args.include_degraded,
            min_sample=args.min_n,
        )
    except (FileNotFoundError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"T曲线报告失败: {exc}")
        return 1
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
