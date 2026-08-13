"""订阅表单六站结构、显隐规则、摘要与场景预设视图。"""

from __future__ import annotations

from collections.abc import Mapping

from notification_config import DEFAULT_NOTIFICATION_METHOD
from form_concepts import (
    CONCEPTS,
    UX2_TIME_CONTROL_FIELDS,
    derive_time_concept_fields,
    project_time_concept_fields,
    validate_concept_registry,
)


REQUIRED_STATION_COUNT = 4


# 二期仅从这层结构元数据接入规律、五档参考价或日历反馈控件；一期不渲染这些数据。
FORM_STATIONS = (
    {
        "number": 1,
        "id": "where",
        "depth": "required",
        "default_collapsed": False,
        "title": "去哪",
        "fields": (
            "subscription_index",
            "origin_select",
            "origin_manual",
            "origin_airports_active",
            "destination",
            "destination_airports_active",
            "route_type",
        ),
    },
    {
        "number": 2,
        "id": "when",
        "depth": "required",
        "default_collapsed": False,
        "title": "什么时候",
        "fields": (
            "depart_date",
            "date_flexibility",
            "round_trip",
            "return_date",
            "return_date_flexibility",
            "same_day_round_trip",
            "day_trip_period",
            "business_start",
            "business_end",
            "meeting_start",
            "meeting_end",
            "meeting_location",
            "meeting_importance",
        ),
    },
    {
        "number": 3,
        "id": "who",
        "depth": "required",
        "default_collapsed": False,
        "title": "谁去",
        "fields": (
            "travel_scenario",
            "trip_natures",
            "user_level",
            "companions",
            "solo_travel",
            "passenger_count",
            "adult_count",
            "child_count",
            "elderly_count",
            "infant_count",
            "child_type",
            "elderly_condition",
            "companion_constraints",
            "outbound_set_off",
            "return_set_off",
            "user_transport_min",
            "origin_transport_min",
            "destination_transport_min",
            "transport_margin_mode",
            "redundancy_min",
            "airport_advance_min",
            "arrival_exit_min",
            "delay_buffer_min",
            "pre_meeting_buffer_min",
            "post_meeting_buffer_min",
            "custom_redundancy_min",
            "team_passenger_count",
            "team_date_flexibility",
            "same_flight_required",
        ),
    },
    {
        "number": 4,
        "id": "budget",
        "depth": "required",
        "default_collapsed": False,
        "title": "预算",
        "fields": (
            "price_strategy",
            "max_budget_mode",
            "max_budget",
            "max_budget_scope",
            "target_price_mode",
            "target_price",
            "target_price_scope",
            "budget_scope",
            "price_tolerance_mode",
            "price_tolerance_custom",
            "reimburse_per_person",
            "invoice_needed",
            "invoice_context",
            "invoice_special_vat",
            "invoice_cabin_limit",
        ),
    },
    {
        "number": 5,
        "id": "flight_preferences",
        "depth": "optional",
        "default_collapsed": True,
        "title": "飞行偏好",
        "fields": (
            "monitor_mode",
            "ux2_concept_form",
            "ux2_time_touched",
            "ux2_original_departure_time_policy",
            "ux2_original_arrival_time_policy",
            "transfer_policy",
            "short_transfer_limit",
            "accept_overnight_transfer",
            "accept_self_transfer",
            "time_preference",
            "allow_redeye",
            "arrival_preference",
            "separate_direction_times",
            "outbound_time_preference",
            "outbound_allow_redeye",
            "outbound_arrival_preference",
            "return_time_preference",
            "return_allow_redeye",
            "return_arrival_preference",
            "departure_time_policy",
            "departure_slots",
            "arrival_slots",
            "outbound_departure_slots",
            "outbound_arrival_slots",
            "return_departure_slots",
            "return_arrival_slots",
            "departure_time_start",
            "departure_time_end",
            "arrival_time_start",
            "arrival_time_end",
            "outbound_departure_time_start",
            "outbound_departure_time_end",
            "outbound_arrival_time_start",
            "outbound_arrival_time_end",
            "return_departure_time_start",
            "return_departure_time_end",
            "return_arrival_time_start",
            "return_arrival_time_end",
            "no_late_arrival",
            "prefer_daytime_arrival",
            "baggage",
            "refund_flexibility",
            "price_sensitivity",
            "airline_policy",
            "exclude_airlines",
            "blocked_airlines_common",
            "lcc_policy",
            "cabin_policy",
            "cabin_arrangement",
            "business_seats",
            "economy_seats",
            "trip_rigidity",
        ),
    },
    {
        "number": 6,
        "id": "notifications",
        "depth": "optional",
        "default_collapsed": True,
        "title": "怎么提醒",
        "fields": (
            "primary_goal",
            "notification_method",
            "notification_email",
            "notification_frequency",
            "notification_frequency_rule",
            "price_change_threshold",
            "secondary_goals",
            "digest_time",
            "remember_preferences",
        ),
    },
)

FIELD_OWNERS = {
    field: station["id"] for station in FORM_STATIONS for field in station["fields"]
}


def validate_concepts(concepts=None) -> dict[str, list]:
    """验证每个声明式表单字段恰好归属一个概念。"""
    return validate_concept_registry(FIELD_OWNERS, concepts)


validate_concepts()

ADVANCED_FIELD_NAMES = frozenset(
    {
        "ux2_concept_form",
        "time_preference",
        *UX2_TIME_CONTROL_FIELDS,
        "departure_time_policy",
        "departure_slots",
        "arrival_slots",
        "outbound_departure_slots",
        "outbound_arrival_slots",
        "return_departure_slots",
        "return_arrival_slots",
        "departure_time_start",
        "departure_time_end",
        "arrival_time_start",
        "arrival_time_end",
        "lcc_policy",
        "cabin_policy",
        "cabin_arrangement",
        "airline_policy",
        "exclude_airlines",
        "refund_flexibility",
        "origin_transport_min",
        "destination_transport_min",
        "airport_advance_min",
        "arrival_exit_min",
        "delay_buffer_min",
        "pre_meeting_buffer_min",
        "post_meeting_buffer_min",
        "custom_redundancy_min",
    }
)

OPTIONAL_SECTIONS = (
    {
        "id": "feasibility",
        "station_id": "who",
        "title": "可行性参数(可选,已按场景预设)",
        "fields": (
            "outbound_set_off",
            "return_set_off",
            "user_transport_min",
            "origin_transport_min",
            "destination_transport_min",
            "transport_margin_mode",
            "airport_advance_min",
            "arrival_exit_min",
            "delay_buffer_min",
            "pre_meeting_buffer_min",
            "post_meeting_buffer_min",
            "custom_redundancy_min",
        ),
    },
    {
        "id": "flight_preferences",
        "station_id": "flight_preferences",
        "title": "飞行偏好(可选)",
        "fields": tuple(FORM_STATIONS[4]["fields"]),
    },
    {
        "id": "notifications",
        "station_id": "notifications",
        "title": "提醒方式(可选)",
        "fields": tuple(FORM_STATIONS[5]["fields"]),
    },
)

OPTIONAL_SECTION_DEFAULTS = {
    "feasibility": {
        "outbound_set_off": "",
        "return_set_off": "",
        "user_transport_min": "",
        "origin_transport_min": "",
        "destination_transport_min": "",
        "transport_margin_mode": "standard",
        "airport_advance_min": "",
        "arrival_exit_min": "",
        "delay_buffer_min": "",
        "pre_meeting_buffer_min": "",
        "post_meeting_buffer_min": "",
        "custom_redundancy_min": "",
    },
    "flight_preferences": {
        "ux2_concept_form": "true",
        "transfer_policy": "reasonable",
        "short_transfer_limit": "extra_6",
        "accept_overnight_transfer": "false",
        "accept_self_transfer": "false",
        "time_preference": "unlimited",
        "allow_redeye": "false",
        "arrival_preference": "any",
        "separate_direction_times": "false",
        "outbound_time_preference": "unlimited",
        "outbound_allow_redeye": "false",
        "outbound_arrival_preference": "any",
        "return_time_preference": "unlimited",
        "return_allow_redeye": "false",
        "return_arrival_preference": "any",
        "outbound_departure_time_start": "",
        "outbound_departure_time_end": "",
        "outbound_arrival_time_start": "",
        "outbound_arrival_time_end": "",
        "return_departure_time_start": "",
        "return_departure_time_end": "",
        "return_arrival_time_start": "",
        "return_arrival_time_end": "",
        "departure_slots": (),
        "arrival_slots": (),
        "outbound_departure_slots": (),
        "outbound_arrival_slots": (),
        "return_departure_slots": (),
        "return_arrival_slots": (),
        "departure_time_start": "",
        "departure_time_end": "",
        "arrival_time_start": "",
        "arrival_time_end": "",
        "no_late_arrival": "false",
        "prefer_daytime_arrival": "false",
        "baggage": "required",
        "refund_flexibility": "preferred",
        "price_sensitivity": "low",
        "airline_policy": "any",
        "exclude_airlines": "",
        "blocked_airlines_common": (),
        "lcc_policy": "any",
        "cabin_policy": "economy_only",
        "cabin_arrangement": "economy_all",
        "business_seats": "0",
        "trip_rigidity": "confirmed",
    },
    "notifications": {
        "primary_goal": "buy_timing",
        "notification_method": DEFAULT_NOTIFICATION_METHOD,
        "notification_email": "",
        "notification_frequency": "important_only",
        "notification_frequency_rule": "important_only",
        "price_change_threshold": "down_100",
        "secondary_goals": (),
        "digest_time": "09:00",
        "remember_preferences": "false",
    },
}


VISIBILITY_RULES = (
    {
        "id": "round_trip",
        "when": {
            "any": (
                {"field": "round_trip", "values": ("true",)},
                {"field": "same_day_round_trip", "values": ("true",)},
            )
        },
        "fields": (
            "return_date",
            "return_date_flexibility",
            "return_set_off",
            "return_departure_slots",
            "return_arrival_slots",
            "separate_direction_times",
        ),
    },
    {
        "id": "separate_direction_times",
        "when": {
            "all": (
                {"field": "round_trip", "values": ("true",)},
                {"field": "separate_direction_times", "values": ("true",)},
            )
        },
        "fields": (
            "outbound_time_preference",
            "outbound_allow_redeye",
            "outbound_arrival_preference",
            "outbound_departure_time_start",
            "outbound_departure_time_end",
            "outbound_arrival_time_start",
            "outbound_arrival_time_end",
            "return_time_preference",
            "return_allow_redeye",
            "return_arrival_preference",
            "return_departure_time_start",
            "return_departure_time_end",
            "return_arrival_time_start",
            "return_arrival_time_end",
        ),
    },
    {
        "id": "same_day_round_trip",
        "when": {"all": ({"field": "same_day_round_trip", "values": ("true",)},)},
        "fields": (
            "day_trip_period",
            "business_start",
            "business_end",
            "meeting_start",
            "meeting_end",
            "meeting_location",
            "meeting_importance",
        ),
    },
    {
        "id": "has_child",
        "when": {
            "any": (
                {"field": "child_count", "operator": "gt", "value": 0},
                {"field": "infant_count", "operator": "gt", "value": 0},
                {"field": "travel_scenario", "values": ("family",)},
            )
        },
        "fields": ("child_type",),
    },
    {
        "id": "has_elderly",
        "when": {
            "any": (
                {"field": "elderly_count", "operator": "gt", "value": 0},
                {"field": "travel_scenario", "values": ("elderly",)},
            )
        },
        "fields": ("elderly_condition",),
    },
    {
        "id": "team_building",
        "when": {"all": ({"field": "trip_natures", "values": ("team_building",)},)},
        "fields": (
            "team_passenger_count",
            "team_date_flexibility",
            "same_flight_required",
        ),
    },
    {
        "id": "business_context",
        "when": {
            "all": ({"field": "monitor_mode", "values": ("precise",), "default": "quick"},),
            "any": (
                {"field": "same_day_round_trip", "values": ("true",)},
                {"field": "travel_scenario", "values": ("business",)},
                {
                    "field": "trip_natures",
                    "values": ("business", "meeting", "team_building"),
                },
                {"field": "invoice_context", "values": ("true",)},
                {"field": "invoice_needed", "values": ("true",)},
                {"field": "invoice_special_vat", "values": ("true",)},
                {"field": "invoice_cabin_limit", "values": ("true",)},
            ),
        },
        "fields": (
            "invoice_needed",
            "invoice_special_vat",
            "invoice_cabin_limit",
            "reimburse_per_person",
        ),
    },
    {
        "id": "email",
        "when": {
            "all": (
                {
                    "field": "notification_method",
                    "values": ("email", "both"),
                    "default": "both",
                },
            )
        },
        "fields": ("notification_email",),
    },
    {
        "id": "threshold",
        "when": {
            "all": (
                {"field": "notification_frequency", "values": ("price_change", "threshold")},
            )
        },
        "fields": ("price_change_threshold",),
    },
    {
        "id": "digest",
        "when": {
            "all": (
                {"field": "notification_frequency", "values": ("daily", "daily_digest")},
            )
        },
        "fields": ("digest_time",),
    },
    {
        "id": "exclude_airlines",
        "when": {
            "all": (
                {
                    "field": "airline_policy",
                    "values": ("exclude", "exclude_selected", "exclude_airlines"),
                },
            )
        },
        "fields": ("exclude_airlines", "blocked_airlines_common"),
    },
    {
        "id": "mixed_cabin",
        "when": {
            "all": ({"field": "cabin_arrangement", "values": ("mixed", "mixed_cabin")},)
        },
        "fields": ("business_seats", "economy_seats"),
    },
    {
        "id": "business_cabin",
        "when": {
            "all": (
                {
                    "field": "trip_natures",
                    "values": ("business", "meeting", "team_building"),
                },
            )
        },
        "fields": ("cabin_arrangement", "cabin_policy"),
    },
)


def _values(data, key: str) -> list[str]:
    if hasattr(data, "getlist"):
        return [str(item) for item in data.getlist(key) if item not in (None, "")]
    raw = data.get(key) if isinstance(data, Mapping) else None
    if raw in (None, ""):
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(item) for item in raw if item not in (None, "")]
    return [str(raw)]


def _first(data, key: str, default="") -> str:
    values = _values(data, key)
    return values[0] if values else str(default)


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "checked"}


def _int(data, key: str) -> int:
    try:
        return max(0, int(float(_first(data, key, "0"))))
    except (TypeError, ValueError):
        return 0


def _scenarios(data) -> set[str]:
    return set(_values(data, "travel_scenario"))


def _condition_values(values, clause: dict) -> list[str]:
    current = _values(values, str(clause.get("field") or ""))
    if not current and clause.get("default") not in (None, ""):
        current = [str(clause["default"])]
    return [str(value).strip().lower() for value in current]


def _condition_clause_matches(values, clause: dict) -> bool:
    current = _condition_values(values, clause)
    if clause.get("operator") == "gt":
        try:
            threshold = float(clause.get("value", 0))
            return any(float(value) > threshold for value in current)
        except (TypeError, ValueError):
            return False
    expected = {str(value).strip().lower() for value in clause.get("values") or ()}
    return bool(expected.intersection(current))


def _visibility_rule_matches(values, rule: dict) -> bool:
    when = rule.get("when") or {}
    all_clauses = tuple(when.get("all") or ())
    any_clauses = tuple(when.get("any") or ())
    return all(_condition_clause_matches(values, clause) for clause in all_clauses) and (
        not any_clauses
        or any(_condition_clause_matches(values, clause) for clause in any_clauses)
    )


def visible_field_names(values) -> set[str]:
    """返回当前条件下应提交的字段；隐藏字段由服务端默认规则补齐。"""
    visible = set(FIELD_OWNERS)
    conditional = {field for rule in VISIBILITY_RULES for field in rule["fields"]}
    visible -= conditional
    for rule in VISIBILITY_RULES:
        if _visibility_rule_matches(values, rule):
            visible.update(rule["fields"])
    return visible


def derive_monitor_mode(
    *, advanced_opened: bool = False, stored_mode: str | None = None, editing: bool = False
) -> str:
    """新建按交互深度派生；编辑时优先保持存量模式。"""
    if editing and stored_mode in {"quick", "precise"}:
        return str(stored_mode)
    return "precise" if advanced_opened else "quick"


def _route_summary(values) -> str:
    origin = (
        _first(values, "origin_manual")
        or _first(values, "origin_select")
        or "未选出发地"
    )
    destination = _first(values, "destination") or "未选目的地"
    return f"{origin} → {destination}"


def _when_summary(values) -> str:
    depart = _first(values, "depart_date") or "日期待定"
    if _truthy(_first(values, "same_day_round_trip")):
        return f"{depart} 当天往返"
    if _truthy(_first(values, "round_trip")):
        return f"{depart} 去 · {_first(values, 'return_date') or '返程待定'} 回"
    return f"{depart} 单程"


def _who_summary(values) -> str:
    parts = []
    labels = (
        ("adult_count", "成人"),
        ("child_count", "儿童"),
        ("elderly_count", "老人"),
        ("infant_count", "婴儿"),
    )
    for field, label in labels:
        count = _int(values, field)
        if count:
            parts.append(f"{count}{label}")
    if not parts:
        parts.append(f"{max(1, _int(values, 'passenger_count'))}成人")
    scenarios = _values(values, "travel_scenario")
    return " + ".join(parts) + (f" · {'/'.join(scenarios)}" if scenarios else "")


def _budget_summary(values) -> str:
    max_budget = _first(values, "max_budget")
    target = _first(values, "target_price")
    scope = _first(
        values,
        "max_budget_scope",
        _first(values, "budget_scope", "per_person"),
    )
    scope_label = "全员" if scope in {"all", "total"} else "单人"
    if max_budget:
        text = f"最高¥{max_budget}({scope_label})"
        if target:
            text += f" · 理想¥{target}"
        return text
    return f"自动判断({scope_label}口径)"


def _feasibility_summary(values) -> str:
    parts = []
    outbound = _first(values, "outbound_set_off")
    return_set_off = _first(values, "return_set_off")
    transport = (
        _first(values, "user_transport_min")
        or _first(values, "origin_transport_min")
        or _first(values, "destination_transport_min")
    )
    margin_mode = _first(values, "transport_margin_mode", "standard")
    if outbound:
        parts.append(f"最早{outbound}出门")
    if return_set_off:
        parts.append(f"返程最早{return_set_off}动身")
    if transport:
        parts.append(f"车程{transport}分钟")
    if margin_mode != "standard" or parts:
        parts.append(
            {
                "tight": "紧凑冗余",
                "standard": "标准冗余",
                "conservative": "保守冗余",
            }.get(margin_mode, "自定义冗余")
        )
    return " · ".join(parts) if parts else "已按场景预设"


def _preference_summary(values) -> str:
    transfer = {
        "direct_only": "必须直飞",
        "reasonable": "合理中转",
        "short_ok": "短中转",
        "cheap_ok": "低价可中转",
        "price_first": "价格优先",
    }.get(_first(values, "transfer_policy", "reasonable"), "合理中转")
    time_text = {
        "no_redeye": "不红眼",
        "daytime": "白天优先",
        "unlimited": "时间不限",
        "any": "时间不限",
    }.get(_first(values, "time_preference", "unlimited"), "自定义时间")
    if _truthy(_first(values, "no_late_arrival")):
        arrival = "不深夜到达"
    elif _truthy(_first(values, "prefer_daytime_arrival")):
        arrival = "白天到达"
    else:
        arrival = "到达不限"
    baggage = {
        "required": "需托运",
        "not_needed": "不需托运",
        "unknown": "行李待确认",
    }.get(_first(values, "baggage", "unknown"), "行李待确认")
    return f"{time_text} · {arrival} · {transfer} · {baggage}"


def _notification_summary(values) -> str:
    method = {
        "email": "邮箱",
        "pushplus": "PushPlus",
        "both": "邮箱+PushPlus",
    }.get(
        _first(values, "notification_method", DEFAULT_NOTIFICATION_METHOD),
        "邮箱+PushPlus",
    )
    frequency = {
        "important_only": "重要变化",
        "price_change": "价格变化",
        "daily": "每日摘要",
        "daily_digest": "每日摘要",
    }.get(_first(values, "notification_frequency", "important_only"), "重要变化")
    return f"{method} · {frequency}"


def summarize_optional_sections(values) -> dict[str, str]:
    """生成三个可选细调段的当前值摘要。"""
    return {
        "feasibility": _feasibility_summary(values),
        "flight_preferences": _preference_summary(values),
        "notifications": _notification_summary(values),
    }


def _canonical_optional_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        return tuple(sorted(str(item).strip() for item in value if item not in (None, "")))
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if text.lower() in {"true", "false"}:
        return text.lower()
    return text


def _section_has_non_default(values, section: dict) -> bool:
    if not isinstance(values, Mapping):
        return False
    defaults = OPTIONAL_SECTION_DEFAULTS.get(section["id"], {})
    for field in section["fields"]:
        if field not in values or field not in defaults:
            continue
        current = _canonical_optional_value(values.get(field))
        expected = _canonical_optional_value(defaults[field])
        if current != expected:
            return True
    return False


def edit_expanded_sections(values, *, editing: bool = False) -> list[str]:
    """编辑精确订阅时，只展开含非默认进阶值的段。"""
    if not editing or _first(values, "monitor_mode", "quick") != "precise":
        return []
    return [
        section["id"]
        for section in OPTIONAL_SECTIONS
        if _section_has_non_default(values, section)
    ]


def summarize_stations(values) -> dict[str, str]:
    """用服务端单一函数生成人话站点摘要。"""
    return {
        "where": _route_summary(values),
        "when": _when_summary(values),
        "who": _who_summary(values),
        "budget": _budget_summary(values),
        "flight_preferences": _preference_summary(values),
        "notifications": _notification_summary(values),
    }


def build_default_chips(subscription_with_defaults: dict) -> list[dict]:
    """把 defaults_applied 的实际结果投影为现有控件的芯片视图。"""
    hard = subscription_with_defaults.get("hard_constraints") or {}
    soft = subscription_with_defaults.get("soft_preferences") or {}
    candidates = (
        (
            "time_preference",
            soft.get("time_preference"),
            {"no_redeye": "不红眼", "daytime": "白天出发"},
        ),
        (
            "transfer_policy",
            hard.get("transfer_policy"),
            {
                "direct_only": "尽量直飞",
                "reasonable": "合理中转",
                "short_ok": "短中转",
            },
        ),
        (
            "baggage",
            (
                "required"
                if hard.get("baggage_default") == "prefer_included"
                else hard.get("baggage") or hard.get("need_baggage")
            ),
            {"required": "需托运", "not_needed": "不需托运"},
        ),
        (
            "refund_flexibility",
            soft.get("refund_flexibility"),
            {"preferred": "退改友好", "required": "必须可退改"},
        ),
        (
            "airline_policy",
            soft.get("airline_policy"),
            {"prefer_full_service": "优先全服务航司"},
        ),
    )
    chips = []
    for field, value, labels in candidates:
        value = str(value or "")
        label = labels.get(value)
        if not label:
            continue
        chips.append(
            {
                "field": field,
                "value": value,
                "label": label,
                "selected": True,
                "source": "defaults_applied",
            }
        )
    if (
        hard.get("allow_self_transfer") is False
        or soft.get("allow_self_transfer") is False
    ):
        chips.append(
            {
                "field": "accept_self_transfer",
                "value": "false",
                "label": "不接受非联程",
                "selected": True,
                "source": "defaults_applied",
            }
        )
    return chips


def form_structure_payload(edit_values=None, *, editing: bool = False) -> dict:
    """提供给模板 data-* 属性的声明式结构。"""
    values = edit_values or {}
    summaries = summarize_optional_sections(values)
    expanded = set(edit_expanded_sections(values, editing=editing))
    optional_sections = []
    for section in OPTIONAL_SECTIONS:
        item = dict(section)
        item["summary"] = summaries[section["id"]]
        item["edit_expanded"] = section["id"] in expanded
        optional_sections.append(item)
    return {
        "stations": [dict(station) for station in FORM_STATIONS],
        "required_station_count": REQUIRED_STATION_COUNT,
        "optional_sections": optional_sections,
        "optional_section_map": {item["id"]: item for item in optional_sections},
        "edit_expanded_sections": sorted(expanded),
        "field_owners": FIELD_OWNERS,
        "visibility_rules": [dict(rule) for rule in VISIBILITY_RULES],
        "advanced_fields": sorted(ADVANCED_FIELD_NAMES),
    }


def _first_defined(*values, default=None):
    for value in values:
        if value is not None:
            return value
    return default


def _mapping(value) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _form_scope(value) -> str:
    text = str(value or "per_person").strip().lower().replace("-", "_")
    if text in {
        "all",
        "total",
        "all_passengers",
        "all_passenger",
        "overall",
        "\u6574\u5355",
        "\u5168\u5458",
        "\u5168\u90e8\u4eba",
    }:
        return "all"
    return "per_person"


def _window_bounds(windows) -> tuple[str, str]:
    if not isinstance(windows, (list, tuple)) or not windows:
        return "", ""
    first = windows[0]
    if not isinstance(first, (list, tuple)) or len(first) < 2:
        return "", ""
    return str(first[0] or ""), str(first[1] or "")


def _short_transfer_value(max_extra, max_total) -> str:
    if max_total == 18:
        return "total_18"
    if max_total == 24:
        return "total_24"
    if max_extra == 3:
        return "extra_3"
    return "extra_6"


def subscription_to_form_values(subscription: Mapping | None) -> dict:
    """\u5c06\u89c4\u8303\u5316\u8ba2\u9605\u6295\u5f71\u56de\u73b0\u6709\u8868\u5355\u5b57\u6bb5\uff0c\u4e0d\u5f15\u5165\u7b2c\u4e8c\u4efd\u4e1a\u52a1\u9ed8\u8ba4\u3002"""
    data = _mapping(subscription)
    basic = _mapping(data.get("basic"))
    constraints = _mapping(data.get("constraints"))
    hard = _mapping(data.get("hard_constraints"))
    soft = _mapping(data.get("soft_preferences"))
    preferences = _mapping(data.get("preferences"))
    goals = _mapping(data.get("notification_goals"))
    advanced = _mapping(data.get("advanced_rules"))
    alerts = _mapping(advanced.get("alerts"))
    transfer = _mapping(advanced.get("transfer"))
    airlines = _mapping(advanced.get("airlines"))

    values: dict = {}
    for layer in (data, basic, preferences, soft, constraints, hard):
        for key, value in layer.items():
            if not isinstance(value, Mapping):
                values[key] = value

    origin = str(_first_defined(data.get("origin"), basic.get("origin"), default="") or "")
    origin_type = str(data.get("origin_type") or "")
    values.update(
        {
            "subscription_index": data.get("_index"),
            "type": data.get("type") or "flight",
            "origin": origin,
            "origin_select": origin if origin_type == "city" else "OTHER",
            "origin_manual": "" if origin_type == "city" else origin,
            "origin_airports_active": ",".join(
                data.get("origin_airports_active")
                or basic.get("origin_airports_active")
                or []
            ),
            "destination": _first_defined(data.get("destination"), basic.get("destination"), default=""),
            "destination_airports_active": ",".join(
                data.get("destination_airports_active")
                or basic.get("destination_airports_active")
                or basic.get("dest_airports")
                or []
            ),
            "route_type": _first_defined(data.get("route_type"), basic.get("route_type"), hard.get("route_type"), default="domestic"),
            "round_trip": "true" if bool(data.get("round_trip")) else "false",
            "depart_date": _first_defined(data.get("depart_date"), basic.get("departure_date"), default=""),
            "return_date": _first_defined(data.get("return_date"), basic.get("return_date"), default=""),
            "date_flexibility": _first_defined(data.get("date_flexibility"), hard.get("date_flexibility"), constraints.get("date_flexibility_days"), default=0),
            "return_date_flexibility": _first_defined(data.get("return_date_flexibility"), default=0),
            "monitor_mode": data.get("monitor_mode") or "quick",
            "same_day_round_trip": bool(_first_defined(hard.get("same_day_round_trip"), constraints.get("same_day_round_trip"), soft.get("same_day_round_trip"), default=False)),
            "day_trip_period": _first_defined(hard.get("day_trip_period"), constraints.get("day_trip_period"), default="morning"),
        }
    )

    passengers = _mapping(
        _first_defined(soft.get("passengers"), preferences.get("passengers"), default={})
    )
    passenger_count = _first_defined(
        soft.get("passenger_count"),
        preferences.get("passenger_count"),
        basic.get("passenger_count"),
        default=sum(int(value or 0) for value in passengers.values()) or 1,
    )
    scenarios = (
        soft.get("travel_scenarios")
        or preferences.get("travel_scenarios")
        or soft.get("travel_purposes")
        or preferences.get("travel_purposes")
        or [soft.get("travel_scenario") or preferences.get("travel_scenario") or "personal"]
    )
    if isinstance(scenarios, str):
        scenarios = [item.strip() for item in scenarios.split(",") if item.strip()]
    values.update(
        {
            "passenger_count": passenger_count,
            "adult_count": int(passengers.get("adult") or 0),
            "child_count": int(passengers.get("child") or 0),
            "elderly_count": int(passengers.get("elderly") or 0),
            "infant_count": int(passengers.get("infant") or 0),
            "travel_scenario": list(scenarios),
            "companions": _first_defined(soft.get("companions"), data.get("companions"), default="solo"),
            "companion_constraints": list(soft.get("companion_constraints") or preferences.get("companion_constraints") or []),
            "child_type": _first_defined(soft.get("child_type"), preferences.get("child_type"), default=""),
            "elderly_condition": _first_defined(soft.get("elderly_condition"), preferences.get("elderly_condition"), default=""),
            "solo_travel": bool(_first_defined(soft.get("solo_travel"), preferences.get("solo_travel"), default=False)),
            "no_late_arrival": bool(_first_defined(soft.get("no_late_arrival"), preferences.get("no_late_arrival"), default=False)),
            "prefer_daytime_arrival": bool(_first_defined(soft.get("prefer_daytime_arrival"), preferences.get("prefer_daytime_arrival"), default=False)),
        }
    )

    budget_scope = _form_scope(
        _first_defined(hard.get("max_budget_scope"), data.get("max_budget_scope"), hard.get("budget_scope"), data.get("budget_scope"))
    )
    target_scope = _form_scope(
        _first_defined(hard.get("target_price_scope"), data.get("target_price_scope"), budget_scope)
    )
    tolerance = _first_defined(soft.get("price_tolerance"), default=100)
    tolerance_text = str(tolerance)
    values.update(
        {
            "price_strategy": _first_defined(hard.get("budget_strategy"), constraints.get("budget_strategy"), default="explicit"),
            "budget_strategy": _first_defined(hard.get("budget_strategy"), constraints.get("budget_strategy"), default="explicit"),
            "max_budget": _first_defined(hard.get("max_budget"), data.get("max_budget"), constraints.get("max_price")),
            "max_budget_scope": budget_scope,
            "budget_scope": budget_scope,
            "target_price": _first_defined(hard.get("target_price"), data.get("target_price"), constraints.get("ideal_price")),
            "target_price_scope": target_scope,
            "price_tolerance_mode": tolerance_text if tolerance_text in {"0", "100", "300"} else "custom",
            "price_tolerance_custom": tolerance if tolerance_text not in {"0", "100", "300"} else "",
        }
    )

    time_mode = str(
        _first_defined(
            preferences.get("time_pref"),
            hard.get("time_preference_mode"),
            soft.get("time_preference_mode"),
            hard.get("time_preference"),
            soft.get("time_preference"),
            data.get("departure_time_policy"),
            default="no_redeye",
        )
        or "no_redeye"
    )
    if time_mode == "any":
        time_mode = "unlimited"
    departure_start, departure_end = _window_bounds(
        hard.get("departure_time_windows") or soft.get("departure_time_windows")
    )
    arrival_start, arrival_end = _window_bounds(
        hard.get("arrival_time_windows") or soft.get("arrival_time_windows")
    )
    values.update(
        {
            "transfer_policy": _first_defined(hard.get("transfer_policy"), data.get("transfer_policy"), default="reasonable"),
            "short_transfer_limit": _short_transfer_value(
                _first_defined(hard.get("max_extra_duration_hours"), transfer.get("max_extra_duration_hours")),
                _first_defined(hard.get("max_total_duration_hours"), transfer.get("max_total_duration_hours")),
            ),
            "accept_overnight_transfer": "true" if bool(_first_defined(hard.get("accept_overnight_transfer"), soft.get("allow_overnight_transfer"), default=False)) else "false",
            "accept_self_transfer": "true" if bool(_first_defined(hard.get("accept_self_transfer"), soft.get("allow_self_transfer"), default=False)) else "false",
            "time_preference": time_mode,
            "departure_time_policy": _first_defined(hard.get("departure_time_policy"), data.get("departure_time_policy"), default="no_redeye"),
            "departure_slots": list(hard.get("departure_slots") or data.get("departure_slots") or []),
            "arrival_slots": list(hard.get("arrival_slots") or data.get("arrival_slots") or []),
            "outbound_departure_slots": list(hard.get("outbound_departure_slots") or data.get("outbound_departure_slots") or []),
            "outbound_arrival_slots": list(hard.get("outbound_arrival_slots") or data.get("outbound_arrival_slots") or []),
            "return_departure_slots": list(hard.get("return_departure_slots") or data.get("return_departure_slots") or []),
            "return_arrival_slots": list(hard.get("return_arrival_slots") or data.get("return_arrival_slots") or []),
            "departure_time_start": departure_start,
            "departure_time_end": departure_end,
            "arrival_time_start": arrival_start,
            "arrival_time_end": arrival_end,
            "arrival_time_policy": _first_defined(
                data.get("arrival_time_policy"),
                hard.get("arrival_time_policy"),
                default="any",
            ),
            "baggage": _first_defined(hard.get("baggage"), data.get("need_baggage"), default="unknown"),
            "refund_flexibility": _first_defined(soft.get("refund_flexibility"), data.get("refund_flexibility"), preferences.get("refund_policy"), default="preferred"),
            "price_sensitivity": _first_defined(soft.get("price_sensitivity"), data.get("price_sensitivity"), default="low"),
            "airline_policy": _first_defined(data.get("airline_policy"), airlines.get("preference"), soft.get("airline_policy"), default="any"),
            "exclude_airlines": ", ".join(soft.get("exclude_airlines") or data.get("exclude_airlines") or airlines.get("blocked") or []),
            "blocked_airlines_common": list(soft.get("exclude_airlines") or data.get("exclude_airlines") or airlines.get("blocked") or []),
            "lcc_policy": _first_defined(hard.get("lcc_policy"), data.get("lcc_policy"), airlines.get("lcc_policy"), default="any"),
            "trip_rigidity": _first_defined(soft.get("trip_rigidity"), data.get("trip_rigidity"), default="confirmed"),
        }
    )

    trip_natures = hard.get("trip_natures") or constraints.get("trip_natures") or soft.get("trip_natures") or []
    if isinstance(trip_natures, str):
        trip_natures = [trip_natures]
    values.update(
        {
            "trip_natures": list(trip_natures),
            "trip_nature": _first_defined(hard.get("trip_nature"), constraints.get("trip_nature"), default=""),
            "business_start": _first_defined(hard.get("business_start"), constraints.get("business_start"), default=""),
            "business_end": _first_defined(hard.get("business_end"), constraints.get("business_end"), default=""),
            "meeting_start": _first_defined(hard.get("meeting_start"), constraints.get("meeting_start"), default=""),
            "meeting_end": _first_defined(hard.get("meeting_end"), constraints.get("meeting_end"), default=""),
            "meeting_location": _first_defined(hard.get("meeting_location"), constraints.get("meeting_location"), default=""),
            "meeting_importance": _first_defined(hard.get("meeting_importance"), constraints.get("meeting_importance"), default="important"),
            "outbound_set_off": _first_defined(hard.get("outbound_set_off"), constraints.get("outbound_set_off"), default=""),
            "return_set_off": _first_defined(hard.get("return_set_off"), constraints.get("return_set_off"), default=""),
            "user_transport_min": _first_defined(hard.get("user_transport_min"), constraints.get("user_transport_min")),
            "origin_transport_min": _first_defined(hard.get("origin_transport_min"), constraints.get("origin_transport_min")),
            "destination_transport_min": _first_defined(hard.get("destination_transport_min"), constraints.get("destination_transport_min")),
            "transport_margin_mode": _first_defined(hard.get("transport_margin_mode"), constraints.get("transport_margin_mode"), default="standard"),
            "redundancy_min": _first_defined(hard.get("redundancy_min"), constraints.get("redundancy_min"), default=25),
            "airport_advance_min": _first_defined(hard.get("airport_advance_min"), constraints.get("airport_advance_min")),
            "arrival_exit_min": _first_defined(hard.get("arrival_exit_min"), constraints.get("arrival_exit_min")),
            "delay_buffer_min": _first_defined(hard.get("delay_buffer_min"), constraints.get("delay_buffer_min")),
            "pre_meeting_buffer_min": _first_defined(hard.get("pre_meeting_buffer_min"), constraints.get("pre_meeting_buffer_min")),
            "post_meeting_buffer_min": _first_defined(hard.get("post_meeting_buffer_min"), constraints.get("post_meeting_buffer_min")),
            "custom_redundancy_min": _first_defined(hard.get("custom_redundancy_min"), constraints.get("custom_redundancy_min")),
            "team_passenger_count": _first_defined(hard.get("team_passenger_count"), constraints.get("team_passenger_count")),
            "team_date_flexibility": _first_defined(hard.get("team_date_flexibility"), constraints.get("team_date_flexibility"), default="fixed"),
            "same_flight_required": "true" if bool(_first_defined(hard.get("same_flight_required"), constraints.get("same_flight_required"), default=False)) else "false",
            "cabin_arrangement": _first_defined(hard.get("cabin_arrangement"), constraints.get("cabin_arrangement"), default="economy_all"),
            "cabin_policy": _first_defined(hard.get("cabin_policy"), constraints.get("cabin_policy"), default="economy_only"),
            "user_level": _first_defined(hard.get("user_level"), constraints.get("user_level"), default="staff"),
            "business_seats": _first_defined(hard.get("business_seats"), constraints.get("business_seats"), default=0),
            "economy_seats": _first_defined(hard.get("economy_seats"), constraints.get("economy_seats"), default=passenger_count),
            "reimburse_per_person": _first_defined(hard.get("reimburse_per_person"), constraints.get("reimburse_per_person")),
        }
    )

    invoice_needed = bool(_first_defined(soft.get("invoice_needed"), preferences.get("invoice_needed"), default=False))
    invoice_special = bool(_first_defined(soft.get("invoice_special_vat"), preferences.get("invoice_special_vat"), default=False))
    invoice_limit = bool(_first_defined(soft.get("invoice_cabin_limit"), preferences.get("invoice_cabin_limit"), default=False))
    frequency = _first_defined(goals.get("frequency"), alerts.get("frequency"), default="important_only")
    values.update(
        {
            "invoice_context": invoice_needed or invoice_special or invoice_limit,
            "invoice_needed": invoice_needed,
            "invoice_special_vat": invoice_special,
            "invoice_cabin_limit": invoice_limit,
            "primary_goal": goals.get("primary") or "buy_timing",
            "secondary_goals": list(goals.get("secondary") or alerts.get("types") or []),
            "notification_method": goals.get("method") or DEFAULT_NOTIFICATION_METHOD,
            "notification_email": goals.get("email") or "",
            "notification_frequency": frequency,
            "notification_frequency_rule": frequency,
            "price_change_threshold": _first_defined(goals.get("price_change_threshold"), alerts.get("price_change_threshold"), default="down_100"),
            "digest_time": _first_defined(goals.get("digest_time"), alerts.get("digest_time"), default="09:00"),
        }
    )
    original_departure_time_policy = values.get("departure_time_policy") or ""
    original_arrival_time_policy = values.get("arrival_time_policy") or ""
    legacy_time_values = {
        "departure_slots": values.get("departure_slots"),
        "arrival_slots": values.get("arrival_slots"),
        "outbound_departure_slots": values.get("outbound_departure_slots"),
        "outbound_arrival_slots": values.get("outbound_arrival_slots"),
        "return_departure_slots": values.get("return_departure_slots"),
        "return_arrival_slots": values.get("return_arrival_slots"),
        "departure_time_windows": hard.get("departure_time_windows") or soft.get("departure_time_windows"),
        "arrival_time_windows": hard.get("arrival_time_windows") or soft.get("arrival_time_windows"),
        "outbound_departure_time_windows": hard.get("outbound_departure_time_windows") or soft.get("outbound_departure_time_windows"),
        "outbound_arrival_time_windows": hard.get("outbound_arrival_time_windows") or soft.get("outbound_arrival_time_windows"),
        "return_departure_time_windows": hard.get("return_departure_time_windows") or soft.get("return_departure_time_windows"),
        "return_arrival_time_windows": hard.get("return_arrival_time_windows") or soft.get("return_arrival_time_windows"),
        "no_late_arrival": values.get("no_late_arrival"),
        "prefer_daytime_arrival": values.get("prefer_daytime_arrival"),
    }
    values.update(
        project_time_concept_fields(
            legacy_time_values,
            round_trip=bool(data.get("round_trip")),
        )
    )
    values.update(
        {
            "ux2_time_touched": "false",
            "ux2_original_departure_time_policy": original_departure_time_policy,
            "ux2_original_arrival_time_policy": original_arrival_time_policy,
        }
    )
    return values
