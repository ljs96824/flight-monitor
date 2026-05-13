"""Four-dimensional flight price decision framework."""

from __future__ import annotations

import statistics
from datetime import date, datetime, time, timedelta

from storage import get_all_history, get_latest_alternatives, get_target_history


IATA_CITY_NAMES = {
    # 中国大陆 / 港澳台
    "PVG": "上海浦东",
    "SHA": "上海虹桥",
    "PEK": "北京首都",
    "PKX": "北京大兴",
    "CAN": "广州",
    "SZX": "深圳",
    "CTU": "成都双流",
    "TFU": "成都天府",
    "CKG": "重庆",
    "HGH": "杭州",
    "NKG": "南京",
    "XIY": "西安",
    "WUH": "武汉",
    "XMN": "厦门",
    "TAO": "青岛",
    "CSX": "长沙",
    "KMG": "昆明",
    "FOC": "福州",
    "DLC": "大连",
    "TSN": "天津",
    "HKG": "香港",
    "MFM": "澳门",
    "TPE": "台北桃园",
    "TSA": "台北松山",
    # 日本 / 韩国 / 东南亚
    "NRT": "东京成田",
    "HND": "东京羽田",
    "KIX": "大阪关西",
    "NGO": "名古屋",
    "FUK": "福冈",
    "ICN": "首尔仁川",
    "GMP": "首尔金浦",
    "SIN": "新加坡",
    "BKK": "曼谷",
    "DMK": "曼谷廊曼",
    "KUL": "吉隆坡",
    "MNL": "马尼拉",
    "SGN": "胡志明",
    "HAN": "河内",
    "DEL": "德里",
    # 北美
    "MCO": "奥兰多",
    "DFW": "达拉斯",
    "MIA": "迈阿密",
    "LAX": "洛杉矶",
    "SFO": "旧金山",
    "SEA": "西雅图",
    "JFK": "纽约肯尼迪",
    "EWR": "纽约纽瓦克",
    "LGA": "纽约拉瓜迪亚",
    "ORD": "芝加哥",
    "ATL": "亚特兰大",
    "DTW": "底特律",
    "MSP": "明尼阿波利斯",
    "BOS": "波士顿",
    "IAD": "华盛顿杜勒斯",
    "DCA": "华盛顿里根",
    "PHL": "费城",
    "CLT": "夏洛特",
    "PHX": "凤凰城",
    "LAS": "拉斯维加斯",
    "DEN": "丹佛",
    "IAH": "休斯敦",
    "HOU": "休斯敦霍比",
    "AUS": "奥斯汀",
    "SAN": "圣迭戈",
    "SJC": "圣何塞",
    "PDX": "波特兰",
    "YYZ": "多伦多",
    "YVR": "温哥华",
    "YUL": "蒙特利尔",
    "YYC": "卡尔加里",
    # 欧洲 / 中东
    "LHR": "伦敦希思罗",
    "LGW": "伦敦盖特威克",
    "CDG": "巴黎戴高乐",
    "AMS": "阿姆斯特丹",
    "FRA": "法兰克福",
    "MUC": "慕尼黑",
    "ZRH": "苏黎世",
    "VIE": "维也纳",
    "MAD": "马德里",
    "BCN": "巴塞罗那",
    "FCO": "罗马",
    "IST": "伊斯坦布尔",
    "DOH": "多哈",
    "DXB": "迪拜",
    "AUH": "阿布扎比",
}


def city_name(iata_code: str) -> str:
    return IATA_CITY_NAMES.get(iata_code, iata_code)


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


def get_future_price_changes(
    price_history, days_to_dept: int, horizon: int = 7
) -> list[float]:
    """Return price changes within horizon days for comparable booking windows."""
    points = _valid_history(price_history)
    if len(points) < 2:
        return []

    depart_ts = _depart_timestamp(days_to_dept)
    horizon_seconds = horizon * 86400
    changes = []

    for index, (timestamp, price) in enumerate(points[:-1]):
        days_from_ts = (depart_ts - timestamp) / 86400
        if abs(days_from_ts - days_to_dept) > 7:
            continue

        future_points = [
            (future_ts, future_price)
            for future_ts, future_price in points[index + 1 :]
            if 0 < future_ts - timestamp <= horizon_seconds
        ]
        if not future_points:
            continue

        future_ts, future_price = max(future_points, key=lambda item: item[0])
        changes.append(future_price - price)

    return changes


def timing_analysis(price_history, current_price, days_to_dept) -> dict:
    """买票时机预测"""
    if _to_float(current_price) is None:
        return {"confidence": "low", "data_insufficient": True}

    try:
        days = int(days_to_dept)
    except (TypeError, ValueError):
        return {"confidence": "low", "data_insufficient": True}

    future_changes = get_future_price_changes(price_history, days, horizon=7)

    if not future_changes:
        return {"confidence": "low", "data_insufficient": True}

    drop_cases = [change for change in future_changes if change < -100]
    rise_cases = [change for change in future_changes if change > 100]
    stable_cases = [change for change in future_changes if -100 <= change <= 100]

    total = len(future_changes)
    result = {
        "drop_probability": round(len(drop_cases) / total * 100),
        "rise_probability": round(len(rise_cases) / total * 100),
        "stable_probability": round(len(stable_cases) / total * 100),
        "avg_drop": round(sum(drop_cases) / len(drop_cases)) if drop_cases else 0,
        "avg_rise": round(sum(rise_cases) / len(rise_cases)) if rise_cases else 0,
    }

    urgency = min(10, max(0, (100 - days) / 10))
    risk = result["rise_probability"] / 100
    result["buy_score"] = round(risk * 5 + urgency * 0.5, 1)

    return result


def weekday_analysis(db_path, route, depart_date) -> dict:
    """分析不同星期几的价格差异"""
    _ = db_path
    history = get_all_history(route, depart_date)

    if len(history) < 14:
        return {"data_insufficient": True}

    from collections import defaultdict

    weekday_prices = defaultdict(list)
    for record in history:
        snapshot_time = record.get("snapshot_time")
        price = _to_float(record.get("price"))
        if not snapshot_time or price is None:
            continue

        try:
            dt = datetime.fromisoformat(snapshot_time)
        except ValueError:
            continue

        weekday_prices[dt.weekday()].append(price)

    if sum(len(prices) for prices in weekday_prices.values()) < 14:
        return {"data_insufficient": True}

    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    stats = {}
    for day, prices in weekday_prices.items():
        stats[weekday_names[day]] = {
            "avg": round(sum(prices) / len(prices)),
            "min": min(prices),
            "count": len(prices),
        }

    sorted_days = sorted(stats.items(), key=lambda item: item[1]["avg"])
    cheapest_day = sorted_days[0][0]
    today = weekday_names[datetime.now().weekday()]

    return {
        "weekday_stats": dict(sorted_days),
        "cheapest_day": cheapest_day,
        "today": today,
        "today_is_cheap": today == cheapest_day,
    }


def airline_competition_analysis(
    flights: list[dict], historical_flights: list[dict] = None
) -> dict:
    """航司竞争态势分析"""
    _ = historical_flights
    from collections import defaultdict

    airline_prices = defaultdict(list)
    for flight in flights or []:
        price = _to_float(flight.get("price"))
        if price is None:
            continue

        airline = flight.get("airline_summary") or "未知"
        airline_prices[airline].append(
            {
                "price": price,
                "combo": flight.get("flight_combo", ""),
                "duration": flight.get("total_hours"),
                "stops": flight.get("stops"),
            }
        )

    result = {}
    for airline, options in airline_prices.items():
        cheapest = min(options, key=lambda item: item["price"])
        result[airline] = {
            "cheapest_price": cheapest["price"],
            "best_option": cheapest["combo"],
            "duration": cheapest["duration"],
            "stops": cheapest["stops"],
            "options_count": len(options),
            "trend": "unknown",
        }

    sorted_airlines = sorted(result.items(), key=lambda item: item[1]["cheapest_price"])

    return {
        "airlines": dict(sorted_airlines),
        "cheapest_airline": sorted_airlines[0][0] if sorted_airlines else None,
        "price_spread": (
            sorted_airlines[-1][1]["cheapest_price"]
            - sorted_airlines[0][1]["cheapest_price"]
            if len(sorted_airlines) > 1
            else 0
        ),
    }


def comfort_score(flight: dict) -> dict:
    """计算航班舒适度评分（0-10）"""
    score = 10.0
    penalties = []
    bonuses = []

    stops = int(flight.get("stops") or 0)
    if stops == 1:
        score -= 1
    elif stops == 2:
        score -= 3
        penalties.append("需转机2次")
    elif stops >= 3:
        score -= 5
        penalties.append(f"需转机{stops}次")

    for layover in flight.get("layovers", []) or []:
        wait = int(layover.get("wait_minutes") or 0)
        city = layover.get("city", "中转地")
        if wait > 480:
            score -= 2
            penalties.append(f"在{city}等待超过8小时，可能需过夜")
        elif wait > 240:
            score -= 1
            penalties.append(f"在{city}等待较长")
        elif wait < 60:
            score -= 1.5
            penalties.append(f"在{city}转机时间仅{wait}分钟，较紧张")
        else:
            bonuses.append(f"转机等待时间合理（{wait // 60}小时{wait % 60}分钟）")

    hours = _to_float(flight.get("total_hours")) or 0
    if hours > 30:
        score -= 2
        penalties.append(f"全程{hours:g}小时，耗时较长")
    elif hours > 24:
        score -= 1
        penalties.append("全程超过24小时")
    elif hours < 20:
        bonuses.append("全程时间合理")

    segments = flight.get("segments", []) or []
    if any(segment.get("overnight") for segment in segments if isinstance(segment, dict)):
        score -= 0.5
        penalties.append("含过夜航段")

    score = max(0, min(10, round(score, 1)))

    return {
        "score": score,
        "level": "推荐" if score >= 7 else "一般" if score >= 5 else "较差",
        "penalties": penalties,
        "bonuses": bonuses,
    }


def detect_anomaly(
    flight: dict, price_insights: dict, all_prices: list[float]
) -> dict:
    """检测价格是否异常偏低"""
    price = _to_float(flight.get("price"))
    if price is None:
        return {"is_anomaly": False}

    typical_range = (price_insights or {}).get("typical_price_range", [])
    if not typical_range or len(typical_range) < 2:
        return {"is_anomaly": False}

    typical_low = _to_float(typical_range[0])
    typical_high = _to_float(typical_range[1])
    if typical_low is None or typical_high is None:
        return {"is_anomaly": False}

    clean_prices = [_to_float(item) for item in all_prices or []]
    clean_prices = [item for item in clean_prices if item is not None]
    avg_price = sum(clean_prices) / len(clean_prices) if clean_prices else typical_high

    if price < typical_low * 0.7:
        discount_pct = round((1 - price / avg_price) * 100) if avg_price else 0
        return {
            "is_anomaly": True,
            "type": "极端低价",
            "discount_pct": discount_pct,
            "message": f"比正常价格低{discount_pct}%，可能是系统错误或限时促销",
        }

    if price < typical_low:
        return {
            "is_anomaly": False,
            "is_good_deal": True,
            "message": "低于市场正常价格区间",
        }

    return {"is_anomaly": False, "is_good_deal": False}


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


def analyze_all_flights(flights: list[dict], price_insights: dict = None) -> dict:
    """对所有航班方案做多维度分析和排名"""
    if not flights:
        return {"error": "no_flights"}

    usable_flights = [
        flight
        for flight in flights
        if flight.get("price") is not None and flight.get("total_duration_min") is not None
    ]
    if not usable_flights:
        return {"error": "no_flights"}

    # 1. 按价格排名
    by_price = sorted(usable_flights, key=lambda f: f["price"])

    # 2. 按总时长排名
    by_duration = sorted(usable_flights, key=lambda f: f["total_duration_min"])

    # 3. 按性价比排名（综合得分）
    prices = [f["price"] for f in usable_flights]
    durations = [f["total_duration_min"] for f in usable_flights]
    min_p, max_p = min(prices), max(prices)
    min_d, max_d = min(durations), max(durations)

    for flight in usable_flights:
        price_score = (
            (flight["price"] - min_p) / (max_p - min_p) if max_p > min_p else 0
        )
        duration_score = (
            (flight["total_duration_min"] - min_d) / (max_d - min_d)
            if max_d > min_d
            else 0
        )
        stops_score = flight["stops"] / 3
        flight["value_score"] = round(
            price_score * 0.5 + duration_score * 0.3 + stops_score * 0.2,
            3,
        )

    by_value = sorted(usable_flights, key=lambda f: f["value_score"])

    # 4. 筛选推荐方案（最多展示5个）
    recommendations = []

    recommendations.append(
        {
            "tag": "💰 最低价",
            "flight": by_price[0],
            "reason": (
                f"价格最低，节省¥{by_price[1]['price'] - by_price[0]['price']:,.0f}"
                if len(by_price) > 1
                else "唯一方案"
            ),
        }
    )

    if by_duration[0]["flight_combo"] != by_price[0]["flight_combo"]:
        time_diff = by_price[0]["total_duration_min"] - by_duration[0]["total_duration_min"]
        recommendations.append(
            {
                "tag": "⚡ 最快到达",
                "flight": by_duration[0],
                "reason": f"比最便宜方案快{time_diff // 60}小时{time_diff % 60}分钟",
            }
        )

    if (
        by_value[0]["flight_combo"] != by_price[0]["flight_combo"]
        and by_value[0]["flight_combo"] != by_duration[0]["flight_combo"]
    ):
        recommendations.append(
            {
                "tag": "⭐ 最佳性价比",
                "flight": by_value[0],
                "reason": "价格和时长的最优平衡",
            }
        )

    min_stops_flight = min(usable_flights, key=lambda f: f["stops"])
    existing_combos = [r["flight"]["flight_combo"] for r in recommendations]
    if (
        min_stops_flight["stops"] < by_price[0]["stops"]
        and min_stops_flight["flight_combo"] not in existing_combos
    ):
        recommendations.append(
            {
                "tag": "🛫 最少中转",
                "flight": min_stops_flight,
                "reason": f"仅需{min_stops_flight['stops']}次中转",
            }
        )

    recommendations = recommendations[:5]

    market_context = {}
    if price_insights:
        market_context = {
            "lowest_market": price_insights.get("lowest_price"),
            "price_level": price_insights.get("price_level"),
            "typical_range": price_insights.get("typical_price_range"),
        }

    return {
        "total_options": len(usable_flights),
        "recommendations": recommendations,
        "all_flights": usable_flights[:10],
        "price_range": [min(prices), max(prices)],
        "duration_range": [min(durations), max(durations)],
        "market_context": market_context,
    }
