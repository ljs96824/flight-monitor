"""Four-dimensional flight price decision framework."""

from __future__ import annotations

import statistics
import copy
import json
import os
import re
from datetime import date, datetime, time, timedelta

from airport_logistics import (
    get_airport_logistics,
    get_arrival_buffer,
    get_departure_buffer,
    estimate_airport_to_meeting,
    get_meeting_importance_defaults,
    route_type_buffer_label,
)
from on_time_data import estimate_punctuality
from price_estimator import (
    build_display_prices,
    build_passenger_price_breakdown,
    build_price_tiers,
    calc_transaction_price,
)
from pricing import (
    assert_same_caliber,
    budget_to_pp,
    caliber_label,
    itinerary_price_pp,
    passenger_rate_sum,
    price_in_scope,
)
from price_calendar import (
    analyze_date_savings as _calendar_date_savings,
    analyze_row_savings as _calendar_row_savings,
    analyze_weekday_pattern as _calendar_weekday_pattern,
    calendar_rows as _calendar_rows,
    calendar_price_on_date as _calendar_price_on_date,
    roundtrip_calendar_rows as _roundtrip_calendar_rows,
)
from domestic_fare_rules import get_aircraft_name
from flight_combo_utils import normalize_combo
from airlines import (
    LCC_POLICIES,
    canonicalize_airline_lcc_policy,
    classify_itinerary,
    lcc_filter_value,
    resolve_lcc_policy,
)
from log_utils import safe_log
from mixed_cabin import match_mixed_cabin_combinations
from observations_store import get_current_round
from storage import get_all_history, get_latest_alternatives, get_target_history

IATA_CITY_NAMES = {
    "PVG": "上海浦东",
    "SHA": "上海虹桥",
    "PEK": "北京首都",
    "PKX": "北京大兴",
    "CAN": "广州白云",
    "SZX": "深圳宝安",
    "CTU": "成都天府",
    "TFU": "成都天府",
    "HGH": "杭州萧山",
    "NKG": "南京禄口",
    "KIX": "大阪关西",
    "ITM": "大阪伊丹",
    "NRT": "东京成田",
    "HND": "东京羽田",
    "ICN": "首尔仁川",
    "TPE": "台北桃园",
    "HKG": "香港",
    "BKK": "曼谷",
    "SIN": "新加坡",
    "LAX": "洛杉矶",
    "JFK": "纽约肯尼迪",
    "SFO": "旧金山",
    "ORD": "芝加哥奥黑尔",
    "DFW": "达拉斯沃斯堡",
    "MCO": "奥兰多",
    "MIA": "迈阿密",
    "ATL": "亚特兰大",
    "SEA": "西雅图",
    "YVR": "温哥华",
    "YYZ": "多伦多皮尔逊",
    "LHR": "伦敦希思罗",
    "CDG": "巴黎戴高乐",
    "FRA": "法兰克福",
    "AMS": "阿姆斯特丹",
    "DXB": "迪拜",
    "DOH": "多哈",
    "ABQ": "阿尔伯克基",
}


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


MIN_SAMPLE_FOR_PRICE_SIGNAL = _positive_int_env(
    "MIN_SAMPLE_FOR_PRICE_SIGNAL",
    5,
)


def city_name(iata_code: str) -> str:
    return IATA_CITY_NAMES.get(iata_code, iata_code)


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def has_enough_detail(flight: dict) -> bool:
    """Whether a flight has enough segment detail to be actionable as a recommendation."""
    segments = flight.get("segments") or []
    if not segments:
        return False
    first = segments[0] if isinstance(segments[0], dict) else {}
    last = segments[-1] if isinstance(segments[-1], dict) else {}
    dep_time = (
        flight.get("departure_time")
        or flight.get("dep_time")
        or first.get("dep_time")
        or first.get("departure_time")
    )
    arr_time = (
        flight.get("arrival_time")
        or flight.get("arr_time")
        or last.get("arr_time")
        or last.get("arrival_time")
    )
    return bool(dep_time and arr_time)


def _reference_only_reason(flight: dict) -> str:
    return (
        flight.get("reference_reason")
        or flight.get("exclude_reason")
        or "航段时间/机型信息不完整，仅作价格参考"
    )


def _set_if_missing(target: dict, key: str, value) -> None:
    if key not in target and value not in (None, ""):
        target[key] = value


SCENARIO_RULES = {
    "personal": {
        "label": "个人出行",
        "defaults": {"price_sensitivity": "medium"},
        "notes": ["个人出行：价格和便利性均衡"],
    },
    "business": {
        "label": "商务/会议",
        "defaults": {
            "trip_type": "business_meeting",
            "direct_preferred": True,
            "time_preference_mode": "daytime",
            "refund_flexibility": "preferred",
            "airline_policy": "prefer_full_service",
        },
        "notes": ["商务/会议：准点、直飞和可改签优先"],
    },
    "tourism": {
        "label": "旅游",
        "defaults": {"trip_type": "tourism", "price_sensitivity": "high"},
        "notes": ["旅游：突出低价日期和合理中转"],
    },
    "family_visit": {
        "label": "探亲/回家",
        "defaults": {
            "trip_type": "family_visit",
            "baggage_default": "prefer_included",
            "price_sensitivity": "medium",
        },
        "notes": ["探亲/回家：行李和合理价格优先"],
    },
    "family": {
        "label": "家庭/亲子",
        "defaults": {
            "companions": "with_child",
            "time_preference_mode": "no_redeye",
            "direct_preferred": True,
            "baggage_default": "prefer_included",
            "max_extra_duration_hours": 3,
        },
        "notes": ["家庭/亲子：优先白天、直飞/短中转和行李明确"],
    },
    "elderly": {
        "label": "有老人同行",
        "defaults": {
            "companions": "with_elderly",
            "time_preference_mode": "no_redeye",
            "direct_preferred": True,
            "baggage_default": "prefer_included",
            "max_extra_duration_hours": 3,
            "refund_flexibility": "preferred",
            "airline_policy": "prefer_full_service",
        },
        "notes": ["有老人同行：白天到达、短中转、全服务航司和可退改优先"],
    },
    "important": {
        "label": "重要事项",
        "defaults": {
            "time_preference_mode": "no_redeye",
            "direct_preferred": True,
            "allow_self_transfer": False,
            "allow_overnight_transfer": False,
            "refund_flexibility": "required",
            "max_extra_duration_hours": 3,
        },
        "notes": ["重要事项：按保守规则处理，降低复杂中转和票规风险"],
    },
    "price_first": {
        "label": "价格优先",
        "defaults": {
            "price_sensitivity": "max",
            "transfer_policy": "price_first",
        },
        "notes": ["价格优先：低价权重最高，可接受合理不便"],
    },
}


COMPANION_RULES = {
    "with_child": {
        "label": "有儿童",
        "defaults": {
            "time_preference_mode": "no_redeye",
            "baggage_default": "prefer_included",
            "direct_preferred": True,
        },
        "notes": ["儿童同行：降低红眼、长中转和行李不明确方案"],
    },
    "with_elderly": {
        "label": "有老人",
        "defaults": {
            "time_preference_mode": "no_redeye",
            "baggage_default": "prefer_included",
            "direct_preferred": True,
            "refund_flexibility": "preferred",
            "airline_policy": "prefer_full_service",
            "max_extra_duration_hours": 3,
        },
        "notes": ["老人同行：提高白天、短中转、全服务航司和可退改权重"],
    },
    "with_elderly_child": {
        "label": "老人和儿童都有",
        "defaults": {
            "time_preference_mode": "no_redeye",
            "baggage_default": "prefer_included",
            "direct_preferred": True,
            "refund_flexibility": "preferred",
            "airline_policy": "prefer_full_service",
            "max_extra_duration_hours": 3,
        },
        "notes": ["老人和儿童同行：优先直飞/短中转、白天和行李明确"],
    },
    "group": {
        "label": "多人同行",
        "defaults": {"availability_priority": "high"},
        "notes": ["多人同行：提高库存可购买性和最终支付价校验权重"],
    },
}


TRAVEL_PROFILE_LABELS = {
    "price": "价格敏感度",
    "time": "时间刚性",
    "comfort": "舒适度需求",
    "risk_averse": "执行风险厌恶",
    "baggage": "行李票规重要性",
}


TRAVEL_PROFILE_LEVEL_LABELS = {
    "low": "低",
    "medium": "中",
    "high": "高",
}


TRAVEL_PROFILE_LABELS = {
    "price": "价格敏感度",
    "time": "时间刚性",
    "comfort": "舒适度需求",
    "risk_averse": "执行风险厌恶",
    "baggage": "行李票规重要性",
}


TRAVEL_PROFILE_LEVEL_LABELS = {
    "low": "低",
    "medium": "中",
    "high": "高",
}


TRAVEL_SCENARIO_LABELS = {
    "personal": "个人出行",
    "business": "商务/会议",
    "tourism": "旅游",
    "family_visit": "探亲/回家",
    "visit_family": "探亲/回家",
    "family": "家庭/亲子",
    "elderly": "有老人同行",
    "with_elderly": "有老人同行",
    "important": "重要事项",
    "price_first": "价格优先",
}

GOAL_TO_ALERTS = {
    "price_alert": ["low_price_alert", "price_risk_alert"],
    "price_drop_alert": ["low_price_alert", "price_risk_alert"],
    "buy_timing": ["price_risk_alert", "better_same_day"],
    "cheaper_date": ["cheaper_date"],
    "best_overall": ["better_same_day"],
}

ROUTE_TYPE_ALERTS = {
    "domestic": {
        "price_alert": ["low_price_alert", "price_risk_alert"],
        "price_drop_alert": ["low_price_alert", "price_risk_alert"],
        "buy_timing": ["price_risk_alert", "better_same_day"],
        "cheaper_date": ["cheaper_date"],
        "best_overall": ["better_same_day"],
    },
    "international": {
        "price_alert": ["large_price_drop", "transfer_risk_change"],
        "price_drop_alert": ["large_price_drop", "transfer_risk_change"],
        "buy_timing": ["large_price_drop", "transfer_risk_change", "interline_risk_change"],
        "cheaper_date": ["large_price_drop"],
        "best_overall": ["transfer_risk_change", "interline_risk_change"],
    },
    "greater_china": {
        "price_alert": ["low_price_alert", "large_price_drop"],
        "price_drop_alert": ["low_price_alert", "large_price_drop"],
        "buy_timing": ["price_risk_alert", "large_price_drop"],
        "cheaper_date": ["cheaper_date"],
        "best_overall": ["price_risk_alert"],
    },
}


def _normalize_travel_scenarios(value) -> list[str]:
    """Normalize legacy single scenario and new multi-select values."""
    if value in (None, "", []):
        return ["personal"]
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value if str(item).strip()]
    else:
        items = [str(value).strip()]
    return items or ["personal"]


def _travel_scenario_labels(scenarios: list[str]) -> list[str]:
    return [TRAVEL_SCENARIO_LABELS.get(item, item) for item in scenarios]


def _to_non_negative_int(value, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _normalize_passengers(value) -> dict:
    if not isinstance(value, dict):
        return {}
    passengers = {
        "adult": _to_non_negative_int(value.get("adult")),
        "child": _to_non_negative_int(value.get("child")),
        "elderly": _to_non_negative_int(value.get("elderly")),
        "infant": _to_non_negative_int(value.get("infant")),
    }
    return passengers if any(passengers.values()) else {}


def _passengers_from_legacy_companions(companions: str) -> dict:
    companions = str(companions or "solo")
    mapping = {
        "with_child": {"adult": 1, "child": 1, "elderly": 0, "infant": 0},
        "with_elderly": {"adult": 1, "child": 0, "elderly": 1, "infant": 0},
        "with_elderly_child": {"adult": 1, "child": 1, "elderly": 1, "infant": 0},
        "with_both": {"adult": 1, "child": 1, "elderly": 1, "infant": 0},
        "multiple": {"adult": 2, "child": 0, "elderly": 0, "infant": 0},
        "group": {"adult": 2, "child": 0, "elderly": 0, "infant": 0},
    }
    return dict(mapping.get(companions, {}))


def get_total_passengers(subscription: dict | None) -> tuple[int, dict | None]:
    """Return the canonical passenger total and optional category breakdown."""
    subscription = subscription or {}
    preferences = subscription.get("preferences") or {}
    basic = subscription.get("basic") or {}
    passengers = _normalize_passengers(preferences.get("passengers"))
    if passengers:
        return sum(passengers.values()), passengers

    count = _to_non_negative_int(
        basic.get("passenger_count") or preferences.get("passenger_count"),
        1,
    )
    if count > 1:
        return count, {"adult": count, "child": 0, "elderly": 0, "infant": 0}

    legacy = (
        preferences.get("travelers")
        or preferences.get("companions")
        or subscription.get("companions")
    )
    legacy_passengers = _passengers_from_legacy_companions(legacy)
    if legacy_passengers:
        return sum(legacy_passengers.values()), legacy_passengers
    fallback_count = max(1, count)
    return fallback_count, {"adult": fallback_count, "child": 0, "elderly": 0, "infant": 0}


PASSENGER_RULE_DEFAULT_WEIGHTS = {
    "price": 0.35,
    "time": 0.20,
    "comfort": 0.15,
    "execution_risk": 0.15,
    "baggage": 0.10,
    "refund": 0.05,
}

PASSENGER_RULE_FAMILY_ELDER_WEIGHTS = {
    "price": 0.18,
    "time": 0.22,
    "comfort": 0.22,
    "execution_risk": 0.22,
    "baggage": 0.10,
    "refund": 0.06,
}


def build_passenger_profile(passengers: dict | None, extra: dict | None = None) -> dict:
    """Derive recommendation-facing passenger needs without storing age/gender."""
    normalized = _normalize_passengers(passengers) or {
        "adult": 1,
        "child": 0,
        "elderly": 0,
        "infant": 0,
    }
    extra = dict(extra or {})
    adult = _to_non_negative_int(normalized.get("adult"), 0)
    child = _to_non_negative_int(normalized.get("child"), 0)
    elderly = _to_non_negative_int(normalized.get("elderly"), 0)
    infant = _to_non_negative_int(normalized.get("infant"), 0)
    child_type = str(extra.get("child_type") or "").strip()
    has_infant = infant > 0 or child_type == "infant"
    has_child = child > 0 or has_infant or child_type in {"preschool", "school_age"} or bool(extra.get("scenario_has_child"))
    has_elderly = elderly > 0 or bool(extra.get("scenario_has_elderly"))
    companion_constraints = extra.get("companion_constraints") or []
    if isinstance(companion_constraints, str):
        companion_constraints = [item.strip() for item in companion_constraints.split(",") if item.strip()]
    mobility_limited = bool(extra.get("mobility_limited") or "limited_mobility" in companion_constraints)
    no_redeye_strict = bool(extra.get("no_redeye_strict") or "no_redeye" in companion_constraints)
    transfer_sensitive = bool(extra.get("transfer_sensitive") or "avoid_long_layover" in companion_constraints)
    needs_baggage = bool(extra.get("needs_baggage_clarity") or "need_baggage" in companion_constraints)
    needs_refund = bool(extra.get("needs_refund_flexibility") or "need_refund_change" in companion_constraints)
    elderly_condition = str(extra.get("elderly_condition") or "")
    if elderly_condition in {"limited_walk_transfer", "no_redeye_early"}:
        mobility_limited = True
    if elderly_condition == "no_redeye_early":
        no_redeye_strict = True
    return {
        "adults": adult,
        "children": child,
        "elderly": elderly,
        "infants": infant,
        "has_child": has_child,
        "has_elderly": has_elderly,
        "has_infant": has_infant,
        "needs_low_fatigue": has_child or has_elderly,
        "needs_baggage_clarity": has_child or has_elderly or has_infant or needs_baggage,
        "needs_time_stability": has_child or has_elderly or no_redeye_strict,
        "needs_refund_flexibility": needs_refund,
        "mobility_sensitive": has_elderly or has_infant or mobility_limited,
        "mobility_limited": mobility_limited,
        "no_redeye_strict": no_redeye_strict,
        "transfer_sensitive": transfer_sensitive,
        "child_type": child_type,
        "elderly_condition": elderly_condition,
    }


def _passenger_rule_base() -> dict:
    weights = dict(PASSENGER_RULE_DEFAULT_WEIGHTS)
    return {
        "prefer_direct": False,
        "allow_red_eye": True,
        "allow_self_transfer": True,
        "allow_airport_change": True,
        "allow_overnight_transfer": True,
        "require_baggage_clarity": False,
        "max_transfers": 2,
        "min_connection_min": 90,
        "prefer_daytime": False,
        "allow_transfer": True,
        "mobility_penalty": "normal",
        "prefer_near_city_airport": False,
        "seat_together_priority": "normal",
        "weights": weights,
        "w_price": weights["price"],
        "w_time": weights["time"],
        "w_comfort": weights["comfort"],
        "w_execution_risk": weights["execution_risk"],
        "w_baggage": weights["baggage"],
        "w_refund": weights["refund"],
    }


def build_passenger_friendly_rules(
    profile: dict | None,
    base_rules: dict | None = None,
    route_type: str | None = None,
) -> dict:
    """Convert passenger profile into hard gates, soft penalties, and weights."""
    passenger_profile = profile or build_passenger_profile(None)
    rules = _passenger_rule_base()
    if base_rules:
        rules.update(base_rules)
        merged_weights = dict(PASSENGER_RULE_DEFAULT_WEIGHTS)
        merged_weights.update((base_rules or {}).get("weights") or {})
        rules["weights"] = merged_weights
    if passenger_profile.get("has_child") or passenger_profile.get("has_elderly"):
        rules.update(
            {
                "prefer_direct": True,
                "allow_red_eye": False,
                "allow_self_transfer": False,
                "allow_airport_change": False,
                "allow_overnight_transfer": False,
                "require_baggage_clarity": True,
                "max_transfers": 1,
                "min_connection_min": int(rules.get("min_connection_min") or 90) + 30,
                "prefer_daytime": True,
                "weights": dict(PASSENGER_RULE_FAMILY_ELDER_WEIGHTS),
            }
        )
    if passenger_profile.get("has_elderly"):
        rules["mobility_penalty"] = "high"
        rules["prefer_near_city_airport"] = True
    if passenger_profile.get("has_infant"):
        rules["require_baggage_clarity"] = True
        rules["seat_together_priority"] = "high"
    if passenger_profile.get("mobility_limited"):
        rules["allow_transfer"] = False
        rules["prefer_direct"] = True
        rules["max_transfers"] = 0
    if passenger_profile.get("no_redeye_strict"):
        rules["allow_red_eye"] = False
    weights = rules.get("weights") or dict(PASSENGER_RULE_DEFAULT_WEIGHTS)
    rules.update(
        {
            "w_price": weights.get("price", 0),
            "w_time": weights.get("time", 0),
            "w_comfort": weights.get("comfort", 0),
            "w_execution_risk": weights.get("execution_risk", 0),
            "w_baggage": weights.get("baggage", 0),
            "w_refund": weights.get("refund", 0),
            "route_type": route_type or "",
        }
    )
    return rules


def normalize_budget_scope(value) -> str:
    scope = str(value or "per_person").strip().lower()
    if scope in {"all", "total", "all_passengers", "all_passenger", "overall", "整单", "全员", "全部人"}:
        return "all"
    return "per_person"


def _budget_visible_scope(scope: str, round_trip: bool = True) -> str:
    normalized = normalize_budget_scope(scope)
    if normalized == "all":
        return "all_passengers_roundtrip" if round_trip else "all_passengers_oneway"
    return "per_person_roundtrip" if round_trip else "per_person_oneway"


def _budget_passengers(passengers=None, total_passengers=1) -> dict:
    if isinstance(passengers, dict) and any(_to_non_negative_int(v) for v in passengers.values()):
        return {
            "adult": _to_non_negative_int(passengers.get("adult"), 0),
            "child": _to_non_negative_int(passengers.get("child"), 0),
            "elderly": _to_non_negative_int(passengers.get("elderly"), 0),
            "infant": _to_non_negative_int(passengers.get("infant"), 0),
        }
    return {"adult": max(1, _to_non_negative_int(total_passengers, 1)), "child": 0, "elderly": 0, "infant": 0}


def passenger_budget_limits(
    max_budget=None,
    ideal_price=None,
    budget_scope="per_person",
    total_passengers=1,
    *,
    passengers=None,
    route_type=None,
    round_trip: bool = True,
    max_budget_scope=None,
    target_price_scope=None,
) -> dict:
    max_scope = normalize_budget_scope(max_budget_scope or budget_scope)
    ideal_scope = normalize_budget_scope(target_price_scope or budget_scope)
    passenger_map = _budget_passengers(passengers, total_passengers)
    passenger_count = max(1, sum(_to_non_negative_int(v) for v in passenger_map.values()))
    max_value = _to_float(max_budget)
    ideal_value = _to_float(ideal_price)
    max_visible_scope = _budget_visible_scope(max_scope, round_trip)
    ideal_visible_scope = _budget_visible_scope(ideal_scope, round_trip)
    output_scope = "all_passengers_roundtrip" if round_trip else "all_passengers_oneway"

    max_pp_oneway = (
        budget_to_pp(max_value, passenger_map, scope=max_visible_scope, route_type=route_type, round_trip=round_trip)
        if max_value is not None else None
    )
    ideal_pp_oneway = (
        budget_to_pp(ideal_value, passenger_map, scope=ideal_visible_scope, route_type=route_type, round_trip=round_trip)
        if ideal_value is not None else None
    )
    max_total = (
        price_in_scope(max_pp_oneway, passenger_map, scope=output_scope, route_type=route_type, round_trip=round_trip)
        if max_pp_oneway is not None else None
    )
    ideal_total = (
        price_in_scope(ideal_pp_oneway, passenger_map, scope=output_scope, route_type=route_type, round_trip=round_trip)
        if ideal_pp_oneway is not None else None
    )
    # The comparison budget in the user-selected visible scope is exactly the
    # number the user typed. Keep it unrounded here; use pp-oneway only for
    # cross-scope conversions such as all-passenger totals.
    max_compare = max_value
    ideal_compare = ideal_value
    if max_scope == "all":
        max_total = max_value
    if ideal_scope == "all":
        ideal_total = ideal_value
    return {
        "budget_scope": max_scope,
        "max_budget_scope": max_scope,
        "target_price_scope": ideal_scope,
        "passenger_count": passenger_count,
        "passengers": passenger_map,
        "multiplier": price_in_scope(1, passenger_map, scope="all_passengers_oneway", route_type=route_type, round_trip=False),
        "input_max_budget": max_value,
        "input_ideal_price": ideal_value,
        "max_budget_pp_oneway": max_pp_oneway,
        "ideal_price_pp_oneway": ideal_pp_oneway,
        "max_budget_total": max_total,
        "ideal_price_total": ideal_total,
        "max_budget_compare": max_compare,
        "ideal_price_compare": ideal_compare,
        "max_budget_compare_scope": max_visible_scope,
        "ideal_price_compare_scope": ideal_visible_scope,
        "max_budget_label": caliber_label(max_visible_scope, passenger_map, route_type),
        "ideal_price_label": caliber_label(ideal_visible_scope, passenger_map, route_type),
    }



def _passenger_scope_kind(label) -> str:
    text = str(label or "")
    has_total = any(token in text for token in ("全员", "多人", "整单", "全部人", "总上限", "total"))
    has_single = any(token in text for token in ("单人", "每人", "per_person", "per-passenger", "single"))
    if has_total and not has_single:
        return "total"
    if has_single and not has_total:
        return "single"
    if has_total and has_single:
        return "total" if any(token in text for token in ("折算", "总", "全员", "整单")) else "single"
    return ""


def assert_price_budget_same_passenger_scope(price_scope_label=None, budget_scope_label=None) -> bool:
    price_scope = _passenger_scope_kind(price_scope_label)
    budget_scope = _passenger_scope_kind(budget_scope_label)
    if price_scope and budget_scope and price_scope != budget_scope:
        raise AssertionError(
            f"价格与预算人数口径不一致: price_scope={price_scope_label!r}, "
            f"budget_scope={budget_scope_label!r}"
        )
    return True

def build_passenger_roundtrip_pricing(
    outbound_price,
    return_price=None,
    passengers: dict | None = None,
    route_type: str | None = None,
    cabin: str | None = "economy",
) -> dict:
    passengers = _normalize_passengers(passengers) or {"adult": 1, "child": 0, "elderly": 0, "infant": 0}
    display_prices = build_display_prices(outbound_price, return_price, passengers, route_type)
    outbound_breakdown = build_passenger_price_breakdown(outbound_price, passengers, cabin, route_type)
    return_breakdown = (
        build_passenger_price_breakdown(return_price, passengers, cabin, route_type)
        if return_price is not None
        else None
    )
    single_adult_total = (_to_float(outbound_price) or 0) + (_to_float(return_price) or 0)
    raw_total = display_prices["raw_total"]
    factor = outbound_breakdown.get("factor") or 1
    price_tiers = build_price_tiers(
        outbound_price,
        return_price,
        passengers,
        route_type,
        purchase_type="roundtrip" if return_breakdown else "oneway",
        cabin=cabin,
    )
    return {
        "applies": bool(factor != 1 or sum(passengers.values()) > 1),
        "scope": "roundtrip" if return_breakdown else "oneway",
        "passengers": passengers,
        "passenger_label": outbound_breakdown.get("passenger_label") or "",
        "factor": factor,
        "route_type": route_type or "",
        "outbound": outbound_breakdown,
        "return": return_breakdown,
        "total_price": raw_total,
        "single_adult_price": single_adult_total,
        "price_tiers": price_tiers,
        "note": outbound_breakdown.get("note") or "",
    }


def determine_cabins(constraints: dict | None) -> list[str]:
    """Return cabin classes to search from reimbursement and level policy."""
    constraints = constraints or {}
    arrangement = str(constraints.get("cabin_arrangement") or "").strip()
    if arrangement == "economy_all":
        return ["economy"]
    if arrangement in {"business_all", "mixed"}:
        return ["economy", "business"]
    policy = str(constraints.get("cabin_policy") or "economy_only").strip()
    cabins = ["economy"]
    if policy == "business_allowed":
        cabins.append("business")
    elif policy == "level_based":
        level = str(constraints.get("user_level") or "staff").strip()
        business_seats = _to_non_negative_int(constraints.get("business_seats"))
        if level in {"director", "vp"} or business_seats > 0:
            cabins.append("business")
    return list(dict.fromkeys(cabins))


def _cheapest_cabin_price(flights: list[dict] | None, cabin_class: str):
    prices = [
        _to_float(flight.get("price"))
        for flight in flights or []
        if (flight.get("cabin_class") or "economy") == cabin_class
    ]
    prices = [price for price in prices if price is not None and price > 0]
    return min(prices) if prices else None


def check_reimburse(cabin_label: str, price, per_person_cap) -> str:
    price_value = _to_float(price)
    cap = _to_float(per_person_cap)
    if price_value is None or cap is None or cap <= 0:
        return ""
    if price_value > cap:
        return f"{cabin_label}¥{price_value:,.0f}超出报销上限¥{cap:,.0f}"
    return f"{cabin_label}¥{price_value:,.0f}在报销上限¥{cap:,.0f}内"


def build_cabin_policy_summary(constraints: dict | None, flights: list[dict] | None) -> dict:
    """Summarize economy/business options without deciding for the user."""
    constraints = constraints or {}
    cabins = determine_cabins(constraints)
    trip_natures = constraints.get("trip_natures") or []
    if isinstance(trip_natures, str):
        trip_natures = [trip_natures]
    if not trip_natures and constraints.get("trip_nature"):
        legacy = str(constraints.get("trip_nature") or "")
        trip_natures = [{"business_meeting": "meeting"}.get(legacy, legacy)]
    cabin_arrangement = str(constraints.get("cabin_arrangement") or "").strip() or "economy_all"
    economy_price = _cheapest_cabin_price(flights, "economy")
    business_price = _cheapest_cabin_price(flights, "business")
    business_seats = _to_non_negative_int(constraints.get("business_seats"))
    economy_seats = _to_non_negative_int(constraints.get("economy_seats"))
    passenger_count = _to_non_negative_int(constraints.get("passenger_count"), 0)
    if cabin_arrangement == "business_all" and passenger_count:
        business_seats = passenger_count
        economy_seats = 0
    elif cabin_arrangement == "economy_all" and passenger_count:
        business_seats = 0
        economy_seats = passenger_count
    if not business_seats and "business" in cabins and constraints.get("cabin_policy") == "business_allowed":
        business_seats = _to_non_negative_int(constraints.get("passenger_count"), 0)
    if not economy_seats and business_seats:
        total = _to_non_negative_int(constraints.get("passenger_count"), 0)
        economy_seats = max(0, total - business_seats) if total else economy_seats
    team_total = None
    if economy_price is not None or business_price is not None:
        team_total = 0
        if economy_price is not None:
            team_total += economy_price * economy_seats
        if business_price is not None:
            team_total += business_price * business_seats
        team_total = round(team_total) if (economy_seats or business_seats) else None
    cap = _to_float(constraints.get("reimburse_per_person"))
    business_note = check_reimburse("商务舱", business_price, cap)
    economy_note = check_reimburse("经济舱", economy_price, cap)
    team_cost_note = ""
    if team_total is not None:
        parts = []
        if business_seats:
            parts.append(f"{business_seats}商务")
        if economy_seats:
            parts.append(f"{economy_seats}经济")
        seat_text = "+".join(parts) if parts else "团队"
        team_cost_note = f"{seat_text}合计参考¥{team_total:,.0f}"
    return {
        "trip_nature": constraints.get("trip_nature") or "",
        "trip_natures": trip_natures,
        "cabin_arrangement": cabin_arrangement,
        "cabin_policy": constraints.get("cabin_policy") or "economy_only",
        "user_level": constraints.get("user_level") or "staff",
        "cabins": cabins,
        "business_seats": business_seats,
        "economy_seats": economy_seats,
        "reimburse_per_person": cap,
        "economy_unit_price": economy_price,
        "business_unit_price": business_price,
        "team_total": team_total,
        "business_reimburse_note": business_note,
        "economy_reimburse_note": economy_note,
        "team_cost_note": team_cost_note,
    }


def _first_time_text(flight: dict, *keys: str) -> str:
    for key in keys:
        value = str(flight.get(key) or "").strip()
        if value:
            return value
    segments = flight.get("segments") or flight.get("flights") or []
    if segments and isinstance(segments[0], dict):
        first = segments[0]
        last = segments[-1] if isinstance(segments[-1], dict) else first
        for key in keys:
            candidates = {
                "departure_time": ("dep_time", "departure_time", "time"),
                "arrival_time": ("arr_time", "arrival_time", "time"),
            }.get(key, (key,))
            segment = last if key == "arrival_time" else first
            for segment_key in candidates:
                value = str(segment.get(segment_key) or "").strip()
                if value:
                    return value
    return ""


def _parse_time_minutes(value) -> int | None:
    text = str(value or "").strip()
    match = re.search(r"(\d{1,2}):(\d{2})", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour * 60 + minute


def parse_flight_time(time_str, date_str: str | None = None) -> datetime | None:
    """Parse flight time with a 24-hour clock and optional flight date."""
    if not time_str:
        return None
    text = str(time_str).strip()
    if not text:
        return None

    day_offset = 0
    offset_match = re.search(r"\+(\d+)\s*$", text)
    if offset_match:
        day_offset = int(offset_match.group(1))
        text = text[: offset_match.start()].strip()

    normalized = text.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(normalized, fmt)
            return parsed + timedelta(days=day_offset)
        except ValueError:
            pass

    time_match = re.search(r"(\d{1,2}):(\d{2})", normalized)
    if not time_match or not date_str:
        return None
    compact_time = f"{int(time_match.group(1)):02d}:{time_match.group(2)}"
    try:
        parsed = datetime.strptime(f"{date_str} {compact_time}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return parsed + timedelta(days=day_offset)


def _minutes_datetime(date_str: str | None, minutes: int | float | None) -> datetime | None:
    if date_str is None or minutes is None:
        return None
    try:
        base = datetime.strptime(str(date_str), "%Y-%m-%d")
    except ValueError:
        return None
    return base + timedelta(minutes=int(round(minutes)))



def _assert_dated_datetime(value, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise AssertionError(f"{label}必须是带日期的datetime, 实际={type(value).__name__}: {value!r}")
    return value

def _flight_date_text(flight: dict, kind: str) -> str:
    keys = (
        ("departure_date", "dep_date", "date")
        if kind == "departure"
        else ("arrival_date", "arr_date", "date")
    )
    for key in keys:
        value = str((flight or {}).get(key) or "").strip()
        if value:
            return value

    segments = (flight or {}).get("segments") or (flight or {}).get("flights") or []
    if segments and isinstance(segments[0], dict):
        segment = segments[0] if kind == "departure" else segments[-1]
        for key in keys:
            value = str(segment.get(key) or "").strip()
            if value:
                return value
    return ""


def _flight_departure_datetime(flight: dict, default_date: str | None = None) -> datetime | None:
    raw = _first_time_text(flight or {}, "departure_time", "dep_time")
    date_text = _flight_date_text(flight or {}, "departure") or default_date
    return parse_flight_time(raw, date_text)


def _explicit_flight_date_text(flight: dict, kind: str) -> str:
    keys = ("departure_date", "dep_date") if kind == "departure" else ("arrival_date", "arr_date")
    for key in keys:
        value = str((flight or {}).get(key) or "").strip()
        if value:
            return value
    segments = (flight or {}).get("segments") or (flight or {}).get("flights") or []
    if segments and isinstance(segments[0], dict):
        segment = segments[0] if kind == "departure" else segments[-1]
        for key in keys:
            value = str(segment.get(key) or "").strip()
            if value:
                return value
    return ""


def _time_text_has_date(value) -> bool:
    return bool(re.search(r"\d{4}-\d{2}-\d{2}", str(value or "")))


def _flight_arrival_datetime(flight: dict, default_date: str | None = None) -> datetime | None:
    flight = flight or {}
    raw = _first_time_text(flight, "arrival_time", "arr_time")
    explicit_arrival_date = _explicit_flight_date_text(flight, "arrival")
    date_text = explicit_arrival_date or _flight_date_text(flight, "arrival") or default_date
    arrival_dt = parse_flight_time(raw, date_text)
    if arrival_dt is None:
        return None

    # 只有 HH:MM 时需要结合出发时间判断是否跨午夜，避免 00:xx 被当成当日凌晨。
    if not explicit_arrival_date and not _time_text_has_date(raw):
        departure_dt = _flight_departure_datetime(flight, default_date)
        if departure_dt is not None and arrival_dt <= departure_dt:
            arrival_dt += timedelta(days=1)
    return arrival_dt


def _window_datetime_from_minutes_or_text(
    windows: dict,
    date_str: str | None,
    minutes_key: str,
    text_key: str,
) -> datetime | None:
    minutes_value = windows.get(minutes_key)
    dt = _minutes_datetime(date_str, minutes_value)
    if dt is not None:
        return dt
    text_value = str(windows.get(text_key) or "").strip()
    if not text_value:
        return None
    return parse_flight_time(text_value, date_str)


def _same_day_window_datetimes(windows: dict | None, date_str: str | None) -> tuple[datetime | None, datetime | None]:
    windows = windows or {}
    return (
        _window_datetime_from_minutes_or_text(windows, date_str, "outbound_arrive_by_minutes", "outbound_arrive_by"),
        _window_datetime_from_minutes_or_text(windows, date_str, "return_depart_after_minutes", "return_depart_after"),
    )


def _airport_window_value(windows: dict | None, map_key: str, airport_iata: str | None):
    values = (windows or {}).get(map_key)
    if not isinstance(values, dict):
        return None
    airport = str(airport_iata or "").strip().upper()
    if not airport:
        return None
    return values.get(airport)


def _has_airport_window_map(windows: dict | None, *keys: str) -> bool:
    windows = windows or {}
    return any(isinstance(windows.get(key), dict) for key in keys)


def _attach_same_day_airport_window_maps(windows: dict, airport_iata: str | None) -> dict:
    result = dict(windows or {})
    airport = str(airport_iata or "").strip().upper()
    if not airport:
        return result

    def put(map_key: str, value_key: str) -> None:
        value = result.get(value_key)
        if value is None:
            return
        existing = result.get(map_key) if isinstance(result.get(map_key), dict) else {}
        merged = dict(existing)
        merged[airport] = value
        result[map_key] = merged

    put("outbound_arrive_by_by_airport", "outbound_arrive_by")
    put("outbound_arrive_by_minutes_by_airport", "outbound_arrive_by_minutes")
    put("return_depart_after_by_airport", "return_depart_after")
    put("return_depart_after_minutes_by_airport", "return_depart_after_minutes")

    reserve = result.get("reserve_breakdown")
    if isinstance(reserve, dict):
        existing = result.get("reserve_breakdown_by_airport") if isinstance(result.get("reserve_breakdown_by_airport"), dict) else {}
        merged = dict(existing)
        merged[airport] = reserve
        result["reserve_breakdown_by_airport"] = merged
    return result


def _same_day_window_from_airport_maps(base_windows: dict | None, airport_iata: str | None) -> dict | None:
    base = dict(base_windows or {})
    airport = str(airport_iata or "").strip().upper()
    if not airport:
        return None
    map_keys = (
        "outbound_arrive_by_by_airport",
        "outbound_arrive_by_minutes_by_airport",
        "return_depart_after_by_airport",
        "return_depart_after_minutes_by_airport",
        "reserve_breakdown_by_airport",
    )
    if not _has_airport_window_map(base, *map_keys):
        return None
    active = dict(base)
    found = False
    mapping = (
        ("outbound_arrive_by_by_airport", "outbound_arrive_by"),
        ("outbound_arrive_by_minutes_by_airport", "outbound_arrive_by_minutes"),
        ("return_depart_after_by_airport", "return_depart_after"),
        ("return_depart_after_minutes_by_airport", "return_depart_after_minutes"),
    )
    for map_key, value_key in mapping:
        value = _airport_window_value(base, map_key, airport)
        if value is not None:
            active[value_key] = value
            found = True
    reserve_map = base.get("reserve_breakdown_by_airport")
    if isinstance(reserve_map, dict) and airport in reserve_map:
        active["reserve_breakdown"] = reserve_map[airport]
        found = True
    return active if found else None


def _merge_same_day_airport_window_maps(target: dict, source: dict | None) -> dict:
    result = dict(target or {})
    for map_key in (
        "outbound_arrive_by_by_airport",
        "outbound_arrive_by_minutes_by_airport",
        "return_depart_after_by_airport",
        "return_depart_after_minutes_by_airport",
        "reserve_breakdown_by_airport",
    ):
        values = (source or {}).get(map_key)
        if not isinstance(values, dict):
            continue
        merged = dict(result.get(map_key) if isinstance(result.get(map_key), dict) else {})
        merged.update(values)
        result[map_key] = merged
    return result


def _ensure_same_day_airport_window_maps(
    windows: dict | None,
    constraints: dict | None,
    airports,
) -> dict:
    result = dict(windows or {})
    normalized = _same_day_constraints(constraints or {})
    if not (normalized.get("business_start") and normalized.get("business_end")):
        return result
    seen = []
    for airport in airports or []:
        airport_code = str(airport or "").strip().upper()
        if airport_code and airport_code not in seen:
            seen.append(airport_code)
    for airport_code in seen:
        if (
            _airport_window_value(result, "outbound_arrive_by_by_airport", airport_code) is not None
            and _airport_window_value(result, "outbound_arrive_by_minutes_by_airport", airport_code) is not None
            and _airport_window_value(result, "return_depart_after_by_airport", airport_code) is not None
            and _airport_window_value(result, "return_depart_after_minutes_by_airport", airport_code) is not None
        ):
            continue
        computed = compute_same_day_windows({"constraints": normalized}, None, airport_code)
        if computed:
            result = _merge_same_day_airport_window_maps(result, computed)
    return result


def _same_day_target_date(date_str: str | None):
    if not date_str:
        return None
    try:
        return datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _same_day_outbound_passes_window(flight: dict, windows: dict | None, date_str: str | None) -> bool:
    flight = flight or {}
    windows = windows or {}
    has_arrive_by = windows.get("outbound_arrive_by_minutes") is not None or str(windows.get("outbound_arrive_by") or "").strip() != ""
    boundary_date = _flight_date_text(flight, "departure") or date_str
    arrive_by_dt, _ = _same_day_window_datetimes(windows, boundary_date)
    arrival_dt = _flight_arrival_datetime(flight, boundary_date)
    if not has_arrive_by:
        return True
    if arrive_by_dt is None or arrival_dt is None:
        return False
    _assert_dated_datetime(arrive_by_dt, "去程到达上限")
    _assert_dated_datetime(arrival_dt, "去程到达时间")
    target_date = _same_day_target_date(boundary_date)
    if target_date is not None and arrival_dt.date() != target_date:
        return False
    return arrival_dt <= arrive_by_dt


def _same_day_return_passes_window(flight: dict, windows: dict | None, date_str: str | None) -> bool:
    flight = flight or {}
    windows = windows or {}
    has_depart_after = windows.get("return_depart_after_minutes") is not None or str(windows.get("return_depart_after") or "").strip() != ""
    boundary_date = _flight_date_text(flight, "departure") or date_str
    _, return_after_dt = _same_day_window_datetimes(windows, boundary_date)
    departure_dt = _flight_departure_datetime(flight, boundary_date)
    if not has_depart_after:
        return True
    if return_after_dt is None or departure_dt is None:
        return False
    _assert_dated_datetime(return_after_dt, "返程出发下限")
    _assert_dated_datetime(departure_dt, "返程出发时间")
    target_date = _same_day_target_date(boundary_date)
    if target_date is not None and departure_dt.date() != target_date:
        return False
    return departure_dt >= return_after_dt

def _same_day_windows_for_airport(
    base_windows: dict | None,
    constraints: dict | None,
    airport_iata: str | None,
) -> dict:
    normalized = _same_day_constraints(constraints or {})
    airport = str(airport_iata or "").strip().upper()
    mapped = _same_day_window_from_airport_maps(base_windows or {}, airport)
    if mapped is not None:
        return mapped
    if airport and normalized.get("business_start") and normalized.get("business_end"):
        computed = compute_same_day_windows({"constraints": normalized}, None, airport)
        if computed:
            return computed
    return base_windows or {}


def _same_day_outbound_transport_minutes(windows: dict | None) -> int | None:
    windows = windows or {}
    reserve = windows.get("reserve_breakdown") if isinstance(windows.get("reserve_breakdown"), dict) else {}
    outbound = reserve.get("outbound") if isinstance(reserve.get("outbound"), dict) else {}
    return _optional_int(
        outbound.get("destination_transport_min"),
        _optional_int(windows.get("destination_transport_min"), _optional_int(windows.get("transport_min"))),
    )

def _same_day_return_transport_minutes(windows: dict | None) -> int | None:
    windows = windows or {}
    reserve = windows.get("reserve_breakdown") if isinstance(windows.get("reserve_breakdown"), dict) else {}
    ret = reserve.get("return") if isinstance(reserve.get("return"), dict) else {}
    return _optional_int(
        ret.get("meeting_to_airport_min"),
        _optional_int(windows.get("destination_transport_min"), _optional_int(windows.get("transport_min"))),
    )


def _date_from_same_day_source(source: dict | None) -> str:
    source = source or {}
    for key in ("depart_date", "departure_date", "date"):
        value = str(source.get(key) or "").strip()
        if value:
            return value[:10]
    for section in ("basic", "constraints", "hard_constraints", "route_info"):
        nested = source.get(section) if isinstance(source.get(section), dict) else {}
        for key in ("depart_date", "departure_date", "date"):
            value = str(nested.get(key) or "").strip()
            if value:
                return value[:10]
    return ""


def _flight_departure_minutes(flight: dict) -> int | None:
    return _parse_time_minutes(_first_time_text(flight or {}, "departure_time", "dep_time"))


def _flight_arrival_minutes(flight: dict) -> int | None:
    return _parse_time_minutes(_first_time_text(flight or {}, "arrival_time", "arr_time"))


def _minutes_to_text(minutes: int | float | None) -> str:
    if minutes is None:
        return ""
    total = int(round(minutes)) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _flight_airport(flight: dict, *keys: str) -> str:
    for key in keys:
        value = str(flight.get(key) or "").strip().upper()
        if value:
            return value
    segments = flight.get("segments") or flight.get("flights") or []
    if segments and isinstance(segments[0], dict):
        first = segments[0]
        last = segments[-1] if isinstance(segments[-1], dict) else first
        for key in keys:
            candidates = {
                "departure_airport": ("dep_airport", "departure_airport", "origin"),
                "arrival_airport": ("arr_airport", "arrival_airport", "dest", "destination"),
            }.get(key, (key,))
            segment = last if key == "arrival_airport" else first
            for segment_key in candidates:
                value = str(segment.get(segment_key) or "").strip().upper()
                if value:
                    return value
    return ""


def _same_day_constraints(source: dict | None) -> dict:
    source = source or {}
    constraints = dict(source.get("constraints") or {})
    hard = source.get("hard_constraints") or {}
    for key in (
        "same_day_round_trip",
        "day_trip_period",
        "business_start",
        "business_end",
        "buffer_hours",
        "transport_mode",
        "user_transport_min",
        "transport_margin_mode",
        "redundancy_min",
        "time_source",
        "route_type",
        "meeting_importance",
        "meeting_location",
        "business_location",
        "meeting_area",
        "destination_area",
        "destination_transport_min",
        "airport_to_meeting_min",
        "meeting_transport_min",
        "origin_transport_min",
        "airport_advance_min",
        "departure_airport_process_min",
        "arrival_exit_min",
        "delay_buffer_min",
        "pre_meeting_buffer_min",
        "post_meeting_buffer_min",
        "custom_redundancy_min",
        "checked_baggage_required",
        "need_baggage",
    ):
        if key not in constraints and hard.get(key) is not None:
            constraints[key] = hard.get(key)
        if key not in constraints and source.get(key) is not None:
            constraints[key] = source.get(key)
        basic = source.get("basic") or {}
        if key not in constraints and basic.get(key) is not None:
            constraints[key] = basic.get(key)
    return constraints


def _normalize_day_trip_period(value) -> str:
    text = str(value or "").strip()
    if text in {"morning", "afternoon", "full_day"}:
        return text
    return "morning"


def _same_day_default_profile(period: str | None) -> dict:
    period = _normalize_day_trip_period(period)
    profiles = {
        "morning": {
            "label": "上午办事",
            "outbound_depart_after": 6 * 60 + 30,
            "outbound_arrive_by": 10 * 60,
            "outbound_target": 9 * 60 + 30,
            "return_depart_after": 16 * 60,
            "return_target": 17 * 60 + 30,
            "return_arrive_by": 23 * 60 + 30,
            "min_stay_minutes": 5 * 60,
        },
        "afternoon": {
            "label": "下午办事",
            "outbound_depart_after": 6 * 60 + 30,
            "outbound_arrive_by": 12 * 60,
            "outbound_target": 10 * 60 + 30,
            "return_depart_after": 18 * 60,
            "return_target": 19 * 60 + 30,
            "return_arrive_by": 23 * 60 + 30,
            "min_stay_minutes": 5 * 60,
        },
        "full_day": {
            "label": "全天办事",
            "outbound_depart_after": 6 * 60 + 30,
            "outbound_arrive_by": 10 * 60 + 30,
            "outbound_target": 9 * 60 + 30,
            "return_depart_after": 18 * 60 + 30,
            "return_target": 20 * 60,
            "return_arrive_by": 23 * 60 + 30,
            "min_stay_minutes": 5 * 60,
        },
    }
    return profiles[period]


def _same_day_relaxed_profile(period: str | None) -> dict:
    profile = dict(_same_day_default_profile(period))
    profile.update(
        {
            "outbound_depart_after": 6 * 60,
            "outbound_arrive_by": 14 * 60,
            "return_depart_after": 16 * 60,
            "return_arrive_by": 23 * 60 + 30,
            "min_stay_minutes": 4 * 60,
            "relaxed": True,
        }
    )
    return profile


def _same_day_default_combo_score(combo: dict, profile: dict) -> float:
    outbound_arr = _flight_arrival_minutes(combo.get("outbound") or {}) or 0
    return_dep = _flight_departure_minutes(combo.get("return") or {}) or 0
    total = _to_float(combo.get("total_price")) or 0
    return (
        abs(outbound_arr - int(profile.get("outbound_target") or outbound_arr)) * 2
        + abs(return_dep - int(profile.get("return_target") or return_dep)) * 1.5
        + total / 100
    )


def _optional_int(value, default: int | None = None) -> int | None:
    if value in (None, ""):
        return default
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def calc_transport_margin(
    transport_min: int | float | None,
    margin_mode: str | None,
    travel_hour: int | float | None = None,
) -> tuple[int, float, bool]:
    """Calculate traffic margin on top of the estimated transport time."""
    RATIOS = {"tight": 0.15, "standard": 0.30, "loose": 0.50}
    transport = _optional_int(transport_min, 0) or 0
    ratio = RATIOS.get(margin_mode or "standard", 0.30)
    rush = False
    if travel_hour is not None:
        try:
            hour = float(travel_hour)
            if 7 <= hour < 9.5 or 17 <= hour < 19.5:
                ratio += 0.10
                rush = True
        except (TypeError, ValueError):
            pass
    margin = max(round(transport * ratio), 15)
    return margin, ratio, rush



def normalize_meeting_importance(value) -> str:
    key = str(value or "important").strip().lower()
    if key in {"normal", "business", "regular"}:
        return "normal"
    if key in {"critical", "must_not_late", "no_late", "cannot_late"}:
        return "critical"
    return "important"


def calc_meeting_transport_margin(
    transport_min: int | float | None,
    importance: str | None,
    travel_hour: int | float | None = None,
) -> tuple[int, float, bool]:
    defaults = get_meeting_importance_defaults(normalize_meeting_importance(importance))
    transport = _optional_int(transport_min, 0) or 0
    ratio = float(defaults.get("road_margin_ratio") or 0.30)
    rush = False
    if travel_hour is not None:
        try:
            hour = float(travel_hour)
            if 7 <= hour < 9.5 or 17 <= hour < 19.5:
                ratio += 0.10
                rush = True
        except (TypeError, ValueError):
            pass
    margin = max(round(transport * ratio), int(defaults.get("road_margin_min") or 15))
    return margin, ratio, rush


def classify_business_time_margin(minutes: int | float | None) -> dict:
    value = _optional_int(minutes, 0) or 0
    if value >= 60:
        return {"level": "稳妥可行", "rank": 0, "margin_min": value, "note": f"安全余量{value}分钟,稳妥可行"}
    if value >= 30:
        return {"level": "可行但偏紧", "rank": 1, "margin_min": value, "note": f"安全余量{value}分钟,可作备选"}
    if value >= 0:
        return {"level": "高风险卡点", "rank": 2, "margin_min": value, "note": f"安全余量仅{value}分钟,商务会议风险偏高"}
    return {"level": "不可行", "rank": 3, "margin_min": value, "note": f"预计迟到约{abs(value)}分钟"}


def _constraint_minutes(constraints: dict, keys: tuple[str, ...], default: int | None = None) -> int | None:
    for key in keys:
        value = constraints.get(key)
        if value not in (None, ""):
            return _optional_int(value, default)
    return default


_MEETING_FIXED_BREAKDOWN_CACHE: dict[tuple[str, str, str, str], dict] = {}


def _stable_cache_value(value):
    if isinstance(value, dict):
        return {str(key): _stable_cache_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_stable_cache_value(item) for item in value]
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _meeting_fixed_breakdown_cache_key(
    constraints: dict | None,
    direction: str,
    airport_iata: str | None,
    route_type: str | None,
) -> tuple[str, str, str, str]:
    # Only fields that affect the fixed reserve calculation belong in the key.
    # Price, passenger and notification fields may differ between render paths,
    # but they must not force the same airport reserve to be recomputed per flight.
    relevant_keys = (
        "same_day_round_trip",
        "business_start",
        "business_end",
        "meeting_importance",
        "transport_mode",
        "checked_baggage_required",
        "need_baggage",
        "destination_transport_min",
        "airport_to_meeting_min",
        "meeting_transport_min",
        "user_transport_min",
        "meeting_location",
        "business_location",
        "meeting_area",
        "destination_area",
        "arrival_exit_min",
        "delay_buffer_min",
        "flight_delay_buffer_min",
        "pre_meeting_buffer_min",
        "post_meeting_buffer_min",
        "airport_advance_min",
        "departure_airport_process_min",
        "checkin_buffer_min",
        "custom_redundancy_min",
        "user_custom_redundancy_min",
        "safety_min",
        "meeting_safety_min",
    )
    source = constraints or {}
    relevant_constraints = {key: source.get(key) for key in relevant_keys if key in source}
    stable_constraints = json.dumps(
        _stable_cache_value(relevant_constraints),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        str(direction or "").strip().lower(),
        str(airport_iata or "").strip().upper(),
        str(route_type or "domestic").strip().lower() or "domestic",
        stable_constraints,
    )


def _cache_meeting_fixed_breakdown(cache_key, result: dict) -> dict:
    _MEETING_FIXED_BREAKDOWN_CACHE[cache_key] = copy.deepcopy(result)
    return result


def _clear_same_day_window_cache_for_tests() -> None:
    _MEETING_FIXED_BREAKDOWN_CACHE.clear()

def compute_meeting_fixed_breakdown(
    subscription: dict | None,
    direction: str,
    airport_iata: str | None,
    route_type: str | None,
) -> dict:
    constraints = _same_day_constraints(subscription)
    airport = str(airport_iata or "").strip().upper()
    normalized_route_type = str(route_type or "domestic").strip() or "domestic"
    cache_key = _meeting_fixed_breakdown_cache_key(constraints, direction, airport, normalized_route_type)
    cached = _MEETING_FIXED_BREAKDOWN_CACHE.get(cache_key)
    if cached is not None:
        return copy.deepcopy(cached)
    logistics = get_airport_logistics(airport)
    importance_explicit = constraints.get("meeting_importance") not in (None, "")
    importance = normalize_meeting_importance(constraints.get("meeting_importance"))
    defaults = get_meeting_importance_defaults(importance)
    custom = _constraint_minutes(constraints, ("custom_redundancy_min", "user_custom_redundancy_min"), 0) or 0
    checked_baggage = bool(constraints.get("checked_baggage_required") or constraints.get("need_baggage") == "required")
    business_start = _parse_time_minutes(constraints.get("business_start"))
    business_end = _parse_time_minutes(constraints.get("business_end"))
    estimated_transport = _optional_int(logistics.get("to_center_min"), 45) or 45
    destination_transport_raw = None
    for key in ("destination_transport_min", "airport_to_meeting_min", "meeting_transport_min", "user_transport_min"):
        if constraints.get(key) not in (None, ""):
            destination_transport_raw = constraints.get(key)
            break
    meeting_location = (
        constraints.get("meeting_location")
        or constraints.get("business_location")
        or constraints.get("meeting_area")
        or constraints.get("destination_area")
    )
    location_estimate = estimate_airport_to_meeting(
        airport,
        meeting_location,
        constraints.get("transport_mode") or "taxi",
    )
    transport_user_filled = destination_transport_raw not in (None, "")
    meeting_location_known = transport_user_filled or bool(location_estimate)
    unknown_meeting_location = not meeting_location_known
    if destination_transport_raw in (None, "") and location_estimate:
        estimated_transport = _optional_int(location_estimate.get("minutes"), estimated_transport) or estimated_transport
    elif destination_transport_raw in (None, "") and constraints.get("same_day_round_trip"):
        # Fast-mode business trips often omit an exact meeting area. Use a moderate
        # conservative city estimate and label it explicitly instead of stacking a
        # large unknown-location buffer on top of the airport default.
        estimated_transport = 35
    destination_transport = _optional_int(destination_transport_raw, estimated_transport) or estimated_transport
    if transport_user_filled:
        destination_transport_source = "\u7528\u6237\u586b\u5199"
    elif location_estimate:
        destination_transport_source = "meeting_location_estimate"
    else:
        destination_transport_source = "\u672a\u586b\u4f1a\u8bae\u5730\u70b9,\u6309\u4fdd\u5b88\u4f30\u7b97" if constraints.get("same_day_round_trip") else "\u673a\u573a\u4f30\u7b97"
    location_confidence = "known" if meeting_location_known else "unknown"
    near_airport_meeting = bool(location_estimate.get("near_airport")) and destination_transport_raw in (None, "")
    arrival_exit_default = int(defaults["arrival_exit_min"]) if importance_explicit else 35
    arrival_exit_base = _constraint_minutes(constraints, ("arrival_exit_min",), arrival_exit_default) or arrival_exit_default
    baggage_extra = int(defaults.get("checked_baggage_extra_min") or 0) if checked_baggage else 0
    arrival_exit = arrival_exit_base + baggage_extra
    airport_advance_default = int(defaults["airport_advance_min"]) if importance_explicit else 75
    airport_advance = _constraint_minutes(
        constraints,
        ("airport_advance_min", "departure_airport_process_min", "checkin_buffer_min"),
        airport_advance_default,
    ) or airport_advance_default
    delay_default = int(defaults["delay_buffer_min"]) if importance_explicit else 0
    pre_default = int(defaults["pre_meeting_buffer_min"]) if importance_explicit else 30
    delay_buffer = _constraint_minutes(constraints, ("delay_buffer_min", "flight_delay_buffer_min"), delay_default) or delay_default
    pre_meeting = _constraint_minutes(constraints, ("pre_meeting_buffer_min",), pre_default) or pre_default
    post_meeting = _constraint_minutes(constraints, ("post_meeting_buffer_min",), int(defaults["post_meeting_buffer_min"])) or int(defaults["post_meeting_buffer_min"])

    if direction == "outbound":
        arrival_override = constraints.get("arrival_exit_min") not in (None, "")
        if checked_baggage and not arrival_override:
            arrival_exit = min(arrival_exit, 45)
        delay_override = any(constraints.get(key) not in (None, "") for key in ("delay_buffer_min", "flight_delay_buffer_min"))
        pre_override = constraints.get("pre_meeting_buffer_min") not in (None, "")
        near_known_meeting = bool(near_airport_meeting)
        high_redundancy_meeting = bool(importance_explicit and meeting_location_known and importance in {"important", "critical"})
        safety = _constraint_minutes(
            constraints,
            ("safety_min", "meeting_safety_min"),
            15 if high_redundancy_meeting else 0,
        ) or 0

        if not arrival_override and (unknown_meeting_location or (near_known_meeting and not high_redundancy_meeting)):
            arrival_exit_base = 25 if importance == "normal" else 35
            # In the hard same-day meeting window, a known near-airport venue or
            # an unknown venue uses a practical landing-exit estimate. Baggage is
            # still noted elsewhere, but it must not silently turn the hard window
            # into an impossible 3-hour-plus buffer.
            baggage_extra = 0
            arrival_exit = arrival_exit_base

        if not delay_override and (unknown_meeting_location or (near_known_meeting and not high_redundancy_meeting) or importance == "normal"):
            delay_buffer = 0
        if not pre_override and (unknown_meeting_location or (near_known_meeting and not high_redundancy_meeting) or importance == "normal"):
            pre_meeting = 0

        if unknown_meeting_location or (near_known_meeting and not high_redundancy_meeting):
            travel_hour = (business_start - destination_transport) / 60 if business_start is not None else None
            base_ratio = 0.20
            ratio = base_ratio + (0.10 if travel_hour is not None and (7 <= travel_hour < 9.5 or 17 <= travel_hour < 19.5) else 0)
            rush = bool(travel_hour is not None and (7 <= travel_hour < 9.5 or 17 <= travel_hour < 19.5))
            margin = max(round(destination_transport * ratio), 15)
        elif importance_explicit:
            travel_hour = (business_start - destination_transport) / 60 if business_start is not None else None
            margin, ratio, rush = calc_meeting_transport_margin(destination_transport, importance, travel_hour)
        else:
            margin = max(round(destination_transport * 0.30), 15)
            ratio = 0.30
            rush = False

        importance_buffer = arrival_exit + delay_buffer + pre_meeting
        itemized_total = arrival_exit + destination_transport + margin + delay_buffer + pre_meeting + safety + custom
        total = itemized_total
        outbound_window_total = total
        window_text = _minutes_to_text(business_start - outbound_window_total) if business_start is not None else ""
        itemization_ok = itemized_total == outbound_window_total
        print(
            f"[\u53bb\u7a0b\u5230\u4f1a-\u65b0] \u673a\u573a={airport} \u5730\u70b9\u72b6\u6001={location_confidence} "
            f"\u6765\u6e90={destination_transport_source} \u843d\u5730\u79bb\u573a={arrival_exit} "
            f"\u8f66\u7a0b={destination_transport} \u4ea4\u901a\u5197\u4f59={margin} \u5ef6\u8bef={delay_buffer} "
            f"\u4f1a\u524d={pre_meeting} \u5b89\u5168={safety} \u81ea\u5b9a\u4e49={custom} \u603b\u9884\u7559={outbound_window_total} "
            f"\u5408\u8ba1\u6821\u9a8c={itemized_total} \u4e00\u81f4={itemization_ok} \u5230\u8fbe\u4e0a\u9650={window_text}"
        )
        return _cache_meeting_fixed_breakdown(cache_key, {
            "legacy": False,
            "model": "meeting_fixed",
            "direction": direction,
            "airport_iata": airport,
            "airport_size": logistics.get("size") or "medium",
            "importance": importance,
            "importance_label": defaults.get("label"),
            "arrival_exit_min": arrival_exit,
            "arrival_exit_base_min": arrival_exit_base,
            "checked_baggage_extra_min": baggage_extra,
            "destination_transport_min": destination_transport,
            "destination_transport_source": destination_transport_source,
            "destination_transport_margin_min": margin,
            "destination_transport_margin_ratio": round(ratio, 2),
            "destination_transport_rush": rush,
            "delay_buffer_min": delay_buffer,
            "pre_meeting_buffer_min": pre_meeting,
            "importance_buffer_min": importance_buffer,
            "safety_min": safety,
            "custom_redundancy_min": custom,
            "itemized_total_min": itemized_total,
            "itemization_ok": itemization_ok,
            "total_min": total,
            "outbound_window_total_min": outbound_window_total,
            "near_airport_meeting": near_airport_meeting,
            "location_confidence": location_confidence,
            "meeting_location": meeting_location,
            "airport_buffer_min": arrival_exit,
            "buffer_label": "\u843d\u5730\u79bb\u573a",
            "transport_min": destination_transport,
            "transport_source": destination_transport_source,
            "margin_min": margin,
            "margin_ratio": round(ratio, 2),
            "rush_hour": rush,
            "route_type": normalized_route_type,
        })

    travel_hour = business_end / 60 if business_end is not None else None
    margin, ratio, rush = calc_meeting_transport_margin(destination_transport, importance, travel_hour)
    total = post_meeting + destination_transport + margin + airport_advance + custom
    return _cache_meeting_fixed_breakdown(cache_key, {
        "legacy": False,
        "model": "meeting_fixed",
        "direction": direction,
        "airport_iata": airport,
        "airport_size": logistics.get("size") or "medium",
        "importance": importance,
        "importance_label": defaults.get("label"),
        "post_meeting_buffer_min": post_meeting,
        "meeting_to_airport_min": destination_transport,
        "meeting_to_airport_source": destination_transport_source,
        "meeting_to_airport_margin_min": margin,
        "meeting_to_airport_margin_ratio": round(ratio, 2),
        "meeting_to_airport_rush": rush,
        "departure_airport_process_min": airport_advance,
        "custom_redundancy_min": custom,
        "total_min": total,
        "airport_buffer_min": airport_advance,
        "buffer_label": "机场提前量",
        "transport_min": destination_transport,
        "transport_source": destination_transport_source,
        "margin_min": margin,
        "margin_ratio": round(ratio, 2),
        "rush_hour": rush,
        "safety_min": custom,
        "route_type": normalized_route_type,
    })
def compute_reserve_breakdown(
    subscription: dict | None,
    direction: str,
    airport_iata: str | None,
    route_type: str | None,
) -> dict:
    """Return the single reserve breakdown used by both filtering and rendering."""
    constraints = _same_day_constraints(subscription)
    airport = str(airport_iata or "").strip().upper()
    mode = str(constraints.get("transport_mode") or "taxi").strip().lower()
    logistics = get_airport_logistics(airport)
    estimated_transport = logistics.get("transit_min") if mode == "transit" else logistics.get("to_center_min")
    user_transport_raw = constraints.get("user_transport_min")
    user_filled = user_transport_raw not in (None, "")
    transport_min = _optional_int(user_transport_raw, _optional_int(estimated_transport, 45))
    if transport_min is None:
        transport_min = 45
    transport_source = "用户填写" if user_filled else "机场估算"

    margin_mode = str(constraints.get("transport_margin_mode") or "standard").strip().lower()
    if margin_mode not in {"tight", "standard", "loose"}:
        margin_mode = "standard"
    safety_min = _optional_int(constraints.get("redundancy_min"), 25)
    if safety_min is None:
        safety_min = 25
    business_start = _parse_time_minutes(constraints.get("business_start"))
    business_end = _parse_time_minutes(constraints.get("business_end"))
    if direction == "outbound":
        airport_buffer = get_arrival_buffer(airport, route_type)
        buffer_label = "到达机场缓冲"
        display_label = route_type_buffer_label(route_type, "arrival")
        travel_hour = (business_start - transport_min) / 60 if business_start is not None else None
    else:
        airport_buffer = get_departure_buffer(airport, route_type)
        buffer_label = "值机安检缓冲"
        display_label = route_type_buffer_label(route_type, "departure")
        travel_hour = business_end / 60 if business_end is not None else None

    margin, ratio, rush = calc_transport_margin(transport_min, margin_mode, travel_hour)
    total = airport_buffer + transport_min + margin + safety_min
    breakdown = {
        "airport_iata": airport,
        "airport_size": logistics.get("size") or "medium",
        "airport_buffer_min": airport_buffer,
        "buffer_label": buffer_label,
        "buffer_detail_label": display_label,
        "transport_min": transport_min,
        "transport_source": transport_source,
        "estimated_transport_min": _optional_int(estimated_transport, 45) or 45,
        "margin_min": margin,
        "margin_ratio": round(ratio, 2),
        "margin_mode": margin_mode,
        "rush_hour": rush,
        "safety_min": safety_min,
        "total_min": total,
        "route_type": str(route_type or "domestic").strip() or "domestic",
        "direction": direction,
        "legacy": False,
    }
    print(
        f"[预留-计算侧] {direction} 机场缓冲={airport_buffer} 车程={transport_min} "
        f"车程来源={transport_source} 路途冗余={margin}(系数{round(ratio, 2)},高峰={rush}) "
        f"安全余量={safety_min} 总计={total}分钟"
    )
    return breakdown


def _legacy_reserve_breakdown(
    reserve_minutes: int,
    buffer_h: float,
    transport_min: int,
    airport_iata: str | None,
) -> dict:
    airport = str(airport_iata or "").strip().upper()
    base = {
        "airport_iata": airport,
        "transport_min": transport_min,
        "transport_source": "机场估算",
        "buffer_hours": buffer_h,
        "total_min": reserve_minutes,
        "legacy": True,
    }
    return {
        "legacy": True,
        "outbound": {**base, "direction": "outbound"},
        "return": {**base, "direction": "return"},
    }


def _time_text(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.strftime("%H:%M")


def _parse_set_off_datetime(value: str | None, date_str: str | None) -> datetime | None:
    return parse_flight_time(value, date_str)


def analyze_departure_feasibility(
    set_off_time,
    flight: dict,
    route_type: str,
    transport_min: int | float | None = None,
    margin_mode: str | None = "standard",
    date_str: str | None = None,
    safety_min: int = 25,
) -> dict:
    """Analyze whether a user set-off time can catch a flight."""
    flight = flight or {}
    dep_airport = _flight_airport(flight, "departure_airport", "dep_airport", "origin")
    set_off_dt = _parse_set_off_datetime(str(set_off_time or "").strip(), date_str)
    dep_dt = _flight_departure_datetime(flight, date_str)
    if not set_off_dt or not dep_dt:
        return {}
    if dep_dt < set_off_dt:
        dep_dt += timedelta(days=1)
    estimated_transport = _optional_int(get_airport_logistics(dep_airport).get("to_center_min"), 45) or 45
    transport = _optional_int(transport_min, estimated_transport) or estimated_transport
    margin, ratio, rush = calc_transport_margin(
        transport,
        margin_mode,
        set_off_dt.hour + set_off_dt.minute / 60,
    )
    dep_buffer = get_departure_buffer(dep_airport, route_type)
    total = transport + margin + dep_buffer + safety_min
    earliest_catchable = set_off_dt + timedelta(minutes=total)
    gap = int(round((dep_dt - earliest_catchable).total_seconds() / 60))
    base = {
        "set_off_time": _time_text(set_off_dt),
        "flight_departure_time": _time_text(dep_dt),
        "departure_airport": dep_airport,
        "route_type": route_type or "domestic",
        "transport_min": transport,
        "transport_margin_min": margin,
        "transport_margin_ratio": round(ratio, 2),
        "transport_rush": rush,
        "departure_buffer_min": dep_buffer,
        "buffer_label": route_type_buffer_label(route_type, "departure"),
        "safety_min": safety_min,
        "total_reserve": total,
        "earliest_catchable": _time_text(earliest_catchable),
    }
    if gap >= 30:
        base.update({"level": "可行", "margin_min": gap})
    elif gap >= 0:
        base.update({"level": "紧张", "margin_min": gap})
    else:
        need_set_off = dep_dt - timedelta(minutes=total)
        base.update({"level": "不可行", "short_min": -gap, "need_set_off": _time_text(need_set_off)})
    return base


def compute_same_day_windows(
    subscription: dict | None,
    origin_airport: str | None = None,
    dest_airport: str | None = None,
) -> dict:
    """Reverse-calculate flight windows from business time, transport and buffer."""
    constraints = _same_day_constraints(subscription)
    business_start = _parse_time_minutes(constraints.get("business_start"))
    business_end = _parse_time_minutes(constraints.get("business_end"))
    if business_start is None or business_end is None:
        return {}
    mode = str(constraints.get("transport_mode") or "taxi").strip().lower()
    logistics = get_airport_logistics(dest_airport or "")
    estimated_transport = logistics.get("transit_min") if mode == "transit" else logistics.get("to_center_min")
    transport_min = _optional_int(constraints.get("user_transport_min"), _optional_int(estimated_transport, 45))
    if transport_min is None:
        transport_min = 45

    fixed_model_keys = (
        "meeting_importance",
        "destination_transport_min",
        "airport_to_meeting_min",
        "meeting_transport_min",
        "origin_transport_min",
        "airport_advance_min",
        "departure_airport_process_min",
        "arrival_exit_min",
        "delay_buffer_min",
        "pre_meeting_buffer_min",
        "post_meeting_buffer_min",
        "custom_redundancy_min",
        "meeting_location",
        "business_location",
        "meeting_area",
        "destination_area",
    )
    same_day_meeting = bool(constraints.get("same_day_round_trip"))
    has_fixed_meeting_model = same_day_meeting or any(constraints.get(key) not in (None, "") for key in fixed_model_keys)
    if has_fixed_meeting_model:
        route_type = str(constraints.get("route_type") or "domestic").strip() or "domestic"
        outbound_breakdown = compute_meeting_fixed_breakdown(
            {"constraints": constraints},
            "outbound",
            dest_airport,
            route_type,
        )
        return_breakdown = compute_meeting_fixed_breakdown(
            {"constraints": constraints},
            "return",
            dest_airport,
            route_type,
        )
        outbound_planning_reserve = outbound_breakdown["total_min"]
        outbound_reserve = outbound_breakdown.get("outbound_window_total_min") or outbound_planning_reserve
        return_reserve = return_breakdown["total_min"]
        outbound_arrive_by = business_start - outbound_reserve
        return_depart_after = business_end + return_reserve
        latest_landing_without_preparation = business_start - (
            outbound_planning_reserve - int(outbound_breakdown.get("pre_meeting_buffer_min") or 0)
        )
        business_safety_arrive_by = business_start - outbound_planning_reserve
        reserve_breakdown = {
            "legacy": False,
            "model": "meeting_fixed",
            "outbound": outbound_breakdown,
            "return": return_breakdown,
            "windows": {
                "arrive_by": _minutes_to_text(outbound_arrive_by),
                "depart_after": _minutes_to_text(return_depart_after),
                "arrive_by_minutes": outbound_arrive_by,
                "depart_after_minutes": return_depart_after,
                "latest_landing_without_preparation": _minutes_to_text(latest_landing_without_preparation),
                "business_safety_arrive_by": _minutes_to_text(business_safety_arrive_by),
                "business_safety_arrive_by_minutes": business_safety_arrive_by,
            },
        }
        return _attach_same_day_airport_window_maps({
            "buffer_model": "meeting_fixed",
            "meeting_importance": outbound_breakdown.get("importance"),
            "meeting_importance_label": outbound_breakdown.get("importance_label"),
            "outbound_arrive_by_minutes": outbound_arrive_by,
            "return_depart_after_minutes": return_depart_after,
            "outbound_arrive_by": _minutes_to_text(outbound_arrive_by),
            "return_depart_after": _minutes_to_text(return_depart_after),
            "latest_landing_without_preparation_minutes": latest_landing_without_preparation,
            "latest_landing_without_preparation": _minutes_to_text(latest_landing_without_preparation),
            "business_safety_arrive_by_minutes": business_safety_arrive_by,
            "business_safety_arrive_by": _minutes_to_text(business_safety_arrive_by),
            "business_start": _minutes_to_text(business_start),
            "business_end": _minutes_to_text(business_end),
            "transport_min": outbound_breakdown.get("destination_transport_min"),
            "estimated_transport_min": _optional_int(estimated_transport, 45) or 45,
            "user_transport_min": _optional_int(constraints.get("user_transport_min")),
            "destination_transport_min": outbound_breakdown.get("destination_transport_min"),
            "arrival_exit_min": outbound_breakdown.get("arrival_exit_min"),
            "delay_buffer_min": outbound_breakdown.get("delay_buffer_min"),
            "pre_meeting_buffer_min": outbound_breakdown.get("pre_meeting_buffer_min"),
            "post_meeting_buffer_min": return_breakdown.get("post_meeting_buffer_min"),
            "airport_advance_min": return_breakdown.get("departure_airport_process_min"),
            "outbound_transport_margin_min": outbound_breakdown.get("destination_transport_margin_min"),
            "return_transport_margin_min": return_breakdown.get("meeting_to_airport_margin_min"),
            "outbound_transport_margin_ratio": outbound_breakdown.get("destination_transport_margin_ratio"),
            "return_transport_margin_ratio": return_breakdown.get("meeting_to_airport_margin_ratio"),
            "outbound_transport_rush": outbound_breakdown.get("destination_transport_rush"),
            "return_transport_rush": return_breakdown.get("meeting_to_airport_rush"),
            "arrival_buffer_min": outbound_breakdown.get("arrival_exit_min"),
            "checkin_buffer_min": return_breakdown.get("departure_airport_process_min"),
            "outbound_reserve_minutes": outbound_reserve,
            "outbound_planning_reserve_minutes": outbound_planning_reserve,
            "return_reserve_minutes": return_reserve,
            "reserve_minutes": outbound_reserve,
            "reserve_h": round(outbound_reserve / 60, 2),
            "buffer_h": round(outbound_reserve / 60, 2),
            "transport_mode": "transit" if mode == "transit" else "taxi",
            "airport_size": logistics.get("size") or "medium",
            "route_type": route_type,
            "reserve_breakdown": reserve_breakdown,
        }, dest_airport)

    has_new_buffer_fields = (
        constraints.get("user_transport_min") not in (None, "")
        or constraints.get("redundancy_min") not in (None, "")
        or constraints.get("buffer_hours") in (None, "")
    )
    if has_new_buffer_fields:
        route_type = str(constraints.get("route_type") or "domestic").strip() or "domestic"
        outbound_breakdown = compute_reserve_breakdown(
            {"constraints": constraints},
            "outbound",
            dest_airport,
            route_type,
        )
        return_breakdown = compute_reserve_breakdown(
            {"constraints": constraints},
            "return",
            dest_airport,
            route_type,
        )
        transport_min = outbound_breakdown["transport_min"]
        redundancy_min = outbound_breakdown["safety_min"]
        margin_mode = outbound_breakdown["margin_mode"]
        arrival_buffer_min = outbound_breakdown["airport_buffer_min"]
        checkin_buffer_min = return_breakdown["airport_buffer_min"]
        outbound_margin = outbound_breakdown["margin_min"]
        return_margin = return_breakdown["margin_min"]
        outbound_ratio = outbound_breakdown["margin_ratio"]
        return_ratio = return_breakdown["margin_ratio"]
        outbound_rush = outbound_breakdown["rush_hour"]
        return_rush = return_breakdown["rush_hour"]
        outbound_reserve = outbound_breakdown["total_min"]
        return_reserve = return_breakdown["total_min"]
        outbound_arrive_by = business_start - outbound_reserve
        return_depart_after = business_end + return_reserve
        reserve_breakdown = {
            "legacy": False,
            "outbound": outbound_breakdown,
            "return": return_breakdown,
            "windows": {
                "arrive_by": _minutes_to_text(outbound_arrive_by),
                "depart_after": _minutes_to_text(return_depart_after),
                "arrive_by_minutes": outbound_arrive_by,
                "depart_after_minutes": return_depart_after,
            },
        }
        return _attach_same_day_airport_window_maps({
            "buffer_model": "airport_split",
            "outbound_arrive_by_minutes": outbound_arrive_by,
            "return_depart_after_minutes": return_depart_after,
            "outbound_arrive_by": _minutes_to_text(outbound_arrive_by),
            "return_depart_after": _minutes_to_text(return_depart_after),
            "business_start": _minutes_to_text(business_start),
            "business_end": _minutes_to_text(business_end),
            "transport_min": transport_min,
            "estimated_transport_min": _optional_int(estimated_transport, 45) or 45,
            "user_transport_min": _optional_int(constraints.get("user_transport_min")),
            "redundancy_min": redundancy_min,
            "transport_margin_mode": margin_mode,
            "route_type": route_type,
            "arrival_buffer_label": route_type_buffer_label(route_type, "arrival"),
            "departure_buffer_label": route_type_buffer_label(route_type, "departure"),
            "outbound_transport_margin_min": outbound_margin,
            "return_transport_margin_min": return_margin,
            "outbound_transport_margin_ratio": round(outbound_ratio, 2),
            "return_transport_margin_ratio": round(return_ratio, 2),
            "outbound_transport_rush": outbound_rush,
            "return_transport_rush": return_rush,
            "arrival_buffer_min": arrival_buffer_min,
            "checkin_buffer_min": checkin_buffer_min,
            "outbound_reserve_minutes": outbound_reserve,
            "return_reserve_minutes": return_reserve,
            "reserve_minutes": outbound_reserve,
            "reserve_h": round(outbound_reserve / 60, 2),
            "buffer_h": round(outbound_reserve / 60, 2),
            "transport_mode": "transit" if mode == "transit" else "taxi",
            "airport_size": logistics.get("size") or "medium",
            "reserve_breakdown": reserve_breakdown,
        }, dest_airport)
    buffer_h = _to_float(constraints.get("buffer_hours"))
    if buffer_h is None:
        buffer_h = 2.5
    buffer_minutes = int(round(buffer_h * 60))
    reserve_minutes = max(buffer_minutes, transport_min + 60)
    outbound_arrive_by = business_start - reserve_minutes
    return_depart_after = business_end + reserve_minutes
    reserve_breakdown = _legacy_reserve_breakdown(reserve_minutes, buffer_h, transport_min, dest_airport)
    reserve_breakdown["windows"] = {
        "arrive_by": _minutes_to_text(outbound_arrive_by),
        "depart_after": _minutes_to_text(return_depart_after),
        "arrive_by_minutes": outbound_arrive_by,
        "depart_after_minutes": return_depart_after,
    }
    return _attach_same_day_airport_window_maps({
        "outbound_arrive_by_minutes": outbound_arrive_by,
        "return_depart_after_minutes": return_depart_after,
        "outbound_arrive_by": _minutes_to_text(outbound_arrive_by),
        "return_depart_after": _minutes_to_text(return_depart_after),
        "business_start": _minutes_to_text(business_start),
        "business_end": _minutes_to_text(business_end),
        "transport_min": transport_min,
        "buffer_h": buffer_h,
        "reserve_minutes": reserve_minutes,
        "reserve_h": round(reserve_minutes / 60, 2),
        "transport_mode": "transit" if mode == "transit" else "taxi",
        "reserve_breakdown": reserve_breakdown,
    }, dest_airport)


def build_same_day_combos(
    outbound_flights: list[dict] | None,
    return_flights: list[dict] | None,
    windows_or_date: dict | str | None = None,
    date_str: str | None = None,
    min_stay_hours: float = 4,
    constraints: dict | None = None,
) -> list[dict]:
    """Build feasible same-day business round-trip combinations."""
    windows = windows_or_date if isinstance(windows_or_date, dict) else None
    external_windows = windows if isinstance(windows, dict) else None
    if date_str is None and isinstance(windows_or_date, str):
        date_str = windows_or_date
    combos: list[dict] = []
    normalized_constraints = _same_day_constraints(constraints or {})
    default_profile = _same_day_default_profile(normalized_constraints.get("day_trip_period"))
    relaxed_profile = _same_day_relaxed_profile(normalized_constraints.get("day_trip_period"))
    has_time_window_constraints = bool(
        normalized_constraints.get("business_start") and normalized_constraints.get("business_end")
    )
    outbound_flights = outbound_flights or []
    return_flights = return_flights or []
    if has_time_window_constraints and isinstance(windows, dict):
        window_airports = [
            *[_flight_airport(flight or {}, "arrival_airport") for flight in outbound_flights],
            *[_flight_airport(flight or {}, "departure_airport") for flight in return_flights],
        ]
        ensured_windows = _ensure_same_day_airport_window_maps(windows, normalized_constraints, window_airports)
        if external_windows is not None:
            external_windows.clear()
            external_windows.update(ensured_windows)
            windows = external_windows
        else:
            windows = ensured_windows
    print(f"[会议比较] 待比较去程数量={len(outbound_flights)}")
    for profile in (default_profile, relaxed_profile):
        for outbound in outbound_flights or []:
            outbound_dep = _flight_departure_minutes(outbound or {})
            outbound_arr = _flight_arrival_minutes(outbound or {})
            outbound_arr_dt = _flight_arrival_datetime(outbound or {}, date_str)
            outbound_price = _to_float((outbound or {}).get("price"))
            if outbound_dep is None or outbound_arr is None or outbound_price is None:
                continue
            dest_airport = _flight_airport(outbound or {}, "arrival_airport")
            active_windows = _same_day_windows_for_airport(windows, constraints or {}, dest_airport)
            if isinstance(windows, dict) and active_windows and dest_airport:
                windows.update(_merge_same_day_airport_window_maps(
                    windows,
                    _attach_same_day_airport_window_maps(active_windows, dest_airport),
                ))
            if active_windows:
                arrive_by = active_windows.get("outbound_arrive_by_minutes")
                arrive_by_dt, return_after_dt = _same_day_window_datetimes(active_windows, date_str)
                arrive_limit = arrive_by_dt or active_windows.get("outbound_arrive_by")
                print(
                    f"[会议窗口] 去程到达上限={arrive_limit} "
                    f"类型={type(arrive_limit)}"
                )
                print(
                    f"[会议窗口] 返程出发下限={return_after_dt or active_windows.get('return_depart_after')} "
                    f"类型={type(return_after_dt or active_windows.get('return_depart_after'))}"
                )
                passed = _same_day_outbound_passes_window(outbound or {}, active_windows, date_str)
                print(
                    f"[会议比较] 去程{outbound.get('flight_no') or outbound.get('flight_combo')} "
                    f"原始到达={repr(_first_time_text(outbound or {}, 'arrival_time', 'arr_time'))} "
                    f"解析后={outbound_arr_dt if outbound_arr_dt is not None else outbound_arr} "
                    f"类型={type(outbound_arr_dt if outbound_arr_dt is not None else outbound_arr)} "
                    f"通过={passed}"
                )
                if not passed:
                    continue
            else:
                if outbound_dep < int(profile["outbound_depart_after"]):
                    continue
                if outbound_arr > int(profile["outbound_arrive_by"]):
                    continue
            for return_flight in return_flights:
                return_dep = _flight_departure_minutes(return_flight or {})
                return_arr = _flight_arrival_minutes(return_flight or {})
                return_dep_dt = _flight_departure_datetime(return_flight or {}, date_str)
                return_arr_dt = _flight_arrival_datetime(return_flight or {}, date_str)
                return_price = _to_float((return_flight or {}).get("price"))
                if return_dep is None or return_arr is None or return_price is None:
                    continue
                return_airport = _flight_airport(return_flight or {}, "departure_airport")
                return_windows = _same_day_windows_for_airport(windows, constraints or {}, return_airport)
                if isinstance(windows, dict) and return_windows and return_airport:
                    windows.update(_merge_same_day_airport_window_maps(
                        windows,
                        _attach_same_day_airport_window_maps(return_windows, return_airport),
                    ))
                if active_windows or return_windows:
                    _, return_after_dt = _same_day_window_datetimes(return_windows, date_str)
                    passed = _same_day_return_passes_window(return_flight or {}, return_windows, date_str)
                    print(
                        f"[\u4f1a\u8bae\u6bd4\u8f83] \u8fd4\u7a0b{return_flight.get('flight_no') or return_flight.get('flight_combo')} "
                        f"\u539f\u59cb\u51fa\u53d1={repr(_first_time_text(return_flight or {}, 'departure_time', 'dep_time'))} "
                        f"\u89e3\u6790\u540e={return_dep_dt if return_dep_dt is not None else return_dep} "
                        f"\u7c7b\u578b={type(return_dep_dt if return_dep_dt is not None else return_dep)} "
                        f"\u901a\u8fc7={passed}"
                    )
                    if not passed:
                        continue
                else:
                    if return_dep < int(profile["return_depart_after"]):
                        continue
                    if return_arr > int(profile["return_arrive_by"]):
                        continue
                if return_arr_dt is not None and return_dep_dt is not None:
                    if return_arr_dt < return_dep_dt:
                        continue
                elif return_arr < return_dep:
                    continue
                if return_dep_dt is not None and outbound_arr_dt is not None:
                    stay_minutes = (return_dep_dt - outbound_arr_dt).total_seconds() / 60
                else:
                    stay_minutes = return_dep - outbound_arr
                min_stay_minutes = min_stay_hours * 60 if active_windows else int(profile["min_stay_minutes"])
                if stay_minutes < min_stay_minutes:
                    continue
                outbound_margin = None
                return_margin = None
                business_feasibility = {}
                business_feasibility_rank = 0
                if active_windows:
                    arrive_by_minutes = active_windows.get("outbound_arrive_by_minutes")
                    return_after_minutes = return_windows.get("return_depart_after_minutes")
                    if arrive_by_minutes is not None:
                        safety_arrive_by = active_windows.get("business_safety_arrive_by_minutes")
                        margin_arrive_by = safety_arrive_by if safety_arrive_by is not None else arrive_by_minutes
                        outbound_margin = int(round(float(margin_arrive_by) - float(outbound_arr)))
                        business_feasibility["outbound"] = classify_business_time_margin(outbound_margin)
                    if return_after_minutes is not None:
                        return_margin = int(round(float(return_dep) - float(return_after_minutes)))
                        business_feasibility["return"] = classify_business_time_margin(return_margin)
                    if business_feasibility:
                        business_feasibility_rank = max(
                            int(item.get("rank", 0) or 0) for item in business_feasibility.values()
                        )
                outbound_transport_min = _same_day_outbound_transport_minutes(active_windows)
                return_transport_min = _same_day_return_transport_minutes(return_windows)
                airport_transport_total_min = None
                if outbound_transport_min is not None and return_transport_min is not None:
                    airport_transport_total_min = outbound_transport_min + return_transport_min
                total = outbound_price + return_price
                note = ""
                if active_windows:
                    note = f"去程{_minutes_to_text(outbound_arr)}到,返程{_minutes_to_text(return_dep)}走,办事时间充足"
                elif profile.get("relaxed"):
                    note = (
                        "当天往返时间较紧,已适当放宽默认早去晚回规则；"
                        "如不便可考虑前一晚到达"
                    )
                else:
                    note = (
                        f"当天往返默认方案(按{profile['label']}):去程选白天较早班,返程选傍晚/晚班,"
                        f"中间办事时间约{round(stay_minutes / 60, 1):g}小时；"
                        "如需精确按会议时间安排,可进精准模式填写会议时段"
                    )
                combo = {
                    "outbound": outbound,
                    "return": return_flight,
                    "outbound_price": outbound_price,
                    "return_price": return_price,
                    "total_price": total,
                    "roundtrip_price": total,
                    "transaction_total": (
                        (_flight_transaction_price(outbound) or outbound_price)
                        + (_flight_transaction_price(return_flight) or return_price)
                    ),
                    "same_day_round_trip": True,
                    "same_day_return_windows": return_windows,
                    "outbound_destination_transport_min": outbound_transport_min,
                    "return_meeting_to_airport_min": return_transport_min,
                    "airport_transport_total_min": airport_transport_total_min,
                    "stay_hours": round(stay_minutes / 60, 1),
                    "feasible": True,
                    "same_day_windows": active_windows,
                    "business_feasibility": business_feasibility,
                    "business_feasibility_rank": business_feasibility_rank,
                    "meeting_arrival_margin_min": outbound_margin,
                    "return_departure_margin_min": return_margin,
                    "schedule_note": note,
                    "tag": "当天往返可行",
                }
                if not active_windows:
                    combo["same_day_default_period"] = _normalize_day_trip_period((constraints or {}).get("day_trip_period"))
                    combo["same_day_default_score"] = _same_day_default_combo_score(combo, profile)
                    combo["same_day_default_relaxed"] = bool(profile.get("relaxed"))
                combos.append(combo)
        if combos or has_time_window_constraints:
            break
    for combo in combos:
        if combo.get("same_day_round_trip"):
            combo["tag"] = "当天往返可行"
            if combo.get("same_day_windows"):
                outbound_arr = _flight_arrival_minutes(combo.get("outbound") or {})
                return_dep = _flight_departure_minutes(combo.get("return") or {})
                combo["schedule_note"] = (
                    f"去程{_minutes_to_text(outbound_arr)}到,返程{_minutes_to_text(return_dep)}走,办事时间充足"
                )
    if combos and not any(combo.get("same_day_windows") for combo in combos):
        return sorted(
            combos,
            key=lambda item: (
                1 if item.get("same_day_default_relaxed") else 0,
                item.get("same_day_default_score") or 999999,
                item.get("total_price") or 999999,
            ),
        )
    return sorted(
        combos,
        key=lambda item: (
            item.get("business_feasibility_rank") or 0,
            item.get("total_price") or 999999,
            item.get("airport_transport_total_min") if item.get("airport_transport_total_min") is not None else 999999,
        ),
    )


def _same_day_no_feasible_note(
    outbound_flights: list[dict] | None,
    return_flights: list[dict] | None,
    constraints: dict | None,
) -> str:
    outbound_candidates = [
        flight for flight in outbound_flights or [] if _flight_arrival_datetime(flight or {}, _date_from_same_day_source(constraints or {})) is not None
    ]
    return_candidates = [
        flight for flight in return_flights or [] if _flight_departure_datetime(flight or {}, _date_from_same_day_source(constraints or {})) is not None
    ]
    if not outbound_candidates or not return_candidates:
        return "本次无方案主因是【时间窗口】：缺少去程或返程完整时间，无法组成当天往返。建议前一晚到达或继续监控。"

    date_str = _date_from_same_day_source(constraints or {})
    target_date = None
    if date_str:
        try:
            target_date = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
        except ValueError:
            target_date = None

    same_day_arrivals = []
    for flight in outbound_candidates:
        arrival_dt = _flight_arrival_datetime(flight or {}, date_str)
        if arrival_dt is None:
            continue
        if target_date and arrival_dt.date() != target_date:
            continue
        same_day_arrivals.append((arrival_dt, flight))
    same_day_returns = []
    for flight in return_candidates:
        departure_dt = _flight_departure_datetime(flight or {}, date_str)
        if departure_dt is None:
            continue
        if target_date and departure_dt.date() != target_date:
            continue
        same_day_returns.append((departure_dt, flight))

    window_reference_flight = min(same_day_arrivals, key=lambda item: item[0])[1] if same_day_arrivals else outbound_candidates[0]
    dest_airport = _flight_airport(window_reference_flight, "arrival_airport")
    windows = compute_same_day_windows(constraints or {}, None, dest_airport)
    if not windows:
        return "本次无方案主因是【时间窗口】：缺少会议时间或机场窗口，无法判断当天往返。建议前一晚到达或补充会议时间。"

    closest_options = _closest_same_day_outbound_options(outbound_candidates, windows, date_str, limit=1, constraints=constraints)
    return_ok = sum(
        1
        for flight in return_candidates
        if _same_day_return_passes_window(
            flight or {},
            _same_day_windows_for_airport(
                windows,
                constraints or {},
                _flight_airport(flight or {}, "departure_airport"),
            ),
            date_str,
        )
    )

    def _physical_impossibility_note() -> str:
        if not same_day_arrivals:
            return ""
        earliest_dt, earliest_flight = min(same_day_arrivals, key=lambda item: item[0])
        active_windows = _same_day_windows_for_airport(
            windows,
            constraints or {},
            _flight_airport(earliest_flight or {}, "arrival_airport"),
        )
        breakdown = (active_windows.get("reserve_breakdown") or {}).get("outbound") or {}
        arrival_exit = _optional_int(breakdown.get("arrival_exit_min"), _optional_int(active_windows.get("arrival_buffer_min"), 0)) or 0
        transport_min = _optional_int(
            breakdown.get("destination_transport_min"),
            _same_day_outbound_transport_minutes(active_windows),
        ) or 0
        minimum_passage = arrival_exit + transport_min
        if minimum_passage <= 0:
            return ""
        normalized = _same_day_constraints(constraints or {})
        meeting_start = parse_flight_time(normalized.get("business_start"), date_str)
        if meeting_start is None:
            return ""
        earliest_meeting_arrival = earliest_dt + timedelta(minutes=minimum_passage)
        if earliest_meeting_arrival <= meeting_start:
            return ""
        meeting_text = _time_text(meeting_start)
        feasible_meeting_text = _time_text(earliest_meeting_arrival)
        arrival_text = _time_text(earliest_dt)
        return (
            f"\u6700\u65e9\u5230\u8fbe {arrival_text},\u843d\u5730\u5230\u4f1a\u573a\u6700\u5c11\u9700{minimum_passage}\u5206\u949f,"
            f"\u8be5\u822a\u7ebf\u5f53\u5929\u65e0\u6cd5\u6ee1\u8db3 {meeting_text} \u4f1a\u8bae;"
            f"\u53ef\u8003\u8651\u524d\u4e00\u665a\u5230\u8fbe\u6216\u5c06\u4f1a\u8bae\u63a8\u8fdf\u81f3 \u2265{feasible_meeting_text}\u3002"
        )

    physical_note = _physical_impossibility_note()

    advice_parts = []
    relaxed_constraints = dict(_same_day_constraints(constraints or {}))
    relaxed_label = "放宽预留"
    if windows.get("buffer_model") == "airport_split":
        relaxed_constraints["transport_margin_mode"] = "tight"
        relaxed_label = "路途冗余改为紧凑"
    elif windows.get("buffer_model") == "meeting_fixed":
        relaxed_constraints["meeting_importance"] = "normal"
        for key in ("arrival_exit_min", "delay_buffer_min", "pre_meeting_buffer_min", "airport_advance_min"):
            relaxed_constraints[key] = ""
        relaxed_label = "会议重要程度改为普通商务"
    else:
        relaxed_constraints["buffer_hours"] = 2
        relaxed_label = "缩短预留至2小时"
    relaxed_windows = compute_same_day_windows({"constraints": relaxed_constraints}, None, dest_airport)
    if relaxed_windows:
        relaxed_count = sum(
            1
            for flight in outbound_candidates
            if _same_day_outbound_passes_window(
                flight or {},
                _same_day_windows_for_airport(
                    relaxed_windows,
                    relaxed_constraints,
                    _flight_airport(flight or {}, "arrival_airport"),
                ),
                date_str,
            )
        )
        advice_parts.append(f"{relaxed_label}后，到达上限{relaxed_windows.get('outbound_arrive_by')}，有{relaxed_count}个去程可选")
    advice_parts.append("或前一晚到达")
    advice = "；".join(advice_parts)

    if closest_options:
        flight = closest_options[0]
        diff = int(flight.get("meeting_arrival_delay_minutes") or 0)
        flight_no = flight.get("flight_no") or flight.get("flight_combo") or "最接近去程"
        arrival_dt = _flight_arrival_datetime(flight or {}, date_str)
        arrival_text = arrival_dt.strftime("%H:%M") if arrival_dt else _first_time_text(flight, "arrival_time", "arr_time") or "待确认"
        window_text = flight.get("window_arrive_by") or windows.get("outbound_arrive_by") or "待确认"
        if diff > 0:
            diff_text = f"{diff // 60}h{diff % 60}m" if diff >= 60 else f"{diff}分钟"
            return_clause = (
                f"\u8fd4\u7a0b\u6709{return_ok}\u4e2a\u53ef\u9009,\u975e\u963b\u585e"
                if return_ok > 0
                else "\u8fd4\u7a0b\u6682\u65e0\u7b26\u5408\u822a\u73ed,\u4f46\u4e3b\u56e0\u4ecd\u5148\u6309\u53bb\u7a0b\u65f6\u95f4\u5224\u5b9a"
            )
            next_step = physical_note or "\u5efa\u8bae\u524d\u4e00\u665a\u5230\u8fbe\u6216\u8c03\u4f4e\u9884\u7559/\u4f1a\u8bae\u91cd\u8981\u5ea6\u3002"
            return (
                f"\u672c\u6b21\u65e0\u65b9\u6848\u4e3b\u56e0\u662f\u3010\u53bb\u7a0b\u65f6\u95f4\u3011:\u6700\u65e9{flight_no} {arrival_text}\u5230,"
                f"\u9700{window_text}\u524d\u843d\u5730,\u665a{diff_text};"
                f"{return_clause};"
                f"{next_step}"
            )
        if return_ok == 0:
            return f"本次无方案主因是【返程时间】：去程可赶到，但返程需 {windows.get('return_depart_after')} 后出发，当天没有符合返程窗口的航班。建议：次日返程或调整会议结束时间。"
        return f"本次无方案主因是【时间窗口】：没有完整往返组合同时满足去程到会和返程起飞窗口。建议：{advice}。"

    if return_ok == 0:
        latest_departure = None
        if same_day_returns:
            latest_dt = max(dt for dt, _ in same_day_returns)
            latest_departure = latest_dt.strftime("%H:%M")
        latest_text = f"，当天最晚返程 {latest_departure}" if latest_departure else ""
        return f"本次无方案主因是【返程时间】：返程需 {windows.get('return_depart_after')} 后出发{latest_text}，无符合航班。建议次日返程或调整会议结束时间。"

    return f"本次无方案主因是【时间窗口】：当天没有航班能组成完整往返。建议：{advice}。"

def _closest_same_day_outbound_options(
    outbound_flights: list[dict] | None,
    windows: dict | None,
    date_str: str | None,
    limit: int = 3,
    constraints: dict | None = None,
) -> list[dict]:
    options = []
    target_date = None
    if date_str:
        try:
            target_date = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
        except ValueError:
            target_date = None
    for flight in outbound_flights or []:
        arrival_dt = _flight_arrival_datetime(flight or {}, date_str)
        departure_dt = _flight_departure_datetime(flight or {}, date_str)
        if target_date and arrival_dt is not None and arrival_dt.date() != target_date:
            continue
        if target_date and departure_dt is not None and departure_dt.date() != target_date:
            continue
        arrival_minutes = _flight_arrival_minutes(flight or {})
        if arrival_dt is None and arrival_minutes is None:
            continue
        airport = _flight_airport(flight or {}, "arrival_airport")
        active_windows = _same_day_windows_for_airport(windows, constraints or {}, airport)
        arrive_by_dt, _ = _same_day_window_datetimes(active_windows or {}, date_str)
        arrive_by_minutes = (active_windows or {}).get("outbound_arrive_by_minutes")
        item = dict(flight)
        if arrival_dt is not None and arrive_by_dt is not None:
            delay = max(0, int(round((arrival_dt - arrive_by_dt).total_seconds() / 60)))
            sort_key = arrival_dt
        elif arrival_minutes is not None:
            delay = max(0, int(arrival_minutes - (arrive_by_minutes or arrival_minutes)))
            sort_key = _minutes_datetime(date_str, arrival_minutes) or datetime(1900, 1, 1) + timedelta(minutes=arrival_minutes)
        else:
            continue
        item["meeting_arrival_delay_minutes"] = delay
        item["same_day_windows"] = active_windows
        transport_min = _same_day_outbound_transport_minutes(active_windows)
        item["destination_transport_min"] = transport_min
        item["window_arrive_by"] = (active_windows or {}).get("outbound_arrive_by")
        if delay >= 60:
            item["meeting_arrival_note"] = f"晚{delay // 60}小时{delay % 60}分钟"
        elif delay > 0:
            item["meeting_arrival_note"] = f"晚{delay}分钟"
        else:
            item["meeting_arrival_note"] = "满足会议到达窗口"
        transport_sort = transport_min if transport_min is not None else 9999
        options.append((delay, transport_sort, sort_key, item))
    options.sort(key=lambda pair: (pair[0], pair[1], pair[2]))
    return [item for _, _, _, item in options[:limit]]


def pick_earliest_same_day(
    raw_valid_outbound: list[dict] | None,
    depart_date: str | None,
    windows: dict | None = None,
    source_label: str = "raw_valid_outbound",
    constraints: dict | None = None,
) -> dict | None:
    """Pick the closest same-day arrival, using per-airport meeting windows when available."""
    raw_valid_outbound = raw_valid_outbound or []
    target_date = None
    if depart_date:
        try:
            target_date = datetime.strptime(str(depart_date)[:10], "%Y-%m-%d").date()
        except ValueError:
            target_date = None
    candidates: list[tuple[int, datetime, dict]] = []
    for flight in raw_valid_outbound:
        dep = _flight_departure_datetime(flight or {}, depart_date)
        arr = _flight_arrival_datetime(flight or {}, depart_date)
        if not dep or not arr:
            continue
        if target_date and dep.date() != target_date:
            continue
        if target_date and arr.date() != target_date:
            continue
        active_windows = _same_day_windows_for_airport(
            windows,
            constraints or {},
            _flight_airport(flight or {}, "arrival_airport"),
        )
        arrive_by_dt, _ = _same_day_window_datetimes(active_windows or {}, depart_date)
        item = dict(flight)
        delay = 0
        if arrive_by_dt:
            delay = max(0, int(round((arr - arrive_by_dt).total_seconds() / 60)))
            item["meeting_arrival_delay_minutes"] = delay
            item["same_day_windows"] = active_windows
            item["destination_transport_min"] = _same_day_outbound_transport_minutes(active_windows)
            if delay >= 60:
                item["meeting_arrival_note"] = f"晚{delay // 60}小时{delay % 60}分钟"
            elif delay > 0:
                item["meeting_arrival_note"] = f"晚{delay}分钟"
            else:
                item["meeting_arrival_note"] = "满足会议到达窗口"
        candidates.append((delay, arr, item))

    candidates.sort(key=lambda pair: (pair[0], pair[1]))
    print(f"[最早班调试] 候选池来源变量名和数量: {source_label}={len(raw_valid_outbound)}")
    print(
        "[最早班调试] 按到达时间排序前5: "
        + str(
            [
                (
                    flight.get("flight_no") or flight.get("flight_combo"),
                    flight.get("departure_time") or flight.get("dep_time"),
                    flight.get("arrival_time") or flight.get("arr_time"),
                )
                for _, _, flight in candidates[:5]
            ]
        )
    )
    selected = candidates[0][2] if candidates else None
    if selected:
        print(
            f"[最早班调试] 当前选中的最早班: "
            f"{selected.get('flight_no') or selected.get('flight_combo')} "
            f"{selected.get('departure_time') or selected.get('dep_time')}"
        )
    return selected


def _date_minus_one(date_str: str | None) -> str:
    try:
        return (datetime.strptime(str(date_str), "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def _date_plus_one(date_str: str | None) -> str:
    try:
        return (datetime.strptime(str(date_str), "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def _flight_sort_price(flight: dict) -> float:
    price = _to_float((flight or {}).get("price"))
    return price if price is not None else 999999


def _flight_time_range_text(flight: dict) -> str:
    dep = _first_time_text(flight or {}, "departure_time", "dep_time") or "--:--"
    arr = _first_time_text(flight or {}, "arrival_time", "arr_time") or "--:--"
    return f"{dep}->{arr}"


def _same_day_return_sort_key(flight: dict, date_str: str | None):
    dep_dt = _flight_departure_datetime(flight or {}, date_str)
    if dep_dt is None:
        dep_min = _flight_departure_minutes(flight or {})
        dep_dt = _minutes_datetime(date_str, dep_min) if dep_min is not None else datetime.max
    return (dep_dt, _flight_sort_price(flight or {}))


def _same_day_return_price_sort_key(flight: dict, date_str: str | None):
    dep_dt = _flight_departure_datetime(flight or {}, date_str)
    if dep_dt is None:
        dep_min = _flight_departure_minutes(flight or {})
        dep_dt = _minutes_datetime(date_str, dep_min) if dep_min is not None else datetime.max
    return (_flight_sort_price(flight or {}), dep_dt)


def _same_day_window_return_candidates(
    return_flights: list[dict] | None,
    windows: dict | None,
    date_str: str | None,
    constraints: dict | None = None,
) -> list[dict]:
    windows = windows or {}
    candidates = []
    for flight in return_flights or []:
        if _flight_departure_minutes(flight or {}) is None:
            continue
        if _to_float((flight or {}).get("price")) is None:
            continue
        dep_date = _flight_date_text(flight or {}, "departure")
        if date_str and dep_date and dep_date != str(date_str)[:10]:
            continue
        active_windows = _same_day_windows_for_airport(
            windows,
            constraints or {},
            _flight_airport(flight or {}, "departure_airport"),
        )
        depart_after = active_windows.get("return_depart_after_minutes")
        _, return_after_dt = _same_day_window_datetimes(active_windows, date_str)
        dep_dt = _flight_departure_datetime(flight or {}, date_str)
        if return_after_dt is not None and dep_dt is not None:
            if dep_dt < return_after_dt:
                continue
        elif depart_after is not None:
            dep_min = _flight_departure_minutes(flight or {})
            if dep_min is not None and dep_min < depart_after:
                continue
        item = dict(flight)
        item["same_day_windows"] = active_windows
        item["meeting_to_airport_min"] = _same_day_return_transport_minutes(active_windows)
        candidates.append(item)
    return sorted(candidates, key=lambda item: _same_day_return_price_sort_key(item, date_str))


def _same_day_next_return_candidates(
    return_flights: list[dict] | None,
    date_str: str | None,
) -> list[dict]:
    next_date = _date_plus_one(date_str)
    candidates = []
    for flight in return_flights or []:
        if _flight_departure_minutes(flight or {}) is None:
            continue
        if _to_float((flight or {}).get("price")) is None:
            continue
        dep_date = _flight_date_text(flight or {}, "departure")
        if next_date and dep_date and dep_date != next_date:
            continue
        candidates.append(flight)
    return sorted(candidates, key=lambda item: _same_day_return_sort_key(item, next_date or date_str))


def _same_day_flight_identity(flight: dict | None) -> str:
    flight = flight or {}
    return str(
        flight.get("flight_no")
        or flight.get("flight_combo")
        or flight.get("id")
        or f"{flight.get('departure_time')}->{flight.get('arrival_time')}"
    )


def _same_day_roundtrip_alternative(
    category: str,
    title: str,
    outbound: dict,
    return_flight: dict | None,
    tradeoff: str,
    note: str = "",
    date_override: str | None = None,
    passengers: dict | None = None,
    route_type: str | None = None,
    extra: dict | None = None,
    max_budget=None,
    budget_scope_label: str | None = None,
) -> dict:
    outbound_price = _to_float((outbound or {}).get("price")) or 0
    if not return_flight:
        payload = {
            "category": category,
            "title": title,
            "flight": outbound,
            "price": outbound_price,
            "time": _flight_time_range_text(outbound),
            "tradeoff": tradeoff,
            "note": note,
        }
        if date_override:
            payload["date"] = date_override
        if extra:
            payload.update(extra)
        return payload

    return_price = _to_float((return_flight or {}).get("price")) or 0
    adult_roundtrip = outbound_price + return_price
    passenger_pricing = build_passenger_roundtrip_pricing(outbound_price, return_price, passengers, route_type)
    display_total = _to_float((passenger_pricing.get("price_tiers") or {}).get("total_roundtrip_ref"))
    if display_total is None:
        display_total = adult_roundtrip
    price_scope_label = "total_roundtrip" if passenger_pricing.get("applies") else "single_roundtrip"
    print(
        f"[备选价格诊断] 备选去程价={outbound_price:g} 返程价={return_price:g} "
        f"往返总价={adult_roundtrip:g} 全员往返总价={display_total:g}"
    )
    payload = {
        "category": category,
        "title": title,
        "outbound": outbound,
        "return": return_flight,
        "flight": outbound,
        "outbound_price": outbound_price,
        "return_price": return_price,
        "adult_roundtrip_price": adult_roundtrip,
        "single_adult_price": adult_roundtrip,
        "roundtrip_price": display_total,
        "total_price": display_total,
        "price": display_total,
        "price_scope_label": price_scope_label,
        "passenger_pricing": passenger_pricing,
        "price_tiers": passenger_pricing.get("price_tiers") or {},
        "is_roundtrip": True,
        "purchase_type": "两个单程拼接",
        "time": f"{_flight_time_range_text(outbound)} / {_flight_time_range_text(return_flight)}",
        "tradeoff": tradeoff,
        "note": note,
    }
    budget_limit = _to_float(max_budget)
    if budget_limit is not None:
        payload["max_budget"] = budget_limit
        resolved_budget_scope_label = budget_scope_label or f"单人往返 vs 上限{budget_limit:g}"
        assert_price_budget_same_passenger_scope(price_scope_label, resolved_budget_scope_label)
        payload["budget_scope_label"] = resolved_budget_scope_label
        if display_total > budget_limit:
            payload["over_budget"] = True
            payload["budget_overage"] = round(display_total - budget_limit)
        else:
            payload["over_budget"] = False
            payload["budget_overage"] = 0
    if date_override:
        payload["date"] = date_override
    if extra:
        payload.update(extra)
    return payload


def _dedupe_same_day_alternatives(alternatives: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for item in alternatives or []:
        outbound = item.get("outbound") or item.get("flight") or {}
        return_flight = item.get("return") or {}
        key = (
            item.get("category"),
            _same_day_flight_identity(outbound),
            _same_day_flight_identity(return_flight),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def build_same_day_alternatives(
    outbound_flights: list[dict] | None,
    return_flights: list[dict] | None,
    windows: dict | None,
    date_str: str | None,
    previous_day_outbound: list[dict] | None = None,
    next_day_return: list[dict] | None = None,
    limit_per_type: int = 2,
    passengers: dict | None = None,
    route_type: str | None = None,
    max_budget=None,
    budget_scope_label: str | None = None,
    constraints: dict | None = None,
) -> list[dict]:
    """Build transparent fallback choices when meeting-window same-day trips are impossible."""
    alternatives: list[dict] = []
    windows = windows or {}
    previous_date = _date_minus_one(date_str)
    next_date = _date_plus_one(date_str)
    current_return_choices = _same_day_window_return_candidates(return_flights, windows, date_str, constraints=constraints)
    next_return_source = list(next_day_return or [])
    if not next_return_source and next_date:
        next_return_source = [
            flight
            for flight in return_flights or []
            if _flight_date_text(flight or {}, "departure") == next_date
        ]
    next_return_choices = _same_day_next_return_candidates(next_return_source, date_str)
    best_current_return = current_return_choices[0] if current_return_choices else None
    best_next_return = next_return_choices[0] if next_return_choices else None
    best_return = best_current_return or best_next_return
    is_roundtrip_alt = bool(best_return)
    print(f"[当天往返全诊断] 进入备选逻辑时 is_roundtrip 标志={is_roundtrip_alt}")
    print(
        "[当天往返全诊断] 备选生成用的是 "
        + ("往返函数" if is_roundtrip_alt else "单程函数")
    )

    prev_candidates = list(previous_day_outbound or [])
    if not prev_candidates and previous_date:
        prev_candidates = [
            flight
            for flight in outbound_flights or []
            if _flight_date_text(flight or {}, "departure") == previous_date
        ]

    evening = []
    redeye = []
    for flight in prev_candidates:
        dep_min = _flight_departure_minutes(flight or {})
        arr_min = _flight_arrival_minutes(flight or {})
        if dep_min is None or arr_min is None:
            continue
        dep_date = _flight_date_text(flight or {}, "departure")
        arr_date = _flight_date_text(flight or {}, "arrival")
        arrives_next_day = bool(dep_date and arr_date and dep_date != arr_date)
        if dep_min >= 21 * 60 and (arrives_next_day or arr_min < 5 * 60):
            redeye.append(flight)
        elif dep_min >= 17 * 60 and not arrives_next_day:
            evening.append(flight)

    for flight in sorted(evening, key=_flight_sort_price)[:limit_per_type]:
        alternatives.append(
            _same_day_roundtrip_alternative(
                "previous_evening",
                "备选A·前一晚到达",
                flight,
                best_return,
                "多一晚酒店成本，但会议时间最稳",
                "到达后入住酒店，次日从容赴会",
                previous_date,
                passengers,
                route_type,
                max_budget=max_budget,
                budget_scope_label=budget_scope_label,
            )
        )

    for flight in sorted(redeye, key=_flight_sort_price)[:limit_per_type]:
        alternatives.append(
            _same_day_roundtrip_alternative(
                "previous_redeye",
                "备选B·前夜深夜班",
                flight,
                best_return,
                "省酒店时间，但凌晨到达有疲劳风险",
                "这是兜底场景，需自行权衡红眼疲劳风险",
                previous_date,
                passengers,
                route_type,
                max_budget=max_budget,
                budget_scope_label=budget_scope_label,
            )
        )

    flight = pick_earliest_same_day(outbound_flights, date_str, windows, "raw_valid_outbound", constraints=constraints)
    if flight:
        arrival_minutes = _flight_arrival_minutes(flight or {})
        business_start = _parse_time_minutes(windows.get("business_start"))
        meeting_arrival = None
        late_minutes = None
        if arrival_minutes is not None:
            meeting_arrival = arrival_minutes + int(windows.get("transport_min") or 0)
            if business_start is not None:
                late_minutes = max(0, meeting_arrival - business_start)
        if late_minutes and late_minutes >= 60:
            late_text = f"预计到会晚{late_minutes // 60}小时{late_minutes % 60}分钟"
        elif late_minutes:
            late_text = f"预计到会晚{late_minutes}分钟"
        else:
            late_text = "时间仍较紧，需自行确认会议弹性"
        if flight.get("meeting_arrival_note"):
            late_text = flight.get("meeting_arrival_note")
        alternatives.append(
            _same_day_roundtrip_alternative(
                "same_day_earliest",
                "备选C·当天最早班",
                flight,
                best_return,
                "有迟到风险，仅在会议可弹性时考虑",
                late_text,
                date_str,
                passengers,
                route_type,
                {
                    "meeting_arrival_time": _minutes_to_text(meeting_arrival) if meeting_arrival is not None else "",
                    "late_minutes": late_minutes,
                },
                max_budget=max_budget,
                budget_scope_label=budget_scope_label,
            )
        )

    morning_returns = [
        flight
        for flight in next_return_choices
        if (_flight_departure_minutes(flight or {}) is not None and _flight_departure_minutes(flight or {}) <= 10 * 60)
    ]
    if morning_returns and not current_return_choices:
        outbound_for_next_return = pick_earliest_same_day(outbound_flights, date_str, windows, "raw_valid_outbound", constraints=constraints)
        if outbound_for_next_return:
            for return_flight in morning_returns[:1]:
                alternatives.append(
                    _same_day_roundtrip_alternative(
                        "next_morning_return",
                        "备选D·次日早班返程",
                        outbound_for_next_return,
                        return_flight,
                        "需在目的地多留一晚，但返程时间更从容",
                        "返程窗口过紧时的次日返程备选",
                        date_str,
                        passengers,
                        route_type,
                        max_budget=max_budget,
                        budget_scope_label=budget_scope_label,
                    )
                )

    alternatives = _dedupe_same_day_alternatives(alternatives)
    labels = ["备选A", "备选B", "备选C", "备选D", "备选E"]
    for index, item in enumerate(alternatives[:5]):
        title = str(item.get("title") or "").strip()
        suffix = title.split("·", 1)[1] if "·" in title else title or "备选方案"
        item["title"] = f"{labels[index]}·{suffix}"
    return alternatives

def _same_day_candidate_debug_rows(
    outbound_flights: list[dict] | None,
    date_str: str | None,
    limit: int = 8,
) -> list[tuple[str, str, str]]:
    rows = []
    for flight in outbound_flights or []:
        arrival_dt = _flight_arrival_datetime(flight or {}, date_str)
        arrival_minutes = _flight_arrival_minutes(flight or {})
        if arrival_dt is None and arrival_minutes is None:
            continue
        sort_key = arrival_dt
        if sort_key is None:
            sort_key = _minutes_datetime(date_str, arrival_minutes) or datetime(1900, 1, 1) + timedelta(minutes=arrival_minutes)
        rows.append(
            (
                sort_key,
                str(flight.get("flight_no") or flight.get("flight_combo") or ""),
                _first_time_text(flight or {}, "departure_time", "dep_time"),
                _first_time_text(flight or {}, "arrival_time", "arr_time"),
            )
        )
    rows.sort(key=lambda row: row[0])
    return [(flight_no, dep, arr) for _, flight_no, dep, arr in rows[:limit]]


def _same_day_return_window_debug_rows(
    return_flights: list[dict] | None,
    windows: dict | None,
    date_str: str | None,
    constraints: dict | None = None,
) -> list[dict]:
    rows = []
    windows = windows or {}
    window_constraints = _same_day_constraints(windows or {})
    passed_constraints = _same_day_constraints(constraints or {})
    recompute_keys = (
        "same_day_round_trip",
        "business_start",
        "business_end",
        "transport_mode",
        "transport_margin_mode",
        "route_type",
        "meeting_importance",
        "meeting_location",
        "business_location",
        "meeting_area",
        "destination_area",
        "checked_baggage_required",
        "need_baggage",
        "airport_advance_min",
        "departure_airport_process_min",
        "post_meeting_buffer_min",
        "custom_redundancy_min",
    )
    normalized_constraints = {}
    for source in (passed_constraints, window_constraints):
        for key in recompute_keys:
            value = source.get(key)
            if value not in (None, ""):
                normalized_constraints[key] = value

    def _return_windows_for_airport(airport_iata: str | None) -> dict:
        airport = str(airport_iata or "").strip().upper()
        active_windows = dict(_same_day_windows_for_airport(windows, normalized_constraints, airport) or {})
        has_captured_floor = (
            active_windows.get("return_depart_after_minutes") is not None
            or str(active_windows.get("return_depart_after") or "").strip() != ""
        )
        if (
            airport
            and not has_captured_floor
            and normalized_constraints.get("business_start")
            and normalized_constraints.get("business_end")
        ):
            computed = compute_same_day_windows({"constraints": normalized_constraints}, None, airport)
            if computed:
                for key in (
                    "return_depart_after",
                    "return_depart_after_minutes",
                    "reserve_breakdown",
                    "transport_min",
                    "destination_transport_min",
                ):
                    if computed.get(key) is not None:
                        active_windows[key] = computed.get(key)
                active_windows = _merge_same_day_airport_window_maps(active_windows, computed)
        return active_windows

    has_return_airport_map = _has_airport_window_map(
        windows,
        "return_depart_after_by_airport",
        "return_depart_after_minutes_by_airport",
    )
    if (return_flights or []) and has_return_airport_map:
        text_map = windows.get("return_depart_after_by_airport") if isinstance(windows.get("return_depart_after_by_airport"), dict) else {}
        minute_map = windows.get("return_depart_after_minutes_by_airport") if isinstance(windows.get("return_depart_after_minutes_by_airport"), dict) else {}
        if not text_map and not minute_map:
            print("[返程窗口降级] 返程机场下限映射为空,将尝试按每班返程机场就地计算")
    for index, flight in enumerate(return_flights or []):
        flight = flight or {}
        departure_airport = _flight_airport(flight, "departure_airport")
        active_windows = _return_windows_for_airport(departure_airport)
        boundary_date = _flight_date_text(flight, "departure") or date_str
        _, return_after_dt = _same_day_window_datetimes(active_windows, boundary_date)
        departure_dt = _flight_departure_datetime(flight, boundary_date)
        warning = ""
        if return_after_dt is None:
            warning = "返程下限缺失,已跳过当天往返返程窗口匹配"
            print(
                f"[返程窗口降级] 航班={flight.get('flight_no') or flight.get('flight_combo') or ''} "
                f"机场={departure_airport or '未知'} 下限=None,跳过窗口匹配"
            )
            passed = False
        else:
            passed = _same_day_return_passes_window(flight, active_windows, boundary_date)
        rows.append(
            {
                "index": index,
                "flight_no": str(flight.get("flight_no") or flight.get("flight_combo") or ""),
                "departure_airport": departure_airport,
                "raw_departure": _first_time_text(flight, "departure_time", "dep_time"),
                "departure_datetime": str(departure_dt) if departure_dt is not None else None,
                "return_depart_after": active_windows.get("return_depart_after"),
                "return_depart_after_datetime": str(return_after_dt) if return_after_dt is not None else None,
                "passed": bool(passed),
                "warning": warning,
            }
        )
    if (return_flights or []) and rows and all(row.get("return_depart_after_datetime") is None for row in rows):
        print("[返程窗口降级] 所有返程下限均为空,本轮不以返程窗口作为硬阻塞,继续生成备选/无方案理由")
    return rows

def _infer_travelers_from_passengers(passengers: dict, fallback: str = "solo") -> str:
    if not passengers:
        return fallback or "solo"
    has_child = passengers.get("child", 0) > 0 or passengers.get("infant", 0) > 0
    has_elderly = passengers.get("elderly", 0) > 0
    total = sum(passengers.values())
    if has_child and has_elderly:
        return "with_elderly_child"
    if has_child:
        return "with_child"
    if has_elderly:
        return "with_elderly"
    if total > 1:
        return "multiple"
    return fallback or "solo"


def build_travel_profile(soft_prefs: dict | None) -> dict:
    """Convert scenario and companions into scoring weights."""
    soft_prefs = soft_prefs or {}
    scenarios = _normalize_travel_scenarios(
        soft_prefs.get("travel_purposes")
        or soft_prefs.get("travel_scenarios")
        or soft_prefs.get("travel_scenario")
        or soft_prefs.get("scenario")
    )
    scenario = scenarios[0]
    fallback_travelers = soft_prefs.get("travelers") or soft_prefs.get("companions") or "solo"
    passengers = _normalize_passengers(soft_prefs.get("passengers"))
    if not passengers and _to_non_negative_int(soft_prefs.get("passenger_count")) > 0:
        passengers = {
            "adult": _to_non_negative_int(soft_prefs.get("passenger_count")),
            "child": 0,
            "elderly": 0,
            "infant": 0,
        }
    if not passengers:
        passengers = _passengers_from_legacy_companions(fallback_travelers)
    travelers = _infer_travelers_from_passengers(passengers, fallback_travelers)
    raw_trip_natures = soft_prefs.get("trip_natures") or []
    if isinstance(raw_trip_natures, str):
        raw_trip_natures = [raw_trip_natures]
    if not raw_trip_natures and soft_prefs.get("trip_nature"):
        raw_trip_natures = [soft_prefs.get("trip_nature")]
    nature_map = {
        "business_meeting": "meeting",
        "business_trip": "business",
        "business": "business",
        "meeting": "meeting",
        "team_building": "team_building",
    }
    trip_natures = []
    for item in raw_trip_natures:
        value = nature_map.get(str(item or "").strip(), str(item or "").strip())
        if value and value not in trip_natures:
            trip_natures.append(value)
    profiles = {
        "personal": {
            "price": "high",
            "time": "medium",
            "comfort": "medium",
            "risk_averse": "medium",
            "baggage": "medium",
        },
        "business": {
            "price": "low",
            "time": "high",
            "comfort": "high",
            "risk_averse": "high",
            "baggage": "medium",
        },
        "tourism": {
            "price": "high",
            "time": "medium",
            "comfort": "medium",
            "risk_averse": "medium",
            "baggage": "medium",
        },
        "visit_family": {
            "price": "medium",
            "time": "medium",
            "comfort": "medium",
            "risk_averse": "medium",
            "baggage": "high",
        },
        "family_visit": {
            "price": "medium",
            "time": "medium",
            "comfort": "medium",
            "risk_averse": "medium",
            "baggage": "high",
        },
        "family": {
            "price": "medium",
            "time": "medium",
            "comfort": "high",
            "risk_averse": "high",
            "baggage": "high",
        },
        "with_elderly": {
            "price": "medium",
            "time": "high",
            "comfort": "high",
            "risk_averse": "high",
            "baggage": "high",
        },
        "elderly": {
            "price": "medium",
            "time": "high",
            "comfort": "high",
            "risk_averse": "high",
            "baggage": "high",
        },
        "important": {
            "price": "low",
            "time": "high",
            "comfort": "high",
            "risk_averse": "high",
            "baggage": "medium",
        },
        "price_first": {
            "price": "high",
            "time": "low",
            "comfort": "low",
            "risk_averse": "low",
            "baggage": "low",
        },
    }
    level = {"low": 1, "medium": 2, "high": 3}
    level_rev = {1: "low", 2: "medium", 3: "high"}
    merged = {"price": 1, "time": 1, "comfort": 1, "risk_averse": 1, "baggage": 1}
    for item in scenarios:
        scenario_profile = profiles.get(item, profiles["personal"])
        for dim in merged:
            merged[dim] = max(merged[dim], level.get(scenario_profile.get(dim), 2))
    profile_by_nature = {
        "business": {"time": "high", "risk_averse": "high", "comfort": "high"},
        "meeting": {"time": "high", "risk_averse": "high", "comfort": "medium"},
        "team_building": {"comfort": "medium"},
    }
    for nature in trip_natures:
        nature_profile = profile_by_nature.get(nature, {})
        for dim, value in nature_profile.items():
            if dim in merged:
                merged[dim] = max(merged[dim], level.get(value, 2))
    profile = {dim: level_rev[value] for dim, value in merged.items()}
    if "meeting" in trip_natures:
        profile["punctuality"] = "critical"
    if "team_building" in trip_natures:
        profile["stock_check"] = "high"
        profile["date_flex"] = "maybe"
    if travelers in ("with_child", "with_elderly_child"):
        profile["comfort"] = "high"
        profile["risk_averse"] = "high"
        profile["baggage"] = "high"
    if travelers == "with_elderly":
        profile["comfort"] = "high"
        profile["risk_averse"] = "high"
    if travelers in ("multiple", "group"):
        profile["baggage"] = "high"
        profile["stock_check"] = "high"
    passenger_count = sum(passengers.values()) if passengers else _to_non_negative_int(soft_prefs.get("passenger_count"), 1)
    if passenger_count > 1:
        profile["stock_check"] = "high"
    if passengers.get("infant", 0) > 0:
        profile["infant"] = True
        profile["infant_note"] = "婴儿票、摇篮和行李额需在支付页单独确认"
    companion_constraints = soft_prefs.get("companion_constraints") or []
    if isinstance(companion_constraints, str):
        companion_constraints = [item.strip() for item in companion_constraints.split(",") if item.strip()]
    scenario_has_child = any(item in scenarios for item in ("family", "with_child", "with_elderly_child"))
    scenario_has_elderly = any(item in scenarios for item in ("elderly", "with_elderly", "with_elderly_child"))
    passenger_extra = {
        "scenario_has_child": scenario_has_child,
        "scenario_has_elderly": scenario_has_elderly,
        "companion_constraints": companion_constraints,
        "mobility_limited": bool(soft_prefs.get("mobility_limited")) or "limited_mobility" in companion_constraints,
        "no_redeye_strict": bool(soft_prefs.get("no_redeye_strict")) or "no_redeye" in companion_constraints,
        "transfer_sensitive": "avoid_long_layover" in companion_constraints,
        "needs_baggage_clarity": "need_baggage" in companion_constraints,
        "needs_refund_flexibility": "need_refund_change" in companion_constraints,
        "elderly_condition": soft_prefs.get("elderly_condition") or "",
        "child_type": soft_prefs.get("child_type") or "",
    }
    passenger_profile = build_passenger_profile(
        passengers or {"adult": passenger_count, "child": 0, "elderly": 0, "infant": 0},
        passenger_extra,
    )
    passenger_rules = build_passenger_friendly_rules(
        passenger_profile,
        route_type=soft_prefs.get("route_type") or soft_prefs.get("basic_route_type"),
    )

    profile["passenger_profile"] = passenger_profile
    profile["passenger_rules"] = passenger_rules
    profile["score_weights"] = dict(passenger_rules.get("weights") or {})
    profile["scenario"] = scenario
    profile["scenarios"] = scenarios
    profile["scenario_combo"] = "+".join(scenarios)
    profile["travelers"] = travelers
    profile["trip_natures"] = trip_natures
    profile["passengers"] = passengers or {"adult": passenger_count, "child": 0, "elderly": 0, "infant": 0}
    profile["passenger_count"] = passenger_count
    return profile


def build_alert_policy(profile: dict | None) -> dict:
    """Describe how the travel profile changes notification sensitivity."""
    profile = profile or build_travel_profile({})
    price_level = profile.get("price", "medium")
    risk_level = profile.get("risk_averse", "medium")
    if price_level == "high":
        price_drop_threshold = 100
        trigger_focus = "价格小幅下降也提醒"
    elif price_level == "low":
        price_drop_threshold = 300
        trigger_focus = "优先提醒稳定可执行方案"
    else:
        price_drop_threshold = 200
        trigger_focus = "价格和方案质量均衡提醒"
    return {
        "price_drop_threshold": price_drop_threshold,
        "risk_quality_gate": "high_confidence_only" if risk_level == "high" else "normal",
        "trigger_focus": trigger_focus,
    }


def travel_profile_explanation(profile: dict | None) -> dict:
    """User-facing explanation for why a scenario combo changes recommendation order."""
    profile = profile or build_travel_profile({})
    scenarios = _normalize_travel_scenarios(profile.get("scenarios") or profile.get("scenario"))
    scenario = scenarios[0]
    basis = {
        "business": "商务/会议提高到达时间稳定、直飞/低风险和可改签权重。",
        "family": "家庭/亲子提高白天直飞、行李明确和低中转风险权重。",
        "elderly": "老人同行提高白天到达、全服务航司和低转机风险权重。",
        "with_elderly": "老人同行提高白天到达、全服务航司和低转机风险权重。",
        "important": "重要事项提高稳定到达、可退改和低执行风险权重。",
        "price_first": "价格优先保留低价敏感度，但仍会提示执行风险。",
        "tourism": "旅游保留价格敏感和日期弹性。",
        "family_visit": "探亲/回家提高行李明确和合理价格权重。",
        "visit_family": "探亲/回家提高行李明确和合理价格权重。",
        "personal": "个人出行按价格和便利性均衡处理。",
    }
    basis_items = [basis.get(item) for item in scenarios if basis.get(item)]
    if len(scenarios) > 1:
        basis_text = "系统合并了多个场景的需求：" + "；".join(basis_items)
    else:
        basis_text = basis_items[0] if basis_items else "按价格、时间、舒适度和执行风险做均衡排序。"
    scenario_set = set(scenarios)
    tradeoff = ""
    if "price_first" in scenario_set and "important" in scenario_set:
        tradeoff = "你同时选择了价格优先和重要事项，系统会先保证可靠性，再在可靠方案中选择价格更低的。"
    elif "price_first" in scenario_set and ("elderly" in scenario_set or "with_elderly" in scenario_set):
        tradeoff = "老人出行的直飞、白天到达和稳定性会适当优先于极致低价。"
    elif "business" in scenario_set and "price_first" in scenario_set:
        tradeoff = "商务场景会先保证准点和低风险，再在同类稳妥方案中选择更低价格。"
    elif "tourism" in scenario_set and "family" in scenario_set:
        tradeoff = "旅游保留价格敏感，但家庭/亲子的安全舒适要求会优先于纯低价。"
    elif ("elderly" in scenario_set or "with_elderly" in scenario_set) and (
        "family_visit" in scenario_set or "visit_family" in scenario_set
    ):
        tradeoff = "探亲/回家提高行李权重，老人同行进一步提高直飞、白天到达和低风险权重。"
    dimensions = {
        TRAVEL_PROFILE_LABELS.get(key, key): TRAVEL_PROFILE_LEVEL_LABELS.get(value, value)
        for key, value in profile.items()
        if key in TRAVEL_PROFILE_LABELS
    }
    return {
        "scenario": scenario,
        "scenarios": scenarios,
        "scenario_label": " + ".join(_travel_scenario_labels(scenarios)),
        "basis": basis_text,
        "tradeoff": tradeoff,
        "dimensions": dimensions,
        "stock_check": profile.get("stock_check"),
    }


def build_recommendation_basis(profile: dict | None, defaults_applied: list[str] | None = None) -> dict:
    """Build one user-facing source of truth for scenario-based ranking."""
    profile = profile or build_travel_profile({})
    scenarios = _normalize_travel_scenarios(profile.get("scenarios") or profile.get("scenario"))
    scenario_set = set(scenarios)
    scenario_labels = _travel_scenario_labels(scenarios)
    trip_natures = profile.get("trip_natures") or []
    if isinstance(trip_natures, str):
        trip_natures = [trip_natures]
    normalized_trip_natures = []
    for item in trip_natures:
        value = str(item or "").strip()
        if value and value not in normalized_trip_natures:
            normalized_trip_natures.append(value)
    trip_nature_set = set(normalized_trip_natures)
    applied_rules: list[str] = []

    if "tourism" in scenario_set:
        applied_rules.append("价格敏感，关注低价和日期弹性（旅游）")
    if "family" in scenario_set:
        applied_rules.extend(
            [
                "优先白天直飞（家庭/亲子）",
                "行李明确优先（家庭/亲子）",
                "降低红眼和长中转风险（家庭/亲子）",
            ]
        )
    if "elderly" in scenario_set or "with_elderly" in scenario_set:
        applied_rules.extend(
            [
                "优先直飞、白天到达和低转机风险（老人同行）",
                "全服务航司和可退改更优先（老人同行）",
            ]
        )
    if "family_visit" in scenario_set or "visit_family" in scenario_set:
        applied_rules.append("行李权重高，避免极端折腾（探亲/回家）")
    if "business" in scenario_set:
        applied_rules.append("准点、直飞和可改签优先（商务/会议）")
    if "important" in scenario_set:
        applied_rules.append("稳定到达和低执行风险优先（重要事项）")
    if "price_first" in scenario_set:
        applied_rules.append("保留低价敏感度，但高风险方案会被提示或降权（价格优先）")
    if "business" in trip_nature_set:
        applied_rules.append("商务出差提高准点、可改签和低执行风险权重")
    if "meeting" in trip_nature_set:
        applied_rules.append("商务会议按会议时间窗口筛选，准点和缓冲优先")
    if "team_building" in trip_nature_set:
        applied_rules.append("公司团建提高多人库存、同行程和日期弹性权重")

    if not applied_rules:
        applied_rules.append("按价格、时间、舒适度和执行风险均衡排序")

    conflict_note = ""
    if "tourism" in scenario_set and "family" in scenario_set:
        conflict_note = "孩子安全舒适优先于纯低价"
    elif ("elderly" in scenario_set or "with_elderly" in scenario_set) and (
        "family_visit" in scenario_set or "visit_family" in scenario_set
    ):
        conflict_note = "老人同行的直飞、白天到达和稳定性优先于极致低价"
    elif "price_first" in scenario_set and "important" in scenario_set:
        conflict_note = "重要事项先保证可靠性，再比较价格"
    elif "business" in scenario_set and "price_first" in scenario_set:
        conflict_note = "先保证准点和低风险，再在稳妥方案中比较价格"
    elif "meeting" in trip_nature_set and "team_building" in trip_nature_set:
        conflict_note = "会议时间刚性优先，团建日期弹性只在不影响会议窗口时参与比较"

    sort_factors = [
        ("价格", TRAVEL_PROFILE_LEVEL_LABELS.get(profile.get("price", "medium"), "中")),
        ("时间", TRAVEL_PROFILE_LEVEL_LABELS.get(profile.get("time", "medium"), "中")),
        ("舒适度", TRAVEL_PROFILE_LEVEL_LABELS.get(profile.get("comfort", "medium"), "中")),
        ("执行风险厌恶", TRAVEL_PROFILE_LEVEL_LABELS.get(profile.get("risk_averse", "medium"), "中")),
        ("行李重要性", TRAVEL_PROFILE_LEVEL_LABELS.get(profile.get("baggage", "medium"), "中")),
    ]

    if "meeting" in trip_nature_set and "team_building" in trip_nature_set:
        plain_language = "先保证会议时间窗口和准点缓冲，再检查团队是否能同航班/分舱位成行。"
        recommendation_text = "该方案按会议时间优先筛选，同时兼顾团队多人库存和分舱位安排，适合商务会议叠加公司团建。"
    elif "meeting" in trip_nature_set:
        plain_language = "先保证会议时间窗口、准点率和低执行风险，再比较价格。"
        recommendation_text = "该方案更重视准点、缓冲和可执行性，适合商务会议行程。"
    elif "team_building" in trip_nature_set:
        plain_language = "先确认团队人数库存和同行程可行，再比较价格和日期弹性。"
        recommendation_text = "该方案会重点检查多人库存、同行程和团队总成本，适合公司团建。"
    elif "business" in trip_nature_set:
        plain_language = "先保证商务出差的准点、退改和低风险，再比较同类方案价格。"
        recommendation_text = "该方案优先考虑准点、可改签和低执行风险，适合商务出差。"
    elif "tourism" in scenario_set and "family" in scenario_set:
        plain_language = "先保证适合带孩子（白天/直飞/行李），再在其中挑便宜的。"
        recommendation_text = "该方案白天直飞、行李明确，价格也在合理区间，适合带孩子的旅行，兼顾省心和性价比。"
    elif ("elderly" in scenario_set or "with_elderly" in scenario_set) and (
        "family_visit" in scenario_set or "visit_family" in scenario_set
    ):
        plain_language = "先保证适合老人探亲（直飞/白天到达/行李），再比较价格。"
        recommendation_text = "该方案直飞、白天到达、行李充足，转机风险低，适合带老人回家探亲。"
    elif "business" in scenario_set and "price_first" in scenario_set:
        plain_language = "先保证商务出行的准点和低风险，再在同类稳妥方案中选价格更低的。"
        recommendation_text = "该方案优先保证准点、直飞和低风险，并在同类稳妥方案里兼顾较低价格。"
    elif "price_first" in scenario_set and "important" in scenario_set:
        plain_language = "先保证重要行程可靠，不把纯低价但容易出错的方案排在前面。"
        recommendation_text = "该方案先按重要事项保证可靠性，再在可执行方案中兼顾低价。"
    elif "family" in scenario_set:
        plain_language = "优先白天直飞、行李明确和低中转风险，价格作为第二层比较。"
        recommendation_text = "该方案优先考虑白天直飞、行李明确和低中转风险，适合带孩子出行，减少折腾。"
    elif "tourism" in scenario_set:
        plain_language = "优先价格和日期弹性，同时保留基本的执行风险提醒。"
        recommendation_text = "该方案兼顾低价日期和合理中转，适合旅游行程继续比较。"
    else:
        plain_language = "按价格、时间、舒适度和执行风险综合排序。"
        recommendation_text = "本次按价格、时间、舒适度、执行风险和行李票规综合排序。"

    return {
        "scenarios": scenarios,
        "trip_natures": normalized_trip_natures,
        "scenario_labels": scenario_labels,
        "applied_rules": applied_rules,
        "sort_factors": sort_factors,
        "conflict_note": conflict_note,
        "plain_language": plain_language,
        "recommendation_text": recommendation_text,
        "defaults_applied": defaults_applied or [],
    }


def _set_default_if_missing(target: dict, key: str, value) -> bool:
    if value in (None, ""):
        return False
    if key in {"companions", "travelers"} and target.get(key) == "solo" and value != "solo":
        target[key] = value
        return True
    if key not in target or target.get(key) in (None, "", "unknown", "any", "unlimited"):
        target[key] = value
        return True
    return False


def _apply_preference_default(
    soft: dict, hard: dict, key: str, value, *, override: bool = False
) -> None:
    if key in {
        "transfer_policy",
        "baggage",
        "baggage_default",
        "max_extra_duration_hours",
        "arrival_time_policy",
        "accept_self_transfer",
        "accept_overnight_transfer",
    }:
        if override:
            hard[key] = value
        else:
            _set_default_if_missing(hard, key, value)
    else:
        if override:
            soft[key] = value
        else:
            _set_default_if_missing(soft, key, value)
        if key == "time_preference_mode":
            if override:
                soft["time_preference"] = value
                hard["time_preference_mode"] = value
                hard["time_preference"] = value
            else:
                _set_default_if_missing(soft, "time_preference", value)
                _set_default_if_missing(hard, "time_preference_mode", value)
                _set_default_if_missing(hard, "time_preference", value)


def _apply_rule_defaults(
    soft: dict, hard: dict, rules: dict, defaults_applied: list[str], *, override: bool = False
) -> None:
    for key, value in (rules.get("defaults") or {}).items():
        _apply_preference_default(soft, hard, key, value, override=override)
    defaults_applied.extend(rules.get("notes") or [])


def _apply_companion_constraints(
    soft: dict, hard: dict, constraints: list[str], defaults_applied: list[str]
) -> None:
    constraint_set = set(constraints or [])
    if "direct_preferred" in constraint_set:
        soft["direct_preferred"] = True
        defaults_applied.append("同行约束：需要尽量直飞")
    if "no_redeye" in constraint_set:
        _apply_preference_default(soft, hard, "time_preference_mode", "no_redeye", override=True)
        defaults_applied.append("同行约束：不接受红眼/凌晨到达")
    if "avoid_long_layover" in constraint_set:
        _apply_preference_default(soft, hard, "max_extra_duration_hours", 3, override=False)
        soft["avoid_long_layover"] = True
        defaults_applied.append("同行约束：不适合长时间中转")
    if "need_baggage" in constraint_set:
        hard["baggage"] = "required"
        defaults_applied.append("同行约束：需要托运行李")
    if "need_refund_change" in constraint_set:
        soft["refund_flexibility"] = "required"
        hard["refund_flexibility"] = "required"
        defaults_applied.append("同行约束：需要可退改")
    if "daytime_arrival" in constraint_set:
        hard["arrival_time_policy"] = "daytime_only"
        soft["prefer_daytime_arrival"] = True
        defaults_applied.append("同行约束：希望白天到达")
    if "limited_mobility" in constraint_set:
        soft["direct_preferred"] = True
        soft["allow_self_transfer"] = False
        hard["accept_self_transfer"] = False
        _apply_preference_default(soft, hard, "max_extra_duration_hours", 3, override=False)
        defaults_applied.append("同行约束：行动不便，降低步行/换乘风险")


def migrate_old_subscription(subscription: dict) -> dict:
    """Normalize legacy flat subscriptions into the four-section V3 shape."""
    sub = dict(subscription or {})
    hard = dict(sub.get("hard_constraints") or {})
    soft = dict(sub.get("soft_preferences") or {})
    goals = dict(sub.get("notification_goals") or {})
    basic = dict(sub.get("basic") or {})
    constraints = dict(sub.get("constraints") or {})
    preferences = dict(sub.get("preferences") or {})
    advanced = dict(sub.get("advanced_rules") or {})

    if not basic:
        basic = {
            "origin": sub.get("origin"),
            "origin_airports": sub.get("origin_airports") or [],
            "origin_airports_active": sub.get("origin_airports_active")
            or sub.get("origin_airports")
            or [],
            "destination": sub.get("destination"),
            "dest_airports": sub.get("destination_airports") or sub.get("dest_airports") or [],
            "destination_airports": sub.get("destination_airports") or [],
            "destination_airports_active": sub.get("destination_airports_active")
            or sub.get("destination_airports")
            or [],
            "trip_type": "round_trip" if sub.get("round_trip") else "one_way",
            "departure_date": sub.get("depart_date") or sub.get("departure_date"),
            "return_date": sub.get("return_date"),
        }

    if not constraints:
        budget_strategy = (
            hard.get("budget_strategy")
            or sub.get("budget_strategy")
            or ("explicit" if hard.get("max_budget") or sub.get("max_budget") else "auto_judge")
        )
        constraints = {
            "budget_strategy": budget_strategy,
            "max_price": hard.get("max_budget") or sub.get("max_budget") or sub.get("budget"),
            "ideal_price": hard.get("target_price")
            or soft.get("target_price")
            or sub.get("target_price"),
            "date_flexibility_days": sub.get("date_flexibility", 0),
            "transfer_policy": hard.get("transfer_policy")
            or sub.get("transfer_policy")
            or sub.get("direct_only")
            or "reasonable",
            "checked_baggage_required": (
                hard.get("baggage") == "required"
                or sub.get("need_baggage") == "required"
            ),
        }

    if not preferences:
        travel_scenarios = _normalize_travel_scenarios(
            soft.get("travel_scenarios")
            or soft.get("travel_scenario")
            or sub.get("travel_scenarios")
            or sub.get("travel_scenario")
        )
        preferences = {
            "travelers": soft.get("companions") or sub.get("companions", "solo"),
            "travel_scenario": travel_scenarios[0],
            "travel_scenarios": travel_scenarios,
            "companion_constraints": soft.get("companion_constraints")
            or sub.get("companion_constraints")
            or [],
            "solo_travel": bool(soft.get("solo_travel") or sub.get("solo_travel")),
            "no_late_arrival": bool(
                soft.get("no_late_arrival") or sub.get("no_late_arrival")
            ),
            "prefer_daytime_arrival": bool(
                soft.get("prefer_daytime_arrival")
                or sub.get("prefer_daytime_arrival")
            ),
            "time_pref": soft.get("time_preference_mode")
            or soft.get("time_preference")
            or hard.get("time_preference_mode")
            or hard.get("time_preference")
            or "unlimited",
            "refund_policy": soft.get("refund_flexibility")
            or hard.get("refund_flexibility")
            or sub.get("refund_flexibility", "preferred"),
            "price_sensitivity": soft.get("price_sensitivity")
            or sub.get("price_sensitivity", "low"),
            "travel_type": soft.get("trip_type") or sub.get("trip_type", "tourism"),
        }

    passengers = (
        _normalize_passengers(preferences.get("passengers"))
        or _normalize_passengers(soft.get("passengers"))
        or _normalize_passengers(sub.get("passengers"))
    )
    passenger_count = _to_non_negative_int(
        basic.get("passenger_count")
        or preferences.get("passenger_count")
        or soft.get("passenger_count")
        or sub.get("passenger_count"),
        0,
    )
    if passengers:
        passenger_count = sum(passengers.values())
        preferences["passengers"] = passengers
    if passenger_count > 0:
        basic["passenger_count"] = passenger_count
        preferences["passenger_count"] = passenger_count

    if not advanced:
        advanced = {
            "time_windows": {
                "departure": soft.get("departure_time_windows") or [],
                "arrival": soft.get("arrival_time_windows") or [],
                "outbound_departure": soft.get("outbound_departure_time_windows") or [],
                "outbound_arrival": soft.get("outbound_arrival_time_windows") or [],
                "return_departure": soft.get("return_departure_time_windows") or [],
                "return_arrival": soft.get("return_arrival_time_windows") or [],
                "hourly": {},
            },
            "transfer": {
                "max_total_duration": hard.get("max_total_duration_hours"),
                "max_extra_duration_hours": hard.get("max_extra_duration_hours"),
                "overnight_transfer": hard.get("accept_overnight_transfer"),
                "self_transfer": hard.get("accept_self_transfer"),
            },
            "airlines": {
                "preference": soft.get("airline_policy")
                or hard.get("airline_policy")
                or sub.get("airline_policy", "any"),
                "blocked": soft.get("exclude_airlines")
                or hard.get("exclude_airlines")
                or sub.get("exclude_airlines")
                or [],
                "lcc_policy": resolve_lcc_policy(sub, "any"),
            },
            "alerts": {
                "frequency": goals.get("frequency") or sub.get("notification_frequency"),
                "types": goals.get("secondary") or sub.get("secondary_goals") or [],
                "price_change_threshold": goals.get("price_change_threshold"),
                "digest_time": goals.get("digest_time"),
            },
        }

    sub["basic"] = basic
    sub["constraints"] = constraints
    sub["preferences"] = preferences
    sub["advanced_rules"] = advanced

    _set_if_missing(sub, "origin", basic.get("origin"))
    _set_if_missing(sub, "origin_airports", basic.get("origin_airports"))
    _set_if_missing(sub, "origin_airports_active", basic.get("origin_airports_active"))
    _set_if_missing(sub, "destination", basic.get("destination"))
    _set_if_missing(sub, "destination_airports", basic.get("destination_airports") or basic.get("dest_airports"))
    _set_if_missing(sub, "destination_airports_active", basic.get("destination_airports_active"))
    _set_if_missing(sub, "depart_date", basic.get("departure_date"))
    _set_if_missing(sub, "return_date", basic.get("return_date"))
    if "round_trip" not in sub:
        sub["round_trip"] = basic.get("trip_type") == "round_trip"

    _set_if_missing(hard, "budget_strategy", constraints.get("budget_strategy"))
    _set_if_missing(hard, "max_budget", constraints.get("max_price"))
    _set_if_missing(hard, "target_price", constraints.get("ideal_price"))
    _set_if_missing(hard, "transfer_policy", constraints.get("transfer_policy"))
    _set_if_missing(hard, "baggage", "required" if constraints.get("checked_baggage_required") else "unknown")
    _set_if_missing(hard, "date_flexibility", constraints.get("date_flexibility_days"))
    _set_if_missing(hard, "same_day_round_trip", constraints.get("same_day_round_trip"))
    _set_if_missing(hard, "business_start", constraints.get("business_start"))
    _set_if_missing(hard, "business_end", constraints.get("business_end"))
    _set_if_missing(hard, "buffer_hours", constraints.get("buffer_hours"))
    _set_if_missing(hard, "transport_mode", constraints.get("transport_mode"))
    _set_if_missing(hard, "user_transport_min", constraints.get("user_transport_min"))
    _set_if_missing(hard, "transport_margin_mode", constraints.get("transport_margin_mode"))
    _set_if_missing(hard, "redundancy_min", constraints.get("redundancy_min"))
    _set_if_missing(hard, "trip_nature", constraints.get("trip_nature"))
    _set_if_missing(hard, "trip_natures", constraints.get("trip_natures"))
    _set_if_missing(hard, "cabin_policy", constraints.get("cabin_policy"))
    _set_if_missing(hard, "user_level", constraints.get("user_level"))
    _set_if_missing(hard, "business_seats", constraints.get("business_seats"))
    _set_if_missing(hard, "economy_seats", constraints.get("economy_seats"))
    _set_if_missing(hard, "reimburse_per_person", constraints.get("reimburse_per_person"))

    transfer_rules = advanced.get("transfer") or {}
    _set_if_missing(hard, "max_total_duration_hours", transfer_rules.get("max_total_duration"))
    _set_if_missing(hard, "max_extra_duration_hours", transfer_rules.get("max_extra_duration_hours"))
    _set_if_missing(hard, "accept_overnight_transfer", transfer_rules.get("overnight_transfer"))
    _set_if_missing(hard, "accept_self_transfer", transfer_rules.get("self_transfer"))

    _set_if_missing(soft, "companions", preferences.get("travelers"))
    _set_if_missing(soft, "travelers", preferences.get("travelers"))
    _set_if_missing(soft, "passengers", preferences.get("passengers"))
    _set_if_missing(soft, "passenger_count", basic.get("passenger_count"))
    _set_if_missing(soft, "travel_purposes", preferences.get("travel_purposes"))
    _set_if_missing(soft, "travel_scenario", preferences.get("travel_scenario"))
    _set_if_missing(soft, "travel_scenarios", preferences.get("travel_scenarios"))
    _set_if_missing(soft, "companion_constraints", preferences.get("companion_constraints"))
    _set_if_missing(soft, "solo_travel", preferences.get("solo_travel"))
    _set_if_missing(soft, "no_late_arrival", preferences.get("no_late_arrival"))
    _set_if_missing(soft, "prefer_daytime_arrival", preferences.get("prefer_daytime_arrival"))
    _set_if_missing(soft, "time_preference", preferences.get("time_pref"))
    _set_if_missing(soft, "time_preference_mode", preferences.get("time_pref"))
    _set_if_missing(soft, "refund_flexibility", preferences.get("refund_policy"))
    _set_if_missing(soft, "price_sensitivity", preferences.get("price_sensitivity"))
    _set_if_missing(soft, "trip_type", preferences.get("travel_type"))

    time_windows = advanced.get("time_windows") or {}
    _set_if_missing(soft, "departure_time_windows", time_windows.get("departure"))
    _set_if_missing(soft, "arrival_time_windows", time_windows.get("arrival"))
    _set_if_missing(soft, "outbound_departure_time_windows", time_windows.get("outbound_departure"))
    _set_if_missing(soft, "outbound_arrival_time_windows", time_windows.get("outbound_arrival"))
    _set_if_missing(soft, "return_departure_time_windows", time_windows.get("return_departure"))
    _set_if_missing(soft, "return_arrival_time_windows", time_windows.get("return_arrival"))

    airline_rules = dict(advanced.get("airlines") or {})
    legacy_airline_policy = (
        soft.get("airline_policy")
        or hard.get("airline_policy")
        or airline_rules.get("preference")
        or sub.get("airline_policy")
        or "any"
    )
    canonical_airline_policy, canonical_lcc_policy, legacy_lcc_migrated = (
        canonicalize_airline_lcc_policy(
            legacy_airline_policy,
            resolve_lcc_policy(sub, "any"),
        )
    )
    if legacy_lcc_migrated:
        soft["airline_policy"] = canonical_airline_policy
        if hard.get("airline_policy") == "no_lcc":
            hard["airline_policy"] = canonical_airline_policy
        if sub.get("airline_policy") == "no_lcc":
            sub["airline_policy"] = canonical_airline_policy
        airline_rules["preference"] = canonical_airline_policy
        airline_rules["lcc_policy"] = canonical_lcc_policy
        advanced["airlines"] = airline_rules
        hard["lcc_policy"] = canonical_lcc_policy
        constraints["lcc_policy"] = canonical_lcc_policy
        safe_log(
            "[口径迁移] airline_policy=no_lcc已归一为"
            f"airline_policy={canonical_airline_policy}, lcc_policy={canonical_lcc_policy}"
        )
    _set_if_missing(soft, "airline_policy", airline_rules.get("preference"))
    _set_if_missing(soft, "exclude_airlines", airline_rules.get("blocked"))
    alert_rules = advanced.get("alerts") or {}
    _set_if_missing(goals, "frequency", alert_rules.get("frequency"))
    _set_if_missing(goals, "secondary", alert_rules.get("types"))
    _set_if_missing(goals, "price_change_threshold", alert_rules.get("price_change_threshold"))
    _set_if_missing(goals, "digest_time", alert_rules.get("digest_time"))

    sub["hard_constraints"] = hard
    sub["soft_preferences"] = soft
    sub["notification_goals"] = goals
    sub["lcc_policy"] = canonical_lcc_policy
    return sub


def apply_default_rules(subscription: dict) -> dict:
    """Apply safe defaults so quick-mode users still get advanced protection."""
    subscription = migrate_old_subscription(subscription or {})
    soft = dict(subscription.get("soft_preferences") or {})
    goals = dict(subscription.get("notification_goals") or {})
    hard = dict(subscription.get("hard_constraints") or {})
    monitor_mode = subscription.get("monitor_mode", "quick")
    quick_mode = monitor_mode != "precise"
    defaults_applied = []

    travel_scenarios = _normalize_travel_scenarios(
        soft.get("travel_purposes") or soft.get("travel_scenarios") or soft.get("travel_scenario")
    )
    same_day_round_trip = bool(
        hard.get("same_day_round_trip")
        or (subscription.get("constraints") or {}).get("same_day_round_trip")
        or subscription.get("same_day_round_trip")
    )
    if same_day_round_trip:
        hard["same_day_round_trip"] = True
        subscription["round_trip"] = True
        basic = dict(subscription.get("basic") or {})
        basic["trip_type"] = "round_trip"
        if basic.get("departure_date") and not basic.get("return_date"):
            basic["return_date"] = basic.get("departure_date")
            subscription["return_date"] = basic.get("departure_date")
        subscription["basic"] = basic
        if "business" not in travel_scenarios:
            travel_scenarios.append("business")
        defaults_applied.append("当天往返商务模式：优先早去晚回、直飞和低执行风险方案")
    raw_trip_natures = hard.get("trip_natures") or []
    if isinstance(raw_trip_natures, str):
        raw_trip_natures = [raw_trip_natures]
    if not raw_trip_natures and hard.get("trip_nature"):
        raw_trip_natures = [hard.get("trip_nature")]
    trip_nature_map = {
        "business_meeting": "meeting",
        "business_trip": "business",
        "business": "business",
        "meeting": "meeting",
        "team_building": "team_building",
    }
    trip_natures = []
    for item in raw_trip_natures:
        value = trip_nature_map.get(str(item or "").strip(), str(item or "").strip())
        if value and value not in trip_natures:
            trip_natures.append(value)
    if ("business" in trip_natures or "meeting" in trip_natures) and "business" not in travel_scenarios:
        travel_scenarios.append("business")
    if "meeting" in trip_natures:
        defaults_applied.append("商务会议：按会议时间窗口提高准点、缓冲和低执行风险权重")
    if "team_building" in trip_natures:
        defaults_applied.append("公司团建：提高多人库存、同行程和日期弹性权重")
    if trip_natures:
        soft["trip_natures"] = trip_natures
    soft["travel_scenarios"] = travel_scenarios
    if soft.get("travel_purposes"):
        soft["travel_purposes"] = travel_scenarios
    soft["travel_scenario"] = travel_scenarios[0]
    scenario_rules = []
    for travel_scenario in travel_scenarios:
        scenario = SCENARIO_RULES.get(travel_scenario)
        if scenario:
            _apply_rule_defaults(soft, hard, scenario, defaults_applied, override=False)
            scenario_rules.append(travel_scenario)

    fallback_companions = soft.get("companions") or soft.get("travelers") or "solo"
    passengers = _normalize_passengers(soft.get("passengers"))
    if not passengers and _to_non_negative_int(soft.get("passenger_count")) > 0:
        passengers = {
            "adult": _to_non_negative_int(soft.get("passenger_count")),
            "child": 0,
            "elderly": 0,
            "infant": 0,
        }
    if not passengers:
        passengers = _passengers_from_legacy_companions(fallback_companions)
    companions = _infer_travelers_from_passengers(
        passengers,
        fallback_companions,
    )
    if passengers:
        soft["passengers"] = passengers
        soft["passenger_count"] = sum(passengers.values())
    soft["companions"] = companions
    soft["travelers"] = companions
    companion_rule = COMPANION_RULES.get(companions)
    if companion_rule:
        _apply_rule_defaults(soft, hard, companion_rule, defaults_applied, override=False)

    companion_constraints = soft.get("companion_constraints") or []
    if isinstance(companion_constraints, str):
        companion_constraints = [
            item.strip() for item in companion_constraints.split(",") if item.strip()
        ]
    soft["companion_constraints"] = companion_constraints
    if companion_constraints:
        _apply_companion_constraints(soft, hard, companion_constraints, defaults_applied)

    if soft.get("solo_travel"):
        soft["no_late_arrival"] = True
        defaults_applied.append("独自出行：降低深夜到达方案权重")
    if soft.get("no_late_arrival"):
        hard["arrival_time_policy"] = "no_midnight"
        defaults_applied.append("不接受深夜到达")
    if soft.get("prefer_daytime_arrival"):
        hard["arrival_time_policy"] = "daytime_only"
        defaults_applied.append("希望优先白天到达")

    time_pref = (
        soft.get("time_preference_mode")
        or soft.get("time_preference")
        or hard.get("time_preference_mode")
        or hard.get("time_preference")
    )
    if quick_mode or not time_pref:
        soft["time_preference"] = "no_redeye"
        soft["time_preference_mode"] = "no_redeye"
        soft["departure_time_windows"] = [["06:00", "23:00"]]
        soft["arrival_time_windows"] = [["06:00", "23:00"]]
        soft["red_eye_allowed"] = False
        soft["early_morning_allowed"] = True
        hard["time_preference"] = "no_redeye"
        hard["time_preference_mode"] = "no_redeye"
        hard["departure_time_policy"] = "no_redeye"
        if not hard.get("arrival_time_policy") or hard.get("arrival_time_policy") == "any":
            hard["arrival_time_policy"] = "no_midnight"
        defaults_applied.append("不推荐红眼/凌晨到达")

    same_day_enabled = bool(
        hard.get("same_day_round_trip")
        or (subscription.get("constraints") or {}).get("same_day_round_trip")
        or subscription.get("same_day_round_trip")
    )
    if same_day_enabled:
        hard["same_day_round_trip"] = True
        soft["same_day_round_trip"] = True
        buffer_h = (
            hard.get("buffer_hours")
            or (subscription.get("constraints") or {}).get("buffer_hours")
            or subscription.get("buffer_hours")
        )
        margin_mode = (
            hard.get("transport_margin_mode")
            or (subscription.get("constraints") or {}).get("transport_margin_mode")
            or "standard"
        )
        if hard.get("business_start") and hard.get("business_end"):
            hard["meeting_time_priority"] = True
            soft["meeting_time_priority"] = True
            hard["time_source"] = "meeting_derived"
            constraints = dict(subscription.get("constraints") or {})
            constraints["time_source"] = "meeting_derived"
            subscription["constraints"] = constraints
            reserve_text = (
                f"{buffer_h}小时预留"
                if buffer_h
                else f"机场缓冲+车程+路途冗余({margin_mode})+安全余量"
            )
            defaults_applied.append(
                f"当天往返会议模式:以会议时间为最高优先,清晨早班/晚班返程均可选,已含{reserve_text}"
            )
            defaults_applied.append(
                f"会议模式接管时间设置:按会议{hard.get('business_start')}-{hard.get('business_end')}+预留推算,用户时间偏好本次不生效"
            )
        defaults_applied.append("当天往返:返程晚班视为正常,深夜限制放宽至午夜前到达")

    if quick_mode and hard.get("baggage") == "required":
        defaults_applied.append("优先含托运行李方案")
    elif hard.get("baggage") in (None, "unknown"):
        hard["baggage_default"] = "prefer_included"
        defaults_applied.append("优先含托运行李方案")

    if quick_mode:
        soft["allow_self_transfer"] = False
        hard.setdefault("accept_self_transfer", False)
        defaults_applied.append("不推荐非联程中转")
    elif "allow_self_transfer" not in soft and "accept_self_transfer" in hard:
        soft["allow_self_transfer"] = bool(hard.get("accept_self_transfer"))
    elif "allow_self_transfer" not in soft:
        soft["allow_self_transfer"] = False
        hard.setdefault("accept_self_transfer", False)
        defaults_applied.append("不推荐非联程中转")

    if quick_mode:
        soft["allow_overnight_transfer"] = False
        hard.setdefault("accept_overnight_transfer", False)
        defaults_applied.append("不推荐过夜中转")
    elif "allow_overnight_transfer" not in soft and "accept_overnight_transfer" in hard:
        soft["allow_overnight_transfer"] = bool(hard.get("accept_overnight_transfer"))
    elif "allow_overnight_transfer" not in soft:
        soft["allow_overnight_transfer"] = False
        hard.setdefault("accept_overnight_transfer", False)
        defaults_applied.append("不推荐过夜中转")

    route_type = str(
        ((subscription.get("basic") or {}).get("route_type"))
        or subscription.get("route_type")
        or ((subscription.get("constraints") or {}).get("route_type"))
        or ""
    ).lower()
    if route_type in {"domestic", "international", "greater_china"}:
        hard["route_type"] = route_type

    if not goals.get("secondary"):
        primary_goal = goals.get("primary", "buy_timing")
        route_alerts = ROUTE_TYPE_ALERTS.get(route_type) or {}
        default_alerts = list(
            route_alerts.get(primary_goal)
            or GOAL_TO_ALERTS.get(primary_goal)
            or GOAL_TO_ALERTS["buy_timing"]
        )
        goals["secondary"] = default_alerts
        if "cheaper_date" in default_alerts:
            defaults_applied.append("提醒前后日期更便宜方案")
        elif "better_same_day" in default_alerts and len(default_alerts) == 1:
            defaults_applied.append("提醒同日更优方案")
        elif "better_same_day" in default_alerts:
            defaults_applied.append("提醒涨价风险和同日更优方案")
        else:
            defaults_applied.append("提醒异常低价和涨价风险")

    if not goals.get("frequency"):
        goals["frequency"] = "important_only"
        defaults_applied.append("只在重要变化时提醒")

    soft["scenario_rules"] = sorted(set(scenario_rules))
    subscription["soft_preferences"] = soft
    subscription["hard_constraints"] = hard
    subscription["notification_goals"] = goals
    subscription["defaults_applied"] = defaults_applied
    return subscription


def _snapshot_timestamp(snapshot_time: str | None) -> float | None:
    if not snapshot_time:
        return None
    try:
        return datetime.fromisoformat(snapshot_time).timestamp()
    except ValueError:
        return None


def _depart_timestamp(days_to_dept: int) -> float:
    depart_day = date.today() + timedelta(days=days_to_dept)
    return datetime.combine(depart_day, time.min).timestamp()


def _valid_history(price_history) -> list[tuple[float, float]]:
    points = []
    for point in price_history or []:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        timestamp = _to_float(point[0])
        price = _to_float(point[1])
        if timestamp is None or price is None:
            continue
        points.append((timestamp, price))
    return sorted(points, key=lambda item: item[0])


def conditional_percentile(
    current_price, price_history, days_to_dept: int
) -> float | None:
    """Calculate current price percentile within a comparable booking window."""
    current = _to_float(current_price)
    if current is None:
        return None

    points = _valid_history(price_history)
    if not points:
        return None

    depart_ts = _depart_timestamp(days_to_dept)
    window_prices = [
        price
        for timestamp, price in points
        if abs(((depart_ts - timestamp) / 86400) - days_to_dept) <= 7
    ]
    if len(window_prices) < 10:
        window_prices = [price for _, price in points]

    if not window_prices:
        return None

    below_or_equal = sum(1 for price in window_prices if price <= current)
    return round((below_or_equal / len(window_prices)) * 100, 1)


def classify_movement(prices_recent: list[float]) -> str | None:
    """Classify recent price movement pattern from adjacent changes."""
    prices = [_to_float(price) for price in prices_recent]
    prices = [price for price in prices if price is not None]
    if len(prices) < 2:
        return None

    changes = []
    for previous, current in zip(prices, prices[1:]):
        if previous == 0:
            continue
        changes.append(((current - previous) / previous) * 100)

    if not changes:
        return None

    if max(abs(change) for change in changes) > 5:
        return "fare_class_jump"
    if max(changes) - min(changes) > 3:
        return "mean_reverting"
    return "stable"


def calc_volatility(prices: list[float]) -> dict:
    """Calculate standard deviation, coefficient of variation, and range."""
    clean_prices = [_to_float(price) for price in prices]
    clean_prices = [price for price in clean_prices if price is not None]
    if len(clean_prices) < 2:
        return {
            "std_dev": None,
            "cv": None,
            "range_pct": None,
            "stability": None,
        }

    avg_price = statistics.mean(clean_prices)
    std_dev = statistics.stdev(clean_prices)

    cv = std_dev / avg_price if avg_price else None
    range_pct = (
        ((max(clean_prices) - min(clean_prices)) / avg_price) * 100
        if avg_price
        else None
    )

    if cv is None:
        stability = None
    elif cv < 0.05:
        stability = "stable"
    elif cv < 0.10:
        stability = "moderate"
    else:
        stability = "volatile"

    return {
        "std_dev": round(std_dev, 2),
        "cv": round(cv, 4) if cv is not None else None,
        "range_pct": round(range_pct, 1) if range_pct is not None else None,
        "stability": stability,
    }


def acceptable_percentile(days_to_dept: int) -> int:
    if days_to_dept > 45:
        return 20
    if days_to_dept > 30:
        return 30
    if days_to_dept > 21:
        return 40
    if days_to_dept > 14:
        return 55
    if days_to_dept > 7:
        return 75
    return 95


def waiting_value(price_history, current_price, days_to_dept: int) -> float:
    """Estimate expected savings from buying now versus waiting one observation."""
    points = _valid_history(price_history)
    if len(points) < 4:
        return 0

    depart_ts = _depart_timestamp(days_to_dept)
    changes = []
    for index, (timestamp, price) in enumerate(points[:-1]):
        days_from_ts = (depart_ts - timestamp) / 86400
        if abs(days_from_ts - days_to_dept) > 3:
            continue
        next_price = points[index + 1][1]
        changes.append(next_price - price)

    if len(changes) < 3:
        return 0

    return round(statistics.mean(changes), 2)


def get_future_price_changes(
    price_history, days_to_dept: int, horizon: int = 7
) -> list[float]:
    """Return price changes within horizon days for comparable booking windows."""
    points = _valid_history(price_history)
    if len(points) < 2:
        return []

    depart_ts = _depart_timestamp(days_to_dept)
    horizon_seconds = horizon * 86400
    changes = []

    for index, (timestamp, price) in enumerate(points[:-1]):
        days_from_ts = (depart_ts - timestamp) / 86400
        if abs(days_from_ts - days_to_dept) > 7:
            continue

        future_points = [
            (future_ts, future_price)
            for future_ts, future_price in points[index + 1 :]
            if 0 < future_ts - timestamp <= horizon_seconds
        ]
        if not future_points:
            continue

        future_ts, future_price = max(future_points, key=lambda item: item[0])
        changes.append(future_price - price)

    return changes


def timing_analysis(price_history, current_price, days_to_dept) -> dict:
    """涔扮エ鏃舵満棰勬祴"""
    if _to_float(current_price) is None:
        return {"confidence": "low", "data_insufficient": True}

    try:
        days = int(days_to_dept)
    except (TypeError, ValueError):
        return {"confidence": "low", "data_insufficient": True}

    future_changes = get_future_price_changes(price_history, days, horizon=7)

    if not future_changes:
        return {"confidence": "low", "data_insufficient": True}

    drop_cases = [change for change in future_changes if change < -100]
    rise_cases = [change for change in future_changes if change > 100]
    stable_cases = [change for change in future_changes if -100 <= change <= 100]

    total = len(future_changes)
    result = {
        "drop_probability": round(len(drop_cases) / total * 100),
        "rise_probability": round(len(rise_cases) / total * 100),
        "stable_probability": round(len(stable_cases) / total * 100),
        "avg_drop": round(sum(drop_cases) / len(drop_cases)) if drop_cases else 0,
        "avg_rise": round(sum(rise_cases) / len(rise_cases)) if rise_cases else 0,
    }

    urgency = min(10, max(0, (100 - days) / 10))
    risk = result["rise_probability"] / 100
    result["buy_score"] = round(risk * 5 + urgency * 0.5, 1)

    return result


def weekday_analysis(db_path, route, depart_date) -> dict:
    """鍒嗘瀽涓嶅悓鏄熸湡鍑犵殑浠锋牸宸紓"""
    _ = db_path
    history = get_all_history(route, depart_date)

    if len(history) < 14:
        return {"data_insufficient": True}

    from collections import defaultdict

    weekday_prices = defaultdict(list)
    for record in history:
        snapshot_time = record.get("snapshot_time")
        price = _to_float(record.get("price"))
        if not snapshot_time or price is None:
            continue

        try:
            dt = datetime.fromisoformat(snapshot_time)
        except ValueError:
            continue

        weekday_prices[dt.weekday()].append(price)

    if sum(len(prices) for prices in weekday_prices.values()) < 14:
        return {"data_insufficient": True}

    weekday_names = ["鍛ㄤ竴", "鍛ㄤ簩", "鍛ㄤ笁", "鍛ㄥ洓", "鍛ㄤ簲", "鍛ㄥ叚", "鍛ㄦ棩"]
    stats = {}
    for day, prices in weekday_prices.items():
        stats[weekday_names[day]] = {
            "avg": round(sum(prices) / len(prices)),
            "min": min(prices),
            "count": len(prices),
        }

    sorted_days = sorted(stats.items(), key=lambda item: item[1]["avg"])
    cheapest_day = sorted_days[0][0]
    today = weekday_names[datetime.now().weekday()]

    return {
        "weekday_stats": dict(sorted_days),
        "cheapest_day": cheapest_day,
        "today": today,
        "today_is_cheap": today == cheapest_day,
    }


def airline_competition_analysis(
    flights: list[dict], historical_flights: list[dict] = None
) -> dict:
    """鑸徃绔炰簤鎬佸娍鍒嗘瀽"""
    _ = historical_flights
    from collections import defaultdict

    airline_prices = defaultdict(list)
    for flight in flights or []:
        price = _to_float(flight.get("price"))
        if price is None:
            continue

        airline = flight.get("airline_summary") or "鏈煡"
        airline_prices[airline].append(
            {
                "flight": dict(flight),
                "price": price,
                "combo": flight.get("flight_combo", ""),
                "duration": flight.get("total_hours"),
                "stops": flight.get("stops"),
            }
        )

    result = {}
    for airline, options in airline_prices.items():
        cheapest = min(options, key=lambda item: item["price"])
        result[airline] = {
            "cheapest_price": cheapest["price"],
            "best_option": cheapest["combo"],
            "duration": cheapest["duration"],
            "stops": cheapest["stops"],
            "options_count": len(options),
            "trend": "unknown",
        }

    sorted_airlines = sorted(result.items(), key=lambda item: item[1]["cheapest_price"])

    return {
        "airlines": dict(sorted_airlines),
        "cheapest_airline": sorted_airlines[0][0] if sorted_airlines else None,
        "price_spread": (
            sorted_airlines[-1][1]["cheapest_price"]
            - sorted_airlines[0][1]["cheapest_price"]
            if len(sorted_airlines) > 1
            else 0
        ),
    }


def comfort_score(flight: dict) -> dict:
    """计算航班舒适度评分（0-10）。"""
    score = 10.0
    penalties = []
    bonuses = []

    stops = int(flight.get("stops") or 0)
    if stops == 1:
        score -= 1
    elif stops == 2:
        score -= 3
        penalties.append("需要转机2次")
    elif stops >= 3:
        score -= 5
        penalties.append(f"需要转机{stops}次")

    for layover in flight.get("layovers", []) or []:
        wait = int(layover.get("wait_minutes") or 0)
        city = layover.get("city", "中转地")
        if wait > 480:
            score -= 2
            penalties.append(f"在{city}等待超过8小时，可能需要过夜")
        elif wait > 240:
            score -= 1
            penalties.append(f"在{city}等待较长")
        elif wait < 60:
            score -= 1.5
            penalties.append(f"在{city}转机时间仅{wait}分钟，较紧张")
        else:
            bonuses.append(f"转机等待时间合理（{wait // 60}小时{wait % 60}分钟）")

    hours = _to_float(flight.get("total_hours")) or 0
    if hours > 30:
        score -= 2
        penalties.append(f"全程{hours:g}小时，耗时较长")
    elif hours > 24:
        score -= 1
        penalties.append("全程超过24小时")
    elif hours < 20:
        bonuses.append("全程时间合理")

    segments = flight.get("segments", []) or []
    if any(segment.get("overnight") for segment in segments if isinstance(segment, dict)):
        score -= 0.5
        penalties.append("含过夜航段")

    score = max(0, min(10, round(score, 1)))

    return {
        "score": score,
        "level": "推荐" if score >= 7 else "一般" if score >= 5 else "较差",
        "penalties": penalties,
        "bonuses": bonuses,
    }


def detect_anomaly(
    flight: dict, price_insights: dict, all_prices: list[float]
) -> dict:
    """检测价格是否异常偏低。"""
    price = _to_float(flight.get("price"))
    if price is None:
        return {"is_anomaly": False}

    typical_range = (price_insights or {}).get("typical_price_range", [])
    if not typical_range or len(typical_range) < 2:
        return {"is_anomaly": False}

    typical_low = _to_float(typical_range[0])
    typical_high = _to_float(typical_range[1])
    if typical_low is None or typical_high is None:
        return {"is_anomaly": False}

    clean_prices = [_to_float(item) for item in all_prices or []]
    clean_prices = [item for item in clean_prices if item is not None]
    avg_price = sum(clean_prices) / len(clean_prices) if clean_prices else typical_high

    if price < typical_low * 0.7:
        discount_pct = round((1 - price / avg_price) * 100) if avg_price else 0
        return {
            "is_anomaly": True,
            "type": "鏋佺浣庝环",
            "discount_pct": discount_pct,
            "message": f"姣旀甯镐环鏍间綆{discount_pct}%锛屽彲鑳芥槸绯荤粺閿欒鎴栭檺鏃朵績閿€",
        }

    if price < typical_low:
        return {
            "is_anomaly": False,
            "is_good_deal": True,
            "message": "浣庝簬甯傚満姝ｅ父浠锋牸鍖洪棿",
        }

    return {"is_anomaly": False, "is_good_deal": False}


def generate_sparkline(prices: list, width: int = 14) -> str:
    """Generate a compact Unicode price sparkline."""
    if not prices or len(prices) < 2:
        return ""

    clean_prices = [_to_float(price) for price in prices]
    clean_prices = [price for price in clean_prices if price is not None and price > 0]
    if len(clean_prices) < 2:
        return ""

    blocks = "▁▂▃▄▅▆▇█"

    if len(clean_prices) > width:
        step = len(clean_prices) / width
        sampled = [clean_prices[int(index * step)] for index in range(width)]
    else:
        sampled = clean_prices

    min_p = min(sampled)
    max_p = max(sampled)

    if max_p == min_p:
        return blocks[3] * len(sampled)

    sparkline = ""
    for price in sampled:
        level = int((price - min_p) / (max_p - min_p) * 7)
        level = max(0, min(7, level))
        sparkline += blocks[level]

    return sparkline


def generate_trend_summary(price_history_data, current_price) -> dict:
    """生成趋势摘要。"""
    if not price_history_data:
        return {"available": False}

    if isinstance(price_history_data[0], (list, tuple)):
        prices = [_to_float(price) for _, price in price_history_data]
    else:
        prices = [_to_float(price) for price in price_history_data]
    prices = [price for price in prices if price is not None and price > 0]

    if len(prices) < 3:
        return {"available": False}

    sparkline = generate_sparkline(prices)
    min_price = min(prices)
    max_price = max(prices)
    avg_price = sum(prices) / len(prices)

    recent = prices[-5:] if len(prices) >= 5 else prices
    if recent[-1] > recent[0] * 1.03:
        recent_trend = "近期上涨"
    elif recent[-1] < recent[0] * 0.97:
        recent_trend = "近期下降"
    else:
        recent_trend = "近期平稳"

    current = _to_float(current_price)
    if current is None:
        current = prices[-1]

    if current <= min_price * 1.05:
        position = "接近历史最低"
    elif current >= max_price * 0.95:
        position = "接近历史最高"
    elif current < avg_price:
        position = "低于平均水平"
    else:
        position = "高于平均水平"

    return {
        "available": True,
        "sparkline": sparkline,
        "min_price": min_price,
        "max_price": max_price,
        "avg_price": round(avg_price),
        "current_position": position,
        "recent_trend": recent_trend,
        "data_points": len(prices),
    }


def price_position_description(current_price, price_history):
    """鐢ㄥ巻鍙叉暟鎹绠楀綋鍓嶄环鏍肩殑浣嶇疆鎻忚堪"""
    prices = _flatten_price_history(price_history)
    if len(prices) < 5:
        return None

    below = sum(1 for price in prices if price < current_price)
    percentile = round(below / len(prices) * 100)

    min_p = min(prices)
    max_p = max(prices)
    avg_p = round(sum(prices) / len(prices))

    if percentile <= 20:
        level = "低价区"
        desc = "当前价格低于历史80%的记录，属于少见的低价"
    elif percentile <= 40:
        level = "偏低区"
        desc = f"当前价格低于历史{100 - percentile}%的记录，低于大多数时候"
    elif percentile <= 60:
        level = "正常区"
        desc = "当前价格处于历史中间水平"
    elif percentile <= 80:
        level = "偏高区"
        desc = f"当前价格高于历史{percentile}%的记录，高于大多数时候"
    else:
        level = "高价区"
        desc = "当前价格高于历史80%的记录，属于偏贵时段"

    return {
        "percentile": percentile,
        "level": level,
        "description": desc,
        "min_price": min_p,
        "max_price": max_p,
        "avg_price": avg_p,
        "data_points": len(prices),
    }


def waiting_risk_description(price_history, current_price, days_to_dept):
    """璁＄畻缁х画绛夊緟涓€鍛ㄧ殑椋庨櫓鏀剁泭"""
    prices = _flatten_price_history(price_history)
    if len(prices) < 10:
        return None

    changes = []
    for index in range(1, len(prices)):
        changes.append(prices[index] - prices[index - 1])

    if not changes:
        return None

    ups = [change for change in changes if change > 0]
    downs = [change for change in changes if change < 0]

    up_prob = round(len(ups) / len(changes) * 100)
    down_prob = round(len(downs) / len(changes) * 100)
    avg_up = round(sum(ups) / len(ups)) if ups else 0
    avg_down = round(abs(sum(downs) / len(downs))) if downs else 0

    if days_to_dept <= 7:
        urgency = "出发在即，等待风险很高"
    elif days_to_dept <= 14:
        urgency = "时间较紧，等待空间有限"
    elif days_to_dept <= 30:
        urgency = "在最佳购买窗口内"
    else:
        urgency = "时间充裕，可以继续观察"

    return {
        "up_probability": up_prob,
        "down_probability": down_prob,
        "avg_up_amount": avg_up,
        "avg_down_amount": avg_down,
        "days_to_dept": days_to_dept,
        "urgency": urgency,
    }


def calc_buy_vs_wait_risk(
    current_price,
    price_history=None,
    days_to_dept=None,
    target_price=None,
    execution_grade=None,
) -> dict:
    """Compare the practical risk of buying now versus waiting."""
    current = _to_float(current_price)
    target = _to_float(target_price)
    prices = _flatten_price_history(price_history)
    try:
        days = int(days_to_dept) if days_to_dept is not None else None
    except (TypeError, ValueError):
        days = None

    buy_risks = [
        "可能遇到支付页跳价",
        "票规需确认（行李/退改）",
        "不同渠道售后政策不同",
    ]
    wait_risks = ["可能错过当前低价", "理想价再次出现不确定"]

    if days is not None and days <= 14:
        wait_risks.insert(1, "临近出发价格通常上涨")
        wait_level = "高"
    elif days is not None and days <= 30:
        wait_risks.insert(1, "出发窗口逐渐接近，价格上行风险增加")
        wait_level = "中"
    else:
        wait_level = "中"

    if execution_grade == "A":
        buy_level = "低"
    elif execution_grade in {"C", "D"}:
        buy_level = "高"
    else:
        buy_level = "中"

    low_position = False
    trend_text = "历史样本仍在积累"
    if prices and current is not None:
        avg_price = sum(prices) / len(prices)
        below = sum(1 for price in prices if price < current)
        percentile = below / len(prices) * 100
        low_position = percentile <= 35 or current <= avg_price
        if len(prices) >= 3:
            recent = prices[-5:] if len(prices) >= 5 else prices
            if recent[-1] < recent[0] * 0.98:
                trend_text = "近期仍有下降"
            elif recent[-1] > recent[0] * 1.02:
                trend_text = "近期价格走高"
            else:
                trend_text = "近期价格相对稳定"

    target_reached = bool(current is not None and target and current <= target)
    if target_reached and low_position:
        leaning = "倾向尽快验证购买"
        summary = "当前已接近理想价且处于低位，继续等的下行空间有限，倾向于尽快验证购买。"
    elif target_reached:
        leaning = "倾向验证购买"
        summary = "当前价格已达到理想价，主要需要确认支付页最终价格和票规。"
    elif trend_text == "近期仍有下降" and (days is None or days > 21):
        leaning = "可以短暂观察"
        summary = "价格仍有下降迹象且时间尚可，可以短暂观察，但需关注涨价风险。"
    else:
        leaning = "谨慎观察"
        summary = "当前价格或执行信息仍有不确定性，适合继续监控并等待更清晰信号。"

    return {
        "buy_level": buy_level,
        "wait_level": wait_level,
        "buy_risks": buy_risks,
        "wait_risks": wait_risks,
        "leaning": leaning,
        "summary": summary,
        "trend": trend_text,
    }


def _history_prices_for_combo(price_history, combo: str) -> list[float]:
    """Extract historical prices for one flight combo from flexible history shapes."""
    if not price_history or not combo:
        return []

    normalized_combo = combo.replace(" ", "").upper()

    if isinstance(price_history, dict):
        candidates = (
            price_history.get(combo)
            or price_history.get(normalized_combo)
            or price_history.get("by_flight", {}).get(combo)
            or price_history.get("by_flight", {}).get(normalized_combo)
        )
        if candidates is None:
            candidates = price_history.get("records") or price_history.get("history")
    else:
        candidates = price_history

    prices = []
    for item in candidates or []:
        if isinstance(item, dict):
            item_combo = item.get("flight_combo") or item.get("combo")
            if item_combo and item_combo.replace(" ", "").upper() != normalized_combo:
                continue
            price = _to_float(item.get("price") or item.get("current_min_price"))
        elif isinstance(item, (list, tuple)):
            if len(item) >= 3:
                item_combo = str(item[0])
                if item_combo.replace(" ", "").upper() != normalized_combo:
                    continue
                price = _to_float(item[2])
            elif len(item) >= 2:
                price = _to_float(item[1])
            else:
                price = None
        else:
            price = _to_float(item)

        if price is not None and price > 0:
            prices.append(price)

    return prices


def _source_price_entries(flight: dict) -> list[dict]:
    entries = (
        flight.get("source_prices")
        or flight.get("source_price_details")
        or flight.get("prices_by_source")
        or []
    )
    if isinstance(entries, dict):
        return [
            {"source": source, "price": price}
            for source, price in entries.items()
        ]
    return [entry for entry in entries if isinstance(entry, dict)]


def _add_anomaly(anomalies: list[dict], anomaly: dict, seen: set[tuple]) -> None:
    key = (
        anomaly.get("flight_combo"),
        anomaly.get("type"),
        anomaly.get("severity"),
        anomaly.get("message"),
    )
    if key in seen:
        return
    seen.add(key)
    anomalies.append(anomaly)


def detect_price_anomalies(flights, price_history=None):
    """Detect statistical, historical, source, and time-series price anomalies."""
    valid_flights = [
        flight for flight in flights or []
        if _to_float(flight.get("price")) is not None
    ]
    prices = [_to_float(flight.get("price")) for flight in valid_flights]
    prices = [price for price in prices if price is not None and price > 0]

    anomalies = []
    seen = set()
    if not prices:
        return anomalies

    avg_price = statistics.mean(prices)
    std_dev = statistics.stdev(prices) if len(prices) >= 2 else 0

    for flight in valid_flights:
        price = _to_float(flight.get("price"))
        if price is None or price <= 0:
            continue

        combo = flight.get("flight_combo", "")
        z_score = (price - avg_price) / std_dev if std_dev else 0

        if price < avg_price * 0.6 or price > avg_price * 1.5 or abs(z_score) > 2:
            if price < avg_price * 0.6 or z_score < -2:
                severity = "alert"
                anomaly_type = "统计低价异常"
            elif price > avg_price * 1.5 or z_score > 2:
                severity = "warning"
                anomaly_type = "统计高价异常"
            else:
                severity = "info"
                anomaly_type = "统计异常"
            _add_anomaly(
                anomalies,
                {
                    "type": anomaly_type,
                    "severity": severity,
                    "flight_combo": combo,
                    "price": price,
                    "z_score": round(z_score, 2),
                    "message": (
                        f"{combo or '该方案'} 当前¥{price:,.0f}，"
                        f"均价¥{avg_price:,.0f}，Z-score={z_score:.2f}"
                    ),
                },
                seen,
            )

        history_prices = _history_prices_for_combo(price_history, combo)
        if history_prices:
            history_min = min(history_prices)
            if price < history_min * 0.85:
                _add_anomaly(
                    anomalies,
                    {
                        "type": "疑似bug票价",
                        "severity": "alert",
                        "flight_combo": combo,
                        "price": price,
                        "reference_price": history_min,
                        "message": (
                            f"{combo or '该方案'} 当前¥{price:,.0f}，"
                            f"低于同航班历史最低¥{history_min:,.0f}超过15%"
                        ),
                    },
                    seen,
                )

        previous_price = _to_float(flight.get("previous_price"))
        if previous_price and previous_price > 0:
            change_pct = (price - previous_price) / previous_price * 100
            if abs(change_pct) > 30:
                _add_anomaly(
                    anomalies,
                    {
                        "type": "价格剧烈波动",
                        "severity": "warning",
                        "flight_combo": combo,
                        "price": price,
                        "previous_price": previous_price,
                        "change_pct": round(change_pct, 1),
                        "message": (
                            f"{combo or '该方案'} 从¥{previous_price:,.0f}"
                            f"变为¥{price:,.0f}，变化{change_pct:+.1f}%"
                        ),
                    },
                    seen,
                )

    grouped = {}
    for flight in valid_flights:
        combo = (flight.get("flight_combo") or "").replace(" ", "").upper()
        if not combo:
            continue
        grouped.setdefault(combo, [])
        source_entries = _source_price_entries(flight)
        if source_entries:
            for entry in source_entries:
                source_price = _to_float(entry.get("price"))
                if source_price is not None and source_price > 0:
                    grouped[combo].append(
                        {
                            "source": entry.get("source") or entry.get("data_source"),
                            "price": source_price,
                            "flight_combo": flight.get("flight_combo"),
                        }
                    )
        else:
            grouped[combo].append(
                {
                    "source": flight.get("data_source") or flight.get("source"),
                    "price": _to_float(flight.get("price")),
                    "flight_combo": flight.get("flight_combo"),
                }
            )

    for entries in grouped.values():
        entries = [entry for entry in entries if entry.get("price")]
        if len(entries) < 2:
            continue
        min_entry = min(entries, key=lambda item: item["price"])
        max_entry = max(entries, key=lambda item: item["price"])
        if min_entry["price"] <= 0:
            continue
        diff_pct = (max_entry["price"] - min_entry["price"]) / min_entry["price"] * 100
        if diff_pct > 20:
            _add_anomaly(
                anomalies,
                {
                    "type": "来源价格矛盾",
                    "severity": "warning",
                    "flight_combo": min_entry.get("flight_combo"),
                    "min_price": min_entry["price"],
                    "max_price": max_entry["price"],
                    "diff_pct": round(diff_pct, 1),
                    "message": (
                        f"{min_entry.get('flight_combo') or '同一航班'} 不同来源价差"
                        f"{diff_pct:.1f}%（¥{min_entry['price']:,.0f} - "
                        f"¥{max_entry['price']:,.0f}）"
                    ),
                    "sources": entries,
                },
                seen,
            )

    severity_order = {"alert": 0, "warning": 1, "info": 2}
    return sorted(
        anomalies,
        key=lambda item: (
            severity_order.get(item.get("severity"), 9),
            item.get("flight_combo") or "",
        ),
    )


def calculate_price_references(
    current_price, price_history, own_history, days_to_dept, current_flights
):
    """计算五层历史最低价参考。"""
    result = {}

    def timestamp_window(items):
        dates = []
        for item in items or []:
            value = item[0] if isinstance(item, (list, tuple)) and item else None
            if value is None:
                continue
            try:
                dates.append(datetime.fromtimestamp(float(value)).date().isoformat())
            except (OSError, OverflowError, TypeError, ValueError):
                continue
        return [min(dates), max(dates)] if dates else None

    def record_window(items):
        dates = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            value = item.get("date") or item.get("observed_at") or item.get("timestamp")
            text = str(value or "")[:10]
            try:
                dates.append(datetime.fromisoformat(text).date().isoformat())
            except (TypeError, ValueError):
                continue
        return [min(dates), max(dates)] if dates else None

    if price_history:
        if isinstance(price_history[0], (list, tuple)):
            all_prices = [price for _, price in price_history if price and price > 0]
        else:
            all_prices = [price for price in price_history if price and price > 0]

        if all_prices:
            result["absolute_min"] = {
                "price": min(all_prices),
                "label": "历史最低（所有条件）",
                "note": "可能出现在淡季或特殊促销，当前条件下不一定可达",
                "sample_size": len(all_prices),
                "window": timestamp_window(price_history),
            }

    if price_history and isinstance(price_history[0], (list, tuple)):
        relevant = []
        relevant_rows = []
        for timestamp, price in price_history:
            if price and price > 0 and timestamp:
                try:
                    hist_days = abs(timestamp - datetime.now().timestamp()) / 86400
                    if abs(hist_days - days_to_dept) <= 7:
                        relevant.append(price)
                        relevant_rows.append((timestamp, price))
                except Exception:
                    pass

        if len(relevant) >= 5:
            result["conditional_min"] = {
                "price": min(relevant),
                "label": f"同条件最低（提前{days_to_dept}天±7天）",
                "note": "在类似购买时间点下的历史最低",
                "sample_size": len(relevant),
                "window": timestamp_window(relevant_rows),
            }

    if own_history:
        recent_prices = [
            record.get("price", 0)
            for record in own_history
            if record.get("price") and record["price"] > 0
        ]
        if recent_prices:
            result["recent_min"] = {
                "price": min(recent_prices),
                "label": "近期最低（你关注以来）",
                "note": f"基于{len(recent_prices)}次采集数据",
                "sample_size": len(recent_prices),
                "window": record_window(own_history),
            }

    if current_flights:
        current_prices = [
            flight.get("price", 0)
            for flight in current_flights
            if flight.get("price") and flight["price"] > 0
        ]
        if current_prices:
            result["current_min"] = {
                "price": min(current_prices),
                "label": "当前可买最低",
                "note": "此刻市场上满足条件的最低价",
                "sample_size": len(current_prices),
            }

    for ref in result.values():
        diff = current_price - ref["price"]
        pct = round(diff / ref["price"] * 100, 1) if ref["price"] > 0 else 0
        ref["diff"] = diff
        ref["diff_pct"] = pct

    return result


def multi_window_analysis(current_price, own_history, google_history, days_to_dept):
    """多时间窗口纵向分析。"""
    result = {}

    # 绐楀彛涓€锛氱煭鏈熻秼鍔匡紙3-7澶╋級
    if own_history and len(own_history) >= 4:
        recent = [
            record["price"]
            for record in own_history[-14:]
            if record.get("price")
        ]  # 鏈€杩?澶┟楁瘡澶?娆?14鏉?
        if len(recent) >= 4:
            split_index = len(recent) // 2
            first_half = sum(recent[:split_index]) / split_index
            second_half = sum(recent[split_index:]) / (len(recent) - split_index)
            change_pct = round((second_half - first_half) / first_half * 100, 1)

            if change_pct > 2:
                trend = "上涨中"
            elif change_pct < -2:
                trend = "下降中"
            else:
                trend = "平稳"

            result["short_term"] = {
                "window": "近3-7天",
                "trend": trend,
                "change_pct": change_pct,
                "high": max(recent),
                "low": min(recent),
                "data_points": len(recent),
            }

    # 绐楀彛浜岋細涓湡浣嶇疆锛?4-30澶╋級
    if own_history and len(own_history) >= 10:
        month_prices = [record["price"] for record in own_history if record.get("price")]
        if month_prices:
            below = sum(1 for price in month_prices if price < current_price)
            percentile = round(below / len(month_prices) * 100)
            avg_price = round(sum(month_prices) / len(month_prices))

            result["mid_term"] = {
                "window": "你关注以来",
                "percentile": percentile,
                "min": min(month_prices),
                "max": max(month_prices),
                "avg": avg_price,
                "data_points": len(month_prices),
                "vs_min": current_price - min(month_prices),
                "vs_avg": current_price - avg_price,
            }

    # 绐楀彛涓夛細闀挎湡鍒嗕綅锛?0-60澶╋紝鐢℅oogle鏁版嵁锛?
    if google_history:
        if isinstance(google_history[0], (list, tuple)):
            prices = [price for _, price in google_history if price and price > 0]
        else:
            prices = [price for price in google_history if price and price > 0]

        if len(prices) >= 10:
            below = sum(1 for price in prices if price < current_price)
            percentile = round(below / len(prices) * 100)

            result["long_term"] = {
                "window": "近60天历史",
                "percentile": percentile,
                "min": min(prices),
                "max": max(prices),
                "avg": round(sum(prices) / len(prices)),
                "data_points": len(prices),
            }

    return result


def nearby_dates_comparison(
    origin, dest, center_date, fetch_function, days_range=2
):
    """鏌ヨ鍑哄彂鏃ュ墠鍚庡嚑澶╃殑鏈€浣庝环锛屽府鐢ㄦ埛鍙戠幇鏇翠究瀹滅殑鏃ユ湡"""
    from datetime import datetime, timedelta

    center = datetime.strptime(center_date, "%Y-%m-%d")
    results = {}

    for offset in range(-days_range, days_range + 1):
        check_date = center + timedelta(days=offset)
        date_str = check_date.strftime("%Y-%m-%d")
        weekday_names = ["鍛ㄤ竴", "鍛ㄤ簩", "鍛ㄤ笁", "鍛ㄥ洓", "鍛ㄤ簲", "鍛ㄥ叚", "鍛ㄦ棩"]
        weekday = weekday_names[check_date.weekday()]

        results[date_str] = {
            "date": date_str,
            "weekday": weekday,
            "offset": offset,
            "min_price": None,
        }

    return results


def compare_flights(flight_a: dict, flight_b: dict) -> dict:
    """Generate a direct comparison between two flight options."""
    price_diff = flight_a["price"] - flight_b["price"]
    time_diff = flight_a["total_duration_min"] - flight_b["total_duration_min"]
    stops_diff = flight_a["stops"] - flight_b["stops"]

    a_pros = []
    a_cons = []

    if price_diff < 0:
        a_pros.append(f"便宜 ¥{abs(price_diff):,.0f}")
    elif price_diff > 0:
        a_cons.append(f"贵 ¥{price_diff:,.0f}")

    if time_diff < 0:
        hours = abs(time_diff) // 60
        mins = abs(time_diff) % 60
        a_pros.append(f"快{hours}小时{mins}分钟")
    elif time_diff > 0:
        hours = time_diff // 60
        mins = time_diff % 60
        a_cons.append(f"慢{hours}小时{mins}分钟")

    if stops_diff < 0:
        a_pros.append(f"少转{abs(stops_diff)}次机")
    elif stops_diff > 0:
        a_cons.append(f"多转{stops_diff}次机")

    a_max_wait = max(
        (layover.get("wait_minutes", 0) for layover in flight_a.get("layovers", [])),
        default=0,
    )
    b_max_wait = max(
        (layover.get("wait_minutes", 0) for layover in flight_b.get("layovers", [])),
        default=0,
    )

    if a_max_wait > 480 and b_max_wait <= 480:
        a_cons.append("需要在机场过夜")
    if b_max_wait > 480 and a_max_wait <= 480:
        a_pros.append("不需要在机场过夜")

    return {
        "a_pros": a_pros,
        "a_cons": a_cons,
        "price_diff": price_diff,
        "time_diff_min": time_diff,
    }


SCORE_WEIGHTS = {
    "budget": {"price": 0.6, "duration": 0.15, "stops": 0.1, "layover": 0.15},
    "fast": {"price": 0.15, "duration": 0.5, "stops": 0.2, "layover": 0.15},
    "comfort": {"price": 0.15, "duration": 0.2, "stops": 0.3, "layover": 0.35},
    "balanced": {"price": 0.35, "duration": 0.25, "stops": 0.2, "layover": 0.2},
}

LCC_AIRLINES = [
    "Spirit",
    "Frontier",
    "鏄ョ鑸┖",
    "涔濆厓鑸┖",
    "Ryanair",
    "EasyJet",
    "AirAsia",
    "Scoot",
    "Peach",
    "Cebu Pacific",
    "IndiGo",
    "VietJet",
]

FULL_SERVICE_AIRLINES = [
    "Air China",
    "涓浗鍥借埅",
    "China Eastern",
    "涓滄柟鑸┖",
    "China Southern",
    "鍗楁柟鑸┖",
    "United",
    "Delta",
    "American",
    "Air Canada",
    "Lufthansa",
    "ANA",
    "Japan Airlines",
    "Singapore Airlines",
    "Cathay Pacific",
]


def overall_score(
    flight: dict, all_prices: list, all_durations: list, mode: str = "balanced"
) -> dict:
    """缁煎悎璇勫垎 0-10"""
    clean_prices = [_to_float(price) for price in all_prices or []]
    clean_prices = [price for price in clean_prices if price is not None]
    clean_durations = [_to_float(duration) for duration in all_durations or []]
    clean_durations = [
        duration for duration in clean_durations if duration is not None
    ]

    price = _to_float(flight.get("price"))
    duration = _to_float(flight.get("total_duration_min"))
    if price is None or duration is None or not clean_prices or not clean_durations:
        return {
            "total": 0,
            "price_score": 0,
            "duration_score": 0,
            "stops_score": 0,
            "layover_score": 0,
        }

    min_p, max_p = min(clean_prices), max(clean_prices)
    if max_p > min_p:
        price_score = 10 - (price - min_p) / (max_p - min_p) * 10
    else:
        price_score = 7

    min_d, max_d = min(clean_durations), max(clean_durations)
    if max_d > min_d:
        duration_score = 10 - (duration - min_d) / (max_d - min_d) * 10
    else:
        duration_score = 7

    stops = int(flight.get("stops") or 0)
    stops_score = {0: 10, 1: 8, 2: 5, 3: 3}.get(stops, 2)

    layover_score = 10
    for layover in flight.get("layovers", []) or []:
        wait = layover.get("wait_minutes", 0) or 0
        if wait > 480:
            layover_score -= 3
        elif wait > 240:
            layover_score -= 1.5
        elif wait < 60:
            layover_score -= 2
    layover_score = max(0, layover_score)

    weights = SCORE_WEIGHTS.get(mode, SCORE_WEIGHTS["balanced"])
    total = (
        price_score * weights["price"]
        + duration_score * weights["duration"]
        + stops_score * weights["stops"]
        + layover_score * weights["layover"]
    )

    return {
        "total": round(total, 1),
        "price_score": round(price_score, 1),
        "duration_score": round(duration_score, 1),
        "stops_score": round(stops_score, 1),
        "layover_score": round(layover_score, 1),
    }


def calc_transfer_risk(flight: dict) -> dict:
    """Evaluate execution risk for transfer-heavy itineraries."""
    risk_score = 0
    risk_factors = []
    segments = flight.get("segments", []) or []
    layovers = flight.get("layovers", []) or []

    stops = _stops_count(flight, default=0)
    if stops == 0:
        return {"level": "none", "label": "直飞", "score": 0, "factors": []}
    if stops >= 2:
        risk_score += 30
        risk_factors.append("多次中转")

    for layover in layovers:
        wait = layover.get("wait_minutes", 0) or 0
        if wait < 90:
            risk_score += 40
            risk_factors.append(f"中转时间仅{wait}分钟，可能赶不上")
        elif wait < 120:
            risk_score += 15
            risk_factors.append(f"中转时间{wait}分钟，较紧张")
        elif wait > 480:
            risk_score += 10
            risk_factors.append(f"中转等待{wait // 60}小时，较长")

    airlines = list(flight.get("airlines") or [])
    for segment in segments:
        airline = segment.get("airline") if isinstance(segment, dict) else ""
        if airline:
            airlines.append(airline)
    unique_airlines = sorted({airline for airline in airlines if airline})
    if len(unique_airlines) > 1:
        risk_score += 25
        risk_factors.append(f"跨航司（{'/'.join(unique_airlines)}），可能非联程")

    international_transfer_airports = {
        "NRT", "HND", "ICN", "TPE", "HKG", "SIN", "BKK", "KUL", "DOH",
        "DXB", "IST", "AMS", "FRA", "CDG", "LHR",
    }
    for layover in layovers:
        airport = layover.get("airport", "")
        if airport in international_transfer_airports:
            risk_score += 15
            risk_factors.append(f"经{city_name(airport)}中转，请确认是否需要过境签")

    if risk_score >= 50:
        level = "high"
        label = "高风险"
    elif risk_score >= 25:
        level = "medium"
        label = "中风险"
    else:
        level = "low"
        label = "低风险"

    return {
        "level": level,
        "label": label,
        "score": risk_score,
        "factors": risk_factors,
    }


def transfer_risk(flight: dict) -> dict:
    """Backward-compatible wrapper for the newer transfer execution risk."""
    return calc_transfer_risk(flight)


def calc_trend(recent_prices: list[float]) -> dict:
    """Compatibility trend summary used by check.py and notification text."""
    prices = [_to_float(price) for price in recent_prices]
    prices = [price for price in prices if price is not None]
    if len(prices) < 2:
        return {"trend": "flat", "change_pct": 0.0}

    midpoint = len(prices) // 2
    first_half = prices[:midpoint]
    second_half = prices[midpoint:]
    first_avg = statistics.mean(first_half)
    second_avg = statistics.mean(second_half)
    change_pct = ((second_avg - first_avg) / first_avg) * 100 if first_avg else 0

    if change_pct > 2:
        trend = "rising"
    elif change_pct < -2:
        trend = "falling"
    else:
        trend = "flat"

    return {"trend": trend, "change_pct": round(change_pct, 1)}


def _movement_desc(movement: str | None) -> str:
    descriptions = {
        "fare_class_jump": "出现舱位跳涨",
        "mean_reverting": "呈现均值回归波动",
        "stable": "相对稳定",
        None: "样本不足",
    }
    return descriptions.get(movement, movement)


def _volatility_desc(volatility: dict) -> str:
    stability = volatility.get("stability")
    cv = volatility.get("cv")
    if stability is None:
        return "样本不足"
    return f"{stability}(CV={cv})"


def _reason(
    pct,
    threshold,
    movement,
    volatility,
    wait_val,
    google_level,
) -> str:
    pct_text = "-" if pct is None else f"{pct}"
    return (
        f"当前价格处于历史P{pct_text}分位（阈值P{threshold}），"
        f"价格{_movement_desc(movement)}，"
        f"波动率{_volatility_desc(volatility)}，"
        f"继续等待的期望收益为{wait_val}元，"
        f"Google市场水平={google_level or '-'}"
    )


def generate_signal_v2(
    current_price,
    price_history,
    prices_recent,
    days_to_dept: int,
    google_insights,
    volatility: dict,
) -> tuple[str, str]:
    """Generate four-dimensional buy/wait signal."""
    pct = conditional_percentile(current_price, price_history, days_to_dept)
    movement = classify_movement(prices_recent)
    threshold = acceptable_percentile(days_to_dept)
    wait_val = waiting_value(price_history, current_price, days_to_dept)
    google_level = (google_insights or {}).get("price_level")
    reason = _reason(pct, threshold, movement, volatility, wait_val, google_level)

    if movement == "fare_class_jump" and days_to_dept < 21:
        return "buy_now", f"检测到不可逆涨价，建议立即购买：{reason}"

    if pct is not None and pct < threshold and wait_val > 0:
        return "strong_buy", reason
    if pct is not None and pct < threshold and wait_val <= 0:
        return "buy", reason
    if pct is not None and pct < threshold + 15 and wait_val > 0:
        return "consider", reason
    if days_to_dept <= 7:
        return "buy_now", f"距出发不足7天；{reason}"

    return "hold", reason


def generate_signal(
    price,
    trend: str,
    days_to_dept: int,
    min_seen,
    avg_price,
    google_level: str | None = None,
) -> str:
    """Compatibility wrapper for older callers."""
    if days_to_dept <= 7:
        return "buy_now"
    price = _to_float(price)
    min_seen = _to_float(min_seen)
    avg_price = _to_float(avg_price)
    if price is None or min_seen is None or avg_price is None:
        return "collecting"
    if price <= min_seen * 1.02 and trend == "rising":
        return "strong_buy"
    if price < avg_price * 0.95 and trend != "falling":
        return "buy"
    if price < avg_price * 0.95 and trend == "falling":
        return "wait"
    if days_to_dept <= 14:
        return "consider"
    if days_to_dept <= 21 and trend != "falling":
        return "consider"
    return "hold"


def analyze_with_google_insights(price_insights, current_price) -> dict:
    """Compatibility helper for Google market-level analysis."""
    price_insights = price_insights or {}
    return {
        "price_level": price_insights.get("price_level"),
        "typical_price_range": price_insights.get("typical_price_range"),
        "historical_percentile": conditional_percentile(
            current_price, price_insights.get("price_history") or [], 0
        ),
    }


def _target_price_history(records: list[dict]) -> list[list[float]]:
    history = []
    for record in records:
        timestamp = _snapshot_timestamp(record.get("snapshot_time"))
        price = _to_float(record.get("price"))
        if timestamp is None or price is None:
            continue
        history.append([timestamp, price])
    return history


def _stage(data_points: int) -> str:
    if data_points < 4:
        return "insufficient"
    if data_points <= 20:
        return "trend_only"
    return "full"


def analyze(
    db_path,
    route: str,
    depart_date: str,
    target_combo: str,
    price_insights: dict | None = None,
) -> dict:
    """Analyze target flight with a four-dimensional decision framework."""
    price_insights = price_insights or {}
    target_history = get_target_history(route, depart_date, target_combo)
    alternatives = get_latest_alternatives(route, depart_date, target_combo)
    prices = [
        price
        for price in (_to_float(record.get("price")) for record in target_history)
        if price is not None
    ]
    days_to_dept = (date.fromisoformat(depart_date) - date.today()).days
    data_points = len(prices)

    current_price = prices[-1] if prices else None
    min_seen = min(prices) if prices else None
    max_seen = max(prices) if prices else None
    avg_price = round(statistics.mean(prices), 2) if prices else None
    prices_recent = prices[-10:]
    trend = calc_trend(prices_recent)
    volatility = calc_volatility(prices)
    movement = classify_movement(prices_recent)
    target_price_history = _target_price_history(target_history)
    google_price_history = price_insights.get("price_history") or []
    decision_history = target_price_history if len(target_price_history) >= 10 else google_price_history
    percentile = conditional_percentile(current_price, decision_history, days_to_dept)
    threshold = acceptable_percentile(days_to_dept)
    wait_val = waiting_value(decision_history, current_price, days_to_dept)
    google_percentile = conditional_percentile(
        current_price, google_price_history, days_to_dept
    )
    google_lowest = price_insights.get("lowest_price")
    google_level = price_insights.get("price_level")
    google_typical_range = price_insights.get("typical_price_range")

    cheapest_alt = alternatives[0] if alternatives else None
    cheapest_alt_price = _to_float(cheapest_alt.get("price")) if cheapest_alt else None
    target_vs_cheapest = (
        round(current_price - cheapest_alt_price, 2)
        if current_price is not None and cheapest_alt_price is not None
        else 0
    )

    if current_price is None:
        signal = "collecting"
        signal_reason = "目标航班暂无价格数据，继续采集"
    elif data_points < 4:
        signal = "collecting"
        signal_reason = f"目标航班仅有{data_points}个数据点，样本不足"
    else:
        signal, signal_reason = generate_signal_v2(
            current_price,
            decision_history,
            prices_recent,
            days_to_dept,
            price_insights,
            volatility,
        )

    market_gap = 0
    market_gap_pct = 0
    google_lowest_float = _to_float(google_lowest)
    if current_price is not None and google_lowest_float:
        market_gap = round(current_price - google_lowest_float, 2)
        market_gap_pct = round((market_gap / google_lowest_float) * 100, 1)

    return {
        "current_price": current_price,
        "min_seen": min_seen,
        "max_seen": max_seen,
        "avg_price": avg_price,
        "data_points": data_points,
        "days_to_dept": days_to_dept,
        "trend": trend,
        "volatility": volatility,
        "movement": movement,
        "percentile": percentile,
        "threshold": threshold,
        "waiting_value": wait_val,
        "signal": signal,
        "signal_reason": signal_reason,
        "stage": _stage(data_points),
        "google_lowest": google_lowest,
        "google_level": google_level,
        "google_typical_range": google_typical_range,
        "google_percentile": google_percentile,
        "cheapest_alt": cheapest_alt,
        "target_vs_cheapest": target_vs_cheapest,
        "market_gap": market_gap,
        "market_gap_pct": market_gap_pct,
        "depart_date": depart_date,
        "route": route,
        "target_combo": target_combo,
    }


def analyze_combined(
    db, route: str, depart_date: str, target_combo: str, price_insights: dict
) -> dict:
    """Compatibility wrapper used by main.py."""
    return analyze(db, route, depart_date, target_combo, price_insights)


def _normalize_priorities(priorities) -> dict:
    if not priorities:
        return {}
    if isinstance(priorities, dict):
        return priorities

    result = {}
    if isinstance(priorities, list):
        for item in priorities:
            if isinstance(item, dict):
                result.update(item)
    return result


def _flight_hours(flight: dict) -> float:
    if flight.get("total_hours") is not None:
        return float(flight.get("total_hours") or 0)
    return float(flight.get("total_duration_min") or 0) / 60


def _max_layover_minutes(flight: dict) -> int:
    return max(
        (int(layover.get("wait_minutes") or 0) for layover in flight.get("layovers", [])),
        default=0,
    )


def _min_layover_minutes(flight: dict) -> int | None:
    waits = [
        int(layover.get("wait_minutes") or 0)
        for layover in flight.get("layovers", [])
        if int(layover.get("wait_minutes") or 0) > 0
    ]
    return min(waits) if waits else None


def _is_likely_self_transfer(flight: dict) -> bool:
    if flight.get("self_transfer") or flight.get("separate_tickets"):
        return True
    if str(flight.get("ticketing", "")).lower() in {"self_transfer", "separate"}:
        return True
    airlines = [airline for airline in (flight.get("airlines") or []) if airline]
    if not airlines:
        airlines = [
            segment.get("airline")
            for segment in (flight.get("segments") or [])
            if isinstance(segment, dict) and segment.get("airline")
        ]
    return int(flight.get("stops") or 0) > 0 and len(set(airlines)) > 1


def _has_airport_change_transfer(flight: dict) -> bool:
    if flight.get("airport_change") or flight.get("change_airport"):
        return True
    for layover in flight.get("layovers") or []:
        if isinstance(layover, dict) and (layover.get("airport_change") or layover.get("change_airport")):
            return True
    segments = [segment for segment in flight.get("segments") or [] if isinstance(segment, dict)]
    for prev_segment, next_segment in zip(segments, segments[1:]):
        arrival_airport = (
            prev_segment.get("arrival_airport")
            or prev_segment.get("arrival")
            or prev_segment.get("arrival_airport_code")
        )
        next_departure_airport = (
            next_segment.get("departure_airport")
            or next_segment.get("departure")
            or next_segment.get("departure_airport_code")
        )
        if arrival_airport and next_departure_airport and str(arrival_airport).upper() != str(next_departure_airport).upper():
            return True
    return False


def _format_hours(minutes: int) -> str:
    hours = minutes // 60
    mins = minutes % 60
    if mins:
        return f"{hours}小时{mins}分钟"
    return f"{hours}小时"


def _priority_violations(flight: dict, priorities: dict) -> list[str]:
    violations = []
    price = _to_float(flight.get("price")) or 0
    total_minutes = int(flight.get("total_duration_min") or 0)
    stops = int(flight.get("stops") or 0)
    max_wait = _max_layover_minutes(flight)

    budget = priorities.get("budget")
    if budget is not None and price > float(budget):
        violations.append(f"超出预算¥{price - float(budget):,.0f}")

    max_hours = priorities.get("max_hours")
    if max_hours is not None and total_minutes > int(float(max_hours) * 60):
        over_minutes = total_minutes - int(float(max_hours) * 60)
        violations.append(
            f"需要{_format_hours(total_minutes)}（超出时间限制{_format_hours(over_minutes)}）"
        )

    max_stops = priorities.get("max_stops")
    if max_stops is not None and stops > int(max_stops):
        violations.append(f"需要转机{stops}次（超出转机限制）")

    if priorities.get("no_overnight") and max_wait > 480:
        violations.append("有过夜转机")

    return violations


def _priority_boundary_notes(flight: dict, priorities: dict) -> list[str]:
    notes = []
    price = _to_float(flight.get("price")) or 0
    total_minutes = int(flight.get("total_duration_min") or 0)
    stops = int(flight.get("stops") or 0)
    max_wait = _max_layover_minutes(flight)

    budget = priorities.get("budget")
    if budget is not None:
        budget = float(budget)
        if budget * 0.95 <= price <= budget:
            notes.append("预算接近上限")

    max_hours = priorities.get("max_hours")
    if max_hours is not None:
        limit_minutes = int(float(max_hours) * 60)
        if limit_minutes - 60 <= total_minutes <= limit_minutes:
            notes.append("时间接近上限")

    max_stops = priorities.get("max_stops")
    if max_stops is not None and stops == int(max_stops):
        notes.append("转机次数刚好到上限")

    if priorities.get("no_overnight") and 360 <= max_wait <= 480:
        notes.append("转机等待较长但不过夜")

    return notes


def _hour_from_time(value: str | None) -> int | None:
    parsed = parse_flight_time(value)
    if parsed is not None:
        return parsed.hour
    text = str(value or "").replace("T", " ")
    match = re.search(r"(\d{1,2}):(\d{2})", text)
    if not match:
        return None
    try:
        hour = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return hour if 0 <= hour <= 23 else None


def _first_departure_hour(flight: dict) -> int | None:
    segments = flight.get("segments") or []
    if segments:
        return _hour_from_time(segments[0].get("dep_time") or segments[0].get("departure_time"))
    return _hour_from_time(flight.get("departure_time") or flight.get("dep_time"))


def _last_arrival_hour(flight: dict) -> int | None:
    segments = flight.get("segments") or []
    if segments:
        return _hour_from_time(segments[-1].get("arr_time") or segments[-1].get("arrival_time"))
    return _hour_from_time(flight.get("arrival_time") or flight.get("arr_time"))


TIME_SLOT_LABELS = {
    "early_morning": "鏃╃彮",
    "morning": "涓婂崍",
    "afternoon": "涓嬪崍",
    "evening": "鍌嶆櫄",
    "night": "鏅氱彮",
    "redeye": "绾㈢溂",
}


def time_slot_from_hour(hour: int | None) -> str | None:
    if hour is None:
        return None
    if 6 <= hour < 9:
        return "dawn"
    if 9 <= hour < 12:
        return "morning"
    if 12 <= hour < 14:
        return "noon"
    if 14 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 20:
        return "evening"
    if 20 <= hour < 23:
        return "night"
    return "redeye"


def _normalize_time_slots(slots) -> set[str]:
    if not slots:
        return set()
    if isinstance(slots, str):
        slots = [slots]
    normalized = set()
    for slot in slots:
        value = str(slot or "").strip()
        if not value:
            continue
        if value == "early_morning":
            value = "dawn"
        normalized.add(value)
    return normalized


def _matches_time_slots(hour: int | None, slots) -> bool:
    allowed = _normalize_time_slots(slots)
    if hour is None or not allowed:
        return True
    return time_slot_from_hour(hour) in allowed


def _direction_time_slots(preferences: dict, direction: str) -> tuple[object, object]:
    if direction == "return":
        dep_slots = preferences.get("return_departure_slots")
        arr_slots = preferences.get("return_arrival_slots")
    else:
        dep_slots = preferences.get("outbound_departure_slots")
        arr_slots = preferences.get("outbound_arrival_slots")

    dep_slots = (
        dep_slots
        or preferences.get("departure_slots")
        or preferences.get("preferred_departure_slots")
    )
    arr_slots = (
        arr_slots
        or preferences.get("arrival_slots")
        or preferences.get("preferred_arrival_slots")
    )
    return dep_slots, arr_slots


def _is_red_eye(flight: dict) -> bool:
    dep_hour = _first_departure_hour(flight)
    arr_hour = _last_arrival_hour(flight)
    return any(
        hour is not None and (hour >= 23 or hour < 6)
        for hour in (dep_hour, arr_hour)
    )


def _matches_departure_policy(flight: dict, policy: str) -> bool:
    hour = _first_departure_hour(flight)
    if hour is None or policy == "any":
        return True
    if policy == "after_06":
        return hour >= 6
    if policy == "daytime":
        return 8 <= hour <= 20
    if policy == "no_redeye":
        return not (hour >= 23 or hour < 6)
    return True


def _matches_arrival_policy(flight: dict, policy: str) -> bool:
    hour = _last_arrival_hour(flight)
    if hour is None or policy == "any":
        return True
    if policy == "no_midnight":
        return not (0 <= hour < 6)
    if policy == "daytime_only":
        return 6 <= hour <= 22
    return True


def _is_daytime_flight(flight: dict) -> bool:
    dep_hour = _first_departure_hour(flight)
    arr_hour = _last_arrival_hour(flight)
    dep_ok = dep_hour is None or 8 <= dep_hour <= 20
    arr_ok = arr_hour is None or 6 <= arr_hour <= 22
    return dep_ok and arr_ok


def _time_to_minutes(value) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    match = re.search(r"(\d{1,2}):(\d{2})", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour * 60 + minute


def _hour_to_minutes(hour: int | None) -> int | None:
    return None if hour is None else int(hour) * 60


def _matches_time_windows(hour: int | None, windows) -> bool:
    if hour is None or not windows:
        return True
    minute = _hour_to_minutes(hour)
    if minute is None:
        return True
    for window in windows:
        if not isinstance(window, (list, tuple)) or len(window) < 2:
            continue
        start = _time_to_minutes(window[0])
        end = _time_to_minutes(window[1])
        if start is None or end is None:
            continue
        if start <= end:
            if start <= minute < end:
                return True
        elif minute >= start or minute < end:
            return True
    return False


def _direction_time_windows(preferences: dict, direction: str) -> tuple[object, object]:
    if direction == "return":
        dep_windows = preferences.get("return_departure_time_windows")
        arr_windows = preferences.get("return_arrival_time_windows")
    else:
        dep_windows = preferences.get("outbound_departure_time_windows")
        arr_windows = preferences.get("outbound_arrival_time_windows")
    return (
        dep_windows or preferences.get("departure_time_windows") or [],
        arr_windows or preferences.get("arrival_time_windows") or [],
    )


def match_time_preference(flight: dict, soft_prefs: dict) -> tuple[bool, str]:
    mode = (
        soft_prefs.get("time_preference_mode")
        or soft_prefs.get("time_preference")
        or "unlimited"
    )
    mode = "unlimited" if mode == "any" else mode
    if mode == "unlimited":
        return True, ""

    dep_hour = _first_departure_hour(flight)
    arr_hour = _last_arrival_hour(flight)
    dep_red_eye = dep_hour is not None and (dep_hour >= 23 or dep_hour < 6)
    arr_red_eye = arr_hour is not None and (arr_hour >= 23 or arr_hour < 6)

    if mode == "daytime":
        is_daytime = (
            (dep_hour is None or 6 <= dep_hour < 20)
            and (arr_hour is None or 6 <= arr_hour < 20)
        )
        return True, "白天航班" if is_daytime else "非白天，排序降权"

    if mode == "no_redeye":
        if soft_prefs.get("same_day_round_trip") and soft_prefs.get("direction") == "return":
            if dep_red_eye or (arr_hour is not None and arr_hour < 6):
                return False, "红眼/凌晨航班，已排除"
            if arr_hour is not None and arr_hour >= 23:
                return True, "当天往返：返程晚班视为正常，深夜限制放宽至午夜前到达"
            return True, ""
        if dep_red_eye or arr_red_eye:
            return False, "红眼/凌晨航班，已排除"
        return True, ""

    if mode == "custom":
        direction = soft_prefs.get("direction", "outbound")
        dep_windows, arr_windows = _direction_time_windows(soft_prefs, direction)
        dep_ok = _matches_time_windows(dep_hour, dep_windows)
        arr_ok = _matches_time_windows(arr_hour, arr_windows)
        if dep_ok and arr_ok:
            return True, ""
        return False, "不在你设置的可接受时段内"

    return True, ""


def _has_free_checked_baggage(flight: dict) -> bool:
    fare_rules = flight.get("fare_rules") or {}
    baggage = fare_rules.get("baggage") or {}
    if baggage.get("checked_pieces") or baggage.get("checked_kg"):
        return True

    extra = flight.get("extra") or {}
    detail = extra.get("baggage_detail") or {}
    checked = detail.get("checked") or {}
    if checked.get("quantity", 0) > 0 and checked.get("is_free", False):
        return True

    return bool(extra.get("baggage"))


def _has_refund_change_flexibility(flight: dict, required: bool = False) -> bool:
    fare_rules = flight.get("fare_rules") or {}
    change = fare_rules.get("change") or {}
    refund = fare_rules.get("refund") or {}
    extra = flight.get("extra") or {}
    refund_change = extra.get("refund_change") or {}

    changeable = bool(
        change.get("allowed")
        or refund_change.get("changeable")
        or extra.get("changeable")
    )
    refundable = bool(
        refund.get("allowed")
        or refund_change.get("refundable")
        or extra.get("refundable")
    )
    return changeable and refundable if required else changeable


def _airline_text(flight: dict) -> str:
    names = []
    for key in ("airline_summary", "airline"):
        if flight.get(key):
            names.append(str(flight.get(key)))
    names.extend(str(name) for name in flight.get("airlines") or [] if name)
    for segment in flight.get("segments") or []:
        if isinstance(segment, dict) and segment.get("airline"):
            names.append(str(segment.get("airline")))
    return " ".join(names)


def _contains_any_airline(flight: dict, airline_names: list[str]) -> bool:
    text = _airline_text(flight).lower()
    return any(name.lower() in text for name in airline_names if name)


def _trip_mode(default_mode: str, preferences: dict | None) -> str:
    price_sensitivity = (preferences or {}).get("price_sensitivity")
    if price_sensitivity == "max":
        return "budget"
    if price_sensitivity == "low":
        return "comfort"
    trip_type = (preferences or {}).get("trip_type")
    if trip_type == "business_meeting":
        return "fast"
    if trip_type == "tourism":
        return "budget"
    if trip_type in {"family_elder", "family_visit"}:
        return "comfort"
    return default_mode


def _cheapest_price(flights: list[dict]) -> float | None:
    prices = [_to_float(flight.get("price")) for flight in flights]
    prices = [price for price in prices if price is not None and price > 0]
    return min(prices) if prices else None


def _stops_count(flight: dict, default: int = 99) -> int:
    """Return stop count; missing/blank values are treated as unknown, not direct."""
    value = flight.get("stops", default)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _collected_minutes_ago(flight: dict) -> float | None:
    raw_value = (
        flight.get("collected_at")
        or flight.get("snapshot_time")
        or flight.get("fetched_at")
    )
    collected_at = _parse_collected_at(raw_value)
    if collected_at is None:
        if raw_value:
            print(f"[采集时间解析失败] collected_at={repr(raw_value)}, 错误=无法识别格式")
        return None
    now = datetime.now(collected_at.tzinfo) if collected_at.tzinfo else datetime.now()
    return max(0, (now - collected_at).total_seconds() / 60)


def _parse_collected_at(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None

    candidates = [text]
    if text.endswith("Z"):
        candidates.append(text[:-1] + "+00:00")

    for candidate in candidates:
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def _calendar_selected_price(rows: list[dict]):
    """Return the selected date's calendar price, preserving the calendar scope."""
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("selected"):
            continue
        try:
            price = float(row.get("min_price"))
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None
    return None


def analyze_roundtrip_price_calendar(
    outbound_calendar: dict,
    target_date: str,
    return_calendar: dict | None,
    return_date: str,
    current_price=None,
) -> dict:
    """Analyze a fixed-return-date roundtrip reference calendar."""
    return_low = _calendar_price_on_date(return_calendar or {}, return_date)
    if return_low is None:
        fallback = analyze_price_calendar(outbound_calendar or {}, target_date, current_price)
        fallback["fallback_reason"] = "return_price_missing"
        fallback["note"] = (
            "返程日价格暂无，以下为出发日单程趋势。"
            + str(fallback.get("note") or "")
        )
        return fallback

    return_info = ((return_calendar or {}).get("dates") or {}).get(str(return_date)[:10]) or {}

    rows = _roundtrip_calendar_rows(
        outbound_calendar or {},
        target_date,
        return_low=return_low,
        return_date=return_date,
        return_sources=return_info.get("sources") or return_info.get("source"),
        return_observed_at=return_info.get("updated_at"),
        return_sample_n=return_info.get("count"),
    )
    savings = _calendar_row_savings(rows, target_date)
    return {
        "route": (outbound_calendar or {}).get("route"),
        "rows": rows,
        "savings": savings,
        "weekday_pattern": {},
        "scope": "roundtrip",
        "return_date": return_date,
        "return_min_price": return_low,
        "note": (
            f"每行=该出发日单程最低 + 返程日({str(return_date)[5:10]})单程最低，"
            "为往返价格参考下限，实际同渠道拼接价可能略高。"
        ),
    }


def analyze_price_calendar(
    calendar: dict,
    target_date: str,
    current_price,
    *,
    round_trip: bool = False,
    return_calendar: dict | None = None,
    return_date: str | None = None,
) -> dict:
    """Analyze nearby date savings and weekday patterns from a rolling calendar."""
    if round_trip and return_date:
        return analyze_roundtrip_price_calendar(
            calendar or {},
            target_date,
            return_calendar or {},
            return_date,
            current_price,
        )
    rows = _calendar_rows(calendar or {}, target_date)
    selected_price = _calendar_selected_price(rows)
    savings_basis = selected_price if selected_price is not None else current_price
    savings = _calendar_date_savings(calendar or {}, target_date, savings_basis)
    weekday_pattern = _calendar_weekday_pattern(calendar or {})
    return {
        "route": (calendar or {}).get("route"),
        "rows": rows,
        "savings": savings,
        "weekday_pattern": weekday_pattern,
        "scope": "oneway",
        "note": "为单程最低参考价，实付以支付页为准。",
    }


def build_price_hint_from_calendar(calendar: dict | None) -> dict:
    """Return a compact recent price range for form-side budget anchoring."""
    prices = []
    for info in ((calendar or {}).get("dates") or {}).values():
        if not isinstance(info, dict):
            continue
        try:
            price = float(info.get("min_price"))
        except (TypeError, ValueError):
            continue
        if price > 0:
            prices.append(price)

    if not prices:
        return {
            "has_data": False,
            "low": None,
            "high": None,
            "typical": None,
            "sample_count": 0,
            "scope": "oneway",
        }

    prices.sort()
    count = len(prices)
    mid = count // 2
    if count % 2:
        typical = prices[mid]
    else:
        typical = (prices[mid - 1] + prices[mid]) / 2

    return {
        "has_data": True,
        "low": round(prices[0]),
        "high": round(prices[-1]),
        "typical": round(typical),
        "sample_count": count,
        "scope": "oneway",
    }


def _flight_airline_code(flight: dict | None) -> str:
    from domestic_fare_rules import airline_code_from_flight

    return airline_code_from_flight(flight)


def _is_domestic_flight(flight: dict | None) -> bool:
    flight = flight or {}
    route_type = str(flight.get("route_type") or "").lower()
    if route_type:
        return route_type == "domestic"
    try:
        from sources.aggregator import is_domestic_route

        dep = (
            flight.get("departure_airport")
            or flight.get("dep_airport")
            or flight.get("origin")
            or ((flight.get("segments") or [{}])[0] or {}).get("dep_airport")
            or ((flight.get("segments") or [{}])[0] or {}).get("departure_airport")
        )
        arr = (
            flight.get("arrival_airport")
            or flight.get("arr_airport")
            or flight.get("destination")
            or ((flight.get("segments") or [{}])[-1] or {}).get("arr_airport")
            or ((flight.get("segments") or [{}])[-1] or {}).get("arrival_airport")
        )
        return is_domestic_route(dep, arr)
    except Exception:
        return False


def _ensure_domestic_fare_rules(flight: dict) -> dict:
    if not _is_domestic_flight(flight):
        return flight.get("fare_rules", {}) or {}
    from sources.fare_rules import standardize_domestic_fare_rules

    fare_rules = standardize_domestic_fare_rules(flight)
    flight["fare_rules"] = fare_rules
    return fare_rules


def _flight_airports(flight: dict | None) -> tuple[str, str]:
    flight = flight or {}
    segments = flight.get("segments") or []
    first = segments[0] if segments and isinstance(segments[0], dict) else {}
    last = segments[-1] if segments and isinstance(segments[-1], dict) else {}
    dep = (
        flight.get("departure_airport")
        or flight.get("dep_airport")
        or flight.get("origin")
        or first.get("dep_airport")
        or first.get("departure_airport")
        or ""
    )
    arr = (
        flight.get("arrival_airport")
        or flight.get("arr_airport")
        or flight.get("destination")
        or last.get("arr_airport")
        or last.get("arrival_airport")
        or ""
    )
    return str(dep).strip().upper(), str(arr).strip().upper()


def _flight_duration_minutes(flight: dict | None) -> float:
    flight = flight or {}
    value = _to_float(flight.get("total_duration_min"))
    if value is not None:
        return value
    hours = _to_float(flight.get("total_hours"))
    if hours is not None:
        return hours * 60
    return 120


def _needs_checked_baggage(preferences: dict | None) -> bool:
    prefs = preferences or {}
    baggage = prefs.get("baggage") or prefs.get("need_baggage") or prefs.get("checked_baggage_required")
    return baggage in {"required", "must", True, "yes", "需要", "必须"}


def calc_effective_cost(flight: dict, subscription, time_value_per_hour: float = 50) -> dict:
    """Estimate effective travel cost with ground transport, time, and baggage."""
    flight = flight or {}
    preferences = subscription or {}
    price = _to_float(flight.get("price")) or 0
    dep_airport, arr_airport = _flight_airports(flight)
    dep_logi = get_airport_logistics(dep_airport)
    arr_logi = get_airport_logistics(arr_airport)
    transport_cost = (dep_logi.get("taxi_cost") or 100) + (arr_logi.get("taxi_cost") or 100)
    flight_min = _flight_duration_minutes(flight)
    transport_min = (dep_logi.get("to_center_min") or 45) + (arr_logi.get("to_center_min") or 45)
    time_cost = round(((flight_min + transport_min) / 60) * float(time_value_per_hour))

    fare_rules = flight.get("fare_rules") or _ensure_domestic_fare_rules(flight)
    baggage_info = (fare_rules or {}).get("baggage") or {}
    baggage_cost = 0
    if _needs_checked_baggage(preferences) and baggage_info.get("included") is False:
        baggage_cost = 100

    profile_source = preferences
    if isinstance(preferences, dict) and (preferences.get("preferences") or preferences.get("soft_preferences")):
        profile_source = {
            **(preferences.get("soft_preferences") or {}),
            **(preferences.get("preferences") or {}),
        }
    passenger_profile = None
    if isinstance(profile_source, dict):
        passenger_profile = profile_source.get("passenger_profile")
        if not passenger_profile:
            passenger_profile = build_travel_profile(profile_source).get("passenger_profile")
    passenger_profile = passenger_profile or build_passenger_profile(None)
    family_fatigue_cost = 0
    if passenger_profile.get("needs_low_fatigue"):
        stops = _stops_count(flight)
        if stops > 0:
            family_fatigue_cost += 180 * stops
        if _is_red_eye(flight):
            family_fatigue_cost += 260
        if _is_likely_self_transfer(flight):
            family_fatigue_cost += 220
        if _has_airport_change_transfer(flight):
            family_fatigue_cost += 240
        dep_hour = _first_departure_hour(flight)
        arr_hour = _last_arrival_hour(flight)
        if dep_hour is not None and dep_hour < 8:
            family_fatigue_cost += 80
        if arr_hour is not None and arr_hour >= 21:
            family_fatigue_cost += 120
    effective = round(price + transport_cost + time_cost + baggage_cost + family_fatigue_cost)
    return {
        "ticket_price": round(price),
        "transport_cost": round(transport_cost),
        "time_cost": time_cost,
        "baggage_cost": baggage_cost,
        "family_fatigue_cost": round(family_fatigue_cost),
        "passenger_friendly_note": (
            "考虑老人/小孩同行的舒适度估算" if family_fatigue_cost else ""
        ),
        "effective_cost": effective,
        "time_value_per_hour": time_value_per_hour,
        "transport_minutes": round(transport_min),
        "breakdown_note": (
            f"票价¥{round(price)}+机场交通约¥{round(transport_cost)}"
            f"+时间成本约¥{time_cost}"
            + (f"+行李加购约¥{baggage_cost}" if baggage_cost else "")
        ),
        "note": "参考性综合估算，交通按打车估算，时间成本按¥50/小时估算。",
    }


def calc_roundtrip_effective_cost(
    outbound: dict,
    return_flight: dict,
    subscription,
    time_value_per_hour: float = 50,
) -> dict:
    """Estimate effective travel cost for a complete round-trip plan."""
    outbound_effective = (outbound or {}).get("effective_cost") or calc_effective_cost(
        outbound or {}, subscription, time_value_per_hour
    )
    return_effective = (return_flight or {}).get("effective_cost") or calc_effective_cost(
        return_flight or {}, subscription, time_value_per_hour
    )
    keys = ("ticket_price", "transport_cost", "time_cost", "baggage_cost", "family_fatigue_cost", "effective_cost")
    totals = {
        key: round(
            (_to_float(outbound_effective.get(key)) or 0)
            + (_to_float(return_effective.get(key)) or 0)
        )
        for key in keys
    }
    totals["scope"] = "roundtrip"
    totals["outbound"] = outbound_effective
    totals["return"] = return_effective
    totals["breakdown_note"] = (
        f"往返机票¥{totals['ticket_price']}+机场交通约¥{totals['transport_cost']}"
        f"+时间成本约¥{totals['time_cost']}"
        + (f"+行李约¥{totals['baggage_cost']}" if totals["baggage_cost"] else "")
    )
    totals["note"] = "往返参考性综合估算，非精确费用。"
    return totals


def enrich_travel_risk_and_cost(flight: dict, preferences: dict | None = None) -> dict:
    """Attach punctuality, logistics notes, and effective cost to one flight."""
    flight = flight or {}
    if not _is_domestic_flight(flight):
        return flight
    dep_airport, arr_airport = _flight_airports(flight)
    airline_code = _flight_airline_code(flight)
    flight["punctuality"] = estimate_punctuality(airline_code, dep_airport, arr_airport)
    dep_logi = get_airport_logistics(dep_airport)
    arr_logi = get_airport_logistics(arr_airport)
    notes = []
    for airport, logistics in ((dep_airport, dep_logi), (arr_airport, arr_logi)):
        note = logistics.get("note")
        if note:
            notes.append(
                f"{note}{logistics.get('to_center_min', 45)}分钟"
                if "分钟" not in str(note)
                else str(note)
            )
    flight["logistics_notes"] = notes
    flight["effective_cost"] = calc_effective_cost(flight, preferences or {})
    return flight


def build_airport_cost_comparison(
    flights: list[dict],
    preferences: dict | None = None,
    limit: int = 4,
) -> list[dict]:
    """按机场组合保留最低有效成本，并携带票价来源和采集时间。"""
    best_by_pair = {}
    for flight in flights or []:
        effective = flight.get("effective_cost") or {}
        value = _to_float(effective.get("effective_cost"))
        if value is None:
            effective = calc_effective_cost(flight, preferences or {})
            value = _to_float(effective.get("effective_cost"))
        if value is None:
            continue
        dep, arr = _flight_airports(flight)
        if not dep or not arr:
            continue
        key = (dep, arr)
        current = best_by_pair.get(key)
        if not current or value < current["effective_cost"]:
            note = "；".join(flight.get("logistics_notes") or [])
            if not note:
                note = str(effective.get("note") or "").strip()
            best_by_pair[key] = {
                "departure_airport": dep,
                "arrival_airport": arr,
                "ticket_price": _to_float(flight.get("price")),
                "effective_cost": value,
                "note": note,
                "flight_no": flight.get("flight_no") or flight.get("flight_combo"),
                "price_source": (
                    flight.get("price_source")
                    or flight.get("source")
                    or flight.get("data_source")
                    or ""
                ),
                "data_source": flight.get("data_source") or flight.get("source") or "",
                "collected_at": (
                    flight.get("collected_at")
                    or flight.get("snapshot_time")
                    or flight.get("fetched_at")
                    or ""
                ),
            }
    return sorted(best_by_pair.values(), key=lambda item: item["effective_cost"])[:limit]


def verify_fare_rules(flight, hard_constraints):
    issues = []
    matches = []
    hard_constraints = hard_constraints or {}
    flight = flight or {}

    fare_rules = _ensure_domestic_fare_rules(flight)
    if not fare_rules:
        fare_rules = flight.get("fare_rules", {}) or {}

    baggage_req = hard_constraints.get("baggage", "unknown")
    baggage_info = fare_rules.get("baggage", {}) or {}
    checked_kg = baggage_info.get("checked_kg", 0) or 0
    checked_pieces = baggage_info.get("checked_pieces", 0) or 0
    baggage_included = baggage_info.get("included")

    if baggage_req == "required":
        if checked_pieces > 0 or checked_kg > 0 or baggage_included is True:
            matches.append(f"含托运行李 {checked_kg or '标准'}kg/{checked_pieces or 1}件")
        elif baggage_included is False:
            issues.append("不含免费托运行李，需额外购买")
        elif fare_rules:
            issues.append("托运行李规则待确认，购买前请核实")
        else:
            issues.append("托运行李信息未确认，购买前请核实")

    refund_pref = hard_constraints.get("refund_flexibility", "unknown")
    refund_info = fare_rules.get("refund", {}) or {}
    change_info = fare_rules.get("change", {}) or {}
    refund_level = refund_info.get("level")

    if refund_pref in ("must_refundable", "required"):
        if refund_level == "高" or refund_info.get("allowed"):
            matches.append(refund_info.get("label") or "可退改")
        elif refund_info:
            issues.append(refund_info.get("note") or "该票退改规则不满足要求")
        else:
            issues.append("退票规则未确认，购买前请核实")

    if refund_pref in ("preferred", "must_refundable", "required"):
        if refund_level in {"高", "中"} or change_info.get("allowed"):
            matches.append(refund_info.get("label") or "可改签")
        elif refund_info:
            issues.append(refund_info.get("note") or "该票退改签较严格")
        else:
            issues.append("改签规则未确认")

    cabin = flight.get("cabin_class", "economy")
    if cabin in ("basic_economy", "light"):
        issues.append("基础经济舱/轻选舱，可能不含行李、不可选座、不可退改")

    airlines = flight.get("airlines", []) or []
    if flight.get("stops", 0) > 0:
        if len(set(airlines)) > 1:
            issues.append("跨航司中转，可能为非联程票，需确认")
        else:
            matches.append("同航司中转，大概率联程票")

    if fare_rules.get("source") == "国内标准规则推断":
        if _is_domestic_flight(flight):
            matches.append("国内标准规则推断，具体条款以支付页为准")
        else:
            matches.append("标准规则推断(国际线)，具体条款以支付页为准")

    if not issues:
        match_level = "full"
        match_label = "票规完全匹配"
    elif len(issues) <= len(matches):
        match_level = "partial"
        match_label = "票规部分匹配"
    else:
        match_level = "mismatch"
        match_label = "票规需确认"

    return {
        "level": match_level,
        "label": match_label,
        "matches": matches,
        "issues": issues,
    }


def make_domestic_tags(flight, profile, lowest_price=None):
    if not _is_domestic_flight(flight):
        return []

    from domestic_fare_rules import FULL_SERVICE, LCC_AIRLINES

    flight = flight or {}
    profile = profile or {}
    tags = []
    airline = _flight_airline_code(flight)
    stops = _stops_count(flight, 0)
    dep_hour = _first_departure_hour(flight)
    fare_rules = flight.get("fare_rules") or _ensure_domestic_fare_rules(flight)
    refund_level = (fare_rules.get("refund") or {}).get("level")
    baggage_included = (fare_rules.get("baggage") or {}).get("included")
    price = _to_float(flight.get("price"))

    if price is not None and (lowest_price is None or price <= float(lowest_price) * 1.03):
        tags.append("价格最优")
    if stops == 0 and (dep_hour is None or 6 <= dep_hour <= 20):
        tags.append("时间最优")
    if stops == 0:
        tags.append("少折腾")
    if airline in FULL_SERVICE and refund_level in ("高", "中") and stops == 0:
        tags.append("商务友好")
    if stops == 0 and (dep_hour is None or 8 <= dep_hour <= 18) and baggage_included:
        tags.append("家庭友好")
    passenger_profile = profile.get("passenger_profile") or {}
    family_or_elder = bool(passenger_profile.get("has_child") or passenger_profile.get("has_elderly"))
    if family_or_elder:
        if passenger_profile.get("has_child") and stops == 0 and baggage_included:
            tags.append("亲子友好")
        if passenger_profile.get("has_elderly") and stops == 0 and (dep_hour is None or 8 <= dep_hour <= 20):
            tags.append("老人友好")
        if stops == 0:
            tags.append("直飞优先")
            tags.append("低折腾")
        if dep_hour is None or 8 <= dep_hour <= 20:
            tags.append("白天到达")
        if baggage_included is not False:
            tags.append("行李明确")
        if stops <= 1:
            tags.append("中转风险低")
    if stops == 0 and airline in FULL_SERVICE:
        tags.append("低风险")
    if airline in LCC_AIRLINES:
        tags.append("廉航低价")

    scenario_priority = []
    if profile.get("price") == "high":
        scenario_priority.extend(["价格最优", "廉航低价"])
    if profile.get("time") == "high":
        scenario_priority.extend(["商务友好", "低风险", "时间最优"])
    if profile.get("comfort") == "high" or profile.get("risk_averse") == "high":
        scenario_priority.extend(["亲子友好", "老人友好", "低折腾", "直飞优先", "家庭友好", "少折腾", "低风险", "行李明确"])

    ordered = []
    for tag in scenario_priority + tags:
        if tag and tag in tags and tag not in ordered:
            ordered.append(tag)
    for tag in tags:
        if tag not in ordered:
            ordered.append(tag)
    return ordered[:4]


def estimate_availability(flight, collected_at=None):
    status = "unknown"
    label = "未验证"

    age_minutes = _collected_minutes_ago(
        {**flight, "collected_at": collected_at or flight.get("collected_at")}
    )

    sources = flight.get("data_source", "") or flight.get("source", "")
    source_count = len([source for source in str(sources).split("+") if source])
    price = _to_float(flight.get("price")) or 0

    if age_minutes is None:
        status = "unknown"
        label = "采集时间未知，建议刷新确认"
    elif age_minutes <= 30 and source_count >= 2 and price > 0:
        status = "likely_available"
        label = "大概率可购买"
    elif age_minutes <= 120 and price > 0:
        status = "possibly_available"
        label = "可能可购买"
    elif age_minutes > 120:
        status = "needs_refresh"
        label = "建议刷新确认"

    if price <= 0:
        status = "invalid"
        label = "价格异常"

    return {
        "status": status,
        "label": label,
        "age_minutes": int(age_minutes) if age_minutes is not None else None,
        "source_count": source_count,
    }


def classify_buyability(flight):
    source = str(flight.get("data_source") or flight.get("source") or "").lower()
    seat = str(flight.get("seat_status") or "").strip()
    age = (flight.get("availability") or {}).get("age_minutes")

    if "juhe" in source:
        sold_out_values = {"0", "无", "售罄", "已售罄", "sold_out", "none", "null"}
        if seat and seat.lower() not in sold_out_values:
            return {
                "status": "buyable",
                "label": "可购买",
                "note": f"余票{seat}, 实时价",
            }
        if not seat:
            return {
                "status": "need_verify",
                "label": "需支付页确认",
                "note": "国内报价，最终库存和实付价需支付页确认",
            }
        return {
            "status": "sold_out",
            "label": "已售罄",
            "note": "聚合实时源显示无可售库存",
        }

    if age is None:
        return {"status": "unknown", "label": "需验证", "note": "采集时间未知"}
    if age <= 30:
        return {"status": "need_verify", "label": "需验证", "note": "价格较新"}
    if age <= 120:
        return {"status": "reference", "label": "仅参考", "note": "价格需刷新确认"}
    return {"status": "expired", "label": "已失效", "note": "建议刷新"}


def calc_execution_risk(flight):
    score = 0
    factors = []

    avail = flight.get("availability", {}) or {}
    age = avail.get("age_minutes")
    if age is None:
        score += 15
        factors.append("采集时间未知")
    elif age > 120:
        score += 30
        factors.append("价格超过2小时未验证")
    elif age > 30:
        score += 15
        factors.append("价格30分钟前采集")

    fare = flight.get("fare_verification", {}) or {}
    if fare.get("level") == "mismatch":
        score += 25
        factors.append("票规与需求不匹配")
    elif fare.get("level") == "partial":
        score += 12
        factors.append("票规部分未确认")

    transfer = flight.get("transfer_risk", {}) or {}
    if transfer.get("level") == "high":
        score += 25
        factors.append("中转执行风险高")
    elif transfer.get("level") == "medium":
        score += 12
        factors.append("中转有一定风险")

    source_count = avail.get("source_count", 0)
    if source_count == 0:
        score += 20
        factors.append("无数据源验证")
    elif source_count == 1:
        score += 10
        factors.append("仅单一数据源")

    if score >= 50:
        risk_level = "high"
        risk_label = "执行风险高"
        advice = "该方案存在较多不确定因素，建议谨慎对待或等待更可靠的方案"
    elif score >= 25:
        risk_level = "medium"
        risk_label = "执行风险中等"
        advice = "建议购买前仔细核对支付页的价格、行李和退改规则"
    else:
        risk_level = "low"
        risk_label = "执行风险低"
        advice = "该方案信息较完整，可信度较高"

    flight["execution_risk"] = {
        "level": risk_level,
        "label": risk_label,
        "score": score,
        "factors": factors,
        "advice": advice,
    }
    return flight["execution_risk"]


def calc_execution_grade(flight: dict, hard_constraints=None) -> dict:
    """Calculate whether a shown option is actionable enough to execute."""
    hard_constraints = hard_constraints or {}
    reasons = []
    price = _to_float(flight.get("price")) or 0
    risk = flight.get("execution_risk") or calc_execution_risk(flight)
    fare = flight.get("fare_verification") or {}
    price_advice = flight.get("price_advice") or {}
    transfer = flight.get("transfer_risk") or calc_transfer_risk(flight)
    companions = hard_constraints.get("companions")

    reasons.extend(risk.get("factors") or [])
    if fare.get("issues"):
        reasons.extend(fare.get("issues")[:2])

    if price <= 0 or price_advice.get("level") == "over_budget":
        grade = "D"
        grade_label = "D级 - 不推荐"
    elif companions in {"with_elderly", "with_child", "with_elderly_child", "with_both"} and transfer.get("level") == "high":
        grade = "D"
        grade_label = "D级 - 不推荐（中转风险高，不适合老人/小孩）"
        reasons.append("中转风险高，不适合老人/小孩")
    elif risk.get("level") == "low" and price_advice.get("level") in {"below_target", "within_tolerance"}:
        grade = "A"
        grade_label = "A级 - 强烈建议"
    elif risk.get("level") == "medium" or price_advice.get("level") == "within_budget":
        grade = "B"
        grade_label = "B级 - 建议确认后购买"
    elif risk.get("level") == "high" or fare.get("level") == "mismatch":
        grade = "C"
        grade_label = "C级 - 仅供参考"
    elif risk.get("level") == "low":
        grade = "A"
        grade_label = "A级 - 强烈建议"
    else:
        grade = "B"
        grade_label = "B级 - 建议确认后购买"

    score = max(0, 100 - int(risk.get("score", 0)))

    flight["execution_grade"] = grade
    flight["execution_label"] = grade_label
    flight["execution_reasons"] = reasons
    flight["execution_score"] = score
    return {
        "grade": grade,
        "label": grade_label,
        "reasons": reasons,
        "score": score,
    }


def _confidence_level_label(value: str) -> str:
    return value if value in {"高", "中", "低"} else "低"


def calc_confidence(flight: dict, source_stats=None, price_history=None) -> dict:
    """Break decision confidence into clean user-readable dimensions."""
    flight = flight or {}
    source_stats = source_stats or {}
    dimensions = {}
    details = {}

    raw_age = (flight.get("availability") or {}).get("age_minutes")
    try:
        age = int(raw_age) if raw_age is not None else None
    except (TypeError, ValueError):
        age = None
    dimensions["价格新鲜度"] = (
        "高" if age is not None and age <= 30 else "中" if age is not None and age <= 120 else "低"
    )
    details["价格新鲜度"] = f"{age}分钟前采集" if age is not None else "采集时间未知"

    history_count = len(price_history) if price_history else 0
    dimensions["历史样本量"] = "高" if history_count >= 14 else "中" if history_count >= 5 else "低"
    details["历史样本量"] = f"近期{history_count}次采集"

    source_count = (flight.get("availability") or {}).get("source_count", 0)
    if not source_count:
        data_source = str(flight.get("data_source") or flight.get("source") or "")
        source_count = len([item for item in data_source.split("+") if item])
    if not source_count and source_stats:
        source_count = sum(
            1
            for value in source_stats.values()
            if isinstance(value, dict) and "成功" in str(value.get("status", ""))
        )
    dimensions["渠道一致性"] = "高" if source_count >= 3 else "中" if source_count >= 2 else "低"
    details["渠道一致性"] = f"{source_count}个数据源可交叉验证" if source_count else "数据源不足"

    fare = flight.get("fare_verification") or {}
    fare_level = fare.get("level")
    dimensions["票规完整度"] = "高" if fare_level == "full" else "中" if fare_level == "partial" else "低"
    details["票规完整度"] = "票规已确认" if fare_level == "full" else "行李/退改签仍需支付页确认"

    avail = flight.get("availability") or {}
    avail_status = avail.get("status", "unknown")
    if avail_status == "likely_available":
        dimensions["可购买性"] = "中高"
        details["可购买性"] = "有多个渠道可验证，但最终价格和票规以支付页为准"
    elif avail_status == "possibly_available":
        dimensions["可购买性"] = "中"
        details["可购买性"] = "需要到支付页确认最终价、库存和票规"
    else:
        dimensions["可购买性"] = "低"
        details["可购买性"] = "购买链路尚未验证"

    route_type = str(flight.get("route_type") or "").lower()
    if not route_type:
        for value in source_stats.values():
            if isinstance(value, dict) and value.get("route_type"):
                route_type = str(value.get("route_type")).lower()
                break

    data_source = str(flight.get("data_source") or flight.get("source") or "").lower()
    primary_source = str(flight.get("primary_source") or "").lower()
    if route_type == "domestic":
        if "juhe" in data_source or primary_source == "juhe":
            dimensions["渠道一致性"] = "高"
            details["渠道一致性"] = "聚合数据为国内主源，Google Flights用于交叉验证"
            if dimensions.get("可购买性") != "低":
                dimensions["可购买性"] = "中高"
            details["可购买性"] = "聚合数据国内报价为主，最终库存和票规以支付页为准"
        elif any(source in data_source for source in ("serpapi", "searchapi", "hasdata")):
            dimensions["渠道一致性"] = "中"
            details["渠道一致性"] = "国内航线仅有Google参考，建议重点确认支付页"
            details["可购买性"] = "仅Google参考，最终价格和库存需支付页确认"
    elif any(source in data_source for source in ("serpapi", "searchapi", "hasdata")):
        if source_count >= 2:
            dimensions["渠道一致性"] = "高"
            details["渠道一致性"] = "Google Flights多源交叉验证"

    high_count = sum(1 for value in dimensions.values() if value == "高")
    medium_or_better = sum(1 for value in dimensions.values() if value in {"高", "中高", "中"})
    if high_count >= 4:
        overall = "高"
    elif medium_or_better >= 4:
        overall = "中高"
    else:
        overall = "中"

    return {"overall": overall, "dimensions": dimensions, "details": details}


def generate_decision_summary(
    lowest_price,
    target_price,
    max_budget,
    confidence=None,
    execution_grade=None,
) -> dict:
    """Generate a compact decision summary for the notification top card."""
    lowest = _to_float(lowest_price)
    target = _to_float(target_price)
    max_b = _to_float(max_budget)
    confidence = confidence or {}
    execution_grade = execution_grade or "C"

    if lowest is None:
        price_judgment = "暂无有效价格"
    elif target and lowest <= target:
        price_judgment = "偏低，已达理想价"
    elif target and lowest <= target * 1.05:
        price_judgment = "接近理想价"
    elif max_b and lowest <= max_b:
        price_judgment = "在预算内但高于理想价"
    elif max_b and lowest > max_b:
        price_judgment = "超出预算"
    else:
        price_judgment = "需要结合历史价格判断"

    if execution_grade == "A":
        exec_judgment = "信息完整，可购买"
    elif execution_grade == "B":
        exec_judgment = "购买前需确认价格和票规"
    else:
        exec_judgment = "购买渠道或票规待确认"

    if price_judgment.startswith("偏低") and execution_grade == "A":
        conclusion = "强烈建议购买"
    elif "接近理想价" in price_judgment or "偏低" in price_judgment:
        conclusion = "可以购买前验证"
    elif "预算内" in price_judgment:
        conclusion = "可以观察"
    else:
        conclusion = "建议等待"

    if lowest and target:
        verify_limit = target * 1.05
    elif lowest:
        verify_limit = lowest * 1.05
    else:
        verify_limit = None

    if conclusion in {"强烈建议购买", "可以购买前验证"} and verify_limit:
        action_advice = f"若支付页最终价≤¥{verify_limit:,.0f}且含托运行李，可以购买"
    elif max_b:
        action_advice = f"若最终价仍低于¥{max_b:,.0f}，可按刚需程度决定"
    else:
        action_advice = "先验证支付页最终价、行李和退改规则"

    reasons = []
    if lowest and target:
        if lowest <= target:
            reasons.append(f"当前价格¥{lowest:,.0f}已达到理想入手价")
        elif lowest <= target * 1.05:
            reasons.append(f"当前价格¥{lowest:,.0f}已接近理想入手价")
        else:
            reasons.append(f"当前价格¥{lowest:,.0f}高于理想入手价")
    elif lowest:
        reasons.append(f"当前价格为¥{lowest:,.0f}")
    reasons.append(f"执行判断：{exec_judgment}")
    if confidence.get("overall"):
        reasons.append(f"数据置信度：{confidence['overall']}")

    return {
        "conclusion": conclusion,
        "price_judgment": price_judgment,
        "execution_judgment": exec_judgment,
        "action_advice": action_advice,
        "confidence": confidence.get("overall", "中"),
        "reasons": reasons[:3],
    }


def _flatten_price_history(price_history) -> list[float]:
    """Normalize price history formats into valid positive prices."""
    prices = []
    if isinstance(price_history, dict):
        price_history = price_history.get("price_history") or price_history.get("history") or []
    for item in price_history or []:
        if isinstance(item, dict):
            value = (
                item.get("price")
                if item.get("price") is not None
                else item.get("total")
            )
        else:
            value = item[1] if isinstance(item, (list, tuple)) and len(item) >= 2 else item
        price = _to_float(value)
        if price and price > 0:
            prices.append(price)
    return prices


def _history_reference_suffix(prices) -> str:
    # 日期窗口由标准依据信封在通知层补入，这里只保留真实样本数。
    return f"（n={len(prices)}）" if prices else ""


def _insufficient_history_copy(prices) -> str:
    sample_count = len(prices or [])
    return (
        f"同条件样本不足（当前n={sample_count}），继续积累中，"
        f"暂不给出价格位置判断{_history_reference_suffix(prices)}"
    )


def build_budget_gap(display_price, max_price=None, ideal_price=None) -> dict:
    """Return positive budget gaps for user-facing notification copy."""
    current = _to_float(display_price)
    max_p = _to_float(max_price)
    ideal = _to_float(ideal_price)
    items = []
    over_max = None
    over_ideal = None
    if current is not None and max_p is not None and current > max_p:
        over_max = current - max_p
        items.append(f"高于最高价¥{over_max:,.0f}")
    if current is not None and ideal is not None and current > ideal:
        over_ideal = current - ideal
        items.append(f"高于理想价¥{over_ideal:,.0f}")
    return {
        "over_max": over_max,
        "over_ideal": over_ideal,
        "items": items,
        "text": " | ".join(items),
        "is_over_budget": over_max is not None,
    }


def build_passenger_budget_gap(
    display_price,
    max_price=None,
    ideal_price=None,
    budget_scope="total",
    total_passengers=1,
) -> dict:
    """Return budget gaps using the same all-passenger price scope as display."""
    current = _to_float(display_price)
    limits = passenger_budget_limits(max_price, ideal_price, budget_scope, total_passengers)
    max_p = limits.get("max_budget_total")
    ideal = limits.get("ideal_price_total")
    items = []
    over_max = None
    over_ideal = None
    if current is not None and max_p is not None and current > max_p:
        over_max = current - max_p
        label = "每人预算折算总上限" if limits["budget_scope"] == "per_person" else "最高价"
        items.append(f"高于{label}¥{over_max:,.0f}")
    if current is not None and ideal is not None and current > ideal:
        over_ideal = current - ideal
        label = "每人理想价折算总价" if limits["budget_scope"] == "per_person" else "理想价"
        items.append(f"高于{label}¥{over_ideal:,.0f}")
    return {
        "over_max": over_max,
        "over_ideal": over_ideal,
        "items": items,
        "text": " | ".join(items),
        "is_over_budget": over_max is not None,
        **limits,
    }


def build_next_step_guidance(
    push_type=None,
    display_price=None,
    max_price=None,
    ideal_price=None,
    scenarios=None,
    trip_rigidity=None,
) -> dict:
    """Build the three user choices after an over-budget or rising-price alert."""
    current = _to_float(display_price)
    max_p = _to_float(max_price)
    scenario_values = scenarios or []
    if isinstance(scenario_values, str):
        scenario_values = [scenario_values]
    scenario_text = " ".join(str(item) for item in scenario_values)
    rigid = bool(trip_rigidity) or any(
        key in scenario_text.lower()
        for key in ("business", "meeting", "important", "商务", "会议", "重要")
    )
    watch_target = max_p if max_p is not None else _to_float(ideal_price)
    watch_summary = (
        f"系统会在跌破¥{watch_target:,.0f}时提醒你"
        if watch_target is not None
        else "系统会在价格回落或出现更优方案时提醒你"
    )
    if current is not None and max_p is not None and current <= max_p and "涨价" in str(push_type or ""):
        watch_summary = "价格仍在预算内,但近期上涨,可继续观察下一次变化"
    verify_summary = (
        "你的出行场景偏刚性,若会议或行程无法改期,可只验证最终价和库存"
        if rigid
        else "若确实必须出行,可验证最终价；否则优先等待或换日期"
    )
    return {
        "rigid": rigid,
        "items": [
            {"label": "继续监控等降价", "summary": watch_summary, "action": "保持监控"},
            {"label": "调整预算或日期", "summary": "当前日期偏贵,换日期可能更便宜", "action": "修改监控"},
            {"label": "因刚需必须出行", "summary": verify_summary, "action": "验证方案A渠道"},
        ],
    }


def _no_result_reason_bucket(reason: str) -> str:
    text = str(reason or "")
    if "直飞" in text or "direct" in text.lower() or "中转" in text:
        return "direct"
    if any(token in text for token in ("会议", "窗口", "时间", "到达", "起飞", "返程", "去程")):
        return "meeting"
    if any(token in text for token in ("预算", "最高", "超", "价格", "budget")):
        return "budget"
    return "other"


def _stage_drop_counts(counts: dict) -> dict:
    """Infer self-consistent eliminations from staged remaining counts."""
    counts = counts or {}

    def _as_int(value):
        try:
            if value is None or value == "":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    start = _as_int(counts.get("valid_price_count"))
    if start is None:
        start = _as_int(counts.get("total_candidates")) or 0
    after_basic = _as_int(
        counts.get("after_direct")
        if counts.get("after_direct") is not None
        else counts.get("after_basic_filter")
    )
    if after_basic is None:
        after_basic = start
    after_meeting = _as_int(
        counts.get("after_meeting")
        if counts.get("after_meeting") is not None
        else counts.get("after_meeting_window")
    )
    if after_meeting is None:
        after_meeting = after_basic
    same_day_combos = _as_int(counts.get("same_day_combos"))
    if same_day_combos is not None:
        after_meeting = min(after_meeting, same_day_combos)
    after_budget = _as_int(counts.get("after_budget"))
    if after_budget is None:
        after_budget = after_meeting

    after_basic = max(0, min(after_basic, start))
    after_meeting = max(0, min(after_meeting, after_basic))
    after_budget = max(0, min(after_budget, after_meeting))
    return {
        "direct": max(0, start - after_basic),
        "meeting": max(0, after_basic - after_meeting),
        "budget": max(0, after_meeting - after_budget),
        "remaining": after_budget,
    }


def _no_result_max_bottleneck(reason_counts: dict, total: int) -> dict:
    labels = {
        "direct": "直飞/基础筛选",
        "meeting": "会议时间窗口",
        "budget": "预算",
        "other": "其他约束",
    }
    meaningful = {
        key: int(value or 0)
        for key, value in (reason_counts or {}).items()
        if key in labels and int(value or 0) > 0
    }
    if not meaningful:
        return {}
    key, count = max(meaningful.items(), key=lambda item: item[1])
    denominator = max(int(total or 0), 1)
    return {
        "key": key,
        "label": labels[key],
        "count": count,
        "ratio": round(count / denominator * 100, 1),
        "pool_scope": "双向候选池",
    }



def _diagnosis_price_text(value) -> str:
    number = _to_float(value)
    if number is None:
        return "¥-"
    return f"¥{number:,.0f}"


def _diagnosis_budget_scope(constraints: dict | None) -> str:
    constraints = constraints or {}
    scope = constraints.get("max_budget_scope") or constraints.get("budget_scope") or "per_person"
    return _budget_visible_scope(normalize_budget_scope(scope), True)


def _diagnosis_passengers(constraints: dict | None) -> dict:
    constraints = constraints or {}
    passengers = _normalize_passengers(constraints.get("passengers"))
    if passengers:
        return passengers
    passenger_count = _to_non_negative_int(constraints.get("passenger_count"), 1)
    return {"adult": max(1, passenger_count), "child": 0, "elderly": 0, "infant": 0}


def _budget_candidate_value(raw: dict, key: str):
    value = raw.get(key)
    if value is not None:
        return value
    nested_key = "outbound" if key.startswith("outbound") else "return"
    nested = raw.get(nested_key) or raw.get(f"{nested_key}_flight") or {}
    if isinstance(nested, dict):
        if key.endswith("price"):
            return nested.get("price")
        if key.endswith("flight"):
            return nested.get("flight_no") or nested.get("flight_combo")
    return None


def _budget_excluded_candidate_rows(stage_counts: dict | None, constraints: dict | None) -> list[dict]:
    stage_counts = stage_counts or {}
    constraints = constraints or {}
    raw_candidates = stage_counts.get("budget_excluded_candidates") or []
    if not raw_candidates:
        return []
    passengers = _diagnosis_passengers(constraints)
    route_type = constraints.get("route_type") or ""
    compare_scope = _diagnosis_budget_scope(constraints)
    label = caliber_label(compare_scope, passengers, route_type)
    max_budget = _to_float(constraints.get("max_budget") or constraints.get("budget") or constraints.get("price_ceiling"))
    budget_limit = None
    if max_budget is not None:
        budget_pp = budget_to_pp(max_budget, passengers, scope=compare_scope, route_type=route_type, round_trip=True)
        budget_limit = price_in_scope(budget_pp, passengers, scope=compare_scope, route_type=route_type, round_trip=True)
    target_value = _to_float(constraints.get("target_price") or constraints.get("ideal_price"))
    rows = []
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        outbound_price = _to_float(_budget_candidate_value(raw, "outbound_price"))
        return_price = _to_float(_budget_candidate_value(raw, "return_price"))
        if outbound_price is None or return_price is None:
            direct_price = _to_float(raw.get("budget_compare_price") or raw.get("price"))
            if direct_price is None:
                continue
            compare_price = direct_price
            single_adult_roundtrip = _to_float(raw.get("single_adult_roundtrip"))
        else:
            single_adult_roundtrip = itinerary_price_pp(outbound_price, return_per_person_oneway=return_price)
            compare_price = price_in_scope(
                outbound_price,
                passengers,
                scope=compare_scope,
                route_type=route_type,
                round_trip=True,
                return_per_person_oneway=return_price,
            )
        outbound_no = str(_budget_candidate_value(raw, "outbound_flight") or "去程待确认")
        return_no = str(_budget_candidate_value(raw, "return_flight") or "返程待确认")
        overage = compare_price - budget_limit if budget_limit is not None else None
        row = {
            "outbound_flight": outbound_no,
            "return_flight": return_no,
            "price": compare_price,
            "price_scope": compare_scope,
            "price_scope_label": label,
            "single_adult_roundtrip": single_adult_roundtrip,
            "max_budget": budget_limit,
            "max_budget_scope": compare_scope,
            "max_budget_label": label,
            "over_budget": bool(budget_limit is not None and compare_price > budget_limit),
            "budget_overage": overage,
            "source": "budget_excluded_candidates",
        }
        rows.append(row)
        print(
            "[预算排除诊断] "
            f"候选={outbound_no}+{return_no} "
            f"预算同口径价={_diagnosis_price_text(compare_price)}({label}) "
            f"预算={_diagnosis_price_text(budget_limit)}({label}) "
            f"超出={_diagnosis_price_text(overage) if overage is not None else '未知'} "
            f"target_price字段={_diagnosis_price_text(target_value) if target_value is not None else '未填'}"
        )
    rows.sort(key=lambda item: item["price"])
    if rows:
        print(
            "[预算排除诊断] "
            f"min={_diagnosis_price_text(rows[0]['price'])}({rows[0]['price_scope_label']}), "
            f"它就是文案最低候选=True, "
            f"文案¥1,200来源字段=target_price({_diagnosis_price_text(target_value) if target_value is not None else '未填'})"
        )
    return rows

def diagnose_no_result(counts: dict, constraints: dict | None = None) -> str:
    """Explain why collected candidates produced no primary recommendation."""
    constraints = constraints or {}
    total = int(counts.get("total_candidates") or 0)
    valid = int(counts.get("valid_price_count") or 0)
    if total and valid == 0:
        return "采集到航班但暂无有效报价,可能数据源未返回价格,建议稍后重试"

    reason_counts = counts.get("reason_counts") or {}
    max_budget = _to_float(constraints.get("max_budget") or constraints.get("budget"))
    remaining = counts.get("after_budget")
    if remaining is None:
        remaining = max(0, valid - sum(int(reason_counts.get(key) or 0) for key in ("direct", "meeting", "budget", "other")))
    remaining = max(0, int(remaining or 0))

    stages = [f"采集到{total}个航班"]
    if reason_counts.get("direct"):
        stages.append(f"直飞/基础筛选排除{int(reason_counts['direct'])}个")
    if reason_counts.get("meeting"):
        stages.append(f"会议时间窗口排除{int(reason_counts['meeting'])}个")
    if reason_counts.get("budget"):
        if max_budget:
            stages.append(f"预算¥{max_budget:,.0f}排除{int(reason_counts['budget'])}个")
        else:
            stages.append(f"预算排除{int(reason_counts['budget'])}个")
    if reason_counts.get("other"):
        stages.append(f"其他约束排除{int(reason_counts['other'])}个")
    stages.append(f"剩余{remaining}个完全匹配")

    max_bottleneck = counts.get("max_bottleneck") or _no_result_max_bottleneck(reason_counts, valid or total)
    suffix = ""
    if max_bottleneck:
        suffix = (
            f"最大卡点:{max_bottleneck['label']}排除最多"
            f"({max_bottleneck['count']}个,占比{max_bottleneck['ratio']}%)。"
        )
    return " → ".join(stages) + ("。" + suffix if suffix else "。")


def _no_result_item_flight(item: dict) -> dict:
    if not isinstance(item, dict):
        return {}
    flight = item.get("flight")
    return flight if isinstance(flight, dict) else item


def _no_result_flight_identity(item: dict) -> tuple:
    flight = _no_result_item_flight(item)
    combo = normalize_combo(flight.get("flight_combo") or flight.get("flight_no") or "")
    return (
        combo,
        str(flight.get("departure_airport") or flight.get("origin") or "").strip().upper(),
        str(flight.get("arrival_airport") or flight.get("destination") or "").strip().upper(),
        str(flight.get("departure_time") or flight.get("dep_time") or "").strip(),
        str(flight.get("arrival_time") or flight.get("arr_time") or "").strip(),
    )


def _no_result_exclusion_record(item: dict) -> dict:
    flight = _no_result_item_flight(item)
    return {
        "reason": str(
            item.get("reason")
            or item.get("exclude_reason")
            or flight.get("exclude_reason")
            or "不满足当前约束"
        ),
        "filter_reason_code": str(
            item.get("filter_reason_code") or flight.get("filter_reason_code") or ""
        ),
        "filter_reason_value": str(
            item.get("filter_reason_value") or flight.get("filter_reason_value") or ""
        ),
    }

def build_no_result_diagnosis(
    all_flights: list[dict] | None,
    excluded_flights: list[dict] | None = None,
    constraints: dict | None = None,
    stage_counts: dict | None = None,
    *,
    fallback_reason: str = "",
) -> dict:
    """Build no-result diagnostics from candidate and exclusion data."""
    flights = [flight for flight in all_flights or [] if isinstance(flight, dict)]
    excluded = [item for item in excluded_flights or [] if isinstance(item, dict)]
    stage_counts = stage_counts or {}
    print(f"[无方案理由诊断] 各阶段计数={stage_counts}")
    valid_price = [flight for flight in flights if _to_float(flight.get("price")) is not None]

    candidate_identities = {
        _no_result_flight_identity(flight)
        for flight in flights
        if _no_result_flight_identity(flight)[0]
    }
    matching_excluded = [
        item
        for item in excluded
        if _no_result_flight_identity(item) in candidate_identities
    ]
    eligible_excluded = matching_excluded if candidate_identities else excluded
    reason_by_identity = {
        _no_result_flight_identity(item): _no_result_exclusion_record(item)
        for item in matching_excluded
    }

    reason_counts = {"direct": 0, "meeting": 0, "budget": 0, "other": 0}
    for item in eligible_excluded:
        reason = _no_result_exclusion_record(item)["reason"]
        reason_counts[_no_result_reason_bucket(reason)] += 1
    stage_drops = _stage_drop_counts({**stage_counts, "valid_price_count": stage_counts.get("valid_price_count") or len(valid_price)})
    for key in ("direct", "meeting", "budget"):
        reason_counts[key] = max(int(reason_counts.get(key) or 0), int(stage_drops.get(key) or 0))

    total_candidates = int(stage_counts.get("total_candidates") or len(flights))
    valid_price_count = int(stage_counts.get("valid_price_count") or len(valid_price))
    if total_candidates < valid_price_count:
        total_candidates = valid_price_count

    capped_reasons = {}
    remaining = valid_price_count
    for key in ("direct", "meeting", "budget", "other"):
        value = min(int(reason_counts.get(key) or 0), max(remaining, 0))
        if value:
            capped_reasons[key] = value
            remaining -= value

    after_basic = stage_counts.get("after_basic_filter")
    after_meeting = stage_counts.get("after_meeting_window")
    same_day_combos = stage_counts.get("same_day_combos")
    if same_day_combos is not None:
        try:
            after_meeting = min(int(after_meeting if after_meeting is not None else valid_price_count), int(same_day_combos))
        except (TypeError, ValueError):
            after_meeting = 0
    after_budget = stage_counts.get("after_budget")
    if after_basic is None and "direct" in capped_reasons:
        after_basic = max(0, valid_price_count - capped_reasons.get("direct", 0))
    if after_meeting is None:
        after_meeting = max(0, int(after_basic if after_basic is not None else valid_price_count) - capped_reasons.get("meeting", 0))
    if after_budget is None:
        after_budget = max(0, int(after_meeting if after_meeting is not None else valid_price_count) - capped_reasons.get("budget", 0))

    counts = {
        "total_candidates": total_candidates,
        "valid_price_count": valid_price_count,
        "after_basic_filter": after_basic,
        "after_meeting_window": after_meeting,
        "after_budget": after_budget,
        "reason_counts": capped_reasons,
    }
    counts["max_bottleneck"] = _no_result_max_bottleneck(capped_reasons, valid_price_count or total_candidates)

    sample = [
        (flight.get("flight_no") or flight.get("flight_combo"), flight.get("price"))
        for flight in flights[:5]
    ]
    print(f"[无方案诊断] 采集去重后总数={counts['total_candidates']}")
    print(f"[无方案诊断] 有效价格航班={counts['valid_price_count']}")
    print(f"[无方案诊断] 常规过滤后(直飞/红眼等)={counts.get('after_basic_filter')}")
    print(f"[无方案诊断] 会议窗口过滤后={counts.get('after_meeting_window')}")
    print(f"[无方案诊断] 预算过滤后={counts.get('after_budget')}")
    print(f"[无方案诊断] 各航班价格样本: {sample}")

    budget_candidate_rows = _budget_excluded_candidate_rows(stage_counts, constraints)
    priced_candidates = sorted(
        (
            (_to_float(flight.get("price")), flight)
            for flight in valid_price
            if _to_float(flight.get("price")) is not None
        ),
        key=lambda row: row[0],
    )
    prices = [price for price, _flight in priced_candidates]
    cheapest_candidate = priced_candidates[0][1] if priced_candidates else None
    summary = {
        "count": len(prices),
        "lowest": prices[0] if prices else None,
        "highest": prices[-1] if prices else None,
        "reason": "",
    }
    if budget_candidate_rows:
        cheapest_budget_candidate = budget_candidate_rows[0]
        summary = {
            "count": len(budget_candidate_rows),
            "lowest": cheapest_budget_candidate.get("price"),
            "highest": budget_candidate_rows[-1].get("price"),
            "reason": '超出预算',
            "price_scope": cheapest_budget_candidate.get("price_scope"),
            "price_scope_label": cheapest_budget_candidate.get("price_scope_label"),
            "max_budget": cheapest_budget_candidate.get("max_budget"),
            "max_budget_scope": cheapest_budget_candidate.get("max_budget_scope"),
            "max_budget_label": cheapest_budget_candidate.get("max_budget_label"),
            "primary_cause": "budget",
            "budget_overage": cheapest_budget_candidate.get("budget_overage"),
            "source": "budget_excluded_candidates",
            "items": budget_candidate_rows,
        }
    elif prices:
        exact_record = reason_by_identity.get(
            _no_result_flight_identity(cheapest_candidate or {})
        )
        if exact_record:
            summary.update(exact_record)
            summary["source"] = "candidate_rejection"
        elif fallback_reason:
            summary["reason"] = str(fallback_reason)
            summary["source"] = "pairing_failure"
        else:
            summary["reason"] = "该候选的逐航班拒因未保留"
            summary["source"] = "reason_unavailable"
        summary["price_scope"] = "per_person_oneway"
    strict_same_day_reason = None
    def _stage_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    if stage_counts.get("same_day_combos") is not None:
        outbound_ok = _stage_int(stage_counts.get("after_meeting_outbound"))
        return_ok = _stage_int(stage_counts.get("return_after_lowerbound"))
        if return_ok is None:
            return_ok = _stage_int(stage_counts.get("after_meeting_return"))
        after_budget_count = _stage_int(stage_counts.get("after_budget"))
        if outbound_ok is not None and outbound_ok <= 0:
            return_clause = (
                f"返程有{return_ok}个可选,非阻塞"
                if (return_ok or 0) > 0
                else "返程暂无符合航班,但主因仍先按去程时间判定"
            )
            strict_same_day_reason = f"本次无方案主因是【去程时间】:去程窗口内0个可选;{return_clause};预算在去程时间之后判断。剩余0个完全匹配。"
            counts["primary_cause"] = "outbound_time"
            outbound_total = _stage_int(stage_counts.get("outbound_collected"))
            if outbound_total is None:
                outbound_total = valid_price_count
            blocked = max(0, outbound_total - max(0, outbound_ok))
            counts["max_bottleneck"] = {
                "key": "outbound_time",
                "label": "去程时间",
                "count": blocked,
                "ratio": round(blocked / max(outbound_total, 1) * 100, 1),
                "pool_scope": "去程池",
            }
        elif return_ok is not None and return_ok <= 0:
            strict_same_day_reason = "本次无方案主因是【返程时间】:去程窗口已有可选,但返程下限后无可选航班。剩余0个完全匹配。"
            counts["primary_cause"] = "return_time"
            return_total = _stage_int(stage_counts.get("return_collected"))
            if return_total is None:
                return_total = valid_price_count
            blocked = max(0, return_total - max(0, return_ok))
            counts["max_bottleneck"] = {
                "key": "return_time",
                "label": "返程时间",
                "count": blocked,
                "ratio": round(blocked / max(return_total, 1) * 100, 1),
                "pool_scope": "返程池",
            }
        elif after_budget_count is not None and after_budget_count <= 0:
            if summary.get("primary_cause") == "budget" and summary.get("lowest") is not None:
                lowest = _to_float(summary.get("lowest"))
                max_budget = _to_float(summary.get("max_budget"))
                scope_label = summary.get("price_scope_label") or "同口径"
                if max_budget is not None and lowest is not None and lowest < max_budget:
                    raise AssertionError(
                        "预算主因口径矛盾: "
                        f"最低候选{_diagnosis_price_text(lowest)}低于预算{_diagnosis_price_text(max_budget)}, "
                        "但仍被判定为超预算"
                    )
                overage = _to_float(summary.get("budget_overage"))
                if overage is None and max_budget is not None and lowest is not None:
                    overage = lowest - max_budget
                strict_same_day_reason = (
                    "本次无方案主因是【预算】:"
                    f"最低候选 {_diagnosis_price_text(lowest)} {scope_label} vs "
                    f"预算 {_diagnosis_price_text(max_budget)} {scope_label},"
                    f"超出 {_diagnosis_price_text(overage)}。剩余0个完全匹配。"
                )
            else:
                max_budget = _to_float((constraints or {}).get("max_budget") or (constraints or {}).get("price_ceiling"))
                budget_text = f"¥{max_budget:,.0f}" if max_budget else "当前预算"
                strict_same_day_reason = f"本次无方案主因是【预算】:去程和返程时间均有可选,但完整往返组合超过{budget_text}。剩余0个完全匹配。"
            counts["primary_cause"] = "budget"
            combo_total = _stage_int(stage_counts.get("same_day_combos")) or 0
            blocked = max(0, combo_total - max(0, after_budget_count))
            counts["max_bottleneck"] = {
                "key": "budget",
                "label": "预算",
                "count": blocked,
                "ratio": round(blocked / max(combo_total, 1) * 100, 1),
                "pool_scope": "完整往返组合池",
            }
    pairing_fallback_reason = None
    if fallback_reason and not matching_excluded and not budget_candidate_rows:
        pairing_count = len(prices) or valid_price_count or total_candidates
        pairing_fallback_reason = (
            f"本次无方案主因是【完整往返】:{fallback_reason};"
            f"去程有{pairing_count}个候选,恢复返程后需重新配对。"
        )
        counts["primary_cause"] = "roundtrip_pairing"
        counts["after_budget"] = 0
        counts["max_bottleneck"] = {
            "key": "roundtrip_pairing",
            "label": "完整往返",
            "count": pairing_count,
            "ratio": 100.0 if pairing_count else 0.0,
            "pool_scope": "去程候选池",
        }
    counts["reason"] = (
        strict_same_day_reason
        or pairing_fallback_reason
        or diagnose_no_result(counts, constraints)
    )
    safe_reason = str(counts["reason"])
    print(f"[无方案理由诊断] diagnose_no_result返回={safe_reason}")
    print(f"[无方案理由诊断] 实际展示的理由文案={safe_reason}")
    return {"counts": counts, "price_summary": summary, "reason": counts["reason"]}


def build_no_result_alternatives(
    candidate_flights: list[dict] | None,
    excluded_flights: list[dict] | None = None,
    limit: int = 3,
    *,
    default_reason: str = "",
    default_reason_code: str = "",
) -> list[dict]:
    """Turn nearest valid-price candidates into fallback alternatives."""
    reason_by_identity = {}
    for item in excluded_flights or []:
        if not isinstance(item, dict):
            continue
        identity = _no_result_flight_identity(item)
        if identity[0]:
            reason_by_identity[identity] = _no_result_exclusion_record(item)
    candidates = []
    for flight in candidate_flights or []:
        if not isinstance(flight, dict):
            continue
        price = _to_float(flight.get("price"))
        if price is None or price <= 0:
            continue
        record = reason_by_identity.get(_no_result_flight_identity(flight)) or {
            "reason": str(default_reason or "不满足当前约束"),
            "filter_reason_code": str(default_reason_code or ""),
            "filter_reason_value": "",
        }
        dep_min = _flight_departure_minutes(flight)
        arr_min = _flight_arrival_minutes(flight)
        time_key = arr_min if arr_min is not None else (dep_min if dep_min is not None else 99999)
        candidates.append((time_key, price, flight, record))
    candidates.sort(key=lambda row: (row[0], row[1]))
    result = []
    labels = ["备选A", "备选B", "备选C"]
    for index, (_, price, flight, record) in enumerate(candidates[:limit]):
        reason = str(record.get("reason") or "不满足当前约束")
        result.append(
            {
                "category": "closest_candidate",
                "title": f"{labels[index]} · 最接近条件",
                "flight": dict(flight),
                "price": price,
                "unmet_reason": reason,
                "filter_reason_code": record.get("filter_reason_code") or "",
                "filter_reason_value": record.get("filter_reason_value") or "",
                "tradeoff": reason,
                "feasibility": reason,
            }
        )
    return result

def determine_push_type(
    current_price,
    target_price=None,
    max_budget=None,
    price_history=None,
    days_to_dept=None,
    last_push_price=None,
    analysis_result=None,
) -> dict:
    """Determine push type using explicit display/transaction/verification price roles."""
    current = _to_float(current_price)
    target = _to_float(target_price)
    max_b = _to_float(max_budget)
    last_price = _to_float(last_push_price)
    analysis_result = analysis_result or {}
    decision_prices = analysis_result.get("decision_prices") or {}
    display_price = _to_float(decision_prices.get("display_price")) or current
    budget_compare_price = _to_float(decision_prices.get("budget_compare_price"))
    reason_price = budget_compare_price if budget_compare_price is not None else display_price
    transaction_price = _to_float(decision_prices.get("transaction_price"))
    verify_price = _to_float(decision_prices.get("verify_price"))
    prices = sorted(_flatten_price_history(price_history))

    percentile = None
    historical_30 = None
    if display_price is not None and prices:
        below = sum(1 for price in prices if price < display_price)
        percentile = round(below / len(prices) * 100)
        index = min(len(prices) - 1, max(0, round((len(prices) - 1) * 0.30)))
        historical_30 = prices[index]

    availability_high = False
    for flight in (analysis_result.get("all_flights") or [])[:3]:
        avail = (flight or {}).get("availability") or {}
        if avail.get("status") == "likely_available":
            availability_high = True
            break

    display_reaches_target = bool(target and display_price is not None and display_price <= target)
    display_reaches_verify = bool(verify_price and display_price is not None and display_price <= verify_price)
    transaction_reaches_verify = bool(
        verify_price and transaction_price is not None and transaction_price <= verify_price
    )
    transaction_over_verify = bool(
        verify_price and transaction_price is not None and transaction_price > verify_price
    )
    round_trip_result = analysis_result.get("round_trip_analysis") or analysis_result
    same_day_conflict = bool(
        isinstance(round_trip_result, dict)
        and round_trip_result.get("same_day_time_conflict")
        and not (round_trip_result.get("top_combinations") or [])
    )

    push_type = "同日更优方案"
    if same_day_conflict:
        push_type = "时间冲突提示"
    elif display_price is None:
        push_type = "价格已失效"
    elif _has_stale_primary_price(analysis_result):
        push_type = "价格已失效"
    elif historical_30 and display_price <= historical_30 and transaction_reaches_verify and availability_high:
        push_type = "异常低价"
    elif display_reaches_verify and transaction_over_verify:
        push_type = "值得验证"
    elif display_reaches_target:
        push_type = "进入低价区间"
    elif _has_cheaper_nearby_date(analysis_result, display_price):
        push_type = "前后日期更便宜"
    elif _has_better_same_day_option(analysis_result):
        push_type = "同日更优方案"
    elif last_price and display_price is not None and display_price < last_price:
        push_type = "价格下降"
    elif _is_price_rise_risk(days_to_dept, analysis_result):
        push_type = "涨价风险"

    reasons = []
    if display_reaches_verify and transaction_over_verify:
        reasons.append("搜索参考价达标，但预估实付价高于验证购买价（你的设置）")
    if target and reason_price is not None:
        if reason_price <= target:
            reasons.append("搜索参考价进入你的理想入手区间（你的设置）")
        else:
            reasons.append(f"搜索参考价距离理想入手价还差¥{reason_price - target:,.0f}（你的设置）")
    if last_price and display_price is not None:
        diff = display_price - last_price
        if diff < 0:
            reasons.append(f"较上次提醒：下降¥{abs(diff):,.0f}（上次同口径提醒）")
        elif diff > 0:
            reasons.append(f"较上次提醒：上涨¥{diff:,.0f}（上次同口径提醒）")
        else:
            reasons.append("与上次提醒价格持平（上次同口径提醒）")
    if percentile is not None:
        history_suffix = _history_reference_suffix(prices)
        if len(prices) < MIN_SAMPLE_FOR_PRICE_SIGNAL:
            reasons.append(_insufficient_history_copy(prices))
        elif percentile <= 0:
            reasons.append(f"当前搜索价低于所有相似采集记录，处于近期低位{history_suffix}")
        elif percentile <= 30:
            reasons.append(f"当前搜索价处于相似历史样本低价区间{history_suffix}")
        elif percentile >= 70:
            reasons.append(f"当前搜索价高于大多数相似历史样本{history_suffix}")
    reasons.extend(_matched_constraint_reasons(analysis_result))
    if _is_price_rise_risk(days_to_dept, analysis_result):
        days_text = f"{days_to_dept}天" if days_to_dept is not None else "临近出发"
        reasons.append(f"距出发{days_text}，低价继续变化的风险上升")

    price_change = None
    if last_price and display_price is not None:
        diff = display_price - last_price
        price_change = {
            "last": last_price,
            "current": display_price,
            "diff": diff,
            "direction": "down" if diff < 0 else "up" if diff > 0 else "flat",
        }

    return {
        "type": push_type,
        "reasons": _dedupe_text(reasons)[:4],
        "price_change": price_change,
        "percentile": percentile,
        "historical_30_price": historical_30,
    }


def build_price_signal(display_price, target_price=None, price_history=None) -> dict:
    """Describe whether the search/display price is attractive, without making purchase claims."""
    display = _to_float(display_price)
    target = _to_float(target_price)
    prices = sorted(_flatten_price_history(price_history))

    percentile = None
    if display is not None and prices:
        below = sum(1 for price in prices if price < display)
        percentile = round(below / len(prices) * 100)

    if display is None:
        return {
            "label": "未知",
            "summary": "搜索参考价暂未确认",
            "percentile": percentile,
            "sample_n": len(prices),
        }

    if percentile is not None and percentile <= 30:
        if len(prices) < MIN_SAMPLE_FOR_PRICE_SIGNAL:
            label = "待积累"
            summary = _insufficient_history_copy(prices)
        else:
            label = "强"
            summary = f"搜索参考价处于近期低位{_history_reference_suffix(prices)}"
    elif target is not None and display <= target:
        label = "强"
        summary = "搜索参考价已进入理想入手区间（你的设置）"
    elif target is not None and display <= target * 1.05:
        label = "中高"
        summary = "搜索参考价接近理想入手价（你的设置）"
    elif target is not None:
        label = "中"
        summary = "搜索参考价仍高于理想入手价（你的设置）"
    else:
        label = "中"
        summary = "搜索参考价可作为低价线索，需结合历史数据判断"

    return {
        "label": label,
        "summary": summary,
        "percentile": percentile,
        "sample_n": len(prices),
    }


def evaluate_purchase_budget(
    unit_roundtrip,
    target_price=None,
    max_budget=None,
    *,
    price_scope=None,
    budget_scope=None,
) -> dict:
    """在调用方已选定的同一口径内判断完整行程价格。"""
    if price_scope is not None or budget_scope is not None:
        assert_same_caliber(
            price_scope or budget_scope,
            budget_scope or price_scope,
        )

    current = _to_float(unit_roundtrip)
    target = _to_float(target_price)
    maximum = _to_float(max_budget)
    if current is None:
        status = "no_price"
    elif maximum is not None and current > maximum:
        status = "over_budget"
    elif target is not None and current <= target:
        status = "at_or_below_target"
    elif maximum is not None and current <= maximum:
        status = "within_budget"
    elif target is not None and current > target:
        status = "above_target"
    else:
        status = "unbounded"

    return {
        "status": status,
        "price": current,
        "target_price": target,
        "max_budget": maximum,
        "is_over_budget": status == "over_budget",
        "price_scope": price_scope,
        "budget_scope": budget_scope,
    }


def build_execution_advice(
    display_price,
    transaction_price=None,
    verify_price=None,
    target_price=None,
    max_price=None,
    push_type=None,
    budget_decision=None,
    price_scope=None,
    budget_scope=None,
) -> dict:
    """Describe whether the current plan is executable, using estimated/checkout price concepts."""
    display = _to_float(display_price)
    transaction = _to_float(transaction_price)
    verify = _to_float(verify_price)
    target = _to_float(target_price)
    max_p = _to_float(max_price)
    push_type_text = str(push_type or "")
    budget_decision = budget_decision or evaluate_purchase_budget(
        display,
        target,
        max_p,
        price_scope=price_scope,
        budget_scope=budget_scope,
    )

    if budget_decision.get("status") == "over_budget":
        return {
            "label": "继续监控",
            "conclusion": f"当前搜索价¥{display:,.0f}已超过你的最高可接受价¥{max_p:,.0f}，不满足购买条件，建议继续监控",
            "summary": "搜索参考价已超过最高可接受价，不建议按当前价买入（你的设置）",
            "condition": f"支付页最终价≤¥{max_p:,.0f}，且含托运行李",
        }

    if "涨价风险" in push_type_text and display is not None:
        if target is not None and display <= target:
            return {
                "label": "可验证后决定",
                "conclusion": "价格仍可接受，但呈上涨趋势，可验证后决定",
                "summary": "搜索参考价仍在理想价内（你的设置），但近期呈上涨趋势",
                "condition": f"支付页最终价≤¥{verify:,.0f}，且含托运行李" if verify else "以支付页最终价和票规为准",
            }
        return {
            "label": "继续监控",
            "conclusion": "价格已高于你的理想价且在上涨，建议继续观察，暂不建议买入",
            "summary": "涨价风险存在，但当前搜索参考价已高于理想入手价（你的设置）",
            "condition": f"支付页最终价≤¥{verify:,.0f}，且含托运行李" if verify else "等待价格回落到理想区间",
        }

    if transaction is not None and verify is not None and transaction <= verify:
        return {
            "label": "可验证购买",
            "conclusion": "可以购买前验证",
            "summary": "预估实付价不高于本次验证价（你的设置），进入渠道确认最终价和票规后可购买",
            "condition": f"支付页最终价≤¥{verify:,.0f}，且含托运行李",
        }

    if display is not None and verify is not None and display <= verify and transaction is not None and transaction > verify:
        return {
            "label": "验证后再买",
            "conclusion": "值得验证，不建议直接下单",
            "summary": "预估实付价高于本次验证价（你的设置），需确认最终价和行李",
            "condition": f"支付页最终价≤¥{verify:,.0f}，且含托运行李",
        }

    if target is not None and display is not None and display > target:
        return {
            "label": "继续观察",
            "conclusion": "继续观察",
            "summary": "搜索参考价仍高于理想入手价（你的设置）",
            "condition": f"理想入手价≤¥{target:,.0f}",
        }

    return {
        "label": "待确认",
        "conclusion": "可以观察",
        "summary": "执行条件需要以支付页最终价、行李和票规为准",
        "condition": "以支付页最终价和票规为准",
    }


def classify_plan_tier(
    is_direct=True,
    execution_grade=None,
    cheaper_than_primary=False,
    has_transfer=False,
    split_ticket=False,
) -> dict:
    """Classify a plan for user-facing A/B presentation."""
    grade = str(execution_grade or "").upper()
    high_risk = grade in {"C", "D"} or bool(has_transfer) or bool(split_ticket)

    if cheaper_than_primary and high_risk:
        reasons = []
        if has_transfer:
            reasons.append("有中转")
        if split_ticket:
            reasons.append("两个单程拼接")
        if grade in {"C", "D"}:
            reasons.append("执行风险较高")
        reason = "价格更低，但" + "、".join(reasons or ["执行风险更高"])
        condition = "如果你能接受" + "、".join(reasons or ["额外执行风险"]) + "，可验证该方案"
        return {"tier": "低价备选", "reason": reason, "suitable_condition": condition}

    if is_direct and grade not in {"C", "D"}:
        return {
            "tier": "首选推荐",
            "reason": "直飞省心，适合家庭/老人同行",
            "suitable_condition": "适合更重视直飞、省心和执行稳定性的行程",
        }

    if has_transfer:
        return {
            "tier": "稳妥备选",
            "reason": "综合得分较高，但含中转，需确认联程和行李",
            "suitable_condition": "适合能接受中转并愿意核对票规的行程",
        }

    return {
        "tier": "首选推荐",
        "reason": "综合得分最高，建议优先验证",
        "suitable_condition": "适合优先验证该方案的价格和票规",
    }


def _dedupe_text(items: list[str]) -> list[str]:
    result = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _has_stale_primary_price(analysis_result: dict) -> bool:
    candidates = []
    if analysis_result.get("all_flights"):
        candidates.extend(analysis_result.get("all_flights") or [])
    round_trip = analysis_result.get("round_trip_analysis") or {}
    for combo in round_trip.get("combinations") or []:
        candidates.extend([combo.get("outbound") or {}, combo.get("return") or {}])
    for flight in candidates[:3]:
        age = ((flight or {}).get("availability") or {}).get("age_minutes")
        try:
            if age is not None and int(age) > 120:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _has_cheaper_nearby_date(analysis_result: dict, current: float | None) -> bool:
    calendar = analysis_result.get("price_calendar") or {}
    calendar_rows = calendar.get("rows") or [] if isinstance(calendar, dict) else []
    if calendar_rows:
        selected = next(
            (
                row
                for row in calendar_rows
                if isinstance(row, dict) and row.get("selected")
            ),
            None,
        )
        selected_price = _to_float((selected or {}).get("min_price"))
        if selected_price is not None:
            return any(
                (_to_float(row.get("min_price")) or float("inf")) < selected_price
                for row in calendar_rows
                if isinstance(row, dict) and row is not selected
            )

    if current is None:
        return False
    is_roundtrip = bool(
        analysis_result.get("round_trip")
        or analysis_result.get("round_trip_analysis")
    )
    candidates = []
    nearby = analysis_result.get("nearby_dates") or analysis_result.get("nearby_date_prices") or {}
    if isinstance(nearby, dict):
        candidates = nearby.values()
    elif isinstance(nearby, list):
        candidates = nearby
    for item in candidates:
        if isinstance(item, dict):
            item_scope = str(item.get("scope") or "").strip().lower()
            has_roundtrip_price = item.get("roundtrip_total") not in (None, "", 0)
            if is_roundtrip and not (has_roundtrip_price or item_scope == "roundtrip"):
                continue
            price = _to_float(
                item.get("roundtrip_total")
                or item.get("min_price")
                or item.get("price")
            )
        else:
            if is_roundtrip:
                continue
            price = _to_float(item)
        if price and price < current:
            return True
    return False


def _has_better_same_day_option(analysis_result: dict) -> bool:
    flights = analysis_result.get("all_flights") or []
    if len(flights) < 2:
        return False
    valid = [flight for flight in flights if _to_float(flight.get("price"))]
    if len(valid) < 2:
        return False
    sorted_by_price = sorted(valid, key=lambda f: _to_float(f.get("price")) or 999999)
    best = sorted_by_price[0]
    second = sorted_by_price[1]
    best_grade = best.get("execution_grade")
    second_grade = second.get("execution_grade")
    return best_grade in {"A", "B"} and second_grade not in {"A", "B"}


def _is_price_rise_risk(days_to_dept, analysis_result: dict) -> bool:
    try:
        days = int(days_to_dept) if days_to_dept is not None else int(analysis_result.get("days_to_dept", 999))
    except (TypeError, ValueError):
        days = 999
    if days <= 14:
        return True
    risk = analysis_result.get("waiting_risk") or {}
    up_prob = _to_float(risk.get("up_probability"))
    down_prob = _to_float(risk.get("down_probability"))
    return bool(up_prob and down_prob is not None and up_prob > down_prob)


def _matched_constraint_reasons(analysis_result: dict) -> list[str]:
    flights = analysis_result.get("all_flights") or []
    if not flights:
        round_trip = analysis_result.get("round_trip_analysis") or {}
        combos = round_trip.get("combinations") or []
        if combos:
            flights = [combos[0].get("outbound") or {}, combos[0].get("return") or {}]
    reasons = []
    first = flights[0] if flights else {}
    if first.get("stops", 0) == 0:
        reasons.append("符合你设置的直飞条件")
    fare = first.get("fare_verification") or {}
    matches = " ".join(fare.get("matches") or [])
    if "托运" in matches or "行李" in matches:
        reasons.append("符合你设置的托运行李要求")
    return reasons


FILTER_DETAIL_TOP_N = 5
FILTER_DETAIL_MAX_PER_ROUND = 10
_filter_detail_round_id: str | None = None
_filter_detail_count = 0
_filter_detail_seen: set[tuple] = set()


def _filter_detail_number(value) -> str:
    number = _to_float(value)
    if number is None:
        return str(value if value not in (None, "") else "unknown")
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _filter_detail_identity(flight: dict) -> tuple:
    return (
        str(flight.get("flight_combo") or flight.get("flight_no") or "unknown"),
        str(flight.get("cabin_class") or flight.get("cabin") or ""),
        str(flight.get("departure_time") or flight.get("dep_time") or ""),
        str(flight.get("arrival_time") or flight.get("arr_time") or ""),
        _to_float(flight.get("price")),
    )


def _filter_detail_times(flight: dict) -> str:
    segments = [item for item in (flight.get("segments") or []) if isinstance(item, dict)]
    first = segments[0] if segments else {}
    last = segments[-1] if segments else {}
    departure = (
        flight.get("departure_time")
        or flight.get("dep_time")
        or first.get("departure_time")
        or first.get("dep_time")
        or "unknown"
    )
    arrival = (
        flight.get("arrival_time")
        or flight.get("arr_time")
        or last.get("arrival_time")
        or last.get("arr_time")
        or "unknown"
    )
    return f"departure={departure},arrival={arrival}"


def _filter_detail_airlines(flight: dict) -> str:
    airlines = flight.get("airlines") or []
    if isinstance(airlines, str):
        airlines = [airlines]
    if not airlines:
        airlines = [
            item.get("airline")
            for item in (flight.get("segments") or [])
            if isinstance(item, dict) and item.get("airline")
        ]
    return str(flight.get("airline_summary") or "/".join(airlines) or "unknown")


def _filter_constraint_detail(flight: dict, preferences: dict) -> tuple[str, str]:
    reason = str(flight.get("exclude_reason") or "不符合当前筛选条件")
    if reason == "用户设置必须直飞":
        return "direct_only", f"stops={_stops_count(flight)}"
    if reason == "超过最高可接受价格":
        return (
            "max_budget",
            (
                f"price={_filter_detail_number(flight.get('price'))},"
                f"max_budget={_filter_detail_number(preferences.get('max_budget') or preferences.get('budget'))}"
            ),
        )
    if "行李" in reason or "托运" in reason:
        baggage = ((flight.get("fare_rules") or {}).get("baggage") or {})
        return (
            "need_baggage",
            f"included={baggage.get('included')},checked_kg={baggage.get('checked_kg')}",
        )
    if "起飞时段" in reason:
        return "departure_slots", _filter_detail_times(flight)
    if "起飞时间" in reason:
        return "departure_time_policy", _filter_detail_times(flight)
    if "到达时段" in reason:
        return "arrival_slots", _filter_detail_times(flight)
    if "到达时间" in reason:
        return "arrival_time_policy", _filter_detail_times(flight)
    if "时间不符合" in reason:
        return "time_preference", _filter_detail_times(flight)
    if reason == "用户不接受廉航":
        return "airline_policy", f"airlines={_filter_detail_airlines(flight)}"
    if reason == "命中用户排除航司":
        return "exclude_airlines", f"airlines={_filter_detail_airlines(flight)}"
    if reason == "命中廉价航空排除策略":
        return "lcc_excluded", lcc_filter_value(flight.get("lcc_summary"))
    if reason == "并非全部航段均为廉价航空":
        summary = flight.get("lcc_summary") or {}
        return (
            "lcc_only_unmet",
            (
                f"{lcc_filter_value(summary)},"
                f"all_lcc={bool(summary.get('all_lcc'))},"
                f"segments={len(summary.get('segment_results') or [])}"
            ),
        )
    if "最长可接受总行程时间" in reason:
        return "max_total_duration", f"total_duration_min={flight.get('total_duration_min')}"
    if "过夜中转" in reason:
        return "allow_overnight_transfer", f"max_layover_min={_max_layover_minutes(flight)}"
    if "非联程中转" in reason:
        return "allow_self_transfer", "likely_self_transfer=True"
    if "中转次数" in reason:
        return "max_transfers", f"stops={_stops_count(flight)}"
    if "尽量选择直飞" in reason:
        return "allow_transfer", f"stops={_stops_count(flight)}"
    if "换机场中转" in reason:
        return "allow_airport_change", "airport_change=True"
    if "中转安全时间" in reason or "中转时间低于" in reason:
        return "min_connection_min", f"min_layover_min={_min_layover_minutes(flight)}"
    if "红眼" in reason or "凌晨到达" in reason or "过早航班" in reason:
        return "red_eye", _filter_detail_times(flight)
    return reason, (
        f"price={_filter_detail_number(flight.get('price'))},"
        f"stops={_stops_count(flight)},{_filter_detail_times(flight)}"
    )


def _attach_filter_reason_details(
    flights: list[dict],
    preferences: dict | None,
) -> list[dict]:
    """把过滤日志使用的精确拒因同步存入排除方案数据。"""
    for flight in flights or []:
        if not isinstance(flight, dict):
            continue
        constraint, value = _filter_constraint_detail(flight, preferences or {})
        flight["filter_reason_code"] = constraint
        flight["filter_reason_value"] = value
    return flights


def _log_low_price_filter_rejections(
    pool: list[dict],
    excluded: list[dict],
    preferences: dict | None,
    *,
    round_id: str | None = None,
) -> None:
    """分别记录直飞与中转低价前五中的被拒航班，每轮最多十条。"""
    global _filter_detail_round_id, _filter_detail_count, _filter_detail_seen
    if round_id is None:
        round_id, _ = get_current_round()
    round_key = str(round_id or "standalone")
    if _filter_detail_round_id != round_key:
        _filter_detail_round_id = round_key
        _filter_detail_count = 0
        _filter_detail_seen = set()
    if _filter_detail_count >= FILTER_DETAIL_MAX_PER_ROUND:
        return

    priced = [
        flight
        for flight in pool
        if (_to_float(flight.get("price")) or 0) > 0
    ]
    price_key = lambda flight: _to_float(flight.get("price")) or float("inf")
    ranked_direct = sorted(
        [flight for flight in priced if _stops_count(flight) == 0],
        key=price_key,
    )[:FILTER_DETAIL_TOP_N]
    ranked_transfer = sorted(
        [flight for flight in priced if _stops_count(flight) != 0],
        key=price_key,
    )[:FILTER_DETAIL_TOP_N]
    ranked = ranked_direct + ranked_transfer
    excluded_by_identity = {
        _filter_detail_identity(flight): flight
        for flight in excluded
        if isinstance(flight, dict)
    }
    for flight in ranked:
        rejected = excluded_by_identity.get(_filter_detail_identity(flight))
        if not rejected:
            continue
        constraint = rejected.get("filter_reason_code")
        value = rejected.get("filter_reason_value")
        if not constraint:
            constraint, value = _filter_constraint_detail(rejected, preferences or {})
        combo = str(rejected.get("flight_combo") or rejected.get("flight_no") or "unknown")
        seen_key = (combo, constraint, value)
        if seen_key in _filter_detail_seen:
            continue
        safe_log(f"[过滤明细] combo={combo} 拒因={constraint} 值={value}")
        _filter_detail_seen.add(seen_key)
        _filter_detail_count += 1
        if _filter_detail_count >= FILTER_DETAIL_MAX_PER_ROUND:
            break


def _apply_user_preferences(
    flights: list[dict], preferences: dict | None
) -> tuple[list[dict], list[dict], dict]:
    preferences = preferences or {}
    direct_only = preferences.get("direct_only", "flexible")
    transfer_policy = preferences.get("transfer_policy", "reasonable")
    direct_required = direct_only in {"must", "direct_only", "must_direct"} or transfer_policy in {
        "must",
        "direct_only",
        "must_direct",
    }
    red_eye = preferences.get("red_eye", "reject")
    departure_time_policy = preferences.get("departure_time_policy", "any")
    arrival_time_policy = preferences.get("arrival_time_policy", "any")
    time_preference_mode = (
        preferences.get("time_preference_mode")
        or preferences.get("time_preference")
        or "unlimited"
    )
    time_preference_mode = "unlimited" if time_preference_mode == "any" else time_preference_mode
    use_legacy_time_filters = time_preference_mode not in {
        "unlimited",
        "daytime",
        "no_redeye",
        "custom",
    }
    direction = preferences.get("direction", "outbound")
    same_day_meeting_time_override = bool(
        preferences.get("same_day_round_trip")
        and preferences.get("business_start")
        and preferences.get("business_end")
    )
    roundtrip_purchase_context = bool(
        preferences.get("round_trip") or preferences.get("same_day_round_trip")
    )
    if same_day_meeting_time_override:
        use_legacy_time_filters = False
    preferred_departure_slots, preferred_arrival_slots = _direction_time_slots(
        preferences, direction
    )
    need_baggage = preferences.get("need_baggage", "unknown")
    refund_flexibility = preferences.get("refund_flexibility", "unknown")
    companions = preferences.get("companions") or preferences.get("travelers") or "solo"
    travel_scenarios = _normalize_travel_scenarios(
        preferences.get("travel_scenarios") or preferences.get("travel_scenario")
    )
    travel_scenario_set = set(travel_scenarios)
    travel_profile = build_travel_profile(preferences)
    companion_constraints = preferences.get("companion_constraints") or []
    if isinstance(companion_constraints, str):
        companion_constraints = [
            item.strip() for item in companion_constraints.split(",") if item.strip()
        ]
    companion_constraints = set(companion_constraints)
    passenger_profile = travel_profile.get("passenger_profile") or build_passenger_profile(
        travel_profile.get("passengers"),
        {"companion_constraints": list(companion_constraints)},
    )
    passenger_rules = travel_profile.get("passenger_rules") or build_passenger_friendly_rules(
        passenger_profile,
        route_type=preferences.get("route_type"),
    )
    passenger_friendly_mode = bool(passenger_profile.get("has_child") or passenger_profile.get("has_elderly"))
    if passenger_rules.get("prefer_direct"):
        preferences.setdefault("direct_preferred", True)
    if passenger_rules.get("require_baggage_clarity"):
        preferences.setdefault("need_baggage", "required")
        need_baggage = "required" if need_baggage == "unknown" else need_baggage
    if passenger_profile.get("needs_refund_flexibility") and refund_flexibility == "unknown":
        refund_flexibility = "preferred"
    direct_preferred = bool(preferences.get("direct_preferred")) or "direct_preferred" in companion_constraints
    avoid_long_layover = bool(preferences.get("avoid_long_layover")) or "avoid_long_layover" in companion_constraints
    no_late_arrival = bool(preferences.get("no_late_arrival")) or "daytime_arrival" in companion_constraints
    prefer_daytime_arrival = bool(preferences.get("prefer_daytime_arrival")) or "daytime_arrival" in companion_constraints or bool(passenger_rules.get("prefer_daytime"))
    solo_travel = bool(preferences.get("solo_travel"))
    price_sensitivity = preferences.get("price_sensitivity", "low")
    trip_rigidity = preferences.get("trip_rigidity", "confirmed")
    airline_policy = preferences.get("airline_policy", "any")
    exclude_airlines = preferences.get("exclude_airlines") or []
    max_budget = _to_float(preferences.get("max_budget"))
    budget = max_budget if max_budget is not None else _to_float(preferences.get("budget"))
    target_price = _to_float(preferences.get("target_price"))
    price_tolerance = _to_float(preferences.get("price_tolerance"))
    if price_tolerance is None:
        price_tolerance = 100
    max_extra_duration_hours = _to_float(preferences.get("max_extra_duration_hours"))
    max_total_duration_hours = _to_float(preferences.get("max_total_duration_hours"))
    allow_overnight_transfer = bool(preferences.get("allow_overnight_transfer"))
    if "allow_overnight_transfer" not in preferences:
        allow_overnight_transfer = bool(preferences.get("accept_overnight_transfer"))
    allow_self_transfer = bool(preferences.get("allow_self_transfer"))
    if "allow_self_transfer" not in preferences:
        allow_self_transfer = bool(preferences.get("accept_self_transfer"))
    if passenger_rules.get("allow_self_transfer") is False:
        allow_self_transfer = False
    if passenger_rules.get("allow_overnight_transfer") is False:
        allow_overnight_transfer = False
    if transfer_policy == "reasonable" and max_extra_duration_hours is None and max_total_duration_hours is None:
        max_extra_duration_hours = 6

    direct_flights = [flight for flight in flights if _stops_count(flight) == 0]
    non_red_eye_flights = [flight for flight in flights if not _is_red_eye(flight)]
    cheapest_direct = _cheapest_price(direct_flights)
    cheapest_non_red_eye = _cheapest_price(non_red_eye_flights)
    direct_durations = [
        int(flight.get("total_duration_min") or 0)
        for flight in direct_flights
        if int(flight.get("total_duration_min") or 0) > 0
    ]
    all_durations = [
        int(flight.get("total_duration_min") or 0)
        for flight in flights
        if int(flight.get("total_duration_min") or 0) > 0
    ]
    duration_baseline = min(direct_durations or all_durations) if all_durations else None
    duration_limit_minutes = None
    if max_total_duration_hours:
        duration_limit_minutes = int(max_total_duration_hours * 60)
    elif max_extra_duration_hours is not None and duration_baseline:
        duration_limit_minutes = int(duration_baseline + max_extra_duration_hours * 60)

    kept = []
    excluded = []
    direct_reference_candidates = []
    for flight in flights:
        notes = list(flight.get("preference_notes") or [])
        penalties = list(flight.get("preference_penalties") or [])
        penalty = 0
        stops = _stops_count(flight)
        price = _to_float(flight.get("price")) or 0

        if same_day_meeting_time_override:
            time_ok, time_note = True, "当天往返会议模式：以会议时间窗口为最高优先，清晨早班/晚班返程可选"
        else:
            time_ok, time_note = match_time_preference(flight, preferences)
        if not time_ok:
            excluded.append({**flight, "exclude_reason": time_note or "时间不符合订阅偏好"})
            continue
        if time_note == "非白天，排序降权":
            penalty += 1
            penalties.append(time_note)
        elif time_note:
            notes.append(time_note)

        if use_legacy_time_filters and preferred_departure_slots and not _matches_time_slots(
            _first_departure_hour(flight), preferred_departure_slots
        ):
            excluded.append({**flight, "exclude_reason": "起飞时段不符合订阅偏好"})
            continue
        if use_legacy_time_filters and not preferred_departure_slots and not _matches_departure_policy(flight, departure_time_policy):
            excluded.append({**flight, "exclude_reason": "起飞时间不符合订阅偏好"})
            continue
        if use_legacy_time_filters and preferred_arrival_slots and not _matches_time_slots(
            _last_arrival_hour(flight), preferred_arrival_slots
        ):
            excluded.append({**flight, "exclude_reason": "到达时段不符合订阅偏好"})
            continue
        if use_legacy_time_filters and not preferred_arrival_slots and not _matches_arrival_policy(flight, arrival_time_policy):
            excluded.append({**flight, "exclude_reason": "到达时间不符合订阅偏好"})
            continue
        if airline_policy == "no_lcc" and _contains_any_airline(flight, LCC_AIRLINES):
            excluded.append({**flight, "exclude_reason": "用户不接受廉航"})
            continue
        if exclude_airlines and _contains_any_airline(flight, exclude_airlines):
            excluded.append({**flight, "exclude_reason": "命中用户排除航司"})
            continue
        if airline_policy == "prefer_full_service":
            if _contains_any_airline(flight, FULL_SERVICE_AIRLINES):
                flight["score_multiplier"] = max(
                    float(flight.get("score_multiplier") or 1), 1.15
                )
                notes.append("偏好全服务航司")
            elif _contains_any_airline(flight, LCC_AIRLINES):
                penalty += 2
                penalties.append("非全服务航司")

        if not roundtrip_purchase_context:
            if max_budget and max_budget > 0 and price > max_budget:
                excluded.append({**flight, "exclude_reason": "\u8d85\u8fc7\u6700\u9ad8\u53ef\u63a5\u53d7\u4ef7\u683c"})
                continue
            if budget and budget > 0:
                notes.append("\u6700\u9ad8\u53ef\u63a5\u53d7\u4ef7\u683c\u5185")
            if target_price and target_price > 0:
                if price <= target_price:
                    notes.append("\u4f4e\u4e8e\u7406\u60f3\u5165\u624b\u4ef7")
                elif price <= target_price + price_tolerance:
                    notes.append("在理想价浮动范围内")
                else:
                    penalties.append(f"\u8ddd\u79bb\u7406\u60f3\u5165\u624b\u4ef7\u00a5{price - target_price:,.0f}")

        if direct_required and stops > 0:
            excluded_flight = {**flight, "exclude_reason": "用户设置必须直飞"}
            excluded.append(excluded_flight)
            direct_reference_candidates.append(flight)
            continue
        if direct_only in {"flexible", "cheap_ok"} and stops > 0:
            penalty += 2 if direct_only == "flexible" else 1
            if cheapest_direct and price < cheapest_direct:
                notes.append(f"中转但便宜¥{cheapest_direct - price:,.0f}")
            else:
                penalties.append("包含中转")

        if transfer_policy in {"reasonable", "short_ok"} and stops > 0:
            total_minutes = int(flight.get("total_duration_min") or 0)
            if duration_limit_minutes and total_minutes > duration_limit_minutes:
                excluded.append(
                    {
                        **flight,
                        "exclude_reason": "超过合理中转最长可接受总行程时间",
                    }
                )
                continue
            if total_minutes > 24 * 60:
                penalty += 2
                penalties.append("中转总时长偏长")
            else:
                notes.append("合理中转可接受")
        elif transfer_policy in {"price_first", "cheap_ok"} and stops > 0:
            notes.append("价格优先，保留中转方案")

        if stops > 0 and not allow_overnight_transfer and _max_layover_minutes(flight) > 480:
            excluded.append({**flight, "exclude_reason": "系统默认不推荐过夜中转"})
            continue
        if stops > 0 and not allow_self_transfer and _is_likely_self_transfer(flight):
            excluded.append({**flight, "exclude_reason": "系统默认不推荐疑似非联程中转"})
            continue
        if passenger_friendly_mode:
            max_transfers = passenger_rules.get("max_transfers")
            if max_transfers is not None and stops > int(max_transfers):
                excluded.append({**flight, "exclude_reason": "老人/儿童同行：中转次数超过友好规则"})
                continue
            if passenger_rules.get("allow_transfer") is False and stops > 0:
                excluded.append({**flight, "exclude_reason": "老人/儿童同行且存在行动敏感：尽量选择直飞"})
                continue
            if passenger_rules.get("allow_airport_change") is False and stops > 0 and _has_airport_change_transfer(flight):
                excluded.append({**flight, "exclude_reason": "老人/儿童同行：默认不推荐换机场中转"})
                continue
            min_connection = int(passenger_rules.get("min_connection_min") or 0)
            if stops > 0 and min_connection and (_min_layover_minutes(flight) or 9999) < min_connection:
                excluded.append({**flight, "exclude_reason": f"老人/儿童同行：中转安全时间低于{min_connection}分钟"})
                continue
            if not same_day_meeting_time_override and passenger_rules.get("allow_red_eye") is False and _is_red_eye(flight):
                excluded.append({**flight, "exclude_reason": "老人/儿童同行：默认不推荐红眼或凌晨到达"})
                continue
        if (
            travel_profile.get("risk_averse") == "high"
            and transfer_policy != "price_first"
            and stops > 0
            and (_min_layover_minutes(flight) or 9999) < 90
        ):
            excluded.append({**flight, "exclude_reason": "出行风险画像：中转时间低于90分钟"})
            continue

        if not same_day_meeting_time_override and red_eye == "reject" and _is_red_eye(flight):
            excluded.append({**flight, "exclude_reason": "用户不接受红眼/过早航班"})
            continue
        if not same_day_meeting_time_override and red_eye in {"accept", "flexible", "cheap_ok"} and _is_red_eye(flight):
            penalty += 2 if red_eye in {"accept", "flexible"} else 1
            if cheapest_non_red_eye and price < cheapest_non_red_eye:
                notes.append(f"红眼但便宜¥{cheapest_non_red_eye - price:,.0f}")
            else:
                penalties.append("红眼/过早航班")

        if need_baggage == "required":
            if _has_free_checked_baggage(flight):
                notes.append("含免费托运")
            else:
                penalty += 3
                penalties.append("托运行李需官网确认")

        if refund_flexibility == "preferred":
            if _has_refund_change_flexibility(flight):
                notes.append("退改签较灵活")
            else:
                penalty += 1
                penalties.append("退改签需确认")
        elif refund_flexibility == "required":
            if _has_refund_change_flexibility(flight, required=True):
                notes.append("满足可退改")
            else:
                penalty += 4
                penalties.append("未确认可退改")

        has_family_companion = passenger_friendly_mode or companions in {"with_elderly", "with_child", "with_elderly_child"}
        if has_family_companion:
            flight["passenger_friendly"] = {"active": True, "profile": dict(passenger_profile), "rules": dict(passenger_rules)}
            airline_text = " ".join(
                str(segment.get("airline", ""))
                for segment in flight.get("segments", [])
                if isinstance(segment, dict)
            )
            low_cost_markers = ["Spirit", "Frontier", "Spring", "VietJet", "AirAsia", "Scoot"]
            if stops == 0:
                notes.append("适合家庭出行：直飞")
            else:
                penalty += max(1, round(stops * 1.3))
            if _is_daytime_flight(flight):
                notes.append("适合家庭出行：白天时段")
            else:
                penalty += 2
                penalties.append("家庭出行时段不够友好")
            total_minutes = int(flight.get("total_duration_min") or 0)
            if total_minutes and total_minutes <= 20 * 60:
                notes.append("适合家庭出行：总时长较短")
            elif total_minutes > 24 * 60:
                penalty += 2
                penalties.append("家庭出行总时长偏长")
            if _has_free_checked_baggage(flight):
                notes.append("适合家庭出行：含免费托运")
            else:
                penalty += 1
            if _has_refund_change_flexibility(flight):
                notes.append("适合家庭出行：退改较灵活")
            else:
                penalty += 1
            if not same_day_meeting_time_override and _is_red_eye(flight):
                penalty += 3
            if _max_layover_minutes(flight) > 360:
                penalty += 2
                penalties.append("长中转不适合家庭出行")
            if any(marker.lower() in airline_text.lower() for marker in low_cost_markers):
                penalty += 2
                penalties.append("廉航不适合家庭出行")

        if "business" in travel_scenario_set:
            if stops > 0:
                penalty += 2
                penalties.append("商务/会议更适合直飞或低风险中转")
            if not same_day_meeting_time_override and not _is_daytime_flight(flight):
                penalty += 1
                penalties.append("商务/会议时段稳定性一般")
        if "important" in travel_scenario_set:
            if stops > 0:
                penalty += 3
                penalties.append("重要事项不适合复杂中转")
            if not same_day_meeting_time_override and _is_red_eye(flight):
                penalty += 3
                penalties.append("重要事项不适合红眼/凌晨航班")
        if "price_first" in travel_scenario_set:
            penalty = max(0, penalty - 2)
            notes.append("价格优先场景：保留低价不便方案")

        if direct_preferred and stops > 0:
            penalty += 2
            penalties.append("同行约束：更偏好直飞")
        if avoid_long_layover and _max_layover_minutes(flight) > 240:
            penalty += 3
            penalties.append("同行约束：中转等待偏长")
        if "need_baggage" in companion_constraints and not _has_free_checked_baggage(flight):
            penalty += 3
            penalties.append("同行约束：托运行李需确认")
        if "need_refund_change" in companion_constraints and not _has_refund_change_flexibility(flight):
            penalty += 3
            penalties.append("同行约束：退改签需确认")
        if not same_day_meeting_time_override and no_late_arrival:
            arrival_hour = _last_arrival_hour(flight)
            if arrival_hour is not None and (arrival_hour >= 23 or arrival_hour < 6):
                penalty += 3
                penalties.append("不接受深夜/凌晨到达")
        if not same_day_meeting_time_override and prefer_daytime_arrival:
            arrival_hour = _last_arrival_hour(flight)
            if arrival_hour is not None and not (6 <= arrival_hour < 22):
                penalty += 2
                penalties.append("希望白天到达")
        if not same_day_meeting_time_override and solo_travel and _is_red_eye(flight):
            penalty += 2
            penalties.append("独自出行降低红眼方案权重")
        if companions == "group":
            notes.append("多人同行：请重点确认低价库存是否充足")

        if price_sensitivity == "low":
            if stops > 0:
                penalty += 2
            if (not same_day_meeting_time_override and _is_red_eye(flight)) or _max_layover_minutes(flight) > 360:
                penalty += 2
            notes.append("便利稳定优先")
        elif price_sensitivity == "medium":
            notes.append("便宜时可接受轻微不便")
        elif price_sensitivity == "high":
            if stops > 0 or (not same_day_meeting_time_override and _is_red_eye(flight)):
                notes.append("便宜但便利性较低")
        elif price_sensitivity == "max":
            penalty = max(0, penalty - 2)
            notes.append("价格优先")

        if trip_rigidity == "confirmed":
            if _has_refund_change_flexibility(flight):
                notes.append("行程确定：可尽早锁定")
            else:
                notes.append("行程确定：关注价格锁定")
        elif trip_rigidity == "mostly":
            notes.append("行程基本确定：可观察1-2天")
        elif trip_rigidity == "flexible":
            notes.append("行程灵活：可等待更低价")

        trip_type = preferences.get("trip_type")
        if trip_type == "business_meeting":
            penalty += stops
            if not same_day_meeting_time_override and _is_red_eye(flight):
                penalty += 2
        elif trip_type == "tourism":
            penalty += 0
        elif trip_type == "family_elder":
            penalty += stops * 2
            if _max_layover_minutes(flight) > 240:
                penalty += 2

        flight["preference_notes"] = notes
        flight["preference_penalties"] = penalties
        flight["preference_penalty"] = penalty
        kept.append(flight)

    if not kept:
        if direct_required and not direct_flights and direct_reference_candidates:
            reference_flights = [
                {
                    **flight,
                    "preference_reference": True,
                    "preference_notes": list(flight.get("preference_notes") or [])
                    + ["未找到直飞航班，以下为中转参考方案"],
                }
                for flight in flights
            ]
            return reference_flights, excluded, {
                "fallback": True,
                "fallback_reason": "no_direct_flights",
                "message": "未找到直飞航班，以下为中转参考方案",
            }
        return [], excluded, {
            "fallback": False,
            "message": "没有航班满足当前硬约束",
        }
    return kept, excluded, {"fallback": False}


def _apply_lcc_policy(
    flights: list[dict],
    preferences: dict | None,
) -> tuple[list[dict], list[dict]]:
    """在既有航司与约束管线之后执行廉航硬过滤，不参与评分。"""
    policy = str((preferences or {}).get("lcc_policy") or "any").strip()
    if policy not in LCC_POLICIES or policy == "any":
        return list(flights or []), []
    kept = []
    excluded = []
    for flight in flights or []:
        summary = classify_itinerary(flight)
        if policy == "exclude_lcc" and summary["has_lcc"]:
            excluded.append(
                {
                    **flight,
                    "exclude_reason": "命中廉价航空排除策略",
                    "lcc_summary": summary,
                }
            )
            continue
        if policy == "lcc_only" and not summary["all_lcc"]:
            excluded.append(
                {
                    **flight,
                    "exclude_reason": "并非全部航段均为廉价航空",
                    "lcc_summary": summary,
                }
            )
            continue
        kept.append(flight)
    return kept, excluded


def _excluded_flight_summary_legacy(flights: list[dict]) -> list[dict]:
    summaries = []
    for flight in flights or []:
        price = _to_float(flight.get("price"))
        if price is None or price <= 0:
            continue
        summaries.append(
            {
                "flight": dict(flight),
                "price": price,
                "flight_combo": flight.get("flight_combo") or "",
                "airline_summary": flight.get("airline_summary")
                or " / ".join(flight.get("airlines") or []),
                "reason": flight.get("exclude_reason") or "不符合当前筛选条件",
            }
        )
    return sorted(summaries, key=lambda item: item["price"])


def _excluded_flight_summary(flights: list[dict]) -> list[dict]:
    """Keep enough excluded-flight context for notification explanations."""
    summaries = []
    for flight in flights or []:
        price = _to_float(flight.get("price"))
        if price is None or price <= 0:
            continue
        summaries.append(
            {
                "flight": dict(flight),
                "price": price,
                "flight_combo": flight.get("flight_combo") or "",
                "airline_summary": flight.get("airline_summary")
                or " / ".join(flight.get("airlines") or []),
                "reason": flight.get("exclude_reason") or "不符合当前筛选条件",
                "filter_reason_code": flight.get("filter_reason_code") or "",
                "filter_reason_value": flight.get("filter_reason_value") or "",
                "scope": flight.get("scope") or flight.get("direction") or "single_leg",
                "route_summary": flight.get("route_summary") or "",
                "segments": flight.get("segments") or [],
                "layovers": flight.get("layovers") or [],
                "airlines": flight.get("airlines") or [],
                "stops": flight.get("stops", 0),
                "fare_verification": flight.get("fare_verification") or {},
                "availability": flight.get("availability") or {},
                "transfer_risk": flight.get("transfer_risk") or {},
                "price_estimate": flight.get("price_estimate") or {},
                "data_source": flight.get("data_source") or flight.get("source") or "",
            }
        )
    return sorted(summaries, key=lambda item: item["price"])


def _extract_history_prices(price_insights: dict | None) -> list[float]:
    history = (price_insights or {}).get("price_history") or []
    prices = []
    for item in history:
        value = item[1] if isinstance(item, (list, tuple)) and len(item) >= 2 else item
        price = _to_float(value)
        if price and price > 0:
            prices.append(price)
    return prices


def _auto_target_price(price_insights: dict | None, mode: str) -> float | None:
    prices = sorted(_extract_history_prices(price_insights))
    if len(prices) < 5:
        return None
    if mode == "low_zone":
        percentile = 0.30
    elif mode == "auto_judge":
        percentile = 0.25
    else:
        percentile = 0.35
    index = min(len(prices) - 1, max(0, round((len(prices) - 1) * percentile)))
    return float(prices[index])


def _auto_budget_price(price_insights: dict | None, percentile: float) -> float | None:
    prices = sorted(_extract_history_prices(price_insights))
    if len(prices) < 5:
        return None
    index = min(len(prices) - 1, max(0, round((len(prices) - 1) * percentile)))
    return float(prices[index])


def price_tolerance_advice(
    price, target_price=None, tolerance=100, max_budget=None
) -> dict | None:
    current = _to_float(price)
    target = _to_float(target_price)
    tolerance_value = _to_float(tolerance)
    max_budget_value = _to_float(max_budget)
    if current is None or current <= 0 or target is None or target <= 0:
        return None
    tolerance_value = tolerance_value if tolerance_value is not None else 100
    buy_upper = target + tolerance_value

    if current <= target:
        level = "below_target"
        label = "低于理想价，建议确认购买"
    elif current <= buy_upper:
        level = "within_tolerance"
        label = "在可接受浮动范围内，建议购买"
    elif max_budget_value and current <= max_budget_value:
        level = "within_budget"
        label = "高于理想区间，仅刚需建议购买"
    else:
        level = "over_budget"
        label = "超出最高预算，不推荐"

    return {
        "level": level,
        "label": label,
        "current_price": current,
        "target_price": target,
        "tolerance": tolerance_value,
        "buy_upper": buy_upper,
        "max_budget": max_budget_value,
    }


def _bounded_score(value) -> float:
    try:
        return max(0, min(100, float(value)))
    except (TypeError, ValueError):
        return 0


def _profile_weight(level: str) -> float:
    return {"high": 0.35, "medium": 0.2, "low": 0.1}.get(level, 0.2)


def _flight_price_score(flight: dict, target_price=None) -> float:
    price = _to_float(flight.get("price")) or 0
    target = _to_float(target_price)
    if target and target > 0 and price > 0:
        return _bounded_score(100 - abs(price - target) / target * 100)
    raw_price_score = (flight.get("scores") or {}).get("price_score", 5)
    return _bounded_score(float(raw_price_score) * 10)


def _flight_time_score(flight: dict) -> float:
    score = 50
    if _stops_count(flight) == 0:
        score += 20
    if _is_daytime_flight(flight):
        score += 20
    total_minutes = int(flight.get("total_duration_min") or 0)
    if total_minutes and total_minutes <= 8 * 60:
        score += 10
    elif total_minutes > 24 * 60:
        score -= 20
    return _bounded_score(score)


def _flight_comfort_score(flight: dict) -> float:
    score = 45
    stops = _stops_count(flight)
    if stops == 0:
        score += 30
    elif stops == 1:
        score += 10
    else:
        score -= 10
    if _max_layover_minutes(flight) <= 180:
        score += 10
    elif _max_layover_minutes(flight) > 480:
        score -= 15
    if _contains_any_airline(flight, FULL_SERVICE_AIRLINES):
        score += 10
    if _is_red_eye(flight):
        score -= 20
    return _bounded_score(score)


def _flight_baggage_score(flight: dict) -> float:
    if _has_free_checked_baggage(flight):
        return 100
    fare_rules = flight.get("fare_rules") or flight.get("fare_verification") or {}
    if fare_rules:
        return 60
    return 40


def _flight_refund_score(flight: dict) -> float:
    if _has_refund_change_flexibility(flight, required=True):
        return 100
    if _has_refund_change_flexibility(flight):
        return 80
    fare_rules = flight.get("fare_rules") or flight.get("fare_verification") or {}
    refund = (fare_rules or {}).get("refund") or {}
    if refund:
        level = str(refund.get("level") or refund.get("label") or "")
        if level in {"高", "中", "friendly", "medium"}:
            return 70
        return 50
    return 40


def _profile_score_weights(profile: dict | None) -> dict:
    profile = profile or {}
    explicit = profile.get("score_weights") or ((profile.get("passenger_rules") or {}).get("weights"))
    if explicit:
        return {
            "price": float(explicit.get("price", explicit.get("w_price", 0.2)) or 0),
            "time": float(explicit.get("time", explicit.get("w_time", 0.2)) or 0),
            "comfort": float(explicit.get("comfort", explicit.get("w_comfort", 0.2)) or 0),
            "risk": float(explicit.get("execution_risk", explicit.get("risk", explicit.get("w_execution_risk", 0.2))) or 0),
            "baggage": float(explicit.get("baggage", explicit.get("w_baggage", 0.1)) or 0),
            "refund": float(explicit.get("refund", explicit.get("w_refund", 0.05)) or 0),
        }
    return {
        "price": _profile_weight(profile.get("price", "medium")),
        "time": _profile_weight(profile.get("time", "medium")),
        "comfort": _profile_weight(profile.get("comfort", "medium")),
        "risk": _profile_weight(profile.get("risk_averse", "medium")),
        "baggage": _profile_weight(profile.get("baggage", "medium")),
        "refund": 0.05,
    }


def calc_final_score(flight: dict, target_price=None, profile: dict | None = None) -> float:
    profile = profile or build_travel_profile({})
    price_score = _flight_price_score(flight, target_price)

    risk_score = (flight.get("execution_risk") or {}).get("score", 50)
    reliability_score = max(0, 100 - float(risk_score))

    preference_score = flight.get("preference_score")
    if preference_score is None:
        preference_score = (flight.get("scores") or {}).get("total", 5)
    preference_score = max(0, min(100, float(preference_score) * 10))

    time_score = _flight_time_score(flight)
    comfort_score = _flight_comfort_score(flight)
    baggage_score = _flight_baggage_score(flight)
    refund_score = _flight_refund_score(flight)
    weights = _profile_score_weights(profile)
    total_w = sum(weights.values()) or 1

    final_score = (
        price_score * weights["price"]
        + time_score * weights["time"]
        + comfort_score * weights["comfort"]
        + reliability_score * weights["risk"]
        + baggage_score * weights["baggage"]
        + refund_score * weights["refund"]
    ) / total_w
    final_score = final_score * 0.85 + _bounded_score(preference_score) * 0.15
    flight["score_components"] = {
        "price": round(price_score, 1),
        "time": round(time_score, 1),
        "comfort": round(comfort_score, 1),
        "risk": round(reliability_score, 1),
        "baggage": round(baggage_score, 1),
        "refund": round(refund_score, 1),
        "preference": round(_bounded_score(preference_score), 1),
        "weights": weights,
    }
    flight["travel_profile"] = dict(profile)
    flight["final_score"] = round(final_score, 1)
    return flight["final_score"]


def analyze_all_flights(
    flights: list[dict],
    price_insights: dict = None,
    mode: str = "balanced",
    priorities=None,
    user_preferences=None,
    hard_constraints=None,
) -> dict:
    """Analyze and rank all flight options."""
    if not flights:
        return {"error": "no_flights"}

    usable_flights = [
        flight
        for flight in flights
        if (_to_float(flight.get("price")) or 0) > 0
        and flight.get("total_duration_min") is not None
    ]
    if not usable_flights:
        return {
            "error": "no_valid_prices",
            "total_options": 0,
            "all_flights": [],
            "price_range": [],
            "current_min_price": None,
            "market_context": {},
            "price_insights": price_insights,
        }

    detail_reference_flights = []
    actionable_flights = []
    for flight in usable_flights:
        if flight.get("reference_only") or not has_enough_detail(flight):
            reference = {
                **flight,
                "reference_only": True,
                "reference_reason": _reference_only_reason(flight),
                "preference_reference": True,
            }
            detail_reference_flights.append(reference)
        else:
            actionable_flights.append(flight)
    usable_flights = actionable_flights
    if not usable_flights:
        return {
            "error": "no_actionable_flights",
            "total_options": 0,
            "total_reference_options": len(detail_reference_flights),
            "all_flights": [],
            "reference_flights": detail_reference_flights,
            "price_range": [],
            "current_min_price": None,
            "market_context": {},
            "price_insights": price_insights,
        }

    mode = _trip_mode(mode, user_preferences)
    mode = mode if mode in SCORE_WEIGHTS else "balanced"
    original_options = len(usable_flights)
    if hard_constraints:
        merged_preferences = {**(user_preferences or {}), **hard_constraints}
        if "baggage" in hard_constraints and "need_baggage" not in merged_preferences:
            merged_preferences["need_baggage"] = hard_constraints.get("baggage")
    else:
        merged_preferences = user_preferences or {}
    mixed_cabin_allocation = merged_preferences.get("cabin_allocation")
    mixed_business_flights = []
    if isinstance(mixed_cabin_allocation, dict):
        mixed_business_flights = [
            flight
            for flight in usable_flights
            if (flight.get("cabin_class") or "economy") == "business"
        ]
        usable_flights = [
            flight
            for flight in usable_flights
            if (flight.get("cabin_class") or "economy") == "economy"
        ]
        if not usable_flights:
            economy_reason = (
                "经济舱记录缺少完整航段信息"
                if detail_reference_flights
                else "无可用经济舱报价"
            )
            return {
                "error": "no_economy_candidates",
                "business_flights": mixed_business_flights,
                "reference_flights": detail_reference_flights,
                "total_reference_options": len(detail_reference_flights),
                "economy_candidate_reason": economy_reason,
            }
    roundtrip_purchase_context = bool(
        merged_preferences.get("round_trip")
        or merged_preferences.get("same_day_round_trip")
    )
    travel_profile = build_travel_profile(merged_preferences)
    alert_policy = build_alert_policy(travel_profile)
    filter_input_pool = list(usable_flights)
    print(f"[过滤前] {len(usable_flights)}个航班, 约束: {merged_preferences}")
    for flight in usable_flights:
        print(
            f"  航班 {flight.get('flight_combo')}: stops={flight.get('stops')}"
        )

    transfer_policy = (
        (hard_constraints or {}).get("transfer_policy")
        or (hard_constraints or {}).get("direct_only")
        or merged_preferences.get("transfer_policy")
        or merged_preferences.get("direct_only")
    )
    if transfer_policy in ("must", "direct_only", "must_direct"):
        direct_flights = [
            flight for flight in usable_flights if _stops_count(flight) == 0
        ]
        if direct_flights:
            direct_policy_excluded = [
                {**flight, "exclude_reason": "用户设置必须直飞"}
                for flight in usable_flights
                if _stops_count(flight) > 0
            ]
            usable_flights = direct_flights
        else:
            direct_policy_excluded = []
            merged_preferences["no_direct_flag"] = True
    else:
        direct_policy_excluded = []

    same_day_base_flights = list(usable_flights)
    usable_flights, preference_excluded, preference_summary = _apply_user_preferences(
        usable_flights, merged_preferences
    )
    scoring_reference_flights = list(usable_flights)
    usable_flights, lcc_excluded = _apply_lcc_policy(
        usable_flights,
        merged_preferences,
    )
    preference_excluded.extend(lcc_excluded)
    excluded_flights = direct_policy_excluded + preference_excluded
    _attach_filter_reason_details(excluded_flights, merged_preferences)
    _log_low_price_filter_rejections(
        filter_input_pool,
        excluded_flights,
        merged_preferences,
    )
    print(f"[过滤后] {len(usable_flights)}个航班")
    if not usable_flights:
        return {
            "error": "no_flights",
            "excluded_flights": _excluded_flight_summary(excluded_flights),
            "reference_flights": detail_reference_flights,
        }

    # 1. 鎸変环鏍兼帓鍚?
    by_price = sorted(usable_flights, key=lambda f: _to_float(f.get("price")) or float("inf"))

    # 2. 鎸夋€绘椂闀挎帓鍚?
    by_duration = sorted(usable_flights, key=lambda f: f["total_duration_min"])

    # 3. 鎸夋€т环姣旀帓鍚嶏紙缁煎悎寰楀垎锛?
    scoring_prices = [
        float(f["price"])
        for f in scoring_reference_flights
        if (_to_float(f.get("price")) or 0) > 0
    ]
    valid_prices = [
        float(f["price"])
        for f in usable_flights
        if (_to_float(f.get("price")) or 0) > 0
    ]
    lowest_price = min(valid_prices) if valid_prices else None
    if lowest_price is None:
        return {
            "error": "no_valid_prices",
            "total_options": len(usable_flights),
            "all_flights": usable_flights,
            "price_range": [],
            "current_min_price": None,
            "market_context": {},
            "price_insights": price_insights,
        }
    prices = valid_prices
    durations = [f["total_duration_min"] for f in usable_flights]
    scoring_durations = [
        f["total_duration_min"]
        for f in scoring_reference_flights
    ]
    min_p, max_p = min(scoring_prices), max(scoring_prices)
    min_d, max_d = min(scoring_durations), max(scoring_durations)
    price_anomalies = detect_price_anomalies(usable_flights, price_insights)
    budget_strategy = (merged_preferences or {}).get("budget_strategy", "explicit")
    target_price_mode = (merged_preferences or {}).get("target_price_mode", "auto")
    target_price = _to_float((merged_preferences or {}).get("target_price"))
    target_price_effective = target_price
    if budget_strategy == "auto_judge":
        target_price_effective = _auto_budget_price(price_insights, 0.25)
    elif budget_strategy == "low_price_alert":
        target_price_effective = _auto_budget_price(price_insights, 0.30)
    elif not target_price_effective and target_price_mode in {"auto", "low_zone", "auto_judge"}:
        target_price_effective = _auto_target_price(price_insights, target_price_mode)
    max_budget_effective = _to_float((merged_preferences or {}).get("max_budget"))
    if budget_strategy == "auto_judge":
        max_budget_effective = _auto_budget_price(price_insights, 0.75)
    elif budget_strategy == "low_price_alert":
        max_budget_effective = None
    elif max_budget_effective is None:
        max_budget_effective = _to_float((merged_preferences or {}).get("budget"))
    price_tolerance = _to_float((merged_preferences or {}).get("price_tolerance"))
    if price_tolerance is None:
        price_tolerance = 100

    for flight in usable_flights:
        price_score = (
            ((float(flight["price"]) - min_p) / (max_p - min_p)) if max_p > min_p else 0
        )
        duration_score = (
            (flight["total_duration_min"] - min_d) / (max_d - min_d)
            if max_d > min_d
            else 0
        )
        stops_score = _stops_count(flight) / 3
        flight["value_score"] = round(
            price_score * 0.5 + duration_score * 0.3 + stops_score * 0.2,
            3,
        )
        flight["scores"] = overall_score(
            flight,
            scoring_prices,
            scoring_durations,
            mode,
        )
        flight["transfer_risk"] = transfer_risk(flight)
        flight["fare_verification"] = verify_fare_rules(flight, merged_preferences)
        enrich_travel_risk_and_cost(flight, merged_preferences)
        flight["domestic_tags"] = make_domestic_tags(
            flight,
            travel_profile,
            lowest_price,
        )
        flight["price_estimate"] = calc_transaction_price(flight, merged_preferences)
        flight["availability"] = estimate_availability(
            flight,
            flight.get("collected_at") or flight.get("snapshot_time") or flight.get("fetched_at"),
        )
        flight["buyability"] = classify_buyability(flight)
        calc_execution_risk(flight)
        if roundtrip_purchase_context:
            flight.pop("price_advice", None)
        else:
            advice = price_tolerance_advice(
                flight.get("price"),
                target_price_effective,
                price_tolerance,
                max_budget_effective,
            )
            if advice:
                flight["price_advice"] = advice
        calc_execution_grade(flight, merged_preferences)
        score_multiplier = float(flight.get("score_multiplier") or 1)
        flight["preference_score"] = round(
            flight["scores"]["total"] * score_multiplier
            - float(flight.get("preference_penalty") or 0),
            1,
        )
        calc_final_score(flight, target_price_effective, travel_profile)

    priority_config = _normalize_priorities(priorities)
    qualified_flights = []
    reference_flights = list(detail_reference_flights)
    if priority_config:
        for flight in by_price:
            violations = _priority_violations(flight, priority_config)
            boundary_notes = _priority_boundary_notes(flight, priority_config)
            flight["priority_violations"] = violations
            flight["priority_boundary_notes"] = boundary_notes
            if violations:
                reference_flights.append(flight)
            else:
                qualified_flights.append(flight)

    # 4. 鎸変娇鐢ㄥ満鏅瓫閫夋帹鑽愭柟妗?
    fastest_duration = by_duration[0]["total_duration_min"]

    def comfortable_layovers(flight: dict) -> bool:
        layovers = flight.get("layovers") or []
        if not layovers:
            return False
        return all(
            90 <= int(layover.get("wait_minutes") or 0) <= 240
            for layover in layovers
        )

    comfortable_candidates = [
        flight
        for flight in usable_flights
        if comfortable_layovers(flight)
        and flight["total_duration_min"] <= fastest_duration * 1.5
    ]
    if comfortable_candidates:
        most_comfortable = min(
            comfortable_candidates,
            key=lambda f: _to_float(f.get("price")) or float("inf"),
        )
    else:
        most_comfortable = min(
            usable_flights,
            key=lambda f: (
                max(
                    (layover.get("wait_minutes", 0) for layover in f.get("layovers", [])),
                    default=0,
                )
                > 480,
                _stops_count(f),
                f["total_duration_min"],
                _to_float(f.get("price")) or float("inf"),
            ),
        )

    recommendations = [
        {
            "tag": "预算有限选这个",
            "desc": "价格最低，但路上时间较长",
            "reason": "价格最低，但路上时间较长",
            "flight": by_price[0],
        },
        {
            "tag": "赶时间选这个",
            "desc": "鍒拌揪鏈€蹇紝浠锋牸绋嶉珮",
            "reason": "鍒拌揪鏈€蹇紝浠锋牸绋嶉珮",
            "flight": by_duration[0],
        },
        {
            "tag": "怕折腾选这个",
            "desc": "杞満鏈€杞绘澗锛屼笉鐢ㄥ湪鏈哄満杩囧",
            "reason": "杞満鏈€杞绘澗锛屼笉鐢ㄥ湪鏈哄満杩囧",
            "flight": most_comfortable,
        },
    ]

    market_context = {}
    if price_insights:
        market_context = {
            "lowest_market": price_insights.get("lowest_price"),
            "price_level": price_insights.get("price_level"),
            "typical_range": price_insights.get("typical_price_range"),
        }

    display_flights = []
    cabin_order = []
    for flight in usable_flights:
        cabin_class = flight.get("cabin_class") or "economy"
        if cabin_class not in cabin_order:
            cabin_order.append(cabin_class)
    for cabin_class in cabin_order:
        cabin_flights = [
            flight
            for flight in usable_flights
            if (flight.get("cabin_class") or "economy") == cabin_class
        ]
        display_flights.extend(
            sorted(cabin_flights, key=lambda f: _to_float(f.get("price")) or float("inf"))[:10]
        )

    economy_flights = [
        flight
        for flight in usable_flights
        if (flight.get("cabin_class") or "economy") == "economy"
    ]
    business_flights = [
        flight
        for flight in usable_flights
        if (flight.get("cabin_class") or "economy") == "business"
    ]
    economy_recommendations, business_recommendation = select_recommendations(
        economy_flights, business_flights, mode
    )
    cabin_price_ranges = {}
    for cabin_class in cabin_order:
        cabin_prices = [
            float(flight["price"])
            for flight in usable_flights
            if (flight.get("cabin_class") or "economy") == cabin_class
            and (_to_float(flight.get("price")) or 0) > 0
        ]
        if cabin_prices:
            cabin_price_ranges[cabin_class] = [min(cabin_prices), max(cabin_prices)]

    budget_strategy = (merged_preferences or {}).get("budget_strategy", "explicit")
    target_price_mode = (merged_preferences or {}).get("target_price_mode", "auto")
    target_price = _to_float((merged_preferences or {}).get("target_price"))
    target_price_effective = target_price
    if budget_strategy == "auto_judge":
        target_price_effective = _auto_budget_price(price_insights, 0.25)
    elif budget_strategy == "low_price_alert":
        target_price_effective = _auto_budget_price(price_insights, 0.30)
    elif not target_price_effective and target_price_mode in {"auto", "low_zone", "auto_judge"}:
        target_price_effective = _auto_target_price(price_insights, target_price_mode)
    max_budget_effective = _to_float((merged_preferences or {}).get("max_budget"))
    if budget_strategy == "auto_judge":
        max_budget_effective = _auto_budget_price(price_insights, 0.75)
    elif budget_strategy == "low_price_alert":
        max_budget_effective = None
    elif max_budget_effective is None:
        max_budget_effective = _to_float((merged_preferences or {}).get("budget"))
    price_tolerance = _to_float((merged_preferences or {}).get("price_tolerance"))
    if price_tolerance is None:
        price_tolerance = 100
    price_band = None
    if not roundtrip_purchase_context:
        price_band = price_tolerance_advice(
            lowest_price,
            target_price_effective,
            price_tolerance,
            max_budget_effective,
        )
    for flight in usable_flights:
        if roundtrip_purchase_context:
            flight.pop("price_advice", None)
        else:
            advice = price_tolerance_advice(
                flight.get("price"),
                target_price_effective,
                price_tolerance,
                max_budget_effective,
            )
            if advice:
                flight["price_advice"] = advice

    decision_flight = by_price[0] if by_price else usable_flights[0]
    confidence_breakdown = calc_confidence(
        decision_flight,
        {},
        (price_insights or {}).get("price_history") if price_insights else None,
    )
    decision_summary = generate_decision_summary(
        lowest_price,
        target_price_effective,
        max_budget_effective,
        confidence_breakdown,
        decision_flight.get("execution_grade"),
    )
    buy_vs_wait_risk = calc_buy_vs_wait_risk(
        lowest_price,
        (price_insights or {}).get("price_history") if price_insights else None,
        merged_preferences.get("days_to_dept"),
        target_price_effective,
        decision_flight.get("execution_grade"),
    )

    return {
        "total_options": len(usable_flights),
        "total_options_before_preferences": original_options,
        "recommendations": recommendations,
        "economy_recommendations": economy_recommendations,
        "business_recommendation": business_recommendation,
        "same_day_base_flights": same_day_base_flights,
        "all_flights": display_flights,
        "price_range": [lowest_price, max(prices)],
        "cabin_price_ranges": cabin_price_ranges,
        "cabin_policy_summary": build_cabin_policy_summary(merged_preferences, usable_flights),
        "duration_range": [min(durations), max(durations)],
        "market_context": market_context,
        "price_insights": price_insights,
        "price_anomalies": price_anomalies,
        "current_min_price": lowest_price,
        "max_budget": max_budget_effective,
        "target_price": target_price,
        "target_price_effective": target_price_effective,
        "target_price_mode": target_price_mode,
        "budget_strategy": budget_strategy,
        "low_price_alert_triggered": (
            budget_strategy != "low_price_alert"
            or (
                target_price_effective is not None
                and lowest_price is not None
                and lowest_price <= target_price_effective
            )
        ),
        "price_tolerance": price_tolerance,
        "price_band": price_band,
        "decision_summary": decision_summary,
        "buy_vs_wait_risk": buy_vs_wait_risk,
        "confidence_breakdown": confidence_breakdown,
        "travel_profile": travel_profile,
        "passenger_profile": travel_profile.get("passenger_profile"),
        "passenger_rules": travel_profile.get("passenger_rules"),
        "travel_profile_explanation": travel_profile_explanation(travel_profile),
        "recommendation_basis": build_recommendation_basis(travel_profile),
        "alert_policy": alert_policy,
        "airport_cost_comparison": build_airport_cost_comparison(
            filter_input_pool,
            merged_preferences,
        ),
        "mode": mode,
        "priorities": priority_config,
        "qualified_flights": qualified_flights,
        "reference_flights": reference_flights,
        "user_preferences": merged_preferences,
        **({"business_flights": mixed_business_flights} if isinstance(mixed_cabin_allocation, dict) else {}),
        "preference_excluded_count": len(preference_excluded),
        "excluded_flights": _excluded_flight_summary(excluded_flights),
        "preference_summary": preference_summary,
    }


def _mixed_economy_leg_reason(analysis: dict, leg_label: str) -> str:
    explicit = str(analysis.get("economy_candidate_reason") or "").strip()
    if explicit:
        return explicit if explicit.startswith(leg_label) else f"{leg_label}{explicit}"
    reference_count = int(
        analysis.get("total_reference_options")
        or len(analysis.get("reference_flights") or [])
        or 0
    )
    if reference_count:
        return f"{leg_label}经济舱记录缺少完整航段信息"
    error = str(analysis.get("error") or "").strip()
    if error == "no_valid_prices":
        return f"{leg_label}经济舱未获取到有效价格"
    if error in {"no_actionable_flights", "no_economy_candidates"}:
        return f"{leg_label}无可执行经济舱候选"
    return f"{leg_label}无可用经济舱候选"


def _mixed_economy_candidate_reason(
    outbound_analysis: dict,
    return_analysis: dict,
    outbound_top: list[dict],
    return_top: list[dict],
) -> str:
    reasons = []
    if not outbound_top:
        reasons.append(_mixed_economy_leg_reason(outbound_analysis, "去程"))
    if not return_top:
        reasons.append(_mixed_economy_leg_reason(return_analysis, "返程"))
    return "；".join(reasons) or "本轮未形成可配对的去返经济舱组合"


def _top_flights_for_round_trip(analysis: dict, limit: int = 3) -> list[dict]:
    flights = analysis.get("economy_recommendations") or analysis.get("all_flights") or []
    return sorted(
        [flight for flight in flights if (_to_float(flight.get("price")) or 0) > 0],
        key=lambda flight: _to_float(flight.get("price")) or 999999,
    )[:limit]


def _all_roundtrip_flights_for_same_day(analysis: dict) -> list[dict]:
    """Return the full hard-valid candidate list before recommendation ranking."""
    candidates = []
    seen = set()

    def add(flight: dict) -> None:
        if not isinstance(flight, dict) or not flight:
            return
        if (_to_float(flight.get("price")) or 0) <= 0:
            return
        key = (
            flight.get("flight_combo") or flight.get("flight_no") or flight.get("flight_number"),
            flight.get("departure_date"),
            flight.get("departure_time") or flight.get("dep_time"),
            flight.get("arrival_date"),
            flight.get("arrival_time") or flight.get("arr_time"),
            flight.get("price"),
        )
        if key in seen:
            return
        seen.add(key)
        candidates.append(flight)

    for source_key in (
        "same_day_base_flights",
        "raw_valid_flights",
        "raw_valid_outbound",
        "all_flights",
        "qualified_flights",
        "economy_recommendations",
    ):
        for flight in analysis.get(source_key) or []:
            add(flight)
    candidates, _ = _apply_lcc_policy(
        candidates,
        analysis.get("user_preferences") or {},
    )
    return candidates


def _flight_transaction_price(flight: dict):
    estimate = flight.get("price_estimate") or {}
    value = _to_float(estimate.get("transaction_price"))
    if value is None:
        value = _to_float(estimate.get("estimated_price"))
    return value if value is not None else _to_float(flight.get("price"))


def _roundtrip_airlines(flight: dict) -> set[str]:
    names = set(str(name) for name in flight.get("airlines") or [] if name)
    for segment in flight.get("segments") or []:
        if isinstance(segment, dict) and segment.get("airline"):
            names.add(str(segment.get("airline")))
    if not names and flight.get("airline_summary"):
        names.update(part.strip() for part in str(flight["airline_summary"]).split("/") if part.strip())
    return names


def _mix_match_tip(combinations: list[dict]) -> str:
    if not combinations:
        return ""
    best = combinations[0]
    best_total = _to_float(best.get("total_price"))
    if best_total is None:
        return ""
    same_airline = []
    for combo in combinations:
        outbound_airlines = _roundtrip_airlines(combo.get("outbound") or {})
        return_airlines = _roundtrip_airlines(combo.get("return") or {})
        if outbound_airlines and return_airlines and outbound_airlines.intersection(return_airlines):
            same_airline.append(combo)
    if not same_airline:
        return ""
    best_same = min(same_airline, key=lambda item: _to_float(item.get("total_price")) or 999999)
    same_total = _to_float(best_same.get("total_price"))
    if same_total is None or same_total <= best_total:
        return ""
    diff = same_total - best_total
    outbound = best.get("outbound") or {}
    return_flight = best.get("return") or {}
    return (
        "如果去程和返程分开买不同航司，总价可能更低："
        f"最优混搭：去程{outbound.get('flight_combo', '')}¥{best.get('outbound_price'):,.0f} + "
        f"返程{return_flight.get('flight_combo', '')}¥{best.get('return_price'):,.0f} = ¥{best_total:,.0f}，"
        f"比最优同航司组合便宜¥{diff:,.0f}"
    )


def analyze_roundtrip_trend(history: list[dict] | None) -> dict:
    """Analyze recent round-trip total price history."""
    rows = history or []
    prices = [
        _to_float(row.get("total", row.get("roundtrip_lowest")))
        for row in rows
        if _to_float(row.get("total", row.get("roundtrip_lowest"))) is not None
    ]
    if not prices:
        return {"available": False}

    recent = prices[-4:]
    if len(recent) >= 2:
        if recent[-1] < recent[0]:
            direction = "连续下降中" if all(recent[i] <= recent[i - 1] for i in range(1, len(recent))) else "整体下降"
            icon = ""
        elif recent[-1] > recent[0]:
            direction = "连续上涨中" if all(recent[i] >= recent[i - 1] for i in range(1, len(recent))) else "整体上涨"
            icon = ""
        else:
            direction = "基本持平"
            icon = ""
    else:
        direction = "数据积累中"
        icon = ""

    return {
        "available": True,
        "prices": prices,
        "recent_prices": recent,
        "previous": rows[-2] if len(rows) >= 2 else None,
        "current": rows[-1] if rows else None,
        "is_recent_low": prices[-1] <= min(prices),
        "direction": direction,
        "icon": icon,
    }


def _roundtrip_row_value(row: dict, key: str):
    if key == "outbound":
        return _to_float(row.get("outbound", row.get("outbound_lowest")))
    if key == "return":
        return _to_float(row.get("return", row.get("return_lowest")))
    return _to_float(row.get("total", row.get("roundtrip_lowest")))


def _roundtrip_percentile_level(percentile: int) -> str:
    if percentile <= 10:
        return f"当前处于极低水平（比{100 - percentile}%的历史价格都便宜）"
    if percentile <= 25:
        return f"当前处于较低水平（比{100 - percentile}%的历史价格都便宜）"
    if percentile <= 50:
        return "当前处于中等偏低水平"
    if percentile <= 75:
        return "当前处于中等偏高水平"
    if percentile <= 90:
        return f"当前处于较高水平（比{percentile}%的历史价格都贵）"
    return "当前处于极高水平"


def _roundtrip_leg_level(current_price, history: list[dict], key: str) -> str:
    current = _to_float(current_price)
    prices = [
        _roundtrip_row_value(row, key)
        for row in history or []
        if _roundtrip_row_value(row, key) is not None
    ]
    if current is None or len(prices) < 3:
        return "历史数据不足"
    percentile = round(sum(1 for price in prices if price < current) / len(prices) * 100)
    if percentile <= 25:
        return "较低水平"
    if percentile <= 50:
        return "中等偏低水平"
    if percentile <= 75:
        return "中等偏高水平"
    return "较高水平"


def analyze_roundtrip_prices(
    history: list[dict] | None,
    current_total,
    outbound_current,
    return_current,
    target_price=None,
    max_budget=None,
    days_to_dept=None,
    budget_is_roundtrip: bool = False,
) -> dict:
    """Analyze round-trip total price references, trend, and leg contribution."""
    rows = history or []
    current_total = _to_float(current_total)
    outbound_current = _to_float(outbound_current)
    return_current = _to_float(return_current)
    if current_total is None:
        return {"available": False}

    def rows_window(items):
        dates = []
        for row in items or []:
            if not isinstance(row, dict):
                continue
            text = str(
                row.get("date")
                or row.get("observed_at")
                or row.get("timestamp")
                or ""
            )[:10]
            try:
                dates.append(date.fromisoformat(text).isoformat())
            except (TypeError, ValueError):
                continue
        return [min(dates), max(dates)] if dates else None

    totals = [
        _roundtrip_row_value(row, "total")
        for row in rows
        if _roundtrip_row_value(row, "total") is not None
    ]
    outbound_prices = [
        _roundtrip_row_value(row, "outbound")
        for row in rows
        if _roundtrip_row_value(row, "outbound") is not None
    ]
    return_prices = [
        _roundtrip_row_value(row, "return")
        for row in rows
        if _roundtrip_row_value(row, "return") is not None
    ]

    chart_rows = list(rows)
    latest_total = _roundtrip_row_value(rows[-1], "total") if rows else None
    if latest_total is None or abs(latest_total - current_total) >= 1:
        chart_rows.append(
            {
                "date": datetime.now().date().isoformat(),
                "outbound": outbound_current,
                "return": return_current,
                "total": current_total,
            }
        )

    if not totals:
        totals = [current_total]
    elif abs(totals[-1] - current_total) >= 1:
        totals = totals + [current_total]

    references = {
        "current": {
            "price": current_total,
            "outbound": outbound_current,
            "return": return_current,
            "sample_size": 1,
            "window": rows_window(chart_rows[-1:]),
        },
    }
    if totals:
        references["absolute_min"] = {
            "price": min(totals),
            "label": "历史往返最低",
            "sample_size": len(totals),
            "window": rows_window(chart_rows),
        }
        references["recent_min"] = {
            "price": min(totals[-14:]),
            "label": "近期往返最低（你关注以来）",
            "sample_size": len(totals[-14:]),
            "window": rows_window(chart_rows[-14:]),
        }
    if totals and days_to_dept is not None:
        references["conditional_min"] = {
            "price": min(totals),
            "label": f"同条件往返最低（提前{days_to_dept}天±7天）",
            "sample_size": len(totals),
            "window": rows_window(chart_rows),
        }

    short_term = {}
    recent = totals[-7:]
    if len(recent) >= 2:
        change_pct = round((recent[-1] - recent[0]) / recent[0] * 100, 1) if recent[0] else 0
        if all(recent[i] <= recent[i - 1] for i in range(1, len(recent))) and recent[-1] < recent[0]:
            trend = "持续下降中"
        elif all(recent[i] >= recent[i - 1] for i in range(1, len(recent))) and recent[-1] > recent[0]:
            trend = "持续上涨中"
        elif recent[-1] < recent[0]:
            trend = "下降中"
        elif recent[-1] > recent[0]:
            trend = "上涨中"
        else:
            trend = "基本持平"

        previous_row = chart_rows[-2] if len(chart_rows) >= 2 else {}
        outbound_previous = _roundtrip_row_value(previous_row, "outbound")
        return_previous = _roundtrip_row_value(previous_row, "return")
        outbound_change = (
            outbound_current - outbound_previous
            if outbound_current is not None and outbound_previous is not None
            else None
        )
        return_change = (
            return_current - return_previous
            if return_current is not None and return_previous is not None
            else None
        )
        short_term = {
            "trend": trend,
            "change_pct": change_pct,
            "prices": recent,
            "outbound_change": outbound_change,
            "return_change": return_change,
        }

    mid_term = {}
    if len(totals) >= 2:
        percentile = round(sum(1 for price in totals if price < current_total) / len(totals) * 100)
        avg_price = round(sum(totals) / len(totals))
        mid_term = {
            "percentile": percentile,
            "level": _roundtrip_percentile_level(percentile),
            "min": min(totals),
            "max": max(totals),
            "avg": avg_price,
            "vs_avg": current_total - avg_price,
            "data_points": len(totals),
        }

    split = {}
    if outbound_prices and return_prices:
        outbound_level = _roundtrip_leg_level(outbound_current, rows, "outbound")
        return_level = _roundtrip_leg_level(return_current, rows, "return")
        contribution = ""
        previous_row = chart_rows[-2] if len(chart_rows) >= 2 else {}
        outbound_change = short_term.get("outbound_change")
        return_change = short_term.get("return_change")
        if outbound_change is not None and return_change is not None:
            if outbound_change > 0 and return_change < 0:
                contribution = "返程降价抵消了去程涨价"
            elif outbound_change < 0 and return_change > 0:
                contribution = "去程降价抵消了返程涨价"
            elif outbound_change < 0 and return_change < 0:
                contribution = "去程和返程同步下降"
            elif outbound_change > 0 and return_change > 0:
                contribution = "去程和返程同步上涨"
        split = {
            "outbound_level": outbound_level,
            "return_level": return_level,
            "contribution": contribution,
            "previous": previous_row,
        }

    target = _to_float(target_price)
    max_b = _to_float(max_budget)
    target_total = target if budget_is_roundtrip and target else (target * 2 if target else None)
    max_total = max_b if budget_is_roundtrip and max_b else (max_b * 2 if max_b else None)
    advice = ""
    if target_total and current_total <= target_total:
        advice = (
            f"往返总价¥{current_total:,.0f}已低于理想价¥{target_total:,.0f}，"
            "且处于近期低位。可以考虑锁定，继续等待的降幅空间有限。"
        )
    elif max_total and current_total <= max_total:
        advice = (
            f"往返总价¥{current_total:,.0f}在最高预算内，"
            "但仍高于理想价，可结合出行确定性继续观察。"
        )
    elif max_total and current_total > max_total:
        advice = (
            f"往返总价¥{current_total:,.0f}超出最高预算，"
            "可等待下一轮价格变化或扩大日期范围。"
        )

    return {
        "available": True,
        "history": rows,
        "references": references,
        "short_term": short_term,
        "mid_term": mid_term,
        "split": split,
        "trend_chart": chart_rows[-7:],
        "advice": advice,
    }


def _roundtrip_budget_advice(roundtrip_lowest, target_price=None, max_budget=None, budget_is_roundtrip: bool = False) -> str:
    total = _to_float(roundtrip_lowest)
    target = _to_float(target_price)
    max_b = _to_float(max_budget)
    if total is None:
        return ""
    target_total = target if budget_is_roundtrip and target else (target * 2 if target else None)
    max_total = max_b if budget_is_roundtrip and max_b else (max_b * 2 if max_b else None)
    if target_total and total <= target_total:
        return f"往返总价¥{total:,.0f}已低于理想总价¥{target_total:,.0f}，建议锁定"
    if max_total and total <= max_total:
        return "往返总价在预算内但高于理想价，可继续观望"
    if max_total and total > max_total:
        return "往返总价超出预算，建议等待降价"
    return ""


def _roundtrip_excluded_flight_from_item(item: dict) -> dict:
    flight = item.get("flight") if isinstance(item, dict) else None
    if isinstance(flight, dict) and flight:
        merged = dict(flight)
        for key in (
            "price",
            "flight_combo",
            "airline_summary",
            "segments",
            "layovers",
            "airlines",
            "stops",
            "total_duration_min",
            "price_estimate",
            "fare_verification",
            "availability",
            "filter_reason_code",
            "filter_reason_value",
            "transfer_risk",
        ):
            if item.get(key) not in (None, "", []):
                merged.setdefault(key, item.get(key))
        return merged
    return dict(item or {})


def _roundtrip_flight_identity(flight: dict) -> tuple:
    segments = flight.get("segments") or []
    first = segments[0] if segments else {}
    last = segments[-1] if segments else {}
    return (
        str(flight.get("flight_combo") or ""),
        _to_float(flight.get("price")),
        str(first.get("dep_airport") or ""),
        str(last.get("arr_airport") or ""),
        str(first.get("dep_time") or ""),
    )


def _roundtrip_candidate_flights(analysis: dict, direction: str) -> list[dict]:
    candidates = []
    seen = set()

    def add_candidate(flight: dict, reason: str = "") -> None:
        if not isinstance(flight, dict) or not flight:
            return
        price = _to_float(flight.get("price"))
        if price is None or price <= 0:
            return
        item = {
            "flight": dict(flight),
            "price": price,
            "reason": str(reason or "").strip(),
            "excluded": bool(reason),
            "direction": direction,
        }
        key = (_roundtrip_flight_identity(item["flight"]), item["excluded"], item["reason"])
        if key in seen:
            return
        seen.add(key)
        candidates.append(item)

    for flight in (analysis.get("economy_recommendations") or []) + (analysis.get("all_flights") or []):
        add_candidate(flight)

    for excluded in analysis.get("excluded_flights") or []:
        if not isinstance(excluded, dict):
            continue
        flight = _roundtrip_excluded_flight_from_item(excluded)
        reason = excluded.get("reason") or flight.get("exclude_reason") or "不符合当前筛选规则"
        add_candidate(flight, reason)

    return candidates


def _roundtrip_debug_aircraft(flight: dict) -> str:
    segments = flight.get("segments") or []
    if segments:
        value = segments[0].get("aircraft") or segments[0].get("plane_type") or segments[0].get("equipment")
        if value:
            return get_aircraft_name(value)
    return str(flight.get("aircraft") or flight.get("plane_type") or flight.get("equipment") or "待确认")


def _roundtrip_debug_departure(flight: dict) -> str:
    segments = flight.get("segments") or []
    if segments:
        value = segments[0].get("dep_time") or segments[0].get("departure_time")
        if value:
            return str(value)
    return str(flight.get("departure_time") or flight.get("dep_time") or "待确认")


def _roundtrip_combo_dedupe_key(combo: dict) -> tuple:
    outbound = combo.get("outbound") or {}
    return_flight = combo.get("return") or {}
    return (
        outbound.get("flight_combo") or _roundtrip_flight_identity(outbound),
        return_flight.get("flight_combo") or _roundtrip_flight_identity(return_flight),
    )


def _dedupe_roundtrip_combinations(combos: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for combo in combos or []:
        key = _roundtrip_combo_dedupe_key(combo)
        if key in seen:
            continue
        seen.add(key)
        result.append(combo)
    return result


def _roundtrip_debug_flight_no(flight: dict | None) -> str:
    flight = flight or {}
    return str(
        flight.get("flight_combo")
        or flight.get("flight_no")
        or flight.get("flight_number")
        or "待确认"
    )


def _print_roundtrip_plan_comparison(
    combos: list[dict],
    *,
    emit_diagnostics: bool = True,
) -> None:
    if not emit_diagnostics:
        return
    for index, combo in enumerate((combos or [])[:2]):
        label = "A" if index == 0 else "B"
        outbound = combo.get("outbound") or {}
        return_flight = combo.get("return") or {}
        safe_log(
            f"[方案对比] {label}去程={_roundtrip_debug_flight_no(outbound)} "
            f"{label}返程={_roundtrip_debug_flight_no(return_flight)} "
            f"{label}价={combo.get('total_price')}"
        )


def _dedupe_and_limit_excluded_roundtrip_combos(combos: list[dict], max_show: int = 3) -> list[dict]:
    seen_pairs = set()
    seen_reasons = set()
    result = []
    for combo in sorted(combos or [], key=lambda item: item.get("total_price") or 999999999):
        pair_key = _roundtrip_combo_dedupe_key(combo)
        if pair_key in seen_pairs:
            continue
        reason_key = tuple(sorted(str(reason) for reason in (combo.get("reasons") or []) if reason))
        if reason_key in seen_reasons:
            continue
        seen_pairs.add(pair_key)
        seen_reasons.add(reason_key)
        result.append(combo)
        if len(result) >= max_show:
            break
    return result


def _is_budget_exclusion_reason(reason: str) -> bool:
    text = str(reason or "").lower()
    return any(
        token in text
        for token in (
            "最高可接受",
            "最高价",
            "最高预算",
            "预算",
            "超出",
            "超过",
            "max budget",
            "over budget",
            "budget",
        )
    )


def _roundtrip_budget_safe_reasons(reasons: list[str], total, max_budget) -> list[str]:
    total_value = _to_float(total)
    max_budget_value = _to_float(max_budget)
    cleaned: list[str] = []
    removed_budget_reason = False
    for reason in reasons or []:
        text = str(reason or "").strip()
        if not text:
            continue
        if _is_budget_exclusion_reason(text):
            if max_budget_value is not None and total_value is not None:
                if total_value > max_budget_value:
                    cleaned.append(
                        f"往返总价¥{total_value:,.0f}超过最高可接受价¥{max_budget_value:,.0f}"
                    )
                else:
                    removed_budget_reason = True
                continue
        cleaned.append(text)
    if not cleaned and removed_budget_reason:
        cleaned.append("价格虽低但时间或其他条件不满足")
    return cleaned


def _roundtrip_budget_context(total, max_budget, recommended_total) -> dict:
    total_value = _to_float(total)
    max_budget_value = _to_float(max_budget)
    recommended_value = _to_float(recommended_total)
    candidate_decision = evaluate_purchase_budget(
        total_value,
        max_budget=max_budget_value,
    )
    recommended_decision = evaluate_purchase_budget(
        recommended_value,
        max_budget=max_budget_value,
    )
    recommended_over_budget = recommended_decision["is_over_budget"]
    return {
        "total": total_value,
        "max_budget": max_budget_value,
        "recommended_total": recommended_value,
        "candidate_budget_decision": candidate_decision,
        "recommended_budget_decision": recommended_decision,
        "candidate_over_budget": candidate_decision["is_over_budget"],
        "recommended_over_budget": recommended_over_budget,
        "all_over_budget_reference": bool(
            recommended_over_budget
            and candidate_decision["is_over_budget"]
            and total_value is not None
            and recommended_value is not None
            and total_value < recommended_value
        ),
    }


def _duration_text_from_minutes(minutes: int | float | None) -> str:
    if minutes is None:
        return ""
    total = int(round(abs(minutes)))
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f"{hours}小时{mins}分钟"
    if hours:
        return f"{hours}小时"
    return f"{mins}分钟"


def _roundtrip_exclusion_basis(
    constraints: dict | None,
    max_budget=None,
    passengers: dict | None = None,
    route_type: str | None = None,
) -> list[str]:
    from constraint_summary import build_constraint_summary

    return build_constraint_summary(
        constraints,
        max_budget=max_budget,
        passengers=passengers,
        route_type=route_type,
    )


def _roundtrip_specific_exclusion_reasons(
    outbound_flight: dict,
    return_flight: dict,
    original_reasons: list[str],
    constraints: dict | None,
    total,
    max_budget,
    budget_context: dict,
) -> list[str]:
    constraints = constraints or {}
    reasons = []
    business_start = _parse_time_minutes(constraints.get("business_start"))
    business_end = _parse_time_minutes(constraints.get("business_end"))
    business_start_text = _minutes_to_text(business_start) if business_start is not None else ""
    business_end_text = _minutes_to_text(business_end) if business_end is not None else ""
    meeting_text = f"会议{business_start_text}-{business_end_text}" if business_start_text and business_end_text else ""

    return_departure = _flight_departure_minutes(return_flight or {})
    return_departure_text = _minutes_to_text(return_departure) if return_departure is not None else ""
    if (
        constraints.get("same_day_round_trip")
        and business_end is not None
        and return_departure is not None
        and return_departure < business_end
    ):
        diff_text = _duration_text_from_minutes(business_end - return_departure)
        reasons.append(
            f"返程{return_departure_text}出发,但你的{meeting_text}还没结束,"
            f"返程早了约{diff_text},无法乘坐。"
        )

    outbound_arrival = _flight_arrival_minutes(outbound_flight or {})
    outbound_arrival_text = _minutes_to_text(outbound_arrival) if outbound_arrival is not None else ""
    if (
        constraints.get("same_day_round_trip")
        and business_start is not None
        and outbound_arrival is not None
        and outbound_arrival > business_start
    ):
        diff_text = _duration_text_from_minutes(outbound_arrival - business_start)
        reasons.append(
            f"去程{outbound_arrival_text}到达,但你的{meeting_text}已开始,"
            f"到达晚了约{diff_text},无法按时赶到。"
        )

    direct_only = str(
        constraints.get("direct_only")
        or constraints.get("transfer_policy")
        or constraints.get("direct_policy")
        or ""
    ).strip()
    if direct_only in {"must", "direct", "direct_only", "nonstop", "必须直飞"}:
        for label, flight in (("去程", outbound_flight), ("返程", return_flight)):
            if _stops_count(flight or {}) > 0:
                reasons.append(f"{label}需中转,但你设置了'必须直飞'。")

    if (
        budget_context.get("candidate_over_budget")
        and not budget_context.get("all_over_budget_reference")
    ):
        total_value = budget_context.get("total")
        max_budget_value = budget_context.get("max_budget")
        reasons.append(
            f"往返总价¥{total_value:,.0f},超过你的最高可接受价¥{max_budget_value:,.0f},"
            f"超出¥{total_value - max_budget_value:,.0f}。"
        )

    generic = []
    for reason in original_reasons or []:
        text = str(reason or "").strip()
        if not text:
            continue
        if reasons and any(token in text for token in ("时间", "会议", "窗口", "不符")):
            continue
        if text not in generic:
            generic.append(text)
    return (reasons + generic) or ["不符合当前约束"]


def _roundtrip_comparison_points(combo: dict, recommended_combo: dict | None, recommended_total) -> list[str]:
    points: list[str] = []
    combo = combo or {}
    recommended_combo = recommended_combo or {}
    outbound_flight = combo.get("outbound") or {}
    return_flight = combo.get("return") or {}

    flight_no = _roundtrip_debug_flight_no(outbound_flight)
    arrival_minutes = _flight_arrival_minutes(outbound_flight)
    arrival_text = (
        _minutes_to_text(arrival_minutes)
        if arrival_minutes is not None
        else (_first_time_text(outbound_flight, "arrival_time", "arr_time") or "到达待确认")
    )
    if flight_no or arrival_text:
        points.append(f"去程:此方案{flight_no} {arrival_text}到达")

    combo_pricing = combo.get("passenger_pricing") or {}
    total = _to_float(
        (combo_pricing.get("price_tiers") or {}).get("total_roundtrip_ref")
        or combo.get("total_price")
    )
    recommended_pricing = recommended_combo.get("passenger_pricing") or {}
    recommended_value = _to_float(
        (recommended_pricing.get("price_tiers") or {}).get("total_roundtrip_ref")
        or recommended_total
    )
    if total is not None and recommended_value is not None:
        diff = recommended_value - total
        if diff > 0:
            points.append(f"价格:此方案¥{total:,.0f},比推荐便宜¥{diff:,.0f} ✓")
        elif diff < 0:
            points.append(f"价格:此方案¥{total:,.0f},比推荐贵¥{abs(diff):,.0f} ✗")
        else:
            points.append(f"价格:此方案¥{total:,.0f},与推荐持平")

    recommended_return = recommended_combo.get("return") or {}
    return_dep = _minutes_to_text(_flight_departure_minutes(return_flight))
    recommended_return_dep = _minutes_to_text(_flight_departure_minutes(recommended_return))
    if return_dep and recommended_return_dep:
        reason_text = str(combo.get("reason") or " ".join(combo.get("reasons") or []))
        unusable_tokens = ("无法乘坐", "时间不符", "会议", "窗口", "不符合")
        this_mark = "不可用" if any(token in reason_text for token in unusable_tokens) else "需确认"
        points.append(f"返程时间:此方案{return_dep}({this_mark}) vs 推荐{recommended_return_dep}(可用) ✗")

    if points and any("不可用" in point for point in points):
        points.append("结论:虽便宜但返程时间不符合你的会议安排")
    return points


def _roundtrip_budget_safe_reasons_v2(
    reasons: list[str],
    total,
    max_budget,
    recommended_total=None,
) -> list[str]:
    """Remove budget-only contradictions from cheaper round-trip exclusions."""
    context = _roundtrip_budget_context(total, max_budget, recommended_total)
    total_value = context["total"]
    max_budget_value = context["max_budget"]
    all_over_budget_reference = context["all_over_budget_reference"]
    cleaned: list[str] = []
    removed_budget_reason = False
    for reason in reasons or []:
        text = str(reason or "").strip()
        if not text:
            continue
        if _is_budget_exclusion_reason(text):
            removed_budget_reason = True
            if (
                context.get("candidate_over_budget")
                and not all_over_budget_reference
            ):
                cleaned.append(
                    f"往返总价¥{total_value:,.0f}超过最高可接受价¥{max_budget_value:,.0f}"
                )
            continue
        cleaned.append(text)

    if not cleaned and removed_budget_reason:
        if all_over_budget_reference and max_budget_value is not None:
            cleaned.append(
                f"预算外低价参考：当前主推也超过预算¥{max_budget_value:,.0f}，此组合虽更便宜但仍仅作参考"
            )
        else:
            cleaned.append("价格虽低但时间或其他条件不满足")
    return cleaned


def _log_excluded_price_diagnostics(
    recommended_total,
    max_budget,
    combos: list[dict],
) -> None:
    prices = [
        price
        for price in (_to_float(combo.get("total_price")) for combo in (combos or []))
        if price is not None
    ]
    unique_keys = {
        _roundtrip_combo_dedupe_key(combo)
        for combo in (combos or [])
        if _to_float(combo.get("total_price")) is not None
    }
    budget = _to_float(max_budget)
    below_limit = sum(1 for price in prices if budget is not None and price < budget)
    lowest_five = sorted(prices)[:5]
    min_price = min(prices) if prices else None
    max_price = max(prices) if prices else None
    summary = (
        f"[排除诊断] 推荐方案价={recommended_total} 最高可接受价={max_budget} "
        f"候选数={len(prices)} 去重后={len(unique_keys)} min={min_price} max={max_price} "
        f"低于上限={below_limit if budget is not None else '不适用'} 最低5={lowest_five}"
    )
    safe_log(summary)
    if str(os.environ.get("FLIGHT_DEBUG_FULL_ARRAYS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        safe_log(f"[排除诊断][完整数组] 排除候选价={prices}")


def build_excluded_roundtrip_combos(
    outbound_analysis: dict,
    return_analysis: dict,
    recommended_total,
    max_show: int = 3,
    max_budget=None,
    constraints: dict | None = None,
    recommended_combo: dict | None = None,
    passengers: dict | None = None,
    route_type: str | None = None,
    emit_diagnostics: bool = True,
    include_without_reference: bool = False,
) -> list[dict]:
    """Build same-unit excluded round-trip combos for notification explanations."""
    recommended_total = _to_float(recommended_total)
    if recommended_total is None and not include_without_reference:
        return []

    outbound_candidates = _roundtrip_candidate_flights(outbound_analysis or {}, "outbound")
    return_candidates = _roundtrip_candidate_flights(return_analysis or {}, "return")
    combos = []

    for outbound in outbound_candidates:
        for return_item in return_candidates:
            reasons = []
            filter_reasons = []
            if outbound.get("excluded") and outbound.get("reason"):
                reasons.append(f"去程：{outbound['reason']}")
                outbound_flight = outbound.get("flight") or {}
                if outbound_flight.get("filter_reason_code"):
                    filter_reasons.append(
                        {
                            "direction": "去程",
                            "code": outbound_flight.get("filter_reason_code"),
                            "value": outbound_flight.get("filter_reason_value") or "",
                        }
                    )
            if return_item.get("excluded") and return_item.get("reason"):
                reasons.append(f"返程：{return_item['reason']}")
                return_flight = return_item.get("flight") or {}
                if return_flight.get("filter_reason_code"):
                    filter_reasons.append(
                        {
                            "direction": "返程",
                            "code": return_flight.get("filter_reason_code"),
                            "value": return_flight.get("filter_reason_value") or "",
                        }
                    )
            if not reasons:
                continue

            outbound_price = _to_float(outbound.get("price"))
            return_price = _to_float(return_item.get("price"))
            if outbound_price is None or return_price is None:
                continue
            total = outbound_price + return_price
            passenger_pricing = {}
            comparison_total = total
            normalized_passengers = _normalize_passengers(passengers) if passengers else {}
            if normalized_passengers:
                passenger_pricing = build_passenger_roundtrip_pricing(
                    outbound_price,
                    return_price,
                    normalized_passengers,
                    route_type,
                )
                comparison_total = _to_float(passenger_pricing.get("total_price")) or total
            if recommended_total is not None and comparison_total >= recommended_total:
                continue
            budget_context = _roundtrip_budget_context(comparison_total, max_budget, recommended_total)
            reasons = _roundtrip_budget_safe_reasons_v2(
                reasons,
                comparison_total,
                max_budget,
                recommended_total,
            )
            reasons = _roundtrip_specific_exclusion_reasons(
                outbound["flight"],
                return_item["flight"],
                reasons,
                constraints,
                comparison_total,
                max_budget,
                budget_context,
            )
            if not reasons:
                continue
            combo_payload = {
                "scope": "roundtrip",
                "is_roundtrip": True,
                "outbound": outbound["flight"],
                "return": return_item["flight"],
                "outbound_price": outbound_price,
                "return_price": return_price,
                "unit_roundtrip_price": total,
                "total_price": comparison_total,
                "roundtrip_price": comparison_total,
                "passenger_pricing": passenger_pricing,
                "price_tiers": passenger_pricing.get("price_tiers") if passenger_pricing else {},
                "diff": (
                    recommended_total - comparison_total
                    if recommended_total is not None
                    else None
                ),
                "reasons": reasons,
                "reason": "；".join(reasons),
                "filter_reasons": filter_reasons,
                "recommended_price": recommended_total,
                "max_budget": budget_context.get("max_budget"),
                "recommended_over_budget": budget_context.get("recommended_over_budget"),
                "all_over_budget_reference": budget_context.get("all_over_budget_reference"),
                "exclusion_basis": _roundtrip_exclusion_basis(
                    constraints,
                    max_budget,
                    normalized_passengers,
                    route_type,
                ),
            }
            combo_payload["comparison_points"] = _roundtrip_comparison_points(
                combo_payload,
                recommended_combo,
                recommended_total,
            )

            combos.append(combo_payload)

    if emit_diagnostics and recommended_total is not None:
        _log_excluded_price_diagnostics(recommended_total, max_budget, combos)
    recommended_budget_decision = evaluate_purchase_budget(
        recommended_total,
        max_budget=max_budget,
    )
    if emit_diagnostics and recommended_total is not None:
        safe_log(
            f"[排除诊断] 推荐方案是否超预算="
            f"{recommended_budget_decision['is_over_budget']}"
        )
    limited = _dedupe_and_limit_excluded_roundtrip_combos(combos, max_show)
    if emit_diagnostics:
        for combo in limited:
            outbound_flight = combo.get("outbound") or {}
            return_flight = combo.get("return") or {}
            safe_log(
                "[排除组合] "
                f"去程={outbound_flight.get('flight_combo')} "
                f"返程={return_flight.get('flight_combo')} "
                f"去程机型={_roundtrip_debug_aircraft(outbound_flight)} "
                f"返程机型={_roundtrip_debug_aircraft(return_flight)} "
                f"去程时间={_roundtrip_debug_departure(outbound_flight)} "
                f"返程时间={_roundtrip_debug_departure(return_flight)}"
            )
    return limited


def analyze_round_trip(
    outbound_analysis: dict,
    return_analysis: dict,
    target_price=None,
    max_budget=None,
    history: list[dict] | None = None,
    emit_diagnostics: bool = True,
) -> dict:
    """Analyze outbound and return legs together for a round-trip subscription."""
    outbound_top = _top_flights_for_round_trip(outbound_analysis, 3)
    return_top = _top_flights_for_round_trip(return_analysis, 3)
    combinations = []
    combined_preferences = {}
    for source in (
        outbound_analysis.get("user_preferences"),
        return_analysis.get("user_preferences"),
        outbound_analysis.get("hard_constraints"),
        return_analysis.get("hard_constraints"),
    ):
        if isinstance(source, dict):
            combined_preferences.update(source)
    pricing_passengers = _normalize_passengers(combined_preferences.get("passengers"))
    if not pricing_passengers:
        pricing_passengers = {
            "adult": max(1, _to_non_negative_int(combined_preferences.get("passenger_count"), 1)),
            "child": 0,
            "elderly": 0,
            "infant": 0,
        }
    pricing_route_type = (
        combined_preferences.get("route_type")
        or outbound_analysis.get("route_type")
        or return_analysis.get("route_type")
        or ""
    )
    budget_scope = normalize_budget_scope(combined_preferences.get("max_budget_scope") or combined_preferences.get("budget_scope"))
    target_budget_scope = normalize_budget_scope(combined_preferences.get("target_price_scope") or budget_scope)
    mixed_cabin_allocation = combined_preferences.get("cabin_allocation")
    mixed_cabin_active = bool(
        isinstance(mixed_cabin_allocation, dict)
        and str(combined_preferences.get("cabin_arrangement") or "") == "mixed"
    )
    if mixed_cabin_active:
        budget_scope = "all"
        target_budget_scope = "all"
    same_day_round_trip = bool(
        combined_preferences.get("same_day_round_trip")
        or outbound_analysis.get("same_day_round_trip")
        or return_analysis.get("same_day_round_trip")
    )
    same_day_combos = []
    same_day_no_feasible_note = ""
    same_day_time_conflict = False
    closest_same_day_outbound_options = []
    same_day_alternatives = []
    same_day_filter_counts = {}
    same_day_return_window_debug = []
    passenger_count_for_budget = max(1, sum((pricing_passengers or {"adult": 1}).values()))
    target_float = _to_float(target_price)
    max_budget_float = _to_float(max_budget)
    same_day_budget_limits = passenger_budget_limits(
        max_budget_float,
        target_float,
        budget_scope,
        passenger_count_for_budget,
        passengers=pricing_passengers,
        route_type=pricing_route_type,
        round_trip=True,
        max_budget_scope=budget_scope,
        target_price_scope=target_budget_scope,
    )
    same_day_max_budget = same_day_budget_limits.get("max_budget_total")
    same_day_budget_scope_label = ""
    if same_day_max_budget is not None:
        if same_day_budget_limits.get("budget_scope") == "per_person":
            same_day_budget_scope_label = (
                f"全员往返 vs 每人上限{same_day_budget_limits.get('input_max_budget'):g}"
                f"×{passenger_count_for_budget}=总上限{same_day_max_budget:g}"
            )
        elif passenger_count_for_budget > 1:
            same_day_budget_scope_label = f"全员往返 vs 总上限{same_day_max_budget:g}"
        else:
            same_day_budget_scope_label = f"单人往返 vs 上限{same_day_max_budget:g}"
    if same_day_round_trip:
        outbound_candidates = _all_roundtrip_flights_for_same_day(outbound_analysis)
        return_candidates = _all_roundtrip_flights_for_same_day(return_analysis)
        print(f"[计数诊断] 去程采集={len(outbound_candidates)} 返程采集={len(return_candidates)}")
        sample_dest = ""
        for flight in outbound_candidates:
            sample_dest = _flight_airport(flight or {}, "arrival_airport")
            if sample_dest:
                break
        depart_date_for_same_day = (
            outbound_analysis.get("depart_date")
            or outbound_analysis.get("departure_date")
            or combined_preferences.get("depart_date")
            or combined_preferences.get("departure_date")
        )
        if depart_date_for_same_day and not combined_preferences.get("depart_date"):
            combined_preferences["depart_date"] = str(depart_date_for_same_day)[:10]
        windows = compute_same_day_windows(combined_preferences, None, sample_dest)
        if windows:
            window_airports = [
                *[_flight_airport(flight or {}, "arrival_airport") for flight in outbound_candidates],
                *[_flight_airport(flight or {}, "departure_airport") for flight in return_candidates],
            ]
            windows = _ensure_same_day_airport_window_maps(windows, combined_preferences, window_airports)
        print(
            "[会议候选] 去程候选按到达时间排序: "
            + str(_same_day_candidate_debug_rows(outbound_candidates, depart_date_for_same_day))
        )
        print(
            "[会议调试] "
            f"same_day={combined_preferences.get('same_day_round_trip')}, "
            f"会议开始={combined_preferences.get('business_start')}, "
            f"会议结束={combined_preferences.get('business_end')}, "
            f"缓冲={combined_preferences.get('buffer_hours')}"
        )
        if windows:
            print(
                "[会议调试] "
                f"反推去程到达不晚于={windows['outbound_arrive_by']}, "
                f"返程出发不早于={windows['return_depart_after']}"
            )
        same_day_combos = build_same_day_combos(
            outbound_candidates,
            return_candidates,
            windows or depart_date_for_same_day,
            depart_date_for_same_day,
            constraints=combined_preferences,
        )
        if windows:
            ob_ok = [
                flight
                for flight in outbound_candidates
                if _flight_arrival_minutes(flight or {}) is not None
                and _same_day_outbound_passes_window(
                    flight or {},
                    _same_day_windows_for_airport(
                        windows,
                        combined_preferences,
                        _flight_airport(flight or {}, "arrival_airport"),
                    ),
                    depart_date_for_same_day,
                )
            ]
            same_day_return_window_debug = _same_day_return_window_debug_rows(
                return_candidates,
                windows,
                depart_date_for_same_day,
                constraints=combined_preferences,
            )
            for row in same_day_return_window_debug:
                print(
                    f"[\u4f1a\u8bae\u6bd4\u8f83] \u8fd4\u7a0b{row.get('flight_no')} "
                    f"\u539f\u59cb\u51fa\u53d1={repr(row.get('raw_departure'))} "
                    f"\u89e3\u6790\u540e={row.get('departure_datetime')} "
                    f"\u4e0b\u9650={row.get('return_depart_after_datetime')} "
                    f"\u7c7b\u578b=<class 'datetime.datetime'> "
                    f"\u901a\u8fc7={row.get('passed')}"
                )
            rt_ok = [
                return_candidates[int(row["index"])]
                for row in same_day_return_window_debug
                if row.get("passed") and int(row.get("index", -1)) < len(return_candidates)
            ]
        else:
            ob_ok = list(outbound_candidates)
            rt_ok = list(return_candidates)
        same_day_filter_counts = {
            "outbound_collected": len(outbound_candidates),
            "return_collected": len(return_candidates),
            "total_candidates": len(outbound_candidates) + len(return_candidates),
            "valid_price_count": len(outbound_candidates) + len(return_candidates),
            "after_meeting_outbound": len(ob_ok),
            "after_meeting_return": len(rt_ok),
            "return_after_lowerbound": len(rt_ok),
            "after_meeting_window": len(ob_ok) + len(rt_ok),
            "same_day_combos": len(same_day_combos),
        }
        print(f"[\u8fd4\u7a0b\u7a97\u53e3\u5f3a\u5236\u8bca\u65ad] return_collected={len(return_candidates)}")
        print(f"[\u8fd4\u7a0b\u7a97\u53e3\u5f3a\u5236\u8bca\u65ad] return_after_lowerbound={len(rt_ok)}")
        for row in same_day_return_window_debug[:5]:
            print(
                f"[\u8fd4\u7a0b\u7a97\u53e3\u5f3a\u5236\u8bca\u65ad] \u822a\u73ed\u53f7={row.get('flight_no')} / "
                f"\u51fa\u53d1datetime={row.get('departure_datetime')} / "
                f"\u4e0b\u9650datetime={row.get('return_depart_after_datetime')} / "
                f"\u662f\u5426>=\u4e0b\u9650={row.get('passed')}"
            )
        print(f"[当天往返全诊断] same_day_round_trip={same_day_round_trip}")
        print(f"[当天往返全诊断] 去程采集数={len(outbound_candidates)}")
        print(f"[当天往返全诊断] 返程采集数={len(return_candidates)}")
        print(f"[当天往返全诊断] 去程窗口符合(到达<=上限)={len(ob_ok)}")
        print(f"[当天往返全诊断] 返程窗口符合(出发>=下限)={len(rt_ok)}")
        print(f"[当天往返全诊断] 配对出的当天往返组合={len(same_day_combos)}")
        print(
            f"[当天往返诊断] same_day={same_day_round_trip}, 去程窗口符合数={len(ob_ok)}, "
            f"返程窗口符合数={len(rt_ok)}"
        )
        print(f"[当天往返诊断] 配对出的当天往返组合数={len(same_day_combos)}")
        if same_day_combos:
            first_combo = same_day_combos[0]
            first_outbound = first_combo.get("outbound") or {}
            first_return = first_combo.get("return") or {}
            combo_outbound_windows = first_combo.get("same_day_windows") or windows or {}
            combo_return_windows = first_combo.get("same_day_return_windows") or combo_outbound_windows
            print(
                f"[\u4f1a\u8bae\u9a8c\u8bc1] \u63a8\u8350\u65b9\u6848A\u53bb\u7a0b\u5230\u8fbe={_first_time_text(first_outbound, 'arrival_time', 'arr_time')} "
                f"(\u8981\u6c42<={combo_outbound_windows.get('outbound_arrive_by') or ''})"
            )
            print(
                f"[\u4f1a\u8bae\u9a8c\u8bc1] \u63a8\u8350\u65b9\u6848A\u8fd4\u7a0b\u51fa\u53d1={_first_time_text(first_return, 'departure_time', 'dep_time')} "
                f"(\u8981\u6c42>={combo_return_windows.get('return_depart_after') or ''})"
            )
        print(
            "[会议调试] 去程符合窗口的航班: "
            + str(
                [
                    f"{(combo.get('outbound') or {}).get('flight_no') or (combo.get('outbound') or {}).get('flight_combo')} "
                    f"{(combo.get('outbound') or {}).get('arrival_time')}"
                    for combo in same_day_combos
                ]
            )
        )
        if not same_day_combos:
            same_day_time_conflict = True
            closest_same_day_outbound_options = _closest_same_day_outbound_options(
                outbound_candidates,
                windows,
                outbound_analysis.get("depart_date") or outbound_analysis.get("departure_date"),
                constraints=combined_preferences,
            )
            same_day_alternatives = build_same_day_alternatives(
                outbound_candidates,
                return_candidates,
                windows,
                outbound_analysis.get("depart_date") or outbound_analysis.get("departure_date"),
                previous_day_outbound=(
                    outbound_analysis.get("previous_day_flights")
                    or outbound_analysis.get("previous_day_outbound")
                    or outbound_analysis.get("previous_day_outbound_flights")
                ),
                next_day_return=(
                    return_analysis.get("next_day_flights")
                    or return_analysis.get("next_day_return")
                    or return_analysis.get("next_day_return_flights")
                ),
                passengers=pricing_passengers,
                route_type=pricing_route_type,
                max_budget=same_day_max_budget,
                budget_scope_label=same_day_budget_scope_label,
                constraints=combined_preferences,
            )
            same_day_no_feasible_note = _same_day_no_feasible_note(
                outbound_candidates,
                return_candidates,
                combined_preferences,
            )

    mixed_cabin_matching = {}
    if same_day_round_trip:
        combinations = list(same_day_combos)
    else:
        for outbound in outbound_top:
            for return_flight in return_top:
                outbound_price = _to_float(outbound.get("price"))
                return_price = _to_float(return_flight.get("price"))
                if not outbound_price or outbound_price <= 0 or not return_price or return_price <= 0:
                    continue
                combinations.append(
                    {
                        "outbound": outbound,
                        "return": return_flight,
                        "outbound_price": outbound_price,
                        "return_price": return_price,
                        "total_price": outbound_price + return_price,
                        "transaction_total": (
                            (_flight_transaction_price(outbound) or outbound_price)
                            + (_flight_transaction_price(return_flight) or return_price)
                        ),
                    }
                )

    if mixed_cabin_active:
        mixed_cabin_matching = match_mixed_cabin_combinations(
            combinations,
            outbound_analysis.get("business_flights") or [],
            return_analysis.get("business_flights") or [],
            cabin_allocation=mixed_cabin_allocation,
            passengers=pricing_passengers,
            route_type=pricing_route_type,
        )
        stats = mixed_cabin_matching.get("stats") or {}
        mixed_cabin_matching["economy_candidate_counts"] = {
            "outbound": len(outbound_top),
            "return": len(return_top),
        }
        if int(stats.get("candidates") or 0) == 0:
            mixed_cabin_matching["economy_candidate_reason"] = (
                _mixed_economy_candidate_reason(
                    outbound_analysis,
                    return_analysis,
                    outbound_top,
                    return_top,
                )
            )
        safe_log(
            "[混舱匹配] "
            f"候选={stats.get('candidates', 0)} "
            f"全匹配={stats.get('full', 0)} "
            f"部分={stats.get('partial', 0)}"
        )
        combinations = list(mixed_cabin_matching.get("priceable") or [])
        if same_day_round_trip:
            same_day_combos = list(combinations)

    for combo in combinations:
        if combo.get("outbound") and combo.get("return") and not combo.get("mixed_cabin"):
            passenger_pricing = build_passenger_roundtrip_pricing(
                combo.get("outbound_price") or (combo.get("outbound") or {}).get("price"),
                combo.get("return_price") or (combo.get("return") or {}).get("price"),
                pricing_passengers,
                pricing_route_type,
            )
            combo["passenger_pricing"] = passenger_pricing
            combo["passenger_total_price"] = passenger_pricing.get("total_price")
            combo["single_adult_price"] = passenger_pricing.get("single_adult_price")
            combo["price_tiers"] = passenger_pricing.get("price_tiers") or {}
            combo["budget_scope"] = budget_scope
        if combo.get("outbound") and combo.get("return") and not combo.get("effective_cost"):
            combo["effective_cost"] = calc_roundtrip_effective_cost(
                combo.get("outbound") or {},
                combo.get("return") or {},
                combined_preferences,
            )

    combinations.sort(key=lambda item: item["total_price"])
    combinations = _dedupe_roundtrip_combinations(combinations)
    _print_roundtrip_plan_comparison(
        combinations,
        emit_diagnostics=emit_diagnostics,
    )
    best_combo = combinations[0] if combinations else {}
    if same_day_round_trip and best_combo:
        outbound_min = _to_float(best_combo.get("outbound_price"))
        return_min = _to_float(best_combo.get("return_price"))
        total_min = _to_float(best_combo.get("total_price"))
    else:
        outbound_min = _to_float(outbound_top[0].get("price")) if outbound_top else None
        return_min = _to_float(return_top[0].get("price")) if return_top else None
        total_min = (
            outbound_min + return_min
            if outbound_min is not None and return_min is not None
            else None
        )

    insight = None
    if outbound_min is not None and return_min is not None:
        total = outbound_min + return_min
        if outbound_min < return_min * 0.8:
            insight = f"鍘荤▼濂戒环浣嗚繑绋嬪亸璐碉紝鎬讳环楼{total:,.0f}"
        elif return_min < outbound_min * 0.8:
            insight = f"杩旂▼濂戒环浣嗗幓绋嬪亸璐碉紝鎬讳环楼{total:,.0f}"
        else:
            insight = f"鍘荤▼鍜岃繑绋嬩环鏍肩浉瀵瑰潎琛★紝鎬讳环楼{total:,.0f}"

    trend = analyze_roundtrip_trend(history)
    previous = trend.get("previous") if trend.get("available") else None
    budget_limits = same_day_budget_limits
    budget_price = (
        _to_float(best_combo.get("raw_passenger_total_price"))
        if best_combo and best_combo.get("mixed_cabin")
        else _to_float(best_combo.get("passenger_total_price")) if best_combo else None
    )
    if budget_price is None:
        budget_price = total_min
    budget_outbound_min = outbound_min
    budget_return_min = return_min
    passenger_pricing_for_budget = best_combo.get("passenger_pricing") if isinstance(best_combo, dict) else {}
    if isinstance(passenger_pricing_for_budget, dict):
        outbound_pricing = passenger_pricing_for_budget.get("outbound") or {}
        return_pricing = passenger_pricing_for_budget.get("return") or {}
        budget_outbound_min = _to_float(outbound_pricing.get("total")) or budget_outbound_min
        budget_return_min = _to_float(return_pricing.get("total")) or budget_return_min
        if best_combo.get("mixed_cabin"):
            budget_outbound_min = _to_float(outbound_pricing.get("raw_total")) or budget_outbound_min
            budget_return_min = _to_float(return_pricing.get("raw_total")) or budget_return_min
    budget_compare_scope = budget_limits.get("max_budget_compare_scope") or _budget_visible_scope(budget_scope, True)
    target_compare_scope = budget_limits.get("ideal_price_compare_scope") or _budget_visible_scope(target_budget_scope, True)
    budget_price_compare = None
    budget_outbound_compare = None
    budget_return_compare = None
    if best_combo and best_combo.get("mixed_cabin"):
        assert budget_compare_scope == "all_passengers_roundtrip", "混舱预算必须使用全员往返口径"
        budget_price_compare = _to_float(best_combo.get("raw_passenger_total_price"))
        budget_outbound_compare = budget_outbound_min
        budget_return_compare = budget_return_min
    elif best_combo and best_combo.get("outbound") and best_combo.get("return"):
        best_outbound_price = _to_float(best_combo.get("outbound_price") or (best_combo.get("outbound") or {}).get("price"))
        best_return_price = _to_float(best_combo.get("return_price") or (best_combo.get("return") or {}).get("price"))
        if best_outbound_price is not None and best_return_price is not None:
            budget_price_compare = price_in_scope(
                best_outbound_price,
                pricing_passengers,
                scope=budget_compare_scope,
                route_type=pricing_route_type,
                round_trip=True,
                return_per_person_oneway=best_return_price,
            )
            budget_outbound_compare = price_in_scope(
                best_outbound_price,
                pricing_passengers,
                scope=("all_passengers_oneway" if budget_compare_scope.startswith("all_") else "per_person_oneway"),
                route_type=pricing_route_type,
                round_trip=False,
            )
            budget_return_compare = price_in_scope(
                best_return_price,
                pricing_passengers,
                scope=("all_passengers_oneway" if budget_compare_scope.startswith("all_") else "per_person_oneway"),
                route_type=pricing_route_type,
                round_trip=False,
            )
    if budget_price_compare is None:
        budget_price_compare = budget_price
        budget_outbound_compare = budget_outbound_min
        budget_return_compare = budget_return_min
    price_analysis = analyze_roundtrip_prices(
        history,
        budget_price_compare,
        budget_outbound_compare,
        budget_return_compare,
        target_price=budget_limits.get("ideal_price_compare"),
        max_budget=budget_limits.get("max_budget_compare"),
        days_to_dept=outbound_analysis.get("days_to_dept"),
        budget_is_roundtrip=True,
    )
    combo_grades = [
        (best_combo.get("outbound") or {}).get("execution_grade"),
        (best_combo.get("return") or {}).get("execution_grade"),
    ]
    grade_order = {"A": 0, "B": 1, "C": 2, "D": 3}
    execution_grade = max(
        [grade for grade in combo_grades if grade],
        key=lambda grade: grade_order.get(grade, 2),
        default="C",
    )
    confidence_breakdown = calc_confidence(
        best_combo.get("outbound") or (outbound_top[0] if outbound_top else {}),
        {},
        history,
    )
    travel_profile = (
        outbound_analysis.get("travel_profile")
        or return_analysis.get("travel_profile")
        or build_travel_profile((outbound_analysis.get("user_preferences") or {}))
    )
    summary_target = budget_limits.get("ideal_price_compare")
    summary_budget = budget_limits.get("max_budget_compare")
    summary_budget_total = budget_limits.get("max_budget_total")
    decision_summary = generate_decision_summary(
        budget_price_compare,
        summary_target,
        summary_budget,
        confidence_breakdown,
        execution_grade,
    )
    buy_vs_wait_risk = calc_buy_vs_wait_risk(
        budget_price_compare,
        [row.get("total") for row in (history or []) if isinstance(row, dict)],
        outbound_analysis.get("days_to_dept"),
        summary_target,
        execution_grade,
    )
    combo_max_budget = summary_budget_total
    if combinations:
        excluded_roundtrip_combos = build_excluded_roundtrip_combos(
            outbound_analysis,
            return_analysis,
            budget_price,
            3,
            max_budget=combo_max_budget,
            constraints=combined_preferences,
            recommended_combo=combinations[0],
            passengers=pricing_passengers,
            route_type=pricing_route_type,
            emit_diagnostics=emit_diagnostics,
        )
    else:
        excluded_roundtrip_combos = build_excluded_roundtrip_combos(
            outbound_analysis,
            return_analysis,
            None,
            3,
            max_budget=combo_max_budget,
            constraints=combined_preferences,
            recommended_combo=None,
            passengers=pricing_passengers,
            route_type=pricing_route_type,
            emit_diagnostics=False,
            include_without_reference=True,
        )
        if emit_diagnostics:
            safe_log(
                "[排除诊断] 无推荐方案,"
                f"保留完整往返排除组合={len(excluded_roundtrip_combos)}"
            )

    mixed_history = {}
    if best_combo and best_combo.get("mixed_cabin"):
        mixed_tree = best_combo.get("mixed_cabin_pricing") or {}
        mixed_outbound = mixed_tree.get("outbound") or {}
        mixed_return = mixed_tree.get("return") or {}
        mixed_history = {
            "outbound": _to_float(mixed_outbound.get("raw_total")),
            "return": _to_float(mixed_return.get("raw_total")),
            "total": _to_float(mixed_tree.get("raw_total")),
            "scope": "all_passengers_roundtrip",
            "sources": ["juhe", "serpapi"],
        }

    return {
        "outbound_min": outbound_min,
        "return_min": return_min,
        "total_min": total_min,
        "passenger_total_min": (best_combo.get("passenger_total_price") if best_combo else None),
        "passenger_pricing": (best_combo.get("passenger_pricing") if best_combo else {}),
        "price_tiers": (best_combo.get("price_tiers") if best_combo else {}),
        "budget_scope": budget_scope,
        "target_price_scope": target_budget_scope,
        "budget_price": budget_price,
        "budget_price_compare": budget_price_compare,
        "budget_price_compare_scope": budget_compare_scope,
        "budget_limits": budget_limits,
        "max_combination": combinations[-1] if combinations else None,
        "top_combinations": combinations[:3],
        "same_day_round_trip": same_day_round_trip,
        "same_day_combos": same_day_combos[:3],
        "same_day_no_feasible_note": same_day_no_feasible_note,
        "same_day_time_conflict": same_day_time_conflict,
        "closest_same_day_outbound_options": closest_same_day_outbound_options,
        "same_day_alternatives": same_day_alternatives,
        "filter_counts": same_day_filter_counts,
        "same_day_return_window_debug": same_day_return_window_debug,
        "outbound_top3": [] if same_day_time_conflict else outbound_top,
        "return_top3": return_top,
        "insight": insight,
        "mix_match_tip": _mix_match_tip(combinations),
        "history": history or [],
        "trend": trend,
        "price_analysis": price_analysis,
        "decision_summary": decision_summary,
        "confidence_breakdown": confidence_breakdown,
        "travel_profile": travel_profile,
        "passenger_profile": travel_profile.get("passenger_profile"),
        "passenger_rules": travel_profile.get("passenger_rules"),
        "travel_profile_explanation": travel_profile_explanation(travel_profile),
        "recommendation_basis": build_recommendation_basis(travel_profile),
        "alert_policy": build_alert_policy(travel_profile),
        "buy_vs_wait_risk": buy_vs_wait_risk,
        "excluded_roundtrip_combos": excluded_roundtrip_combos,
        "previous": previous,
        **({"mixed_cabin_matching": mixed_cabin_matching} if mixed_cabin_active else {}),
        **({"mixed_cabin_history": mixed_history} if mixed_history else {}),
        "advice": _roundtrip_budget_advice(budget_price, summary_target, summary_budget, budget_is_roundtrip=True),
    }


def select_recommendations(economy_flights, business_flights, mode: str = "balanced"):
    """Select push options: up to four economy options and one business option."""
    economy_flights = [
        flight
        for flight in economy_flights
        if not flight.get("reference_only") and has_enough_detail(flight)
    ]
    business_flights = [
        flight
        for flight in business_flights
        if not flight.get("reference_only") and has_enough_detail(flight)
    ]

    def max_layover_minutes(flight: dict) -> int:
        return max(
            (int(layover.get("wait_minutes") or 0) for layover in flight.get("layovers", [])),
            default=0,
        )

    def sort_key(flight: dict):
        preference_penalty = flight.get("preference_penalty", 0) or 0
        if flight.get("final_score") is not None:
            return (
                -float(flight.get("final_score") or 0),
                preference_penalty,
                _to_float(flight.get("price")) or 99999,
            )
        if mode == "budget":
            return (
                preference_penalty,
                _to_float(flight.get("price")) or 99999,
                flight.get("total_duration_min", 99999),
            )
        if mode == "fast":
            return (
                preference_penalty,
                flight.get("total_duration_min", 99999),
                _to_float(flight.get("price")) or 99999,
            )
        if mode == "comfort":
            return (
                preference_penalty,
                flight.get("stops", 99),
                max_layover_minutes(flight),
                flight.get("total_duration_min", 99999),
                _to_float(flight.get("price")) or 99999,
            )
        return (
            preference_penalty,
            flight.get("value_score", 99999),
            _to_float(flight.get("price")) or 99999,
            flight.get("total_duration_min", 99999),
        )

    eco_recs = []
    seen_routes = set()

    for flight in sorted(economy_flights, key=sort_key):
        route = flight.get("route_summary", "")
        if route not in seen_routes and len(eco_recs) < 4:
            eco_recs.append(flight)
            seen_routes.add(route)

    business_rec = None
    if business_flights:
        business_rec = min(
            business_flights, key=lambda item: _to_float(item.get("price")) or 99999
        )

    return eco_recs, business_rec






