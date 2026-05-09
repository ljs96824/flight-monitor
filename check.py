"""Terminal status overview for collected flight prices."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path

from analyzer import calc_trend, generate_signal
from storage import DB_PATH, init_db


BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"


def _load_config_targets() -> dict[tuple[str, str], str]:
    if not CONFIG_PATH.exists():
        return {}

    targets = {}
    current = {}
    for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            if current:
                _add_target(targets, current)
            current = {}
            stripped = stripped[2:].strip()

        if ":" not in stripped or stripped == "subscriptions:":
            continue

        key, value = stripped.split(":", 1)
        current[key.strip()] = value.strip().strip('"').strip("'")

    if current:
        _add_target(targets, current)

    return targets


def _add_target(targets: dict[tuple[str, str], str], sub: dict) -> None:
    required = ["origin", "destination", "depart_date"]
    if not all(key in sub for key in required):
        return

    route = f"{sub['origin']}-{sub['destination']}"
    targets[(route, sub["depart_date"])] = sub.get("target_combo", "")


def _load_rows() -> list[dict]:
    init_db()
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT *
            FROM price_snapshots
            ORDER BY route ASC, depart_date ASC, snapshot_time ASC, id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _date_part(snapshot_time: str | None) -> str:
    if not snapshot_time:
        return "-"
    return snapshot_time.split("T", 1)[0].split(" ", 1)[0]


def _money(value) -> str:
    if value is None:
        return "-"
    return f"{float(value):.0f}"


def _price_value(record: dict) -> float | None:
    price = record.get("price")
    if price is None:
        return None
    return float(price)


def _sort_price(record: dict) -> float:
    price = _price_value(record)
    return price if price is not None else float("inf")


def _trend_label(trend: str) -> str:
    labels = {
        "rising": "上涨",
        "falling": "下跌",
        "flat": "平稳",
    }
    return labels.get(trend, trend)


def _pick_target_combo(records: list[dict], fallback: str) -> str:
    target_records = [record for record in records if record.get("is_target") == 1]
    if target_records:
        return target_records[-1].get("flight_combo") or fallback
    return fallback


def _latest_snapshot_records(records: list[dict]) -> list[dict]:
    latest_time = max((record.get("snapshot_time") or "" for record in records), default="")
    return [record for record in records if (record.get("snapshot_time") or "") == latest_time]


def _print_group(route: str, depart_date: str, records: list[dict], target_combo: str) -> None:
    days_to_dept = (date.fromisoformat(depart_date) - date.today()).days
    target_records = [
        record
        for record in records
        if record.get("is_target") == 1 and record.get("flight_combo") == target_combo
    ]
    if not target_records:
        target_records = [record for record in records if record.get("is_target") == 1]

    prices = [
        _price_value(record)
        for record in target_records
        if _price_value(record) is not None
    ]
    data_points = len(prices)
    collection_days = len(
        {
            _date_part(record.get("snapshot_time"))
            for record in records
            if record.get("snapshot_time")
        }
    )

    current_price = prices[-1] if prices else None
    priced_target_records = [
        record for record in target_records if _price_value(record) is not None
    ]
    min_record = min(priced_target_records, key=_sort_price) if priced_target_records else None
    max_record = max(priced_target_records, key=_sort_price) if priced_target_records else None
    avg_price = sum(prices) / len(prices) if prices else None
    trend = calc_trend(prices[-10:]) if prices else {"trend": "flat", "change_pct": 0.0}
    signal = (
        generate_signal(
            current_price,
            trend["trend"],
            days_to_dept,
            _price_value(min_record),
            avg_price,
        )
        if data_points >= 4 and min_record is not None and avg_price is not None
        else "collecting"
    )

    latest_records = _latest_snapshot_records(records)
    alternatives = [
        record
        for record in latest_records
        if record.get("is_target") != 1 and _price_value(record) is not None
    ]
    alternatives.sort(key=_sort_price)

    print("========================================")
    print(f"航线: {route} | 目标: {target_combo}")
    print(f"出发日: {depart_date} | 距出发: {days_to_dept}天")
    print("========================================")
    print(f"数据点: {data_points}个 | 采集天数: {collection_days}天")
    print(f"当前价: ¥{_money(current_price)}")
    print(f"最低价: ¥{_money(min_record.get('price') if min_record else None)} ({_date_part(min_record.get('snapshot_time') if min_record else None)})")
    print(f"最高价: ¥{_money(max_record.get('price') if max_record else None)} ({_date_part(max_record.get('snapshot_time') if max_record else None)})")
    print(f"均  价: ¥{_money(avg_price)}")
    print(f"趋  势: {_trend_label(trend['trend'])} ({trend['change_pct']}%)")
    print(f"信  号: {signal}")
    print("----------------------------------------")
    print("最新替代方案:")
    if not alternatives:
        print("  暂无")
    for alt in alternatives[:5]:
        alt_price = _price_value(alt)
        diff = alt_price - current_price if current_price is not None and alt_price is not None else 0
        sign = "+" if diff >= 0 else "-"
        stopover = alt.get("stopover_city") or "-"
        print(
            f"  {alt.get('flight_combo') or '-'} 经{stopover}: "
            f"¥{_money(alt_price)} (差价 {sign}{_money(abs(diff))})"
        )
    print("========================================")


def main() -> None:
    rows = _load_rows()
    if not rows:
        print("暂无数据，请先运行 python main.py 采集")
        return

    targets = _load_config_targets()
    groups = defaultdict(list)
    for row in rows:
        groups[(row["route"], row["depart_date"])].append(row)

    for (route, depart_date), records in groups.items():
        fallback_target = targets.get((route, depart_date), "")
        target_combo = _pick_target_combo(records, fallback_target)
        _print_group(route, depart_date, records, target_combo)


if __name__ == "__main__":
    main()
