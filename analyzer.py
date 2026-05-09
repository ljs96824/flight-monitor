"""Price analysis helpers."""

from __future__ import annotations

from datetime import date, datetime

from storage import get_latest_alternatives, get_target_history


def calc_trend(recent_prices: list[float]) -> dict:
    """Calculate price trend by comparing first-half and second-half averages."""
    if len(recent_prices) < 2:
        return {"trend": "flat", "change_pct": 0.0}

    midpoint = len(recent_prices) // 2
    first_half = recent_prices[:midpoint]
    second_half = recent_prices[midpoint:]

    first_avg = sum(first_half) / len(first_half)
    second_avg = sum(second_half) / len(second_half)

    if first_avg == 0:
        change_pct = 0.0
    else:
        change_pct = ((second_avg - first_avg) / first_avg) * 100

    if change_pct > 2:
        trend = "rising"
    elif change_pct < -2:
        trend = "falling"
    else:
        trend = "flat"

    return {"trend": trend, "change_pct": round(change_pct, 1)}


def generate_signal(
    price: float,
    trend: str,
    days_to_dept: int,
    min_seen: float,
    avg_price: float,
    google_level: str | None = None,
) -> str:
    """Generate a buy/wait signal from price, trend, and departure timing."""
    if days_to_dept <= 7:
        signal = "buy_now"
    elif price <= min_seen * 1.02 and trend == "rising":
        signal = "strong_buy"
    elif price < avg_price * 0.95 and trend != "falling":
        signal = "buy"
    elif price < avg_price * 0.95 and trend == "falling":
        signal = "wait"
    elif days_to_dept <= 14:
        signal = "consider"
    elif days_to_dept <= 21 and trend != "falling":
        signal = "consider"
    else:
        signal = "hold"

    if google_level == "low":
        signal = _strengthen_buy_signal(signal)
    elif google_level == "high":
        signal = _strengthen_wait_signal(signal)

    return signal


def _strengthen_buy_signal(signal: str) -> str:
    upgrades = {
        "hold": "consider",
        "wait": "consider",
        "consider": "buy",
        "buy": "strong_buy",
    }
    return upgrades.get(signal, signal)


def _strengthen_wait_signal(signal: str) -> str:
    downgrades = {
        "strong_buy": "buy",
        "buy": "consider",
        "consider": "hold",
        "hold": "wait",
    }
    return downgrades.get(signal, signal)


def analyze_with_google_insights(price_insights, current_price) -> dict:
    """Analyze Google Flights price insights and historical price percentile."""
    typical_range = price_insights.get("typical_price_range") or []
    price_level = price_insights.get("price_level")
    price_history = price_insights.get("price_history") or []

    range_status = "unknown"
    if len(typical_range) >= 2 and current_price is not None:
        low, high = typical_range[0], typical_range[1]
        if current_price < low:
            range_status = "below_typical"
        elif low <= current_price <= high:
            range_status = "typical"
        elif current_price > high:
            range_status = "above_typical"

    history_series = []
    history_prices = []
    for point in price_history:
        if not isinstance(point, list) or len(point) < 2:
            continue
        timestamp, price = point[0], point[1]
        if price is None:
            continue
        history_prices.append(float(price))
        history_series.append(
            {
                "date": datetime.fromtimestamp(timestamp).date().isoformat(),
                "price": float(price),
            }
        )

    historical_percentile = None
    if history_prices and current_price is not None:
        current = float(current_price)
        below_or_equal = sum(1 for price in history_prices if price <= current)
        historical_percentile = round((below_or_equal / len(history_prices)) * 100, 1)

    if range_status == "below_typical":
        assessment = "低于典型区间，好价格"
    elif range_status == "typical":
        assessment = "正常价格"
    elif range_status == "above_typical":
        assessment = "高于典型区间，偏贵"
    else:
        assessment = "缺少典型价格区间"

    if price_level:
        assessment = f"{assessment}；Google价格水平={price_level}"

    return {
        "assessment": assessment,
        "range_status": range_status,
        "price_level": price_level,
        "typical_price_range": typical_range,
        "history_series": history_series,
        "history_points": len(history_series),
        "historical_percentile": historical_percentile,
    }


def analyze(db_path, route: str, depart_date: str, target_combo: str) -> dict:
    """Analyze target history and latest alternatives from SQLite storage."""
    target_history = get_target_history(route, depart_date, target_combo)
    alternatives = get_latest_alternatives(route, depart_date, target_combo)
    prices = [
        float(record["price"])
        for record in target_history
        if record.get("price") is not None
    ]
    days_to_dept = (date.fromisoformat(depart_date) - date.today()).days

    base_result = {
        "current_price": 0.0,
        "min_seen": 0.0,
        "max_seen": 0.0,
        "avg_price": 0.0,
        "data_points": len(prices),
        "days_to_dept": days_to_dept,
        "trend": {"trend": "flat", "change_pct": 0.0},
        "cheapest_alt": alternatives[0] if alternatives else None,
        "target_vs_cheapest": 0,
        "signal": "collecting",
        "stage": "insufficient",
        "depart_date": depart_date,
        "route": route,
        "target_combo": target_combo,
    }

    if not prices:
        return base_result

    current_price = prices[-1]
    min_seen = min(prices)
    max_seen = max(prices)
    avg_price = sum(prices) / len(prices)
    recent_prices = prices[-10:]
    trend_info = calc_trend(recent_prices)

    cheapest_alt = alternatives[0] if alternatives else None
    target_vs_cheapest = 0
    if cheapest_alt and cheapest_alt.get("price") is not None:
        target_vs_cheapest = round(current_price - float(cheapest_alt["price"]), 2)

    if len(prices) < 4:
        stage = "insufficient"
        signal = "collecting"
    elif len(prices) <= 20:
        stage = "trend_only"
        signal = generate_signal(
            current_price,
            trend_info["trend"],
            days_to_dept,
            min_seen,
            avg_price,
        )
    else:
        stage = "full"
        signal = generate_signal(
            current_price,
            trend_info["trend"],
            days_to_dept,
            min_seen,
            avg_price,
        )

    return {
        "current_price": current_price,
        "min_seen": min_seen,
        "max_seen": max_seen,
        "avg_price": round(avg_price, 2),
        "data_points": len(prices),
        "days_to_dept": days_to_dept,
        "trend": trend_info,
        "cheapest_alt": cheapest_alt,
        "target_vs_cheapest": target_vs_cheapest,
        "signal": signal,
        "stage": stage,
        "depart_date": depart_date,
        "route": route,
        "target_combo": target_combo,
    }


def analyze_combined(
    db, route: str, depart_date: str, target_combo: str, price_insights: dict
) -> dict:
    """Analyze target history together with Google market-level insights."""
    analysis = analyze(db, route, depart_date, target_combo)
    current_price = analysis.get("current_price")
    google_lowest = price_insights.get("lowest_price")
    google_level = price_insights.get("price_level")
    google_typical_range = price_insights.get("typical_price_range")
    google_analysis = analyze_with_google_insights(price_insights, current_price)

    market_gap = 0
    market_gap_pct = 0
    if current_price and google_lowest:
        market_gap = round(float(current_price) - float(google_lowest), 2)
        market_gap_pct = round((market_gap / float(google_lowest)) * 100, 1)

    if analysis.get("signal") != "collecting" and current_price:
        analysis["signal"] = generate_signal(
            float(current_price),
            analysis["trend"]["trend"],
            analysis["days_to_dept"],
            float(analysis["min_seen"]),
            float(analysis["avg_price"]),
            google_level,
        )

    analysis.update(
        {
            "google_lowest": google_lowest,
            "google_level": google_level,
            "google_typical_range": google_typical_range,
            "google_percentile": google_analysis.get("historical_percentile"),
            "market_gap": market_gap,
            "market_gap_pct": market_gap_pct,
            "google_insights": google_analysis,
        }
    )
    return analysis
