"""Four-dimensional flight price decision framework."""

from __future__ import annotations

import statistics
import re
from datetime import date, datetime, time, timedelta

from price_estimator import calc_transaction_price
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


def apply_default_rules(subscription: dict) -> dict:
    """Apply safe defaults so quick-mode users still get advanced protection."""
    subscription = dict(subscription or {})
    soft = dict(subscription.get("soft_preferences") or {})
    goals = dict(subscription.get("notification_goals") or {})
    hard = dict(subscription.get("hard_constraints") or {})
    monitor_mode = subscription.get("monitor_mode", "quick")
    quick_mode = monitor_mode != "precise"
    defaults_applied = []

    time_pref = (
        soft.get("time_preference_mode")
        or soft.get("time_preference")
        or hard.get("time_preference_mode")
        or hard.get("time_preference")
    )
    if quick_mode or not time_pref:
        soft["time_preference"] = "no_redeye"
        soft["time_preference_mode"] = "no_redeye"
        soft["departure_time_windows"] = [["06:00", "23:00"]]
        soft["arrival_time_windows"] = [["06:00", "23:00"]]
        soft["red_eye_allowed"] = False
        soft["early_morning_allowed"] = True
        hard["time_preference"] = "no_redeye"
        hard["time_preference_mode"] = "no_redeye"
        hard["departure_time_policy"] = "no_redeye"
        if not hard.get("arrival_time_policy") or hard.get("arrival_time_policy") == "any":
            hard["arrival_time_policy"] = "no_midnight"
        defaults_applied.append("不推荐红眼/凌晨到达")

    if quick_mode and hard.get("baggage") == "required":
        defaults_applied.append("优先含托运行李方案")
    elif hard.get("baggage") in (None, "unknown"):
        hard["baggage_default"] = "prefer_included"
        defaults_applied.append("优先含托运行李方案")

    if quick_mode:
        soft["allow_self_transfer"] = False
        hard.setdefault("accept_self_transfer", False)
        defaults_applied.append("不推荐非联程中转")
    elif "allow_self_transfer" not in soft and "accept_self_transfer" in hard:
        soft["allow_self_transfer"] = bool(hard.get("accept_self_transfer"))
    elif "allow_self_transfer" not in soft:
        soft["allow_self_transfer"] = False
        hard.setdefault("accept_self_transfer", False)
        defaults_applied.append("不推荐非联程中转")

    if quick_mode:
        soft["allow_overnight_transfer"] = False
        hard.setdefault("accept_overnight_transfer", False)
        defaults_applied.append("不推荐过夜中转")
    elif "allow_overnight_transfer" not in soft and "accept_overnight_transfer" in hard:
        soft["allow_overnight_transfer"] = bool(hard.get("accept_overnight_transfer"))
    elif "allow_overnight_transfer" not in soft:
        soft["allow_overnight_transfer"] = False
        hard.setdefault("accept_overnight_transfer", False)
        defaults_applied.append("不推荐过夜中转")

    if not goals.get("secondary"):
        goals["secondary"] = ["low_price_alert", "price_rise_alert"]
        defaults_applied.append("提醒异常低价和涨价风险")

    if not goals.get("frequency"):
        goals["frequency"] = "important_only"
        defaults_applied.append("只在重要变化时提醒")

    subscription["soft_preferences"] = soft
    subscription["hard_constraints"] = hard
    subscription["notification_goals"] = goals
    subscription["defaults_applied"] = defaults_applied
    return subscription


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


def calc_buy_vs_wait_risk(
    current_price,
    price_history=None,
    days_to_dept=None,
    target_price=None,
    execution_grade=None,
) -> dict:
    """Compare the practical risk of buying now versus waiting."""
    current = _to_float(current_price)
    target = _to_float(target_price)
    if price_history and isinstance(price_history[0], (list, tuple)):
        prices = [price for _, price in price_history if price and price > 0]
    else:
        prices = [price for price in (price_history or []) if price and price > 0]
    try:
        days = int(days_to_dept) if days_to_dept is not None else None
    except (TypeError, ValueError):
        days = None

    buy_risks = [
        "可能遇到支付页跳价",
        "票规需确认（行李/退改）",
        "不同渠道售后政策不同",
    ]
    wait_risks = ["可能错过当前低价", "理想价再次出现不确定"]

    if days is not None and days <= 14:
        wait_risks.insert(1, "临近出发价格通常上涨")
        wait_level = "高"
    elif days is not None and days <= 30:
        wait_risks.insert(1, "出发窗口逐渐接近，价格上行风险增加")
        wait_level = "中"
    else:
        wait_level = "中"

    if execution_grade == "A":
        buy_level = "低"
    elif execution_grade in {"C", "D"}:
        buy_level = "高"
    else:
        buy_level = "中"

    low_position = False
    trend_text = "历史样本仍在积累"
    if prices and current is not None:
        avg_price = sum(prices) / len(prices)
        below = sum(1 for price in prices if price < current)
        percentile = below / len(prices) * 100
        low_position = percentile <= 35 or current <= avg_price
        if len(prices) >= 3:
            recent = prices[-5:] if len(prices) >= 5 else prices
            if recent[-1] < recent[0] * 0.98:
                trend_text = "近期仍有下降"
            elif recent[-1] > recent[0] * 1.02:
                trend_text = "近期价格走高"
            else:
                trend_text = "近期价格相对稳定"

    target_reached = bool(current is not None and target and current <= target)
    if target_reached and low_position:
        leaning = "倾向尽快验证购买"
        summary = "当前已接近理想价且处于低位，继续等的下行空间有限，倾向于尽快验证购买。"
    elif target_reached:
        leaning = "倾向验证购买"
        summary = "当前价格已达到理想价，主要需要确认支付页最终价格和票规。"
    elif trend_text == "近期仍有下降" and (days is None or days > 21):
        leaning = "可以短暂观察"
        summary = "价格仍有下降迹象且时间尚可，可以短暂观察，但需关注涨价风险。"
    else:
        leaning = "谨慎观察"
        summary = "当前价格或执行信息仍有不确定性，适合继续监控并等待更清晰信号。"

    return {
        "buy_level": buy_level,
        "wait_level": wait_level,
        "buy_risks": buy_risks,
        "wait_risks": wait_risks,
        "leaning": leaning,
        "summary": summary,
        "trend": trend_text,
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

LCC_AIRLINES = [
    "Spirit",
    "Frontier",
    "春秋航空",
    "九元航空",
    "Ryanair",
    "EasyJet",
    "AirAsia",
    "Scoot",
    "Peach",
    "Cebu Pacific",
    "IndiGo",
    "VietJet",
]

FULL_SERVICE_AIRLINES = [
    "Air China",
    "中国国航",
    "China Eastern",
    "东方航空",
    "China Southern",
    "南方航空",
    "United",
    "Delta",
    "American",
    "Air Canada",
    "Lufthansa",
    "ANA",
    "Japan Airlines",
    "Singapore Airlines",
    "Cathay Pacific",
]


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


def calc_transfer_risk(flight: dict) -> dict:
    """Evaluate execution risk for transfer-heavy itineraries."""
    risk_score = 0
    risk_factors = []
    segments = flight.get("segments", []) or []
    layovers = flight.get("layovers", []) or []

    stops = _stops_count(flight, default=0)
    if stops == 0:
        return {"level": "none", "label": "直飞", "score": 0, "factors": []}
    if stops >= 2:
        risk_score += 30
        risk_factors.append("多次中转")

    for layover in layovers:
        wait = layover.get("wait_minutes", 0) or 0
        if wait < 90:
            risk_score += 40
            risk_factors.append(f"中转时间仅{wait}分钟，可能赶不上")
        elif wait < 120:
            risk_score += 15
            risk_factors.append(f"中转时间{wait}分钟，较紧张")
        elif wait > 480:
            risk_score += 10
            risk_factors.append(f"中转等待{wait // 60}小时，较长")

    airlines = list(flight.get("airlines") or [])
    for segment in segments:
        airline = segment.get("airline") if isinstance(segment, dict) else ""
        if airline:
            airlines.append(airline)
    unique_airlines = sorted({airline for airline in airlines if airline})
    if len(unique_airlines) > 1:
        risk_score += 25
        risk_factors.append(f"跨航司({'/'.join(unique_airlines)})，可能非联程")

    international_transfer_airports = {
        "NRT", "HND", "ICN", "TPE", "HKG", "SIN", "BKK", "KUL", "DOH",
        "DXB", "IST", "AMS", "FRA", "CDG", "LHR",
    }
    for layover in layovers:
        airport = layover.get("airport", "")
        if airport in international_transfer_airports:
            risk_score += 15
            risk_factors.append(f"经{city_name(airport)}中转，请确认是否需要过境签")

    if risk_score >= 50:
        level = "high"
        label = "🔴 高风险"
    elif risk_score >= 25:
        level = "medium"
        label = "🟡 中风险"
    else:
        level = "low"
        label = "🟢 低风险"

    return {
        "level": level,
        "label": label,
        "score": risk_score,
        "factors": risk_factors,
    }


def transfer_risk(flight: dict) -> dict:
    """Backward-compatible wrapper for the newer transfer execution risk."""
    return calc_transfer_risk(flight)


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


def _is_likely_self_transfer(flight: dict) -> bool:
    if flight.get("self_transfer") or flight.get("separate_tickets"):
        return True
    if str(flight.get("ticketing", "")).lower() in {"self_transfer", "separate"}:
        return True
    airlines = [airline for airline in (flight.get("airlines") or []) if airline]
    if not airlines:
        airlines = [
            segment.get("airline")
            for segment in (flight.get("segments") or [])
            if isinstance(segment, dict) and segment.get("airline")
        ]
    return int(flight.get("stops") or 0) > 0 and len(set(airlines)) > 1


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


def _hour_from_time(value: str | None) -> int | None:
    text = str(value or "").replace("T", " ")
    if " " in text:
        text = text.split(" ", 1)[1]
    try:
        return int(text[:2])
    except (TypeError, ValueError):
        return None


def _first_departure_hour(flight: dict) -> int | None:
    segments = flight.get("segments") or []
    if not segments:
        return None
    return _hour_from_time(segments[0].get("dep_time"))


def _last_arrival_hour(flight: dict) -> int | None:
    segments = flight.get("segments") or []
    if not segments:
        return None
    return _hour_from_time(segments[-1].get("arr_time"))


TIME_SLOT_LABELS = {
    "early_morning": "早班",
    "morning": "上午",
    "afternoon": "下午",
    "evening": "傍晚",
    "night": "晚班",
    "redeye": "红眼",
}


def time_slot_from_hour(hour: int | None) -> str | None:
    if hour is None:
        return None
    if 6 <= hour < 9:
        return "dawn"
    if 9 <= hour < 12:
        return "morning"
    if 12 <= hour < 14:
        return "noon"
    if 14 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 20:
        return "evening"
    if 20 <= hour < 23:
        return "night"
    return "redeye"


def _normalize_time_slots(slots) -> set[str]:
    if not slots:
        return set()
    if isinstance(slots, str):
        slots = [slots]
    normalized = set()
    for slot in slots:
        value = str(slot or "").strip()
        if not value:
            continue
        if value == "early_morning":
            value = "dawn"
        normalized.add(value)
    return normalized


def _matches_time_slots(hour: int | None, slots) -> bool:
    allowed = _normalize_time_slots(slots)
    if hour is None or not allowed:
        return True
    return time_slot_from_hour(hour) in allowed


def _direction_time_slots(preferences: dict, direction: str) -> tuple[object, object]:
    if direction == "return":
        dep_slots = preferences.get("return_departure_slots")
        arr_slots = preferences.get("return_arrival_slots")
    else:
        dep_slots = preferences.get("outbound_departure_slots")
        arr_slots = preferences.get("outbound_arrival_slots")

    dep_slots = (
        dep_slots
        or preferences.get("departure_slots")
        or preferences.get("preferred_departure_slots")
    )
    arr_slots = (
        arr_slots
        or preferences.get("arrival_slots")
        or preferences.get("preferred_arrival_slots")
    )
    return dep_slots, arr_slots


def _is_red_eye(flight: dict) -> bool:
    dep_hour = _first_departure_hour(flight)
    arr_hour = _last_arrival_hour(flight)
    return any(
        hour is not None and (hour >= 23 or hour < 6)
        for hour in (dep_hour, arr_hour)
    )


def _matches_departure_policy(flight: dict, policy: str) -> bool:
    hour = _first_departure_hour(flight)
    if hour is None or policy == "any":
        return True
    if policy == "after_06":
        return hour >= 6
    if policy == "daytime":
        return 8 <= hour <= 20
    if policy == "no_redeye":
        return not (hour >= 23 or hour < 6)
    return True


def _matches_arrival_policy(flight: dict, policy: str) -> bool:
    hour = _last_arrival_hour(flight)
    if hour is None or policy == "any":
        return True
    if policy == "no_midnight":
        return not (0 <= hour < 6)
    if policy == "daytime_only":
        return 6 <= hour <= 22
    return True


def _is_daytime_flight(flight: dict) -> bool:
    dep_hour = _first_departure_hour(flight)
    arr_hour = _last_arrival_hour(flight)
    dep_ok = dep_hour is None or 8 <= dep_hour <= 20
    arr_ok = arr_hour is None or 6 <= arr_hour <= 22
    return dep_ok and arr_ok


def _time_to_minutes(value) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    match = re.search(r"(\d{1,2}):(\d{2})", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    return hour * 60 + minute


def _hour_to_minutes(hour: int | None) -> int | None:
    return None if hour is None else int(hour) * 60


def _matches_time_windows(hour: int | None, windows) -> bool:
    if hour is None or not windows:
        return True
    minute = _hour_to_minutes(hour)
    if minute is None:
        return True
    for window in windows:
        if not isinstance(window, (list, tuple)) or len(window) < 2:
            continue
        start = _time_to_minutes(window[0])
        end = _time_to_minutes(window[1])
        if start is None or end is None:
            continue
        if start <= end:
            if start <= minute < end:
                return True
        elif minute >= start or minute < end:
            return True
    return False


def _direction_time_windows(preferences: dict, direction: str) -> tuple[object, object]:
    if direction == "return":
        dep_windows = preferences.get("return_departure_time_windows")
        arr_windows = preferences.get("return_arrival_time_windows")
    else:
        dep_windows = preferences.get("outbound_departure_time_windows")
        arr_windows = preferences.get("outbound_arrival_time_windows")
    return (
        dep_windows or preferences.get("departure_time_windows") or [],
        arr_windows or preferences.get("arrival_time_windows") or [],
    )


def match_time_preference(flight: dict, soft_prefs: dict) -> tuple[bool, str]:
    mode = (
        soft_prefs.get("time_preference_mode")
        or soft_prefs.get("time_preference")
        or "unlimited"
    )
    mode = "unlimited" if mode == "any" else mode
    if mode == "unlimited":
        return True, ""

    dep_hour = _first_departure_hour(flight)
    arr_hour = _last_arrival_hour(flight)
    dep_red_eye = dep_hour is not None and (dep_hour >= 23 or dep_hour < 6)
    arr_red_eye = arr_hour is not None and (arr_hour >= 23 or arr_hour < 6)

    if mode == "daytime":
        is_daytime = (
            (dep_hour is None or 6 <= dep_hour < 20)
            and (arr_hour is None or 6 <= arr_hour < 20)
        )
        return True, "白天航班" if is_daytime else "非白天，排序降权"

    if mode == "no_redeye":
        if dep_red_eye or arr_red_eye:
            return False, "红眼/凌晨航班，已排除"
        return True, ""

    if mode == "custom":
        direction = soft_prefs.get("direction", "outbound")
        dep_windows, arr_windows = _direction_time_windows(soft_prefs, direction)
        dep_ok = _matches_time_windows(dep_hour, dep_windows)
        arr_ok = _matches_time_windows(arr_hour, arr_windows)
        if dep_ok and arr_ok:
            return True, ""
        return False, "不在你设置的可接受时段内"

    return True, ""


def _has_free_checked_baggage(flight: dict) -> bool:
    fare_rules = flight.get("fare_rules") or {}
    baggage = fare_rules.get("baggage") or {}
    if baggage.get("checked_pieces") or baggage.get("checked_kg"):
        return True

    extra = flight.get("extra") or {}
    detail = extra.get("baggage_detail") or {}
    checked = detail.get("checked") or {}
    if checked.get("quantity", 0) > 0 and checked.get("is_free", False):
        return True

    return bool(extra.get("baggage"))


def _has_refund_change_flexibility(flight: dict, required: bool = False) -> bool:
    fare_rules = flight.get("fare_rules") or {}
    change = fare_rules.get("change") or {}
    refund = fare_rules.get("refund") or {}
    extra = flight.get("extra") or {}
    refund_change = extra.get("refund_change") or {}

    changeable = bool(
        change.get("allowed")
        or refund_change.get("changeable")
        or extra.get("changeable")
    )
    refundable = bool(
        refund.get("allowed")
        or refund_change.get("refundable")
        or extra.get("refundable")
    )
    return changeable and refundable if required else changeable


def _airline_text(flight: dict) -> str:
    names = []
    for key in ("airline_summary", "airline"):
        if flight.get(key):
            names.append(str(flight.get(key)))
    names.extend(str(name) for name in flight.get("airlines") or [] if name)
    for segment in flight.get("segments") or []:
        if isinstance(segment, dict) and segment.get("airline"):
            names.append(str(segment.get("airline")))
    return " ".join(names)


def _contains_any_airline(flight: dict, airline_names: list[str]) -> bool:
    text = _airline_text(flight).lower()
    return any(name.lower() in text for name in airline_names if name)


def _trip_mode(default_mode: str, preferences: dict | None) -> str:
    price_sensitivity = (preferences or {}).get("price_sensitivity")
    if price_sensitivity == "max":
        return "budget"
    if price_sensitivity == "low":
        return "comfort"
    trip_type = (preferences or {}).get("trip_type")
    if trip_type == "business_meeting":
        return "fast"
    if trip_type == "tourism":
        return "budget"
    if trip_type in {"family_elder", "family_visit"}:
        return "comfort"
    return default_mode


def _cheapest_price(flights: list[dict]) -> float | None:
    prices = [_to_float(flight.get("price")) for flight in flights]
    prices = [price for price in prices if price is not None and price > 0]
    return min(prices) if prices else None


def _stops_count(flight: dict, default: int = 99) -> int:
    """Return stop count; missing/blank values are treated as unknown, not direct."""
    value = flight.get("stops", default)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _collected_minutes_ago(flight: dict) -> float | None:
    raw_value = (
        flight.get("collected_at")
        or flight.get("snapshot_time")
        or flight.get("fetched_at")
    )
    if not raw_value:
        return None
    try:
        collected_at = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except ValueError:
        return None
    now = datetime.now(collected_at.tzinfo) if collected_at.tzinfo else datetime.now()
    return max(0, (now - collected_at).total_seconds() / 60)


def verify_fare_rules(flight, hard_constraints):
    issues = []
    matches = []
    hard_constraints = hard_constraints or {}

    baggage_req = hard_constraints.get("baggage", "unknown")
    fare_rules = flight.get("fare_rules", {}) or {}
    baggage_info = fare_rules.get("baggage", {}) or {}
    checked_kg = baggage_info.get("checked_kg", 0) or 0
    checked_pieces = baggage_info.get("checked_pieces", 0) or 0

    if baggage_req == "required":
        if checked_pieces > 0 or checked_kg > 0:
            matches.append(f"✅ 含托运行李 {checked_kg}kg/{checked_pieces}件")
        elif fare_rules:
            issues.append("⚠️ 不含免费托运行李，需额外购买")
        else:
            issues.append("❓ 托运行李信息未确认，购买前请核实")

    refund_pref = hard_constraints.get("refund_flexibility", "unknown")
    refund_info = fare_rules.get("refund", {}) or {}
    change_info = fare_rules.get("change", {}) or {}

    if refund_pref == "must_refundable":
        if refund_info.get("allowed"):
            fee = refund_info.get("fee", "未知")
            matches.append(f"✅ 可退票（手续费: {fee}）")
        elif refund_info:
            issues.append("⚠️ 该票不可退，与你的要求不符")
        else:
            issues.append("❓ 退票规则未确认，购买前请核实")

    if refund_pref in ("preferred", "must_refundable"):
        if change_info.get("allowed"):
            matches.append("✅ 可改签")
        elif change_info:
            issues.append("⚠️ 该票不可改签")
        else:
            issues.append("❓ 改签规则未确认")

    cabin = flight.get("cabin_class", "economy")
    if cabin in ("basic_economy", "light"):
        issues.append("⚠️ 基础经济舱/轻选舱，可能不含行李、不可选座、不可退改")

    airlines = flight.get("airlines", []) or []
    if flight.get("stops", 0) > 0:
        if len(set(airlines)) > 1:
            issues.append("⚠️ 跨航司中转，可能为非联程票，需确认")
        else:
            matches.append("✅ 同航司中转，大概率联程票")

    if not issues:
        match_level = "full"
        match_label = "🟢 票规完全匹配"
    elif len(issues) <= len(matches):
        match_level = "partial"
        match_label = "🟡 票规部分匹配"
    else:
        match_level = "mismatch"
        match_label = "🔴 票规需确认"

    return {
        "level": match_level,
        "label": match_label,
        "matches": matches,
        "issues": issues,
    }


def estimate_availability(flight, collected_at=None):
    status = "unknown"
    label = "❓ 未验证"

    age_minutes = _collected_minutes_ago(
        {**flight, "collected_at": collected_at or flight.get("collected_at")}
    )
    if age_minutes is None:
        age_minutes = 9999

    sources = flight.get("data_source", "") or flight.get("source", "")
    source_count = len([source for source in str(sources).split("+") if source])
    price = _to_float(flight.get("price")) or 0

    if age_minutes <= 30 and source_count >= 2 and price > 0:
        status = "likely_available"
        label = "🟢 大概率可购买"
    elif age_minutes <= 120 and price > 0:
        status = "possibly_available"
        label = "🟡 可能可购买"
    elif age_minutes > 120:
        status = "needs_refresh"
        label = "🟠 建议刷新确认"

    if price <= 0:
        status = "invalid"
        label = "🔴 价格异常"

    return {
        "status": status,
        "label": label,
        "age_minutes": int(age_minutes),
        "source_count": source_count,
    }


def calc_execution_risk(flight):
    score = 0
    factors = []

    avail = flight.get("availability", {}) or {}
    age = avail.get("age_minutes", 9999)
    if age > 120:
        score += 30
        factors.append("价格超过2小时未验证")
    elif age > 30:
        score += 15
        factors.append("价格30分钟前采集")

    fare = flight.get("fare_verification", {}) or {}
    if fare.get("level") == "mismatch":
        score += 25
        factors.append("票规与需求不匹配")
    elif fare.get("level") == "partial":
        score += 12
        factors.append("票规部分未确认")

    transfer = flight.get("transfer_risk", {}) or {}
    if transfer.get("level") == "high":
        score += 25
        factors.append("中转执行风险高")
    elif transfer.get("level") == "medium":
        score += 12
        factors.append("中转有一定风险")

    source_count = avail.get("source_count", 0)
    if source_count == 0:
        score += 20
        factors.append("无数据源验证")
    elif source_count == 1:
        score += 10
        factors.append("仅单一数据源")

    if score >= 50:
        risk_level = "high"
        risk_label = "🔴 执行风险高"
        advice = "该方案存在较多不确定因素，建议谨慎对待或等待更可靠的方案"
    elif score >= 25:
        risk_level = "medium"
        risk_label = "🟡 执行风险中等"
        advice = "建议购买前仔细核对支付页的价格、行李和退改规则"
    else:
        risk_level = "low"
        risk_label = "🟢 执行风险低"
        advice = "该方案信息较完整，可信度较高"

    flight["execution_risk"] = {
        "level": risk_level,
        "label": risk_label,
        "score": score,
        "factors": factors,
        "advice": advice,
    }
    return flight["execution_risk"]


def calc_execution_grade(flight: dict, hard_constraints=None) -> dict:
    """Calculate whether a shown option is actionable enough to execute."""
    hard_constraints = hard_constraints or {}
    reasons = []
    price = _to_float(flight.get("price")) or 0
    risk = flight.get("execution_risk") or calc_execution_risk(flight)
    fare = flight.get("fare_verification") or {}
    price_advice = flight.get("price_advice") or {}
    transfer = flight.get("transfer_risk") or calc_transfer_risk(flight)
    companions = hard_constraints.get("companions")

    reasons.extend(risk.get("factors") or [])
    if fare.get("issues"):
        reasons.extend(fare.get("issues")[:2])

    if price <= 0 or price_advice.get("level") == "over_budget":
        grade = "D"
        grade_label = "❌ 不推荐"
    elif companions in {"with_elderly", "with_child", "with_elderly_child", "with_both"} and transfer.get("level") == "high":
        grade = "D"
        grade_label = "❌ 不推荐（中转风险高，不适合老人/小孩）"
        reasons.append("中转风险高，不适合老人/小孩")
    elif risk.get("level") == "low" and price_advice.get("level") in {"below_target", "within_tolerance"}:
        grade = "A"
        grade_label = "A级 - 强烈建议"
    elif risk.get("level") == "medium" or price_advice.get("level") == "within_budget":
        grade = "B"
        grade_label = "B级 - 建议确认后购买"
    elif risk.get("level") == "high" or fare.get("level") == "mismatch":
        grade = "C"
        grade_label = "C级 - 仅供参考"
    elif risk.get("level") == "low":
        grade = "A"
        grade_label = "A级 - 强烈建议"
    else:
        grade = "B"
        grade_label = "B级 - 建议确认后购买"

    score = max(0, 100 - int(risk.get("score", 0)))

    flight["execution_grade"] = grade
    flight["execution_label"] = grade_label
    flight["execution_reasons"] = reasons
    flight["execution_score"] = score
    return {
        "grade": grade,
        "label": grade_label,
        "reasons": reasons,
        "score": score,
    }


def _confidence_level_label(value: str) -> str:
    return value if value in {"高", "中", "低"} else "低"


def calc_confidence(flight: dict, source_stats=None, price_history=None) -> dict:
    """Break decision confidence into user-readable dimensions."""
    flight = flight or {}
    source_stats = source_stats or {}
    dimensions = {}
    details = {}

    age = (flight.get("availability") or {}).get("age_minutes", 9999)
    try:
        age = int(age)
    except (TypeError, ValueError):
        age = 9999
    dimensions["价格新鲜度"] = "高" if age <= 30 else "中" if age <= 120 else "低"
    details["价格新鲜度"] = f"{age}分钟前采集" if age < 9999 else "采集时间未知"

    history_count = len(price_history) if price_history else 0
    dimensions["历史样本量"] = "高" if history_count >= 14 else "中" if history_count >= 5 else "低"
    details["历史样本量"] = f"近期{history_count}次采集"

    source_count = (flight.get("availability") or {}).get("source_count", 0)
    if not source_count:
        data_source = str(flight.get("data_source") or flight.get("source") or "")
        source_count = len([item for item in data_source.split("+") if item])
    if not source_count and source_stats:
        source_count = sum(
            1
            for value in source_stats.values()
            if isinstance(value, dict) and "成功" in str(value.get("status", ""))
        )
    dimensions["渠道一致性"] = "高" if source_count >= 3 else "中" if source_count >= 2 else "低"
    details["渠道一致性"] = f"{source_count}个数据源一致" if source_count else "数据源不足"

    fare = flight.get("fare_verification") or {}
    fare_level = fare.get("level")
    dimensions["票规完整度"] = (
        "高" if fare_level == "full" else "中" if fare_level == "partial" else "低"
    )
    details["票规完整度"] = "票规已确认" if fare_level == "full" else "行李退改待确认"

    avail = flight.get("availability") or {}
    avail_status = avail.get("status", "unknown")
    dimensions["可购买性"] = (
        "高"
        if avail_status == "likely_available"
        else "中"
        if avail_status == "possibly_available"
        else "低"
    )
    details["可购买性"] = avail.get("label") or "待支付页验证"

    high_count = sum(1 for value in dimensions.values() if _confidence_level_label(value) == "高")
    if high_count >= 4:
        overall = "高"
    elif high_count >= 2:
        overall = "中高"
    else:
        overall = "中"

    return {"overall": overall, "dimensions": dimensions, "details": details}


def generate_decision_summary(
    lowest_price,
    target_price,
    max_budget,
    confidence=None,
    execution_grade=None,
) -> dict:
    """Generate a compact decision summary for the notification top card."""
    lowest = _to_float(lowest_price)
    target = _to_float(target_price)
    max_b = _to_float(max_budget)
    confidence = confidence or {}
    execution_grade = execution_grade or "C"

    if lowest is None:
        price_judgment = "暂无有效价格"
    elif target and lowest <= target:
        price_judgment = "偏低，已达理想价"
    elif target and lowest <= target * 1.05:
        price_judgment = "接近理想价"
    elif max_b and lowest <= max_b:
        price_judgment = "在预算内但高于理想价"
    elif max_b and lowest > max_b:
        price_judgment = "超出预算"
    else:
        price_judgment = "需要结合历史价格判断"

    if execution_grade == "A":
        exec_judgment = "信息完整，可购买"
    elif execution_grade == "B":
        exec_judgment = "购买前需确认价格和票规"
    else:
        exec_judgment = "购买渠道或票规待确认"

    if price_judgment.startswith("偏低") and execution_grade == "A":
        conclusion = "强烈建议购买"
    elif "接近理想价" in price_judgment or "偏低" in price_judgment:
        conclusion = "可以购买前验证"
    elif "预算内" in price_judgment:
        conclusion = "可以观察"
    else:
        conclusion = "建议等待"

    if lowest and target:
        verify_limit = target * 1.05
    elif lowest:
        verify_limit = lowest * 1.05
    else:
        verify_limit = None

    if conclusion in {"强烈建议购买", "可以购买前验证"} and verify_limit:
        action_advice = f"若支付页最终价≤¥{verify_limit:,.0f}且含托运行李，可以购买"
    elif max_b:
        action_advice = f"若最终价仍低于¥{max_b:,.0f}，可按刚需程度决定"
    else:
        action_advice = "先验证支付页最终价、行李和退改规则"

    reasons = []
    if lowest and target:
        if lowest <= target:
            reasons.append(f"当前价格¥{lowest:,.0f}已达到理想入手价")
        elif lowest <= target * 1.05:
            reasons.append(f"当前价格¥{lowest:,.0f}已接近理想入手价")
        else:
            reasons.append(f"当前价格¥{lowest:,.0f}高于理想入手价")
    elif lowest:
        reasons.append(f"当前价格为¥{lowest:,.0f}")
    reasons.append(f"执行判断：{exec_judgment}")
    if confidence.get("overall"):
        reasons.append(f"数据置信度：{confidence['overall']}")

    return {
        "conclusion": conclusion,
        "price_judgment": price_judgment,
        "execution_judgment": exec_judgment,
        "action_advice": action_advice,
        "confidence": confidence.get("overall", "中"),
        "reasons": reasons[:3],
    }


def _flatten_price_history(price_history) -> list[float]:
    """Normalize price history formats into valid positive prices."""
    prices = []
    if isinstance(price_history, dict):
        price_history = price_history.get("price_history") or price_history.get("history") or []
    for item in price_history or []:
        value = item[1] if isinstance(item, (list, tuple)) and len(item) >= 2 else item
        price = _to_float(value)
        if price and price > 0:
            prices.append(price)
    return prices


def determine_push_type(
    current_price,
    target_price=None,
    max_budget=None,
    price_history=None,
    days_to_dept=None,
    last_push_price=None,
    analysis_result=None,
) -> dict:
    """Determine the action-notification type and user-facing trigger reasons."""
    current = _to_float(current_price)
    target = _to_float(target_price)
    max_b = _to_float(max_budget)
    last_price = _to_float(last_push_price)
    analysis_result = analysis_result or {}
    prices = sorted(_flatten_price_history(price_history))

    percentile = None
    historical_30 = None
    if current is not None and prices:
        below = sum(1 for price in prices if price < current)
        percentile = round(below / len(prices) * 100)
        index = min(len(prices) - 1, max(0, round((len(prices) - 1) * 0.30)))
        historical_30 = prices[index]

    push_type = "同日更优方案"
    if current is None:
        push_type = "价格已失效"
    elif _has_stale_primary_price(analysis_result):
        push_type = "价格已失效"
    elif historical_30 and current <= historical_30:
        push_type = "异常低价"
    elif target and current <= target:
        push_type = "进入低价区间"
    elif _has_cheaper_nearby_date(analysis_result, current):
        push_type = "前后日期更便宜"
    elif _has_better_same_day_option(analysis_result):
        push_type = "同日更优方案"
    elif _is_price_rise_risk(days_to_dept, analysis_result):
        push_type = "涨价风险"

    reasons = []
    if target and current is not None:
        if current <= target:
            reasons.append("当前价格进入你的理想入手区间")
        else:
            reasons.append(f"当前距离理想入手价还差¥{current - target:,.0f}")
    if last_price and current is not None:
        diff = current - last_price
        if diff < 0:
            reasons.append(f"比上次提醒下降¥{abs(diff):,.0f}")
        elif diff > 0:
            reasons.append(f"比上次提醒上涨¥{diff:,.0f}")
        else:
            reasons.append("与上次提醒价格持平")
    if percentile is not None:
        if percentile <= 30:
            reasons.append(f"低于相似历史价格的{100 - percentile}%")
        elif percentile >= 70:
            reasons.append(f"高于相似历史价格的{percentile}%")
    reasons.extend(_matched_constraint_reasons(analysis_result))
    if _is_price_rise_risk(days_to_dept, analysis_result):
        days_text = f"{days_to_dept}天" if days_to_dept is not None else "临近出发"
        reasons.append(f"距出发{days_text}，低价继续变化的风险上升")

    price_change = None
    if last_price and current is not None:
        diff = current - last_price
        price_change = {
            "last": last_price,
            "current": current,
            "diff": diff,
            "direction": "down" if diff < 0 else "up" if diff > 0 else "flat",
        }

    return {
        "type": push_type,
        "reasons": _dedupe_text(reasons)[:4],
        "price_change": price_change,
        "percentile": percentile,
        "historical_30_price": historical_30,
    }


def _dedupe_text(items: list[str]) -> list[str]:
    result = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _has_stale_primary_price(analysis_result: dict) -> bool:
    candidates = []
    if analysis_result.get("all_flights"):
        candidates.extend(analysis_result.get("all_flights") or [])
    round_trip = analysis_result.get("round_trip_analysis") or {}
    for combo in round_trip.get("combinations") or []:
        candidates.extend([combo.get("outbound") or {}, combo.get("return") or {}])
    for flight in candidates[:3]:
        age = ((flight or {}).get("availability") or {}).get("age_minutes")
        try:
            if age is not None and int(age) > 120:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _has_cheaper_nearby_date(analysis_result: dict, current: float | None) -> bool:
    if current is None:
        return False
    candidates = []
    nearby = analysis_result.get("nearby_dates") or analysis_result.get("nearby_date_prices") or {}
    if isinstance(nearby, dict):
        candidates = nearby.values()
    elif isinstance(nearby, list):
        candidates = nearby
    for item in candidates:
        if isinstance(item, dict):
            price = _to_float(item.get("min_price") or item.get("price"))
        else:
            price = _to_float(item)
        if price and price < current:
            return True
    return False


def _has_better_same_day_option(analysis_result: dict) -> bool:
    flights = analysis_result.get("all_flights") or []
    if len(flights) < 2:
        return False
    valid = [flight for flight in flights if _to_float(flight.get("price"))]
    if len(valid) < 2:
        return False
    sorted_by_price = sorted(valid, key=lambda f: _to_float(f.get("price")) or 999999)
    best = sorted_by_price[0]
    second = sorted_by_price[1]
    best_grade = best.get("execution_grade")
    second_grade = second.get("execution_grade")
    return best_grade in {"A", "B"} and second_grade not in {"A", "B"}


def _is_price_rise_risk(days_to_dept, analysis_result: dict) -> bool:
    try:
        days = int(days_to_dept) if days_to_dept is not None else int(analysis_result.get("days_to_dept", 999))
    except (TypeError, ValueError):
        days = 999
    if days <= 14:
        return True
    risk = analysis_result.get("waiting_risk") or {}
    up_prob = _to_float(risk.get("up_probability"))
    down_prob = _to_float(risk.get("down_probability"))
    return bool(up_prob and down_prob is not None and up_prob > down_prob)


def _matched_constraint_reasons(analysis_result: dict) -> list[str]:
    flights = analysis_result.get("all_flights") or []
    if not flights:
        round_trip = analysis_result.get("round_trip_analysis") or {}
        combos = round_trip.get("combinations") or []
        if combos:
            flights = [combos[0].get("outbound") or {}, combos[0].get("return") or {}]
    reasons = []
    first = flights[0] if flights else {}
    if first.get("stops", 0) == 0:
        reasons.append("符合你设置的直飞条件")
    fare = first.get("fare_verification") or {}
    matches = " ".join(fare.get("matches") or [])
    if "托运" in matches or "行李" in matches:
        reasons.append("符合你设置的托运行李要求")
    return reasons


def _apply_user_preferences(
    flights: list[dict], preferences: dict | None
) -> tuple[list[dict], list[dict], dict]:
    preferences = preferences or {}
    direct_only = preferences.get("direct_only", "flexible")
    transfer_policy = preferences.get("transfer_policy", "reasonable")
    direct_required = direct_only in {"must", "direct_only", "must_direct"} or transfer_policy in {
        "must",
        "direct_only",
        "must_direct",
    }
    red_eye = preferences.get("red_eye", "reject")
    departure_time_policy = preferences.get("departure_time_policy", "any")
    arrival_time_policy = preferences.get("arrival_time_policy", "any")
    time_preference_mode = (
        preferences.get("time_preference_mode")
        or preferences.get("time_preference")
        or "unlimited"
    )
    time_preference_mode = "unlimited" if time_preference_mode == "any" else time_preference_mode
    use_legacy_time_filters = time_preference_mode not in {
        "unlimited",
        "daytime",
        "no_redeye",
        "custom",
    }
    direction = preferences.get("direction", "outbound")
    preferred_departure_slots, preferred_arrival_slots = _direction_time_slots(
        preferences, direction
    )
    need_baggage = preferences.get("need_baggage", "unknown")
    refund_flexibility = preferences.get("refund_flexibility", "unknown")
    companions = preferences.get("companions", "solo")
    price_sensitivity = preferences.get("price_sensitivity", "low")
    trip_rigidity = preferences.get("trip_rigidity", "confirmed")
    airline_policy = preferences.get("airline_policy", "any")
    exclude_airlines = preferences.get("exclude_airlines") or []
    max_budget = _to_float(preferences.get("max_budget"))
    budget = max_budget if max_budget is not None else _to_float(preferences.get("budget"))
    target_price = _to_float(preferences.get("target_price"))
    price_tolerance = _to_float(preferences.get("price_tolerance"))
    if price_tolerance is None:
        price_tolerance = 100
    max_extra_duration_hours = _to_float(preferences.get("max_extra_duration_hours"))
    max_total_duration_hours = _to_float(preferences.get("max_total_duration_hours"))
    allow_overnight_transfer = bool(preferences.get("allow_overnight_transfer"))
    if "allow_overnight_transfer" not in preferences:
        allow_overnight_transfer = bool(preferences.get("accept_overnight_transfer"))
    allow_self_transfer = bool(preferences.get("allow_self_transfer"))
    if "allow_self_transfer" not in preferences:
        allow_self_transfer = bool(preferences.get("accept_self_transfer"))
    if transfer_policy == "reasonable" and max_extra_duration_hours is None and max_total_duration_hours is None:
        max_extra_duration_hours = 6

    direct_flights = [flight for flight in flights if _stops_count(flight) == 0]
    non_red_eye_flights = [flight for flight in flights if not _is_red_eye(flight)]
    cheapest_direct = _cheapest_price(direct_flights)
    cheapest_non_red_eye = _cheapest_price(non_red_eye_flights)
    direct_durations = [
        int(flight.get("total_duration_min") or 0)
        for flight in direct_flights
        if int(flight.get("total_duration_min") or 0) > 0
    ]
    all_durations = [
        int(flight.get("total_duration_min") or 0)
        for flight in flights
        if int(flight.get("total_duration_min") or 0) > 0
    ]
    duration_baseline = min(direct_durations or all_durations) if all_durations else None
    duration_limit_minutes = None
    if max_total_duration_hours:
        duration_limit_minutes = int(max_total_duration_hours * 60)
    elif max_extra_duration_hours is not None and duration_baseline:
        duration_limit_minutes = int(duration_baseline + max_extra_duration_hours * 60)

    kept = []
    excluded = []
    direct_reference_candidates = []
    for flight in flights:
        notes = list(flight.get("preference_notes") or [])
        penalties = list(flight.get("preference_penalties") or [])
        penalty = 0
        stops = _stops_count(flight)
        price = _to_float(flight.get("price")) or 0

        time_ok, time_note = match_time_preference(flight, preferences)
        if not time_ok:
            excluded.append({**flight, "exclude_reason": time_note or "时间不符合订阅偏好"})
            continue
        if time_note == "非白天，排序降权":
            penalty += 1
            penalties.append(time_note)
        elif time_note:
            notes.append(time_note)

        if use_legacy_time_filters and preferred_departure_slots and not _matches_time_slots(
            _first_departure_hour(flight), preferred_departure_slots
        ):
            excluded.append({**flight, "exclude_reason": "起飞时段不符合订阅偏好"})
            continue
        if use_legacy_time_filters and not preferred_departure_slots and not _matches_departure_policy(flight, departure_time_policy):
            excluded.append({**flight, "exclude_reason": "起飞时间不符合订阅偏好"})
            continue
        if use_legacy_time_filters and preferred_arrival_slots and not _matches_time_slots(
            _last_arrival_hour(flight), preferred_arrival_slots
        ):
            excluded.append({**flight, "exclude_reason": "到达时段不符合订阅偏好"})
            continue
        if use_legacy_time_filters and not preferred_arrival_slots and not _matches_arrival_policy(flight, arrival_time_policy):
            excluded.append({**flight, "exclude_reason": "到达时间不符合订阅偏好"})
            continue
        if airline_policy == "no_lcc" and _contains_any_airline(flight, LCC_AIRLINES):
            excluded.append({**flight, "exclude_reason": "用户不接受廉航"})
            continue
        if exclude_airlines and _contains_any_airline(flight, exclude_airlines):
            excluded.append({**flight, "exclude_reason": "命中用户排除航司"})
            continue
        if airline_policy == "prefer_full_service":
            if _contains_any_airline(flight, FULL_SERVICE_AIRLINES):
                flight["score_multiplier"] = max(
                    float(flight.get("score_multiplier") or 1), 1.15
                )
                notes.append("偏好全服务航司")
            elif _contains_any_airline(flight, LCC_AIRLINES):
                penalty += 2
                penalties.append("非全服务航司")

        if max_budget and max_budget > 0 and price > max_budget:
            excluded.append({**flight, "exclude_reason": "\u8d85\u8fc7\u6700\u9ad8\u53ef\u63a5\u53d7\u4ef7\u683c"})
            continue
        if budget and budget > 0:
            notes.append("\u6700\u9ad8\u53ef\u63a5\u53d7\u4ef7\u683c\u5185")
        if target_price and target_price > 0:
            if price <= target_price:
                notes.append("\u4f4e\u4e8e\u7406\u60f3\u5165\u624b\u4ef7")
            elif price <= target_price + price_tolerance:
                notes.append("在理想价浮动范围内")
            else:
                penalties.append(f"\u8ddd\u79bb\u7406\u60f3\u5165\u624b\u4ef7\u00a5{price - target_price:,.0f}")

        if direct_required and stops > 0:
            excluded_flight = {**flight, "exclude_reason": "用户设置必须直飞"}
            excluded.append(excluded_flight)
            direct_reference_candidates.append(flight)
            continue
        if direct_only in {"flexible", "cheap_ok"} and stops > 0:
            penalty += 2 if direct_only == "flexible" else 1
            if cheapest_direct and price < cheapest_direct:
                notes.append(f"中转但便宜¥{cheapest_direct - price:,.0f}")
            else:
                penalties.append("包含中转")

        if transfer_policy in {"reasonable", "short_ok"} and stops > 0:
            total_minutes = int(flight.get("total_duration_min") or 0)
            if duration_limit_minutes and total_minutes > duration_limit_minutes:
                excluded.append(
                    {
                        **flight,
                        "exclude_reason": "超过合理中转最长可接受总行程时间",
                    }
                )
                continue
            if total_minutes > 24 * 60:
                penalty += 2
                penalties.append("中转总时长偏长")
            else:
                notes.append("合理中转可接受")
        elif transfer_policy in {"price_first", "cheap_ok"} and stops > 0:
            notes.append("价格优先，保留中转方案")

        if stops > 0 and not allow_overnight_transfer and _max_layover_minutes(flight) > 480:
            excluded.append({**flight, "exclude_reason": "系统默认不推荐过夜中转"})
            continue
        if stops > 0 and not allow_self_transfer and _is_likely_self_transfer(flight):
            excluded.append({**flight, "exclude_reason": "系统默认不推荐疑似非联程中转"})
            continue

        if red_eye == "reject" and _is_red_eye(flight):
            excluded.append({**flight, "exclude_reason": "用户不接受红眼/过早航班"})
            continue
        if red_eye in {"accept", "flexible", "cheap_ok"} and _is_red_eye(flight):
            penalty += 2 if red_eye in {"accept", "flexible"} else 1
            if cheapest_non_red_eye and price < cheapest_non_red_eye:
                notes.append(f"红眼但便宜¥{cheapest_non_red_eye - price:,.0f}")
            else:
                penalties.append("红眼/过早航班")

        if need_baggage == "required":
            if _has_free_checked_baggage(flight):
                notes.append("含免费托运")
            else:
                penalty += 3
                penalties.append("托运行李需官网确认")

        if refund_flexibility == "preferred":
            if _has_refund_change_flexibility(flight):
                notes.append("退改签较灵活")
            else:
                penalty += 1
                penalties.append("退改签需确认")
        elif refund_flexibility == "required":
            if _has_refund_change_flexibility(flight, required=True):
                notes.append("满足可退改")
            else:
                penalty += 4
                penalties.append("未确认可退改")

        has_family_companion = companions in {"with_elderly", "with_child", "with_elderly_child"}
        if has_family_companion:
            airline_text = " ".join(
                str(segment.get("airline", ""))
                for segment in flight.get("segments", [])
                if isinstance(segment, dict)
            )
            low_cost_markers = ["Spirit", "Frontier", "Spring", "VietJet", "AirAsia", "Scoot"]
            if stops == 0:
                notes.append("适合家庭出行：直飞")
            else:
                penalty += max(1, round(stops * 1.3))
            if _is_daytime_flight(flight):
                notes.append("适合家庭出行：白天时段")
            else:
                penalty += 2
                penalties.append("家庭出行时段不够友好")
            total_minutes = int(flight.get("total_duration_min") or 0)
            if total_minutes and total_minutes <= 20 * 60:
                notes.append("适合家庭出行：总时长较短")
            elif total_minutes > 24 * 60:
                penalty += 2
                penalties.append("家庭出行总时长偏长")
            if _has_free_checked_baggage(flight):
                notes.append("适合家庭出行：含免费托运")
            else:
                penalty += 1
            if _has_refund_change_flexibility(flight):
                notes.append("适合家庭出行：退改较灵活")
            else:
                penalty += 1
            if _is_red_eye(flight):
                penalty += 3
            if _max_layover_minutes(flight) > 360:
                penalty += 2
                penalties.append("长中转不适合家庭出行")
            if any(marker.lower() in airline_text.lower() for marker in low_cost_markers):
                penalty += 2
                penalties.append("廉航不适合家庭出行")

        if price_sensitivity == "low":
            if stops > 0:
                penalty += 2
            if _is_red_eye(flight) or _max_layover_minutes(flight) > 360:
                penalty += 2
            notes.append("便利稳定优先")
        elif price_sensitivity == "medium":
            notes.append("便宜时可接受轻微不便")
        elif price_sensitivity == "high":
            if stops > 0 or _is_red_eye(flight):
                notes.append("便宜但便利性较低")
        elif price_sensitivity == "max":
            penalty = max(0, penalty - 2)
            notes.append("价格优先")

        if trip_rigidity == "confirmed":
            if _has_refund_change_flexibility(flight):
                notes.append("行程确定：可尽早锁定")
            else:
                notes.append("行程确定：关注价格锁定")
        elif trip_rigidity == "mostly":
            notes.append("行程基本确定：可观察1-2天")
        elif trip_rigidity == "flexible":
            notes.append("行程灵活：可等待更低价")

        trip_type = preferences.get("trip_type")
        if trip_type == "business_meeting":
            penalty += stops
            if _is_red_eye(flight):
                penalty += 2
        elif trip_type == "tourism":
            penalty += 0
        elif trip_type == "family_elder":
            penalty += stops * 2
            if _max_layover_minutes(flight) > 240:
                penalty += 2

        flight["preference_notes"] = notes
        flight["preference_penalties"] = penalties
        flight["preference_penalty"] = penalty
        kept.append(flight)

    if not kept:
        if direct_required and not direct_flights and direct_reference_candidates:
            reference_flights = [
                {
                    **flight,
                    "preference_reference": True,
                    "preference_notes": list(flight.get("preference_notes") or [])
                    + ["未找到直飞航班，以下为中转参考方案"],
                }
                for flight in flights
            ]
            return reference_flights, excluded, {
                "fallback": True,
                "fallback_reason": "no_direct_flights",
                "message": "未找到直飞航班，以下为中转参考方案",
            }
        return [], excluded, {
            "fallback": False,
            "message": "没有航班满足当前硬约束",
        }
    return kept, excluded, {"fallback": False}


def _excluded_flight_summary_legacy(flights: list[dict]) -> list[dict]:
    summaries = []
    for flight in flights or []:
        price = _to_float(flight.get("price"))
        if price is None or price <= 0:
            continue
        summaries.append(
            {
                "price": price,
                "flight_combo": flight.get("flight_combo") or "",
                "airline_summary": flight.get("airline_summary")
                or " / ".join(flight.get("airlines") or []),
                "reason": flight.get("exclude_reason") or "不符合当前筛选条件",
            }
        )
    return sorted(summaries, key=lambda item: item["price"])


def _excluded_flight_summary(flights: list[dict]) -> list[dict]:
    """Keep enough excluded-flight context for notification explanations."""
    summaries = []
    for flight in flights or []:
        price = _to_float(flight.get("price"))
        if price is None or price <= 0:
            continue
        summaries.append(
            {
                "price": price,
                "flight_combo": flight.get("flight_combo") or "",
                "airline_summary": flight.get("airline_summary")
                or " / ".join(flight.get("airlines") or []),
                "reason": flight.get("exclude_reason") or "不符合当前筛选条件",
                "route_summary": flight.get("route_summary") or "",
                "segments": flight.get("segments") or [],
                "layovers": flight.get("layovers") or [],
                "airlines": flight.get("airlines") or [],
                "stops": flight.get("stops", 0),
                "fare_verification": flight.get("fare_verification") or {},
                "availability": flight.get("availability") or {},
                "transfer_risk": flight.get("transfer_risk") or {},
                "price_estimate": flight.get("price_estimate") or {},
                "data_source": flight.get("data_source") or flight.get("source") or "",
            }
        )
    return sorted(summaries, key=lambda item: item["price"])


def _extract_history_prices(price_insights: dict | None) -> list[float]:
    history = (price_insights or {}).get("price_history") or []
    prices = []
    for item in history:
        value = item[1] if isinstance(item, (list, tuple)) and len(item) >= 2 else item
        price = _to_float(value)
        if price and price > 0:
            prices.append(price)
    return prices


def _auto_target_price(price_insights: dict | None, mode: str) -> float | None:
    prices = sorted(_extract_history_prices(price_insights))
    if len(prices) < 5:
        return None
    if mode == "low_zone":
        percentile = 0.30
    elif mode == "auto_judge":
        percentile = 0.25
    else:
        percentile = 0.35
    index = min(len(prices) - 1, max(0, round((len(prices) - 1) * percentile)))
    return float(prices[index])


def _auto_budget_price(price_insights: dict | None, percentile: float) -> float | None:
    prices = sorted(_extract_history_prices(price_insights))
    if len(prices) < 5:
        return None
    index = min(len(prices) - 1, max(0, round((len(prices) - 1) * percentile)))
    return float(prices[index])


def price_tolerance_advice(
    price, target_price=None, tolerance=100, max_budget=None
) -> dict | None:
    current = _to_float(price)
    target = _to_float(target_price)
    tolerance_value = _to_float(tolerance)
    max_budget_value = _to_float(max_budget)
    if current is None or current <= 0 or target is None or target <= 0:
        return None
    tolerance_value = tolerance_value if tolerance_value is not None else 100
    buy_upper = target + tolerance_value

    if current <= target:
        level = "below_target"
        label = "🔥 低于理想价！强烈建议确认购买"
    elif current <= buy_upper:
        level = "within_tolerance"
        label = "✅ 在可接受浮动范围内，建议购买"
    elif max_budget_value and current <= max_budget_value:
        level = "within_budget"
        label = "📊 高于理想区间，仅刚需建议购买"
    else:
        level = "over_budget"
        label = "❌ 超出最高预算，不推荐"

    return {
        "level": level,
        "label": label,
        "current_price": current,
        "target_price": target,
        "tolerance": tolerance_value,
        "buy_upper": buy_upper,
        "max_budget": max_budget_value,
    }


def calc_final_score(flight: dict, target_price=None) -> float:
    price = _to_float(flight.get("price")) or 0
    target = _to_float(target_price)
    if target and target > 0 and price > 0:
        price_score = max(0, 100 - abs(price - target) / target * 100)
    else:
        raw_price_score = (flight.get("scores") or {}).get("price_score", 5)
        price_score = max(0, min(100, float(raw_price_score) * 10))

    risk_score = (flight.get("execution_risk") or {}).get("score", 50)
    reliability_score = max(0, 100 - float(risk_score))

    preference_score = flight.get("preference_score")
    if preference_score is None:
        preference_score = (flight.get("scores") or {}).get("total", 5)
    preference_score = max(0, min(100, float(preference_score) * 10))

    final_score = price_score * 0.4 + reliability_score * 0.3 + preference_score * 0.3
    flight["final_score"] = round(final_score, 1)
    return flight["final_score"]


def analyze_all_flights(
    flights: list[dict],
    price_insights: dict = None,
    mode: str = "balanced",
    priorities=None,
    user_preferences=None,
    hard_constraints=None,
) -> dict:
    """对所有航班方案做多维度分析和排名"""
    if not flights:
        return {"error": "no_flights"}

    usable_flights = [
        flight
        for flight in flights
        if (_to_float(flight.get("price")) or 0) > 0
        and flight.get("total_duration_min") is not None
    ]
    if not usable_flights:
        return {
            "error": "no_valid_prices",
            "total_options": 0,
            "all_flights": [],
            "price_range": [],
            "current_min_price": None,
            "market_context": {},
            "price_insights": price_insights,
        }

    mode = _trip_mode(mode, user_preferences)
    mode = mode if mode in SCORE_WEIGHTS else "balanced"
    original_options = len(usable_flights)
    if hard_constraints:
        merged_preferences = {**(user_preferences or {}), **hard_constraints}
        if "baggage" in hard_constraints and "need_baggage" not in merged_preferences:
            merged_preferences["need_baggage"] = hard_constraints.get("baggage")
    else:
        merged_preferences = user_preferences or {}
    print(f"[过滤前] {len(usable_flights)}个航班, 约束: {merged_preferences}")
    for flight in usable_flights:
        print(
            f"  航班 {flight.get('flight_combo')}: stops={flight.get('stops')}"
        )

    transfer_policy = (
        (hard_constraints or {}).get("transfer_policy")
        or (hard_constraints or {}).get("direct_only")
        or merged_preferences.get("transfer_policy")
        or merged_preferences.get("direct_only")
    )
    if transfer_policy in ("must", "direct_only", "must_direct"):
        direct_flights = [
            flight for flight in usable_flights if _stops_count(flight) == 0
        ]
        if direct_flights:
            direct_policy_excluded = [
                {**flight, "exclude_reason": "用户设置必须直飞"}
                for flight in usable_flights
                if _stops_count(flight) > 0
            ]
            usable_flights = direct_flights
        else:
            direct_policy_excluded = []
            merged_preferences["no_direct_flag"] = True
    else:
        direct_policy_excluded = []

    usable_flights, preference_excluded, preference_summary = _apply_user_preferences(
        usable_flights, merged_preferences
    )
    excluded_flights = direct_policy_excluded + preference_excluded
    print(f"[过滤后] {len(usable_flights)}个航班")
    if not usable_flights:
        return {
            "error": "no_flights",
            "excluded_flights": _excluded_flight_summary(excluded_flights),
        }

    # 1. 按价格排名
    by_price = sorted(usable_flights, key=lambda f: _to_float(f.get("price")) or float("inf"))

    # 2. 按总时长排名
    by_duration = sorted(usable_flights, key=lambda f: f["total_duration_min"])

    # 3. 按性价比排名（综合得分）
    valid_prices = [
        float(f["price"])
        for f in usable_flights
        if (_to_float(f.get("price")) or 0) > 0
    ]
    lowest_price = min(valid_prices) if valid_prices else None
    if lowest_price is None:
        return {
            "error": "no_valid_prices",
            "total_options": len(usable_flights),
            "all_flights": usable_flights,
            "price_range": [],
            "current_min_price": None,
            "market_context": {},
            "price_insights": price_insights,
        }
    prices = valid_prices
    durations = [f["total_duration_min"] for f in usable_flights]
    min_p, max_p = min(prices), max(prices)
    min_d, max_d = min(durations), max(durations)
    price_anomalies = detect_price_anomalies(usable_flights, price_insights)
    budget_strategy = (merged_preferences or {}).get("budget_strategy", "explicit")
    target_price_mode = (merged_preferences or {}).get("target_price_mode", "auto")
    target_price = _to_float((merged_preferences or {}).get("target_price"))
    target_price_effective = target_price
    if budget_strategy == "auto_judge":
        target_price_effective = _auto_budget_price(price_insights, 0.25)
    elif budget_strategy == "low_price_alert":
        target_price_effective = _auto_budget_price(price_insights, 0.30)
    elif not target_price_effective and target_price_mode in {"auto", "low_zone", "auto_judge"}:
        target_price_effective = _auto_target_price(price_insights, target_price_mode)
    max_budget_effective = _to_float((merged_preferences or {}).get("max_budget"))
    if budget_strategy == "auto_judge":
        max_budget_effective = _auto_budget_price(price_insights, 0.75)
    elif budget_strategy == "low_price_alert":
        max_budget_effective = None
    elif max_budget_effective is None:
        max_budget_effective = _to_float((merged_preferences or {}).get("budget"))
    price_tolerance = _to_float((merged_preferences or {}).get("price_tolerance"))
    if price_tolerance is None:
        price_tolerance = 100

    for flight in usable_flights:
        price_score = (
            ((float(flight["price"]) - min_p) / (max_p - min_p)) if max_p > min_p else 0
        )
        duration_score = (
            (flight["total_duration_min"] - min_d) / (max_d - min_d)
            if max_d > min_d
            else 0
        )
        stops_score = _stops_count(flight) / 3
        flight["value_score"] = round(
            price_score * 0.5 + duration_score * 0.3 + stops_score * 0.2,
            3,
        )
        flight["scores"] = overall_score(flight, prices, durations, mode)
        flight["transfer_risk"] = transfer_risk(flight)
        flight["fare_verification"] = verify_fare_rules(flight, merged_preferences)
        flight["price_estimate"] = calc_transaction_price(flight, merged_preferences)
        flight["availability"] = estimate_availability(
            flight,
            flight.get("collected_at") or flight.get("snapshot_time") or flight.get("fetched_at"),
        )
        calc_execution_risk(flight)
        advice = price_tolerance_advice(
            flight.get("price"),
            target_price_effective,
            price_tolerance,
            max_budget_effective,
        )
        if advice:
            flight["price_advice"] = advice
        calc_execution_grade(flight, merged_preferences)
        score_multiplier = float(flight.get("score_multiplier") or 1)
        flight["preference_score"] = round(
            flight["scores"]["total"] * score_multiplier
            - float(flight.get("preference_penalty") or 0),
            1,
        )
        calc_final_score(flight, target_price_effective)

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
        most_comfortable = min(
            comfortable_candidates,
            key=lambda f: _to_float(f.get("price")) or float("inf"),
        )
    else:
        most_comfortable = min(
            usable_flights,
            key=lambda f: (
                max(
                    (layover.get("wait_minutes", 0) for layover in f.get("layovers", [])),
                    default=0,
                )
                > 480,
                _stops_count(f),
                f["total_duration_min"],
                _to_float(f.get("price")) or float("inf"),
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
        display_flights.extend(
            sorted(cabin_flights, key=lambda f: _to_float(f.get("price")) or float("inf"))[:10]
        )

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
            float(flight["price"])
            for flight in usable_flights
            if (flight.get("cabin_class") or "economy") == cabin_class
            and (_to_float(flight.get("price")) or 0) > 0
        ]
        if cabin_prices:
            cabin_price_ranges[cabin_class] = [min(cabin_prices), max(cabin_prices)]

    budget_strategy = (merged_preferences or {}).get("budget_strategy", "explicit")
    target_price_mode = (merged_preferences or {}).get("target_price_mode", "auto")
    target_price = _to_float((merged_preferences or {}).get("target_price"))
    target_price_effective = target_price
    if budget_strategy == "auto_judge":
        target_price_effective = _auto_budget_price(price_insights, 0.25)
    elif budget_strategy == "low_price_alert":
        target_price_effective = _auto_budget_price(price_insights, 0.30)
    elif not target_price_effective and target_price_mode in {"auto", "low_zone", "auto_judge"}:
        target_price_effective = _auto_target_price(price_insights, target_price_mode)
    max_budget_effective = _to_float((merged_preferences or {}).get("max_budget"))
    if budget_strategy == "auto_judge":
        max_budget_effective = _auto_budget_price(price_insights, 0.75)
    elif budget_strategy == "low_price_alert":
        max_budget_effective = None
    elif max_budget_effective is None:
        max_budget_effective = _to_float((merged_preferences or {}).get("budget"))
    price_tolerance = _to_float((merged_preferences or {}).get("price_tolerance"))
    if price_tolerance is None:
        price_tolerance = 100
    price_band = price_tolerance_advice(
        lowest_price,
        target_price_effective,
        price_tolerance,
        max_budget_effective,
    )
    for flight in usable_flights:
        advice = price_tolerance_advice(
            flight.get("price"),
            target_price_effective,
            price_tolerance,
            max_budget_effective,
        )
        if advice:
            flight["price_advice"] = advice

    decision_flight = by_price[0] if by_price else usable_flights[0]
    confidence_breakdown = calc_confidence(
        decision_flight,
        {},
        (price_insights or {}).get("price_history") if price_insights else None,
    )
    decision_summary = generate_decision_summary(
        lowest_price,
        target_price_effective,
        max_budget_effective,
        confidence_breakdown,
        decision_flight.get("execution_grade"),
    )
    buy_vs_wait_risk = calc_buy_vs_wait_risk(
        lowest_price,
        (price_insights or {}).get("price_history") if price_insights else None,
        merged_preferences.get("days_to_dept"),
        target_price_effective,
        decision_flight.get("execution_grade"),
    )

    return {
        "total_options": len(usable_flights),
        "total_options_before_preferences": original_options,
        "recommendations": recommendations,
        "economy_recommendations": economy_recommendations,
        "business_recommendation": business_recommendation,
        "all_flights": display_flights,
        "price_range": [lowest_price, max(prices)],
        "cabin_price_ranges": cabin_price_ranges,
        "duration_range": [min(durations), max(durations)],
        "market_context": market_context,
        "price_insights": price_insights,
        "price_anomalies": price_anomalies,
        "current_min_price": lowest_price,
        "max_budget": max_budget_effective,
        "target_price": target_price,
        "target_price_effective": target_price_effective,
        "target_price_mode": target_price_mode,
        "budget_strategy": budget_strategy,
        "low_price_alert_triggered": (
            budget_strategy != "low_price_alert"
            or (
                target_price_effective is not None
                and lowest_price is not None
                and lowest_price <= target_price_effective
            )
        ),
        "price_tolerance": price_tolerance,
        "price_band": price_band,
        "decision_summary": decision_summary,
        "buy_vs_wait_risk": buy_vs_wait_risk,
        "confidence_breakdown": confidence_breakdown,
        "mode": mode,
        "priorities": priority_config,
        "qualified_flights": qualified_flights,
        "reference_flights": reference_flights,
        "user_preferences": merged_preferences,
        "preference_excluded_count": len(preference_excluded),
        "excluded_flights": _excluded_flight_summary(excluded_flights),
        "preference_summary": preference_summary,
    }


def _top_flights_for_round_trip(analysis: dict, limit: int = 3) -> list[dict]:
    flights = analysis.get("economy_recommendations") or analysis.get("all_flights") or []
    return sorted(
        [flight for flight in flights if (_to_float(flight.get("price")) or 0) > 0],
        key=lambda flight: _to_float(flight.get("price")) or 999999,
    )[:limit]


def _flight_transaction_price(flight: dict):
    estimate = flight.get("price_estimate") or {}
    value = _to_float(estimate.get("transaction_price"))
    if value is None:
        value = _to_float(estimate.get("estimated_price"))
    return value if value is not None else _to_float(flight.get("price"))


def _roundtrip_airlines(flight: dict) -> set[str]:
    names = set(str(name) for name in flight.get("airlines") or [] if name)
    for segment in flight.get("segments") or []:
        if isinstance(segment, dict) and segment.get("airline"):
            names.add(str(segment.get("airline")))
    if not names and flight.get("airline_summary"):
        names.update(part.strip() for part in str(flight["airline_summary"]).split("/") if part.strip())
    return names


def _mix_match_tip(combinations: list[dict]) -> str:
    if not combinations:
        return ""
    best = combinations[0]
    best_total = _to_float(best.get("total_price"))
    if best_total is None:
        return ""
    same_airline = []
    for combo in combinations:
        outbound_airlines = _roundtrip_airlines(combo.get("outbound") or {})
        return_airlines = _roundtrip_airlines(combo.get("return") or {})
        if outbound_airlines and return_airlines and outbound_airlines.intersection(return_airlines):
            same_airline.append(combo)
    if not same_airline:
        return ""
    best_same = min(same_airline, key=lambda item: _to_float(item.get("total_price")) or 999999)
    same_total = _to_float(best_same.get("total_price"))
    if same_total is None or same_total <= best_total:
        return ""
    diff = same_total - best_total
    outbound = best.get("outbound") or {}
    return_flight = best.get("return") or {}
    return (
        "💡 如果去程和返程分开买不同航司，总价可能更低："
        f"最优混搭：去程{outbound.get('flight_combo', '')}¥{best.get('outbound_price'):,.0f} + "
        f"返程{return_flight.get('flight_combo', '')}¥{best.get('return_price'):,.0f} = ¥{best_total:,.0f}，"
        f"比最优同航司组合便宜¥{diff:,.0f}"
    )


def analyze_roundtrip_trend(history: list[dict] | None) -> dict:
    """Analyze recent round-trip total price history."""
    rows = history or []
    prices = [
        _to_float(row.get("total", row.get("roundtrip_lowest")))
        for row in rows
        if _to_float(row.get("total", row.get("roundtrip_lowest"))) is not None
    ]
    if not prices:
        return {"available": False}

    recent = prices[-4:]
    if len(recent) >= 2:
        if recent[-1] < recent[0]:
            direction = "连续下降中" if all(recent[i] <= recent[i - 1] for i in range(1, len(recent))) else "整体下降"
            icon = "📉"
        elif recent[-1] > recent[0]:
            direction = "连续上涨中" if all(recent[i] >= recent[i - 1] for i in range(1, len(recent))) else "整体上涨"
            icon = "📈"
        else:
            direction = "基本持平"
            icon = "➡️"
    else:
        direction = "数据积累中"
        icon = ""

    return {
        "available": True,
        "prices": prices,
        "recent_prices": recent,
        "previous": rows[-2] if len(rows) >= 2 else None,
        "current": rows[-1] if rows else None,
        "is_recent_low": prices[-1] <= min(prices),
        "direction": direction,
        "icon": icon,
    }


def _roundtrip_row_value(row: dict, key: str):
    if key == "outbound":
        return _to_float(row.get("outbound", row.get("outbound_lowest")))
    if key == "return":
        return _to_float(row.get("return", row.get("return_lowest")))
    return _to_float(row.get("total", row.get("roundtrip_lowest")))


def _roundtrip_percentile_level(percentile: int) -> str:
    if percentile <= 10:
        return f"当前处于极低水平（比{100 - percentile}%的历史价格都便宜）"
    if percentile <= 25:
        return f"当前处于较低水平（比{100 - percentile}%的历史价格都便宜）"
    if percentile <= 50:
        return "当前处于中等偏低水平"
    if percentile <= 75:
        return "当前处于中等偏高水平"
    if percentile <= 90:
        return f"当前处于较高水平（比{percentile}%的历史价格都贵）"
    return "当前处于极高水平"


def _roundtrip_leg_level(current_price, history: list[dict], key: str) -> str:
    current = _to_float(current_price)
    prices = [
        _roundtrip_row_value(row, key)
        for row in history or []
        if _roundtrip_row_value(row, key) is not None
    ]
    if current is None or len(prices) < 3:
        return "历史数据不足"
    percentile = round(sum(1 for price in prices if price < current) / len(prices) * 100)
    if percentile <= 25:
        return "较低水平"
    if percentile <= 50:
        return "中等偏低水平"
    if percentile <= 75:
        return "中等偏高水平"
    return "较高水平"


def analyze_roundtrip_prices(
    history: list[dict] | None,
    current_total,
    outbound_current,
    return_current,
    target_price=None,
    max_budget=None,
    days_to_dept=None,
) -> dict:
    """Analyze round-trip total price references, trend, and leg contribution."""
    rows = history or []
    current_total = _to_float(current_total)
    outbound_current = _to_float(outbound_current)
    return_current = _to_float(return_current)
    if current_total is None:
        return {"available": False}

    totals = [
        _roundtrip_row_value(row, "total")
        for row in rows
        if _roundtrip_row_value(row, "total") is not None
    ]
    outbound_prices = [
        _roundtrip_row_value(row, "outbound")
        for row in rows
        if _roundtrip_row_value(row, "outbound") is not None
    ]
    return_prices = [
        _roundtrip_row_value(row, "return")
        for row in rows
        if _roundtrip_row_value(row, "return") is not None
    ]

    chart_rows = list(rows)
    latest_total = _roundtrip_row_value(rows[-1], "total") if rows else None
    if latest_total is None or abs(latest_total - current_total) >= 1:
        chart_rows.append(
            {
                "date": datetime.now().date().isoformat(),
                "outbound": outbound_current,
                "return": return_current,
                "total": current_total,
            }
        )

    if not totals:
        totals = [current_total]
    elif abs(totals[-1] - current_total) >= 1:
        totals = totals + [current_total]

    references = {
        "current": {
            "price": current_total,
            "outbound": outbound_current,
            "return": return_current,
        },
    }
    if totals:
        references["absolute_min"] = {
            "price": min(totals),
            "label": "历史往返最低",
        }
        references["recent_min"] = {
            "price": min(totals[-14:]),
            "label": "近期往返最低（你关注以来）",
            "sample_size": len(totals[-14:]),
        }
    if totals and days_to_dept is not None:
        references["conditional_min"] = {
            "price": min(totals),
            "label": f"同条件往返最低（提前{days_to_dept}天±7天）",
            "sample_size": len(totals),
        }

    short_term = {}
    recent = totals[-7:]
    if len(recent) >= 2:
        change_pct = round((recent[-1] - recent[0]) / recent[0] * 100, 1) if recent[0] else 0
        if all(recent[i] <= recent[i - 1] for i in range(1, len(recent))) and recent[-1] < recent[0]:
            trend = "📉 持续下降中"
        elif all(recent[i] >= recent[i - 1] for i in range(1, len(recent))) and recent[-1] > recent[0]:
            trend = "📈 持续上涨中"
        elif recent[-1] < recent[0]:
            trend = "📉 下降中"
        elif recent[-1] > recent[0]:
            trend = "📈 上涨中"
        else:
            trend = "➡️ 基本持平"

        previous_row = chart_rows[-2] if len(chart_rows) >= 2 else {}
        outbound_previous = _roundtrip_row_value(previous_row, "outbound")
        return_previous = _roundtrip_row_value(previous_row, "return")
        outbound_change = (
            outbound_current - outbound_previous
            if outbound_current is not None and outbound_previous is not None
            else None
        )
        return_change = (
            return_current - return_previous
            if return_current is not None and return_previous is not None
            else None
        )
        short_term = {
            "trend": trend,
            "change_pct": change_pct,
            "prices": recent,
            "outbound_change": outbound_change,
            "return_change": return_change,
        }

    mid_term = {}
    if len(totals) >= 2:
        percentile = round(sum(1 for price in totals if price < current_total) / len(totals) * 100)
        avg_price = round(sum(totals) / len(totals))
        mid_term = {
            "percentile": percentile,
            "level": _roundtrip_percentile_level(percentile),
            "min": min(totals),
            "max": max(totals),
            "avg": avg_price,
            "vs_avg": current_total - avg_price,
            "data_points": len(totals),
        }

    split = {}
    if outbound_prices and return_prices:
        outbound_level = _roundtrip_leg_level(outbound_current, rows, "outbound")
        return_level = _roundtrip_leg_level(return_current, rows, "return")
        contribution = ""
        previous_row = chart_rows[-2] if len(chart_rows) >= 2 else {}
        outbound_change = short_term.get("outbound_change")
        return_change = short_term.get("return_change")
        if outbound_change is not None and return_change is not None:
            if outbound_change > 0 and return_change < 0:
                contribution = "返程降价抵消了去程涨价"
            elif outbound_change < 0 and return_change > 0:
                contribution = "去程降价抵消了返程涨价"
            elif outbound_change < 0 and return_change < 0:
                contribution = "去程和返程同步下降"
            elif outbound_change > 0 and return_change > 0:
                contribution = "去程和返程同步上涨"
        split = {
            "outbound_level": outbound_level,
            "return_level": return_level,
            "contribution": contribution,
            "previous": previous_row,
        }

    target = _to_float(target_price)
    max_b = _to_float(max_budget)
    advice = ""
    if target and current_total <= target * 2:
        advice = (
            f"⭐ 往返购买建议：往返总价¥{current_total:,.0f}已低于理想价¥{target * 2:,.0f}，"
            "且处于近期低位。可以考虑锁定，继续等待的降幅空间有限。"
        )
    elif max_b and current_total <= max_b * 2:
        advice = (
            f"⭐ 往返购买建议：往返总价¥{current_total:,.0f}在最高预算内，"
            "但仍高于理想价，可结合出行确定性继续观察。"
        )
    elif max_b and current_total > max_b * 2:
        advice = (
            f"⭐ 往返购买建议：往返总价¥{current_total:,.0f}超出最高预算，"
            "可等待下一轮价格变化或扩大日期范围。"
        )

    return {
        "available": True,
        "history": rows,
        "references": references,
        "short_term": short_term,
        "mid_term": mid_term,
        "split": split,
        "trend_chart": chart_rows[-7:],
        "advice": advice,
    }


def _roundtrip_budget_advice(roundtrip_lowest, target_price=None, max_budget=None) -> str:
    total = _to_float(roundtrip_lowest)
    target = _to_float(target_price)
    max_b = _to_float(max_budget)
    if total is None:
        return ""
    if target and total <= target * 2:
        return f"🔥 往返总价¥{total:,.0f}已低于理想总价¥{target * 2:,.0f}，建议锁定"
    if max_b and total <= max_b * 2:
        return f"📊 往返总价在预算内但高于理想价，可继续观望"
    if max_b and total > max_b * 2:
        return f"⏳ 往返总价超出预算，建议等待降价"
    return ""


def analyze_round_trip(
    outbound_analysis: dict,
    return_analysis: dict,
    target_price=None,
    max_budget=None,
    history: list[dict] | None = None,
) -> dict:
    """Analyze outbound and return legs together for a round-trip subscription."""
    outbound_top = _top_flights_for_round_trip(outbound_analysis, 3)
    return_top = _top_flights_for_round_trip(return_analysis, 3)
    combinations = []

    for outbound in outbound_top:
        for return_flight in return_top:
            outbound_price = _to_float(outbound.get("price"))
            return_price = _to_float(return_flight.get("price"))
            if not outbound_price or outbound_price <= 0 or not return_price or return_price <= 0:
                continue
            combinations.append(
                {
                    "outbound": outbound,
                    "return": return_flight,
                    "outbound_price": outbound_price,
                    "return_price": return_price,
                    "total_price": outbound_price + return_price,
                    "transaction_total": (
                        (_flight_transaction_price(outbound) or outbound_price)
                        + (_flight_transaction_price(return_flight) or return_price)
                    ),
                }
            )

    combinations.sort(key=lambda item: item["total_price"])
    outbound_min = _to_float(outbound_top[0].get("price")) if outbound_top else None
    return_min = _to_float(return_top[0].get("price")) if return_top else None
    total_min = (
        outbound_min + return_min
        if outbound_min is not None and return_min is not None
        else None
    )

    insight = None
    if outbound_min is not None and return_min is not None:
        total = outbound_min + return_min
        if outbound_min < return_min * 0.8:
            insight = f"去程好价但返程偏贵，总价¥{total:,.0f}"
        elif return_min < outbound_min * 0.8:
            insight = f"返程好价但去程偏贵，总价¥{total:,.0f}"
        else:
            insight = f"去程和返程价格相对均衡，总价¥{total:,.0f}"

    trend = analyze_roundtrip_trend(history)
    previous = trend.get("previous") if trend.get("available") else None
    price_analysis = analyze_roundtrip_prices(
        history,
        total_min,
        outbound_min,
        return_min,
        target_price=target_price,
        max_budget=max_budget,
        days_to_dept=outbound_analysis.get("days_to_dept"),
    )
    best_combo = combinations[0] if combinations else {}
    combo_grades = [
        (best_combo.get("outbound") or {}).get("execution_grade"),
        (best_combo.get("return") or {}).get("execution_grade"),
    ]
    grade_order = {"A": 0, "B": 1, "C": 2, "D": 3}
    execution_grade = max(
        [grade for grade in combo_grades if grade],
        key=lambda grade: grade_order.get(grade, 2),
        default="C",
    )
    confidence_breakdown = calc_confidence(
        best_combo.get("outbound") or (outbound_top[0] if outbound_top else {}),
        {},
        history,
    )
    target_float = _to_float(target_price)
    max_budget_float = _to_float(max_budget)
    decision_summary = generate_decision_summary(
        total_min,
        target_float * 2 if target_float else None,
        max_budget_float * 2 if max_budget_float else None,
        confidence_breakdown,
        execution_grade,
    )
    buy_vs_wait_risk = calc_buy_vs_wait_risk(
        total_min,
        [row.get("total") for row in (history or []) if isinstance(row, dict)],
        outbound_analysis.get("days_to_dept"),
        target_float * 2 if target_float else None,
        execution_grade,
    )

    return {
        "outbound_min": outbound_min,
        "return_min": return_min,
        "total_min": total_min,
        "max_combination": combinations[-1] if combinations else None,
        "top_combinations": combinations[:3],
        "outbound_top3": outbound_top,
        "return_top3": return_top,
        "insight": insight,
        "mix_match_tip": _mix_match_tip(combinations),
        "history": history or [],
        "trend": trend,
        "price_analysis": price_analysis,
        "decision_summary": decision_summary,
        "confidence_breakdown": confidence_breakdown,
        "buy_vs_wait_risk": buy_vs_wait_risk,
        "previous": previous,
        "advice": _roundtrip_budget_advice(total_min, target_price, max_budget),
    }


def select_recommendations(economy_flights, business_flights, mode: str = "balanced"):
    """筛选推送方案：经济舱最多4个 + 商务舱1个。"""
    def max_layover_minutes(flight: dict) -> int:
        return max(
            (int(layover.get("wait_minutes") or 0) for layover in flight.get("layovers", [])),
            default=0,
        )

    def sort_key(flight: dict):
        preference_penalty = flight.get("preference_penalty", 0) or 0
        if flight.get("final_score") is not None:
            return (
                -float(flight.get("final_score") or 0),
                preference_penalty,
                _to_float(flight.get("price")) or 99999,
            )
        if mode == "budget":
            return (
                preference_penalty,
                _to_float(flight.get("price")) or 99999,
                flight.get("total_duration_min", 99999),
            )
        if mode == "fast":
            return (
                preference_penalty,
                flight.get("total_duration_min", 99999),
                _to_float(flight.get("price")) or 99999,
            )
        if mode == "comfort":
            return (
                preference_penalty,
                flight.get("stops", 99),
                max_layover_minutes(flight),
                flight.get("total_duration_min", 99999),
                _to_float(flight.get("price")) or 99999,
            )
        return (
            preference_penalty,
            flight.get("value_score", 99999),
            _to_float(flight.get("price")) or 99999,
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
            business_flights, key=lambda item: _to_float(item.get("price")) or 99999
        )

    return eco_recs, business_rec
