"""PushPlus notification helpers."""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path

import httpx

from analyzer import city_name, generate_trend_summary


BUY_SIGNALS = {"strong_buy", "buy", "buy_now"}
BASE_DIR = Path(__file__).parent
NOTIFICATIONS_LOG = BASE_DIR / "data" / "notifications_log.txt"


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


DISCLAIMER = "以上内容基于历史价格数据分析，仅供参考。\n实际购买请以航司或OTA官网价格为准。"


def format_price(price) -> str:
    """¥8,200 格式"""
    if price is None:
        return "¥-"
    return f"¥{float(price):,.0f}"


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
    """IATA代码转中文城市名"""
    mapping = {
        "PVG": "上海浦东",
        "SHA": "上海虹桥",
        "MCO": "奥兰多",
        "DFW": "达拉斯",
        "MIA": "迈阿密",
        "LAX": "洛杉矶",
        "SFO": "旧金山",
        "JFK": "纽约",
        "NRT": "东京成田",
        "ICN": "首尔仁川",
        "BKK": "曼谷",
        "SIN": "新加坡",
        "HND": "东京羽田",
        "ATL": "亚特兰大",
        "DTW": "底特律",
        "SEA": "西雅图",
        "YYZ": "多伦多",
        "FRA": "法兰克福",
        "TPE": "台北桃园",
    }
    return mapping.get(iata_code, iata_code)


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
            f"🔄 {alt.get('route_summary', '-')}",
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


def send(content: str, title: str = "航班监控通知") -> bool:
    """发送推送通知，优先PushPlus，备选企业微信。"""
    pushplus_token = os.environ.get("PUSHPLUS_TOKEN", "")
    if pushplus_token:
        try:
            resp = httpx.post(
                "https://www.pushplus.plus/send",
                json={
                    "token": pushplus_token,
                    "title": title,
                    "content": content,
                    "template": "html",
                },
                timeout=15,
            )
            result = resp.json()
            if result.get("code") == 200:
                print("PushPlus推送成功")
                return True
            print(f"PushPlus推送失败: {result.get('msg', '未知错误')}")
            return False
        except Exception as exc:
            print(f"PushPlus推送异常: {exc}")
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

    ctrip = (
        f"https://flights.ctrip.com/online/list/oneway-{origin}-{dest}"
        f"?depdate={depart_date}"
    )
    fliggy = (
        "https://www.fliggy.com/flight/international-search"
        f"?from={origin}&to={dest}&depDate={depart_date}"
    )
    google = (
        "https://www.google.com/travel/flights"
        f"?q=flights+from+{origin}+to+{dest}+on+{depart_date}"
    )

    airline_sites = {
        "美航": ("美航官网", "https://www.aa.com"),
        "American Airlines": ("美航官网", "https://www.aa.com"),
        "加航": ("加航官网", "https://www.aircanada.com"),
        "Air Canada": ("加航官网", "https://www.aircanada.com"),
        "联合": ("联合官网", "https://www.united.com"),
        "United": ("联合官网", "https://www.united.com"),
        "达美": ("达美官网", "https://www.delta.com"),
        "Delta": ("达美官网", "https://www.delta.com"),
    }
    airline_name = segments[0].get("airline", "")
    airline_site = airline_sites.get(airline_name)

    entries = [
        ("携程", ctrip),
        ("飞猪", fliggy),
    ]
    if airline_site:
        airline_label, airline_url = airline_site
        entries.append((airline_label, airline_url))
    entries.append(("Google Flights", google))

    lines = ["🔗 去购买", ""]
    circled_numbers = ["①", "②", "③", "④", "⑤", "⑥"]
    for index, (label, url) in enumerate(entries):
        prefix = circled_numbers[index] if index < len(circled_numbers) else f"{index + 1}."
        lines.extend([f"{prefix} {label}", url, ""])

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
    "balanced": "🎯 当前模式：均衡推荐",
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
    if value is None:
        return "未知"
    try:
        return f"¥{float(value):,.0f}"
    except (TypeError, ValueError):
        return f"¥{value}"


def _time_only(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).replace("T", " ")
    if " " in text:
        text = text.split(" ", 1)[1]
    return text[:5] if len(text) >= 5 else text


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
    label = parts[1] if len(parts) > 1 else str(tag or "推荐")
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
        "balanced": "均衡推荐",
        "budget": "省钱优先",
        "fast": "速度优先",
        "comfort": "舒适优先",
    }
    return mapping.get(mode or "balanced", mode or "均衡推荐")


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


def generate_booking_links(origin, dest, date, airlines=None):
    """返回 [(名称, URL)] 列表"""
    links = [
        ("携程", f"https://flights.ctrip.com/online/list/oneway-{origin}-{dest}?depdate={date}"),
        ("飞猪", f"https://www.fliggy.com/flight/international-search?from={origin}&to={dest}&depDate={date}"),
        ("Google Flights", f"https://www.google.com/travel/flights?q=flights+from+{origin}+to+{dest}+on+{date}"),
    ]
    if airlines:
        airline_str = " ".join(str(airline) for airline in airlines)
        if "美航" in airline_str or "American" in airline_str or "AA" in airline_str:
            links.append(("美航官网", "https://www.aa.com"))
        if "加航" in airline_str or "Air Canada" in airline_str or "AC" in airline_str:
            links.append(("加航官网", "https://www.aircanada.com"))
        if "联合" in airline_str or "United" in airline_str or "UA" in airline_str:
            links.append(("联合官网", "https://www.united.com"))
        if "达美" in airline_str or "Delta" in airline_str or "DL" in airline_str:
            links.append(("达美官网", "https://www.delta.com"))
    return links


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


def format_html_message(
    analysis_result, route_info, source_stats=None, price_insights=None
):
    """生成纯客观数据HTML消息。"""
    recs = analysis_result.get("recommendations", [])
    all_flights = analysis_result.get("all_flights") or [
        rec.get("flight") for rec in recs if rec.get("flight")
    ]
    days = analysis_result.get("days_to_dept", "")

    lines = []

    lines.append(f"<b>✈️ {city_name(route_info.get('origin',''))} → {city_name(route_info.get('destination',''))}</b>")
    lines.append("")
    lines.append(f"📅 出发日期：{route_info.get('depart_date','')}")
    lines.append(f"⏳ 距出发：{days}天")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━")

    current_min = (
        analysis_result.get("price_range", [0])[0]
        if analysis_result.get("price_range")
        else 0
    )
    trend = generate_trend_summary(
        price_insights.get("price_history") if price_insights else None,
        current_min,
    )
    current_prices = [
        flight.get("price")
        for flight in all_flights
        if flight and flight.get("price") is not None
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
        high_price = low_price = avg_price = current_min = 0

    lines.append("<b>📈 价格走势（近60天）</b>")
    lines.append("")
    lines.append(f"最高：¥{high_price:,.0f}")
    lines.append(f"最低：¥{low_price:,.0f}")
    lines.append(f"平均：¥{avg_price:,.0f}")
    lines.append(f"当前最低价：¥{current_min:,.0f}")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━")

    qualified_flights = analysis_result.get("qualified_flights") or []
    display_flights = qualified_flights or all_flights
    display_flights = sorted(
        [flight for flight in display_flights if flight],
        key=lambda flight: flight.get("price") or float("inf"),
    )

    for i, f in enumerate(display_flights):
        segments = f.get("segments", [])
        airline_text = " / ".join(_airlines_from_segments(segments))
        if not airline_text:
            airline_text = f.get("airline_summary") or " / ".join(f.get("airlines") or [])
        stops = f.get("stops", 0)

        lines.append("")
        lines.append(f"<b>{_option_label(i)}</b>")
        lines.append("")
        lines.append(f"💵 ¥{f['price']:,.0f}")
        lines.append(f"🏢 {airline_text}")
        lines.append(f"✈️ {f.get('route_summary','')}")
        lines.append(f"⏱️ 全程：{f.get('total_hours','')}小时")
        lines.append(f"🔄 转机：{'直飞' if stops == 0 else f'{stops}次'}")
        lines.append("")

        if not segments:
            lines.append("详细航段请查询航司官网")
            lines.append("")

        for j, seg in enumerate(segments):
            dep_time = str(seg.get("dep_time",""))
            if " " in dep_time:
                dep_time = dep_time.split(" ")[-1]
            arr_time = str(seg.get("arr_time",""))
            if " " in arr_time:
                arr_time = arr_time.split(" ")[-1]

            lines.append(f"✈ 第{j+1}段：{seg.get('flight_no','')}（{_airline_full_display(seg.get('airline',''))}）")
            lines.append(
                f"　　{city_name(seg.get('dep_airport',''))} {dep_time} → "
                f"{city_name(seg.get('arr_airport',''))} {arr_time}"
            )

            if j < len(f.get("layovers", [])):
                lay = f["layovers"][j]
                wait = lay.get("wait_minutes", 0)
                airport = lay.get("airport", "")
                city = city_name(airport) if airport else lay.get("city", "")
                lines.append("")
                lines.append(f"　　⏳ {city}转机：等待{wait//60}小时{wait%60}分钟")

            lines.append("")

        extra = f.get("extra", {})
        if f.get("has_baggage_info"):
            for baggage_line in format_baggage(extra):
                lines.append(baggage_line)
            for seat_line in format_seat(extra):
                lines.append(seat_line)
            change_text = "可改签" if extra.get("changeable") else "不可改签"
            refund_text = "可退票" if extra.get("refundable") else "不可退票"
            lines.append(f"🔄 退改：{change_text} · {refund_text}")
            lines.append("（数据来源：Duffel航司直连）")
        else:
            lines.append("🧳 行李：请查询航司官网")
            for seat_line in format_seat(extra):
                lines.append(seat_line)
            lines.append("🔄 退改：请查询航司官网")

        lines.append(f"📎 来源：{_source_label(f.get('data_source') or f.get('source'))}")
        lines.append("")

        lines.append("🔗 购买链接")
        lines.append("")

        origin = route_info.get("origin", "")
        dest = route_info.get("destination", "")
        date = route_info.get("depart_date", "")

        links = [
            ("携程", f"https://flights.ctrip.com/online/list/oneway-{origin}-{dest}?depdate={date}"),
            ("飞猪", f"https://www.fliggy.com/flight/international-search?from={origin}&to={dest}&depDate={date}"),
        ]
        airline_str = " ".join(str(a) for a in f.get("airlines", []))
        if "美航" in airline_str or "American" in airline_str or "AA" in str(f.get("flight_combo","")):
            links.append(("美航官网", "https://www.aa.com"))
        elif "加航" in airline_str or "Air Canada" in airline_str:
            links.append(("加航官网", "https://www.aircanada.com"))
        links.append((
            "Google Flights",
            f"https://www.google.com/travel/flights?q=flights+from+{origin}+to+{dest}+on+{date}",
        ))

        for link_index, (name, url) in enumerate(links, start=1):
            number_labels = ["①", "②", "③", "④", "⑤", "⑥"]
            number = number_labels[link_index - 1] if link_index <= len(number_labels) else str(link_index)
            lines.append(f"{number} {name}")
            lines.append(url)
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━")

    prices = analysis_result.get("price_range", [0, 0])
    lines.append("")
    lines.append(f"📊 价格区间：¥{prices[0]:,.0f} - ¥{prices[1]:,.0f}")
    lines.append("")

    lines.append("📡 数据源汇总")
    source_display = {
        "serpapi": "SerpAPI",
        "searchapi": "SearchAPI",
        "travelpayouts": "Travelpayouts（Aviasales）",
        "skyscanner": "Skyscanner（via RapidAPI）",
        "duffel": "Duffel",
    }
    if source_stats:
        for key, name in source_display.items():
            info = source_stats.get(key)
            if info and isinstance(info, dict):
                status = info.get("status", "")
                if key == "duffel" and "成功" in status:
                    lines.append(
                        f"　- {name}：行李退改信息补充（匹配到{source_stats.get('enriched_count', 0)}个方案）"
                    )
                elif "成功" in status:
                    lines.append(f"　- {name}：{info['count']}个方案 ✅")
                else:
                    lines.append(f"　- {name}：采集失败")
        lines.append(f"　- 合计{source_stats.get('total_raw',0)}个 → 去重后{source_stats.get('after_dedup',0)}个")
        lines.append("")

    from datetime import datetime
    lines.append(f"🕐 采集时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append("以上数据来自第三方API，仅供参考。")
    lines.append("实际价格请以航司或OTA官网为准。")

    return "<br>".join(lines)


def format_comparison_message(
    analysis_result: dict, route_info: dict, source_stats=None
) -> str:
    """生成多方案对比推送消息"""
    recs = analysis_result.get("recommendations", [])
    market = analysis_result.get("market_context", {})
    days_to_dept = _days_to_depart(route_info)
    depart_date = route_info["depart_date"]
    lines = [
        f"✈️ {city_name(route_info['origin'])} → {city_name(route_info['destination'])}",
        "",
        f"📅 出发日期：{depart_date}",
    ]

    if days_to_dept is not None:
        lines.append(f"⏳ 距出发还有：{days_to_dept}天")
    lines.append(_mode_label(route_info.get("mode") or analysis_result.get("mode")))

    market_line = _market_line(market)
    if market_line:
        lines.append(market_line)

    lines.extend(["", "━━━ 推荐方案 ━━━", ""])

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
                f"✈️ 航线：{flight.get('route_summary', '')}",
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
            lines.append(f"⭐ 综合评分：{scores['total']} / 10")

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
        ]
    )

    return "\n".join(lines)
