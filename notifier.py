"""PushPlus notification helpers."""

from __future__ import annotations

import os
import re
import json
import time
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
    calculate_price_references,
    calc_confidence,
    determine_push_type,
    generate_decision_summary,
    generate_trend_summary,
    multi_window_analysis,
    price_position_description,
    waiting_risk_description,
)
from storage import get_last_push_price, save_last_push_price


BUY_SIGNALS = {"strong_buy", "buy", "buy_now"}
BASE_DIR = Path(__file__).parent
NOTIFICATIONS_LOG = BASE_DIR / "data" / "notifications_log.txt"
PUSHPLUS_MAX_CHARS = 30000
PUSHPLUS_COMPACT_CHARS = 25000
COMPACT_NOTICE = "完整方案详情因篇幅限制已精简，如需查看全部方案请回复'详情'"


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
            if "购买前请确认" in stripped:
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
                "历史平均",
                "历史最高",
                "数据量",
                "价格上涨概率",
                "价格下降概率",
                "平均涨",
                "平均降",
                "近60天",
                "数据点",
            ]
            if any(word in stripped for word in verbose_price_words):
                continue

        if level >= 2:
            if "关于价格说明" in stripped:
                skip_price_explanation = True
                continue
            if "执行评估：" in stripped or "票规校验" in stripped:
                continue
            if stripped.startswith(("├", "└")) and "综合等级" not in stripped:
                continue
            if any(word in stripped for word in ("票规匹配", "可购买性", "执行风险")):
                continue

        compacted.append(line)

    return _append_compact_notice("<br>".join(compacted))


def _prepare_pushplus_content(content: str) -> str:
    print(f"[推送] 消息长度: {len(content)} 字符")
    if len(content) <= PUSHPLUS_COMPACT_CHARS:
        return content

    compacted = _compact_pushplus_message(content, level=1)
    print(f"[推送] 消息较长，已精简: {len(compacted)} 字符")
    if len(compacted) <= PUSHPLUS_MAX_CHARS:
        return compacted

    compacted = _compact_pushplus_message(compacted, level=2)
    print(f"[推送] 二次精简后长度: {len(compacted)} 字符")
    if len(compacted) > PUSHPLUS_MAX_CHARS:
        compacted = _hard_limit_pushplus_message(compacted)
        print(f"[推送] 硬截断后长度: {len(compacted)} 字符")
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
        print(f"[推送] PushPlus返回空响应，消息可能过长({len(content)}字符)")
        return None
    try:
        return resp.json()
    except json.JSONDecodeError:
        print(f"[推送] JSON解析失败，响应内容: {resp.text[:200]}")
        print(f"[推送] 消息长度: {len(content)}字符，可能超出限制")
        return None


DISCLAIMER = "以上内容基于历史价格数据分析，仅供参考。\n实际购买请以航司或OTA官网价格为准。"


def format_price(price) -> str:
    """¥8,200 格式"""
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
        return f"理论{_price_text(display_price)} → 交易{_price_text(estimated_price)}"
    return (
        f"理论{_price_text(display_price)} → "
        f"交易{_price_text(estimated_price)}"
    )


def _price_discrepancy_notice(flight: dict) -> str:
    prices = [entry["price"] for entry in _source_price_entries_for_display(flight)]
    if len(prices) < 2:
        return ""
    low = min(prices)
    high = max(prices)
    if low > 0 and (high - low) / low > 0.10:
        return "⚠️ 各数据源价格差异较大，建议多平台比价"
    return ""


def _format_price(value) -> str:
    return format_price(value).replace("¥", "")


def percentile_to_words(pct) -> str:
    """把分位数翻译成人话"""
    if pct is None:
        return "样本还不够多"
    pct = float(pct)
    if pct < 10:
        return "非常罕见的低价"
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


def _city_label(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    upper = text.upper()
    if 2 <= len(upper) <= 4 and upper.isalpha():
        city = get_airport_city(upper)
        return city if city and city != upper else upper
    return text


def _display_route_summary(route_summary: str | None) -> str:
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
        lines.append("目前整体处于低价期")
    elif level == "typical":
        lines.append("目前整体价格正常")
    elif level == "high":
        lines.append("目前整体价格偏高，但你关注的航班相对划算")
    return "\n".join(f"- {line}" for line in lines) or "- 暂时没有可用的市场参考"


def _trend_description(analysis: dict) -> str:
    movement = analysis.get("movement")
    trend = (analysis.get("trend") or {}).get("trend")
    pct = analysis.get("percentile")
    if movement == "fare_class_jump":
        return "⚠️ 价格在最近出现了一次明显跳涨，这通常意味着便宜的舱位已经卖完了。跳涨后的价格很难再降回去。"
    if movement == "mean_reverting" and trend == "rising":
        return "价格在前几天降到了低点后开始回升，这可能是这轮降价的尾声。"
    if movement == "mean_reverting" and trend == "falling":
        return "价格最近还有回落迹象，如果你不急，可以再盯几天。"
    if movement == "stable" and pct is not None and float(pct) < 35:
        return "价格最近很稳定，而且处于较低水平，是比较踏实的入手时机。"
    if movement == "stable":
        return "价格最近很稳定，短期内大幅变化的概率不高。"
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
        return f"你现在正处在{window}，我会继续帮你盯着价格变化。"
    return (
        f"这条航线通常在{window}容易出现比较合适的价格。\n"
        f"你现在正好在这个窗口里。\n"
        f"当前价格又低于历史{cheaper_than:.0f}%的情况，机会不错。"
    )


def _risk_description(analysis: dict) -> str:
    wait_val = analysis.get("waiting_value")
    days = analysis.get("days_to_dept")
    if wait_val is not None and float(wait_val) > 0:
        avg_increase = float(wait_val)
        up_prob = min(85, max(55, 55 + avg_increase / 100))
        return (
            "根据历史数据，类似情况下继续等待，\n"
            f"价格上涨的概率约{up_prob:.0f}%，\n"
            f"平均会多花{format_price(avg_increase)}。"
        )
    if days is not None and days < 14:
        return (
            f"距离出发只剩{days}天了，\n"
            "临近出发价格几乎只涨不跌。"
        )
    return "如果继续等，可能还能看到小幅波动，但错过当前价格的风险也在增加。"


def _short_trend(analysis: dict) -> str:
    movement = analysis.get("movement")
    trend = (analysis.get("trend") or {}).get("trend")
    if movement == "fare_class_jump":
        return "最近有明显涨价迹象，继续等的风险变高。"
    if trend == "rising":
        return "最近价格在往上走。"
    if trend == "falling":
        return "最近价格还有回落迹象。"
    return "最近价格比较平稳。"


def _first_price(analysis: dict):
    return analysis.get("first_price") or analysis.get("avg_price") or analysis.get("current_price")


def _min_date(analysis: dict) -> str:
    return analysis.get("min_date") or "记录期内"


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
    pct = analysis.get("percentile")
    message = "\n".join(
        [
            "🔔 你的奥兰多机票到了好价格",
            "",
            f"✈️ {_route_info(analysis)}",
            f"📅 {analysis.get('depart_date', '-')} · 还有{analysis.get('days_to_dept', '-')}天",
            "",
            f"💰 当前价格：{format_price(analysis.get('current_price'))}",
            "",
            "📊 这个价格怎么样？",
            f"- 比最近一个月的均价便宜了 {format_price(_savings(analysis))}",
            f"- 在近期价格中排名前{float(pct):.0f}%（越低越便宜）" if pct is not None else "- 近期价格记录还不够多",
            _google_comparison(analysis),
            "",
            "📈 价格趋势",
            _trend_description(analysis),
            "",
            "💡 如果行程确定：现在入手",
            _reason_description(analysis),
            "",
            "⚡ 如果继续等",
            _risk_description(analysis),
        ]
    )
    return _append_disclaimer(message, run_status)


def format_consider_message(analysis, run_status: str | None = None) -> str:
    message = "\n".join(
        [
            "🟢 你的奥兰多机票价格还不错",
            "",
            f"✈️ {_route_info(analysis)}",
            f"📅 {analysis.get('depart_date', '-')} · 还有{analysis.get('days_to_dept', '-')}天",
            f"💰 {format_price(analysis.get('current_price'))}",
            "",
            (
                f"比均价便宜{format_price(_savings(analysis))}，"
                f"在近期价格中属于{percentile_to_words(analysis.get('percentile'))}。"
            ),
            _short_trend(analysis),
            "",
            "目前买入是合理的选择，但如果不急，",
            "还可以再观察几天看看有没有更低的价格。",
        ]
    )
    return _append_disclaimer(message, run_status)


def format_milestone_message(analysis, days, run_status: str | None = None) -> str:
    min_date = _min_date(analysis)
    if days == 30:
        advice = (
            "还有充足的时间，不用着急。"
            f"如果价格降到{format_price(_target_price(analysis))}以下我会第一时间通知你。"
        )
    elif days == 21:
        advice = "进入了通常的最佳购买窗口，值得密切关注。"
    elif days == 14:
        advice = "时间开始紧张了。如果价格没有明显下降，那么未来一周内做决定会更稳。"
    elif days == 7:
        advice = "最后一周了。如果价格能接受，那么今天确定会更稳。出发前几天涨价的概率很高。"
    else:
        advice = "今天是一个适合复盘价格的位置，我会继续盯着。"

    message = "\n".join(
        [
            f"⏰ 距离出发还有{days}天",
            "",
            f"✈️ {_route_info(analysis, include_stop=False)}",
            f"💰 当前价格：{format_price(analysis.get('current_price'))}",
            "",
            "📊 价格走势回顾",
            f"- 你开始关注时的价格：{format_price(_first_price(analysis))}",
            f"- 这段时间的最低价：{format_price(analysis.get('min_seen'))}（{min_date}）",
            f"- 这段时间的最高价：{format_price(analysis.get('max_seen'))}",
            "",
            advice,
        ]
    )
    return _append_disclaimer(message, run_status)


def format_alternative_message(analysis, run_status: str | None = None) -> str:
    alt = analysis.get("cheapest_alt") or {}
    target_price = analysis.get("current_price")
    alt_price = alt.get("price")
    diff = analysis.get("target_vs_cheapest")
    if diff is None and target_price is not None and alt_price is not None:
        diff = float(target_price) - float(alt_price)
    diff = max(float(diff or 0), 0)
    target_stopovers = analysis.get("stopovers") or 1
    alt_stopovers = alt.get("stopovers")
    if alt_stopovers is not None and alt_stopovers > target_stopovers:
        comparison_note = "不过这个方案需要多转一次机，总时长也更长，适合时间灵活的情况。"
    else:
        comparison_note = "中转次数相同，时长也接近，性价比更高。"

    message = "\n".join(
        [
            "💡 发现更便宜的航线方案",
            "",
            f"你关注的 {analysis.get('target_combo', '-')}：{format_price(target_price)}",
            "",
            "我发现了一个更便宜的选择：",
            f"✈️ {alt.get('flight_combo', '-')}",
            f"💰 {format_price(alt_price)}（便宜{format_price(diff)}）",
            f"🔄 {_display_route_summary(alt.get('route_summary', '-'))}",
            f"⏱️ 总时长{_duration_text(alt.get('duration_hours'))}",
            "",
            comparison_note,
        ]
    )
    return _append_disclaimer(message, run_status)


def format_message(
    analysis: dict, trigger_reason: str | None, run_status: str | None = None
) -> str:
    """Choose one human-friendly notification template."""
    if trigger_reason == "cheaper_alt" and analysis.get("cheapest_alt"):
        return format_alternative_message(analysis, run_status)
    if trigger_reason == "milestone":
        return format_milestone_message(
            analysis, analysis.get("days_to_dept"), run_status
        )
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
    """发送推送通知，优先PushPlus，备选企业微信。"""
    pushplus_token = os.environ.get("PUSHPLUS_TOKEN", "")
    if pushplus_token:
        msg = _prepare_pushplus_content(content)
        title = _notification_title_from_content(msg, title)
        for attempt in range(2):
            try:
                result = _post_pushplus(pushplus_token, title, msg)
                if result is None:
                    retry_msg = _compact_pushplus_message(msg, level=2)
                    retry_msg = _hard_limit_pushplus_message(retry_msg)
                    if retry_msg != msg:
                        print(f"[推送] 尝试二次精简重发: {len(retry_msg)} 字符")
                        result = _post_pushplus(pushplus_token, title, retry_msg)
                if result and result.get("code") == 200:
                    print("PushPlus推送成功")
                    return True
                print(f"PushPlus返回异常: {result}")
            except httpx.TimeoutException:
                print(f"[推送] 第{attempt + 1}次超时，{'重试中...' if attempt == 0 else '放弃'}")
            except Exception as exc:
                print(f"[推送] 异常: {exc}")
                break

            if attempt == 0:
                time.sleep(3)
        return False

    webhook = os.environ.get("WECOM_WEBHOOK", "")
    if webhook:
        try:
            resp = httpx.post(
                webhook,
                json={"msgtype": "markdown", "markdown": {"content": content}},
                timeout=15,
            )
            return resp.status_code == 200
        except Exception as exc:
            print(f"企业微信推送异常: {exc}")
            return False

    print("推送未配置，消息仅在终端显示：")
    print(content)
    return False


def health_report(results: list[dict]) -> str:
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
    return "、".join(dict.fromkeys(labels))


def format_source_summary(source_stats):
    if not source_stats:
        return ""

    display_names = {
        "serpapi": "SerpAPI（Google Flights）",
        "searchapi": "SearchAPI（Google Flights）",
        "travelpayouts": "Travelpayouts（Aviasales）",
        "skyscanner": "Skyscanner（via RapidAPI）",
        "duffel": "Duffel（航司直连）",
        "SerpAPISource": "SerpAPI（Google Flights）",
        "SearchAPISource": "SearchAPI（Google Flights）",
        "TravelpayoutsSource": "Travelpayouts（Aviasales）",
        "SkyscannerSource": "Skyscanner（via RapidAPI）",
        "DuffelSource": "Duffel（航司直连）",
    }

    lines = ["📡 数据源汇总"]

    for key, value in source_stats.items():
        if key in ("total_raw", "after_dedup", "enriched_count"):
            continue
        if not isinstance(value, dict):
            continue

        name = display_names.get(key, key)
        if value.get("status") == "成功":
            lines.append(f"    • {name}：{value['count']}个方案 ✅")
        else:
            lines.append(f"    • {name}：采集失败 ❌")

    total = source_stats.get("total_raw", 0)
    dedup = source_stats.get("after_dedup", 0)
    if total > 0:
        lines.append(f"    • 合计采集{total}个 → 去重后{dedup}个方案")

    return "\n".join(lines)


def format_price_change(current_price, previous_price) -> str:
    """和上一次推送的价格对比"""
    if previous_price is None:
        return "📊 首次采集，暂无历史对比"

    diff = current_price - previous_price
    pct = diff / previous_price * 100

    if abs(diff) < 50:
        return f"📊 价格基本持平（和上次相比变化¥{abs(diff):,.0f}）"
    elif diff < 0:
        return f"📊 比上次便宜了 ¥{abs(diff):,.0f}（↓{abs(pct):.1f}%）🟢"
    else:
        return f"📊 比上次贵了 ¥{diff:,.0f}（↑{pct:.1f}%）🔴"


def text_price_chart(prices_with_dates: list[tuple], width=20) -> str:
    """用文字字符画一个迷你价格走势图"""
    if len(prices_with_dates) < 3:
        return "📈 数据积累中，3天后可显示走势图"

    prices = [price for _, price in prices_with_dates if price is not None]
    if len(prices) < 3:
        return "📈 数据积累中，3天后可显示走势图"

    min_p = min(prices)
    max_p = max(prices)

    if max_p == min_p:
        return "📈 " + "━" * width + f" ¥{min_p:,.0f}"

    blocks = " ▁▂▃▄▅▆▇█"
    recent_points = prices_with_dates[-width:]
    recent_prices = [price for _, price in recent_points]

    chart = "📈 "
    for price in recent_prices:
        level = int((price - min_p) / (max_p - min_p) * 8)
        level = min(8, max(0, level))
        chart += blocks[level]

    chart_width = min(width, len(recent_prices))
    chart += f"\n   ¥{min_p:,.0f}"
    chart += " " * max(1, chart_width - 8)
    chart += f"¥{max_p:,.0f}"

    first_date = recent_points[0][0][:10] if recent_points else ""
    last_date = recent_points[-1][0][:10] if recent_points else ""
    chart += f"\n   {first_date}  →  {last_date}"

    return chart


def _price_change_phrase(label: str, current_price, previous_price) -> str | None:
    if current_price is None or previous_price is None:
        return None

    diff = current_price - previous_price
    if abs(diff) < 50:
        return f"{label}基本持平"
    if diff < 0:
        return f"{label}降了¥{abs(diff):,.0f} 🟢"
    return f"{label}涨了¥{diff:,.0f} 🔴"


def _find_recommendation_flight(recs: list[dict], keyword: str) -> dict | None:
    for rec in recs:
        if keyword in str(rec.get("tag", "")):
            return rec.get("flight")
    return None


def _overall_price_change_summary(analysis_result: dict, previous_prices: dict) -> str:
    if not previous_prices:
        return "📊 和上次对比：首次采集，暂无历史对比"

    recs = analysis_result.get("recommendations", [])
    all_flights = analysis_result.get("all_flights", [])
    cheapest = _find_recommendation_flight(recs, "最低价")
    fastest = _find_recommendation_flight(recs, "最快")

    if cheapest is None and all_flights:
        cheapest = min(all_flights, key=lambda flight: flight.get("price") or float("inf"))
    if fastest is None and all_flights:
        fastest = min(
            all_flights,
            key=lambda flight: flight.get("total_duration_min") or float("inf"),
        )

    parts = []
    if cheapest:
        combo = cheapest.get("flight_combo")
        parts.append(
            _price_change_phrase(
                "最低价", cheapest.get("price"), previous_prices.get(combo)
            )
        )
    if fastest:
        combo = fastest.get("flight_combo")
        parts.append(
            _price_change_phrase(
                "最快方案", fastest.get("price"), previous_prices.get(combo)
            )
        )

    parts = [part for part in parts if part]
    if not parts:
        return "📊 和上次对比：暂无可匹配的历史方案"
    return f"📊 和上次对比：{'，'.join(parts)}"


def format_booking_links_text(flight: dict, depart_date: str) -> str:
    """生成各平台的搜索链接"""
    segments = flight.get("segments") or []
    if not segments:
        return ""

    origin = segments[0].get("dep_airport", "")
    dest = segments[-1].get("arr_airport", "")
    if not origin or not dest:
        return ""

    entries = _possible_booking_links(
        origin,
        dest,
        depart_date,
        flight.get("flight_combo"),
        flight.get("cabin_class") or "economy",
    )

    lines = ["🔗 购买", ""]
    circled_numbers = ["①", "②", "③", "④", "⑤", "⑥"]
    for index, (label, url) in enumerate(entries):
        prefix = circled_numbers[index] if index < len(circled_numbers) else f"{index + 1}."
        lines.extend([f"{prefix} {label}", url, ""])
    lines.append(_booking_options_hint(flight))

    return "\n".join(lines)


def generate_warnings(flight: dict) -> list[str]:
    """检测航班方案的潜在问题"""
    warnings = []

    for layover in flight.get("layovers", []) or []:
        wait_minutes = layover.get("wait_minutes", 0) or 0
        if wait_minutes > 480:
            warnings.append(
                f"⚠️ 在{layover.get('city', '中转地')}转机需等待{wait_minutes // 60}小时，"
                "可能需要在机场过夜或额外订酒店"
            )

    for segment in flight.get("segments", []) or []:
        dep_time = segment.get("dep_time", "")
        if " " not in dep_time:
            continue

        time_part = dep_time.split(" ")[1]
        try:
            hour = int(time_part.split(":")[0])
        except (TypeError, ValueError):
            continue

        if 0 <= hour < 6:
            warnings.append(
                f"⚠️ {segment.get('flight_no', '')}是凌晨航班"
                f"（{time_part}起飞），注意交通安排"
            )

    for layover in flight.get("layovers", []) or []:
        wait_minutes = layover.get("wait_minutes", 999) or 999
        if 0 < wait_minutes < 75:
            warnings.append(
                f"⚠️ 在{layover.get('city', '中转地')}转机时间仅{wait_minutes}分钟，"
                "如果前段航班延误可能赶不上，需要确认是否联程票"
            )

    segments = flight.get("segments", []) or []
    if segments:
        dep_date = (segments[0].get("dep_time", "") or "")[:10]
        arr_date = (segments[-1].get("arr_time", "") or "")[:10]
        if dep_date and arr_date and dep_date != arr_date:
            warnings.append(f"ℹ️ 到达日期为{arr_date}（非出发当天），注意安排接机和住宿")

    return warnings


def _format_duffel_extra(flight: dict) -> str:
    data_source = flight.get("data_source") or ""
    if "duffel" not in data_source:
        return ""

    extra = flight.get("extra") or {}
    baggage = extra.get("baggage") or []
    checked_quantity = sum(
        int(item.get("quantity") or 0)
        for item in baggage
        if "checked" in str(item.get("type", "")).lower()
    )
    carry_on_quantity = sum(
        int(item.get("quantity") or 0)
        for item in baggage
        if "carry" in str(item.get("type", "")).lower()
    )

    if checked_quantity:
        baggage_text = f"含{checked_quantity}件托运行李"
    elif carry_on_quantity:
        baggage_text = f"含{carry_on_quantity}件随身行李"
    elif baggage:
        total_quantity = sum(int(item.get("quantity") or 0) for item in baggage)
        baggage_text = f"含{total_quantity}件行李"
    else:
        baggage_text = "Duffel未返回明确行李额度"

    change_text = "可改签" if extra.get("changeable") else "不可改签"
    refund_text = "可退票" if extra.get("refundable") else "不可退票"

    return f"  🧳 行李：{baggage_text}\n  🔄 退改：出发前{change_text}，{refund_text}\n"


def _days_to_depart(route_info: dict) -> int | None:
    days = route_info.get("days_to_dept")
    if days is not None:
        try:
            return int(days)
        except (TypeError, ValueError):
            return None

    depart_date = route_info.get("depart_date")
    if not depart_date:
        return None

    try:
        return (date.fromisoformat(depart_date) - date.today()).days
    except ValueError:
        return None


MODE_LABELS = {
    "balanced": "🎯 当前模式：均衡排序",
    "budget": "💰 当前模式：省钱优先",
    "fast": "⚡ 当前模式：速度优先",
    "comfort": "🛋️ 当前模式：舒适优先",
}


def _mode_label(mode: str | None) -> str:
    return MODE_LABELS.get(mode or "balanced", MODE_LABELS["balanced"])


def format_summary_advice(analysis, days_to_dept) -> str:
    """一句话总结情况"""
    market = analysis.get("market_context", {})
    level = market.get("price_level", "typical")
    cheapest = analysis["price_range"][0]

    if level == "low" and days_to_dept > 14:
        return (
            "💬 总结：当前整体处于低价期，"
            f"最低¥{cheapest:,}是不错的价格。"
            "如果行程确定，那么可以抓住低价期；低价期通常不会持续太久。"
        )
    elif level == "low" and days_to_dept <= 14:
        return (
            "💬 总结：低价期+临近出发，"
            "如果行程确定，那么尽快购买会更稳。"
        )
    elif level == "typical" and days_to_dept > 30:
        return (
            "💬 总结：当前价格正常，不着急的话可以再观察一两周。"
            "我会持续监控，有好价格第一时间通知你。"
        )
    elif level == "typical" and days_to_dept <= 30:
        return (
            "💬 总结：价格正常，距出发不到一个月了。"
            "如果看到合适的方案可以考虑入手，再往后价格上涨概率增大。"
        )
    elif level == "high":
        return (
            "💬 总结：当前价格偏高。"
            f"如果不急，那么等价格回落到¥{market.get('typical_range', [0, 0])[0]:,}以下再考虑。"
        )
    else:
        return "💬 总结：我会持续关注这条航线的价格变化。"


def _money(value) -> str:
    return _price_text(value)


def _budget_notice(current_min, budget) -> str | None:
    price = _to_float(current_min)
    budget_value = _to_float(budget)
    if price is None or price <= 0 or budget_value is None or budget_value <= 0:
        return None
    if price <= budget_value:
        return "✅ 发现符合预算的航班！"
    return f"当前最低价超出预算¥{price - budget_value:,.0f}，继续监控中"



def _legacy_price_scale_lines(current_min, route_info: dict, analysis_result: dict) -> list[str]:
    price = _to_float(current_min)
    if price is None or price <= 0:
        return ["<b>💰 价格标尺</b>", "当前最低价：暂无有效价格数据", ""]
    if price is None:
        return []

    target = (
        _to_float(analysis_result.get("target_price_effective"))
        or _to_float(_preference_value(route_info, analysis_result, "target_price"))
    )
    max_budget = (
        _to_float(analysis_result.get("max_budget"))
        or _to_float(_preference_value(route_info, analysis_result, "max_budget"))
        or _to_float(_preference_value(route_info, analysis_result, "budget"))
    )
    if target is None and max_budget is None:
        return []

    lines = ["<b>\U0001f4b0 \u4ef7\u683c\u6807\u5c3a</b>"]
    if target:
        lines.append(f"\u7406\u60f3\u5165\u624b\u4ef7\uff1a\u00a5{target:,.0f}")
        if price <= target:
            lines.append(f"\u5f53\u524d\u6700\u4f4e\u4ef7\uff1a\u00a5{price:,.0f} \u2705 \u5df2\u8fbe\u6807\uff01")
            lines.append(
                f"\U0001f525 \u5f53\u524d\u6700\u4f4e\u4ef7\u00a5{price:,.0f}\uff0c\u5df2\u4f4e\u4e8e\u4f60\u7684\u7406\u60f3\u5165\u624b\u4ef7\u00a5{target:,.0f}\uff01"
            )
        else:
            lines.append(f"\u5f53\u524d\u6700\u4f4e\u4ef7\uff1a\u00a5{price:,.0f}")
            lines.append(
                f"\U0001f4ca \u5f53\u524d\u6700\u4f4e\u4ef7\u00a5{price:,.0f}\uff0c\u8ddd\u79bb\u7406\u60f3\u4ef7\u8fd8\u5dee\u00a5{price - target:,.0f}\uff0c\u5efa\u8bae\u7ee7\u7eed\u89c2\u671b"
            )
    else:
        lines.append(f"\u5f53\u524d\u6700\u4f4e\u4ef7\uff1a\u00a5{price:,.0f}")
    if max_budget:
        lines.append(f"\u6700\u9ad8\u53ef\u63a5\u53d7\uff1a\u00a5{max_budget:,.0f}")
    lines.append("")
    return lines

def _goals(route_info: dict, analysis_result: dict) -> set[str]:
    goals = route_info.get("goals") or analysis_result.get("goals") or []
    if not goals:
        return {"price_drop_alert", "buy_timing", "best_overall"}
    return set(goals)


def _primary_goal(route_info: dict, analysis_result: dict) -> str | None:
    notification_goals = (
        route_info.get("notification_goals")
        or analysis_result.get("notification_goals")
        or {}
    )
    return notification_goals.get("primary") if isinstance(notification_goals, dict) else None


def _preference_value(route_info: dict, analysis_result: dict, key: str, default=None):
    soft = route_info.get("soft_preferences") or analysis_result.get("soft_preferences") or {}
    hard = route_info.get("hard_constraints") or analysis_result.get("hard_constraints") or {}
    user_preferences = analysis_result.get("user_preferences") or {}
    return (
        route_info.get(key)
        or analysis_result.get(key)
        or user_preferences.get(key)
        or soft.get(key)
        or hard.get(key)
        or default
    )


def _companions_label(value: str) -> str:
    mapping = {
        "solo": "仅本人",
        "with_elderly": "有老人同行",
        "with_child": "有小孩同行",
        "with_elderly_child": "老人和小孩都有",
    }
    return mapping.get(value or "solo", value or "仅本人")


def _price_sensitivity_label(value: str) -> str:
    mapping = {
        "low": "便利和稳定优先",
        "medium": "便宜约200元可接受轻微不便",
        "high": "便宜500元以上可接受不方便",
        "max": "价格优先",
    }
    return mapping.get(value or "low", value or "便利和稳定优先")


def _trip_rigidity_guidance(value: str) -> str:
    mapping = {
        "confirmed": "基本不会变，确定出行。",
        "mostly": "可能小幅调整日期。",
        "flexible": "不太确定，可能改期或取消。",
    }
    return mapping.get(value or "confirmed", mapping["confirmed"])


def _flight_has_refund_change(flight: dict) -> bool:
    fare_rules = flight.get("fare_rules") or {}
    change_rules = fare_rules.get("change") or {}
    refund_rules = fare_rules.get("refund") or {}
    extra = flight.get("extra") or {}
    refund_change = extra.get("refund_change") or {}
    return bool(
        change_rules.get("allowed")
        or refund_rules.get("allowed")
        or refund_change.get("changeable")
        or refund_change.get("refundable")
        or extra.get("changeable")
        or extra.get("refundable")
    )


def _refund_rigidity_tip(route_info: dict, analysis_result: dict) -> str | None:
    trip_rigidity = _preference_value(route_info, analysis_result, "trip_rigidity", "confirmed")
    refund_flexibility = _preference_value(route_info, analysis_result, "refund_flexibility", "unknown")
    if trip_rigidity == "flexible" and refund_flexibility == "not_needed":
        return "💡 您的行程存在变动可能，建议关注可退改方案"
    if trip_rigidity != "confirmed" or refund_flexibility != "required":
        return None

    flights = analysis_result.get("all_flights") or []
    flexible_prices = [
        _to_float(flight.get("price"))
        for flight in flights
        if _flight_has_refund_change(flight) and _to_float(flight.get("price"))
    ]
    locked_prices = [
        _to_float(flight.get("price"))
        for flight in flights
        if not _flight_has_refund_change(flight) and _to_float(flight.get("price"))
    ]
    if flexible_prices and locked_prices:
        diff = min(flexible_prices) - min(locked_prices)
        if diff > 0:
            return f"💡 行程已确定，选择不可退改的票可能更便宜¥{diff:,.0f}"
    return "💡 行程已确定，选择不可退改的票可能更便宜"


def _price_sensitivity_flight_note(flight: dict, all_flights: list[dict], sensitivity: str) -> str | None:
    if not all_flights:
        return None
    price = _to_float(flight.get("price")) or 0
    min_price = min(
        (_to_float(item.get("price")) for item in all_flights if _to_float(item.get("price"))),
        default=0,
    )
    diff = max(0, price - min_price)
    stops = int(flight.get("stops") or 0)
    is_inconvenient = stops > 0 or _max_wait_minutes(flight) > 360 or _is_redeye_for_message(flight)
    if sensitivity == "low" and not is_inconvenient:
        return "价格敏感度：保留同等便利条件下的方案"
    if sensitivity == "medium" and is_inconvenient:
        return f"价格敏感度：便宜时可考虑，但需接受中转/时段不便"
    if sensitivity == "high" and is_inconvenient:
        return f"价格敏感度：比最低价贵¥{diff:,.0f}，重点看省钱和取舍"
    if sensitivity == "max":
        return "价格敏感度：价格优先展示"
    return None


def _max_wait_minutes(flight: dict) -> int:
    return max(
        (int(layover.get("wait_minutes") or 0) for layover in flight.get("layovers") or []),
        default=0,
    )


def _is_redeye_for_message(flight: dict) -> bool:
    segments = flight.get("segments") or []
    if not segments:
        return False
    dep_time = _time_only(segments[0].get("dep_time"))
    try:
        hour = int(str(dep_time).split(":", 1)[0])
    except (TypeError, ValueError):
        return False
    return hour >= 23 or hour < 6


def _time_only(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).replace("T", " ")
    if " " in text:
        text = text.split(" ", 1)[1]
    return text[:5] if len(text) >= 5 else text


def _hour_from_time_text(value: str | None) -> int | None:
    time_text = _time_only(value)
    try:
        return int(time_text.split(":", 1)[0])
    except (AttributeError, TypeError, ValueError):
        return None


def _slot_label_from_hour(hour: int | None, arrival: bool = False) -> str:
    if hour is None:
        return "时段未知"
    if 6 <= hour < 9:
        return "清晨" if arrival else "早班"
    if 9 <= hour < 12:
        return "上午"
    if 12 <= hour < 17:
        return "下午"
    if 17 <= hour < 20:
        return "傍晚"
    if 20 <= hour < 23:
        return "晚间" if arrival else "晚班"
    return "凌晨" if arrival else "红眼"


def _flight_slot_label(flight: dict) -> str:
    segments = flight.get("segments") or []
    if not segments:
        return "时段未知"
    dep_label = _slot_label_from_hour(
        _hour_from_time_text(segments[0].get("dep_time")), arrival=False
    )
    arr_label = _slot_label_from_hour(
        _hour_from_time_text(segments[-1].get("arr_time")), arrival=True
    )
    return f"{dep_label}起飞·{arr_label}到达"


def _duration_hours(flight: dict) -> str:
    hours = flight.get("total_hours")
    if hours is None:
        duration_min = flight.get("total_duration_min") or 0
        hours = round(duration_min / 60, 1) if duration_min else 0
    return f"{float(hours):.1f}".rstrip("0").rstrip(".")


def _wait_text(minutes) -> str:
    minutes = int(minutes or 0)
    return f"{minutes // 60}小时{minutes % 60}分钟"


def _segment_title(index: int) -> str:
    names = ["第一段", "第二段", "第三段", "第四段", "第五段", "第六段"]
    return names[index] if index < len(names) else f"第{index + 1}段"


def _airline_display(name: str | None) -> str:
    mapping = {
        "American Airlines": "美航",
        "Air Canada": "加航",
        "United": "联合",
        "United Airlines": "联合",
        "Delta": "达美",
        "Delta Air Lines": "达美",
    }
    return mapping.get(name or "", name or "")


def _airline_full_display(name: str | None) -> str:
    if not name:
        return ""

    short_name = _airline_display(name)
    if short_name and short_name != name:
        return f"{short_name} {name}"
    return name


def _airlines_from_segments(segments: list[dict]) -> list[str]:
    airlines = []
    for segment in segments:
        airline = _airline_full_display(segment.get("airline", ""))
        if airline and airline not in airlines:
            airlines.append(airline)
    return airlines


def _plan_title(index: int, tag: str) -> str:
    names = ["一", "二", "三", "四", "五"]
    plan_no = names[index] if index < len(names) else str(index + 1)
    parts = str(tag or "方案").split(maxsplit=1)
    icon = parts[0] if parts else "⭐"
    label = parts[1] if len(parts) > 1 else str(tag or "方案")
    return f"{icon} 方案{plan_no}：{label}"


def _market_line(market: dict) -> str | None:
    level = market.get("price_level")
    typical_range = market.get("typical_range") or market.get("typical_price_range")
    if not level and not typical_range:
        return None

    level_text = {"low": "低价期", "typical": "正常水平", "high": "偏贵"}.get(
        level, level or "暂无评级"
    )
    if typical_range and len(typical_range) >= 2:
        return (
            f"📊 当前市场：{level_text}"
            f"（通常{_money(typical_range[0])}-{_money(typical_range[1])}）"
        )
    return f"📊 当前市场：{level_text}"


def _layover_risk_line(layover: dict) -> str | None:
    wait = int(layover.get("wait_minutes") or 0)
    if wait <= 0:
        return None
    if wait < 75:
        return "🔴 风险较高：转机时间较短"
    if wait < 120:
        return "🟡 需注意：需快速通关"
    if wait > 600:
        return "🟡 需注意：转机超过10小时，可能需要过夜"
    if wait > 360:
        return "🟡 需注意：等待时间较长"
    return None


def _duffel_extra_lines(flight: dict) -> list[str]:
    data_source = flight.get("data_source") or ""
    if "duffel" not in data_source:
        return []

    extra = flight.get("extra") or {}
    baggage = extra.get("baggage") or []
    checked_quantity = sum(
        int(item.get("quantity") or 0)
        for item in baggage
        if "checked" in str(item.get("type", "")).lower()
    )
    carry_on_quantity = sum(
        int(item.get("quantity") or 0)
        for item in baggage
        if "carry" in str(item.get("type", "")).lower()
    )

    if checked_quantity:
        baggage_text = f"含{checked_quantity}件托运行李"
    elif carry_on_quantity:
        baggage_text = f"含{carry_on_quantity}件随身行李"
    elif baggage:
        total_quantity = sum(int(item.get("quantity") or 0) for item in baggage)
        baggage_text = f"含{total_quantity}件行李"
    else:
        baggage_text = "Duffel未返回明确行李额度"

    change_text = "可改签" if extra.get("changeable") else "不可改签"
    refund_text = "可退票" if extra.get("refundable") else "不可退票"
    return [f"🧳 行李：{baggage_text}", f"🔄 退改：出发前{change_text}，{refund_text}"]


def _clean_warning(warning: str) -> str:
    text = re.sub(r"^[⚠️ℹ️✅🟡🔴\s]+", "", str(warning)).strip()
    text = re.sub(
        r"^在(.+?)转机需等待(\d+)小时，可能需要在机场过夜或额外订酒店$",
        r"\1转机等待\2小时，可能需要过夜或订酒店",
        text,
    )
    text = re.sub(
        r"^到达日期为\d{4}-\d{2}-\d{2}（非出发当天），注意安排接机和住宿$",
        "到达日期为次日（非出发当天）",
        text,
    )
    return text


def _summary_text(analysis_result: dict, days_to_dept: int | None) -> str:
    if days_to_dept is None:
        return "我会持续关注这条航线的价格变化。"
    summary = format_summary_advice(analysis_result, days_to_dept)
    summary = summary.replace("💬 总结：", "").replace("💬 总结", "").strip()
    return summary.replace("。我", "。\n我")


def _plain_price_position(position: str) -> str:
    text = str(position or "").strip()
    for marker in ["🟢", "🔴", "🟡", "🟠"]:
        text = text.replace(marker, "")
    return text.strip()


def _short_price_position(position: str) -> str:
    text = _plain_price_position(position)
    if "低于平均" in text:
        return "低于平均"
    if "高于平均" in text:
        return "高于平均"
    if "历史最低" in text:
        return "接近最低"
    if "历史最高" in text:
        return "接近最高"
    return text or "位置未知"


def _plain_recent_trend(recent_trend: str) -> str:
    text = str(recent_trend or "").strip()
    for prefix in ["📈 近期", "📉 近期", "➡️ 近期"]:
        if text.startswith(prefix):
            text = text.replace(prefix, "", 1)
            break
    return text or "暂无趋势"


def get_tag_color(tag):
    if "最低价" in str(tag):
        return "#34a853"
    if "最快" in str(tag):
        return "#1a73e8"
    if "最优" in str(tag):
        return "#f59e0b"
    if "最少" in str(tag):
        return "#8b5cf6"
    return "#666"


def _mode_text(mode: str | None) -> str:
    mapping = {
        "balanced": "均衡排序",
        "budget": "省钱优先",
        "fast": "速度优先",
        "comfort": "舒适优先",
    }
    return mapping.get(mode or "balanced", mode or "均衡排序")


def _sort_rule_text(mode: str | None) -> str:
    mapping = {
        "balanced": "📋 当前排序：综合考虑价格、时长、转机（可在配置中修改）",
        "budget": "📋 当前排序：价格从低到高（可在配置中修改）",
        "fast": "📋 当前排序：总时长从短到长（可在配置中修改）",
        "comfort": "📋 当前排序：转机次数少、等待时间短优先（可在配置中修改）",
    }
    return mapping.get(mode or "balanced", mapping["balanced"])


def format_market_level(market: dict) -> str:
    level = market.get("price_level")
    typical_range = market.get("typical_range") or market.get("typical_price_range")
    level_text = {"low": "低价期", "typical": "正常水平", "high": "偏贵"}.get(
        level, level or "暂无评级"
    )
    if typical_range and len(typical_range) >= 2:
        return f"{level_text}（通常{_money(typical_range[0])}-{_money(typical_range[1])}）"
    return level_text


def _flight_airlines(flight: dict) -> list[str]:
    airlines = list(flight.get("airlines") or [])
    for segment in flight.get("segments", []) or []:
        airline = segment.get("airline")
        if airline and airline not in airlines:
            airlines.append(airline)
    airline_summary = flight.get("airline_summary")
    if airline_summary:
        airlines.append(airline_summary)
    return airlines


def _clean_flight_numbers(flight_nos) -> tuple[str, str]:
    if isinstance(flight_nos, str):
        numbers = re.split(r"[+,/]\s*", flight_nos)
    else:
        numbers = [str(item) for item in (flight_nos or [])]
    numbers = [number.strip() for number in numbers if number and number.strip()]
    display = " ".join(numbers).strip()
    compact = "".join(re.sub(r"\s+", "", number) for number in numbers)
    return display, compact


def build_google_flights_url(origin, dest, date_str, return_date=None):
    """Build a Google Flights search URL with a safe Google Search fallback."""
    origin = str(origin or "").upper()
    dest = str(dest or "").upper()
    date_str = str(date_str or "")
    params = [
        f"departure_id={quote_plus(origin)}",
        f"arrival_id={quote_plus(dest)}",
        f"outbound_date={quote_plus(date_str)}",
        "type=2",
        "curr=CNY",
        "hl=zh-CN",
    ]
    if return_date:
        params[3] = "type=1"
        params.append(f"return_date={quote_plus(str(return_date))}")
    return "https://www.google.com/travel/flights/search?" + "&".join(params)


def _google_flights_url(origin: str, dest: str, date_str: str) -> str:
    """Build a direct Google Flights search URL."""
    return build_google_flights_url(origin, dest, date_str)


def _google_flights_query_url(origin: str, dest: str, date_str: str) -> str:
    origin = str(origin or "").upper()
    dest = str(dest or "").upper()
    date_str = str(date_str or "")
    origin_en = AIRPORT_CITY_EN.get(origin, origin)
    dest_en = AIRPORT_CITY_EN.get(dest, dest)
    return (
        "https://www.google.com/travel/flights"
        f"?q=flights+from+{quote_plus(origin_en)}+{origin}+to+"
        f"{quote_plus(dest_en)}+{dest}+on+{date_str}"
        "&curr=CNY&hl=zh-CN"
    )


def _ctrip_url(origin: str, dest: str, date_str: str, cabin="economy", flight_no: str = "") -> str:
    """Build a Ctrip one-way search URL with date, cabin, and optional flight number."""
    origin = str(origin or "").upper()
    dest = str(dest or "").upper()
    date_str = str(date_str or "")
    origin_city = get_airport_city(origin)
    dest_city = get_airport_city(dest)
    cabin_code = "c" if str(cabin or "").lower() == "business" else "y_s"
    url = (
        f"https://flights.ctrip.com/online/list/oneway-{origin_city}-{dest_city}"
        f"?depdate={date_str}&cabin={cabin_code}"
    )
    if flight_no:
        url += f"&flightno={quote_plus(str(flight_no))}"
    return url


AIRLINE_BOOKING_URLS = {
    "MU": ("东航官网", "https://www.ceair.com"),
    "CA": ("国航官网", "https://www.airchina.com.cn"),
    "CZ": ("南航官网", "https://www.csair.com"),
    "9C": ("春秋航空官网", "https://www.ch.com"),
    "HO": ("吉祥航空官网", "https://www.juneyaoair.com"),
    "3U": ("川航官网", "https://www.sichuanair.com"),
    "NH": ("全日空官网", "https://www.ana.co.jp/zh/cn/"),
    "JL": ("日航官网", "https://www.jal.co.jp/zhcn/"),
    "MM": ("乐桃官网", "https://www.flypeach.com/zh-cn"),
    "AA": ("美航官网", "https://www.aa.com"),
    "UA": ("美联航官网", "https://www.united.com"),
    "DL": ("达美官网", "https://zh.delta.com"),
}


def _option_price(option: dict):
    try:
        price = float(option.get("price") or 0)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _verified_booking_options(flight: dict | None) -> list[dict]:
    options = []
    for option in (flight or {}).get("booking_options") or []:
        if not isinstance(option, dict):
            continue
        if option.get("url") and _option_price(option):
            options.append(option)
    return sorted(options, key=lambda option: _option_price(option) or 999999)


def _airline_code_from_flight_nos(flight_nos=None) -> str:
    display, _ = _clean_flight_numbers(flight_nos)
    match = re.match(r"\s*([A-Z0-9]{2})", display.upper())
    return match.group(1) if match else ""


def _possible_booking_links(origin, dest, date_str, flight_nos=None, cabin="economy") -> list[tuple[str, str]]:
    origin = str(origin or "").upper()
    dest = str(dest or "").upper()
    date_str = str(date_str or "")
    origin_city = AIRPORT_CITY.get(origin, origin)
    dest_city = AIRPORT_CITY.get(dest, dest)
    ctrip_url = (
        "https://flights.ctrip.com/online/list/"
        f"oneway-{origin}-{dest}?depdate={date_str}&cabin=y_s"
    )
    fliggy_url = (
        "https://s.fliggy.com/search?"
        f"keyword={quote(origin_city + '到' + dest_city + '机票+' + date_str)}"
    )
    qunar_url = (
        "https://flight.qunar.com/site/oneway_list.htm"
        f"?searchDepartureAirport={quote(origin_city)}"
        f"&searchArrivalAirport={quote(dest_city)}"
        f"&searchDepartureTime={date_str}"
    )
    trip_url = (
        "https://www.trip.com/flights/"
        f"{origin.lower()}-to-{dest.lower()}/tickets-{origin.lower()}-{dest.lower()}"
        f"?dcity={origin}&acity={dest}&ddate={date_str}&class=Y"
    )
    sky_url = (
        "https://www.tianxun.com/transport/flights/"
        f"{origin}/{dest}/{date_str}/"
        "?adultsv2=1&cabinclass=economy&currency=CNY"
    )

    return [
        ("携程", ctrip_url),
        ("飞猪", fliggy_url),
        ("去哪儿", qunar_url),
        ("Trip.com", trip_url),
        ("天巡", sky_url),
        (
            "Google Flights",
            "https://www.google.com/travel/flights?"
            f"q=flights+from+{quote(origin_city)}+to+{quote(dest_city)}+on+{date_str}"
            "&curr=CNY&hl=zh-CN",
        ),
    ]


def _booking_link_entries(
    origin, dest, date_str, flight_nos=None, cabin="economy", flight: dict | None = None
) -> tuple[list[tuple[str, str]], bool]:
    verified = _verified_booking_options(flight)
    if verified:
        entries = []
        for option in verified:
            platform = str(option.get("platform") or "购买渠道")
            price = _option_price(option)
            entries.append((f"{platform} {_price_text(price)} 🟢", option["url"]))
        return entries, True

    fallback = [
        (f"{name} 🟡", url)
        for name, url in _possible_booking_links(origin, dest, date_str, flight_nos, cabin)
        if url
    ]
    return fallback, False


def _booking_options_hint(flight: dict | None) -> str:
    return "⚠️ 价格以各平台实际显示为准"


def generate_booking_links(
    origin,
    dest,
    date_str,
    flight_no="",
    origin_city="",
    dest_city="",
    flight: dict | None = None,
):
    """生成多个OTA的购买链接，按国内用户优先级排序。"""
    origin = str(origin or "").upper()
    dest = str(dest or "").upper()
    date_str = str(date_str or "")
    if not origin_city:
        origin_city = AIRPORT_CITY.get(origin, origin)
    if not dest_city:
        dest_city = AIRPORT_CITY.get(dest, dest)
    links = _possible_booking_links(origin, dest, date_str, flight_no)
    return " | ".join(
        f'<a href="{url}" target="_blank">{label}</a>' for label, url in links
    )


def _format_html_baggage(extra: dict) -> str:
    baggage = extra.get("baggage") or []
    if not baggage:
        return ""
    checked_quantity = sum(
        int(item.get("quantity") or 0)
        for item in baggage
        if "checked" in str(item.get("type", "")).lower()
    )
    if checked_quantity:
        return f"🧳 含{checked_quantity}件托运行李<br>"
    total_quantity = sum(int(item.get("quantity") or 0) for item in baggage)
    if total_quantity:
        return f"🧳 含{total_quantity}件行李<br>"
    return "🧳 已返回行李信息<br>"


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

    # 如果没有任何行李数据（非Duffel数据源）
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

    # 舱位
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

    # 选座
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
        return f"{dep_time} → 到达：{arr_time}（当地时间）"
    return "请查询航司官网"


def _seat_selection_line(extra: dict) -> str:
    seat = extra.get("seat_detail") or {}
    if not seat:
        return "🪑 选座：请查询航司官网"
    if seat.get("seat_selectable"):
        if seat.get("seat_free"):
            return "🪑 选座：免费"
        if seat.get("seat_price"):
            price = float(seat["seat_price"])
            currency = seat.get("seat_currency", "CNY")
            if currency == "CNY":
                return f"🪑 选座：需付费（¥{price:,.0f}起）"
            return f"🪑 选座：需付费（{currency} {price:,.0f}起）"
        return "🪑 选座：可选座（费用详询航司）"
    return "🪑 选座：请查询航司官网"


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
        lines.append("📎 服务信息来源：Duffel（航司直连）")
        return lines

    return [
        "🧳 行李：请查询航司官网",
        "🪑 选座：请查询航司官网",
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
        lines.append(f"历史上类似时间点/价格水平的记录中，下一次价格继续下降的比例约{drop_probability}%。")
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
    return f"方案{index}：¥{flight.get('price', 0):,.0f} 但{reason}"


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
    durations = [f["total_duration_min"] for f in usable_flights]
    sorted_prices = sorted(prices)
    sorted_durations = sorted(durations)
    lower_index = min(len(sorted_prices) - 1, len(sorted_prices) // 3)

    # 价格
    if flight["price"] == min(prices):
        pros.append("所有方案中价格最低")
    elif flight["price"] <= sorted_prices[lower_index]:
        pros.append("价格较低")
    else:
        diff = flight["price"] - min(prices)
        cons.append(f"比最低价贵¥{diff:,.0f}")

    # 时长
    if flight["total_duration_min"] == min(durations):
        pros.append("耗时最短")
    elif flight["total_duration_min"] <= sorted_durations[lower_index]:
        pros.append("耗时较短")
    else:
        diff_h = (flight["total_duration_min"] - min(durations)) // 60
        cons.append(f"比最快方案慢{diff_h}小时")

    # 转机
    if flight["stops"] == 0:
        pros.append("直飞，无需转机")
    elif flight["stops"] == 1:
        for lay in flight.get("layovers", []):
            wait = lay.get("wait_minutes", 0)
            if wait < 180:
                pros.append("转机等待时间短，紧凑高效")
            elif wait > 480:
                cons.append(f"转机等待{wait // 60}小时，可能需过夜")
    elif flight["stops"] >= 2:
        cons.append(f"需转机{flight['stops']}次")

    # 退改
    extra = flight.get("extra", {})
    if extra.get("refundable") and extra.get("changeable"):
        pros.append("可退可改，灵活度高")
    elif not extra.get("refundable"):
        cons.append("不可退票")

    # 单一航司
    airlines = set()
    for seg in flight.get("segments", []):
        airline = seg.get("airline", "")
        if airline:
            airlines.add(airline)
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
        for segment in flight.get("segments") or []
        if segment.get("flight_no")
    ]
    if numbers:
        return " → ".join(numbers)
    combo = flight.get("flight_combo") or ""
    return combo.replace("+", " → ") if combo else "请查询航司官网"


def _compact_aircrafts(flight: dict) -> str:
    aircrafts = [
        segment.get("aircraft", "")
        for segment in flight.get("segments") or []
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


def _compact_source_label(flight: dict) -> str:
    data_source = flight.get("data_source") or flight.get("source") or ""
    labels = []
    for source in str(data_source).split("+"):
        if source == "serpapi":
            labels.append("SerpAPI")
        elif source == "searchapi":
            labels.append("SearchAPI")
        elif source == "hasdata":
            labels.append("HasData")
        elif source == "duffel":
            labels.append("Duffel")
        elif source:
            labels.append(source)
    if flight.get("has_baggage_info") and "Duffel" not in labels:
        labels.append("Duffel")
    return " + ".join(dict.fromkeys(labels)) or "Google Flights"


def _compact_booking_links(flight: dict, route_info: dict) -> list[tuple[str, str]]:
    segments = flight.get("segments") or []
    first_segment = segments[0] if segments else {}
    last_segment = segments[-1] if segments else {}
    origin = (
        first_segment.get("dep_airport")
        or first_segment.get("departure_airport")
        or route_info.get("origin", "")
    )
    dest = (
        last_segment.get("arr_airport")
        or last_segment.get("arrival_airport")
        or route_info.get("destination", "")
    )
    depart_date = _flight_search_date(flight, route_info.get("depart_date", ""))
    return _possible_booking_links(
        origin,
        dest,
        depart_date,
        flight.get("flight_combo"),
        flight.get("cabin_class") or "economy",
    )


def _channel_key(name: str, url: str = "") -> str:
    text = f"{name} {url}".lower()
    if "google" in text:
        return "google_flights"
    if "ctrip" in text or "携程" in name:
        return "ctrip"
    if "fliggy" in text or "飞猪" in name:
        return "fliggy"
    if "trip.com" in text:
        return "trip_com"
    if "aa.com" in text or "aircanada" in text or "united.com" in text or "delta.com" in text or "官网" in name:
        return "airline_official"
    return "overseas_ota"


def _channel_summary(links: list[tuple[str, str]]) -> str:
    parts = []
    for name, url in links:
        channel = CHANNEL_INFO.get(_channel_key(name, url), CHANNEL_INFO["overseas_ota"])
        label = channel.get("label", "")
        if "Google" in name and channel.get("type"):
            label = "🟢 聚合比价"
        parts.append(f"{name} {label}".strip())
    return " | ".join(parts)


def _transfer_risk_lines(flight: dict) -> list[str]:
    risk = flight.get("transfer_risk") or {}
    if not risk or risk.get("level") in {"none", "无"}:
        return []
    lines = [f"中转风险：{risk.get('label', '中转风险待确认')}"]
    for factor in (risk.get("factors") or risk.get("notes") or [])[:3]:
        lines.append(f"- {factor}")
    if (risk.get("factors") or risk.get("notes")):
        lines.append("💡 建议确认是否为联程票，非联程需自行转机和重新托运行李")
    return lines


def _availability_text(flight: dict) -> str:
    availability = flight.get("availability") or {}
    label = availability.get("label", "❓ 未验证")
    source_count = availability.get("source_count", 0)
    age = availability.get("age_minutes")
    age_text = "采集时间未知" if age is None or age >= 9999 else f"{age}分钟前采集"
    source_text = f"{source_count}个数据源验证" if source_count else "无数据源验证"
    return f"{label}（{source_text}，{age_text}）"


def _target_for_flight(route_info: dict | None, analysis_result: dict | None = None) -> float | None:
    route_info = route_info or {}
    analysis_result = analysis_result or {}
    return (
        _to_float(analysis_result.get("target_price_effective"))
        or _to_float(analysis_result.get("target_price"))
        or _to_float(_preference_value(route_info, analysis_result, "target_price"))
    )


def _status_price_label(flight: dict, route_info: dict | None = None, analysis_result: dict | None = None) -> str:
    price = _to_float(flight.get("price"))
    target = _target_for_flight(route_info, analysis_result)
    if price is None or not target:
        return "价格待判断"
    if price <= target:
        return "价格偏低"
    if price <= target * 1.05:
        return "接近理想"
    if price <= target * 1.25:
        return "价格中等"
    return "价格偏高"


def _status_availability_label(flight: dict) -> str:
    status = (flight.get("availability") or {}).get("status")
    if status == "likely_available":
        return "可购买"
    if status == "needs_refresh":
        return "需刷新"
    return "可买性待确认"


def _status_confidence_label(
    flight: dict, route_info: dict | None = None, analysis_result: dict | None = None
) -> str:
    analysis_result = analysis_result or {}
    confidence = flight.get("confidence_breakdown")
    if not confidence:
        confidence = calc_confidence(
            flight,
            analysis_result.get("source_stats") or {},
            (analysis_result.get("price_insights") or {}).get("price_history"),
        )
    return f"置信度{confidence.get('overall', '中')}"


def _status_risk_label(flight: dict) -> str:
    risk = flight.get("execution_risk") or {}
    transfer = flight.get("transfer_risk") or {}
    levels = [risk.get("level"), transfer.get("level")]
    if "high" in levels or "red" in levels:
        return "风险高"
    if "medium" in levels or "yellow" in levels:
        return "风险中"
    return "风险低"


def _flight_status_tags(
    flight: dict, route_info: dict | None = None, analysis_result: dict | None = None
) -> str:
    tags = [
        _status_price_label(flight, route_info, analysis_result),
        _status_availability_label(flight),
        _status_confidence_label(flight, route_info, analysis_result),
        _status_risk_label(flight),
    ]
    return " | ".join(tags)


def _human_recommendation_text(
    flight: dict, route_info: dict | None = None, analysis_result: dict | None = None
) -> str:
    price = _to_float(flight.get("price"))
    target = _target_for_flight(route_info, analysis_result)
    grade = flight.get("execution_grade") or "C"
    if target and price and price <= target and grade == "A":
        return "强烈建议购买 - 价格达标且信息较完整"
    if target and price and price <= target * 1.05:
        return "值得验证 - 价格达标，但购买链路尚未完全确认"
    if grade in {"A", "B"}:
        return "可以观察 - 方案可参考，等待更明确价格信号"
    if grade == "D":
        return "不建议购买 - 执行风险或约束冲突较高"
    return "建议等待 - 当前价格或票规仍需确认"


def _fare_verification_lines(flight: dict) -> list[str]:
    fare = flight.get("fare_verification") or {}
    if not fare:
        return []
    lines = [f"📋 票规校验：{fare.get('label', '票规待确认')}"]
    lines.extend(fare.get("matches") or [])
    lines.extend(fare.get("issues") or [])
    return lines


def _execution_assessment_lines(flight: dict) -> list[str]:
    fare = flight.get("fare_verification") or {}
    transfer = flight.get("transfer_risk") or {}
    risk = flight.get("execution_risk") or {}
    grade = flight.get("execution_grade")
    grade_label = flight.get("execution_label") or ""
    transfer_label = transfer.get("label", "无（直飞）")
    if transfer.get("level") in {"none", "无"}:
        transfer_label = "无（直飞）"
    lines = [
        "执行评估：",
        f"├ 可购买性：{_availability_text(flight)}",
        f"├ 票规匹配：{fare.get('label', '票规待确认')}",
        f"├ 中转风险：{transfer_label}",
        f"├ 执行风险：{risk.get('label', '执行风险待确认')}",
        f"└ 综合等级：{grade or '-'}级 - {grade_label}",
    ]
    return lines


def generate_context(flight, all_flights):
    """生成方案的取舍说明和适合人群"""
    price = float(flight.get("price")) if _has_valid_price(flight.get("price")) else 0
    hours = flight.get("total_hours", 0)
    stops = flight.get("stops", 0)
    prices = [
        float(f.get("price"))
        for f in all_flights
        if _has_valid_price(f.get("price"))
    ]
    min_price = min(prices) if prices else 0

    tradeoffs = []
    if price == min_price:
        tradeoffs.append("价格最低")
    elif price <= min_price * 1.1:
        tradeoffs.append("价格接近最低")
    else:
        tradeoffs.append(f"比最低价贵¥{price - min_price:,.0f}")

    if hours <= 20:
        tradeoffs.append("耗时较短")
    elif hours >= 28:
        tradeoffs.append("耗时较长")

    if stops == 0:
        tradeoffs.append("直飞省心")
    elif stops == 1:
        for lay in flight.get("layovers", []):
            wait = lay.get("wait_minutes", 0)
            if wait > 480:
                tradeoffs.append("转机需过夜")
            elif wait < 90:
                tradeoffs.append("转机时间紧")
            else:
                tradeoffs.append("转机时间适中")

    extra = flight.get("extra", {})
    refundable = bool(extra.get("refundable") or (extra.get("refund_change") or {}).get("refundable"))
    changeable = bool(extra.get("changeable") or (extra.get("refund_change") or {}).get("changeable"))
    baggage_detail = extra.get("baggage_detail") or {}
    checked_baggage = baggage_detail.get("checked") or {}
    has_free_checked_baggage = checked_baggage.get("is_free") or checked_baggage.get("quantity", 0) > 0
    segments = flight.get("segments") or []
    dep_time = _time_only(segments[0].get("dep_time")) if segments else ""
    dep_hour = None
    if dep_time and ":" in dep_time:
        try:
            dep_hour = int(dep_time.split(":", 1)[0])
        except ValueError:
            dep_hour = None
    airlines = {
        segment.get("airline", "")
        for segment in segments
        if segment.get("airline")
    }

    if refundable:
        tradeoffs.append("可退票")
    if dep_hour is not None and (dep_hour >= 22 or dep_hour < 6):
        tradeoffs.append("红眼航班")

    tradeoff_text = " · ".join(tradeoffs)

    suitable = []
    not_suitable = []

    if price == min_price:
        suitable.append("预算有限")
    if hours <= 20 and stops <= 1:
        suitable.append("时间紧凑的出行")
    if refundable and changeable:
        suitable.append("行程未确定")
    if stops == 0 and hours < 20:
        suitable.append("带老人小孩")
        suitable.append("第一次出国")
    if has_free_checked_baggage:
        suitable.append("行李较多")
    if dep_hour is not None and 8 <= dep_hour <= 14:
        suitable.append("不想赶早班")

    if hours >= 28:
        not_suitable.append("体力较差")
    if stops >= 2:
        not_suitable.append("带小孩出行")
    for lay in flight.get("layovers", []):
        if lay.get("wait_minutes", 0) > 480:
            not_suitable.append("不想在机场过夜")
        if lay.get("wait_minutes", 0) > 600:
            not_suitable.append("体力有限")
            not_suitable.append("带小孩")
    if not (refundable and changeable):
        not_suitable.append("行程可能变动")
    if not has_free_checked_baggage:
        not_suitable.append("行李较多")
    if len(airlines) > 1:
        not_suitable.append("第一次出国（行李可能不直挂）")

    suit_text = "、".join(dict.fromkeys(suitable).keys()) if suitable else "一般出行"
    not_suit_text = "、".join(dict.fromkeys(not_suitable).keys()) if not_suitable else ""

    return tradeoff_text, suit_text, not_suit_text


def _append_compact_flight(
    lines: list[str],
    flight: dict,
    label: str,
    route_info: dict,
    all_flights: list[dict],
    analysis_result: dict | None = None,
) -> None:
    segments = flight.get("segments") or []
    airline_text = " / ".join(_airlines_from_segments(segments))
    if not airline_text:
        airline_text = flight.get("airline_summary") or " / ".join(flight.get("airlines") or [])
    stops = int(flight.get("stops") or max(len(segments) - 1, 0))

    lines.append(f"<b>{label}</b>")
    lines.append(f"💵 {_flight_price_text(flight)}")
    lines.append(f"🏷 {_flight_status_tags(flight, route_info, analysis_result)}")
    lines.append(_human_recommendation_text(flight, route_info, analysis_result))
    lines.extend(_price_estimate_summary_lines(flight))
    lines.append(f"🏢 {airline_text or '请查询航司官网'}")
    if (flight.get("cabin_class") or "economy") == "business":
        lines.append(f"💺 {_cabin_label('business')}")
    lines.append(f"✈️ {_display_route_summary(flight.get('route_summary', ''))}")
    lines.append(f"🕐 {_arrival_time_text(flight)}")
    lines.append(f"🕘 时段：{_flight_slot_label(flight)}")
    lines.append(f"⏱️ 全程：{flight.get('total_hours', '')}小时 · 转机{stops}次" if stops else f"⏱️ 全程：{flight.get('total_hours', '')}小时 · 直飞")
    lines.append(f"✈ {_compact_flight_numbers(flight)}")
    lines.append(f"　机型：{_compact_aircrafts(flight)}")
    lines.append(f"　转机：{_compact_layover(flight)}")
    lines.append("")
    lines.append(_compact_cabin_rule_line(flight))
    lines.append(_compact_baggage_line(flight))
    lines.append(_compact_refund_line(flight))
    lines.append(f"📎 数据来源：{_compact_source_label(flight)}")
    discrepancy_notice = _price_discrepancy_notice(flight)
    if discrepancy_notice:
        lines.append(discrepancy_notice)
    preference_notes = (flight.get("preference_notes") or []) + (
        flight.get("preference_penalties") or []
    )
    if preference_notes:
        lines.append(f"⚙️ 偏好匹配：{'；'.join(dict.fromkeys(preference_notes))}")
    lines.append(f"价格采集于 {_collected_time_text(flight)} | 新鲜度：{_freshness_label(flight)}")
    lines.append("")
    companions = _preference_value(route_info, {}, "companions", "solo")
    if companions in {"with_elderly", "with_child", "with_elderly_child"}:
        family_notes = [note for note in preference_notes if "家庭" in str(note)]
        if family_notes or stops == 0:
            lines.append(f"👪 适合家庭出行：{_companions_label(companions)}")
    sensitivity = _preference_value(route_info, {}, "price_sensitivity", "low")
    sensitivity_note = _price_sensitivity_flight_note(flight, all_flights, sensitivity)
    if sensitivity_note:
        lines.append(f"💰 {sensitivity_note}")
    lines.append("")
    tradeoff_text, suit_text, not_suit_text = generate_context(flight, all_flights)
    lines.append(f"📋 {tradeoff_text}")
    lines.append(f"👤 适合：{suit_text}")
    if not_suit_text:
        lines.append(f"⚠️ 不太适合：{not_suit_text}")
    if _status_risk_label(flight) != "风险低":
        lines.append("⚠️ 详细风险请在支付页确认票规、中转和行李规则")
    lines.append("")
    booking_links = _compact_booking_links(flight, route_info)
    if booking_links:
        link_text = " | ".join(
            f'<a href="{url}" target="_blank">{name}</a>' for name, url in booking_links
        )
        lines.append(f"🔗 购买: {link_text}")
        lines.append(_booking_options_hint(flight))
        lines.append("💡 建议优先通过携程或飞猪购买，售后保障更好")
    lines.append("")


def _cabin_price_range_text(
    flights: list[dict], cabin_class: str, analysis_result: dict | None = None
) -> str:
    if analysis_result:
        ranges = analysis_result.get("cabin_price_ranges") or {}
        price_range = ranges.get(cabin_class)
        if price_range and len(price_range) >= 2:
            low, high = price_range[0], price_range[1]
            if _has_valid_price(low) and _has_valid_price(high):
                return f"{_price_text(low)} - {_price_text(high)}"

    prices = [
        flight.get("price")
        for flight in flights
        if (flight.get("cabin_class") or "economy") == cabin_class
        and _has_valid_price(flight.get("price"))
    ]
    if not prices:
        return "暂无有效价格数据"
    return f"{_price_text(min(prices))} - {_price_text(max(prices))}"


def _compact_source_summary_lines(source_stats: dict | None) -> list[str]:
    if not source_stats:
        return []
    status_line = _compact_source_status_line(source_stats)
    return ["—— 数据源统计 ——", status_line] if status_line else []


def _source_status_parts(source_stats: dict | None) -> list[str]:
    if not source_stats:
        return []

    source_names = {
        "serpapi": "SerpAPI",
        "searchapi": "SearchAPI",
        "hasdata": "HasData",
        "travelpayouts": "Travelpayouts",
        "skyscanner": "Skyscanner",
        "duffel": "Duffel",
    }
    parts = []
    for key, info in source_stats.items():
        if key in {"total_raw", "after_dedup", "after_dedup_by_cabin", "enriched_count"}:
            continue
        if not isinstance(info, dict):
            continue
        name = source_names.get(key, key)
        status = str(info.get("status", ""))
        ok = "成功" in status or status.lower() == "success"
        mark = "✅" if ok else "❌"
        count = info.get("count")
        if count is None:
            count = sum((info.get("cabin_counts") or {}).values())
        parts.append(f"{name} {mark} {int(count or 0)}个")
    return parts


def _compact_source_status_line(source_stats: dict | None) -> str:
    parts = _source_status_parts(source_stats)
    if not parts:
        return ""
    return " | ".join(parts) + f" → 去重后{(source_stats or {}).get('after_dedup', 0)}个方案"


def _valid_option_count(analysis: dict | None) -> int:
    analysis = analysis or {}
    try:
        total = int(analysis.get("total_options"))
    except (TypeError, ValueError):
        total = None
    if total is not None:
        return total
    flights = analysis.get("all_flights") or []
    return sum(1 for flight in flights if _has_valid_price(flight.get("price")))


def _append_low_option_count_notice(
    lines: list[str],
    analysis: dict | None,
    label: str = "",
) -> None:
    count = _valid_option_count(analysis)
    if not (0 < count < 5):
        return
    prefix = f"{label}" if label else ""
    lines.append(
        f"ℹ️ {prefix}当前仅搜到{count}个方案，可能因为航司尚未发布完整排班。"
    )
    lines.append("随着出发日期临近，航班选择会增多，系统将持续监控。")


def _append_purchase_checklist(
    lines: list[str], route_info: dict, analysis_result: dict
) -> None:
    lines.append("📋 购买前请确认：")
    checklist = [
        "□ 支付页最终价格是否在可接受范围内",
        "□ 是否含税费和燃油费",
        "□ 是否含托运行李（如需要）",
        "□ 退改签规则是否可接受",
        "□ 中转是否为联程票（如有中转）",
        "□ 出发和到达时间是否正确",
    ]
    companions = _preference_value(route_info, analysis_result, "companions", "solo")
    if companions in {"with_elderly", "with_child", "with_elderly_child"}:
        checklist.extend(
            [
                "□ 是否避免红眼和凌晨到达",
                "□ 中转时间是否充裕（建议≥2小时）",
            ]
        )
    lines.extend(checklist)


def _append_price_explanation_lines(lines: list[str]) -> None:
    lines.append("💡 关于价格说明：")
    lines.append("- 展示价：搜索引擎显示的票面价格（通常含税含燃油）")
    lines.append("- 预估交易价：根据航司类型估算的实际支付价格（含行李/选座等）")
    lines.append("- 全服务航司（国航、东航、南航、全日空等）通常含免费托运行李")
    lines.append("- 廉航（春秋、乐桃等）行李和选座通常需要额外购买")
    lines.append("- 最终价格以购买平台支付页为准")


def _successful_source_count(source_stats: dict | None) -> int:
    if not source_stats:
        return 0
    count = 0
    for key, info in source_stats.items():
        if key in {"total_raw", "after_dedup", "after_dedup_by_cabin", "enriched_count"}:
            continue
        if not isinstance(info, dict):
            continue
        status = str(info.get("status", ""))
        if "成功" in status or status.lower() == "success":
            count += 1
    return count


def _evidence_text(price_pos: dict | None, source_stats: dict | None) -> str:
    parts = []
    data_points = (price_pos or {}).get("data_points")
    if data_points:
        parts.append(f"基于{data_points}次采集")
    source_count = _successful_source_count(source_stats)
    if source_count:
        parts.append(f"{source_count}个数据源交叉验证")
    return f"（{'、'.join(parts)}）" if parts else ""


def _history_prices(price_history) -> list[float]:
    prices = []
    for item in price_history or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            price = item[1]
        else:
            price = item
        value = _to_float(price)
        if value and value > 0:
            prices.append(value)
    return prices


def _trend_arrow_line(price_history, width: int = 4) -> str:
    prices = _history_prices(price_history)
    if len(prices) < 2:
        return ""
    recent = prices[-width:]
    if all(recent[i] > recent[i + 1] for i in range(len(recent) - 1)):
        prefix = "📉"
    elif all(recent[i] < recent[i + 1] for i in range(len(recent) - 1)):
        prefix = "📈"
    else:
        prefix = "📊"
    values = " → ".join(f"¥{price:,.0f}" for price in recent)
    return f"{prefix} {values}"


def _append_price_drop_alert(
    lines: list[str], analysis_result: dict, route_info: dict
) -> None:
    previous_prices = route_info.get("previous_prices") or {}
    flights = analysis_result.get("all_flights") or []
    if not flights:
        return

    current_prices = [
        float(flight.get("price"))
        for flight in flights
        if _has_valid_price(flight.get("price"))
    ]
    if not current_prices:
        return
    current_min = min(current_prices)
    lines.append("<b>🔔 跌价提醒</b>")
    if not previous_prices:
        lines.append(f"首次记录当前最低价：{_price_text(current_min)}")
        lines.append("")
        return

    previous_values = [
        float(price) for price in previous_prices.values() if _has_valid_price(price)
    ]
    if not previous_values:
        lines.append(f"首次记录当前最低价：{_price_text(current_min)}")
        lines.append("")
        return

    previous_min = min(previous_values)
    diff = current_min - previous_min
    if diff < 0:
        lines.append(f"✅ 当前最低价比上次便宜¥{abs(diff):,.0f}")
    elif diff > 0:
        lines.append(f"当前最低价比上次贵¥{diff:,.0f}")
    else:
        lines.append("当前最低价与上次持平")
    lines.append("")


def _append_nearby_dates(lines: list[str], nearby_dates: list[dict] | None) -> None:
    if not nearby_dates:
        return

    valid_prices = [
        float(item.get("min_price"))
        for item in nearby_dates
        if _has_valid_price(item.get("min_price"))
    ]
    cheapest = min(valid_prices) if valid_prices else None
    selected = next((item for item in nearby_dates if item.get("selected")), None)
    selected_price = float(selected.get("min_price")) if selected and _has_valid_price(selected.get("min_price")) else None
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    lines.append("<b>📅 前后日期价格对比</b>")
    for item in sorted(nearby_dates, key=lambda value: value.get("date", "")):
        price = item.get("min_price")
        price_text = _price_text(price)
        date_text = item.get("date", "")
        try:
            day = date.fromisoformat(date_text)
            label = f"{day.month}/{day.day}（{weekday_names[day.weekday()]}）"
        except ValueError:
            label = date_text
        markers = []
        if cheapest is not None and price == cheapest:
            markers.append("← 最便宜")
        if item.get("selected"):
            markers.append("← 你选的日期")
        suffix = " ".join(markers)
        lines.append(f"{label}: {price_text} {suffix}".rstrip())

    if cheapest is not None and selected_price is not None and cheapest < selected_price:
        cheaper_item = next(
            item for item in nearby_dates if item.get("min_price") == cheapest
        )
        offset = cheaper_item.get("offset", 0)
        if offset < 0:
            day_text = f"提前{abs(offset)}天"
        elif offset > 0:
            day_text = f"推后{offset}天"
        else:
            day_text = "你选的日期"
        lines.append("")
        lines.append(
            f"💡 建议：{day_text}出发可以便宜¥{selected_price - cheapest:,.0f}"
        )
    lines.append("")


def _append_best_overall_summary(
    lines: list[str], analysis_result: dict, route_info: dict
) -> None:
    flights = analysis_result.get("all_flights") or []
    if not flights:
        return
    ranked = sorted(
        [flight for flight in flights if _has_valid_price(flight.get("price"))],
        key=lambda flight: (
            -(flight.get("preference_score") or (flight.get("scores") or {}).get("total", 0)),
            float(flight.get("price") or 999999),
        ),
    )[:3]
    if not ranked:
        return

    lines.append("<b>🏆 综合评分Top3方案</b>")
    for index, flight in enumerate(ranked, start=1):
        score = flight.get("preference_score") or (flight.get("scores") or {}).get("total")
        score_text = f" | 匹配度{score}/10" if score is not None else ""
        lines.append(
            f"{index}. {_price_text(flight.get('price'))} | "
            f"{_display_route_summary(flight.get('route_summary', ''))} | "
            f"{flight.get('total_hours', '')}小时{score_text}"
        )
    lines.append("")


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _z_score_description(z_score) -> str | None:
    z_value = _to_float(z_score)
    if z_value is None:
        return None
    if z_value < -2:
        return "🔥 价格远低于平均水平，可能是捡漏机会"
    if z_value < -1:
        return "✅ 价格低于平均水平，性价比不错"
    if z_value <= 1:
        return "📊 价格处于正常范围"
    if z_value <= 2:
        return "⚠️ 价格高于平均水平"
    return "❌ 价格远高于平均水平，建议观望"


def _extract_average_price(anomaly: dict) -> float | None:
    for key in ("avg_price", "average_price", "mean_price"):
        value = _to_float(anomaly.get(key))
        if value and value > 0:
            return value

    message = str(anomaly.get("message", ""))
    match = re.search(r"(?:均值|平均(?:价格)?)¥?([0-9,]+(?:\.\d+)?)", message)
    if match:
        return _to_float(match.group(1).replace(",", ""))
    return None


def _format_average_price_gap(price, average_price) -> str | None:
    price_value = _to_float(price)
    avg_value = _to_float(average_price)
    if price_value is None or avg_value is None or avg_value <= 0:
        return None

    diff_pct = (price_value - avg_value) / avg_value * 100
    pct_text = f"{abs(diff_pct):.1f}".rstrip("0").rstrip(".")
    if abs(diff_pct) < 0.5:
        return "与平均价格基本持平"
    if diff_pct < 0:
        return f"比平均价格便宜{pct_text}%"
    return f"比平均价格贵{pct_text}%"


def _sanitize_anomaly_message(message: str) -> str:
    text = re.sub(r"，?Z-score=[+-]?\d+(?:\.\d+)?", "", message or "")
    text = re.sub(r"，?偏离均值([+-]?\d+(?:\.\d+)?)%", r"，相对平均价格变化\1%", text)
    return text.strip("， ")


def _plain_anomaly_lines(anomaly: dict) -> list[str]:
    result = []
    combo = anomaly.get("flight_combo")
    price = _to_float(anomaly.get("price"))
    if combo and price is not None:
        result.append(f"{combo} 当前¥{price:,.0f}")
    elif price is not None:
        result.append(f"当前价格¥{price:,.0f}")

    z_desc = _z_score_description(anomaly.get("z_score"))
    if z_desc:
        result.append(z_desc)

    gap = _format_average_price_gap(price, _extract_average_price(anomaly))
    if gap:
        result.append(gap)

    if not z_desc and not gap:
        message = _sanitize_anomaly_message(str(anomaly.get("message", "")))
        if message:
            result.append(message)

    return result


def _append_price_anomaly_lines(lines: list[str], anomalies: list[dict] | None) -> None:
    if not anomalies:
        return

    colors = {
        "alert": "#d93025",
        "warning": "#f59e0b",
        "info": "#1a73e8",
    }
    labels = {
        "alert": "严重",
        "warning": "注意",
        "info": "提示",
    }

    highest = min(
        anomalies,
        key=lambda item: {"alert": 0, "warning": 1, "info": 2}.get(
            item.get("severity"), 9
        ),
    )
    color = colors.get(highest.get("severity"), "#d93025")

    lines.append(f'<font color="{color}"><b>🚨 价格异常提醒</b></font>')
    lines.append("")
    for anomaly in anomalies[:6]:
        severity = anomaly.get("severity", "info")
        label = labels.get(severity, severity)
        anomaly_type = anomaly.get("type", "价格异常")
        lines.append(
            f'<font color="{colors.get(severity, "#1a73e8")}">'
            f"【{label}】{anomaly_type}</font>"
        )
        for item in _plain_anomaly_lines(anomaly):
            lines.append(f"　{item}")
    if len(anomalies) > 6:
        lines.append(f"　还有{len(anomalies) - 6}条异常未展示")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append("")


def _append_system_health_lines(lines: list[str], system_health: dict | None) -> None:
    if not system_health:
        return

    score = system_health.get("score", 0)
    level = system_health.get("level", "未知")
    emoji = system_health.get("emoji", "")
    warnings = system_health.get("warnings") or []

    lines.append("")
    lines.append(f"🩺 <b>系统健康度：{emoji} {score}/100（{level}）</b>")
    lines.append(f"　• 活跃数据源：{system_health.get('active_sources', 0)}个")
    lines.append(f"　• 去重后方案：{system_health.get('option_count', 0)}个")
    if warnings:
        for warning in warnings:
            lines.append(f"　• ⚠️ {warning}")
    else:
        lines.append("　• 未发现系统健康警告")


def _normalize_own_history_for_refs(route_info: dict) -> list[dict]:
    """Normalize stored lowest-price history for price reference calculations."""
    history = (
        route_info.get("own_history")
        or route_info.get("lowest_price_history")
        or route_info.get("price_history")
        or []
    )
    normalized = []
    for record in history:
        if isinstance(record, dict):
            normalized.append(record)
        elif isinstance(record, (list, tuple)) and len(record) >= 2:
            normalized.append({"snapshot_time": record[0], "price": record[1]})
    return normalized


def _format_ref_percent(value) -> str:
    try:
        return f"{float(value):g}%"
    except (TypeError, ValueError):
        return "0%"


def _format_reference_diff(ref: dict) -> str:
    diff = ref.get("diff", 0) or 0
    pct = ref.get("diff_pct", 0) or 0
    if diff > 0:
        return f"↑ 当前比它贵¥{diff:,.0f}（高{_format_ref_percent(pct)}）"
    if diff < 0:
        return f"↓ 当前比它低¥{abs(diff):,.0f}（低{_format_ref_percent(abs(pct))}）"
    return "= 当前与它持平"


def _reference_price(ref: dict | None) -> float | None:
    if not ref:
        return None
    try:
        return float(ref.get("price"))
    except (TypeError, ValueError):
        return None


def _price_gap(current_price: float, reference_price: float) -> tuple[float, float]:
    diff = current_price - reference_price
    pct = round(diff / reference_price * 100, 1) if reference_price > 0 else 0
    return diff, pct


def _format_absolute_min_line(current_price: float, ref: dict) -> str:
    ref_price = _reference_price(ref)
    if ref_price is None:
        return ""

    diff, pct = _price_gap(current_price, ref_price)
    if abs(diff) < 1:
        return "🔥 当前即为历史最低价，值得关注"
    if diff < 0:
        return f"🔥 突破历史最低！比历史最低还便宜¥{abs(diff):,.0f}"
    return (
        f"比历史最低贵¥{diff:,.0f}（贵{_format_ref_percent(pct)}），"
        "历史最低可能出现在淡季或特殊促销"
    )


def _format_conditional_min_line(current_price: float, ref: dict) -> str:
    ref_price = _reference_price(ref)
    if ref_price is None:
        return ""

    diff, _ = _price_gap(current_price, ref_price)
    if abs(diff) < 1:
        return "当前处于同条件最低水平"
    if diff < 0:
        return f"✅ 创同条件新低！比同条件历史最低还便宜¥{abs(diff):,.0f}"
    return f"比同条件最低贵¥{diff:,.0f}，仍有降价空间"


def _format_generic_reference_line(current_price: float, ref: dict) -> str:
    ref_price = _reference_price(ref)
    if ref_price is None:
        return ""
    diff, pct = _price_gap(current_price, ref_price)
    if abs(diff) < 1:
        return "当前与该参考价持平"
    if diff < 0:
        return f"比该参考价便宜¥{abs(diff):,.0f}（低{_format_ref_percent(abs(pct))}）"
    return f"比该参考价贵¥{diff:,.0f}（贵{_format_ref_percent(pct)}）"


def _format_reference_line(key: str, ref: dict, current_price: float) -> str:
    if key == "absolute_min":
        return _format_absolute_min_line(current_price, ref)
    if key == "conditional_min":
        return _format_conditional_min_line(current_price, ref)
    return _format_generic_reference_line(current_price, ref)


def _format_purchase_advice(
    references: dict, current_price: float, evidence: str = ""
) -> str | None:
    absolute_price = _reference_price(references.get("absolute_min"))
    conditional_price = _reference_price(references.get("conditional_min"))

    if absolute_price is not None and conditional_price is not None:
        if current_price <= absolute_price and current_price <= conditional_price:
            return f"⭐ 综合建议：当前是近期好价，建议尽快入手{evidence}"

    if conditional_price is None or conditional_price <= 0:
        return None

    diff, pct = _price_gap(current_price, conditional_price)
    if diff > 0 and pct < 5:
        return f"📊 综合建议：价格接近低点，可以考虑入手{evidence}"
    if diff > 0 and pct > 10:
        return f"⏳ 综合建议：价格偏高，建议继续观望{evidence}"
    if diff <= 0:
        return f"⭐ 综合建议：当前是近期好价，建议尽快入手{evidence}"
    return f"📊 综合建议：价格略高于低点，可结合预算和行程确定性判断{evidence}"


def _percentile_position_text(percentile) -> str:
    pct = _to_float(percentile)
    if pct is None:
        return "历史数据还不够多，暂时不好判断高低"
    if pct <= 0:
        return "处于最低水平"
    if pct <= 10:
        return "处于极低水平（比90%的历史价格都便宜）"
    if pct <= 25:
        return "处于较低水平（比75%的历史价格都便宜）"
    if pct <= 50:
        return "处于中等偏低水平"
    if pct <= 75:
        return "处于中等偏高水平"
    if pct <= 90:
        return "处于较高水平（比75%的历史价格都贵）"
    return "处于极高水平，建议观望"


def _format_average_amount_diff(diff) -> str:
    value = _to_float(diff) or 0
    if abs(value) < 1:
        return "　与平均价格基本持平"
    if value < 0:
        return f"　比平均价格便宜¥{abs(value):,.0f}"
    return f"　比平均价格贵¥{value:,.0f}"


def _format_min_amount_diff(diff) -> str:
    value = _to_float(diff) or 0
    if abs(value) < 1:
        return "　已经是最低价"
    if value < 0:
        return f"　比此前最低价还便宜¥{abs(value):,.0f}"
    return f"　比最低价贵¥{value:,.0f}"


def _append_price_references(
    lines: list[str], references: dict | None, current_min: float, evidence: str = ""
) -> None:
    if not references or not current_min:
        return

    display_order = ["absolute_min", "conditional_min", "recent_min"]
    available = [
        (key, references[key])
        for key in display_order
        if isinstance(references.get(key), dict)
        and references[key].get("price") is not None
    ]
    if not available:
        return

    lines.append("<b>📊 价格参考</b>")
    lines.append("")
    lines.append(f"当前最低价：¥{current_min:,.0f}")
    lines.append("")

    for key, ref in available:
        lines.append(f"{ref.get('label', '参考价格')}：¥{ref['price']:,.0f}")
        detail = _format_reference_line(key, ref, current_min)
        if detail:
            lines.append(f"　　{detail}{evidence}")
        sample_size = ref.get("sample_size")
        if key == "recent_min" and sample_size:
            lines.append(f"　　ℹ️ 基于过去两周的{sample_size}次采集")
        elif sample_size:
            lines.append(f"　　ℹ️ {ref.get('note', '')}，基于{sample_size}个数据点")
        elif ref.get("note") and key not in {"absolute_min", "conditional_min"}:
            lines.append(f"　　ℹ️ {ref['note']}")
        lines.append("")

    advice = _format_purchase_advice(references, current_min, evidence)
    if advice:
        lines.append(advice)
        lines.append("")


def _append_multi_window_analysis(lines: list[str], windows: dict | None) -> None:
    if not windows:
        return

    sections = []

    short_term = windows.get("short_term")
    if isinstance(short_term, dict):
        sections.extend(
            [
                f"短期（近7天）：{short_term.get('trend')}（{short_term.get('change_pct')}%）",
                f"　最高¥{short_term.get('high', 0):,.0f} → 最低¥{short_term.get('low', 0):,.0f}",
                "",
            ]
        )

    mid_term = windows.get("mid_term")
    if isinstance(mid_term, dict):
        vs_avg = mid_term.get("vs_avg", 0) or 0
        vs_min = mid_term.get("vs_min", 0) or 0
        sections.extend(
            [
                f"中期（你关注以来）：{_percentile_position_text(mid_term.get('percentile'))}",
                _format_average_amount_diff(vs_avg),
                _format_min_amount_diff(vs_min),
                "",
            ]
        )

    long_term = windows.get("long_term")
    if isinstance(long_term, dict):
        percentile = long_term.get("percentile", 0) or 0
        sections.extend(
            [
                f"长期（近60天）：{_percentile_position_text(percentile)}",
                f"　历史最低¥{long_term.get('min', 0):,.0f} → 最高¥{long_term.get('max', 0):,.0f}",
                "",
            ]
        )

    if not sections:
        return

    lines.append("📊 <b>价格分析</b>")
    lines.append("")
    lines.extend(sections)


def _round_trip_city_code(code: str | None) -> str:
    return _city_label(code)


def _round_trip_date_text(date_text: str | None) -> str:
    try:
        parsed = date.fromisoformat(str(date_text or ""))
    except ValueError:
        return str(date_text or "")
    return f"{parsed.month}月{parsed.day}日"


def _round_trip_airline_text(flight: dict) -> str:
    segments = flight.get("segments") or []
    airlines = _airlines_from_segments(segments)
    if airlines:
        return " / ".join(airlines)
    if flight.get("airline_summary"):
        return str(flight.get("airline_summary"))
    airlines = [str(item) for item in flight.get("airlines") or [] if item]
    return " / ".join(airlines) if airlines else "航司待确认"


def _round_trip_duration_text(flight: dict) -> str:
    minutes = flight.get("total_duration_min")
    if minutes:
        minutes = int(minutes)
        return f"{minutes // 60}h{minutes % 60}m"
    try:
        minutes = round(float(flight.get("total_hours") or 0) * 60)
    except (TypeError, ValueError):
        minutes = 0
    if minutes <= 0:
        return "时长待确认"
    return f"{minutes // 60}h{minutes % 60}m"


def _round_trip_stops_text(flight: dict) -> str:
    segments = flight.get("segments") or []
    try:
        stops = int(flight.get("stops") if flight.get("stops") is not None else max(len(segments) - 1, 0))
    except (TypeError, ValueError):
        stops = max(len(segments) - 1, 0)
    return "直飞" if stops <= 0 else f"中转{stops}次"


def _round_trip_time_range(flight: dict) -> str:
    segments = flight.get("segments") or []
    if not segments:
        return "时间待确认"
    dep_time = _time_only(segments[0].get("dep_time"))
    arr_time = _time_only(segments[-1].get("arr_time"))
    if dep_time and arr_time:
        return f"{dep_time}→{arr_time}"
    return dep_time or arr_time or "时间待确认"


def _month_day_time(value: str | None, fallback_date: str | None = None) -> str:
    text = str(value or "").strip()
    date_part = ""
    time_part = ""
    if text:
        normalized = text.replace("T", " ")
        if " " in normalized:
            date_part, time_part = normalized.split(" ", 1)
        else:
            time_part = _time_only(normalized)
    if not date_part:
        date_part = str(fallback_date or "").strip()

    display_date = ""
    try:
        parsed = date.fromisoformat(date_part[:10])
        display_date = f"{parsed.month}月{parsed.day}日"
    except ValueError:
        display_date = date_part[:10]

    time_text = _time_only(time_part) or _time_only(text) or "时间待确认"
    return f"{display_date} {time_text}".strip()


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
        return "🔴建议刷新"
    now = datetime.now(collected_at.tzinfo) if collected_at.tzinfo else datetime.now()
    minutes = max(0, (now - collected_at).total_seconds() / 60)
    if minutes <= 30:
        return "🟢新鲜"
    if minutes <= 120:
        return "🟡需确认"
    return "🔴建议刷新"


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
        lines.extend(
            [
                "✅ 操作建议：",
                f"若支付页最终价不超过{final_limit_text}，{baggage_clause}，建议购买。",
                f"若最终价超过{target_price_text}，建议继续监控。",
            ]
        )
    elif grade == "B":
        lines.extend(
            [
                "🔶 操作建议：",
                "点击链接确认最终价格和票规后再购买。",
                "注意确认是否含托运行李、是否联程票。",
            ]
        )
    elif grade == "C":
        lines.extend(
            [
                "⚠️ 仅供参考：",
                "该价格仅用于判断市场区间，当前可购买性未验证。",
            ]
        )
    else:
        lines.extend(
            [
                "❌ 其他参考：",
                "当前可执行性较低，不作为主购买方案。",
            ]
        )
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
    return str(aircraft).strip() or "未知"


def _round_trip_score_text(flight: dict) -> str:
    return _human_recommendation_text(flight)


def format_flight_detail(
    flight: dict,
    date_str: str | None = None,
    label: str | None = None,
    route_info: dict | None = None,
    analysis_result: dict | None = None,
) -> str:
    """统一格式化往返方案详情，去程和返程共用。"""
    flight_no = _compact_flight_numbers(flight)
    airline = _round_trip_airline_text(flight)
    price_text = _flight_price_text(flight)
    estimate_lines = _price_estimate_summary_lines(flight)
    segments = flight.get("segments") or []
    first_segment = segments[0] if segments else {}
    last_segment = segments[-1] if segments else {}
    dep_airport = first_segment.get("dep_airport") or first_segment.get("departure_airport")
    arr_airport = last_segment.get("arr_airport") or last_segment.get("arrival_airport")
    dep_tz = get_airport_timezone(dep_airport)
    arr_tz = get_airport_timezone(arr_airport)
    show_timezone = bool(dep_airport and arr_airport and dep_tz != arr_tz)

    dep_text = _month_day_time(first_segment.get("dep_time"), date_str) if segments else (
        f"{_round_trip_date_text(date_str)} 时间待确认".strip()
    )
    dep_date_text, _, dep_time_text = dep_text.rpartition(" ")
    if dep_time_text and dep_time_text != "时间待确认":
        dep_text = " ".join(
            part
            for part in [
                dep_date_text,
                _time_with_timezone(dep_time_text, dep_airport, show_timezone),
            ]
            if part
        )
    arr_time = _time_only(last_segment.get("arr_time")) if segments else ""
    arr_text = _time_with_timezone(arr_time, arr_airport, show_timezone) if arr_time else "时间待确认"
    search_date = _flight_search_date(flight, date_str)
    booking_links = (
        generate_booking_links(
            dep_airport,
            arr_airport,
            search_date,
            flight_no,
            flight=flight,
        )
        if dep_airport and arr_airport and search_date
        else ""
    )
    prefix = f"{label}: " if label else ""
    detail = (
        f"{prefix}{flight_no} {airline} | {price_text}<br>"
        f"  🏷 {_flight_status_tags(flight, route_info, analysis_result)}<br>"
        f"  {_human_recommendation_text(flight, route_info, analysis_result)}<br>"
        f"  {city_name(dep_airport)} → {city_name(arr_airport)}<br>"
        f"  {dep_text}起飞 → {arr_text}到达 | {_round_trip_stops_text(flight)} "
        f"{_round_trip_duration_text(flight)} | {_flight_slot_label(flight)} | "
        f"机型: {_round_trip_aircraft_text(flight)}"
    )
    for estimate_line in estimate_lines:
        detail += f"<br>  {estimate_line}"
    if show_timezone:
        detail += "<br>  到达时间按当地时间计算"
    if booking_links:
        title = "购买渠道" if _verified_booking_options(flight) else "可能的购买渠道"
        detail += f"<br>  🔗 {title}: {booking_links}"
        detail += f"<br>  {_booking_options_hint(flight)}"
        detail += "<br>  💡 建议优先通过携程或飞猪购买，售后保障更好"
    discrepancy_notice = _price_discrepancy_notice(flight)
    if discrepancy_notice:
        detail += f"<br>  {discrepancy_notice}"
    detail += (
        f"<br>  价格采集于 {_collected_time_text(flight)} | "
        f"新鲜度：{_freshness_label(flight)}"
    )
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
        line += f"<br>  🔗 {links}"
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
    lines.append("<b>⭐ 综合评分Top3</b>")
    if outbound_ranked:
        lines.append("━━ 去程 ━━")
        for index, flight in enumerate(outbound_ranked, start=1):
            lines.append(_round_trip_score_line(index, flight, (route_info or {}).get("depart_date")))
    if return_ranked:
        lines.append("━━ 返程 ━━")
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
            lines.append(f"  预估交易价：{_price_text(transaction_total)}（+额外费用{_price_text(extra)}）")
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
        f"去程最低: {_price_text(previous.get('outbound_lowest'))}  "
        f"{_price_text(round_trip.get('outbound_min'))}  "
        f"{_change_text(round_trip.get('outbound_min'), previous.get('outbound_lowest'))}"
    )
    lines.append(
        f"返程最低: {_price_text(previous.get('return_lowest'))}  "
        f"{_price_text(round_trip.get('return_min'))}  "
        f"{_change_text(round_trip.get('return_min'), previous.get('return_lowest'))}"
    )
    lines.append(
        f"往返最优: {_price_text(previous.get('roundtrip_lowest'))}  "
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
    lines.append(
        f"当前往返最低总价：{_price_text(total_min)}（去{_price_text(outbound_min)} + 回{_price_text(return_min)}）"
    )
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
        lines.append(_roundtrip_reference_gap(total_min, conditional_ref.get("price"), "它"))
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
        lines.append(
            f"短期（近7天）：{short_term.get('trend', '数据积累中')}（{short_term.get('change_pct', 0)}%）"
        )
        sequence = _roundtrip_price_sequence(short_term.get("prices") or [])
        if sequence:
            lines.append(f"  往返总价：{sequence}")
        lines.append(
            f"  其中去程{_format_leg_change(short_term.get('outbound_change'))}，"
            f"返程{_format_leg_change(short_term.get('return_change'))}"
        )
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
        return "██████████"
    width = 12
    level = int((price - min_price) / (max_price - min_price) * (width - 4)) + 4
    return "█" * max(4, min(width, level))


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
    outbound_flights = round_trip.get("outbound_top3") or _round_trip_top_flights(outbound_analysis)
    return_flights = round_trip.get("return_top3") or _round_trip_top_flights(return_analysis)

    top_combinations = round_trip.get("top_combinations") or []
    max_combo = round_trip.get("max_combination")
    outbound_min = round_trip.get("outbound_min")
    return_min = round_trip.get("return_min")
    total_min = round_trip.get("total_min")

    lines.append("<b>💰 往返总价一览</b>")
    if _has_valid_price(total_min):
        lines.append(
            f"最优组合：{_price_text(total_min)}（去{_price_text(outbound_min)} + 回{_price_text(return_min)}）"
        )
    if max_combo:
        lines.append(
            f"最贵组合：{_price_text(max_combo.get('total_price'))}"
            f"（去{_price_text(max_combo.get('outbound_price'))} + 回{_price_text(max_combo.get('return_price'))}）"
        )
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
    _append_roundtrip_trend_chart(lines, round_trip)

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
        icon = "✓" if level in {"高", "中"} else "⚠"
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
    lines.append(f"价格位置：{price_judgment}")
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
        "上海", "北京", "广州", "深圳", "成都", "杭州", "南京", "厦门", "福州",
        "武汉", "西安", "重庆", "昆明", "青岛", "长沙", "郑州", "天津",
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
        limits.append("历史样本仍在积累，价格区间判断会随数据增多而更稳")

    lines.append("<b>⚠️ 当前判断的限制：</b>")
    for item in limits[:3]:
        lines.append(f"- {item}")
    lines.append("")


def _section(lines: list[str], title: str | None = None) -> None:
    lines.append("━━━━━━━━━━━━━━━━━━")
    if title:
        lines.append(title)
        lines.append("")


CARD_STYLE = "border:1px solid #ddd;border-radius:8px;padding:12px;margin:8px 0;"
PRIMARY_TITLE_STYLE = "font-weight:bold;color:#2563eb;"
ACTION_STYLE = "margin-top:6px;color:#16a34a;"
ACTION_ZONE_STYLE = (
    "border-left:4px solid #16a34a;padding:8px;margin:8px 0;background:#f0fdf4;"
)


def _compact_link_text(link_text: str, limit: int = 4) -> str:
    parts = [part for part in str(link_text or "").split(" | ") if part.strip()]
    return " | ".join(parts[:limit])


def _flight_link_text(flight: dict, route_info: dict, limit: int = 4) -> str:
    links = _compact_booking_links(flight, route_info)
    if not links:
        return ""
    return " | ".join(
        f'<a href="{url}" target="_blank">{name}</a>' for name, url in links[:limit]
    )


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
    if "需刷新" in availability_labels:
        availability = "需刷新"
    elif availability_labels and all(label == "可购买" for label in availability_labels):
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

    return " | ".join(
        [
            price_label,
            availability,
            f"置信度{(confidence or {}).get('overall', '中')}",
            risk,
        ]
    )


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
    lines.append(
        f"<div>渠道：{_channel_names(link_limit)}</div>"
    )
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
    lines.append(
        f'<div style="{ACTION_STYLE}">操作建议：{_human_recommendation_text(flight, route_info, analysis_result)}</div>'
    )
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
    extra = (
        transaction_total - total
        if transaction_total is not None and total is not None
        else None
    )

    lines.append(f'<div style="{CARD_STYLE}">')
    lines.append(_card_title(label, variant, primary))
    if transaction_total is not None:
        if extra and extra > 0:
            lines.append(
                f"<div>往返总价：{_price_text(total)}（去{_price_text(outbound.get('price'))} + 回{_price_text(return_flight.get('price'))}）</div>"
            )
            lines.append(f"<div>预估实付：{_price_text(transaction_total)}（含额外费用）</div>")
        else:
            lines.append(
                f"<div>往返总价：{_price_text(total)}（去{_price_text(outbound.get('price'))} + 回{_price_text(return_flight.get('price'))}）</div>"
            )
            lines.append(f"<div>预估实付：{_price_text(transaction_total)}（无额外费用）</div>")
    else:
        lines.append(f"<div>往返总价：{_price_text(total)}</div>")
    lines.append(f"<div>{_combo_leg_line('去', outbound, route_info.get('depart_date'))}</div>")
    lines.append(f"<div>{_combo_leg_line('回', return_flight, route_info.get('return_date'))}</div>")
    lines.append(f"<div>渠道：{_channel_names(link_limit)}</div>")
    combo_risk = "高" if "风险高" in _round_trip_combo_tags(combo, route_info, confidence) else ("中" if "风险中" in _round_trip_combo_tags(combo, route_info, confidence) else "低")
    lines.append(
        f"<div>可购买性：中高 | 执行风险：{combo_risk} | "
        f"符合约束：{_constraint_match_text(outbound, return_flight)}</div>"
    )
    lines.append(f"<div>🏷 {_round_trip_combo_tags(combo, route_info, confidence)}</div>")
    lines.append(f'<div style="{ACTION_STYLE}">操作建议：{_combo_human_recommendation(combo, route_info)}</div>')
    outbound_links = _compact_link_text(
        _combo_full_booking_links(outbound, route_info.get("depart_date")),
        link_limit,
    )
    return_links = _compact_link_text(
        _combo_full_booking_links(return_flight, route_info.get("return_date")),
        link_limit,
    )
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
    _section(lines, "<b>📍 为什么现在提醒你?</b>")
    reasons = (push_meta or {}).get("reasons") or ["当前价格或方案状态触发了你的监控条件"]
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
    lines.append(f"{'当前往返价' if is_round_trip else '当前价'}：{_price_text(current)}")
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
        lines.append(f"{'你的理想总价' if is_round_trip else '你的理想价'}：{_price_text(target)}")
        if current is not None and current <= target:
            lines.append(f"→ 当前低于理想价{_price_text(target - current)}，在强烈建议区间")
    if max_budget:
        lines.append(f"{'最高可接受总价' if is_round_trip else '最高可接受'}：{_price_text(max_budget)}")
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
    _section(lines, "<b>💡 为什么这样判断？</b>")
    reasons = (decision.get("reasons") or [])[:3]
    if reasons:
        for index, reason in enumerate(reasons, start=1):
            lines.append(f"{index}. {reason}")
    else:
        lines.append("1. 当前价格和执行信息需要结合支付页最终结果确认")
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
    _section(lines, "<b>📌 本次排序优先级</b>")
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
        details.append("渠道可购买性未验证，需到支付页确认")

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
            details.append(f"{name}约+{_price_text(amount)}{f'（{note}）' if note else ''}")

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

    _section(lines, "<b>🚫 已排除的更低价方案</b>")
    if not cheaper:
        lines.append("暂无比推荐方案更便宜但被排除的方案。")
        lines.append("")
        return

    for item in cheaper[: 2 if compact else 3]:
        combo = item.get("flight_combo") or "未命名方案"
        reason = item.get("reason") or "不符合当前要求"
        lines.append(f"- {_price_text(item.get('price'))} {combo}：{reason}")
    lines.append("💡 这些方案虽然更便宜，但不满足你的要求，所以未推荐")
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

    _section(lines, "<b>🚫 为什么没推荐更便宜的方案？</b>")
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
    form_url = _subscription_form_url(route_info)
    lines.append("查看购买渠道：已在方案卡片内列出")
    lines.append("复制购买前检查清单：见详细分析中的检查清单")
    lines.append("价格变化会自动推送，无需手动刷新")
    lines.append(f'修改监控偏好：<a href="{form_url}" target="_blank">打开订阅表单</a>')
    lines.append("买不到/价格不对：可回复“价格不对”或“详情”反馈")
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
    _section(lines, "<b>📈 详细分析</b>")
    if is_round_trip:
        round_trip = analysis_result.get("round_trip_analysis") or {}
        _append_roundtrip_trend_chart(lines, round_trip)
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
        trend = generate_trend_summary(history, current_min)
        arrow_line = _trend_arrow_line(history)
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

    decision, confidence, current, target, max_budget = _decision_context(
        analysis_result, route_info, source_stats_for_message, price_insights, is_round_trip
    )
    route_key, depart_key, return_key = _last_push_route_parts(route_info, is_round_trip)
    last_push = get_last_push_price(route_key, depart_key, return_key)
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

    primary_flight = None
    primary_items = []
    alternative_items = []

    _section(lines, "<b>⭐ 推荐方案</b>")
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
    _append_operation_section(lines, decision, current, target, max_budget, is_round_trip)
    _append_validity_section(lines, analysis_result, route_info, primary_flight)

    _section(lines, "<b>🔀 备选方案</b>")
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
    lines.append("💡 机票价格实时波动，推荐方案基于采集时数据。")
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
    save_last_push_price(
        route_key,
        depart_key,
        return_key,
        current,
        (push_meta or {}).get("type"),
        datetime.now().isoformat(timespec="seconds"),
    )
    return "<br>".join(lines)


def format_html_message(
    analysis_result=None,
    route_info=None,
    source_stats=None,
    price_insights=None,
    outbound_analysis=None,
    return_analysis=None,
):
    """生成压缩版HTML消息：经济舱3-4个方案 + 商务舱1个方案。"""
    message = _format_structured_html_message(
        analysis_result=analysis_result,
        route_info=route_info,
        source_stats=source_stats,
        price_insights=price_insights,
        outbound_analysis=outbound_analysis,
        return_analysis=return_analysis,
        compact=False,
    )
    if len(message) > PUSHPLUS_COMPACT_CHARS:
        message = _format_structured_html_message(
            analysis_result=analysis_result,
            route_info=route_info,
            source_stats=source_stats,
            price_insights=price_insights,
            outbound_analysis=outbound_analysis,
            return_analysis=return_analysis,
            compact=True,
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
            lines.append(f"👪 同行人员：{_companions_label(companions)}，优先关注白天、少折腾、行李和退改更稳的方案")
        lines.append(f"💰 价格敏感度：{_price_sensitivity_label(price_sensitivity)}")
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
            lines.append(f"数据量：{price_pos['data_points']}个历史价格点")
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

            lines.append("━━━━━━━━━━━━━━━━")
            lines.append("")
            lines.append("📈 价格走势")
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
                f"📊 <b>数据置信度：{confidence.get('emoji', '')} "
                f"{confidence.get('level', '未知')}</b>"
            )
            for reason in confidence.get("reasons", []):
                lines.append(f"　• {reason}")
        _append_system_health_lines(
            lines, analysis_result.get("system_health") or {}
        )
        if is_round_trip:
            _append_low_option_count_notice(lines, outbound_analysis, "去程")
            _append_low_option_count_notice(lines, return_analysis, "返程")
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
    """生成多方案对比推送消息"""
    recs = analysis_result.get("recommendations", [])
    market = analysis_result.get("market_context", {})
    days_to_dept = _days_to_depart(route_info)
    depart_date = route_info["depart_date"]
    lines = [
        f"✈️ {_city_label(route_info['origin'])} → {_city_label(route_info['destination'])}",
        "",
        f"📅 出发日期：{depart_date}",
    ]

    if days_to_dept is not None:
        lines.append(f"⏳ 距出发还有：{days_to_dept}天")
    lines.append(_mode_label(route_info.get("mode") or analysis_result.get("mode")))
    lines.append(_sort_rule_text(route_info.get("mode") or analysis_result.get("mode")))
    lines.append("以下方案按当前排序规则展示，排序不代表推荐。")

    market_line = _market_line(market)
    if market_line:
        lines.append(market_line)

    lines.extend(["", "━━━ 符合条件的方案 ━━━", ""])

    for index, rec in enumerate(recs):
        flight = rec.get("flight", {})
        segments = flight.get("segments") or []
        layovers = flight.get("layovers") or []
        stops = int(flight.get("stops") or max(len(segments) - 1, 0))

        if index:
            lines.extend(["", "━━━━━━━━━━━━━━━━━━━━", ""])

        lines.extend(
            [
                _plan_title(index, rec.get("tag", "")),
                "",
                f"💵 价格：{_money(flight.get('price'))}",
                f"✈️ 航线：{_display_route_summary(flight.get('route_summary', ''))}",
                f"⏱️ 全程：{_duration_hours(flight)}小时",
                f"🔄 转机：{stops}次" if stops else "🔄 转机：直飞",
                "",
            ]
        )

        for segment_index, segment in enumerate(segments):
            lines.extend(
                [
                    f"📍 {_segment_title(segment_index)}",
                    f"    航班：{segment.get('flight_no', '')} {_airline_display(segment.get('airline'))}",
                    f"    出发：{city_name(segment.get('dep_airport', ''))} {_time_only(segment.get('dep_time'))}",
                    f"    到达：{city_name(segment.get('arr_airport', ''))} {_time_only(segment.get('arr_time'))}",
                ]
            )

            if segment_index < len(layovers):
                layover = layovers[segment_index]
                layover_place = city_name(layover.get("airport") or "") or layover.get(
                    "city", "中转地"
                )
                lines.extend(
                    [
                        "",
                        f"    ⏳ 在{layover_place}转机",
                        f"    等待：{_wait_text(layover.get('wait_minutes'))}",
                    ]
                )
                risk_line = _layover_risk_line(layover)
                if risk_line:
                    lines.append(f"    {risk_line}")

            lines.append("")

        scores = flight.get("scores") or {}
        if scores.get("total") is not None:
            lines.append(f"⭐ 条件匹配度：{scores['total']} / 10")

        lines.extend(_duffel_extra_lines(flight))
        lines.append(f"📎 数据来源：{_source_label(flight.get('data_source'))}")

        warnings = generate_warnings(flight)
        if warnings:
            lines.extend(["", "⚠️ 注意事项"])
            for warning in warnings:
                cleaned = _clean_warning(warning)
                if cleaned:
                    lines.append(f"    • {cleaned}")

        booking_links = format_booking_links_text(flight, depart_date)
        if booking_links:
            lines.extend(["", booking_links.rstrip()])

    prices = analysis_result["price_range"]
    lines.extend(["", "━━━━━━━━━━━━━━━━━━━━", "", f"📊 价格区间：{_money(prices[0])} - {_money(prices[1])}", ""])

    source_summary = format_source_summary(
        source_stats
        or route_info.get("source_stats")
        or analysis_result.get("source_stats")
    )
    if source_summary:
        lines.extend([source_summary, ""])

    lines.extend(
        [
            f"🕐 采集时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
            "💬 总结",
            _summary_text(analysis_result, days_to_dept),
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            "以上内容基于历史价格数据分析，仅供参考。",
            "实际购买请以航司或OTA官网价格为准。",
            "以上排序基于当前配置规则，不代表最优选择。请根据您的时间、预算和出行需求自行判断。",
        ]
    )

    return "\n".join(lines)
