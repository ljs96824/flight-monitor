"""混舱乘客分配的规范化与校验单一真值。"""

from __future__ import annotations

from collections.abc import Mapping


CABIN_ORDER = ("business", "economy")
PASSENGER_TYPE_ORDER = ("adult", "elderly", "child", "infant")
PASSENGER_TYPE_LABELS = {
    "adult": "成人",
    "child": "儿童",
    "elderly": "老人",
    "infant": "婴儿",
}
CABIN_LABELS = {"business": "商务", "economy": "经济"}


def normalize_passenger_counts(passengers) -> dict[str, int]:
    source = passengers if isinstance(passengers, Mapping) else {}
    result = {}
    for key in PASSENGER_TYPE_ORDER:
        value = source.get(key, 0)
        try:
            number = int(value or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{PASSENGER_TYPE_LABELS[key]}人数必须是整数") from exc
        if number < 0:
            raise ValueError(f"{PASSENGER_TYPE_LABELS[key]}人数不能为负数")
        result[key] = number
    return result


def normalize_cabin_allocation(allocation) -> dict[str, dict[str, int]]:
    source = allocation if isinstance(allocation, Mapping) else {}
    result = {}
    for cabin in CABIN_ORDER:
        cabin_source = source.get(cabin) if isinstance(source.get(cabin), Mapping) else {}
        counts = {}
        for key in PASSENGER_TYPE_ORDER:
            value = cabin_source.get(key, 0)
            try:
                number = int(value or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{CABIN_LABELS[cabin]}舱{PASSENGER_TYPE_LABELS[key]}人数必须是整数"
                ) from exc
            if number < 0:
                raise ValueError(
                    f"{CABIN_LABELS[cabin]}舱{PASSENGER_TYPE_LABELS[key]}人数不能为负数"
                )
            counts[key] = number
        result[cabin] = counts
    return result


def cabin_allocation_label(allocation) -> str:
    normalized = normalize_cabin_allocation(allocation)
    parts = []
    for cabin in CABIN_ORDER:
        count = sum(normalized[cabin].values())
        if count:
            parts.append(f"{CABIN_LABELS[cabin]}{count}人")
    return "+".join(parts) or "未分配舱位"


def validate_cabin_allocation(allocation, passengers) -> dict:
    normalized = normalize_cabin_allocation(allocation)
    passenger_counts = normalize_passenger_counts(passengers)
    for key in PASSENGER_TYPE_ORDER:
        allocated = sum(normalized[cabin][key] for cabin in CABIN_ORDER)
        expected = passenger_counts[key]
        if allocated != expected:
            raise ValueError(
                f"{PASSENGER_TYPE_LABELS[key]}共有{expected}人，但分舱合计为{allocated}人"
            )
    business_seats = sum(normalized["business"].values())
    economy_seats = sum(normalized["economy"].values())
    if business_seats + economy_seats <= 0:
        raise ValueError("混舱分配至少需要一名乘客")
    return {
        "allocation": normalized,
        "business_seats": business_seats,
        "economy_seats": economy_seats,
        "label": cabin_allocation_label(normalized),
    }


def find_explicit_cabin_allocation(*containers):
    """找到显式保存的分舱结构；缺失时返回None，不替旧订阅造字段。"""
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        value = container.get("cabin_allocation")
        if isinstance(value, Mapping):
            return value
    return None


def cabin_allocation_from_form(form) -> tuple[dict, bool]:
    """读取页面分舱表；八个字段全空/零时视为未显式启用。"""
    allocation = {cabin: {} for cabin in CABIN_ORDER}
    explicit = False
    for cabin in CABIN_ORDER:
        for key in PASSENGER_TYPE_ORDER:
            field = f"cabin_{cabin}_{key}"
            raw = form.get(field) if hasattr(form, "get") else None
            if raw not in (None, ""):
                try:
                    value = int(raw or 0)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"{CABIN_LABELS[cabin]}舱{PASSENGER_TYPE_LABELS[key]}人数必须是整数"
                    ) from exc
                explicit = explicit or value != 0
            else:
                value = 0
            allocation[cabin][key] = value
    return allocation, explicit


def allocation_has_business(allocation) -> bool:
    normalized = normalize_cabin_allocation(allocation)
    return sum(normalized["business"].values()) > 0
