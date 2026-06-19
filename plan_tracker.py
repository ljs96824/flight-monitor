"""Track previously pushed plans and compare them with current results."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from filename_utils import sanitize_filename


BASE_DIR = Path(__file__).parent
DEFAULT_DATA_DIR = BASE_DIR / "data" / "pushed_plans"
DEFAULT_FEEDBACK_PATH = BASE_DIR / "data" / "feedback.json"


def _storage_dir(data_dir=None) -> Path:
    return Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR


def _storage_path(sub_id, data_dir=None) -> Path:
    return _storage_dir(data_dir) / f"{sanitize_filename(sub_id)}.json"


def _feedback_path(data_dir=None) -> Path:
    if data_dir is None:
        return DEFAULT_FEEDBACK_PATH
    return Path(data_dir) / "feedback.json"


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _flight_no(flight: dict | None) -> str:
    flight = flight or {}
    for key in ("flight_no", "flight_number", "flight_combo"):
        value = str(flight.get(key) or "").strip()
        if value:
            return value
    segments = flight.get("segments") or flight.get("flights") or []
    if segments:
        numbers = [
            str(seg.get("flight_no") or seg.get("flight_number") or "").strip()
            for seg in segments
            if isinstance(seg, dict)
        ]
        numbers = [item for item in numbers if item]
        if numbers:
            return "+".join(numbers)
    return ""


def _plan_primary_flight(plan: dict | None) -> dict:
    plan = plan or {}
    for key in ("main_flight", "outbound_flight", "flight", "outbound"):
        flight = plan.get(key)
        if isinstance(flight, dict) and flight:
            return flight
    return {}


def _plan_leg_flight(plan: dict | None, direction: str) -> dict:
    plan = plan or {}
    keys = (
        ("outbound_flight", "outbound", "go_flight")
        if direction == "outbound"
        else ("return_flight", "return", "back_flight")
    )
    for key in keys:
        flight = plan.get(key)
        if isinstance(flight, dict) and flight:
            return flight
    return {}


def _is_roundtrip_plan(plan: dict | None) -> bool:
    plan = plan or {}
    scope = str(plan.get("scope") or "").lower()
    if bool(plan.get("is_roundtrip")) or scope == "roundtrip":
        return True
    return bool(_plan_leg_flight(plan, "outbound") and _plan_leg_flight(plan, "return"))


def _leg_price(plan: dict | None, direction: str, flight: dict | None) -> float | None:
    plan = plan or {}
    key = "outbound_price" if direction == "outbound" else "return_price"
    return _to_float(plan.get(key) or (flight or {}).get("price"))


def _roundtrip_price(plan: dict | None, outbound: dict | None, return_flight: dict | None) -> float | None:
    plan = plan or {}
    tiers = plan.get("price_tiers") if isinstance(plan.get("price_tiers"), dict) else {}
    tier_price = _to_float(tiers.get("total_roundtrip_ref"))
    if tier_price is not None:
        return tier_price
    for key in ("roundtrip_price", "total_price", "roundtrip_total", "price"):
        value = _to_float(plan.get(key))
        if value is not None:
            return value
    outbound_price = _leg_price(plan, "outbound", outbound)
    return_price = _leg_price(plan, "return", return_flight)
    if outbound_price is not None and return_price is not None:
        return outbound_price + return_price
    return None


def _plan_record(plan: dict, index: int) -> dict | None:
    label = plan.get("label") or f"方案{chr(65 + index)}"
    pushed_at = datetime.now().isoformat(timespec="seconds")
    if _is_roundtrip_plan(plan):
        outbound = _plan_leg_flight(plan, "outbound")
        return_flight = _plan_leg_flight(plan, "return")
        outbound_no = _flight_no(outbound)
        return_no = _flight_no(return_flight)
        if not outbound_no and not return_no:
            return None
        outbound_price = _leg_price(plan, "outbound", outbound)
        return_price = _leg_price(plan, "return", return_flight)
        total = _roundtrip_price(plan, outbound, return_flight)
        price_tiers = plan.get("price_tiers") if isinstance(plan.get("price_tiers"), dict) else {}
        return {
            "flight_no": f"{outbound_no}+{return_no}".strip("+"),
            "is_roundtrip": True,
            "scope": "roundtrip",
            "outbound_flight": outbound_no,
            "return_flight": return_no,
            "roundtrip_price": total,
            "price": total,
            "outbound_price": outbound_price,
            "return_price": return_price,
            "price_tiers": price_tiers,
            "estimated_roundtrip_price": _to_float(price_tiers.get("total_estimated")),
            "date": plan.get("date") or outbound.get("departure_date"),
            "return_date": plan.get("return_date") or return_flight.get("departure_date"),
            "label": label,
            "pushed_at": pushed_at,
        }

    flight = _plan_primary_flight(plan)
    flight_no = _flight_no(flight)
    price = _to_float(plan.get("price") or flight.get("price"))
    if not flight_no:
        return None
    return {
        "flight_no": flight_no,
        "is_roundtrip": False,
        "scope": "single",
        "price": price,
        "label": label,
        "pushed_at": pushed_at,
    }


def extract_pushed_plan_records(plans: list[dict] | None) -> dict:
    records: dict[str, dict] = {}
    for index, plan in enumerate(plans or []):
        if not isinstance(plan, dict):
            continue
        record = _plan_record(plan, index)
        if not record:
            continue
        key = "plan_a" if index == 0 else f"plan_{chr(97 + index)}"
        records[key] = record
    return records


def save_pushed_plans(sub_id, plans: list[dict] | None, data_dir=None) -> dict:
    records = extract_pushed_plan_records(plans)
    if not records:
        return {}
    path = _storage_path(sub_id, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"subscription_id": str(sub_id), "last_pushed": records}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def load_pushed_plans(sub_id, data_dir=None) -> dict:
    path = _storage_path(sub_id, data_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def get_subscription_feedback(subscription_id, data_dir=None, unresolved_only: bool = True) -> list[dict]:
    """Return feedback records for one subscription, newest first."""
    path = _feedback_path(data_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    records = data if isinstance(data, list) else []
    target = str(subscription_id or "")
    matched = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("subscription_id") or "") != target:
            continue
        if unresolved_only and (
            record.get("resolved_at")
            or str(record.get("status") or "").lower() in {"resolved", "closed", "done"}
        ):
            continue
        matched.append(record)
    return list(reversed(matched))


def feedback_acknowledgement(subscription_id, data_dir=None) -> str:
    records = get_subscription_feedback(subscription_id, data_dir=data_dir)
    if not records:
        return ""
    feedback_type = str(records[0].get("feedback_type") or "").strip()
    unavailable_reason = str(records[0].get("unavailable_reason") or "").strip()
    if feedback_type in {"unavailable", "sold_out"} or unavailable_reason in {"sold_out", "unavailable"}:
        return "📌 你反馈过这条买不到,本次已重新核实可购买性,以下为最新采集结果。"
    if feedback_type in {"price_changed", "price_mismatch"} or unavailable_reason == "price_changed":
        return "📌 你反馈过价格不符,本次价格已重新采集,请以支付页最终价为准。"
    if feedback_type in {"no_baggage", "baggage"} or unavailable_reason == "no_baggage":
        return "📌 你反馈过行李问题,本次已标注各方案行李状态。"
    return "📌 你之前提交过反馈,本次已按最新采集结果重新核实。"


def find_flight(current_flights: list[dict] | None, flight_no: str) -> dict | None:
    target = str(flight_no or "").replace(" ", "").upper()
    if not target:
        return None
    for flight in current_flights or []:
        if not isinstance(flight, dict):
            continue
        current = _flight_no(flight).replace(" ", "").upper()
        if current == target:
            return flight
        if "+" in current and target in current.split("+"):
            return flight
    return None


def _item_leg_flight(item: dict | None, direction: str) -> dict:
    item = item or {}
    keys = (
        ("outbound", "outbound_flight", "go_flight")
        if direction == "outbound"
        else ("return", "return_flight", "back_flight")
    )
    for key in keys:
        flight = item.get(key)
        if isinstance(flight, dict) and flight:
            return flight
    return {}


def _find_roundtrip_combo(current_items: list[dict] | None, outbound_no: str, return_no: str) -> dict | None:
    outbound_target = str(outbound_no or "").replace(" ", "").upper()
    return_target = str(return_no or "").replace(" ", "").upper()
    if not outbound_target or not return_target:
        return None
    for item in current_items or []:
        if not isinstance(item, dict):
            continue
        outbound = _item_leg_flight(item, "outbound")
        return_flight = _item_leg_flight(item, "return")
        if not outbound or not return_flight:
            continue
        current_outbound = _flight_no(outbound).replace(" ", "").upper()
        current_return = _flight_no(return_flight).replace(" ", "").upper()
        if current_outbound == outbound_target and current_return == return_target:
            return item
    return None


def _format_price(value) -> str:
    price = _to_float(value)
    return f"¥{price:,.0f}" if price is not None else "暂无报价"


def _roundtrip_desc(outbound_no: str, return_no: str) -> str:
    if outbound_no and return_no:
        return f"{outbound_no}去+{return_no}回"
    return outbound_no or return_no or "航班待确认"


def _current_roundtrip_from_combo(combo: dict | None) -> tuple[float | None, float | None, float | None]:
    combo = combo or {}
    outbound = _item_leg_flight(combo, "outbound")
    return_flight = _item_leg_flight(combo, "return")
    outbound_price = _leg_price(combo, "outbound", outbound)
    return_price = _leg_price(combo, "return", return_flight)
    total = _roundtrip_price(combo, outbound, return_flight)
    return total, outbound_price, return_price


def _change_payload(
    status: str,
    flight_no: str,
    previous_price,
    current_price,
    diff,
    msg: str,
    scope: str,
    extra: dict | None = None,
) -> dict:
    payload = {
        "status": status,
        "flight_no": flight_no,
        "previous_price": previous_price,
        "current_price": current_price,
        "price_diff": diff,
        "scope": scope,
        "msg": msg,
    }
    if extra:
        payload.update(extra)
    return payload


def _price_change_status(diff: float | None, previous_price: float | None, previous_scope: str, current_scope: str) -> str:
    if diff is None or previous_price in (None, 0):
        return "stable"
    if abs(diff) / previous_price > 0.4 and previous_scope != current_scope:
        return "scope_mismatch"
    if diff > 50:
        return "price_up"
    if diff < -50:
        return "price_down"
    return "stable"


def _format_change(diff: float | None) -> str:
    if diff is None:
        return ""
    if diff > 0:
        return f"↑¥{diff:,.0f}"
    if diff < 0:
        return f"↓¥{abs(diff):,.0f}"
    return "持平"


def _track_roundtrip_plan(plan_a: dict, current_items: list[dict] | None) -> dict | None:
    outbound_no = str(plan_a.get("outbound_flight") or "").strip()
    return_no = str(plan_a.get("return_flight") or "").strip()
    desc = _roundtrip_desc(outbound_no, return_no)
    previous_price = _to_float(plan_a.get("roundtrip_price") or plan_a.get("price"))
    combo = _find_roundtrip_combo(current_items, outbound_no, return_no)
    outbound_current = find_flight(current_items, outbound_no)
    return_current = find_flight(current_items, return_no)

    print(f"[方案追踪诊断] 航班={desc}")
    print(
        f"[方案追踪诊断] 上次价={previous_price}, 上次口径=往返, "
        f"上次记录的是={json.dumps(plan_a, ensure_ascii=False, default=str)}"
    )

    if combo:
        current_price, outbound_price, return_price = _current_roundtrip_from_combo(combo)
        current_source = combo
    elif outbound_current and return_current:
        outbound_price = _to_float(outbound_current.get("price"))
        return_price = _to_float(return_current.get("price"))
        current_price = (
            outbound_price + return_price
            if outbound_price is not None and return_price is not None
            else None
        )
        current_source = {"outbound": outbound_current, "return": return_current}
    else:
        current_price = None
        outbound_price = _to_float((outbound_current or {}).get("price")) if outbound_current else None
        return_price = _to_float((return_current or {}).get("price")) if return_current else None
        current_source = {"outbound": outbound_current, "return": return_current}

    print(
        f"[方案追踪诊断] 本次价={current_price}, 本次口径=往返, "
        f"本次取到的是={json.dumps(current_source, ensure_ascii=False, default=str)}"
    )
    diff = current_price - previous_price if current_price is not None and previous_price is not None else None
    print(f"[方案追踪诊断] 差额={None if diff is None else previous_price - current_price}")

    if current_price is None:
        missing = []
        available = []
        if outbound_no:
            if outbound_price is None:
                missing.append(f"去程{outbound_no}")
            else:
                available.append(f"去程{outbound_no}仍{_format_price(outbound_price)}")
        if return_no:
            if return_price is None:
                missing.append(f"返程{return_no}")
            else:
                available.append(f"返程{return_no}仍{_format_price(return_price)}")
        if missing:
            msg = (
                f"上次推荐:{desc},往返{_format_price(previous_price)}。"
                f"本次:{'，'.join(available) + '，' if available else ''}"
                f"{'、'.join(missing)}本次未获取到报价,无法计算完整往返价,建议在渠道核实。"
            )
            return _change_payload(
                "partial_unavailable",
                desc,
                previous_price,
                current_price,
                None,
                msg,
                "roundtrip",
                {
                    "outbound_flight": outbound_no,
                    "return_flight": return_no,
                    "outbound_price": outbound_price,
                    "return_price": return_price,
                },
            )
        msg = (
            f"上次推荐:{desc},往返{_format_price(previous_price)}。"
            "本次未获取到该组合报价,可能是采集覆盖问题或售罄,建议在渠道核实。"
        )
        return _change_payload("unavailable", desc, previous_price, None, None, msg, "roundtrip")

    status = _price_change_status(diff, previous_price, "roundtrip", "roundtrip")
    change_text = _format_change(diff)
    if status == "price_up":
        verdict = f"上涨{change_text.replace('↑', '')}"
    elif status == "price_down":
        verdict = f"下降{change_text.replace('↓', '')}"
    elif change_text in {"", "持平"}:
        verdict = "价格稳定"
    else:
        verdict = f"小幅变化({change_text})"
    msg = (
        f"上次推荐:{desc},往返{_format_price(previous_price)}。"
        f"本次同组合:往返{_format_price(current_price)}({verdict})。"
        "（同往返口径对比）"
    )
    return _change_payload(
        status,
        desc,
        previous_price,
        current_price,
        diff,
        msg,
        "roundtrip",
        {
            "outbound_flight": outbound_no,
            "return_flight": return_no,
            "outbound_price": outbound_price,
            "return_price": return_price,
        },
    )


def track_plan_status(sub_id, current_flights: list[dict] | None, data_dir=None) -> dict | None:
    last = load_pushed_plans(sub_id, data_dir)
    plan_a = (last.get("last_pushed") or {}).get("plan_a") or {}
    if plan_a.get("is_roundtrip") or plan_a.get("scope") == "roundtrip":
        return _track_roundtrip_plan(plan_a, current_flights)

    flight_no = plan_a.get("flight_no")
    if not flight_no:
        return None
    same = find_flight(current_flights, flight_no)
    previous_price = _to_float(plan_a.get("price"))
    print(f"[方案追踪诊断] 航班={flight_no}")
    print(
        f"[方案追踪诊断] 上次价={previous_price}, 上次口径=单程, "
        f"上次记录的是={json.dumps(plan_a, ensure_ascii=False, default=str)}"
    )
    if not same:
        return {
            "status": "unavailable",
            "flight_no": flight_no,
            "previous_price": previous_price,
            "scope": "single",
            "msg": f"上次推荐的{flight_no}本次未获取到报价(可能是采集覆盖问题或售罄,建议在渠道核实)",
        }
    current_price = _to_float(same.get("price"))
    print(
        f"[方案追踪诊断] 本次价={current_price}, 本次口径=单程, "
        f"本次取到的是={json.dumps(same, ensure_ascii=False, default=str)}"
    )
    diff = current_price - previous_price if current_price is not None and previous_price is not None else None
    print(f"[方案追踪诊断] 差额={None if diff is None else previous_price - current_price}")
    if previous_price is None or current_price is None:
        return {
            "status": "stable",
            "flight_no": flight_no,
            "previous_price": previous_price,
            "current_price": current_price,
            "scope": "single",
            "msg": f"上次推荐的{flight_no}仍有报价,价格需支付页确认",
        }
    status = _price_change_status(diff, previous_price, "single", "single")
    if status == "price_up":
        return {
            "status": "price_up",
            "flight_no": flight_no,
            "previous_price": previous_price,
            "current_price": current_price,
            "price_diff": diff,
            "scope": "single",
            "msg": f"上次推荐的{flight_no}已涨价¥{diff:,.0f}(¥{previous_price:,.0f}→¥{current_price:,.0f})",
        }
    if status == "price_down":
        return {
            "status": "price_down",
            "flight_no": flight_no,
            "previous_price": previous_price,
            "current_price": current_price,
            "price_diff": diff,
            "scope": "single",
            "msg": f"上次推荐的{flight_no}又降了¥{abs(diff):,.0f}",
        }
    return {
        "status": "stable",
        "flight_no": flight_no,
        "previous_price": previous_price,
        "current_price": current_price,
        "price_diff": diff,
        "scope": "single",
        "msg": f"上次推荐的{flight_no}价格稳定(¥{current_price:,.0f})",
    }
