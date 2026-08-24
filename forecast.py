"""基于 P4 日格的可解释分解预测与累计走前回测。"""

from __future__ import annotations

import json
import os
import statistics
from datetime import date, timedelta
from pathlib import Path

from method_registry import method_version
from provenance import build_envelope
from tcurve import DEFAULT_DB_PATH, MIN_SAMPLE_FOR_TCURVE, _clean_number, load_tcurve_daily_cells, percentile_linear, route_cities_from_info


METHOD_VERSION = method_version("forecast")


def _positive_int_env(name, default):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _positive_float_env(name, default):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


MIN_OBS_FOR_LEVEL = _positive_int_env("MIN_OBS_FOR_LEVEL", 4)
MIN_BACKTEST_CASES = _positive_int_env("MIN_BACKTEST_CASES", 5)
SKILL_GATE_IMPROVEMENT = _positive_float_env("SKILL_GATE_IMPROVEMENT", 0.10)

# 与 T 曲线共用同一个证据门，避免诊断层出现两套“够样本”定义。
MIN_SHAPE_N = MIN_SAMPLE_FOR_TCURVE
REGIME_ORDER = ("normal", "weekend", "holiday_eve", "holiday", "holiday_return")


def _usable(cells, cutoff_day=None):
    return [
        item for item in cells
        if not item.get("degraded")
        and (not cutoff_day or str(item.get("observed_day")) <= str(cutoff_day))
        and float(item.get("min_price") or 0) > 0
    ]


def _summary(values):
    if not values:
        return None
    return {
        "n": len(values),
        "median": _clean_number(statistics.median(values), 8),
        "p10": _clean_number(percentile_linear(values, 0.10), 8),
        "p25": _clean_number(percentile_linear(values, 0.25), 8),
        "p75": _clean_number(percentile_linear(values, 0.75), 8),
        "p90": _clean_number(percentile_linear(values, 0.90), 8),
    }


def _gated_summary(values, *, min_shape_n=MIN_SHAPE_N):
    raw = _summary(values)
    if raw is None:
        return None
    sufficient = int(raw["n"]) >= int(min_shape_n)
    point = {
        "n": raw["n"],
        "sufficient": sufficient,
        "status": "ok" if sufficient else f"样本不足(n={raw['n']})",
        "raw": raw,
    }
    for key in ("median", "p10", "p25", "p75", "p90"):
        point[key] = raw[key] if sufficient else None
    return point


def classify_regime(target_date, holiday_labels):
    """按既有假日标签与星期派生互斥日型，不跨日型借样本。"""
    target = date.fromisoformat(str(target_date))
    labels = [str(item) for item in holiday_labels or []]
    if any("(当天)" in item for item in labels):
        return "holiday"
    if any("(节前" in item for item in labels):
        return "holiday_eve"
    if any("(节后" in item for item in labels):
        return "holiday_return"
    if target.weekday() >= 5:
        return "weekend"
    return "normal"


def build_shape(cells, *, cutoff_day=None, min_shape_n=MIN_SHAPE_N):
    trajectories = {}
    for item in _usable(cells, cutoff_day):
        trajectories.setdefault(str(item["depart_date"]), []).append(item)
    by_t = {}
    for items in trajectories.values():
        base = statistics.median(float(item["min_price"]) for item in items)
        if base <= 0:
            continue
        for item in items:
            by_t.setdefault(int(item["days_to_departure"]), []).append(float(item["min_price"]) / base)
    return {
        t: _gated_summary(values, min_shape_n=min_shape_n)
        for t, values in sorted(by_t.items())
    }


def build_shapes_by_regime(
    cells,
    regime_by_depart_date,
    *,
    cutoff_day=None,
    min_shape_n=MIN_SHAPE_N,
):
    """按出发日日型独立建 shape；同一轨迹不会进入其他日型。"""
    grouped = {}
    for item in _usable(cells, cutoff_day):
        depart = str(item["depart_date"])
        regime = str((regime_by_depart_date or {}).get(depart) or "normal")
        grouped.setdefault(regime, []).append(item)
    return {
        regime: build_shape(
            grouped[regime],
            cutoff_day=cutoff_day,
            min_shape_n=min_shape_n,
        )
        for regime in REGIME_ORDER
        if grouped.get(regime)
    }


def estimate_level(cells, shape, *, depart_date, min_obs=MIN_OBS_FOR_LEVEL, cutoff_day=None):
    ratios = []
    used = []
    for item in _usable(cells, cutoff_day):
        if str(item.get("depart_date")) != str(depart_date):
            continue
        point = shape.get(int(item["days_to_departure"]))
        if not point or not point.get("median"):
            continue
        ratios.append(float(item["min_price"]) / float(point["median"]))
        used.append(str(item["observed_day"]))
    value = statistics.median(ratios) if ratios else None
    return {
        "depart_date": str(depart_date),
        "n": len(ratios),
        "value": _clean_number(value, 6) if value is not None else None,
        "reliable": len(ratios) >= min_obs,
        "status": "ok" if len(ratios) >= min_obs else "level不可靠",
        "observed_days": sorted(set(used)),
        "method_version": METHOD_VERSION,
    }


def predict_price(level, shape, *, target_t):
    point = shape.get(int(target_t))
    if not point:
        return {"t": int(target_t), "status": "无可用shape"}
    if not point.get("sufficient", True):
        return {
            "t": int(target_t),
            "status": f"shape样本不足(n={point.get('n', 0)})",
        }
    if not level.get("reliable") or level.get("value") is None:
        return {"t": int(target_t), "status": "level不可靠"}
    factor = float(level["value"])
    return {
        "t": int(target_t),
        "status": "ok",
        "median": _clean_number(factor * float(point["median"])),
        "p25": _clean_number(factor * float(point["p25"])),
        "p75": _clean_number(factor * float(point["p75"])),
        "p10": _clean_number(factor * float(point["p10"])),
        "p90": _clean_number(factor * float(point["p90"])),
        "method_version": METHOD_VERSION,
    }


def assess_overall_reliability(
    *,
    level,
    shape_points,
    backtest_gate,
    source_coverage,
    regime_sample_n,
    min_regime_n=MIN_SHAPE_N,
):
    """以五个二元证据门的最小值表示整体可靠性，不生成主观复合评分。"""
    points = list(shape_points or [])
    shape_n = min((int(item.get("n") or 0) for item in points), default=0)
    shape_passed = bool(points) and all(item.get("sufficient") for item in points)
    components = {
        "level_reliability": {
            "value": int(bool(level.get("reliable"))),
            "passed": bool(level.get("reliable")),
            "detail": f"level(n={int(level.get('n') or 0)})",
        },
        "shape_reliability": {
            "value": int(shape_passed),
            "passed": shape_passed,
            "detail": f"shape(n={shape_n})",
        },
        "backtest_skill": {
            "value": int(bool((backtest_gate or {}).get("passed"))),
            "passed": bool((backtest_gate or {}).get("passed")),
            "detail": f"backtest(n={int((backtest_gate or {}).get('case_n') or 0)})",
        },
        "source_coverage": {
            "value": int(bool(source_coverage)),
            "passed": bool(source_coverage),
            "detail": "source_coverage(完整)" if source_coverage else "source_coverage(不完整)",
        },
        "regime_match": {
            "value": int(int(regime_sample_n or 0) >= int(min_regime_n)),
            "passed": int(regime_sample_n or 0) >= int(min_regime_n),
            "detail": f"regime(n={int(regime_sample_n or 0)})",
        },
    }
    value = min(item["value"] for item in components.values())
    aliases = {
        "level_reliability": "level",
        "shape_reliability": "shape",
        "backtest_skill": "backtest",
        "source_coverage": "source",
        "regime_match": "regime",
    }
    bottlenecks = [
        aliases[key] for key, item in components.items() if item["value"] == value
    ]
    return {
        "value": value,
        "passed": bool(value),
        "components": components,
        "bottlenecks": bottlenecks,
        "bottleneck_details": [
            components[key]["detail"]
            for key in components
            if components[key]["value"] == value
        ],
    }


def assert_no_walk_forward_leakage(case):
    cutoff = str(case["cutoff_day"])
    assert str(case["target_day"]) > cutoff
    assert all(str(day) <= cutoff for day in case.get("fit_observed_days") or [])
    return True


def _metrics(cases, key):
    if not cases:
        return {"mae": None, "mape": None}
    errors = [abs(float(case[key]) - float(case["actual"])) for case in cases]
    percentages = [error / float(case["actual"]) * 100 for error, case in zip(errors, cases) if float(case["actual"]) > 0]
    return {"mae": _clean_number(statistics.mean(errors)), "mape": _clean_number(statistics.mean(percentages), 4)}


def evaluate_skill_gate(*, model_mape, naive_mape, case_n, min_cases=MIN_BACKTEST_CASES, improvement=SKILL_GATE_IMPROVEMENT):
    enough = int(case_n) >= int(min_cases)
    comparable = model_mape is not None and naive_mape not in (None, 0)
    gain = (float(naive_mape) - float(model_mape)) / float(naive_mape) if comparable else None
    return {"passed": bool(enough and comparable and gain + 1e-12 >= improvement), "case_n": int(case_n), "min_cases": int(min_cases), "improvement": _clean_number(gain * 100, 2) if gain is not None else None}


def walk_forward_backtest(
    cells,
    *,
    horizons=(1, 3, 7),
    min_level_obs=MIN_OBS_FOR_LEVEL,
    regime_by_depart_date=None,
    min_shape_n=MIN_SHAPE_N,
):
    usable = _usable(cells)
    by_depart_day = {(str(item["depart_date"]), str(item["observed_day"])): item for item in usable}
    cases_by_horizon = {}
    results = {}
    for horizon in horizons:
        cases = []
        for (depart, target_day), target in sorted(by_depart_day.items()):
            cutoff = date.fromisoformat(target_day) - timedelta(days=int(horizon))
            cutoff_text = cutoff.isoformat()
            fit = [item for item in usable if str(item["observed_day"]) <= cutoff_text]
            target_regime = (regime_by_depart_date or {}).get(depart)
            if target_regime:
                fit_for_shape = [
                    item
                    for item in fit
                    if (regime_by_depart_date or {}).get(str(item["depart_date"])) == target_regime
                ]
            else:
                fit_for_shape = fit
            shape = build_shape(
                fit_for_shape,
                cutoff_day=cutoff_text,
                min_shape_n=min_shape_n,
            )
            level = estimate_level(fit, shape, depart_date=depart, min_obs=min_level_obs, cutoff_day=cutoff_text)
            predicted = predict_price(level, shape, target_t=int(target["days_to_departure"]))
            history = sorted((item for item in fit if str(item["depart_date"]) == depart), key=lambda item: str(item["observed_day"]))
            t_prices = [float(item["min_price"]) for item in fit if int(item["days_to_departure"]) == int(target["days_to_departure"])]
            if predicted.get("status") != "ok" or not history or not t_prices:
                continue
            case = {"cutoff_day": cutoff_text, "target_day": target_day, "fit_observed_days": sorted({str(item["observed_day"]) for item in fit}), "actual": float(target["min_price"]), "model": float(predicted["median"]), "naive": float(history[-1]["min_price"]), "tcurve": statistics.median(t_prices)}
            assert_no_walk_forward_leakage(case)
            cases.append(case)
        model = _metrics(cases, "model")
        naive = _metrics(cases, "naive")
        static = _metrics(cases, "tcurve")
        gate = evaluate_skill_gate(model_mape=model["mape"], naive_mape=naive["mape"], case_n=len(cases))
        cases_by_horizon[str(horizon)] = cases
        results[str(horizon)] = {"n": len(cases), "model": model, "naive": naive, "tcurve": static, "skill_gate": gate}
    return {"method_version": METHOD_VERSION, "horizons": results, "cases": cases_by_horizon}


def write_backtest_report(report, path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def build_notification_forecast(route_info, *, db_path=DEFAULT_DB_PATH, as_of_day=None):
    """构建三重闸门结果；调用方仅在 eligible 时写入 payload。"""
    origin_city, dest_city = route_cities_from_info(route_info)
    route = f"{origin_city}-{dest_city}"
    all_cells = load_tcurve_daily_cells(db_path, route=route)
    cells = [item for item in all_cells if not item.get("degraded")]
    if not cells:
        return {"eligible": False, "reason": "无可用非退化日格"}
    as_of = str(as_of_day or max(item["observed_day"] for item in cells))
    depart_date = str(route_info.get("depart_date") or "")
    if not depart_date:
        return {"eligible": False, "reason": "缺少出发日期"}
    # 本任务只统一诊断报告门控；用户推送技能门保持既有行为与契约。
    shape = build_shape(cells, cutoff_day=as_of, min_shape_n=1)
    level = estimate_level(cells, shape, depart_date=depart_date, cutoff_day=as_of)
    current_t = (date.fromisoformat(depart_date) - date.fromisoformat(as_of)).days
    current_prediction = predict_price(level, shape, target_t=current_t)
    backtest = walk_forward_backtest(cells, min_shape_n=1)
    gate = backtest["horizons"].get("3", {}).get("skill_gate") or {"passed": False}
    if not gate.get("passed"):
        return {"eligible": False, "reason": f"技能门未过(MAPE={backtest['horizons']['3']['model']['mape']}% vs 基线={backtest['horizons']['3']['naive']['mape']}%)", "backtest": backtest}
    if not level.get("reliable"):
        return {"eligible": False, "reason": f"level不可靠(n={level.get('n', 0)})", "backtest": backtest}
    if current_prediction.get("status") != "ok":
        return {"eligible": False, "reason": f"当前T={current_t}不在shape覆盖内", "backtest": backtest}
    predictions = []
    for offset in range(1, 8):
        target_day = date.fromisoformat(as_of) + timedelta(days=offset)
        target_t = (date.fromisoformat(depart_date) - target_day).days
        item = predict_price(level, shape, target_t=target_t)
        if item.get("status") == "ok":
            predictions.append({"target_day": target_day.isoformat(), **item})
    if not predictions:
        return {"eligible": False, "reason": "未来7天无精确shape T", "backtest": backtest}
    used = [item for item in cells if str(item["observed_day"]) <= as_of]
    window = [min(item["observed_day"] for item in used), max(item["observed_day"] for item in used)]
    sources = sorted({source for item in used for source in item.get("min_sources") or []})
    envelope = build_envelope("forecast.market_min", sample_n=len(used), window=window, sources=sources, degraded_excluded=sum(1 for item in all_cells if item.get("degraded")), bucket="市场最低参考价·单人单程·与用户筛选无关")
    envelope["backtest"] = {"horizon": 3, **backtest["horizons"]["3"]}
    return {"eligible": True, "reason": "ok", "method_version": METHOD_VERSION, "price_caliber": "市场最低参考价·单人单程CNY·与用户筛选无关", "as_of_day": as_of, "depart_date": depart_date, "current_t": current_t, "current_market_reference": current_prediction, "level": level, "predictions": predictions, "backtest": backtest["horizons"]["3"], "provenance": envelope}
