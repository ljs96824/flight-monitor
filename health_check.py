import json
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).parent
SOURCE_HEALTH_PATH = BASE_DIR / "data" / "source_health.json"
METADATA_KEYS = {
    "total_raw",
    "after_dedup",
    "after_dedup_by_cabin",
    "enriched_count",
}
ENRICHMENT_SOURCES = {"duffel"}


def _load_source_health() -> dict:
    if not SOURCE_HEALTH_PATH.exists():
        return {}
    try:
        return json.loads(SOURCE_HEALTH_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_source_health(history: dict) -> None:
    SOURCE_HEALTH_PATH.parent.mkdir(exist_ok=True)
    SOURCE_HEALTH_PATH.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _is_success(status) -> bool:
    text = str(status or "").lower()
    return "成功" in text or "success" in text


def _source_entries(source_stats: dict | None) -> dict:
    return {
        key: value
        for key, value in (source_stats or {}).items()
        if key not in METADATA_KEYS and isinstance(value, dict)
    }


def _update_source_history(source_stats: dict | None) -> tuple[dict, list[str]]:
    history = _load_source_health()
    warnings = []
    now = datetime.now().isoformat()

    for source, info in _source_entries(source_stats).items():
        record = history.get(source, {"consecutive_failures": 0})
        if _is_success(info.get("status")):
            record["consecutive_failures"] = 0
        else:
            record["consecutive_failures"] = int(record.get("consecutive_failures", 0)) + 1

        record["last_status"] = info.get("status")
        record["last_count"] = info.get("count", 0)
        record["updated_at"] = now
        history[source] = record

        if record["consecutive_failures"] >= 3:
            warnings.append(f"数据源异常：{source}连续{record['consecutive_failures']}次失败")

    _save_source_health(history)
    return history, warnings


def _active_source_count(source_stats: dict | None) -> int:
    count = 0
    for source, info in _source_entries(source_stats).items():
        if source in ENRICHMENT_SOURCES:
            continue
        if _is_success(info.get("status")) and int(info.get("count") or 0) > 0:
            count += 1
    return count


def _prices(flights: list[dict] | None) -> list[float]:
    prices = []
    for flight in flights or []:
        try:
            price = float(flight.get("price"))
        except (TypeError, ValueError):
            continue
        prices.append(price)
    return prices


def _score(active_sources: int, option_count: int, prices: list[float]) -> int:
    score = 0

    score += min(active_sources, 4) * 10

    if option_count >= 20:
        score += 35
    elif option_count >= 10:
        score += 25
    elif option_count >= 5:
        score += 15
    elif option_count > 0:
        score += 5

    positive_prices = [price for price in prices if price > 0]
    if len(positive_prices) >= 2:
        low = min(positive_prices)
        high = max(positive_prices)
        spread_pct = (high - low) / low * 100 if low else 999
        if spread_pct == 0:
            score += 5
        elif spread_pct <= 80:
            score += 25
        elif spread_pct <= 150:
            score += 15
        else:
            score += 8
    elif len(positive_prices) == 1:
        score += 5

    return max(0, min(100, score))


def system_health_check(source_stats=None, flights=None, analysis_result=None) -> dict:
    """Monitor data-source health, data quality, and analysis confidence."""
    warnings = []
    source_history, source_warnings = _update_source_history(source_stats)
    warnings.extend(source_warnings)

    active_sources = _active_source_count(source_stats)
    if active_sources < 2:
        warnings.append("数据覆盖不足")

    option_count = 0
    if source_stats and source_stats.get("after_dedup") is not None:
        option_count = int(source_stats.get("after_dedup") or 0)
    elif analysis_result and analysis_result.get("total_options") is not None:
        option_count = int(analysis_result.get("total_options") or 0)
    else:
        option_count = len(flights or [])

    if option_count < 5:
        warnings.append("样本不足，分析可能不准确")

    prices = _prices(flights)
    if prices and len(set(prices)) == 1:
        warnings.append("价格数据可能陈旧")
    if any(price <= 0 for price in prices):
        warnings.append("数据异常：最低价为0或负数")

    score = _score(active_sources, option_count, prices)
    if warnings:
        score = max(0, score - min(30, len(warnings) * 8))

    if score >= 80:
        level = "高"
        emoji = "🟢"
    elif score >= 60:
        level = "中"
        emoji = "🟡"
    else:
        level = "低"
        emoji = "🔴"

    return {
        "score": score,
        "level": level,
        "emoji": emoji,
        "warnings": warnings,
        "active_sources": active_sources,
        "option_count": option_count,
        "source_history": source_history,
        "checked_at": datetime.now().isoformat(),
    }
