"""表单确认页与通知排除卡共用的约束依据摘要。"""

from __future__ import annotations

from pricing import passenger_rate_sum


def _to_float(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _budget_scope(value) -> str:
    text = str(value or "per_person").strip().lower().replace("-", "_")
    if text in {
        "all",
        "total",
        "all_passengers",
        "all_passengers_roundtrip",
        "all_passenger",
        "overall",
        "total_roundtrip",
        "\u6574\u5355",
        "\u5168\u5458",
        "\u5168\u90e8\u4eba",
    }:
        return "all"
    return "per_person"


def format_constraint_summary(parts: list[str] | tuple[str, ...] | None) -> str:
    values = [str(value).strip() for value in (parts or []) if str(value or "").strip()]
    if not values:
        return "\u4f9d\u636e:\u672a\u8bbe\u7f6e\u786c\u7ea6\u675f"
    return "\u4f9d\u636e:" + "\u00b7".join(values)


def build_constraint_summary(
    constraints: dict | None,
    max_budget=None,
    passengers: dict | None = None,
    route_type: str | None = None,
) -> list[str]:
    """按既有邮件顺序生成可追溯的用户约束摘要。"""
    constraints = constraints or {}
    summary: list[str] = []
    if constraints.get("same_day_round_trip"):
        summary.append("当天往返")

    business_start = str(constraints.get("business_start") or "").strip()
    business_end = str(constraints.get("business_end") or "").strip()
    if business_start and business_end:
        summary.append(f"会议{business_start}-{business_end}")

    direct_only = str(
        constraints.get("direct_only")
        or constraints.get("transfer_policy")
        or constraints.get("direct_policy")
        or ""
    ).strip()
    if direct_only in {"must", "direct", "direct_only", "nonstop", "必须直飞"}:
        summary.append("必须直飞")

    baggage = str(
        constraints.get("need_baggage") or constraints.get("baggage") or ""
    ).strip()
    if baggage in {"required", "must", "checked_required", "必须托运"}:
        summary.append("必须含托运")

    lcc_policy = str(constraints.get("lcc_policy") or "any").strip()
    if lcc_policy == "exclude_lcc":
        summary.append("排除廉航")
    elif lcc_policy == "lcc_only":
        summary.append("仅看廉航(全段)")

    max_budget_value = _to_float(max_budget)
    if max_budget_value is None:
        return summary

    max_scope = _budget_scope(
        constraints.get("max_budget_scope") or constraints.get("budget_scope")
    )
    input_max_budget = _to_float(
        constraints.get("max_budget")
        or constraints.get("budget")
        or constraints.get("price_ceiling")
    )
    if max_scope == "per_person" and input_max_budget is not None and passengers:
        factor = passenger_rate_sum(passengers, route_type)
        summary.append(
            f"最高可接受价¥{max_budget_value:,.0f}"
            f"(全员,=单人¥{input_max_budget:,.0f}×{factor:g})"
        )
    else:
        scope_label = "全员往返" if max_scope == "all" else "单人往返"
        summary.append(f"最高可接受价¥{max_budget_value:,.0f}({scope_label})")
    return summary
