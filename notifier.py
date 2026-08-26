"""PushPlus notification helpers."""

from __future__ import annotations

import os
import re
import json
import time
import html
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import quote, quote_plus

import httpx

from airports import (
    AIRPORT_CITY,
    AIRPORT_CITY_EN,
    get_airport_city,
    get_airport_city_en,
    get_airport_name,
    get_airport_timezone,
)
from channels import CHANNEL_INFO
from constraint_fingerprint import (
    constraint_fingerprint,
    short_constraint_fingerprint,
)
from constraint_summary import format_constraint_summary
from domestic_fare_rules import get_aircraft_name
from detail_access import canonical_detail_uuid
from airlines import classify_itinerary, classify_segment
from flight_combo_utils import normalize_combo
from log_utils import safe_log
from analyzer import (
    MIN_SAMPLE_FOR_PRICE_SIGNAL,
    build_execution_advice,
    build_budget_gap,
    build_cabin_policy_summary,
    build_next_step_guidance,
    build_no_result_alternatives,
    build_no_result_diagnosis,
    build_price_signal,
    build_recommendation_basis,
    build_travel_profile,
    calculate_price_references,
    calc_confidence,
    analyze_departure_feasibility,
    analyze_price_calendar,
    classify_plan_tier,
    determine_push_type,
    evaluate_purchase_budget,
    generate_decision_summary,
    generate_trend_summary,
    get_total_passengers,
    multi_window_analysis,
    price_position_description,
    travel_profile_explanation,
    waiting_risk_description,
)
from mixed_cabin import MIXED_CABIN_DISCLOSURE
from notification_config import (
    DEFAULT_NOTIFICATION_PRIVACY_LEVEL,
    resolve_notification_privacy_level,
)
from price_estimator import (
    build_display_prices,
    build_passenger_price_breakdown,
    build_price_tiers,
    calc_total_price_for_passengers,
    round_display_price,
)
from pricing import assert_same_caliber, budget_to_pp, caliber_label, itinerary_price_pp, passenger_rate_sum, price_in_scope
from project_time import SHANGHAI_TZ
from source_profiles import normalize_route_type, retired_listing_sources
from provenance import (
    attach_payload_provenance,
    format_dual_source_agreement,
    format_micro_provenance,
    history_observation_window,
    replace_micro_provenance,
)
from sources.aggregator import MERGE_PRICE_STRATEGY, PRICE_GAP_DISCLOSE_PCT
from tcurve import TCURVE_MIN_CELLS, select_anchor_points
from storage import (
    get_lowest_price_history,
    get_last_push_price,
    get_last_push_snapshot,
    get_roundtrip_price_history,
    save_last_push_price,
    save_push_snapshot,
)
from plan_tracker import save_pushed_plans, track_plan_status
from pushplus_sections import (
    PUSHPLUS_COMPACT_CHARS,
    PushRender,
    PushSection,
    detail_link_html,
    prepare_push_render,
    render_push_render,
    valid_detail_url,
)


_RENDER_LOG_CHANNEL: ContextVar[str] = ContextVar(
    "notification_render_log_channel",
    default="渲染",
)


def _render_log_prefix(label: str) -> str:
    return f"[{label}][{_RENDER_LOG_CHANNEL.get()}]"


def _with_render_log_channel(channel: str):
    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            token = _RENDER_LOG_CHANNEL.set(channel)
            try:
                return func(*args, **kwargs)
            finally:
                _RENDER_LOG_CHANNEL.reset(token)

        return wrapped

    return decorator


BUY_SIGNALS = {"strong_buy", "buy", "buy_now"}
BASE_DIR = Path(__file__).parent
NOTIFICATIONS_LOG = BASE_DIR / "data" / "notifications_log.txt"

TRANSIT_VISA_RISK_AIRPORTS = {
    "JFK": ("美国", "美国转机通常需要核实签证/入境许可要求"),
    "EWR": ("美国", "美国转机通常需要核实签证/入境许可要求"),
    "LAX": ("美国", "美国转机通常需要核实签证/入境许可要求"),
    "SFO": ("美国", "美国转机通常需要核实签证/入境许可要求"),
    "SEA": ("美国", "美国转机通常需要核实签证/入境许可要求"),
    "ORD": ("美国", "美国转机通常需要核实签证/入境许可要求"),
    "DFW": ("美国", "美国转机通常需要核实签证/入境许可要求"),
    "LHR": ("英国", "英国转机可能涉及空侧/陆侧过境规则"),
    "LGW": ("英国", "英国转机可能涉及空侧/陆侧过境规则"),
    "STN": ("英国", "英国转机可能涉及空侧/陆侧过境规则"),
    "YVR": ("加拿大", "加拿大转机可能需要核实过境签或eTA要求"),
    "YYZ": ("加拿大", "加拿大转机可能需要核实过境签或eTA要求"),
}

AIRPORT_UTC_OFFSETS = {
    "PVG": 8, "SHA": 8, "PEK": 8, "PKX": 8, "CAN": 8, "SZX": 8,
    "HKG": 8, "MFM": 8, "TPE": 8, "TSA": 8,
    "NRT": 9, "HND": 9, "KIX": 9, "ITM": 9, "ICN": 9, "GMP": 9,
    "SIN": 8, "BKK": 7, "DMK": 7, "DXB": 4, "DWC": 4, "DOH": 3,
    "LHR": 0, "LGW": 0, "STN": 0, "CDG": 1, "ORY": 1, "FRA": 1, "AMS": 1,
    "JFK": -5, "EWR": -5, "LGA": -5, "IAD": -5, "DCA": -5,
    "LAX": -7, "SFO": -7, "OAK": -7, "SJC": -7, "SEA": -7,
    "YVR": -8, "YYZ": -5,
}


def should_notify(analysis: dict, prev_signal: str | None) -> tuple[bool, str | None]:
    """Decide whether an analysis result should trigger a notification."""
    signal = analysis.get("signal")
    if signal in BUY_SIGNALS and prev_signal not in BUY_SIGNALS:
        return True, "signal_upgrade"
    if analysis.get("days_to_dept") in [30, 21, 14, 7]:
        return True, "milestone"
    if (
        analysis.get("current_price", 0) <= analysis.get("min_seen", 0)
        and analysis.get("data_points", 0) >= 6
    ):
        return True, "new_low"
    if analysis.get("target_vs_cheapest", 0) > 1000:
        return True, "cheaper_alt"
    return False, None


def _generic_long_pushplus_warning() -> str:
    return (
        "<b>通知内容过长</b><br>"
        "本次通用告警未直接展开,完整原文已写入本地通知日志。"
    )


def _prepare_pushplus_content(content: str | PushRender) -> str:
    if isinstance(content, PushRender):
        prepared = prepare_push_render(content)
        print(
            f"[推送] 结构化消息: mode={prepared.mode} "
            f"长度={len(prepared.content)} 小节={list(prepared.kept_section_ids)}"
        )
        return prepared.content

    raw_content = str(content or "")
    print(f"[推送] 消息长度: {len(raw_content)} 字符")
    if len(raw_content) <= PUSHPLUS_COMPACT_CHARS:
        return raw_content
    warning = _generic_long_pushplus_warning()
    print(f"[推送] 通用消息异常过长,改发安全告警模板: {len(warning)} 字符")
    return warning

def _post_pushplus(pushplus_token: str, title: str, content: str):
    resp = httpx.post(
        "https://www.pushplus.plus/send",
        json={
            "token": pushplus_token,
            "title": title,
            "content": content,
            "template": "html",
        },
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=30,
    )
    if not resp.text:
        print(f"[推送] PushPlus返回空响应，消息可能过长({len(content)}字符)")
        return None
    try:
        return resp.json()
    except json.JSONDecodeError:
        print(f"[推送] JSON解析失败，响应内容: {resp.text[:200]}")
        print(f"[推送] 消息长度: {len(content)}字符，可能超出限制")
        return None


DISCLAIMER = "以上内容基于历史价格数据分析，仅供参考。\n实际购买请以航司或OTA官网价格为准。"
def _round_push_price(value):
    """兼容旧调用；实际舍入唯一委托金额树的 ROUND_HALF_UP 入口。"""
    return round_display_price(value)


def format_price(price) -> str:
    """Format a CNY price."""
    try:
        value = float(price)
    except (TypeError, ValueError):
        return "暂无报价"
    if value <= 0:
        return "暂无报价"
    rounded = _round_push_price(value)
    return f"¥{rounded:,}" if rounded is not None else "暂无报价"


def _has_valid_price(price) -> bool:
    try:
        return float(price) > 0
    except (TypeError, ValueError):
        return False


def _price_text(price) -> str:
    return format_price(price)


def _caliber_label(scope, passengers=None, route_type=None) -> str:
    try:
        return caliber_label(scope, passengers, route_type)
    except Exception:
        return str(scope or "\u672a\u77e5\u53e3\u5f84")


def _price_text_with_caliber(price, scope, passengers=None, route_type=None) -> str:
    """Format an already-scoped price and append its visible caliber."""
    text = _price_text(price)
    if text == "\u6682\u65e0\u62a5\u4ef7":
        return text
    return f"{text} {_caliber_label(scope, passengers, route_type)}"


_SHORT_CALIBER_LABELS = {
    "per_person_oneway": "单人单程",
    "per_person_roundtrip": "单人往返",
    "all_passengers_oneway": "全员单程",
    "all_passengers_roundtrip": "全员往返",
}


def _short_caliber_label(scope) -> str:
    return _SHORT_CALIBER_LABELS.get(str(scope or "").strip().lower(), "")


def _price_text_with_parenthesized_caliber(price, scope) -> str:
    text = _price_text(price)
    label = _short_caliber_label(scope)
    return f"{text}({label})" if label and text != "暂无报价" else text


def _estimated_price_subject(price, scope) -> str:
    price_text = _price_text_with_parenthesized_caliber(price, scope)
    label = _short_caliber_label(scope)
    if label.startswith("单人"):
        return f"单人参考价(成人口径)约{price_text}"
    if label.startswith("全员"):
        return f"当前预估实付总价{price_text}"
    return f"当前预估实付价{price_text}"


def _budget_purchase_condition(limit, scope) -> str:
    price_text = _price_text_with_parenthesized_caliber(limit, scope)
    if _short_caliber_label(scope).startswith("单人"):
        return f"支付页单人价≤{price_text}，且含托运行李"
    return f"支付页总价≤{price_text}，且含托运行李"


def _scoped_price_text_from_pp(
    per_person_oneway,
    passengers=None,
    scope="per_person_oneway",
    route_type=None,
    round_trip=False,
    return_per_person_oneway=None,
) -> str:
    """Format from the only storage unit: per-person one-way price."""
    scoped = price_in_scope(
        per_person_oneway,
        passengers or {"adult": 1, "child": 0, "elderly": 0, "infant": 0},
        scope=scope,
        route_type=route_type,
        round_trip=round_trip,
        return_per_person_oneway=return_per_person_oneway,
    )
    return _price_text_with_caliber(scoped, scope, passengers, route_type)



def _pricing_passengers(pricing: dict | None, fallback_count=None) -> dict:
    pricing = pricing if isinstance(pricing, dict) else {}
    passengers = pricing.get("passengers")
    if isinstance(passengers, dict) and any(_to_float(v) for v in passengers.values()):
        return passengers
    label = str(pricing.get("passenger_label") or "")
    counts = {
        "adult": sum(int(n) for n in re.findall(r"(\d+)\u6210\u4eba", label)),
        "child": sum(int(n) for n in re.findall(r"(\d+)\u513f\u7ae5", label)),
        "elderly": sum(int(n) for n in re.findall(r"(\d+)\u8001\u4eba", label)),
        "infant": sum(int(n) for n in re.findall(r"(\d+)\u5a74\u513f", label)),
    }
    if any(counts.values()):
        return counts
    count = _to_float(pricing.get("passenger_count") or fallback_count)
    if not count:
        factor = _to_float(pricing.get("factor"))
        if factor and float(factor).is_integer():
            count = factor
    if count and count > 0:
        return {"adult": int(count), "child": 0, "elderly": 0, "infant": 0}
    return {"adult": 1, "child": 0, "elderly": 0, "infant": 0}


def _plan_price_context(plan: dict | None) -> tuple[dict, str]:
    plan = plan or {}
    pricing = plan.get("passenger_pricing") or {}
    passengers = _pricing_passengers(pricing)
    price_tiers = plan.get("price_tiers") if isinstance(plan.get("price_tiers"), dict) else {}
    pricing_tiers = pricing.get("price_tiers") if isinstance(pricing.get("price_tiers"), dict) else {}
    route_type = str(
        plan.get("route_type")
        or pricing.get("route_type")
        or price_tiers.get("route_type")
        or pricing_tiers.get("route_type")
        or ""
    )
    return passengers, route_type


def _same_day_price_context(item: dict | None, payload: dict | None = None) -> tuple[dict, str]:
    item = item or {}
    payload = payload or {}
    pricing = item.get("passenger_pricing") or payload.get("passenger_pricing") or {}
    passengers = _pricing_passengers(pricing)
    route_type = str(item.get("route_type") or payload.get("route_type") or pricing.get("route_type") or "")
    return passengers, route_type


def _scoped_price_text_from_legs(outbound_price, return_price, passengers=None, route_type=None, scope="per_person_roundtrip") -> str:
    return _scoped_price_text_from_pp(
        outbound_price,
        passengers=passengers,
        scope=scope,
        route_type=route_type,
        round_trip=True,
        return_per_person_oneway=return_price,
    )



def _plan_leg_price_text(plan: dict | None, price) -> str:
    passengers, route_type = _plan_price_context(plan)
    return _scoped_price_text_from_pp(price, passengers, "per_person_oneway", route_type)


def _plan_roundtrip_price_text(plan: dict | None, scope: str | None = None) -> str:
    plan = plan or {}
    passengers, route_type = _plan_price_context(plan)
    outbound = plan.get("outbound_price") if plan.get("outbound_price") is not None else (plan.get("outbound_flight") or {}).get("price")
    ret = plan.get("return_price") if plan.get("return_price") is not None else (plan.get("return_flight") or {}).get("price")
    if scope is None:
        scope = "all_passengers_roundtrip" if _passenger_pricing_applies(plan.get("passenger_pricing")) else "per_person_roundtrip"
    return _scoped_price_text_from_legs(outbound, ret, passengers, route_type, scope)


def _display_price_tree_for_item(item: dict | None) -> dict:
    item = item or {}
    mixed_tree = item.get("mixed_cabin_pricing")
    if not isinstance(mixed_tree, dict):
        pricing = item.get("passenger_pricing") or {}
        mixed_tree = pricing if isinstance(pricing, dict) and pricing.get("mixed_cabin") else {}
    if mixed_tree.get("mixed_cabin"):
        return mixed_tree
    passengers, route_type = _plan_price_context(item)
    outbound_flight = item.get("outbound_flight") or item.get("outbound") or item.get("main_flight") or item.get("flight") or {}
    return_flight = item.get("return_flight") or item.get("return") or {}
    outbound_unit = _to_float(
        item.get("outbound_price")
        if item.get("outbound_price") is not None
        else outbound_flight.get("price")
    )
    return_unit = _to_float(
        item.get("return_price")
        if item.get("return_price") is not None
        else return_flight.get("price")
    )
    if outbound_unit is None:
        outbound_unit = _to_float(item.get("single_adult_price") or item.get("price"))
    if outbound_unit is None:
        return {}
    is_roundtrip = bool(item.get("is_roundtrip") or return_flight or return_unit is not None)
    return build_display_prices(
        outbound_unit,
        return_unit if is_roundtrip and return_unit is not None else None,
        passengers,
        route_type,
    )


def _log_card_price_consistency(
    item: dict,
    card_label: str,
    displayed_total=None,
    reference_total=None,
    displayed_difference=None,
) -> None:
    tree = _display_price_tree_for_item(item)
    legs = [leg for leg in (tree.get("outbound"), tree.get("return")) if isinstance(leg, dict)]
    if not legs:
        safe_log(
            f"{_render_log_prefix('口径校验')} card={card_label} "
            "成分和=空 段合计=空 总价=空 差价=空 一致=True"
        )
        return
    component_sum = sum(sum(part.get("total") or 0 for part in leg.get("parts") or []) for leg in legs)
    leg_sum = sum(leg.get("total") or 0 for leg in legs)
    canonical_total = tree.get("total")
    total_value = canonical_total if displayed_total is None else round_display_price(displayed_total)
    expected_difference = None
    if reference_total is not None and canonical_total is not None:
        expected_difference = round_display_price(reference_total) - canonical_total
    difference_value = expected_difference if displayed_difference is None else round_display_price(displayed_difference)
    consistent = component_sum == leg_sum == canonical_total == total_value
    if expected_difference is not None:
        consistent = consistent and difference_value == expected_difference
    safe_log(
        f"{_render_log_prefix('口径校验')} card={card_label} "
        f"成分和={component_sum} 段合计={leg_sum} "
        f"总价={canonical_total} 差价={difference_value if difference_value is not None else '空'} "
        f"一致={consistent}"
    )


def _log_recommended_total_consistency(plan: dict) -> None:
    tree = _display_price_tree_for_item(plan)
    if not tree:
        return
    panel_total = tree.get("total")
    raw_total = tree.get("raw_total")
    if panel_total is None or raw_total is None:
        return
    legs = [leg for leg in (tree.get("outbound"), tree.get("return")) if isinstance(leg, dict)]
    rounding_item_count = sum(
        max(0, int(part.get("count") or 0))
        for leg in legs
        for part in (leg.get("parts") or [])
        if isinstance(part, dict)
    )
    drift = float(panel_total) - float(raw_total)
    tolerance = 0.5 * rounding_item_count
    consistent = abs(drift) <= tolerance + 1e-9
    drift_text = f"{drift:+.2f}".rstrip("0").rstrip(".")
    if "." not in drift_text:
        drift_text += ".0"
    safe_log(
        f"{_render_log_prefix('口径校验')} 推荐总价 "
        f"面板={panel_total} 原始浮点={raw_total} "
        f"漂移={drift_text} 分项数={rounding_item_count} "
        f"允差={tolerance:.1f} 一致={consistent}"
    )


def _valid_price_float(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _preference_value(route_info: dict | None, analysis_result: dict | None, key: str, default=None):
    route_info = route_info or {}
    analysis_result = analysis_result or {}
    for container in (
        route_info,
        route_info.get("hard_constraints") or {},
        route_info.get("soft_preferences") or {},
        route_info.get("constraints") or {},
        route_info.get("preferences") or {},
        analysis_result,
        analysis_result.get("hard_constraints") or {},
        analysis_result.get("soft_preferences") or {},
        analysis_result.get("constraints") or {},
        analysis_result.get("preferences") or {},
    ):
        if isinstance(container, dict) and key in container and container.get(key) is not None:
            return container.get(key)
    return default


def _time_only(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"(\d{1,2}:\d{2})", text)
    return match.group(1) if match else text


def _append_status_tag(text: str, tag: str) -> str:
    parts = [part.strip() for part in str(text or "").split("|") if part.strip()]
    if tag and tag not in parts:
        parts.append(tag)
    return " | ".join(parts)


def _flight_lcc_summary(flight: dict | None) -> dict:
    return classify_itinerary(flight or {})


def _combo_lcc_summary(*flights: dict | None) -> dict:
    segments = []
    for flight in flights:
        if not isinstance(flight, dict) or not flight:
            continue
        flight_segments = flight.get("segments")
        if isinstance(flight_segments, list) and flight_segments:
            segments.extend(
                segment for segment in flight_segments if isinstance(segment, dict)
            )
        else:
            segments.append(flight)
    return classify_itinerary({"segments": segments})


def _lcc_status_tag_for_flight(flight: dict | None) -> str:
    return "含廉航段" if _flight_lcc_summary(flight).get("has_lcc") else ""


def _flight_status_tags(flight: dict | None, route_info: dict | None = None, analysis_result: dict | None = None) -> str:
    flight = flight or {}
    domestic_tags = [str(tag) for tag in flight.get("domestic_tags") or [] if tag]
    if domestic_tags:
        return _append_status_tag(
            " | ".join(domestic_tags[:4]),
            _lcc_status_tag_for_flight(flight),
        )
    price = _to_float(flight.get("price"))
    target = _to_float(_preference_value(route_info, analysis_result, "target_price")) if route_info or analysis_result else None
    if target and price:
        price_tag = "接近理想" if price <= target * 1.05 else "中等"
    else:
        price_tag = "价格待判断"
    availability = flight.get("availability") or {}
    status = availability.get("status")
    buy_tag = "可购买" if status == "likely_available" else "待确认"
    confidence_tag = "置信度中"
    risk = (flight.get("execution_risk") or {}).get("level") or (flight.get("transfer_risk") or {}).get("level")
    risk_tag = {"low": "风险低", "medium": "风险中", "high": "风险高"}.get(risk, "风险待确认")
    return _append_status_tag(
        f"{price_tag} | {buy_tag} | {confidence_tag} | {risk_tag}",
        _lcc_status_tag_for_flight(flight),
    )


def _status_risk_label(flight: dict | None) -> str:
    flight = flight or {}
    risk = (flight.get("execution_risk") or {}).get("level") or (flight.get("transfer_risk") or {}).get("level")
    return {"low": "风险低", "medium": "风险中", "high": "风险高"}.get(risk, "风险中")


def _status_availability_label(flight: dict | None) -> str:
    """Return a compact availability label from a flight dict."""
    flight = flight or {}
    buyability = flight.get("buyability") or {}
    if isinstance(buyability, dict):
        label = str(buyability.get("label") or "").strip()
        note = str(buyability.get("note") or "").strip()
        if label and note:
            return f"{label}({note})"
        if label:
            return label

    availability = flight.get("availability") or {}
    if not isinstance(availability, dict):
        return ""

    status = availability.get("status")
    if status == "likely_available":
        return "可购买"
    if status == "possibly_available":
        return "可买性待确认"
    if status == "needs_refresh":
        return "需刷新"
    if status == "invalid":
        return "价格异常"

    label = str(availability.get("label") or "").strip()
    if "刷新" in label:
        return "需刷新"
    if "大概率" in label or "可购买" in label:
        return "可购买"
    if label:
        return label
    return ""


def _human_recommendation_text(flight: dict | None, route_info: dict | None = None, analysis_result: dict | None = None) -> str:
    price = _to_float((flight or {}).get("price"))
    target = _to_float(_preference_value(route_info, analysis_result, "target_price"))
    if price and target and price <= target * 1.05:
        return f"支付页≤{_price_text(price * 1.05)}且票规可接受时，可购买前验证"
    return "点击购买页确认最终价格和票规后再判断"


def _source_price_entries_for_display(flight: dict | None) -> list[dict]:
    entries = (
        (flight or {}).get("source_price_details")
        or (flight or {}).get("source_prices")
        or (flight or {}).get("prices_by_source")
        or []
    )
    if isinstance(entries, dict):
        entries = [
            {"source": source, "price": price}
            for source, price in entries.items()
        ]

    normalized = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        price = _valid_price_float(entry.get("price"))
        if price is None:
            continue
        normalized.append(
            {
                "source": entry.get("source") or entry.get("data_source") or "unknown",
                "price": price,
            }
        )
    return normalized


_SOURCE_CHANNEL_LABELS = {
    "hasdata": "Google",
    "serpapi": "Google",
    "searchapi": "Google",
    "juhe": "OTA",
}
_SOURCE_CHANNEL_ORDER = {"Google": 0, "OTA": 1}


def _source_channel_label(source: str | None) -> str:
    key = str(source or "unknown").strip().lower()
    return _SOURCE_CHANNEL_LABELS.get(key, key.upper() if key else "UNKNOWN")


def _payload_source_channel_rows(
    flight: dict | None,
    direction: str,
) -> list[dict]:
    """把同一航段的分源单人单程价转换为渠道对照行。"""
    flight = flight or {}
    by_provider: dict[str, dict] = {}
    for entry in _source_price_entries_for_display(flight):
        source = str(entry.get("source") or "unknown").strip().lower()
        provider = _source_channel_label(source)
        price = _valid_price_float(entry.get("price"))
        if price is None:
            continue
        current = by_provider.get(provider)
        if current is None:
            by_provider[provider] = {
                "source": source,
                "price": price,
                "sources": [source],
            }
            continue
        if source not in current["sources"]:
            current["sources"].append(source)
        if price < current["price"]:
            current["source"] = source
            current["price"] = price

    if len(by_provider) < 2:
        return []

    selected_source = str(flight.get("price_source") or "").strip().lower()
    selected_price = _valid_price_float(flight.get("price"))
    combo = normalize_combo(flight.get("flight_combo") or flight.get("flight_no") or "")
    direction_label = {"outbound": "去程", "return": "返程", "main": "去程"}.get(
        direction,
        direction,
    )
    rows = []
    for provider, item in sorted(
        by_provider.items(),
        key=lambda pair: (_SOURCE_CHANNEL_ORDER.get(pair[0], 9), pair[0]),
    ):
        selected = selected_source in item["sources"]
        if not selected and not selected_source and selected_price is not None:
            selected = abs(item["price"] - selected_price) < 0.01
        rows.append(
            {
                "label": f"{direction_label} {combo} · {provider}".strip(),
                "value": item["price"],
                "scope": "oneway",
                "price_scope": "per_person_oneway",
                "direction": direction,
                "flight_combo": combo,
                "source": item["source"],
                "provider": provider,
                "selected": selected,
            }
        )
    return rows


def _should_disclose_source_price_gap(anomaly: dict | None) -> bool:
    anomaly = anomaly or {}
    diff_pct = _to_float(anomaly.get("diff_pct"))
    if diff_pct is None or diff_pct <= PRICE_GAP_DISCLOSE_PCT:
        return False

    source_prices = {}
    for entry in anomaly.get("sources") or []:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source") or "").strip().lower()
        price = _valid_price_float(entry.get("price"))
        if source and price is not None:
            source_prices[source] = price

    google_price = source_prices.get("hasdata")
    ota_price = source_prices.get("juhe")
    return google_price is not None and ota_price is not None


def _source_price_anomaly_map(anomalies: list[dict] | None) -> dict[str, dict]:
    result = {}
    for anomaly in anomalies or []:
        if not isinstance(anomaly, dict) or not _should_disclose_source_price_gap(anomaly):
            continue
        combo = normalize_combo(anomaly.get("flight_combo") or "")
        if not combo:
            continue
        current = result.get(combo)
        if current is None or (_to_float(anomaly.get("diff_pct")) or 0) > (
            _to_float(current.get("diff_pct")) or 0
        ):
            result[combo] = anomaly
    return result


def _attach_source_price_anomalies_to_plans(
    plans: list[dict],
    outbound_anomalies: list[dict] | None,
    return_anomalies: list[dict] | None,
) -> list[dict]:
    outbound_map = _source_price_anomaly_map(outbound_anomalies)
    return_map = _source_price_anomaly_map(return_anomalies)

    def attach(flight: dict | None, anomaly_map: dict[str, dict]) -> None:
        if not isinstance(flight, dict):
            return
        combo = normalize_combo(flight.get("flight_combo") or flight.get("flight_no") or "")
        anomaly = anomaly_map.get(combo)
        if anomaly:
            flight["source_price_anomaly"] = dict(anomaly)

    for plan in plans or []:
        if plan.get("is_roundtrip"):
            attach(plan.get("outbound_flight"), outbound_map)
            attach(plan.get("return_flight"), return_map)
        else:
            attach(plan.get("main_flight") or plan.get("flight"), outbound_map)
    return plans


def _flight_price_text(flight: dict) -> str:
    entries = _source_price_entries_for_display(flight)
    prices = [entry["price"] for entry in entries]
    own_price = _valid_price_float(flight.get("price"))
    if own_price is not None and not prices:
        prices = [own_price]

    if not prices:
        return _price_text(flight.get("price"))

    low = min(prices)
    high = max(prices)
    if len(set(round(price, 2) for price in prices)) > 1:
        price_part = f"{_price_text(low)} ~ {_price_text(high)} (多平台报价)"
    else:
        price_part = _price_text(low)

    source = _compact_source_label(flight)
    collected_at = _collected_time_text(flight)
    return f"{price_part} (来源:{source}, 采集于{collected_at})"


def _compact_source_label(flight: dict | None) -> str:
    source = str((flight or {}).get("data_source") or (flight or {}).get("source") or "unknown")
    labels = []
    for part in source.split("+"):
        key = part.strip()
        if not key:
            continue
        labels.append(SOURCE_LABELS.get(key, key))
    return "+".join(labels) if labels else "unknown"


def _price_estimate_data(flight: dict) -> dict:
    estimate = flight.get("price_estimate") or {}
    return estimate if isinstance(estimate, dict) else {}


def _estimated_price_value(flight: dict):
    estimate = _price_estimate_data(flight)
    estimated = _valid_price_float(estimate.get("transaction_price"))
    if estimated is None:
        estimated = _valid_price_float(estimate.get("estimated_price"))
    if estimated is not None and estimated > 0:
        return estimated
    return _valid_price_float(flight.get("price"))


def _price_estimate_summary_lines(flight: dict) -> list[str]:
    estimate = _price_estimate_data(flight)
    if not estimate:
        return []

    display_price = _valid_price_float(estimate.get("display_price")) or _valid_price_float(
        flight.get("price")
    )
    transaction_price = _valid_price_float(estimate.get("transaction_price"))
    if transaction_price is None:
        transaction_price = _valid_price_float(estimate.get("estimated_price"))
    transaction_price = transaction_price or display_price
    if not display_price or not transaction_price:
        return []

    source = str(flight.get("data_source") or flight.get("source") or "").lower()
    if "juhe" in source:
        lines = [f"💰 票面价：{_price_text(display_price)}"]
        lines.append("　实付说明：支付页通常另含机建、燃油及平台服务费")
        note = flight.get("price_note")
        if note:
            lines.append(f"　价格口径：{note}")
        if abs(transaction_price - display_price) >= 1:
            lines.append(f"💳 预估交易价：{_price_text(transaction_price)}")
        return lines

    extra_items = [
        item for item in estimate.get("extra_items") or [] if isinstance(item, dict)
    ]
    is_lcc = bool(estimate.get("is_lcc"))
    theory_label = "理论最低价"
    theory_suffix = "（不含行李）" if is_lcc and extra_items else ""
    lines = [
        f"💰 {theory_label}：{_price_text(display_price)}{theory_suffix}",
        f"💳 预估交易价：{_price_text(transaction_price)}",
    ]

    if not extra_items:
        lines.append("　已包含：税费 + 燃油 + 机建 + 23kg免费托运")
        lines.append("　无额外费用 ✅")
        return lines

    lines.append("　已包含：税费 + 燃油 + 机建")
    lines.append("　额外费用：")
    for item in extra_items:
        name = item.get("name", "额外费用")
        amount = _valid_price_float(item.get("amount")) or 0
        note = item.get("note")
        suffix = f"（{note}）" if note else ""
        lines.append(f"　+ {name} {_price_text(amount)}{suffix}")
    if is_lcc:
        lines.append("　⚠️ 廉航展示价不含行李，实际支付更高")
    return lines


def _round_trip_price_estimate_line(flight: dict) -> str:
    display_price = _valid_price_float(flight.get("price"))
    estimated_price = _estimated_price_value(flight)
    if not display_price or not estimated_price:
        return _price_text(display_price)
    if abs(estimated_price - display_price) < 1:
        return f"鐞嗚{_price_text(display_price)} 鈫?浜ゆ槗{_price_text(estimated_price)}"
    return (
        f"鐞嗚{_price_text(display_price)} 鈫?"
        f"浜ゆ槗{_price_text(estimated_price)}"
    )


def _price_discrepancy_notice(flight: dict) -> str:
    prices = [entry["price"] for entry in _source_price_entries_for_display(flight)]
    if len(prices) < 2:
        return ""
    low = min(prices)
    high = max(prices)
    if low > 0 and (high - low) / low > 0.10:
        return "鈿狅笍 鍚勬暟鎹簮浠锋牸宸紓杈冨ぇ锛屽缓璁骞冲彴姣斾环"
    return ""


def _format_price(value) -> str:
    return format_price(value).replace("楼", "")


def percentile_to_words(pct) -> str:
    """把分位数翻译成普通用户能理解的描述。"""
    if pct is None:
        return "样本还不够多"
    pct = float(pct)
    if pct < 10:
        return "非常少见的低价"
    if pct < 20:
        return "很便宜"
    if pct < 35:
        return "比大多数时候便宜"
    if pct < 50:
        return "中等偏低"
    if pct < 65:
        return "中等水平"
    if pct < 80:
        return "略偏贵"
    return "比较贵"

def city_name(iata_code) -> str:
    """IATA代码转中文机场名，显示为 中文名(IATA)。"""
    code = str(iata_code or "").strip().upper()
    if not code:
        return ""
    name = get_airport_name(code)
    return f"{name}({code})" if name and name != code else code


def format_route_summary(route_summary) -> str:
    """Replace IATA codes in a route summary with 中文名(IATA)."""
    text = str(route_summary or "")
    return re.sub(r"\b[A-Z]{3}\b", lambda match: city_name(match.group(0)), text)


def _route_codes(analysis: dict) -> list[str]:
    route_summary = ""
    target = analysis.get("target") or {}
    cheapest_alt = analysis.get("cheapest_alt") or {}
    for source in [target, cheapest_alt, analysis]:
        route_summary = source.get("route_summary") or route_summary
    if route_summary:
        codes = re.findall(r"\b[A-Z]{3}\b", route_summary)
        if codes:
            return codes
    route = analysis.get("route", "")
    return [part.strip() for part in route.split("-") if part.strip()]


def _route_info(analysis: dict, include_stop: bool = True) -> str:
    codes = _route_codes(analysis)
    if not codes:
        return analysis.get("route", "-")
    origin = city_name(codes[0])
    dest = city_name(codes[-1])
    if include_stop and len(codes) > 2:
        return f"{origin} → {dest}（{city_name(codes[1])}转机）"
    if include_stop and analysis.get("stopover_city"):
        return f"{origin} → {dest}（{city_name(analysis['stopover_city'])}转机）"
    return f"{origin} → {dest}"


def _savings(analysis: dict) -> float:
    price = analysis.get("current_price")
    avg_price = analysis.get("avg_price")
    if price is None or avg_price is None:
        return 0
    return max(float(avg_price) - float(price), 0)


def _google_comparison(analysis: dict) -> str:
    lines = []
    typical_range = analysis.get("google_typical_range") or []
    if len(typical_range) >= 2:
        lines.append(
            "Google评估这条航线的正常价格在"
            f"{format_price(typical_range[0])}-{format_price(typical_range[1])}之间"
        )
    level = analysis.get("google_level")
    if level == "low":
        lines.append("目前整体处于低价区")
    elif level == "typical":
        lines.append("目前整体价格处于常见范围")
    elif level == "high":
        lines.append("目前整体价格偏高")
    return "\n".join(f"- {line}" for line in lines) or "- 暂时没有可用的市场参考"


def _trend_description(analysis: dict) -> str:
    movement = analysis.get("movement")
    trend = (analysis.get("trend") or {}).get("trend")
    pct = analysis.get("percentile")
    if movement == "fare_class_jump":
        return "价格最近出现明显跳涨，低价舱位可能减少。"
    if movement == "mean_reverting" and trend == "rising":
        return "价格在前几天触底后开始回升。"
    if movement == "mean_reverting" and trend == "falling":
        return "价格最近仍有回落迹象。"
    if movement == "stable" and pct is not None and float(pct) < 35:
        return "价格近期稳定，且处于较低水平。"
    if movement == "stable":
        return "价格近期较为稳定。"
    return "价格最近没有特别明确的方向。"


def _reason_description(analysis: dict) -> str:
    days = analysis.get("days_to_dept")
    pct = analysis.get("percentile")
    cheaper_than = 100 - float(pct) if pct is not None else None
    if days is None:
        window = "这段时间"
    elif days > 45:
        window = "出发前45天以上"
    elif days > 30:
        window = "出发前30到45天"
    elif days > 21:
        window = "出发前21到30天"
    elif days > 14:
        window = "出发前14到21天"
    else:
        window = "临近出发"

    if cheaper_than is None:
        return f"当前处于{window}，系统会继续盯这条航线的价格变化。"
    return (
        f"当前处于{window}。\n"
        f"当前价格低于历史约{cheaper_than:.0f}%的记录。"
    )


def _risk_description(analysis: dict) -> str:
    wait_val = analysis.get("waiting_value")
    days = analysis.get("days_to_dept")
    if wait_val is not None and float(wait_val) > 0:
        avg_increase = float(wait_val)
        up_prob = min(85, max(55, 55 + avg_increase / 100))
        return (
            "根据历史数据，类似情况下继续等待，\n"
            f"价格上涨概率约{up_prob:.0f}%，\n"
            f"平均多花{format_price(avg_increase)}。"
        )
    if days is not None and days < 14:
        return f"距出发仅{days}天，继续等待的不确定性较高。"
    return "继续等待可能仍有小幅波动，但也存在错过当前价格的风险。"


def _short_trend(analysis: dict) -> str:
    movement = analysis.get("movement")
    trend = (analysis.get("trend") or {}).get("trend")
    if movement == "fare_class_jump":
        return "最近有明显涨价迹象，继续等待的风险变高。"
    if trend == "rising":
        return "最近价格在往上走。"
    if trend == "falling":
        return "最近价格仍有回落迹象。"
    return "最近价格比较平稳。"

def _first_price(analysis: dict):
    return analysis.get("first_price") or analysis.get("avg_price") or analysis.get("current_price")


def _min_date(analysis: dict) -> str:
    return analysis.get("min_date") or "璁板綍鏈熷唴"


def _target_price(analysis: dict):
    min_seen = analysis.get("min_seen")
    avg_price = analysis.get("avg_price")
    if min_seen is not None:
        return min_seen
    if avg_price is not None:
        return float(avg_price) * 0.95
    return analysis.get("current_price")


def _duration_text(hours) -> str:
    if hours is None:
        return "-"
    total_minutes = round(float(hours) * 60)
    return f"{total_minutes // 60}小时{total_minutes % 60}分钟"


def _append_disclaimer(message: str, run_status: str | None = None) -> str:
    parts = [message]
    if run_status:
        parts.extend(["", run_status])
    parts.extend(["", "---", DISCLAIMER])
    return "\n".join(parts)


def _advice(trigger_reason: str | None) -> str:
    advice_map = {
        "signal_upgrade": "如果行程已经确定：买入信号升级，可以优先检查目标航班并准备下单。",
        "milestone": "如果今天要复盘：这是关键观察节点，可以复查价格和替代方案。",
        "new_low": "如果目标航班符合行程：当前刷新历史低价，可以重点比较预算和退改条件。",
        "cheaper_alt": "如果时间安排灵活：替代方案明显更便宜，可以比较中转和总时长。",
    }
    return advice_map.get(trigger_reason, "如果价格还不够明确：可以继续观察价格信号。")


def format_buy_message(analysis, run_status: str | None = None) -> str:
    message = "\n".join([
        "航班价格提醒",
        "",
        f"航线：{_route_info(analysis)}",
        f"日期：{analysis.get('depart_date', '-')}",
        f"当前价格：{format_price(analysis.get('current_price'))}",
        "",
        _trend_description(analysis),
        _reason_description(analysis),
    ])
    return _append_disclaimer(message, run_status)


def format_consider_message(analysis, run_status: str | None = None) -> str:
    message = "\n".join([
        "航班价格提醒",
        "",
        f"航线：{_route_info(analysis)}",
        f"日期：{analysis.get('depart_date', '-')}",
        f"当前价格：{format_price(analysis.get('current_price'))}",
        "",
        _short_trend(analysis),
    ])
    return _append_disclaimer(message, run_status)


def format_milestone_message(analysis, days, run_status: str | None = None) -> str:
    message = "\n".join([
        f"距出发还有{days if days is not None else '-'}天",
        "",
        f"航线：{_route_info(analysis, include_stop=False)}",
        f"当前价格：{format_price(analysis.get('current_price'))}",
        _trend_description(analysis),
    ])
    return _append_disclaimer(message, run_status)


def format_alternative_message(analysis, run_status: str | None = None) -> str:
    alt = analysis.get("cheapest_alt") or {}
    target_price = analysis.get("current_price")
    alt_price = alt.get("price")
    diff = analysis.get("target_vs_cheapest")
    if diff is None and target_price is not None and alt_price is not None:
        diff = float(target_price) - float(alt_price)
    diff = max(float(diff or 0), 0)
    message = "\n".join([
        "发现更便宜的航线方案",
        "",
        f"当前关注：{analysis.get('target_combo', '-')}，{format_price(target_price)}",
        f"替代方案：{alt.get('flight_combo', '-')}，{format_price(alt_price)}",
        f"价差：{format_price(diff)}",
        f"路线：{format_route_summary(alt.get('route_summary', '-'))}",
        f"总时长：{_duration_text(alt.get('duration_hours'))}",
    ])
    return _append_disclaimer(message, run_status)


def format_message(
    analysis: dict, trigger_reason: str | None, run_status: str | None = None
) -> str:
    """Choose one human-friendly notification template."""
    if trigger_reason == "cheaper_alt" and analysis.get("cheapest_alt"):
        return format_alternative_message(analysis, run_status)
    if trigger_reason == "milestone":
        return format_milestone_message(analysis, analysis.get("days_to_dept"), run_status)
    signal = analysis.get("signal")
    if signal in {"strong_buy", "buy_now"}:
        return format_buy_message(analysis, run_status)
    if signal in {"buy", "consider"}:
        return format_consider_message(analysis, run_status)
    return format_milestone_message(analysis, analysis.get("days_to_dept"), run_status)

def _log_notification(content: str) -> None:
    NOTIFICATIONS_LOG.parent.mkdir(exist_ok=True)
    entry = (
        f"\n===== {datetime.now().isoformat(timespec='seconds')} =====\n"
        f"{content}\n"
    )
    with NOTIFICATIONS_LOG.open("a", encoding="utf-8") as file:
        file.write(entry)


def _notification_title_from_content(content: str, fallback: str) -> str:
    """Use the action label at the top of the message as the PushPlus title."""
    text = re.sub(r"<[^>]+>", "", content or "").replace("&nbsp;", " ").strip()
    first_line = re.split(r"(?:<br>|\n)", text, maxsplit=1)[0].strip()
    if first_line.startswith("【") and "】" in first_line:
        return first_line[:80]
    return fallback


def send(content: str | PushRender, title: str = "航班监控通知") -> bool:
    """发送推送通知；航班消息按小节降级，通用告警保持字符串语义。"""
    structured = isinstance(content, PushRender)
    original_content = render_push_render(content) if structured else str(content or "")
    pushplus_token = os.environ.get("PUSHPLUS_TOKEN", "")
    if not pushplus_token:
        _log_notification(original_content)
        print("[推送] 未配置 PUSHPLUS_TOKEN，已写入本地通知日志")
        return False

    if not structured and len(original_content) > PUSHPLUS_COMPACT_CHARS:
        _log_notification(original_content)
    msg = _prepare_pushplus_content(content)
    resolved_title = (
        content.title
        if structured and content.title
        else _notification_title_from_content(msg, title)
    )
    print(f"[推送] 消息长度: {len(msg)} 字符")
    result = _post_pushplus(pushplus_token, resolved_title, msg)
    if result and result.get("code") == 200:
        print("PushPlus推送成功")
        return True
    print(f"PushPlus返回异常: {result}")
    if result is None and structured:
        minimal = prepare_push_render(content, compact_chars=0, max_chars=0).content
        if minimal != msg:
            result = _post_pushplus(pushplus_token, resolved_title, minimal)
            if result and result.get("code") == 200:
                print("PushPlus最小安全模板推送成功")
                return True
    _log_notification(original_content)
    return False


def format_run_status(results: list[dict]) -> str:
    """Return a short collection status line."""
    success_results = [result for result in results if result.get("status") == "ok"]
    current = success_results[0] if success_results else {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    source = current.get("source") or "-"
    flight_count = current.get("flight_count", len(success_results))
    return f"📋 本次采集：{now} | {flight_count}条航班 | 数据源：{source}"


SOURCE_LABELS = {
    "juhe": "聚合数据（国内报价）",
    "serpapi": "Google Flights（via SerpAPI）",
    "searchapi": "Google Flights（via SearchAPI）",
    "hasdata": "Google Flights（via HasData）",
    "travelpayouts": "Travelpayouts（Aviasales）",
    "skyscanner": "Skyscanner（via RapidAPI）",
    "serpapi+searchapi": "Google Flights（via SerpAPI + SearchAPI）",
    "searchapi+serpapi": "Google Flights（via SerpAPI + SearchAPI）",
    "duffel": "Duffel",
}


def _source_label(data_source: str | None) -> str:
    if not data_source:
        return "Google Flights"
    if "+" in data_source:
        labels = [_source_label(source) for source in data_source.split("+")]
        return " + ".join(dict.fromkeys(labels))
    return SOURCE_LABELS.get(data_source, data_source)


def _source_summary(analysis_result: dict) -> str:
    sources = []

    for rec in analysis_result.get("recommendations", []):
        flight = rec.get("flight", {})
        data_source = flight.get("data_source")
        if data_source and data_source not in sources:
            sources.append(data_source)

    for flight in analysis_result.get("all_flights", []):
        data_source = flight.get("data_source")
        if data_source and data_source not in sources:
            sources.append(data_source)

    if not sources:
        return "Google Flights"

    labels = [_source_label(source) for source in sources]
    return " / ".join(dict.fromkeys(labels))


def format_source_summary(source_stats):
    if not source_stats:
        return ""

    display_names = {
        "serpapi": "SerpAPI（Google Flights）",
        "searchapi": "SearchAPI（Google Flights）",
        "travelpayouts": "Travelpayouts（Aviasales）",
        "skyscanner": "Skyscanner（via RapidAPI）",
        "duffel": "Duffel（航司直连）",
        "hasdata": "HasData",
        "SerpAPISource": "SerpAPI（Google Flights）",
        "SearchAPISource": "SearchAPI（Google Flights）",
        "DuffelSource": "Duffel（航司直连）",
        "HasDataSource": "HasData",
    }

    lines = ["📡 数据源汇总"]
    for key, value in source_stats.items():
        if key in ("total_raw", "after_dedup", "enriched_count"):
            continue
        if not isinstance(value, dict):
            continue
        name = display_names.get(key, key)
        count = value.get("count", 0)
        status = value.get("status", "")
        if "成功" in str(status) or status == "success":
            lines.append(f"　- {name}：{count}个方案 ✅")
        else:
            lines.append(f"　- {name}：{count}个方案，状态：{status or '失败'}")

    total = source_stats.get("total_raw", 0)
    dedup = source_stats.get("after_dedup", 0)
    if total > 0:
        lines.append(f"　- 合计采集{total}个 → 去重后{dedup}个方案")
    return "\n".join(lines)


def format_price_change(current_price, previous_price) -> str:
    if previous_price is None:
        return "📊 首次采集，暂无历史对比"
    diff = current_price - previous_price
    pct = diff / previous_price * 100 if previous_price else 0
    if abs(diff) < 50:
        return f"📊 价格基本持平（和上次相比变化¥{abs(diff):,.0f}）"
    if diff < 0:
        return f"📉 比上次便宜¥{abs(diff):,.0f}（下降{abs(pct):.1f}%）"
    return f"📈 比上次贵¥{diff:,.0f}（上涨{pct:.1f}%）"

def format_baggage(extra):
    lines = []
    bag = extra.get("baggage_detail", {})
    checked = bag.get("checked", {})
    carry_on = bag.get("carry_on", {})

    if checked.get("quantity", 0) > 0:
        text = f"🧳 托运行李：免费{checked['quantity']}件"
        if checked.get("weight_kg"):
            text += f"（每件≤{checked['weight_kg']}kg）"
        else:
            text += "（重量以航司规定为准）"
        lines.append(text)
    else:
        lines.append("🧳 托运行李：不含免费托运，需另购")

    if carry_on.get("quantity", 0) > 0:
        text = f"👜 手提行李：免费{carry_on['quantity']}件"
        if carry_on.get("weight_kg"):
            text += f"（每件≤{carry_on['weight_kg']}kg）"
        lines.append(text)

    if not bag:
        lines = []
        if extra.get("baggage"):
            lines.append("🧳 行李：含托运行李（详情以航司规定为准）")
        else:
            lines.append("🧳 行李：请查询航司官网确认托运额度")
    return lines


def format_seat(extra):
    seat = extra.get("seat_detail", {})
    if not seat:
        return ["💺 舱位：经济舱", "🪑 选座：请查询航司官网确认"]

    cabin_names = {
        "economy": "经济舱",
        "premium_economy": "超级经济舱",
        "business": "商务舱",
        "first": "头等舱",
    }
    cabin = cabin_names.get(seat.get("cabin_class", ""), seat.get("cabin_class", ""))
    cabin_marketing = seat.get("cabin_class_name", "")
    lines = []
    if cabin_marketing:
        lines.append(f"💺 舱位：{cabin}（{cabin_marketing}）")
    else:
        lines.append(f"💺 舱位：{cabin}")

    if seat.get("seat_selectable"):
        if seat.get("seat_free"):
            lines.append("🪑 选座：可免费选座 ✅")
        elif seat.get("seat_price"):
            price = seat["seat_price"]
            currency = seat.get("seat_currency", "CNY")
            if currency == "CNY":
                lines.append(f"🪑 选座：需付费 ¥{price:.0f}起")
            else:
                lines.append(f"🪑 选座：需付费 {currency} {price:.0f}起")
        else:
            lines.append("🪑 选座：可选座（费用详询航司）")
    else:
        lines.append("🪑 选座：暂无选座服务或值机时选择")
    return lines

def _cabin_label(cabin_class: str | None) -> str:
    labels = {
        "economy": "经济舱（Economy）",
        "premium_economy": "超级经济舱（Premium Economy）",
        "business": "商务舱（Business）",
        "first": "头等舱（First）",
    }
    return labels.get(cabin_class or "economy", cabin_class or "经济舱")


def _cabin_group_title(cabin_class: str | None) -> str:
    titles = {
        "economy": "━━━ 经济舱方案 ━━━",
        "premium_economy": "━━━ 超级经济舱方案 ━━━",
        "business": "━━━ 商务舱方案 ━━━",
        "first": "━━━ 头等舱方案 ━━━",
    }
    return titles.get(cabin_class or "economy", f"━━━ {_cabin_label(cabin_class)}方案 ━━━")

def _ordered_cabin_classes(flights: list[dict], configured=None) -> list[str]:
    present = []
    for flight in flights:
        cabin_class = flight.get("cabin_class") or "economy"
        if cabin_class not in present:
            present.append(cabin_class)

    ordered = []
    configured_classes = configured or []
    if isinstance(configured_classes, str):
        configured_classes = [configured_classes]
    for cabin_class in configured_classes:
        if cabin_class in present and cabin_class not in ordered:
            ordered.append(cabin_class)
    for cabin_class in present:
        if cabin_class not in ordered:
            ordered.append(cabin_class)
    return ordered


def _aircraft_summary(flight: dict) -> str:
    aircrafts = []
    for segment in flight.get("segments") or []:
        aircraft = get_aircraft_name(segment.get("aircraft") or segment.get("equipment"))
        if aircraft and aircraft not in aircrafts:
            aircrafts.append(aircraft)
    return " / ".join(aircrafts) if aircrafts else "请查询航司官网"


def _duration_minutes_text(minutes) -> str:
    minutes = int(minutes or 0)
    if minutes <= 0:
        return "请查询航司官网"
    return f"{minutes // 60}小时{minutes % 60}分钟"


def _flight_start_end_text(flight: dict) -> str:
    segments = flight.get("segments") or []
    if not segments:
        return "请查询航司官网"
    dep_time = _time_only(segments[0].get("dep_time"))
    arr_time = _time_only(segments[-1].get("arr_time"))
    if dep_time and arr_time:
        return f"{dep_time} 起飞 → {arr_time} 到达（当地时间）"
    return "请查询航司官网"


def _seat_selection_line(extra: dict) -> str:
    seat = extra.get("seat_detail") or {}
    if not seat:
        return "💺 选座：请查询航司官网"
    if seat.get("seat_selectable"):
        if seat.get("seat_free"):
            return "💺 选座：免费"
        if seat.get("seat_price"):
            price = float(seat["seat_price"])
            currency = seat.get("seat_currency", "CNY")
            if currency == "CNY":
                return f"💺 选座：需付费（¥{price:,.0f}起）"
            return f"💺 选座：需付费（{currency} {price:,.0f}起）"
        return "💺 选座：可选座（费用详询航司）"
    return "💺 选座：请查询航司官网"


def _refund_change_lines(extra: dict) -> list[str]:
    refund_change = extra.get("refund_change") or {}
    changeable = refund_change.get("changeable", extra.get("changeable"))
    refundable = refund_change.get("refundable", extra.get("refundable"))
    change_fee = refund_change.get("change_fee")

    if changeable:
        if change_fee == "免费":
            change_line = "🔄 改签：出发前免费改签"
        else:
            change_line = "🔄 改签：出发前可改签"
    else:
        change_line = "🔄 改签：不可改签"
    refund_line = "💰 退票：可退票" if refundable else "💰 退票：不可退票"
    return [change_line, refund_line]

def _service_info_lines(flight: dict) -> list[str]:
    extra = flight.get("extra") or {}
    if flight.get("has_baggage_info"):
        lines = []
        lines.extend(format_baggage(extra))
        lines.append(_seat_selection_line(extra))
        lines.extend(_refund_change_lines(extra))
        lines.append("馃搸 鏈嶅姟淇℃伅鏉ユ簮锛欴uffel锛堣埅鍙哥洿杩烇級")
        return lines

    return [
        "馃С 琛屾潕锛氳鏌ヨ鑸徃瀹樼綉",
        "馃獞 閫夊骇锛氳鏌ヨ鑸徃瀹樼綉",
        "🔄 退改：请查询航司官网",
    ]


def _estimate_drop_probability(price_history, current_price) -> int | None:
    """估算接近当前价格时，下一次记录继续下降的比例。"""
    if not price_history or not current_price:
        return None
    prices = []
    for item in price_history:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            price = item[1]
        else:
            price = item
        if price and price > 0:
            prices.append(float(price))
    if len(prices) < 6:
        return None
    similar_changes = []
    tolerance = current_price * 0.1
    for index, price in enumerate(prices[:-1]):
        if abs(price - current_price) <= tolerance:
            similar_changes.append(prices[index + 1] - price)
    if len(similar_changes) < 3:
        similar_changes = [prices[index + 1] - price for index, price in enumerate(prices[:-1])]
    if not similar_changes:
        return None
    drops = [change for change in similar_changes if change < -100]
    return round(len(drops) / len(similar_changes) * 100)


def generate_neutral_summary(analysis, trend, price_insights=None):
    """生成客观的市场情况说明，不做购买指令。"""
    lines = []
    min_price = analysis.get("price_range", [0, 0])[0]
    avg_price = trend.get("avg_price", 0) if trend else 0
    recent = trend.get("recent_trend", "") if trend else ""
    position = trend.get("current_position", "") if trend else ""

    if avg_price and min_price:
        if min_price < avg_price:
            lines.append(f"当前最低价¥{min_price:,.0f}，低于近60天平均价¥{avg_price:,.0f}。")
        else:
            lines.append(f"当前最低价¥{min_price:,.0f}，高于近60天平均价¥{avg_price:,.0f}。")
    if position:
        lines.append(f"当前价格处于近60天的{_plain_price_position(position)}。")
    if "上涨" in recent:
        lines.append("近期价格呈上涨趋势。")
    elif "下降" in recent:
        lines.append("近期价格在下降。")
    else:
        lines.append("近期价格较为平稳。")

    drop_probability = _estimate_drop_probability(
        price_insights.get("price_history") if price_insights else None,
        min_price,
    )
    if drop_probability is not None:
        lines.append(f"历史类似记录中，下一次价格继续下降的比例约{drop_probability}%。")
    else:
        lines.append("历史价格样本不足，暂时无法估算后续下降比例。")
    return lines

def _priority_summary_text(priorities: dict) -> str:
    parts = []
    if priorities.get("budget") is not None:
        parts.append(f"预算{float(priorities['budget']):,.0f}内".replace(",", ""))
    if priorities.get("max_hours") is not None:
        parts.append(f"{priorities['max_hours']}小时内")
    if priorities.get("max_stops") is not None:
        parts.append(f"{priorities['max_stops']}次中转以内")
    if priorities.get("no_overnight"):
        parts.append("不过夜转机")
    return "、".join(parts)


def _reference_flight_line(index: int, flight: dict) -> str:
    violations = flight.get("priority_violations") or []
    reason = "；".join(violations) if violations else "不符合部分条件"
    return f"方案{index}：¥{flight.get('price', 0):,.0f}，{reason}"


def _option_label(index: int) -> str:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if index < len(letters):
        return f"方案{letters[index]}"
    return f"方案{index + 1}"


def generate_pros_cons(flight, all_flights):
    pros = []
    cons = []
    usable_flights = [f for f in all_flights if f.get("price") is not None]
    if not usable_flights:
        return pros, cons

    prices = [f["price"] for f in usable_flights]
    durations = [f.get("total_duration_min", 0) for f in usable_flights]
    sorted_prices = sorted(prices)
    sorted_durations = sorted(durations)
    lower_index = min(len(sorted_prices) - 1, len(sorted_prices) // 3)

    if flight["price"] == min(prices):
        pros.append("所有方案中价格最低")
    elif flight["price"] <= sorted_prices[lower_index]:
        pros.append("价格较低")
    else:
        diff = flight["price"] - min(prices)
        cons.append(f"比最低价贵¥{diff:,.0f}")

    duration = flight.get("total_duration_min", 0)
    if duration == min(durations):
        pros.append("耗时最短")
    elif duration <= sorted_durations[lower_index]:
        pros.append("耗时较短")
    else:
        diff_h = (duration - min(durations)) // 60
        cons.append(f"比最快方案慢{diff_h}小时")

    stops = flight.get("stops", 0)
    if stops == 0:
        pros.append("直飞，无需转机")
    elif stops == 1:
        for lay in flight.get("layovers", []):
            wait = lay.get("wait_minutes", 0)
            if wait < 180:
                pros.append("转机等待时间短，紧凑高效")
            elif wait > 480:
                cons.append(f"转机等待{wait // 60}小时，可能需过夜")
    elif stops >= 2:
        cons.append(f"需转机{stops}次")

    extra = flight.get("extra", {})
    if extra.get("refundable") and extra.get("changeable"):
        pros.append("可退可改，灵活度高")
    elif not extra.get("refundable"):
        cons.append("不可退票")

    airlines = {seg.get("airline", "") for seg in flight.get("segments", []) if seg.get("airline")}
    if len(airlines) == 1:
        pros.append("全程同一航司，行李直挂有保障")
    elif len(airlines) > 1:
        cons.append(f"涉及{len(airlines)}家航司，行李可能无法直挂")
    return pros, cons

def _select_compact_recommendations(analysis_result: dict) -> tuple[list[dict], dict | None]:
    economy_recs = list(analysis_result.get("economy_recommendations") or [])
    business_rec = analysis_result.get("business_recommendation")

    if economy_recs or business_rec:
        return economy_recs[:4], business_rec

    all_flights = analysis_result.get("all_flights") or []
    economy_flights = [
        flight
        for flight in all_flights
        if (flight.get("cabin_class") or "economy") == "economy"
    ]
    business_flights = [
        flight
        for flight in all_flights
        if (flight.get("cabin_class") or "economy") == "business"
    ]

    economy_recs = []
    seen_routes = set()
    for flight in sorted(economy_flights, key=lambda item: item.get("price", 99999)):
        route = flight.get("route_summary", "")
        if route not in seen_routes and len(economy_recs) < 4:
            economy_recs.append(flight)
            seen_routes.add(route)

    if business_flights:
        business_rec = min(business_flights, key=lambda item: item.get("price", 99999))

    return economy_recs, business_rec


def _arrival_time_text(flight: dict) -> str:
    segments = flight.get("segments") or []
    if not segments:
        return "请查询航司官网"

    dep_raw = str(segments[0].get("dep_time") or "")
    arr_raw = str(segments[-1].get("arr_time") or "")
    dep_time = _time_only(dep_raw)
    arr_time = _time_only(arr_raw)
    dep_date = dep_raw[:10] if len(dep_raw) >= 10 else ""
    arr_date = arr_raw[:10] if len(arr_raw) >= 10 else ""
    prefix = "次日" if dep_date and arr_date and dep_date != arr_date else ""

    if dep_time and arr_time:
        return f"出发{dep_time} → 到达{prefix}{arr_time}"
    return "请查询航司官网"


def _compact_flight_numbers(flight: dict) -> str:
    numbers = [
        segment.get("flight_no", "")
        for segment in _email_plan_segments(flight)
        if segment.get("flight_no")
    ]
    if numbers:
        return " → ".join(numbers)
    combo = flight.get("flight_combo") or ""
    return combo.replace("+", " → ") if combo else "请查询航司官网"


def _compact_aircrafts(flight: dict) -> str:
    aircrafts = [
        get_aircraft_name(segment.get("aircraft", "") or segment.get("equipment", ""))
        for segment in _email_plan_segments(flight)
        if segment.get("aircraft") or segment.get("equipment")
    ]
    return " → ".join(aircrafts) if aircrafts else "请查询航司官网"


def _compact_layover(flight: dict) -> str:
    layovers = flight.get("layovers") or []
    if not layovers:
        return "直飞"
    parts = []
    for layover in layovers:
        airport = layover.get("airport", "")
        city = city_name(airport) if airport else layover.get("city", "中转地")
        wait = int(layover.get("wait_minutes") or 0)
        parts.append(f"{city} 等待{wait // 60}小时{wait % 60}分")
    return "；".join(parts)


def _compact_cabin_rule_line(flight: dict) -> str:
    fare_rules = flight.get("fare_rules") or {}
    cabin_class = fare_rules.get("cabin_class") or flight.get("cabin_class") or "economy"
    return f"💺 票规舱位：{_cabin_label(cabin_class)}"


def _compact_baggage_line(flight: dict) -> str:
    fare_rules = flight.get("fare_rules") or {}
    baggage_rules = fare_rules.get("baggage") or {}
    checked_pieces = int(baggage_rules.get("checked_pieces") or 0)
    if checked_pieces > 0:
        checked_kg = baggage_rules.get("checked_kg")
        if checked_kg:
            return f"🧳 托运：免费{checked_pieces}件≤{checked_kg}kg"
        return f"🧳 托运：免费{checked_pieces}件"

    if not flight.get("has_baggage_info"):
        return "🧳 行李：请查询航司官网"

    baggage = (flight.get("extra") or {}).get("baggage_detail") or {}
    checked = baggage.get("checked") or {}
    quantity = int(checked.get("quantity") or 0)
    if quantity <= 0:
        return "🧳 托运：不含免费托运"
    weight = checked.get("weight_kg")
    if weight:
        return f"🧳 托运：免费{quantity}件≤{weight}kg"
    return f"🧳 托运：免费{quantity}件"


def _compact_refund_line(flight: dict) -> str:
    fare_rules = flight.get("fare_rules") or {}
    change_rules = fare_rules.get("change") or {}
    refund_rules = fare_rules.get("refund") or {}
    has_standard_rules = (
        change_rules.get("allowed") is not None
        or refund_rules.get("allowed") is not None
        or change_rules.get("fee") is not None
        or refund_rules.get("fee") is not None
    )
    if has_standard_rules:
        if change_rules.get("allowed"):
            change_fee = change_rules.get("fee")
            change_text = "免费改签" if change_fee == 0 else "可改签"
        else:
            change_text = "不可改签"
        if refund_rules.get("allowed"):
            refund_fee = refund_rules.get("fee")
            refund_text = "免费退票" if refund_fee == 0 else "可退票"
        else:
            refund_text = "不可退票"
        return f"🔄 退改：{change_text} · {refund_text}"

    if not flight.get("has_baggage_info"):
        return "🔄 退改：请查询航司官网"

    extra = flight.get("extra") or {}
    refund_change = extra.get("refund_change") or {}
    changeable = refund_change.get("changeable", extra.get("changeable"))
    refundable = refund_change.get("refundable", extra.get("refundable"))
    change_text = "免费改签" if refund_change.get("change_fee") == "免费" else "可改签"
    if not changeable:
        change_text = "不可改签"
    refund_text = "可退票" if refundable else "不可退票"
    return f"🔄 退改：{change_text} · {refund_text}"

def _flight_search_date(flight: dict, fallback_date: str | None = None) -> str:
    segments = flight.get("segments") or []
    dep_time = str((segments[0] if segments else {}).get("dep_time") or "")
    if len(dep_time) >= 10:
        return dep_time[:10]
    return str(fallback_date or "")


def _collected_time_text(flight: dict) -> str:
    collected_at = _collected_datetime(flight)
    if collected_at:
        return collected_at.strftime("%H:%M")
    return datetime.now().strftime("%H:%M")


def _collected_datetime(flight: dict) -> datetime | None:
    raw_value = (
        flight.get("collected_at")
        or flight.get("snapshot_time")
        or flight.get("fetched_at")
    )
    if raw_value:
        try:
            return datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _freshness_label(flight: dict) -> str:
    collected_at = _collected_datetime(flight)
    if not collected_at:
        return "馃敶寤鸿鍒锋柊"
    now = datetime.now(collected_at.tzinfo) if collected_at.tzinfo else datetime.now()
    minutes = max(0, (now - collected_at).total_seconds() / 60)
    if minutes <= 30:
        return "馃煝鏂伴矞"
    if minutes <= 120:
        return "馃煛闇€纭"
    return "馃敶寤鸿鍒锋柊"


def _has_free_checked_baggage(flight: dict) -> bool:
    extra = flight.get("extra") or {}
    baggage_detail = extra.get("baggage_detail") or {}
    checked = baggage_detail.get("checked") or {}
    if (checked.get("quantity") or 0) > 0:
        return True
    fare_rules = flight.get("fare_rules") or {}
    fare_baggage = fare_rules.get("baggage") or {}
    if (fare_baggage.get("checked_pieces") or 0) > 0:
        return True
    return bool(extra.get("baggage"))


def _execution_target_price(
    flight: dict, route_info: dict | None = None, analysis_result: dict | None = None
) -> float | None:
    route_info = route_info or {}
    analysis_result = analysis_result or {}
    price = _to_float(flight.get("price"))
    candidates = [
        flight.get("target_price"),
        analysis_result.get("target_price_effective"),
        analysis_result.get("target_price"),
        _preference_value(route_info, analysis_result, "target_price"),
        _preference_value(route_info, analysis_result, "budget"),
        _preference_value(route_info, analysis_result, "max_budget"),
    ]
    for candidate in candidates:
        value = _to_float(candidate)
        if value and value > 0:
            return value
    return price * 1.05 if price and price > 0 else None


def _execution_advice_lines(
    flight: dict, route_info: dict | None = None, analysis_result: dict | None = None
) -> list[str]:
    grade = flight.get("execution_grade") or "C"
    label = flight.get("execution_label")
    price = _to_float(flight.get("price"))
    final_limit = price * 1.05 if price and price > 0 else None
    target_price = _execution_target_price(flight, route_info, analysis_result)
    final_limit_text = _price_text(final_limit) if final_limit else "当前采集价上浮5%以内"
    target_price_text = _price_text(target_price) if target_price else "你的目标价"
    baggage_clause = "且含托运行李" if _has_free_checked_baggage(flight) else "且行李规则符合需求"

    lines = []
    if label:
        lines.append(label)
    reasons = flight.get("execution_reasons") or []
    if reasons:
        lines.append(f"执行提醒：{'；'.join(str(reason) for reason in reasons[:3])}")

    if grade == "A":
        lines.extend([
            "✅ 操作建议：",
            f"若支付页最终价不超过{final_limit_text}，{baggage_clause}，建议购买。",
            f"若最终价超过{target_price_text}，建议保持本条航线监控。",
        ])
    elif grade == "B":
        lines.extend([
            "🔶 操作建议：",
            "点击链接确认最终价格和票规后再购买。",
            "注意确认是否含托运行李、是否联程票。",
        ])
    elif grade == "C":
        lines.extend([
            "⚠️ 仅供参考：",
            "该价格仅用于判断市场区间，当前可购买性未验证。",
        ])
    else:
        lines.extend([
            "❌ 其他参考：",
            "当前可执行性较低，不作为主购买方案。",
        ])
    return lines

def _message_collected_time(analysis_result: dict, route_info: dict) -> str:
    for flight in analysis_result.get("all_flights") or []:
        collected_at = _collected_datetime(flight)
        if collected_at:
            return collected_at.strftime("%Y-%m-%d %H:%M")
    value = route_info.get("collected_at") or analysis_result.get("collected_at")
    if value:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _time_with_timezone(time_text: str, airport_code: str, show_timezone: bool) -> str:
    if not show_timezone:
        return time_text
    return f"{time_text}({get_airport_timezone(airport_code)})"


def _flight_booking_link(flight: dict, date_str: str | None, label: str) -> str:
    segments = flight.get("segments") or []
    first_segment = segments[0] if segments else {}
    last_segment = segments[-1] if segments else {}
    origin = first_segment.get("dep_airport") or first_segment.get("departure_airport")
    dest = last_segment.get("arr_airport") or last_segment.get("arrival_airport")
    search_date = _flight_search_date(flight, date_str)
    if not origin or not dest or not search_date:
        return ""
    return generate_booking_links(origin, dest, search_date, flight.get("flight_combo"), flight=flight)


def _round_trip_aircraft_text(flight: dict) -> str:
    segments = flight.get("segments") or []
    aircraft = ""
    if segments:
        aircraft = segments[0].get("aircraft") or ""
    aircraft = get_aircraft_name(aircraft)
    return str(aircraft).strip() or "机型待确认"


def _round_trip_score_text(flight: dict) -> str:
    return _human_recommendation_text(flight)


def _round_trip_option_line(
    index: int,
    flight: dict,
    date_str: str | None = None,
    route_info: dict | None = None,
    analysis_result: dict | None = None,
) -> str:
    return format_flight_detail(flight, date_str, _option_label(index))


def _round_trip_combo_flight_line(prefix: str, flight: dict, date_str: str | None) -> str:
    label = "购买去程" if prefix == "去" else "购买返程"
    link = _flight_booking_link(flight, date_str, label)
    return (
        f"  {prefix}: {_compact_flight_numbers(flight)} {_round_trip_airline_text(flight)} "
        f"{_round_trip_price_estimate_line(flight)} | {_flight_status_tags(flight)} | 🔗 {link}"
    )


def _append_round_trip_combo_lines(lines: list[str], combinations: list[dict]) -> None:
    if not combinations:
        return
    lines.append("<b>🔄 往返最优组合</b>")
    for index, combo in enumerate(combinations[:3], start=1):
        outbound = combo.get("outbound") or {}
        return_flight = combo.get("return") or {}
        total_price = combo.get("total_price")
        if total_price is None:
            outbound_price = combo.get("outbound_price") or outbound.get("price")
            return_price = combo.get("return_price") or return_flight.get("price")
            if _has_valid_price(outbound_price) and _has_valid_price(return_price):
                total_price = float(outbound_price) + float(return_price)
        total_text = _price_text(total_price)
        estimated_total = None
        outbound_estimated = _estimated_price_value(outbound)
        return_estimated = _estimated_price_value(return_flight)
        if _has_valid_price(outbound_estimated) and _has_valid_price(return_estimated):
            estimated_total = float(outbound_estimated) + float(return_estimated)
        estimated_text = _price_text(estimated_total)

        lines.append(f"组合{index}: 往返展示总价{total_text}")
        if outbound:
            outbound_date = combo.get("outbound_date") or outbound.get("depart_date")
            lines.append(_round_trip_combo_flight_line("去", outbound, outbound_date))
        if return_flight:
            return_date = combo.get("return_date") or return_flight.get("depart_date")
            lines.append(_round_trip_combo_flight_line("回", return_flight, return_date))
        if estimated_total is not None:
            diff = estimated_total - float(total_price or 0)
            if diff > 0:
                lines.append(
                    f"  往返预估交易价: {estimated_text} ⚠️ 差价{_price_text(diff)}"
                )
            else:
                lines.append(f"  往返预估交易价: {estimated_text} ✅ 全服务航司无额外费用")
        lines.append("")


def _round_trip_top_flights(analysis: dict | None) -> list[dict]:
    analysis = analysis or {}
    flights = (
        analysis.get("economy_recommendations")
        or analysis.get("all_flights")
        or []
    )
    valid_flights = [flight for flight in flights if _has_valid_price(flight.get("price"))]
    primary_flights = [
        flight for flight in valid_flights if flight.get("execution_grade") != "D"
    ]
    return sorted(
        primary_flights or valid_flights,
        key=lambda flight: float(flight.get("price") or 999999),
    )


def _round_trip_score_flights(analysis: dict | None) -> list[dict]:
    analysis = analysis or {}
    flights = analysis.get("all_flights") or analysis.get("economy_recommendations") or []

    def sort_key(flight: dict):
        score = flight.get("preference_score")
        if score is None:
            score = (flight.get("scores") or {}).get("total")
        try:
            score_value = float(score)
        except (TypeError, ValueError):
            score_value = -1
        return (-score_value, float(flight.get("price") or 999999))

    return sorted(
        [flight for flight in flights if _has_valid_price(flight.get("price"))],
        key=sort_key,
    )[:3]


def _short_month_day(date_str: str | None) -> str:
    if not date_str:
        return ""
    try:
        value = datetime.fromisoformat(str(date_str)[:10])
        return f"{value.month}/{value.day}"
    except ValueError:
        return str(date_str)


def _flight_combo_time_text(flight: dict, date_str: str | None) -> str:
    segments = flight.get("segments") or []
    first_segment = segments[0] if segments else {}
    last_segment = segments[-1] if segments else {}
    dep = _time_only(first_segment.get("dep_time")) or "待确认"
    arr = _time_only(last_segment.get("arr_time")) or "待确认"
    prefix = _short_month_day(date_str)
    return f"{prefix} {dep}→{arr}".strip()


def _flight_combo_summary(flight: dict, date_str: str | None) -> str:
    return (
        f"{_compact_flight_numbers(flight)} {_round_trip_airline_text(flight)} | "
        f"{_flight_combo_time_text(flight, date_str)} | "
        f"{_round_trip_stops_text(flight)} | {_price_text(flight.get('price'))}"
    )


def _combo_grade(combo: dict) -> str:
    grades = [
        (combo.get("outbound") or {}).get("execution_grade"),
        (combo.get("return") or {}).get("execution_grade"),
    ]
    grades = [grade for grade in grades if grade]
    if not grades:
        return "未知"
    order = {"A": 1, "B": 2, "C": 3, "D": 4}
    return max(grades, key=lambda grade: order.get(grade, 9))


def _combo_price_status(total_price, route_info: dict) -> str:
    total = _to_float(total_price)
    target = _to_float(route_info.get("target_price"))
    max_budget = _to_float(route_info.get("max_budget") or route_info.get("budget"))
    if total is None:
        return ""
    budget_scope = _normalize_payload_budget_scope(
        route_info.get("max_budget_scope") or route_info.get("budget_scope")
    )
    if budget_scope == "all":
        return ""
    decision = evaluate_purchase_budget(
        total,
        target,
        max_budget,
        price_scope="per_person_roundtrip",
        budget_scope="per_person_roundtrip",
    )
    if decision["status"] == "at_or_below_target":
        return " ✅ 低于理想价"
    if decision["status"] == "within_budget":
        return " ✅ 预算内"
    if decision["status"] == "over_budget":
        return " ⚠️ 超预算"
    return ""


def _combo_full_booking_links(flight: dict, date_str: str | None) -> str:
    segments = flight.get("segments") or []
    if not segments:
        return ""
    origin = segments[0].get("dep_airport") or segments[0].get("departure_airport")
    dest = segments[-1].get("arr_airport") or segments[-1].get("arrival_airport")
    search_date = _flight_search_date(flight, date_str)
    if not origin or not dest or not search_date:
        return ""
    return generate_booking_links(
        origin,
        dest,
        search_date,
        flight.get("flight_combo"),
        flight=flight,
    )


def _append_round_trip_combo_card(lines: list[str], index: int, combo: dict, route_info: dict) -> None:
    outbound = combo.get("outbound") or {}
    return_flight = combo.get("return") or {}
    total = combo.get("total_price")
    transaction_total = combo.get("transaction_total")
    if transaction_total is None:
        outbound_est = _estimated_price_value(outbound) or combo.get("outbound_price")
        return_est = _estimated_price_value(return_flight) or combo.get("return_price")
        if _has_valid_price(outbound_est) and _has_valid_price(return_est):
            transaction_total = float(outbound_est) + float(return_est)
    extra = (float(transaction_total or 0) - float(total or 0)) if transaction_total is not None else 0
    outbound_links = _combo_full_booking_links(outbound, route_info.get("depart_date"))
    return_links = _combo_full_booking_links(return_flight, route_info.get("return_date"))
    lines.append(f"No.{index} 总价{_price_text(total)}{_combo_price_status(total, route_info)}")
    lines.append(f"┌ 去: {_flight_combo_summary(outbound, route_info.get('depart_date'))}")
    if outbound_links:
        lines.append(f"│ 🔗 {outbound_links}")
    lines.append(f"└ 回: {_flight_combo_summary(return_flight, route_info.get('return_date'))}")
    if return_links:
        lines.append(f"  🔗 {return_links}")
    if transaction_total is not None:
        if extra > 0:
            lines.append(f"  预估交易价：{_price_text(transaction_total)}（额外费用{_price_text(extra)}）")
        else:
            lines.append(f"  预估交易价：{_price_text(transaction_total)}（全服务，无额外费用）")
    lines.append(f"  执行等级：{_combo_grade(combo)}级")
    lines.append("")


def _change_text(current, previous) -> str:
    current_value = _to_float(current)
    previous_value = _to_float(previous)
    if current_value is None or previous_value is None:
        return "暂无"
    diff = current_value - previous_value
    if abs(diff) < 1:
        return "持平"
    arrow = "↓" if diff < 0 else "↑"
    return f"{arrow}{_price_text(abs(diff))}"


def _append_round_trip_change_table(lines: list[str], round_trip: dict) -> None:
    previous = round_trip.get("previous") or {}
    if not previous:
        return
    lines.append("<b>📈 价格变化（vs上次采集）</b>")
    lines.append("　　　　　上次　　本次　　变化")
    lines.append(
        f"去程最低 {_price_text(previous.get('outbound_lowest'))}  "
        f"{_price_text(round_trip.get('outbound_min'))}  "
        f"{_change_text(round_trip.get('outbound_min'), previous.get('outbound_lowest'))}"
    )
    lines.append(
        f"返程最低 {_price_text(previous.get('return_lowest'))}  "
        f"{_price_text(round_trip.get('return_min'))}  "
        f"{_change_text(round_trip.get('return_min'), previous.get('return_lowest'))}"
    )
    lines.append(
        f"往返最优 {_price_text(previous.get('roundtrip_lowest'))}  "
        f"{_price_text(round_trip.get('total_min'))}  "
        f"{_change_text(round_trip.get('total_min'), previous.get('roundtrip_lowest'))}"
    )
    trend = round_trip.get("trend") or {}
    if trend.get("direction"):
        lines.append(f"趋势判断：{trend.get('direction')}")
    lines.append("")


def _append_round_trip_all_options(
    lines: list[str], title: str, flights: list[dict] | None, date_str: str | None
) -> None:
    flights = flights or []
    if not flights:
        return
    lines.append(f"━━ {title} ━━")
    for index, flight in enumerate(flights[:5], start=1):
        lines.append(
            f"{index}. {_compact_flight_numbers(flight)} {_round_trip_airline_text(flight)} "
            f"{_price_text(flight.get('price'))} | {_flight_combo_time_text(flight, date_str)} "
            f"{_round_trip_stops_text(flight)} | {_round_trip_aircraft_text(flight)} | "
            f"{_flight_status_tags(flight)}"
        )
    lines.append("")


def _roundtrip_value(row: dict | None, key: str):
    row = row or {}
    if key == "outbound":
        return _to_float(row.get("outbound", row.get("outbound_lowest")))
    if key == "return":
        return _to_float(row.get("return", row.get("return_lowest")))
    return _to_float(row.get("total", row.get("roundtrip_lowest")))


def _roundtrip_date_label(row: dict) -> str:
    raw = str(row.get("date") or row.get("timestamp") or row.get("snapshot_time") or "")
    raw = raw[:10]
    try:
        parsed = date.fromisoformat(raw)
        return f"{parsed.month}/{parsed.day}"
    except ValueError:
        return raw or "--"


def _roundtrip_reference_gap(current, reference, reference_name: str) -> str:
    current_value = _to_float(current)
    reference_value = _to_float(reference)
    if current_value is None or reference_value is None:
        return ""
    diff = current_value - reference_value
    if abs(diff) < 1:
        return f"  → 当前即为{reference_name} 🔥"
    if diff > 0:
        return f"  → 当前比{reference_name}贵{_price_text(diff)}"
    return f"  → 当前比{reference_name}便宜{_price_text(abs(diff))} ✅"


def _append_roundtrip_price_reference(
    lines: list[str], round_trip: dict, route_info: dict
) -> None:
    total_min = _to_float(round_trip.get("total_min"))
    outbound_min = _to_float(round_trip.get("outbound_min"))
    return_min = _to_float(round_trip.get("return_min"))
    if total_min is None:
        return

    analysis = round_trip.get("price_analysis") or {}
    references = analysis.get("references") or {}
    lines.append("<b>📊 往返价格参考</b>")
    lines.append("")
    lines.append(f"当前往返最低总价：{_price_text(total_min)}（去{_price_text(outbound_min)} + 回{_price_text(return_min)}）")
    lines.append("")

    absolute_ref = references.get("absolute_min") or {}
    if absolute_ref.get("price") is not None:
        lines.append(f"历史往返最低：{_price_text(absolute_ref.get('price'))}")
        lines.append(_roundtrip_reference_gap(total_min, absolute_ref.get("price"), "历史最低"))
        lines.append("")

    conditional_ref = references.get("conditional_min") or {}
    if conditional_ref.get("price") is not None:
        label = conditional_ref.get("label") or "同条件往返最低"
        lines.append(f"{label}：{_price_text(conditional_ref.get('price'))}")
        lines.append(_roundtrip_reference_gap(total_min, conditional_ref.get("price"), "同条件最低"))
        sample_size = conditional_ref.get("sample_size")
        if sample_size:
            lines.append(f"  → 基于{sample_size}个往返价格点")
        lines.append("")

    recent_ref = references.get("recent_min") or {}
    if recent_ref.get("price") is not None:
        lines.append(f"近期往返最低（你关注以来）：{_price_text(recent_ref.get('price'))}")
        lines.append(_roundtrip_reference_gap(total_min, recent_ref.get("price"), "近期最低"))
        lines.append("")

    target = _to_float(route_info.get("target_price"))
    if target:
        ideal_total = target * 2
        lines.append(f"理想往返总价：{_price_text(ideal_total)}")
        diff = ideal_total - total_min
        if diff >= 0:
            lines.append(f"  → 低于理想价{_price_text(diff)} ✅ 已达标")
        else:
            lines.append(f"  → 距离理想价还差{_price_text(abs(diff))}")
        lines.append("")


def _roundtrip_price_sequence(prices: list[float]) -> str:
    if not prices:
        return ""
    return " → ".join(_price_text(price) for price in prices[-7:])


def _format_leg_change(value) -> str:
    amount = _to_float(value)
    if amount is None:
        return "暂无变化数据"
    if abs(amount) < 1:
        return "持平"
    verb = "降" if amount < 0 else "涨"
    return f"{verb}{_price_text(abs(amount))}"

def _append_roundtrip_price_analysis(lines: list[str], round_trip: dict) -> None:
    analysis = round_trip.get("price_analysis") or {}
    if not analysis.get("available"):
        return

    short_term = analysis.get("short_term") or {}
    mid_term = analysis.get("mid_term") or {}
    split = analysis.get("split") or {}
    if not short_term and not mid_term and not split:
        return

    lines.append("<b>📈 往返价格分析</b>")
    lines.append("")

    if short_term:
        lines.append(f"短期（近7天）：{short_term.get('trend', '数据积累中')}（{short_term.get('change_pct', 0)}%）")
        sequence = _roundtrip_price_sequence(short_term.get("prices") or [])
        if sequence:
            lines.append(f"  往返总价：{sequence}")
        lines.append(f"  其中去程{_format_leg_change(short_term.get('outbound_change'))}，返程{_format_leg_change(short_term.get('return_change'))}")
        lines.append("")

    if mid_term:
        lines.append("中期（你关注以来）：")
        if mid_term.get("level"):
            lines.append(f"  {mid_term['level']}")
        vs_avg = _to_float(mid_term.get("vs_avg"))
        if vs_avg is not None:
            if abs(vs_avg) < 1:
                lines.append("  与平均往返价格基本持平")
            elif vs_avg < 0:
                lines.append(f"  比平均往返价格便宜{_price_text(abs(vs_avg))}")
            else:
                lines.append(f"  比平均往返价格贵{_price_text(vs_avg)}")
        lines.append("")

    if split:
        lines.append("拆分看：")
        if split.get("outbound_level"):
            lines.append(f"  去程价格处于{split['outbound_level']}")
        if split.get("return_level"):
            marker = " ← 返程拉低了总价" if "较低" in str(split.get("return_level")) else ""
            lines.append(f"  返程价格处于{split['return_level']}{marker}")
        if split.get("contribution"):
            lines.append(f"  {split['contribution']}")
        lines.append("")

    if analysis.get("advice"):
        lines.append(analysis["advice"])
        lines.append("")


def _roundtrip_bar(price: float, min_price: float, max_price: float) -> str:
    if max_price <= min_price:
        return "██████"
    width = 12
    level = int((price - min_price) / (max_price - min_price) * (width - 4)) + 4
    return "█" * max(4, min(width, level))

def _split_channel_row_label(label: str) -> tuple[str, str]:
    text = str(label or "").strip()
    match = re.match(r"^(.*?)\s*[\(（]\s*via\s*([^\)）]+)\s*[\)）]\s*$", text, flags=re.I)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return text, ""


def _dedupe_chart_rows(rows: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, float], dict] = {}
    ordered_keys = []
    for row in rows or []:
        value = _to_float(row.get("value"))
        if value is None or value <= 0:
            continue
        base_label, provider = _split_channel_row_label(row.get("label") or "")
        key = (base_label, round(value, 2))
        if key not in grouped:
            grouped[key] = {**row, "label": base_label, "value": value, "_providers": []}
            ordered_keys.append(key)
        if provider and provider not in grouped[key]["_providers"]:
            grouped[key]["_providers"].append(provider)
    result = []
    for key in ordered_keys:
        row = grouped[key]
        providers = row.pop("_providers", [])
        if len(providers) >= 2:
            row["note"] = f"{'、'.join(providers)} {len(providers)}个数据源一致"
        result.append(row)
    return result


def _append_css_bar_chart(lines: list[str], title: str, rows: list[dict]) -> None:
    chart_rows = []
    for row in _dedupe_chart_rows(rows):
        value = _to_float(row.get("value"))
        if value is None or value <= 0:
            continue
        chart_rows.append({**row, "value": value})
    if not chart_rows:
        return
    max_value = max(row["value"] for row in chart_rows)
    min_value = min(row["value"] for row in chart_rows)
    lines.append(f"<b>{title}</b>")
    for row in chart_rows:
        width = 100 if max_value <= 0 else max(12, min(100, row["value"] / max_value * 100))
        color = row.get("color")
        if not color:
            if row.get("highlight") == "low":
                color = "#16a34a"
            elif row.get("highlight") == "selected":
                color = "#2563eb"
            elif row["value"] == min_value:
                color = "#16a34a"
            else:
                color = "#9ca3af"
        label = html.escape(str(row.get("label") or ""))
        raw_note = str(row.get("description") or row.get("note") or "").strip()
        if raw_note in {"A", "B", "C", "D"}:
            raw_note = ""
        note = html.escape(raw_note)
        caliber_scope = _chart_caliber_scope(row)
        if caliber_scope:
            price_text = _price_text_with_caliber(row["value"], caliber_scope, row.get("passengers"), row.get("route_type"))
        else:
            price_text = _price_text(row["value"])
        separator = ":" if label else ""
        note_text = f",{note}" if note else ""
        lines.append(
            '<div style="margin:8px 0;">'
            f'<div style="font-size:13px;color:#374151;">{label}{separator}{price_text}{note_text}</div>'
            '<div style="background:#e5e7eb;height:20px;border-radius:3px;overflow:hidden;">'
            f'<div style="background:{color};height:20px;width:{width:.1f}%;border-radius:3px;"></div>'
            '</div></div>'
        )
    lines.append("")


def _chart_scope_label(row: dict) -> str:
    scope = str(row.get("scope") or row.get("unit") or "").strip().lower()
    if scope in {"roundtrip", "round_trip", "往返"}:
        return "往返"
    if scope in {"oneway", "one_way", "single", "single_leg", "outbound", "return", "单程"}:
        return "单程"
    return ""




def _chart_caliber_scope(row: dict) -> str | None:
    scope = str(row.get("scope") or row.get("unit") or "").strip().lower()
    text = " ".join(str(row.get(key) or "") for key in ("scope", "unit", "label", "note", "price_scope")).lower()
    is_all = any(token in text for token in ("all", "total", "passenger", "\u5168\u5458", "\u591a\u4eba"))
    if "roundtrip" in scope or "round_trip" in scope or "\u5f80\u8fd4" in scope:
        return "all_passengers_roundtrip" if is_all else "per_person_roundtrip"
    if scope in {"oneway", "one_way", "single", "single_leg", "outbound", "return", "\u5355\u7a0b"}:
        return "all_passengers_oneway" if is_all else "per_person_oneway"
    return None

def _append_nearby_dates_bar_chart(lines: list[str], nearby_dates, is_round_trip: bool = False) -> None:
    items = list((nearby_dates or {}).values()) if isinstance(nearby_dates, dict) else list(nearby_dates or [])
    prices = [
        _to_float(item.get("roundtrip_total") or item.get("total") or item.get("min_price"))
        for item in items
        if isinstance(item, dict)
    ]
    valid_prices = [price for price in prices if price and price > 0]
    if not valid_prices:
        return
    cheapest = min(valid_prices)
    rows = []
    for item in sorted(items, key=lambda value: str(value.get("date", ""))):
        price = _to_float(item.get("roundtrip_total") or item.get("total") or item.get("min_price"))
        if not price:
            continue
        date_text = str(item.get("date", ""))
        try:
            parsed = date.fromisoformat(date_text)
            label = f"{parsed.month}/{parsed.day}"
        except ValueError:
            label = date_text
        notes = []
        highlight = ""
        if abs(price - cheapest) < 1:
            notes.append("← 最低")
            highlight = "low"
        if item.get("selected"):
            notes.append("（你选的）")
            highlight = highlight or "selected"
        rows.append({"label": label, "value": price, "note": " ".join(notes), "highlight": highlight})
    title = "📊 前后日期最低价（往返总价）" if is_round_trip else "📊 前后日期最低价"
    _append_css_bar_chart(lines, title, rows)


def _append_channel_price_bar_chart(lines: list[str], flight: dict | None) -> None:
    options = _verified_booking_options(flight)
    if len(options) < 2:
        return
    prices = [_option_price(option) for option in options]
    prices = [price for price in prices if price]
    if not prices:
        return
    cheapest = min(prices)
    rows = []
    for option in options[:6]:
        price = _option_price(option)
        if not price:
            continue
        rows.append(
            {
                "label": str(option.get("platform") or "购买渠道"),
                "value": price,
                "note": "← 最低" if abs(price - cheapest) < 1 else "",
                "highlight": "low" if abs(price - cheapest) < 1 else "",
            }
        )
    _append_css_bar_chart(lines, "📊 不同渠道报价对比", rows)


def _append_option_price_bar_chart(
    lines: list[str],
    analysis_result: dict,
    is_round_trip: bool,
    route_info: dict,
) -> None:
    rows = []
    if is_round_trip:
        for index, combo in enumerate(_round_trip_combinations(analysis_result)[:3], start=1):
            total = _to_float(combo.get("total_price"))
            if total is None:
                continue
            rows.append(
                {
                    "label": f"方案{chr(64 + index)}",
                    "value": total,
                    "note": f"风险{_combo_grade(combo)}级",
                    "highlight": "selected" if index == 1 else "",
                }
            )
    else:
        for index, flight in enumerate(_single_flights_for_sections(analysis_result)[:3], start=1):
            price = _to_float(flight.get("price"))
            if price is None:
                continue
            rows.append(
                {
                    "label": f"方案{chr(64 + index)}",
                    "value": price,
                    "note": _status_risk_label(flight),
                    "highlight": "selected" if index == 1 else "",
                }
            )
    _append_css_bar_chart(lines, "📊 方案价格对比", rows)


def _append_roundtrip_trend_chart(lines: list[str], round_trip: dict) -> None:
    analysis = round_trip.get("price_analysis") or {}
    rows = analysis.get("trend_chart") or round_trip.get("history") or []
    chart_rows = []
    for row in rows[-7:]:
        total = _roundtrip_value(row, "total")
        if total is not None:
            chart_rows.append((row, total))
    if not chart_rows:
        return

    totals = [total for _, total in chart_rows]
    min_price = min(totals)
    max_price = max(totals)
    trend_label = (analysis.get("short_term") or {}).get("trend") or (round_trip.get("trend") or {}).get("direction", "")
    lines.append("<b>📉 往返总价走势（近7次采集）</b>")
    for index, (row, total) in enumerate(chart_rows):
        suffix = " ← 当前" if index == len(chart_rows) - 1 else ""
        lines.append(
            f"{_roundtrip_date_label(row)}  {_price_text(total)}  {_roundtrip_bar(total, min_price, max_price)}{suffix}"
        )
    if trend_label:
        lines.append(f"趋势：{trend_label}")
    lines.append("")


def _price_scale_lines(current_min, route_info: dict, analysis_result: dict) -> list[str]:
    price = _to_float(current_min)
    if price is None or price <= 0:
        return ["<b>💰 价格区间标尺</b>", "当前最低价：暂无有效价格数据", ""]

    target = (
        _to_float(analysis_result.get("target_price_effective"))
        or _to_float(_preference_value(route_info, analysis_result, "target_price"))
    )
    tolerance = (
        _to_float(analysis_result.get("price_tolerance"))
        or _to_float(_preference_value(route_info, analysis_result, "price_tolerance"))
        or 100
    )
    max_budget = (
        _to_float(analysis_result.get("max_budget"))
        or _to_float(_preference_value(route_info, analysis_result, "max_budget"))
        or _to_float(_preference_value(route_info, analysis_result, "budget"))
    )
    if target is None and max_budget is None:
        return []

    lines = ["<b>💰 价格区间标尺</b>"]
    if target:
        buy_upper = target + tolerance
        lines.append(f"¥{target:,.0f} 理想价")
        lines.append(f"├── ¥{target:,.0f}-{buy_upper:,.0f} 强烈建议买入区 ──┤")
        if max_budget:
            if buy_upper < max_budget:
                lines.append(f"├── ¥{buy_upper:,.0f}-{max_budget:,.0f} 可接受区间 ──┤")
            lines.append(f"└── ¥{max_budget:,.0f}以上 超预算 ──┘")
        advice = analysis_result.get("price_band") or {}
        label = advice.get("label")
        if not label:
            if price <= target:
                label = "低于理想价 🔥"
            elif price <= buy_upper:
                label = "在买入区间内 ✅"
            elif max_budget and price <= max_budget:
                label = "在可接受区间内 📊"
            else:
                label = "超预算 ❌"
        lines.append(f"当前最低价：¥{price:,.0f} → {label}")
    else:
        lines.append(f"当前最低价：¥{price:,.0f}")
    if max_budget:
        lines.append(f"最高可接受：¥{max_budget:,.0f}")
    lines.append("")
    return lines

def _best_decision_flight(analysis: dict | None) -> dict:
    analysis = analysis or {}
    candidates = (
        analysis.get("economy_recommendations")
        or analysis.get("recommendations")
        or analysis.get("all_flights")
        or []
    )
    normalized = []
    for item in candidates:
        flight = item.get("flight") if isinstance(item, dict) and item.get("flight") else item
        if isinstance(flight, dict) and _has_valid_price(flight.get("price")):
            normalized.append(flight)
    return sorted(normalized, key=lambda flight: _to_float(flight.get("price")) or 999999)[0] if normalized else {}


def _decision_prices(
    analysis_result: dict, route_info: dict, is_round_trip: bool
) -> tuple[float | None, float | None, float | None]:
    if is_round_trip:
        round_trip = analysis_result.get("round_trip_analysis") or {}
        current = _to_float(round_trip.get("total_min"))
        target_single = (
            _to_float(route_info.get("target_price"))
            or _to_float(analysis_result.get("target_price_effective"))
            or _to_float(analysis_result.get("target_price"))
        )
        max_single = (
            _to_float(route_info.get("max_budget"))
            or _to_float(route_info.get("budget"))
            or _to_float(analysis_result.get("max_budget"))
        )
        return (
            current,
            target_single * 2 if target_single else None,
            max_single * 2 if max_single else None,
        )

    current = (
        _to_float(analysis_result.get("current_min_price"))
        or _to_float((analysis_result.get("price_range") or [None])[0])
    )
    target = (
        _to_float(analysis_result.get("target_price_effective"))
        or _to_float(_preference_value(route_info, analysis_result, "target_price"))
    )
    max_budget = (
        _to_float(analysis_result.get("max_budget"))
        or _to_float(_preference_value(route_info, analysis_result, "max_budget"))
        or _to_float(_preference_value(route_info, analysis_result, "budget"))
    )
    return current, target, max_budget


def _action_zone_label(current, target, max_budget) -> str:
    current = _to_float(current)
    target = _to_float(target)
    max_budget = _to_float(max_budget)
    if current is None:
        return "暂无有效价格"
    if target and current <= target:
        return "强烈建议购买"
    if target and current <= target * 1.05:
        return "仍值得购买"
    if target and max_budget and current <= (target + max_budget) / 2:
        return "可以考虑"
    if max_budget and current <= max_budget:
        return "仅刚需建议"
    if max_budget and current > max_budget:
        return "不建议购买"
    return "需要人工确认"


def _action_threshold_lines(current, target, max_budget) -> list[str]:
    target = _to_float(target)
    max_budget = _to_float(max_budget)
    current = _to_float(current)
    if not target and not max_budget:
        return []
    lines = ["<b>🎯 你的操作区间：</b>"]
    if target:
        lines.append(f"≤ {_price_text(target)}：强烈建议验证并购买")
        lines.append(f"{_price_text(target)}-{_price_text(target * 1.05)}：仍值得购买")
    if target and max_budget:
        midpoint = (target + max_budget) / 2
        lines.append(f"{_price_text(target * 1.05)}-{_price_text(midpoint)}：可以考虑，但不是最佳价")
        lines.append(f"{_price_text(midpoint)}-{_price_text(max_budget)}：仅刚需建议")
    if max_budget:
        lines.append(f"> {_price_text(max_budget)}：不建议购买")
    if current:
        lines.append(f"当前价格 {_price_text(current)} → 落在【{_action_zone_label(current, target, max_budget)}】区间")
    lines.append("")
    return lines

def _confidence_lines(confidence: dict | None) -> list[str]:
    if not confidence:
        return []
    dimensions = confidence.get("dimensions") or {}
    details = confidence.get("details") or {}
    lines = [f"📊 数据置信度：{confidence.get('overall', '中')}"]
    for name in ["价格新鲜度", "历史样本量", "渠道一致性", "票规完整度", "可购买性"]:
        level = dimensions.get(name)
        if not level:
            continue
        icon = "✓" if level in {"高", "中高"} else "⚠"
        detail = details.get(name)
        suffix = f"（{detail}）" if detail else ""
        lines.append(f"{icon} {name}：{level}{suffix}")
    lines.append("")
    return lines

def _decision_context(
    analysis_result: dict,
    route_info: dict,
    source_stats: dict | None,
    price_insights: dict | None,
    is_round_trip: bool,
) -> tuple[dict, dict, float | None, float | None, float | None]:
    current, target, max_budget = _decision_prices(analysis_result, route_info, is_round_trip)
    if is_round_trip:
        round_trip = analysis_result.get("round_trip_analysis") or {}
        combo = (round_trip.get("top_combinations") or [{}])[0]
        best_flight = combo.get("outbound") or {}
        confidence = round_trip.get("confidence_breakdown") or calc_confidence(
            best_flight, source_stats, round_trip.get("history") or []
        )
        decision = round_trip.get("decision_summary") or generate_decision_summary(
            current,
            target,
            max_budget,
            confidence,
            best_flight.get("execution_grade"),
        )
        return decision, confidence, current, target, max_budget

    best_flight = _best_decision_flight(analysis_result)
    history = (price_insights or {}).get("price_history") if price_insights else None
    confidence = analysis_result.get("confidence_breakdown") or calc_confidence(
        best_flight, source_stats, history
    )
    decision = analysis_result.get("decision_summary") or generate_decision_summary(
        current,
        target,
        max_budget,
        confidence,
        best_flight.get("execution_grade"),
    )
    return decision, confidence, current, target, max_budget


def _append_decision_summary_card(
    lines: list[str],
    analysis_result: dict,
    route_info: dict,
    source_stats: dict | None,
    price_insights: dict | None,
    is_round_trip: bool,
) -> None:
    decision, confidence, current, target, max_budget = _decision_context(
        analysis_result, route_info, source_stats, price_insights, is_round_trip
    )
    conclusion = decision.get("conclusion") or "可以观察"
    price_judgment = decision.get("price_judgment") or "需要结合历史价格判断"
    exec_judgment = decision.get("execution_judgment") or "购买渠道或票规待确认"
    action_advice = decision.get("action_advice") or "先验证支付页最终价、行李和退改规则"
    availability = (confidence.get("dimensions") or {}).get("可购买性", "中")

    lines.append("━━━━━━━━━━━━━━")
    lines.append("<b>📌 当前判断</b>")
    lines.append("")
    lines.append(f"结论：{conclusion}")
    lines.append(f"置信度：{confidence.get('overall', decision.get('confidence', '中'))}")
    lines.append("")
    lines.append(f"{'当前往返总价' if is_round_trip else '当前价格'}：{_price_text(current)}")
    if target:
        lines.append(f"理想入手价：{_price_text(target)}")
    if max_budget:
        lines.append(f"最高可接受价：{_price_text(max_budget)}")
    lines.append("")
    lines.append(f"价格判断：{price_judgment}")
    lines.append(f"执行判断：{exec_judgment}")
    lines.append(f"行动建议：{action_advice}")
    lines.append(f"可购买性：{availability}")
    lines.append("")
    lines.append("一句话原因：")
    reasons = decision.get("reasons") or []
    if reasons:
        lines.append("；".join(reasons[:2]))
    else:
        lines.append("当前价格和执行信息需要结合支付页最终结果确认。")
    lines.append("━━━━━━━━━━━━━━")
    lines.append("")

    lines.extend(_action_threshold_lines(current, target, max_budget))
    lines.extend(_confidence_lines(confidence))
    lines.append("━━━ 以下为详细分析 ━━━")
    lines.append("")
    lines.append("<b>💡 为什么这样判断？</b>")
    for index, reason in enumerate((decision.get("reasons") or [])[:3], start=1):
        lines.append(f"{index}. {reason}")
    lines.append("")

def _route_is_domestic(route_info: dict | None) -> bool:
    route_info = route_info or {}
    cn_airports = {
        "PVG", "SHA", "PEK", "PKX", "CAN", "SZX", "CTU", "TFU", "HGH",
        "NKG", "XMN", "FOC", "WUH", "XIY", "CKG", "KMG", "TAO", "CSX",
        "CGO", "TSN",
    }
    cn_cities = {
        "涓婃捣", "鍖椾含", "骞垮窞", "娣卞湷", "鎴愰兘", "鏉窞", "鍗椾含", "鍘﹂棬", "绂忓窞",
        "姝︽眽", "瑗垮畨", "閲嶅簡", "鏄嗘槑", "闈掑矝", "闀挎矙", "閮戝窞", "澶╂触",
    }

    origin_codes = route_info.get("origin_airports") or [route_info.get("origin")]
    dest_codes = route_info.get("destination_airports") or [route_info.get("destination")]
    values = [str(item or "").strip().upper() for item in origin_codes + dest_codes if item]
    if not values:
        return False

    for value in values:
        if value in cn_airports:
            continue
        if value in cn_cities:
            continue
        return False
    return True


def _has_transfer_options(*analysis_results: dict | None) -> bool:
    for analysis in analysis_results:
        if not analysis:
            continue
        flights = []
        flights.extend(analysis.get("all_flights") or [])
        flights.extend(analysis.get("economy_recommendations") or [])
        flights.extend(analysis.get("recommendations") or [])
        for item in flights:
            flight = item.get("flight") if isinstance(item, dict) and "flight" in item else item
            if not isinstance(flight, dict):
                continue
            try:
                if int(flight.get("stops") or 0) > 0:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _history_count_for_limits(
    analysis_result: dict | None,
    price_insights: dict | None,
    is_round_trip: bool,
) -> int:
    analysis_result = analysis_result or {}
    if is_round_trip:
        round_trip_analysis = analysis_result.get("round_trip_analysis") or {}
        history = round_trip_analysis.get("history") or round_trip_analysis.get("trend_history") or []
        return len(history)
    if "constraint_price_history" in analysis_result:
        return len(analysis_result.get("constraint_price_history") or [])
    history = (price_insights or {}).get("price_history") or []
    return len(history)


def _append_judgment_limits(
    lines: list[str],
    route_info: dict,
    analysis_result: dict,
    price_insights: dict | None,
    is_round_trip: bool,
    return_analysis: dict | None = None,
) -> None:
    limits = ["显示价格仍需支付页最终确认"]
    if not _route_is_domestic(route_info):
        limits.append("国际航线票规可能存在渠道差异")
    if _has_transfer_options(analysis_result, return_analysis):
        limits.append("如涉及中转，需确认是否联程及是否需要过境签")

    history_count = _history_count_for_limits(analysis_result, price_insights, is_round_trip)
    if history_count >= 14:
        limits.append("历史价格反映相似区间，不代表未来必然重复")
    else:
        limits.append("历史样本仍在积累，价格区间判断会随数据增多而更稳定")

    lines.append("<b>⚠️ 当前判断的限制：</b>")
    for item in limits[:3]:
        lines.append(f"- {item}")
    lines.append("")


def _section(lines: list[str], title: str | None = None) -> None:
    lines.append("━━━━━━━━━━━━━━━━")
    if title:
        lines.append(title)
        lines.append("")


CARD_STYLE = "border:1px solid #ddd;border-radius:8px;padding:12px;margin:8px 0;"
PRIMARY_TITLE_STYLE = "font-weight:bold;color:#2563eb;"
ACTION_STYLE = "margin-top:6px;color:#16a34a;"
ACTION_ZONE_STYLE = (
    "border-left:4px solid #16a34a;padding:8px;margin:8px 0;background:#f0fdf4;"
)

AIRLINE_NAMES = {
    "9C": "春秋航空",
    "MU": "东方航空",
    "CA": "中国国际航空",
    "CZ": "南方航空",
    "HO": "吉祥航空",
    "MM": "乐桃航空",
    "NH": "全日空",
    "JL": "日本航空",
    "OZ": "韩亚航空",
    "KE": "大韩航空",
    "CI": "中华航空",
    "BR": "长荣航空",
    "PR": "菲律宾航空",
    "MF": "厦门航空",
    "SC": "山东航空",
    "FM": "上海航空",
    "ZH": "深圳航空",
    "3U": "四川航空",
    "HU": "海南航空",
    "AA": "美国航空",
    "UA": "美联航",
    "DL": "达美航空",
    "AC": "加拿大航空",
}

AIRPORT_SHORT_DISPLAY = {
    "PVG": "浦东",
    "SHA": "虹桥",
    "KIX": "关西",
    "ITM": "伊丹",
    "NRT": "成田",
    "HND": "羽田",
    "ICN": "仁川",
    "GMP": "金浦",
    "PEK": "首都",
    "PKX": "大兴",
    "CAN": "白云",
    "SZX": "宝安",
    "HKG": "香港",
    "TPE": "桃园",
    "SIN": "樟宜",
    "BKK": "素万那普",
    "LAX": "洛杉矶",
    "SFO": "旧金山",
    "JFK": "肯尼迪",
    "DFW": "达拉斯",
    "MCO": "奥兰多",
}

AIRPORT_LOCAL_CITY = {
    "PVG": "上海",
    "SHA": "上海",
    "KIX": "大阪",
    "ITM": "大阪",
    "NRT": "东京",
    "HND": "东京",
    "ICN": "首尔",
    "GMP": "首尔",
    "PEK": "北京",
    "PKX": "北京",
    "CAN": "广州",
    "SZX": "深圳",
    "HKG": "香港",
    "TPE": "台北",
    "SIN": "新加坡",
    "BKK": "曼谷",
    "LAX": "洛杉矶",
    "SFO": "旧金山",
    "JFK": "纽约",
    "DFW": "达拉斯",
    "MCO": "奥兰多",
}


def _compact_link_text(link_text: str, limit: int = 4) -> str:
    parts = [part for part in str(link_text or "").split(" | ") if part.strip()]
    return " | ".join(parts[:limit])


def _flight_link_text(flight: dict, route_info: dict, limit: int = 4) -> str:
    date_str = route_info.get("depart_date") if isinstance(route_info, dict) else None
    link_text = _combo_full_booking_links(flight or {}, date_str)
    if not link_text:
        origin = (route_info or {}).get("origin", "")
        dest = (route_info or {}).get("destination", "")
        link_text = generate_booking_links(origin, dest, date_str or "")
    return _compact_link_text(link_text, limit)


def generate_booking_links(
    origin,
    dest,
    date_str,
    flight_no: str = "",
    origin_city: str = "",
    dest_city: str = "",
    cabin: str = "economy",
    flight: dict | None = None,
) -> str:
    origin = str(origin or "")
    dest = str(dest or "")
    if not origin_city:
        origin_city = get_airport_city(origin) or AIRPORT_CITY.get(origin, origin)
    if not dest_city:
        dest_city = get_airport_city(dest) or AIRPORT_CITY.get(dest, dest)
    origin_en = get_airport_city_en(origin) or AIRPORT_CITY_EN.get(origin, origin)
    dest_en = get_airport_city_en(dest) or AIRPORT_CITY_EN.get(dest, dest)
    links = []
    ctrip_url = f"https://flights.ctrip.com/online/list/oneway-{origin}-{dest}?depdate={date_str}&cabin=y_s"
    links.append(f'<a href="{ctrip_url}" target="_blank">携程</a>')
    fliggy_url = f"https://s.fliggy.com/search?keyword={quote(str(origin_city) + '到' + str(dest_city) + '机票 ' + str(date_str))}"
    links.append(f'<a href="{fliggy_url}" target="_blank">飞猪</a>')
    qunar_url = (
        "https://flight.qunar.com/site/oneway_list.htm"
        f"?searchDepartureAirport={quote(str(origin_city))}"
        f"&searchArrivalAirport={quote(str(dest_city))}"
        f"&searchDepartureTime={date_str}"
    )
    links.append(f'<a href="{qunar_url}" target="_blank">去哪儿</a>')
    trip_url = (
        f"https://www.trip.com/flights/{origin.lower()}-to-{dest.lower()}/tickets-{origin.lower()}-{dest.lower()}"
        f"?dcity={origin}&acity={dest}&ddate={date_str}&class=Y"
    )
    links.append(f'<a href="{trip_url}" target="_blank">Trip.com</a>')
    sky_url = f"https://www.tianxun.com/transport/flights/{origin}/{dest}/{date_str}/?adultsv2=1&cabinclass={cabin}&currency=CNY"
    links.append(f'<a href="{sky_url}" target="_blank">天巡</a>')
    google_url = (
        "https://www.google.com/travel/flights"
        f"?q=flights+from+{quote_plus(str(origin_en))}+to+{quote_plus(str(dest_en))}+on+{date_str}"
        "&curr=CNY&hl=zh-CN"
    )
    links.append(f'<a href="{google_url}" target="_blank">Google Flights</a>')
    return " | ".join(links)


def _verified_booking_options(flight: dict | None) -> list[dict]:
    options = (flight or {}).get("booking_options") or []
    return [option for option in options if isinstance(option, dict)]


def _option_price(option: dict | None):
    option = option or {}
    return _to_float(option.get("price") or option.get("amount") or option.get("total_amount"))


def _channel_names(limit: int = 4) -> str:
    return " / ".join(["携程", "飞猪", "去哪儿", "Trip.com"][:limit])


def _status_span(text: str, color: str = "#16a34a") -> str:
    return f'<span style="color:{color};font-weight:bold;">{text}</span>'


def _constraint_match_text(*flights: dict) -> str:
    grades = [flight.get("execution_grade") for flight in flights if isinstance(flight, dict)]
    return "否" if "D" in grades else "是"


def _card_title(label: str, variant: str = "推荐", primary: bool = True) -> str:
    style = PRIMARY_TITLE_STYLE if primary else "font-weight:bold;"
    return f'<div style="{style}">{label} ｜ {variant}</div>'


def _combo_leg_line(prefix: str, flight: dict, date_str: str | None) -> str:
    return (
        f"{prefix}: {_compact_flight_numbers(flight)} {_round_trip_airline_text(flight)} | "
        f"{_flight_combo_time_text(flight, date_str)} | "
        f"{_round_trip_stops_text(flight)} | {_price_text(flight.get('price'))}"
    )


def _combo_transaction_total(combo: dict) -> float | None:
    transaction_total = _to_float(combo.get("transaction_total"))
    if transaction_total is not None:
        return transaction_total
    outbound = combo.get("outbound") or {}
    return_flight = combo.get("return") or {}
    outbound_est = _estimated_price_value(outbound) or combo.get("outbound_price")
    return_est = _estimated_price_value(return_flight) or combo.get("return_price")
    if _has_valid_price(outbound_est) and _has_valid_price(return_est):
        return float(outbound_est) + float(return_est)
    return None


def _round_trip_combo_tags(combo: dict, route_info: dict, confidence: dict | None) -> str:
    legs = [combo.get("outbound") or {}, combo.get("return") or {}]
    lcc_tag = (
        "含廉航段"
        if _combo_lcc_summary(*legs).get("has_lcc")
        else ""
    )
    domestic_tags = []
    for flight in legs:
        for tag in flight.get("domestic_tags") or []:
            if tag and tag not in domestic_tags:
                domestic_tags.append(str(tag))
    if domestic_tags:
        return _append_status_tag(" | ".join(domestic_tags[:4]), lcc_tag)

    total = _to_float(combo.get("total_price"))
    target = _to_float(route_info.get("target_price"))
    max_budget = _to_float(route_info.get("max_budget") or route_info.get("budget"))
    budget_scope = _normalize_payload_budget_scope(
        route_info.get("max_budget_scope") or route_info.get("budget_scope")
    )
    if total is None or budget_scope == "all":
        price_label = "价格待判断"
    else:
        decision = evaluate_purchase_budget(
            total,
            target,
            max_budget,
            price_scope="per_person_roundtrip",
            budget_scope="per_person_roundtrip",
        )
        if decision["status"] == "at_or_below_target":
            price_label = "价格偏低"
        elif decision["status"] == "within_budget":
            price_label = "预算内"
        elif decision["status"] == "over_budget":
            price_label = "超预算"
        elif target and total <= target * 1.05:
            price_label = "接近理想"
        elif target and total <= target * 1.25:
            price_label = "价格中等"
        else:
            price_label = "价格偏高"

    availability_labels = [_status_availability_label(flight) for flight in legs if flight]
    availability_labels = [label for label in availability_labels if label]
    if "需刷新" in availability_labels:
        availability = "需刷新"
    elif (
        availability_labels
        and len(availability_labels) == len([flight for flight in legs if flight])
        and all(label == "可购买" for label in availability_labels)
    ):
        availability = "可购买"
    else:
        availability = "可买性待确认"

    risk_labels = [_status_risk_label(flight) for flight in legs if flight]
    if "风险高" in risk_labels:
        risk = "风险高"
    elif "风险中" in risk_labels:
        risk = "风险中"
    else:
        risk = "风险低"

    return _append_status_tag(
        " | ".join(
            [
                price_label,
                availability,
                f"置信度{(confidence or {}).get('overall', '中')}",
                risk,
            ]
        ),
        lcc_tag,
    )

def _combo_direct_first_key(combo: dict) -> tuple[int, float]:
    legs = [combo.get("outbound") or {}, combo.get("return") or {}]
    direct = all(int(flight.get("stops") or 0) == 0 for flight in legs if flight)
    return (0 if direct else 1, _to_float(combo.get("total_price")) or 999999)


def _round_trip_combinations(analysis_result: dict) -> list[dict]:
    round_trip = analysis_result.get("round_trip_analysis") or {}
    combos = [combo for combo in (round_trip.get("top_combinations") or []) if combo]
    if not combos and round_trip.get("same_day_time_conflict"):
        return []
    if not combos and "mixed_cabin_matching" in round_trip:
        return []
    if not combos:
        outbound_flights = (round_trip.get("outbound_top3") or _round_trip_top_flights(analysis_result))[:3]
        return_analysis = analysis_result.get("return_analysis") or {}
        return_flights = (round_trip.get("return_top3") or _round_trip_top_flights(return_analysis))[:3]
        for outbound in outbound_flights:
            for return_flight in return_flights:
                outbound_price = _to_float(outbound.get("price"))
                return_price = _to_float(return_flight.get("price"))
                if outbound_price is None or return_price is None:
                    continue
                combos.append(
                    {
                        "outbound": outbound,
                        "return": return_flight,
                        "outbound_price": outbound_price,
                        "return_price": return_price,
                        "total_price": outbound_price + return_price,
                    }
                )
    return sorted(combos, key=_combo_direct_first_key)


def _combo_human_recommendation(combo: dict, route_info: dict) -> str:
    explicit_advice = combo.get("purchase_advice") or {}
    if isinstance(explicit_advice, dict) and explicit_advice.get("conclusion"):
        return str(explicit_advice["conclusion"])
    if combo.get("buy_condition"):
        return str(combo["buy_condition"])
    total = _to_float(combo.get("total_price"))
    target = _to_float(route_info.get("target_price"))
    max_budget = _to_float(route_info.get("max_budget") or route_info.get("budget"))
    if total is None:
        return "建议等待 - 当前总价仍需确认"
    budget_scope = _normalize_payload_budget_scope(
        route_info.get("max_budget_scope") or route_info.get("budget_scope")
    )
    if budget_scope == "all":
        return "请按完整往返总价与整单预算核对后决定"
    decision = evaluate_purchase_budget(
        total,
        target,
        max_budget,
        price_scope="per_person_roundtrip",
        budget_scope="per_person_roundtrip",
    )
    advice = build_execution_advice(
        total,
        total,
        _payload_verify_price(total, max_budget),
        target,
        max_budget,
        budget_decision=decision,
        price_scope="per_person_roundtrip",
        budget_scope="per_person_roundtrip",
    )
    return str(advice.get("conclusion") or "请核对完整往返总价后决定")


def _single_flights_for_sections(analysis_result: dict) -> list[dict]:
    candidates = (
        analysis_result.get("economy_recommendations")
        or analysis_result.get("recommendations")
        or analysis_result.get("all_flights")
        or []
    )
    flights = []
    for item in candidates:
        flight = item.get("flight") if isinstance(item, dict) and item.get("flight") else item
        if isinstance(flight, dict) and _has_valid_price(flight.get("price")):
            flights.append(flight)
    return sorted(
        flights,
        key=lambda flight: (
            0 if int(flight.get("stops") or 0) == 0 else 1,
            _to_float(flight.get("price")) or 999999,
        ),
    )


def _single_option_lines(
    lines: list[str],
    flight: dict,
    label: str,
    route_info: dict,
    analysis_result: dict,
    link_limit: int = 4,
    variant: str = "推荐",
    primary: bool = True,
) -> None:
    links = _flight_link_text(flight, route_info, link_limit)
    lines.append(f'<div style="{CARD_STYLE}">')
    lines.append(_card_title(label, variant, primary))
    lines.append(f"<div>价格：{_flight_price_text(flight)}</div>")
    lines.append(
        f"<div>{_compact_flight_numbers(flight)} {_round_trip_airline_text(flight)} | "
        f"{_flight_combo_time_text(flight, route_info.get('depart_date'))} | "
        f"{_round_trip_stops_text(flight)}</div>"
    )
    lines.append(f"<div>渠道：{_channel_names(link_limit)}</div>")
    lines.append(
        f"<div>可购买性：{_status_availability_label(flight).replace('可买性', '')} | "
        f"执行风险：{_status_risk_label(flight).replace('风险', '')} | "
        f"符合约束：{_constraint_match_text(flight)}</div>"
    )
    lines.append(f"<div>🏷 {_flight_status_tags(flight, route_info, analysis_result)}</div>")
    estimate_lines = _price_estimate_summary_lines(flight)
    if estimate_lines:
        for item in estimate_lines[:2]:
            lines.append(f"<div>{item}</div>")
    lines.append(f'<div style="{ACTION_STYLE}">操作建议：{_human_recommendation_text(flight, route_info, analysis_result)}</div>')
    if links:
        lines.append(f'<div style="margin-top:4px;">🔗 {links}</div>')
    lines.append("</div>")

def _round_trip_combo_option_lines(
    lines: list[str],
    combo: dict,
    label: str,
    route_info: dict,
    confidence: dict | None,
    link_limit: int = 4,
    variant: str = "推荐",
    primary: bool = True,
) -> None:
    outbound = combo.get("outbound") or {}
    return_flight = combo.get("return") or {}
    total = _to_float(combo.get("total_price"))
    transaction_total = _combo_transaction_total(combo)
    extra = transaction_total - total if transaction_total is not None and total is not None else None

    lines.append(f'<div style="{CARD_STYLE}">')
    lines.append(_card_title(label, variant, primary))
    if transaction_total is not None:
        lines.append(f"<div>往返总价：{_price_text(total)}（去{_price_text(outbound.get('price'))} + 回{_price_text(return_flight.get('price'))}）</div>")
        if extra and extra > 0:
            lines.append(f"<div>预估实付：{_price_text(transaction_total)}（含额外费用）</div>")
        else:
            lines.append(f"<div>预估实付：{_price_text(transaction_total)}（无额外费用）</div>")
    else:
        lines.append(f"<div>往返总价：{_price_text(total)}</div>")
    lines.append(f"<div>{_combo_leg_line('去', outbound, route_info.get('depart_date'))}</div>")
    lines.append(f"<div>{_combo_leg_line('回', return_flight, route_info.get('return_date'))}</div>")
    lines.append(f"<div>渠道：{_channel_names(link_limit)}</div>")
    tags = _round_trip_combo_tags(combo, route_info, confidence)
    combo_risk = "高" if "风险高" in tags else ("中" if "风险中" in tags else "低")
    lines.append(f"<div>可购买性：中高 | 执行风险：{combo_risk} | 符合约束：{_constraint_match_text(outbound, return_flight)}</div>")
    lines.append(f"<div>🏷 {tags}</div>")
    lines.append(f'<div style="{ACTION_STYLE}">操作建议：{_combo_human_recommendation(combo, route_info)}</div>')
    outbound_links = _compact_link_text(_combo_full_booking_links(outbound, route_info.get("depart_date")), link_limit)
    return_links = _compact_link_text(_combo_full_booking_links(return_flight, route_info.get("return_date")), link_limit)
    if outbound_links:
        lines.append(f'<div style="margin-top:4px;">🔗 去程：{outbound_links}</div>')
    if return_links:
        lines.append(f'<div>🔗 返程：{return_links}</div>')
    lines.append("</div>")

def _confidence_compact_lines(confidence: dict | None) -> list[str]:
    confidence = confidence or {}
    dimensions = confidence.get("dimensions") or {}
    if not dimensions:
        return []
    positive = []
    warnings = []
    for name in ["价格新鲜度", "渠道一致性", "历史样本量", "票规完整度", "可购买性"]:
        level = dimensions.get(name)
        if not level:
            continue
        item = f"{name}：{level}"
        if level in {"高", "中高", "中"}:
            positive.append(f"✓ {item}")
        else:
            warnings.append(f"⚠ {item}")
    lines = ["数据置信度构成："]
    if positive:
        lines.append(" | ".join(positive[:3]))
    if warnings:
        lines.append(" | ".join(warnings[:3]))
    return lines

def _last_push_route_parts(route_info: dict, is_round_trip: bool) -> tuple[str, str, str | None]:
    origin = route_info.get("origin") or route_info.get("origin_city") or ""
    dest = route_info.get("destination") or route_info.get("destination_city") or ""
    route = route_info.get("route") or f"{origin}-{dest}"
    depart_date = route_info.get("depart_date") or ""
    return_date = route_info.get("return_date") if is_round_trip else None
    return route, depart_date, return_date


def _first_nonempty_identity(*values):
    for value in values:
        if value is not None and str(value).strip():
            return value
    return None


def _notification_subscription_id(route_info: dict, subscription: dict):
    return _first_nonempty_identity(
        (route_info or {}).get("subscription_id"),
        (subscription or {}).get("id"),
        (subscription or {}).get("subscription_id"),
        (subscription or {}).get("_index"),
    )


def _price_history_for_push(price_insights: dict | None, analysis_result: dict, is_round_trip: bool):
    if is_round_trip:
        round_trip = analysis_result.get("round_trip_analysis") or {}
        for key in ("history", "price_history"):
            if key in round_trip:
                return round_trip.get(key) or []
    if "constraint_price_history" in analysis_result:
        return analysis_result.get("constraint_price_history") or []
    return (price_insights or {}).get("price_history") or analysis_result.get("price_history") or []


def _price_signal_provenance_metadata(
    signal_history,
    price_insights: dict | None,
    analysis_result: dict,
    is_round_trip: bool,
) -> dict:
    """描述 price signal 实际使用的完整历史，不借图表14条截断猜样本。"""
    if signal_history is not None:
        raw_history = signal_history or []
    elif is_round_trip:
        round_trip = (analysis_result or {}).get("round_trip_analysis") or {}
        raw_history = (
            round_trip.get("history")
            or round_trip.get("price_history")
            or []
        )
    else:
        raw_history = (
            (analysis_result or {}).get("constraint_price_history")
            or (price_insights or {}).get("price_history")
            or (analysis_result or {}).get("price_history")
            or []
        )

    def history_price(value):
        if isinstance(value, dict):
            value = value.get("total") or value.get("price") or value.get("min_price")
        elif isinstance(value, (list, tuple)) and len(value) >= 2:
            value = value[1]
        return _to_float(value)

    valid_count = sum(
        1
        for value in (signal_history or [])
        if (history_price(value) or 0) > 0
    )
    sources = set()
    for item in raw_history:
        if not isinstance(item, dict):
            continue
        for key in ("sources", "source", "price_source", "data_source"):
            raw_sources = item.get(key)
            values = raw_sources if isinstance(raw_sources, (list, tuple, set)) else str(raw_sources or "").replace("|", "+").split("+")
            sources.update(str(source).strip().lower() for source in values if str(source).strip())
    return {
        "sample_n": valid_count,
        "window": history_observation_window(raw_history),
        "sources": sorted(sources),
        "_provenance_history": list(raw_history),
    }


def _push_title_text(push_meta: dict, route_info: dict, current, is_round_trip: bool) -> str:
    push_type = (push_meta or {}).get("type") or "价格提醒"
    origin = route_info.get("origin_city") or get_airport_city(route_info.get("origin", "")) or route_info.get("origin", "")
    dest = route_info.get("destination_city") or get_airport_city(route_info.get("destination", "")) or route_info.get("destination", "")
    if is_round_trip:
        return f"【{push_type}】{origin} → {dest} 往返{_price_text(current)}"
    return f"【{push_type}】{origin} → {dest} {_price_text(current)}"


def _confidence_deduction_text(confidence: dict) -> str:
    dimensions = (confidence or {}).get("dimensions") or {}
    lows = [str(key) for key, value in dimensions.items() if value in {"低", "待确认"}]
    return "、".join(lows[:2]) + "仍需确认" if lows else ""


def _recommendation_price_line(analysis_result: dict, current, is_round_trip: bool) -> str:
    if not is_round_trip:
        return _price_text(current)
    round_trip = analysis_result.get("round_trip_analysis") or {}
    outbound_min = round_trip.get("outbound_min")
    return_min = round_trip.get("return_min")
    if outbound_min is not None and return_min is not None:
        return f"往返{_price_text(current)}（去{_price_text(outbound_min)} + 回{_price_text(return_min)}）"
    return f"往返{_price_text(current)}"


def _append_action_header_section(
    lines: list[str],
    push_meta: dict,
    route_info: dict,
    decision: dict,
    confidence: dict,
    current,
    target,
    max_budget,
    analysis_result: dict,
    is_round_trip: bool,
) -> None:
    title = _push_title_text(push_meta, route_info, current, is_round_trip)
    verify_limit = _to_float(current)
    verify_limit = verify_limit * 1.05 if verify_limit else None
    risk_hint = _confidence_deduction_text(confidence) or "票规/渠道需确认"
    lines.append(f"<b>{title}</b>")
    lines.append("")
    lines.append(f"当前建议：{decision.get('conclusion', '可以观察')}")
    lines.append(f"推荐方案：{_recommendation_price_line(analysis_result, current, is_round_trip)}")
    lines.append(f"购买条件：支付页≤{_price_text(verify_limit)}且含托运行李")
    lines.append(f"置信度：{confidence.get('overall', decision.get('confidence', '中'))}")
    lines.append(f"主要风险：{risk_hint}")
    if target:
        target_label = "理想总价" if is_round_trip else "理想入手价"
        status = "已达标" if current is not None and current <= target else "未达标"
        lines.append(f"{target_label}：{_price_text(target)} | {status}")
    if max_budget:
        lines.append(f"最高可接受：{_price_text(max_budget)}")
    lines.append("")

def _append_push_reason_section(lines: list[str], push_meta: dict) -> None:
    _section(lines, "<b>馃搷 涓轰粈涔堢幇鍦ㄦ彁閱掍綘?</b>")
    reasons = (push_meta or {}).get("reasons") or ["褰撳墠浠锋牸鎴栨柟妗堢姸鎬佽Е鍙戜簡浣犵殑鐩戞帶鏉′欢"]
    for reason in reasons[:4]:
        lines.append(f"- {reason}")
    lines.append("")


def _append_price_change_section(
    lines: list[str],
    current,
    target,
    max_budget,
    push_meta: dict,
    is_round_trip: bool,
) -> None:
    _section(lines, "<b>价格变化</b>")
    current_label = "当前往返价" if is_round_trip else "当前价"
    lines.append(f"{current_label}：{_price_text(current)}")
    change = (push_meta or {}).get("price_change") or {}
    if change:
        scope_suffix = _price_change_scope_suffix(change)
        lines.append(f"上次提醒：{_price_text(change.get('last'))}{scope_suffix}")
        diff = _to_float(change.get("diff"))
        if diff is not None:
            if diff < 0:
                lines.append(f"下降：{_price_text(abs(diff))}{scope_suffix}")
            elif diff > 0:
                lines.append(f"上涨：{_price_text(diff)}{scope_suffix}")
            else:
                lines.append(f"变化：持平{scope_suffix}")
    else:
        lines.append("上次提醒：暂无记录")
    if target:
        target_label = "你的理想总价" if is_round_trip else "你的理想价"
        lines.append(f"{target_label}：{_price_text(target)}")
        if current is not None and current <= target:
            lines.append(f"→ 当前低于理想价{_price_text(target - current)}，在强烈建议区间")
    if max_budget:
        max_label = "最高可接受总价" if is_round_trip else "最高可接受"
        lines.append(f"{max_label}：{_price_text(max_budget)}")
    lines.append("")


def _append_validity_section(lines: list[str], analysis_result: dict, route_info: dict, primary_flight: dict | None) -> None:
    _section(lines, "<b>推荐有效期</b>")
    lines.append(f"价格更新时间：{_message_collected_time(analysis_result, route_info)}")
    age = ((primary_flight or {}).get("availability") or {}).get("age_minutes")
    try:
        age_value = int(age)
    except (TypeError, ValueError):
        age_value = None
    if age_value is not None and age_value > 120:
        lines.append("该价格已超过2小时未验证，仅供参考，请以支付页为准")
    else:
        lines.append("建议有效期：30分钟")
        lines.append("超过有效期请在支付页重新确认")
    lines.append("")


def _notification_frequency(route_info: dict, analysis_result: dict) -> str:
    goals = (
        route_info.get("notification_goals")
        or analysis_result.get("notification_goals")
        or {}
    )
    if isinstance(goals, dict):
        value = goals.get("frequency") or "important_only"
        return {
            "daily_summary": "daily_digest",
            "every_change": "price_change",
        }.get(value, value)
    return "important_only"


def _resolved_detail_level(route_info: dict, analysis_result: dict, detail_level: str | None) -> str:
    if detail_level in {"short", "full"}:
        return detail_level
    frequency = _notification_frequency(route_info, analysis_result)
    return "full" if frequency == "daily_digest" else "short"


def _subscription_edit_url(route_info: dict) -> str:
    base = _subscription_form_url(route_info).rstrip("/")
    sub_id = (
        route_info.get("subscription_id")
        or route_info.get("id")
        or route_info.get("_index")
        or route_info.get("index")
    )
    if sub_id is not None and str(sub_id) != "":
        return f"{base}/?edit={quote(str(sub_id))}"
    return base


def _feedback_url(route_info: dict) -> str:
    base = _subscription_form_url(route_info).rstrip("/")
    sub_id = (
        route_info.get("subscription_id")
        or route_info.get("id")
        or route_info.get("_index")
        or route_info.get("index")
        or _last_push_route_parts(route_info, bool(route_info.get("round_trip")))[0]
    )
    return f"{base}/feedback?sub={quote(str(sub_id))}"


def _primary_booking_links_for_action(route_info: dict, primary_flight: dict | None, limit: int = 3) -> str:
    if primary_flight:
        links = _flight_link_text(primary_flight, route_info, limit)
        if links:
            return links
    origin = route_info.get("origin", "")
    dest = route_info.get("destination", "")
    date_str = route_info.get("depart_date", "")
    return generate_booking_links(origin, dest, date_str)


def _append_action_links_section(
    lines: list[str],
    route_info: dict,
    primary_flight: dict | None,
    is_round_trip: bool,
) -> None:
    _section(lines, "<b>下一步操作</b>")
    edit_url = _subscription_edit_url(route_info)
    feedback_url = _feedback_url(route_info)
    links = _primary_booking_links_for_action(route_info, primary_flight, 3)
    if is_round_trip:
        lines.append(f"重新验证价格：去程 {links}")
    else:
        lines.append(f"重新验证价格：{links}")
    lines.append(f'修改监控偏好：<a href="{edit_url}" target="_blank">打开订阅表单</a>')
    lines.append("购买前请确认：最终价、托运行李、退改签、是否联程")
    lines.append(f'反馈买不到/价格不对：<a href="{feedback_url}" target="_blank">反馈</a>')
    interval = route_info.get("check_interval_hours") or os.environ.get("CHECK_INTERVAL_HOURS") or "6"
    lines.append(f"系统每隔{interval}小时自动检查一次，价格有重要变化会再次提醒你。")
    lines.append("")


def _snapshot_channels(flight: dict | None) -> list[str]:
    if not flight:
        return []
    options = flight.get("booking_options") or []
    channels = [
        str(option.get("platform"))
        for option in options
        if isinstance(option, dict) and option.get("platform")
    ]
    if channels:
        return sorted(set(channels))
    source = str(flight.get("data_source") or flight.get("source") or "")
    return sorted({item for item in source.split("+") if item})


def _snapshot_fare_status(flight: dict | None) -> str:
    if not flight:
        return ""
    fare = flight.get("fare_verification") or {}
    matches = " ".join(fare.get("matches") or [])
    issues = " ".join(fare.get("issues") or [])
    if "托运" in matches or "行李" in matches:
        return "已确认含托运行李"
    if "托运" in issues or "行李" in issues:
        return "行李待确认"
    return fare.get("label") or fare.get("level") or "票规待确认"


def _append_last_push_difference_section(
    lines: list[str],
    last_snapshot: dict | None,
    current,
    confidence: dict,
    primary_flight: dict | None,
) -> None:
    _section(lines, "<b>与上次提醒的区别</b>")
    if not last_snapshot:
        lines.append("这是该航线的首次提醒。")
        lines.append("")
        return

    try:
        pushed_at = datetime.fromisoformat(str(last_snapshot.get("pushed_at")))
        days_ago = (datetime.now() - pushed_at).days
        time_text = f"{days_ago}天前" if days_ago else "上次"
    except (TypeError, ValueError):
        time_text = "上次"
    lines.append(f"相比上次提醒（{time_text}）：")

    old_price = _to_float(last_snapshot.get("price"))
    now_price = _to_float(current)
    if old_price and now_price:
        diff = now_price - old_price
        if diff < 0:
            lines.append(f"- 价格下降{_price_text(abs(diff))}")
        elif diff > 0:
            lines.append(f"- 价格上涨{_price_text(diff)}")
        else:
            lines.append("- 价格持平")

    old_conf = last_snapshot.get("confidence")
    new_conf = (confidence or {}).get("overall")
    if old_conf and new_conf and old_conf != new_conf:
        lines.append(f"- 置信度从{old_conf}变为{new_conf}")

    try:
        old_channels = set(json.loads(last_snapshot.get("channels") or "[]"))
    except json.JSONDecodeError:
        old_channels = set()
    new_channels = set(_snapshot_channels(primary_flight))
    added = sorted(new_channels - old_channels)
    if added:
        lines.append(f"- 新增可购买渠道：{'、'.join(added[:3])}")

    old_fare = last_snapshot.get("fare_status")
    new_fare = _snapshot_fare_status(primary_flight)
    if new_fare and old_fare != new_fare:
        lines.append(f"- 票规状态：{new_fare}")
    lines.append("")


def _save_current_push_snapshot(
    route_key: str,
    depart_key: str,
    return_key: str | None,
    current,
    confidence: dict,
    primary_flight: dict | None,
    push_meta: dict,
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    save_last_push_price(
        route_key,
        depart_key,
        return_key,
        current,
        (push_meta or {}).get("type"),
        now,
    )
    save_push_snapshot(
        route_key,
        depart_key,
        return_key,
        current,
        (confidence or {}).get("overall"),
        _snapshot_channels(primary_flight),
        _snapshot_fare_status(primary_flight),
        (push_meta or {}).get("type"),
        now,
    )


def _append_current_judgment_section(
    lines: list[str],
    analysis_result: dict,
    route_info: dict,
    source_stats: dict | None,
    price_insights: dict | None,
    is_round_trip: bool,
) -> tuple[dict, dict, float | None, float | None, float | None]:
    decision, confidence, current, target, max_budget = _decision_context(
        analysis_result, route_info, source_stats, price_insights, is_round_trip
    )
    conclusion = decision.get("conclusion", "可以观察")
    verify_limit = _to_float(current)
    verify_limit = verify_limit * 1.05 if verify_limit else None
    risk_hint = "票规/渠道待确认"
    if is_round_trip:
        round_trip = analysis_result.get("round_trip_analysis") or {}
        outbound_min = round_trip.get("outbound_min")
        return_min = round_trip.get("return_min")
        recommend_line = (
            f"方案A 往返{_price_text(current)}（去{_price_text(outbound_min)} + 回{_price_text(return_min)}）"
        )
        condition_line = f"支付页≤{_price_text(verify_limit)}且含托运行李"
        label = "理想总价"
    else:
        recommend_line = f"方案A {_price_text(current)}"
        condition_line = f"支付页≤{_price_text(verify_limit)}且含托运行李"
        label = "理想价"

    lines.append(
        '<div style="border:1px solid #dbeafe;border-radius:8px;'
        'padding:12px;margin:8px 0;background:#eff6ff;">'
    )
    lines.append(f"<div>当前建议：{conclusion}</div>")
    lines.append(f"<div>推荐方案：{recommend_line}</div>")
    lines.append(f"<div>购买条件：{condition_line}</div>")
    lines.append(
        f"<div>置信度：{confidence.get('overall', decision.get('confidence', '中'))}，"
        f"主要风险是{risk_hint}</div>"
    )
    if target:
        status = (
            _status_span("已达标", "#16a34a")
            if current is not None and current <= target
            else _status_span("未达标", "#dc2626")
        )
        lines.append(f"<div>{label}：{_price_text(target)} | {status}</div>")
    lines.append("</div>")
    return decision, confidence, current, target, max_budget


def _append_operation_section(
    lines: list[str],
    decision: dict,
    current,
    target,
    max_budget,
    is_round_trip: bool,
) -> None:
    _section(lines, "<b>操作建议</b>")
    verify_limit = _to_float(current)
    if verify_limit:
        verify_limit *= 1.05
        price_name = "往返总价" if is_round_trip else "最终价"
        lines.append(
            f"若支付页{price_name}≤{_price_text(verify_limit)}且含托运行李，可以购买前验证。"
        )
    if max_budget:
        price_name = "总价" if is_round_trip else "价格"
        lines.append(f"若{price_name}涨到{_price_text(max_budget)}以上，建议保持本条航线监控。")
    advice = decision.get("action_advice")
    if advice:
        lines.append(advice)
    lines.append("")
    price_label = "往返总价" if is_round_trip else "价格"
    lines.append(f'<div style="{ACTION_ZONE_STYLE}">')
    lines.append(f"<div>你的价格行动区间（{price_label}）：</div>")
    if current and max_budget:
        midpoint = (current + max_budget) / 2
        lines.append(
            f"<div>≤{_price_text(current)} 强烈建议验证并购买</div>"
        )
        lines.append(
            f"<div>{_price_text(current)}-{_price_text(verify_limit or current)} 值得购买</div>"
        )
        lines.append(
            f"<div>{_price_text(verify_limit or current)}-{_price_text(midpoint)} 可以考虑</div>"
        )
        lines.append(
            f"<div>{_price_text(midpoint)}-{_price_text(max_budget)} 仅刚需建议</div>"
        )
        lines.append(
            f"<div>&gt;{_price_text(max_budget)} 不建议购买</div>"
        )
    else:
        for item in _action_threshold_lines(current, target, max_budget)[:4]:
            lines.append(f"<div>{item}</div>")
    lines.append(
        f"<div>当前{_price_text(current)} → 落在【{_status_span(_action_zone_label(current, target, max_budget))}】区间</div>"
    )
    lines.append("</div>")
    lines.append("")


def _append_core_reasons_section(lines: list[str], decision: dict, confidence: dict) -> None:
    _section(lines, "<b>为什么这样判断？</b>")
    reasons = (decision.get("reasons") or [])[:3]
    if reasons:
        for index, reason in enumerate(reasons, start=1):
            lines.append(f"{index}. {reason}")
    else:
        lines.append("1. 当前价格和执行信息需要结合支付页最终结果确认。")
    lines.append("")


def _append_confidence_section(lines: list[str], confidence: dict) -> None:
    _section(lines, "<b>置信度拆解</b>")
    confidence = confidence or {}
    dimensions = confidence.get("dimensions") or {}
    details = confidence.get("details") or {}
    labels = [
        ("价格新鲜度", "价格新鲜度"),
        ("历史样本量", "历史样本量"),
        ("渠道可购买性", "可购买性"),
        ("票规完整度", "票规完整度"),
        ("用户约束匹配", "用户约束匹配"),
    ]
    fallback_notes = {
        "价格新鲜度": "基于最近一次采集时间",
        "历史样本量": "基于近期/历史价格点数量",
        "可购买性": "待支付页验证",
        "票规完整度": "行李/退改未核实",
        "用户约束匹配": "基于当前筛选条件",
    }
    if not dimensions:
        lines.append("暂无足够置信度拆解数据")
    else:
        for display_name, key in labels:
            level = dimensions.get(key) or ("待确认" if key == "票规完整度" else None)
            if not level:
                continue
            note = details.get(key) or fallback_notes.get(key, "")
            lines.append(f"{display_name}：{level}（{note}）")
    lines.append("")
    lines.append(f"总体：{confidence.get('overall', '中')}")
    low_items = [
        name
        for name, key in labels
        if dimensions.get(key) in {"低", "待确认"}
    ]
    if low_items:
        lines.append(f"主要扣分项：{'、'.join(low_items[:2])}尚未确认")
    else:
        lines.append("主要扣分项：暂无明显短板")
    lines.append("")


def _append_sorting_logic_section(lines: list[str], route_info: dict, is_round_trip: bool) -> None:
    _section(lines, "<b>本次排序优先级</b>")
    max_budget = _to_float(route_info.get("max_budget") or route_info.get("budget"))
    target = _to_float(route_info.get("target_price"))
    if is_round_trip:
        max_budget = max_budget * 2 if max_budget else None
        target = target * 2 if target else None
    budget_text = _price_text(max_budget) if max_budget else "当前配置"
    target_text = _price_text(target) if target else "合理价格"
    lines.append(f"1. 不超过最高预算 {budget_text}")
    lines.append("2. 满足托运行李要求")
    lines.append("3. 尽量直飞/低中转风险")
    lines.append(f"4. 接近理想入手价 {target_text}")
    lines.append("5. 购买渠道可靠")
    lines.append("")


_FILTER_REASON_LABELS = {
    "direct_only": "需要中转，但你设置了必须直飞",
    "max_budget": "价格超过当前预算上限",
    "departure_slots": "起飞时段不符合设置",
    "departure_time_policy": "起飞时间不符合设置",
    "arrival_slots": "到达时段不符合设置",
    "arrival_time_policy": "到达时间不符合设置",
    "time_preference": "起降时间不符合设置",
    "airline_policy": "航司类型不符合设置",
    "exclude_airlines": "命中你排除的航司",
    "lcc_excluded": "命中你设置的排除廉航条件",
    "lcc_only_unmet": "并非全部航段均由廉航执飞",
    "max_total_duration": "总行程时长超过设置",
    "allow_overnight_transfer": "包含不接受的过夜中转",
    "allow_self_transfer": "包含不接受的非联程中转",
    "max_transfers": "中转次数超过设置",
    "allow_transfer": "中转方案不符合直飞优先设置",
    "allow_airport_change": "包含不接受的换机场中转",
    "min_connection_min": "中转时间低于安全下限",
    "red_eye": "红眼、过早起飞或凌晨到达不符合设置",
    "need_baggage": "托运行李要求未满足",
    "return_collection_failed": "返程采集失败，无法组成完整往返",
    "return_candidates_empty": "返程无可用候选，无法组成完整往返",
    "roundtrip_pairing_failed": "去返程未能组成完整往返",
}


def _exact_filter_reason_lines(item: dict) -> list[str]:
    entries = []
    if item.get("filter_reason_code"):
        entries.append(
            {
                "direction": "",
                "code": item.get("filter_reason_code"),
                "value": item.get("filter_reason_value") or "",
            }
        )
    for entry in item.get("filter_reasons") or []:
        if isinstance(entry, dict) and entry.get("code"):
            entries.append(entry)

    lines = []
    for entry in entries:
        code = str(entry.get("code") or "").strip()
        value = str(entry.get("value") or "").strip()
        direction = str(entry.get("direction") or "").strip()
        label = _FILTER_REASON_LABELS.get(code, "触发筛选约束")
        prefix = f"{direction}:" if direction else ""
        evidence = f"；{value}" if value else ""
        text = f"{prefix}{label}({code}{evidence})"
        if text not in lines:
            lines.append(text)
    return lines


def _excluded_reason_details(item: dict) -> list[str]:
    reason = str(item.get("reason") or "不符合当前要求")
    exact_details = _exact_filter_reason_lines(item)
    details = list(exact_details) if exact_details else [reason]

    fare = item.get("fare_verification") or {}
    for issue in fare.get("issues") or []:
        if issue not in details:
            details.append(issue)

    availability = item.get("availability") or {}
    if availability and availability.get("status") not in ("likely_available", "possibly_available"):
        details.append("渠道可购买性未验证，需要到支付页确认")

    transfer = item.get("transfer_risk") or {}
    for factor in transfer.get("factors") or []:
        if factor not in details:
            details.append(factor)

    price_estimate = item.get("price_estimate") or {}
    for extra in price_estimate.get("extra_items") or []:
        name = extra.get("name")
        amount = extra.get("amount")
        note = extra.get("note")
        if name and amount:
            suffix = f"（{note}）" if note else ""
            details.append(f"{name}约{_price_text(amount)}{suffix}")

    lower_reason = reason.lower()
    if not exact_details and ("行李" in reason or "托运" in reason) and len(details) == 1:
        details.append("不含托运行李或托运行李额度未确认")
    if not exact_details and ("红眼" in reason or "凌晨" in reason) and len(details) == 1:
        details.append("起飞或到达时间触发默认时间安全规则")
    if not exact_details and ("非联程" in reason or "self" in lower_reason) and len(details) == 1:
        details.append("可能需要自行转机和重新托运行李")
    if not exact_details and ("过夜" in reason or "中转" in reason) and len(details) == 1:
        details.append("中转时间或中转方式不符合当前偏好")

    clean = []
    for detail in details:
        text = str(detail).strip()
        if text and text not in clean:
            clean.append(text)
    return clean[:3]


def _excluded_item_flight(item: dict) -> dict:
    flight = item.get("flight") if isinstance(item, dict) else None
    if isinstance(flight, dict) and flight:
        merged = dict(flight)
        for key in (
            "price",
            "flight_combo",
            "airline_summary",
            "route_summary",
            "segments",
            "layovers",
            "airlines",
            "stops",
            "fare_verification",
            "availability",
            "transfer_risk",
            "price_estimate",
            "data_source",
        ):
            if key in item and item.get(key) not in (None, "", []):
                merged.setdefault(key, item.get(key))
        return merged
    return dict(item or {})


def _excluded_flight_detail_text(item: dict, route_info: dict | None = None) -> str:
    flight = _excluded_item_flight(item)
    date_str = (route_info or {}).get("depart_date")
    detail = format_flight_detail(flight, date_str, "去程")
    if not detail or "航班信息待确认" in detail:
        combo = item.get("flight_combo") or flight.get("flight_combo") or ""
        if combo:
            return f"去程:{combo}｜航班信息待确认"
        return "去程:航班信息待确认"
    combo = item.get("flight_combo") or flight.get("flight_combo") or ""
    if combo and combo not in detail:
        return f"{combo}｜{detail}"
    return detail


def _excluded_scope(item: dict, is_roundtrip: bool) -> str:
    scope = str(item.get("scope") or item.get("direction") or "").strip().lower()
    if scope in {"roundtrip", "round_trip", "combo"}:
        return "roundtrip"
    if scope in {"outbound", "depart", "departure", "去程"}:
        return "outbound"
    if scope in {"return", "inbound", "返程"}:
        return "return"
    if item.get("is_roundtrip") or (item.get("outbound") and item.get("return")):
        return "roundtrip"
    return "single_leg" if is_roundtrip else "oneway"


def _excluded_scope_label(scope: str) -> str:
    return {
        "roundtrip": "往返组合",
        "outbound": "去程方案",
        "return": "返程方案",
        "single_leg": "单段方案",
        "oneway": "方案",
    }.get(scope, "方案")


def _excluded_price_intro(item: dict, current_price, is_roundtrip: bool) -> str:
    price = _to_float(item.get("total_price") or item.get("roundtrip_price") or item.get("price"))
    scope = _excluded_scope(item, is_roundtrip)
    label = _excluded_scope_label(scope)
    if price is None:
        return f"已排除的{label}"
    if item.get("all_over_budget_reference"):
        current = _to_float(current_price)
        diff = current - price if current is not None and price < current else None
        diff_text = f"（比主推便宜{_price_text(diff)}）" if diff else ""
        return f"预算外低价参考{label}：{_price_text(price)}{diff_text}"
    if is_roundtrip and scope != "roundtrip":
        return f"已排除的更低价{label}：{_price_text(price)}"
    diff = None
    current = _to_float(current_price)
    if current is not None and price < current:
        diff = current - price
    diff_text = f"（比推荐便宜{_price_text(diff)}）" if diff else ""
    prefix = "已排除的更低价" if diff else "已排除的"
    return f"{prefix}{label}：{_price_text(price)}{diff_text}"


def _canonical_price_comparison_points(points, plan_total, recommended_total) -> list[str]:
    """排除方案价格对比只使用两个展示总价的直接差额。"""
    result = [
        str(value).strip()
        for value in (points or [])
        if str(value or "").strip() and not str(value or "").strip().startswith("价格:")
    ]
    price = round_display_price(plan_total)
    current = round_display_price(recommended_total)
    if price is None or current is None:
        return result
    difference = current - price
    if difference > 0:
        price_point = f"价格:此方案{_price_text(price)},比推荐便宜{_price_text(difference)} ✓"
    elif difference < 0:
        price_point = f"价格:此方案{_price_text(price)},比推荐贵{_price_text(abs(difference))} ✗"
    else:
        price_point = f"价格:此方案{_price_text(price)},与推荐持平"
    result.insert(1 if result else 0, price_point)
    return result


def _excluded_scope_note(item: dict, is_roundtrip: bool) -> str:
    scope = _excluded_scope(item, is_roundtrip)
    if is_roundtrip and scope != "roundtrip":
        direction = _excluded_scope_label(scope).replace("方案", "")
        direction = direction or "单段"
        return f"注：此为{direction}单段价，非往返总价。"
    return ""


EXCLUDED_CARD_STYLE = "border:1px solid #f0d0d0;border-radius:8px;padding:12px;margin:10px 0;background:#fdf8f8;"
EXCLUDED_TITLE_STYLE = "font-weight:600;color:#b91c1c;margin-bottom:8px;"
EXCLUDED_LABEL_STYLE = "color:#999;width:80px;vertical-align:top;padding:4px 8px 4px 0;"
EXCLUDED_VALUE_STYLE = "color:#111;vertical-align:top;padding:4px 0;"


def _excluded_table_row(label: str, value: str, danger: bool = False) -> str:
    color = "#b91c1c" if danger else "#111"
    return (
        "<tr>"
        f"<td style='{EXCLUDED_LABEL_STYLE}'>{html.escape(str(label or ''))}</td>"
        f"<td style='{EXCLUDED_VALUE_STYLE}color:{color};'>{value}</td>"
        "</tr>"
    )


def _excluded_segment_value(segment: dict) -> str:
    flight_no = str(segment.get("flight_no") or "").strip()
    airline = str(segment.get("airline") or "").strip()
    dep = str(segment.get("dep_airport") or "").strip().upper()
    arr = str(segment.get("arr_airport") or "").strip().upper()
    dep_time = _local_time_label(dep, segment.get("dep_time"))
    arr_time = _local_time_label(arr, segment.get("arr_time"))
    aircraft = get_aircraft_name(segment.get("aircraft") or segment.get("equipment") or segment.get("plane_type") or "")
    left = " ".join(part for part in [flight_no, airline] if part) or "航班信息待确认"
    value = f"{html.escape(left)}｜{html.escape(dep)} {html.escape(dep_time)} → {html.escape(arr)} {html.escape(arr_time)}"
    if aircraft and aircraft not in {"未知", "unknown", "Unknown", "请查询航司官网"}:
        value += f"｜{html.escape(aircraft)}"
    return value


def _excluded_aircraft_text(flight: dict) -> str:
    aircrafts = []
    for segment in flight.get("segments") or []:
        aircraft = get_aircraft_name(segment.get("aircraft") or segment.get("equipment") or segment.get("plane_type") or "")
        if aircraft and aircraft not in {"未知", "unknown", "Unknown", "请查询航司官网"} and aircraft not in aircrafts:
            aircrafts.append(aircraft)
    return " / ".join(aircrafts) if aircrafts else "机型待确认"


def _excluded_transfer_text(flight: dict) -> str:
    segments = flight.get("segments") or []
    stops = int(flight.get("stops") if flight.get("stops") is not None else max(0, len(segments) - 1))
    duration = _pushplus_duration_text(flight)
    if stops <= 0:
        return "直飞" + (f"｜总时长{duration}" if duration else "")
    layovers = []
    for layover in flight.get("layovers") or []:
        airport = str(layover.get("airport") or "").strip().upper()
        city = str(layover.get("city") or "").strip()
        label = city or airport
        if airport and airport not in label:
            label = f"{label}{airport}" if label else airport
        if label:
            layovers.append(label)
    transfer = f"{stops}次"
    if layovers:
        transfer += " " + " / ".join(layovers)
    if duration:
        transfer += f"｜总时长{duration}"
    return transfer


def _excluded_card_flights(item: dict, is_roundtrip: bool) -> list[tuple[str, dict]]:
    if isinstance(item.get("outbound"), dict) or isinstance(item.get("return"), dict):
        result = []
        if isinstance(item.get("outbound"), dict):
            result.append(("去程", item["outbound"]))
        if isinstance(item.get("return"), dict):
            result.append(("返程", item["return"]))
        return result
    scope = _excluded_scope(item, is_roundtrip)
    label = "去程" if scope == "outbound" else "返程" if scope == "return" else "航班"
    return [(label, _excluded_item_flight(item))]


def _excluded_leg_price(item: dict, prefix: str, flight: dict) -> float | None:
    key = "outbound_price" if prefix == "去程" else "return_price" if prefix == "返程" else ""
    if key:
        price = _to_float(item.get(key))
        if price is not None:
            return price
    return _to_float((flight or {}).get("price"))


def _render_excluded_plan_card(item: dict, current_price, is_roundtrip: bool) -> str:
    item = dict(item or {})
    display_tree = _display_price_tree_for_item(item)
    if display_tree and (item.get("is_roundtrip") or (item.get("outbound") and item.get("return"))):
        item["total_price"] = display_tree["total"]
        item["roundtrip_price"] = display_tree["total"]
        item["price"] = display_tree["total"]
    current_display = round_display_price(current_price)
    canonical_total = display_tree.get("total") if display_tree else _to_float(item.get("total_price") or item.get("price"))
    canonical_diff = (
        current_display - canonical_total
        if current_display is not None and canonical_total is not None and canonical_total < current_display
        else None
    )
    item["diff"] = canonical_diff
    outbound_identity = _plan_render_identity(item)
    card_label = "排除:" + "+".join(value for value in (outbound_identity or ())[:2] if value)
    _log_card_price_consistency(
        item,
        card_label or "排除方案",
        displayed_total=canonical_total,
        reference_total=current_display,
        displayed_difference=canonical_diff,
    )
    reason_lines = _excluded_reason_details(item)
    reason = reason_lines[0] if reason_lines else (item.get("reason") or "不符合当前规则")
    semantic_intro = _excluded_price_intro(item, current_price, is_roundtrip)
    price = _to_float(item.get("total_price") or item.get("roundtrip_price") or item.get("price"))
    scope = _excluded_scope(item, is_roundtrip)
    price_phrase = ""
    if price is not None:
        price_phrase = f"往返{_price_text(price)}" if scope == "roundtrip" else _price_text(price)
    if item.get("all_over_budget_reference"):
        title = f"预算外低价参考 · {price_phrase}" if price_phrase else "预算外低价参考"
    else:
        title = f"已排除方案 · {price_phrase}" if price_phrase else "已排除方案"
    body_parts = []
    rows = []
    current = _to_float(current_price)
    if price is not None and current is not None and price < current:
        body_parts.append(
            "<div style='font-size:13px;color:#666;margin-bottom:8px;'>"
            f"(比推荐方案便宜{html.escape(_price_text(current - price))},但因以下原因不推荐)"
            "</div>"
        )
    combo_text = str(item.get("flight_combo") or "").strip()
    if combo_text:
        rows.append(_excluded_table_row("航班组合", html.escape(combo_text)))
    for prefix, flight in _excluded_card_flights(item, is_roundtrip):
        body_parts.append(_email_plan_leg_group(prefix, flight, _excluded_flight_detail_text({"flight": flight, **item})))
        leg_price = _excluded_leg_price(item, prefix, flight)
        if leg_price is not None:
            rows.append(_excluded_table_row(f"{prefix}票面价", html.escape(f"{_price_text(leg_price)}(单程)")))
    if price is not None:
        price_label = "往返总价" if scope == "roundtrip" else (
            "价格" if not is_roundtrip else f"{_excluded_scope_label(scope).replace('方案', '')}价格"
        )
        price_value = f"{_price_text(price)}(往返)" if scope == "roundtrip" else _price_text(price)
        rows.append(_excluded_table_row(price_label, html.escape(price_value)))
    scope_note = _excluded_scope_note(item, is_roundtrip)
    if scope_note:
        rows.append(_excluded_table_row("说明", html.escape(scope_note)))
    rows.append(_excluded_table_row("排除原因(基于你的设置)", html.escape(str(reason)), danger=True))
    for extra in reason_lines[1:3]:
        rows.append(_excluded_table_row("", html.escape(str(extra)), danger=True))
    basis = [str(value).strip() for value in (item.get("exclusion_basis") or []) if str(value or "").strip()]
    if basis:
        rows.append(_excluded_table_row("依据", html.escape(format_constraint_summary(basis))))
    comparison_points = _canonical_price_comparison_points(
        item.get("comparison_points") or [],
        price,
        current,
    )
    if comparison_points:
        comparison_html = "<br>".join(html.escape(point) for point in comparison_points[:4])
        rows.append(_excluded_table_row("对比推荐方案", comparison_html))
    execution_summary = _excluded_compact_execution_summary(item)
    if execution_summary:
        rows.append(_excluded_table_row("执行信息", html.escape(execution_summary)))
    return (
        f'<div style="{EXCLUDED_CARD_STYLE}">'
        f'<div style="{EXCLUDED_TITLE_STYLE}">{html.escape(title)}</div>'
        f'<div style="display:none;">{html.escape(semantic_intro)}</div>'
        + "".join(body_parts)
        + '<table style="width:100%;font-size:13px;line-height:1.6;border-collapse:collapse;">'
        + "".join(rows)
        + "</table></div>"
    )


def _excluded_relax_hints(items: list[dict]) -> list[str]:
    hints = []
    mapping = [
        (("红眼", "凌晨"), "允许红眼/凌晨航班"),
        (("非联程", "self"), "允许非联程中转"),
        (("过夜", "中转时间", "总时长"), "接受更长中转"),
        (("行李", "托运"), "放宽托运行李要求"),
        (("廉航",), "允许廉航方案"),
    ]
    for item in items:
        text = " ".join(
            str(part)
            for part in [
                item.get("reason"),
                " ".join((item.get("transfer_risk") or {}).get("factors") or []),
                " ".join((item.get("fare_verification") or {}).get("issues") or []),
            ]
            if part
        ).lower()
        for keys, hint in mapping:
            if any(key.lower() in text for key in keys) and hint not in hints:
                hints.append(hint)
    return hints[:4]


def _append_excluded_low_price_section_legacy(
    lines: list[str],
    analysis_result: dict,
    current_price,
    route_info: dict | None = None,
    compact: bool = False,
) -> None:
    excluded = analysis_result.get("excluded_flights") or []
    current = _to_float(current_price)
    cheaper = []
    for item in excluded:
        price = _to_float(item.get("price"))
        if price is None or (current is not None and price >= current):
            continue
        cheaper.append(item)
    cheaper = sorted(cheaper, key=lambda item: _to_float(item.get("price")) or 999999)

    _section(lines, "<b>已排除的更低价方案</b>")
    if not cheaper:
        lines.append("暂无比推荐方案更便宜但被排除的方案。")
        lines.append("")
        return

    for item in cheaper[: 2 if compact else 3]:
        combo = item.get("flight_combo") or "未命名方案"
        reason = item.get("reason") or "不符合当前要求"
        lines.append(f"- {_price_text(item.get('price'))} {combo}：{reason}")
    lines.append("这些方案虽然更便宜，但不满足你的要求，所以未推荐。")
    lines.append("")


def _append_excluded_low_price_section(
    lines: list[str],
    analysis_result: dict,
    current_price,
    route_info: dict | None = None,
    compact: bool = False,
) -> None:
    excluded = analysis_result.get("excluded_flights") or []
    current = _to_float(current_price)
    cheaper = []
    for item in excluded:
        price = _to_float(item.get("price"))
        if price is None or (current is not None and price >= current):
            continue
        cheaper.append(item)
    cheaper = sorted(cheaper, key=lambda item: _to_float(item.get("price")) or 999999)

    _section(lines, "<b>为什么没推荐更便宜的方案？</b>")
    if not cheaper:
        lines.append("暂无比主推方案更便宜但被排除的方案。")
        lines.append("")
        return

    shown = cheaper[: 2 if compact else 3]
    for item in shown:
        combo = item.get("flight_combo") or "未命名方案"
        price = _to_float(item.get("price"))
        diff = max(0, current - price) if current is not None and price is not None else None
        diff_text = f"（比推荐便宜{_price_text(diff)}）" if diff else ""
        lines.append(f"{_price_text(price)}方案 {combo}{diff_text}：")
        lines.append(_excluded_flight_detail_text(item, route_info))
        reason_lines = _excluded_reason_details(item)
        if reason_lines:
            lines.append(f"排除原因：{reason_lines[0]}")
            for detail in reason_lines[1:]:
                lines.append(f"- {detail}")
        lines.append("")
        continue
        for detail in _excluded_reason_details(item):
            lines.append(f"- {detail}")
        lines.append("")

    lines.append("这些方案虽然便宜，但触发了系统默认安全规则，所以未作为主推荐。")
    hints = _excluded_relax_hints(shown)
    if hints:
        lines.append(f"如果你能接受这些条件，可在精准监控中调整：{' / '.join(hints)}")
    form_url = _subscription_form_url(route_info)
    lines.append(f'修改链接：<a href="{form_url}" target="_blank">打开订阅偏好</a>')
    lines.append("")


def _append_risk_section(
    lines: list[str],
    route_info: dict,
    analysis_result: dict,
    price_insights: dict | None,
    is_round_trip: bool,
    return_analysis: dict | None,
    primary_flight: dict | None = None,
) -> None:
    _section(lines, "<b>风险权衡</b>")
    primary_flight = primary_flight or {}
    risk = (
        (analysis_result.get("round_trip_analysis") or {}).get("buy_vs_wait_risk")
        if is_round_trip
        else analysis_result.get("buy_vs_wait_risk")
    ) or {}
    buy_risks = risk.get("buy_risks") or [
        "可能遇到支付页跳价",
        "票规需确认（行李/退改）",
        "不同渠道售后政策不同",
    ]
    wait_risks = risk.get("wait_risks") or [
        "可能错过当前低价",
        "临近出发价格通常上涨",
        "理想价再次出现不确定",
    ]
    lines.append(f"<b>如果现在买（风险：{risk.get('buy_level', '中')}）：</b>")
    for item in buy_risks[:3]:
        lines.append(f"- {item}")
    lines.append("")
    lines.append(f"<b>如果继续等（风险：{risk.get('wait_level', '中')}）：</b>")
    for item in wait_risks[:3]:
        lines.append(f"- {item}")
    lines.append("")
    summary = risk.get("summary")
    if not summary:
        status = _status_risk_label(primary_flight) if primary_flight else "风险中"
        summary = f"当前执行风险为{status.replace('风险', '')}，建议以支付页最终价格和票规为准。"
    lines.append(f"权衡建议：{summary}")
    lines.append("")
    _append_judgment_limits(
        lines,
        route_info,
        analysis_result,
        price_insights,
        is_round_trip,
        return_analysis,
    )


def _subscription_form_url(route_info: dict | None = None) -> str:
    route_info = route_info or {}
    return (
        route_info.get("subscription_form_url")
        or os.environ.get("SUBSCRIPTION_FORM_URL")
        or os.environ.get("PYTHONANYWHERE_FORM_URL")
        or "https://ljs96824.pythonanywhere.com"
    )


def _append_next_actions_section(lines: list[str], route_info: dict) -> None:
    _section(lines, "<b>下一步操作</b>")
    form_url = _subscription_edit_url(route_info)
    feedback_url = _feedback_url(route_info)
    lines.append("查看购买渠道：已在方案卡片内列出")
    lines.append("复制购买前检查清单：见详细分析中的检查清单")
    lines.append("价格变化会自动推送，无需手动刷新")
    lines.append(f'修改监控偏好：<a href="{form_url}" target="_blank">打开订阅表单</a>')
    lines.append(f'买不到/价格不对：<a href="{feedback_url}" target="_blank">提交反馈</a>')
    lines.append("")


def _append_detailed_analysis_section(
    lines: list[str],
    analysis_result: dict,
    route_info: dict,
    price_insights: dict | None,
    source_stats: dict | None,
    is_round_trip: bool,
    outbound_analysis: dict,
    return_analysis: dict | None,
    compact: bool = False,
) -> None:
    _section(lines, "<b>详细分析</b>")
    if is_round_trip:
        round_trip = analysis_result.get("round_trip_analysis") or {}
        nearby_dates = route_info.get("nearby_dates") or analysis_result.get("nearby_dates")
        _append_nearby_dates_bar_chart(lines, nearby_dates, is_round_trip=True)
        _append_option_price_bar_chart(lines, analysis_result, True, route_info)
        top_combinations = _round_trip_combinations(analysis_result)
        if not compact:
            _append_roundtrip_price_reference(lines, round_trip, route_info)
            _append_roundtrip_price_analysis(lines, round_trip)
            _append_round_trip_change_table(lines, round_trip)
        outbound_flights = round_trip.get("outbound_top3") or _round_trip_top_flights(outbound_analysis)
        return_flights = round_trip.get("return_top3") or _round_trip_top_flights(return_analysis)
        limit = 3 if compact else 5
        _append_round_trip_all_options(
            lines,
            "去程全部方案（按价格排序）",
            outbound_flights[:limit],
            route_info.get("depart_date"),
        )
        _append_round_trip_all_options(
            lines,
            "返程全部方案（按价格排序）",
            return_flights[:limit],
            route_info.get("return_date"),
        )
    else:
        current_min = (
            analysis_result.get("price_range", [None])[0]
            if analysis_result.get("price_range")
            else None
        )
        history = price_insights.get("price_history") if price_insights else None
        nearby_dates = route_info.get("nearby_dates") or analysis_result.get("nearby_dates")
        _append_nearby_dates_bar_chart(lines, nearby_dates, is_round_trip=False)
        _append_option_price_bar_chart(lines, analysis_result, False, route_info)
        first_flight = next(iter(_single_flights_for_sections(analysis_result) or []), None)
        _append_channel_price_bar_chart(lines, first_flight)
        trend = {}
        arrow_line = ""
        if arrow_line:
            lines.append(f"价格走势：{arrow_line}")
        elif trend.get("available"):
            lines.append(
                f"价格走势：最低{_price_text(trend.get('min_price'))} | "
                f"最高{_price_text(trend.get('max_price'))} | 平均{_price_text(trend.get('avg_price'))}"
            )
        own_history = _normalize_own_history_for_refs(route_info)
        if not compact and current_min:
            refs = calculate_price_references(
                current_min,
                history,
                own_history,
                analysis_result.get("days_to_dept") or 0,
                analysis_result.get("all_flights") or [],
            )
            _append_price_references(lines, refs, current_min, "")
            window_analysis = multi_window_analysis(
                current_min,
                own_history,
                history,
                analysis_result.get("days_to_dept") or 0,
            )
            _append_multi_window_analysis(lines, window_analysis)
            _append_price_anomaly_lines(lines, analysis_result.get("price_anomalies") or [])

    lines.append("")
    _append_purchase_checklist(lines, route_info, analysis_result)
    lines.append("")
    lines.extend(_compact_source_summary_lines(source_stats))
    _append_system_health_lines(lines, analysis_result.get("system_health") or {})
    lines.append("")


def _format_structured_html_message(
    analysis_result=None,
    route_info=None,
    source_stats=None,
    price_insights=None,
    outbound_analysis=None,
    return_analysis=None,
    compact: bool = False,
    detail_level: str | None = None,
    persist_snapshot: bool = True,
) -> str:
    route_info = route_info or {}
    analysis_result = analysis_result or outbound_analysis or {}
    outbound_analysis = outbound_analysis or analysis_result
    return_analysis = return_analysis or analysis_result.get("return_analysis") or {}
    is_round_trip = bool(route_info.get("round_trip"))
    source_stats_for_message = (
        source_stats
        or route_info.get("source_stats")
        or analysis_result.get("source_stats")
    )

    lines: list[str] = []
    main_limit = 2
    alt_limit = 2 if compact else 3
    link_limit = 3 if compact else 4
    detail_level = _resolved_detail_level(route_info, analysis_result, detail_level)

    decision, confidence, current, target, max_budget = _decision_context(
        analysis_result, route_info, source_stats_for_message, price_insights, is_round_trip
    )
    route_key, depart_key, return_key = _last_push_route_parts(route_info, is_round_trip)
    last_push = get_last_push_price(route_key, depart_key, return_key)
    last_snapshot = get_last_push_snapshot(route_key, depart_key, return_key)
    price_history_for_push = _price_history_for_push(price_insights, analysis_result, is_round_trip)
    push_meta = determine_push_type(
        current,
        target,
        max_budget,
        price_history_for_push,
        analysis_result.get("days_to_dept"),
        None if is_round_trip else (last_push or {}).get("price"),
        analysis_result,
    )
    _append_action_header_section(
        lines,
        push_meta,
        route_info,
        decision,
        confidence,
        current,
        target,
        max_budget,
        analysis_result,
        is_round_trip,
    )
    _append_push_trend_linechart(
        lines,
        analysis_result,
        route_info,
        price_insights,
        is_round_trip,
        current,
        target,
        max_budget,
    )

    primary_flight = None
    primary_items = []
    alternative_items = []

    _section(lines, "<b>推荐方案</b>")
    if is_round_trip:
        combos = _round_trip_combinations(analysis_result)
        primary_items = combos[:main_limit]
        alternative_items = combos[main_limit : main_limit + alt_limit]
        primary_flight = (primary_items[0].get("outbound") or {}) if primary_items else {}
        if primary_items:
            for index, combo in enumerate(primary_items):
                _round_trip_combo_option_lines(
                    lines,
                    combo,
                    f"推荐方案{chr(65 + index)}",
                    route_info,
                    confidence,
                    link_limit,
                    "推荐",
                    True,
                )
        else:
            lines.append("暂无可展示的往返组合")
            lines.append("")
    else:
        flights = _single_flights_for_sections(analysis_result)
        primary_items = [flight for flight in flights if flight.get("execution_grade") != "D"][:main_limit]
        alternative_items = [flight for flight in flights if flight not in primary_items][:alt_limit]
        primary_flight = primary_items[0] if primary_items else {}
        if primary_items:
            for index, flight in enumerate(primary_items):
                _single_option_lines(
                    lines,
                    flight,
                    f"推荐方案{chr(65 + index)}",
                    route_info,
                    analysis_result,
                    link_limit,
                    "推荐",
                    True,
                )
        else:
            lines.append("暂无可展示的主推方案")
            lines.append("")

    _append_push_reason_section(lines, push_meta)
    _append_price_change_section(lines, current, target, max_budget, push_meta, is_round_trip)
    _append_action_links_section(lines, route_info, primary_flight, is_round_trip)
    if detail_level == "short":
        if persist_snapshot:
            _save_current_push_snapshot(
                route_key,
                depart_key,
                return_key,
                current,
                confidence,
                primary_flight,
                push_meta,
            )
        return "<br>".join(lines)

    _append_operation_section(lines, decision, current, target, max_budget, is_round_trip)
    _append_validity_section(lines, analysis_result, route_info, primary_flight)

    _section(lines, "<b>备选方案</b>")
    if alternative_items:
        for index, item in enumerate(alternative_items):
            label = f"方案{chr(65 + main_limit + index)}"
            if is_round_trip:
                variant = "更稳" if _combo_grade(item) == "A" else "备选"
                _round_trip_combo_option_lines(
                    lines,
                    item,
                    label,
                    route_info,
                    confidence,
                    3,
                    variant,
                    False,
                )
            else:
                variant = "更稳" if item.get("execution_grade") == "A" else "备选"
                _single_option_lines(
                    lines,
                    item,
                    label,
                    route_info,
                    analysis_result,
                    3,
                    variant,
                    False,
                )
    else:
        lines.append("暂无更多符合条件的备选方案")
        lines.append("")

    _append_sorting_logic_section(lines, route_info, is_round_trip)
    lines.append("━━━ 以下为判断依据 ━━━")
    _append_core_reasons_section(lines, decision, confidence)
    _append_risk_section(
        lines,
        route_info,
        analysis_result,
        price_insights,
        is_round_trip,
        return_analysis,
        primary_flight,
    )
    _append_excluded_low_price_section(lines, analysis_result, current, route_info, compact)
    _append_last_push_difference_section(lines, last_snapshot, current, confidence, primary_flight)
    _append_confidence_section(lines, confidence)
    _append_next_actions_section(lines, route_info)
    _append_detailed_analysis_section(
        lines,
        analysis_result,
        route_info,
        price_insights,
        source_stats_for_message,
        is_round_trip,
        outbound_analysis,
        return_analysis,
        compact,
    )

    if is_round_trip:
        _append_low_option_count_notice(lines, outbound_analysis, "去程")
        _append_low_option_count_notice(lines, return_analysis, "返程")
    else:
        _append_low_option_count_notice(lines, analysis_result)

    collected_at = _message_collected_time(analysis_result, route_info)
    lines.append("")
    lines.append(f"数据采集于 {collected_at} | 价格可能随时变动，建议尽快确认")
    lines.append("机票价格实时波动，推荐方案基于采集时数据。")
    lines.append("点击链接后如价格有变化属于正常现象。")
    lines.append("如果涨价幅度超过5%，系统会在下次采集时提醒你。")
    lines.append("")
    if not compact:
        _append_price_explanation_lines(lines)
        lines.append("")
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append("以上数据来自第三方API，仅供参考。")
    lines.append("实际价格请以航司或OTA官网价格为准。")
    lines.append("以上排序基于当前配置规则，不代表最优选择。请根据您的时间、预算和出行需求自行判断。")
    if persist_snapshot:
        _save_current_push_snapshot(
            route_key,
            depart_key,
            return_key,
            current,
            confidence,
            primary_flight,
            push_meta,
        )
    return "<br>".join(lines)


def _payload_route_text(route_info: dict) -> str:
    origin = route_info.get("origin_city") or get_airport_city(route_info.get("origin", "")) or route_info.get("origin", "")
    dest = route_info.get("destination_city") or get_airport_city(route_info.get("destination", "")) or route_info.get("destination", "")
    return f"{origin} → {dest}"


def _payload_route_airports(route_info: dict) -> str:
    origins = route_info.get("origin_airports") or [route_info.get("origin")]
    dests = route_info.get("destination_airports") or [route_info.get("destination")]
    origin_text = "/".join(str(item) for item in origins if item)
    dest_text = "/".join(str(item) for item in dests if item)
    return f"{origin_text} → {dest_text}".strip(" →")


def _payload_plan_leg(flight: dict | None, date_str: str | None = None, prefix: str = "") -> str:
    flight = flight or {}
    label = prefix or "航班"
    if not flight.get("segments") and not flight.get("flight_combo"):
        return f"{label}:航班信息待确认"
    return f"{_flight_local_time_summary(flight, label, compact=True)} | {_flight_price_text(flight)}"


def _round_trip_airline_text(flight: dict | None) -> str:
    flight = flight or {}
    airlines = flight.get("airlines") or []
    if airlines:
        return " / ".join(str(item) for item in airlines if item)
    segments = flight.get("segments") or []
    names = []
    for segment in segments:
        name = segment.get("airline") or ""
        if name and name not in names:
            names.append(name)
    return " / ".join(names)


def _round_trip_stops_text(flight: dict | None) -> str:
    flight = flight or {}
    try:
        stops = int(flight.get("stops") if flight.get("stops") is not None else 0)
    except (TypeError, ValueError):
        stops = 0
    return "直飞" if stops <= 0 else f"中转{stops}次"


def format_flight_detail(flight: dict | None, date_str: str | None = None, prefix: str = "") -> str:
    """Format one flight consistently for recommendation and alternative cards."""
    return _payload_plan_leg(flight, date_str, prefix)


def _escape_multiline(value) -> str:
    return html.escape(str(value or "")).replace("\n", "<br>")


def _payload_booking_links_for_flight(flight: dict | None, route_info: dict, date_str: str | None, limit: int = 6) -> str:
    flight = flight or {}
    links = _compact_link_text(_combo_full_booking_links(flight, date_str or route_info.get("depart_date")), limit)
    if links:
        return links
    return _flight_link_text(flight, route_info, limit)


def _payload_channel_rows(flight: dict | None, scope: str = "oneway") -> list[dict]:
    rows = []
    for option in _verified_booking_options(flight)[:6]:
        price = _option_price(option)
        if price:
            rows.append({"label": str(option.get("platform") or "购买渠道"), "value": price, "scope": scope})
    return rows


def _payload_roundtrip_channel_rows(combo: dict | None) -> list[dict]:
    combo = combo or {}
    rows = []
    for option in (combo.get("booking_options") or combo.get("verified_booking_options") or [])[:6]:
        if not isinstance(option, dict):
            continue
        price = _option_price(option)
        if price:
            rows.append({"label": str(option.get("platform") or "购买渠道"), "value": price, "scope": "roundtrip"})
    for row in combo.get("channel_prices") or []:
        if not isinstance(row, dict):
            continue
        price = _to_float(row.get("value") or row.get("price"))
        if price:
            item = dict(row)
            item["value"] = price
            item["scope"] = "roundtrip"
            rows.append(item)
    return rows


def _first_airline_code(flight: dict | None) -> str:
    flight = flight or {}
    segments = flight.get("segments") or []
    if segments:
        code = _airline_code_from_flight_no(segments[0].get("flight_no") or "")
        if code:
            return code
    return _airline_code_from_flight_no(flight.get("flight_combo") or "")


def _combo_purchase_mode(outbound: dict | None, return_flight: dict | None) -> str:
    outbound_code = _first_airline_code(outbound)
    return_code = _first_airline_code(return_flight)
    outbound_source = str((outbound or {}).get("data_source") or (outbound or {}).get("source") or "")
    return_source = str((return_flight or {}).get("data_source") or (return_flight or {}).get("source") or "")
    if outbound_code and outbound_code == return_code and outbound_source and outbound_source == return_source:
        return "往返组合"
    return "两个单程拼接"


def _purchase_mode_note(mode: str) -> str:
    if mode == "两个单程拼接":
        return "该方案为去程和返程分别购买，退改签和售后可能分别处理"
    return "建议在同一渠道内验证整套往返价格和票规"


def _pushplus_baggage_line_for_flight(flight: dict | None) -> str:
    flight = flight or {}
    estimate = flight.get("price_estimate") or {}
    for item in estimate.get("extra_items") or []:
        name = str(item.get("name") or "")
        amount = _to_float(item.get("amount"))
        if "托运" in name or "行李" in name:
            if amount:
                return f"行李:不含托运,需额外购买约{_price_text(amount)}"
            return "行李:不含托运,需额外购买"

    fare_rules = flight.get("fare_rules") or {}
    baggage = fare_rules.get("baggage") or {}
    if baggage.get("included") is False:
        note = baggage.get("note") or "托运需另购"
        return f"行李:仅含手提,{note}"
    if baggage.get("included") is True:
        kg = baggage.get("checked_kg")
        pieces = baggage.get("checked_pieces")
        if kg:
            return f"行李:已含托运{kg}kg"
        if pieces:
            return f"行李:已含{pieces}件托运"
        return "行李:已含托运"
    pieces = baggage.get("checked_pieces") or 0
    kg = baggage.get("checked_kg") or 0
    if pieces:
        return f"行李:已含{pieces}件托运"
    if kg:
        return f"行李:已含托运{kg}kg"

    extra = flight.get("extra") or {}
    detail = extra.get("baggage_detail") or {}
    checked = detail.get("checked") or {}
    if checked.get("quantity"):
        return f"行李:已含{checked.get('quantity')}件托运"
    if extra.get("baggage") or flight.get("has_baggage_info"):
        return "行李:已含托运"
    return "行李:支付页需确认"


def _verification_refund_line_for_flight(flight: dict | None) -> str:
    flight = flight or {}
    fare_rules = flight.get("fare_rules") or {}
    refund = fare_rules.get("refund") or {}
    if isinstance(refund, dict):
        parts = [
            str(refund.get("label") or "").strip(),
            str(refund.get("note") or "").strip(),
        ]
        text = "，".join(item for item in parts if item)
        if text:
            return f"退改:{text}"
        level = str(refund.get("level") or "").strip()
        if level:
            return f"退改:{level}"
    compact = _compact_refund_line(flight)
    return re.sub(r"^[^退]*退改[:：]\s*", "退改:", compact).strip() or "退改:以支付页为准"


def _pushplus_baggage_line_for_combo(outbound: dict | None, return_flight: dict | None) -> str:
    lines = [_pushplus_baggage_line_for_flight(outbound), _pushplus_baggage_line_for_flight(return_flight)]
    if any("不含托运" in line for line in lines):
        return next(line for line in lines if "不含托运" in line)
    if all("已含" in line for line in lines):
        return "行李:去回程已含托运"
    return "行李:支付页需确认"


def _pushplus_link_candidates(link_text: str, max_links: int = 2) -> list[tuple[str, str]]:
    anchors = re.findall(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', str(link_text or ""), flags=re.I)
    clean = [(html.unescape(name).strip(), url) for url, name in anchors if url and name]
    priority = ["携程", "飞猪", "去哪儿", "航司", "官网", "Trip.com"]
    ordered: list[tuple[str, str]] = []
    for key in priority:
        for name, url in clean:
            if key in name and (name, url) not in ordered:
                ordered.append((name, url))
    for item in clean:
        if item not in ordered:
            ordered.append(item)
    return ordered[:max_links]


def _pushplus_plan_booking_links(plan: dict | None, max_links: int = 2) -> list[tuple[str, str]]:
    plan = plan or {}
    links = plan.get("links") or {}
    if isinstance(links, dict):
        if links.get("main"):
            return _pushplus_link_candidates(links.get("main"), max_links)
        candidates = []
        for key in ("outbound", "return"):
            candidates.extend(_pushplus_link_candidates(links.get(key, ""), max_links))
        unique = []
        for item in candidates:
            if item not in unique:
                unique.append(item)
        return unique[:max_links]
    return _pushplus_link_candidates(str(links), max_links)


def _pushplus_link_line(link_text: str, max_links: int = 6) -> str:
    links = _pushplus_link_candidates(link_text, max_links)
    return " | ".join(
        f'<a href="{html.escape(url, quote=True)}" target="_blank">{html.escape(name)}</a>'
        for name, url in links
    )


def _pushplus_plan_flight_label(plan: dict, direction: str) -> str:
    line = str(plan.get(f"{direction}_push_line") or plan.get("main_push_line") or "")
    match = re.search(r"^(去程|返程):([^｜\n]+)", line)
    if match:
        return f"{match.group(1)} {match.group(2).strip()}"
    return "去程" if direction in {"outbound", "main"} else "返程"



def _pushplus_channel_section(payload: dict, plan: dict | None) -> list[str]:
    detail_url = str(payload.get("detail_url") or "")
    detail_link = (
        f'<a href="{html.escape(detail_url, quote=True)}" target="_blank">{html.escape(detail_url)}</a>'
        if detail_url
        else "\u8be6\u60c5\u9875\u6682\u672a\u751f\u6210"
    )
    links = plan.get("links") if isinstance(plan, dict) else {}
    is_roundtrip = bool(plan.get("is_roundtrip")) if isinstance(plan, dict) else False
    purchase_mode = str(plan.get("purchase_mode") or "") if isinstance(plan, dict) else ""
    split_ticket = is_roundtrip and "\u5355\u7a0b" in purchase_mode
    total_price = plan.get("price") if isinstance(plan, dict) else None
    plan_label = str((plan or {}).get("label") or "\u65b9\u6848A").strip() or "\u65b9\u6848A"
    lines = [f"\u9a8c\u8bc1\u9996\u9009{plan_label}{'(\u4e24\u6bb5)' if split_ticket else ''}:"]

    if isinstance(links, dict) and split_ticket:
        for direction in ("outbound", "return"):
            line = _pushplus_link_line(links.get(direction, ""), 6)
            if line:
                price = plan.get(f"{direction}_price")
                price_text = f" \u7ea6{_plan_leg_price_text(plan, price)}" if price else ""
                lines.append(f"{_pushplus_plan_flight_label(plan, direction)}{price_text}:")
                lines.append(line)
        if total_price:
            lines.append(f"\u5f80\u8fd4\u5408\u8ba1\u53c2\u8003:{_plan_roundtrip_price_text(plan)}")
        lines.append("\u6ce8:\u4e24\u6bb5\u72ec\u7acb\u7968,\u9700\u5206\u522b\u4e0b\u5355")
    elif isinstance(links, dict) and plan.get("is_roundtrip"):
        combo_price = f"\u7ea6{_plan_roundtrip_price_text(plan)} " if total_price else ""
        lines.append(f"\u6574\u5957\u5f80\u8fd4\u9a8c\u8bc1:{combo_price}\u5efa\u8bae\u540c\u4e00\u6e20\u9053\u9009\u62e9\u5f80\u8fd4\u641c\u7d22")
        line = _pushplus_link_line(links.get("main") or links.get("outbound") or links.get("return"), 6)
        if line:
            lines.append(line)
    elif isinstance(links, dict):
        line = _pushplus_link_line(links.get("main", ""), 6)
        if line:
            lines.append(line)
    else:
        line = _pushplus_link_line(str(links or ""), 6)
        if line:
            lines.append(line)

    if len(lines) == 1:
        return [
            "\u9a8c\u8bc1\u9996\u9009\u65b9\u6848\u6e20\u9053:\u8be6\u60c5\u9875\u5df2\u9644\u5b8c\u6574\u94fe\u63a5",
            f"\u7f51\u9875\u8be6\u60c5:{detail_link}",
        ]
    return lines

def _pushplus_freshness_line(payload: dict) -> str:
    age = _to_float(payload.get("freshness_minutes"))
    if age is not None and age > 120:
        return "⚠️ 该价格已超过2小时未验证,仅供参考"
    collected = str(payload.get("collected_at") or "").strip()
    if collected:
        time_text = _time_only(collected) or collected
    else:
        time_text = "刚刚"
    return f"价格更新:{time_text},建议30分钟内验证"


def _payload_freshness_text(payload: dict) -> str:
    age = _to_float(payload.get("freshness_minutes"))
    if age is None:
        collected = str(payload.get("collected_at") or payload.get("run_started_at") or "").strip()
        if collected:
            try:
                value = datetime.fromisoformat(collected.replace("Z", "+00:00"))
                return value.strftime("%Y-%m-%d %H:%M采集")
            except ValueError:
                return f"{collected[:16]}采集"
        return "采集时间待确认"
    if age < 1:
        return "刚刚采集"
    if age < 60:
        return f"{int(age)}分钟前采集"
    return f"{int(age // 60)}小时前采集"


def _payload_combo_plan(combo: dict, route_info: dict, index: int, variant: str) -> dict:
    outbound = combo.get("outbound") or {}
    return_flight = combo.get("return") or {}
    total = _to_float(combo.get("total_price"))
    transaction_total = _combo_transaction_total(combo)
    outbound_date = route_info.get("depart_date")
    return_date = route_info.get("return_date")
    purchase_mode = _combo_purchase_mode(outbound, return_flight)
    source_channel_rows = (
        _payload_source_channel_rows(outbound, "outbound")
        + _payload_source_channel_rows(return_flight, "return")
    )
    plan = {
        "label": f"方案{chr(65 + index)}",
        "variant": variant,
        "is_roundtrip": True,
        "price": total,
        "estimated_price": transaction_total,
        "outbound_price": _to_float(outbound.get("price")),
        "return_price": _to_float(return_flight.get("price")),
        "outbound_flight": outbound,
        "return_flight": return_flight,
        "outbound_line": format_flight_detail(outbound, outbound_date, "去程"),
        "return_line": format_flight_detail(return_flight, return_date, "返程"),
        "outbound_push_line": _pushplus_leg_summary(outbound, "去程"),
        "return_push_line": _pushplus_leg_summary(return_flight, "返程"),
        "summary": f"往返总价 {_price_text(total)}",
        "baggage_line": _pushplus_baggage_line_for_combo(outbound, return_flight),
        "purchase_mode": purchase_mode,
        "purchase_note": _purchase_mode_note(purchase_mode),
        "same_day_round_trip": bool(combo.get("same_day_round_trip")),
        "stay_hours": combo.get("stay_hours"),
        "same_day_tag": combo.get("tag") or ("当天往返可行" if combo.get("same_day_round_trip") else ""),
        "same_day_windows": combo.get("same_day_windows") or {},
        "business_feasibility": combo.get("business_feasibility") or {},
        "business_feasibility_rank": combo.get("business_feasibility_rank"),
        "meeting_arrival_margin_min": combo.get("meeting_arrival_margin_min"),
        "return_departure_margin_min": combo.get("return_departure_margin_min"),
        "schedule_note": combo.get("schedule_note") or "",
        "tags": _round_trip_combo_tags(combo, route_info, None),
        "risk": _combo_grade(combo),
        "buy_condition": _combo_human_recommendation(combo, route_info),
        "links": {
            "outbound": _payload_booking_links_for_flight(outbound, route_info, outbound_date, 6),
            "return": _payload_booking_links_for_flight(return_flight, route_info, return_date, 6),
        },
        "channel_prices": source_channel_rows or _payload_roundtrip_channel_rows(combo),
    }
    if combo.get("mixed_cabin"):
        mixed_tree = combo.get("mixed_cabin_pricing") or {}
        plan.update(
            {
                "mixed_cabin": True,
                "cabin_allocation": combo.get("cabin_allocation") or {},
                "cabin_label": combo.get("cabin_label") or mixed_tree.get("cabin_label") or "",
                "mixed_cabin_pricing": mixed_tree,
                "passenger_pricing": mixed_tree,
                "price_tiers": combo.get("price_tiers") or {},
                "raw_passenger_total_price": combo.get("raw_passenger_total_price"),
                "passenger_total_price": combo.get("passenger_total_price"),
                "business_outbound": combo.get("business_outbound") or {},
                "business_return": combo.get("business_return") or {},
                "business_price_source": combo.get("business_price_source") or "serpapi",
                "mixed_cabin_disclosure": combo.get("mixed_cabin_disclosure") or MIXED_CABIN_DISCLOSURE,
                "mixed_cabin_price_notes": combo.get("mixed_cabin_price_notes") or {},
            }
        )
        if mixed_tree.get("total") is not None:
            plan["price"] = mixed_tree["total"]
            plan["estimated_price"] = mixed_tree["total"]
            plan["summary"] = f"混舱往返全员总价 {_price_text(mixed_tree['total'])}"
        tags = list(plan.get("tags") or [])
        if "混舱" not in tags:
            tags.append("混舱")
        plan["tags"] = tags
    lcc_summary = _combo_lcc_summary(outbound, return_flight)
    if lcc_summary.get("has_lcc"):
        plan["lcc_summary"] = lcc_summary
        plan["need_baggage"] = _preference_value(
            route_info,
            None,
            "need_baggage",
            "unknown",
        )
    return plan


def _payload_single_plan(flight: dict, route_info: dict, analysis_result: dict, index: int, variant: str) -> dict:
    source_channel_rows = _payload_source_channel_rows(flight, "main")
    plan = {
        "label": f"方案{chr(65 + index)}",
        "variant": variant,
        "is_roundtrip": False,
        "price": _to_float(flight.get("price")),
        "estimated_price": _to_float((flight.get("price_estimate") or {}).get("transaction_price") or flight.get("price")),
        "main_flight": flight,
        "summary": format_flight_detail(flight, route_info.get("depart_date"), "去程"),
        "main_push_line": _pushplus_leg_summary(flight, "去程"),
        "baggage_line": _pushplus_baggage_line_for_flight(flight),
        "tags": _flight_status_tags(flight, route_info, analysis_result),
        "risk": _status_risk_label(flight),
        "buy_condition": _human_recommendation_text(flight, route_info, analysis_result),
        "links": {"main": _payload_booking_links_for_flight(flight, route_info, route_info.get("depart_date"), 6)},
        "channel_prices": source_channel_rows or _payload_channel_rows(flight),
    }
    lcc_summary = _flight_lcc_summary(flight)
    if lcc_summary.get("has_lcc"):
        plan["lcc_summary"] = lcc_summary
        plan["need_baggage"] = _preference_value(
            route_info,
            analysis_result,
            "need_baggage",
            "unknown",
        )
    return plan


def _plan_feasibility_rank(plan: dict) -> int:
    levels = []
    feasibility = plan.get("feasibility") or {}
    if isinstance(feasibility, dict):
        for item in feasibility.values():
            if isinstance(item, dict) and item.get("level"):
                levels.append(str(item.get("level")))
    if not levels:
        return 0
    if "不可行" in levels:
        return 2
    if "紧张" in levels:
        return 1
    return 0


def _plan_leg_flights(plan: dict) -> list[dict]:
    flights = []
    if plan.get("is_roundtrip"):
        for key in ("outbound_flight", "return_flight"):
            flight = plan.get(key)
            if isinstance(flight, dict) and flight:
                flights.append(flight)
    else:
        flight = plan.get("main_flight") or plan.get("flight")
        if isinstance(flight, dict) and flight:
            flights.append(flight)
    return flights


def _hour_from_flight_time(value) -> int | None:
    text = _time_only(value)
    if not text or ":" not in text:
        return None
    try:
        return int(text.split(":", 1)[0])
    except (TypeError, ValueError):
        return None


def _flight_has_baggage_clarity(flight: dict) -> bool:
    fare_rules = flight.get("fare_rules") or flight.get("fare_verification") or {}
    baggage = (fare_rules or {}).get("baggage") or {}
    if baggage:
        return baggage.get("included") is not None or bool(baggage.get("note"))
    return bool(flight.get("baggage") or flight.get("baggage_line"))


def _flight_has_refund_clarity(flight: dict) -> bool:
    fare_rules = flight.get("fare_rules") or flight.get("fare_verification") or {}
    return bool((fare_rules or {}).get("refund") or (fare_rules or {}).get("change"))


def _flight_is_direct_enough(flight: dict) -> bool:
    try:
        return int(flight.get("stops") or 0) == 0
    except (TypeError, ValueError):
        return False


def _flight_is_daytime_enough(flight: dict) -> bool:
    dep_hour = _hour_from_flight_time(flight.get("departure_time") or flight.get("dep_time"))
    arr_hour = _hour_from_flight_time(flight.get("arrival_time") or flight.get("arr_time"))
    dep_ok = dep_hour is None or 8 <= dep_hour <= 20
    arr_ok = arr_hour is None or 9 <= arr_hour <= 21
    return dep_ok and arr_ok


_PASSENGER_FRIENDLY_TAGS = {"亲子友好", "老人友好", "亲子/老人友好", "亲子·老人友好"}
_COMBINED_FRIENDLY_TAG_TOKEN = "__PASSENGER_FRIENDLY_COMBINED__"


def _tag_parts(existing: str) -> list[str]:
    protected = str(existing or "").replace("亲子·老人友好", _COMBINED_FRIENDLY_TAG_TOKEN)
    parts = [part.strip() for part in re.split(r"[|·]", protected) if part.strip()]
    return ["亲子·老人友好" if part == _COMBINED_FRIENDLY_TAG_TOKEN else part for part in parts]


def _append_unique_tags(existing: str, tags: list[str]) -> str:
    parts = _tag_parts(existing)
    for tag in tags:
        if tag and tag not in parts:
            parts.append(tag)
    return " | ".join(parts)


def _apply_passenger_friendly_to_plans(plans: list[dict], passenger_profile: dict | None) -> list[dict]:
    profile = passenger_profile or {}
    if not (profile.get("has_child") or profile.get("has_elderly")):
        return plans
    result = []
    for plan in plans:
        item = dict(plan)
        flights = _plan_leg_flights(item)
        direct = bool(flights) and all(_flight_is_direct_enough(flight) for flight in flights)
        daytime = bool(flights) and all(_flight_is_daytime_enough(flight) for flight in flights)
        baggage_clear = bool(flights) and all(_flight_has_baggage_clarity(flight) for flight in flights)
        refund_clear = bool(flights) and all(_flight_has_refund_clarity(flight) for flight in flights)
        tags = []
        has_child = bool(profile.get("has_child"))
        has_elderly = bool(profile.get("has_elderly"))
        friendly_tag = ""
        if has_child and has_elderly and direct and (daytime or baggage_clear):
            friendly_tag = "亲子·老人友好"
        elif has_child and direct and baggage_clear:
            friendly_tag = "亲子友好"
        elif has_elderly and direct and daytime:
            friendly_tag = "老人友好"
        if friendly_tag:
            tags.append(friendly_tag)
        if direct and daytime:
            tags.append("白天直飞")
        if baggage_clear:
            tags.append("行李明确")
        if refund_clear:
            tags.append("退改清晰")
        if direct:
            tags.append("低折腾")
        retained_tags = [part for part in _tag_parts(item.get("tags")) if part not in _PASSENGER_FRIENDLY_TAGS]
        item["tags"] = _append_unique_tags(" | ".join(retained_tags), tags[:5])
        if tags:
            item["friendly_reason"] = (
                "白天直飞、行李和退改信息更清楚，适合老人/小孩同行；"
                "系统已降低纯价格权重，优先执行稳定性。"
            )
        result.append(item)
    return result

def _apply_departure_feasibility_to_plans(
    plans: list[dict],
    constraints: dict,
    route_type: str,
    route_info: dict,
) -> list[dict]:
    outbound_set_off = str(constraints.get("outbound_set_off") or "").strip()
    return_set_off = str(constraints.get("return_set_off") or "").strip()
    if not outbound_set_off and not return_set_off:
        result = []
        for plan in plans:
            item = dict(plan)
            if _plan_feasibility_rank(item) == 2:
                item["tags"] = _append_unique_tags(item.get("tags") or "", ["需调整动身时间"])
            result.append(item)
        return result
    transport_min = constraints.get("user_transport_min")
    margin_mode = str(constraints.get("transport_margin_mode") or "standard")
    result = []
    for plan in plans:
        item = dict(plan)
        feasibility = {}
        if outbound_set_off:
            outbound_flight = item.get("outbound_flight") or item.get("main_flight") or item.get("flight")
            if isinstance(outbound_flight, dict) and outbound_flight:
                outbound = analyze_departure_feasibility(
                    outbound_set_off,
                    outbound_flight,
                    route_type,
                    transport_min,
                    margin_mode,
                    route_info.get("depart_date"),
                )
                if outbound:
                    feasibility["outbound"] = outbound
        if item.get("is_roundtrip") and return_set_off:
            return_flight = item.get("return_flight")
            if isinstance(return_flight, dict) and return_flight:
                ret = analyze_departure_feasibility(
                    return_set_off,
                    return_flight,
                    route_type,
                    transport_min,
                    margin_mode,
                    route_info.get("return_date"),
                )
                if ret:
                    feasibility["return"] = ret
        if feasibility:
            item["feasibility"] = feasibility
            item["feasibility_rank"] = _plan_feasibility_rank(item)
        if _plan_feasibility_rank(item) == 2:
            item["tags"] = _append_unique_tags(item.get("tags") or "", ["需调整动身时间"])
        result.append(item)
    return sorted(result, key=lambda plan: (int(plan.get("feasibility_rank") or 0), str(plan.get("label") or "")))


def _plan_flights(plan: dict) -> list[dict]:
    flights = []
    for key in ("outbound_flight", "return_flight", "main_flight"):
        flight = plan.get(key)
        if isinstance(flight, dict) and flight:
            flights.append(flight)
    return flights


def _plan_lcc_summary(plan: dict | None) -> dict:
    plan = plan or {}
    existing = plan.get("lcc_summary")
    if isinstance(existing, dict):
        return existing
    return _combo_lcc_summary(*_plan_flights(plan))


def _plan_lcc_baggage_warning(plan: dict | None) -> str:
    plan = plan or {}
    if str(plan.get("need_baggage") or "").strip() != "required":
        return ""
    if not _plan_lcc_summary(plan).get("has_lcc"):
        return ""
    return "⚠ 含廉航段:票价通常不含托运行李,请以支付页为准"


def _tracking_current_flights(
    analysis_result: dict,
    all_items: list[dict],
    is_roundtrip: bool,
    return_analysis: dict | None = None,
    source_names: list[str] | None = None,
) -> list[dict]:
    flights: list[dict] = []
    sources = source_names if source_names is not None else []

    def extend_source(name: str, candidates) -> None:
        valid = [item for item in candidates or [] if isinstance(item, dict) and item]
        if not valid:
            return
        flights.extend(valid)
        sources.append(name)

    plan_flights = []
    for plan in all_items or []:
        plan_flights.extend(_plan_flights(plan))
    extend_source("all_items", plan_flights)

    if is_roundtrip:
        combo_flights = []
        for combo in _round_trip_combinations(analysis_result):
            for key in ("outbound", "return"):
                flight = combo.get(key)
                if isinstance(flight, dict) and flight:
                    combo_flights.append(flight)
        extend_source("round_trip_analysis.top_combinations", combo_flights)

        resolved_return = return_analysis or analysis_result.get("return_analysis") or {}
        candidate_keys = (
            "same_day_base_flights",
            "raw_valid_flights",
            "raw_valid_outbound",
            "all_flights",
            "qualified_flights",
            "economy_recommendations",
        )
        for prefix, analysis in (
            ("analysis_result", analysis_result),
            ("return_analysis", resolved_return),
        ):
            if not isinstance(analysis, dict):
                continue
            for key in candidate_keys:
                extend_source(f"{prefix}.{key}", analysis.get(key))
    else:
        extend_source("single_flights_for_sections", _single_flights_for_sections(analysis_result))

    seen: set[str] = set()
    unique: list[dict] = []
    for flight in flights:
        normalized = normalize_combo(
            flight.get("flight_combo")
            or flight.get("flight_no")
            or flight.get("flight_number")
        )
        key = normalized or str(id(flight))
        if key in seen:
            continue
        seen.add(key)
        unique.append(flight)
    return unique


def _tracking_item_label(item: dict) -> str:
    outbound = item.get("outbound") or item.get("outbound_flight") or {}
    return_flight = item.get("return") or item.get("return_flight") or {}
    if isinstance(outbound, dict) and isinstance(return_flight, dict) and outbound and return_flight:
        outbound_key = normalize_combo(
            outbound.get("flight_combo") or outbound.get("flight_no") or outbound.get("flight_number")
        )
        return_key = normalize_combo(
            return_flight.get("flight_combo") or return_flight.get("flight_no") or return_flight.get("flight_number")
        )
        return f"{outbound_key}/{return_key}"
    return normalize_combo(
        item.get("flight_combo") or item.get("flight_no") or item.get("flight_number")
    )


def _tracking_current_items(
    analysis_result: dict,
    all_items: list[dict],
    is_roundtrip: bool,
    return_analysis: dict | None = None,
) -> list[dict]:
    items: list[dict] = []
    items.extend(all_items or [])
    if is_roundtrip:
        items.extend(_round_trip_combinations(analysis_result))
    source_names: list[str] = []
    items.extend(
        _tracking_current_flights(
            analysis_result,
            all_items,
            is_roundtrip,
            return_analysis=return_analysis,
            source_names=source_names,
        )
    )
    pool = [item for item in items if isinstance(item, dict) and item]
    normalized_items = [label for label in (_tracking_item_label(item) for item in pool) if label]
    safe_log(
        f"[追踪池] 池来源={'+'.join(dict.fromkeys(source_names)) or 'empty'} "
        f"池大小={len(pool)} 池内容(norm后)={normalized_items[:10]}"
    )
    return pool


def _plan_total_stops(plan: dict) -> int:
    total = 0
    for flight in _plan_flights(plan):
        try:
            total += int(flight.get("stops") or 0)
        except (TypeError, ValueError):
            continue
    return total


def _flight_identity_for_plan(flight: dict | None) -> tuple:
    flight = flight or {}
    return (
        flight.get("flight_combo")
        or flight.get("flight_no")
        or flight.get("flight_number")
        or "",
        flight.get("departure_airport") or flight.get("origin") or flight.get("dep_airport") or "",
        flight.get("arrival_airport") or flight.get("destination") or flight.get("arr_airport") or "",
        str(flight.get("departure_time") or flight.get("dep_time") or ""),
        str(flight.get("arrival_time") or flight.get("arr_time") or ""),
    )


def _payload_plan_key(plan: dict) -> tuple:
    if plan.get("is_roundtrip"):
        return (
            "roundtrip",
            _flight_identity_for_plan(plan.get("outbound_flight")),
            _flight_identity_for_plan(plan.get("return_flight")),
            round(_to_float(plan.get("price")) or 0, 2),
        )
    return (
        "oneway",
        _flight_identity_for_plan(plan.get("main_flight")),
        round(_to_float(plan.get("price")) or 0, 2),
    )


def _dedupe_payload_plans(plans: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for plan in plans or []:
        if not isinstance(plan, dict):
            continue
        key = _payload_plan_key(plan)
        if key in seen:
            continue
        seen.add(key)
        result.append(plan)
    for index, plan in enumerate(result):
        if str(plan.get("label") or "").startswith("方案"):
            plan["label"] = f"方案{chr(65 + index)}"
    return result


def _plan_time_minutes(flight: dict | None, key: str) -> int | None:
    flight = flight or {}
    value = str(flight.get(key) or "").strip()
    match = re.search(r"(\d{1,2}):(\d{2})", value)
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _plan_flight_no_text(flight: dict | None) -> str:
    flight = flight or {}
    return str(flight.get("flight_combo") or flight.get("flight_no") or flight.get("flight_number") or "").strip()


def _same_price_plan_difference_reason(plan: dict, primary: dict) -> str:
    differences = []
    outbound_no = _plan_flight_no_text(plan.get("outbound_flight") or plan.get("main_flight"))
    primary_outbound_no = _plan_flight_no_text(primary.get("outbound_flight") or primary.get("main_flight"))
    return_no = _plan_flight_no_text(plan.get("return_flight"))
    primary_return_no = _plan_flight_no_text(primary.get("return_flight"))
    if outbound_no and primary_outbound_no and outbound_no != primary_outbound_no:
        differences.append("去程航班不同")
    if return_no and primary_return_no and return_no != primary_return_no:
        return_dep = _plan_time_minutes(plan.get("return_flight"), "departure_time")
        primary_return_dep = _plan_time_minutes(primary.get("return_flight"), "departure_time")
        if return_dep is not None and primary_return_dep is not None:
            if return_dep > primary_return_dep:
                differences.append("返程较晚")
            elif return_dep < primary_return_dep:
                differences.append("返程较早")
            else:
                differences.append("返程航班不同")
        else:
            differences.append("返程航班不同")
    if _plan_total_stops(plan) != _plan_total_stops(primary):
        differences.append("中转次数不同")
    purchase_mode = str(plan.get("purchase_mode") or "")
    primary_purchase_mode = str(primary.get("purchase_mode") or "")
    if purchase_mode and primary_purchase_mode and purchase_mode != primary_purchase_mode:
        differences.append(f"购票方式不同({purchase_mode})")
    if differences:
        return "与方案A同价，" + "、".join(dict.fromkeys(differences))
    return "与方案A同价但无明显结构差异，综合排序次于方案A"


def _plan_difference_reason(plan: dict, primary: dict) -> str:
    plan_price = _to_float(plan.get("price"))
    primary_price = _to_float(primary.get("price"))
    if plan_price is not None and primary_price is not None:
        if plan_price > primary_price:
            return f"价格高于方案A{_price_text(plan_price - primary_price)}，综合排序次于方案A"
        if abs(plan_price - primary_price) < 1:
            return _same_price_plan_difference_reason(plan, primary)
        if plan_price < primary_price:
            return f"价格低于方案A{_price_text(primary_price - plan_price)}，但综合条件次于方案A"
    return "综合排序次于方案A，作为备选验证"


def _plan_execution_grade(plan: dict) -> str:
    risk = str(plan.get("risk") or "").strip()
    if risk in {"A", "B", "C", "D"}:
        return risk
    grades = [str(flight.get("execution_grade") or "") for flight in _plan_flights(plan)]
    grades = [grade for grade in grades if grade]
    if not grades:
        return ""
    order = {"A": 1, "B": 2, "C": 3, "D": 4}
    return max(grades, key=lambda grade: order.get(grade, 9))


def _plan_tier_reason(plan: dict, primary_plan: dict | None = None) -> tuple[str, str, str]:
    stops = _plan_total_stops(plan)
    purchase_mode = str(plan.get("purchase_mode") or "")
    price = _to_float(plan.get("price"))
    primary_price = _to_float((primary_plan or {}).get("price"))
    cheaper_than_primary = bool(price is not None and primary_price is not None and price < primary_price)
    tier = classify_plan_tier(
        is_direct=stops == 0,
        execution_grade=_plan_execution_grade(plan),
        cheaper_than_primary=cheaper_than_primary,
        has_transfer=stops > 0,
        split_ticket="两个单程" in purchase_mode,
    )
    return tier.get("tier", "首选推荐"), tier.get("reason", ""), tier.get("suitable_condition", "")


def _apply_plan_tiers(plans: list[dict]) -> list[dict]:
    if not plans:
        return plans
    plans = _dedupe_payload_plans(plans)
    primary = plans[0]
    for index, plan in enumerate(plans):
        existing_tier = str(plan.get("tier") or plan.get("variant") or "").split(":", 1)[0].strip()
        if index > 0 and existing_tier and existing_tier not in {"推荐", "首选推荐"}:
            tier = "备选方案" if existing_tier == "备选" else existing_tier
            reason = str(plan.get("tier_reason") or "").strip()
            condition = str(plan.get("suitable_condition") or "").strip()
        else:
            tier, reason, condition = _plan_tier_reason(plan, primary if index else None)
        if index == 0:
            tier = "首选推荐"
            reason = reason or "综合得分最高，建议优先验证"
            condition = condition or "适合优先验证该方案的价格和票规"
        elif tier == "首选推荐":
            tier = "次选方案"
            reason = _plan_difference_reason(plan, primary)
            condition = "如果方案A价格或票规不合适，可再验证该方案"
        plan["tier"] = tier
        plan["tier_reason"] = reason
        plan["suitable_condition"] = condition
        plan["variant"] = f"{tier}:{reason}"
    return plans


def _payload_nearby_date_rows(route_info: dict, analysis_result: dict, is_roundtrip: bool) -> list[dict]:
    nearby = route_info.get("nearby_dates") or analysis_result.get("nearby_dates") or []
    items = list(nearby.values()) if isinstance(nearby, dict) else list(nearby or [])
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        has_roundtrip_value = item.get("roundtrip_total") not in (None, "", 0)
        value = _to_float(item.get("roundtrip_total") or item.get("total") or item.get("min_price"))
        if not value:
            continue
        rows.append({
            "label": str(item.get("date") or ""),
            "value": value,
            "selected": bool(item.get("selected")),
            "scope": "roundtrip" if has_roundtrip_value else "oneway",
        })
    return rows


def _payload_price_calendar(route_info: dict, analysis_result: dict) -> dict:
    calendar = route_info.get("price_calendar") or analysis_result.get("price_calendar") or {}
    if not calendar:
        calendar = _price_calendar_from_nearby_dates(route_info, analysis_result)
    if not isinstance(calendar, dict):
        return {}
    rows = calendar.get("rows") or []
    row_dates = {
        str(row.get("date") or "")[:10]
        for row in rows
        if isinstance(row, dict) and row.get("date")
    }
    route = (
        f"{route_info.get('origin') or ''}-{route_info.get('destination') or ''}"
    ).strip("-")
    nearby_calendar = _nearby_dates_as_calendar(
        route,
        route_info.get("nearby_dates") or analysis_result.get("nearby_dates") or [],
    )
    uncollected_by_date = {}
    for row in [
        *(calendar.get("uncollected_rows") or []),
        *(nearby_calendar.get("uncollected_rows") or []),
    ]:
        if not isinstance(row, dict):
            continue
        row_date = str(row.get("date") or "")[:10]
        if len(row_date) != 10 or row_date in row_dates:
            continue
        uncollected_by_date[row_date] = {**row, "date": row_date}
    uncollected_rows = [
        uncollected_by_date[key] for key in sorted(uncollected_by_date)
    ]
    savings = calendar.get("savings") or []
    today = date.today()
    def is_future_row(row: dict) -> bool:
        if not isinstance(row, dict) or not row.get("date"):
            return False
        try:
            return date.fromisoformat(str(row.get("date"))[:10]) >= today
        except ValueError:
            return False

    if isinstance(rows, list):
        rows = [row for row in rows if is_future_row(row)]
    if isinstance(uncollected_rows, list):
        uncollected_rows = [row for row in uncollected_rows if is_future_row(row)]
    if isinstance(savings, list):
        savings = [row for row in savings if is_future_row(row)]
    weekday_pattern = calendar.get("weekday_pattern") or {}
    return {
        "route": calendar.get("route"),
        "rows": rows if isinstance(rows, list) else [],
        "uncollected_rows": (
            uncollected_rows if isinstance(uncollected_rows, list) else []
        ),
        "savings": savings if isinstance(savings, list) else [],
        "weekday_pattern": weekday_pattern if isinstance(weekday_pattern, dict) else {},
        "scope": calendar.get("scope") or "oneway",
        "return_date": calendar.get("return_date"),
        "return_min_price": calendar.get("return_min_price"),
        "note": calendar.get("note") or "为单程最低参考价，实付以支付页为准。",
    }


def _nearby_dates_as_calendar(route: str, nearby) -> dict:
    items = list(nearby.values()) if isinstance(nearby, dict) else list(nearby or [])
    dates = {}
    uncollected_rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        date_text = str(item.get("date") or item.get("label") or "").strip()[:10]
        price = _to_float(item.get("min_price") or item.get("price") or item.get("value"))
        if not date_text or price is None or price <= 0:
            if date_text and item.get("today_uncollected"):
                uncollected_rows.append(
                    {
                        "date": date_text,
                        "selected": bool(item.get("selected")),
                        "status": "今日未采",
                        "sources": [],
                        "sample_n": 0,
                        "observed_at": None,
                    }
                )
            continue
        existing = _to_float((dates.get(date_text) or {}).get("min_price"))
        if existing is not None and existing <= price:
            continue
        dates[date_text] = {
            "min_price": price,
            "count": item.get("count"),
            "selected": bool(item.get("selected")),
            "source_scope": "merged_search_pool",
            "sources": item.get("sources") or [],
            "updated_at": item.get("collected_at"),
            "collection_state": item.get("collection_state"),
        }
    return {"route": route, "dates": dates, "uncollected_rows": uncollected_rows}


def _price_calendar_from_nearby_dates(route_info: dict, analysis_result: dict) -> dict:
    outbound_nearby = route_info.get("nearby_dates") or analysis_result.get("nearby_dates") or []
    route = f"{route_info.get('origin') or ''}-{route_info.get('destination') or ''}".strip("-")
    outbound_calendar = _nearby_dates_as_calendar(route, outbound_nearby)
    if not outbound_calendar.get("dates"):
        return {
            "route": route,
            "rows": [],
            "savings": [],
            "weekday_pattern": {},
            "scope": "oneway",
            "uncollected_rows": outbound_calendar.get("uncollected_rows") or [],
            "note": "弹性日期仅评估今日面板；标记“今日未采”的日期未发起补采。",
        }

    depart_date = str(route_info.get("depart_date") or "")[:10]
    selected_info = (outbound_calendar.get("dates") or {}).get(depart_date) or {}
    current_price = _to_float(selected_info.get("min_price"))
    is_roundtrip = bool(route_info.get("round_trip") or analysis_result.get("round_trip"))
    return_date = str(route_info.get("return_date") or "")[:10]
    return_analysis = analysis_result.get("return_analysis") or {}
    return_nearby = return_analysis.get("nearby_dates") or []
    return_route = f"{route_info.get('destination') or ''}-{route_info.get('origin') or ''}".strip("-")
    return_calendar = _nearby_dates_as_calendar(return_route, return_nearby)
    if is_roundtrip and return_date and not return_calendar.get("dates"):
        return_price = _to_float(return_analysis.get("current_min_price"))
        if return_price is None:
            return_price = _to_float((return_analysis.get("price_range") or [None])[0])
        if return_price is None:
            return_price = min(
                (
                    price
                    for price in (
                        _to_float(flight.get("price"))
                        for flight in (return_analysis.get("all_flights") or [])
                        if isinstance(flight, dict)
                    )
                    if price is not None and price > 0
                ),
                default=None,
            )
        if return_price is not None and return_price > 0:
            return_calendar = {
                "route": return_route,
                "dates": {
                    return_date: {
                        "min_price": return_price,
                        "selected": True,
                        "source_scope": "fixed_return_analysis",
                    }
                },
            }

    result = analyze_price_calendar(
        outbound_calendar,
        depart_date,
        current_price,
        round_trip=is_roundtrip,
        return_calendar=return_calendar,
        return_date=return_date or None,
    )
    result["uncollected_rows"] = outbound_calendar.get("uncollected_rows") or []
    return result


def _payload_plan_chart_description(plan: dict) -> str:
    plan = plan or {}
    tier = str(plan.get("tier") or plan.get("variant") or "").split(":", 1)[0].strip()
    if not tier or tier == "推荐":
        tier = "首选推荐"
    elif tier == "备选":
        tier = "备选方案"

    if not plan.get("is_roundtrip"):
        return tier

    outbound_stops = _plan_leg_stops(plan.get("outbound_flight") or {})
    return_stops = _plan_leg_stops(plan.get("return_flight") or {})
    parts = []
    if outbound_stops > 0:
        parts.append("去程中转")
    if return_stops > 0:
        parts.append("返程中转")
    if not parts:
        parts.append("去返均直飞")

    purchase_mode = str(plan.get("purchase_mode") or "")
    if "两个单程" in purchase_mode:
        parts.append("购票方式:两个单程拼接")
    elif purchase_mode:
        parts.append(f"购票方式:{purchase_mode}")

    reason = str(plan.get("tier_reason") or "").strip()
    friendly_reason = str(plan.get("friendly_reason") or "").strip()
    parts.append(tier)
    if friendly_reason:
        parts.append("老人/儿童友好")
    if reason and reason not in parts:
        parts.append(reason)
    return " · ".join(parts)


def _plan_leg_stops(flight: dict | None) -> int:
    flight = flight or {}
    try:
        return int(flight.get("stops") or 0)
    except (TypeError, ValueError):
        return 0


def _payload_plan_price_rows(plans: list[dict]) -> list[dict]:
    rows = []
    for plan in plans or []:
        passengers, route_type = _plan_price_context(plan)
        passenger_pricing = plan.get("passenger_pricing") or {}
        all_passengers = _passenger_pricing_applies(passenger_pricing)
        is_roundtrip = bool(plan.get("is_roundtrip"))
        tiers = plan.get("price_tiers") or {}
        if is_roundtrip:
            outbound_price = _to_float(
                plan.get("outbound_price")
                or (plan.get("outbound_flight") or {}).get("price")
            )
            return_price = _to_float(
                plan.get("return_price")
                or (plan.get("return_flight") or {}).get("price")
            )
            display_tree = (
                build_display_prices(
                    outbound_price,
                    return_price,
                    passengers,
                    route_type,
                )
                if outbound_price is not None and return_price is not None
                else {}
            )
            price = _to_float(
                display_tree.get("total")
                if all_passengers
                else tiers.get("unit_roundtrip")
            )
            if price is None:
                price = _to_float(
                    tiers.get("total_roundtrip_ref")
                    if all_passengers
                    else plan.get("single_adult_price")
                )
            scope = (
                "all_passengers_roundtrip"
                if all_passengers
                else "per_person_roundtrip"
            )
        else:
            unit_price = _to_float(
                plan.get("outbound_price")
                or (plan.get("main_flight") or {}).get("price")
                or plan.get("price")
            )
            display_tree = (
                build_display_prices(unit_price, None, passengers, route_type)
                if unit_price is not None
                else {}
            )
            price = _to_float(
                (display_tree.get("outbound") or {}).get("total")
                if all_passengers
                else unit_price
            )
            scope = (
                "all_passengers_oneway"
                if all_passengers
                else "per_person_oneway"
            )
        if price is None:
            price = _to_float(plan.get("price"))
        if not price:
            continue
        rows.append(
            {
                "label": plan.get("label"),
                "value": price,
                "scope": scope,
                "passengers": passengers,
                "route_type": route_type,
                "description": _payload_plan_chart_description(plan),
            }
        )
    return rows


def _normalize_chart_history(history) -> list[dict]:
    rows = []
    for item in history or []:
        if isinstance(item, dict):
            price = _to_float(item.get("price") or item.get("total"))
            label = item.get("date") or item.get("label") or item.get("timestamp") or ""
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            label, price = item[0], _to_float(item[1])
        else:
            continue
        if price and price > 0:
            rows.append({"date": str(label), "price": price})
    return rows[-14:]


def _normalize_own_history_for_refs(route_info: dict) -> list[dict]:
    """把订阅自身历史统一为参考价函数既有的字典格式。"""
    raw_history = (
        (route_info or {}).get("own_history")
        or (route_info or {}).get("lowest_price_history")
        or []
    )
    normalized = []
    for item in raw_history:
        if isinstance(item, dict):
            price = _to_float(item.get("price") or item.get("min_price"))
            observed_at = item.get("date") or item.get("timestamp") or item.get("observed_at")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            observed_at, raw_price = item[0], item[1]
            price = _to_float(raw_price)
        else:
            observed_at = None
            price = _to_float(item)
        if price is None or price <= 0:
            continue
        normalized.append({"date": observed_at, "price": price})
    return normalized


def _normalize_price_history_for_refs(history) -> list[tuple[float | None, float]]:
    """把不同历史结构收敛为五档参考价函数接受的(时间戳,价格)。"""

    def timestamp_of(value) -> float | None:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime.combine(value, datetime.min.time())
        elif isinstance(value, (int, float)):
            return float(value)
        else:
            text = str(value or "").strip()
            if not text:
                return None
            try:
                return float(text)
            except ValueError:
                try:
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                except ValueError:
                    return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return parsed.timestamp()

    normalized = []
    for item in history or []:
        if isinstance(item, dict):
            observed_at = item.get("date") or item.get("timestamp") or item.get("observed_at")
            price = _to_float(item.get("price") or item.get("min_price") or item.get("total"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            observed_at, raw_price = item[0], item[1]
            price = _to_float(raw_price)
        else:
            observed_at = None
            price = _to_float(item)
        if price is None or price <= 0:
            continue
        normalized.append((timestamp_of(observed_at), price))
    return normalized


def _chart_history_for_message(route_info: dict, analysis_result: dict, price_insights: dict | None, is_roundtrip: bool):
    if is_roundtrip:
        round_trip = (analysis_result or {}).get("round_trip_analysis") or {}
        for key in ("price_history", "history", "roundtrip_history"):
            if key in round_trip:
                return round_trip.get(key) or []
    if "constraint_price_history" in (analysis_result or {}):
        return (analysis_result or {}).get("constraint_price_history") or []
    if price_insights and price_insights.get("price_history"):
        return price_insights.get("price_history")
    if analysis_result and analysis_result.get("price_history"):
        return analysis_result.get("price_history")
    return []


def _trend_fallback_line(history) -> str:
    rows = _normalize_chart_history(history)
    if not rows:
        return ""
    prices = [row["price"] for row in rows[-4:]]
    return " → ".join(_price_text(price) for price in prices)


def _trend_linechart_summary(history, ideal_price=None, current_price=None, low_zone=None) -> str:
    rows = _normalize_chart_history(history)
    unique_prices = {round(row["price"], 2) for row in rows}
    if len(rows) < 3 or len(unique_prices) < 2:
        return "历史样本不足，仅供参考。"
    start = rows[0]["price"]
    end = rows[-1]["price"]
    diff = start - end
    direction = "下降" if diff > 0 else "上涨" if diff < 0 else "持平"
    conclusion = f"近{len(rows)}次采集{direction}约{_price_text(abs(diff))}"
    ideal = _to_float(ideal_price)
    current = _to_float(current_price) or end
    if ideal and current <= ideal:
        conclusion += "，当前已低于理想价，建议验证支付页价格。"
    elif ideal and current <= ideal * 1.05:
        conclusion += "，当前已接近理想入手价。"
    else:
        conclusion += "。"
    return conclusion


def _payload_action_range(current, target, max_budget) -> dict:
    current = _to_float(current)
    target = _to_float(target)
    max_budget = _to_float(max_budget)
    ranges = []
    if target and max_budget:
        if max_budget <= target:
            ranges = [
                {"label": "强烈建议验证并购买", "min": None, "max": max_budget, "text": f"≤{_price_text(max_budget)}"},
                {"label": "不建议购买", "min": max_budget, "max": None, "text": f">{_price_text(max_budget)}"},
            ]
        else:
            raw_bounds = [target, min(round(target * 1.05), max_budget), round((target + max_budget) / 2), max_budget]
            bounds = sorted({float(bound) for bound in raw_bounds if bound is not None})
            labels = ["强烈建议验证并购买", "值得购买", "可以考虑", "仅刚需建议"]
            ranges.append({"label": labels[0], "min": None, "max": bounds[0], "text": f"≤{_price_text(bounds[0])}"})
            previous = bounds[0]
            for index, bound in enumerate(bounds[1:], start=1):
                if bound <= previous:
                    continue
                ranges.append({
                    "label": labels[min(index, len(labels) - 1)],
                    "min": previous,
                    "max": bound,
                    "text": f"{_price_text(previous)}-{_price_text(bound)}",
                })
                previous = bound
            ranges.append({"label": "不建议购买", "min": previous, "max": None, "text": f">{_price_text(previous)}"})
    elif target:
        ranges = [
            {"label": "强烈建议验证并购买", "min": None, "max": target, "text": f"≤{_price_text(target)}"},
            {"label": "继续观察", "min": target, "max": None, "text": f">{_price_text(target)}"},
        ]
    elif max_budget:
        ranges = [
            {"label": "预算内", "min": None, "max": max_budget, "text": f"≤{_price_text(max_budget)}"},
            {"label": "超预算", "min": max_budget, "max": None, "text": f">{_price_text(max_budget)}"},
        ]
    return {"current": current, "target": target, "max": max_budget, "ranges": ranges, "current_label": _action_zone_label(current, target, max_budget)}


def _payload_verify_price(display_price, max_budget=None) -> float | None:
    display = _to_float(display_price)
    if not display:
        return None
    verify = round(display * 1.05)
    max_b = _to_float(max_budget)
    if max_b is not None and max_b > 0:
        verify = min(verify, max_b)
    return verify


def _payload_price_policy_decision(
    display_price,
    transaction_price,
    verify_price,
    target_price,
    max_price=None,
    fallback="可以观察",
    price_scope=None,
    budget_decision=None,
) -> dict:
    display = _to_float(display_price)
    transaction = _to_float(transaction_price)
    verify = _to_float(verify_price)
    target = _to_float(target_price)
    max_p = _to_float(max_price)

    is_over_budget = (
        bool(budget_decision.get("is_over_budget"))
        if isinstance(budget_decision, dict)
        else bool(transaction is not None and max_p is not None and transaction > max_p)
    )
    if is_over_budget:
        decision_price = _to_float((budget_decision or {}).get("price")) or transaction or display
        transaction_text = _estimated_price_subject(decision_price, price_scope)
        max_text = _price_text_with_parenthesized_caliber(max_p, price_scope)
        return {
            "conclusion": (
                f"{transaction_text}已超过你的最高可接受价{max_text}，"
                "不满足购买条件，建议保持监控本条航线"
            ),
            "reason": (
                "单人参考价(成人口径)已超过最高可接受价，不建议按当前价买入（你的设置）"
                if _short_caliber_label(price_scope).startswith("单人")
                else "预估实付总价已超过最高可接受价，不建议按当前价买入（你的设置）"
            ),
            "push_type_hint": None,
        }

    if budget_decision is None and display is not None and max_p is not None and display > max_p:
        return {
            "conclusion": (
                f"当前搜索价{_price_text(display)}已超过你的最高可接受价{_price_text(max_p)}，"
                "不满足购买条件，建议保持监控本条航线"
            ),
            "reason": "搜索参考价已超过最高可接受价，不建议按当前价买入（你的设置）",
            "push_type_hint": None,
        }

    if transaction is not None and verify is not None and transaction <= verify:
        return {
            "conclusion": "可以购买前验证",
            "reason": "预估实付价不高于本次验证购买价（你的设置）",
            "push_type_hint": None,
        }
    if display is not None and verify is not None and display <= verify and transaction is not None and transaction > verify:
        return {
            "conclusion": "值得验证，不建议直接下单",
            "reason": "搜索参考价达标，但预估实付价高于验证购买价（你的设置）",
            "push_type_hint": "值得验证",
        }
    if target is not None and display is not None and display > target:
        return {
            "conclusion": "继续观察",
            "reason": "搜索参考价仍高于理想入手价（你的设置）",
            "push_type_hint": None,
        }
    return {
        "conclusion": fallback or "可以观察",
        "reason": "",
        "push_type_hint": None,
    }


def _payload_primary_price_values(current, primary_plan, max_budget=None) -> dict:
    tiers = (primary_plan or {}).get("price_tiers") or {}
    display = (
        _to_float(tiers.get("total_roundtrip_ref"))
        or _to_float((primary_plan or {}).get("price"))
        or _to_float(current)
    )
    transaction = (
        _to_float(tiers.get("total_estimated"))
        or _to_float((primary_plan or {}).get("estimated_price"))
        or display
    )
    verify = _payload_verify_price(display, max_budget)
    return {
        "display_price": display,
        "transaction_price": transaction,
        "verify_price": verify,
    }



def _normalize_payload_budget_scope(value) -> str:
    text = str(value or "per_person").strip().lower()
    if text in {"all", "total", "all_passengers", "all_passenger", "overall", "\u6574\u5355", "\u5168\u5458", "\u5168\u90e8\u4eba"}:
        return "all"
    return "per_person"


def _payload_budget_visible_scope(scope: str, is_roundtrip: bool) -> str:
    normalized = _normalize_payload_budget_scope(scope)
    if normalized == "all":
        return "all_passengers_roundtrip" if is_roundtrip else "all_passengers_oneway"
    return "per_person_roundtrip" if is_roundtrip else "per_person_oneway"


def _payload_budget_scopes(subscription: dict | None) -> tuple[str, str]:
    subscription = subscription or {}
    containers = (
        subscription.get("constraints") or {},
        subscription.get("hard_constraints") or {},
        subscription.get("soft_preferences") or {},
        subscription.get("preferences") or {},
        subscription,
    )

    def first_value(*keys):
        for container in containers:
            if not isinstance(container, dict):
                continue
            for key in keys:
                value = container.get(key)
                if value not in (None, ""):
                    return value
        return None

    max_scope = _normalize_payload_budget_scope(first_value("max_budget_scope", "budget_scope"))
    target_scope = _normalize_payload_budget_scope(first_value("target_price_scope") or max_scope)
    return max_scope, target_scope


def _round_payload_price(value):
    number = _to_float(value)
    if number is None:
        return None
    rounded = round(number, 2)
    return int(rounded) if float(rounded).is_integer() else rounded



def _payload_budget_input_values(subscription, route_info, analysis_result, fallback_target=None, fallback_max=None):
    containers = (
        (subscription or {}).get("constraints") or {},
        (subscription or {}).get("hard_constraints") or {},
        (subscription or {}).get("soft_preferences") or {},
        (subscription or {}).get("preferences") or {},
        subscription or {},
        route_info or {},
        analysis_result or {},
    )

    def first_value(keys, fallback):
        for container in containers:
            if not isinstance(container, dict):
                continue
            for key in keys:
                value = container.get(key)
                if value not in (None, ""):
                    return value
        return fallback

    target_input = first_value(("target_price", "ideal_price"), fallback_target)
    max_input = first_value(("max_budget", "budget", "max_price"), fallback_max)
    return target_input, max_input


def _budget_value_in_scope(value, passengers, route_type, visible_scope, is_roundtrip):
    number = _to_float(value)
    if number is None:
        return None, None
    per_person_oneway = budget_to_pp(
        number,
        passengers,
        scope=visible_scope,
        route_type=route_type,
        round_trip=is_roundtrip,
    )
    return _round_payload_price(number), per_person_oneway


def _plan_price_in_budget_scope(primary_plan, fallback_price, passengers, route_type, is_roundtrip, visible_scope):
    primary_plan = primary_plan or {}
    if primary_plan.get("mixed_cabin"):
        if visible_scope != "all_passengers_roundtrip":
            raise AssertionError(
                "混舱预算必须使用全员往返总价口径: "
                f"visible_scope={visible_scope}"
            )
        raw_total = _to_float(
            primary_plan.get("raw_passenger_total_price")
            or (primary_plan.get("mixed_cabin_pricing") or {}).get("raw_total")
        )
        return _round_payload_price(raw_total) if raw_total is not None else None
    tiers = primary_plan.get("price_tiers") or {}
    unit_oneway = tiers.get("unit_oneway") if isinstance(tiers.get("unit_oneway"), dict) else {}
    outbound = _to_float(unit_oneway.get("outbound"))
    return_price = _to_float(unit_oneway.get("return"))

    if outbound is None:
        if is_roundtrip:
            outbound = _to_float(primary_plan.get("outbound_price") or (primary_plan.get("outbound_flight") or {}).get("price"))
        else:
            outbound = _to_float(
                primary_plan.get("single_adult_price")
                or (primary_plan.get("main_flight") or {}).get("price")
                or primary_plan.get("price")
            )
    if return_price is None and is_roundtrip:
        return_price = _to_float(primary_plan.get("return_price") or (primary_plan.get("return_flight") or {}).get("price"))

    if outbound is not None and (not is_roundtrip or return_price is not None):
        scoped = price_in_scope(
            outbound,
            passengers,
            scope=visible_scope,
            route_type=route_type,
            round_trip=is_roundtrip,
            return_per_person_oneway=return_price,
        )
        return _round_payload_price(scoped)

    unit_roundtrip = _to_float(tiers.get("unit_roundtrip") or primary_plan.get("adult_roundtrip_price"))
    if is_roundtrip and unit_roundtrip is not None:
        itinerary_pp = itinerary_price_pp(unit_roundtrip, round_trip=False)
        if visible_scope.startswith("all_passengers"):
            return _round_payload_price(itinerary_pp * passenger_rate_sum(passengers, route_type))
        return _round_payload_price(itinerary_pp)

    return _to_float(fallback_price)


def _apply_roundtrip_purchase_advice(
    plans,
    target_price,
    max_budget,
    passengers,
    route_type,
    visible_scope,
):
    for plan in plans or []:
        if not isinstance(plan, dict) or not plan.get("is_roundtrip"):
            continue
        compare_price = _plan_price_in_budget_scope(
            plan,
            plan.get("estimated_price") or plan.get("price"),
            passengers,
            route_type,
            True,
            visible_scope,
        )
        verify_limit = _payload_verify_price(compare_price, max_budget)
        decision = evaluate_purchase_budget(
            compare_price,
            target_price,
            max_budget,
            price_scope=visible_scope,
            budget_scope=visible_scope,
        )
        advice = build_execution_advice(
            compare_price,
            compare_price,
            verify_limit,
            target_price,
            max_budget,
            budget_decision=decision,
            price_scope=visible_scope,
            budget_scope=visible_scope,
        )
        plan["budget_compare_price"] = compare_price
        plan["budget_compare_scope"] = visible_scope
        plan["purchase_budget_decision"] = decision
        plan["purchase_advice"] = advice
        plan["buy_condition"] = advice.get("conclusion") or "请核对完整往返总价后决定"
    return plans

def _payload_dedupe_text(items) -> list[str]:
    result = []
    for item in items or []:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _is_previous_price_reason(text: str) -> bool:
    value = str(text or "")
    return any(
        marker in value
        for marker in (
            "较上次提醒",
            "比上次提醒",
            "与上次提醒价格",
            "比上次涨",
            "比上次降",
        )
    )


def _apply_plan_tracking_change(
    push_meta: dict | None,
    plan_status_change: dict | None,
    is_roundtrip: bool,
) -> dict:
    """用同组合的单人往返追踪结果统一推送涨跌口径。"""
    result = dict(push_meta or {})
    status_payload = plan_status_change if isinstance(plan_status_change, dict) else {}
    if not is_roundtrip or not status_payload:
        return result
    if status_payload.get("scope") != "per_person_roundtrip":
        return result

    reasons = [
        item
        for item in (result.get("reasons") or [])
        if not _is_previous_price_reason(str(item or ""))
    ]
    message = str(status_payload.get("msg") or "").strip()
    status = str(status_payload.get("status") or "")
    previous_price = _to_float(status_payload.get("previous_price"))
    current_price = _to_float(status_payload.get("current_price"))
    diff = _to_float(status_payload.get("price_diff"))

    if status == "comparison_skipped":
        result["price_change"] = None
        if result.get("type") == "价格下降":
            result["type"] = "价格口径变化"
    elif previous_price is not None and current_price is not None and diff is not None:
        result["price_change"] = {
            "last": previous_price,
            "current": current_price,
            "diff": diff,
            "direction": "down" if diff < 0 else "up" if diff > 0 else "flat",
            "scope": "per_person_roundtrip",
        }
        if status == "price_up":
            result["type"] = "涨价风险"
        elif status == "price_down":
            result["type"] = "价格下降"
        elif status == "stable" and result.get("type") == "价格下降":
            result["type"] = "价格稳定"
    else:
        result["price_change"] = None
        if result.get("type") == "价格下降":
            result["type"] = "价格状态待核实"

    if message:
        reasons.insert(0, message)
    result["reasons"] = _payload_dedupe_text(reasons)[:4]
    return result


def _source_set_from_plan(plan: dict | None) -> set[str]:
    sources: set[str] = set()
    if not isinstance(plan, dict):
        return sources
    legs = []
    if plan.get("is_roundtrip"):
        legs.extend([plan.get("outbound_flight"), plan.get("return_flight")])
    else:
        legs.append(plan.get("flight"))
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        for value in (
            leg.get("data_source"),
            leg.get("source"),
            leg.get("price_source"),
        ):
            for part in str(value or "").split("+"):
                part = part.strip().lower()
                if part:
                    sources.add(part)
        for entry in _source_price_entries_for_display(leg):
            source = str(entry.get("source") or "").strip().lower()
            if source:
                sources.add(source)
    return sources


def _parse_snapshot_sources(snapshot: dict | None) -> set[str]:
    if not isinstance(snapshot, dict):
        return set()
    raw = snapshot.get("source_set") or snapshot.get("channels") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [raw]
    sources = set()
    for item in raw or []:
        for part in str(item or "").split("+"):
            part = part.strip().lower()
            if part in {"juhe", "hasdata", "serpapi", "searchapi"}:
                sources.add(part)
    return sources


def _source_error_text(source_errors: list[dict] | None, source_name: str) -> str:
    source_name = source_name.lower()
    for item in source_errors or []:
        if str(item.get("source") or "").strip().lower() != source_name:
            continue
        text = str(item.get("error") or item.get("reason") or "").strip()
        if text:
            return text
    return ""

_LISTING_SOURCE_LABELS = {
    "juhe": "OTA交叉源",
    "hasdata": "Google数据源",
}
_LISTING_SOURCE_TRACKING_LABELS = {
    "juhe": "OTA源",
    "hasdata": "Google源",
}

_LISTING_SOURCE_POOL_LABELS = {
    "juhe": "OTA",
    "hasdata": "Google",
}



def _source_stats_count(entry: dict | None) -> int:
    try:
        return max(0, int((entry or {}).get("count") or 0))
    except (TypeError, ValueError):
        return 0


def _source_degradation_detail(
    source_name: str,
    source_stat: dict | None,
    source_errors: list[dict] | None,
) -> str:
    error_text = _source_error_text(source_errors, source_name)
    if error_text:
        return error_text
    source_stat = source_stat or {}
    reason = str(source_stat.get("reason") or "").strip()
    status = str(source_stat.get("status") or "").strip().lower()
    if status == "empty":
        return reason or "HTTP成功但空结果(原因未知)"
    if reason:
        return reason
    if status:
        return f"源状态={source_stat.get('status')}"
    return "本轮未返回有效方案(原因未知)"


def _build_collection_failure_context(
    collection_failures: list[dict] | None,
    *,
    previous_sources: set[str],
    current_sources: set[str] | None,
) -> dict:
    failures = [
        dict(item)
        for item in (collection_failures or [])
        if isinstance(item, dict)
    ]
    if not failures:
        return {"active": False}
    statements = []
    missing_sources = set()
    for failure in failures:
        direction = str(failure.get("direction") or "航段").strip()
        errors = [
            item
            for item in (failure.get("source_errors") or [])
            if isinstance(item, dict)
        ]
        for error in errors:
            source_name = str(error.get("source") or "").strip().lower()
            if source_name:
                missing_sources.add(source_name)
        detail = str(failure.get("reason") or "").strip()
        if not detail:
            detail = "; ".join(
                f"{item.get('source') or 'source'}:{item.get('error') or '返回失败'}"
                for item in errors
            )
        detail = detail or "采集源返回失败"
        statements.append(
            f"本轮{direction}采集失败(原因={detail}),结论不代表市场无票"
        )
    return {
        "active": True,
        "data_incomplete": True,
        "push_type": "数据不完整",
        "reason": "；".join(statements),
        "reason_detail": "；".join(
            str(item.get("reason") or "").strip() for item in failures
            if str(item.get("reason") or "").strip()
        ),
        "collection_failures": failures,
        "previous_sources": sorted(previous_sources),
        "current_sources": sorted(current_sources or set()),
        "missing_sources": sorted(missing_sources),
        "first_occurrence": True,
        "disclosure_level": "full",
    }


def _build_source_degradation_context(
    *,
    source_stats: dict | None,
    last_snapshot: dict | None,
    source_errors: list[dict] | None,
    current_sources: set[str] | None = None,
    retired_sources: set[str] | None = None,
    collection_failures: list[dict] | None = None,
) -> dict:
    """Build result-side source degradation evidence without changing prices."""
    source_stats = source_stats if isinstance(source_stats, dict) else {}
    previous_sources = _parse_snapshot_sources(last_snapshot)
    failure_context = _build_collection_failure_context(
        collection_failures,
        previous_sources=previous_sources,
        current_sources=current_sources,
    )
    if failure_context.get("active"):
        return failure_context
    positive_sources = {
        str(name).strip().lower()
        for name, entry in source_stats.items()
        if str(name).strip().lower() in _LISTING_SOURCE_LABELS
        and isinstance(entry, dict)
        and _source_stats_count(entry) > 0
    }
    if current_sources:
        positive_sources.update(str(item).strip().lower() for item in current_sources if item)
    retired_sources = {
        str(item).strip().lower()
        for item in (retired_sources or set())
        if str(item).strip()
    }
    unavailable = []
    for source_name, entry in source_stats.items():
        normalized = str(source_name).strip().lower()
        if (
            normalized not in _LISTING_SOURCE_LABELS
            or normalized in retired_sources
            or not isinstance(entry, dict)
        ):
            continue
        if _source_stats_count(entry) == 0:
            unavailable.append(normalized)
    first_candidates = [name for name in unavailable if name in previous_sources]
    continued = str((last_snapshot or {}).get("push_type") or "") == "数据源受限"
    source_name = next(
        (name for name in ("juhe", "hasdata") if name in first_candidates),
        "",
    )
    first_occurrence = bool(source_name)
    if not source_name and continued:
        source_name = next(
            (name for name in ("juhe", "hasdata") if name in unavailable),
            "",
        )
    if not source_name:
        return {"active": False}
    positive_label = "+".join(
        _LISTING_SOURCE_POOL_LABELS.get(name, name)
        for name in sorted(positive_sources)
    ) or "其他可用源"
    stat = source_stats.get(source_name) or {}
    detail = _source_degradation_detail(source_name, stat, source_errors)
    label = _LISTING_SOURCE_LABELS[source_name]
    if first_occurrence:
        reason = (
            f"本轮{label}不可用(原因={detail}),"
            f"入池仅{positive_label},与上次价格不可直接比"
        )
        disclosure_level = "full"
    else:
        reason = f"{label}仍不可用(原因={detail}),价格仍不可直接与恢复前比较"
        disclosure_level = "compact"
    return {
        "active": True,
        "source": source_name,
        "source_label": _LISTING_SOURCE_TRACKING_LABELS[source_name],
        "reason_detail": detail,
        "reason": reason,
        "first_occurrence": first_occurrence,
        "disclosure_level": disclosure_level,
        "previous_sources": sorted(previous_sources),
        "current_sources": sorted(positive_sources),
        "missing_sources": [source_name],
    }


def _build_source_retirement_context(
    route_type: str | None,
    last_snapshot: dict | None,
) -> dict:
    """生成计划退役披露；它不是源故障，也不参与价格判定。"""
    normalized = normalize_route_type(route_type)
    if not normalized:
        return {"active": False}
    retirements = retired_listing_sources(normalized)
    if not retirements:
        return {"active": False}
    retired_names = {
        str(item.get("name") or "").strip().lower()
        for item in retirements
        if str(item.get("name") or "").strip()
    }
    previous_sources = _parse_snapshot_sources(last_snapshot)
    previous_pushed_at = str((last_snapshot or {}).get("pushed_at") or "")[:10]
    first_occurrence = not previous_sources or bool(previous_sources.intersection(retired_names))
    retired_dates = [str(item.get("retired_on") or "")[:10] for item in retirements]
    if (
        previous_pushed_at
        and retired_dates
        and previous_pushed_at >= max(retired_dates)
        and not previous_sources.intersection(retired_names)
    ):
        first_occurrence = False
    primary = retirements[0]
    notice = ""
    if first_occurrence and str(primary.get("name") or "").strip().lower() == "hasdata":
        notice = (
            f"Google源(HasData)已于{primary.get('retired_on')}停用,"
            "此后为OTA单源+Duffel规则参考"
        )
    return {
        "active": True,
        "first_occurrence": first_occurrence,
        "notice": notice,
        "sources": sorted(retired_names),
        "retired_on": str(primary.get("retired_on") or ""),
        "reason": str(primary.get("reason") or ""),
    }


def _constraint_change_context(
    current_fingerprint: str | None,
    last_snapshot: dict | None,
) -> dict:
    current = str(current_fingerprint or "").strip()
    previous = str((last_snapshot or {}).get("constraint_fingerprint") or "").strip()
    try:
        previous_sample_n = int((last_snapshot or {}).get("constraint_sample_n") or 0)
    except (TypeError, ValueError):
        previous_sample_n = 0
    changed = bool(current and previous and current != previous)
    disclosure = ""
    if changed:
        disclosure = (
            "筛选条件已变更，"
            f"旧条件样本(n={previous_sample_n})不再计入，"
            "同条件样本重新积累"
        )
    return {
        "changed": changed,
        "current_fingerprint": current,
        "previous_fingerprint": previous,
        "previous_sample_n": previous_sample_n,
        "disclosure": disclosure,
    }


def _constraint_history_reason(text: str) -> bool:
    value = str(text or "")
    return any(
        marker in value
        for marker in (
            "较上次提醒",
            "上次同口径",
            "上涨",
            "下降",
            "价格持平",
            "历史样本",
            "相似采集记录",
            "近期低位",
            "高于大多数",
            "低于所有",
        )
    )


def _apply_constraint_change_to_push_meta(
    push_meta: dict | None,
    change: dict | None,
) -> dict:
    result = dict(push_meta or {})
    change = change or {}
    if not change.get("changed"):
        return result
    disclosure = str(change.get("disclosure") or "").strip()
    reasons = [
        str(item)
        for item in (result.get("reasons") or [])
        if not _constraint_history_reason(str(item))
    ]
    if disclosure:
        reasons.insert(0, disclosure)
    data_incomplete = bool(
        (result.get("source_degradation") or {}).get("data_incomplete")
    )
    result["type"] = "数据不完整" if data_incomplete else "筛选条件已变更"
    result["price_change"] = None
    result["percentile"] = None
    result["historical_30_price"] = None
    result["constraint_change"] = dict(change)
    result["reasons"] = _payload_dedupe_text(reasons)[:4]
    return result


def _apply_constraint_change_to_price_signal(
    price_signal: dict | None,
    change: dict | None,
) -> dict:
    result = dict(price_signal or {})
    change = change or {}
    if not change.get("changed"):
        return result
    result["label"] = "待积累"
    disclosure = str(change.get("disclosure") or "同条件样本重新积累")
    try:
        sample_n = int(result.get("sample_n") or 0)
    except (TypeError, ValueError):
        sample_n = 0
    if sample_n < MIN_SAMPLE_FOR_PRICE_SIGNAL:
        result["summary"] = (
            f"{disclosure}；同条件样本不足（当前n={sample_n}），"
            "继续积累中，暂不给出价格位置判断"
        )
    else:
        result["summary"] = disclosure
    result["percentile"] = None
    result["constraint_change"] = dict(change)
    return result


def _apply_source_degradation_to_push_meta(
    push_meta: dict | None,
    *,
    current_sources: set[str],
    previous_sources: set[str],
    source_errors: list[dict] | None = None,
    degradation_context: dict | None = None,
) -> dict:
    result = dict(push_meta or {})
    if degradation_context is not None:
        context = dict(degradation_context or {})
        if not context.get("active"):
            return result
        reason = str(context.get("reason") or "").strip()
        reasons = [
            str(item)
            for item in (result.get("reasons") or [])
            if not _is_previous_price_reason(str(item or ""))
            and "上涨" not in str(item)
            and "下降" not in str(item)
        ]
        if reason:
            reasons.insert(0, reason)
        result["type"] = context.get("push_type") or "数据源受限"
        result["price_change"] = None
        result["source_degradation"] = context
        result["reasons"] = _payload_dedupe_text(reasons)[:4]
        return result

    if not previous_sources or not current_sources:
        return result
    missing_sources = previous_sources - current_sources
    if "juhe" not in missing_sources:
        return result
    juhe_error = _source_error_text(source_errors, "juhe")
    if juhe_error and not any(marker in juhe_error for marker in ("配额", "112", "10012")):
        return result
    reason = "本轮OTA交叉源不可用"
    if juhe_error:
        if "配额" in juhe_error:
            reason += "(配额不足)"
        else:
            reason += f"({juhe_error})"
    reason += ",入池仅Google,与上次价格不可直接比"
    reasons = [
        str(item)
        for item in (result.get("reasons") or [])
        if not _is_previous_price_reason(str(item or ""))
        and "上涨" not in str(item)
        and "下降" not in str(item)
    ]
    reasons.insert(0, reason)
    result["type"] = "数据源受限"
    result["price_change"] = None
    result["source_degradation"] = {
        "previous_sources": sorted(previous_sources),
        "current_sources": sorted(current_sources),
        "missing_sources": sorted(missing_sources),
        "reason": reason,
    }
    result["reasons"] = _payload_dedupe_text(reasons)[:4]
    return result


def _price_change_scope_suffix(change: dict | None) -> str:
    change = change if isinstance(change, dict) else {}
    scope = str(change.get("scope") or "").strip()
    if not scope:
        return ""
    try:
        return f"（{caliber_label(scope)}）"
    except ValueError:
        return ""


def _email_subject(payload: dict) -> str:
    push_type = payload.get("push_type") or "价格提醒"
    route = payload.get("route") or "航班监控"
    alternatives = payload.get("same_day_alternatives") or []
    if _data_incomplete_state(payload):
        return f"【数据不完整】{route}"
    if _no_primary_plan_state(payload):
        return f"【无符合方案】{route}｜提供{len(alternatives[:3])}个备选"
    gap = _budget_gap(payload)
    headline_type = _email_headline_type(payload)
    if gap.get("is_over_budget"):
        if push_type == "前后日期更便宜":
            return f"【{headline_type}】{route}"
        return f"【{headline_type}】{route}｜当前价已高于预算"
    primary_plan = _plan_for_render((payload.get("recommended_plans") or [{}])[0] or {}, payload)
    display = _price_text(primary_plan.get("price") or payload.get("display_price") or payload.get("current_price"))
    tier = str(primary_plan.get("tier") or "").strip()
    if primary_plan and primary_plan.get("is_roundtrip") and _plan_total_stops(primary_plan) == 0:
        plan_label = "首选直飞方案"
    elif tier:
        plan_label = tier
    else:
        plan_label = "方案"
    return f"【{headline_type}】{route}｜{plan_label}{display}"


def _email_headline_type(payload: dict) -> str:
    push_type = str(payload.get("push_type") or "价格提醒")
    if push_type != "前后日期更便宜":
        return push_type
    if (_budget_gap(payload) or {}).get("is_over_budget"):
        return "超预算·别的日期更便宜"
    return "别的日期更便宜"


def _cheaper_date_trigger_evidence(payload: dict) -> str:
    if str(payload.get("push_type") or "") != "前后日期更便宜":
        return ""
    calendar = payload.get("price_calendar") or {}
    rows = [
        row
        for row in (calendar.get("rows") or [])
        if isinstance(row, dict) and _to_float(row.get("min_price")) is not None
    ]
    if not rows:
        return ""
    is_roundtrip = bool(payload.get("is_roundtrip"))
    calendar_scope = str(calendar.get("scope") or "oneway").strip().lower()
    if is_roundtrip and calendar_scope != "roundtrip":
        return ""
    selected = next((row for row in rows if row.get("selected")), None)
    if selected is None:
        depart_date = str(payload.get("depart_date") or "")[:10]
        selected = next((row for row in rows if str(row.get("date") or "")[:10] == depart_date), None)
    selected_price = _to_float((selected or {}).get("min_price"))
    if selected_price is None or selected_price <= 0:
        return ""
    alternatives = [
        row
        for row in rows
        if row is not selected and (_to_float(row.get("min_price")) or float("inf")) < selected_price
    ]
    if not alternatives:
        return ""
    cheaper = min(alternatives, key=lambda row: _to_float(row.get("min_price")) or float("inf"))
    cheaper_price = _to_float(cheaper.get("min_price"))
    if cheaper_price is None:
        return ""
    percent = int(((selected_price - cheaper_price) / selected_price) * 100 + 0.5)
    scope = "per_person_roundtrip" if calendar_scope == "roundtrip" else "per_person_oneway"
    cheaper_date = str(cheaper.get("date") or "")[:10]
    selected_date = str(selected.get("date") or payload.get("depart_date") or "")[:10]
    return (
        f"{cheaper_date[5:10]} {_price_text_with_parenthesized_caliber(cheaper_price, scope)} "
        f"比你选的 {selected_date[5:10]} {_price_text_with_parenthesized_caliber(selected_price, scope)} "
        f"低 {percent}%"
    )


def _no_result_candidate_flights(
    analysis_result: dict,
    outbound_analysis: dict | None,
    return_analysis: dict | None,
    is_roundtrip: bool,
    *,
    include_return: bool = False,
) -> list[dict]:
    sources: list[tuple[dict, str]] = []

    def _extend(analysis: dict | None, direction: str, keys=("all_flights", "recommendations")):
        if not isinstance(analysis, dict):
            return
        for key in keys:
            for item in analysis.get(key) or []:
                if isinstance(item, dict):
                    sources.append((item, direction))

    if is_roundtrip:
        _extend(outbound_analysis, "outbound")
        if include_return:
            _extend(return_analysis, "return")
        _extend(analysis_result, "outbound")
        round_trip = (analysis_result or {}).get("round_trip_analysis") or {}
        _extend(round_trip, "outbound", ("closest_same_day_outbound_options", "outbound_top3"))
        _extend(round_trip, "return", ("return_top3",))
    else:
        _extend(analysis_result, "outbound", ("all_flights", "recommendations", "economy_recommendations"))

    result = []
    seen = set()
    for item, direction in sources:
        flight = item.get("flight") if isinstance(item.get("flight"), dict) else item
        if not isinstance(flight, dict):
            continue
        candidate = dict(flight)
        candidate.setdefault("direction", direction)
        key = _no_result_notification_identity(candidate)
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _no_result_excluded_flights(
    analysis_result: dict,
    outbound_analysis: dict | None,
    return_analysis: dict | None,
) -> list[dict]:
    result = []

    def _extend(analysis: dict | None, direction: str):
        if not isinstance(analysis, dict):
            return
        for item in analysis.get("excluded_flights") or []:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row.setdefault("direction", direction)
            if isinstance(row.get("flight"), dict):
                row["flight"] = dict(row["flight"])
                row["flight"].setdefault("direction", direction)
            result.append(row)

    _extend(analysis_result, "outbound")
    _extend(outbound_analysis, "outbound")
    _extend(return_analysis, "return")
    round_trip = (analysis_result or {}).get("round_trip_analysis") or {}
    _extend(round_trip, "")
    return result


def _no_result_notification_flight(item: dict) -> dict:
    if not isinstance(item, dict):
        return {}
    flight = item.get("flight")
    return flight if isinstance(flight, dict) else item


def _no_result_notification_identity(item: dict) -> tuple:
    flight = _no_result_notification_flight(item)
    return (
        normalize_combo(flight.get("flight_combo") or flight.get("flight_no") or ""),
        str(flight.get("departure_airport") or flight.get("origin") or "").strip().upper(),
        str(flight.get("arrival_airport") or flight.get("destination") or "").strip().upper(),
        str(flight.get("departure_time") or flight.get("dep_time") or "").strip(),
        str(flight.get("arrival_time") or flight.get("arr_time") or "").strip(),
    )


def _no_result_pairing_failure_reason(
    analysis_result: dict,
    return_analysis: dict | None,
    is_roundtrip: bool,
) -> tuple[str, str]:
    if not is_roundtrip:
        return "", ""
    return_candidates = []
    if isinstance(return_analysis, dict):
        for key in ("all_flights", "recommendations", "economy_recommendations"):
            return_candidates.extend(return_analysis.get(key) or [])
    if return_candidates:
        return "去返程未能组成完整往返", "roundtrip_pairing_failed"
    source_errors = []
    for container in (analysis_result, return_analysis):
        if isinstance(container, dict):
            source_errors.extend(container.get("source_errors") or [])
    if source_errors:
        return "返程采集失败，无法组成完整往返", "return_collection_failed"
    return "返程无可用候选，无法组成完整往返", "return_candidates_empty"


def _build_single_leg_rejection_rows(
    candidates: list[dict] | None,
    excluded: list[dict] | None,
    *,
    default_reason: str,
    default_reason_code: str,
    limit: int = 10,
) -> list[dict]:
    rows = []
    represented = set()
    seen = set()

    for item in excluded or []:
        if not isinstance(item, dict):
            continue
        flight = _no_result_notification_flight(item)
        identity = _no_result_notification_identity(item)
        direction = str(item.get("direction") or flight.get("direction") or "outbound")
        reason = str(item.get("reason") or item.get("exclude_reason") or flight.get("exclude_reason") or "不满足当前约束")
        code = str(item.get("filter_reason_code") or flight.get("filter_reason_code") or "")
        value = str(item.get("filter_reason_value") or flight.get("filter_reason_value") or "")
        dedupe_key = (identity, direction, code, reason)
        if not identity[0] or dedupe_key in seen:
            continue
        row = dict(item)
        row["flight"] = dict(flight)
        row["price"] = _to_float(item.get("price") or flight.get("price"))
        row["direction"] = direction
        row["reason"] = reason
        row["filter_reason_code"] = code
        row["filter_reason_value"] = value
        row["stops"] = flight.get("stops", item.get("stops", 0))
        rows.append(row)
        represented.add(identity)
        seen.add(dedupe_key)

    for flight in candidates or []:
        if not isinstance(flight, dict):
            continue
        identity = _no_result_notification_identity(flight)
        if not identity[0] or identity in represented:
            continue
        direction = str(flight.get("direction") or "outbound")
        row = {
            "flight": dict(flight),
            "price": _to_float(flight.get("price")),
            "direction": direction,
            "reason": str(default_reason or "不满足当前约束"),
            "filter_reason_code": str(default_reason_code or ""),
            "filter_reason_value": "",
            "stops": flight.get("stops", 0),
        }
        dedupe_key = (identity, direction, row["filter_reason_code"], row["reason"])
        if dedupe_key in seen:
            continue
        rows.append(row)
        represented.add(identity)
        seen.add(dedupe_key)

    return sorted(
        rows,
        key=lambda item: (
            _to_float(item.get("price")) if _to_float(item.get("price")) is not None else float("inf"),
            _no_result_notification_identity(item),
        ),
    )[: max(1, int(limit))]

def _prepare_no_result_alternatives(
    alternatives: list[dict] | None,
    candidates: list[dict] | None,
    excluded: list[dict] | None,
    *,
    default_reason: str,
    default_reason_code: str,
) -> list[dict]:
    if not alternatives:
        return build_no_result_alternatives(
            candidates,
            excluded,
            3,
            default_reason=default_reason,
            default_reason_code=default_reason_code,
        )

    reason_by_identity = {}
    for excluded_item in excluded or []:
        if not isinstance(excluded_item, dict):
            continue
        identity = _no_result_notification_identity(excluded_item)
        if not identity[0]:
            continue
        flight = _no_result_notification_flight(excluded_item)
        reason_by_identity[identity] = {
            "reason": str(
                excluded_item.get("reason")
                or excluded_item.get("exclude_reason")
                or flight.get("exclude_reason")
                or "该候选的逐航班拒因未保留"
            ),
            "filter_reason_code": str(
                excluded_item.get("filter_reason_code")
                or flight.get("filter_reason_code")
                or ""
            ),
            "filter_reason_value": str(
                excluded_item.get("filter_reason_value")
                or flight.get("filter_reason_value")
                or ""
            ),
        }

    prepared = []
    for original in alternatives:
        if not isinstance(original, dict):
            continue
        item = dict(original)
        exact = reason_by_identity.get(_no_result_notification_identity(item))
        existing = str(item.get("unmet_reason") or "").strip()
        if exact:
            item["unmet_reason"] = exact["reason"]
            item["filter_reason_code"] = exact["filter_reason_code"]
            item["filter_reason_value"] = exact["filter_reason_value"]
        elif not existing or existing == "不满足当前约束":
            item["unmet_reason"] = str(
                default_reason or "该候选的逐航班拒因未保留"
            )
            item["filter_reason_code"] = str(default_reason_code or "")
            item["filter_reason_value"] = ""
        prepared.append(item)
    return prepared

def _layered_channel_links(link_html: str) -> str:
    anchors = re.findall(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', str(link_html or ""), flags=re.I)
    if not anchors:
        return ""
    priority_names = ("Trip.com", "Google Flights", "携程")
    primary = []
    backup = []
    seen = set()
    for url, name in anchors:
        clean_name = html.unescape(re.sub(r"<.*?>", "", name)).strip()
        if not clean_name or (clean_name, url) in seen:
            continue
        seen.add((clean_name, url))
        anchor = f'<a href="{html.escape(url)}" target="_blank">{html.escape(clean_name)}</a>'
        if any(key in clean_name for key in priority_names):
            primary.append(anchor)
        else:
            backup.append(anchor)
    lines = []
    if primary:
        lines.append("优先验证渠道：" + " | ".join(primary))
    if backup:
        lines.append("备用渠道：" + " | ".join(backup))
    return "<br>".join(lines)


def _payload_source_summary(source_stats: dict) -> str:
    names = []
    mapping = {
        "juhe": "聚合数据",
        "serpapi": "Google Flights via SerpAPI",
        "searchapi": "Google Flights via SearchAPI",
        "hasdata": "Google Flights via HasData",
        "duffel": "Duffel",
    }
    for key, value in (source_stats or {}).items():
        if not isinstance(value, dict):
            continue
        name = mapping.get(str(key).lower(), str(key))
        if name not in names:
            names.append(name)
    return "、".join(names)


def _judgment_limit_items(
    route_info: dict,
    analysis_result: dict,
    price_insights: dict | None,
    is_round_trip: bool,
    return_analysis: dict | None = None,
) -> list[str]:
    limits = ["显示价格仍需支付页最终确认"]
    if not _route_is_domestic(route_info):
        limits.append("国际航线票规可能存在渠道差异")
    if _has_transfer_options(analysis_result, return_analysis):
        limits.append("如涉及中转，需确认是否联程及是否需要过境签")
    history_count = _history_count_for_limits(analysis_result, price_insights, is_round_trip)
    if history_count >= 14:
        limits.append("历史价格反映相似区间，不代表未来必然重复")
    else:
        limits.append("历史样本仍在积累，价格区间判断会随数据增多而更新")
    return limits


def _first_time_from_text(value: str) -> str:
    match = re.search(r"(\d{1,2}:\d{2})", str(value or ""))
    return match.group(1) if match else ""


def _purchase_checklist_items(route_info: dict, analysis_result: dict, primary_plan: dict | None = None, verify_price=None) -> list[str]:
    primary_plan = primary_plan or {}
    verify_text = _price_text(verify_price) if verify_price else "可接受范围"
    checklist = [
        f"支付页最终价是否≤{verify_text}",
        "是否含税费、燃油费、平台服务费",
        f"是否含托运行李；若不含，加购后是否仍≤{verify_text}",
        "退改签规则是否可接受",
    ]
    if primary_plan.get("is_roundtrip"):
        outbound_time = _first_time_from_text(primary_plan.get("outbound_line") or primary_plan.get("outbound_push_line"))
        return_time = _first_time_from_text(primary_plan.get("return_line") or primary_plan.get("return_push_line"))
        if outbound_time:
            checklist.append(f"去程{outbound_time}起飞是否可接受")
        if return_time:
            checklist.append(f"返程{return_time}起飞是否可接受")
        checklist.append("是往返组合还是两个单程分别购买")
        combined_text = " ".join(str(primary_plan.get(key) or "") for key in ("outbound_line", "return_line", "tags"))
        if "中转" in combined_text:
            checklist.append("中转是否为联程票、是否需要过境签")
    else:
        dep_time = _first_time_from_text(primary_plan.get("summary") or primary_plan.get("main_push_line"))
        if dep_time:
            checklist.append(f"去程{dep_time}起飞是否可接受")
        if "中转" in str(primary_plan.get("summary") or primary_plan.get("tags") or ""):
            checklist.append("中转是否为联程票、是否需要过境签")
    companions = _preference_value(route_info, analysis_result, "companions", "solo")
    if companions in {"with_elderly", "with_child", "with_elderly_child", "with_both"}:
        checklist.extend(["是否避免红眼和凌晨到达", "中转时间是否充裕（建议≥2小时）"])
    return checklist


def _sorting_logic_items(route_info: dict, is_round_trip: bool) -> list[str]:
    max_budget = _to_float(route_info.get("max_budget") or route_info.get("budget"))
    target = _to_float(route_info.get("target_price"))
    if is_round_trip:
        max_budget = max_budget * 2 if max_budget else None
        target = target * 2 if target else None
    return [
        f"不超过最高预算 {_price_text(max_budget) if max_budget else '当前配置'}",
        "满足托运行李要求",
        "尽量直飞/低中转风险",
        f"接近理想入手价 {_price_text(target) if target else '合理价格'}",
        "购买渠道可靠",
    ]


def _payload_travel_profile(analysis_result: dict, subscription: dict) -> tuple[dict, dict]:
    round_trip = (analysis_result or {}).get("round_trip_analysis") or {}
    subscription = subscription or {}
    soft = dict(subscription.get("soft_preferences") or {})
    preferences = subscription.get("preferences") or {}
    constraints = subscription.get("constraints") or {}
    hard_constraints = subscription.get("hard_constraints") or {}
    for key in ("travel_purposes", "travel_scenarios", "travel_scenario"):
        if not soft.get(key) and preferences.get(key):
            soft[key] = preferences.get(key)
    for key in ("trip_natures", "trip_nature"):
        if not soft.get(key):
            if preferences.get(key):
                soft[key] = preferences.get(key)
            elif constraints.get(key):
                soft[key] = constraints.get(key)
            elif hard_constraints.get(key):
                soft[key] = hard_constraints.get(key)
    total_passengers, passenger_breakdown = get_total_passengers(subscription)
    if passenger_breakdown:
        soft["passengers"] = passenger_breakdown
    soft["passenger_count"] = total_passengers
    subscription_scenarios = (
        soft.get("travel_purposes")
        or soft.get("travel_scenarios")
        or soft.get("travel_scenario")
        or soft.get("trip_natures")
        or soft.get("trip_nature")
    )
    subscription_profile = build_travel_profile(soft) if subscription_scenarios else None
    profile = (
        round_trip.get("travel_profile")
        or (analysis_result or {}).get("travel_profile")
        or subscription_profile
        or build_travel_profile(soft)
    )
    if subscription_profile and (
        profile.get("scenarios") != subscription_profile.get("scenarios")
        or profile.get("passenger_count") != total_passengers
    ):
        profile = subscription_profile
    profile = dict(profile)
    profile["passenger_count"] = total_passengers
    if passenger_breakdown:
        profile["passengers"] = passenger_breakdown
    refreshed_profile = build_travel_profile(profile)
    profile["passenger_profile"] = refreshed_profile.get("passenger_profile") or profile.get("passenger_profile")
    profile["passenger_rules"] = refreshed_profile.get("passenger_rules") or profile.get("passenger_rules")
    profile["score_weights"] = refreshed_profile.get("score_weights") or profile.get("score_weights")
    explanation = (
        round_trip.get("travel_profile_explanation")
        or (analysis_result or {}).get("travel_profile_explanation")
        or travel_profile_explanation(profile)
    )
    if explanation.get("scenarios") != profile.get("scenarios"):
        explanation = travel_profile_explanation(profile)
    return profile, explanation


def _scenario_recommendation_text(
    explanation: dict,
    profile: dict | None = None,
    recommendation_basis: dict | None = None,
) -> str:
    if recommendation_basis and recommendation_basis.get("recommendation_text"):
        return str(recommendation_basis["recommendation_text"])
    scenario = (explanation or {}).get("scenario") or (profile or {}).get("scenario")
    scenarios = set((explanation or {}).get("scenarios") or (profile or {}).get("scenarios") or [scenario])
    if "tourism" in scenarios and "family" in scenarios:
        return "该方案白天直飞、行李明确，价格也在合理区间，适合带孩子的旅行，兼顾省心和性价比。"
    if ("elderly" in scenarios or "with_elderly" in scenarios) and (
        "family_visit" in scenarios or "visit_family" in scenarios
    ):
        return "该方案直飞、白天到达、行李充足，转机风险低，适合带老人回家探亲。"
    if "business" in scenarios and "price_first" in scenarios:
        return "该方案优先保证准点、直飞和低风险，并在同类稳妥方案里兼顾较低价格。"
    if "price_first" in scenarios and "important" in scenarios:
        return "该方案先按重要事项保证可靠性，再在可执行方案中兼顾低价。"
    mapping = {
        "business": "该方案价格不一定最低，但更重视到达时间稳定、直飞/低风险和可改签，适合商务出行。",
        "family": "该方案优先考虑白天直飞、行李明确和低中转风险，适合带孩子出行，减少折腾。",
        "elderly": "该方案优先考虑直飞/短中转、白天到达和全服务航司，转机风险更低，适合老人出行。",
        "with_elderly": "该方案优先考虑直飞/短中转、白天到达和全服务航司，转机风险更低，适合老人出行。",
        "important": "该方案更重视稳定到达和可退改，适合考试、婚礼、医疗、邮轮等重要行程。",
        "price_first": "该方案更看重当前低价区间；如果能接受时间和中转不便，性价比更高。",
        "tourism": "该方案兼顾低价日期和合理中转，适合旅游行程继续比较。",
        "family_visit": "该方案更重视行李明确和合理价格，不推荐极端折腾方案。",
        "visit_family": "该方案更重视行李明确和合理价格，不推荐极端折腾方案。",
    }
    return mapping.get(scenario, "本次按价格、时间、舒适度、执行风险和行李票规综合排序。")


def build_notification_payload(
    analysis_result,
    outbound_analysis=None,
    return_analysis=None,
    route_info=None,
    subscription=None,
    price_history=None,
    source_stats=None,
    price_insights=None,
) -> dict:
    """Build one normalized notification payload for every delivery channel."""
    route_info = dict(route_info or {})
    subscription = subscription or {}
    if subscription:
        subscription_id = _notification_subscription_id(route_info, subscription)
        if subscription_id is not None:
            route_info.setdefault("subscription_id", subscription_id)
    analysis_result = analysis_result or outbound_analysis or {}
    outbound_analysis = outbound_analysis or analysis_result
    return_analysis = return_analysis or analysis_result.get("return_analysis") or {}
    is_roundtrip = bool(route_info.get("round_trip"))
    source_stats = source_stats or route_info.get("source_stats") or analysis_result.get("source_stats")
    source_errors = (
        analysis_result.get("source_errors")
        or route_info.get("source_errors")
        or []
    )
    collection_failures = (
        analysis_result.get("collection_failures")
        or route_info.get("collection_failures")
        or []
    )
    payload_route_type = (
        ((subscription.get("basic") or {}).get("route_type"))
        or subscription.get("route_type")
        or route_info.get("route_type")
        or _source_stats_route_type(source_stats)
    )
    decision, confidence, current, target, max_budget = _decision_context(
        analysis_result,
        route_info,
        source_stats,
        price_insights,
        is_roundtrip,
    )
    route_key, depart_key, return_key = _last_push_route_parts(route_info, is_roundtrip)
    subscription_snapshot_id = _notification_subscription_id(
        route_info,
        subscription,
    )
    last_push = get_last_push_price(
        route_key,
        depart_key,
        return_key,
        subscription_id=subscription_snapshot_id,
    )
    last_snapshot = get_last_push_snapshot(
        route_key,
        depart_key,
        return_key,
        subscription_id=subscription_snapshot_id,
    )
    source_retirement_context = _build_source_retirement_context(
        payload_route_type,
        last_snapshot,
    )
    source_degradation_context = _build_source_degradation_context(
        source_stats=source_stats,
        last_snapshot=last_snapshot,
        source_errors=source_errors,
        retired_sources=set(source_retirement_context.get("sources") or []),
        collection_failures=collection_failures,
    )
    current_constraint_fingerprint = str(
        route_info.get("constraint_fingerprint")
        or analysis_result.get("constraint_fingerprint")
        or (constraint_fingerprint(subscription) if subscription else "")
    ).strip()
    constraint_change = _constraint_change_context(
        current_constraint_fingerprint,
        last_snapshot,
    )
    history = (
        _chart_history_for_message(
            route_info,
            analysis_result,
            price_insights,
            is_roundtrip,
        )
        if is_roundtrip or price_history is None
        else price_history
    )
    risk = (
        (analysis_result.get("round_trip_analysis") or {}).get("buy_vs_wait_risk")
        if is_roundtrip
        else analysis_result.get("buy_vs_wait_risk")
    ) or {}
    travel_profile, profile_explanation = _payload_travel_profile(analysis_result, subscription)
    total_passengers, passenger_breakdown = get_total_passengers(subscription)
    print(
        "[人数调试] basic.passenger_count = "
        f"{((subscription or {}).get('basic') or {}).get('passenger_count')}"
    )
    print(
        "[人数调试] preferences.passengers = "
        f"{((subscription or {}).get('preferences') or {}).get('passengers')}"
    )
    print(
        "[人数定位] 完整订阅: "
        f"{json.dumps(subscription or {}, ensure_ascii=False, default=str)}"
    )
    print(f"[人数调试] 推送将显示总数 = {travel_profile.get('passenger_count') or total_passengers}")
    print(
        "[场景调试] 订阅里的 travel_scenarios = "
        f"{((subscription or {}).get('soft_preferences') or {}).get('travel_scenarios')}"
    )
    print(f"[场景调试] 画像里的 scenarios = {travel_profile.get('scenarios')}")
    recommendation_basis = (
        ((analysis_result.get("round_trip_analysis") or {}).get("recommendation_basis"))
        or analysis_result.get("recommendation_basis")
        or build_recommendation_basis(travel_profile)
    )
    if recommendation_basis.get("scenarios") != travel_profile.get("scenarios"):
        recommendation_basis = build_recommendation_basis(travel_profile)

    passenger_pricing_breakdown = passenger_breakdown or {
        "adult": total_passengers,
        "child": 0,
        "elderly": 0,
        "infant": 0,
    }
    if is_roundtrip:
        all_items = [
            _payload_combo_plan(combo, route_info, index, "推荐" if index < 2 else "备选")
            for index, combo in enumerate(_round_trip_combinations(analysis_result)[:5])
        ]
        mixed_matching = (analysis_result.get("round_trip_analysis") or {}).get(
            "mixed_cabin_matching"
        )
        if mixed_matching:
            stats = mixed_matching.get("stats") or {}
            render_matching = {
                **stats,
                "business_visible_count": mixed_matching.get("business_visible_count", 0),
                "business_reference": mixed_matching.get("business_reference"),
            }
            for plan in all_items:
                if plan.get("mixed_cabin"):
                    plan["mixed_cabin_matching"] = render_matching
    else:
        flights = _single_flights_for_sections(analysis_result)
        all_items = [
            _payload_single_plan(flight, route_info, analysis_result, index, "推荐" if index < 2 else "备选")
            for index, flight in enumerate(flights[:5])
        ]
    outbound_source_price_anomalies = (
        outbound_analysis.get("dual_source_price_anomalies")
        or analysis_result.get("dual_source_price_anomalies")
        or []
    )
    return_source_price_anomalies = return_analysis.get("dual_source_price_anomalies") or []
    all_items = _attach_source_price_anomalies_to_plans(
        all_items,
        outbound_source_price_anomalies,
        return_source_price_anomalies,
    )
    all_items = _apply_plan_tiers(all_items)
    passenger_profile = (
        (analysis_result or {}).get("passenger_profile")
        or travel_profile.get("passenger_profile")
        or {}
    )
    passenger_rules = (
        (analysis_result or {}).get("passenger_rules")
        or travel_profile.get("passenger_rules")
        or {}
    )
    all_items = _apply_passenger_pricing_to_plans(
        all_items,
        passenger_pricing_breakdown,
        payload_route_type,
    )
    all_items = _apply_passenger_friendly_to_plans(all_items, passenger_profile)
    constraints_for_cabin = {
        **(subscription.get("hard_constraints") or {}),
        **(subscription.get("constraints") or {}),
        "passenger_count": (subscription.get("basic") or {}).get("passenger_count")
        or subscription.get("passenger_count"),
    }
    all_items = _apply_departure_feasibility_to_plans(
        all_items,
        constraints_for_cabin,
        payload_route_type,
        route_info,
    )
    cabin_policy_summary = (
        analysis_result.get("cabin_policy_summary")
        or outbound_analysis.get("cabin_policy_summary")
        or build_cabin_policy_summary(
            constraints_for_cabin,
            _tracking_current_flights(
                analysis_result,
                all_items,
                is_roundtrip,
                return_analysis=return_analysis,
            ),
        )
    )
    plan_status_change = track_plan_status(
        _first_nonempty_identity(
            route_info.get("subscription_id"),
            subscription.get("id"),
            route_key,
        ),
        _tracking_current_items(
            analysis_result,
            all_items,
            is_roundtrip,
            return_analysis=return_analysis,
        ),
        source_degradation=source_degradation_context,
    )

    primary_flight = None
    if is_roundtrip:
        combos = _round_trip_combinations(analysis_result)
        primary_flight = (combos[0].get("outbound") or {}) if combos else {}
    else:
        flights = _single_flights_for_sections(analysis_result)
        primary_flight = flights[0] if flights else {}

    primary_plan = all_items[0] if all_items else {}
    budget_scope, target_budget_scope = _payload_budget_scopes(subscription)
    budget_compare_scope = _payload_budget_visible_scope(budget_scope, is_roundtrip)
    target_compare_scope = _payload_budget_visible_scope(target_budget_scope, is_roundtrip)
    assert_same_caliber(budget_compare_scope, _payload_budget_visible_scope(budget_scope, is_roundtrip))
    assert_same_caliber(target_compare_scope, _payload_budget_visible_scope(target_budget_scope, is_roundtrip))
    budget_input_target, budget_input_max = _payload_budget_input_values(
        subscription,
        route_info,
        analysis_result,
        fallback_target=target,
        fallback_max=max_budget,
    )
    max_budget_value = _to_float(budget_input_max)
    target_value = _to_float(budget_input_target)
    compare_max_budget, max_budget_pp_oneway = _budget_value_in_scope(
        max_budget_value,
        passenger_pricing_breakdown,
        payload_route_type,
        budget_compare_scope,
        is_roundtrip,
    )
    compare_target, target_price_pp_oneway = _budget_value_in_scope(
        target_value,
        passenger_pricing_breakdown,
        payload_route_type,
        target_compare_scope,
        is_roundtrip,
    )
    price_tiers = (
        primary_plan.get("price_tiers")
        or ((analysis_result.get("round_trip_analysis") or {}).get("price_tiers") if is_roundtrip else {})
        or {}
    )
    price_values = _payload_primary_price_values(current, primary_plan, max_budget)
    display_price = price_values.get("display_price")
    transaction_price = price_values.get("transaction_price")
    budget_compare_price = _plan_price_in_budget_scope(
        primary_plan,
        transaction_price if transaction_price is not None else display_price,
        passenger_pricing_breakdown,
        payload_route_type,
        is_roundtrip,
        budget_compare_scope,
    )
    verify_limit = _payload_verify_price(budget_compare_price, compare_max_budget)
    policy_compare_price = budget_compare_price
    purchase_budget_decision = evaluate_purchase_budget(
        policy_compare_price,
        compare_target,
        compare_max_budget,
        price_scope=budget_compare_scope,
        budget_scope=budget_compare_scope,
    )
    if is_roundtrip:
        all_items = _apply_roundtrip_purchase_advice(
            all_items,
            compare_target,
            compare_max_budget,
            passenger_pricing_breakdown,
            payload_route_type,
            budget_compare_scope,
        )
        primary_plan = all_items[0] if all_items else {}
        purchase_budget_decision = (
            primary_plan.get("purchase_budget_decision")
            or purchase_budget_decision
        )
    price_policy = _payload_price_policy_decision(
        policy_compare_price,
        policy_compare_price,
        verify_limit,
        compare_target,
        compare_max_budget,
        decision.get("conclusion") or "\u53ef\u4ee5\u89c2\u5bdf",
        price_scope=budget_compare_scope,
        budget_decision=purchase_budget_decision,
    )
    signal_history = _price_history_for_push(
        price_insights,
        analysis_result,
        is_roundtrip,
    )
    price_signal = build_price_signal(
        policy_compare_price,
        compare_target,
        signal_history,
    )
    signal_metadata = _price_signal_provenance_metadata(
        signal_history,
        price_insights,
        analysis_result,
        is_roundtrip,
    )
    signal_metadata["sample_n"] = int(
        price_signal.get("sample_n")
        if price_signal.get("sample_n") is not None
        else signal_metadata.get("sample_n") or 0
    )
    if not signal_metadata.get("sources"):
        signal_metadata["sources"] = sorted(_source_set_from_plan(primary_plan))
    price_signal.update(signal_metadata)
    price_signal = _apply_constraint_change_to_price_signal(
        price_signal,
        constraint_change,
    )
    execution_advice = build_execution_advice(
        policy_compare_price,
        policy_compare_price,
        verify_limit,
        compare_target,
        compare_max_budget,
        budget_decision=purchase_budget_decision,
        price_scope=budget_compare_scope,
        budget_scope=budget_compare_scope,
    )
    if execution_advice.get("conclusion"):
        price_policy["conclusion"] = execution_advice["conclusion"]
    if execution_advice.get("summary"):
        price_policy["reason"] = execution_advice["summary"]
    calendar_analysis = dict(analysis_result)
    if return_analysis and not calendar_analysis.get("return_analysis"):
        calendar_analysis["return_analysis"] = return_analysis
    price_calendar_payload = _payload_price_calendar(route_info, calendar_analysis)
    push_analysis = dict(analysis_result)
    if price_calendar_payload.get("rows"):
        push_analysis["price_calendar"] = price_calendar_payload
    push_analysis["decision_prices"] = {
        "display_price": display_price,
        "transaction_price": transaction_price,
        "budget_compare_price": budget_compare_price,
        "budget_compare_scope": budget_compare_scope,
        "verify_price": verify_limit,
    }
    push_meta = determine_push_type(
        budget_compare_price,
        compare_target,
        compare_max_budget,
        _price_history_for_push(price_insights, analysis_result, is_roundtrip),
        analysis_result.get("days_to_dept"),
        None if is_roundtrip else (last_push or {}).get("price"),
        push_analysis,
    )
    execution_advice = build_execution_advice(
        policy_compare_price,
        policy_compare_price,
        verify_limit,
        compare_target,
        compare_max_budget,
        (push_meta or {}).get("type"),
        budget_decision=purchase_budget_decision,
        price_scope=budget_compare_scope,
        budget_scope=budget_compare_scope,
    )
    if purchase_budget_decision.get("is_over_budget"):
        compare_subject = _estimated_price_subject(budget_compare_price, budget_compare_scope)
        compare_max_text = _price_text_with_parenthesized_caliber(compare_max_budget, budget_compare_scope)
        compare_is_per_person = _short_caliber_label(budget_compare_scope).startswith("单人")
        execution_advice = {
            "label": "保持监控本条航线",
            "conclusion": (
                f"{compare_subject}已超过你的最高可接受价{compare_max_text}，"
                "不满足购买条件，建议继续保持监控本条航线"
            ),
            "summary": (
                "单人参考价(成人口径)已超过最高可接受价（你的设置）"
                if compare_is_per_person
                else "预估实付总价已超过最高可接受价（你的设置）"
            ),
            "condition": _budget_purchase_condition(compare_max_budget, budget_compare_scope),
        }
    if execution_advice.get("conclusion"):
        price_policy["conclusion"] = execution_advice["conclusion"]
    if execution_advice.get("summary"):
        price_policy["reason"] = execution_advice["summary"]
    if is_roundtrip and primary_plan:
        primary_plan["purchase_budget_decision"] = purchase_budget_decision
        primary_plan["purchase_advice"] = execution_advice
        primary_plan["buy_condition"] = price_policy.get("conclusion") or execution_advice.get("conclusion")
    if price_policy.get("push_type_hint"):
        push_meta["type"] = price_policy["push_type_hint"]
    if price_policy.get("reason"):
        push_meta["reasons"] = _payload_dedupe_text([price_policy["reason"]] + (push_meta.get("reasons") or []))[:4]
    push_meta = _apply_plan_tracking_change(push_meta, plan_status_change, is_roundtrip)
    current_source_set = _source_set_from_plan(primary_plan)
    previous_source_set = _parse_snapshot_sources(last_snapshot)
    push_meta = _apply_source_degradation_to_push_meta(
        push_meta,
        current_sources=current_source_set,
        previous_sources=previous_source_set,
        source_errors=source_errors,
        degradation_context=(
            source_degradation_context
            if source_degradation_context.get("active")
            else None
        ),
    )
    push_meta = _apply_constraint_change_to_push_meta(
        push_meta,
        constraint_change,
    )
    if (
        plan_status_change
        and plan_status_change.get("status") == "sold_out"
        and push_meta.get("type") not in {"数据源受限", "数据不完整"}
        and not constraint_change.get("changed")
    ):
        push_meta["type"] = "涨价风险"

    change = (push_meta or {}).get("price_change") or {}
    fallback_line = (
        ""
        if constraint_change.get("changed")
        else _trend_fallback_line(history)
    )
    trend_summary = (
        str(constraint_change.get("disclosure") or "")
        if constraint_change.get("changed")
        else (_trend_linechart_summary(history, target, display_price, None) if history else "")
    )
    goals = (
        route_info.get("notification_goals")
        or analysis_result.get("notification_goals")
        or subscription.get("notification_goals")
        or {}
    )
    frequency = "important_only"
    if isinstance(goals, dict):
        frequency = {
            "daily_summary": "daily_digest",
            "every_change": "price_change",
        }.get(goals.get("frequency") or "important_only", goals.get("frequency") or "important_only")
    privacy_level = resolve_notification_privacy_level(
        goals if isinstance(goals, dict) else {}
    )
    secondary_goals = goals.get("secondary") if isinstance(goals, dict) else []
    if not isinstance(secondary_goals, list):
        secondary_goals = []
    budget_gap = build_budget_gap(
        budget_compare_price,
        compare_max_budget,
        compare_target,
    )
    budget_gap["budget_scope"] = budget_scope
    budget_gap["target_price_scope"] = target_budget_scope
    budget_gap["budget_compare_scope"] = budget_compare_scope
    budget_gap["target_compare_scope"] = target_compare_scope
    if is_roundtrip:
        unit_roundtrip = _to_float(
            (price_tiers or {}).get("unit_roundtrip")
            or primary_plan.get("adult_roundtrip_price")
            or primary_plan.get("single_adult_price")
        )
        max_budget_unit_roundtrip = (
            itinerary_price_pp(max_budget_pp_oneway, round_trip=True)
            if max_budget_pp_oneway is not None
            else None
        )
        if primary_plan and unit_roundtrip is not None:
            diagnosis_consistent = (
                bool(purchase_budget_decision.get("is_over_budget"))
                == bool(budget_gap.get("is_over_budget"))
            )
            safe_log(
                "[购买建议] "
                f"unit_roundtrip={_round_payload_price(unit_roundtrip)} "
                f"max_budget={_round_payload_price(max_budget_unit_roundtrip)} "
                f"判定={purchase_budget_decision.get('status')} "
                f"与排除诊断一致={diagnosis_consistent}"
            )
    next_step_guidance = build_next_step_guidance(
        (push_meta or {}).get("type"),
        budget_compare_price,
        compare_max_budget,
        compare_target,
        (travel_profile.get("scenarios") or []) + (recommendation_basis.get("scenario_labels") or []),
        (travel_profile.get("time") == "high" or travel_profile.get("risk_averse") == "high"),
    )
    calendar_goal_enabled = (
        "cheaper_date" in secondary_goals
        or "nearby_date_cheaper" in secondary_goals
        or (isinstance(goals, dict) and goals.get("primary") == "cheaper_date")
    )
    calendar_savings = price_calendar_payload.get("savings") or []
    if calendar_goal_enabled and calendar_savings:
        best_saving = max(calendar_savings, key=lambda item: _to_float(item.get("save")) or 0)
        if (_to_float(best_saving.get("save")) or 0) >= 200:
            push_meta["type"] = "前后日期更便宜"
            reason = (
                best_saving.get("tip")
                or f"{best_saving.get('date')}比目标日便宜{_price_text(best_saving.get('save'))}"
            )
            push_meta["reasons"] = _payload_dedupe_text([reason] + (push_meta.get("reasons") or []))[:4]
    form_url = _subscription_edit_url(route_info)
    feedback_url = _feedback_url(route_info)
    detail_id = canonical_detail_uuid(route_info.get("subscription_id"))
    detail_url = (
        f"{_subscription_form_url(route_info).rstrip('/')}/detail?sub={quote(detail_id)}"
        if detail_id
        else ""
    )
    merged_constraints = {
        **(subscription.get("hard_constraints") or {}),
        **(subscription.get("constraints") or {}),
    }
    time_filter_note = ""
    if merged_constraints.get("time_source") == "meeting_derived" or (
        merged_constraints.get("same_day_round_trip")
        and merged_constraints.get("business_start")
        and merged_constraints.get("business_end")
    ):
        reserve_note = (
            "机场标准缓冲+车程估算+路途冗余+安全余量"
            if not merged_constraints.get("buffer_hours")
            else f"{merged_constraints.get('buffer_hours') or 2.5}h预留"
        )
        time_filter_note = (
            f"时间筛选:按会议安排({merged_constraints.get('business_start')}-{merged_constraints.get('business_end')})"
            f"+{reserve_note}推算,你的通用时间偏好本次未参与筛选。"
        )
    same_day_alternatives = (
        (analysis_result.get("round_trip_analysis") or {}).get("same_day_alternatives")
        or analysis_result.get("same_day_alternatives")
        or []
    )
    no_primary_candidates = []
    no_primary_diagnosis = {}
    candidate_price_summary = {}
    no_primary_reason = ""
    no_primary_default_reason = ""
    no_primary_default_reason_code = ""
    single_leg_rejections = []
    data_incomplete = bool(source_degradation_context.get("data_incomplete"))
    if not all_items and data_incomplete:
        no_primary_reason = (
            f"{source_degradation_context.get('reason') or '本轮采集失败'}。"
            "数据不完整,本轮结论不可用。"
        )
        no_primary_diagnosis = {"counts": {}, "data_incomplete": True}
        same_day_alternatives = []
    elif not all_items:
        no_primary_candidates = _no_result_candidate_flights(
            analysis_result,
            outbound_analysis,
            return_analysis,
            is_roundtrip,
        )
        no_primary_excluded = _no_result_excluded_flights(
            analysis_result,
            outbound_analysis,
            return_analysis,
        )
        no_primary_default_reason, no_primary_default_reason_code = (
            _no_result_pairing_failure_reason(
                analysis_result,
                return_analysis,
                is_roundtrip,
            )
        )
        no_primary_diagnosis = build_no_result_diagnosis(
            no_primary_candidates,
            no_primary_excluded,
            merged_constraints or route_info,
            (
                analysis_result.get("filter_counts")
                or (analysis_result.get("round_trip_analysis") or {}).get("filter_counts")
                or {}
            ),
            fallback_reason=no_primary_default_reason,
        )
        candidate_price_summary = no_primary_diagnosis.get("price_summary") or {}
        no_primary_reason = no_primary_diagnosis.get("reason") or ""
        same_day_alternatives = _prepare_no_result_alternatives(
            same_day_alternatives,
            no_primary_candidates,
            no_primary_excluded,
            default_reason=no_primary_default_reason,
            default_reason_code=no_primary_default_reason_code,
        )
        single_leg_candidates = _no_result_candidate_flights(
            analysis_result,
            outbound_analysis,
            return_analysis,
            is_roundtrip,
            include_return=True,
        )
        single_leg_rejections = _build_single_leg_rejection_rows(
            single_leg_candidates,
            no_primary_excluded,
            default_reason=no_primary_default_reason,
            default_reason_code=no_primary_default_reason_code,
        )
    payload_push_type = (push_meta or {}).get("type") or "价格提醒"
    if not all_items:
        if data_incomplete:
            payload_push_type = "数据不完整"
            push_meta["reasons"] = [no_primary_reason]
        else:
            payload_push_type = "无符合方案·备选参考"
            push_meta["reasons"] = [
                "本次为'无符合方案'提醒,告知你当前约束下暂无匹配航班"
            ]
        display_price = None
        transaction_price = None
        budget_compare_price = None
        policy_compare_price = None
        verify_limit = None
        price_tiers = {}
        execution_advice = {}
        budget_gap = {}
        next_step_guidance = {}
        change = {}
        if data_incomplete:
            price_signal = {
                **(price_signal or {}),
                "label": "不可判断",
                "summary": "数据不完整,本轮不作价格位置判断",
                "percentile": None,
            }
            trend_summary = "数据不完整,本轮不作价格走势判断"
        price_policy = {
            "conclusion": (
                "数据不完整,本轮结论不可用"
                if data_incomplete
                else "未找到完全符合条件的方案"
            ),
            "reason": no_primary_reason,
        }
        purchase_budget_decision = {
            "status": "not_applicable",
            "price": None,
            "target_price": compare_target,
            "max_budget": compare_max_budget,
            "is_over_budget": False,
            "price_scope": budget_compare_scope,
            "budget_scope": budget_compare_scope,
            "reason": (
                "数据不完整,本轮结论不可用"
                if data_incomplete
                else "无推荐方案,不适用"
            ),
        }
    excluded_plans_payload = (
        ((analysis_result.get("round_trip_analysis") or {}).get("excluded_roundtrip_combos") or [])
        if is_roundtrip
        else (analysis_result.get("excluded_flights") or [])
    )
    if data_incomplete:
        excluded_plans_payload = []
    if is_roundtrip:
        excluded_plans_payload = _apply_passenger_pricing_to_excluded(
            list(excluded_plans_payload),
            passenger_pricing_breakdown,
            payload_route_type,
            display_price,
        )
    if not is_roundtrip or excluded_plans_payload:
        # 单腿拒因表只补“无完整往返组合”的缺口；已有组合继续走原排除卡。
        single_leg_rejections = []
    fallback_passenger_pricing = build_passenger_price_breakdown(
        0,
        passenger_pricing_breakdown,
        "economy",
        payload_route_type,
    )
    fallback_passenger_pricing["passenger_count"] = total_passengers
    fallback_passenger_pricing["applies"] = _passenger_pricing_applies(fallback_passenger_pricing)
    payload_passenger_pricing = primary_plan.get("passenger_pricing") or fallback_passenger_pricing
    if is_roundtrip:
        raw_price_references = (
            ((analysis_result.get("round_trip_analysis") or {}).get("price_analysis") or {}).get("references")
            or {}
        )
    else:
        reference_current = _to_float((analysis_result.get("price_range") or [None])[0])
        oneway_history = (
            analysis_result.get("constraint_price_history") or []
            if "constraint_price_history" in analysis_result
            else (price_insights or {}).get("price_history") or price_history or []
        )
        reference_history = _normalize_price_history_for_refs(
            oneway_history
        )
        raw_price_references = (
            calculate_price_references(
                reference_current,
                reference_history,
                _normalize_own_history_for_refs(route_info),
                analysis_result.get("days_to_dept") or 0,
                analysis_result.get("all_flights") or [],
            )
            if reference_current is not None
            else {}
        )
    price_references = {
        str(name): dict(reference)
        for name, reference in raw_price_references.items()
        if isinstance(reference, dict)
    }
    payload = {
        "push_type": payload_push_type,
        "route": _payload_route_text(route_info),
        "subscription_id": _first_nonempty_identity(
            route_info.get("subscription_id"),
            subscription.get("id"),
            subscription.get("_index"),
        ),
        "route_airports": _payload_route_airports(route_info),
        "origin_airports_active": route_info.get("origin_airports_active"),
        "destination_airports_active": route_info.get("destination_airports_active"),
        "origin_airports": route_info.get("origin_airports"),
        "destination_airports": route_info.get("destination_airports"),
        "route_info": {
            "origin_airports_active": route_info.get("origin_airports_active"),
            "destination_airports_active": route_info.get("destination_airports_active"),
            "origin_airports": route_info.get("origin_airports"),
            "destination_airports": route_info.get("destination_airports"),
        },
        "depart_date": route_info.get("depart_date"),
        "return_date": route_info.get("return_date"),
        "route_type": payload_route_type,
        "constraint_fingerprint": current_constraint_fingerprint,
        "constraint_fingerprint_short": short_constraint_fingerprint(
            current_constraint_fingerprint
        ),
        "constraint_change": constraint_change if constraint_change.get("changed") else {},
        "invoice_preferences": {
            "invoice_needed": bool((subscription.get("preferences") or {}).get("invoice_needed")),
            "invoice_special_vat": bool((subscription.get("preferences") or {}).get("invoice_special_vat")),
            "invoice_cabin_limit": bool((subscription.get("preferences") or {}).get("invoice_cabin_limit")),
            "cabin_policy": (
                ((subscription.get("constraints") or {}).get("cabin_policy"))
                or ((subscription.get("hard_constraints") or {}).get("cabin_policy"))
                or ""
            ),
        },
        "trip_type": "round_trip" if is_roundtrip else "one_way",
        "is_roundtrip": is_roundtrip,
        "current_price": display_price,
        "display_price": display_price,
        "transaction_price": transaction_price,
        "verify_price": verify_limit,
        "ideal_price": compare_target,
        "max_price": compare_max_budget,
        "budget_scope": budget_scope,
        "max_budget_scope": budget_scope,
        "target_price_scope": target_budget_scope,
        "budget_compare_scope": budget_compare_scope,
        "target_compare_scope": target_compare_scope,
        "budget_compare_price": budget_compare_price,
        "budget_input_ideal_price": budget_input_target,
        "budget_input_max_price": budget_input_max,
        "max_budget_pp_oneway": max_budget_pp_oneway,
        "target_price_pp_oneway": target_price_pp_oneway,
        "price_tiers": price_tiers,
        "dual_source_price_anomalies": [
            {"direction": "outbound", **item}
            for item in _source_price_anomaly_map(outbound_source_price_anomalies).values()
        ]
        + [
            {"direction": "return", **item}
            for item in _source_price_anomaly_map(return_source_price_anomalies).values()
        ],
        "last_push_price": (last_push or {}).get("price"),
        "recommendation": price_policy.get("conclusion") or decision.get("conclusion") or "可以观察",
        "price_policy_reason": price_policy.get("reason") or "",
        "price_signal": price_signal,
        "price_references": price_references,
        "execution_advice": execution_advice,
        "no_primary_diagnosis": no_primary_diagnosis.get("counts") or {},
        "no_primary_reason": no_primary_reason,
        "candidate_price_summary": candidate_price_summary,
        "single_leg_rejections": single_leg_rejections,
        "budget_gap": budget_gap,
        "purchase_budget_decision": purchase_budget_decision,
        "next_step_guidance": next_step_guidance,
        "confidence": confidence.get("overall") or decision.get("confidence") or "中",
        "confidence_dimensions": confidence.get("dimensions") or {},
        "confidence_details": confidence.get("details") or {},
        "travel_profile": travel_profile,
        "passenger_profile": passenger_profile,
        "passenger_rules": passenger_rules,
        "passenger_pricing": payload_passenger_pricing,
        "travel_profile_explanation": profile_explanation,
        "travel_scenarios": travel_profile.get("scenarios") or [],
        "recommendation_basis": recommendation_basis,
        "time_filter_note": time_filter_note,
        "scenario_recommendation": _scenario_recommendation_text(
            profile_explanation,
            travel_profile,
            recommendation_basis,
        ),
        "alert_policy": (
            ((analysis_result.get("round_trip_analysis") or {}).get("alert_policy"))
            or analysis_result.get("alert_policy")
            or {}
        ),
        "buy_condition": "无推荐方案,不适用" if not all_items else (
            (
                _budget_purchase_condition(verify_limit, budget_compare_scope)
                if price_tiers and verify_limit
                else ""
            )
            or execution_advice.get("condition")
            or (
                f"支付页最终价≤{_price_text(verify_limit)}，且含托运行李"
                if verify_limit
                else "以支付页最终价和票规为准"
            )
        ),
        "buy_condition_explanation": "" if not all_items else (
            (
                f"本次验证价{_price_text(verify_limit)}受你的最高可接受价{_price_text(compare_max_budget)}封顶，"
                f"当前搜索参考价{_price_text(budget_compare_price)}已超过该上限，不满足购买条件。"
                if compare_max_budget and verify_limit and budget_compare_price and verify_limit <= compare_max_budget and budget_compare_price > compare_max_budget
                else (
                    f"本次验证价{_price_text(verify_limit)} = 当前搜索参考价{_price_text(budget_compare_price)} "
                    f"+ 可接受浮动和费用容忍区间，用于判断该方案在当前价位是否仍值得买，"
                    f"与你的理想入手价{_price_text(compare_target)}是不同概念。"
                )
            )
            if verify_limit and budget_compare_price
            else ""
        ),
        "action_range": {} if not all_items else _payload_action_range(budget_compare_price, compare_target, compare_max_budget),
        "trigger_reason": (push_meta or {}).get("reasons") or (decision.get("reasons") or [])[:3],
        "recommended_plans": all_items[:2],
        "alternative_plans": all_items[2:5],
        "adjustment_required_plans": [plan for plan in all_items if _plan_feasibility_rank(plan) == 2],
        "excluded_plans": excluded_plans_payload,
        "buy_risk": risk.get("buy_risks") or ["可能遇到支付页跳价", "票规需确认（行李/退改）", "不同渠道售后政策不同"],
        "wait_risk": risk.get("wait_risks") or ["可能错过当前低价", "临近出发价格通常上涨", "理想价再次出现不确定"],
        "risk_summary": risk.get("summary") or "",
        "limits": _judgment_limit_items(route_info, analysis_result, price_insights, is_roundtrip, return_analysis),
        "price_history": _normalize_chart_history(history),
        "trend_summary": trend_summary,
        "trend_fallback": fallback_line,
        "checklist": _purchase_checklist_items(route_info, analysis_result, primary_plan, verify_limit) if all_items else [],
        "sorting_logic": _sorting_logic_items(route_info, is_roundtrip),
        "diff_from_last": {
            "last_price": (
                None
                if constraint_change.get("changed")
                else change.get("last") or (last_push or {}).get("price")
            ),
            "diff": change.get("diff") if all_items else None,
            "scope": change.get("scope"),
            "last_snapshot": last_snapshot or {},
            "comparable": not constraint_change.get("changed"),
        },
        "freshness_minutes": ((primary_flight or {}).get("availability") or {}).get("age_minutes"),
        "source_count": ((primary_flight or {}).get("availability") or {}).get("source_count"),
        "frequency": frequency,
        "nearby_date_prices": _payload_nearby_date_rows(route_info, analysis_result, is_roundtrip),
        "price_calendar": price_calendar_payload,
        "tcurve": route_info.get("tcurve") or {},
        "days_to_dept": analysis_result.get("days_to_dept"),
        "airport_cost_comparison": analysis_result.get("airport_cost_comparison") or [],
        "cabin_policy_summary": cabin_policy_summary,
        "same_day_no_feasible_note": (
            (analysis_result.get("round_trip_analysis") or {}).get("same_day_no_feasible_note")
            or analysis_result.get("same_day_no_feasible_note")
            or ""
        ),
        "same_day_alternatives": (
            same_day_alternatives
        ),
        "plan_status_change": plan_status_change,
        "plan_price_rows": _payload_plan_price_rows(all_items[:5]),
        "channel_price_rows": (all_items[0].get("channel_prices") if all_items else []),
        "detail_url": detail_url,
        "form_url": form_url,
        "feedback_url": feedback_url,
        "source_stats": source_stats or {},
        "source_errors": source_errors,
        "collection_failures": collection_failures,
        "source_degradation": (push_meta or {}).get("source_degradation") or {},
        "source_retirement": source_retirement_context,
        "data_freshness": route_info.get("data_freshness") or {},
        "collected_at": _message_collected_time(analysis_result, route_info),
        "snapshot": {
            "route": route_key,
            "subscription_id": subscription_snapshot_id,
            "depart_date": depart_key,
            "return_date": return_key,
            "channels": sorted(current_source_set) or _snapshot_channels(primary_flight),
            "source_set": sorted(current_source_set),
            "fare_status": _snapshot_fare_status(primary_flight),
            "constraint_fingerprint": current_constraint_fingerprint,
            "constraint_sample_n": int(price_signal.get("sample_n") or 0),
        },
    }
    if privacy_level != DEFAULT_NOTIFICATION_PRIVACY_LEVEL:
        payload["notification_privacy_level"] = privacy_level
    roundtrip_analysis = analysis_result.get("round_trip_analysis") or {}
    mixed_matching = roundtrip_analysis.get("mixed_cabin_matching") or {}
    if primary_plan.get("mixed_cabin") or mixed_matching:
        cabin_allocation = (
            primary_plan.get("cabin_allocation")
            or constraints_for_cabin.get("cabin_allocation")
            or {}
        )
        mixed_reference = _build_mixed_cabin_reference_price(
            primary_plan=primary_plan,
            mixed_matching=mixed_matching,
            cabin_summary=cabin_policy_summary,
            cabin_allocation=cabin_allocation,
            passengers=passenger_pricing_breakdown,
            route_type=payload_route_type,
        )
        reference_tree = mixed_reference.get("display_tree") or {}
        payload["mixed_cabin"] = {
            "cabin_allocation": cabin_allocation,
            "cabin_label": primary_plan.get("cabin_label") or reference_tree.get("cabin_label") or "",
            "matching": mixed_matching,
            "business_reference": mixed_matching.get("business_reference"),
            "business_visible_count": mixed_matching.get("business_visible_count", 0),
            "reference_price": mixed_reference,
            "disclosure": mixed_matching.get("disclosure") or MIXED_CABIN_DISCLOSURE,
            "provenance": {
                "business_source": "serpapi",
                "price_note": "SerpAPI展示价,税费构成未拆分,以支付页为准",
            },
        }
    print(f"[场景调试] 推送将显示 = {payload.get('travel_scenarios')}")
    return attach_payload_provenance(
        payload,
        context=route_info.get("provenance_context") or {},
    )


def _payload_price(value) -> str:
    return _price_text(value)


def _render_pushplus_legacy(payload: dict) -> str:
    """Legacy PushPlus renderer kept for compatibility; render_pushplus below is used."""
    payload = payload or {}
    lines = [
        f"<b>【{html.escape(str(payload.get('push_type') or '价格提醒'))}】{html.escape(str(payload.get('route') or '航班监控'))}</b>",
        "",
        f"当前价：{_payload_price(payload.get('current_price'))}{'（往返）' if payload.get('is_roundtrip') else ''}",
        f"建议：{html.escape(str(payload.get('recommendation') or '可以观察'))}",
        f"购买条件：{html.escape(str(payload.get('buy_condition') or '以支付页为准'))}",
        f"置信度：{html.escape(str(payload.get('confidence') or '中'))}",
        "",
        "<b>为什么提醒：</b>",
        "，".join(str(item) for item in (payload.get("trigger_reason") or [])[:3]) or "当前价格触发监控条件",
    ]
    risks = payload.get("buy_risk") or payload.get("limits") or []
    if risks:
        lines.extend(["", "<b>主要风险：</b>", "，".join(str(item) for item in risks[:2])])
    fallback = payload.get("trend_fallback")
    diff_from_last = payload.get("diff_from_last") or {}
    diff = _to_float(diff_from_last.get("diff"))
    scope_suffix = _price_change_scope_suffix(diff_from_last)
    if fallback:
        trend_text = f"近期：{fallback}"
        if diff is not None:
            trend_text += (
                f"（{'下降' if diff < 0 else '上涨' if diff > 0 else '持平'}"
                f"{_price_text(abs(diff)) if diff else ''}{scope_suffix}）"
            )
        lines.extend(["", trend_text])
    links = [
        f'<a href="{payload.get("detail_url", "")}" target="_blank">查看网页版完整分析(如未显示请稍后刷新)</a>',
        f'<a href="{payload.get("form_url", "")}" target="_blank">修改偏好</a>',
        f'<a href="{payload.get("feedback_url", "")}" target="_blank">反馈</a>',
    ]
    first_plan = (payload.get("recommended_plans") or [{}])[0]
    plan_links = first_plan.get("links") or {}
    if isinstance(plan_links, dict):
        first_link = plan_links.get("outbound") or plan_links.get("main")
        if first_link:
            links.insert(0, first_link)
    lines.extend(["", "下一步：" + " | ".join(link for link in links if link)])
    return "<br>".join(lines)


def _pushplus_duration_text(flight: dict) -> str:
    minutes = _to_float(flight.get("total_duration_min"))
    if minutes is None:
        hours = _to_float(flight.get("total_hours"))
        minutes = hours * 60 if hours is not None else None
    if minutes is None:
        return ""
    minutes = int(round(minutes))
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _pushplus_aircraft_text(flight: dict) -> str:
    segments = _email_plan_segments(flight)
    aircraft = ""
    if segments:
        aircraft = str(segments[0].get("aircraft") or "").strip()
    aircraft = get_aircraft_name(aircraft)
    if not aircraft or aircraft in {"未知", "请查询航司官网", "unknown", "Unknown"}:
        return ""
    return aircraft


def _airline_code_from_flight_no(value: str) -> str:
    match = re.match(r"\s*([A-Z0-9]{2})", str(value or "").replace(" ", "").upper())
    return match.group(1) if match else ""


def _flight_airline_name(flight: dict | None) -> str:
    flight = flight or {}
    segments = _email_plan_segments(flight)
    codes = []
    names = []
    for segment in segments:
        code = _airline_code_from_flight_no(segment.get("flight_no") or "")
        if code and code not in codes:
            codes.append(code)
        name = str(segment.get("airline") or "").strip()
        if name and name not in names:
            names.append(name)
    if not codes:
        code = _airline_code_from_flight_no(flight.get("flight_combo") or "")
        if code:
            codes.append(code)
    mapped = [AIRLINE_NAMES.get(code, code) for code in codes if code]
    display = mapped or names or [str(flight.get("airline_summary") or "").strip()]
    display = [item for item in display if item]
    return "+".join(dict.fromkeys(display)) if display else "航司待确认"


def _airport_short_label(code: str) -> str:
    code = str(code or "").strip().upper()
    if not code:
        return "机场待确认"
    name = AIRPORT_SHORT_DISPLAY.get(code)
    if not name:
        raw = get_airport_name(code)
        name = raw if raw and raw != code else code
    return f"{name}({code})"


def _airport_local_city(code: str) -> str:
    code = str(code or "").strip().upper()
    return AIRPORT_LOCAL_CITY.get(code) or get_airport_city(code) or code or "当地"


def _local_time_label(airport_code: str, time_value) -> str:
    time_text = _time_only(time_value) or "待确认"
    city = _airport_local_city(airport_code)
    return f"{time_text}({city}当地)"


def _pushplus_transfer_point(flight: dict) -> str:
    layovers = flight.get("layovers") or []
    if layovers:
        first = layovers[0] or {}
        airport = first.get("airport") or ""
        return airport or first.get("city") or ""
    segments = flight.get("segments") or []
    if len(segments) >= 2:
        return segments[0].get("arr_airport") or segments[0].get("arr_city") or ""
    return ""


def _lcc_segment_display_labels(flight: dict | None) -> list[str]:
    flight = flight or {}
    segments = [
        item
        for item in (flight.get("segments") or [])
        if isinstance(item, dict)
    ] or [flight]
    labels = []
    for segment in segments:
        result = classify_segment(segment)
        if not result.get("is_lcc"):
            continue
        marker = (
            "廉航(按市场承运)"
            if result.get("basis") == "marketing_fallback"
            else "廉航"
        )
        flight_no = str(
            segment.get("flight_no")
            or segment.get("flight_number")
            or segment.get("flightNo")
            or ""
        ).strip()
        label = f"{flight_no} {marker}".strip() if len(segments) > 1 else marker
        if label not in labels:
            labels.append(label)
    return labels


def _lcc_flight_display_suffix(flight: dict | None) -> str:
    labels = _lcc_segment_display_labels(flight)
    return " / ".join(labels)


def _flight_local_time_summary(flight: dict | None, label: str, compact: bool = False) -> str:
    flight = flight or {}
    segments = _email_plan_segments(flight)
    try:
        stops = int(flight.get("stops") if flight.get("stops") is not None else max(len(segments) - 1, 0))
    except (TypeError, ValueError):
        stops = max(len(segments) - 1, 0)
    first = segments[0] if segments else {}
    last = segments[-1] if segments else {}
    flight_numbers = _compact_flight_numbers(flight)
    airline_name = _flight_airline_name(flight)
    lcc_suffix = _lcc_flight_display_suffix(flight)
    if lcc_suffix:
        airline_name = f"{airline_name}｜{lcc_suffix}"
    aircraft = _pushplus_aircraft_text(flight)
    dep_airport = str(first.get("dep_airport") or "").strip().upper()
    arr_airport = str(last.get("arr_airport") or "").strip().upper()
    dep_label = f"{_airport_short_label(dep_airport)} {_local_time_label(dep_airport, first.get('dep_time'))}"
    arr_label = f"{_airport_short_label(arr_airport)} {_local_time_label(arr_airport, last.get('arr_time'))}"

    if stops <= 0:
        lines = [
            f"{label}:{flight_numbers}｜{airline_name}",
            f"{dep_label} → {arr_label}",
            "直飞" + (f"｜{aircraft}" if aircraft else ""),
        ]
    else:
        transfer = _pushplus_transfer_point(flight)
        transfer_label = _airport_short_label(transfer) if transfer else "中转地待确认"
        duration = _pushplus_duration_text(flight)
        lines = [
            f"{label}:{airline_name}",
            f"{dep_label} → 经{transfer_label}中转 → {arr_label}",
            f"中转{stops}次 {transfer or ''}".strip()
            + (f"｜总时长{duration}" if duration else ""),
        ]
    if compact:
        return " | ".join(lines)
    return "\n".join(lines)


def _pushplus_leg_summary(flight: dict | None, label: str) -> str:
    return _flight_local_time_summary(flight, label)


def _pushplus_plan_lines(payload: dict) -> list[str]:
    plans = payload.get("recommended_plans") or []
    if not plans:
        return []
    primary = plans[:1]
    if len(plans) > 1:
        second = plans[1] or {}
        if second.get("variant") in {"更稳", "全服务", "推荐"} or second.get("risk") == "A":
            primary.append(second)
    detail_lines: list[str] = []
    for index, plan in enumerate(primary[:2]):
        if plan.get("is_roundtrip"):
            current = [
                "━━ 去程 ━━",
                str(plan.get("outbound_push_line") or ""),
                f"去程票价:{_price_text(plan.get('outbound_price'))}",
                "━━ 返程 ━━",
                str(plan.get("return_push_line") or ""),
                f"返程票价:{_price_text(plan.get('return_price'))}",
                "━━ 合计 ━━",
                (
                    f"往返总价:{_price_text(plan.get('price'))}"
                    f"(去程{_price_text(plan.get('outbound_price'))} + "
                    f"返程{_price_text(plan.get('return_price'))})"
                ),
                f"购票方式:{plan.get('purchase_mode') or '待确认'}",
            ]
        else:
            current = [str(plan.get("main_push_line") or "")]
        feasibility = _pushplus_feasibility_summary(plan)
        if feasibility:
            current.append(feasibility)
        current = [line for line in current if line.strip()]
        if current:
            if detail_lines:
                detail_lines.append("")
            for item in current:
                detail_lines.extend(html.escape(line) for line in item.splitlines() if line.strip())
    if not detail_lines:
        return []
    return ["", "推荐方案:"] + detail_lines


def _pushplus_feasibility_summary(plan: dict) -> str:
    business_feasibility = _business_feasibility_text(plan)
    if business_feasibility:
        return "商务安全:" + business_feasibility
    feasibility = plan.get("feasibility") or {}
    if not isinstance(feasibility, dict) or not feasibility:
        return ""
    meeting_context = _plan_has_meeting_context(plan)
    parts = []
    for label, key in (("去程", "outbound"), ("返程", "return")):
        item = feasibility.get(key)
        if not isinstance(item, dict):
            continue
        level = item.get("level")
        if level == "不可行":
            parts.append(f"{label}不可行(差{item.get('short_min')}分钟,需{item.get('need_set_off')}前动身)")
        elif level:
            parts.append(f"{label}{level}({_humanize_margin(item.get('margin_min'), meeting_context)})")
    return "可行性:" + "；".join(parts) if parts else ""


def _reserve_ratio_text(value) -> str:
    ratio = _to_float(value)
    if ratio is None:
        return ""
    return f"{round(ratio * 100):g}%"


def _reserve_breakdown_part(item: dict | None, direction_label: str) -> str:
    item = item or {}
    if item.get("legacy"):
        total = item.get("total_min")
        buffer_h = item.get("buffer_hours")
        transport = item.get("transport_min")
        return f"{direction_label}预留{total}分钟(旧版设置{buffer_h}小时,车程约{transport}分钟)"
    if item.get("model") == "meeting_fixed":
        importance = item.get("importance_label") or "会议"
        custom = _to_float(item.get("custom_redundancy_min")) or 0
        custom_part = f"+自定义冗余{int(custom)}分钟" if custom > 0 else ""
        if direction_label == "去程" or item.get("arrival_exit_min") is not None:
            ratio = _reserve_ratio_text(item.get("destination_transport_margin_ratio"))
            rush = "+高峰上浮" if item.get("destination_transport_rush") else ""
            baggage = _to_float(item.get("checked_baggage_extra_min")) or 0
            baggage_part = f"(含托运行李+{int(baggage)}分钟)" if baggage > 0 else ""
            return (
                f"{direction_label}总预留≈{item.get('total_min')}分钟({importance})="
                f"落地离场{item.get('arrival_exit_min')}分钟{baggage_part}"
                f"+机场到会场{item.get('destination_transport_min')}分钟({item.get('destination_transport_source') or '估算'})"
                f"+路途冗余{item.get('destination_transport_margin_min')}分钟({ratio}{rush})"
                f"+延误冗余{item.get('delay_buffer_min')}分钟"
                f"+会前准备{item.get('pre_meeting_buffer_min')}分钟"
                f"{custom_part}"
            )
        ratio = _reserve_ratio_text(item.get("meeting_to_airport_margin_ratio"))
        rush = "+高峰上浮" if item.get("meeting_to_airport_rush") else ""
        return (
            f"{direction_label}总预留≈{item.get('total_min')}分钟({importance})="
            f"会后缓冲{item.get('post_meeting_buffer_min')}分钟"
            f"+会场到机场{item.get('meeting_to_airport_min')}分钟({item.get('meeting_to_airport_source') or '估算'})"
            f"+路途冗余{item.get('meeting_to_airport_margin_min')}分钟({ratio}{rush})"
            f"+机场提前量{item.get('departure_airport_process_min')}分钟"
            f"{custom_part}"
        )
    buffer_label = str(item.get("buffer_label") or "机场缓冲")
    airport = str(item.get("airport_iata") or "").strip()
    size = str(item.get("airport_size") or "").strip()
    airport_label = f"({airport}·{size})" if airport or size else ""
    ratio = _reserve_ratio_text(item.get("margin_ratio"))
    rush = "+高峰上浮" if item.get("rush_hour") else ""
    return (
        f"{direction_label}总预留≈{item.get('total_min')}分钟="
        f"{buffer_label}{item.get('airport_buffer_min')}分钟{airport_label}"
        f"+车程{item.get('transport_min')}分钟({item.get('transport_source') or '未知'})"
        f"+路途冗余{item.get('margin_min')}分钟({ratio}{rush})"
        f"+安全余量{item.get('safety_min')}分钟"
    )


def _business_feasibility_text(plan: dict | None) -> str:
    plan = plan or {}
    feasibility = plan.get("business_feasibility") or {}
    if not isinstance(feasibility, dict) or not feasibility:
        return ""

    def part(label: str, item: dict | None) -> str:
        item = item or {}
        level = item.get("level")
        if not level:
            return ""
        margin = _to_float(item.get("margin_min"))
        if margin is None:
            return f"{label}{level}"
        minutes = int(round(margin))
        if minutes >= 0:
            return f"{label}{level}(安全余量{minutes}分钟)"
        return f"{label}{level}(预计差{abs(minutes)}分钟)"

    pieces = [
        part("到会", feasibility.get("outbound")),
        part("返程", feasibility.get("return")),
    ]
    return "；".join(piece for piece in pieces if piece)

def _same_day_reserve_text(windows: dict | None) -> str:
    windows = windows or {}
    breakdown = windows.get("reserve_breakdown") or {}
    if not isinstance(breakdown, dict) or not breakdown:
        if windows.get("buffer_model") == "airport_split":
            breakdown = {
                "legacy": False,
                "outbound": {
                    "total_min": windows.get("outbound_reserve_minutes"),
                    "buffer_label": "到达机场缓冲",
                    "airport_buffer_min": windows.get("arrival_buffer_min"),
                    "transport_min": windows.get("transport_min"),
                    "transport_source": "未知",
                    "margin_min": windows.get("outbound_transport_margin_min"),
                    "margin_ratio": windows.get("outbound_transport_margin_ratio"),
                    "rush_hour": windows.get("outbound_transport_rush"),
                    "safety_min": windows.get("redundancy_min"),
                },
                "return": {
                    "total_min": windows.get("return_reserve_minutes"),
                    "buffer_label": "值机安检缓冲",
                    "airport_buffer_min": windows.get("checkin_buffer_min"),
                    "transport_min": windows.get("transport_min"),
                    "transport_source": "未知",
                    "margin_min": windows.get("return_transport_margin_min"),
                    "margin_ratio": windows.get("return_transport_margin_ratio"),
                    "rush_hour": windows.get("return_transport_rush"),
                    "safety_min": windows.get("redundancy_min"),
                },
            }
        else:
            breakdown = {
                "legacy": True,
                "outbound": {
                    "legacy": True,
                    "total_min": windows.get("reserve_minutes"),
                    "buffer_hours": windows.get("buffer_h"),
                    "transport_min": windows.get("transport_min"),
                },
                "return": {
                    "legacy": True,
                    "total_min": windows.get("reserve_minutes"),
                    "buffer_hours": windows.get("buffer_h"),
                    "transport_min": windows.get("transport_min"),
                },
            }
    outbound = _reserve_breakdown_part(breakdown.get("outbound"), "去程")
    ret = _reserve_breakdown_part(breakdown.get("return"), "返程")
    windows_info = breakdown.get("windows") if isinstance(breakdown.get("windows"), dict) else {}
    arrive_by = windows_info.get("arrive_by") or windows.get("outbound_arrive_by")
    depart_after = windows_info.get("depart_after") or windows.get("return_depart_after")
    legacy_note = "，按旧版设置展示" if breakdown.get("legacy") else ""
    return (
        f"办事{windows.get('business_start')}-{windows.get('business_end')}{legacy_note}，"
        f"{outbound} → 去程需{arrive_by}前到达；"
        f"{ret} → 返程{depart_after}后出发"
    )


def _plan_status_change_text(payload: dict) -> str:
    constraint_change = payload.get("constraint_change") or {}
    if constraint_change.get("changed"):
        return str(
            constraint_change.get("disclosure")
            or "筛选条件已变更，同条件样本重新积累"
        ).strip()
    status = payload.get("plan_status_change") or {}
    return str(status.get("msg") or "").strip() if isinstance(status, dict) else ""


def _alternative_flight_text(item: dict) -> str:
    item = item or {}
    flight = item.get("flight") or {}
    flight_no = html.escape(str(flight.get("flight_no") or flight.get("flight_combo") or "航班待确认"))
    dep = html.escape(str(flight.get("departure_time") or "--:--"))
    arr = html.escape(str(flight.get("arrival_time") or "--:--"))
    price = _price_text(item.get("price") or flight.get("price"))
    tradeoff = html.escape(str(item.get("tradeoff") or item.get("note") or ""))
    return f"{flight_no} {dep}-{arr} {price} {tradeoff}".strip()


def _has_primary_plans(payload: dict) -> bool:
    return bool(payload.get("recommended_plans") or [])


def _no_primary_plan_state(payload: dict) -> bool:
    return not _has_primary_plans(payload)


def _data_incomplete_state(payload: dict) -> bool:
    degradation = payload.get("source_degradation") or {}
    return bool(
        payload.get("push_type") == "数据不完整"
        or degradation.get("data_incomplete")
        or payload.get("collection_failures")
    )


def _data_incomplete_reason(payload: dict) -> str:
    degradation = payload.get("source_degradation") or {}
    reason = str(
        degradation.get("reason")
        or payload.get("no_primary_reason")
        or ""
    ).strip()
    return reason or "本轮采集失败,原因未完整记录"


def _private_data_incomplete_reason(payload: dict) -> str:
    """隐私通知仅披露失败方向，不携带 errno、路径或源响应细节。"""
    directions = []
    for item in payload.get("collection_failures") or []:
        if not isinstance(item, dict):
            continue
        direction = str(item.get("direction") or "").strip()
        if direction and direction not in directions:
            directions.append(direction)
    label = "、".join(directions)
    prefix = f"本轮{label}采集失败" if label else "本轮采集失败"
    return f"{prefix}，技术细节已隐藏"


def _build_mixed_cabin_reference_price(
    *,
    primary_plan: dict | None,
    mixed_matching: dict | None,
    cabin_summary: dict | None,
    cabin_allocation: dict | None,
    passengers: dict | None,
    route_type: str | None,
) -> dict:
    """对外保留 notifier 接口，实际金额逻辑集中在纯函数模块。"""
    from mixed_cabin_reference import build_mixed_cabin_reference_price

    return build_mixed_cabin_reference_price(
        primary_plan=primary_plan,
        mixed_matching=mixed_matching,
        cabin_summary=cabin_summary,
        cabin_allocation=cabin_allocation,
        passengers=passengers,
        route_type=route_type,
    )


def _mixed_cabin_unavailable_text(payload: dict) -> str:
    mixed = payload.get("mixed_cabin")
    if not isinstance(mixed, dict) or not mixed:
        return ""
    matching = mixed.get("matching")
    if not isinstance(matching, dict) or not matching:
        return ""
    stats = matching.get("stats") or {}
    if int(stats.get("candidates") or 0) != 0:
        return ""
    reason = str(
        matching.get("economy_candidate_reason")
        or "本轮未形成可配对的去返经济舱组合"
    ).strip()
    return f"经济舱候选为空(原因={reason}),混舱计价不可用"


def _no_primary_cause_key(text: str | None) -> str:
    text = str(text or "")
    if "【去程时间】" in text or "去程时间" in text or "outbound_time" in text:
        return "outbound_time"
    if "【返程时间】" in text or "返程时间" in text or "return_time" in text:
        return "return_time"
    if "【预算】" in text or "预算" in text or "budget" in text:
        return "budget"
    if "【时间窗口】" in text or "时间窗口" in text:
        return "time_window"
    return ""


def _no_primary_reason(payload: dict) -> str:
    diagnosis = payload.get("no_primary_diagnosis") or {}
    if not isinstance(diagnosis, dict):
        diagnosis = {}
    for value in (payload.get("no_primary_reason"), diagnosis.get("reason")):
        value = str(value or "").strip()
        if value:
            return value
    note = str(payload.get("same_day_no_feasible_note") or "").strip()
    if note:
        return note
    for key in ("risk_summary", "price_policy_reason"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    reasons = [str(item).strip() for item in (payload.get("trigger_reason") or []) if str(item or "").strip()]
    if reasons:
        return reasons[0]
    return "当前约束或数据条件下没有可推荐的可执行航班"


def _same_day_note_for_no_primary(payload: dict, headline: str | None = None) -> str:
    note = str(payload.get("same_day_no_feasible_note") or "").strip()
    if not note:
        return ""
    headline = str(headline or _no_primary_reason(payload) or "").strip()
    headline_cause = _no_primary_cause_key(headline)
    note_cause = _no_primary_cause_key(note)
    if headline_cause and note_cause and headline_cause != note_cause:
        print(
            "[无方案主因告警] 推送主因与当天往返提示不一致,已跳过当天往返提示: "
            f"headline={headline_cause}, same_day_note={note_cause}"
        )
        return ""
    if headline and note == headline:
        return ""
    return note

def _no_primary_max_bottleneck_text(payload: dict) -> str:
    diagnosis = payload.get("no_primary_diagnosis") or {}
    max_bottleneck = diagnosis.get("max_bottleneck") or {}
    if not max_bottleneck:
        return ""
    label = str(max_bottleneck.get("label") or "当前约束").strip()
    pool_scope = str(max_bottleneck.get("pool_scope") or "双向候选池").strip()
    scoped_label = f"{label}({pool_scope})"
    count = max_bottleneck.get("count")
    ratio = max_bottleneck.get("ratio")
    if count is None:
        return f"最大卡点:{scoped_label}"
    if ratio is not None:
        return f"最大卡点:{scoped_label}排除最多({count}个,占比{ratio}%)"
    return f"最大卡点:{scoped_label}排除最多({count}个)"


def _no_primary_next_step_text(payload: dict) -> str:
    rows = [
        row
        for row in (payload.get("single_leg_rejections") or [])
        if isinstance(row, dict)
    ]
    return_failure_rows = [
        row
        for row in rows
        if row.get("filter_reason_code") in {
            "return_collection_failed",
            "return_candidates_empty",
            "roundtrip_pairing_failed",
        }
    ]
    if return_failure_rows:
        direct_count = sum(
            1
            for row in return_failure_rows
            if int((_no_result_notification_flight(row) or {}).get("stops") or row.get("stops") or 0) == 0
        )
        label = f"{direct_count}个直飞去程" if direct_count else f"{len(return_failure_rows)}个去程候选"
        return (
            f"下一步:① 恢复返程采集后可重新评估{label} | "
            "② 换日期看低价日历 | ③ 继续等待完整往返报价"
        )

    lcc_rows = [row for row in rows if row.get("filter_reason_code") == "lcc_excluded"]
    if lcc_rows:
        direct_count = sum(
            1
            for row in lcc_rows
            if int((_no_result_notification_flight(row) or {}).get("stops") or row.get("stops") or 0) == 0
        )
        count_text = direct_count or len(lcc_rows)
        return (
            f"下一步:① 放宽廉航限制即可解锁{count_text}个直飞备选 | "
            "② 换日期看低价日历 | ③ 继续等待匹配航班"
        )

    refund_rows = [
        row
        for row in rows
        if row.get("filter_reason_code") == "refund_required"
        or "退改" in str(row.get("reason") or "")
    ]
    if refund_rows:
        direct_count = sum(
            1
            for row in refund_rows
            if int((_no_result_notification_flight(row) or {}).get("stops") or row.get("stops") or 0) == 0
        )
        count_text = direct_count or len(refund_rows)
        return (
            f"下一步:① 放宽退改即可解锁{count_text}个直飞备选 | "
            "② 换日期看低价日历 | ③ 继续等待匹配航班"
        )

    diagnosis = payload.get("no_primary_diagnosis") or {}
    reason_counts = diagnosis.get("reason_counts") or {}
    loosen = []
    if reason_counts.get("direct"):
        loosen.append("直飞")
    if reason_counts.get("meeting"):
        loosen.append("会议时间")
    if reason_counts.get("budget"):
        loosen.append("预算")
    loosen_text = "/".join(loosen) if loosen else "当前硬约束"
    return (
        "下一步:① 放宽条件("
        + loosen_text
        + ") | ② 换日期看低价日历 | ③ 继续等待匹配航班"
    )

def _candidate_summary_reason(summary: dict) -> str:
    raw = str(summary.get("reason") or "").strip()
    default_reason = '不满足当前约束'
    budget_reason = '超出预算'
    is_budget = str(summary.get("primary_cause") or "").lower() == "budget"
    lowest = _to_float(summary.get("lowest"))
    max_budget = _to_float(summary.get("max_budget"))
    if not raw:
        return default_reason
    if "?" in raw:
        if is_budget or (max_budget is not None and lowest is not None and lowest > max_budget):
            return budget_reason
        return default_reason
    return raw


def _candidate_price_summary_text(payload: dict) -> str:
    summary = payload.get("candidate_price_summary") or {}
    lowest = _to_float(summary.get("lowest"))
    count = int(summary.get("count") or 0)
    if lowest is None or count <= 0:
        return ""
    reason = _candidate_summary_reason(summary)
    scope = summary.get("price_scope") or summary.get("scope")
    max_budget = _to_float(summary.get("max_budget"))
    max_scope = summary.get("max_budget_scope") or scope
    if scope and max_scope:
        assert_same_caliber(scope, max_scope)
    if (
        str(summary.get("primary_cause") or "").lower() == "budget"
        or "预算" in reason
    ) and max_budget is not None:
        if lowest < max_budget:
            raise AssertionError(
                "预算主因口径矛盾: "
                f"候选最低{_price_text(lowest)}低于预算{_price_text(max_budget)}, "
                "但文案仍判定超预算"
            )
    if scope:
        passengers = summary.get("passengers") or payload.get("passengers") or payload.get("passenger_pricing_breakdown") or {}
        route_type = summary.get("route_type") or payload.get("route_type")
        price_text = _price_text_with_caliber(lowest, scope, passengers, route_type)
    else:
        price_text = _price_text(lowest)
    reason_text = reason if "不可购" in reason else f"{reason},不可购"
    return f"候选中最低{price_text}({reason_text})"


def _email_no_primary_candidate_reference_body(payload: dict) -> str:
    rows = []
    price_hint = _candidate_price_summary_text(payload)
    if price_hint:
        rows.append(("候选池最低", html.escape(price_hint)))

    candidate_summary = payload.get("candidate_price_summary") or {}
    fallback_scope = (
        candidate_summary.get("max_budget_scope")
        or candidate_summary.get("price_scope")
        or candidate_summary.get("scope")
    )
    target_scope = payload.get("target_compare_scope") or payload.get("budget_compare_scope") or fallback_scope
    budget_scope = payload.get("budget_compare_scope") or target_scope
    ideal_price = _to_float(payload.get("ideal_price"))
    max_price = _to_float(payload.get("max_price"))
    if ideal_price is not None:
        ideal_text = (
            _price_text_with_caliber(ideal_price, target_scope)
            if target_scope
            else _price_text(ideal_price)
        )
        rows.append(("理想入手价", html.escape(ideal_text)))
    if max_price is not None:
        max_text = (
            _price_text_with_caliber(max_price, budget_scope)
            if budget_scope
            else _price_text(max_price)
        )
        rows.append(("最高可接受价", html.escape(max_text)))

    body = _email_table(rows) if rows else "<div style='color:#888;font-size:12px;'>候选池暂无有效报价。</div>"
    return body + "<div style='margin-top:8px;color:#666;font-size:12px;'>候选价格仅用于了解当前价位；因时间窗口或其他约束不符，不可直接购买。</div>"


def _alternative_labels(alternatives: list[dict]) -> str:
    labels = []
    fallback = {
        "previous_evening": "前一晚到达",
        "previous_redeye": "前夜深夜班",
        "same_day_earliest": "当天最早班",
    }
    for item in alternatives[:3]:
        category = str((item or {}).get("category") or "").strip()
        title = str((item or {}).get("title") or "").strip()
        label = fallback.get(category)
        if not label and title:
            label = title
        if label:
            labels.append(label)
    return " / ".join(labels)


def _payload_depart_date(payload: dict) -> str:
    return str(
        payload.get("depart_date")
        or (payload.get("snapshot") or {}).get("depart_date")
        or ""
    )


def _same_day_alternative_date(item: dict, payload: dict) -> str:
    item = item or {}
    flight = item.get("flight") or {}
    for key in ("date", "date_str", "departure_date", "depart_date"):
        value = item.get(key) or flight.get(key)
        if value:
            return str(value)[:10]
    flight_date = _flight_search_date(flight, None)
    if flight_date:
        return flight_date[:10]
    base_date = _payload_depart_date(payload)
    category = str(item.get("category") or "")
    if base_date and category.startswith("previous"):
        try:
            return (datetime.fromisoformat(base_date[:10]) - timedelta(days=1)).strftime("%Y-%m-%d")
        except ValueError:
            return base_date
    return base_date


def _same_day_alternative_date_label(item: dict, payload: dict) -> str:
    date_str = _same_day_alternative_date(item, payload)
    if not date_str:
        return "日期待确认"
    category = str((item or {}).get("category") or "")
    base_date = _payload_depart_date(payload)
    previous = category.startswith("previous")
    if not previous and base_date:
        try:
            previous = datetime.fromisoformat(date_str[:10]).date() < datetime.fromisoformat(base_date[:10]).date()
        except ValueError:
            previous = False
    suffix = "(出发前一天)" if previous else ""
    return f"{date_str}{suffix}"


def _same_day_alternative_links(item: dict, payload: dict, limit: int = 6) -> str:
    item = item or {}
    flight = item.get("flight") or {}
    date_str = _same_day_alternative_date(item, payload)
    segments = _email_plan_segments(flight)
    first = segments[0] if segments else {}
    last = segments[-1] if segments else {}
    origin = (
        first.get("dep_airport")
        or _safe_flight_field(flight, "departure_airport", "dep_airport", "origin_airport", "origin")
    )
    dest = (
        last.get("arr_airport")
        or _safe_flight_field(flight, "arrival_airport", "arr_airport", "destination_airport", "destination")
    )
    route_info = {
        "depart_date": date_str,
        "origin": origin,
        "destination": dest,
    }
    links = _payload_booking_links_for_flight(flight, route_info, date_str, limit)
    if links:
        return links
    if origin and dest and date_str:
        return _compact_link_text(
            generate_booking_links(origin, dest, date_str, flight.get("flight_no") or flight.get("flight_combo") or ""),
            limit,
        )
    return ""


def _same_day_alternative_feasibility(item: dict) -> str:
    item = item or {}
    feasibility = item.get("feasibility")
    if isinstance(feasibility, dict):
        for key in ("summary", "label", "note", "message"):
            if feasibility.get(key):
                return str(feasibility.get(key))
        level = feasibility.get("level")
        if level:
            return str(level)
    if feasibility:
        return str(feasibility)
    return str(item.get("schedule_note") or item.get("note") or item.get("tradeoff") or "以实际行程安排为准")


def _same_day_has_roundtrip_legs(item: dict | None) -> bool:
    return isinstance(item, dict) and isinstance(item.get("outbound"), dict) and isinstance(item.get("return"), dict)


def _same_day_leg_date(item: dict, payload: dict, direction: str) -> str:
    item = item or {}
    flight = item.get(direction) or {}
    if direction == "outbound" and item.get("date"):
        return str(item.get("date"))[:10]
    for key in ("departure_date", "date", "date_str", "depart_date"):
        value = flight.get(key)
        if value:
            return str(value)[:10]
    if direction == "return":
        route_info = payload.get("route_info") or {}
        for value in (payload.get("return_date"), route_info.get("return_date")):
            if value:
                return str(value)[:10]
    return _same_day_alternative_date(item, payload)


def _same_day_leg_links(flight: dict, date_str: str, limit: int = 6) -> str:
    segments = _email_plan_segments(flight)
    first = segments[0] if segments else {}
    last = segments[-1] if segments else {}
    origin = (
        first.get("dep_airport")
        or _safe_flight_field(flight, "departure_airport", "dep_airport", "origin_airport", "origin")
    )
    dest = (
        last.get("arr_airport")
        or _safe_flight_field(flight, "arrival_airport", "arr_airport", "destination_airport", "destination")
    )
    route_info = {"depart_date": date_str, "origin": origin, "destination": dest}
    links = _payload_booking_links_for_flight(flight, route_info, date_str, limit)
    if links:
        return links
    if origin and dest and date_str:
        return _compact_link_text(
            generate_booking_links(origin, dest, date_str, flight.get("flight_no") or flight.get("flight_combo") or ""),
            limit,
        )
    return ""


def _same_day_leg_baggage_text(flight: dict | None) -> str:
    flight = flight or {}
    fare_rules = flight.get("fare_rules") if isinstance(flight.get("fare_rules"), dict) else {}
    baggage_rules = fare_rules.get("baggage") if isinstance(fare_rules.get("baggage"), dict) else {}
    return str(
        flight.get("baggage_line")
        or baggage_rules.get("note")
        or "以支付页为准"
    )



def _same_day_roundtrip_leg_rows(
    label: str,
    flight: dict,
    date_str: str,
    price,
    passengers=None,
    route_type=None,
) -> list[tuple[str, str]]:
    segments = _email_plan_segments(flight)
    first = segments[0] if segments else {}
    last = segments[-1] if segments else {}
    dep_airport = str(first.get("dep_airport") or "").strip().upper()
    arr_airport = str(last.get("arr_airport") or "").strip().upper()
    return [
        ("\u65e5\u671f", html.escape(str(date_str or "\u65e5\u671f\u5f85\u786e\u8ba4"))),
        ("\u822a\u73ed", _email_plan_flight_text(flight)),
        ("\u8d77\u98de", _email_plan_local_time(dep_airport, first.get("dep_time")) if segments else "\u65f6\u95f4\u5f85\u786e\u8ba4"),
        ("\u5230\u8fbe", _email_plan_local_time(arr_airport, last.get("arr_time")) if segments else "\u65f6\u95f4\u5f85\u786e\u8ba4"),
        ("\u4e2d\u8f6c", html.escape(_email_plan_transfer_text(flight))),
        ("\u673a\u578b", html.escape(_email_plan_aircraft_text(flight))),
        ("\u7968\u9762\u4ef7", _scoped_price_text_from_pp(price, passengers, "per_person_oneway", route_type)),
        ("\u884c\u674e", html.escape(_same_day_leg_baggage_text(flight))),
    ]


def _same_day_roundtrip_alternative_card(item: dict, payload: dict) -> str:
    outbound = item.get("outbound") or {}
    return_flight = item.get("return") or {}
    title = html.escape(str(item.get("title") or "\u5907\u9009\u65b9\u6848"))
    tradeoff = html.escape(str(item.get("tradeoff") or item.get("note") or "\u8bf7\u6839\u636e\u65f6\u95f4\u3001\u6210\u672c\u548c\u75b2\u52b3\u98ce\u9669\u81ea\u884c\u53d6\u820d"))
    outbound_date = _same_day_leg_date(item, payload, "outbound")
    return_date = _same_day_leg_date(item, payload, "return")
    outbound_price = item.get("outbound_price") if item.get("outbound_price") is not None else outbound.get("price")
    return_price = item.get("return_price") if item.get("return_price") is not None else return_flight.get("price")
    outbound_links = _same_day_leg_links(outbound, outbound_date, 6)
    return_links = _same_day_leg_links(return_flight, return_date, 6)
    passenger_pricing = item.get("passenger_pricing") or {}
    passengers, route_type = _same_day_price_context(item, payload)
    outbound_leg_text = _scoped_price_text_from_pp(outbound_price, passengers, "per_person_oneway", route_type)
    return_leg_text = _scoped_price_text_from_pp(return_price, passengers, "per_person_oneway", route_type)
    link_block = ""
    if outbound_links or return_links:
        link_lines = [
            "<div style='font-weight:600;margin-bottom:4px;'>\u9a8c\u8bc1\u6b64\u5907\u9009(\u4e24\u6bb5\u9700\u5206\u522b\u786e\u8ba4)</div>",
        ]
        if outbound_links:
            link_lines.append(f"<div>\u53bb\u7a0b {_email_plan_flight_text(outbound)}({outbound_leg_text}):{outbound_links}</div>")
        if return_links:
            link_lines.append(f"<div>\u8fd4\u7a0b {_email_plan_flight_text(return_flight)}({return_leg_text}):{return_links}</div>")
        link_lines.append("<div style='color:#b45309;margin-top:4px;'>\u6ce8:\u4e24\u6bb5\u662f\u72ec\u7acb\u673a\u7968,\u9700\u5206\u522b\u4e0b\u5355\u786e\u8ba4</div>")
        link_block = (
            "<div style='margin-top:10px;padding-top:8px;border-top:1px solid #f0f0f0;'>"
            + "".join(link_lines)
            + "</div>"
        )
    single_adult = item.get("single_adult_price") or item.get("adult_roundtrip_price")
    if single_adult is None and _has_valid_price(outbound_price) and _has_valid_price(return_price):
        single_adult = float(outbound_price) + float(return_price)
    if _passenger_pricing_applies(passenger_pricing):
        passenger_label = passenger_pricing.get("passenger_label") or _passenger_label_from_counts(
            passenger_pricing.get("passengers")
        )
        total_row_items = [
            (
                "\u5168\u5458\u5f80\u8fd4\u603b\u4ef7",
                f"{_scoped_price_text_from_legs(outbound_price, return_price, passengers, route_type, 'all_passengers_roundtrip')}({html.escape(str(passenger_label or '\u5168\u5458'))})",
            ),
            (
                "\u5355\u4eba\u5f80\u8fd4\u53c2\u8003",
                f"{_scoped_price_text_from_legs(outbound_price, return_price, passengers, route_type, 'per_person_roundtrip')}(\u53bb{outbound_leg_text} + \u8fd4{return_leg_text})",
            ),
        ]
    else:
        total_row_items = [
            (
                "\u5f80\u8fd4\u603b\u4ef7",
                f"{_scoped_price_text_from_legs(outbound_price, return_price, passengers, route_type, 'per_person_roundtrip')}(\u53bb{outbound_leg_text} + \u8fd4{return_leg_text})",
            ),
        ]
    if item.get("over_budget"):
        overage = item.get("budget_overage")
        scope_label = html.escape(str(item.get("budget_scope_label") or "\u9884\u7b97\u53e3\u5f84\u5f85\u786e\u8ba4"))
        total_row_items.append(
            (
                "\u9884\u7b97\u72b6\u6001",
                f"<span style='color:#b91c1c;font-weight:600;'>\u8d85\u51fa\u9884\u7b97 {_price_text(overage)}({scope_label})</span>",
            )
        )
    unmet_reason = str(
        item.get("unmet_reason")
        or item.get("feasibility")
        or item.get("tradeoff")
        or ""
    ).strip()
    if unmet_reason:
        total_row_items.append(("未达条件", html.escape(unmet_reason)))
    total_row_items.append(("\u53ef\u884c\u6027", html.escape(_same_day_alternative_feasibility(item))))
    total_rows = _email_leg_table(total_row_items)
    return (
        "<div style='border:1px solid #e5e7eb;border-radius:10px;padding:16px;margin:14px 0;background:#fff;'>"
        "<div style='font-weight:600;color:#d97706;margin-bottom:4px;'>"
        f"{title}</div>"
        f"<div style='font-size:13px;color:#666;margin-bottom:10px;'>\u6743\u8861:{tradeoff}</div>"
        "<div style='font-weight:600;margin:8px 0 4px;'>\u53bb\u7a0b</div>"
        f"{_email_leg_table(_same_day_roundtrip_leg_rows('\u53bb\u7a0b', outbound, outbound_date, outbound_price, passengers, route_type))}"
        "<div style='font-weight:600;margin:12px 0 4px;'>\u8fd4\u7a0b</div>"
        f"{_email_leg_table(_same_day_roundtrip_leg_rows('\u8fd4\u7a0b', return_flight, return_date, return_price, passengers, route_type))}"
        "<div style='font-weight:600;margin:12px 0 4px;'>\u5408\u8ba1</div>"
        f"{total_rows}"
        f"{link_block}"
        "</div>"
    )

def _same_day_alternative_card(item: dict, payload: dict) -> str:
    item = item or {}
    if _same_day_has_roundtrip_legs(item):
        return _same_day_roundtrip_alternative_card(item, payload)
    flight = item.get("flight") or {}
    title = html.escape(str(item.get("title") or "备选方案"))
    tradeoff = html.escape(str(item.get("tradeoff") or item.get("note") or "请根据时间、成本和疲劳风险自行取舍"))
    segments = _email_plan_segments(flight)
    first = segments[0] if segments else {}
    last = segments[-1] if segments else {}
    dep_airport = str(first.get("dep_airport") or "").strip().upper()
    arr_airport = str(last.get("arr_airport") or "").strip().upper()
    price = item.get("price") if item.get("price") is not None else flight.get("price")
    fare_rules = flight.get("fare_rules") if isinstance(flight.get("fare_rules"), dict) else {}
    baggage_rules = fare_rules.get("baggage") if isinstance(fare_rules.get("baggage"), dict) else {}
    baggage = (
        item.get("baggage")
        or flight.get("baggage_line")
        or baggage_rules.get("note")
        or "以支付页为准"
    )
    unmet_reason = str(
        item.get("unmet_reason")
        or item.get("feasibility")
        or item.get("tradeoff")
        or "不满足当前约束"
    ).strip()
    rows = [
        ("日期", html.escape(_same_day_alternative_date_label(item, payload))),
        ("航班", _email_plan_flight_text(flight)),
        ("起飞", _email_plan_local_time(dep_airport, first.get("dep_time")) if segments else "时间待确认"),
        ("到达", _email_plan_local_time(arr_airport, last.get("arr_time")) if segments else "时间待确认"),
        ("中转", html.escape(_email_plan_transfer_text(flight))),
        ("机型", html.escape(_email_plan_aircraft_text(flight))),
        ("票面价", f"{_price_text(price)}(单程)"),
        ("行李", html.escape(str(baggage))),
        ("未达条件", html.escape(unmet_reason)),
        ("可行性", html.escape(_same_day_alternative_feasibility(item))),
    ]
    links = _same_day_alternative_links(item, payload, 6)
    link_block = ""
    if links:
        link_block = (
            "<div style='margin-top:10px;padding-top:8px;border-top:1px solid #f0f0f0;'>"
            "验证购票:" + links + "</div>"
        )
    return (
        "<div style='border:1px solid #e5e7eb;border-radius:10px;padding:16px;margin:14px 0;background:#fff;'>"
        "<div style='font-weight:600;color:#d97706;margin-bottom:4px;'>"
        f"{title}</div>"
        f"<div style='font-size:13px;color:#666;margin-bottom:10px;'>权衡:{tradeoff}</div>"
        f"{_email_leg_table(rows)}"
        f"{link_block}"
        "</div>"
    )



def _pushplus_same_day_roundtrip_alternative_lines(item: dict, payload: dict) -> list[str]:
    title = html.escape(str(item.get("title") or "\u5907\u9009\u65b9\u6848"))
    outbound = item.get("outbound") or {}
    return_flight = item.get("return") or {}
    outbound_no = html.escape(str(outbound.get("flight_no") or outbound.get("flight_combo") or "\u53bb\u7a0b\u5f85\u786e\u8ba4"))
    return_no = html.escape(str(return_flight.get("flight_no") or return_flight.get("flight_combo") or "\u8fd4\u7a0b\u5f85\u786e\u8ba4"))
    outbound_dep = html.escape(_time_only(outbound.get("departure_time") or outbound.get("dep_time")) or "--:--")
    outbound_arr = html.escape(_time_only(outbound.get("arrival_time") or outbound.get("arr_time")) or "--:--")
    return_dep = html.escape(_time_only(return_flight.get("departure_time") or return_flight.get("dep_time")) or "--:--")
    return_arr = html.escape(_time_only(return_flight.get("arrival_time") or return_flight.get("arr_time")) or "--:--")
    outbound_links = _same_day_leg_links(outbound, _same_day_leg_date(item, payload, "outbound"), 3)
    return_links = _same_day_leg_links(return_flight, _same_day_leg_date(item, payload, "return"), 3)
    passengers, route_type = _same_day_price_context(item, payload)
    outbound_price = item.get("outbound_price") or outbound.get("price")
    return_price = item.get("return_price") or return_flight.get("price")
    lines = [
        title,
        f"\u53bb\u7a0b:{outbound_no} {outbound_dep}->{outbound_arr} {_scoped_price_text_from_pp(outbound_price, passengers, 'per_person_oneway', route_type)}",
        f"\u8fd4\u7a0b:{return_no} {return_dep}->{return_arr} {_scoped_price_text_from_pp(return_price, passengers, 'per_person_oneway', route_type)}",
        f"\u5f80\u8fd4\u603b\u4ef7:{_scoped_price_text_from_legs(outbound_price, return_price, passengers, route_type, 'all_passengers_roundtrip' if _passenger_pricing_applies(item.get('passenger_pricing')) else 'per_person_roundtrip')}",
    ]
    if item.get("over_budget"):
        scope_label = str(item.get("budget_scope_label") or "\u9884\u7b97\u53e3\u5f84\u5f85\u786e\u8ba4")
        lines.append(f"\u9884\u7b97\u72b6\u6001:\u8d85\u51fa\u9884\u7b97{_price_text(item.get('budget_overage'))}({html.escape(scope_label)})")
    unmet_reason = str(
        item.get("unmet_reason")
        or item.get("feasibility")
        or item.get("tradeoff")
        or ""
    ).strip()
    if unmet_reason:
        lines.append(f"未达条件:{html.escape(unmet_reason)}")
    if outbound_links:
        lines.append(f"\u53bb\u7a0b\u9a8c\u8bc1:{outbound_links}")
    if return_links:
        lines.append(f"\u8fd4\u7a0b\u9a8c\u8bc1:{return_links}")
    lines.append("\u6ce8:\u4e24\u6bb5\u72ec\u7acb\u7968,\u9700\u5206\u522b\u4e0b\u5355\u5e76\u5148\u786e\u8ba4\u4e24\u6bb5\u90fd\u6709\u7968")
    return [line for line in lines if str(line).strip()]

def _pushplus_same_day_alternative_lines(payload: dict) -> list[str]:
    alternatives = payload.get("same_day_alternatives") or []
    if not alternatives:
        return []
    lines = ["", "可选备选(由你决定):"]
    for item in alternatives[:3]:
        item = item or {}
        if _same_day_has_roundtrip_legs(item):
            lines.extend(_pushplus_same_day_roundtrip_alternative_lines(item, payload))
            continue
        title = html.escape(str(item.get("title") or "备选方案"))
        flight = item.get("flight") or {}
        flight_no = html.escape(str(flight.get("flight_no") or flight.get("flight_combo") or "航班待确认"))
        segments = _email_plan_segments(flight)
        first = segments[0] if segments else {}
        last = segments[-1] if segments else {}
        dep = html.escape(_time_only(first.get("dep_time") if segments else flight.get("departure_time")) or "--:--")
        arr = html.escape(_time_only(last.get("arr_time") if segments else flight.get("arrival_time")) or "--:--")
        dep_airport = html.escape(str(first.get("dep_airport") if segments else flight.get("departure_airport") or ""))
        arr_airport = html.escape(str(last.get("arr_airport") if segments else flight.get("arrival_airport") or ""))
        price = _price_text(item.get("price") or flight.get("price"))
        links = _same_day_alternative_links(item, payload, 6)
        lines.append(title)
        lines.append(f"航班:{flight_no}")
        time_airports = f"{dep_airport} " if dep_airport else ""
        time_airports += dep
        time_airports += " → "
        time_airports += f"{arr_airport} " if arr_airport else ""
        time_airports += arr
        lines.append(f"时间:{time_airports}")
        lines.append(f"价格:{price}")
        unmet_reason = str(
            item.get("unmet_reason")
            or item.get("feasibility")
            or item.get("tradeoff")
            or ""
        ).strip()
        if unmet_reason:
            lines.append(f"未达条件:{html.escape(unmet_reason)}")
        if links:
            lines.append(f"验证购票:{links}")
    return lines


def _pushplus_single_leg_rejection_lines(payload: dict) -> list[str]:
    rows = [
        row
        for row in (payload.get("single_leg_rejections") or [])
        if isinstance(row, dict)
    ]
    if not rows:
        return []
    lines = ["", "逐航班拒因:"]
    for item in rows[:3]:
        flight = _no_result_notification_flight(item)
        combo = normalize_combo(flight.get("flight_combo") or flight.get("flight_no") or "") or "航班待确认"
        direction = _single_leg_rejection_direction(item)
        price = _to_float(item.get("price") or flight.get("price"))
        price_text = _price_text(price) if price is not None else "价格待确认"
        details = _excluded_reason_details(item)
        reason = details[0] if details else str(item.get("reason") or "不满足当前约束")
        lines.append(f"{direction} {combo} {price_text}(单人单程):{reason}")
    return [html.escape(str(line)) for line in lines]

def _same_day_alternatives_body(payload: dict) -> str:
    alternatives = payload.get("same_day_alternatives") or []
    if not alternatives:
        return ""
    return (
        "<div style='margin-bottom:8px;color:#444;'>可选备选(三种取舍,由你决定):</div>"
        + "".join(_same_day_alternative_card(item, payload) for item in alternatives[:3])
    )


def _pushplus_next_step_lines(payload: dict) -> list[str]:
    guidance = _next_step_guidance(payload)
    items = guidance.get("items") or []
    if not items:
        return []
    lines = ["你可以:"]
    nums = ["①", "②", "③"]
    for index, item in enumerate(items[:3]):
        lines.append(
            f"{nums[index]} {item.get('label')} —— {item.get('summary')}"
        )
    if guidance.get("rigid"):
        lines.append("刚需提示:商务/会议等刚性行程可验证方案A;否则建议等待或换日期。")
    return [html.escape(str(line)) for line in lines]



def _pushplus_plan_brief_lines(payload: dict) -> list[str]:
    plan = _plan_for_render((payload.get("recommended_plans") or [{}])[0] or {}, payload)
    if not plan:
        return []
    gap = _budget_gap(payload)
    passengers, route_type = _plan_price_context(plan)
    gap_suffix = ""
    if gap.get("over_max"):
        scope = payload.get("budget_compare_scope") or ("per_person_roundtrip" if plan.get("is_roundtrip") else "per_person_oneway")
        gap_suffix = f"(\u5df2\u8d85\u9884\u7b97{_price_text_with_caliber(gap.get('over_max'), scope, passengers, route_type)})"
    lines = [f"{plan.get('label') or '??A'} | \u5f53\u524d\u6700\u4f18\u5019\u9009{gap_suffix}"]
    if plan.get("is_roundtrip"):
        outbound = str(plan.get("outbound_push_line") or "").strip()
        ret = str(plan.get("return_push_line") or "").strip()
        if outbound:
            lines.append(f"{outbound} {_plan_leg_price_text(plan, plan.get('outbound_price'))}")
        if ret:
            lines.append(f"{ret} {_plan_leg_price_text(plan, plan.get('return_price'))}")
        lines.append(
            f"\u5f80\u8fd4{_plan_roundtrip_price_text(plan)} | {plan.get('purchase_mode') or '\u8d2d\u7968\u65b9\u5f0f\u5f85\u786e\u8ba4'} | "
            f"{'\u76f4\u98de' if _plan_total_stops(plan) == 0 else '\u542b\u4e2d\u8f6c'}"
        )
    else:
        line = str(plan.get("main_push_line") or plan.get("summary") or "").strip()
        if line:
            lines.append(line)
        lines.append(f"\u4ef7\u683c:{_plan_leg_price_text(plan, plan.get('price'))}")
    if plan.get("tags"):
        lines.append(f"\u6807\u7b7e:{plan.get('tags')}")
    feasibility = _pushplus_feasibility_summary(plan)
    if feasibility:
        lines.append(feasibility)
    schedule_note = str(plan.get("schedule_note") or "").strip()
    if schedule_note:
        lines.append(f"\u5b89\u6392\u8bf4\u660e:{schedule_note}")
    baggage = str(plan.get("baggage_line") or "").strip()
    if baggage:
        lines.append(f"\u884c\u674e/\u9000\u6539:{baggage}")
    lcc_baggage_warning = _plan_lcc_baggage_warning(plan)
    if lcc_baggage_warning:
        lines.append(lcc_baggage_warning)
    lines.append("\u5b8c\u6574\u7968\u89c4/\u6210\u672c/\u53ef\u884c\u6027 \u2192 \u89c1\u7f51\u9875\u8be6\u60c5")
    return [html.escape(str(line)) for line in lines if str(line).strip()]


def _pushplus_calendar_summary_lines(payload: dict) -> list[str]:
    insight = _price_calendar_insight_text(payload)
    calendar = payload.get("price_calendar") or {}
    rows = [
        row
        for row in (calendar.get("rows") or [])
        if isinstance(row, dict) and _to_float(row.get("min_price")) is not None
    ]
    uncollected_rows = [
        row
        for row in (calendar.get("uncollected_rows") or [])
        if isinstance(row, dict) and row.get("date")
    ]
    if not insight and not rows and not uncollected_rows:
        return []
    is_roundtrip_scope, _unit = _calendar_scope_unit(calendar)
    primary_plan = ((payload.get("recommended_plans") or [{}]) or [{}])[0] or {}
    passenger_pricing = payload.get("passenger_pricing") or primary_plan.get("passenger_pricing") or {}
    passenger_calendar_applies = is_roundtrip_scope and _passenger_pricing_applies(passenger_pricing)
    passengers = _pricing_passengers(passenger_pricing)
    route_type = payload.get("route_type")
    row_scope = "all_passengers_roundtrip" if passenger_calendar_applies else ("per_person_roundtrip" if is_roundtrip_scope else "per_person_oneway")
    is_roundtrip_payload = bool(payload.get("is_roundtrip") or primary_plan.get("is_roundtrip"))
    if is_roundtrip_scope:
        return_date = str(calendar.get("return_date") or "").strip()
        return_short = return_date[5:10] if len(return_date) >= 10 else return_date
        suffix = f"(\u8fd4\u7a0b\u65e5\u56fa\u5b9a{return_short})" if return_short else ""
        lines = [f"\u5f80\u8fd4\u53c2\u8003\u4ef7\u6458\u8981{suffix}:"]
    elif is_roundtrip_payload:
        lines = ["\u5355\u7a0b\u4ef7\u683c\u8d8b\u52bf\u6458\u8981(\u4ec5\u4f9b\u53c2\u8003\u51fa\u53d1\u65e5\u9009\u62e9):"]
    else:
        lines = ["\u4f4e\u4ef7\u65e5\u5386\u6458\u8981:"]
    if insight:
        lines.append(insight)
    for row in sorted(rows, key=lambda item: _to_float(item.get("min_price")) or float("inf"))[:3]:
        date_text = f"{str(row.get('date') or '')[5:]} {row.get('weekday') or ''}".strip()
        if is_roundtrip_scope:
            outbound_price = _to_float(row.get("outbound_min_price"))
            return_price = _to_float(row.get("return_min_price") or calendar.get("return_min_price"))
            breakdown = ""
            if outbound_price is not None and return_price is not None:
                breakdown = (
                    f" (\u53bb{_price_text_with_caliber(outbound_price, 'per_person_oneway', passengers, route_type)}"
                    f"+\u8fd4{_price_text_with_caliber(return_price, 'per_person_oneway', passengers, route_type)})"
                )
            lines.append(f"{date_text} {_price_text_with_caliber(row.get('min_price'), row_scope, passengers, route_type)}{breakdown}")
        else:
            lines.append(f"{date_text} {_price_text_with_caliber(row.get('min_price'), 'per_person_oneway', passengers, route_type)}")
    if uncollected_rows:
        dates = "、".join(str(row.get("date"))[5:10] for row in uncollected_rows[:5])
        lines.append(f"弹性日期:{dates} 今日未采(未发起补采)")
    return [html.escape(str(line)) for line in lines if str(line).strip()]


def _privacy_price_band(payload: dict) -> str:
    price = _to_float(payload.get("display_price") or payload.get("current_price"))
    if price is None:
        return "金额暂缺"
    lower = int(price // 1000) * 1000
    upper = lower + 999
    return f"¥{lower:,.0f}-{upper:,.0f}"


def _render_private_pushplus(payload: dict) -> str | None:
    level = resolve_notification_privacy_level(payload)
    if level == DEFAULT_NOTIFICATION_PRIVACY_LEVEL:
        return None
    route = html.escape(str(payload.get("route") or "航班监控"))
    if level == "minimal":
        return f"<b>航班监控有变动</b><br>{route}"
    band = html.escape(_privacy_price_band(payload))
    return (
        f"<b>【航班监控·已脱敏】{route}</b><br>"
        f"价格区间:{band}<br>"
        "乘客构成与精确金额已隐藏，请在本地详情页查看完整信息"
    )


def _render_private_email(payload: dict) -> tuple[str, str] | None:
    level = resolve_notification_privacy_level(payload)
    if level == DEFAULT_NOTIFICATION_PRIVACY_LEVEL:
        return None
    route_text = str(payload.get("route") or "航班监控")
    route = html.escape(route_text)
    subject = f"【航班监控有变动】{route_text}"
    if level == "minimal":
        return subject, f"<b>航班监控有变动</b><br>{route}"
    band = html.escape(_privacy_price_band(payload))
    body = (
        f"<b>【航班监控·已脱敏】{route}</b><br>"
        f"价格区间:{band}<br>"
        "乘客构成与精确金额已隐藏，请在本地详情页查看完整信息"
    )
    return subject, body


def _push_section(
    section_id: str,
    priority: int,
    lines,
    *,
    mandatory: bool = False,
) -> PushSection:
    if isinstance(lines, str):
        section_html = lines
    else:
        section_html = "<br>".join(str(line) for line in lines)
    return PushSection(section_id, priority, section_html, mandatory)


def _pushplus_title(payload: dict, fallback: str = "价格提醒") -> str:
    push_type = str(payload.get("push_type") or fallback)
    route = str(payload.get("route") or "航班监控")
    return f"【{push_type}】{route}"


def _pushplus_detail_section(payload: dict) -> tuple[PushSection, str | None]:
    detail_url = valid_detail_url(payload.get("detail_url"))
    return (
        _push_section(
            "detail_link",
            0,
            detail_link_html(detail_url),
            mandatory=True,
        ),
        detail_url,
    )


def render_pushplus_sections(payload: dict) -> PushRender:
    """从统一 payload 直接构造 PushPlus 小节，不解析已渲染 HTML。"""
    payload = payload or {}
    private_render = _render_private_pushplus(payload)
    if private_render is not None and not _data_incomplete_state(payload):
        return PushRender(
            _pushplus_title(payload, "航班监控有变动"),
            (_push_section("privacy", 0, private_render, mandatory=True),),
            None,
        )

    feedback_ack = str(payload.get("feedback_ack") or "").strip()
    freshness_headline = _data_freshness_headline(payload)
    detail_section, detail_url = _pushplus_detail_section(payload)

    if _data_incomplete_state(payload):
        route = html.escape(str(payload.get("route") or "航班监控"))
        privacy_level = resolve_notification_privacy_level(payload)
        reason_text = (
            _data_incomplete_reason(payload)
            if privacy_level == DEFAULT_NOTIFICATION_PRIVACY_LEVEL
            else _private_data_incomplete_reason(payload)
        )
        sections = [
            _push_section(
                "header",
                0,
                f"<b>【数据不完整】{route}</b>",
                mandatory=True,
            ),
            _push_section(
                "current_judgment",
                0,
                [
                    html.escape(freshness_headline) if freshness_headline else "",
                    "当前判断:数据不完整,本轮结论不可用",
                    f"原因:{html.escape(reason_text)}",
                    "本轮不作航班可行性、市场无票或价格位置判断",
                ],
                mandatory=True,
            ),
            _push_section(
                "current_price",
                0,
                "当前价:本轮数据不完整,不作价格判断",
                mandatory=True,
            ),
            _push_section(
                "purchase_condition",
                0,
                "购买条件:本轮数据不完整,不提供购买判断",
                mandatory=True,
            ),
            _push_section(
                "primary_plan",
                0,
                "首选方案:本轮数据不完整,不提供方案",
                mandatory=True,
            ),
        ]
        if privacy_level == DEFAULT_NOTIFICATION_PRIVACY_LEVEL:
            sections.append(detail_section)
            form_url = valid_detail_url(payload.get("form_url"))
            if form_url:
                escaped = html.escape(form_url, quote=True)
                sections.append(
                    _push_section(
                        "settings_link",
                        3,
                        f'修改偏好:<a href="{escaped}" target="_blank">{escaped}</a>',
                    )
                )
        sections.extend(
            [
                _push_section(
                    "data_freshness",
                    0,
                    _pushplus_freshness_line(payload),
                    mandatory=True,
                ),
                _push_section(
                    "disclaimer",
                    0,
                    "提示:本轮数据不完整,结论不代表市场无票；最终状态以下单页为准",
                    mandatory=True,
                ),
            ]
        )
        return PushRender(
            f"【数据不完整】{str(payload.get('route') or '航班监控')}",
            tuple(sections),
            detail_url,
        )

    alternatives = payload.get("same_day_alternatives") or []
    if _no_primary_plan_state(payload):
        route = html.escape(str(payload.get("route") or "航班监控"))
        reason_text = _no_primary_reason(payload)
        mixed_notice = _mixed_cabin_unavailable_text(payload)
        max_line = _no_primary_max_bottleneck_text(payload)
        alt_labels = _alternative_labels(alternatives)
        alt_text = f"{len(alternatives[:3])}个"
        if alt_labels:
            alt_text += f"({html.escape(alt_labels)})"
        else:
            alt_text = "暂无可展示备选"
        price_hint = _candidate_price_summary_text(payload)
        judgment_lines = [
            "当前判断:❌ 未找到完全符合条件的方案",
            f"主因:{html.escape(reason_text)}",
            f"分舱报价:{html.escape(mixed_notice)}" if mixed_notice else "",
            html.escape(max_line) if max_line else "",
        ]
        if feedback_ack:
            judgment_lines.insert(0, html.escape(feedback_ack))
        same_day_note = _same_day_note_for_no_primary(payload, reason_text)
        risk_lines = []
        if same_day_note:
            risk_lines.append("当天往返提示:" + html.escape(same_day_note))
        time_filter_note = str(payload.get("time_filter_note") or "").strip()
        if time_filter_note:
            risk_lines.append(html.escape(time_filter_note))
        alternative_lines = [
            f"可用备选:{alt_text}",
            f"【可选备选】{alt_text}",
            f"【放宽预演】{html.escape(_no_primary_next_step_text(payload))}",
            *_pushplus_same_day_alternative_lines(payload),
        ]
        sections = [
            _push_section(
                "header",
                0,
                f"<b>【无符合方案】{route} 提供{len(alternatives[:3])}个备选</b>",
                mandatory=True,
            ),
            _push_section(
                "current_judgment",
                0,
                judgment_lines,
                mandatory=True,
            ),
            _push_section(
                "current_price",
                0,
                f"价格:{html.escape(price_hint)}" if price_hint else "价格:暂无可订组合价",
                mandatory=True,
            ),
            _push_section(
                "purchase_condition",
                0,
                "购买条件:当前没有完全符合条件的可订方案",
                mandatory=True,
            ),
            _push_section(
                "primary_plan",
                0,
                "首选方案:暂无完全符合方案",
                mandatory=True,
            ),
            _push_section("main_risk", 1, risk_lines),
            _push_section("alternative_summary", 2, alternative_lines),
            _push_section(
                "excluded_plans",
                3,
                _pushplus_single_leg_rejection_lines(payload),
            ),
            detail_section,
            _push_section(
                "data_freshness",
                0,
                [
                    html.escape(freshness_headline) if freshness_headline else "",
                    _pushplus_freshness_line(payload),
                ],
                mandatory=True,
            ),
            _push_section(
                "disclaimer",
                0,
                "提示:备选方案为取舍参考,最终价、库存、行李和票规以下单页为准",
                mandatory=True,
            ),
        ]
        form_url = valid_detail_url(payload.get("form_url"))
        if form_url:
            escaped = html.escape(form_url, quote=True)
            sections.insert(
                -2,
                _push_section(
                    "settings_link",
                    3,
                    f'修改偏好:<a href="{escaped}" target="_blank">{escaped}</a>',
                ),
            )
        return PushRender(
            f"【无符合方案】{str(payload.get('route') or '航班监控')}",
            tuple(sections),
            detail_url,
        )

    raw_push_type = str(payload.get("push_type") or "价格提醒")
    raw_route = str(payload.get("route") or "航班监控")
    push_type = html.escape(raw_push_type)
    route = html.escape(raw_route)
    recommendation = html.escape(str(payload.get("recommendation") or "可以观察"))
    buy_condition = html.escape(
        str(payload.get("buy_condition") or "以支付页最终价和票规为准")
    )
    primary_plan = (payload.get("recommended_plans") or [{}])[0] or {}
    gap_line = _budget_gap_line(payload)

    reasons = [str(item) for item in (payload.get("trigger_reason") or []) if item]
    diff_from_last = payload.get("diff_from_last") or {}
    diff = _to_float(diff_from_last.get("diff"))
    if diff is not None and diff != 0:
        reasons.append(
            f"比上次{'降' if diff < 0 else '涨'}{_price_text(abs(diff))}"
            f"{_price_change_scope_suffix(diff_from_last)}"
        )
    current = _to_float(payload.get("current_price"))
    ideal = _to_float(payload.get("ideal_price"))
    if current is not None and ideal and current <= ideal * 1.05:
        reasons.append(f"接近理想价{_price_text(ideal)}")
    reason_text = "，".join(dict.fromkeys(reasons[:2])) or "当前价格触发监控条件"
    reason_line = _budget_reason_line(payload, reason_text)
    trigger_evidence = _cheaper_date_trigger_evidence(payload)

    reminder_lines = []
    if feedback_ack:
        reminder_lines.append(html.escape(feedback_ack))
    if trigger_evidence:
        reminder_lines.append(f"触发依据:{html.escape(trigger_evidence)}")
    if gap_line:
        reminder_lines.append(html.escape(gap_line))
    reminder_lines.append(f"触发原因:{html.escape(reason_text)}")

    primary_lines = ["方案简卡:", *_pushplus_plan_brief_lines(payload)]
    risk_lines = []
    same_day_note = _same_day_note_for_no_primary(payload, reason_text)
    if same_day_note:
        risk_lines.append("当天往返提示:" + html.escape(same_day_note))
        risk_lines.extend(_pushplus_same_day_alternative_lines(payload))
    if payload.get("time_filter_note"):
        risk_lines.append(html.escape(str(payload.get("time_filter_note"))))

    calendar_lines = _pushplus_calendar_summary_lines(payload)
    source_channel_lines = _pushplus_source_channel_price_lines(payload)
    channel_payload = dict(payload)
    channel_payload["detail_url"] = detail_url
    channel_lines = _pushplus_channel_section(channel_payload, primary_plan)
    sections = [
        _push_section(
            "header",
            0,
            f"<b>【{push_type}】{route}</b>",
            mandatory=True,
        ),
        _push_section(
            "current_judgment",
            0,
            [
                html.escape(freshness_headline) if freshness_headline else "",
                "",
                f"当前判断:{recommendation}",
            ],
            mandatory=True,
        ),
        _push_section("current_price", 0, reason_line, mandatory=True),
        _push_section(
            "primary_plan_head",
            0,
            f"首选候选:{html.escape(str(primary_plan.get('label') or '方案A'))}, "
            f"{_price_text(primary_plan.get('price') or payload.get('display_price'))}",
            mandatory=True,
        ),
        _push_section("reminder_reason", 1, reminder_lines),
        _push_section(
            "purchase_condition",
            0,
            f"购买条件:{buy_condition}",
            mandatory=True,
        ),
        _push_section("next_steps", 1, ["", *_pushplus_next_step_lines(payload), ""]),
        _push_section("primary_plan", 0, primary_lines, mandatory=True),
        _push_section("main_risk", 1, risk_lines),
        _push_section("calendar", 3, ["", *calendar_lines] if calendar_lines else []),
        _push_section("source_price_details", 3, ["", *source_channel_lines] if source_channel_lines else []),
        _push_section("technical_links", 3, ["", *channel_lines] if channel_lines else []),
        _push_section(
            "data_freshness",
            0,
            ["", _pushplus_freshness_line(payload)],
            mandatory=True,
        ),
        detail_section,
        _push_section(
            "disclaimer",
            0,
            "提示:最终价、库存、行李、退改签和机型以下单页为准；价格以各平台支付页为准",
            mandatory=True,
        ),
    ]
    return PushRender(
        f"【{raw_push_type}】{raw_route}",
        tuple(sections),
        detail_url,
    )


def render_pushplus(payload: dict) -> str:
    """从 payload 直构小节并按完整小节拼接 PushPlus 正文。"""
    return render_push_render(render_pushplus_sections(payload))


def _plan_route_type(plan: dict) -> str:
    route_type = str((plan or {}).get("route_type") or "").lower()
    if route_type:
        return route_type
    for flight in _plan_flights(plan or {}):
        route_type = str(flight.get("route_type") or "").lower()
        if route_type:
            return route_type
    return ""


def _transit_airports_for_flight(flight: dict | None) -> list[str]:
    flight = flight or {}
    airports: list[str] = []
    for layover in flight.get("layovers") or []:
        code = str((layover or {}).get("airport") or "").strip().upper()
        if code and code not in airports:
            airports.append(code)
    segments = _email_plan_segments(flight)
    for segment in segments[:-1]:
        code = str(segment.get("arr_airport") or "").strip().upper()
        if code and code not in airports:
            airports.append(code)
    return airports


def _international_transit_visa_line(plan: dict) -> str:
    warnings = []
    for flight in _plan_flights(plan):
        for airport in _transit_airports_for_flight(flight):
            info = TRANSIT_VISA_RISK_AIRPORTS.get(airport)
            if not info:
                continue
            country, note = info
            city = _airport_short_label(airport)
            text = f"该方案经{city}中转（{country}），部分国籍可能需过境签或入境许可；{note}，请自行核实。"
            if text not in warnings:
                warnings.append(text)
    if not warnings:
        return ""
    return '<span style="color:#b91c1c;">⚠️ ' + html.escape(" / ".join(warnings[:2])) + "</span>"


def _parse_datetime_loose(value):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text[:16], fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "").split("+")[0])
    except ValueError:
        return None


def _timezone_difference_line_for_flight(flight: dict | None) -> str:
    segments = _email_plan_segments(flight)
    if not segments:
        return ""
    dep_airport = str(segments[0].get("dep_airport") or "").strip().upper()
    arr_airport = str(segments[-1].get("arr_airport") or "").strip().upper()
    dep_offset = AIRPORT_UTC_OFFSETS.get(dep_airport)
    arr_offset = AIRPORT_UTC_OFFSETS.get(arr_airport)
    parts = []
    if dep_offset is not None and arr_offset is not None and dep_offset != arr_offset:
        diff = arr_offset - dep_offset
        sign = "+" if diff > 0 else ""
        parts.append(f"{_airport_short_label(arr_airport)}相对{_airport_short_label(dep_airport)}{sign}{diff}h时差")
    dep_dt = _parse_datetime_loose(segments[0].get("dep_time"))
    arr_dt = _parse_datetime_loose(segments[-1].get("arr_time"))
    if dep_dt and arr_dt:
        day_diff = (arr_dt.date() - dep_dt.date()).days
        if day_diff > 0:
            parts.append(f"到达日期+{day_diff}天")
        elif day_diff < 0:
            parts.append(f"到达日期{day_diff}天")
    duration = _email_plan_duration_text(flight)
    if duration:
        parts.append(f"实际飞行/行程约{duration}")
    return "；".join(parts)


def _timezone_difference_line(plan: dict) -> str:
    lines = []
    for flight in _plan_flights(plan):
        line = _timezone_difference_line_for_flight(flight)
        if line and line not in lines:
            lines.append(line)
    if not lines:
        return ""
    return html.escape(" / ".join(lines[:2]))


def _interline_purchase_line(plan: dict, route_type: str) -> str:
    purchase_mode = str(plan.get("purchase_mode") or "").strip()
    if route_type != "international" and "单程" not in purchase_mode and "非联程" not in purchase_mode:
        return ""
    if "非联程" in purchase_mode or "单程" in purchase_mode:
        return '<span style="color:#b91c1c;">非联程（两张票分别购买，需自行转机提行李，误机风险自担）</span>'
    if purchase_mode:
        return html.escape(f"{purchase_mode}（一张票，通常可行李直挂，误机保障以票规为准）")
    return ""


def _route_specific_plan_rows(plan: dict) -> list[tuple[str, str]]:
    route_type = _plan_route_type(plan)
    rows: list[tuple[str, str]] = []
    if route_type == "international":
        transit_line = _international_transit_visa_line(plan)
        if transit_line:
            rows.append(("过境签提示", transit_line))
        timezone_line = _timezone_difference_line(plan)
        if timezone_line:
            rows.append(("时区/时差", timezone_line))
        interline_line = _interline_purchase_line(plan, route_type)
        if interline_line:
            rows.append(("联程/非联程", interline_line))
        rows.append(("国际票务提示", "跨境支付、退改更复杂，建议优先信誉好的渠道，保留行程单和支付凭证。"))
    elif route_type == "greater_china":
        rows.append(("证件提示", "港澳台往返需港澳通行证/台湾通行证及有效签注，请确认证件。"))
        rows.append(("渠道提示", "港澳台航线国内OTA基本都能买，建议用航司官网、携程、飞猪、去哪儿交叉验证。"))
        timezone_line = _timezone_difference_line(plan)
        if timezone_line:
            rows.append(("时区提示", timezone_line))
    invoice_line = _invoice_reimbursement_line(plan)
    if invoice_line:
        rows.append(("开票/报销", invoice_line))
    return rows


def _split_ticket_leg_verification_html(plan: dict, direction: str) -> str:
    flight = plan.get(f"{direction}_flight") or {}
    links = (plan.get("links") or {}).get(direction) or ""
    label = _pushplus_plan_flight_label(plan, direction)
    price = _plan_leg_price_text(plan, plan.get(f"{direction}_price") or flight.get("price"))
    availability = _status_availability_label(flight) or "需支付页确认"
    baggage = _pushplus_baggage_line_for_flight(flight)
    refund = _verification_refund_line_for_flight(flight)
    channel_links = _layered_channel_links(links) or links or "请到支付页确认"
    price_label = "成人单段参考价" if _passenger_pricing_applies(plan.get("passenger_pricing")) else "票面价"
    rows = [
        f"<div style='font-weight:600;color:#111;margin:8px 0 4px;'>{html.escape(label)}</div>",
        f"<div>{price_label}:{price}</div>",
        f"<div>库存:{html.escape(availability)}</div>",
        f"<div>{html.escape(baggage)}</div>",
        f"<div>{html.escape(refund)}</div>",
        f"<div>验证渠道:{channel_links}</div>",
    ]
    return "".join(rows)


def _split_ticket_verification_html(plan: dict) -> str:
    parts = [
        "<div style='font-weight:600;color:#111;margin-bottom:6px;'>━━ 验证此方案(两段需分别购买) ━━</div>",
    ]
    for direction in ("outbound", "return"):
        if (plan.get("links") or {}).get(direction) or plan.get(f"{direction}_flight"):
            parts.append(_split_ticket_leg_verification_html(plan, direction))
    if plan.get("price"):
        parts.append(
            f"<div style='margin-top:8px;font-weight:600;'>往返合计参考:{_plan_roundtrip_price_text(plan)}(两段分别购买)</div>"
        )
    passenger_pricing = plan.get("passenger_pricing") or {}
    if _passenger_pricing_applies(passenger_pricing):
        label = passenger_pricing.get("passenger_label") or _passenger_label_from_counts(passenger_pricing.get("passengers"))
        parts.append(
            "<div style='margin-top:6px;color:#666;font-size:12px;'>"
            f"价格已按{html.escape(label)}估算；打开渠道后请按实际乘客人数重新搜索并确认儿童/婴儿票价。"
            "</div>"
        )
    parts.append(
        "<div style='margin-top:6px;color:#b91c1c;font-size:12px;'>"
        "⚠ 提示:两段是独立机票,需分别下单;建议先确认两段都有票再购买,避免只买到一段。"
        "</div>"
    )
    return "".join(parts)


def _invoice_reimbursement_line(plan: dict) -> str:
    prefs = (plan or {}).get("invoice_preferences") or {}
    if not any(prefs.get(key) for key in ("invoice_needed", "invoice_special_vat", "invoice_cabin_limit")):
        return ""
    parts = []
    if prefs.get("invoice_needed"):
        parts.append("开票:航司官网/携程通常可开行程单和发票；部分OTA特价票开票可能受限，报销前请确认。")
    if prefs.get("invoice_special_vat"):
        parts.append("专票:优先航司官网或企业差旅渠道，具体开票主体和税票类型以渠道为准。")
    if prefs.get("invoice_cabin_limit"):
        cabin = str(prefs.get("cabin_policy") or "未填写").strip()
        parts.append(f"舱位限制:已标记有报销舱位限制（当前政策:{cabin}），请核对经济舱/商务舱是否符合公司标准。")
    return html.escape(" ".join(parts))


def _render_payload_plan_card(plan: dict, compact: bool = False, primary_plan: dict | None = None) -> str:
    display_tree = _display_price_tree_for_item(plan)
    _log_card_price_consistency(
        plan,
        str(plan.get("label") or "方案"),
        displayed_total=display_tree.get("total") if display_tree else plan.get("price"),
    )
    if _plan_is_domestic(plan):
        return _render_domestic_payload_plan_card(plan, compact=compact, primary_plan=primary_plan)
    label = str(plan.get("label", "方案"))
    tier = str(plan.get("tier") or plan.get("variant") or "").split(":", 1)[0].strip()
    if tier == "推荐":
        tier = "首选推荐"
    elif tier == "备选":
        tier = "备选方案"
    badge = _plan_tier_badge(plan, tier)
    title = html.escape(f"{label} ｜ {tier} ｜ {badge}".strip(" ｜"))
    body_parts: list[str] = [_plan_tradeoff_summary_html(plan, primary_plan)]
    rows = []
    if plan.get("is_roundtrip"):
        body_parts.append(
            _email_plan_leg_group("━━ 去程 ━━", plan.get("outbound_flight"), str(plan.get("outbound_line") or ""))
        )
        body_parts.append(
            _email_plan_price_group([("去程票价", _plan_leg_price_text(plan, plan.get("outbound_price")))])
        )
        body_parts.append(
            _email_plan_leg_group("━━ 返程 ━━", plan.get("return_flight"), str(plan.get("return_line") or ""))
        )
        body_parts.append(
            _email_plan_price_group([("返程票价", _plan_leg_price_text(plan, plan.get("return_price")))])
        )
        outbound_price_text = _plan_leg_price_text(plan, plan.get("outbound_price"))
        return_price_text = _plan_leg_price_text(plan, plan.get("return_price"))
        body_parts.append(
            '<div style="font-weight:600;color:#111;margin:12px 0 6px;'
            'background:#f5f7fa;padding:4px 8px;border-radius:4px;">━━ 合计 ━━</div>'
        )
        rows.extend(
            _passenger_pricing_rows(plan)
            or [
                (
                    "往返总价",
                    f"{_plan_roundtrip_price_text(plan)}(去程{outbound_price_text} + 返程{return_price_text})",
                )
            ]
        )
        if _passenger_pricing_applies(plan.get("passenger_pricing")):
            rows.append(("单人单段参考", f"去程{outbound_price_text} + 返程{return_price_text}"))
        rows.extend(
            [
                ("预估实付价", _price_text_with_caliber(plan.get("estimated_price"), "all_passengers_roundtrip" if _passenger_pricing_applies(plan.get("passenger_pricing")) and plan.get("is_roundtrip") else ("all_passengers_oneway" if _passenger_pricing_applies(plan.get("passenger_pricing")) else ("per_person_roundtrip" if plan.get("is_roundtrip") else "per_person_oneway")), *_plan_price_context(plan))),
                ("购票方式", html.escape(str(plan.get("purchase_mode") or "待确认"))),
                ("行李状态", f'<span style="color:#d97706;">{html.escape(str(plan.get("baggage_line") or "支付页需确认"))}</span>'),
            ]
        )
        if plan.get("purchase_note"):
            rows.append(("说明", html.escape(str(plan.get("purchase_note")))))
        if plan.get("same_day_round_trip"):
            stay = _to_float(plan.get("stay_hours"))
            stay_text = f"约{stay:g}小时（可办事）" if stay is not None else "当天往返可行"
            rows.append(("停留", html.escape(stay_text)))
            windows = plan.get("same_day_windows") or {}
            if windows:
                rows.append(
                    (
                        "时间反推",
                        html.escape(_same_day_reserve_text(windows)),
                    )
                )
            business_feasibility = _business_feasibility_text(plan)
            if business_feasibility:
                rows.append(("到会/返程安全", html.escape(business_feasibility)))
            if plan.get("schedule_note"):
                rows.append(("安排说明", html.escape(str(plan.get("schedule_note")))))
        if plan.get("same_day_tag"):
            rows.append(("商务模式", html.escape(str(plan.get("same_day_tag")))))
        links = plan.get("links") or {}
        purchase_mode_text = str(plan.get("purchase_mode") or "")
        split_ticket = "单程" in purchase_mode_text
        if split_ticket and (links.get("outbound") or links.get("return")):
            rows.append(("验证此方案", _split_ticket_verification_html(plan)))
        else:
            link_lines = []
            if links.get("main") or links.get("outbound") or links.get("return"):
                combo_links = links.get("main") or links.get("outbound") or links.get("return")
                link_lines.append(_layered_channel_links(combo_links) or combo_links)
            if link_lines:
                verify_intro = "验证此方案(往返一张票):"
                if plan.get("price"):
                    verify_intro += f"{html.escape(str(plan.get('outbound_push_line') or '去程'))} / {html.escape(str(plan.get('return_push_line') or '返程'))},往返{_plan_roundtrip_price_text(plan)}。"
                verify_intro += "建议在同一渠道选择往返搜索。"
                rows.append(("验证此方案", verify_intro + "<br>" + "<br>".join(link_lines)))
    else:
        main_flight = plan.get("main_flight") or plan.get("outbound_flight") or plan.get("flight")
        body_parts.append(
            _email_plan_leg_group("去程", main_flight, str(plan.get("summary") or ""))
        )
        rows.extend(
            [
                ("搜索参考价", _price_text(plan.get("price"))),
                ("预估实付价", _price_text_with_caliber(plan.get("estimated_price"), "all_passengers_roundtrip" if _passenger_pricing_applies(plan.get("passenger_pricing")) and plan.get("is_roundtrip") else ("all_passengers_oneway" if _passenger_pricing_applies(plan.get("passenger_pricing")) else ("per_person_roundtrip" if plan.get("is_roundtrip") else "per_person_oneway")), *_plan_price_context(plan))),
                ("行李状态", f'<span style="color:#d97706;">{html.escape(str(plan.get("baggage_line") or "支付页需确认"))}</span>'),
            ]
        )
        links = (plan.get("links") or {}).get("main")
        if links:
            rows.append(("验证此方案", _layered_channel_links(links) or links))
    if plan.get("tags"):
        rows.append(("状态", html.escape(str(plan.get("tags") or ""))))
    lcc_baggage_warning = _plan_lcc_baggage_warning(plan)
    if lcc_baggage_warning:
        rows.append(("廉航行李提醒", html.escape(lcc_baggage_warning)))
    feasibility_line = _plan_feasibility_line(plan)
    if feasibility_line:
        rows.append(("可行性分析", feasibility_line))
    refund_line = _plan_refund_line(plan)
    if refund_line:
        rows.append(("退改", refund_line))
    rows.extend(_route_specific_plan_rows(plan))
    punctuality_line = _plan_punctuality_line(plan)
    if punctuality_line:
        rows.append(("准点率", punctuality_line))
    effective_cost_line = _plan_effective_cost_line(plan)
    if effective_cost_line:
        rows.append(("有效出行成本", effective_cost_line))
    logistics_line = _plan_logistics_line(plan)
    if logistics_line:
        rows.append(("机场交通", logistics_line))
    source_label = _plan_source_label(plan)
    if source_label:
        rows.append(("数据来源", html.escape(source_label)))
    channel_advice = _plan_channel_purchase_advice(plan)
    if channel_advice:
        rows.append(("购买渠道建议", channel_advice))
    if plan.get("tier_reason"):
        rows.append(("分级原因", html.escape(str(plan.get("tier_reason")))))
    suitable_condition = str(plan.get("suitable_condition") or "").strip()
    if not suitable_condition and str(plan.get("tier") or "").strip() == "低价备选":
        suitable_condition = f"如果你能接受{plan.get('tier_reason') or '额外执行风险'}，可验证该方案"
    if suitable_condition:
        rows.append(("适合条件", html.escape(suitable_condition)))
    feedback_link = _plan_feedback_link(plan)
    if feedback_link:
        rows.append(("反馈", feedback_link))
    plan_checks = _plan_inline_checklist(plan)
    if plan_checks:
        rows.append(("验证重点", "<br>".join(html.escape(item) for item in plan_checks)))
    if not compact:
        rows.append(("操作建议", f'<span style="color:#16a34a;">{html.escape(str(plan.get("buy_condition") or "以支付页为准"))}</span>'))
    body_parts.append(_email_plan_price_group(rows))
    return _email_card(title, "".join(body_parts), _plan_card_style(plan, tier))


def _plan_feedback_link(plan: dict) -> str:
    url = str(plan.get("feedback_url") or "").strip()
    if not url:
        return ""
    label = str(plan.get("label") or "").strip() or "方案"
    plan_code = re.sub(r"^方案", "", label).strip() or label
    sep = "&" if "?" in url else "?"
    return (
        f'<a href="{html.escape(url + sep + "plan=" + quote_plus(plan_code))}" target="_blank">'
        "价格不一致?反馈</a>"
    )


def _feasibility_item_text(label: str, item: dict, meeting_context: bool = False) -> str:
    level = str(item.get("level") or "").strip()
    if not level:
        return ""
    prefix = {"可行": "✓", "紧张": "⚠", "不可行": "✗"}.get(level, "")
    if level == "不可行":
        status = f"{prefix} {label}{level}(差{item.get('short_min')}分钟,需{item.get('need_set_off')}前动身)"
    else:
        status = f"{prefix} {label}{level}({_humanize_margin(item.get('margin_min'), meeting_context)})"
    parts = [
        f"车程{item.get('transport_min')}",
        f"路途冗余{item.get('transport_margin_min')}",
        f"{item.get('buffer_label')}{item.get('departure_buffer_min')}",
        f"安全余量{item.get('safety_min')}",
    ]
    return f"{status}; " + "+".join(str(part) + "分钟" for part in parts if part and not str(part).endswith("None"))


def _humanize_margin(minutes, meeting_context: bool = False) -> str:
    value = _to_float(minutes)
    if value is None:
        return "时间余量待确认"
    value = int(round(value))
    prefix = "距会议开始还有约" if meeting_context else "时间余量约"
    if value >= 600:
        return f"{prefix}{value // 60}小时,时间充足"
    if value >= 60:
        hours, mins = divmod(value, 60)
        return f"{prefix}{hours}小时{mins}分钟"
    if value >= 0:
        return f"{prefix}{value}分钟,较紧凑"
    return f"赶不上,差约{-value}分钟"


def _plan_has_meeting_context(plan: dict | None) -> bool:
    plan = plan or {}
    if plan.get("_meeting_context"):
        return True
    windows = plan.get("same_day_windows") or {}
    business = plan.get("business_feasibility") or {}
    return any(
        str(value or "").strip()
        for value in (
            plan.get("meeting_start"),
            plan.get("business_start"),
            windows.get("meeting_start"),
            windows.get("business_start"),
            business.get("meeting_start"),
            business.get("business_start"),
        )
    )


def _plan_feasibility_line(plan: dict, meeting_context: bool | None = None) -> str:
    feasibility = plan.get("feasibility") or {}
    if not isinstance(feasibility, dict) or not feasibility:
        return ""
    if meeting_context is None:
        meeting_context = _plan_has_meeting_context(plan)
    lines = []
    if feasibility.get("outbound"):
        lines.append(_feasibility_item_text("去程", feasibility["outbound"], meeting_context))
    if feasibility.get("return"):
        lines.append(_feasibility_item_text("返程", feasibility["return"], meeting_context))
    return "<br>".join(html.escape(line) for line in lines if line)


def _cabin_policy_summary_body(payload: dict) -> str:
    summary = payload.get("cabin_policy_summary") or {}
    if not isinstance(summary, dict):
        return ""
    policy = str(summary.get("cabin_policy") or "economy_only")
    cabins = summary.get("cabins") or []
    if policy == "economy_only" and "business" not in cabins:
        return ""
    nature_labels = {
        "business": "商务出差",
        "business_trip": "商务出差",
        "meeting": "商务会议",
        "business_meeting": "商务会议",
        "team_building": "公司团建",
    }
    raw_natures = summary.get("trip_natures") or []
    if isinstance(raw_natures, str):
        raw_natures = [raw_natures]
    if not raw_natures and summary.get("trip_nature"):
        raw_natures = [summary.get("trip_nature")]
    natures = []
    for item in raw_natures:
        value = str(item or "").strip()
        if value and value not in natures:
            natures.append(value)
    nature_label = " + ".join(nature_labels.get(item, item) for item in natures)
    arrangement_label = {
        "economy_all": "全部经济舱",
        "business_all": "全部商务舱",
        "mixed": "混合舱位",
    }.get(str(summary.get("cabin_arrangement") or ""), str(summary.get("cabin_arrangement") or ""))
    policy_label = {
        "economy_only": "仅经济舱报销",
        "level_based": "部分职级可商务舱",
        "business_allowed": "均可商务舱",
    }.get(policy, policy)
    business_seats = int(summary.get("business_seats") or 0)
    economy_seats = int(summary.get("economy_seats") or 0)
    team_count = business_seats + economy_seats
    mixed_reference = (payload.get("mixed_cabin") or {}).get("reference_price") or {}
    rows = [
        ("出行性质", html.escape(nature_label or "未设置")),
        ("团队人数", html.escape(f"{team_count}人" if team_count else "未设置")),
        ("舱位安排", html.escape(arrangement_label or "未设置")),
        ("舱位政策", html.escape(policy_label)),
        ("本次查询舱位", html.escape(" / ".join(_cabin_label(item) for item in cabins) or "经济舱")),
    ]
    if business_seats or economy_seats:
        rows.append(("团队席位", html.escape(f"商务舱{business_seats}人，经济舱{economy_seats}人")))
    if not mixed_reference:
        economy_price = summary.get("economy_unit_price")
        business_price = summary.get("business_unit_price")
        if business_price is not None:
            business_total = float(business_price) * business_seats if business_seats else None
            business_text = f"参考单人价 {_price_text(business_price)}"
            if business_total is not None:
                business_text += f" × {business_seats} = {_price_text(round(business_total))}"
            rows.append(("商务舱", html.escape(business_text)))
        if economy_price is not None:
            economy_total = float(economy_price) * economy_seats if economy_seats else None
            economy_text = f"参考单人价 {_price_text(economy_price)}"
            if economy_total is not None:
                economy_text += f" × {economy_seats} = {_price_text(round(economy_total))}"
            rows.append(("经济舱", html.escape(economy_text)))
    if mixed_reference:
        reference_label = str(mixed_reference.get("label") or "混舱报价参考")
        reference_amount = _to_float(mixed_reference.get("amount"))
        if reference_amount is not None:
            rows.append((html.escape(reference_label), _price_text(reference_amount)))
        else:
            reason = str(mixed_reference.get("reason") or "混舱报价信息不足")
            rows.append(
                (
                    html.escape(reference_label),
                    html.escape(f"暂不可用(原因={reason})"),
                )
            )
    elif summary.get("team_cost_note"):
        rows.append(("团队合计", html.escape(str(summary.get("team_cost_note")))))
    notes = [
        str(summary.get("business_reimburse_note") or "").strip(),
        str(summary.get("economy_reimburse_note") or "").strip(),
    ]
    notes = [note for note in notes if note]
    if notes:
        rows.append(("报销判断", "<br>".join(html.escape(note) for note in notes)))
    rows.append(
        (
            "舱位差异",
            "商务舱通常包含更灵活退改、优先值机/安检、休息室等；经济舱按具体舱位执行行李退改规则。",
        )
    )
    rows.append(("说明", "系统仅客观展示是否在报销范围内和差价对应服务，最终舱位由你按公司规定选择。"))
    return _email_table(rows)


def _plan_inline_checklist(plan: dict) -> list[str]:
    tier = str(plan.get("tier") or "")
    purchase_mode = str(plan.get("purchase_mode") or "")
    checks: list[str] = []
    if "低价" in tier or "单程" in purchase_mode:
        if "单程" in purchase_mode:
            checks.append("方案是否接受两个单程分别购买")
            checks.append("两段售后是否分别处理")
        if _plan_total_stops(plan) > 0:
            checks.append("中转时间是否足够")
    return checks


def _plan_tier_badge(plan: dict, tier: str) -> str:
    if "低价" in tier:
        return "更便宜但风险更高"
    if "首选" in tier:
        return "更省心"
    return "备选"


def _plan_card_style(plan: dict, tier: str) -> str:
    if "低价" in tier:
        return (
            "background:#fff;border:1px solid #d1d5db;border-radius:10px;"
            "padding:16px;margin:14px 0;"
        )
    if "首选" in tier:
        return (
            "background:#fff;border:1px solid #93c5fd;border-radius:10px;"
            "padding:16px;margin:14px 0;"
        )
    return EMAIL_CARD_STYLE


def _plan_tradeoff_summary_html(plan: dict, primary_plan: dict | None = None) -> str:
    summary = _plan_tradeoff_summary(plan, primary_plan)
    if not summary:
        return ""
    return (
        "<div style='margin-bottom:10px;color:#374151;font-size:14px;'>"
        f"{html.escape(summary)}"
        "</div>"
    )


def _plan_tradeoff_summary(plan: dict, primary_plan: dict | None = None) -> str:
    label = str(plan.get("label") or "方案")
    tier = str(plan.get("tier") or "").strip()
    reason = str(plan.get("tier_reason") or "").strip()
    friendly_reason = str(plan.get("friendly_reason") or "").strip()
    if friendly_reason:
        return f"{label}:{friendly_reason}"
    if "低价" in tier:
        diff = None
        primary_price = _to_float((primary_plan or {}).get("price"))
        price = _to_float(plan.get("price"))
        if primary_price is not None and price is not None and price < primary_price:
            diff = primary_price - price
        diff_text = f"便宜约{_price_text(diff)}" if diff else "价格更低"
        risk_text = reason or "执行风险更高"
        return f"{label}:{diff_text},但{risk_text}"
    if plan.get("is_roundtrip") and _plan_total_stops(plan) == 0:
        return f"{label}:直飞,省心,但仍需确认最终价、行李和票规后再买。"
    return f"{label}:信息仍需支付页验证,确认最终价和票规后再买。"


def _payload_bar_html(title: str, rows: list[dict]) -> str:
    lines: list[str] = []
    _append_css_bar_chart(lines, title, rows)
    return "<br>".join(lines)


EMAIL_CARD_STYLE = (
    "background:#fff;border:1px solid #e5e7eb;border-radius:10px;"
    "padding:16px;margin:14px 0;"
)
EMAIL_CARD_TITLE_STYLE = (
    "font-size:15px;font-weight:600;color:#111;margin-bottom:10px;"
    "border-bottom:1px solid #f0f0f0;padding-bottom:6px;"
)
EMAIL_CARD_BODY_STYLE = "font-size:14px;color:#333;line-height:1.7;"
EMAIL_LABEL_CELL_STYLE = "color:#888;width:90px;vertical-align:top;padding:4px 8px 4px 0;"
EMAIL_VALUE_CELL_STYLE = "color:#333;vertical-align:top;padding:4px 0;"
EMAIL_LEG_GROUP_STYLE = "margin-bottom:14px;"
EMAIL_LEG_TITLE_STYLE = (
    "font-weight:600;color:#111;margin-bottom:6px;"
    "background:#f5f7fa;padding:4px 8px;border-radius:4px;"
)
EMAIL_LEG_LABEL_CELL_STYLE = "color:#999;width:80px;vertical-align:top;padding:4px 8px 4px 0;"
EMAIL_LEG_VALUE_CELL_STYLE = "color:#333;vertical-align:top;padding:4px 0;"


def _email_card(title: str, body: str, card_style: str | None = None) -> str:
    style = card_style or EMAIL_CARD_STYLE
    return (
        f'<div style="{style}">'
        f'<div style="{EMAIL_CARD_TITLE_STYLE}">{html.escape(str(title or ""))}</div>'
        f'<div style="{EMAIL_CARD_BODY_STYLE}">{body}</div>'
        "</div>"
    )


def _email_table(rows: list[tuple[str, str]]) -> str:
    cells = []
    for label, value in rows:
        if value in (None, ""):
            continue
        cells.append(
            "<tr>"
            f"<td style='{EMAIL_LABEL_CELL_STYLE}'>{html.escape(str(label))}</td>"
            f"<td style='{EMAIL_VALUE_CELL_STYLE}'>{value}</td>"
            "</tr>"
        )
    if not cells:
        return ""
    return "<table style='width:100%;font-size:14px;border-collapse:collapse;'>" + "".join(cells) + "</table>"


def _email_leg_table(rows: list[tuple[str, str]]) -> str:
    cells = []
    for label, value in rows:
        if value in (None, ""):
            continue
        cells.append(
            "<tr>"
            f"<td style='{EMAIL_LEG_LABEL_CELL_STYLE}'>{html.escape(str(label))}</td>"
            f"<td style='{EMAIL_LEG_VALUE_CELL_STYLE}'>{value}</td>"
            "</tr>"
        )
    if not cells:
        return ""
    return "<table style='width:100%;font-size:14px;line-height:1.8;border-collapse:collapse;'>" + "".join(cells) + "</table>"


def _email_plan_local_time(airport_code: str, time_value) -> str:
    airport = _airport_short_label(airport_code)
    time_text = _time_only(time_value) or "时间待确认"
    local_city = _airport_local_city(airport_code)
    return f"{html.escape(airport)} {html.escape(time_text)}　{html.escape(local_city)}当地时间"


def _safe_flight_field(flight: dict | None, *keys: str, default=""):
    flight = flight or {}
    for key in keys:
        value = flight.get(key)
        if value not in (None, "", []):
            return value
    return default


def _safe_nested_field(value, *keys: str, default=""):
    if isinstance(value, dict):
        for key in keys:
            item = value.get(key)
            if item not in (None, "", []):
                return item
    elif isinstance(value, str) and value:
        return value
    return default


def _normalize_email_segment(segment: dict | None, fallback_airline: str = "") -> dict:
    segment = segment or {}
    dep = segment.get("departure_airport") or segment.get("origin") or {}
    arr = segment.get("arrival_airport") or segment.get("destination") or {}
    return {
        "flight_no": _safe_nested_field(
            segment, "flight_no", "flight_number", "number", "flight", default=""
        ),
        "airline": _safe_nested_field(segment, "airline", "carrier", default=fallback_airline),
        "dep_airport": _safe_nested_field(
            segment, "dep_airport", "departure_airport_id", "origin", default=""
        )
        or _safe_nested_field(dep, "id", "airport_id", "code", "iata", default=""),
        "dep_time": _safe_nested_field(
            segment, "dep_time", "departure_time", "departure", "time", default=""
        )
        or _safe_nested_field(dep, "time", "departure_time", default=""),
        "arr_airport": _safe_nested_field(
            segment, "arr_airport", "arrival_airport_id", "destination", default=""
        )
        or _safe_nested_field(arr, "id", "airport_id", "code", "iata", default=""),
        "arr_time": _safe_nested_field(
            segment, "arr_time", "arrival_time", "arrival", default=""
        )
        or _safe_nested_field(arr, "time", "arrival_time", default=""),
        "aircraft": get_aircraft_name(
            _safe_nested_field(segment, "aircraft", "airplane", "plane_type", "equipment", default="")
        ),
        "duration_min": _safe_nested_field(segment, "duration_min", "duration", default=0),
    }


def _email_plan_segments(flight: dict | None) -> list[dict]:
    flight = flight or {}
    raw_segments = flight.get("segments") or flight.get("flights") or flight.get("legs") or []
    fallback_airline = flight.get("airline_summary") or flight.get("airline") or ""
    segments = [
        _normalize_email_segment(segment, fallback_airline)
        for segment in raw_segments
        if isinstance(segment, dict)
    ]
    if segments:
        return segments

    dep_airport = _safe_flight_field(
        flight, "departure_airport", "dep_airport", "origin_airport", "origin"
    )
    arr_airport = _safe_flight_field(
        flight, "arrival_airport", "arr_airport", "destination_airport", "destination"
    )
    dep_time = _safe_flight_field(flight, "departure_time", "dep_time")
    arr_time = _safe_flight_field(flight, "arrival_time", "arr_time")
    aircraft = get_aircraft_name(_safe_flight_field(flight, "aircraft", "airplane", "plane_type", "equipment"))
    if not any([dep_airport, arr_airport, dep_time, arr_time, aircraft, flight.get("flight_combo")]):
        return []
    return [
        {
            "flight_no": flight.get("flight_combo") or flight.get("flight_no") or "",
            "airline": flight.get("airline_summary") or flight.get("airline") or "",
            "dep_airport": dep_airport,
            "dep_time": dep_time,
            "arr_airport": arr_airport,
            "arr_time": arr_time,
            "aircraft": aircraft,
        }
    ]


def _email_plan_duration_text(flight: dict | None) -> str:
    flight = flight or {}
    minutes = _to_float(flight.get("total_duration_min"))
    if minutes is None:
        hours = _to_float(flight.get("total_hours"))
        minutes = hours * 60 if hours is not None else None
    if minutes is None:
        return ""
    minutes = int(round(minutes))
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _email_plan_wait_text(minutes) -> str:
    value = _to_float(minutes)
    if value is None:
        return ""
    value = int(round(value))
    return f"{value // 60}h{value % 60:02d}m"


def _email_plan_aircraft_text(flight: dict | None) -> str:
    flight = flight or {}
    aircraft = []
    for segment in _email_plan_segments(flight):
        item = str(
            segment.get("aircraft")
            or segment.get("airplane")
            or segment.get("plane_type")
            or segment.get("equipment")
            or ""
        ).strip()
        item = get_aircraft_name(item)
        if item and item not in {"未知", "unknown", "Unknown", "请查询航司官网"} and item not in aircraft:
            aircraft.append(item)
    top_level = str(_safe_flight_field(flight, "aircraft", "airplane", "plane_type", "equipment") or "").strip()
    top_level = get_aircraft_name(top_level)
    if top_level and top_level not in {"未知", "unknown", "Unknown", "请查询航司官网"} and top_level not in aircraft:
        aircraft.append(top_level)
    return " / ".join(aircraft) if aircraft else "机型待确认"


def _email_plan_transfer_text(flight: dict | None) -> str:
    flight = flight or {}
    segments = _email_plan_segments(flight)
    try:
        stops = int(flight.get("stops") if flight.get("stops") is not None else max(len(segments) - 1, 0))
    except (TypeError, ValueError):
        stops = max(len(segments) - 1, 0)
    if stops <= 0:
        return "直飞"

    layovers = flight.get("layovers") or []
    parts = []
    if layovers:
        for layover in layovers[:2]:
            airport = str(layover.get("airport") or "").strip().upper()
            city = str(layover.get("city") or "").strip()
            place = _airport_short_label(airport) if airport else city or "中转地待确认"
            wait = _email_plan_wait_text(layover.get("wait_minutes"))
            parts.append(f"{place} 等待{wait}" if wait else place)
    elif len(segments) >= 2:
        airport = str(segments[0].get("arr_airport") or "").strip().upper()
        parts.append(_airport_short_label(airport) if airport else "中转地待确认")

    duration = _email_plan_duration_text(flight)
    summary = f"中转{stops}次"
    if parts:
        summary += " 经" + " / ".join(parts)
    if duration:
        summary += f"｜总时长{duration}"
    return html.escape(summary)


def _email_plan_flight_text(flight: dict | None) -> str:
    flight = flight or {}
    text = f"{_compact_flight_numbers(flight)} {_flight_airline_name(flight)}"
    lcc_suffix = _lcc_flight_display_suffix(flight)
    if lcc_suffix:
        text += f" | {lcc_suffix}"
    return html.escape(text)


def _email_plan_leg_group(title: str, flight: dict | None, fallback: str = "") -> str:
    flight = flight or {}
    segments = _email_plan_segments(flight)
    flight_debug_no = str(flight.get("flight_no") or flight.get("flight_number") or flight.get("flight_combo") or "")
    needs_debug = bool(flight) and (
        flight_debug_no.upper().startswith("CA")
        or not segments
        or _email_plan_aircraft_text(flight) == "机型待确认"
    )
    if needs_debug:
        print(f"[航班调试] 航班号={flight.get('flight_no') or flight.get('flight_number') or flight.get('flight_combo')}")
        print(f"[航班调试] 完整字段: {json.dumps(flight, ensure_ascii=False, default=str)}")
    heading = (
        f'<div style="{EMAIL_LEG_TITLE_STYLE}">✈ {html.escape(str(title or "航程"))}</div>'
    )
    if not segments and not flight.get("flight_combo"):
        rows = [("航班", _escape_multiline(fallback or "航班信息待确认"))]
        return f'<div style="{EMAIL_LEG_GROUP_STYLE}">{heading}{_email_leg_table(rows)}</div>'

    first = segments[0] if segments else {}
    last = segments[-1] if segments else {}
    dep_airport = str(first.get("dep_airport") or first.get("departure_airport") or "").strip().upper()
    arr_airport = str(last.get("arr_airport") or last.get("arrival_airport") or "").strip().upper()

    rows = [
        ("航班", _email_plan_flight_text(flight)),
        ("起飞", _email_plan_local_time(dep_airport, first.get("dep_time")) if segments else "时间待确认"),
    ]
    if segments and len(segments) > 1:
        rows.append(("中转", _email_plan_transfer_text(flight)))
    rows.append(("到达", _email_plan_local_time(arr_airport, last.get("arr_time")) if segments else "时间待确认"))
    if not segments or len(segments) <= 1:
        rows.append(("中转", _email_plan_transfer_text(flight)))
    duration = _email_plan_duration_text(flight)
    if segments and len(segments) > 1 and duration:
        rows.append(("总时长", html.escape(duration)))
    rows.append(("机型", html.escape(_email_plan_aircraft_text(flight))))
    return f'<div style="{EMAIL_LEG_GROUP_STYLE}">{heading}{_email_leg_table(rows)}</div>'


def _email_plan_price_group(rows: list[tuple[str, str]]) -> str:
    return (
        '<div style="border-top:1px solid #f0f0f0;padding-top:10px;">'
        + _email_table(rows)
        + "</div>"
    )


def _email_list(items, limit: int = 5) -> str:
    rows = [str(item).strip() for item in (items or []) if str(item or "").strip()][:limit]
    if not rows:
        return "<div style='color:#888;font-size:12px;'>暂无更多信息</div>"
    return "".join(f"<div>- {html.escape(row)}</div>" for row in rows)


def _email_price_span(value, color: str = "#111") -> str:
    return f'<span style="color:{color};font-weight:600;">{_price_text(value)}</span>'


def _passenger_breakdown_text(passengers: dict | None) -> str:
    passengers = passengers or {}
    parts = []
    for key, label in (("adult", "成人"), ("child", "儿童"), ("elderly", "老人"), ("infant", "婴儿")):
        try:
            count = int(passengers.get(key) or 0)
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            parts.append(f"{label}{count}")
    return "+".join(parts)


def _passenger_label_from_counts(passengers: dict | None) -> str:
    passengers = passengers or {}
    parts = []
    for key, label in (("adult", "成人"), ("child", "儿童"), ("elderly", "老人"), ("infant", "婴儿")):
        try:
            count = int(passengers.get(key) or 0)
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            parts.append(f"{count}{label}")
    return "+".join(parts) or "1成人"


def _passenger_total_count(passengers: dict | None) -> int:
    passengers = passengers or {}
    total = 0
    for key in ("adult", "child", "elderly", "infant"):
        try:
            total += max(0, int(passengers.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return total or 1


def _per_head_blended_label(passenger_count) -> str:
    count = _to_float(passenger_count)
    count = max(1, int(count)) if count is not None else 1
    return f"人均摊薄(全员÷{count},含儿童折扣)"


def _passenger_part_text(breakdown: dict | None) -> str:
    parts = []
    for item in (breakdown or {}).get("parts") or []:
        label = str(item.get("label") or "").strip()
        count = int(item.get("count") or 0)
        unit_price = item.get("unit_price")
        if not label or count <= 0:
            continue
        if count == 1:
            parts.append(f"{label}{_price_text(unit_price)}")
        else:
            parts.append(f"{label}{_price_text(unit_price)}×{count}")
    return "+".join(parts)


def _passenger_pricing_applies(passenger_pricing: dict | None) -> bool:
    if not isinstance(passenger_pricing, dict):
        return False
    if passenger_pricing.get("applies"):
        return True
    factor = _to_float(passenger_pricing.get("factor"))
    if factor is not None and factor != 1:
        return True
    return _passenger_total_count(passenger_pricing.get("passengers")) > 1


def _apply_passenger_pricing_to_plans(
    plans: list[dict],
    passengers: dict | None,
    route_type: str | None = None,
) -> list[dict]:
    passengers = passengers or {"adult": 1, "child": 0, "elderly": 0, "infant": 0}
    for plan in plans or []:
        if not isinstance(plan, dict):
            continue
        route = plan.get("route_type") or route_type or ""
        if route:
            plan["route_type"] = route
        if plan.get("mixed_cabin"):
            tree = plan.get("mixed_cabin_pricing") or plan.get("passenger_pricing") or {}
            if not isinstance(tree, dict) or not tree.get("mixed_cabin"):
                continue
            pricing = dict(tree)
            pricing.update(
                {
                    "applies": True,
                    "scope": "roundtrip",
                    "total_price": tree.get("total"),
                    "estimated_total": tree.get("total"),
                }
            )
            plan["passenger_pricing"] = pricing
            plan["mixed_cabin_pricing"] = tree
            plan["price_tiers"] = plan.get("price_tiers") or {}
            plan["price"] = tree.get("total")
            plan["roundtrip_price"] = tree.get("total")
            plan["estimated_price"] = tree.get("total")
            plan["passenger_total_price"] = tree.get("total")
            plan["raw_passenger_total_price"] = tree.get("raw_total")
            continue
        if plan.get("is_roundtrip"):
            outbound_unit = _to_float(plan.get("outbound_price") or (plan.get("outbound_flight") or {}).get("price"))
            return_unit = _to_float(plan.get("return_price") or (plan.get("return_flight") or {}).get("price"))
            if outbound_unit is None or return_unit is None:
                continue
            display_prices = build_display_prices(outbound_unit, return_unit, passengers, route)
            outbound_breakdown = build_passenger_price_breakdown(outbound_unit, passengers, "economy", route)
            return_breakdown = build_passenger_price_breakdown(return_unit, passengers, "economy", route)
            single_adult = outbound_unit + return_unit
            factor = _to_float(outbound_breakdown.get("factor")) or 1.0
            total = display_prices["total"]
            existing_estimated_total = _to_float((plan.get("price_tiers") or {}).get("total_estimated"))
            if existing_estimated_total is not None:
                estimated_total = round_display_price(existing_estimated_total)
            else:
                estimated_unit = _to_float(plan.get("estimated_price")) or single_adult
                estimated_total = build_display_prices(estimated_unit, None, passengers, route)["total"]
            pricing = {
                "applies": bool(outbound_breakdown.get("factor") != 1 or _passenger_total_count(passengers) > 1),
                "scope": "roundtrip",
                "passengers": outbound_breakdown.get("passengers"),
                "passenger_label": outbound_breakdown.get("passenger_label"),
                "factor": factor,
                "route_type": route,
                "outbound": outbound_breakdown,
                "return": return_breakdown,
                "total_price": total,
                "estimated_total": estimated_total,
                "single_adult_price": single_adult,
                "note": outbound_breakdown.get("note") or "",
            }
            price_tiers = build_price_tiers(
                outbound_unit,
                return_unit,
                passengers,
                route,
                purchase_type=plan.get("purchase_mode") or plan.get("purchase_type"),
                total_estimated=pricing["estimated_total"],
            )
            price_tiers["total_roundtrip_ref"] = total
            price_tiers["total_estimated"] = estimated_total
            pricing["price_tiers"] = price_tiers
            plan["passenger_pricing"] = pricing
            plan["price_tiers"] = price_tiers
            plan["single_adult_price"] = single_adult
            plan["adult_roundtrip_price"] = single_adult
            if pricing["applies"]:
                plan["price"] = total
                plan["roundtrip_price"] = total
                plan["estimated_price"] = pricing["estimated_total"]
            continue
        unit = _to_float(plan.get("price"))
        if unit is None:
            continue
        breakdown = build_passenger_price_breakdown(unit, passengers, "economy", route)
        pricing = {
            "applies": bool(breakdown.get("factor") != 1 or _passenger_total_count(passengers) > 1),
            "scope": "oneway",
            "passengers": breakdown.get("passengers"),
            "passenger_label": breakdown.get("passenger_label"),
            "factor": breakdown.get("factor"),
            "route_type": route,
            "main": breakdown,
            "total_price": breakdown.get("total"),
            "single_adult_price": unit,
            "note": breakdown.get("note") or "",
        }
        price_tiers = build_price_tiers(
            unit,
            None,
            passengers,
            route,
            purchase_type=plan.get("purchase_mode") or plan.get("purchase_type") or "oneway",
            total_estimated=pricing.get("total_price"),
        )
        pricing["price_tiers"] = price_tiers
        plan["passenger_pricing"] = pricing
        plan["price_tiers"] = price_tiers
        plan["single_adult_price"] = unit
        if pricing["applies"]:
            plan["price"] = breakdown.get("total")
            existing_estimated_total = _to_float((plan.get("price_tiers") or {}).get("total_estimated"))
            if existing_estimated_total is not None:
                plan["estimated_price"] = round_display_price(existing_estimated_total)
            else:
                estimated_unit = _to_float(plan.get("estimated_price")) or unit
                plan["estimated_price"] = calc_total_price_for_passengers(
                    estimated_unit,
                    passengers,
                    "economy",
                    route,
                )
            plan["price_tiers"]["total_estimated"] = round_display_price(plan["estimated_price"])
            plan["price_tiers"]["per_person_estimated"] = round_display_price(
                plan["estimated_price"] / max(1, _passenger_total_count(passengers))
            )
    return plans


def _apply_passenger_pricing_to_excluded(
    excluded_items: list[dict],
    passengers: dict | None,
    route_type: str | None = None,
    recommended_price=None,
) -> list[dict]:
    passengers = passengers or {"adult": 1, "child": 0, "elderly": 0, "infant": 0}
    for item in excluded_items or []:
        if not isinstance(item, dict):
            continue
        outbound = item.get("outbound") or {}
        ret = item.get("return") or {}
        if not outbound or not ret:
            continue
        outbound_unit = _to_float(item.get("outbound_price") or outbound.get("price"))
        return_unit = _to_float(item.get("return_price") or ret.get("price"))
        if outbound_unit is None or return_unit is None:
            continue
        if route_type:
            item["route_type"] = route_type
        display_prices = build_display_prices(outbound_unit, return_unit, passengers, route_type)
        outbound_breakdown = build_passenger_price_breakdown(outbound_unit, passengers, "economy", route_type)
        return_breakdown = build_passenger_price_breakdown(return_unit, passengers, "economy", route_type)
        single_adult = outbound_unit + return_unit
        factor = _to_float(outbound_breakdown.get("factor")) or 1.0
        total = display_prices["total"]
        pricing = {
            "applies": bool(outbound_breakdown.get("factor") != 1 or _passenger_total_count(passengers) > 1),
            "scope": "roundtrip",
            "passengers": outbound_breakdown.get("passengers"),
            "passenger_label": outbound_breakdown.get("passenger_label"),
            "factor": factor,
            "route_type": route_type or "",
            "outbound": outbound_breakdown,
            "return": return_breakdown,
            "total_price": total,
            "single_adult_price": single_adult,
            "note": outbound_breakdown.get("note") or "",
        }
        price_tiers = build_price_tiers(
            outbound_unit,
            return_unit,
            passengers,
            route_type,
            purchase_type=item.get("purchase_mode") or item.get("purchase_type") or "roundtrip",
            total_estimated=total,
        )
        price_tiers["total_roundtrip_ref"] = total
        price_tiers["total_estimated"] = total
        pricing["price_tiers"] = price_tiers
        item["passenger_pricing"] = pricing
        item["price_tiers"] = price_tiers
        item["single_adult_price"] = single_adult
        if pricing["applies"]:
            item["total_price"] = total
            item["roundtrip_price"] = total
            item["price"] = total
            ref = _to_float(recommended_price)
            if ref is not None:
                item["diff"] = ref - total
                item["recommended_price"] = ref
                item["comparison_points"] = _canonical_price_comparison_points(
                    item.get("comparison_points") or [],
                    total,
                    ref,
                )
    return excluded_items



def _passenger_pricing_rows(plan: dict) -> list[tuple[str, str]]:
    pricing = plan.get("passenger_pricing") or {}
    if not _passenger_pricing_applies(pricing):
        return []
    if pricing.get("mixed_cabin") or plan.get("mixed_cabin"):
        tree = plan.get("mixed_cabin_pricing") or pricing
        passengers, route_type = _plan_price_context(plan)
        cabin_label = tree.get("cabin_label") or plan.get("cabin_label") or "混舱"
        rows = [
            (
                f"往返全员总价({cabin_label})",
                _price_text_with_caliber(
                    tree.get("total"),
                    "all_passengers_roundtrip",
                    passengers,
                    route_type,
                ),
            )
        ]
        matching = plan.get("mixed_cabin_matching") or {}
        candidates = int(matching.get("candidates") or 0)
        full = int(matching.get("full") or 0)
        visible = int(matching.get("business_visible_count") or 0)
        if candidates:
            rows.append(
                (
                    "商务舱报价匹配",
                    f"{full}/{candidates}个候选同航班两舱完整匹配；本轮可见{visible}个商务舱报价",
                )
            )
        reference = matching.get("business_reference") or {}
        if reference.get("price") is not None:
            airline = html.escape(str(reference.get("airline") or "航司待确认"))
            rows.append(
                (
                    "商务舱单程参考",
                    f"{_price_text(reference.get('price'))}（{airline}；不限方案航班，非方案价）",
                )
            )
        for direction, direction_label, economy_flight, business_flight in (
            (
                "outbound",
                "去程",
                plan.get("outbound_flight") or {},
                plan.get("business_outbound") or {},
            ),
            (
                "return",
                "返程",
                plan.get("return_flight") or {},
                plan.get("business_return") or {},
            ),
        ):
            leg = tree.get(direction) or {}
            for cabin, cabin_label_text, flight in (
                ("business", "商务舱", business_flight),
                ("economy", "经济舱", economy_flight),
            ):
                cabin_tree = (leg.get("cabins") or {}).get(cabin) or {}
                if not cabin_tree:
                    continue
                parts = []
                for part in cabin_tree.get("parts") or []:
                    parts.append(
                        f"{part.get('label')}{part.get('count')}人"
                        f"×{_price_text(part.get('unit_price'))}"
                    )
                combo = (
                    flight.get("flight_combo")
                    or flight.get("flight_no")
                    or "同航班"
                )
                rows.append(
                    (
                        f"{direction_label}{cabin_label_text}",
                        f"{html.escape(str(combo))}；单人成人价"
                        f"{_price_text(cabin_tree.get('unit_price'))}；"
                        f"{' + '.join(parts)} = {_price_text(cabin_tree.get('total'))}小计",
                    )
                )
            rows.append(
                (
                    f"{direction_label}全员",
                    _price_text_with_caliber(
                        leg.get("total"),
                        "all_passengers_oneway",
                        passengers,
                        route_type,
                    ),
                )
            )
        if tree.get("per_person_blended") is not None:
            rows.append(
                (
                    _per_head_blended_label(tree.get("passenger_count")),
                    f"约{_price_text(tree.get('per_person_blended'))}",
                )
            )
        notes = plan.get("mixed_cabin_price_notes") or {}
        if notes.get("economy"):
            rows.append(("经济舱报价说明", html.escape(str(notes["economy"]))))
        if notes.get("business"):
            rows.append(("商务舱报价说明", html.escape(str(notes["business"]))))
        rows.append(
            (
                "双舱库存核验",
                html.escape(str(plan.get("mixed_cabin_disclosure") or MIXED_CABIN_DISCLOSURE)),
            )
        )
        if tree.get("note"):
            rows.append(("人数票价口径", html.escape(str(tree.get("note")))))
        return rows
    label = pricing.get("passenger_label") or _passenger_label_from_counts(pricing.get("passengers"))
    note = str(pricing.get("note") or "").strip()
    passengers, route_type = _plan_price_context(plan)
    if plan.get("is_roundtrip"):
        outbound = pricing.get("outbound") or {}
        ret = pricing.get("return") or {}
        tiers = plan.get("price_tiers") or pricing.get("price_tiers") or {}
        outbound_unit = _to_float(outbound.get("unit_price") or plan.get("outbound_price"))
        return_unit = _to_float(ret.get("unit_price") or plan.get("return_price"))
        display_prices = (
            build_display_prices(outbound_unit, return_unit, passengers, route_type)
            if outbound_unit is not None and return_unit is not None
            else {}
        )
        display_outbound = display_prices.get("outbound") or outbound
        display_return = display_prices.get("return") or ret
        outbound_total = _to_float(display_outbound.get("total"))
        return_total = _to_float(display_return.get("total"))
        outbound_price_text = (
            _price_text_with_caliber(outbound_total, "all_passengers_oneway", passengers, route_type)
            if outbound_total is not None
            else _scoped_price_text_from_pp(outbound_unit, passengers, "all_passengers_oneway", route_type)
        )
        return_price_text = (
            _price_text_with_caliber(return_total, "all_passengers_oneway", passengers, route_type)
            if return_total is not None
            else _scoped_price_text_from_pp(return_unit, passengers, "all_passengers_oneway", route_type)
        )
        outbound_text = f"\u53bb\u7a0b\u5168\u5458{outbound_price_text}"
        outbound_parts = _passenger_part_text(display_outbound)
        if outbound_parts:
            outbound_text += f"({outbound_parts})"
        return_text = f"\u8fd4\u7a0b\u5168\u5458{return_price_text}"
        return_parts = _passenger_part_text(display_return)
        if return_parts:
            return_text += f"({return_parts})"
        rows = [
            (
                f"\u5f80\u8fd4\u603b\u4ef7({label})",
                _price_text_with_caliber(
                    display_prices.get("total"),
                    "all_passengers_roundtrip",
                    passengers,
                    route_type,
                ),
            ),
            ("\u4eba\u6570\u4ef7\u683c\u62c6\u89e3", f"{outbound_text} + {return_text}"),
        ]
        if tiers.get("total_estimated") is not None:
            rows.append(("\u591a\u4eba\u5f80\u8fd4\u9884\u4f30\u5b9e\u4ed8\u603b\u4ef7", f"\u7ea6{_price_text_with_caliber(tiers.get('total_estimated'), 'all_passengers_roundtrip', passengers, route_type)}"))
        if tiers.get("total_estimated") is not None:
            per_person_estimated = round_display_price(
                _to_float(tiers.get("total_estimated"))
                / max(1, int(tiers.get("passenger_count") or _passenger_total_count(passengers)))
            )
            rows.append((
                _per_head_blended_label(tiers.get("passenger_count") or _passenger_total_count(passengers)),
                f"\u7ea6{_price_text(per_person_estimated)}",
            ))
        if pricing.get("single_adult_price"):
            rows.append(("\u5355\u4eba\u5f80\u8fd4\u53c2\u8003", f"\u7ea6{_price_text_with_caliber(pricing.get('single_adult_price'), 'per_person_roundtrip', passengers, route_type)}/\u6210\u4eba"))
    else:
        main = pricing.get("main") or {}
        tiers = plan.get("price_tiers") or pricing.get("price_tiers") or {}
        unit = _to_float(main.get("unit_price") or pricing.get("single_adult_price") or plan.get("price"))
        display_prices = build_display_prices(unit, None, passengers, route_type) if unit is not None else {}
        display_main = display_prices.get("outbound") or main
        rows = [
            (
                f"\u5168\u5458\u53c2\u8003\u4ef7({label})",
                _price_text_with_caliber(display_main.get("total"), "all_passengers_oneway", passengers, route_type),
            ),
        ]
        parts = _passenger_part_text(display_main)
        if parts:
            rows.append(("\u4eba\u6570\u4ef7\u683c\u62c6\u89e3", parts))
        if tiers.get("total_estimated") is not None:
            rows.append(("\u5168\u5458\u9884\u4f30\u5b9e\u4ed8", f"\u7ea6{_price_text_with_caliber(tiers.get('total_estimated'), 'all_passengers_oneway', passengers, route_type)}"))
        if tiers.get("total_estimated") is not None:
            per_person_estimated = round_display_price(
                _to_float(tiers.get("total_estimated"))
                / max(1, int(tiers.get("passenger_count") or _passenger_total_count(passengers)))
            )
            rows.append((
                _per_head_blended_label(tiers.get("passenger_count") or _passenger_total_count(passengers)),
                f"\u7ea6{_price_text(per_person_estimated)}",
            ))
        if pricing.get("single_adult_price"):
            rows.append(("\u5355\u4eba\u53c2\u8003", f"\u7ea6{_price_text_with_caliber(pricing.get('single_adult_price'), 'per_person_oneway', passengers, route_type)}/\u6210\u4eba"))
    if note:
        rows.append(("\u4eba\u6570\u7968\u4ef7\u53e3\u5f84", html.escape(note)))
    return rows

def _email_action_panel_body(
    payload: dict,
    primary_plan: dict,
    verify_text: str,
    price_reason: str,
    interactive_channels: bool = False,
) -> str:
    if _data_incomplete_state(payload):
        reason = _data_incomplete_reason(payload)
        blocks = [
            "<div style='font-weight:600;color:#b91c1c;'>当前判断:数据不完整,本轮结论不可用</div>",
            f"<div><strong>原因:</strong>{html.escape(reason)}</div>",
            "<div style='margin-top:8px;color:#666;font-size:12px;'>"
            "本轮不作航班可行性、市场无票或价格位置判断；订阅已保留，下轮自动重试。"
            "</div>",
            _email_action_links(
                payload,
                None,
                interactive_channels=interactive_channels,
                include_channel_picker=False,
            ),
        ]
        return "".join(block for block in blocks if block)
    if _no_primary_plan_state(payload):
        alternatives = payload.get("same_day_alternatives") or []
        reason = _no_primary_reason(payload)
        labels = _alternative_labels(alternatives)
        alt_text = f"{len(alternatives[:3])}个"
        if labels:
            alt_text += f"({labels})"
        else:
            alt_text = "暂无可展示备选"
        price_hint = _candidate_price_summary_text(payload)
        max_line = _no_primary_max_bottleneck_text(payload)
        mixed_notice = _mixed_cabin_unavailable_text(payload)
        blocks = [
            "<div style='font-weight:600;color:#b91c1c;'>当前判断:❌ 未找到完全符合条件的方案</div>",
            f"<div><strong>主因:</strong>{html.escape(reason)}</div>",
            f"<div><strong>分舱报价:</strong>{html.escape(mixed_notice)}</div>" if mixed_notice else "",
            f"<div><strong>价格:</strong>{html.escape(price_hint)}</div>" if price_hint else "",
            "<div style='margin:8px 0;border-top:1px solid #e5e7eb;'></div>",
            f"<div style='color:#666;font-size:12px;'>{html.escape(max_line)}</div>" if max_line else "",
            f"<div style='margin-top:8px;'><strong>【可选备选】</strong>{html.escape(alt_text)}</div>",
            f"<div style='margin-top:8px;'><strong>【放宽预演】</strong>{html.escape(_no_primary_next_step_text(payload))}</div>",
            "<div style='margin-top:8px;color:#666;font-size:12px;'>触发类型:无符合方案 | 备选参考 | 非直接购买</div>",
            _email_action_links(
                payload,
                None,
                interactive_channels=interactive_channels,
                include_channel_picker=False,
            ),
        ]
        return "".join(block for block in blocks if block)

    conclusion = _clarify_monitoring_copy(str(payload.get("recommendation") or "可以观察"))
    primary_line = _email_primary_plan_line(payload, primary_plan)
    buy_condition = str(payload.get("buy_condition") or "以支付页为准")
    trigger_type = _email_trigger_type(payload)
    trigger_reason = str(price_reason or _email_trigger_reason_text(payload) or "请查看下方原因")
    trigger_evidence = _cheaper_date_trigger_evidence(payload)
    gap_line = _budget_gap_line(payload)
    reason_line = _budget_reason_line(payload, trigger_reason)
    blocks = [
        f"<div>当前判断:{html.escape(conclusion)}</div>",
        f"<div>首选方案:{html.escape(primary_line)}</div>",
        *[f"<div>{line}</div>" for line in _action_panel_price_tier_lines(payload, primary_plan)],
        f"<div>购买条件:{html.escape(buy_condition)}</div>",
        f"<div>{html.escape(gap_line)}</div>" if gap_line else "",
        f"<div>{html.escape(reason_line)}</div>",
        f"<div>触发依据:{html.escape(trigger_evidence)}</div>" if trigger_evidence else "",
        "<div>下一步:保持当前监控本条航线(无需操作,有变化会再次提醒你) | 修改本监控 | (刚需)在下方方案A卡内验证</div>",
        f"<div style='margin-top:8px;color:#666;font-size:12px;'>触发类型:{html.escape(trigger_type)}</div>",
        f"<div style='color:#666;font-size:12px;'>触发原因:{html.escape(trigger_reason)}</div>",
        _next_step_guidance_html(payload),
        _email_action_links(
            payload,
            primary_plan,
            interactive_channels=interactive_channels,
            include_channel_picker=False,
        ),
    ]
    return "".join(block for block in blocks if block)


def _email_primary_plan_line(payload: dict, primary_plan: dict) -> str:
    if not primary_plan:
        return "方案待确认"
    label = str(primary_plan.get("label") or "方案A")
    route_kind = "直飞往返" if primary_plan.get("is_roundtrip") and _plan_total_stops(primary_plan) == 0 else (
        "往返方案" if primary_plan.get("is_roundtrip") else "单程方案"
    )
    price_rows = _payload_plan_price_rows([primary_plan])
    price_row = price_rows[0] if price_rows else {}
    price = _price_text(
        price_row.get("value")
        or primary_plan.get("price")
        or payload.get("display_price")
        or payload.get("current_price")
    )
    if str(price_row.get("scope") or "").startswith("all_passengers_"):
        pricing = primary_plan.get("passenger_pricing") or {}
        factor = _to_float(pricing.get("factor"))
        factor_text = f"（费率合计{factor:g}×单人）" if factor is not None else ""
        return f"{label},{route_kind},全员参考价{price}{factor_text}"
    return f"{label},{route_kind},搜索参考价{price}"



def _action_panel_price_tier_lines(payload: dict, primary_plan: dict) -> list[str]:
    tiers = payload.get("price_tiers") or (primary_plan or {}).get("price_tiers") or {}
    passenger_count = _to_float(tiers.get("passenger_count"))
    show_layers = bool(payload.get("is_roundtrip")) or (passenger_count is not None and passenger_count > 1)
    if not tiers or not show_layers:
        return []
    lines = []
    label = str(tiers.get("passenger_label") or "").strip()
    if label:
        lines.append(f"\u4e58\u673a\u4eba:{html.escape(label)}")
    passengers = _pricing_passengers(tiers, passenger_count)
    route_type = tiers.get("route_type") or payload.get("route_type")
    if tiers.get("total_roundtrip_ref") is not None:
        lines.append(f"\u591a\u4eba\u5f80\u8fd4\u53c2\u8003\u4ef7:{_price_text_with_caliber(tiers.get('total_roundtrip_ref'), 'all_passengers_roundtrip', passengers, route_type)}")
    if tiers.get("total_estimated") is not None:
        lines.append(f"\u9884\u4f30\u5b9e\u4ed8\u603b\u4ef7:\u7ea6{_price_text_with_caliber(tiers.get('total_estimated'), 'all_passengers_roundtrip', passengers, route_type)}")
    if tiers.get("per_person_estimated") is not None:
        blended_label = _per_head_blended_label(passenger_count or _passenger_total_count(passengers))
        lines.append(f"{blended_label}:\u7ea6{_price_text(tiers.get('per_person_estimated'))}")
    return lines

def _email_trigger_type(payload: dict) -> str:
    push_type = str(payload.get("push_type") or "")
    execution = payload.get("execution_advice") or {}
    if "验证" in push_type or "验证" in str(execution.get("label") or "") or "验证" in str(payload.get("recommendation") or ""):
        return "低价线索 | 需验证 | 非直接购买"
    if "异常低价" in push_type or "进入低价" in push_type:
        return "低价线索 | 可验证 | 以支付页为准"
    return f"{push_type or '价格提醒'} | 需确认 | 以支付页为准"


def _email_trigger_reason_text(payload: dict) -> str:
    reasons = [str(item).strip() for item in (payload.get("trigger_reason") or []) if str(item or "").strip()]
    return ",".join(reasons[:2])


def _budget_compare_price_value(payload: dict):
    """Return the reference price in the same visible scope as max_price."""
    value = _to_float((payload or {}).get("budget_compare_price"))
    if value is not None:
        return value
    return _to_float((payload or {}).get("display_price") or (payload or {}).get("current_price"))


def _budget_reason_line(payload: dict, fallback_reason: str) -> str:
    max_price = _to_float((payload or {}).get("max_price"))
    compare_price = _budget_compare_price_value(payload or {})
    if max_price is not None and compare_price is not None:
        route_scope = "往返搜索参考价" if (payload or {}).get("is_roundtrip") else "搜索参考价"
        price_scope = (payload or {}).get("budget_compare_scope")
        compare_text = _price_text_with_parenthesized_caliber(compare_price, price_scope)
        max_text = _price_text_with_parenthesized_caliber(max_price, price_scope)
        if compare_price > max_price:
            return (
                f"原因:{route_scope}{compare_text},"
                f"高于你的最高可接受价{max_text}"
            )
        return (
            f"原因:{route_scope}{compare_text}≤"
            f"最高可接受价{max_text}，未超预算"
        )
    return f"原因:{fallback_reason}"


def _budget_gap(payload: dict) -> dict:
    gap = payload.get("budget_gap")
    if isinstance(gap, dict):
        return gap
    return build_budget_gap(
        _budget_compare_price_value(payload),
        payload.get("max_price"),
        payload.get("ideal_price"),
    )

def _budget_gap_line(payload: dict) -> str:
    text = str((_budget_gap(payload) or {}).get("text") or "").strip()
    return f"预算差距:{text}" if text else ""


def _clarify_monitoring_copy(text: str) -> str:
    """Make passive monitoring copy explicit: keeping this subscription needs no action."""
    value = str(text or "")
    replacements = {
        "继续监控等降价": "继续盯这条航线等降价",
        "保持监控": "保持当前监控",
        "建议继续监控": "建议保持监控本条航线",
        "建议继续观察": "建议保持监控本条航线并观察",
        "不建议购买,继续监控": "不建议购买,保持监控本条航线",
        "不建议购买，继续监控": "不建议购买，保持监控本条航线",
        "继续监控价格变化": "继续盯这条航线的价格变化",
        "继续监控并等待": "保持监控本条航线并等待",
        "继续监控": "保持监控本条航线",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    return value


def _scenario_values_for_guidance(payload: dict) -> list[str]:
    values = []
    raw = payload.get("travel_scenarios") or []
    if isinstance(raw, str):
        raw = [raw]
    values.extend(str(item) for item in raw if item)
    basis = payload.get("recommendation_basis") or {}
    labels = basis.get("scenario_labels") or []
    if isinstance(labels, str):
        labels = [labels]
    values.extend(str(item) for item in labels if item)
    return values


def _next_step_guidance(payload: dict) -> dict:
    guidance = payload.get("next_step_guidance")
    if isinstance(guidance, dict) and guidance.get("items"):
        return guidance
    return build_next_step_guidance(
        payload.get("push_type"),
        payload.get("display_price") or payload.get("current_price"),
        payload.get("max_price"),
        payload.get("ideal_price"),
        _scenario_values_for_guidance(payload),
        payload.get("trip_rigidity"),
    )


def _next_step_guidance_html(payload: dict) -> str:
    guidance = _next_step_guidance(payload)
    items = guidance.get("items") or []
    if not items:
        return ""
    parts = ["<div style='margin-top:8px;font-weight:600;'>你可以:</div>"]
    nums = ["①", "②", "③"]
    for index, item in enumerate(items[:3]):
        label_text = _clarify_monitoring_copy(str(item.get("label") or "下一步"))
        summary_text = _clarify_monitoring_copy(str(item.get("summary") or ""))
        action_text = _clarify_monitoring_copy(str(item.get("action") or ""))
        if index == 0 and ("盯这条航线" in label_text or "监控" in label_text):
            summary_text = f"无需任何操作,{summary_text}"
            action_text = "保持当前监控"
        label = html.escape(label_text)
        summary = html.escape(summary_text)
        action = html.escape(action_text)
        parts.append(
            f"<div>{nums[index]} {label} —— {summary}"
            + (f" [{action}]" if action else "")
            + "</div>"
        )
    if guidance.get("rigid"):
        labels = [
            label
            for label in _scenario_values_for_guidance(payload)
            if any(key in label.lower() for key in ("business", "meeting", "商务", "会议", "重要", "important"))
        ]
        scenario = " + ".join(dict.fromkeys(labels)) or "商务/重要事项"
        parts.append(
            f"<div style='margin-top:6px;color:#666;font-size:12px;'>刚需提示:你的出行场景是{html.escape(scenario)},"
            "若会议必须按时出行,可验证方案A;否则建议等待或换日期。</div>"
        )
    return "".join(parts)


def _email_action_links(
    payload: dict,
    primary_plan: dict | None = None,
    interactive_channels: bool = False,
    include_channel_picker: bool = True,
) -> str:
    plan_label = str((primary_plan or {}).get("label") or "方案A").strip() or "方案A"
    channel_picker = (
        _email_channel_picker(
            primary_plan or {},
            interactive=interactive_channels,
            context_label=f"快速验证首选{plan_label}",
        )
        if include_channel_picker
        else ""
    )
    detail_url = str(payload.get("detail_url") or "")
    form_url = str(payload.get("form_url") or "")
    feedback_url = str(payload.get("feedback_url") or "")
    links = []
    if detail_url:
        links.append(("查看网页版完整分析(如未显示请稍后刷新)", detail_url))
    if form_url:
        links.append(("修改本监控", form_url))
    if feedback_url:
        links.append(("反馈买不到", feedback_url))
    if not links and not channel_picker:
        return ""
    action_links = ""
    if links:
        action_links = (
            "<div style='margin-top:8px;'>"
            + " | ".join(_email_button_link(label, url) for label, url in links)
            + "</div>"
        )
    return (
        "<div style='margin-top:10px;'>"
        + channel_picker
        + "<div style='margin-top:8px;color:#666;font-size:12px;'>保持本条监控:无需操作,系统会继续盯这条航线,有变化会再次提醒你。</div>"
        + action_links
        + "</div>"
    )



def _email_channel_picker(plan: dict, interactive: bool = False, context_label: str = "") -> str:
    price = plan.get("price") or plan.get("display_price") or plan.get("estimated_price")
    sections = _email_channel_sections(plan)
    is_roundtrip = bool((plan or {}).get("is_roundtrip"))
    purchase_mode = str((plan or {}).get("purchase_mode") or "")
    split_ticket = is_roundtrip and "\u5355\u7a0b" in purchase_mode
    if not sections:
        verify_url = _email_primary_booking_url(plan or {})
        if not verify_url:
            return ""
        sections = [("", [("\u53bb\u9a8c\u8bc1\u4ef7\u683c", verify_url)])]
    if split_ticket:
        def section_price(title: str):
            if "\u53bb\u7a0b" in title:
                return plan.get("outbound_price")
            if "\u8fd4\u7a0b" in title:
                return plan.get("return_price")
            return None

        body = (
            "<div style='font-weight:600;margin-bottom:4px;'>"
            f"{html.escape(context_label or '\u53bb\u9a8c\u8bc1\u4ef7\u683c')}(\u4e24\u4e2a\u5355\u7a0b\u62fc\u63a5,\u9700\u5206\u522b\u9a8c\u8bc1):</div>"
        )
        for title, links in sections:
            leg_price = section_price(title)
            priced_title = f"{title}(\u7ea6{_plan_leg_price_text(plan, leg_price)})" if leg_price else title
            body += _email_channel_section_html(priced_title, links)
        if price:
            body += (
                "<div style='margin-top:6px;color:#666;font-size:12px;'>"
                f"\u5f80\u8fd4\u5408\u8ba1\u53c2\u8003:{_plan_roundtrip_price_text(plan)}</div>"
            )
        return body
    if is_roundtrip:
        heading = context_label or "\u53bb\u9a8c\u8bc1\u4ef7\u683c(\u9009\u62e9\u6e20\u9053)"
        price_text = f"\u7ea6{_plan_roundtrip_price_text(plan)}" if price else ""
        body = (
            "<div style='font-weight:600;margin-bottom:4px;'>"
            f"{html.escape(heading)}:\u6574\u5957\u5f80\u8fd4\u9a8c\u8bc1:{price_text}</div>"
        )
        body += "".join(_email_channel_section_html(title, links, price=price) for title, links in sections)
        if interactive:
            return (
                "<details style='margin:8px 0;'>"
                "<summary style='cursor:pointer;color:#2563eb;font-weight:600;'>\u53bb\u9a8c\u8bc1\u4ef7\u683c \u25be</summary>"
                f"<div style='margin-top:6px;'>{body}</div>"
                "</details>"
            )
        return body
    if interactive:
        body = "".join(_email_channel_section_html(title, links) for title, links in sections)
        return (
            "<details style='margin:8px 0;'>"
            "<summary style='cursor:pointer;color:#2563eb;font-weight:600;'>\u53bb\u9a8c\u8bc1\u4ef7\u683c \u25be</summary>"
            f"<div style='margin-top:6px;'>{body}</div>"
            "</details>"
        )
    body = "<div style='font-weight:600;margin-bottom:4px;'>" + html.escape(context_label or "\u53bb\u9a8c\u8bc1\u4ef7\u683c") + ":</div>"
    body += "".join(_email_channel_section_html(title, links) for title, links in sections)
    return body

def _email_channel_sections(plan: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    links = (plan or {}).get("links") or {}
    purchase_mode = str((plan or {}).get("purchase_mode") or "")
    is_roundtrip = bool((plan or {}).get("is_roundtrip"))
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    if is_roundtrip and "单程" in purchase_mode:
        for key, title in (("outbound", "去程"), ("return", "返程")):
            channel_links = _extract_primary_channel_links(links.get(key))
            if channel_links:
                sections.append((_pushplus_plan_flight_label(plan or {}, key), channel_links))
        return sections
    if is_roundtrip:
        combo_links = _extract_primary_channel_links(links.get("main") or links.get("outbound") or links.get("return"))
        return [("往返组合", combo_links)] if combo_links else []
    candidates = [
        ("", links.get("main")),
        ("", links.get("outbound")),
        ("", links.get("return")),
    ]
    seen: set[str] = set()
    for title, link_html in candidates:
        channel_links = [
            (label, href)
            for label, href in _extract_primary_channel_links(link_html)
            if not (href in seen or seen.add(href))
        ]
        if channel_links:
            sections.append((title, channel_links))
            if is_roundtrip:
                break
    return sections


def _active_airport_combo_count(payload: dict) -> int:
    route_airports = payload.get("route_airports")
    print(f"[机场对比调试] route_airports类型={type(route_airports)}, 值={repr(route_airports)[:300]}")

    def _to_list(value) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item or "").strip()]
        if isinstance(value, tuple):
            return [str(item).strip() for item in value if str(item or "").strip()]
        if isinstance(value, str) and value.strip():
            return [part for part in re.split(r"[、,，/\s]+", value.strip()) if part]
        return []

    route_airports_dict = route_airports if isinstance(route_airports, dict) else {}
    route_info = payload.get("route_info") or {}
    if not isinstance(route_info, dict):
        route_info = {}
    sub = payload.get("subscription") or payload.get("snapshot") or {}
    if not isinstance(sub, dict):
        sub = {}
    basic = sub.get("basic") or {}
    if not isinstance(basic, dict):
        basic = {}

    origins = (
        _to_list(route_info.get("origin_airports_active"))
        or _to_list(basic.get("origin_airports_active"))
        or _to_list(sub.get("origin_airports_active"))
        or _to_list(payload.get("origin_airports_active"))
        or _to_list(route_airports_dict.get("origins"))
        or _to_list(route_airports_dict.get("origin_airports_active"))
        or _to_list(route_airports_dict.get("origin_airports"))
        or _to_list(route_info.get("origin_airports"))
        or _to_list(payload.get("origin_airports"))
    )
    destinations = (
        _to_list(route_info.get("destination_airports_active"))
        or _to_list(basic.get("destination_airports_active"))
        or _to_list(sub.get("destination_airports_active"))
        or _to_list(payload.get("destination_airports_active"))
        or _to_list(route_airports_dict.get("destinations"))
        or _to_list(route_airports_dict.get("destination_airports_active"))
        or _to_list(route_airports_dict.get("destination_airports"))
        or _to_list(route_info.get("destination_airports"))
        or _to_list(payload.get("destination_airports"))
    )
    if not origins or not destinations:
        return 0
    return len(origins) * len(destinations)


def _should_show_airport_comparison(payload: dict) -> bool:
    try:
        comparison = payload.get("airport_cost_comparison")
        if isinstance(comparison, dict):
            comparison = comparison.get("rows") or []
        return bool(comparison)
    except Exception as exc:
        safe_log(f"[机场对比] 判断失败,默认不显示: {exc}")
        return False


def _airport_section_title(payload: dict) -> str:
    rows = payload.get("airport_cost_comparison") or []
    if isinstance(rows, dict):
        rows = rows.get("rows") or []
    pairs = {
        (
            str(item.get("departure_airport") or "").strip().upper(),
            str(item.get("arrival_airport") or item.get("airport") or "").strip().upper(),
        )
        for item in rows
        if isinstance(item, dict)
    }
    return "机场选择对比" if len(pairs) >= 2 else "机场参考"


def _non_price_change_reasons(payload: dict) -> list[str]:
    if _no_primary_plan_state(payload):
        return ["本次为'无符合方案'提醒,告知你当前约束下暂无匹配航班"]
    result = []
    status_text = _plan_status_change_text(payload)
    normalized_status = re.sub(r"[\s，。；：、,.;:]", "", status_text)
    for item in payload.get("trigger_reason") or []:
        text = str(item or "").strip()
        if not text:
            continue
        if "较上次提醒" in text or "上涨" in text or "下降" in text or "涨" in text or "降" in text:
            continue
        normalized_text = re.sub(r"[\s，。；：、,.;:]", "", text)
        if (
            normalized_status
            and len(normalized_text) >= 8
            and (normalized_text in normalized_status or normalized_status in normalized_text)
        ):
            continue
        if text not in result:
            result.append(text)
    return result


def _extract_primary_channel_links(link_html: str) -> list[tuple[str, str]]:
    wanted = ("携程", "飞猪", "去哪儿", "航司官网")
    found: list[tuple[str, str]] = []
    for href, label_html in re.findall(
        r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        str(link_html or ""),
        flags=re.I | re.S,
    ):
        label = re.sub(r"<[^>]+>", "", label_html)
        label = html.unescape(label).strip()
        href = html.unescape(href)
        for name in wanted:
            if name in label and name not in [item[0] for item in found]:
                found.append((name, href))
                break
    return found


def _email_channel_section_html(title: str, links: list[tuple[str, str]], price=None) -> str:
    if not links:
        return ""
    prefix = f"<div style='color:#666;font-size:12px;margin-top:4px;'>{html.escape(title)}</div>" if title else ""
    price_suffix = f" {_price_text(price)}" if price else ""
    rows = "".join(
        "<div style='font-size:13px;line-height:1.7;'>"
        f"- {html.escape(label + price_suffix)} → <a href=\"{html.escape(str(url))}\" target=\"_blank\">{html.escape(label)}</a>"
        "</div>"
        for label, url in links
    )
    return prefix + rows


def _email_button_link(label: str, url: str) -> str:
    return (
        f'<a href="{html.escape(str(url))}" target="_blank" '
        'style="display:inline-block;background:#2563eb;color:#fff;text-decoration:none;'
        'border-radius:5px;padding:6px 10px;margin:3px 4px 3px 0;font-size:13px;">'
        f"{html.escape(label)}</a>"
    )


def _email_primary_booking_url(plan: dict) -> str:
    links = plan.get("links") or {}
    for key in ("outbound", "main", "return"):
        href = _first_anchor_href(links.get(key))
        if href:
            return href
    return ""


def _first_anchor_href(link_html: str) -> str:
    match = re.search(r'<a\s+href="([^"]+)"', str(link_html or ""), flags=re.I)
    return html.unescape(match.group(1)) if match else ""


def _payload_has_meeting_context(payload: dict | None) -> bool:
    payload = payload or {}
    nested_sources = [
        payload,
        payload.get("constraints") or {},
        payload.get("subscription") or {},
        payload.get("snapshot") or {},
    ]
    for source in nested_sources:
        if not isinstance(source, dict):
            continue
        constraints = source.get("constraints") if isinstance(source.get("constraints"), dict) else {}
        for values in (source, constraints):
            if any(str(values.get(key) or "").strip() for key in ("meeting_start", "business_start")):
                return True
    return "按会议安排" in str(payload.get("time_filter_note") or "")


def _plan_for_render(plan: dict, payload: dict) -> dict:
    if not isinstance(plan, dict):
        return {}
    rendered = {**plan}
    if not rendered.get("feedback_url"):
        rendered["feedback_url"] = payload.get("feedback_url")
    if payload.get("route_type") and not rendered.get("route_type"):
        rendered["route_type"] = payload.get("route_type")
    if payload.get("invoice_preferences") and not rendered.get("invoice_preferences"):
        rendered["invoice_preferences"] = payload.get("invoice_preferences")
    if (
        not rendered.get("need_baggage")
        and _plan_lcc_summary(rendered).get("has_lcc")
    ):
        need_baggage = _preference_value(payload, None, "need_baggage")
        if need_baggage:
            rendered["need_baggage"] = need_baggage
    rendered["_meeting_context"] = _plan_has_meeting_context(rendered) or _payload_has_meeting_context(payload)
    return rendered


def _prepared_payload_plans(payload: dict, plans: list[dict]) -> list[dict]:
    rendered_plans = [
        _plan_for_render(plan, payload)
        for plan in (plans or [])
        if isinstance(plan, dict)
    ]
    return _apply_plan_tiers(rendered_plans)


def _plan_render_identity(plan: dict) -> tuple[str, str, str, str] | None:
    plan = plan or {}
    outbound = plan.get("outbound_flight") or plan.get("outbound") or plan.get("main_flight") or plan.get("flight") or {}
    ret = plan.get("return_flight") or plan.get("return") or {}

    def combo(flight):
        raw = str((flight or {}).get("flight_combo") or (flight or {}).get("flight_no") or "").strip()
        return normalize_combo(raw) if raw else ""

    def cabin(flight):
        return str(
            (flight or {}).get("cabin_class")
            or (flight or {}).get("cabin")
            or plan.get("cabin_class")
            or plan.get("cabin")
            or "economy"
        ).strip().lower()

    outbound_combo = combo(outbound)
    return_combo = combo(ret)
    if not outbound_combo and not return_combo:
        return None
    return outbound_combo, return_combo, cabin(outbound), cabin(ret) if ret else ""


def _compact_adjustment_reference(plan: dict) -> str:
    label = str(plan.get("label") or "方案").strip()
    details = []
    feasibility = plan.get("feasibility") or {}
    for key, direction in (("outbound", "去程"), ("return", "返程")):
        item = feasibility.get(key) if isinstance(feasibility, dict) else None
        if not isinstance(item, dict) or item.get("level") != "不可行":
            continue
        need_set_off = str(item.get("need_set_off") or "").strip()
        details.append(f"{direction}需{need_set_off}前动身" if need_set_off else f"{direction}需调整动身时间")
    detail = "；".join(details) or "需调整动身时间"
    return (
        "<div style='border:1px solid #fde68a;background:#fffbeb;padding:10px 12px;margin:8px 0;"
        "font-size:13px;color:#92400e;'>"
        f"<b>{html.escape(label)}(见上)</b>：{html.escape(detail)}"
        "</div>"
    )


def _assert_compact_has_no_full_card_title(markup: str, plans: list[dict]) -> None:
    for plan in plans or []:
        label = str((plan or {}).get("label") or "").strip()
        if label and f"{label} ｜" in markup:
            raise AssertionError(f"compact 输出含完整卡标题: {label}")


def _render_payload_plan_cards(
    payload: dict,
    plans: list[dict],
    primary_plan: dict,
    compact: bool = False,
    rendered_full_keys: set | None = None,
    render_stats: dict | None = None,
    section: str = "main",
) -> str:
    rendered_plans = _prepared_payload_plans(payload, plans)
    effective_primary = primary_plan or (rendered_plans[0] if rendered_plans else {})
    if compact:
        markup = "".join(_compact_adjustment_reference(plan) for plan in rendered_plans)
        _assert_compact_has_no_full_card_title(markup, rendered_plans)
        if render_stats is not None:
            render_stats["compact_refs"] = render_stats.get("compact_refs", 0) + len(rendered_plans)
        return markup

    seen = rendered_full_keys if rendered_full_keys is not None else set()
    parts = []
    for plan in rendered_plans:
        identity = _plan_render_identity(plan)
        if identity is not None and identity in seen:
            compact_markup = _compact_adjustment_reference(plan)
            _assert_compact_has_no_full_card_title(compact_markup, [plan])
            parts.append(compact_markup)
            if render_stats is not None:
                render_stats["compact_refs"] = render_stats.get("compact_refs", 0) + 1
            continue
        parts.append(_render_payload_plan_card(plan, compact=False, primary_plan=effective_primary))
        if identity is not None:
            seen.add(identity)
        if render_stats is not None:
            key = "full_main" if section == "main" else "full_adjustment"
            render_stats[key] = render_stats.get(key, 0) + 1
            full_counts = render_stats.setdefault("full_identity_counts", {})
            if identity is not None:
                full_counts[identity] = full_counts.get(identity, 0) + 1
    return "".join(parts)


def _plan_is_domestic(plan: dict) -> bool:
    if str(plan.get("route_type") or "") == "domestic":
        return True
    return any(str(flight.get("route_type") or "") == "domestic" for flight in _plan_flights(plan))


def _domestic_reference_price_line(plan: dict) -> str:
    flight = (_plan_flights(plan) or [{}])[0] or {}
    price = plan.get("price") or flight.get("price")
    bare = _to_float(flight.get("bare_price") or flight.get("ticket_price"))
    airport_tax = _to_float(flight.get("airport_tax"))
    fuel_tax = _to_float(flight.get("fuel_tax"))
    price_text = _price_text(price)
    if bare is not None and airport_tax is not None and fuel_tax is not None:
        return (
            f"{price_text}"
            f"（票面{_price_text(bare)}+机建{_price_text(airport_tax)}+燃油{_price_text(fuel_tax)}）"
        )
    note = str(flight.get("price_note") or flight.get("price_includes") or "实付以支付页为准").strip()
    return f"{price_text}（{html.escape(note)}）"


def _domestic_buyability_line(plan: dict) -> str:
    labels = []
    for flight in _plan_flights(plan):
        buyability = flight.get("buyability") or {}
        availability = flight.get("availability") or {}
        label = str(buyability.get("label") or availability.get("label") or "").strip()
        if label and label not in labels:
            labels.append(label)
    return " / ".join(labels) or "需支付页确认"


def _domestic_baggage_line(plan: dict) -> str:
    if plan.get("baggage_line"):
        return html.escape(str(plan.get("baggage_line")))
    parts = []
    for flight in _plan_flights(plan):
        baggage = ((flight.get("fare_rules") or {}).get("baggage") or {})
        note = str(baggage.get("note") or "").strip()
        if baggage.get("included") is True and baggage.get("checked_kg"):
            note = note or f"含{baggage.get('checked_kg')}kg托运"
        elif baggage.get("included") is False:
            note = note or "托运需另购"
        if note and note not in parts:
            parts.append(note)
    return html.escape(" / ".join(parts) or "支付页需确认")


def _domestic_refund_line(plan: dict) -> str:
    refund_line = _plan_refund_line(plan)
    if refund_line:
        return refund_line
    parts = []
    for flight in _plan_flights(plan):
        refund = ((flight.get("fare_rules") or {}).get("refund") or {})
        text = "，".join(
            item
            for item in [str(refund.get("label") or "").strip(), str(refund.get("note") or "").strip()]
            if item
        )
        if text and text not in parts:
            parts.append(text)
    return html.escape(" / ".join(parts) or "支付页确认")


def _render_domestic_payload_plan_card(plan: dict, compact: bool = False, primary_plan: dict | None = None) -> str:
    label = str(plan.get("label", "方案"))
    tier = str(plan.get("tier") or plan.get("variant") or "首选推荐").split(":", 1)[0].strip()
    if tier == "推荐":
        tier = "首选推荐"
    title = html.escape(f"国内航班推荐卡 · {label} ｜ {tier}".strip(" ｜"))
    body_parts: list[str] = [_plan_tradeoff_summary_html(plan, primary_plan)]
    if plan.get("is_roundtrip"):
        body_parts.append(
            _email_plan_leg_group("━━ 去程 ━━", plan.get("outbound_flight"), str(plan.get("outbound_line") or ""))
        )
        body_parts.append(
            _email_plan_price_group([("去程票价", _plan_leg_price_text(plan, plan.get("outbound_price")))])
        )
        body_parts.append(
            _email_plan_leg_group("━━ 返程 ━━", plan.get("return_flight"), str(plan.get("return_line") or ""))
        )
        body_parts.append(
            _email_plan_price_group([("返程票价", _plan_leg_price_text(plan, plan.get("return_price")))])
        )
    else:
        main_flight = plan.get("main_flight") or plan.get("outbound_flight") or plan.get("flight")
        body_parts.append(_email_plan_leg_group("去程", main_flight, str(plan.get("summary") or "")))

    links = plan.get("links") or {}
    link_value = links.get("main") or links.get("outbound") or ""
    if isinstance(link_value, dict):
        link_value = " | ".join(str(item) for item in link_value.values() if item)
    rows = []
    if plan.get("is_roundtrip"):
        body_parts.append(
            '<div style="font-weight:600;color:#111;margin:12px 0 6px;'
            'background:#f5f7fa;padding:4px 8px;border-radius:4px;">━━ 合计 ━━</div>'
        )
        rows.extend(
            _passenger_pricing_rows(plan)
            or [
                (
                    "往返总价",
                    f"{_plan_roundtrip_price_text(plan)}"
                    f"(去程{_plan_leg_price_text(plan, plan.get('outbound_price'))} + "
                    f"返程{_plan_leg_price_text(plan, plan.get('return_price'))})",
                )
            ]
        )
        if _passenger_pricing_applies(plan.get("passenger_pricing")):
            rows.append(
                (
                    "单人单段参考",
                    f"去程{_plan_leg_price_text(plan, plan.get('outbound_price'))} + 返程{_plan_leg_price_text(plan, plan.get('return_price'))}",
                )
            )
        rows.extend(
            [
                ("预估实付", _price_text_with_caliber(plan.get("estimated_price"), "all_passengers_roundtrip" if _passenger_pricing_applies(plan.get("passenger_pricing")) and plan.get("is_roundtrip") else ("all_passengers_oneway" if _passenger_pricing_applies(plan.get("passenger_pricing")) else ("per_person_roundtrip" if plan.get("is_roundtrip") else "per_person_oneway")), *_plan_price_context(plan))),
                ("购票方式", html.escape(str(plan.get("purchase_mode") or "待确认"))),
            ]
        )
    rows.extend([
        ("实时含税价", _domestic_reference_price_line(plan)),
        ("库存状态", html.escape(_domestic_buyability_line(plan))),
        ("行李", _domestic_baggage_line(plan)),
        ("退改签", _domestic_refund_line(plan)),
    ])
    lcc_baggage_warning = _plan_lcc_baggage_warning(plan)
    if lcc_baggage_warning:
        rows.append(("廉航行李提醒", html.escape(lcc_baggage_warning)))
    invoice_line = _invoice_reimbursement_line(plan)
    if invoice_line:
        rows.append(("开票/报销", invoice_line))
    punctuality_line = _plan_punctuality_line(plan)
    if punctuality_line:
        rows.append(("准点率", punctuality_line))
    effective_cost_line = _plan_effective_cost_line(plan)
    if effective_cost_line:
        rows.append(("有效出行成本", effective_cost_line))
    logistics_line = _plan_logistics_line(plan)
    if logistics_line:
        rows.append(("机场交通", logistics_line))
    channel_advice = _plan_channel_purchase_advice(plan)
    if channel_advice:
        rows.append(("渠道建议", channel_advice))
    if link_value:
        rows.append(("渠道", _layered_channel_links(str(link_value)) or str(link_value)))
    if plan.get("tags"):
        rows.append(("标签", html.escape(str(plan.get("tags")))))
    business_feasibility = _business_feasibility_text(plan)
    if business_feasibility:
        rows.append(("到会/返程安全", html.escape(business_feasibility)))
    feasibility_line = _plan_feasibility_line(plan)
    if feasibility_line:
        rows.append(("可行性分析", feasibility_line))
    source_label = _plan_source_label(plan)
    if source_label:
        rows.append(("数据来源", html.escape(source_label)))
    if not compact:
        rows.append(("操作建议", f'<span style="color:#16a34a;">{html.escape(str(plan.get("buy_condition") or "以支付页为准"))}</span>'))
    body_parts.append(_email_plan_price_group(rows))
    return _email_card(title, "".join(body_parts), _plan_card_style(plan, tier))


def _plan_source_label(plan: dict) -> str:
    def source_name(source) -> str:
        key = str(source or "").strip().lower()
        if key in {"serpapi", "searchapi", "hasdata"}:
            return "Google Flights"
        if key == "juhe":
            return "OTA(聚合)"
        if key == "duffel":
            return "Duffel"
        return key or "待确认"

    def source_facts(flight: dict) -> tuple[str, str]:
        structure_source = flight.get("primary_source") or flight.get("source")
        if not structure_source:
            merged_sources = [
                part.strip()
                for part in str(flight.get("data_source") or "").split("+")
                if part.strip()
            ]
            structure_source = merged_sources[0] if len(merged_sources) == 1 else None
        return source_name(structure_source), source_name(flight.get("price_source"))

    outbound = plan.get("outbound_flight") or plan.get("main_flight") or plan.get("flight")
    inbound = plan.get("return_flight")
    legs = [flight for flight in (outbound, inbound) if isinstance(flight, dict) and flight]
    if not legs:
        return ""

    facts = [source_facts(flight) for flight in legs]
    structures = [structure for structure, _ in facts]
    selected_prices = [selected for _, selected in facts]
    same_structure = len(set(structures)) == 1
    same_selected = len(set(selected_prices)) == 1

    if same_structure and same_selected:
        if structures[0] == selected_prices[0]:
            return f"结构与入池:{structures[0]}"
        return f"航班结构:{structures[0]} / 入池价:{selected_prices[0]}"

    if len(facts) == 1:
        structure, selected = facts[0]
        return f"航班结构:{structure} / 入池价:{selected}"

    parts = []
    if same_structure:
        parts.append(f"航班结构:{structures[0]}")
    else:
        parts.extend((f"去程结构:{structures[0]}", f"返程结构:{structures[1]}"))
    parts.extend((f"去程入池:{selected_prices[0]}", f"返程入池:{selected_prices[1]}"))
    return " / ".join(parts)


def _action_range_display_text(row: dict) -> str:
    text = row.get("text")
    if not text:
        left = "-∞" if row.get("min") is None else _price_text(row.get("min"))
        right = "+∞" if row.get("max") is None else _price_text(row.get("max"))
        text = f"{left} - {right}"
    return f"{text}：{row.get('label')}（你的设置）"


FULL_SERVICE_AIRLINE_CODES = {
    "CA",
    "MU",
    "CZ",
    "HU",
    "MF",
    "ZH",
    "SC",
    "3U",
    "FM",
    "GJ",
    "EU",
}
LCC_AIRLINE_CODES = {"9C", "HO", "PN", "KN", "BK", "JD", "GS", "MM", "TR", "AK"}


def _plan_airline_codes(plan: dict) -> list[str]:
    codes: list[str] = []
    for flight in _plan_flights(plan):
        code = _first_airline_code(flight)
        if code and code not in codes:
            codes.append(code)
    return codes


def _plan_channel_purchase_advice(plan: dict) -> str:
    codes = _plan_airline_codes(plan)
    if not codes:
        return "各渠道价格和服务费可能不同，以支付页最终价为准。"
    if any(code in FULL_SERVICE_AIRLINE_CODES for code in codes):
        focus = "重视售后/报销：优先航司官网；只看价格：再验证携程、飞猪、去哪儿。"
    elif any(code in LCC_AIRLINE_CODES for code in codes):
        focus = "廉航/特价方案：优先验证携程、飞猪、去哪儿低价，同时确认托运行李和服务费。"
    else:
        focus = "建议先验证携程、飞猪、去哪儿；如官网价格接近，售后/报销优先官网。"
    return html.escape(focus + " 价格以各平台支付页为准。")


def _plan_refund_line(plan: dict) -> str:
    lines = []
    international_route = _plan_route_type(plan) in {"international", "greater_china"}
    for flight in _plan_flights(plan):
        fare_rules = flight.get("fare_rules") or {}
        refund = fare_rules.get("refund") or {}
        if refund.get("label") or refund.get("note"):
            text = refund.get("note") or refund.get("label")
            if refund.get("label") and refund.get("note"):
                text = f"{refund.get('label')}，{refund.get('note')}"
            if text and text not in lines:
                lines.append(str(text))
        source_note = fare_rules.get("source_note")
        if international_route and source_note and "国内标准规则推断" in str(source_note):
            source_note = str(source_note).replace("国内标准规则推断", "标准规则推断(国际线)")
        if source_note and source_note not in lines:
            lines.append(str(source_note))
    return "<br>".join(html.escape(item) for item in lines[:3])


def _plan_punctuality_line(plan: dict) -> str:
    parts = []
    flights = _plan_flights(plan)
    for index, flight in enumerate(flights):
        punctuality = flight.get("punctuality") or {}
        level = str(punctuality.get("level") or "").strip()
        if not level:
            continue
        note = str(punctuality.get("note") or "准点率参考，实际以航班动态为准").strip()
        factors = [str(item).strip() for item in punctuality.get("risk_factors") or [] if str(item).strip()]
        text = f"{level}（{note}）"
        if factors:
            text += "；" + "；".join(factors[:2])
        if plan.get("is_roundtrip"):
            direction = "去程" if index == 0 else "返程"
            text = f"{direction}:{text}"
        if text not in parts:
            parts.append(text)
    return "；".join(html.escape(item) for item in parts[:2])


def _plan_effective_cost_line(plan: dict) -> str:
    values = []
    components = {"ticket_price": 0.0, "transport_cost": 0.0, "time_cost": 0.0, "baggage_cost": 0.0}
    component_seen = {key: False for key in components}
    notes = []
    for flight in _plan_flights(plan):
        effective = flight.get("effective_cost") or {}
        value = _to_float(effective.get("effective_cost"))
        if value is None:
            continue
        values.append(value)
        for key in components:
            component_value = _to_float(effective.get(key))
            if component_value is not None:
                components[key] += component_value
                component_seen[key] = True
        if effective.get("breakdown_note"):
            notes.append(str(effective.get("breakdown_note")))
    if not values:
        return ""
    if plan.get("is_roundtrip") and len(values) > 1:
        total = sum(values)
        ticket = components["ticket_price"] if component_seen["ticket_price"] else None
        if ticket is None:
            ticket = _to_float(
                (plan.get("price_tiers") or {}).get("unit_roundtrip")
                or plan.get("single_adult_price")
                or plan.get("price")
            )
        text = f"约{_price_text(total)}"
        detail_parts = []
        if ticket is not None:
            detail_parts.append(f"机票{_price_text(ticket)}(单人往返)")
        if component_seen["transport_cost"]:
            detail_parts.append(f"机场交通约{_price_text(components['transport_cost'])}")
        if component_seen["time_cost"]:
            detail_parts.append(f"时间成本约{_price_text(components['time_cost'])}")
        if component_seen["baggage_cost"] and components["baggage_cost"]:
            detail_parts.append(f"行李约{_price_text(components['baggage_cost'])}")
        if detail_parts:
            text += "=" + "+".join(detail_parts)
    else:
        text = f"约{_price_text(values[0])}"
    notes = list(dict.fromkeys(notes))
    if notes and not (plan.get("is_roundtrip") and len(values) > 1):
        text += "：" + "；".join(notes[:2])
    text += "。参考性综合估算，非精确费用。"
    return html.escape(text)


def _plan_logistics_line(plan: dict) -> str:
    notes = []
    for flight in _plan_flights(plan):
        for note in flight.get("logistics_notes") or []:
            text = str(note).strip()
            if text and text not in notes:
                notes.append(text)
    return "<br>".join(html.escape(item) for item in notes[:3])


def _detail_section(title: str, body: str, open_by_default: bool = False) -> str:
    open_attr = " open" if open_by_default else ""
    return (
        f'<details{open_attr} style="{EMAIL_CARD_STYLE}">'
        f'<summary style="{EMAIL_CARD_TITLE_STYLE}cursor:pointer;">{html.escape(title)}</summary>'
        f'<div style="{EMAIL_CARD_BODY_STYLE}">{body}</div>'
        "</details>"
    )


def _source_stat_count(source_stats: dict, names: tuple[str, ...]) -> int:
    count = 0
    for name in names:
        value = (source_stats or {}).get(name)
        if not isinstance(value, dict):
            continue
        if not _source_stat_is_usable(value.get("status", "")):
            continue
        try:
            count += int(value.get("count") or 0)
        except (TypeError, ValueError):
            continue
    return count


def _source_stats_route_type(source_stats: dict | None) -> str:
    for value in (source_stats or {}).values():
        if isinstance(value, dict) and value.get("route_type"):
            return str(value.get("route_type"))
    return ""


def _email_source_rows(payload: dict) -> list[str]:
    source_stats = payload.get("source_stats") or {}
    route_type = str(payload.get("route_type") or _source_stats_route_type(source_stats) or "")
    juhe_count = _source_stat_count(source_stats, ("juhe",))
    hasdata_count = _source_stat_count(source_stats, ("hasdata",))
    other_google_count = _source_stat_count(source_stats, ("serpapi", "searchapi"))
    duffel_count = _source_stat_count(source_stats, ("duffel",))
    rows = []
    retirement = payload.get("source_retirement") or {}

    if route_type == "international" and retirement.get("active"):
        rows.append(f"<div>🔹 主源:聚合数据(OTA)—{juhe_count}个方案</div>")
        rows.append(f"<div>🔹 入池价:按全局最低({html.escape(MERGE_PRICE_STRATEGY)})</div>")
        if duffel_count:
            rows.append(f"<div>🔹 行李/退改:Duffel 规则参考 — {duffel_count}条</div>")
        else:
            rows.append("<div>🔹 行李/退改:Duffel 规则参考</div>")
        rows.append(
            "<div style='margin-top:6px;color:#666;font-size:12px;'>"
            "说明:国际航线按当前源策略以聚合数据为搜索源,价格以平台支付页为准。"
            "</div>"
        )
        return rows

    if route_type == "domestic":
        rows.append(f"<div>🔹 主源:聚合数据(Juhe)—{juhe_count}个方案</div>")
        if hasdata_count or other_google_count:
            rows.append(
                f"<div>🔹 额外交叉来源:Google Flights—{hasdata_count + other_google_count}个方案</div>"
            )
        rows.append(f"<div>🔹 入池价:按全局最低({html.escape(MERGE_PRICE_STRATEGY)})</div>")
        if duffel_count:
            rows.append(f"<div>🔹 行李/退改:Duffel 规则参考 — {duffel_count}条</div>")
        else:
            rows.append("<div>🔹 行李/退改:Duffel 规则参考</div>")
        rows.append(
            "<div style='margin-top:6px;color:#666;font-size:12px;'>"
            "说明:国内航线按当前源策略以聚合数据为搜索源,价格以平台支付页为准。"
            "</div>"
        )
        return rows

    rows.append(f"<div>🔹 主源:Google Flights(HasData)—{hasdata_count}个方案</div>")
    rows.append(f"<div>🔹 交叉/OTA:聚合数据—{juhe_count}个方案</div>")
    rows.append(f"<div>🔹 入池价:按全局最低({html.escape(MERGE_PRICE_STRATEGY)})</div>")
    if duffel_count:
        rows.append(f"<div>🔹 行李/退改:Duffel 规则参考 — {duffel_count}条</div>")
    else:
        rows.append("<div>🔹 行李/退改:Duffel 规则参考</div>")
    route_label = "港澳台" if route_type == "greater_china" else "国际"
    rows.append(
        "<div style='margin-top:6px;color:#666;font-size:12px;'>"
        f"说明:{route_label}航线保留双源原始报价,候选入池按全局最低价,最终以支付页为准。"
        "</div>"
    )
    return rows


def _freshness_source_name(source) -> str:
    key = str(source or "").strip().lower()
    return {
        "hasdata": "HasData",
        "juhe": "聚合数据",
        "duffel": "Duffel",
        "serpapi": "SerpAPI",
        "searchapi": "SearchAPI",
    }.get(key, key or "来源待确认")


def _freshness_timestamp(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(SHANGHAI_TZ)
    return parsed


def _data_freshness_legs(payload: dict) -> list[dict]:
    freshness = (
        payload.get("data_freshness")
        or (payload.get("route_info") or {}).get("data_freshness")
        or {}
    )
    legs = freshness.get("legs") if isinstance(freshness, dict) else []
    result = []
    seen = set()
    for item in legs or []:
        if not isinstance(item, dict):
            continue
        normalized = {
            "direction": str(item.get("direction") or "航段"),
            "source": str(item.get("source") or "").strip().lower(),
            "state": str(item.get("state") or "").strip().lower(),
            "collected_at": item.get("collected_at"),
            "origin": str(item.get("origin") or "").strip().upper(),
            "destination": str(item.get("destination") or "").strip().upper(),
            "depart_date": str(item.get("depart_date") or "")[:10],
        }
        key = tuple(normalized.values())
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _freshness_state_text(item: dict) -> str:
    state = str(item.get("state") or "").lower()
    collected = _freshness_timestamp(item.get("collected_at"))
    time_text = collected.strftime("%H:%M") if collected else ""
    if state == "panel_reused":
        return f"面板复用{time_text}"
    if state in {"fresh", "realtime"}:
        return f"实时采集{time_text}"
    if state in {"cache", "cache_reused"}:
        return f"缓存复用{time_text}"
    if state in {"panel_missing", "skipped", "skipped_panel_only"}:
        return "今日未采"
    return str(item.get("state") or "时点待确认")


def _data_freshness_source_rows(payload: dict) -> list[str]:
    rows = []
    for item in _data_freshness_legs(payload):
        route = ""
        if item.get("origin") and item.get("destination"):
            route = f"({item['origin']}→{item['destination']})"
        rows.append(
            "<div>🔹 "
            f"{html.escape(item['direction'])}:"
            f"{html.escape(_freshness_source_name(item.get('source')))} "
            f"{html.escape(_freshness_state_text(item))}"
            f"{html.escape(route)}</div>"
        )
    return rows


def _data_freshness_headline(payload: dict) -> str:
    reused = [
        (parsed, item)
        for item in _data_freshness_legs(payload)
        if item.get("state") == "panel_reused"
        and (parsed := _freshness_timestamp(item.get("collected_at"))) is not None
    ]
    if not reused:
        return ""
    oldest, _item = min(reused, key=lambda pair: pair[0])
    return f"数据时点:含面板复用，最旧为{oldest.strftime('%Y-%m-%d %H:%M')}"


def _email_source_body(payload: dict) -> str:
    rows = _email_source_rows(payload)
    rows.extend(_data_freshness_source_rows(payload))
    rows.append("<div>🔹 候选方案:已去重并筛选</div>")
    agreement = payload.get("dual_source_agreement") or {}
    agreement_window = agreement.get("window") or [None, None]
    window_text = ""
    if len(agreement_window) >= 2 and agreement_window[0] and agreement_window[1]:
        window_text = f"（窗口{agreement_window[0]}~{agreement_window[1]}）"
    rows.append(
        "<div>🔹 双源历史一致度:"
        f"{html.escape(format_dual_source_agreement(agreement))}"
        f"{html.escape(window_text)}</div>"
    )
    degradation = payload.get("source_degradation") or {}
    degradation_reason = str(degradation.get("reason") or "").strip()
    if degradation_reason:
        rows.append(
            "<div style='margin-top:6px;color:#b45309;font-size:13px;'>"
            f"⚠ {html.escape(degradation_reason)}"
            "</div>"
        )
    retirement = payload.get("source_retirement") or {}
    retirement_notice = str(retirement.get("notice") or "").strip()
    if retirement_notice:
        rows.append(
            "<div style='margin-top:6px;color:#666;font-size:13px;'>"
            f"{html.escape(retirement_notice)}"
            "</div>"
        )
    rows.append(f"<div style='margin-top:8px;color:#666;font-size:12px;'>采集时间:{html.escape(_payload_freshness_text(payload))}</div>")
    rows.append("<div style='color:#666;font-size:12px;'>说明:技术明细见网页详情页,价格以平台支付页为准。</div>")
    return "".join(rows)


def _compact_source_summary_lines(source_stats: dict | None) -> list[str]:
    if not source_stats:
        return []
    route_type = _source_stats_route_type(source_stats)
    retirement = {
        "active": bool(retired_listing_sources(route_type)),
        "notice": "",
    }
    body = _email_source_body(
        {
            "source_stats": source_stats,
            "route_type": route_type,
            "source_retirement": retirement,
        }
    )
    return ["<b>数据来源</b>", body]


def _email_technical_source_body(payload: dict) -> str:
    source_stats = payload.get("source_stats") or {}
    if not source_stats:
        return _email_source_body(payload)
    rows = []
    for key, value in source_stats.items():
        if isinstance(value, dict):
            status = value.get("status", "")
            count = value.get("count", "")
            rows.append(
                f"<div>- {html.escape(str(key))}: count={html.escape(str(count))}, status={html.escape(str(status))}</div>"
            )
        else:
            rows.append(f"<div>- {html.escape(str(key))}: {html.escape(str(value))}</div>")
    return _email_source_body(payload) + "<hr style='border:0;border-top:1px solid #eee;margin:10px 0;'>" + "".join(rows)


def _detail_technical_source_body(payload: dict) -> str:
    """详情页专用：在既有来源明细尾部追加本地配额概览。"""
    return (
        _email_technical_source_body(payload)
        + "<hr style='border:0;border-top:1px solid #eee;margin:10px 0;'>"
        + f"<div>{html.escape(_quota_overview_text())}</div>"
    )


def _quota_overview_text() -> str:
    """只读现有台账；与轮末日志共用同一格式函数。"""
    from api_usage import DEFAULT_USAGE_PATH, format_quota_overview, load_usage
    from collection_plan import load_collection_settings

    settings = load_collection_settings(BASE_DIR / "config.yaml")
    return format_quota_overview(
        load_usage(DEFAULT_USAGE_PATH),
        settings.get("source_quota_budget") or {},
    )

def _provenance_value_text(stat_key: str, value) -> str:
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, (int, float)):
        if str(stat_key).startswith("price_signal."):
            return f"{value:g}%" if isinstance(value, float) else f"{value}%"
        return f"CNY{float(value):,.2f}".rstrip("0").rstrip(".")
    return str(value)


def _mark_provenance_reference(payload: dict, stat_key: str) -> dict:
    """在统计值真正进入渲染时登记引用，并让缺口立即可见。"""
    provenance = payload.setdefault("provenance", {})
    referenced = provenance.setdefault("referenced_stat_keys", [])
    if stat_key not in referenced:
        referenced.append(stat_key)
        referenced.sort()
    entry = (provenance.get("statistics") or {}).get(stat_key)
    if isinstance(entry, dict):
        return entry
    missing_logged = provenance.setdefault("missing_stat_keys", [])
    if stat_key not in missing_logged:
        missing_logged.append(stat_key)
        safe_log(f"[依据缺失] stat={stat_key}")
    return {}


def _price_signal_summary_with_provenance(payload: dict) -> str:
    signal = payload.get("price_signal") or {}
    summary = str(signal.get("summary") or "")
    statistics = ((payload.get("provenance") or {}).get("statistics") or {})
    has_envelope = "price_signal.history_position" in statistics
    if signal.get("percentile") is None and not int(signal.get("sample_n") or 0) and not has_envelope:
        return summary
    envelope = _mark_provenance_reference(payload, "price_signal.history_position")
    return replace_micro_provenance(summary, envelope) if envelope else summary


def _detail_provenance_body(payload: dict) -> str:
    provenance = payload.get("provenance") or {}
    statistics = provenance.get("statistics") or {}
    referenced = provenance.get("referenced_stat_keys") or []
    if not referenced:
        return "<div style='color:#888;font-size:12px;'>本次未引用历史统计值。</div>"

    rows = []
    for stat_key in referenced:
        entry = _mark_provenance_reference(payload, str(stat_key))
        if not entry:
            rows.append((str(stat_key), "<span style='color:#b91c1c;'>依据缺失</span>"))
            continue
        window = entry.get("window") or [None, None]
        window_text = "~".join(str(item or "未标明") for item in window[:2])
        sources = "+".join(entry.get("sources") or []) or "未标明"
        agreement = format_dual_source_agreement(entry.get("dual_source_agreement"))
        value_text = _provenance_value_text(str(stat_key), entry.get("value"))
        detail = (
            f"值={value_text}<br>"
            f"版本={entry.get('method_version') or '未标明'} · n={int(entry.get('sample_n') or 0)} · "
            f"窗口={window_text}<br>"
            f"源={sources} · 剔除退化={int(entry.get('degraded_excluded') or 0)} · "
            f"一致度={agreement}<br>"
            f"桶={entry.get('bucket') or '未标明'}"
        )
        rows.append((str(stat_key), html.escape(detail).replace("&lt;br&gt;", "<br>")))
    return _email_table(rows)


def _source_stat_is_usable(status: str) -> bool:
    text = str(status or "").lower()
    blocked = ("失败", "error", "fail", "429", "timeout", "超时", "异常")
    return not any(item in text for item in blocked)


def _display_channel_price_rows(payload: dict) -> list[dict]:
    rows = payload.get("channel_price_rows") or []
    is_roundtrip = bool(payload.get("is_roundtrip"))
    leg_rows = []
    legacy_rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = _to_float(row.get("value") or row.get("price"))
        if value is None or value <= 0:
            continue
        if row.get("direction") and row.get("flight_combo") and row.get("provider"):
            item = dict(row)
            item["value"] = value
            item["scope"] = "oneway"
            leg_rows.append(item)
            continue
        row_scope = _chart_scope_label(row)
        if is_roundtrip:
            if row_scope != "往返":
                continue
        elif row_scope == "往返":
            continue
        item = dict(row)
        item["value"] = value
        if not item.get("scope"):
            item["scope"] = "oneway"
        legacy_rows.append(item)

    if leg_rows:
        deduped_by_key = {}
        for row in leg_rows:
            key = (
                str(row.get("direction") or ""),
                normalize_combo(row.get("flight_combo") or ""),
                str(row.get("provider") or ""),
            )
            current = deduped_by_key.get(key)
            if current is None or float(row["value"]) < float(current["value"]):
                deduped_by_key[key] = row

        provider_sets: dict[tuple[str, str], set[str]] = {}
        for key, row in deduped_by_key.items():
            group_key = key[:2]
            provider_sets.setdefault(group_key, set()).add(str(row.get("provider") or ""))
        qualified_groups = {
            key for key, providers in provider_sets.items() if len(providers) >= 2
        }
        direction_order = {"outbound": 0, "return": 1, "main": 0}
        return sorted(
            [
                row
                for key, row in deduped_by_key.items()
                if key[:2] in qualified_groups
            ],
            key=lambda row: (
                direction_order.get(str(row.get("direction") or ""), 9),
                str(row.get("flight_combo") or ""),
                _SOURCE_CHANNEL_ORDER.get(str(row.get("provider") or ""), 9),
            ),
        )

    deduped = _dedupe_chart_rows(legacy_rows)
    if len(deduped) < 2:
        return []
    return deduped


def _channel_cny_text(value) -> str:
    number = _to_float(value)
    if number is None:
        return "CNY待确认"
    if float(number).is_integer():
        return f"CNY{number:,.0f}"
    return f"CNY{number:,.2f}".rstrip("0").rstrip(".")


def _source_channel_comparison_lines(payload: dict) -> list[str]:
    rows = [row for row in _display_channel_price_rows(payload) if row.get("direction")]
    if not rows:
        return []

    anomaly_by_leg = {}
    for anomaly in payload.get("dual_source_price_anomalies") or []:
        if not isinstance(anomaly, dict) or not _should_disclose_source_price_gap(anomaly):
            continue
        key = (
            str(anomaly.get("direction") or ""),
            normalize_combo(anomaly.get("flight_combo") or ""),
        )
        anomaly_by_leg[key] = anomaly

    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = (
            str(row.get("direction") or ""),
            normalize_combo(row.get("flight_combo") or ""),
        )
        groups.setdefault(key, []).append(row)

    direction_order = {"outbound": 0, "return": 1, "main": 0}
    direction_labels = {"outbound": "去程", "return": "返程", "main": "去程"}
    lines = []
    for (direction, combo), group in sorted(
        groups.items(),
        key=lambda item: (direction_order.get(item[0][0], 9), item[0][1]),
    ):
        ordered = sorted(
            group,
            key=lambda row: _SOURCE_CHANNEL_ORDER.get(str(row.get("provider") or ""), 9),
        )
        prices = " / ".join(
            f"{row.get('provider')} {_channel_cny_text(row.get('value'))}"
            for row in ordered
        )
        selected_provider = next(
            (str(row.get("provider")) for row in ordered if row.get("selected")),
            "",
        )
        selected_text = f"(入池{selected_provider})" if selected_provider else ""
        gap_warning = ""
        if anomaly_by_leg.get((direction, combo)):
            gap_warning = " ⚠ 渠道价差>15%，运价条款可能不同，以支付页为准"
        lines.append(
            f"{direction_labels.get(direction, direction)} {combo}:{prices}{selected_text}{gap_warning}"
        )
    return lines


def _email_source_channel_price_body(payload: dict) -> str:
    lines = _source_channel_comparison_lines(payload)
    if not lines:
        return ""
    body = "".join(f"<div>{html.escape(line)}</div>" for line in lines)
    return (
        body
        + "<div style='margin-top:8px;color:#666;font-size:12px;'>"
        "以上均为同一航段的单人单程含税参考价；入池表示合并候选采用的价格来源，最终以支付页为准。"
        "</div>"
    )


def _pushplus_source_channel_price_lines(payload: dict) -> list[str]:
    lines = _source_channel_comparison_lines(payload)
    if not lines:
        return []
    return ["首选方案A渠道价(单人单程):", *(html.escape(line) for line in lines)]


def _email_detail_charts_body(payload: dict) -> str:
    parts = []
    if payload.get("nearby_date_prices"):
        title, note = _nearby_date_chart_title_and_note(payload["nearby_date_prices"])
        parts.append(_payload_bar_html(title, payload["nearby_date_prices"]))
        if note:
            parts.append(f"<div style='color:#666;font-size:12px;'>{html.escape(note)}</div>")
    channel_rows = _display_channel_price_rows(payload)
    if channel_rows and not any(row.get("direction") for row in channel_rows):
        parts.append(_payload_bar_html("不同渠道报价对比", channel_rows))
    if payload.get("plan_price_rows"):
        parts.append(_payload_bar_html("方案价格对比", payload["plan_price_rows"]))
    return "<br>".join(part for part in parts if part) or "<div style='color:#888;font-size:12px;'>暂无更多图表数据。</div>"


def _nearby_date_chart_title_and_note(rows: list[dict]) -> tuple[str, str]:
    scopes = {_chart_scope_label(row) for row in rows or []}
    scopes.discard("")
    if scopes == {"往返"}:
        return "前后日期最低价(往返组合参考价)", ""
    return "前后日期最低价(单程参考价)", "注:为单程价,非往返总价"


def _single_leg_rejection_direction(item: dict) -> str:
    direction = str(item.get("direction") or item.get("scope") or "").strip().lower()
    if direction in {"return", "inbound", "返程"}:
        return "返程"
    return "去程"


def _email_single_leg_rejection_table(rows: list[dict]) -> str:
    if not rows:
        return ""
    body_rows = []
    for item in rows[:10]:
        flight = _no_result_notification_flight(item)
        combo = normalize_combo(flight.get("flight_combo") or flight.get("flight_no") or "") or "航班待确认"
        direction = _single_leg_rejection_direction(item)
        price = _to_float(item.get("price") or flight.get("price"))
        price_text = _price_text(price) if price is not None else "价格待确认"
        details = _excluded_reason_details(item)
        reason = details[0] if details else str(item.get("reason") or "不满足当前约束")
        body_rows.append(
            "<tr>"
            f"<td style='padding:6px;border-bottom:1px solid #eee;'>{html.escape(combo)}</td>"
            f"<td style='padding:6px;border-bottom:1px solid #eee;'>{html.escape(direction)}</td>"
            f"<td style='padding:6px;border-bottom:1px solid #eee;'>{html.escape(price_text)}(单人单程)</td>"
            f"<td style='padding:6px;border-bottom:1px solid #eee;'>{html.escape(reason)}</td>"
            "</tr>"
        )
    return (
        "<div style='font-weight:600;margin-bottom:8px;color:#111;'>逐航班拒因表</div>"
        "<table style='width:100%;font-size:13px;border-collapse:collapse;'>"
        "<thead><tr>"
        "<th style='text-align:left;padding:6px;'>航班</th>"
        "<th style='text-align:left;padding:6px;'>方向</th>"
        "<th style='text-align:left;padding:6px;'>价格</th>"
        "<th style='text-align:left;padding:6px;'>拒因</th>"
        "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table>"
    )

def _email_excluded_compact_body(payload: dict) -> str:
    excluded = payload.get("excluded_plans") or []
    if not excluded:
        single_leg_rejections = payload.get("single_leg_rejections") or []
        if single_leg_rejections:
            return _email_single_leg_rejection_table(single_leg_rejections)
        if _no_primary_plan_state(payload):
            from notification_sections import section_fallback

            return (
                "<div style='color:#888;font-size:12px;'>"
                f"{html.escape(section_fallback('excluded_plans', '分析层未保留结构化排除候选'))}</div>"
            )
        return "<div style='color:#888;font-size:12px;'>暂无被排除的更低价方案。</div>"

    is_roundtrip = bool(payload.get("is_roundtrip"))
    current_price = _to_float(payload.get("current_price"))
    candidates = []
    for item in excluded:
        if not isinstance(item, dict):
            continue
        price = _excluded_item_price(item)
        if price is None:
            continue
        scope = _excluded_scope(item, is_roundtrip)
        if scope == "roundtrip" and current_price is not None and price >= current_price:
            continue
        candidates.append((price, item))

    if not candidates:
        return "<div style='color:#888;font-size:12px;'>暂无被排除的更低价方案。</div>"

    shown_candidates = sorted(candidates, key=lambda row: row[0])[:3]
    shown_items = [item for _price, item in shown_candidates]
    all_over_budget_reference = any(item.get("all_over_budget_reference") for item in shown_items)
    heading = "预算外低价参考" if all_over_budget_reference else "已排除的更低价组合"
    parts = []
    parts.append(f"<div style='font-weight:600;margin-bottom:8px;color:#111;'>{html.escape(heading)}</div>")
    if all_over_budget_reference:
        parts.append(
            "<div style='margin-bottom:8px;color:#92400e;font-weight:600;'>"
            "预算外低价参考：当前主推也超过预算，以下组合价格较低但仍在预算外，需结合你的时间/行李/执行约束判断。"
            "</div>"
        )
    shared_outbound = _excluded_shared_outbound(shown_items)
    if shared_outbound:
        return_choices = [
            _excluded_flight_brief(item.get("return"))
            for item in shown_items
            if isinstance(item.get("return"), dict)
        ]
        parts.append(
            "<div style='margin-bottom:8px;color:#374151;font-size:13px;'>"
            f"共同去程:{html.escape(_excluded_flight_brief(shared_outbound))}"
            "</div>"
        )
        if return_choices:
            parts.append(
                "<div style='margin-bottom:10px;color:#374151;font-size:13px;'>"
                f"返程选择:{html.escape('；'.join(return_choices))}"
                "</div>"
            )
    for _price, item in shown_candidates:
        parts.append(_render_excluded_plan_card(item, current_price, is_roundtrip))
    remaining = len(candidates) - len(shown_candidates)
    if remaining > 0:
        parts.append(
            "<div style='margin-top:8px;color:#666;font-size:12px;'>"
            f"另有{remaining}个方案因类似原因排除。"
            "</div>"
        )
    return "".join(parts)


def _excluded_shared_outbound(items: list[dict]) -> dict | None:
    outbound_flights = [item.get("outbound") for item in items or [] if isinstance(item.get("outbound"), dict)]
    if len(outbound_flights) < 2:
        return None
    identities = {
        (
            str(flight.get("flight_combo") or flight.get("flight_no") or "").strip(),
            str(flight.get("departure_airport") or flight.get("origin") or "").strip(),
            str(flight.get("arrival_airport") or flight.get("destination") or "").strip(),
            str(flight.get("departure_time") or flight.get("dep_time") or "").strip(),
        )
        for flight in outbound_flights
    }
    if len(identities) == 1:
        return outbound_flights[0]
    return None


def _excluded_flight_brief(flight: dict) -> str:
    combo = str(flight.get("flight_combo") or flight.get("flight_no") or "航班待确认").strip()
    dep = str(flight.get("departure_airport") or flight.get("origin") or "").strip()
    arr = str(flight.get("arrival_airport") or flight.get("destination") or "").strip()
    dep_time = str(flight.get("departure_time") or flight.get("dep_time") or "").strip()
    arr_time = str(flight.get("arrival_time") or flight.get("arr_time") or "").strip()
    route = f" {dep}{dep_time}→{arr}{arr_time}" if dep or arr or dep_time or arr_time else ""
    transfer = _excluded_transfer_text(flight)
    aircraft = _email_plan_aircraft_text(flight)
    aircraft_text = "" if aircraft in {"", "机型待确认"} else f" {aircraft}"
    return f"{combo}{route} {transfer}{aircraft_text}".strip()


def _excluded_compact_name(item: dict, index: int, shared_outbound: bool = False) -> str:
    if shared_outbound and isinstance(item.get("return"), dict):
        ret = item["return"]
        brief = _excluded_flight_brief(ret)
        if brief:
            return f"返程{brief}"
    for key in ("name", "label", "flight_combo", "combo"):
        value = str(item.get(key) or "").strip()
        if value:
            if "方案" in value:
                return value
            return f"{value}方案"
    outbound = item.get("outbound") or {}
    ret = item.get("return") or {}
    combo = outbound.get("flight_combo") or ret.get("flight_combo")
    if combo:
        return f"{combo}方案"
    return f"更便宜方案{index}"


def _excluded_item_price(item: dict):
    return _to_float(item.get("total_price") or item.get("roundtrip_price") or item.get("price"))


def _excluded_leg_execution_summary(label: str, flight: dict | None) -> list[str]:
    flight = flight or {}
    if not flight:
        return []
    parts = []
    status = _status_availability_label(flight)
    if status:
        parts.append(f"{label}库存:{status}")
    baggage = _pushplus_baggage_line_for_flight(flight)
    if baggage:
        parts.append(baggage.replace("行李:", f"{label}行李:", 1))
    refund = _verification_refund_line_for_flight(flight)
    if refund:
        parts.append(refund.replace("退改:", f"{label}退改:", 1))
    return parts


def _excluded_compact_execution_summary(item: dict) -> str:
    parts = []
    if isinstance(item.get("outbound"), dict):
        parts.extend(_excluded_leg_execution_summary("去程", item.get("outbound")))
    if isinstance(item.get("return"), dict):
        parts.extend(_excluded_leg_execution_summary("返程", item.get("return")))
    if not parts:
        flight = _excluded_item_flight(item)
        parts.extend(_excluded_leg_execution_summary("", flight))
    return " / ".join(part for part in parts if part)


def _email_excluded_compact_reason(item: dict, scope: str, is_roundtrip: bool, current_price=None) -> str:
    details = [str(value).strip() for value in _excluded_reason_details(item) if str(value or "").strip()]
    reason = details[0] if details else "不符合当前规则"
    if is_roundtrip and scope != "roundtrip" and "非往返总价" not in reason:
        direction = _excluded_scope_label(scope).replace("方案", "") or "单段"
        reason += f"(注:此为{direction}单段价,非往返总价)"
    price = _excluded_item_price(item)
    current = _to_float(current_price)
    if price is not None and current is not None and price < current and "对比主推" not in reason:
        reason += f"；对比主推:虽便宜{_price_text(current - price)},但上述条件不满足"
    execution = _excluded_compact_execution_summary(item)
    if execution:
        reason += f"；执行信息:{execution}"
    return reason


def _no_primary_history_text(payload: dict, *, kind: str) -> str:
    constraint_change = payload.get("constraint_change") or {}
    if constraint_change.get("changed"):
        return str(
            constraint_change.get("disclosure")
            or "筛选条件已变更，旧条件样本不再计入，同条件样本重新积累"
        ).strip()

    signal = payload.get("price_signal") or {}
    sample_n = int(signal.get("sample_n") or len(payload.get("price_history") or []))
    if sample_n < MIN_SAMPLE_FOR_PRICE_SIGNAL:
        return f"同条件样本不足(当前n={sample_n}),继续积累中,暂不给出价格{kind}判断"
    if kind == "走势":
        text = str(payload.get("trend_summary") or "").strip()
    else:
        text = str(signal.get("summary") or signal.get("label") or "").strip()
    if text:
        return text
    return f"同条件历史统计暂缺,暂不给出价格{kind}判断"


def _email_no_primary_price_signal_body(payload: dict) -> str:
    if _data_incomplete_state(payload):
        return (
            "<div style='color:#666;font-size:12px;'>"
            "数据不完整,本轮不作价格位置判断。</div>"
        )
    return (
        "<div style='color:#666;font-size:12px;'>"
        f"{html.escape(_no_primary_history_text(payload, kind='位置'))}</div>"
    )


def _email_trend_card_body(payload: dict) -> str:
    if _data_incomplete_state(payload):
        return (
            "<div style='color:#666;font-size:12px;'>"
            "数据不完整,本轮不作价格走势判断。</div>"
        )
    if _no_primary_plan_state(payload):
        status_text = _plan_status_change_text(payload)
        trend_text = str(payload.get("trend_summary") or "").strip()
        missing_markers = ("未获取到报价", "未获报价", "暂无报价", "无报价")
        if any(marker in status_text or marker in trend_text for marker in missing_markers):
            return "<div style='color:#666;font-size:12px;'>上次方案航班本次未获报价,趋势暂缺。</div>"
        return (
            "<div style='color:#666;font-size:12px;'>"
            f"{html.escape(_no_primary_history_text(payload, kind='走势'))}</div>"
        )

    constraint_change = payload.get("constraint_change") or {}
    if constraint_change.get("changed"):
        disclosure = str(constraint_change.get("disclosure") or "").strip()
        return (
            "<div style='color:#666;font-size:12px;'>"
            f"{html.escape(disclosure or '筛选条件已变更,同条件样本重新积累')}"
            "</div>"
        )

    history_rows = payload.get("price_history") or []
    unique_prices = {
        round(float(row.get("price")), 2)
        for row in history_rows
        if isinstance(row, dict) and row.get("price")
    }
    if len(history_rows) < 3 or len(unique_prices) < 2:
        return "<div style='color:#888;font-size:12px;'>历史样本不足，仅供参考。</div>"

    body = '<img src="cid:trendchart" style="max-width:100%;height:auto;border:0;" alt="近期价格走势">'
    if payload.get("trend_summary"):
        body += f"<div style='margin-top:8px;'>{html.escape(str(payload['trend_summary']))}</div>"
    else:
        body += "<div style='margin-top:8px;color:#666;font-size:12px;'>当前搜索参考价已进入可验证区间，建议以支付页最终价为准。</div>"
    return body


def _calendar_row_price(row: dict):
    if not isinstance(row, dict):
        return None
    return _to_float(row.get("min_price") or row.get("value") or row.get("price"))


def _calendar_unit_row_price(row: dict):
    if not isinstance(row, dict):
        return None
    return _to_float(
        row.get("unit_roundtrip_price")
        or row.get("unit_price")
        or row.get("single_person_price")
        or row.get("single_adult_roundtrip")
        or row.get("roundtrip_ref_price")
        or row.get("unit_oneway_price")
    )


def _calendar_row_is_passenger_scoped(row: dict, passenger_factor: float = 1) -> bool:
    if not isinstance(row, dict):
        return False
    scope_text = " ".join(
        str(row.get(key) or "")
        for key in ("scope", "price_scope", "label", "note")
    ).lower()
    if "passenger" in scope_text or "\u5168\u5458" in scope_text:
        return True
    if "\u4eba" in scope_text and ("\u5168\u5458" in scope_text or "\u5f80\u8fd4" in scope_text):
        return True
    row_factor = _to_float(row.get("passenger_factor"))
    factor = _to_float(passenger_factor) or row_factor or 1
    unit_price = _calendar_unit_row_price(row)
    row_price = _calendar_row_price(row)
    if unit_price is None or row_price is None or factor <= 1:
        return False
    return abs(row_price - unit_price * factor) <= max(1, row_price * 0.01)


def _calendar_row_unit_or_price(row: dict):
    unit_price = _calendar_unit_row_price(row)
    return unit_price if unit_price is not None else _calendar_row_price(row)


def _calendar_scope_unit(calendar: dict) -> tuple[bool, str]:
    scope = str((calendar or {}).get("scope") or "oneway").lower()
    is_roundtrip_scope = (
        scope in {"roundtrip", "\u5f80\u8fd4"}
        or "roundtrip" in scope
        or "passenger_roundtrip" in scope
        or "\u5f80\u8fd4" in scope
    )
    return is_roundtrip_scope, "\u5f80\u8fd4" if is_roundtrip_scope else "\u5355\u7a0b"


def _calendar_short_date(row: dict) -> str:
    raw = str((row or {}).get("date") or "")
    return raw[5:] if len(raw) >= 10 else raw


def _calendar_parse_date(value: str):
    try:
        return datetime.strptime(str(value or ""), "%Y-%m-%d").date()
    except ValueError:
        return None


def _calendar_price_pairs_same_scope(
    rows: list[dict],
    selected: dict,
    selected_price: float,
    passenger_factor: float = 1,
    passengers: dict | None = None,
    route_type: str | None = None,
):
    factor = _to_float(passenger_factor) or 1
    selected_number = _to_float(selected_price)
    selected_display = _calendar_display_price(selected, factor, passengers, route_type) if isinstance(selected, dict) else None
    if selected_number is not None and selected_display is not None:
        tolerance = max(1, abs(selected_display) * 0.01)
        if abs(selected_number - selected_display) > tolerance:
            raise AssertionError(
                f"低价日历选中价与数组人数口径不一致: "
                f"selected_price={selected_number:g}, expected={selected_display:g}, "
                f"passenger_factor={factor:g}"
            )

    price_pairs = []
    any_passenger_scoped = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        display_price = _calendar_display_price(row, factor, passengers, route_type)
        unit_price = _calendar_row_unit_or_price(row)
        row_price = _calendar_row_price(row)
        if display_price is None or display_price <= 0:
            continue
        if factor > 1 and row_price is not None and not _calendar_row_is_passenger_scoped(row, factor):
            expected = _calendar_display_price(row, factor, passengers, route_type)
            tolerance = max(1, abs(expected) * 0.01)
            if abs(display_price - expected) > tolerance:
                raise AssertionError(
                    f"低价日历数组人数口径不一致: date={row.get('date')!r}, "
                    f"display_price={display_price:g}, expected={expected:g}, "
                    f"passenger_factor={factor:g}"
                )
        price_pairs.append((round(unit_price if unit_price is not None else display_price, 2), round(display_price, 2)))
        any_passenger_scoped = any_passenger_scoped or _calendar_row_is_passenger_scoped(row, factor)
    return price_pairs, any_passenger_scoped


def _calendar_selected_level(
    rows: list[dict],
    selected: dict,
    selected_price: float,
    passenger_factor: float = 1,
    passengers: dict | None = None,
    route_type: str | None = None,
) -> str:
    price_pairs, any_passenger_scoped = _calendar_price_pairs_same_scope(
        rows,
        selected,
        selected_price,
        passenger_factor,
        passengers,
        route_type,
    )
    if not price_pairs:
        return "价格位置待确认"
    raw_prices = sorted(unit for unit, _display in price_pairs)
    prices = sorted(display for _unit, display in price_pairs)
    selected_number = _to_float(selected_price)
    if selected_number is None:
        return "价格位置待确认"
    price_array_multiplied = bool(
        any_passenger_scoped
        or any(abs(display - unit) > 0.01 for unit, display in price_pairs)
    )
    more_expensive = sum(1 for price in prices if price > selected_number)
    percentile = more_expensive / len(prices)
    print(
        f"[日历对比] 数组前3(before单人)={raw_prices[:3]}, "
        f"after={prices[:3]}, 是否已×人数={price_array_multiplied}"
    )
    print(
        f"[日历对比] 你选日期价={selected_number:g}, 全部价格={prices}, "
        f"比你选贵的天数={more_expensive}, 分位={percentile:.2f}"
    )
    if percentile >= 0.75:
        return "较便宜"
    if percentile <= 0.25:
        return "偏贵"
    return "中等水平"


def _calendar_display_price(
    row: dict,
    passenger_factor: float = 1,
    passengers: dict | None = None,
    route_type: str | None = None,
):
    price = _calendar_row_price(row)
    if price is None:
        return None
    factor = _to_float(passenger_factor) or 1
    if _calendar_row_is_passenger_scoped(row, factor):
        return price
    if factor == 1:
        return price
    if isinstance(passengers, dict) and any(_to_float(value) for value in passengers.values()):
        return build_display_prices(price, None, passengers, route_type)["total"]
    return round_display_price(price * factor)


def _calendar_display_savings(
    calendar: dict,
    passenger_factor: float = 1,
    unit_override: str | None = None,
    passengers: dict | None = None,
    route_type: str | None = None,
) -> list[dict]:
    rows = [row for row in ((calendar or {}).get("rows") or []) if isinstance(row, dict)]
    selected = next((row for row in rows if row.get("selected")), None)
    selected_price = _calendar_display_price(selected, passenger_factor, passengers, route_type) if selected else None
    if not selected or selected_price is None:
        return (calendar or {}).get("savings") or []
    _, unit = _calendar_scope_unit(calendar or {})
    unit = unit_override or unit
    selected_date = _calendar_short_date(selected) or str(selected.get("date") or "")
    target_dt = _calendar_parse_date(str(selected.get("date") or ""))
    savings = []
    for row in rows:
        if row is selected:
            continue
        price = _calendar_display_price(row, passenger_factor, passengers, route_type)
        if price is None or price >= selected_price:
            continue
        save = selected_price - price
        row_date = str(row.get("date") or "")
        row_dt = _calendar_parse_date(row_date)
        direction = ""
        if target_dt and row_dt:
            diff_days = (row_dt - target_dt).days
            direction = "提前" if diff_days < 0 else "推迟"
            direction = f"{direction}{abs(diff_days)}天"
        date_text = f"{_calendar_short_date(row)} {row.get('weekday') or ''}".strip()
        savings.append(
            {
                "date": row_date,
                "weekday": row.get("weekday"),
                "price": price,
                "save": save,
                "tip": (
                    f"{direction}({date_text},{unit}最低{_price_text(price)})"
                    f"比你选的{selected_date}({_price_text(selected_price)})"
                    f"省约{_price_text(save)}/{unit}"
                ).lstrip("()"),
            }
        )
    savings.sort(key=lambda item: item["save"], reverse=True)
    return savings[:3]


def _email_price_calendar_body(payload: dict) -> str:
    calendar = payload.get("price_calendar") or {}
    rows = calendar.get("rows") or []
    uncollected_rows = [
        row
        for row in (calendar.get("uncollected_rows") or [])
        if isinstance(row, dict) and row.get("date")
    ]
    if not rows and not uncollected_rows:
        return "<div style='color:#888;font-size:12px;'>暂无低价日历数据。</div>"
    if not rows:
        items = "".join(
            "<div>"
            f"{html.escape(str(row.get('date'))[:10])}: "
            "<span style='color:#888;'>今日未采</span>"
            "</div>"
            for row in uncollected_rows[:10]
        )
        return (
            "<div style='font-weight:600;margin-bottom:8px;color:#111;'>"
            "弹性日期面板评估</div>"
            "<div style='margin-bottom:8px;color:#666;font-size:12px;'>"
            "本轮按面板评估弹性日期；以下日期今日无可复用观测，未发起补采。"
            "</div>"
            f"{items}"
        )
    is_roundtrip_scope, _unit = _calendar_scope_unit(calendar)
    primary_plan = ((payload.get("recommended_plans") or [{}]) or [{}])[0] or {}
    is_roundtrip_monitor = bool(payload.get("is_roundtrip") or primary_plan.get("is_roundtrip"))
    is_roundtrip_monitor_with_oneway_calendar = is_roundtrip_monitor and not is_roundtrip_scope
    passenger_pricing = payload.get("passenger_pricing") or primary_plan.get("passenger_pricing") or {}
    passenger_calendar_applies = _passenger_pricing_applies(passenger_pricing)
    passenger_label = passenger_pricing.get("passenger_label") or _passenger_label_from_counts(passenger_pricing.get("passengers"))
    passenger_factor = _to_float(passenger_pricing.get("factor")) or 1
    passenger_count = _to_float(passenger_pricing.get("passenger_count")) or _passenger_total_count(passenger_pricing.get("passengers"))
    label_count = sum(int(n) for n in re.findall(r"(\d+)(?:成人|儿童|老人|婴儿)", str(passenger_label)))
    if passenger_count <= 1 and label_count > 1:
        passenger_count = label_count
    passenger_count_text = str(int(passenger_count)) if float(passenger_count).is_integer() else f"{passenger_count:g}"
    calendar_passengers = _pricing_passengers(passenger_pricing)
    calendar_price_scope = (
        "all_passengers_roundtrip"
        if is_roundtrip_scope and passenger_calendar_applies
        else ("per_person_roundtrip" if is_roundtrip_scope else "per_person_oneway")
    )
    return_date_text = str(calendar.get("return_date") or "").strip()
    return_short = return_date_text[5:10] if len(return_date_text) >= 10 else return_date_text
    if is_roundtrip_scope:
        fixed_return = f"(返程日固定{return_short})" if return_short else ""
        if passenger_calendar_applies:
            title = f"低价日历 ｜ {passenger_count_text}人({passenger_label})往返参考价(单人趋势×{passenger_factor:g}){fixed_return}"
        else:
            title = f"低价日历 ｜ 往返参考价{fixed_return}"
    elif is_roundtrip_monitor_with_oneway_calendar:
        title = "单程价格趋势(仅供参考出发日选择)"
    else:
        title = "低价日历 ｜ 单程最低参考价"

    table = [
        f"<div style='font-weight:600;margin-bottom:8px;color:#111;'>{html.escape(title)}</div>",
    ]
    if is_roundtrip_monitor_with_oneway_calendar:
        table.append(
            "<div style='margin-bottom:8px;color:#666;font-size:12px;'>"
            "说明:下方为各日期的单程最低价,用于判断哪天出发的单程更便宜。"
            "你的往返方案需去程+返程各自验证,单程趋势仅供日期趋势参考。"
            "</div>"
        )
    elif is_roundtrip_scope:
        return_desc = f"返程日({html.escape(return_short)})" if return_short else "返程日"
        lowest_row_for_passengers = min(
            (row for row in rows if isinstance(row, dict) and _calendar_row_price(row) is not None),
            key=lambda row: _calendar_row_price(row) or float("inf"),
            default=None,
        )
        passenger_example = ""
        if passenger_calendar_applies and lowest_row_for_passengers:
            row_price = _calendar_row_price(lowest_row_for_passengers)
            passenger_example = (
                f"下方为单人往返参考价的全员换算展示,已按{html.escape(passenger_label)}约×{passenger_factor:g}换算。"
                f"{html.escape(_calendar_short_date(lowest_row_for_passengers))} "
                f"单人往返{_price_text(row_price)}×{passenger_factor:g} → "
                f"全员约{_price_text(_calendar_display_price(lowest_row_for_passengers, passenger_factor, calendar_passengers, payload.get('route_type')))}。"
            )
        table.append(
            "<div style='margin-bottom:8px;color:#666;font-size:12px;'>"
            f"说明:每行=该出发日单程最低 + {return_desc}单程最低,"
            "为往返价格参考下限,实际同渠道拼接价可能略高。"
            f"{passenger_example}"
            "</div>"
        )
    insight = _price_calendar_insight_text(payload)
    if insight:
        table.append(f"<div style='margin-bottom:10px;color:#374151;'>💡 {html.escape(insight)}</div>")
    table.extend(
        [
            "<table style='width:100%;font-size:13px;line-height:1.6;border-collapse:collapse;'>",
            "<thead><tr>",
            "<th style='text-align:left;color:#666;border-bottom:1px solid #eee;padding:6px 4px;'>日期</th>",
            f"<th style='text-align:left;color:#666;border-bottom:1px solid #eee;padding:6px 4px;'>{'往返参考价' if is_roundtrip_scope else '最低价'}</th>",
            "<th style='text-align:left;color:#666;border-bottom:1px solid #eee;padding:6px 4px;'>说明</th>",
            "</tr></thead><tbody>",
        ]
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_date_key = str(row.get("date") or "")[:10]
        if len(row_date_key) == 10:
            _mark_provenance_reference(payload, f"calendar.{row_date_key}.min")
        date_text = f"{str(row.get('date') or '')[5:]} {row.get('weekday') or ''}".strip()
        tags = []
        if row.get("lowest"):
            tags.append("最低")
        if row.get("selected"):
            tags.append("你选的")
        if is_roundtrip_scope:
            outbound_price = _to_float(row.get("outbound_min_price"))
            return_price = _to_float(row.get("return_min_price") or calendar.get("return_min_price"))
            if outbound_price is not None and return_price is not None:
                tags.append(f"去{_price_text(outbound_price)}+返{_price_text(return_price)}")
        tag_text = " / ".join(tags)
        price_style = "color:#16a34a;font-weight:600;" if row.get("lowest") else "color:#333;"
        if row.get("selected"):
            price_style = "color:#2563eb;font-weight:600;"
        row_single_price = _calendar_row_price(row)
        display_price = _calendar_display_price(
            row,
            passenger_factor if passenger_calendar_applies else 1,
            calendar_passengers,
            payload.get("route_type"),
        )
        if is_roundtrip_scope and passenger_calendar_applies and row_single_price is not None:
            tags.append(f"单人往返{_price_text(row_single_price)}×{passenger_factor:g}")
        price_text = _price_text(display_price if display_price is not None else row.get("min_price"))
        if not is_roundtrip_scope and "(单程)" not in price_text:
            price_text += "(单程)"
        table.append(
            "<tr>"
            f"<td style='padding:7px 4px;border-bottom:1px solid #f5f5f5;'>{html.escape(date_text)}</td>"
            f"<td style='padding:7px 4px;border-bottom:1px solid #f5f5f5;{price_style}'>{html.escape(price_text)}</td>"
            f"<td style='padding:7px 4px;border-bottom:1px solid #f5f5f5;color:#666;'>{html.escape(tag_text)}</td>"
            "</tr>"
        )
    table.append("</tbody></table>")
    if uncollected_rows:
        dates = "、".join(str(row.get("date"))[:10] for row in uncollected_rows[:10])
        table.append(
            "<div style='margin-top:8px;color:#888;font-size:12px;'>"
            f"今日未采(未发起补采):{html.escape(dates)}"
            "</div>"
        )

    savings = _calendar_display_savings(
        calendar,
        passenger_factor if passenger_calendar_applies else 1,
        "全员往返" if passenger_calendar_applies else None,
        calendar_passengers,
        payload.get("route_type"),
    )
    if savings:
        table.append("<div style='margin-top:10px;font-weight:600;'>省钱提示</div>")
        for item in savings[:3]:
            tip = item.get("tip") or (
                f"{item.get('date')} {item.get('weekday')} 便宜{_price_text(item.get('save'))}"
            )
            table.append(f"<div style='color:#374151;'>- {html.escape(str(tip))}</div>")

    weekday = calendar.get("weekday_pattern") or {}
    if weekday and not weekday.get("data_insufficient") and weekday.get("tip"):
        weekday_provenance = weekday.get("provenance") or {}
        minimum_envelope = _mark_provenance_reference(payload, "weekday.minimum")
        if not minimum_envelope:
            minimum_envelope = weekday_provenance.get("weekday.minimum")
        provenance_note = format_micro_provenance(minimum_envelope) if minimum_envelope else ""
        table.append(
            f"<div style='margin-top:8px;color:#374151;'>"
            f"{html.escape(str(weekday.get('tip')))}{html.escape(provenance_note)}</div>"
        )
    elif weekday and weekday.get("data_insufficient"):
        table.append("<div style='margin-top:8px;color:#888;font-size:12px;'>周几更便宜：数据积累中。</div>")

    note = calendar.get("note") or ("为往返参考价下限，实付以支付页为准。" if is_roundtrip_scope else "为单程最低参考价，实付以支付页为准。")
    if is_roundtrip_scope:
        selected = next((row for row in rows if isinstance(row, dict) and row.get("selected")), None)
        selected_ref = _calendar_row_price(selected) if isinstance(selected, dict) else None
        actual_roundtrip = (
            _to_float(passenger_pricing.get("single_adult_price"))
            if passenger_calendar_applies
            else _to_float(payload.get("display_price") or payload.get("current_price"))
        )
        if selected_ref is not None and actual_roundtrip is not None and actual_roundtrip > selected_ref:
            gap = actual_roundtrip - selected_ref
            actual_label = "当前实际方案单人往返" if passenger_calendar_applies else "当前实际方案往返"
            table.append(
                "<div style='margin-top:8px;color:#374151;font-size:12px;'>"
                f"你选日期的往返参考下限约{_price_text(selected_ref)},"
                f"{actual_label}{_price_text(actual_roundtrip)},"
                f"高于参考下限约{_price_text(gap)},可能因临近出发或舱位原因。"
                "</div>"
            )
    if is_roundtrip_monitor and not is_roundtrip_scope:
        roundtrip_price = _to_float(payload.get("display_price") or payload.get("current_price"))
        roundtrip_note = "下方为单程最低参考价;你的往返方案需去程+返程各自验证。"
        if roundtrip_price is not None:
            roundtrip_note += f"往返总价约{_price_text(roundtrip_price)},与单程日历口径不同。"
        roundtrip_note += "单程趋势仅帮你发现便宜的出发日,不等于往返总价。"
        note = f"{roundtrip_note} {note}"
    if str(payload.get("route_type") or "").strip().lower() == "domestic":
        return_floor = _to_float(calendar.get("return_min_price"))
        if return_floor is None:
            return_floor = next(
                (
                    _to_float(row.get("return_min_price"))
                    for row in rows
                    if isinstance(row, dict) and _to_float(row.get("return_min_price")) is not None
                ),
                None,
            )
        example = f"(如返程{_price_text(return_floor)}班次)" if return_floor is not None else ""
        note = f"{note} 下限班次可能不满足你的时间窗{example},备选按可行时间选取。"
    table.append(f"<div style='margin-top:8px;color:#666;font-size:12px;'>注:{html.escape(str(note))}</div>")
    calendar_prices_multiplied = bool(
        any(_calendar_row_is_passenger_scoped(row, passenger_factor) for row in rows if isinstance(row, dict))
        or (passenger_calendar_applies and (passenger_factor or 1) != 1)
    )
    print(
        f"[\u65e5\u5386\u4eba\u6570\u8bca\u65ad] passengers={passenger_pricing.get('passengers')}, "
        f"\u65e5\u5386\u5355\u4ef7={rows[0].get('min_price') if rows and isinstance(rows[0], dict) else None}, "
        f"\u662f\u5426\u5df2\u00d7\u4eba\u6570={calendar_prices_multiplied}"
    )
    return "".join(table)


def _email_tcurve_body(payload: dict) -> str:
    curve = payload.get("tcurve") or {}
    if not isinstance(curve, dict) or not curve:
        return ""
    points = curve.get("points") or []
    qualified_count = sum(1 for point in points if point.get("sufficient"))
    if qualified_count < TCURVE_MIN_CELLS:
        safe_log(f"[T曲线] 样本不足 n合格格数={qualified_count} 跳过渲染")
        return ""

    current_t = curve.get("current_t")
    anchors = select_anchor_points(points, current_t, limit=5)
    if not anchors:
        safe_log("[T曲线] 样本不足 n合格格数=0 跳过渲染")
        return ""
    coverage = curve.get("coverage") or {}
    lines = [
        "<div style='margin-bottom:8px;color:#374151;'>"
        f"本订阅当前 T={html.escape(str(current_t))} 天"
        "</div>",
        "<div style='margin-bottom:8px;color:#666;font-size:12px;'>"
        f"观测覆盖 T={html.escape(str(coverage.get('t_min')))} 至 "
        f"T={html.escape(str(coverage.get('t_max')))} 天；仅描述该范围内的历史观测，不外推未覆盖区间。"
        "</div>",
    ]
    for point in anchors:
        stat_key = f"tcurve.T{int(point['t'])}.median"
        point_envelope = _mark_provenance_reference(payload, stat_key)
        provenance_note = format_micro_provenance(point_envelope or point.get("provenance"))
        lines.append(
            "<div>"
            f"T={int(point['t'])}天：中位{_price_text(point.get('median'))}，"
            f"IQR {_price_text(point.get('p25'))}-{_price_text(point.get('p75'))}"
            f"{html.escape(provenance_note)}，同航线历史观测"
            "</div>"
        )
    degraded_count = int(curve.get("degraded_count") or 0)
    if degraded_count:
        if curve.get("include_degraded"):
            disclosure = f"本节包含{degraded_count}个源覆盖不完整日格，结果可能受数据源缺失影响。"
        else:
            disclosure = f"已剔除{degraded_count}个源覆盖不完整日格。"
        lines.append(
            "<div style='margin-top:8px;color:#666;font-size:12px;'>"
            f"{html.escape(disclosure)}"
            "</div>"
        )
    return "".join(lines)


def _email_forecast_body(payload: dict) -> str:
    forecast = payload.get("forecast") or {}
    if not isinstance(forecast, dict) or not forecast.get("eligible"):
        return ""
    predictions = forecast.get("predictions") or []
    if not predictions:
        return ""
    backtest = forecast.get("backtest") or {}
    model = backtest.get("model") or {}
    naive = backtest.get("naive") or {}
    plan_price = _valid_price_float(
        ((payload.get("price_tiers") or {}).get("unit_roundtrip"))
        or payload.get("unit_roundtrip_price")
    )
    market = _valid_price_float((forecast.get("current_market_reference") or {}).get("median"))
    lines = [
        "<div style='margin-bottom:8px;color:#374151;'>预测对象为该航线市场最低参考价，与你的筛选条件无关。</div>",
    ]
    if plan_price is not None and market is not None:
        lines.append(
            "<div style='margin-bottom:8px;color:#666;font-size:12px;'>"
            f"你的筛选后方案价(当前单人往返 {_price_text(plan_price)})可能持续高于此曲线；"
            f"当前市场单人单程参考下限约 {_price_text(market)}。"
            "</div>"
        )
    for item in predictions:
        lines.append(
            "<div>"
            f"{html.escape(str(item.get('target_day')))}：中位 {_price_text(item.get('median'))}，"
            f"IQR {_price_text(item.get('p25'))}-{_price_text(item.get('p75'))}，"
            f"P10-P90 {_price_text(item.get('p10'))}-{_price_text(item.get('p90'))}"
            "</div>"
        )
    lines.append(
        "<div style='margin-top:8px;color:#666;font-size:12px;'>"
        f"基于历史规律的统计估计，非承诺；累计走前回测 k=3 MAPE={html.escape(str(model.get('mape')))}%，"
        f"朴素基线={html.escape(str(naive.get('mape')))}%。"
        "</div>"
    )
    return "".join(lines)



def _price_calendar_insight_text(payload: dict) -> str:
    calendar = payload.get("price_calendar") or {}
    rows = [row for row in (calendar.get("rows") or []) if isinstance(row, dict)]
    if not rows:
        return ""
    selected = next((row for row in rows if row.get("selected")), None)
    lowest = min(
        (row for row in rows if _calendar_row_price(row) is not None),
        key=lambda row: _calendar_row_price(row) or float("inf"),
        default=None,
    )
    if not selected or not lowest:
        return ""
    is_roundtrip_scope, unit = _calendar_scope_unit(calendar)
    primary_plan = ((payload.get("recommended_plans") or [{}]) or [{}])[0] or {}
    passenger_pricing = payload.get("passenger_pricing") or primary_plan.get("passenger_pricing") or {}
    passenger_calendar_applies = is_roundtrip_scope and _passenger_pricing_applies(passenger_pricing)
    passenger_factor = _to_float(passenger_pricing.get("factor")) or 1
    display_factor = passenger_factor if passenger_calendar_applies else 1
    passengers = _pricing_passengers(passenger_pricing)
    route_type = payload.get("route_type")
    selected_price = _calendar_display_price(selected, display_factor, passengers, route_type)
    lowest_price = _calendar_display_price(lowest, display_factor, passengers, route_type)
    if selected_price is None or lowest_price is None:
        return ""
    selected_date = _calendar_short_date(selected) or str(selected.get("date") or "\u4f60\u9009\u65e5\u671f")
    lowest_date = _calendar_short_date(lowest) or str(lowest.get("date") or "\u6700\u4f4e\u65e5")
    lowest_weekday = str(lowest.get("weekday") or "").strip()
    scope_label = "\u5168\u5458\u5f80\u8fd4" if passenger_calendar_applies else unit
    scope_key = "all_passengers_roundtrip" if passenger_calendar_applies else ("per_person_roundtrip" if is_roundtrip_scope else "per_person_oneway")
    level = _calendar_selected_level(
        rows,
        selected,
        selected_price,
        display_factor,
        passengers,
        route_type,
    )
    lowest_text = f"{scope_label}\u6700\u4f4e{_price_text_with_caliber(lowest_price, scope_key, passengers, route_type)}({lowest_date} {lowest_weekday})".strip()
    date_flex = str(payload.get("date_flexibility") or payload.get("date_flex") or "").strip()
    base = f"\u4f60\u9009\u7684{selected_date}{scope_label}{_price_text_with_caliber(selected_price, scope_key, passengers, route_type)},\u5904\u4e8e{level};{lowest_text}"
    if passenger_calendar_applies:
        single_lowest = _calendar_row_price(lowest)
        base += f"(\u5355\u4eba\u5f80\u8fd4{_price_text_with_caliber(single_lowest, 'per_person_roundtrip', passengers, route_type)}\u00d7{passenger_factor:g})"
    if selected is lowest or selected_price <= lowest_price:
        return base
    save = round(selected_price - lowest_price)
    save_text = _price_text_with_caliber(save, scope_key, passengers, route_type)
    if date_flex in {"0", "none", "fixed", "\u4e0d\u53ef\u8c03", "\u56fa\u5b9a"}:
        return f"{base};\u4f60\u8bbe\u4e86\u65e5\u671f\u4e0d\u53ef\u8c03,\u4ec5\u4f5c\u8d8b\u52bf\u53c2\u8003,\u5dee\u989d\u7ea6{save_text}/{scope_label}"
    return f"{base};\u82e5\u65e5\u671f\u53ef\u8c03,\u7406\u8bba\u53ef\u7701\u7ea6{save_text}/{scope_label}"

def _email_airport_cost_comparison_body(payload: dict) -> str:
    rows = payload.get("airport_cost_comparison") or []
    if isinstance(rows, dict):
        rows = rows.get("rows") or []
    if not rows:
        return "<div style='color:#888;font-size:12px;'>暂无机场参考数据。</div>"
    parts = [
        "<table style='width:100%;font-size:13px;line-height:1.6;border-collapse:collapse;'>",
        "<thead><tr>",
        "<th style='text-align:left;color:#666;border-bottom:1px solid #eee;padding:6px 4px;'>机场组合</th>",
        "<th style='text-align:left;color:#666;border-bottom:1px solid #eee;padding:6px 4px;'>票价(单人单程参考)</th>",
        "<th style='text-align:left;color:#666;border-bottom:1px solid #eee;padding:6px 4px;'>有效成本</th>",
        "<th style='text-align:left;color:#666;border-bottom:1px solid #eee;padding:6px 4px;'>说明</th>",
        "</tr></thead><tbody>",
    ]
    for item in rows[:4]:
        dep = _airport_short_label(item.get("departure_airport"))
        arr = _airport_short_label(item.get("arrival_airport") or item.get("airport"))
        raw_source = str(item.get("price_source") or item.get("data_source") or "").strip()
        source_parts = [part.strip().lower() for part in raw_source.split("+") if part.strip()]
        source_text = "+".join(dict.fromkeys(_source_channel_label(part) for part in source_parts))
        source_text = source_text or "来源待确认"
        collected_at = _collected_datetime(item)
        collected_text = collected_at.strftime("%m-%d %H:%M") if collected_at else "时间待确认"
        price_provenance = f"来源:{source_text} · {collected_text}采集"
        parts.append(
            "<tr>"
            f"<td style='padding:7px 4px;border-bottom:1px solid #f5f5f5;'>{html.escape(dep)} → {html.escape(arr)}</td>"
            f"<td style='padding:7px 4px;border-bottom:1px solid #f5f5f5;'>{_price_text(item.get('ticket_price'))}"
            f"<div style='color:#888;font-size:11px;'>{html.escape(price_provenance)}</div></td>"
            f"<td style='padding:7px 4px;border-bottom:1px solid #f5f5f5;color:#2563eb;font-weight:600;'>{_price_text(item.get('effective_cost'))}</td>"
            f"<td style='padding:7px 4px;border-bottom:1px solid #f5f5f5;color:#666;'>{html.escape(str(item.get('note') or ''))}</td>"
            "</tr>"
        )
    parts.append("</tbody></table>")
    parts.append(
        "<div style='margin-top:8px;color:#666;font-size:12px;'>"
        "票价为单人单程参考；多人同行请在渠道按实际人数重新确认。"
        "有效成本为参考估算，交通按打车估算，时间成本默认按¥50/小时估算。"
        "</div>"
    )
    return "".join(parts)


def _log_render_stats(render_stats: dict) -> None:
    duplicate_count = sum(
        max(0, count - 1)
        for count in (render_stats.get("full_identity_counts") or {}).values()
    )
    safe_log(
        f"{_render_log_prefix('渲染统计')} "
        f"整卡:主区={render_stats.get('full_main', 0)} "
        f"需调整区={render_stats.get('full_adjustment', 0)} "
        f"紧凑引用={render_stats.get('compact_refs', 0)} 重复={duplicate_count}"
    )


_NOTIFICATION_SECTION_TITLES = {
    "action_panel": "行动面板",
    "alternative_plans": "可选备选方案",
    "excluded_plans": "为什么不推荐更便宜方案",
    "price_trend": "价格走势",
    "price_signal": "价格信号",
    "data_source": "数据来源",
    "data_freshness": "数据时点:未记录",
    "quota_overview": "[配额总览]",
    "provenance": "数据依据",
    "mixed_cabin": "经济舱 / 商务舱并列参考",
}


def _no_match_mixed_cabin(payload: dict) -> bool:
    summary = payload.get("cabin_policy_summary") or {}
    mixed = payload.get("mixed_cabin") or {}
    return bool(
        summary.get("cabin_arrangement") == "mixed"
        or mixed.get("cabin_allocation")
    )


def _ensure_no_match_notification_sections(cards: list[str], payload: dict) -> list[str]:
    """按 canonical 清单补齐无方案或数据不完整通知。"""
    from notification_sections import missing_notification_sections, section_fallback

    trigger_type = "data_incomplete" if _data_incomplete_state(payload) else "no_match"
    missing = missing_notification_sections(
        "".join(cards),
        "",
        trigger_type=trigger_type,
        mixed_cabin=_no_match_mixed_cabin(payload),
    )
    for section in missing:
        title = _NOTIFICATION_SECTION_TITLES[section]
        cards.append(
            _email_card(
                title,
                html.escape(section_fallback(section, "本轮渲染未提供该小节结构化数据")),
            )
        )
    return cards


def _render_data_incomplete_report(
    payload: dict,
    subject: str,
    *,
    interactive_channels: bool = False,
) -> str:
    freshness_text = _data_freshness_headline(payload) or (
        f"采集时间:{_payload_freshness_text(payload)}"
        if payload.get("collected_at")
        else "本轮采集时点未记录"
    )
    heading = (
        "<h2 style='font-size:18px;color:#111;margin:0 0 12px;'>"
        f"{html.escape(subject)}</h2>"
    )
    cards = [
        heading,
        _email_card(
            "行动面板",
            _email_action_panel_body(
                payload,
                {},
                "以支付页为准",
                _data_incomplete_reason(payload),
                interactive_channels=interactive_channels,
            ),
        ),
        _email_card("价格走势", _email_trend_card_body(payload)),
        _email_card("价格信号", _email_no_primary_price_signal_body(payload)),
        _email_card("数据来源", _email_source_body(payload)),
        _email_card("数据时点", html.escape(f"数据时点:{freshness_text}")),
        _email_card("配额总览", html.escape(_quota_overview_text())),
        _email_card(
            "数据依据",
            _detail_provenance_body(payload)
            or "<div style='color:#888;font-size:12px;'>本次未引用历史统计值。</div>",
        ),
        _email_card(
            "操作链接",
            _email_action_links(
                payload,
                None,
                interactive_channels=interactive_channels,
                include_channel_picker=False,
            ),
        ),
    ]
    return "".join(_ensure_no_match_notification_sections(cards, payload))


@_with_render_log_channel("邮件")
def render_email(payload: dict) -> tuple[str, str]:
    """Render the full HTML email report from a normalized payload."""
    payload = payload or {}
    private_render = _render_private_email(payload)
    subject = _email_subject(payload)
    if _data_incomplete_state(payload):
        if private_render is not None:
            route = html.escape(str(payload.get("route") or "航班监控"))
            reason = html.escape(_private_data_incomplete_reason(payload))
            body = (
                f"<b>【数据不完整】{route}</b><br>"
                "当前判断:数据不完整,本轮结论不可用<br>"
                f"原因:{reason}<br>"
                "乘客构成、精确金额与技术细节已隐藏"
            )
            return subject, body
        return subject, _render_data_incomplete_report(payload, subject)
    if private_render is not None:
        return private_render
    verify_text = f"支付页≤{_price_text(payload.get('verify_price'))}" if payload.get("verify_price") else "以支付页为准"
    price_reason = str(payload.get("price_policy_reason") or "请以预估实付价和支付页最终价为准")
    baggage_line = ""
    primary_plan = (payload.get("recommended_plans") or [{}])[0] or {}
    rendered_full_keys = set()
    render_stats = {
        "full_main": 0,
        "full_adjustment": 0,
        "compact_refs": 0,
        "full_identity_counts": {},
    }
    if primary_plan:
        _log_recommended_total_consistency(primary_plan)
    no_primary = _no_primary_plan_state(payload)
    price_signal = payload.get("price_signal") or {}
    execution_advice = payload.get("execution_advice") or {}
    primary_plan_line = "方案待确认"
    if primary_plan:
        primary_plan_line = (
            f"{primary_plan.get('label') or '方案A'}，"
            f"{primary_plan.get('tier') or '首选方案'}，"
            f"搜索参考价{_price_text(primary_plan.get('price') or payload.get('display_price'))}，"
            f"预估实付{_price_text(primary_plan.get('estimated_price') or payload.get('transaction_price'))}"
        )
    if primary_plan.get("baggage_line"):
        baggage_line = f"<div><span style='color:#888;'>行李状态：</span>{html.escape(str(primary_plan.get('baggage_line')))}</div>"
        if "确认" in str(primary_plan.get("baggage_line")) or "不含" in str(primary_plan.get("baggage_line")):
            baggage_line += "<div style='color:#666;font-size:12px;'>当前价格可能不含托运行李；若支付页加行李后超过本次方案验证价，则不建议购买。</div>"
    heading_push_type = "无符合方案" if no_primary else _email_headline_type(payload)
    freshness_headline = _data_freshness_headline(payload)
    heading_html = (
        f"<h2 style='font-size:18px;color:#111;margin:0 0 12px;'>"
        f"【{html.escape(heading_push_type)}】"
        f"{html.escape(str(payload.get('route') or '航班监控'))}</h2>"
    )
    if freshness_headline:
        heading_html += (
            "<div style='margin:-4px 0 12px;color:#666;font-size:12px;'>"
            f"{html.escape(freshness_headline)}</div>"
        )
    cards = [
        heading_html,
        _email_card(
            "行动面板",
            _email_action_panel_body(payload, primary_plan, verify_text, price_reason),
        ),
    ]
    if no_primary:
        cards.append(_email_card("候选池参考", _email_no_primary_candidate_reference_body(payload)))
        cards.append(_email_card("价格信号", _email_no_primary_price_signal_body(payload)))
    else:
        cards.extend(
            [
                _email_card(
                    "价格口径与信号",
                    _email_table(
                        [
                            ("价格信号", html.escape(f"{price_signal.get('label') or '待确认'} - {_price_signal_summary_with_provenance(payload) or '搜索参考价用于判断便不便宜'}")),
                            ("执行建议", html.escape(f"{execution_advice.get('label') or '待确认'} - {execution_advice.get('summary') or price_reason}")),
                            (
                                "搜索参考价",
                                _email_price_span(payload.get("budget_compare_price") or payload.get("display_price") or payload.get("current_price"), "#2563eb"),
                            ),
                            ("预估实付价", _email_price_span(payload.get("transaction_price"), "#111")),
                            ("本次验证价", html.escape(verify_text)),
                            ("理想入手价", _price_text(payload.get("ideal_price"))),
                            ("最高可接受价", _price_text(payload.get("max_price"))),
                            ("验证价说明", html.escape(str(payload.get("buy_condition_explanation") or ""))),
                        ]
                    )
                    + "<div style='margin-top:8px;color:#666;font-size:12px;'>价格信号只回答“便不便宜”；执行建议只回答“能不能按当前条件下单”。</div>",
                ),
                (
                    '<div style="display:none;">'
                    f"<b>当前判断：</b>{html.escape(str(payload.get('recommendation') or '可以观察'))}"
                    f"<b>原因：</b>{html.escape(price_reason)}"
                    f"<b>搜索参考价：</b>{_price_text(payload.get('display_price') or payload.get('current_price'))}"
                    f"<b>预估实付价：</b>{_price_text(payload.get('transaction_price'))}"
                    f"<b>本次方案验证价：</b>{html.escape(verify_text)}"
                    f"<b>你的理想入手价：</b>{_price_text(payload.get('ideal_price'))}"
                    f"<b>最高可接受价：</b>{_price_text(payload.get('max_price'))}"
                    "</div>"
                ),
            ]
        )
    if payload.get("feedback_ack"):
        cards.insert(1, _email_card("反馈已响应", html.escape(str(payload.get("feedback_ack")))))
    if no_primary:
        insert_at = 2
        same_day_alternatives_body = _same_day_alternatives_body(payload)
        if not same_day_alternatives_body:
            from notification_sections import section_fallback

            same_day_alternatives_body = (
                "<div style='color:#888;font-size:12px;'>"
                f"{html.escape(section_fallback('alternative_plans', '没有完整去返航班组合'))}</div>"
            )
        cards.insert(insert_at, _email_card("可选备选方案", same_day_alternatives_body))
        insert_at += 1
        same_day_note = _same_day_note_for_no_primary(payload, _no_primary_reason(payload))
        if same_day_note:
            cards.insert(
                insert_at,
                _email_card(
                    "为什么没有符合方案",
                    html.escape(same_day_note),
                ),
            )
    cards.append(
        _render_payload_plan_cards(
            payload,
            payload.get("recommended_plans") or [],
            primary_plan,
            rendered_full_keys=rendered_full_keys,
            render_stats=render_stats,
            section="main",
        )
    )
    source_channel_body = _email_source_channel_price_body(payload)
    if source_channel_body:
        cards.append(_email_card("首选方案A渠道价对照", source_channel_body))
    adjustment_plans = payload.get("adjustment_required_plans") or []
    if adjustment_plans:
        cards.append(
            _email_card(
                "需调整动身时间的方案",
                _render_payload_plan_cards(
                    payload,
                    adjustment_plans[:3],
                    primary_plan,
                    rendered_full_keys=rendered_full_keys,
                    render_stats=render_stats,
                    section="adjustment",
                )
                + "<div style='margin-top:8px;color:#666;font-size:12px;'>这些方案航班本身可能合适，但按你填写的动身时间赶不上；可改动身时间或换更晚航班。</div>",
            )
        )
    cabin_policy_body = _cabin_policy_summary_body(payload)
    if cabin_policy_body:
        cards.append(_email_card("经济舱 / 商务舱并列参考", cabin_policy_body))
    if _should_show_airport_comparison(payload):
        cards.append(_email_card(_airport_section_title(payload), _email_airport_cost_comparison_body(payload)))

    profile_explanation = payload.get("travel_profile_explanation") or {}
    recommendation_basis = payload.get("recommendation_basis") or {}
    profile_dimensions = profile_explanation.get("dimensions") or {}
    scenario_label = " + ".join(recommendation_basis.get("scenario_labels") or [])
    profile_rows = [
        ("出行场景", html.escape(str(scenario_label or profile_explanation.get("scenario_label") or "个人出行"))),
        (
            "排序依据",
            html.escape(
                str(
                    recommendation_basis.get("plain_language")
                    or profile_explanation.get("basis")
                    or "按价格、时间、舒适度和执行风险综合排序。"
                )
            ),
        ),
        ("场景话术", html.escape(str(payload.get("scenario_recommendation") or ""))),
    ]
    if recommendation_basis.get("conflict_note") or profile_explanation.get("tradeoff"):
        profile_rows.append(
            (
                "权衡说明",
                html.escape(str(recommendation_basis.get("conflict_note") or profile_explanation.get("tradeoff"))),
            )
        )
    applied_rules = recommendation_basis.get("applied_rules") or []
    if applied_rules:
        profile_rows.append(("实际生效规则", "<br>".join(html.escape(str(item)) for item in applied_rules[:4])))
    sort_factors = recommendation_basis.get("sort_factors") or []
    if sort_factors:
        profile_rows.append(
            (
                "排序因子",
                " | ".join(f"{html.escape(str(name))}:{html.escape(str(level))}" for name, level in sort_factors),
            )
        )
    if not sort_factors:
        for key, value in profile_dimensions.items():
            profile_rows.append((str(key), html.escape(str(value))))
    if payload.get("time_filter_note"):
        profile_rows.append(("时间筛选", html.escape(str(payload.get("time_filter_note")))))
    if (payload.get("travel_profile") or {}).get("stock_check") == "high":
        travel_profile = payload.get("travel_profile") or {}
        passenger_count = travel_profile.get("passenger_count")
        breakdown = _passenger_breakdown_text(travel_profile.get("passengers"))
        breakdown_text = f"({breakdown})" if breakdown else ""
        profile_rows.append(
            (
                "多人同行提示",
                f"{int(passenger_count)}人出行{breakdown_text}，低价舱位库存可能不足，建议尽快验证能否同时预订{int(passenger_count)}张。"
                if isinstance(passenger_count, (int, float)) and passenger_count > 1
                else "低价舱位库存可能不足，建议尽快验证支付页能否同时预订多张。",
            )
        )
    cards.append(_email_card("推荐依据", _email_table(profile_rows)))

    cards.append(_email_card("为什么提醒你", _email_list(_non_price_change_reasons(payload), 3)))
    status_text = _plan_status_change_text(payload)
    if status_text:
        cards.append(_email_card("上次推荐方案追踪", html.escape(status_text)))
    cards.append(_email_card("价格走势", _email_trend_card_body(payload)))
    tcurve_body = _email_tcurve_body(payload)
    if tcurve_body:
        cards.append(_email_card("提前购买参考(同航线历史观测)", tcurve_body))
    forecast_body = _email_forecast_body(payload)
    if forecast_body:
        cards.append(_email_card("价格预测参考(实验)", forecast_body))
    calendar_payload = payload.get("price_calendar") or {}
    if calendar_payload.get("rows") or calendar_payload.get("uncollected_rows"):
        cards.append(_email_card("低价日历", _email_price_calendar_body(payload)))

    action_rows = [
        ("购买条件", html.escape(str(payload.get("buy_condition") or "以支付页为准"))),
        ("理想入手价", _price_text(payload.get("ideal_price"))),
        ("最高可接受价", _price_text(payload.get("max_price"))),
    ]
    action = payload.get("action_range") or {}
    if action.get("current_label"):
        action_rows.append(("价格信号", html.escape(_price_signal_summary_with_provenance(payload) or str(action.get("current_label")))))
    if not no_primary:
        cards.append(
            _email_card(
                "操作建议",
                _email_table(action_rows)
                + "<div style='margin-top:8px;color:#666;font-size:12px;'>若支付页最终价、行李和票规不满足上方条件，建议保持本条航线监控。</div>",
            )
        )

    if (not no_primary) and payload.get("same_day_no_feasible_note"):
        cards.append(
            _email_card(
                "当天往返时间提示",
                html.escape(str(payload.get("same_day_no_feasible_note"))),
            )
        )
    same_day_alternatives_body = _same_day_alternatives_body(payload)
    if (not no_primary) and same_day_alternatives_body:
        cards.append(_email_card("可选备选方案", same_day_alternatives_body))

    cards.append(_email_card("为什么不推荐更便宜方案", _email_excluded_compact_body(payload)))

    diff_from_last = payload.get("diff_from_last") or {}
    diff = _to_float(diff_from_last.get("diff"))
    scope_suffix = _price_change_scope_suffix(diff_from_last)
    change_lines = []
    if (not no_primary) and diff is not None:
        if diff is not None:
            if diff < 0:
                change_lines.append(
                    f"<div>较上次提醒：下降{_price_text(abs(diff))}{scope_suffix}</div>"
                )
            elif diff > 0:
                change_lines.append(
                    f"<div>较上次提醒：上涨{_price_text(diff)}{scope_suffix}</div>"
                )
            else:
                change_lines.append(f"<div>较上次提醒：价格持平{scope_suffix}</div>")
        change_lines.append(f"<div>本次提醒主要由“{html.escape(str(payload.get('push_type') or '价格变化'))}”触发。</div>")
    if (not no_primary) and action.get("ranges"):
        range_lines = []
        for row in action["ranges"]:
            range_lines.append(f"<div>{html.escape(_action_range_display_text(row))}</div>")
        change_lines.extend(range_lines)
    if change_lines:
        cards.append(_email_card("价格变化与参考区间", "".join(change_lines)))

    cards.append(_email_card("数据来源", _email_source_body(payload)))
    if no_primary:
        freshness_text = _data_freshness_headline(payload) or (
            f"采集时间:{_payload_freshness_text(payload)}"
            if payload.get("collected_at")
            else "本轮采集时点未记录"
        )
        cards.extend(
            [
                _email_card("数据时点", html.escape(freshness_text)),
                _email_card("配额总览", html.escape(_quota_overview_text())),
                _email_card(
                    "数据依据",
                    _detail_provenance_body(payload)
                    or "<div style='color:#888;font-size:12px;'>本次未引用历史统计值。</div>",
                ),
            ]
        )
        cards = _ensure_no_match_notification_sections(cards, payload)

    cards.append(
        _email_card(
            "更多分析",
            f'排除方案详情、置信度拆解、购买前检查清单和详细数据来源见网页详情页：'
            f'<a href="{html.escape(str(payload.get("detail_url") or ""))}" target="_blank">查看网页版完整分析(如未显示请稍后刷新)</a>',
        )
    )

    cards.append(
        _email_card(
            "操作链接",
            _email_action_links(payload, primary_plan, include_channel_picker=False)
            + f"<div style='margin-top:8px;color:#666;font-size:12px;'>数据采集于 {html.escape(str(payload.get('collected_at') or ''))}。最终价格以购买平台支付页为准。</div>",
        )
    )
    _log_render_stats(render_stats)
    return subject, "".join(cards)


@_with_render_log_channel("详情")
def render_detail_html(payload: dict) -> str:
    """Render the web detail page HTML with core modules visible and details folded."""
    payload = payload or {}
    subject = _email_subject(payload)
    if _data_incomplete_state(payload):
        return _render_data_incomplete_report(
            payload,
            subject,
            interactive_channels=True,
        )
    verify_text = f"支付页≤{_price_text(payload.get('verify_price'))}" if payload.get("verify_price") else "以支付页为准"
    price_reason = str(payload.get("price_policy_reason") or "请以预估实付价和支付页最终价为准")
    primary_plan = _plan_for_render((payload.get("recommended_plans") or [{}])[0] or {}, payload)
    no_primary = _no_primary_plan_state(payload)
    rendered_full_keys = set()
    render_stats = {
        "full_main": 0,
        "full_adjustment": 0,
        "compact_refs": 0,
        "full_identity_counts": {},
    }
    freshness_headline = _data_freshness_headline(payload)
    heading_html = (
        f"<h2 style='font-size:18px;color:#111;margin:0 0 12px;'>"
        f"{html.escape(subject)}</h2>"
    )
    if freshness_headline:
        heading_html += (
            "<div style='margin:-4px 0 12px;color:#666;font-size:12px;'>"
            f"{html.escape(freshness_headline)}</div>"
        )
    cards = [
        heading_html,
        _email_card(
            "行动面板",
            _email_action_panel_body(
                payload,
                primary_plan,
                verify_text,
                price_reason,
                interactive_channels=True,
            ),
        ),
        _render_payload_plan_cards(
            payload,
            payload.get("recommended_plans") or [],
            primary_plan,
            rendered_full_keys=rendered_full_keys,
            render_stats=render_stats,
            section="main",
        ),
        _email_card("为什么提醒你", _email_list(_non_price_change_reasons(payload), 3)),
        _email_card("价格走势", _email_trend_card_body(payload)),
    ]
    source_channel_body = _email_source_channel_price_body(payload)
    if source_channel_body:
        cards.insert(3, _email_card("首选方案A渠道价对照", source_channel_body))
    if payload.get("feedback_ack"):
        cards.insert(2, _email_card("反馈已响应", html.escape(str(payload.get("feedback_ack")))))
    if no_primary:
        cards.insert(2, _email_card("候选池参考", _email_no_primary_candidate_reference_body(payload)))
        insert_at = 2
        same_day_alternatives_body = _same_day_alternatives_body(payload)
        if not same_day_alternatives_body:
            from notification_sections import section_fallback

            same_day_alternatives_body = (
                "<div style='color:#888;font-size:12px;'>"
                f"{html.escape(section_fallback('alternative_plans', '没有完整去返航班组合'))}</div>"
            )
        cards.insert(insert_at, _email_card("可选备选方案", same_day_alternatives_body))
        insert_at += 1
        cards.insert(insert_at, _email_card("价格信号", _email_no_primary_price_signal_body(payload)))
        insert_at += 1
        same_day_note = _same_day_note_for_no_primary(payload, _no_primary_reason(payload))
        if same_day_note:
            cards.insert(
                insert_at,
                _email_card(
                    "为什么没有符合方案",
                    html.escape(same_day_note),
                ),
            )
    status_text = _plan_status_change_text(payload)
    if status_text:
        cards.insert(4, _email_card("上次推荐方案追踪", html.escape(status_text)))
    cabin_policy_body = _cabin_policy_summary_body(payload)
    if cabin_policy_body:
        cards.insert(3, _email_card("经济舱 / 商务舱并列参考", cabin_policy_body))
    if _should_show_airport_comparison(payload):
        cards.insert(3, _email_card(_airport_section_title(payload), _email_airport_cost_comparison_body(payload)))

    action_rows = [
        ("购买条件", html.escape(str(payload.get("buy_condition") or "以支付页为准"))),
        ("理想入手价", _price_text(payload.get("ideal_price"))),
        ("最高可接受价", _price_text(payload.get("max_price"))),
    ]
    if not no_primary:
        cards.append(
            _email_card(
                "操作建议",
                _email_table(action_rows)
                + "<div style='margin-top:8px;color:#666;font-size:12px;'>若支付页最终价、行李和票规不满足上方条件，建议保持本条航线监控。</div>",
            )
        )

    if (not no_primary) and payload.get("same_day_no_feasible_note"):
        cards.append(
            _detail_section(
                "当天往返时间提示",
                html.escape(str(payload.get("same_day_no_feasible_note"))),
            )
        )
    same_day_alternatives_body = _same_day_alternatives_body(payload)
    if (not no_primary) and same_day_alternatives_body:
        cards.append(_detail_section("可选备选方案", same_day_alternatives_body))

    excluded_body = _email_excluded_compact_body(payload)
    cards.append(_detail_section("展开:排除方案", excluded_body))

    cards.append(_detail_section("展开:价格走势详情", _email_trend_card_body(payload) + _email_detail_charts_body(payload)))

    calendar_payload = payload.get("price_calendar") or {}
    if calendar_payload.get("rows") or calendar_payload.get("uncollected_rows"):
        cards.append(_detail_section("展开:低价日历", _email_price_calendar_body(payload)))

    checklist = payload.get("checklist") or []
    checklist_body = "".join(f"<div>□ {html.escape(str(item))}</div>" for item in checklist) or "<div style='color:#888;font-size:12px;'>暂无检查清单。</div>"
    cards.append(_detail_section("展开:购买前检查清单", checklist_body))

    dims = payload.get("confidence_dimensions") or {}
    if dims:
        details = payload.get("confidence_details") or {}
        confidence_rows = []
        for name, level in dims.items():
            detail = details.get(name)
            text = html.escape(str(level))
            if detail:
                text += f"<span style='color:#666;font-size:12px;'>（{html.escape(str(detail))}）</span>"
            confidence_rows.append((str(name), text))
        confidence_body = _email_table(confidence_rows)
    else:
        confidence_body = "<div style='color:#888;font-size:12px;'>暂无置信度拆解。</div>"
    cards.append(_detail_section("展开:置信度拆解", confidence_body))

    cards.append(_detail_section("数据依据", _detail_provenance_body(payload)))
    cards.append(_detail_section("展开:详细数据来源", _detail_technical_source_body(payload)))

    cards.append(
        _email_card(
            "下一步",
            _email_action_links(
                payload,
                primary_plan,
                interactive_channels=True,
                include_channel_picker=False,
            )
            + f"<div style='margin-top:8px;color:#666;font-size:12px;'>数据采集于 {html.escape(str(payload.get('collected_at') or ''))}。最终价格以购买平台支付页为准。</div>",
        )
    )
    _log_render_stats(render_stats)
    return "".join(cards)


def persist_notification_payload(payload: dict) -> None:
    """Persist latest push price/snapshot after a channel has accepted the message."""
    payload = payload or {}
    snapshot = payload.get("snapshot") or {}
    route = snapshot.get("route")
    depart_date = snapshot.get("depart_date")
    if not route or not depart_date:
        return
    now = datetime.now().isoformat(timespec="seconds")
    subscription_id = _first_nonempty_identity(
        snapshot.get("subscription_id"),
        payload.get("subscription_id"),
    )
    save_last_push_price(
        route,
        depart_date,
        snapshot.get("return_date"),
        payload.get("current_price"),
        payload.get("push_type"),
        now,
        subscription_id=subscription_id,
    )
    save_push_snapshot(
        route,
        depart_date,
        snapshot.get("return_date"),
        payload.get("current_price"),
        payload.get("confidence"),
        snapshot.get("channels") or [],
        snapshot.get("fare_status") or "",
        payload.get("push_type"),
        now,
        constraint_fingerprint=snapshot.get("constraint_fingerprint"),
        constraint_sample_n=snapshot.get("constraint_sample_n"),
        subscription_id=subscription_id,
    )
    save_pushed_plans(
        _first_nonempty_identity(
            payload.get("snapshot", {}).get("subscription_id"),
            payload.get("subscription_id"),
            snapshot.get("subscription_id"),
            route,
        ),
        payload.get("recommended_plans") or [],
    )


def format_html_message(
    analysis_result=None,
    route_info=None,
    source_stats=None,
    price_insights=None,
    outbound_analysis=None,
    return_analysis=None,
    detail_level=None,
    enforce_pushplus_limit=True,
):
    """生成压缩版HTML消息。"""
    message = _format_structured_html_message(
        analysis_result=analysis_result,
        route_info=route_info,
        source_stats=source_stats,
        price_insights=price_insights,
        outbound_analysis=outbound_analysis,
        return_analysis=return_analysis,
        compact=False,
        detail_level=detail_level,
        persist_snapshot=False,
    )
    if enforce_pushplus_limit and len(message) > PUSHPLUS_COMPACT_CHARS:
        message = _format_structured_html_message(
            analysis_result=analysis_result,
            route_info=route_info,
            source_stats=source_stats,
            price_insights=price_insights,
            outbound_analysis=outbound_analysis,
            return_analysis=return_analysis,
            compact=True,
            detail_level=detail_level,
            persist_snapshot=True,
        )
    else:
        message = _format_structured_html_message(
            analysis_result=analysis_result,
            route_info=route_info,
            source_stats=source_stats,
            price_insights=price_insights,
            outbound_analysis=outbound_analysis,
            return_analysis=return_analysis,
            compact=False,
            detail_level=detail_level,
            persist_snapshot=True,
        )
    return message
class ComparisonMessageUnavailable(RuntimeError):
    """The retired legacy comparison semantics cannot be rendered safely."""


def _format_comparison_details(
    analysis_result: dict, route_info: dict, source_stats=None
) -> str:
    """Fail explicitly instead of reconstructing retired purchase-advice semantics."""
    raise ComparisonMessageUnavailable(
        "历史方案对比语义已退役,缺少当前口径的总结规则"
    )


def _comparison_location_label(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    code = text.upper()
    if 2 <= len(code) <= 4 and code.isalpha():
        city = get_airport_city(code)
        return city if city and city != code else code
    return text


def _comparison_conclusion_text(value) -> str:
    if isinstance(value, dict):
        value = value.get("conclusion") or value.get("label")
    return str(value or "").strip()


def _comparison_message_fallback(
    analysis_result: dict, route_info: dict, source_stats=None
) -> str:
    origin = _comparison_location_label(route_info.get("origin"))
    destination = _comparison_location_label(route_info.get("destination"))
    depart_date = str(route_info.get("depart_date") or "").strip()
    lines = [
        f"✈️ {origin} → {destination}",
        "",
    ]
    if depart_date:
        lines.extend([f"📅 出发日期：{depart_date}", ""])
    lines.append("方案对比详情暂不可用,核心推荐不受影响")

    conclusion = _comparison_conclusion_text(analysis_result.get("conclusion"))
    if conclusion:
        lines.append(f"当前结论：{conclusion}")

    prices = analysis_result.get("price_range") or []
    if prices and _has_valid_price(prices[0]):
        lines.append(f"当前最低参考价：{_price_text(prices[0])}(沿用输入口径)")

    recommendations = analysis_result.get("recommendations") or []
    if recommendations:
        flight = recommendations[0].get("flight") or {}
        detail = format_flight_detail(flight, depart_date or None, "").replace(
            "<br>", "\n"
        )
        if detail:
            lines.append(f"首选方案概要：{detail}")

    source_summary = format_source_summary(
        source_stats
        or route_info.get("source_stats")
        or analysis_result.get("source_stats")
    )
    if source_summary:
        lines.append(source_summary.replace("<br>", "\n"))

    detail_url = valid_detail_url(
        route_info.get("detail_url") or analysis_result.get("detail_url")
    )
    if detail_url:
        lines.append(f"网页详情：{detail_url}")
    else:
        lines.append("网页详情未配置,完整结果见本通知")

    lines.extend(
        [
            "",
            "以上内容仅保留输入中已有的结论、价格与方案概要。",
            "实际购买请以航司或OTA官网价格为准。",
        ]
    )
    return "\n".join(lines)


def format_comparison_message(
    analysis_result: dict, route_info: dict, source_stats=None
) -> str:
    """Render the legacy entry safely without inventing retired comparison claims."""
    try:
        return _format_comparison_details(analysis_result, route_info, source_stats)
    except ComparisonMessageUnavailable as exc:
        safe_log(f"[方案对比降级] 原因={exc} 处置=仅展示已有核心信息")
        return _comparison_message_fallback(
            analysis_result,
            route_info,
            source_stats,
        )

















