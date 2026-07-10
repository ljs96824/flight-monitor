"""Track previously pushed plans and compare them with current results."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from filename_utils import sanitize_filename
from flight_combo_utils import normalize_combo
from log_utils import safe_log


BASE_DIR = Path(__file__).parent
DEFAULT_DATA_DIR = BASE_DIR / "data" / "pushed_plans"
DEFAULT_FEEDBACK_PATH = BASE_DIR / "data" / "feedback.json"
ROUNDTRIP_TRACKING_SCOPE = "per_person_roundtrip"
ROUNDTRIP_TRACKING_LABEL = "单人往返"


def _storage_dir(data_dir=None) -> Path:
    return Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR


def _storage_path(sub_id, data_dir=None) -> Path:
    return _storage_dir(data_dir) / f"{sanitize_filename(sub_id)}.json"


def _feedback_path(data_dir=None) -> Path:
    if data_dir is None:
        return DEFAULT_FEEDBACK_PATH
    return Path(data_dir) / "feedback.json"



def _track_combo_key(value) -> str:
    normalized = normalize_combo(value)
    if normalized:
        return normalized
    return str(value or "").replace(" ", "").upper()


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
            return _track_combo_key(value)
    segments = flight.get("segments") or flight.get("flights") or []
    if segments:
        numbers = [
            str(seg.get("flight_no") or seg.get("flight_number") or "").strip()
            for seg in segments
            if isinstance(seg, dict)
        ]
        numbers = [item for item in numbers if item]
        if numbers:
            return _track_combo_key("+".join(numbers))
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


def _price_tiers(item: dict | None) -> dict:
    item = item or {}
    tiers = item.get("price_tiers")
    return tiers if isinstance(tiers, dict) else {}


def _passenger_factor(item: dict | None) -> float | None:
    item = item or {}
    passenger_pricing = item.get("passenger_pricing")
    containers = [
        _price_tiers(item),
        passenger_pricing if isinstance(passenger_pricing, dict) else {},
        item,
    ]
    for container in containers:
        for key in ("factor", "passenger_factor", "passenger_rate_sum"):
            value = _to_float(container.get(key))
            if value is not None and value > 0:
                return value
    return None


def _passengers(item: dict | None) -> dict:
    item = item or {}
    passenger_pricing = item.get("passenger_pricing")
    containers = [
        _price_tiers(item),
        passenger_pricing if isinstance(passenger_pricing, dict) else {},
        item,
    ]
    for container in containers:
        passengers = container.get("passengers")
        if isinstance(passengers, dict) and passengers:
            return dict(passengers)
    return {}


def _passenger_signature(item: dict | None) -> str:
    item = item or {}
    explicit = str(item.get("passenger_signature") or "").strip()
    if explicit:
        return explicit
    passengers = _passengers(item)
    if not passengers:
        return ""

    def count(*keys: str) -> int:
        for key in keys:
            if key not in passengers:
                continue
            try:
                return max(0, int(passengers.get(key) or 0))
            except (TypeError, ValueError):
                return 0
        return 0

    values = [
        count("adult", "adults"),
        count("child", "children"),
        count("elderly"),
    ]
    infant = count("infant", "infants")
    if infant:
        values.append(infant)
    return "+".join(str(value) for value in values)


def _explicit_unit_roundtrip(item: dict | None) -> float | None:
    item = item or {}
    tiers = _price_tiers(item)
    value = _to_float(tiers.get("unit_roundtrip"))
    if value is not None:
        return value
    for key in ("unit_roundtrip_price", "adult_roundtrip_price"):
        value = _to_float(item.get(key))
        if value is not None:
            return value
    return None


def _historical_unit_roundtrip(item: dict | None) -> tuple[float | None, str]:
    item = item or {}
    explicit = _explicit_unit_roundtrip(item)
    if explicit is not None:
        return explicit, "price_tiers.unit_roundtrip"

    scope = str(item.get("price_scope") or item.get("tracking_scope") or "").strip().lower()
    raw_total = None
    for key in ("roundtrip_price", "total_price", "roundtrip_total", "price"):
        raw_total = _to_float(item.get(key))
        if raw_total is not None:
            break
    if raw_total is None:
        return None, "missing_price"
    if scope in {ROUNDTRIP_TRACKING_SCOPE, "single_person_roundtrip", "unit_roundtrip"}:
        return raw_total, "explicit_per_person_scope"

    factor = _passenger_factor(item)
    if factor is None:
        return None, "missing_factor"
    return float(round(raw_total / factor)), "roundtrip_price/factor"


def _current_unit_roundtrip(
    item: dict | None,
    outbound: dict | None,
    return_flight: dict | None,
) -> float | None:
    explicit = _explicit_unit_roundtrip(item)
    if explicit is not None:
        return explicit
    outbound_price = _leg_price(item, "outbound", outbound)
    return_price = _leg_price(item, "return", return_flight)
    if outbound_price is not None and return_price is not None:
        return outbound_price + return_price
    factor = _passenger_factor(item)
    if factor is None:
        return None
    total = _roundtrip_price(item, outbound, return_flight)
    return float(round(total / factor)) if total is not None else None


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
        unit_roundtrip = _current_unit_roundtrip(plan, outbound, return_flight)
        price_tiers = dict(_price_tiers(plan))
        if unit_roundtrip is not None:
            price_tiers.setdefault("unit_roundtrip", unit_roundtrip)
        factor = _passenger_factor(plan)
        passengers = _passengers(plan)
        passenger_signature = _passenger_signature(plan)
        return {
            "flight_no": f"{outbound_no}+{return_no}".strip("+"),
            "is_roundtrip": True,
            "scope": "roundtrip",
            "outbound_flight": outbound_no,
            "return_flight": return_no,
            "roundtrip_price": total,
            "price": total,
            "unit_roundtrip_price": unit_roundtrip,
            "price_scope": ROUNDTRIP_TRACKING_SCOPE,
            "outbound_price": outbound_price,
            "return_price": return_price,
            "price_tiers": price_tiers,
            "passenger_factor": factor,
            "passengers": passengers,
            "passenger_signature": passenger_signature,
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
    target = _track_combo_key(flight_no)
    if not target:
        return None
    for flight in current_flights or []:
        if not isinstance(flight, dict):
            continue
        current = _track_combo_key(_flight_no(flight))
        if current == target:
            return flight
        if "+" in current and target in current.split("+"):
            for direction in ("outbound", "return"):
                leg = _item_leg_flight(flight, direction)
                if _track_combo_key(_flight_no(leg)) == target:
                    return leg
            if flight.get("is_roundtrip") or str(flight.get("scope") or "").lower() == "roundtrip":
                return None
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
    outbound_target = _track_combo_key(outbound_no)
    return_target = _track_combo_key(return_no)
    if not outbound_target or not return_target:
        return None
    for item in current_items or []:
        if not isinstance(item, dict):
            continue
        outbound = _item_leg_flight(item, "outbound")
        return_flight = _item_leg_flight(item, "return")
        if not outbound or not return_flight:
            continue
        current_outbound = _track_combo_key(_flight_no(outbound))
        current_return = _track_combo_key(_flight_no(return_flight))
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
    total = _current_unit_roundtrip(combo, outbound, return_flight)
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


def _current_collection_count(current_items: list[dict] | None) -> int:
    count = 0
    for item in current_items or []:
        if not isinstance(item, dict):
            continue
        if _item_leg_flight(item, "outbound") or _item_leg_flight(item, "return"):
            count += 1
        elif item.get("flight_no") or item.get("flight_combo"):
            count += 1
    return count


def _item_from_cache(item: dict | None) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("from_cache") or item.get("cache_hit") or str(item.get("source_status") or "").lower() == "cache":
        return True
    for key in ("outbound", "return", "return_flight"):
        child = item.get(key)
        if isinstance(child, dict) and _item_from_cache(child):
            return True
    return False


def _current_collection_uses_cache(current_items: list[dict] | None) -> bool:
    return any(_item_from_cache(item) for item in current_items or [] if isinstance(item, dict))


def _missing_quote_confidence(current_items: list[dict] | None, matched_any: bool = False) -> dict:
    collection_count = _current_collection_count(current_items)
    cache_used = _current_collection_uses_cache(current_items)
    if collection_count < 5:
        note = "本次采集覆盖可能不完整,该航班状态未知,下次采集再确认。"
        if cache_used:
            note += "使用缓存数据,该航班最新状态需实时核实。"
        return {
            "confidence": "low",
            "collection_count": collection_count,
            "cache_used": cache_used,
            "status": "coverage_uncertain",
            "note": note,
        }
    if cache_used:
        return {
            "confidence": "low",
            "collection_count": collection_count,
            "cache_used": True,
            "status": "cache_uncertain",
            "note": "使用缓存数据,该航班最新状态需实时核实。",
        }
    if matched_any:
        return {
            "confidence": "medium",
            "collection_count": collection_count,
            "cache_used": False,
            "status": "partial_unavailable",
            "note": f"本次该航线采集到{collection_count}个航班,但部分航段未获取到报价,建议渠道核实。",
        }
    return {
        "confidence": "medium",
        "collection_count": collection_count,
        "cache_used": False,
        "status": "unavailable",
        "note": f"本次该航线采集到{collection_count}个航班,但未出现该组合/航班;可能已售罄或停飞,建议渠道核实。",
    }


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
    previous_price, previous_price_source = _historical_unit_roundtrip(plan_a)
    previous_passengers = _passenger_signature(plan_a)
    combo = _find_roundtrip_combo(current_items, outbound_no, return_no)
    outbound_current = find_flight(current_items, outbound_no)
    return_current = find_flight(current_items, return_no)

    print(f"[方案追踪诊断] 航班={desc}")
    print(
        f"[方案追踪诊断] 上次价={previous_price}, 上次口径={ROUNDTRIP_TRACKING_LABEL}, "
        f"取数来源={previous_price_source}, "
        f"上次记录的是={json.dumps(plan_a, ensure_ascii=False, default=str)}"
    )

    outbound_norm = _track_combo_key(outbound_no)
    return_norm = _track_combo_key(return_no)
    print(
        f"[\u8ffd\u8e2a\u8bca\u65ad] norm\u540e \u4e0a\u6b21\u53bb\u7a0b={outbound_norm}, "
        f"\u4e0a\u6b21\u8fd4\u7a0b={return_norm}, \u5339\u914d={bool(combo or (outbound_current and return_current))}"
    )
    safe_log(
        f"[追踪池] 目标去程={outbound_norm} 在池中={bool(outbound_current or combo)} "
        f"目标返程={return_norm} 在池中={bool(return_current or combo)}"
    )

    if combo:
        current_price, outbound_price, return_price = _current_roundtrip_from_combo(combo)
        current_source = combo
        current_passengers = _passenger_signature(combo)
    elif outbound_current and return_current:
        outbound_price = _to_float(outbound_current.get("price"))
        return_price = _to_float(return_current.get("price"))
        current_price = (
            outbound_price + return_price
            if outbound_price is not None and return_price is not None
            else None
        )
        current_source = {"outbound": outbound_current, "return": return_current}
        current_passengers = (
            _passenger_signature(outbound_current)
            or _passenger_signature(return_current)
        )
    else:
        current_price = None
        outbound_price = _to_float((outbound_current or {}).get("price")) if outbound_current else None
        return_price = _to_float((return_current or {}).get("price")) if return_current else None
        current_source = {"outbound": outbound_current, "return": return_current}
        current_passengers = (
            _passenger_signature(outbound_current)
            or _passenger_signature(return_current)
        )

    print(
        f"[方案追踪诊断] 本次价={current_price}, 本次口径={ROUNDTRIP_TRACKING_LABEL}, "
        f"本次取到的是={json.dumps(current_source, ensure_ascii=False, default=str)}"
    )
    matched_any = bool(combo or outbound_current or return_current)
    collection_count = _current_collection_count(current_items)
    print(
        f"[追踪诊断] 上次航班={desc}, 本次采集是否包含该航班号={matched_any}, "
        f"本次该航线总航班数={collection_count}"
    )
    composition_note = ""
    if previous_passengers and current_passengers and previous_passengers != current_passengers:
        composition_note = (
            f"构成变化={previous_passengers}→{current_passengers}"
            "(全员价不跨轮对比)"
        )
    previous_log = "None" if previous_price is None else f"{previous_price:.1f}"
    current_log = "None" if current_price is None else f"{current_price:.1f}"
    safe_log(
        f"[追踪口径] 上次={previous_log}({ROUNDTRIP_TRACKING_LABEL}), "
        f"本次={current_log}({ROUNDTRIP_TRACKING_LABEL})"
        f"{' ' + composition_note if composition_note else ''}"
    )
    diff = current_price - previous_price if current_price is not None and previous_price is not None else None
    print(f"[方案追踪诊断] 差额={diff}")

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
        confidence = _missing_quote_confidence(current_items, matched_any=matched_any)
        if missing:
            msg = (
                f"上次推荐:{desc},{ROUNDTRIP_TRACKING_LABEL}{_format_price(previous_price)}。"
                f"本次:{'，'.join(available) + '，' if available else ''}"
                f"{'、'.join(missing)}本次未获取到报价,无法计算完整往返价。"
                f"{confidence['note']}"
            )
            return _change_payload(
                "partial_unavailable" if matched_any else confidence["status"],
                desc,
                previous_price,
                current_price,
                None,
                msg,
                ROUNDTRIP_TRACKING_SCOPE,
                {
                    "outbound_flight": outbound_no,
                    "return_flight": return_no,
                    "outbound_price": outbound_price,
                    "return_price": return_price,
                    "confidence": confidence["confidence"],
                    "collection_count": confidence["collection_count"],
                    "cache_used": confidence["cache_used"],
                },
            )
        msg = (
            f"上次推荐:{desc},{ROUNDTRIP_TRACKING_LABEL}{_format_price(previous_price)}。"
            f"本次未获取到该组合报价。{confidence['note']}"
        )
        return _change_payload(
            confidence["status"],
            desc,
            previous_price,
            None,
            None,
            msg,
            ROUNDTRIP_TRACKING_SCOPE,
            {
                "confidence": confidence["confidence"],
                "collection_count": confidence["collection_count"],
                "cache_used": confidence["cache_used"],
            },
        )

    if previous_price is None:
        safe_log(
            f"[追踪跳过] 原因=历史记录无单人口径 航班={desc} "
            f"历史来源={previous_price_source}"
        )
        msg = (
            f"上次推荐:{desc}。历史记录未保存{ROUNDTRIP_TRACKING_LABEL}价格，"
            "且缺少可反推的乘客费率因子，本次不展示涨跌。"
        )
        return _change_payload(
            "comparison_skipped",
            desc,
            None,
            current_price,
            None,
            msg,
            ROUNDTRIP_TRACKING_SCOPE,
            {
                "outbound_flight": outbound_no,
                "return_flight": return_no,
                "outbound_price": outbound_price,
                "return_price": return_price,
                "price_scope": ROUNDTRIP_TRACKING_SCOPE,
                "previous_price_source": previous_price_source,
                "passenger_composition_note": composition_note,
            },
        )

    status = _price_change_status(
        diff,
        previous_price,
        ROUNDTRIP_TRACKING_SCOPE,
        ROUNDTRIP_TRACKING_SCOPE,
    )
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
        f"上次推荐:{desc},{ROUNDTRIP_TRACKING_LABEL}{_format_price(previous_price)}。"
        f"本次同组合:{ROUNDTRIP_TRACKING_LABEL}{_format_price(current_price)}({verdict})。"
        f"（同{ROUNDTRIP_TRACKING_LABEL}口径对比）"
        f"{' ' + composition_note if composition_note else ''}"
    )
    return _change_payload(
        status,
        desc,
        previous_price,
        current_price,
        diff,
        msg,
        ROUNDTRIP_TRACKING_SCOPE,
        {
            "outbound_flight": outbound_no,
            "return_flight": return_no,
            "outbound_price": outbound_price,
            "return_price": return_price,
            "price_scope": ROUNDTRIP_TRACKING_SCOPE,
            "previous_price_source": previous_price_source,
            "previous_passenger_signature": previous_passengers,
            "current_passenger_signature": current_passengers,
            "passenger_composition_note": composition_note,
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
    print(f"[\u8ffd\u8e2a\u8bca\u65ad] norm\u540e \u4e0a\u6b21\u822a\u73ed={_track_combo_key(flight_no)}, \u5339\u914d={bool(same)}")
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
            "status": "coverage_uncertain",
            "flight_no": flight_no,
            "previous_price": previous_price,
            "current_price": current_price,
            "scope": "single",
            "msg": f"\u4e0a\u6b21\u63a8\u8350\u7684{flight_no}\u672c\u6b21\u672a\u83b7\u53d6\u5230\u540c\u53e3\u5f84\u5355\u7a0b\u62a5\u4ef7\uff0c\u65e0\u6cd5\u76f4\u63a5\u5bf9\u6bd4\uff0c\u5efa\u8bae\u5728\u6e20\u9053\u6838\u5b9e\u3002",
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
