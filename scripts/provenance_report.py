"""只读输出航线统计值及其完整依据信封。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from provenance import (  # noqa: E402
    DEFAULT_DB_PATH,
    MIN_PAIRS_FOR_AGREEMENT,
    build_panel_report_payload,
    format_dual_source_agreement,
)


FAMILY_HEADINGS = {
    "reftier": "五档参考价",
    "calendar": "低价日历",
    "weekday": "周几统计",
    "price_signal": "价格信号历史比较",
    "tcurve": "T曲线",
}


def _value_text(value) -> str:
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _entry_line(entry: dict) -> str:
    window = entry.get("window") or [None, None]
    window_text = f"{window[0]}~{window[1]}"
    sources = "+".join(entry.get("sources") or []) or "未标明"
    agreement = format_dual_source_agreement(entry.get("dual_source_agreement"))
    return (
        f"{entry.get('stat_key')} | 值={_value_text(entry.get('value'))} | "
        f"版本={entry.get('method_version')} | n={entry.get('sample_n')} | "
        f"窗口={window_text} | 源={sources} | "
        f"剔除退化={entry.get('degraded_excluded')} | "
        f"桶={entry.get('bucket')} | 一致度={agreement}"
    )


def generate_report(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    route: str,
    airport_pair=None,
    as_of_date: date | None = None,
    window_days: int = 30,
    min_pairs: int = MIN_PAIRS_FOR_AGREEMENT,
    min_tcurve_sample: int = 5,
) -> str:
    payload = build_panel_report_payload(
        db_path,
        route=route,
        airport_pair=airport_pair,
        as_of_date=as_of_date,
        window_days=window_days,
        min_pairs=min_pairs,
        min_tcurve_sample=min_tcurve_sample,
    )
    if not payload:
        return f"航线 {route} 暂无可用观测数据。"

    provenance = payload.get("provenance") or {}
    statistics = provenance.get("statistics") or {}
    lines = [
        "统计依据信封报告",
        f"航线={route}",
        "口径=单人单程CNY含税",
        "双源相对差定义=abs(hasdata-juhe)/min(hasdata,juhe)",
    ]
    for family, heading in FAMILY_HEADINGS.items():
        lines.extend(["", f"【{heading}】"])
        if family == "tcurve":
            points = (payload.get("tcurve") or {}).get("points") or []
            if not points:
                lines.append("数据不足")
                continue
            for point in points:
                envelope = dict(point.get("provenance") or {})
                envelope["value"] = (
                    point.get("median")
                    if point.get("median") is not None
                    else f"样本不足(n={int(point.get('n') or 0)})"
                )
                lines.append(_entry_line(envelope))
            continue
        entries = [
            entry
            for key, entry in sorted(statistics.items())
            if key.startswith(f"{family}.")
        ]
        if not entries:
            lines.append("数据不足")
            continue
        lines.extend(_entry_line(entry) for entry in entries)

    agreement = payload.get("dual_source_agreement") or {}
    window = agreement.get("window") or [None, None]
    lines.extend(
        [
            "",
            "【双源一致度】",
            f"窗口={window[0]}~{window[1]}",
            f"配对数n={int(agreement.get('sample_n') or 0)}",
            f"结果={format_dual_source_agreement(agreement)}",
            f"方法版本={agreement.get('method_version')}",
        ]
    )
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="统计依据信封只读报告")
    parser.add_argument("--route", required=True, help="城市航线，例如 上海-大阪")
    parser.add_argument("--pair", help="可选机场对，例如 PVG-KIX")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="只读观测库路径")
    parser.add_argument("--window-days", type=int, default=30, help="一致度近窗口天数")
    parser.add_argument("--min-pairs", type=int, default=MIN_PAIRS_FOR_AGREEMENT)
    parser.add_argument("--min-tcurve-sample", type=int, default=5)
    args = parser.parse_args(argv)
    print(
        generate_report(
            db_path=args.db,
            route=args.route,
            airport_pair=args.pair,
            window_days=args.window_days,
            min_pairs=args.min_pairs,
            min_tcurve_sample=args.min_tcurve_sample,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
