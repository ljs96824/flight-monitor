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


def generate_sparkline(prices: list, width: int = 14) -> str:
    """用Unicode方块字符生成迷你趋势图。"""
    if not prices or len(prices) < 2:
        return ""

    clean_prices = [_to_float(price) for price in prices]
    clean_prices = [price for price in clean_prices if price is not None and price > 0]
    if len(clean_prices) < 2:
        return ""

    blocks = "▁▂▃▄▅▆▇█"

    if len(clean_prices) > width:
        step = len(clean_prices) / width
        sampled = [clean_prices[int(index * step)] for index in range(width)]
    else:
        sampled = clean_prices

    min_p = min(sampled)
    max_p = max(sampled)

    if max_p == min_p:
        return blocks[3] * len(sampled)

    sparkline = ""
    for price in sampled:
        level = int((price - min_p) / (max_p - min_p) * 7)
        level = max(0, min(7, level))
        sparkline += blocks[level]

    return sparkline


def generate_trend_summary(price_history_data, current_price) -> dict:
    """生成趋势摘要。"""
    if not price_history_data:
        return {"available": False}

    if isinstance(price_history_data[0], (list, tuple)):
        prices = [_to_float(price) for _, price in price_history_data]
    else:
        prices = [_to_float(price) for price in price_history_data]
    prices = [price for price in prices if price is not None and price > 0]

    if len(prices) < 3:
        return {"available": False}

    sparkline = generate_sparkline(prices)
    min_price = min(prices)
    max_price = max(prices)
    avg_price = sum(prices) / len(prices)

    recent = prices[-5:] if len(prices) >= 5 else prices
    if recent[-1] > recent[0] * 1.03:
        recent_trend = "📈 近期上涨中"
    elif recent[-1] < recent[0] * 0.97:
        recent_trend = "📉 近期下降中"
    else:
        recent_trend = "➡️ 近期平稳"

    current = _to_float(current_price)
    if current is None:
        current = prices[-1]

    if current <= min_price * 1.05:
        position = "接近历史最低 🟢"
    elif current >= max_price * 0.95:
        position = "接近历史最高 🔴"
    elif current < avg_price:
        position = "低于平均水平 🟡"
    else:
        position = "高于平均水平 🟠"

    return {
        "available": True,
        "sparkline": sparkline,
        "min_price": min_price,
        "max_price": max_price,
        "avg_price": round(avg_price),
        "current_position": position,
        "recent_trend": recent_trend,
        "data_points": len(prices),
    }


def price_position_description(current_price, price_history):
    """用历史数据计算当前价格的位置描述"""
    if not price_history or len(price_history) < 5:
        return None

    if isinstance(price_history[0], (list, tuple)):
        prices = [price for _, price in price_history if price and price > 0]
    else:
        prices = [price for price in price_history if price and price > 0]

    if not prices:
        return None

    below = sum(1 for price in prices if price < current_price)
    percentile = round(below / len(prices) * 100)

    min_p = min(prices)
    max_p = max(prices)
    avg_p = round(sum(prices) / len(prices))

    if percentile <= 20:
        level = "低价区"
        desc = "当前价格低于历史80%的记录，属于少见的低价"
    elif percentile <= 40:
        level = "偏低区"
        desc = f"当前价格低于历史{100 - percentile}%的记录，低于大多数时候"
    elif percentile <= 60:
        level = "正常区"
        desc = "当前价格处于历史中间水平"
    elif percentile <= 80:
        level = "偏高区"
        desc = f"当前价格高于历史{percentile}%的记录，高于大多数时候"
    else:
        level = "高价区"
        desc = "当前价格高于历史80%的记录，属于偏贵时段"

    return {
        "percentile": percentile,
        "level": level,
        "description": desc,
        "min_price": min_p,
        "max_price": max_p,
        "avg_price": avg_p,
        "data_points": len(prices),
    }


def waiting_risk_description(price_history, current_price, days_to_dept):
    """计算继续等待一周的风险收益"""
    if not price_history or len(price_history) < 10:
        return None

    if isinstance(price_history[0], (list, tuple)):
        prices = [price for _, price in price_history if price and price > 0]
    else:
        prices = [price for price in price_history if price and price > 0]

    if len(prices) < 10:
        return None

    changes = []
    for index in range(1, len(prices)):
        changes.append(prices[index] - prices[index - 1])

    if not changes:
        return None

    ups = [change for change in changes if change > 0]
    downs = [change for change in changes if change < 0]

    up_prob = round(len(ups) / len(changes) * 100)
    down_prob = round(len(downs) / len(changes) * 100)
    avg_up = round(sum(ups) / len(ups)) if ups else 0
    avg_down = round(abs(sum(downs) / len(downs))) if downs else 0

    if days_to_dept <= 7:
        urgency = "出发在即，等待风险很高"
    elif days_to_dept <= 14:
        urgency = "时间较紧，等待空间有限"
    elif days_to_dept <= 30:
        urgency = "在最佳购买窗口内"
    else:
        urgency = "时间充裕，可以继续观察"

    return {
        "up_probability": up_prob,
        "down_probability": down_prob,
        "avg_up_amount": avg_up,
        "avg_down_amount": avg_down,
        "days_to_dept": days_to_dept,
        "urgency": urgency,
    }


def _history_prices_for_combo(price_history, combo: str) -> list[float]:
    """Extract historical prices for one flight combo from flexible history shapes."""
    if not price_history or not combo:
        return []

    normalized_combo = combo.replace(" ", "").upper()

    if isinstance(price_history, dict):
        candidates = (
            price_history.get(combo)
            or price_history.get(normalized_combo)
            or price_history.get("by_flight", {}).get(combo)
            or price_history.get("by_flight", {}).get(normalized_combo)
        )
        if candidates is None:
            candidates = price_history.get("records") or price_history.get("history")
    else:
        candidates = price_history

    prices = []
    for item in candidates or []:
        if isinstance(item, dict):
            item_combo = item.get("flight_combo") or item.get("combo")
            if item_combo and item_combo.replace(" ", "").upper() != normalized_combo:
                continue
            price = _to_float(item.get("price") or item.get("current_min_price"))
        elif isinstance(item, (list, tuple)):
            if len(item) >= 3:
                item_combo = str(item[0])
                if item_combo.replace(" ", "").upper() != normalized_combo:
                    continue
                price = _to_float(item[2])
            elif len(item) >= 2:
                price = _to_float(item[1])
            else:
                price = None
        else:
            price = _to_float(item)

        if price is not None and price > 0:
            prices.append(price)

    return prices


def _source_price_entries(flight: dict) -> list[dict]:
    entries = (
        flight.get("source_prices")
        or flight.get("source_price_details")
        or flight.get("prices_by_source")
        or []
    )
    if isinstance(entries, dict):
        return [
            {"source": source, "price": price}
            for source, price in entries.items()
        ]
    return [entry for entry in entries if isinstance(entry, dict)]


def _add_anomaly(anomalies: list[dict], anomaly: dict, seen: set[tuple]) -> None:
    key = (
        anomaly.get("flight_combo"),
        anomaly.get("type"),
        anomaly.get("severity"),
        anomaly.get("message"),
    )
    if key in seen:
        return
    seen.add(key)
    anomalies.append(anomaly)


def detect_price_anomalies(flights, price_history=None):
    """Detect statistical, historical, source, and time-series price anomalies."""
    valid_flights = [
        flight for flight in flights or []
        if _to_float(flight.get("price")) is not None
    ]
    prices = [_to_float(flight.get("price")) for flight in valid_flights]
    prices = [price for price in prices if price is not None and price > 0]

    anomalies = []
    seen = set()
    if not prices:
        return anomalies

    avg_price = statistics.mean(prices)
    std_dev = statistics.stdev(prices) if len(prices) >= 2 else 0

    for flight in valid_flights:
        price = _to_float(flight.get("price"))
        if price is None or price <= 0:
            continue

        combo = flight.get("flight_combo", "")
        z_score = (price - avg_price) / std_dev if std_dev else 0

        if price < avg_price * 0.6 or price > avg_price * 1.5 or abs(z_score) > 2:
            if price < avg_price * 0.6 or z_score < -2:
                severity = "alert"
                anomaly_type = "统计低价异常"
            elif price > avg_price * 1.5 or z_score > 2:
                severity = "warning"
                anomaly_type = "统计高价异常"
            else:
                severity = "info"
                anomaly_type = "统计异常"
            _add_anomaly(
                anomalies,
                {
                    "type": anomaly_type,
                    "severity": severity,
                    "flight_combo": combo,
                    "price": price,
                    "z_score": round(z_score, 2),
                    "message": (
                        f"{combo or '该方案'} 当前¥{price:,.0f}，"
                        f"均值¥{avg_price:,.0f}，Z-score={z_score:.2f}"
                    ),
                },
                seen,
            )

        history_prices = _history_prices_for_combo(price_history, combo)
        if history_prices:
            history_min = min(history_prices)
            if price < history_min * 0.85:
                _add_anomaly(
                    anomalies,
                    {
                        "type": "疑似bug票价",
                        "severity": "alert",
                        "flight_combo": combo,
                        "price": price,
                        "reference_price": history_min,
                        "message": (
                            f"{combo or '该方案'} 当前¥{price:,.0f}，"
                            f"低于同航班历史最低¥{history_min:,.0f}超过15%"
                        ),
                    },
                    seen,
                )

        previous_price = _to_float(flight.get("previous_price"))
        if previous_price and previous_price > 0:
            change_pct = (price - previous_price) / previous_price * 100
            if abs(change_pct) > 30:
                _add_anomaly(
                    anomalies,
                    {
                        "type": "价格剧烈波动",
                        "severity": "warning",
                        "flight_combo": combo,
                        "price": price,
                        "previous_price": previous_price,
                        "change_pct": round(change_pct, 1),
                        "message": (
                            f"{combo or '该方案'} 从¥{previous_price:,.0f}"
                            f"变为¥{price:,.0f}，变化{change_pct:+.1f}%"
                        ),
                    },
                    seen,
                )

    grouped = {}
    for flight in valid_flights:
        combo = (flight.get("flight_combo") or "").replace(" ", "").upper()
        if not combo:
            continue
        grouped.setdefault(combo, [])
        source_entries = _source_price_entries(flight)
        if source_entries:
            for entry in source_entries:
                source_price = _to_float(entry.get("price"))
                if source_price is not None and source_price > 0:
                    grouped[combo].append(
                        {
                            "source": entry.get("source") or entry.get("data_source"),
                            "price": source_price,
                            "flight_combo": flight.get("flight_combo"),
                        }
                    )
        else:
            grouped[combo].append(
                {
                    "source": flight.get("data_source") or flight.get("source"),
                    "price": _to_float(flight.get("price")),
                    "flight_combo": flight.get("flight_combo"),
                }
            )

    for entries in grouped.values():
        entries = [entry for entry in entries if entry.get("price")]
        if len(entries) < 2:
            continue
        min_entry = min(entries, key=lambda item: item["price"])
        max_entry = max(entries, key=lambda item: item["price"])
        if min_entry["price"] <= 0:
            continue
        diff_pct = (max_entry["price"] - min_entry["price"]) / min_entry["price"] * 100
        if diff_pct > 20:
            _add_anomaly(
                anomalies,
                {
                    "type": "来源价格矛盾",
                    "severity": "warning",
                    "flight_combo": min_entry.get("flight_combo"),
                    "min_price": min_entry["price"],
                    "max_price": max_entry["price"],
                    "diff_pct": round(diff_pct, 1),
                    "message": (
                        f"{min_entry.get('flight_combo') or '同一航班'} 不同来源价差"
                        f"{diff_pct:.1f}%（¥{min_entry['price']:,.0f} - "
                        f"¥{max_entry['price']:,.0f}）"
                    ),
                    "sources": entries,
                },
                seen,
            )

    severity_order = {"alert": 0, "warning": 1, "info": 2}
    return sorted(
        anomalies,
        key=lambda item: (
            severity_order.get(item.get("severity"), 9),
            item.get("flight_combo") or "",
        ),
    )


def calculate_price_references(
    current_price, price_history, own_history, days_to_dept, current_flights
):
    """计算五层历史最低价参考"""
    result = {}

    if price_history:
        if isinstance(price_history[0], (list, tuple)):
            all_prices = [price for _, price in price_history if price and price > 0]
        else:
            all_prices = [price for price in price_history if price and price > 0]

        if all_prices:
            result["absolute_min"] = {
                "price": min(all_prices),
                "label": "历史最低（所有条件）",
                "note": "可能出现在淡季或特殊促销，当前条件下不一定可达",
            }

    if price_history and isinstance(price_history[0], (list, tuple)):
        from datetime import datetime

        relevant = []
        for timestamp, price in price_history:
            if price and price > 0 and timestamp:
                try:
                    hist_days = abs(timestamp - datetime.now().timestamp()) / 86400
                    if abs(hist_days - days_to_dept) <= 7:
                        relevant.append(price)
                except Exception:
                    pass

        if len(relevant) >= 5:
            result["conditional_min"] = {
                "price": min(relevant),
                "label": f"同条件最低（提前{days_to_dept}天±7天）",
                "note": "在类似购买时间点下的历史最低",
                "sample_size": len(relevant),
            }

    if own_history:
        recent_prices = [
            record.get("price", 0)
            for record in own_history
            if record.get("price") and record["price"] > 0
        ]
        if recent_prices:
            result["recent_min"] = {
                "price": min(recent_prices),
                "label": "近期最低（你关注以来）",
                "note": f"基于{len(recent_prices)}次采集数据",
                "sample_size": len(recent_prices),
            }

    if current_flights:
        current_prices = [
            flight.get("price", 0)
            for flight in current_flights
            if flight.get("price") and flight["price"] > 0
        ]
        if current_prices:
            result["current_min"] = {
                "price": min(current_prices),
                "label": "当前可买最低",
                "note": "此刻市场上满足条件的最低价",
            }

    for ref in result.values():
        diff = current_price - ref["price"]
        pct = round(diff / ref["price"] * 100, 1) if ref["price"] > 0 else 0
        ref["diff"] = diff
        ref["diff_pct"] = pct

    return result


def multi_window_analysis(current_price, own_history, google_history, days_to_dept):
    """多时间窗口纵向分析"""
    result = {}

    # 窗口一：短期趋势（3-7天）
    if own_history and len(own_history) >= 4:
        recent = [
            record["price"]
            for record in own_history[-14:]
            if record.get("price")
        ]  # 最近7天×每天2次=14条
        if len(recent) >= 4:
            split_index = len(recent) // 2
            first_half = sum(recent[:split_index]) / split_index
            second_half = sum(recent[split_index:]) / (len(recent) - split_index)
            change_pct = round((second_half - first_half) / first_half * 100, 1)

            if change_pct > 2:
                trend = "上涨中"
            elif change_pct < -2:
                trend = "下降中"
            else:
                trend = "平稳"

            result["short_term"] = {
                "window": "近3-7天",
                "trend": trend,
                "change_pct": change_pct,
                "high": max(recent),
                "low": min(recent),
                "data_points": len(recent),
            }

    # 窗口二：中期位置（14-30天）
    if own_history and len(own_history) >= 10:
        month_prices = [record["price"] for record in own_history if record.get("price")]
        if month_prices:
            below = sum(1 for price in month_prices if price < current_price)
            percentile = round(below / len(month_prices) * 100)
            avg_price = round(sum(month_prices) / len(month_prices))

            result["mid_term"] = {
                "window": "你关注以来",
                "percentile": percentile,
                "min": min(month_prices),
                "max": max(month_prices),
                "avg": avg_price,
                "data_points": len(month_prices),
                "vs_min": current_price - min(month_prices),
                "vs_avg": current_price - avg_price,
            }

    # 窗口三：长期分位（30-60天，用Google数据）
    if google_history:
        if isinstance(google_history[0], (list, tuple)):
            prices = [price for _, price in google_history if price and price > 0]
        else:
            prices = [price for price in google_history if price and price > 0]

        if len(prices) >= 10:
            below = sum(1 for price in prices if price < current_price)
            percentile = round(below / len(prices) * 100)

            result["long_term"] = {
                "window": "近60天历史",
                "percentile": percentile,
                "min": min(prices),
                "max": max(prices),
                "avg": round(sum(prices) / len(prices)),
                "data_points": len(prices),
            }

    return result


def nearby_dates_comparison(
    origin, dest, center_date, fetch_function, days_range=2
):
    """查询出发日前后几天的最低价，帮用户发现更便宜的日期"""
    from datetime import datetime, timedelta

    center = datetime.strptime(center_date, "%Y-%m-%d")
    results = {}

    for offset in range(-days_range, days_range + 1):
        check_date = center + timedelta(days=offset)
        date_str = check_date.strftime("%Y-%m-%d")
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        weekday = weekday_names[check_date.weekday()]

        results[date_str] = {
            "date": date_str,
            "weekday": weekday,
            "offset": offset,
            "min_price": None,
        }

    return results


def compare_flights(flight_a: dict, flight_b: dict) -> dict:
    """生成两个方案之间的直接对比"""
    price_diff = flight_a["price"] - flight_b["price"]
    time_diff = flight_a["total_duration_min"] - flight_b["total_duration_min"]
    stops_diff = flight_a["stops"] - flight_b["stops"]

    a_pros = []
    a_cons = []

    if price_diff < 0:
        a_pros.append(f"便宜 ¥{abs(price_diff):,.0f}")
    elif price_diff > 0:
        a_cons.append(f"贵 ¥{price_diff:,.0f}")

    if time_diff < 0:
        hours = abs(time_diff) // 60
        mins = abs(time_diff) % 60
        a_pros.append(f"快{hours}小时{mins}分钟")
    elif time_diff > 0:
        hours = time_diff // 60
        mins = time_diff % 60
        a_cons.append(f"慢{hours}小时{mins}分钟")

    if stops_diff < 0:
        a_pros.append(f"少转{abs(stops_diff)}次机")
    elif stops_diff > 0:
        a_cons.append(f"多转{stops_diff}次机")

    a_max_wait = max(
        (layover.get("wait_minutes", 0) for layover in flight_a.get("layovers", [])),
        default=0,
    )
    b_max_wait = max(
        (layover.get("wait_minutes", 0) for layover in flight_b.get("layovers", [])),
        default=0,
    )

    if a_max_wait > 480 and b_max_wait <= 480:
        a_cons.append("需要在机场过夜")
    if b_max_wait > 480 and a_max_wait <= 480:
        a_pros.append("不需要在机场过夜")

    return {
        "a_pros": a_pros,
        "a_cons": a_cons,
        "price_diff": price_diff,
        "time_diff_min": time_diff,
    }


SCORE_WEIGHTS = {
    "budget": {"price": 0.6, "duration": 0.15, "stops": 0.1, "layover": 0.15},
    "fast": {"price": 0.15, "duration": 0.5, "stops": 0.2, "layover": 0.15},
    "comfort": {"price": 0.15, "duration": 0.2, "stops": 0.3, "layover": 0.35},
    "balanced": {"price": 0.35, "duration": 0.25, "stops": 0.2, "layover": 0.2},
}


def overall_score(
    flight: dict, all_prices: list, all_durations: list, mode: str = "balanced"
) -> dict:
    """综合评分 0-10"""
    clean_prices = [_to_float(price) for price in all_prices or []]
    clean_prices = [price for price in clean_prices if price is not None]
    clean_durations = [_to_float(duration) for duration in all_durations or []]
    clean_durations = [
        duration for duration in clean_durations if duration is not None
    ]

    price = _to_float(flight.get("price"))
    duration = _to_float(flight.get("total_duration_min"))
    if price is None or duration is None or not clean_prices or not clean_durations:
        return {
            "total": 0,
            "price_score": 0,
            "duration_score": 0,
            "stops_score": 0,
            "layover_score": 0,
        }

    min_p, max_p = min(clean_prices), max(clean_prices)
    if max_p > min_p:
        price_score = 10 - (price - min_p) / (max_p - min_p) * 10
    else:
        price_score = 7

    min_d, max_d = min(clean_durations), max(clean_durations)
    if max_d > min_d:
        duration_score = 10 - (duration - min_d) / (max_d - min_d) * 10
    else:
        duration_score = 7

    stops = int(flight.get("stops") or 0)
    stops_score = {0: 10, 1: 8, 2: 5, 3: 3}.get(stops, 2)

    layover_score = 10
    for layover in flight.get("layovers", []) or []:
        wait = layover.get("wait_minutes", 0) or 0
        if wait > 480:
            layover_score -= 3
        elif wait > 240:
            layover_score -= 1.5
        elif wait < 60:
            layover_score -= 2
    layover_score = max(0, layover_score)

    weights = SCORE_WEIGHTS.get(mode, SCORE_WEIGHTS["balanced"])
    total = (
        price_score * weights["price"]
        + duration_score * weights["duration"]
        + stops_score * weights["stops"]
        + layover_score * weights["layover"]
    )

    return {
        "total": round(total, 1),
        "price_score": round(price_score, 1),
        "duration_score": round(duration_score, 1),
        "stops_score": round(stops_score, 1),
        "layover_score": round(layover_score, 1),
    }


def transfer_risk(flight: dict) -> dict:
    """转机风险评级：green/yellow/red"""
    if (flight.get("stops") or 0) == 0:
        return {"level": "green", "label": "✅ 直飞", "notes": []}

    risks = []
    level = "green"

    def raise_level(new_level: str) -> None:
        nonlocal level
        order = {"green": 0, "yellow": 1, "red": 2}
        if order[new_level] > order[level]:
            level = new_level

    us_airports = {
        "JFK",
        "LAX",
        "SFO",
        "ORD",
        "DFW",
        "ATL",
        "MIA",
        "SEA",
        "DTW",
        "IAH",
        "EWR",
    }

    for layover in flight.get("layovers", []) or []:
        wait = layover.get("wait_minutes", 0) or 0
        city = layover.get("city", "中转地")
        airport = layover.get("airport", "")

        if wait < 75:
            risks.append(f"⚠️ {city}转机仅{wait}分钟，国际航班可能不够")
            raise_level("red")
        elif wait < 120:
            risks.append(f"🟡 {city}转机{wait // 60}小时{wait % 60}分钟，需快速通关")
            raise_level("yellow")
        elif wait > 600:
            risks.append(f"🟡 {city}转机超过10小时，需要在机场过夜或外出住宿")
            raise_level("yellow")
        elif wait > 360:
            risks.append(f"🟡 {city}等待较长（{wait // 60}小时），建议了解机场休息设施")
            raise_level("yellow")

        if airport in us_airports:
            risks.append(f"ℹ️ 在美国{city}转机需要办理入境手续、提取行李重新托运")

    airlines = {
        segment.get("airline", "")
        for segment in flight.get("segments", []) or []
        if segment.get("airline")
    }
    if len(airlines) > 1:
        risks.append(f"ℹ️ 涉及{len(airlines)}家航司（{'、'.join(sorted(airlines))}），行李可能无法直挂")
        raise_level("yellow")

    label_map = {"green": "✅ 转机安全", "yellow": "🟡 需注意", "red": "🔴 风险较高"}
    return {"level": level, "label": label_map[level], "notes": risks}


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


def _normalize_priorities(priorities) -> dict:
    if not priorities:
        return {}
    if isinstance(priorities, dict):
        return priorities

    result = {}
    if isinstance(priorities, list):
        for item in priorities:
            if isinstance(item, dict):
                result.update(item)
    return result


def _flight_hours(flight: dict) -> float:
    if flight.get("total_hours") is not None:
        return float(flight.get("total_hours") or 0)
    return float(flight.get("total_duration_min") or 0) / 60


def _max_layover_minutes(flight: dict) -> int:
    return max(
        (int(layover.get("wait_minutes") or 0) for layover in flight.get("layovers", [])),
        default=0,
    )


def _format_hours(minutes: int) -> str:
    hours = minutes // 60
    mins = minutes % 60
    if mins:
        return f"{hours}小时{mins}分钟"
    return f"{hours}小时"


def _priority_violations(flight: dict, priorities: dict) -> list[str]:
    violations = []
    price = _to_float(flight.get("price")) or 0
    total_minutes = int(flight.get("total_duration_min") or 0)
    stops = int(flight.get("stops") or 0)
    max_wait = _max_layover_minutes(flight)

    budget = priorities.get("budget")
    if budget is not None and price > float(budget):
        violations.append(f"超出预算¥{price - float(budget):,.0f}")

    max_hours = priorities.get("max_hours")
    if max_hours is not None and total_minutes > int(float(max_hours) * 60):
        over_minutes = total_minutes - int(float(max_hours) * 60)
        violations.append(
            f"需要{_format_hours(total_minutes)}（超出时间限制{_format_hours(over_minutes)}）"
        )

    max_stops = priorities.get("max_stops")
    if max_stops is not None and stops > int(max_stops):
        violations.append(f"需要转机{stops}次（超出转机限制）")

    if priorities.get("no_overnight") and max_wait > 480:
        violations.append("有过夜转机")

    return violations


def _priority_boundary_notes(flight: dict, priorities: dict) -> list[str]:
    notes = []
    price = _to_float(flight.get("price")) or 0
    total_minutes = int(flight.get("total_duration_min") or 0)
    stops = int(flight.get("stops") or 0)
    max_wait = _max_layover_minutes(flight)

    budget = priorities.get("budget")
    if budget is not None:
        budget = float(budget)
        if budget * 0.95 <= price <= budget:
            notes.append("预算接近上限")

    max_hours = priorities.get("max_hours")
    if max_hours is not None:
        limit_minutes = int(float(max_hours) * 60)
        if limit_minutes - 60 <= total_minutes <= limit_minutes:
            notes.append("时间接近上限")

    max_stops = priorities.get("max_stops")
    if max_stops is not None and stops == int(max_stops):
        notes.append("转机次数刚好到上限")

    if priorities.get("no_overnight") and 360 <= max_wait <= 480:
        notes.append("转机等待较长但不过夜")

    return notes


def analyze_all_flights(
    flights: list[dict],
    price_insights: dict = None,
    mode: str = "balanced",
    priorities=None,
) -> dict:
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
    price_anomalies = detect_price_anomalies(usable_flights, price_insights)

    mode = mode if mode in SCORE_WEIGHTS else "balanced"

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
        flight["scores"] = overall_score(flight, prices, durations, mode)
        flight["transfer_risk"] = transfer_risk(flight)

    priority_config = _normalize_priorities(priorities)
    qualified_flights = []
    reference_flights = []
    if priority_config:
        for flight in by_price:
            violations = _priority_violations(flight, priority_config)
            boundary_notes = _priority_boundary_notes(flight, priority_config)
            flight["priority_violations"] = violations
            flight["priority_boundary_notes"] = boundary_notes
            if violations:
                reference_flights.append(flight)
            else:
                qualified_flights.append(flight)

    # 4. 按使用场景筛选推荐方案
    fastest_duration = by_duration[0]["total_duration_min"]

    def comfortable_layovers(flight: dict) -> bool:
        layovers = flight.get("layovers") or []
        if not layovers:
            return False
        return all(
            90 <= int(layover.get("wait_minutes") or 0) <= 240
            for layover in layovers
        )

    comfortable_candidates = [
        flight
        for flight in usable_flights
        if comfortable_layovers(flight)
        and flight["total_duration_min"] <= fastest_duration * 1.5
    ]
    if comfortable_candidates:
        most_comfortable = min(comfortable_candidates, key=lambda f: f["price"])
    else:
        most_comfortable = min(
            usable_flights,
            key=lambda f: (
                max(
                    (layover.get("wait_minutes", 0) for layover in f.get("layovers", [])),
                    default=0,
                )
                > 480,
                f["stops"],
                f["total_duration_min"],
                f["price"],
            ),
        )

    recommendations = [
        {
            "tag": "💰 预算有限选这个",
            "desc": "价格最低，但路上时间较长",
            "reason": "价格最低，但路上时间较长",
            "flight": by_price[0],
        },
        {
            "tag": "⏱️ 赶时间选这个",
            "desc": "到达最快，价格稍高",
            "reason": "到达最快，价格稍高",
            "flight": by_duration[0],
        },
        {
            "tag": "🛋️ 怕折腾选这个",
            "desc": "转机最轻松，不用在机场过夜",
            "reason": "转机最轻松，不用在机场过夜",
            "flight": most_comfortable,
        },
    ]

    market_context = {}
    if price_insights:
        market_context = {
            "lowest_market": price_insights.get("lowest_price"),
            "price_level": price_insights.get("price_level"),
            "typical_range": price_insights.get("typical_price_range"),
        }

    display_flights = []
    cabin_order = []
    for flight in usable_flights:
        cabin_class = flight.get("cabin_class") or "economy"
        if cabin_class not in cabin_order:
            cabin_order.append(cabin_class)
    for cabin_class in cabin_order:
        cabin_flights = [
            flight
            for flight in usable_flights
            if (flight.get("cabin_class") or "economy") == cabin_class
        ]
        display_flights.extend(sorted(cabin_flights, key=lambda f: f["price"])[:10])

    economy_flights = [
        flight
        for flight in usable_flights
        if (flight.get("cabin_class") or "economy") == "economy"
    ]
    business_flights = [
        flight
        for flight in usable_flights
        if (flight.get("cabin_class") or "economy") == "business"
    ]
    economy_recommendations, business_recommendation = select_recommendations(
        economy_flights, business_flights, mode
    )
    cabin_price_ranges = {}
    for cabin_class in cabin_order:
        cabin_prices = [
            flight["price"]
            for flight in usable_flights
            if (flight.get("cabin_class") or "economy") == cabin_class
        ]
        if cabin_prices:
            cabin_price_ranges[cabin_class] = [min(cabin_prices), max(cabin_prices)]

    return {
        "total_options": len(usable_flights),
        "recommendations": recommendations,
        "economy_recommendations": economy_recommendations,
        "business_recommendation": business_recommendation,
        "all_flights": display_flights,
        "price_range": [min(prices), max(prices)],
        "cabin_price_ranges": cabin_price_ranges,
        "duration_range": [min(durations), max(durations)],
        "market_context": market_context,
        "price_insights": price_insights,
        "price_anomalies": price_anomalies,
        "current_min_price": min(prices),
        "mode": mode,
        "priorities": priority_config,
        "qualified_flights": qualified_flights,
        "reference_flights": reference_flights,
    }


def select_recommendations(economy_flights, business_flights, mode: str = "balanced"):
    """筛选推送方案：经济舱最多4个 + 商务舱1个。"""
    def max_layover_minutes(flight: dict) -> int:
        return max(
            (int(layover.get("wait_minutes") or 0) for layover in flight.get("layovers", [])),
            default=0,
        )

    def sort_key(flight: dict):
        if mode == "budget":
            return (flight.get("price", 99999), flight.get("total_duration_min", 99999))
        if mode == "fast":
            return (flight.get("total_duration_min", 99999), flight.get("price", 99999))
        if mode == "comfort":
            return (
                flight.get("stops", 99),
                max_layover_minutes(flight),
                flight.get("total_duration_min", 99999),
                flight.get("price", 99999),
            )
        return (
            flight.get("value_score", 99999),
            flight.get("price", 99999),
            flight.get("total_duration_min", 99999),
        )

    eco_recs = []
    seen_routes = set()

    for flight in sorted(economy_flights, key=sort_key):
        route = flight.get("route_summary", "")
        if route not in seen_routes and len(eco_recs) < 4:
            eco_recs.append(flight)
            seen_routes.add(route)

    business_rec = None
    if business_flights:
        business_rec = min(
            business_flights, key=lambda item: item.get("price", 99999)
        )

    return eco_recs, business_rec
