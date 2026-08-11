"""只读输出航线价格预测、回测成绩与航班规律。"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forecast import build_shape, estimate_level, predict_price, walk_forward_backtest, write_backtest_report
from holidays import holiday_labels_for_route
from patterns import build_route_patterns
from provenance import load_route_observations
from tcurve import DEFAULT_DB_PATH, load_tcurve_daily_cells


def _fmt(value):
    return "-" if value is None else f"{float(value):,.2f}"


def generate_report(*, db_path=DEFAULT_DB_PATH, route, airport_pair=None, as_of_day=None, write_backtest=None):
    cells = load_tcurve_daily_cells(db_path, route=route, airport_pair=airport_pair)
    included = [item for item in cells if not item.get("degraded")]
    degraded = len(cells) - len(included)
    if not included:
        return f"航线 {route} 无可用非退化观测数据。\n退化日剔除={degraded}", {"route": route, "status": "无数据", "degraded_excluded": degraded}
    as_of = str(as_of_day or max(item["observed_day"] for item in included))
    shape = build_shape(included, cutoff_day=as_of)
    backtest = walk_forward_backtest(included)
    if write_backtest:
        write_backtest_report(backtest, write_backtest)
    depart_dates = sorted({item["depart_date"] for item in included})
    rows = load_route_observations(db_path, route=route, airport_pair=airport_pair)
    patterns = build_route_patterns(db_path, route=route, airport_pair=airport_pair)
    lines = [f"# 价格预测报告: {route}", "", "口径: 市场最低参考价·单人单程CNY·与用户筛选无关", f"观测截止: {as_of}", f"退化日剔除: {degraded}", "", "## shape(T)", "T | n | 中位 | P25-P75 | P10-P90"]
    for t, point in sorted(shape.items()):
        lines.append(f"{t} | {point['n']} | {_fmt(point['median'])} | {_fmt(point['p25'])}-{_fmt(point['p75'])} | {_fmt(point['p10'])}-{_fmt(point['p90'])}")
    lines.extend(["", "## level 与未来7天预测"])
    forecasts = {}
    route_codes = None
    if rows:
        route_codes = (rows[0].get("origin_airport"), rows[0].get("dest_airport"))
    for depart in depart_dates:
        level = estimate_level(included, shape, depart_date=depart, cutoff_day=as_of)
        labels = holiday_labels_for_route(*route_codes, date.fromisoformat(depart)) if route_codes and all(route_codes) else []
        lines.append(f"{depart}: level={_fmt(level.get('value'))}× n={level['n']} 可靠={level['reliable']}" + (f" 节假日={';'.join(labels)}" if labels else ""))
        depart_forecasts = []
        for offset in range(1, 8):
            target_day = date.fromisoformat(as_of) + timedelta(days=offset)
            target_t = (date.fromisoformat(depart) - target_day).days
            prediction = predict_price(level, shape, target_t=target_t)
            if prediction.get("status") == "ok":
                depart_forecasts.append({"target_day": target_day.isoformat(), **prediction})
                lines.append(f"  {target_day.isoformat()} T={target_t}: 中位CNY{_fmt(prediction['median'])} IQR CNY{_fmt(prediction['p25'])}-{_fmt(prediction['p75'])} P10-P90 CNY{_fmt(prediction['p10'])}-{_fmt(prediction['p90'])}")
        if not depart_forecasts:
            lines.append("  未来7天无精确shape T可用，未插值。")
        forecasts[depart] = {"level": level, "holiday_labels": labels, "predictions": depart_forecasts}
    lines.extend(["", "## 累计走前回测"])
    for horizon in ("1", "3", "7"):
        item = backtest["horizons"][horizon]
        lines.append(f"k={horizon}: n={item['n']} 模型MAPE={_fmt(item['model']['mape'])}% 朴素={_fmt(item['naive']['mape'])}% T曲线={_fmt(item['tcurve']['mape'])}% 技能门={'通过' if item['skill_gate']['passed'] else '未过'}")
    lines.extend(["", "## 航班规律", f"组合出现率: {len(patterns['combo_occurrence'])} 个组合"])
    for item in patterns["combo_occurrence"][:10]:
        lines.append(f"- {item['combo']}: {item['label']}")
    supply = patterns["supply_mix"]
    lines.append(f"直飞/中转供给: {supply['direct']}/{supply['transfer']} (n={supply['n']}, {supply['basis']})")
    lines.append(f"起飞时段稳定性: {patterns['departure_period']['status']}，{patterns['departure_period']['reason']}")
    return "\n".join(lines), {"route": route, "as_of_day": as_of, "shape": shape, "forecasts": forecasts, "backtest": backtest, "patterns": patterns, "degraded_excluded": degraded}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--route", required=True)
    parser.add_argument("--pair")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--write-backtest")
    args = parser.parse_args()
    text, _ = generate_report(db_path=args.db, route=args.route, airport_pair=args.pair, write_backtest=args.write_backtest)
    print(text)


if __name__ == "__main__":
    main()
