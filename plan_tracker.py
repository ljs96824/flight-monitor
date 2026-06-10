"""Track previously pushed plans and compare them with current results."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from filename_utils import sanitize_filename


BASE_DIR = Path(__file__).parent
DEFAULT_DATA_DIR = BASE_DIR / "data" / "pushed_plans"


def _storage_dir(data_dir=None) -> Path:
    return Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR


def _storage_path(sub_id, data_dir=None) -> Path:
    return _storage_dir(data_dir) / f"{sanitize_filename(sub_id)}.json"


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


def _plan_record(plan: dict, index: int) -> dict | None:
    flight = _plan_primary_flight(plan)
    flight_no = _flight_no(flight)
    price = _to_float(plan.get("price") or flight.get("price"))
    if not flight_no:
        return None
    return {
        "flight_no": flight_no,
        "price": price,
        "label": plan.get("label") or f"方案{chr(65 + index)}",
        "pushed_at": datetime.now().isoformat(timespec="seconds"),
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


def track_plan_status(sub_id, current_flights: list[dict] | None, data_dir=None) -> dict | None:
    last = load_pushed_plans(sub_id, data_dir)
    plan_a = (last.get("last_pushed") or {}).get("plan_a") or {}
    flight_no = plan_a.get("flight_no")
    if not flight_no:
        return None
    same = find_flight(current_flights, flight_no)
    previous_price = _to_float(plan_a.get("price"))
    if not same:
        return {
            "status": "sold_out",
            "flight_no": flight_no,
            "previous_price": previous_price,
            "msg": f"上次推荐的{flight_no}当前已无报价(可能售罄或停售)",
        }
    current_price = _to_float(same.get("price"))
    if previous_price is None or current_price is None:
        return {
            "status": "stable",
            "flight_no": flight_no,
            "previous_price": previous_price,
            "current_price": current_price,
            "msg": f"上次推荐的{flight_no}仍有报价,价格需支付页确认",
        }
    diff = current_price - previous_price
    if diff > 50:
        return {
            "status": "price_up",
            "flight_no": flight_no,
            "previous_price": previous_price,
            "current_price": current_price,
            "price_diff": diff,
            "msg": f"上次推荐的{flight_no}已涨价¥{diff:,.0f}(¥{previous_price:,.0f}→¥{current_price:,.0f})",
        }
    if diff < -50:
        return {
            "status": "price_down",
            "flight_no": flight_no,
            "previous_price": previous_price,
            "current_price": current_price,
            "price_diff": diff,
            "msg": f"上次推荐的{flight_no}又降了¥{abs(diff):,.0f}",
        }
    return {
        "status": "stable",
        "flight_no": flight_no,
        "previous_price": previous_price,
        "current_price": current_price,
        "price_diff": diff,
        "msg": f"上次推荐的{flight_no}价格稳定(¥{current_price:,.0f})",
    }
