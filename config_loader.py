"""Strictly load policy defaults and local runtime configuration."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.defaults.yaml"
RUNTIME_CONFIG_PATH = PROJECT_ROOT / "data" / "runtime_config.yaml"


class RuntimeConfigError(RuntimeError):
    """A required configuration document is absent, corrupt, or incomplete."""


def deep_merge(defaults: dict, runtime: dict) -> dict:
    """Return a recursive mapping merge; runtime values replace policy values."""
    result = deepcopy(defaults)
    for key, value in runtime.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _read_yaml_mapping(path: str | Path, *, label: str) -> dict:
    target = Path(path)
    try:
        payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeConfigError(f"{label}不存在: {target}") from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RuntimeConfigError(
            f"{label}读取失败: {target} ({type(exc).__name__})"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeConfigError(f"{label}根节点必须是对象: {target}")
    return payload


def _require_mapping(payload: dict, key: str, *, label: str) -> dict:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise RuntimeConfigError(f"{label}.{key}必须是对象")
    return value


def validate_runtime_config(payload: dict) -> None:
    budgets = _require_mapping(payload, "source_quota_budget", label="runtime")
    juhe = _require_mapping(budgets, "juhe", label="runtime.source_quota_budget")
    packs = juhe.get("packs")
    if not isinstance(packs, list) or not packs:
        raise RuntimeConfigError("runtime.source_quota_budget.juhe.packs必须是非空数组")
    for index, pack in enumerate(packs):
        if not isinstance(pack, dict):
            raise RuntimeConfigError(f"runtime juhe pack[{index}]必须是对象")
        if not str(pack.get("id") or "").strip() or int(pack.get("added") or 0) <= 0:
            raise RuntimeConfigError(f"runtime juhe pack[{index}]缺少有效id/added")
        try:
            date.fromisoformat(str(pack.get("added_at") or ""))
        except ValueError as exc:
            raise RuntimeConfigError(
                f"runtime juhe pack[{index}].added_at必须是ISO日期"
            ) from exc

    reconciliation = juhe.get("reconciliation")
    if not isinstance(reconciliation, dict):
        raise RuntimeConfigError("runtime juhe reconciliation必须是对象")
    for key in ("checked_at", "console_remaining"):
        if reconciliation.get(key) in (None, ""):
            raise RuntimeConfigError(f"runtime juhe reconciliation缺少{key}")
    try:
        if int(reconciliation["console_remaining"]) < 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise RuntimeConfigError("runtime juhe console_remaining必须是非负整数") from exc

    reserve = juhe.get("reserve")
    if not isinstance(reserve, dict):
        raise RuntimeConfigError("runtime juhe reserve必须是对象")
    try:
        datetime.fromisoformat(str(reserve.get("epoch_started_at") or ""))
        date.fromisoformat(str(reserve.get("target_date") or ""))
    except ValueError as exc:
        raise RuntimeConfigError(
            "runtime juhe reserve必须含ISO epoch_started_at与target_date"
        ) from exc

    if not isinstance(payload.get("RESEARCH_BASKET_ENABLED"), bool):
        raise RuntimeConfigError("runtime.RESEARCH_BASKET_ENABLED必须是布尔值")
    strategy = str(payload.get("RESEARCH_BASKET_STRATEGY") or "").strip().lower()
    if strategy not in {"cohort_v2", "legacy"}:
        raise RuntimeConfigError("runtime.RESEARCH_BASKET_STRATEGY无效")
    if not isinstance(payload.get("paused_research_routes"), list):
        raise RuntimeConfigError("runtime.paused_research_routes必须是数组")
    if not isinstance(payload.get("subscriptions"), list):
        raise RuntimeConfigError("runtime.subscriptions必须是数组")


def load_merged_config(
    defaults_path: str | Path = DEFAULT_CONFIG_PATH,
    runtime_path: str | Path = RUNTIME_CONFIG_PATH,
) -> dict:
    """Strictly load and merge both layers; never synthesize an empty budget."""
    defaults = _read_yaml_mapping(defaults_path, label="政策配置")
    runtime = _read_yaml_mapping(runtime_path, label="运行配置")
    validate_runtime_config(runtime)
    return deep_merge(defaults, runtime)


def load_standalone_config(path: str | Path) -> dict:
    """Load a complete one-file fixture used by tests and explicit diagnostics."""
    return _read_yaml_mapping(path, label="独立配置")


def split_legacy_config(payload: dict) -> tuple[dict, dict]:
    """Split a legacy complete mapping without changing its merged meaning."""
    if not isinstance(payload, dict):
        raise RuntimeConfigError("旧配置根节点必须是对象")
    defaults = deepcopy(payload)
    runtime: dict[str, Any] = {}

    budgets = defaults.setdefault("source_quota_budget", {})
    if not isinstance(budgets, dict):
        raise RuntimeConfigError("旧配置source_quota_budget必须是对象")
    juhe = budgets.setdefault("juhe", {})
    if not isinstance(juhe, dict):
        raise RuntimeConfigError("旧配置juhe预算必须是对象")
    runtime_juhe: dict[str, Any] = {}
    runtime_budgets = {"juhe": runtime_juhe}
    runtime["source_quota_budget"] = runtime_budgets

    for key, placeholder in (("packs", []), ("reconciliation", {})):
        runtime_juhe[key] = deepcopy(juhe.get(key, placeholder))
        juhe[key] = deepcopy(placeholder)

    reserve = juhe.setdefault("reserve", {})
    if not isinstance(reserve, dict):
        raise RuntimeConfigError("旧配置juhe reserve必须是对象")
    runtime_reserve = {}
    for key in ("epoch_started_at", "target_date"):
        runtime_reserve[key] = reserve.get(key)
        reserve[key] = None
    runtime_juhe["reserve"] = runtime_reserve

    for key, placeholder in (
        ("RESEARCH_BASKET_ENABLED", False),
        ("RESEARCH_BASKET_STRATEGY", "cohort_v2"),
        ("paused_research_routes", []),
        ("subscriptions", []),
    ):
        runtime[key] = deepcopy(defaults.get(key, placeholder))
        defaults[key] = deepcopy(placeholder)

    if deep_merge(defaults, runtime) != payload:
        raise RuntimeConfigError("配置拆分未能保持逐字段等价")
    return defaults, runtime


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "RUNTIME_CONFIG_PATH",
    "RuntimeConfigError",
    "deep_merge",
    "load_merged_config",
    "load_standalone_config",
    "split_legacy_config",
    "validate_runtime_config",
]
