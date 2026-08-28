from datetime import datetime
from pathlib import Path

from atomic_json_store import JsonStoreReadError, read_json, update_json
from log_utils import safe_log
from source_profiles import expected_listing_sources


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
    history = read_json(SOURCE_HEALTH_PATH)
    if not isinstance(history, dict):
        raise JsonStoreReadError("source_health 根节点必须是对象")
    return history


def _is_success(status) -> bool:
    text = str(status or "").lower()
    return "成功" in text or "success" in text


def _source_entries(source_stats: dict | None) -> dict:
    return {
        key: value
        for key, value in (source_stats or {}).items()
        if key not in METADATA_KEYS and isinstance(value, dict)
    }


def _apply_source_history_updates(
    history: dict,
    entries: dict,
    *,
    now: str,
) -> tuple[dict, list[str]]:
    warnings = []

    for source, info in entries.items():
        record = history.get(source, {"consecutive_failures": 0})
        if not isinstance(record, dict):
            record = {"consecutive_failures": 0}
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

    return history, warnings


def _update_source_history(source_stats: dict | None) -> tuple[dict, list[str]]:
    entries = _source_entries(source_stats)
    now = datetime.now().isoformat()

    if not entries:
        try:
            return _load_source_health(), []
        except JsonStoreReadError as exc:
            safe_log(
                "[源健康] source_health_state_degraded "
                f"原因={type(exc).__name__}:{exc}; 本轮仅使用当前source_stats"
            )
            return {}, []

    warnings: list[str] = []

    def mutate(payload):
        nonlocal warnings
        if payload is None:
            history = {}
        elif isinstance(payload, dict):
            history = payload
        else:
            raise JsonStoreReadError("source_health 根节点必须是对象")
        history, warnings = _apply_source_history_updates(
            history,
            entries,
            now=now,
        )
        return history

    try:
        history = update_json(SOURCE_HEALTH_PATH, mutate)
    except JsonStoreReadError as exc:
        safe_log(
            "[源健康] source_health_state_degraded "
            f"原因={type(exc).__name__}:{exc}; 本轮仅使用当前source_stats"
        )
        history, warnings = _apply_source_history_updates({}, entries, now=now)
    return history, warnings


def _active_source_count(source_stats: dict | None) -> int:
    count = 0
    for source, info in _source_entries(source_stats).items():
        if source in ENRICHMENT_SOURCES:
            continue
        if _is_success(info.get("status")) and int(info.get("count") or 0) > 0:
            count += 1
    return count


def _successful_listing_sources(source_stats: dict | None) -> set[str]:
    successful = set()
    for source, info in _source_entries(source_stats).items():
        normalized = str(source).strip().lower()
        if (
            normalized
            and normalized not in ENRICHMENT_SOURCES
            and _is_success(info.get("status"))
        ):
            successful.add(normalized)
    return successful


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


def system_health_check(
    source_stats=None,
    flights=None,
    analysis_result=None,
    *,
    route_type=None,
    cabin_class="economy",
    observed_day=None,
) -> dict:
    """Monitor data-source health, data quality, and analysis confidence."""
    warnings = []
    source_history, source_warnings = _update_source_history(source_stats)
    warnings.extend(source_warnings)

    expected_sources = expected_listing_sources(
        route_type,
        observed_day=observed_day,
        cabin_class=cabin_class,
    )
    successful_sources = _successful_listing_sources(source_stats)
    missing_sources = expected_sources - successful_sources
    coverage_complete = not missing_sources
    source_diversity_n = _active_source_count(source_stats)
    cross_check_status = (
        "performed" if source_diversity_n >= 2 else "not_performed"
    )
    if not coverage_complete:
        warnings.append("数据覆盖不足")
    elif len(expected_sources) == 1 and source_diversity_n == 1:
        safe_log("覆盖完整,当前为单源正式报价,未进行额外交叉验证")

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

    score = _score(source_diversity_n, option_count, prices)
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
        "active_sources": source_diversity_n,
        "coverage_complete": coverage_complete,
        "expected_sources": sorted(expected_sources),
        "successful_sources": sorted(successful_sources),
        "missing_sources": sorted(missing_sources),
        "source_diversity_n": source_diversity_n,
        "cross_check_status": cross_check_status,
        "option_count": option_count,
        "source_history": source_history,
        "checked_at": datetime.now().isoformat(),
    }
