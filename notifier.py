"""PushPlus notification helpers."""

from __future__ import annotations

import os
from datetime import datetime


BUY_SIGNALS = {"strong_buy", "buy", "buy_now"}


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


def _format_route(route: str) -> str:
    return route.replace("-", "→")


def _format_price(value) -> str:
    if value is None:
        return "-"
    return f"{float(value):.0f}"


def _advice(trigger_reason: str | None) -> str:
    advice_map = {
        "signal_upgrade": "建议：买入信号升级，优先检查目标航班并准备下单。",
        "milestone": "建议：进入关键观察节点，复查价格和替代方案。",
        "new_low": "建议：目标航班刷新历史低价，可以重点考虑。",
        "cheaper_alt": "建议：替代方案明显更便宜，建议比较中转和总时长。",
    }
    return advice_map.get(trigger_reason, "建议：继续观察，等待更明确的价格信号。")


def format_message(analysis: dict, trigger_reason: str | None) -> str:
    """Format an analysis result as a WeCom markdown message."""
    trend = analysis.get("trend", {})
    cheapest_alt = analysis.get("cheapest_alt")
    price_diff = analysis.get("target_vs_cheapest", 0)

    lines = [
        f"✈️ {_format_route(analysis.get('route', ''))} {analysis.get('depart_date', '')}",
        "",
        f"💰 {analysis.get('target_combo', '')}：¥{_format_price(analysis.get('current_price'))}",
        (
            f"📉 市场最低价：¥{_format_price(analysis.get('google_lowest'))}"
            f"（{analysis.get('google_level', '-')}）"
        ),
        (
            f"📊 均价¥{_format_price(analysis.get('avg_price'))} | "
            f"最低¥{_format_price(analysis.get('min_seen'))} | "
            f"最高¥{_format_price(analysis.get('max_seen'))}"
        ),
        (
            f"📈 趋势：{trend.get('trend', 'flat')}"
            f"（{trend.get('change_pct', 0)}%）"
        ),
        (
            f"📅 距出发 {analysis.get('days_to_dept', 0)} 天 | "
            f"数据点 {analysis.get('data_points', 0)}个"
        ),
        "",
        _advice(trigger_reason),
    ]

    if cheapest_alt and price_diff > 500:
        lines.extend(
            [
                "",
                "更便宜替代方案：",
                (
                    f"{cheapest_alt.get('flight_combo', '')}："
                    f"¥{_format_price(cheapest_alt.get('price'))}，"
                    f"便宜¥{_format_price(price_diff)}"
                ),
                (
                    f"路线：{cheapest_alt.get('route_summary', '-')}"
                    f" | 时长：{cheapest_alt.get('duration_hours', '-')}小时"
                ),
            ]
        )

    return "\n".join(lines)


def send(content: str) -> bool:
    """Send notification content through PushPlus."""
    token = os.environ.get("PUSHPLUS_TOKEN")
    if not token:
        print("推送失败: 请在.env文件中设置PUSHPLUS_TOKEN")
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
