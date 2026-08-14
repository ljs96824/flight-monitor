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


def cabin_allocation_detail_label(allocation) -> str:
    """确认页按舱位列出实际乘客类型，不把同舱人群压成一个总数。"""
    normalized = normalize_cabin_allocation(allocation)
    display_order = ("adult", "child", "elderly", "infant")
    cabin_parts = []
    for cabin in CABIN_ORDER:
        passenger_parts = [
            f"{PASSENGER_TYPE_LABELS[key]}×{normalized[cabin][key]}"
            for key in display_order
            if normalized[cabin][key] > 0
        ]
        if passenger_parts:
            cabin_parts.append(f"{CABIN_LABELS[cabin]}:{'+'.join(passenger_parts)}")
    return " / ".join(cabin_parts) or "未分配舱位"

def business_types_from_allocation(allocation, passengers) -> tuple[list[str], bool]:
    """投影整类分舱；细粒度拆分返回representable=False以保留旧矩阵。"""
    normalized = normalize_cabin_allocation(allocation)
    passenger_counts = normalize_passenger_counts(passengers)
    selected = []
    representable = True
    for key in PASSENGER_TYPE_ORDER:
        expected = passenger_counts[key]
        business = normalized["business"][key]
        economy = normalized["economy"][key]
        if expected == 0:
            representable = representable and business == 0 and economy == 0
            continue
        if business == expected and economy == 0:
            selected.append(key)
            continue
        if business == 0 and economy == expected:
            continue
        representable = False
    return selected, representable

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


def cabin_allocation_from_form(form, passengers=None) -> tuple[dict, bool]:
    """读取类型勾选或旧矩阵；旧八字段继续用于直接POST兼容。"""
    ui_mode = str(form.get("cabin_allocation_ui") or "").strip()
    if ui_mode == "types":
        passenger_counts = normalize_passenger_counts(passengers)
        selected = set(form.getlist("cabin_business_types")) if hasattr(form, "getlist") else set()
        unknown = selected - set(PASSENGER_TYPE_ORDER)
        if unknown:
            raise ValueError(f"未知乘客类型: {','.join(sorted(unknown))}")
        allocation = {cabin: {} for cabin in CABIN_ORDER}
        for key in PASSENGER_TYPE_ORDER:
            count = passenger_counts[key]
            allocation["business"][key] = count if key in selected else 0
            allocation["economy"][key] = 0 if key in selected else count
        return allocation, sum(passenger_counts.values()) > 0

    # 旧客户端仍可直接提交细粒度矩阵；没有显式模式时维持原语义。
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
