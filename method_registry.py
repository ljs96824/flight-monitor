"""统计方法版本的单一注册表。"""

from __future__ import annotations

from types import MappingProxyType


_METHOD_VERSIONS = {
    "obs_store": "v2",
    "collection_ledger": "collection_ledger_v1",
    "tcurve": "tcurve_v2",
    "weekday": "weekday_v2",
    "reftier": "reftier_v1",
    "calendar": "calendar_v1",
    "price_signal": "price_signal_v2",
    "dual_source_agreement": "agreement_v1",
    "provenance": "provenance_v1",
    "forecast": "forecast_v2",
    "patterns": "patterns_v1",
}

METHOD_VERSIONS = MappingProxyType(_METHOD_VERSIONS)
EXPECTED_METHOD_KEYS = frozenset(_METHOD_VERSIONS)

_REGISTRY_VERSIONS = {
    "lcc_registry": "lcc_v1",
    "holiday_calendar": "holiday_calendar_v1",
}
REGISTRY_VERSIONS = MappingProxyType(_REGISTRY_VERSIONS)
EXPECTED_REGISTRY_KEYS = frozenset(_REGISTRY_VERSIONS)
_ALL_VERSIONS = MappingProxyType({**_METHOD_VERSIONS, **_REGISTRY_VERSIONS})

_STAT_FAMILY_METHODS = {
    "reftier": "reftier",
    "calendar": "calendar",
    "weekday": "weekday",
    "price_signal": "price_signal",
    "tcurve": "tcurve",
    "forecast": "forecast",
    "patterns": "patterns",
}


def method_version(method_key: str) -> str:
    """返回已登记的方法版本；未知方法必须显式失败。"""
    try:
        return _ALL_VERSIONS[str(method_key)]
    except KeyError as exc:
        raise KeyError(f"未登记的方法版本: {method_key}") from exc


def method_key_for_stat(stat_key: str) -> str:
    """把统计键映射到方法注册键。"""
    family = str(stat_key or "").split(".", 1)[0]
    try:
        return _STAT_FAMILY_METHODS[family]
    except KeyError as exc:
        raise KeyError(f"未登记的统计键: {stat_key}") from exc


def method_version_for_stat(stat_key: str) -> str:
    return method_version(method_key_for_stat(stat_key))
