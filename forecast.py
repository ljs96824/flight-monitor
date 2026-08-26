"""基于 P4 日格的可解释分解预测与累计走前回测。"""

from __future__ import annotations

import hashlib
import json
import os
import statistics
from datetime import date, timedelta
from pathlib import Path

from airports import AIRPORTS
from holidays import holiday_labels_for_route
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
FORECAST_SAMPLE_ROLES = frozenset(
    {"trajectory_anchor", "user_monitor", "legacy"}
)
REGIME_ORDER = ("normal", "weekend", "holiday_eve", "holiday", "holiday_return")
FORECAST_ELIGIBILITY_PRIORITY = (
    "lineage_incomplete",
    "skill_gate_failed",
    "regime_insufficient",
    "shape_sample_insufficient",
    "source_degraded",
    "eligible",
)


def filter_forecast_cells(cells):
    """Exclude pure cross-sectional probes from trajectory shape/level."""
    result = []
    for item in cells or []:
        roles = set(item.get("sample_roles") or [item.get("sample_role") or "legacy"])
        if roles & FORECAST_SAMPLE_ROLES:
            result.append(item)
    return result


def _usable(cells, cutoff_day=None):
    return [
        item for item in filter_forecast_cells(cells)
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


def route_airport_codes_from_info(route_info):
    """从通知路由信息提取已知 IATA；机场真值只读 AIRPORTS。"""
    route_info = route_info or {}

    def pick(active_key, value_key):
        active = route_info.get(active_key)
        candidates = list(active) if isinstance(active, (list, tuple)) else []
        candidates.append(route_info.get(value_key))
        for candidate in candidates:
            code = str(candidate or "").strip().upper()
            if code in AIRPORTS:
                return code
        return None

    origin = pick("origin_airports_active", "origin")
    destination = pick("destination_airports_active", "destination")
    return (origin, destination) if origin and destination else None


def build_regime_map(depart_dates, route_codes=None):
    """为每个出发日生成唯一日型；缺机场码时仅按星期事实分类。"""
    result = {}
    for depart in depart_dates:
        labels = (
            holiday_labels_for_route(
                *route_codes,
                date.fromisoformat(str(depart)),
            )
            if route_codes
            else []
        )
        result[str(depart)] = classify_regime(depart, labels)
    return result


def source_coverage_for_departure(cells, depart_date, as_of_day):
    relevant = [
        item
        for item in cells or []
        if str(item.get("depart_date")) == str(depart_date)
        and str(item.get("observed_day")) <= str(as_of_day)
    ]
    return bool(relevant) and all(not item.get("degraded") for item in relevant)


def regime_departure_n(cells, regime_by_depart_date, regime, as_of_day):
    return len(
        {
            str(item["depart_date"])
            for item in cells or []
            if not item.get("degraded")
            and str(item.get("observed_day")) <= str(as_of_day)
            and regime_by_depart_date.get(str(item["depart_date"])) == regime
        }
    )


def lineage_complete_for_cells(cells, *, as_of_day=None):
    used = [
        item
        for item in cells or []
        if not as_of_day or str(item.get("observed_day")) <= str(as_of_day)
    ]
    return bool(used) and all(
        item.get("lineage_complete") is True and item.get("round_ids")
        for item in used
    )


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


def evaluate_forecast_eligibility(
    *,
    level,
    shape_points,
    backtest_gate,
    source_coverage,
    regime_sample_n,
    lineage_complete=True,
    regime=None,
    min_regime_n=MIN_SHAPE_N,
    skill_failure_text=None,
    shape_failure_text=None,
):
    """把最短板证据门翻译成唯一、机器可读的预测资格裁决。

    状态优先级固定为：lineage_incomplete > skill_gate_failed >
    regime_insufficient > shape_sample_insufficient > source_degraded >
    eligible。最高优先级失败作为 primary_reason，其余失败完整保留在
    reason_codes；高分项不得补偿任何低分硬门。
    """
    reliability = assess_overall_reliability(
        level=level,
        shape_points=shape_points,
        backtest_gate=backtest_gate,
        source_coverage=source_coverage,
        regime_sample_n=regime_sample_n,
        min_regime_n=min_regime_n,
    )
    components = reliability["components"]
    reason_codes = []
    if not lineage_complete:
        reason_codes.append("lineage_incomplete")
    if not components["backtest_skill"]["passed"]:
        reason_codes.append("skill_gate_failed")
    if not components["regime_match"]["passed"]:
        reason_codes.append("regime_insufficient")
    if not components["shape_reliability"]["passed"]:
        reason_codes.append("shape_sample_insufficient")
    if not components["level_reliability"]["passed"]:
        reason_codes.append("level_unreliable")
    if not components["source_coverage"]["passed"]:
        reason_codes.append("source_degraded")

    failed_statuses = {
        "lineage_incomplete": "lineage_incomplete" in reason_codes,
        "skill_gate_failed": "skill_gate_failed" in reason_codes,
        "regime_insufficient": "regime_insufficient" in reason_codes,
        "shape_sample_insufficient": (
            "shape_sample_insufficient" in reason_codes
            or "level_unreliable" in reason_codes
        ),
        "source_degraded": "source_degraded" in reason_codes,
    }
    status = next(
        item
        for item in FORECAST_ELIGIBILITY_PRIORITY
        if item == "eligible" or failed_statuses[item]
    )

    if status == "lineage_incomplete":
        bottleneck = "lineage"
        human_text = "round_id lineage不完整，暂不提供预测"
    elif status == "skill_gate_failed":
        bottleneck = "backtest_skill"
        human_text = skill_failure_text or (
            f"技能门未过(n={int((backtest_gate or {}).get('case_n') or 0)})"
        )
    elif status == "regime_insufficient":
        bottleneck = "regime_match"
        human_text = (
            f"同类日型样本不足(regime={regime or 'unknown'},"
            f"n={int(regime_sample_n or 0)})"
        )
    elif status == "shape_sample_insufficient":
        if "level_unreliable" in reason_codes:
            bottleneck = "level_reliability"
            human_text = f"level不可靠(n={int((level or {}).get('n') or 0)})"
        else:
            bottleneck = "shape_reliability"
            human_text = (
                shape_failure_text or components["shape_reliability"]["detail"]
            )
    elif status == "source_degraded":
        bottleneck = "source_coverage"
        human_text = "源覆盖不完整，暂不提供预测"
    else:
        bottleneck = None
        human_text = "预测资格已满足"

    return {
        "status": status,
        "eligible": status == "eligible",
        "primary_reason": None if status == "eligible" else status,
        "bottleneck": bottleneck,
        "reason_codes": reason_codes,
        "human_text": human_text,
        "overall_reliability": reliability,
    }


def _fold_training_signature(cells):
    """为单个历史折生成只覆盖真实训练输入的稳定指纹。"""
    rows = sorted(
        (
            str(item["depart_date"]),
            str(item["observed_day"]),
            int(item["days_to_departure"]),
            float(item["min_price"]),
            bool(item.get("degraded")),
        )
        for item in cells
    )
    serialized = json.dumps(
        rows,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


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
            case = {
                "depart_date": depart,
                "target_day": target_day,
                "target_t": int(target["days_to_departure"]),
                "cutoff_day": cutoff_text,
                "fit_n": len(fit),
                "fit_training_signature": _fold_training_signature(fit),
                "fit_observed_days": sorted(
                    {str(item["observed_day"]) for item in fit}
                ),
                "actual": float(target["min_price"]),
                "model": float(predicted["median"]),
                "naive": float(history[-1]["min_price"]),
                "tcurve": statistics.median(t_prices),
            }
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
    """通过统一资格裁决构建预测；调用方仅在 eligible 时写入 payload。"""
    origin_city, dest_city = route_cities_from_info(route_info)
    route = f"{origin_city}-{dest_city}"
    all_cells = filter_forecast_cells(load_tcurve_daily_cells(db_path, route=route))
    non_degraded = [item for item in all_cells if not item.get("degraded")]
    if not non_degraded:
        return {"eligible": False, "reason": "无可用非退化日格"}
    as_of = str(as_of_day or max(item["observed_day"] for item in non_degraded))
    cells = [
        item for item in non_degraded if str(item.get("observed_day")) <= as_of
    ]
    if not cells:
        return {"eligible": False, "reason": "观测截止日前无可用非退化日格"}
    depart_date = str(route_info.get("depart_date") or "")
    if not depart_date:
        return {"eligible": False, "reason": "缺少出发日期"}
    depart_dates = sorted(
        {str(item["depart_date"]) for item in cells} | {depart_date}
    )
    route_codes = route_airport_codes_from_info(route_info)
    regime_by_depart_date = build_regime_map(depart_dates, route_codes)
    target_regime = regime_by_depart_date[depart_date]
    shapes = build_shapes_by_regime(
        cells,
        regime_by_depart_date,
        cutoff_day=as_of,
        min_shape_n=MIN_SHAPE_N,
    )
    shape = shapes.get(target_regime) or {}
    level = estimate_level(cells, shape, depart_date=depart_date, cutoff_day=as_of)
    current_t = (date.fromisoformat(depart_date) - date.fromisoformat(as_of)).days
    current_prediction = predict_price(level, shape, target_t=current_t)
    backtest = walk_forward_backtest(
        cells,
        regime_by_depart_date=regime_by_depart_date,
        min_shape_n=MIN_SHAPE_N,
    )
    gate = backtest["horizons"].get("3", {}).get("skill_gate") or {"passed": False}
    horizon = backtest["horizons"].get("3") or {}
    targets = []
    future_shape_points = []
    for offset in range(1, 8):
        target_day = date.fromisoformat(as_of) + timedelta(days=offset)
        target_t = (date.fromisoformat(depart_date) - target_day).days
        targets.append((target_day, target_t))
        future_shape_points.append(
            shape.get(target_t) or {"n": 0, "sufficient": False}
        )
    referenced_shape_points = [
        shape.get(current_t) or {"n": 0, "sufficient": False},
        *future_shape_points,
    ]
    source_coverage = source_coverage_for_departure(
        all_cells,
        depart_date,
        as_of,
    )
    regime_sample_n = regime_departure_n(
        cells,
        regime_by_depart_date,
        target_regime,
        as_of,
    )
    lineage_complete = lineage_complete_for_cells(cells, as_of_day=as_of)
    decision = evaluate_forecast_eligibility(
        level=level,
        shape_points=referenced_shape_points,
        backtest_gate=gate,
        source_coverage=source_coverage,
        regime_sample_n=regime_sample_n,
        lineage_complete=lineage_complete,
        regime=target_regime,
        skill_failure_text=(
            f"技能门未过(MAPE={(horizon.get('model') or {}).get('mape')}% "
            f"vs 基线={(horizon.get('naive') or {}).get('mape')}%)"
        ),
        shape_failure_text=f"当前T={current_t}或未来7天shape样本不足",
    )
    if decision["status"] != "eligible":
        return {
            "eligible": False,
            "reason": decision["human_text"],
            "eligibility": decision,
            "backtest": backtest,
        }
    predictions = []
    for target_day, target_t in targets:
        item = predict_price(level, shape, target_t=target_t)
        if item.get("status") == "ok":
            predictions.append({"target_day": target_day.isoformat(), **item})
    if not predictions:
        no_future = evaluate_forecast_eligibility(
            level=level,
            shape_points=[],
            backtest_gate=gate,
            source_coverage=source_coverage,
            regime_sample_n=regime_sample_n,
            lineage_complete=lineage_complete,
            regime=target_regime,
            shape_failure_text="未来7天无精确shape T",
        )
        return {
            "eligible": False,
            "reason": no_future["human_text"],
            "eligibility": no_future,
            "backtest": backtest,
        }
    used = [item for item in cells if str(item["observed_day"]) <= as_of]
    window = [min(item["observed_day"] for item in used), max(item["observed_day"] for item in used)]
    sources = sorted({source for item in used for source in item.get("min_sources") or []})
    envelope = build_envelope("forecast.market_min", sample_n=len(used), window=window, sources=sources, degraded_excluded=sum(1 for item in all_cells if item.get("degraded")), bucket="市场最低参考价·单人单程·与用户筛选无关")
    envelope["backtest"] = {"horizon": 3, **backtest["horizons"]["3"]}
    return {"eligible": True, "reason": "ok", "eligibility": decision, "method_version": METHOD_VERSION, "price_caliber": "市场最低参考价·单人单程CNY·与用户筛选无关", "as_of_day": as_of, "depart_date": depart_date, "current_t": current_t, "current_market_reference": current_prediction, "level": level, "predictions": predictions, "backtest": backtest["horizons"]["3"], "provenance": envelope}
