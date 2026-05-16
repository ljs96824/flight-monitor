"""PushPlus notification helpers."""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path

import httpx

from analyzer import city_name


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


DISCLAIMER = "以上建议基于历史价格数据分析，仅供参考。\n实际购买请以航司或OTA官网价格为准。"


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
        "signal_upgrade": "建议：买入信号升级，优先检查目标航班并准备下单。",
        "milestone": "建议：进入关键观察节点，复查价格和替代方案。",
        "new_low": "建议：目标航班刷新历史低价，可以重点考虑。",
        "cheaper_alt": "建议：替代方案明显更便宜，建议比较中转和总时长。",
    }
    return advice_map.get(trigger_reason, "建议：继续观察，等待更明确的价格信号。")


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
            "💡 我的建议：现在入手",
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
        advice = "时间开始紧张了。除非价格明显在下降，否则建议在未来一周内做决定。"
    elif days == 7:
        advice = "最后一周了。如果价格能接受，建议今天就买。出发前几天涨价的概率很高。"
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
                    "template": "markdown",
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
        "duffel": "Duffel（航司直连）",
        "SerpAPISource": "SerpAPI（Google Flights）",
        "SearchAPISource": "SearchAPI（Google Flights）",
        "DuffelSource": "Duffel（航司直连）",
    }

    lines = ["📡 数据源汇总"]

    for key, value in source_stats.items():
        if key in ("total_raw", "after_dedup"):
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


def generate_booking_links(flight: dict, depart_date: str) -> str:
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
                "如果前段航班延误可能赶不上，建议确认是否联程票"
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
    """一句话总结建议"""
    market = analysis.get("market_context", {})
    level = market.get("price_level", "typical")
    cheapest = analysis["price_range"][0]

    if level == "low" and days_to_dept > 14:
        return (
            "💬 总结：当前整体处于低价期，"
            f"最低¥{cheapest:,}是不错的价格。"
            "如果行程确定建议抓住机会，低价期通常不会持续太久。"
        )
    elif level == "low" and days_to_dept <= 14:
        return (
            "💬 总结：低价期+临近出发，"
            "强烈建议尽快购买。"
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
            f"如果不急，建议等价格回落到¥{market.get('typical_range', [0, 0])[0]:,}以下再考虑。"
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
    return re.sub(r"^[⚠️ℹ️✅🟡🔴\s]+", "", str(warning)).strip()


def _summary_text(analysis_result: dict, days_to_dept: int | None) -> str:
    if days_to_dept is None:
        return "我会持续关注这条航线的价格变化。"
    summary = format_summary_advice(analysis_result, days_to_dept)
    summary = summary.replace("💬 总结：", "").replace("💬 总结", "").strip()
    return summary.replace("。我", "。\n我")


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

    previous_prices = route_info.get("previous_prices") or {}
    if previous_prices:
        lines.extend(
            [
                "",
                _overall_price_change_summary(analysis_result, previous_prices),
            ]
        )

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

        booking_links = generate_booking_links(flight, depart_date)
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
            "以上建议基于历史价格数据分析，仅供参考。",
            "实际购买请以航司或OTA官网价格为准。",
        ]
    )

    return "\n".join(lines)
