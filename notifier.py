"""PushPlus notification helpers."""

from __future__ import annotations

import os
import re
import json
import time
import html
from datetime import date, datetime
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
from analyzer import (
    build_execution_advice,
    build_price_signal,
    build_recommendation_basis,
    build_travel_profile,
    calculate_price_references,
    calc_confidence,
    classify_plan_tier,
    determine_push_type,
    generate_decision_summary,
    generate_trend_summary,
    get_total_passengers,
    multi_window_analysis,
    price_position_description,
    travel_profile_explanation,
    waiting_risk_description,
)
from storage import (
    get_lowest_price_history,
    get_last_push_price,
    get_last_push_snapshot,
    get_roundtrip_price_history,
    save_last_push_price,
    save_push_snapshot,
)


BUY_SIGNALS = {"strong_buy", "buy", "buy_now"}
BASE_DIR = Path(__file__).parent
NOTIFICATIONS_LOG = BASE_DIR / "data" / "notifications_log.txt"
PUSHPLUS_MAX_CHARS = 30000
PUSHPLUS_COMPACT_CHARS = 25000
COMPACT_NOTICE = "瀹屾暣鏂规璇︽儏鍥犵瘒骞呴檺鍒跺凡绮剧畝锛屽闇€鏌ョ湅鍏ㄩ儴鏂规璇峰洖澶?璇︽儏'"


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


def _compact_booking_line(line: str) -> str:
    if "<a" not in line:
        return line
    parts = line.split(" | ")
    kept = [
        part
        for part in parts
        if "携程" in part or "飞猪" in part or "去哪儿" in part
    ]
    return " | ".join(kept) if kept else line


def _append_compact_notice(content: str) -> str:
    if COMPACT_NOTICE in content:
        return content
    return f"{content}<br><br>{COMPACT_NOTICE}"


def _hard_limit_pushplus_message(content: str, limit: int = PUSHPLUS_MAX_CHARS) -> str:
    notice = f"<br><br>{COMPACT_NOTICE}"
    if len(content) <= limit:
        return content
    keep = max(0, limit - len(notice) - 20)
    return content[:keep] + notice


def _compact_pushplus_message(content: str, level: int = 1) -> str:
    """Shrink generated HTML when PushPlus rejects overly long messages."""
    lines = str(content or "").split("<br>")
    compacted = []
    checklist_count = None
    skip_price_explanation = False

    for raw_line in lines:
        line = raw_line
        stripped = re.sub(r"<[^>]+>", "", line).strip()

        if skip_price_explanation:
            if not stripped or "━" in stripped:
                skip_price_explanation = False
            else:
                continue

        if level >= 1:
            if "<a " in line:
                line = _compact_booking_line(line)
            if re.match(r"^(No\.[4-9]|[4-9]\.)", stripped):
                continue
            if "璐拱鍓嶈纭" in stripped:
                checklist_count = 0
                compacted.append(line)
                continue
            if checklist_count is not None and stripped.startswith("□"):
                checklist_count += 1
                if checklist_count > 5:
                    continue
            elif checklist_count is not None and not stripped.startswith("□"):
                checklist_count = None

            verbose_price_words = [
                "历史最低",
                "鍘嗗彶骞冲潎",
                "历史最高",
                "数据量",
                "浠锋牸涓婃定姒傜巼",
                "浠锋牸涓嬮檷姒傜巼",
                "平均涨",
                "平均降",
                "近60天",
                "数据点",
            ]
            if any(word in stripped for word in verbose_price_words):
                continue

        if level >= 2:
            if "鍏充簬浠锋牸璇存槑" in stripped:
                skip_price_explanation = True
                continue
            if "执行评估" in stripped or "绁ㄨ鏍￠獙" in stripped:
                continue
            if stripped.startswith(("├", "└")) and "综合等级" not in stripped:
                continue
            if any(word in stripped for word in ("绁ㄨ鍖归厤", "可购买性", "执行风险")):
                continue

        compacted.append(line)

    return _append_compact_notice("<br>".join(compacted))


def _prepare_pushplus_content(content: str) -> str:
    print(f"[鎺ㄩ€乚 娑堟伅闀垮害: {len(content)} 瀛楃")
    if len(content) <= PUSHPLUS_COMPACT_CHARS:
        return content

    compacted = _compact_pushplus_message(content, level=1)
    print(f"[鎺ㄩ€乚 娑堟伅杈冮暱锛屽凡绮剧畝: {len(compacted)} 瀛楃")
    if len(compacted) <= PUSHPLUS_MAX_CHARS:
        return compacted

    compacted = _compact_pushplus_message(compacted, level=2)
    print(f"[鎺ㄩ€乚 浜屾绮剧畝鍚庨暱搴? {len(compacted)} 瀛楃")
    if len(compacted) > PUSHPLUS_MAX_CHARS:
        compacted = _hard_limit_pushplus_message(compacted)
        print(f"[鎺ㄩ€乚 纭埅鏂悗闀垮害: {len(compacted)} 瀛楃")
    return compacted


def _post_pushplus(pushplus_token: str, title: str, content: str):
    resp = httpx.post(
        "https://www.pushplus.plus/send",
        json={
            "token": pushplus_token,
            "title": title,
            "content": content,
            "template": "html",
        },
        timeout=30,
    )
    if not resp.text:
        print(f"[鎺ㄩ€乚 PushPlus杩斿洖绌哄搷搴旓紝娑堟伅鍙兘杩囬暱({len(content)}瀛楃)")
        return None
    try:
        return resp.json()
    except json.JSONDecodeError:
        print(f"[鎺ㄩ€乚 JSON瑙ｆ瀽澶辫触锛屽搷搴斿唴瀹? {resp.text[:200]}")
        print(f"[推送] 消息长度: {len(content)}字符，可能超出限制")
        return None


DISCLAIMER = "以上内容基于历史价格数据分析，仅供参考。\n实际购买请以航司或OTA官网价格为准。"


def format_price(price) -> str:
    """Format a CNY price."""
    try:
        value = float(price)
    except (TypeError, ValueError):
        return "暂无报价"
    if value <= 0:
        return "暂无报价"
    return f"¥{value:,.0f}"


def _has_valid_price(price) -> bool:
    try:
        return float(price) > 0
    except (TypeError, ValueError):
        return False


def _price_text(price) -> str:
    return format_price(price)


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


def _flight_status_tags(flight: dict | None, route_info: dict | None = None, analysis_result: dict | None = None) -> str:
    flight = flight or {}
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
    return f"{price_tag} | {buy_tag} | {confidence_tag} | {risk_tag}"


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
        return f"当前处于{window}，系统会继续监控价格变化。"
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
        f"路线：{_display_route_summary(alt.get('route_summary', '-'))}",
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


def send(content: str, title: str = "航班监控通知") -> bool:
    """发送推送通知，优先 PushPlus。"""
    pushplus_token = os.environ.get("PUSHPLUS_TOKEN", "")
    if not pushplus_token:
        _log_notification(content)
        print("[推送] 未配置 PUSHPLUS_TOKEN，已写入本地通知日志")
        return False
    msg = _prepare_pushplus_content(content)
    title = _notification_title_from_content(msg, title)
    print(f"[推送] 消息长度: {len(msg)} 字符")
    result = _post_pushplus(pushplus_token, title, msg)
    if result and result.get("code") == 200:
        print("PushPlus推送成功")
        return True
    print(f"PushPlus返回异常: {result}")
    if result is None:
        compact_msg = _compact_pushplus_message(msg, level=2)
        if compact_msg != msg:
            result = _post_pushplus(pushplus_token, title, compact_msg)
            if result and result.get("code") == 200:
                print("PushPlus精简后推送成功")
                return True
    _log_notification(content)
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
        aircraft = segment.get("aircraft")
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
        segment.get("aircraft", "")
        for segment in _email_plan_segments(flight)
        if segment.get("aircraft")
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
            f"若最终价超过{target_price_text}，建议继续监控。",
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


def _booking_link(origin: str, dest: str, date_str: str, label: str) -> str:
    style = "color:#1a73e8;text-decoration:underline;"
    url = _google_flights_url(origin, dest, date_str)
    return f'<a href="{url}" style="{style}">{label}</a>'


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
    return str(aircraft).strip() or "机型待确认"


def _round_trip_score_text(flight: dict) -> str:
    return _human_recommendation_text(flight)


def format_flight_detail(
    flight: dict,
    date_str: str | None = None,
    label: str | None = None,
    route_info: dict | None = None,
    analysis_result: dict | None = None,
) -> str:
    """统一格式化每个航班方案，推荐和备选方案共用。"""
    segments = flight.get("segments") or []
    if not flight or (not segments and not flight.get("flight_combo")):
        return f"{label + ': ' if label else ''}航班信息待确认"

    flight_no = _compact_flight_numbers(flight)
    airline = _round_trip_airline_text(flight)
    price_text = _flight_price_text(flight)
    estimate_lines = _price_estimate_summary_lines(flight)
    first_segment = segments[0] if segments else {}
    last_segment = segments[-1] if segments else {}
    dep_airport = first_segment.get("dep_airport") or first_segment.get("departure_airport") or ""
    arr_airport = last_segment.get("arr_airport") or last_segment.get("arrival_airport") or ""

    dep_text = _month_day_time(first_segment.get("dep_time"), date_str) if segments else _round_trip_date_text(date_str)
    arr_time = _time_only(last_segment.get("arr_time")) if segments else ""
    dep_tz = get_airport_timezone(dep_airport)
    arr_tz = get_airport_timezone(arr_airport)
    show_timezone = bool(dep_airport and arr_airport and dep_tz != arr_tz)
    dep_date_text, _, dep_time_text = dep_text.rpartition(" ")
    if dep_time_text:
        dep_text = " ".join(part for part in [dep_date_text, _time_with_timezone(dep_time_text, dep_airport, show_timezone)] if part)
    arr_text = _time_with_timezone(arr_time, arr_airport, show_timezone) if arr_time else "时间待确认"

    route_text = f"{city_name(dep_airport)} → {city_name(arr_airport)}" if dep_airport or arr_airport else _display_route_summary(flight.get("route_summary", ""))
    prefix = f"{label}: " if label else ""
    detail = (
        f"{prefix}{flight_no} {airline} | {price_text}<br>"
        f"  🏷 {_flight_status_tags(flight, route_info, analysis_result)}<br>"
        f"  {_human_recommendation_text(flight, route_info, analysis_result)}<br>"
        f"  {route_text}<br>"
        f"  {dep_text}起飞 → {arr_text}到达 | {_round_trip_stops_text(flight)} "
        f"{_round_trip_duration_text(flight)} | {_flight_slot_label(flight)} | "
        f"机型: {_round_trip_aircraft_text(flight)}"
    )
    for estimate_line in estimate_lines:
        detail += f"<br>  {estimate_line}"
    if show_timezone:
        detail += "<br>  到达时间按当地时间计算"

    search_date = _flight_search_date(flight, date_str)
    booking_links = (
        generate_booking_links(dep_airport, arr_airport, search_date, flight_no, flight=flight)
        if dep_airport and arr_airport and search_date
        else ""
    )
    if booking_links:
        title = "购买渠道" if _verified_booking_options(flight) else "可能的购买渠道"
        detail += f"<br>  🔗 {title}: {booking_links}"
        detail += f"<br>  {_booking_options_hint(flight)}"
    discrepancy_notice = _price_discrepancy_notice(flight)
    if discrepancy_notice:
        detail += f"<br>  {discrepancy_notice}"
    detail += f"<br>  价格采集于{_collected_time_text(flight)} | 新鲜度：{_freshness_label(flight)}"
    if _status_risk_label(flight) != "风险低":
        detail += "<br>  ⚠️ 详细风险请在支付页确认票规、中转和行李规则"
    return detail

def _round_trip_option_line(
    index: int,
    flight: dict,
    date_str: str | None = None,
    route_info: dict | None = None,
    analysis_result: dict | None = None,
) -> str:
    return format_flight_detail(
        flight, date_str, _option_label(index), route_info, analysis_result
    )


def _append_round_trip_recommendations(
    lines: list[str],
    title: str,
    origin: str,
    destination: str,
    depart_date: str | None,
    flights: list[dict] | None,
    route_info: dict | None = None,
    analysis_result: dict | None = None,
    limit: int = 5,
) -> None:
    flights = flights or []
    if not flights:
        return
    lines.append(
        f"<b>{title}：{_round_trip_city_code(origin)} → "
        f"{_round_trip_city_code(destination)} | {_round_trip_date_text(depart_date)}</b>"
    )
    lines.append("━━━ 推荐方案 ━━━")
    for index, flight in enumerate(flights[:limit]):
        lines.append(
            _round_trip_option_line(index, flight, depart_date, route_info, analysis_result)
        )
    lines.append("")


def _append_simple_top3(lines: list[str], title: str, flights: list[dict] | None) -> None:
    _append_round_trip_recommendations(lines, title, "", "", "", flights, limit=3)


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


def _round_trip_score_line(index: int, flight: dict, date_str: str | None = None) -> str:
    flight_no = _compact_flight_numbers(flight)
    airline = _round_trip_airline_text(flight)
    price = flight.get("price")
    price_text = _price_text(price)
    line = (
        f"{index}. {flight_no} {airline} | {price_text} | "
        f"{_round_trip_time_range(flight)} | {_flight_slot_label(flight)} | "
        f"{_round_trip_stops_text(flight)} | "
        f"{_round_trip_aircraft_text(flight)} | {_round_trip_score_text(flight)}"
    )
    links = _combo_full_booking_links(flight, date_str)
    if links:
        line += f"<br>  馃敆 {links}"
    return line


def _append_round_trip_score_top3(
    lines: list[str],
    outbound_analysis: dict,
    return_analysis: dict,
    route_info: dict | None = None,
) -> None:
    outbound_ranked = _round_trip_score_flights(outbound_analysis)
    return_ranked = _round_trip_score_flights(return_analysis)
    if not outbound_ranked and not return_ranked:
        return
    lines.append("<b>猸?缁煎悎璇勫垎Top3</b>")
    if outbound_ranked:
        lines.append("鈹佲攣 鍘荤▼ 鈹佲攣")
        for index, flight in enumerate(outbound_ranked, start=1):
            lines.append(_round_trip_score_line(index, flight, (route_info or {}).get("depart_date")))
    if return_ranked:
        lines.append("鈹佲攣 杩旂▼ 鈹佲攣")
        for index, flight in enumerate(return_ranked, start=1):
            lines.append(_round_trip_score_line(index, flight, (route_info or {}).get("return_date")))
    lines.append("")


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
    if target and total <= target * 2:
        return " ✅ 低于理想价"
    if max_budget and total <= max_budget * 2:
        return " ✅ 预算内"
    if max_budget and total > max_budget * 2:
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
        scope = _chart_scope_label(row)
        price_text = _price_text(row["value"])
        if scope:
            price_text += f"({scope})"
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


def _append_round_trip_block(
    lines: list[str],
    outbound_analysis: dict,
    route_info: dict,
    return_analysis: dict | None = None,
) -> None:
    if not route_info.get("round_trip"):
        return

    return_analysis = return_analysis or outbound_analysis.get("return_analysis") or {}
    round_trip = outbound_analysis.get("round_trip_analysis") or {}
    outbound_flights = outbound_analysis.get("all_flights") or []
    return_flights = return_analysis.get("all_flights") or []
    top_combinations = round_trip.get("top_combinations") or []
    max_combo = round_trip.get("max_combination")
    outbound_min = round_trip.get("outbound_min")
    return_min = round_trip.get("return_min")
    total_min = round_trip.get("total_min")

    lines.append("<b>💰 往返总价一览</b>")
    if _has_valid_price(total_min):
        lines.append(f"最优组合：{_price_text(total_min)}（去{_price_text(outbound_min)} + 回{_price_text(return_min)}）")
    if max_combo:
        lines.append(f"最贵组合：{_price_text(max_combo.get('total_price'))}（去{_price_text(max_combo.get('outbound_price'))} + 回{_price_text(max_combo.get('return_price'))}）")
    target = _to_float(route_info.get("target_price"))
    max_budget = _to_float(route_info.get("max_budget") or route_info.get("budget"))
    if target:
        lines.append(f"你的理想总价：{_price_text(target * 2)}")
    if max_budget:
        lines.append(f"你的最高预算：{_price_text(max_budget * 2)}")
    trend = round_trip.get("trend") or {}
    recent_prices = trend.get("recent_prices") or []
    if recent_prices:
        trend_line = " → ".join(_price_text(price) for price in recent_prices)
        lines.append(f"📊 总价趋势：{trend_line} {trend.get('icon', '')} {trend.get('direction', '')}".strip())
    if round_trip.get("advice"):
        lines.append(round_trip["advice"])
    if round_trip.get("mix_match_tip"):
        lines.append(round_trip["mix_match_tip"])
    lines.append("")

    _append_roundtrip_price_reference(lines, round_trip, route_info)
    _append_roundtrip_price_analysis(lines, round_trip)
    _append_option_price_bar_chart(lines, outbound_analysis, True, route_info)

    if top_combinations:
        lines.append("<b>🔄 往返最优组合 Top3</b>")
        for index, combo in enumerate(top_combinations[:3], start=1):
            _append_round_trip_combo_card(lines, index, combo, route_info)
        lines.append("")

    _append_round_trip_change_table(lines, round_trip)
    _append_round_trip_all_options(lines, "去程全部方案（按价格排序）", outbound_flights, route_info.get("depart_date"))
    _append_round_trip_all_options(lines, "返程全部方案（按价格排序）", return_flights, route_info.get("return_date"))
    if return_analysis.get("nearby_dates"):
        _append_nearby_dates(lines, return_analysis.get("nearby_dates"))

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
    total = _to_float(combo.get("total_price"))
    target = _to_float(route_info.get("target_price"))
    target_total = target * 2 if target else None
    if total is None or not target_total:
        price_label = "价格待判断"
    elif total <= target_total:
        price_label = "价格偏低"
    elif total <= target_total * 1.05:
        price_label = "接近理想"
    elif total <= target_total * 1.25:
        price_label = "价格中等"
    else:
        price_label = "价格偏高"

    legs = [combo.get("outbound") or {}, combo.get("return") or {}]
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

    return " | ".join([price_label, availability, f"置信度{(confidence or {}).get('overall', '中')}", risk])

def _combo_direct_first_key(combo: dict) -> tuple[int, float]:
    legs = [combo.get("outbound") or {}, combo.get("return") or {}]
    direct = all(int(flight.get("stops") or 0) == 0 for flight in legs if flight)
    return (0 if direct else 1, _to_float(combo.get("total_price")) or 999999)


def _round_trip_combinations(analysis_result: dict) -> list[dict]:
    round_trip = analysis_result.get("round_trip_analysis") or {}
    combos = [combo for combo in (round_trip.get("top_combinations") or []) if combo]
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
    total = _to_float(combo.get("total_price"))
    target = _to_float(route_info.get("target_price"))
    max_budget = _to_float(route_info.get("max_budget") or route_info.get("budget"))
    target_total = target * 2 if target else None
    max_total = max_budget * 2 if max_budget else None
    grade = _combo_grade(combo)
    if total is None:
        return "建议等待 - 当前总价仍需确认"
    if target_total and total <= target_total and grade == "A":
        return "强烈建议购买 - 往返总价达标且执行信息较完整"
    if target_total and total <= target_total * 1.05:
        return "值得验证 - 价格达标，但购买链路尚未完全确认"
    if max_total and total <= max_total:
        return "可以观察 - 总价仍在预算内"
    return "建议等待 - 当前往返总价或执行信息仍需确认"


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


def _price_history_for_push(price_insights: dict | None, analysis_result: dict, is_round_trip: bool):
    if is_round_trip:
        round_trip = analysis_result.get("round_trip_analysis") or {}
        history = round_trip.get("history") or round_trip.get("price_history") or []
        if history:
            return [item.get("total") if isinstance(item, dict) else item for item in history]
    return (price_insights or {}).get("price_history") or analysis_result.get("price_history") or []


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
        lines.append(f"上次提醒：{_price_text(change.get('last'))}")
        diff = _to_float(change.get("diff"))
        if diff is not None:
            if diff < 0:
                lines.append(f"下降：{_price_text(abs(diff))}")
            elif diff > 0:
                lines.append(f"上涨：{_price_text(diff)}")
            else:
                lines.append("变化：持平")
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
        lines.append(f"若{price_name}涨到{_price_text(max_budget)}以上，建议继续监控。")
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


def _excluded_reason_details(item: dict) -> list[str]:
    reason = str(item.get("reason") or "不符合当前要求")
    details = [reason]

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
    if ("行李" in reason or "托运" in reason) and len(details) == 1:
        details.append("不含托运行李或托运行李额度未确认")
    if ("红眼" in reason or "凌晨" in reason) and len(details) == 1:
        details.append("起飞或到达时间触发默认时间安全规则")
    if ("非联程" in reason or "self" in lower_reason) and len(details) == 1:
        details.append("可能需要自行转机和重新托运行李")
    if ("过夜" in reason or "中转" in reason) and len(details) == 1:
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
    if is_roundtrip and scope != "roundtrip":
        return f"已排除的更低价{label}：{_price_text(price)}"
    diff = None
    current = _to_float(current_price)
    if current is not None and price < current:
        diff = current - price
    diff_text = f"（比推荐便宜{_price_text(diff)}）" if diff else ""
    prefix = "已排除的更低价" if diff else "已排除的"
    return f"{prefix}{label}：{_price_text(price)}{diff_text}"


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
    aircraft = str(segment.get("aircraft") or "").strip()
    left = " ".join(part for part in [flight_no, airline] if part) or "航班信息待确认"
    value = f"{html.escape(left)}｜{html.escape(dep)} {html.escape(dep_time)} → {html.escape(arr)} {html.escape(arr_time)}"
    if aircraft and aircraft not in {"未知", "unknown", "Unknown", "请查询航司官网"}:
        value += f"｜{html.escape(aircraft)}"
    return value


def _excluded_aircraft_text(flight: dict) -> str:
    aircrafts = []
    for segment in flight.get("segments") or []:
        aircraft = str(segment.get("aircraft") or "").strip()
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


def _render_excluded_plan_card(item: dict, current_price, is_roundtrip: bool) -> str:
    reason_lines = _excluded_reason_details(item)
    reason = reason_lines[0] if reason_lines else (item.get("reason") or "不符合当前规则")
    intro = _excluded_price_intro(item, current_price, is_roundtrip).replace("已排除的", "").replace("已排除", "").strip("： ")
    semantic_intro = _excluded_price_intro(item, current_price, is_roundtrip)
    title = f"已排除 · {intro}" if intro else "已排除"
    body_parts = []
    rows = []
    combo_text = str(item.get("flight_combo") or "").strip()
    if combo_text:
        rows.append(_excluded_table_row("航班组合", html.escape(combo_text)))
    for prefix, flight in _excluded_card_flights(item, is_roundtrip):
        body_parts.append(_email_plan_leg_group(prefix, flight, _excluded_flight_detail_text({"flight": flight, **item})))
    price = _to_float(item.get("total_price") or item.get("roundtrip_price") or item.get("price"))
    if price is not None:
        scope = _excluded_scope(item, is_roundtrip)
        price_label = "价格" if scope == "roundtrip" or not is_roundtrip else f"{_excluded_scope_label(scope).replace('方案', '')}价格"
        rows.append(_excluded_table_row(price_label, html.escape(_price_text(price))))
    scope_note = _excluded_scope_note(item, is_roundtrip)
    if scope_note:
        rows.append(_excluded_table_row("说明", html.escape(scope_note)))
    rows.append(_excluded_table_row("排除原因", html.escape(str(reason)), danger=True))
    for extra in reason_lines[1:3]:
        rows.append(_excluded_table_row("", html.escape(str(extra)), danger=True))
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
        if top_combinations:
            first_combo = top_combinations[0]
            _append_channel_price_bar_chart(lines, first_combo.get("outbound"))
            _append_channel_price_bar_chart(lines, first_combo.get("return"))
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
        (last_push or {}).get("price"),
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


def _payload_channel_rows(flight: dict | None) -> list[dict]:
    rows = []
    for option in _verified_booking_options(flight)[:6]:
        price = _option_price(option)
        if price:
            rows.append({"label": str(option.get("platform") or "璐拱娓犻亾"), "value": price})
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
        else "详情页暂未生成"
    )
    links = plan.get("links") if isinstance(plan, dict) else {}
    lines = ["购买渠道:点击验证最终价格"]

    if isinstance(links, dict) and plan.get("is_roundtrip"):
        for direction in ("outbound", "return"):
            line = _pushplus_link_line(links.get(direction, ""), 6)
            if line:
                lines.append(f"{_pushplus_plan_flight_label(plan, direction)}:")
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
            "当前价格接近低价区间,但暂未发现已验证购买渠道。",
            f"查看详情继续监控:{detail_link}",
        ]

    lines.append("价格以各平台支付页为准")
    lines.append(f"完整分析:{detail_link}")
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
    return {
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
        "tags": _round_trip_combo_tags(combo, route_info, None),
        "risk": _combo_grade(combo),
        "buy_condition": _combo_human_recommendation(combo, route_info),
        "links": {
            "outbound": _payload_booking_links_for_flight(outbound, route_info, outbound_date, 6),
            "return": _payload_booking_links_for_flight(return_flight, route_info, return_date, 6),
        },
        "channel_prices": _payload_channel_rows(outbound) or _payload_channel_rows(return_flight),
    }


def _payload_single_plan(flight: dict, route_info: dict, analysis_result: dict, index: int, variant: str) -> dict:
    return {
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
        "channel_prices": _payload_channel_rows(flight),
    }


def _plan_flights(plan: dict) -> list[dict]:
    flights = []
    for key in ("outbound_flight", "return_flight", "main_flight"):
        flight = plan.get(key)
        if isinstance(flight, dict) and flight:
            flights.append(flight)
    return flights


def _plan_total_stops(plan: dict) -> int:
    total = 0
    for flight in _plan_flights(plan):
        try:
            total += int(flight.get("stops") or 0)
        except (TypeError, ValueError):
            continue
    return total


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
    primary = plans[0]
    for index, plan in enumerate(plans):
        tier, reason, condition = _plan_tier_reason(plan, primary if index else None)
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
    transfer_parts = []
    if outbound_stops > 0:
        transfer_parts.append("去程中转")
    if return_stops > 0:
        transfer_parts.append("返程中转")
    if not transfer_parts:
        transfer_parts.append("直飞往返")

    purchase_mode = str(plan.get("purchase_mode") or "")
    if "两个单程" in purchase_mode:
        transfer_parts.append("两个单程拼接")

    return f"{'+'.join(transfer_parts)},{tier}"


def _plan_leg_stops(flight: dict | None) -> int:
    flight = flight or {}
    try:
        return int(flight.get("stops") or 0)
    except (TypeError, ValueError):
        return 0


def _payload_plan_price_rows(plans: list[dict]) -> list[dict]:
    rows = []
    for plan in plans or []:
        price = _to_float(plan.get("price"))
        if not price:
            continue
        rows.append(
            {
                "label": plan.get("label"),
                "value": price,
                "scope": "roundtrip" if plan.get("is_roundtrip") else "oneway",
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


def _chart_history_for_message(route_info: dict, analysis_result: dict, price_insights: dict | None, is_roundtrip: bool):
    if is_roundtrip:
        round_trip = (analysis_result or {}).get("round_trip_analysis") or {}
        for key in ("price_history", "history", "roundtrip_history"):
            if round_trip.get(key):
                return round_trip.get(key)
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


def _payload_verify_price(display_price) -> float | None:
    display = _to_float(display_price)
    return round(display * 1.05) if display else None


def _payload_price_policy_decision(display_price, transaction_price, verify_price, target_price, fallback="可以观察") -> dict:
    display = _to_float(display_price)
    transaction = _to_float(transaction_price)
    verify = _to_float(verify_price)
    target = _to_float(target_price)

    if transaction is not None and verify is not None and transaction <= verify:
        return {
            "conclusion": "可以购买前验证",
            "reason": "预估实付价不高于本次验证购买价",
            "push_type_hint": None,
        }
    if display is not None and verify is not None and display <= verify and transaction is not None and transaction > verify:
        return {
            "conclusion": "值得验证，不建议直接下单",
            "reason": "搜索参考价达标，但预估实付价高于验证购买价",
            "push_type_hint": "值得验证",
        }
    if target is not None and display is not None and display > target:
        return {
            "conclusion": "继续观察",
            "reason": "搜索参考价仍高于理想入手价",
            "push_type_hint": None,
        }
    return {
        "conclusion": fallback or "可以观察",
        "reason": "",
        "push_type_hint": None,
    }


def _payload_primary_price_values(current, primary_plan) -> dict:
    display = _to_float(primary_plan.get("price") if primary_plan else None) or _to_float(current)
    transaction = _to_float(primary_plan.get("estimated_price") if primary_plan else None) or display
    verify = _payload_verify_price(display)
    return {
        "display_price": display,
        "transaction_price": transaction,
        "verify_price": verify,
    }


def _payload_dedupe_text(items) -> list[str]:
    result = []
    for item in items or []:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _email_subject(payload: dict) -> str:
    push_type = payload.get("push_type") or "价格提醒"
    route = payload.get("route") or "航班监控"
    primary_plan = _plan_for_render((payload.get("recommended_plans") or [{}])[0] or {}, payload)
    display = _price_text(primary_plan.get("price") or payload.get("display_price") or payload.get("current_price"))
    tier = str(primary_plan.get("tier") or "").strip()
    if primary_plan and primary_plan.get("is_roundtrip") and _plan_total_stops(primary_plan) == 0:
        plan_label = "首选直飞方案"
    elif tier:
        plan_label = tier
    else:
        plan_label = "方案"
    return f"【{push_type}】{route}｜{plan_label}{display}"


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
    for key in ("travel_purposes", "travel_scenarios", "travel_scenario"):
        if not soft.get(key) and preferences.get(key):
            soft[key] = preferences.get(key)
    total_passengers, passenger_breakdown = get_total_passengers(subscription)
    if passenger_breakdown:
        soft["passengers"] = passenger_breakdown
    soft["passenger_count"] = total_passengers
    subscription_scenarios = soft.get("travel_purposes") or soft.get("travel_scenarios") or soft.get("travel_scenario")
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
        route_info.setdefault("subscription_id", subscription.get("subscription_id") or subscription.get("id") or subscription.get("_index"))
    analysis_result = analysis_result or outbound_analysis or {}
    outbound_analysis = outbound_analysis or analysis_result
    return_analysis = return_analysis or analysis_result.get("return_analysis") or {}
    is_roundtrip = bool(route_info.get("round_trip"))
    source_stats = source_stats or route_info.get("source_stats") or analysis_result.get("source_stats")
    decision, confidence, current, target, max_budget = _decision_context(
        analysis_result,
        route_info,
        source_stats,
        price_insights,
        is_roundtrip,
    )
    route_key, depart_key, return_key = _last_push_route_parts(route_info, is_roundtrip)
    last_push = get_last_push_price(route_key, depart_key, return_key)
    last_snapshot = get_last_push_snapshot(route_key, depart_key, return_key)
    history = (
        _chart_history_for_message(route_info, analysis_result, price_insights, is_roundtrip)
        if is_roundtrip
        else price_history or _chart_history_for_message(route_info, analysis_result, price_insights, is_roundtrip)
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

    if is_roundtrip:
        all_items = [
            _payload_combo_plan(combo, route_info, index, "推荐" if index < 2 else "备选")
            for index, combo in enumerate(_round_trip_combinations(analysis_result)[:5])
        ]
    else:
        flights = _single_flights_for_sections(analysis_result)
        all_items = [
            _payload_single_plan(flight, route_info, analysis_result, index, "推荐" if index < 2 else "备选")
            for index, flight in enumerate(flights[:5])
        ]
    all_items = _apply_plan_tiers(all_items)

    primary_flight = None
    if is_roundtrip:
        combos = _round_trip_combinations(analysis_result)
        primary_flight = (combos[0].get("outbound") or {}) if combos else {}
    else:
        flights = _single_flights_for_sections(analysis_result)
        primary_flight = flights[0] if flights else {}

    primary_plan = all_items[0] if all_items else {}
    price_values = _payload_primary_price_values(current, primary_plan)
    display_price = price_values.get("display_price")
    transaction_price = price_values.get("transaction_price")
    verify_limit = price_values.get("verify_price")
    price_policy = _payload_price_policy_decision(
        display_price,
        transaction_price,
        verify_limit,
        target,
        decision.get("conclusion") or "可以观察",
    )
    price_signal = build_price_signal(
        display_price,
        target,
        _price_history_for_push(price_insights, analysis_result, is_roundtrip),
    )
    execution_advice = build_execution_advice(display_price, transaction_price, verify_limit, target)
    if execution_advice.get("conclusion"):
        price_policy["conclusion"] = execution_advice["conclusion"]
    if execution_advice.get("summary"):
        price_policy["reason"] = execution_advice["summary"]
    push_analysis = dict(analysis_result)
    push_analysis["decision_prices"] = {
        "display_price": display_price,
        "transaction_price": transaction_price,
        "verify_price": verify_limit,
    }
    push_meta = determine_push_type(
        display_price,
        target,
        max_budget,
        _price_history_for_push(price_insights, analysis_result, is_roundtrip),
        analysis_result.get("days_to_dept"),
        (last_push or {}).get("price"),
        push_analysis,
    )
    if price_policy.get("push_type_hint"):
        push_meta["type"] = price_policy["push_type_hint"]
    if price_policy.get("reason"):
        push_meta["reasons"] = _payload_dedupe_text([price_policy["reason"]] + (push_meta.get("reasons") or []))[:4]

    change = (push_meta or {}).get("price_change") or {}
    fallback_line = _trend_fallback_line(history)
    trend_summary = _trend_linechart_summary(history, target, display_price, None) if history else ""
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
    form_url = _subscription_edit_url(route_info)
    feedback_url = _feedback_url(route_info)
    detail_url = f"{_subscription_form_url(route_info).rstrip('/')}/detail?sub={quote(str(route_info.get('subscription_id') or route_key))}"
    payload = {
        "push_type": (push_meta or {}).get("type") or "价格提醒",
        "route": _payload_route_text(route_info),
        "route_airports": _payload_route_airports(route_info),
        "trip_type": "round_trip" if is_roundtrip else "one_way",
        "is_roundtrip": is_roundtrip,
        "current_price": display_price,
        "display_price": display_price,
        "transaction_price": transaction_price,
        "verify_price": verify_limit,
        "ideal_price": target,
        "max_price": max_budget,
        "last_push_price": (last_push or {}).get("price"),
        "recommendation": price_policy.get("conclusion") or decision.get("conclusion") or "可以观察",
        "price_policy_reason": price_policy.get("reason") or "",
        "price_signal": price_signal,
        "execution_advice": execution_advice,
        "confidence": confidence.get("overall") or decision.get("confidence") or "中",
        "confidence_dimensions": confidence.get("dimensions") or {},
        "confidence_details": confidence.get("details") or {},
        "travel_profile": travel_profile,
        "travel_profile_explanation": profile_explanation,
        "travel_scenarios": travel_profile.get("scenarios") or [],
        "recommendation_basis": recommendation_basis,
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
        "buy_condition": (
            f"支付页最终价≤{_price_text(verify_limit)}，且含托运行李"
            if verify_limit
            else "以支付页最终价和票规为准"
        ),
        "buy_condition_explanation": (
            f"本次验证价{_price_text(verify_limit)} = 当前搜索参考价{_price_text(display_price)} "
            f"+ 可接受浮动和费用容忍区间，用于判断该方案在当前价位是否仍值得买，"
            f"与你的理想入手价{_price_text(target)}是不同概念。"
            if verify_limit and display_price
            else ""
        ),
        "action_range": _payload_action_range(display_price, target, max_budget),
        "trigger_reason": (push_meta or {}).get("reasons") or (decision.get("reasons") or [])[:3],
        "recommended_plans": all_items[:2],
        "alternative_plans": all_items[2:5],
        "excluded_plans": (
            ((analysis_result.get("round_trip_analysis") or {}).get("excluded_roundtrip_combos") or [])
            if is_roundtrip
            else (analysis_result.get("excluded_flights") or [])
        ),
        "buy_risk": risk.get("buy_risks") or ["可能遇到支付页跳价", "票规需确认（行李/退改）", "不同渠道售后政策不同"],
        "wait_risk": risk.get("wait_risks") or ["可能错过当前低价", "临近出发价格通常上涨", "理想价再次出现不确定"],
        "risk_summary": risk.get("summary") or "",
        "limits": _judgment_limit_items(route_info, analysis_result, price_insights, is_roundtrip, return_analysis),
        "price_history": _normalize_chart_history(history),
        "trend_summary": trend_summary,
        "trend_fallback": fallback_line,
        "checklist": _purchase_checklist_items(route_info, analysis_result, primary_plan, verify_limit),
        "sorting_logic": _sorting_logic_items(route_info, is_roundtrip),
        "diff_from_last": {
            "last_price": change.get("last") or (last_push or {}).get("price"),
            "diff": change.get("diff"),
            "last_snapshot": last_snapshot or {},
        },
        "freshness_minutes": ((primary_flight or {}).get("availability") or {}).get("age_minutes"),
        "source_count": ((primary_flight or {}).get("availability") or {}).get("source_count"),
        "frequency": frequency,
        "nearby_date_prices": _payload_nearby_date_rows(route_info, analysis_result, is_roundtrip),
        "plan_price_rows": _payload_plan_price_rows(all_items[:5]),
        "channel_price_rows": (all_items[0].get("channel_prices") if all_items else []),
        "detail_url": detail_url,
        "form_url": form_url,
        "feedback_url": feedback_url,
        "source_stats": source_stats or {},
        "collected_at": _message_collected_time(analysis_result, route_info),
        "snapshot": {
            "route": route_key,
            "depart_date": depart_key,
            "return_date": return_key,
            "channels": _snapshot_channels(primary_flight),
            "fare_status": _snapshot_fare_status(primary_flight),
        },
    }
    print(f"[场景调试] 推送将显示 = {payload.get('travel_scenarios')}")
    return payload


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
    diff = _to_float((payload.get("diff_from_last") or {}).get("diff"))
    if fallback:
        trend_text = f"近期：{fallback}"
        if diff is not None:
            trend_text += f"（{'下降' if diff < 0 else '上涨' if diff > 0 else '持平'}{_price_text(abs(diff)) if diff else ''}）"
        lines.extend(["", trend_text])
    links = [
        f'<a href="{payload.get("detail_url", "")}" target="_blank">查看详情</a>',
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
                str(plan.get("outbound_push_line") or ""),
                str(plan.get("return_push_line") or ""),
            ]
        else:
            current = [str(plan.get("main_push_line") or "")]
        current = [line for line in current if line.strip()]
        if current:
            if detail_lines:
                detail_lines.append("")
            for item in current:
                detail_lines.extend(html.escape(line) for line in item.splitlines() if line.strip())
    if not detail_lines:
        return []
    return ["", "推荐方案:"] + detail_lines


def render_pushplus(payload: dict) -> str:
    """Render the strictly short PushPlus message from the unified payload."""
    payload = payload or {}
    push_type = html.escape(str(payload.get("push_type") or "价格提醒"))
    route = html.escape(str(payload.get("route") or "航班监控"))
    display_text = _payload_price(payload.get("display_price") or payload.get("current_price"))
    transaction_text = _payload_price(payload.get("transaction_price"))
    verify_text = _payload_price(payload.get("verify_price"))
    recommendation = html.escape(str(payload.get("recommendation") or "可以观察"))
    buy_condition = html.escape(str(payload.get("buy_condition") or "以支付页最终价和票规为准"))
    primary_plan = (payload.get("recommended_plans") or [{}])[0] or {}
    baggage_line = html.escape(str(primary_plan.get("baggage_line") or "行李:支付页需确认"))

    reasons = [str(item) for item in (payload.get("trigger_reason") or []) if item]
    diff = _to_float((payload.get("diff_from_last") or {}).get("diff"))
    if diff is not None and diff != 0:
        reasons.append(f"比上次{'降' if diff < 0 else '涨'}{_price_text(abs(diff))}")
    current = _to_float(payload.get("current_price"))
    ideal = _to_float(payload.get("ideal_price"))
    if current is not None and ideal and current <= ideal * 1.05:
        reasons.append(f"接近理想价{_price_text(ideal)}")
    reason_text = "，".join(dict.fromkeys(reasons[:2])) or "当前价格触发监控条件"

    lines = [
        f"<b>【{push_type}】{route}</b>",
        "",
        f"搜索参考价:{display_text}",
        f"预估实付价:{transaction_text}",
        f"本次验证价:支付页≤{verify_text}",
        baggage_line,
        f"结论:{recommendation}",
        f"购买条件:{buy_condition}",
    ]
    lines.extend(_pushplus_plan_lines(payload))
    recommendation_basis = payload.get("recommendation_basis") or {}
    scenario_label = " + ".join(recommendation_basis.get("scenario_labels") or [])
    profile_explanation = payload.get("travel_profile_explanation") or {}
    if not scenario_label:
        scenario_label = str(profile_explanation.get("scenario_label") or "")
    basis_line = recommendation_basis.get("plain_language") or profile_explanation.get("basis") or ""
    if scenario_label:
        lines.extend(
            [
                "",
                f"推荐依据:按“{html.escape(scenario_label)}”综合排序",
                html.escape(str(basis_line)),
            ]
        )
    lines.extend(
        [
            "",
            f"提醒原因:{html.escape(reason_text)}",
            "",
            *_pushplus_channel_section(payload, primary_plan),
            "",
            _pushplus_freshness_line(payload),
            "提示:最终价、库存、行李、退改签和机型以下单页为准",
        ]
    )
    return "<br>".join(lines)


def _render_payload_plan_card(plan: dict, compact: bool = False, primary_plan: dict | None = None) -> str:
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
            _email_plan_leg_group("去程", plan.get("outbound_flight"), str(plan.get("outbound_line") or ""))
        )
        body_parts.append(
            _email_plan_leg_group("返程", plan.get("return_flight"), str(plan.get("return_line") or ""))
        )
        rows.extend(
            [
                ("搜索参考价", f"往返 {_price_text(plan.get('price'))}"),
                ("预估实付价", _price_text(plan.get("estimated_price"))),
                ("购票方式", html.escape(str(plan.get("purchase_mode") or "待确认"))),
                ("行李状态", f'<span style="color:#d97706;">{html.escape(str(plan.get("baggage_line") or "支付页需确认"))}</span>'),
            ]
        )
        if plan.get("purchase_note"):
            rows.append(("说明", html.escape(str(plan.get("purchase_note")))))
        links = plan.get("links") or {}
        link_lines = []
        if links.get("outbound"):
            link_lines.append("去程：" + (_layered_channel_links(links["outbound"]) or links["outbound"]))
        if links.get("return"):
            link_lines.append("返程：" + (_layered_channel_links(links["return"]) or links["return"]))
        if link_lines:
            rows.append(("验证渠道", "验证整套往返：建议先在同一渠道选择往返搜索。<br>" + "<br>".join(link_lines)))
    else:
        main_flight = plan.get("main_flight") or plan.get("outbound_flight") or plan.get("flight")
        body_parts.append(
            _email_plan_leg_group("去程", main_flight, str(plan.get("summary") or ""))
        )
        rows.extend(
            [
                ("搜索参考价", _price_text(plan.get("price"))),
                ("预估实付价", _price_text(plan.get("estimated_price"))),
                ("行李状态", f'<span style="color:#d97706;">{html.escape(str(plan.get("baggage_line") or "支付页需确认"))}</span>'),
            ]
        )
        links = (plan.get("links") or {}).get("main")
        if links:
            rows.append(("验证渠道", _layered_channel_links(links) or links))
    if plan.get("tags"):
        rows.append(("状态", html.escape(str(plan.get("tags") or ""))))
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
        "aircraft": _safe_nested_field(
            segment, "aircraft", "airplane", "plane_type", "equipment", default=""
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
    aircraft = _safe_flight_field(flight, "aircraft", "airplane", "plane_type", "equipment")
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
        if item and item not in {"未知", "unknown", "Unknown", "请查询航司官网"} and item not in aircraft:
            aircraft.append(item)
    top_level = str(_safe_flight_field(flight, "aircraft", "airplane", "plane_type", "equipment") or "").strip()
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
    return html.escape(f"{_compact_flight_numbers(flight)} {_flight_airline_name(flight)}")


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


def _email_action_panel_body(
    payload: dict,
    primary_plan: dict,
    verify_text: str,
    price_reason: str,
    interactive_channels: bool = False,
) -> str:
    conclusion = str(payload.get("recommendation") or "可以观察")
    primary_line = _email_primary_plan_line(payload, primary_plan)
    buy_condition = str(payload.get("buy_condition") or "以支付页为准")
    trigger_type = _email_trigger_type(payload)
    trigger_reason = str(price_reason or _email_trigger_reason_text(payload) or "请查看下方原因")
    blocks = [
        f"<div>当前判断:{html.escape(conclusion)}</div>",
        f"<div>首选方案:{html.escape(primary_line)}</div>",
        f"<div>购买条件:{html.escape(buy_condition)}</div>",
        "<div>下一步:去验证价格 | 查看详情 | 继续监控</div>",
        f"<div style='margin-top:8px;color:#666;font-size:12px;'>触发类型:{html.escape(trigger_type)}</div>",
        f"<div style='color:#666;font-size:12px;'>触发原因:{html.escape(trigger_reason)}</div>",
        (
            "<div style='margin-top:8px;color:#666;font-size:12px;'>"
            f"验证价说明:这次方案值得买的上限,不等于你的最高预算。"
            f"若支付页高于{html.escape(verify_text.replace('支付页≤', ''))},说明搜索低价没真正落地,建议继续监控。"
            "</div>"
        ),
        _email_action_links(payload, primary_plan, interactive_channels=interactive_channels),
    ]
    return "".join(blocks)


def _email_primary_plan_line(payload: dict, primary_plan: dict) -> str:
    if not primary_plan:
        return "方案待确认"
    label = str(primary_plan.get("label") or "方案A")
    route_kind = "直飞往返" if primary_plan.get("is_roundtrip") and _plan_total_stops(primary_plan) == 0 else (
        "往返方案" if primary_plan.get("is_roundtrip") else "单程方案"
    )
    price = _price_text(primary_plan.get("price") or payload.get("display_price") or payload.get("current_price"))
    return f"{label},{route_kind},搜索参考价{price}"


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


def _email_action_links(
    payload: dict,
    primary_plan: dict | None = None,
    interactive_channels: bool = False,
) -> str:
    channel_picker = _email_channel_picker(primary_plan or {}, interactive=interactive_channels)
    detail_url = str(payload.get("detail_url") or "")
    form_url = str(payload.get("form_url") or "")
    feedback_url = str(payload.get("feedback_url") or "")
    links = []
    if detail_url:
        links.append(("查看网页详情", detail_url))
    if form_url:
        links.append(("继续监控", form_url))
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
        + action_links
        + "</div>"
    )


def _email_channel_picker(plan: dict, interactive: bool = False) -> str:
    price = plan.get("price") or plan.get("display_price") or plan.get("estimated_price")
    sections = _email_channel_sections(plan)
    if not sections:
        verify_url = _email_primary_booking_url(plan or {})
        if not verify_url:
            return ""
        sections = [("", [("去验证价格", verify_url)])]
    if interactive:
        body = "".join(_email_channel_section_html(title, links, price) for title, links in sections)
        return (
            "<details style='margin:8px 0;'>"
            "<summary style='cursor:pointer;color:#2563eb;font-weight:600;'>去验证价格 ▾</summary>"
            f"<div style='margin-top:6px;'>{body}</div>"
            "</details>"
        )
    body = "<div style='font-weight:600;margin-bottom:4px;'>去验证价格(选择渠道):</div>"
    body += "".join(_email_channel_section_html(title, links, price) for title, links in sections)
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
                sections.append((title, channel_links))
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


def _extract_primary_channel_links(link_html: str) -> list[tuple[str, str]]:
    wanted = ("携程", "飞猪", "去哪儿")
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


def _plan_for_render(plan: dict, payload: dict) -> dict:
    if not isinstance(plan, dict):
        return {}
    if plan.get("feedback_url"):
        return plan
    return {**plan, "feedback_url": payload.get("feedback_url")}


def _render_payload_plan_cards(payload: dict, plans: list[dict], primary_plan: dict, compact: bool = False) -> str:
    return "".join(
        _render_payload_plan_card(_plan_for_render(plan, payload), compact=compact, primary_plan=primary_plan)
        for plan in plans
        if isinstance(plan, dict)
    )


def _detail_section(title: str, body: str, open_by_default: bool = False) -> str:
    open_attr = " open" if open_by_default else ""
    return (
        f'<details{open_attr} style="{EMAIL_CARD_STYLE}">'
        f'<summary style="{EMAIL_CARD_TITLE_STYLE}cursor:pointer;">{html.escape(title)}</summary>'
        f'<div style="{EMAIL_CARD_BODY_STYLE}">{body}</div>'
        "</details>"
    )


def _email_source_body(payload: dict) -> str:
    rows = [
        "<div>- 价格:Google Flights 多源交叉验证</div>",
        "<div>- 票规:Duffel 行李/退改规则参考</div>",
        "<div>- 候选方案:已去重并筛选</div>",
    ]
    rows.append(f"<div style='margin-top:8px;color:#666;font-size:12px;'>采集时间:{html.escape(_payload_freshness_text(payload))}</div>")
    rows.append("<div style='color:#666;font-size:12px;'>说明:技术明细见网页详情页,价格以平台支付页为准。</div>")
    return "".join(rows)


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


def _source_stat_is_usable(status: str) -> bool:
    text = str(status or "").lower()
    blocked = ("失败", "error", "fail", "429", "timeout", "超时", "异常")
    return not any(item in text for item in blocked)


def _email_detail_charts_body(payload: dict) -> str:
    parts = []
    if payload.get("nearby_date_prices"):
        title, note = _nearby_date_chart_title_and_note(payload["nearby_date_prices"])
        parts.append(_payload_bar_html(title, payload["nearby_date_prices"]))
        if note:
            parts.append(f"<div style='color:#666;font-size:12px;'>{html.escape(note)}</div>")
    if payload.get("channel_price_rows"):
        parts.append(_payload_bar_html("不同渠道报价对比", payload["channel_price_rows"]))
    if payload.get("plan_price_rows"):
        parts.append(_payload_bar_html("方案价格对比", payload["plan_price_rows"]))
    return "<br>".join(part for part in parts if part) or "<div style='color:#888;font-size:12px;'>暂无更多图表数据。</div>"


def _nearby_date_chart_title_and_note(rows: list[dict]) -> tuple[str, str]:
    scopes = {_chart_scope_label(row) for row in rows or []}
    scopes.discard("")
    if scopes == {"往返"}:
        return "前后日期最低价(往返组合参考价)", ""
    return "前后日期最低价(单程参考价)", "注:为单程价,非往返总价"


def _email_excluded_compact_body(payload: dict) -> str:
    excluded = payload.get("excluded_plans") or []
    if not excluded:
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

    rows = [
        "<table style='width:100%;font-size:13px;line-height:1.6;border-collapse:collapse;'>"
        "<thead><tr>"
        "<th style='text-align:left;color:#666;border-bottom:1px solid #eee;padding:6px 4px;'>更便宜方案</th>"
        "<th style='text-align:left;color:#666;border-bottom:1px solid #eee;padding:6px 4px;'>价格</th>"
        "<th style='text-align:left;color:#666;border-bottom:1px solid #eee;padding:6px 4px;'>排除原因</th>"
        "</tr></thead><tbody>"
    ]
    for index, (price, item) in enumerate(sorted(candidates, key=lambda row: row[0])[:3], start=1):
        scope = _excluded_scope(item, is_roundtrip)
        price_scope = "往返" if scope == "roundtrip" else "单程"
        reason = _email_excluded_compact_reason(item, scope, is_roundtrip)
        name = _excluded_compact_name(item, index)
        rows.append(
            "<tr>"
            f"<td style='padding:7px 4px;border-bottom:1px solid #f5f5f5;'>{html.escape(name)}</td>"
            f"<td style='padding:7px 4px;border-bottom:1px solid #f5f5f5;'>{_price_text(price)}({price_scope})</td>"
            f"<td style='padding:7px 4px;border-bottom:1px solid #f5f5f5;color:#b91c1c;'>{html.escape(reason)}</td>"
            "</tr>"
        )
    rows.append("</tbody></table>")
    rows.append(
        "<div style='margin-top:8px;color:#666;font-size:12px;'>"
        "完整排除方案详情见网页详情页。"
        "</div>"
    )
    return "".join(rows)


def _excluded_compact_name(item: dict, index: int) -> str:
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


def _email_excluded_compact_reason(item: dict, scope: str, is_roundtrip: bool) -> str:
    details = [str(value).strip() for value in _excluded_reason_details(item) if str(value or "").strip()]
    reason = details[0] if details else "不符合当前规则"
    if is_roundtrip and scope != "roundtrip" and "非往返总价" not in reason:
        direction = _excluded_scope_label(scope).replace("方案", "") or "单段"
        reason += f"(注:此为{direction}单段价,非往返总价)"
    return reason


def _email_trend_card_body(payload: dict) -> str:
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


def render_email(payload: dict) -> tuple[str, str]:
    """Render the full HTML email report from a normalized payload."""
    payload = payload or {}
    subject = _email_subject(payload)
    verify_text = f"支付页≤{_price_text(payload.get('verify_price'))}" if payload.get("verify_price") else "以支付页为准"
    price_reason = str(payload.get("price_policy_reason") or "请以预估实付价和支付页最终价为准")
    baggage_line = ""
    primary_plan = (payload.get("recommended_plans") or [{}])[0] or {}
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
    cards = [
        f"<h2 style='font-size:18px;color:#111;margin:0 0 12px;'>【{html.escape(str(payload.get('push_type') or '价格提醒'))}】{html.escape(str(payload.get('route') or '航班监控'))}</h2>",
        _email_card(
            "行动面板",
            _email_action_panel_body(payload, primary_plan, verify_text, price_reason),
        ),
        _email_card(
            "价格口径与信号",
            _email_table(
                [
                    ("价格信号", html.escape(f"{price_signal.get('label') or '待确认'} - {price_signal.get('summary') or '搜索参考价用于判断便不便宜'}")),
                    ("执行建议", html.escape(f"{execution_advice.get('label') or '待确认'} - {execution_advice.get('summary') or price_reason}")),
                    ("搜索参考价", _email_price_span(payload.get("display_price") or payload.get("current_price"), "#2563eb")),
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
    cards.append(_render_payload_plan_cards(payload, payload.get("recommended_plans") or [], primary_plan))

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

    cards.append(_email_card("为什么提醒你", _email_list(payload.get("trigger_reason") or [], 3)))
    cards.append(_email_card("价格走势", _email_trend_card_body(payload)))

    action_rows = [
        ("购买条件", html.escape(str(payload.get("buy_condition") or "以支付页为准"))),
        ("本次验证价", html.escape(verify_text)),
        ("理想入手价", _price_text(payload.get("ideal_price"))),
        ("最高可接受价", _price_text(payload.get("max_price"))),
    ]
    action = payload.get("action_range") or {}
    if action.get("current_label"):
        signal = payload.get("price_signal") or {}
        action_rows.append(("价格信号", html.escape(str(signal.get("summary") or action.get("current_label")))))
    cards.append(
        _email_card(
            "操作建议",
            _email_table(action_rows)
            + "<div style='margin-top:8px;color:#666;font-size:12px;'>若支付页最终价、行李和票规不满足上方条件，建议继续监控。</div>",
        )
    )

    cards.append(_email_card("为什么不推荐更便宜方案", _email_excluded_compact_body(payload)))

    diff = _to_float((payload.get("diff_from_last") or {}).get("diff"))
    trend_summary_text = str(payload.get("trend_summary") or "").strip()
    change_lines = []
    if diff is not None or trend_summary_text:
        if diff is not None:
            if diff < 0:
                change_lines.append(f"<div>较上次提醒：下降{_price_text(abs(diff))}</div>")
            elif diff > 0:
                change_lines.append(f"<div>较上次提醒：上涨{_price_text(diff)}</div>")
            else:
                change_lines.append("<div>较上次提醒：价格持平</div>")
        if trend_summary_text:
            change_lines.append(f"<div>近14次采集趋势：{html.escape(trend_summary_text)}</div>")
        change_lines.append(f"<div>本次提醒主要由“{html.escape(str(payload.get('push_type') or '价格变化'))}”触发。</div>")
    if action.get("ranges"):
        range_lines = []
        for row in action["ranges"]:
            text = row.get("text")
            if not text:
                left = "-∞" if row.get("min") is None else _price_text(row.get("min"))
                right = "+∞" if row.get("max") is None else _price_text(row.get("max"))
                text = f"{left} - {right}"
            range_lines.append(f"<div>{html.escape(str(text))}：{html.escape(str(row.get('label')))}</div>")
        change_lines.extend(range_lines)
    if change_lines:
        cards.append(_email_card("价格变化与参考区间", "".join(change_lines)))

    cards.append(_email_card("数据来源", _email_source_body(payload)))
    cards.append(
        _email_card(
            "更多分析",
            f'排除方案详情、置信度拆解、购买前检查清单和详细数据来源见网页详情页：'
            f'<a href="{html.escape(str(payload.get("detail_url") or ""))}" target="_blank">查看完整分析</a>',
        )
    )

    cards.append(
        _email_card(
            "操作链接",
            _email_action_links(payload, primary_plan)
            + f"<div style='margin-top:8px;color:#666;font-size:12px;'>数据采集于 {html.escape(str(payload.get('collected_at') or ''))}。最终价格以购买平台支付页为准。</div>",
        )
    )
    return subject, "".join(cards)


def render_detail_html(payload: dict) -> str:
    """Render the web detail page HTML with core modules visible and details folded."""
    payload = payload or {}
    subject = _email_subject(payload)
    verify_text = f"支付页≤{_price_text(payload.get('verify_price'))}" if payload.get("verify_price") else "以支付页为准"
    price_reason = str(payload.get("price_policy_reason") or "请以预估实付价和支付页最终价为准")
    primary_plan = _plan_for_render((payload.get("recommended_plans") or [{}])[0] or {}, payload)
    cards = [
        f"<h2 style='font-size:18px;color:#111;margin:0 0 12px;'>{html.escape(subject)}</h2>",
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
        _render_payload_plan_cards(payload, payload.get("recommended_plans") or [], primary_plan),
        _email_card("为什么提醒你", _email_list(payload.get("trigger_reason") or [], 3)),
        _email_card("价格走势", _email_trend_card_body(payload)),
    ]

    action_rows = [
        ("购买条件", html.escape(str(payload.get("buy_condition") or "以支付页为准"))),
        ("本次验证价", html.escape(verify_text)),
        ("理想入手价", _price_text(payload.get("ideal_price"))),
        ("最高可接受价", _price_text(payload.get("max_price"))),
    ]
    cards.append(
        _email_card(
            "操作建议",
            _email_table(action_rows)
            + "<div style='margin-top:8px;color:#666;font-size:12px;'>若支付页最终价、行李和票规不满足上方条件，建议继续监控。</div>",
        )
    )

    excluded_body = _email_excluded_compact_body(payload)
    excluded_full = []
    is_roundtrip = bool(payload.get("is_roundtrip"))
    current_price = payload.get("current_price")
    for item in (payload.get("excluded_plans") or [])[:3]:
        if isinstance(item, dict):
            excluded_full.append(_render_excluded_plan_card(item, current_price, is_roundtrip))
    if excluded_full:
        excluded_body += "<div style='margin-top:12px;'>" + "".join(excluded_full) + "</div>"
    cards.append(_detail_section("展开:排除方案", excluded_body))

    cards.append(_detail_section("展开:价格走势详情", _email_trend_card_body(payload) + _email_detail_charts_body(payload)))

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

    cards.append(_detail_section("展开:详细数据来源", _email_technical_source_body(payload)))

    cards.append(
        _email_card(
            "下一步",
            _email_action_links(payload, primary_plan, interactive_channels=True)
            + f"<div style='margin-top:8px;color:#666;font-size:12px;'>数据采集于 {html.escape(str(payload.get('collected_at') or ''))}。最终价格以购买平台支付页为准。</div>",
        )
    )
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
    save_last_push_price(
        route,
        depart_date,
        snapshot.get("return_date"),
        payload.get("current_price"),
        payload.get("push_type"),
        now,
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

    route_info = route_info or {}
    analysis_result = analysis_result or outbound_analysis or {}
    outbound_analysis = outbound_analysis or analysis_result
    return_analysis = return_analysis or analysis_result.get("return_analysis") or {}
    all_flights = [flight for flight in analysis_result.get("all_flights", []) if flight]
    economy_recs, business_rec = _select_compact_recommendations(analysis_result)

    def build_message(economy_limit: int) -> str:
        lines = []
        days = analysis_result.get("days_to_dept")
        if days is None:
            try:
                days = (
                    date.fromisoformat(route_info.get("depart_date", ""))
                    - date.today()
                ).days
            except ValueError:
                days = ""

        is_round_trip = bool(route_info.get("round_trip"))
        source_stats_for_message = (
            source_stats
            or route_info.get("source_stats")
            or analysis_result.get("source_stats")
        )
        _append_decision_summary_card(
            lines,
            analysis_result,
            route_info,
            source_stats_for_message,
            price_insights,
            is_round_trip,
        )
        _append_judgment_limits(
            lines,
            route_info,
            analysis_result,
            price_insights,
            is_round_trip,
            return_analysis,
        )
        if is_round_trip:
            lines.append(
                f"<b>✈️ {_city_label(route_info.get('origin',''))} → "
                f"{_city_label(route_info.get('destination',''))} 往返监控</b>"
            )
            lines.append(
                f"去程：{route_info.get('depart_date','')} | "
                f"返程：{route_info.get('return_date') or '未设置'}"
            )
        else:
            lines.append(f"<b>✈️ {_city_label(route_info.get('origin',''))} → {_city_label(route_info.get('destination',''))}</b>")
            lines.append(f"📅 出发日期：{route_info.get('depart_date','')}")
        lines.append(f"⏳ 距出发：{days}天")
        lines.append(f"📊 数据采集时间：{_message_collected_time(analysis_result, route_info)}")
        lines.append("⏱ 建议在2小时内确认购买，超时请等待下次推送刷新价格")
        lines.append(_sort_rule_text(route_info.get("mode") or analysis_result.get("mode")))
        lines.append("以下方案按当前排序规则展示，排序不代表推荐。")
        preference_summary = analysis_result.get("preference_summary") or {}
        if preference_summary.get("message"):
            lines.append(f"<b>{preference_summary['message']}</b>")
        companions = _preference_value(route_info, analysis_result, "companions", "solo")
        price_sensitivity = _preference_value(route_info, analysis_result, "price_sensitivity", "low")
        trip_rigidity = _preference_value(route_info, analysis_result, "trip_rigidity", "confirmed")
        if companions != "solo":
            lines.append(f"👥 同行人员：{_companions_label(companions)}，优先关注白天、少折腾、行李和退改更稳的方案")
        lines.append(f"💵 价格敏感度：{_price_sensitivity_label(price_sensitivity)}")
        lines.append(f"🧭 行程刚性：{_trip_rigidity_guidance(trip_rigidity)}")
        refund_tip = _refund_rigidity_tip(route_info, analysis_result)
        if refund_tip:
            lines.append(refund_tip)
        lines.append("")

        current_min = (
            analysis_result.get("price_range", [0])[0]
            if analysis_result.get("price_range")
            else None
        )
        if not _has_valid_price(current_min):
            current_min = None
        goals = _goals(route_info, analysis_result)
        if not is_round_trip:
            lines.extend(_price_scale_lines(current_min, route_info, analysis_result))
        if not is_round_trip and _primary_goal(route_info, analysis_result) == "cheaper_date":
            _append_nearby_dates(
                lines, route_info.get("nearby_dates") or analysis_result.get("nearby_dates")
            )
        if is_round_trip:
            _append_round_trip_block(lines, outbound_analysis, route_info, return_analysis)
        history = None if is_round_trip else (price_insights.get("price_history") if price_insights else None)
        price_pos = None if is_round_trip else (price_position_description(current_min, history) if current_min else None)
        wait_risk = None
        own_history = []
        price_refs = {}
        window_analysis = {}
        if not is_round_trip:
            wait_risk = (
                waiting_risk_description(history, current_min, days or 0)
                if current_min
                else None
            )
            own_history = _normalize_own_history_for_refs(route_info)
            price_refs = (
                calculate_price_references(current_min, history, own_history, days or 0, all_flights)
                if current_min
                else {}
            )
            window_analysis = (
                multi_window_analysis(current_min, own_history, history, days or 0)
                if current_min
                else {}
            )
        evidence_source = price_pos or {"data_points": len(_history_prices(history))}
        evidence = _evidence_text(evidence_source, source_stats_for_message)

        if not is_round_trip and "price_drop_alert" in goals:
            _append_price_drop_alert(lines, analysis_result, route_info)

        if "buy_timing" in goals and price_pos:
            lines.append("<b>📊 当前价格位置</b>")
            lines.append("")
            lines.append(f"当前最低价：{_price_text(current_min)}")
            lines.append(f"历史最低：¥{price_pos['min_price']:,.0f}")
            lines.append(f"历史平均：¥{price_pos['avg_price']:,.0f}")
            lines.append(f"历史最高：¥{price_pos['max_price']:,.0f}")
            lines.append(
                f"当前水平：{_percentile_position_text(price_pos.get('percentile'))}{evidence}"
            )
            lines.append(f"鏁版嵁閲忥細{price_pos['data_points']}涓巻鍙蹭环鏍肩偣")
            lines.append("")

        if "buy_timing" in goals:
            _append_price_references(lines, price_refs, current_min, evidence)
        _append_multi_window_analysis(lines, window_analysis)
        if "cheaper_date" in goals and _primary_goal(route_info, analysis_result) != "cheaper_date":
            _append_nearby_dates(
                lines, route_info.get("nearby_dates") or analysis_result.get("nearby_dates")
            )

        if "buy_timing" in goals and wait_risk:
            lines.append("<b>⏳ 继续等待的风险</b>")
            lines.append("")
            lines.append(
                f"价格上涨概率：{wait_risk['up_probability']}%，平均涨¥{wait_risk['avg_up_amount']:,}"
            )
            lines.append(
                f"价格下降概率：{wait_risk['down_probability']}%，平均降¥{wait_risk['avg_down_amount']:,}"
            )
            lines.append(f"距出发：{wait_risk['days_to_dept']}天")
            lines.append(f"时间判断：{wait_risk['urgency']}")
            lines.append("")
            lines.append("━━━━━━━━━━━━━━━━")
            lines.append("")

        if not is_round_trip:
            lines.append("━━━ 经济舱方案 ━━━")
            lines.append("")

        if not is_round_trip and "best_overall" in goals:
            _append_best_overall_summary(lines, analysis_result, route_info)

        if not is_round_trip:
            _append_price_anomaly_lines(
                lines, analysis_result.get("price_anomalies") or []
            )

        if not is_round_trip:
            main_economy_recs = [
                flight for flight in economy_recs if flight.get("execution_grade") != "D"
            ][:economy_limit]
            other_reference_recs = [
                flight for flight in economy_recs if flight.get("execution_grade") == "D"
            ][:2]
            for index, flight in enumerate(main_economy_recs):
                _append_compact_flight(
                    lines,
                    flight,
                    _option_label(index),
                    route_info,
                    all_flights,
                    analysis_result,
                )
            if other_reference_recs:
                lines.append("━━━ 其他参考 ━━━")
                lines.append("")
                for index, flight in enumerate(other_reference_recs):
                    _append_compact_flight(
                        lines,
                        flight,
                        f"参考{index + 1}",
                        route_info,
                        all_flights,
                        analysis_result,
                    )

        if not is_round_trip and business_rec:
            lines.append("━━━ 商务舱参考 ━━━")
            lines.append("")
            _append_compact_flight(
                lines, business_rec, "商务舱最低价", route_info, all_flights, analysis_result
            )

        if not is_round_trip:
            trend = generate_trend_summary(
                history,
                current_min,
            )
            current_prices = [
                float(flight.get("price"))
                for flight in all_flights
                if _has_valid_price(flight.get("price"))
            ]
            if trend.get("available"):
                high_price = max(trend["max_price"], current_min) if current_min else trend["max_price"]
                low_price = min(trend["min_price"], current_min) if current_min else trend["min_price"]
                avg_price = trend["avg_price"]
            elif current_prices:
                high_price = max(current_prices)
                low_price = min(current_prices)
                avg_price = round(sum(current_prices) / len(current_prices))
                current_min = current_min or low_price
            else:
                high_price = low_price = avg_price = current_min = None

            lines.append("鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣")
            lines.append("")
            lines.append("馃搱 浠锋牸璧板娍")
            arrow_line = _trend_arrow_line(history)
            if arrow_line:
                lines.append(arrow_line)
            if current_min is None:
                lines.append("暂无有效价格数据")
            else:
                lines.append(f"最高：{_price_text(high_price)}")
                lines.append(f"最低：{_price_text(low_price)}")
                lines.append(f"平均：{_price_text(avg_price)}")
                lines.append(f"当前最低价：{_price_text(current_min)}")
            lines.append("")
        lines.append(f"📊 经济舱价格区间：{_cabin_price_range_text(all_flights, 'economy', analysis_result)}")
        lines.append(f"📊 商务舱价格区间：{_cabin_price_range_text(all_flights, 'business', analysis_result)}")
        lines.append("")
        lines.extend(_compact_source_summary_lines(source_stats_for_message))
        confidence = analysis_result.get("confidence") or {}
        if confidence:
            lines.append("")
            lines.append(
                f"馃搳 <b>鏁版嵁缃俊搴︼細{confidence.get('emoji', '')} "
                f"{confidence.get('level', '鏈煡')}</b>"
            )
            for reason in confidence.get("reasons", []):
                lines.append(f"銆€鈥?{reason}")
        _append_system_health_lines(
            lines, analysis_result.get("system_health") or {}
        )
        if is_round_trip:
            _append_low_option_count_notice(lines, outbound_analysis, "鍘荤▼")
            _append_low_option_count_notice(lines, return_analysis, "杩旂▼")
        else:
            _append_low_option_count_notice(lines, analysis_result)
        lines.append("")
        _append_purchase_checklist(lines, route_info, analysis_result)
        lines.append("")

        collected_at = _message_collected_time(analysis_result, route_info)
        lines.append(f"数据采集于 {collected_at} | 价格可能随时变动，建议尽快确认")
        lines.append("💡 机票价格实时波动，推荐方案基于采集时数据。")
        lines.append("点击链接后如价格有变化属于正常现象。")
        lines.append("如果涨价幅度超过5%，系统会在下次采集时提醒你。")
        lines.append("")
        _append_price_explanation_lines(lines)
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━")
        lines.append("以上数据来自第三方API，仅供参考。")
        lines.append("实际价格请以航司或OTA官网为准。")
        lines.append("以上排序基于当前配置规则，不代表最优选择。请根据您的时间、预算和出行需求自行判断。")
        return "<br>".join(lines)

    message = build_message(4)
    if len(message) > 8000 and len(economy_recs) >= 4:
        message = build_message(3)
    return message


def format_comparison_message(
    analysis_result: dict, route_info: dict, source_stats=None
) -> str:
    """生成多方案对比推送消息。"""
    recs = analysis_result.get("recommendations", [])
    days_to_dept = _days_to_depart(route_info)
    depart_date = route_info.get("depart_date", "")
    lines = [
        f"✈️ {_city_label(route_info.get('origin'))} → {_city_label(route_info.get('destination'))}",
        "",
        f"📅 出发日期：{depart_date}",
    ]
    if days_to_dept is not None:
        lines.append(f"⏳ 距出发还有：{days_to_dept}天")
    lines.append("以下方案按当前排序规则展示，排序不代表推荐。")
    lines.extend(["", "━━━ 符合条件的方案 ━━━", ""])

    for index, rec in enumerate(recs):
        flight = rec.get("flight", {})
        if index:
            lines.extend(["", "━━━━━━━━━━━━━━━━", ""])
        lines.append(_plan_title(index, rec.get("tag", "")))
        lines.append("")
        lines.append(format_flight_detail(flight, depart_date, None, route_info, analysis_result).replace("<br>", "\n"))

    prices = analysis_result.get("price_range") or []
    if len(prices) >= 2:
        lines.extend(["", "━━━━━━━━━━━━━━━━", "", f"📊 价格区间：{_money(prices[0])} - {_money(prices[1])}", ""])

    source_summary = format_source_summary(
        source_stats or route_info.get("source_stats") or analysis_result.get("source_stats")
    )
    if source_summary:
        lines.extend([source_summary, ""])

    lines.extend([
        f"🕐 采集时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "💬 总结",
        _summary_text(analysis_result, days_to_dept),
        "",
        "━━━━━━━━━━━━━━━━",
        "以上内容基于历史价格数据分析，仅供参考。",
        "实际购买请以航司或OTA官网价格为准。",
        "以上排序基于当前配置规则，不代表最优选择。请根据您的时间、预算和出行需求自行判断。",
    ])
    return "\n".join(lines)


















