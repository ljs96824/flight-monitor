"""基于 P4 日格的可解释分解预测与累计走前回测。"""

from __future__ import annotations

import json
import os
import statistics
from datetime import date, timedelta
from pathlib import Path

from method_registry import method_version
from tcurve import _clean_number, percentile_linear


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


def build_shape(cells, *, cutoff_day=None):
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
    return {t: _summary(values) for t, values in sorted(by_t.items())}


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


def walk_forward_backtest(cells, *, horizons=(1, 3, 7), min_level_obs=MIN_OBS_FOR_LEVEL):
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
            shape = build_shape(fit, cutoff_day=cutoff_text)
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
