"""PushPlus notification helpers."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path


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


DISCLAIMER = "---\n以上建议基于历史价格数据分析，仅供参考。\n实际购买请以航司或OTA官网价格为准。"


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


def _append_disclaimer(message: str) -> str:
    return f"{message}\n\n{DISCLAIMER}"


def _advice(trigger_reason: str | None) -> str:
    advice_map = {
        "signal_upgrade": "建议：买入信号升级，优先检查目标航班并准备下单。",
        "milestone": "建议：进入关键观察节点，复查价格和替代方案。",
        "new_low": "建议：目标航班刷新历史低价，可以重点考虑。",
        "cheaper_alt": "建议：替代方案明显更便宜，建议比较中转和总时长。",
    }
    return advice_map.get(trigger_reason, "建议：继续观察，等待更明确的价格信号。")


def format_buy_message(analysis) -> str:
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
    return _append_disclaimer(message)


def format_consider_message(analysis) -> str:
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
    return _append_disclaimer(message)


def format_milestone_message(analysis, days) -> str:
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
    return _append_disclaimer(message)


def format_alternative_message(analysis) -> str:
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
    return _append_disclaimer(message)


def format_message(analysis: dict, trigger_reason: str | None) -> str:
    """Choose one human-friendly notification template."""
    if trigger_reason == "cheaper_alt" and analysis.get("cheapest_alt"):
        return format_alternative_message(analysis)
    if trigger_reason == "milestone":
        return format_milestone_message(analysis, analysis.get("days_to_dept"))
    signal = analysis.get("signal")
    if signal in {"strong_buy", "buy_now"}:
        return format_buy_message(analysis)
    if signal in {"buy", "consider"}:
        return format_consider_message(analysis)
    return format_milestone_message(analysis, analysis.get("days_to_dept"))


def _log_notification(content: str) -> None:
    NOTIFICATIONS_LOG.parent.mkdir(exist_ok=True)
    entry = (
        f"\n===== {datetime.now().isoformat(timespec='seconds')} =====\n"
        f"{content}\n"
    )
    with NOTIFICATIONS_LOG.open("a", encoding="utf-8") as file:
        file.write(entry)


def send(content: str) -> bool:
    """Send notification content through PushPlus."""
    if not os.environ.get("WECOM_WEBHOOK"):
        print(content)
        _log_notification(content)

    token = os.environ.get("PUSHPLUS_TOKEN")
    if not token:
        return False

    payload = {
        "token": token,
        "title": "航班监控通知",
        "content": content,
    }

    try:
        import requests

        response = requests.post("http://www.pushplus.plus/send", json=payload, timeout=10)
        response.raise_for_status()
    except Exception as exc:
        print(f"推送失败: {exc}")
        return False

    return True


def health_report(results: list[dict]) -> bool:
    """Send a short collection status report."""
    success_results = [result for result in results if result.get("status") == "ok"]
    current = success_results[0] if success_results else {}
    current_price = current.get("current_price", current.get("price"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    content = (
        f"✅ {now} 采集完成：{len(success_results)}条成功\n"
        f"AA128+AA1336 当前¥{_format_price(current_price)} | "
        f"信号：{current.get('signal', '-')}"
    )
    return send(content)
