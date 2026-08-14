"""UX 3.0 双页表单的静态结构与渲染上下文。"""

from __future__ import annotations

from collections.abc import Mapping

from form_concepts import (
    BUSINESS_SCENARIO_CONCEPTS,
    CANONICAL_TIME_WINDOW_FIELDS,
    CONCEPTS,
)
from form_structure import FORM_STATIONS, summarize_stations
from notification_config import DEFAULT_NOTIFICATION_METHOD


SECTION_IDS = (
    "where",
    "when",
    "who",
    "budget",
    "flight_preferences",
    "notifications",
)

SECTION_TITLES = {
    "where": "去哪",
    "when": "什么时候",
    "who": "谁去",
    "budget": "预算",
    "flight_preferences": "飞行偏好",
    "notifications": "怎么提醒",
}

SECONDARY_GROUP_DEFINITIONS = (
    {
        "id": "business-travel",
        "title": "商务出行",
        "description": "商务类型、会议、团队、报销与发票设置；非商务行程可保持关闭。",
        "after_section": "who",
        "after_concept": "travel_context",
        "concept_names": (
            "business_nature",
            "business_level",
            "team_arrangement",
            "reimbursement",
            "invoice",
            "same_day_round_trip",
            "meeting_window",
            "meeting_location",
            "meeting_importance",
            "same_day_execution",
        ),
    },
    {
        "id": "feasibility",
        "title": "可行性参数",
        "description": "动身时间、机场交通与冗余；未填写时沿用系统估算。",
        "after_section": "who",
        "concept_names": (
            "set_off_times",
            "transport_estimates",
            "transport_margin",
            "reserve_overrides",
        ),
    },
)
_BUSINESS_GROUP_CONCEPTS = frozenset(
    next(
        group["concept_names"]
        for group in SECONDARY_GROUP_DEFINITIONS
        if group["id"] == "business-travel"
    )
)
if _BUSINESS_GROUP_CONCEPTS != BUSINESS_SCENARIO_CONCEPTS:
    raise ValueError("商务场景范围与页面分组不一致")

SECONDARY_CONCEPT_NAMES = frozenset(
    concept_name
    for group in SECONDARY_GROUP_DEFINITIONS
    for concept_name in group["concept_names"]
)

VISIBILITY_CONTRACTS = frozenset({"passenger-profile", "notification-email", "business-scenario"})

ORIGIN_OPTIONS = (
    ("上海", "上海（浦东PVG + 虹桥SHA）"),
    ("北京", "北京（首都PEK + 大兴PKX）"),
    ("广州", "广州（白云CAN）"),
    ("深圳", "深圳（宝安SZX）"),
    ("成都", "成都（天府CTU）"),
    ("杭州", "杭州（萧山HGH）"),
    ("南京", "南京（禄口NKG）"),
    ("OTHER", "其他（手动输入）"),
)


ROUTE_TYPE_LABELS = {
    "domestic": "国内",
    "greater_china": "港澳台",
    "international": "国际",
}

OPTIONS = {
    "route_type": tuple(ROUTE_TYPE_LABELS.items()),
    "round_trip": (("false", "单程"), ("true", "往返")),
    "date_flexibility": (("0", "就这天"), ("1", "前后1天"), ("3", "前后3天"), ("7", "前后一周")),
    "return_date_flexibility": (("0", "就这天"), ("1", "前后1天"), ("3", "前后3天"), ("7", "前后一周")),
    "day_trip_period": (("morning", "上午办事"), ("afternoon", "下午办事"), ("full_day", "全天安排")),
    "meeting_importance": (("normal", "普通商务"), ("important", "重要会议"), ("critical", "不可迟到")),
    "travel_scenario": (("personal", "个人出行"), ("business", "商务/会议"), ("tourism", "旅游"), ("family_visit", "探亲/回家"), ("family", "家庭/亲子"), ("elderly", "有老人同行"), ("important", "重要事项"), ("price_first", "价格优先")),
    "trip_natures": (("business", "商务出行"), ("meeting", "参加会议"), ("team_building", "团队出行")),
    "user_level": (("staff", "普通员工"), ("manager", "经理"), ("executive", "高管"), ("vip", "重要嘉宾")),
    "child_type": (("", "不补充"), ("infant", "婴儿"), ("preschool", "学龄前"), ("school_age", "学龄儿童")),
    "elderly_condition": (("normal", "普通"), ("mobility_limited", "不适合长时间步行/换乘"), ("no_early_late", "不适合红眼或早班")),
    "companion_constraints": (("direct_preferred", "尽量直飞"), ("no_redeye", "不接受红眼"), ("avoid_long_layover", "避免长中转"), ("need_baggage", "需要托运行李"), ("need_refund_change", "需要可退改"), ("daytime_arrival", "希望白天到达"), ("limited_mobility", "行动不便")),
    "transport_margin_mode": (("tight", "紧凑"), ("standard", "标准"), ("loose", "保守")),
    "transport_mode": (("", "系统自动估算"), ("taxi", "驾车/出租车"), ("transit", "公共交通")),
    "price_strategy": (("explicit", "填写具体金额"), ("auto_judge", "由系统判断"), ("low_price_alert", "进入低价区时提醒")),
    "max_budget_scope": (("per_person", "单人"), ("total", "全员")),
    "target_price_scope": (("per_person", "单人"), ("total", "全员")),
    "transfer_policy": (("direct_only", "必须直飞"), ("reasonable", "合理中转"), ("price_first", "价格优先")),
    "short_transfer_limit": (("extra_3", "最多多3小时"), ("extra_6", "最多多6小时"), ("total_12", "总时长不超12小时"), ("total_18", "总时长不超18小时")),
    "time_preference": (("unlimited", "不限"), ("daytime", "白天优先")),
    "outbound_time_preference": (("unlimited", "不限"), ("daytime", "白天出发"), ("custom", "自定义")),
    "return_time_preference": (("unlimited", "不限"), ("daytime", "白天出发"), ("custom", "自定义")),
    "arrival_preference": (("any", "不限"), ("daytime", "白天到达"), ("no_late", "避免深夜")),
    "outbound_arrival_preference": (("any", "不限"), ("daytime", "白天到达"), ("no_late", "避免深夜"), ("custom", "自定义")),
    "return_arrival_preference": (("any", "不限"), ("daytime", "白天到达"), ("no_late", "避免深夜"), ("custom", "自定义")),
    "baggage": (("required", "必须含托运"), ("not_needed", "不需要托运"), ("unknown", "不确定")),
    "refund_flexibility": (("not_needed", "不重要"), ("preferred", "最好能改签"), ("required", "必须可退改"), ("unknown", "不确定")),
    "price_sensitivity": (("low", "稳定优先"), ("medium", "适度看价格"), ("high", "明显低价可妥协"), ("max", "价格优先")),
    "airline_policy": (("any", "不限"), ("prefer_full_service", "偏好全服务"), ("no_lcc", "不接受廉航"), ("exclude_airlines", "排除指定航司")),
    "blocked_airlines_common": (("9C", "春秋航空"), ("HO", "吉祥航空"), ("KN", "中国联航"), ("AQ", "九元航空")),
    "lcc_policy": (("any", "不限"), ("exclude_lcc", "排除廉航"), ("lcc_only", "仅看廉航")),
    "cabin_policy": (("economy_only", "仅经济舱"), ("premium_allowed", "可含高端经济舱"), ("business_allowed", "可含商务舱")),
    "cabin_arrangement": (("economy_all", "全员经济舱"), ("business_all", "全员商务舱"), ("mixed", "混合舱位")),
    "primary_goal": (("price_drop_alert", "合适价格提醒"), ("buy_timing", "判断购买时机"), ("cheaper_date", "找更便宜日期"), ("best_overall", "找综合合适方案")),
    "notification_method": (("email", "邮件"), ("pushplus", "微信 PushPlus"), ("both", "两者")),
    "notification_frequency": (("important_only", "仅重要变化"), ("daily_digest", "每日摘要"), ("price_change", "每次价格变化")),
    "price_change_threshold": (("50", "变化50元"), ("100", "变化100元"), ("200", "变化200元"), ("500", "变化500元")),
    "secondary_goals": (("low_price_alert", "异常低价"), ("price_risk_alert", "涨价风险"), ("cheaper_date", "邻近日期更便宜"), ("better_same_day", "同日更优方案")),
    "digest_time": (("08:00", "08:00"), ("12:00", "12:00"), ("20:00", "20:00")),
    "team_date_flexibility": (("fixed", "固定日期"), ("flexible", "日期可协调")),
}


MULTI_FIELDS = frozenset(
    {"travel_scenario", "trip_natures", "companion_constraints", "blocked_airlines_common", "secondary_goals"}
)

BOOLEAN_FIELDS = frozenset(
    {
        "same_day_round_trip",
        "solo_travel",
        "same_flight_required",
        "invoice_needed",
        "invoice_context",
        "invoice_special_vat",
        "invoice_cabin_limit",
        "accept_overnight_transfer",
        "accept_self_transfer",
        "allow_redeye",
        "separate_direction_times",
        "outbound_allow_redeye",
        "return_allow_redeye",
        "remember_preferences",
    }
)

HIDDEN_FIELDS = frozenset(
    {
        "subscription_index",
        "monitor_mode",
        "route_type",
        "origin_airports_active",
        "destination_airports_active",
        "max_budget_mode",
        "budget_scope",
        "target_price_mode",
        "price_tolerance_mode",
        "price_tolerance_custom",
        "ux2_concept_form",
        "ux2_time_touched",
        "ux2_original_departure_time_policy",
        "ux2_original_arrival_time_policy",
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
        "no_late_arrival",
        "prefer_daytime_arrival",
        "companions",
        "passenger_count",
        "trip_rigidity",
        "notification_frequency_rule",
    }
)

DATE_FIELDS = frozenset({"depart_date", "return_date"})
TIME_FIELDS = frozenset(
    {
        "business_start", "business_end", "meeting_start", "meeting_end",
        "outbound_set_off", "return_set_off", "departure_time_start",
        "departure_time_end", "arrival_time_start", "arrival_time_end",
        "outbound_departure_time_start", "outbound_departure_time_end",
        "outbound_arrival_time_start", "outbound_arrival_time_end",
        "return_departure_time_start", "return_departure_time_end",
        "return_arrival_time_start", "return_arrival_time_end",
        *CANONICAL_TIME_WINDOW_FIELDS,
    }
)
NUMBER_FIELDS = frozenset(
    {
        "buffer_hours", "user_transport_min", "origin_transport_min", "destination_transport_min",
        "redundancy_min", "airport_advance_min", "arrival_exit_min",
        "delay_buffer_min", "pre_meeting_buffer_min", "post_meeting_buffer_min",
        "custom_redundancy_min", "team_passenger_count", "max_budget",
        "target_price", "reimburse_per_person", "business_seats", "economy_seats",
        "adult_count", "child_count", "elderly_count", "infant_count",
    }
)

LABELS = {
    "route_type": "航线类型",
    "origin_select": "出发地",
    "origin_manual": "其他出发地",
    "destination": "目的地",
    "depart_date": "出发日期",
    "date_flexibility": "出发日期弹性",
    "round_trip": "行程类型",
    "return_date": "返程日期（单程可留空）",
    "return_date_flexibility": "返程日期弹性",
    "same_day_round_trip": "当天往返",
    "day_trip_period": "当天安排",
    "business_start": "办事/会议开始",
    "business_end": "办事/会议结束",
    "meeting_start": "会议开始（兼容字段）",
    "meeting_end": "会议结束（兼容字段）",
    "meeting_location": "会议地点/区域",
    "meeting_importance": "会议重要程度",
    "buffer_hours": "会议安全缓冲（小时）",
    "transport_mode": "机场到会场交通方式",
    "travel_scenario": "出行场景",
    "trip_natures": "商务类型",
    "user_level": "出行层级",
    "adult_count": "成人",
    "child_count": "儿童",
    "elderly_count": "老人",
    "infant_count": "婴儿",
    "child_type": "儿童情况",
    "elderly_condition": "老人情况",
    "companion_constraints": "同行约束",
    "outbound_set_off": "去程最早可出门",
    "return_set_off": "返程最早可动身",
    "user_transport_min": "机场到会场车程（分钟）",
    "origin_transport_min": "出发地到机场（分钟）",
    "destination_transport_min": "机场到目的地（分钟）",
    "transport_margin_mode": "交通冗余模式",
    "redundancy_min": "自定义冗余（分钟）",
    "airport_advance_min": "机场提前量（分钟）",
    "arrival_exit_min": "落地离场（分钟）",
    "delay_buffer_min": "延误冗余（分钟）",
    "pre_meeting_buffer_min": "会前准备（分钟）",
    "post_meeting_buffer_min": "会后缓冲（分钟）",
    "custom_redundancy_min": "额外冗余（分钟）",
    "team_passenger_count": "团队人数",
    "team_date_flexibility": "团队日期弹性",
    "same_flight_required": "团队必须同航班",
    "price_strategy": "预算方式",
    "max_budget": "最高可接受价",
    "max_budget_scope": "最高价口径",
    "target_price": "理想入手价",
    "target_price_scope": "理想价口径",
    "reimburse_per_person": "人均报销上限",
    "invoice_needed": "需要发票",
    "invoice_context": "这是报销行程",
    "invoice_special_vat": "需要增值税专票",
    "invoice_cabin_limit": "舱位受报销限制",
    "transfer_policy": "直飞与中转",
    "short_transfer_limit": "中转时长上限",
    "accept_overnight_transfer": "接受过夜中转",
    "accept_self_transfer": "接受非联程自行中转",
    "time_preference": "出发时段",
    "allow_redeye": "接受红眼",
    "arrival_preference": "到达偏好",
    "shared_departure_window_start": "通用出发不早于",
    "shared_departure_window_end": "通用出发不晚于",
    "shared_arrival_window_start": "通用到达不早于",
    "shared_arrival_window_end": "通用到达不晚于",
    "outbound_departure_window_start": "去程出发不早于",
    "outbound_departure_window_end": "去程出发不晚于",
    "outbound_arrival_window_start": "去程到达不早于",
    "outbound_arrival_window_end": "去程到达不晚于",
    "return_departure_window_start": "返程出发不早于",
    "return_departure_window_end": "返程出发不晚于",
    "return_arrival_window_start": "返程到达不早于",
    "return_arrival_window_end": "返程到达不晚于",
    "separate_direction_times": "去返分别设置",
    "outbound_time_preference": "去程出发时段",
    "outbound_allow_redeye": "去程接受红眼",
    "outbound_arrival_preference": "去程到达偏好",
    "return_time_preference": "返程出发时段",
    "return_allow_redeye": "返程接受红眼",
    "return_arrival_preference": "返程到达偏好",
    "baggage": "托运行李",
    "refund_flexibility": "退改要求",
    "price_sensitivity": "价格敏感度",
    "airline_policy": "航司偏好",
    "exclude_airlines": "排除航司代码（逗号分隔）",
    "blocked_airlines_common": "常见排除航司",
    "lcc_policy": "廉价航空",
    "cabin_policy": "允许舱位",
    "cabin_arrangement": "团队舱位安排",
    "business_seats": "商务舱人数",
    "economy_seats": "经济舱人数",
    "primary_goal": "提醒主目标",
    "notification_method": "提醒方式",
    "notification_email": "接收邮箱",
    "notification_frequency": "提醒频率",
    "price_change_threshold": "价格变化阈值",
    "secondary_goals": "附加提醒",
    "digest_time": "摘要时间",
    "remember_preferences": "记住本次偏好",
}

DEFAULTS = {
    "route_type": "domestic",
    "origin_select": "上海",
    "round_trip": "true",
    "date_flexibility": "0",
    "return_date_flexibility": "0",
    "day_trip_period": "morning",
    "meeting_importance": "important",
    "buffer_hours": "",
    "transport_mode": "",
    "travel_scenario": ["personal"],
    "user_level": "staff",
    "passenger_count": 1,
    "adult_count": 1,
    "child_count": 0,
    "elderly_count": 0,
    "infant_count": 0,
    "companions": "solo",
    "transport_margin_mode": "standard",
    "redundancy_min": 25,
    "price_strategy": "explicit",
    "max_budget_scope": "per_person",
    "target_price_scope": "per_person",
    "max_budget_mode": "fixed",
    "target_price_mode": "fixed",
    "budget_scope": "per_person",
    "price_tolerance_mode": "percent",
    "price_tolerance_custom": "10",
    "monitor_mode": "precise",
    "ux2_concept_form": "true",
    "ux2_time_touched": "false",
    "transfer_policy": "reasonable",
    "short_transfer_limit": "extra_6",
    "time_preference": "unlimited",
    "allow_redeye": "false",
    "arrival_preference": "any",
    "shared_departure_window_start": "",
    "shared_departure_window_end": "",
    "shared_arrival_window_start": "",
    "shared_arrival_window_end": "",
    "outbound_departure_window_start": "",
    "outbound_departure_window_end": "",
    "outbound_arrival_window_start": "",
    "outbound_arrival_window_end": "",
    "return_departure_window_start": "",
    "return_departure_window_end": "",
    "return_arrival_window_start": "",
    "return_arrival_window_end": "",
    "separate_direction_times": "false",
    "outbound_time_preference": "unlimited",
    "outbound_allow_redeye": "false",
    "outbound_arrival_preference": "any",
    "return_time_preference": "unlimited",
    "return_allow_redeye": "false",
    "return_arrival_preference": "any",
    "baggage": "unknown",
    "refund_flexibility": "preferred",
    "price_sensitivity": "low",
    "airline_policy": "any",
    "lcc_policy": "any",
    "cabin_policy": "economy_only",
    "cabin_arrangement": "economy_all",
    "trip_rigidity": "confirmed",
    "primary_goal": "buy_timing",
    "notification_method": DEFAULT_NOTIFICATION_METHOD,
    "notification_frequency": "important_only",
    "notification_frequency_rule": "important_only",
    "price_change_threshold": "100",
    "digest_time": "20:00",
}

QUICK_GROUPS = (
    {"id": "route", "title": "出发与目的地", "fields": ("origin_select", "origin_airports_active", "destination", "destination_airports_active", "route_type")},
    {"id": "dates", "title": "日期与往返", "fields": ("depart_date", "round_trip", "return_date")},
    {"id": "passengers", "title": "乘客构成", "fields": ("passenger_count",)},
    {"id": "budget", "title": "预算", "fields": ("max_budget", "max_budget_scope", "target_price", "target_price_scope")},
    {"id": "scenario", "title": "出行场景与同行要求", "fields": ("travel_scenario", "companion_constraints")},
)
QUICK_CANONICAL_INPUT_NAMES = (
    "form_page",
    "monitor_mode",
    "subscription_index",
    *tuple(field for group in QUICK_GROUPS for field in group["fields"]),
)


def _first(values: Mapping, name: str, default=None):
    value = values.get(name, default)
    if value in (None, ""):
        return default
    return value


def _as_list(value) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part) for part in value]
    return []


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "checked"}


def _field_type(name: str) -> str:
    if name == "time_preference":
        return "radio"
    if name in HIDDEN_FIELDS:
        return "hidden"
    if name in MULTI_FIELDS:
        return "multi"
    if name in BOOLEAN_FIELDS:
        return "checkbox"
    if name in OPTIONS or name == "origin_select":
        return "select"
    if name in DATE_FIELDS:
        return "date"
    if name in TIME_FIELDS:
        return "time"
    if name in NUMBER_FIELDS:
        return "number"
    if name == "notification_email":
        return "email"
    return "text"


def _field_spec(name: str, values: Mapping, *, page_mode: str) -> dict:
    default = DEFAULTS.get(name, "")
    value = _first(values, name, default)
    if name == "monitor_mode":
        value = _first(values, name, "quick" if page_mode == "quick" else "precise")
        if page_mode == "quick":
            value = "quick"
    if name == "subscription_index":
        value = _first(values, name, "")
    options = list(ORIGIN_OPTIONS if name == "origin_select" else OPTIONS.get(name, ()))
    if page_mode == "quick" and name == "origin_select":
        field_type = "text"
    elif page_mode == "quick" and name == "passenger_count":
        field_type = "number"
    else:
        field_type = _field_type(name)
    selected_values = _as_list(value) if field_type == "multi" else []
    selected_candidates = selected_values if field_type == "multi" else [str(value)]
    known_values = {str(option_value) for option_value, _ in options}
    legacy_labels = {"page_only": "微信 PushPlus（存量值）"}
    for selected_value in selected_candidates:
        if selected_value and selected_value not in known_values:
            options.append(
                (
                    selected_value,
                    legacy_labels.get(selected_value, f"保留现有值（{selected_value}）"),
                )
            )
            known_values.add(selected_value)
    return {
        "name": name,
        "id": f"field-{name.replace('_', '-')}",
        "label": LABELS.get(name, name.replace("_", " ")),
        "type": field_type,
        "value": "" if value is None else str(value),
        "checked": _truthy(value),
        "options": [
            {
                "value": str(option_value),
                "label": option_label,
                "selected": (
                    str(option_value) in selected_values
                    if field_type == "multi"
                    else str(option_value) == str(value)
                ),
            }
            for option_value, option_label in options
        ],
        "required": name in {"origin_select", "destination", "depart_date"},
        "min": 0 if name in NUMBER_FIELDS else None,
        "visibility": (
            "notification-email"
            if name == "notification_email"
            else "passenger-profile"
            if name in {"child_type", "elderly_condition"}
            else ""
        ),
    }


def _concept_spec(concept_name: str, concept: Mapping, values: Mapping) -> dict:
    fields = [_field_spec(name, values, page_mode="full") for name in concept["fields"]]
    visible = [field for field in fields if field["type"] != "hidden"]
    hidden = [field for field in fields if field["type"] == "hidden"]
    result = {
        "name": concept_name,
        "title": concept["canonical_control"] if visible else "",
        "fields": visible,
    }
    if concept_name == "time":
        fields_by_name = {field["name"]: field for field in visible}
        common_names = CANONICAL_TIME_WINDOW_FIELDS[:4]
        directional_names = CANONICAL_TIME_WINDOW_FIELDS[4:]
        result["fields"] = [
            fields_by_name[name]
            for name in ("time_preference", "allow_redeye", "arrival_preference")
        ]
        result["custom_window_fields"] = [
            fields_by_name[name] for name in common_names
        ]
        result["directional_window_fields"] = [
            fields_by_name[name] for name in directional_names
        ]
        result["custom_window_open"] = any(
            field["value"].strip() for field in result["custom_window_fields"]
        )
        result["directional_window_open"] = any(
            field["value"].strip() for field in result["directional_window_fields"]
        )
    if hidden:
        result["hidden_fields"] = hidden
    return result


def _normalized_group_value(name: str, value):
    if name in BOOLEAN_FIELDS:
        return _truthy(value)
    if name in MULTI_FIELDS:
        return tuple(sorted(_as_list(value)))
    return str(value if value is not None else "").strip()


def _secondary_group_has_nondefault(group: Mapping, values: Mapping) -> bool:
    for concept_name in group["concept_names"]:
        for name in CONCEPTS[concept_name]["canonical_input_names"]:
            actual = values.get(name, DEFAULTS.get(name, ""))
            default = DEFAULTS.get(name, False if name in BOOLEAN_FIELDS else "")
            if _normalized_group_value(name, actual) != _normalized_group_value(name, default):
                return True
    return False


def _full_sections(values: Mapping) -> list[dict]:
    sections = []
    for section_id in SECTION_IDS:
        concepts = []
        for concept_name, concept in CONCEPTS.items():
            if concept["station_id"] != section_id or concept_name in SECONDARY_CONCEPT_NAMES:
                continue
            concepts.append(_concept_spec(concept_name, concept, values))
        sections.append(
            {
                "id": section_id,
                "html_id": section_id.replace("_", "-"),
                "title": SECTION_TITLES[section_id],
                "summary": summarize_stations(values).get(section_id, "尚未填写"),
                "concepts": concepts,
            }
        )
    return sections


def _secondary_groups(values: Mapping, *, editing: bool) -> list[dict]:
    scenarios = set(_as_list(values.get("travel_scenario")))
    groups = []
    for group in SECONDARY_GROUP_DEFINITIONS:
        is_business = group["id"] == "business-travel"
        parent_selected = is_business and "business" in scenarios
        has_nondefault = _secondary_group_has_nondefault(group, values)
        visible = not is_business or parent_selected or (editing and has_nondefault)
        groups.append(
            {
                **group,
                "concepts": [
                    _concept_spec(concept_name, CONCEPTS[concept_name], values)
                    for concept_name in group["concept_names"]
                ],
                "visibility": "business-scenario" if is_business else "",
                "hidden": not visible,
                "open": editing and (parent_selected or has_nondefault),
            }
        )
    return groups


def _quick_groups(values: Mapping) -> list[dict]:
    groups = []
    for group in QUICK_GROUPS:
        fields = [_field_spec(name, values, page_mode="quick") for name in group["fields"]]
        groups.append({**group, "fields": fields})
    return groups


def build_form_page_context(page_mode: str, values=None, *, edit_index=None) -> dict:
    """构建双页唯一渲染上下文，不在模板中派生业务默认。"""
    if page_mode not in {"quick", "full"}:
        raise ValueError(f"未知表单页面: {page_mode}")
    data = dict(values or {})
    if edit_index is not None:
        data["subscription_index"] = edit_index
    if page_mode == "quick":
        data["monitor_mode"] = "quick"
    elif edit_index is None:
        data["monitor_mode"] = "precise"
    route_type = str(data.get("route_type") or "").strip()
    route_type_badge = {"code": route_type, "label": ROUTE_TYPE_LABELS.get(route_type, "待识别")}
    return {
        "mode": page_mode,
        "title": "快速创建监控" if page_mode == "quick" else "完整设置",
        "groups": _quick_groups(data) if page_mode == "quick" else [],
        "sections": _full_sections(data) if page_mode == "full" else [],
        "secondary_groups": (
            _secondary_groups(data, editing=edit_index is not None)
            if page_mode == "full"
            else []
        ),
        "monitor_mode": str(data.get("monitor_mode") or ("quick" if page_mode == "quick" else "precise")),
        "subscription_index": data.get("subscription_index", ""),
        "values": data,
        "route_type_badge": route_type_badge,
    }


def validate_page_contract() -> dict[str, list[str]]:
    declared_fields = {field for station in FORM_STATIONS for field in station["fields"]}
    concept_fields = {
        field for concept in CONCEPTS.values() for field in concept.get("fields") or ()
    }
    missing = sorted(declared_fields - concept_fields)
    extra = sorted(concept_fields - declared_fields)
    if missing or extra:
        raise ValueError(f"双页字段契约无效: missing={missing}, extra={extra}")
    return {"missing": missing, "extra": extra}


validate_page_contract()


FORM_PAGE_TEMPLATE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ page.title }}</title>
  <link rel="icon" href="data:,">
  <style>
    :root { color-scheme: light; --ink:#17212b; --muted:#64707d; --line:#d8dee5; --accent:#0b6b50; --soft:#f4f7f6; --warn:#9a5a00; }
    * { box-sizing: border-box; }
    body { margin:0; font-family:Arial,"Microsoft YaHei",sans-serif; color:var(--ink); background:#fff; line-height:1.55; letter-spacing:0; }
    a { color:#075e47; }
    .page-header { border-bottom:1px solid var(--line); padding:24px max(20px,calc((100vw - 1180px)/2)); background:#fff; }
    .page-header h1 { margin:0 0 6px; font-size:26px; }
    .page-header p { margin:0; color:var(--muted); }
    .mode-link { display:inline-block; margin-top:10px; font-weight:700; }
    .quick-shell { max-width:720px; margin:0 auto; padding:8px 20px 48px; }
    .quick-shell .field-grid { grid-template-columns:1fr; }
    .full-shell { max-width:1180px; margin:0 auto; padding:0 20px 56px; display:grid; grid-template-columns:210px minmax(0,1fr); gap:36px; align-items:start; }
    .anchor-directory { position:sticky; top:16px; padding:18px 0; border-right:1px solid var(--line); }
    .anchor-directory strong { display:block; margin-bottom:10px; }
    .anchor-directory a { display:block; padding:8px 14px 8px 0; text-decoration:none; color:#33404c; }
    .anchor-directory a:hover, .anchor-directory a:focus { color:var(--accent); text-decoration:underline; }
    .form-section, .quick-group { padding:28px 0; border-bottom:1px solid var(--line); scroll-margin-top:16px; }
    .form-section h2, .quick-group h2 { margin:0; font-size:21px; }
    .section-heading { display:flex; gap:16px; justify-content:space-between; align-items:baseline; margin-bottom:18px; }
    .section-summary { color:var(--muted); font-size:13px; text-align:right; }
    .concept { margin:0 0 24px; padding:0; border:0; }
    .concept:last-child { margin-bottom:0; }
    .concept legend { font-weight:700; margin-bottom:10px; font-size:15px; }
    .secondary-group { margin:0; padding:0 0 22px; border-bottom:1px solid var(--line); scroll-margin-top:16px; }
    .secondary-group > summary { cursor:pointer; padding:18px 0; color:var(--ink); }
    .secondary-group > summary::marker { color:var(--accent); }
    .secondary-group-title { font-size:18px; font-weight:800; margin-right:10px; }
    .secondary-group-note { color:var(--muted); font-size:13px; }
    .secondary-group-body { padding:2px 0 4px; }
    .field-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px 18px; }
    .field { display:flex; flex-direction:column; gap:6px; min-width:0; }
    .field-wide { grid-column:1/-1; }
    label { font-size:14px; font-weight:600; }
    input, select, textarea, button { font:inherit; letter-spacing:0; }
    .route-type-badge { grid-column:1 / -1; display:flex; align-items:baseline; gap:6px; margin:0 0 14px; padding:8px 10px; border-left:3px solid var(--accent); background:var(--soft); font-size:14px; }
    .route-type-badge strong { color:var(--accent); }
    input:not([type="checkbox"]):not([type="radio"]), select, textarea { width:100%; min-height:42px; border:1px solid #aeb7c0; border-radius:6px; padding:8px 10px; background:#fff; color:var(--ink); }
    select[multiple] { min-height:112px; }
    input:focus, select:focus, textarea:focus { outline:2px solid #8ccfba; outline-offset:1px; border-color:var(--accent); }
    .check-field { flex-direction:row; align-items:center; min-height:42px; padding-top:22px; }
    .check-field input { width:18px; height:18px; margin:0; }
    .radio-options { display:flex; flex-wrap:wrap; gap:10px 18px; min-height:42px; align-items:center; }
    .radio-option { display:inline-flex; align-items:center; gap:6px; font-weight:500; }
    .radio-option input { width:18px; height:18px; margin:0; }
    .time-window-details { grid-column:1/-1; border:1px solid var(--line); border-radius:6px; padding:0 14px; }
    .time-window-details > summary { cursor:pointer; padding:12px 0; font-weight:700; }
    .time-window-body { padding:4px 0 14px; }
    .hint { color:var(--muted); font-size:13px; font-weight:400; }
    .candidate-list { display:flex; flex-wrap:wrap; gap:8px; margin-top:6px; }
    .candidate-list button { border:1px solid var(--line); background:#fff; border-radius:6px; padding:5px 8px; cursor:pointer; }
    .location-status, #price-hint { min-height:20px; color:var(--muted); font-size:13px; margin-top:6px; }
    .server-error { border-left:4px solid #b42318; background:#fff2f0; padding:12px 14px; margin:18px 0; color:#7a271a; }
    .conditional-block[hidden] { display:none !important; }
    .submit-band { padding:28px 0 0; display:flex; align-items:center; flex-wrap:wrap; gap:12px; }
    .primary-button { border:0; background:var(--accent); color:#fff; min-height:44px; padding:10px 20px; border-radius:6px; cursor:pointer; font-weight:700; }
    .primary-button:hover { background:#07543f; }
    .secondary-link { padding:9px 0; font-weight:700; }
    .preset-note { color:var(--muted); font-size:13px; flex:1 1 320px; }
    .build-marker { max-width:1180px; margin:0 auto; padding:16px 20px 22px; border-top:1px solid var(--line); color:var(--muted); font-size:12px; text-align:right; }
    .confirmation-map { margin-top:34px; padding-top:26px; border-top:2px solid var(--ink); }
    .confirmation-map h2 { margin:0 0 8px; }
    .confirmation-row { display:grid; grid-template-columns:120px 1fr auto; gap:14px; padding:11px 0; border-bottom:1px solid var(--line); align-items:start; }
    .constraint-summary { margin-top:16px; padding:12px 14px; background:var(--soft); border-left:4px solid var(--accent); }
    .route-fields { display:grid; grid-template-columns:1fr 1fr; gap:14px 18px; }
    .quick-group[data-ux-control="passengers"] .field { max-width:260px; }
    @media (max-width:780px) {
      .page-header { padding:20px; }
      .full-shell { display:block; padding:0 16px 44px; }
      .anchor-directory { position:static; display:flex; gap:8px; overflow-x:auto; border-right:0; border-bottom:1px solid var(--line); white-space:nowrap; padding:10px 0; }
      .anchor-directory strong { display:none; }
      .anchor-directory a { display:inline-block; padding:8px; }
      .field-grid, .route-fields { grid-template-columns:1fr; }
      .section-heading { display:block; }
      .section-summary { text-align:left; margin-top:5px; }
      .confirmation-row { grid-template-columns:1fr auto; }
      .confirmation-row span { grid-column:1/-1; }
    }
  </style>
</head>
<body data-page-mode="{{ page.mode }}">
  <header class="page-header">
    <h1>{{ page.title }}</h1>
    <p>{% if page.mode == 'quick' %}填写核心信息，其他设置按场景预设。{% else %}六组设置一次展开，使用左侧目录直接定位。{% endif %}</p>
    {% if page.mode == 'quick' %}
    <a class="mode-link" data-mode-link="full" href="{{ url_for('settings') }}">需要完整控制？进入完整设置 →</a>
    {% else %}
    <a class="mode-link" data-mode-link="quick" href="{{ url_for('index') }}">← 返回快速创建</a>
    {% endif %}
  </header>

  {% macro render_field(field) -%}
    {% if field.type == 'hidden' %}
      <input type="hidden" id="{{ field.id }}" name="{{ field.name }}" value="{{ field.value }}">
    {% else %}
      <div class="field{% if field.name in ['origin_manual','destination','exclude_airlines','meeting_location'] %} field-wide{% endif %}{% if field.type == 'checkbox' %} check-field{% endif %}{% if field.visibility %} conditional-block{% endif %}"
           {% if field.visibility %}data-visibility-contract="{{ field.visibility }}"{% endif %}
           {% if field.name == 'child_type' %}data-passenger-kind="child"{% elif field.name == 'elderly_condition' %}data-passenger-kind="elderly"{% endif %}
           {% if field.name == 'notification_email' %}data-email-field="true"{% endif %}>
        {% if field.type == 'checkbox' %}
          <input type="checkbox" id="{{ field.id }}" name="{{ field.name }}" value="true"{% if field.checked %} checked{% endif %}>
          <label for="{{ field.id }}">{{ field.label }}</label>
        {% elif field.type == 'radio' %}
          <span class="hint">{{ field.label }}</span>
          <div class="radio-options" role="radiogroup" aria-label="{{ field.label }}">
            {% for option in field.options %}
            <label class="radio-option" for="{{ field.id }}-{{ option.value }}">
              <input id="{{ field.id }}-{{ option.value }}" name="{{ field.name }}" type="radio" value="{{ option.value }}"{% if option.selected %} checked{% endif %}>
              <span>{{ option.label }}</span>
            </label>
            {% endfor %}
          </div>
        {% elif field.type in ['select','multi'] %}
          <label for="{{ field.id }}">{{ field.label }}</label>
          <select id="{{ field.id }}" name="{{ field.name }}"{% if field.type == 'multi' %} multiple{% endif %}{% if field.required %} required{% endif %}>
            {% for option in field.options %}<option value="{{ option.value }}"{% if option.selected %} selected{% endif %}>{{ option.label }}</option>{% endfor %}
          </select>
        {% else %}
          <label for="{{ field.id }}">{{ field.label }}</label>
          <input id="{{ field.id }}" name="{{ field.name }}" type="{{ field.type }}" value="{{ field.value }}"{% if field.required %} required{% endif %}{% if field.min is not none %} min="{{ field.min }}"{% endif %}>
        {% endif %}
        {% if field.name == 'destination' %}
          <div id="destination-candidates" class="candidate-list" aria-live="polite"></div>
          <div id="destination-status" class="location-status" aria-live="polite"></div>
        {% elif field.name == 'origin_manual' or (page.mode == 'quick' and field.name == 'origin_select') %}
          <div id="origin-candidates" class="candidate-list" aria-live="polite"></div>
          <div id="origin-status" class="location-status" aria-live="polite"></div>
        {% elif field.name == 'passenger_count' %}
          <span class="hint">快速页按总人数建单；老人或儿童同行请在场景中选择对应项。</span>
        {% elif field.name == 'notification_method' %}
          <span class="hint">邮件、PushPlus 或两者；邮箱只在需要邮件时显示。</span>
        {% endif %}
      </div>
    {% endif %}
  {%- endmacro %}

  {% macro render_concept(concept) -%}
    {% for field in concept.hidden_fields or [] %}{{ render_field(field) }}{% endfor %}
    {% if concept.fields %}
      <fieldset class="concept" data-form-concept="{{ concept.name }}">
        <legend>{{ concept.title }}</legend>
        <div class="field-grid">
          {% for field in concept.fields %}{{ render_field(field) }}{% endfor %}
          {% if concept.name == 'time' %}
          <details class="time-window-details" data-time-window-group="custom"{% if concept.custom_window_open %} open{% endif %}>
            <summary>自定义时间窗（可选，填写即覆盖上方偏好，留空不生效）</summary>
            <div class="time-window-body">
              <div class="field-grid">{% for field in concept.custom_window_fields %}{{ render_field(field) }}{% endfor %}</div>
              <details class="time-window-details" data-time-window-group="directional"{% if concept.directional_window_open %} open{% endif %}>
                <summary>去程/返程分别设置（可选，填写即覆盖通用）</summary>
                <div class="time-window-body field-grid">{% for field in concept.directional_window_fields %}{{ render_field(field) }}{% endfor %}</div>
              </details>
              <p class="hint">生效优先级：分方向完整时间窗 > 通用完整时间窗 > 时段偏好。起止只填一项视为未完成，不覆盖下一层。</p>
            </div>
          </details>
          {% elif concept.name == 'cabin' %}
          <p class="hint field-wide">当前按全员同舱监控；混舱（如成人商务+儿童经济）为规划中特性。</p>
          {% endif %}
        </div>
      </fieldset>
    {% endif %}
  {%- endmacro %}

  {% macro render_secondary_group(group) -%}
    <details id="group-{{ group.id }}" class="secondary-group{% if group.visibility %} conditional-block{% endif %}" data-secondary-group="{{ group.id }}"
             {% if group.visibility %}data-visibility-contract="{{ group.visibility }}"{% endif %}
             {% if group.hidden %} hidden{% endif %}{% if group.open %} open{% endif %}>
      <summary>
        <span class="secondary-group-title">{{ group.title }}</span>
        <span class="secondary-group-note">{{ group.description }}</span>
      </summary>
      <div class="secondary-group-body">
        {% for concept in group.concepts %}{{ render_concept(concept) }}{% endfor %}
      </div>
    </details>
  {%- endmacro %}

  {% if page.mode == 'quick' %}
  <main class="quick-shell">
    <form id="subscription-form" method="post" action="{{ url_for('subscribe') }}" data-page-mode="quick" novalidate>
      <input type="hidden" name="form_page" value="quick">
      <input type="hidden" name="monitor_mode" value="quick">
      <input type="hidden" name="subscription_index" value="{{ page.subscription_index }}">
      {% if form_error %}<div class="server-error" role="alert">{{ form_error }}</div>{% endif %}
      {% for group in page.groups %}
        <section class="quick-group" data-ux-control="{{ group.id }}">
          <h2>{{ group.title }}</h2>
          <div class="field-grid">
          {% if group.id == 'route' %}
          <div class="route-type-badge" data-route-type-badge="true" data-route-type="{{ page.route_type_badge.code }}">航线类型：<strong data-route-type-label>{{ page.route_type_badge.label }}</strong><span>（自动识别）</span></div>
          {% endif %}
            {% for field in group.fields %}{% if field.name not in ['monitor_mode','subscription_index'] %}{{ render_field(field) }}{% endif %}{% endfor %}
          </div>
          {% if group.id == 'route' %}<div id="price-hint" aria-live="polite"></div>{% endif %}
        </section>
      {% endfor %}
      <div class="submit-band">
        <button class="primary-button" type="submit">创建监控</button>
        <a class="secondary-link" href="{{ url_for('settings') }}">高级设置</a>
        <span class="preset-note">时间/航司/行李/提醒等已按场景预设，可稍后在完整设置中调整</span>
      </div>
    </form>
  </main>
  {% else %}
  <main class="full-shell">
    <nav class="anchor-directory" aria-label="完整设置目录" data-anchor-directory="true">
      <strong>设置目录</strong>
      {% for section in page.sections %}
        <a href="#section-{{ section.html_id }}">{{ section.title }}</a>
        {% for group in page.secondary_groups %}{% if group.after_section == section.id %}<a href="#group-{{ group.id }}"{% if group.visibility %} class="conditional-block" data-visibility-contract="{{ group.visibility }}"{% if group.hidden %} hidden{% endif %}{% endif %}>{{ group.title }}</a>{% endif %}{% endfor %}
      {% endfor %}
    </nav>
    <form id="subscription-form" method="post" action="{{ url_for('subscribe') }}" data-page-mode="full" novalidate>
      <input type="hidden" name="form_page" value="full">
      {% if form_error %}<div class="server-error" role="alert">{{ form_error }}</div>{% endif %}
      {% for section in page.sections %}
        <section id="section-{{ section.html_id }}" class="form-section" data-form-section="{{ section.id }}">
          <div class="section-heading">
            <h2>{{ loop.index }}. {{ section.title }}</h2>
            <span class="section-summary" data-section-summary="{{ section.id }}">{{ section.summary }}</span>
          </div>
          {% if section.id == 'where' %}
          <div class="route-type-badge" data-route-type-badge="true" data-route-type="{{ page.route_type_badge.code }}">航线类型：<strong data-route-type-label>{{ page.route_type_badge.label }}</strong><span>（自动识别）</span></div>
          {% endif %}
          {% for concept in section.concepts %}
            {{ render_concept(concept) }}
            {% for group in page.secondary_groups %}
              {% if group.after_section == section.id and group.after_concept == concept.name %}
                {{ render_secondary_group(group) }}
              {% endif %}
            {% endfor %}
          {% endfor %}
        </section>
        {% for group in page.secondary_groups %}
          {% if group.after_section == section.id and not group.after_concept %}
            {{ render_secondary_group(group) }}
          {% endif %}
        {% endfor %}
      {% endfor %}
      <section id="confirmation-map" class="confirmation-map" aria-labelledby="confirmation-title">
        <h2 id="confirmation-title">提交前确认</h2>
        <p class="hint">摘要与邮件依据使用同一套服务端函数生成。</p>
        {% for section in page.sections %}
          <div class="confirmation-row">
            <strong>{{ section.title }}</strong>
            <span data-confirm-summary="{{ section.id }}">{{ section.summary }}</span>
            <a data-confirm-edit="section-{{ section.html_id }}" href="#section-{{ section.html_id }}">修改</a>
          </div>
        {% endfor %}
        <div id="constraint-summary-preview" class="constraint-summary">约束依据将在字段完整后显示。</div>
        <div class="submit-band">
          <button class="primary-button" type="submit">保存完整设置</button>
          <a class="secondary-link" href="{{ url_for('index') }}">返回快速创建</a>
        </div>
      </section>
    </form>
  </main>
  {% endif %}

  <footer class="build-marker" data-build-marker="true">{{ build_marker }}</footer>

  <script>
    (() => {
      'use strict';
      const form = document.getElementById('subscription-form');
      if (!form) return;
      const pageMode = document.body.dataset.pageMode;
      const cityAirports = {{ city_airports|tojson }};
      const cityAliases = {{ city_aliases|tojson }};
      const exactLocations = {{ exact_location_airports|tojson }};
      const airportCodes = new Set({{ airport_codes|tojson }});
      const canonicalNames = Object.keys(cityAirports);

      function field(name) { return form.elements.namedItem(name); }
      function scalar(name) {
        const element = field(name);
        if (!element) return '';
        return String(element.value || '').trim();
      }
      function resolveExact(raw) {
        const value = String(raw || '').trim();
        if (!value) return null;
        const upper = value.toUpperCase();
        if (/^[A-Z]{3}$/.test(upper) && airportCodes.has(upper)) return [upper];
        if (cityAirports[value]) return cityAirports[value];
        if (exactLocations[value]) return exactLocations[value];
        const canonical = cityAliases[value];
        if (canonical && cityAirports[canonical]) return cityAirports[canonical];
        return null;
      }
      function suggestions(raw) {
        const query = String(raw || '').trim().toLowerCase();
        if (query.length < 2) return [];
        return [...canonicalNames, ...Object.keys(cityAliases)]
          .filter((name, index, all) => all.indexOf(name) === index)
          .filter(name => name.toLowerCase().includes(query))
          .slice(0, 5);
      }
      function renderCandidates(kind, raw, targetInput) {
        const box = document.getElementById(`${kind}-candidates`);
        if (!box) return;
        box.textContent = '';
        if (resolveExact(raw)) return;
        suggestions(raw).forEach(name => {
          const button = document.createElement('button');
          button.type = 'button';
          button.textContent = name;
          button.addEventListener('click', () => {
            targetInput.value = name;
            updateLocations();
          });
          box.appendChild(button);
        });
      }
      function originRaw() {
        const selected = scalar('origin_select');
        if (pageMode === 'quick') return selected;
        return selected === 'OTHER' ? scalar('origin_manual') : selected;
      }
      function updateLocations() {
        const origin = resolveExact(originRaw());
        const destination = resolveExact(scalar('destination'));
        const originActive = field('origin_airports_active');
        const destinationActive = field('destination_airports_active');
        if (originActive) originActive.value = origin ? origin.join(',') : '';
        if (destinationActive) destinationActive.value = destination ? destination.join(',') : '';
        const originStatus = document.getElementById('origin-status');
        const destinationStatus = document.getElementById('destination-status');
        if (originStatus) originStatus.textContent = origin ? `已识别：${origin.join('/')}` : (originRaw() ? '请从候选中选择或输入完整城市/IATA' : '');
        if (destinationStatus) destinationStatus.textContent = destination ? `已识别：${destination.join('/')}` : (scalar('destination') ? '请从候选中选择或输入完整城市/IATA' : '');
        const originManual = field('origin_manual') || field('origin_select');
        const destinationInput = field('destination');
        if (originManual) renderCandidates('origin', originRaw(), originManual);
        if (destinationInput) renderCandidates('destination', scalar('destination'), destinationInput);
        updatePriceHint(origin, destination);
      }
      let priceHintTimer = 0;
      function updatePriceHint(origin, destination) {
        const output = document.getElementById('price-hint');
        const badge = document.querySelector('[data-route-type-badge="true"]');
        const badgeLabel = badge?.querySelector('[data-route-type-label]');
        window.clearTimeout(priceHintTimer);
        if (!origin || !destination) {
          if (output) output.textContent = '';
          if (badge) badge.dataset.routeType = '';
          if (badgeLabel) badgeLabel.textContent = '待识别';
          return;
        }
        priceHintTimer = window.setTimeout(async () => {
          try {
            const params = new URLSearchParams({origin: origin[0], dest: destination[0]});
            const response = await fetch(`/price_hint?${params.toString()}`);
            const data = await response.json();
            if (output) output.textContent = data.has_data ? `历史单程参考：${data.text || data.price || '已有观测'}` : '暂无历史价格参考';
            if (badge) badge.dataset.routeType = data.route_type || '';
            if (badgeLabel) badgeLabel.textContent = data.route_type_label || '待识别';
            const routeTypeField = field('route_type');
            if (routeTypeField) routeTypeField.value = data.route_type || '';
          } catch (_) {
            if (output) output.textContent = '价格参考暂不可用';
            if (badge) badge.dataset.routeType = '';
            if (badgeLabel) badgeLabel.textContent = '待识别';
          }
        }, 180);
      }

      function selectedScenarios() {
        const element = field('travel_scenario');
        if (!element) return [];
        return element.multiple
          ? Array.from(element.selectedOptions).map(option => option.value)
          : [element.value];
      }
      function updatePassengerProfile() {
        const childCount = Number(scalar('child_count') || 0);
        const elderlyCount = Number(scalar('elderly_count') || 0);
        const scenarios = selectedScenarios();
        document.querySelectorAll('[data-visibility-contract="passenger-profile"]').forEach(element => {
          const kind = element.dataset.passengerKind;
          element.hidden = kind === 'child'
            ? !(childCount > 0 || scenarios.includes('family'))
            : !(elderlyCount > 0 || scenarios.includes('elderly'));
        });
      }
      function updateEmailVisibility() {
        const method = scalar('notification_method');
        document.querySelectorAll('[data-visibility-contract="notification-email"]').forEach(element => {
          element.hidden = !['email', 'both'].includes(method);
        });
      }

      let summaryTimer = 0;
      function updateBusinessVisibility() {
        const enabled = selectedScenarios().includes('business');
        document.querySelectorAll('[data-visibility-contract="business-scenario"]').forEach(element => {
          element.hidden = !enabled;
        });
      }
      function scheduleSummary() {
        if (pageMode !== 'full') return;
        window.clearTimeout(summaryTimer);
        summaryTimer = window.setTimeout(async () => {
          try {
            const response = await fetch('/defaults_preview', {method: 'POST', body: new FormData(form)});
            const data = await response.json();
            Object.entries(data.station_summaries || {}).forEach(([key, value]) => {
              document.querySelectorAll(`[data-section-summary="${key}"],[data-confirm-summary="${key}"]`).forEach(element => { element.textContent = value; });
            });
            const constraint = document.getElementById('constraint-summary-preview');
            if (constraint) constraint.textContent = data.constraint_summary_text || '约束依据将在字段完整后显示。';
          } catch (_) {
            const constraint = document.getElementById('constraint-summary-preview');
            if (constraint) constraint.textContent = '摘要暂不可用，字段仍可正常保存。';
          }
        }, 220);
      }

      ['origin_select','origin_manual','destination'].forEach(name => field(name)?.addEventListener('input', updateLocations));
      ['child_count','elderly_count','travel_scenario'].forEach(name => field(name)?.addEventListener('change', updatePassengerProfile));
      field('notification_method')?.addEventListener('change', updateEmailVisibility);
      form.addEventListener('input', scheduleSummary);
      form.addEventListener('change', scheduleSummary);
      field('travel_scenario')?.addEventListener('change', updateBusinessVisibility);
      form.addEventListener('submit', event => {
        updateLocations();
        if (!resolveExact(originRaw()) || !resolveExact(scalar('destination'))) {
          event.preventDefault();
          const destinationStatus = document.getElementById('destination-status');
          if (destinationStatus) destinationStatus.textContent = '请先选择可识别的出发地和目的地。';
          return;
        }
      });
      updateLocations();
      updatePassengerProfile();
      updateEmailVisibility();
      scheduleSummary();
    })();
  </script>
</body>
</html>
"""
