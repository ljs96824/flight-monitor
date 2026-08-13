"""订阅表单概念注册表与 UX2 规范控件的纯派生函数。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping


TIME_SEGMENTS = {
    "dawn": ("06:00", "09:00"),
    "morning": ("09:00", "12:00"),
    "noon": ("12:00", "14:00"),
    "afternoon": ("14:00", "17:00"),
    "evening": ("17:00", "20:00"),
    "night": ("20:00", "23:00"),
    "redeye": ("23:00", "06:00"),
}
DEFAULT_DEPARTURE_SLOTS = ("dawn", "morning", "noon", "afternoon", "evening", "night")
DEFAULT_ARRIVAL_SLOTS = DEFAULT_DEPARTURE_SLOTS
DAYTIME_TIME_SLOTS = ("dawn", "morning", "noon", "afternoon", "evening")
ALL_TIME_SLOTS = (*DEFAULT_DEPARTURE_SLOTS, "redeye")

UX2_TIME_CONTROL_FIELDS = (
    "allow_redeye",
    "arrival_preference",
    "separate_direction_times",
    "outbound_time_preference",
    "outbound_allow_redeye",
    "outbound_arrival_preference",
    "return_time_preference",
    "return_allow_redeye",
    "return_arrival_preference",
    "outbound_departure_time_start",
    "outbound_departure_time_end",
    "outbound_arrival_time_start",
    "outbound_arrival_time_end",
    "return_departure_time_start",
    "return_departure_time_end",
    "return_arrival_time_start",
    "return_arrival_time_end",
)


def _normalized_slots(value) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _window_bounds(windows) -> tuple[str, str]:
    if not isinstance(windows, (list, tuple)) or not windows:
        return "", ""
    first = windows[0]
    if not isinstance(first, (list, tuple)) or len(first) < 2:
        return "", ""
    return str(first[0] or ""), str(first[1] or "")


def _legacy_direction(values: Mapping, prefix: str = "") -> dict:
    key_prefix = f"{prefix}_" if prefix else ""
    departure_slots = _normalized_slots(
        values.get(f"{key_prefix}departure_slots") or values.get("departure_slots")
    )
    arrival_slots = _normalized_slots(
        values.get(f"{key_prefix}arrival_slots") or values.get("arrival_slots")
    )
    departure_windows = (
        values.get(f"{key_prefix}departure_time_windows")
        or values.get("departure_time_windows")
        or []
    )
    arrival_windows = (
        values.get(f"{key_prefix}arrival_time_windows")
        or values.get("arrival_time_windows")
        or []
    )
    return {
        "departure_slots": departure_slots,
        "arrival_slots": arrival_slots,
        "departure_windows": departure_windows,
        "arrival_windows": arrival_windows,
    }


def _project_direction(values: Mapping, *, no_late: bool, prefer_daytime: bool) -> dict:
    departure_slots = list(values.get("departure_slots") or [])
    arrival_slots = list(values.get("arrival_slots") or [])
    departure_windows = values.get("departure_windows") or []
    arrival_windows = values.get("arrival_windows") or []

    if departure_slots == list(DAYTIME_TIME_SLOTS):
        mode = "daytime"
        allow_redeye = False
    elif departure_slots == list(ALL_TIME_SLOTS) or not departure_windows:
        mode = "unlimited"
        allow_redeye = "redeye" in departure_slots or not departure_slots
    elif departure_slots == list(DEFAULT_DEPARTURE_SLOTS):
        mode = "unlimited"
        allow_redeye = False
    else:
        mode = "custom"
        allow_redeye = "redeye" in departure_slots

    if no_late:
        arrival = "no_late"
    elif prefer_daytime or (
        arrival_slots == list(DAYTIME_TIME_SLOTS) and mode != "daytime"
    ):
        arrival = "daytime"
    elif mode == "daytime" and arrival_slots == list(DAYTIME_TIME_SLOTS):
        arrival = "any"
    elif arrival_windows and arrival_windows not in (
        [["06:00", "23:00"]],
        [("06:00", "23:00")],
    ):
        arrival = "custom"
    else:
        arrival = "any"

    departure_start, departure_end = _window_bounds(departure_windows)
    arrival_start, arrival_end = _window_bounds(arrival_windows)
    return {
        "time_preference": mode,
        "allow_redeye": "true" if allow_redeye else "false",
        "arrival_preference": arrival,
        "departure_time_start": departure_start,
        "departure_time_end": departure_end,
        "arrival_time_start": arrival_start,
        "arrival_time_end": arrival_end,
    }


def project_time_concept_fields(values: Mapping, *, round_trip: bool) -> dict:
    """把已有订阅的时段字段无副作用投影到 UX2 唯一控件。"""
    no_late = _truthy(values.get("no_late_arrival"))
    prefer_daytime = _truthy(values.get("prefer_daytime_arrival"))
    shared_raw = _legacy_direction(values)
    outbound_raw = _legacy_direction(values, "outbound") if round_trip else shared_raw
    return_raw = _legacy_direction(values, "return") if round_trip else shared_raw
    outbound = _project_direction(
        outbound_raw,
        no_late=no_late,
        prefer_daytime=prefer_daytime,
    )
    returned = _project_direction(
        return_raw,
        no_late=no_late,
        prefer_daytime=prefer_daytime,
    )
    separate = round_trip and outbound_raw != return_raw
    result = {
        "ux2_concept_form": "true",
        "time_preference": outbound["time_preference"],
        "allow_redeye": outbound["allow_redeye"],
        "arrival_preference": outbound["arrival_preference"],
        "separate_direction_times": "true" if separate else "false",
        "departure_time_start": outbound["departure_time_start"],
        "departure_time_end": outbound["departure_time_end"],
        "arrival_time_start": outbound["arrival_time_start"],
        "arrival_time_end": outbound["arrival_time_end"],
    }
    for prefix, projected in (("outbound", outbound), ("return", returned)):
        for key, value in projected.items():
            result[f"{prefix}_{key}"] = value
    return result


def _concept(
    station_id: str,
    canonical_control: str,
    fields,
    derived=(),
    *,
    canonical_input_names=None,
):
    declared_fields = tuple(fields)
    return {
        "station_id": station_id,
        "canonical_control": canonical_control,
        "fields": declared_fields,
        "canonical_input_names": tuple(canonical_input_names or declared_fields),
        "derived_schema_fields": tuple(derived),
    }


CONCEPTS = {
    "form_identity": _concept("where", "编辑订阅标识(内部)", ("subscription_index",), ()),
    "route_origin": _concept("where", "出发地点选择器", ("origin_select", "origin_manual"), ("origin", "origin_type")),
    "origin_airports": _concept("where", "出发机场范围", ("origin_airports_active",), ("origin_airports", "origin_airports_active")),
    "route_destination": _concept("where", "到达地点选择器", ("destination",), ("destination", "destination_type")),
    "destination_airports": _concept("where", "到达机场范围", ("destination_airports_active",), ("destination_airports", "destination_airports_active")),
    "route_type": _concept("where", "航线类型", ("route_type",), ("route_type",)),
    "departure_date": _concept("when", "出发日期", ("depart_date",), ("depart_date", "basic.departure_date")),
    "departure_flexibility": _concept("when", "出发日期弹性", ("date_flexibility",), ("date_flexibility",)),
    "trip_direction": _concept("when", "单程或往返", ("round_trip",), ("round_trip", "basic.trip_type")),
    "return_date": _concept("when", "返程日期", ("return_date",), ("return_date",)),
    "return_flexibility": _concept("when", "返程日期弹性", ("return_date_flexibility",), ("return_date_flexibility",)),
    "same_day_round_trip": _concept("when", "当天往返", ("same_day_round_trip", "day_trip_period"), ("constraints.same_day_round_trip", "constraints.day_trip_period")),
    "meeting_window": _concept("when", "会议开始与结束", ("business_start", "business_end", "meeting_start", "meeting_end"), ("constraints.business_start", "constraints.business_end", "constraints.meeting_start", "constraints.meeting_end")),
    "meeting_location": _concept("when", "会议地点", ("meeting_location",), ("constraints.meeting_location",)),
    "meeting_importance": _concept("when", "会议重要程度", ("meeting_importance",), ("constraints.meeting_importance",)),
    "same_day_execution": _concept(
        "when",
        "当天往返执行参数",
        ("buffer_hours", "transport_mode", "user_transport_min", "redundancy_min"),
        (
            "constraints.buffer_hours",
            "constraints.transport_mode",
            "constraints.user_transport_min",
            "constraints.redundancy_min",
        ),
    ),
    "travel_context": _concept("who", "出行场景与目的", ("travel_scenario", "trip_natures"), ("preferences.travel_scenarios", "constraints.trip_natures")),
    "business_level": _concept("who", "商务层级", ("user_level",), ("constraints.user_level",)),
    "companion_mode": _concept("who", "同行形态", ("companions", "solo_travel"), ("companions", "preferences.solo_travel")),
    "passenger_mix": _concept("who", "乘客构成", ("passenger_count", "adult_count", "child_count", "elderly_count", "infant_count"), ("preferences.passengers", "basic.passenger_count")),
    "child_profile": _concept("who", "儿童画像", ("child_type",), ("preferences.child_type",)),
    "elderly_profile": _concept("who", "老人画像", ("elderly_condition",), ("preferences.elderly_condition",)),
    "companion_constraints": _concept("who", "同行约束", ("companion_constraints",), ("preferences.companion_constraints",)),
    "set_off_times": _concept("who", "最早动身时间", ("outbound_set_off", "return_set_off"), ("constraints.outbound_set_off", "constraints.return_set_off")),
    "transport_estimates": _concept("who", "交通时间估算", ("origin_transport_min", "destination_transport_min"), ("constraints.origin_transport_min", "constraints.destination_transport_min")),
    "transport_margin": _concept("who", "交通冗余", ("transport_margin_mode",), ("constraints.transport_margin_mode",)),
    "reserve_overrides": _concept("who", "商务冗余覆盖", ("airport_advance_min", "arrival_exit_min", "delay_buffer_min", "pre_meeting_buffer_min", "post_meeting_buffer_min", "custom_redundancy_min"), ("constraints.airport_advance_min", "constraints.arrival_exit_min", "constraints.delay_buffer_min", "constraints.pre_meeting_buffer_min", "constraints.post_meeting_buffer_min", "constraints.custom_redundancy_min")),
    "team_arrangement": _concept("who", "团队安排", ("team_passenger_count", "team_date_flexibility", "same_flight_required"), ("constraints.team_passenger_count", "constraints.team_date_flexibility", "constraints.same_flight_required")),
    "price_strategy": _concept("budget", "价格策略", ("price_strategy",), ("constraints.budget_strategy",)),
    "max_budget": _concept("budget", "最高预算与口径", ("max_budget_mode", "max_budget", "max_budget_scope"), ("max_budget", "max_budget_mode", "max_budget_scope")),
    "target_price": _concept("budget", "理想价与口径", ("target_price_mode", "target_price", "target_price_scope"), ("target_price", "target_price_mode", "target_price_scope")),
    "legacy_budget_scope": _concept("budget", "预算口径兼容别名", ("budget_scope",), ("budget_scope",)),
    "price_tolerance": _concept("budget", "价格容忍度", ("price_tolerance_mode", "price_tolerance_custom"), ("soft_preferences.price_tolerance",)),
    "reimbursement": _concept("budget", "报销上限", ("reimburse_per_person",), ("constraints.reimburse_per_person",)),
    "invoice": _concept("budget", "发票要求", ("invoice_needed", "invoice_context", "invoice_special_vat", "invoice_cabin_limit"), ("preferences.invoice_needed", "preferences.invoice_special_vat", "preferences.invoice_cabin_limit")),
    "interaction_depth": _concept("flight_preferences", "向导交互深度(服务端派生)", ("monitor_mode",), ("monitor_mode",)),
    "transfer": _concept("flight_preferences", "中转偏好芯片组", ("transfer_policy", "short_transfer_limit", "accept_overnight_transfer", "accept_self_transfer"), ("transfer_policy", "direct_only", "advanced_rules.transfer")),
    "time": _concept(
        "flight_preferences",
        "统一时间偏好组",
        (
            "ux2_concept_form", "ux2_time_touched",
            "ux2_original_departure_time_policy", "ux2_original_arrival_time_policy",
            "time_preference", *UX2_TIME_CONTROL_FIELDS, "departure_time_policy",
            "departure_slots", "arrival_slots", "outbound_departure_slots",
            "outbound_arrival_slots", "return_departure_slots", "return_arrival_slots",
            "departure_time_start", "departure_time_end", "arrival_time_start",
            "arrival_time_end", "no_late_arrival", "prefer_daytime_arrival",
        ),
        ("departure_time_policy", "arrival_time_policy", "red_eye", "red_eye_policy", "hard_constraints.*_slots", "hard_constraints.*_time_windows", "soft_preferences.*_time_windows"),
    ),
    "baggage": _concept("flight_preferences", "行李芯片组", ("baggage",), ("need_baggage", "hard_constraints.checked_baggage_required")),
    "refund": _concept("flight_preferences", "退改芯片组", ("refund_flexibility",), ("refund_flexibility", "preferences.refund_policy")),
    "price_sensitivity": _concept("flight_preferences", "价格敏感度芯片组", ("price_sensitivity",), ("price_sensitivity",)),
    "airline": _concept("flight_preferences", "航司约束", ("airline_policy", "exclude_airlines", "blocked_airlines_common"), ("airline_policy", "exclude_airlines", "advanced_rules.airlines")),
    "lcc": _concept("flight_preferences", "廉航三态芯片组", ("lcc_policy",), ("lcc_policy", "advanced_rules.airlines.lcc_policy")),
    "cabin": _concept("flight_preferences", "舱位安排", ("cabin_policy", "cabin_arrangement", "business_seats", "economy_seats"), ("cabin_classes", "constraints.cabin_arrangement", "constraints.business_seats", "constraints.economy_seats")),
    "trip_rigidity": _concept("flight_preferences", "行程确定性", ("trip_rigidity",), ("trip_rigidity",)),
    "notification_primary": _concept("notifications", "提醒主目标", ("primary_goal",), ("notification_goals.primary",)),
    "notification_channel": _concept("notifications", "提醒渠道与邮箱", ("notification_method", "notification_email"), ("notification_goals.method", "notification_goals.email")),
    "notification_frequency": _concept("notifications", "提醒频率", ("notification_frequency", "notification_frequency_rule"), ("notification_goals.frequency", "advanced_rules.alerts.frequency")),
    "notification_threshold": _concept("notifications", "价格变化阈值", ("price_change_threshold",), ("notification_goals.price_change_threshold", "advanced_rules.alerts.price_change_threshold")),
    "notification_secondary": _concept("notifications", "提醒附加目标", ("secondary_goals",), ("notification_goals.secondary", "advanced_rules.alerts.types")),
    "notification_digest": _concept("notifications", "摘要时间", ("digest_time",), ("notification_goals.digest_time", "advanced_rules.alerts.digest_time")),
    "remember_preferences": _concept("notifications", "记住偏好", ("remember_preferences",), ()),
}


def validate_concept_registry(field_owners: Mapping[str, str], concepts=None) -> dict[str, list]:
    """确保声明式表单字段恰好归属一个概念且站点一致。"""
    registry = concepts or CONCEPTS
    owners = Counter()
    stations = {}
    for concept_name, concept in registry.items():
        station_id = concept.get("station_id")
        for field in concept.get("fields") or ():
            owners[field] += 1
            stations.setdefault(field, []).append((concept_name, station_id))
    missing = sorted(set(field_owners) - set(owners))
    duplicates = sorted(field for field, count in owners.items() if count > 1)
    unknown = sorted(set(owners) - set(field_owners))
    wrong_station = sorted(
        (field, concept_name, station_id, field_owners[field])
        for field, entries in stations.items()
        if field in field_owners
        for concept_name, station_id in entries
        if field_owners[field] != station_id
    )
    missing_canonical = sorted(
        concept_name
        for concept_name, concept in registry.items()
        if not concept.get("canonical_input_names")
    )
    invalid_canonical = sorted(
        (concept_name, field)
        for concept_name, concept in registry.items()
        for field in concept.get("canonical_input_names") or ()
        if field not in (concept.get("fields") or ())
    )
    result = {
        "missing": missing,
        "duplicates": duplicates,
        "wrong_station": wrong_station,
        "unknown": unknown,
        "missing_canonical": missing_canonical,
        "invalid_canonical": invalid_canonical,
    }
    errors = []
    if missing:
        errors.append(f"未归属={missing}")
    if duplicates:
        errors.append(f"重复归属={duplicates}")
    if wrong_station:
        errors.append(f"跨站归属={wrong_station}")
    if unknown:
        errors.append(f"未知字段={unknown}")
    if missing_canonical:
        errors.append(f"缺少规范控件={missing_canonical}")
    if invalid_canonical:
        errors.append(f"规范控件未归属概念={invalid_canonical}")
    if errors:
        raise ValueError("概念注册表无效: " + "; ".join(errors))
    return result


def _first(values, key: str, default="") -> str:
    if hasattr(values, "getlist"):
        items = values.getlist(key)
        value = items[0] if items else default
    elif isinstance(values, Mapping):
        value = values.get(key, default)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else default
    else:
        value = default
    return str(value if value not in (None, "") else default)


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "checked"}


def _mode_slots(mode: str, allow_redeye: bool) -> list[str]:
    if mode == "daytime":
        return list(DAYTIME_TIME_SLOTS)
    if mode == "unlimited" and allow_redeye:
        return list(ALL_TIME_SLOTS)
    return list(DEFAULT_DEPARTURE_SLOTS)


def _windows(mode: str, slots: list[str], start: str, end: str) -> list[list[str]]:
    if mode == "unlimited" and "redeye" in slots:
        return []
    if mode == "daytime":
        return [["06:00", "20:00"]]
    if mode == "custom" and start and end:
        return [[start, end]]
    if mode == "unlimited":
        return [["06:00", "23:00"]]
    return [list(TIME_SEGMENTS[slot]) for slot in slots if slot in TIME_SEGMENTS]


def _direction_time(values, prefix: str = "") -> dict:
    key_prefix = f"{prefix}_" if prefix else ""
    mode = _first(values, f"{key_prefix}time_preference", _first(values, "time_preference", "unlimited"))
    if mode == "no_redeye":
        mode = "unlimited"
        allow_redeye = False
    else:
        allow_redeye = _truthy(
            _first(values, f"{key_prefix}allow_redeye", _first(values, "allow_redeye", "false"))
        )
    arrival = _first(
        values,
        f"{key_prefix}arrival_preference",
        _first(values, "arrival_preference", "any"),
    )
    departure_slots = _mode_slots(mode, allow_redeye)
    if arrival == "daytime" or (arrival == "any" and mode == "daytime"):
        arrival_slots = list(DAYTIME_TIME_SLOTS)
    elif allow_redeye and arrival == "any":
        arrival_slots = list(ALL_TIME_SLOTS)
    else:
        arrival_slots = list(DEFAULT_ARRIVAL_SLOTS)
    departure_start = _first(values, f"{key_prefix}departure_time_start", _first(values, "departure_time_start"))
    departure_end = _first(values, f"{key_prefix}departure_time_end", _first(values, "departure_time_end"))
    arrival_start = _first(values, f"{key_prefix}arrival_time_start", _first(values, "arrival_time_start"))
    arrival_end = _first(values, f"{key_prefix}arrival_time_end", _first(values, "arrival_time_end"))
    return {
        "mode": mode if mode in {"unlimited", "daytime", "custom"} else "unlimited",
        "allow_redeye": allow_redeye,
        "arrival_preference": arrival if arrival in {"any", "daytime", "no_late", "custom"} else "any",
        "departure_slots": departure_slots,
        "arrival_slots": arrival_slots,
        "departure_start": departure_start,
        "departure_end": departure_end,
        "arrival_start": arrival_start,
        "arrival_end": arrival_end,
    }


def derive_time_concept_fields(values, *, round_trip: bool) -> dict:
    """把 UX2 唯一时间控件派生为现有 slot/window/policy 字段。"""
    separate = round_trip and _truthy(_first(values, "separate_direction_times", "false"))
    shared = _direction_time(values)
    outbound = _direction_time(values, "outbound") if separate else shared
    returned = _direction_time(values, "return") if separate else shared
    modes = {outbound["mode"], returned["mode"]} if round_trip else {shared["mode"]}
    red_eye_values = {outbound["allow_redeye"], returned["allow_redeye"]} if round_trip else {shared["allow_redeye"]}
    if separate and (
        outbound["departure_slots"] != returned["departure_slots"]
        or outbound["arrival_slots"] != returned["arrival_slots"]
    ):
        legacy_mode = "custom"
    elif "custom" in modes:
        legacy_mode = "custom"
    elif modes == {"daytime"}:
        legacy_mode = "daytime"
    elif red_eye_values == {False}:
        legacy_mode = "no_redeye"
    else:
        legacy_mode = "unlimited"

    base = outbound if round_trip else shared
    departure_policy = {
        "no_redeye": "no_redeye",
        "daytime": "daytime",
    }.get(legacy_mode, "any")
    arrival_preferences = {
        outbound["arrival_preference"],
        returned["arrival_preference"],
    } if round_trip else {shared["arrival_preference"]}
    if "no_late" in arrival_preferences:
        arrival_policy = "no_midnight"
    elif "daytime" in arrival_preferences or legacy_mode == "daytime":
        arrival_policy = "daytime_only"
    else:
        arrival_policy = "any"

    result = {
        "time_preference": legacy_mode,
        "departure_time_policy": departure_policy,
        "arrival_time_policy": arrival_policy,
        "departure_slots": list(base["departure_slots"]),
        "arrival_slots": list(base["arrival_slots"]),
        "outbound_departure_slots": list(outbound["departure_slots"]),
        "outbound_arrival_slots": list(outbound["arrival_slots"]),
        "return_departure_slots": list(returned["departure_slots"]),
        "return_arrival_slots": list(returned["arrival_slots"]),
        "departure_time_start": base["departure_start"],
        "departure_time_end": base["departure_end"],
        "arrival_time_start": base["arrival_start"],
        "arrival_time_end": base["arrival_end"],
        "departure_time_windows": _windows(base["mode"], base["departure_slots"], base["departure_start"], base["departure_end"]),
        "arrival_time_windows": _windows("custom" if base["arrival_preference"] == "custom" else ("daytime" if base["arrival_preference"] == "daytime" or base["mode"] == "daytime" else "unlimited"), base["arrival_slots"], base["arrival_start"], base["arrival_end"]),
        "outbound_departure_time_windows": _windows(outbound["mode"], outbound["departure_slots"], outbound["departure_start"], outbound["departure_end"]),
        "outbound_arrival_time_windows": _windows("custom" if outbound["arrival_preference"] == "custom" else ("daytime" if outbound["arrival_preference"] == "daytime" or outbound["mode"] == "daytime" else "unlimited"), outbound["arrival_slots"], outbound["arrival_start"], outbound["arrival_end"]),
        "return_departure_time_windows": _windows(returned["mode"], returned["departure_slots"], returned["departure_start"], returned["departure_end"]),
        "return_arrival_time_windows": _windows("custom" if returned["arrival_preference"] == "custom" else ("daytime" if returned["arrival_preference"] == "daytime" or returned["mode"] == "daytime" else "unlimited"), returned["arrival_slots"], returned["arrival_start"], returned["arrival_end"]),
        "no_late_arrival": "true" if any(item["arrival_preference"] == "no_late" for item in (outbound, returned) if item) else "false",
        "prefer_daytime_arrival": "true" if any(item["arrival_preference"] == "daytime" for item in (outbound, returned) if item) else "false",
    }
    return result
