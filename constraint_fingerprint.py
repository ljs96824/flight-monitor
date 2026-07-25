"""同条件价格历史使用的硬约束规范化指纹。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


CONSTRAINT_FINGERPRINT_FIELDS = (
    "direct_only",
    "transfer_policy",
    "red_eye",
    "red_eye_policy",
    "allow_red_eye",
    "no_redeye_strict",
    "departure_time_policy",
    "arrival_time_policy",
    "departure_slots",
    "arrival_slots",
    "outbound_departure_slots",
    "outbound_arrival_slots",
    "return_departure_slots",
    "return_arrival_slots",
    "departure_time_windows",
    "arrival_time_windows",
    "outbound_departure_time_windows",
    "outbound_arrival_time_windows",
    "return_departure_time_windows",
    "return_arrival_time_windows",
    "time_preference_mode",
    "same_day_round_trip",
    "day_trip_period",
    "business_start",
    "business_end",
    "meeting_location",
    "meeting_importance",
    "outbound_set_off",
    "return_set_off",
    "user_transport_min",
    "origin_transport_min",
    "destination_transport_min",
    "airport_advance_min",
    "arrival_exit_min",
    "delay_buffer_min",
    "pre_meeting_buffer_min",
    "post_meeting_buffer_min",
    "custom_redundancy_min",
    "transport_margin_mode",
    "redundancy_min",
    "need_baggage",
    "airline_policy",
    "exclude_airlines",
    "lcc_policy",
    "max_extra_duration_hours",
    "max_total_duration_hours",
    "accept_overnight_transfer",
    "accept_self_transfer",
    "origin_airport_preference",
    "origin_airports_active",
    "destination_airports_active",
    "excluded_airports",
    "cabin_classes",
)

EXPECTED_CONSTRAINT_FINGERPRINT_FIELDS = frozenset(
    {
        "direct_only",
        "transfer_policy",
        "red_eye",
        "red_eye_policy",
        "allow_red_eye",
        "no_redeye_strict",
        "departure_time_policy",
        "arrival_time_policy",
        "departure_slots",
        "arrival_slots",
        "outbound_departure_slots",
        "outbound_arrival_slots",
        "return_departure_slots",
        "return_arrival_slots",
        "departure_time_windows",
        "arrival_time_windows",
        "outbound_departure_time_windows",
        "outbound_arrival_time_windows",
        "return_departure_time_windows",
        "return_arrival_time_windows",
        "time_preference_mode",
        "same_day_round_trip",
        "day_trip_period",
        "business_start",
        "business_end",
        "meeting_location",
        "meeting_importance",
        "outbound_set_off",
        "return_set_off",
        "user_transport_min",
        "origin_transport_min",
        "destination_transport_min",
        "airport_advance_min",
        "arrival_exit_min",
        "delay_buffer_min",
        "pre_meeting_buffer_min",
        "post_meeting_buffer_min",
        "custom_redundancy_min",
        "transport_margin_mode",
        "redundancy_min",
        "need_baggage",
        "airline_policy",
        "exclude_airlines",
        "lcc_policy",
        "max_extra_duration_hours",
        "max_total_duration_hours",
        "accept_overnight_transfer",
        "accept_self_transfer",
        "origin_airport_preference",
        "origin_airports_active",
        "destination_airports_active",
        "excluded_airports",
        "cabin_classes",
    }
)

_DEFAULTS = {
    "direct_only": "flexible",
    "transfer_policy": "reasonable",
    "red_eye": "reject",
    "red_eye_policy": "not_allowed",
    "allow_red_eye": False,
    "no_redeye_strict": False,
    "departure_time_policy": "after_06",
    "arrival_time_policy": "any",
    "departure_slots": [],
    "arrival_slots": [],
    "outbound_departure_slots": [],
    "outbound_arrival_slots": [],
    "return_departure_slots": [],
    "return_arrival_slots": [],
    "departure_time_windows": [],
    "arrival_time_windows": [],
    "outbound_departure_time_windows": [],
    "outbound_arrival_time_windows": [],
    "return_departure_time_windows": [],
    "return_arrival_time_windows": [],
    "time_preference_mode": "",
    "same_day_round_trip": False,
    "day_trip_period": "",
    "business_start": "",
    "business_end": "",
    "meeting_location": "",
    "meeting_importance": "",
    "outbound_set_off": "",
    "return_set_off": "",
    "user_transport_min": None,
    "origin_transport_min": None,
    "destination_transport_min": None,
    "airport_advance_min": None,
    "arrival_exit_min": None,
    "delay_buffer_min": None,
    "pre_meeting_buffer_min": None,
    "post_meeting_buffer_min": None,
    "custom_redundancy_min": None,
    "transport_margin_mode": "",
    "redundancy_min": None,
    "need_baggage": "unknown",
    "airline_policy": "any",
    "exclude_airlines": [],
    "lcc_policy": "any",
    "max_extra_duration_hours": None,
    "max_total_duration_hours": None,
    "accept_overnight_transfer": False,
    "accept_self_transfer": False,
    "origin_airport_preference": "all",
    "origin_airports_active": [],
    "destination_airports_active": [],
    "excluded_airports": [],
    "cabin_classes": [],
}

_LIST_FIELDS = {
    "departure_slots",
    "arrival_slots",
    "outbound_departure_slots",
    "outbound_arrival_slots",
    "return_departure_slots",
    "return_arrival_slots",
    "departure_time_windows",
    "arrival_time_windows",
    "outbound_departure_time_windows",
    "outbound_arrival_time_windows",
    "return_departure_time_windows",
    "return_arrival_time_windows",
    "exclude_airlines",
    "origin_airports_active",
    "destination_airports_active",
    "excluded_airports",
    "cabin_classes",
}
_BOOL_FIELDS = {
    "allow_red_eye",
    "no_redeye_strict",
    "same_day_round_trip",
    "accept_overnight_transfer",
    "accept_self_transfer",
}
_NUMERIC_FIELDS = {
    "user_transport_min",
    "origin_transport_min",
    "destination_transport_min",
    "airport_advance_min",
    "arrival_exit_min",
    "delay_buffer_min",
    "pre_meeting_buffer_min",
    "post_meeting_buffer_min",
    "custom_redundancy_min",
    "redundancy_min",
    "max_extra_duration_hours",
    "max_total_duration_hours",
}
_LOWER_FIELDS = {
    "direct_only",
    "transfer_policy",
    "red_eye",
    "red_eye_policy",
    "departure_time_policy",
    "arrival_time_policy",
    "time_preference_mode",
    "day_trip_period",
    "meeting_importance",
    "transport_margin_mode",
    "need_baggage",
    "airline_policy",
    "lcc_policy",
    "origin_airport_preference",
}
_ALIASES = {
    "departure_slots": ("departure_slots", "outbound_departure_slots", "preferred_departure_slots"),
    "arrival_slots": ("arrival_slots", "outbound_arrival_slots", "preferred_arrival_slots"),
    "need_baggage": ("need_baggage", "baggage"),
}


def _containers(subscription: Mapping | None) -> list[Mapping]:
    subscription = subscription if isinstance(subscription, Mapping) else {}
    result = [subscription]
    for key in (
        "hard_constraints",
        "constraints",
        "soft_preferences",
        "preferences",
        "basic",
        "advanced_rules",
    ):
        value = subscription.get(key)
        if isinstance(value, Mapping):
            result.append(value)
    return result


def _first_value(containers: list[Mapping], field: str):
    aliases = _ALIASES.get(field, (field,))
    for container in containers:
        for alias in aliases:
            if alias not in container:
                continue
            value = container.get(alias)
            if value is not None and value != "":
                return value
    return _DEFAULTS[field]


def _normalize_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "allow",
        "allowed",
        "strict",
        "是",
        "允许",
    }


def _normalize_nested(value):
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_nested(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_nested(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_normalize_nested(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    if isinstance(value, str):
        return value.strip().upper()
    return value


def _normalize_list(value) -> list:
    if value is None:
        values = []
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
    else:
        text = str(value).replace("，", ",").replace("、", ",")
        values = text.split(",")
    normalized_by_key = {}
    for item in values:
        if not str(item or "").strip():
            continue
        normalized = _normalize_nested(item)
        key = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        normalized_by_key[key] = normalized
    return [normalized_by_key[key] for key in sorted(normalized_by_key)]


def _normalize_scalar(value):
    if isinstance(value, str):
        return value.strip()
    return value


def _normalize_number(value):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value).strip()
    return int(number) if number.is_integer() else number


def normalized_constraint_set(subscription: Mapping | None) -> dict:
    """返回仅含冻结硬约束字段、可稳定序列化的规范化结构。"""
    if set(CONSTRAINT_FINGERPRINT_FIELDS) != EXPECTED_CONSTRAINT_FINGERPRINT_FIELDS:
        raise AssertionError("约束指纹字段清单已变化，必须显式更新冻结守卫")
    containers = _containers(subscription)
    result = {}
    for field in CONSTRAINT_FINGERPRINT_FIELDS:
        value = _first_value(containers, field)
        if field in _LIST_FIELDS:
            result[field] = _normalize_list(value)
        elif field in _BOOL_FIELDS:
            result[field] = _normalize_bool(value)
        elif field in _NUMERIC_FIELDS:
            result[field] = _normalize_number(value)
        elif field in _LOWER_FIELDS:
            result[field] = str(value or "").strip().lower()
        else:
            result[field] = _normalize_scalar(value)
    return result


def constraint_fingerprint(subscription: Mapping | None) -> str:
    """对规范化硬约束集计算稳定 SHA-256 指纹。"""
    canonical = json.dumps(
        normalized_constraint_set(subscription),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def short_constraint_fingerprint(value: str | None) -> str:
    return str(value or "").strip()[:8]
