import json
from datetime import datetime
from pathlib import Path


SIGNALS_LOG = Path(__file__).parent / "data" / "signals_history.jsonl"


def log_signal(route, depart_date, analysis_result, price_insights=None):
    """Record one signal after each collection and analysis run."""
    record = {
        "timestamp": datetime.now().isoformat(),
        "route": route,
        "depart_date": depart_date,
        "current_min_price": analysis_result.get("price_range", [0])[0],
        "price_range": analysis_result.get("price_range"),
        "total_options": analysis_result.get("total_options", 0),
        "price_percentile": None,
        "waiting_risk": None,
        "trend_direction": None,
        "confidence": None,
        "outcome": None,
        "outcome_price": None,
        "was_correct": None,
    }

    if analysis_result.get("price_position"):
        record["price_percentile"] = analysis_result["price_position"].get(
            "percentile"
        )

    if analysis_result.get("waiting_risk"):
        waiting_risk = analysis_result["waiting_risk"]
        record["waiting_risk"] = {
            "up_prob": waiting_risk.get("up_probability"),
            "down_prob": waiting_risk.get("down_probability"),
        }

    trend = analysis_result.get("trend")
    if isinstance(trend, dict):
        record["trend_direction"] = trend.get("trend")
    elif trend:
        record["trend_direction"] = trend

    record["confidence"] = calculate_confidence(analysis_result, price_insights)

    SIGNALS_LOG.parent.mkdir(exist_ok=True)
    with open(SIGNALS_LOG, "a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")

    return record


def calculate_confidence(analysis_result, price_insights=None):
    """Calculate confidence for the current analysis."""
    score = 0
    reasons = []

    sources_count = 0
    for value in (analysis_result.get("source_stats") or {}).values():
        if isinstance(value, dict) and "成功" in str(value.get("status", "")):
            sources_count += 1

    if sources_count >= 3:
        score += 30
        reasons.append("3个数据源交叉验证")
    elif sources_count == 2:
        score += 20
        reasons.append("2个数据源")
    elif sources_count == 1:
        score += 10
        reasons.append("仅1个数据源")

    total = analysis_result.get("total_options", 0)
    if total >= 20:
        score += 20
        reasons.append(f"方案充足（{total}个）")
    elif total >= 10:
        score += 15
        reasons.append(f"方案较多（{total}个）")
    elif total >= 5:
        score += 10
        reasons.append(f"方案有限（{total}个）")
    else:
        score += 5
        reasons.append(f"方案很少（{total}个）")

    if price_insights and price_insights.get("price_history"):
        history_len = len(price_insights["price_history"])
        if history_len >= 50:
            score += 25
            reasons.append(f"历史数据丰富（{history_len}个点）")
        elif history_len >= 20:
            score += 15
            reasons.append(f"历史数据适中（{history_len}个点）")
        else:
            score += 5
            reasons.append(f"历史数据较少（{history_len}个点）")
    else:
        reasons.append("无历史数据")

    price_range = analysis_result.get("price_range")
    if price_range and len(price_range) >= 2:
        low, high = price_range
        if low and high and high > 0:
            spread = (high - low) / low * 100
            if spread < 30:
                score += 15
                reasons.append("价格分布集中，判断更可靠")
            elif spread < 60:
                score += 10
                reasons.append("价格分布适中")
            else:
                score += 5
                reasons.append("价格分布很散，判断不确定性高")

    days = analysis_result.get("days_to_dept", 0) or 0
    if 21 <= days <= 45:
        score += 10
        reasons.append("在最佳购买窗口内，数据规律较明显")
    elif days > 45:
        score += 5
        reasons.append("距出发较远，价格仍有较大波动空间")
    elif days < 7:
        score += 5
        reasons.append("临近出发，价格变动不可预测")

    if score >= 75:
        level = "高"
        emoji = "🟢"
    elif score >= 50:
        level = "中"
        emoji = "🟡"
    else:
        level = "低"
        emoji = "🔴"

    return {
        "score": score,
        "level": level,
        "emoji": emoji,
        "reasons": reasons,
    }
