"""只读输出航线价格预测、回测成绩与航班规律。"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forecast import (
    REGIME_ORDER,
    assess_overall_reliability,
    build_shapes_by_regime,
    classify_regime,
    estimate_level,
    predict_price,
    walk_forward_backtest,
    write_backtest_report,
)
from holidays import holiday_labels_for_route
from patterns import build_route_patterns
from provenance import load_route_observations
from tcurve import DEFAULT_DB_PATH, load_tcurve_daily_cells


MARKET_CALIBER = "市场最低参考价·单人单程CNY·与用户筛选无关"


def _fmt(value):
    return "-" if value is None else f"{float(value):,.2f}"


def _route_codes(rows):
    for row in rows or []:
        origin = str(row.get("origin_airport") or "").strip().upper()
        destination = str(row.get("dest_airport") or "").strip().upper()
        if origin and destination:
            return origin, destination
    return None


def _regime_map(depart_dates, route_codes):
    result = {}
    for depart in depart_dates:
        labels = (
            holiday_labels_for_route(*route_codes, date.fromisoformat(depart))
            if route_codes
            else []
        )
        result[depart] = classify_regime(depart, labels)
    return result


def _shape_row(t_value, point, *, diagnostic):
    if point.get("sufficient"):
        return (
            f"{t_value} | {point['n']} | {_fmt(point['median'])} | "
            f"{_fmt(point['p25'])}-{_fmt(point['p75'])} | "
            f"{_fmt(point['p10'])}-{_fmt(point['p90'])}"
        )
    status = f"样本不足(n={point.get('n', 0)})"
    if not diagnostic:
        return f"{t_value} | {point.get('n', 0)} | {status} | {status} | {status}"
    raw = point.get("raw") or {}
    return (
        f"{t_value} | {point.get('n', 0)} | {_fmt(raw.get('median'))} | "
        f"{_fmt(raw.get('p25'))}-{_fmt(raw.get('p75'))} | "
        f"{_fmt(raw.get('p10'))}-{_fmt(raw.get('p90'))} | "
        "原始值,不可用于判断"
    )


def _source_coverage(cells, depart_date, as_of):
    relevant = [
        item
        for item in cells
        if str(item.get("depart_date")) == str(depart_date)
        and str(item.get("observed_day")) <= str(as_of)
    ]
    return bool(relevant) and all(not item.get("degraded") for item in relevant)


def _regime_departure_n(cells, regime_by_depart_date, regime, as_of):
    return len(
        {
            str(item["depart_date"])
            for item in cells
            if not item.get("degraded")
            and str(item.get("observed_day")) <= str(as_of)
            and regime_by_depart_date.get(str(item["depart_date"])) == regime
        }
    )


def _future_shape_points(shape, *, depart_date, as_of):
    points = []
    targets = []
    for offset in range(1, 8):
        target_day = date.fromisoformat(as_of) + timedelta(days=offset)
        target_t = (date.fromisoformat(depart_date) - target_day).days
        targets.append((target_day, target_t))
        points.append(shape.get(target_t) or {"n": 0, "sufficient": False})
    return targets, points


def _unmet_items(reliability, *, regime):
    components = reliability["components"]
    items = []
    if not components["level_reliability"]["passed"]:
        items.append(components["level_reliability"]["detail"].replace("level(", "level不可靠("))
    if not components["shape_reliability"]["passed"]:
        items.append(f"shape不足({components['shape_reliability']['detail']})")
    if not components["backtest_skill"]["passed"]:
        items.append("技能门未过")
    if not components["source_coverage"]["passed"]:
        items.append("源覆盖不完整")
    if not components["regime_match"]["passed"]:
        regime_n = components["regime_match"]["detail"].split("=")[-1].rstrip(")")
        items.append(f"同类日型样本不足(regime={regime},n={regime_n})")
    return items


def generate_report(
    *,
    db_path=DEFAULT_DB_PATH,
    route,
    airport_pair=None,
    as_of_day=None,
    write_backtest=None,
    diagnostic=False,
):
    cells = load_tcurve_daily_cells(db_path, route=route, airport_pair=airport_pair)
    included = [item for item in cells if not item.get("degraded")]
    degraded = len(cells) - len(included)
    if not included:
        text = "\n".join(
            [
                f"# 价格预测报告: {route}",
                "",
                "本报告为内部诊断输出;技能门=未过;预测未进入用户推送",
                "",
                "无可用非退化观测数据。",
                f"退化日剔除={degraded}",
            ]
        )
        return text, {
            "route": route,
            "status": "无数据",
            "degraded_excluded": degraded,
        }

    as_of = str(as_of_day or max(item["observed_day"] for item in included))
    depart_dates = sorted({str(item["depart_date"]) for item in included})
    rows = load_route_observations(db_path, route=route, airport_pair=airport_pair)
    route_codes = _route_codes(rows)
    regime_by_depart_date = _regime_map(depart_dates, route_codes)
    shapes = build_shapes_by_regime(
        included,
        regime_by_depart_date,
        cutoff_day=as_of,
    )
    backtest = walk_forward_backtest(
        included,
        regime_by_depart_date=regime_by_depart_date,
    )
    if write_backtest:
        write_backtest_report(backtest, write_backtest)
    gate = backtest["horizons"].get("3", {}).get("skill_gate") or {"passed": False}
    patterns = build_route_patterns(
        db_path,
        route=route,
        airport_pair=airport_pair,
    )

    forecasts = {}
    any_prediction_entered = False
    for depart in depart_dates:
        regime = regime_by_depart_date[depart]
        shape = shapes.get(regime) or {}
        level = estimate_level(
            included,
            shape,
            depart_date=depart,
            cutoff_day=as_of,
        )
        targets, shape_points = _future_shape_points(
            shape,
            depart_date=depart,
            as_of=as_of,
        )
        reliability = assess_overall_reliability(
            level=level,
            shape_points=shape_points,
            backtest_gate=gate,
            source_coverage=_source_coverage(cells, depart, as_of),
            regime_sample_n=_regime_departure_n(
                included,
                regime_by_depart_date,
                regime,
                as_of,
            ),
        )
        labels = (
            holiday_labels_for_route(*route_codes, date.fromisoformat(depart))
            if route_codes
            else []
        )
        predictions = []
        if reliability["passed"]:
            for target_day, target_t in targets:
                prediction = predict_price(level, shape, target_t=target_t)
                if prediction.get("status") == "ok":
                    predictions.append(
                        {"target_day": target_day.isoformat(), **prediction}
                    )
            any_prediction_entered = bool(predictions) or any_prediction_entered
        forecasts[depart] = {
            "regime": regime,
            "level": level,
            "holiday_labels": labels,
            "predictions": predictions,
            "overall_reliability": reliability,
            "unmet_items": _unmet_items(reliability, regime=regime),
        }

    skill_text = "已过" if gate.get("passed") else "未过"
    push_text = "已进入" if any_prediction_entered else "未进入"
    lines = [
        f"# 价格预测报告: {route}",
        "",
        f"本报告为内部诊断输出;技能门={skill_text};预测{push_text}用户推送",
        "",
        f"口径: {MARKET_CALIBER}",
        f"观测截止: {as_of}",
        f"退化日剔除: {degraded}",
    ]

    for regime in REGIME_ORDER:
        shape = shapes.get(regime)
        if not shape:
            continue
        lines.extend(
            [
                "",
                f"## shape(T) · regime={regime}",
                "shape口径: global_min市场最低参考价·与用户筛选无关",
                "T | n | 中位 | P25-P75 | P10-P90"
                + (" | 诊断标记" if diagnostic else ""),
            ]
        )
        for t_value, point in sorted(shape.items()):
            lines.append(_shape_row(t_value, point, diagnostic=diagnostic))

    lines.extend(["", "## level 与未来7天预测"])
    for depart in depart_dates:
        item = forecasts[depart]
        level = item["level"]
        labels = item["holiday_labels"]
        reliability = item["overall_reliability"]
        lines.append(
            f"{depart}: regime={item['regime']} 价格基准 level=CNY{_fmt(level.get('value'))}"
            "(公式:预测=level×shape(T)) "
            f"n={level['n']} 可靠={level['reliable']}"
            + (f" 节假日={';'.join(labels)}" if labels else "")
            + " 口径=市场最低参考价·与用户筛选无关"
        )
        component_text = ";".join(
            f"{name}:{component['value']}"
            for name, component in reliability["components"].items()
        )
        bottleneck = ",".join(reliability["bottleneck_details"])
        lines.append(
            f"  overall_reliability={reliability['value']} 分量={component_text} "
            f"瓶颈={bottleneck}"
        )
        if not reliability["passed"]:
            lines.append(
                "  暂不提供预测;未达项="
                + ";".join(item["unmet_items"])
            )
            continue
        for prediction in item["predictions"]:
            lines.append(
                f"  {prediction['target_day']} T={prediction['t']}: "
                f"中位CNY{_fmt(prediction['median'])} "
                f"IQR CNY{_fmt(prediction['p25'])}-{_fmt(prediction['p75'])} "
                f"P10-P90 CNY{_fmt(prediction['p10'])}-{_fmt(prediction['p90'])} "
                "(市场最低参考价·与用户筛选无关)"
            )

    lines.extend(["", "## 累计走前回测"])
    for horizon in ("1", "3", "7"):
        item = backtest["horizons"][horizon]
        lines.append(
            f"k={horizon}: n={item['n']} 模型MAPE={_fmt(item['model']['mape'])}% "
            f"朴素={_fmt(item['naive']['mape'])}% "
            f"T曲线={_fmt(item['tcurve']['mape'])}% "
            f"技能门={'通过' if item['skill_gate']['passed'] else '未过'}"
        )

    lines.extend(
        [
            "",
            "## 航班规律",
            f"组合出现率: {len(patterns['combo_occurrence'])} 个组合",
        ]
    )
    for item in patterns["combo_occurrence"][:10]:
        lines.append(f"- {item['combo']}: {item['label']}")
    supply = patterns["supply_mix"]
    lines.append(
        f"候选组合结构:直飞组合{int(supply['direct']):,} / "
        f"中转组合{int(supply['transfer']):,}(共{int(supply['n']):,})"
    )
    lines.append(
        "注:反映搜索结果组合结构,不代表座位库存或真实运力;"
        "中转组合存在拼接膨胀"
    )
    lines.append(
        f"起飞时段稳定性: {patterns['departure_period']['status']}，"
        f"{patterns['departure_period']['reason']}"
    )
    return "\n".join(lines), {
        "route": route,
        "as_of_day": as_of,
        "shape": shapes,
        "regime_by_depart_date": regime_by_depart_date,
        "forecasts": forecasts,
        "backtest": backtest,
        "patterns": patterns,
        "degraded_excluded": degraded,
        "diagnostic": bool(diagnostic),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--route", required=True)
    parser.add_argument("--pair")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--write-backtest")
    parser.add_argument(
        "--diagnostic",
        action="store_true",
        help="显示低样本原始统计，并明确标注不可用于判断",
    )
    args = parser.parse_args()
    text, _ = generate_report(
        db_path=args.db,
        route=args.route,
        airport_pair=args.pair,
        write_backtest=args.write_backtest,
        diagnostic=args.diagnostic,
    )
    print(text)


if __name__ == "__main__":
    main()
