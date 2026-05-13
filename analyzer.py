"""Four-dimensional flight price decision framework."""

from __future__ import annotations

import statistics
from datetime import date, datetime, time, timedelta

from storage import get_latest_alternatives, get_target_history


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
        return "buy_now", f"检测到不可逆涨价，建议立即购买；{reason}"

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
