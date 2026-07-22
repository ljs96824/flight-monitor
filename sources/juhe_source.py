"""Juhe domestic flight adapter.

The exact Juhe endpoint and response fields depend on the purchased API
package. This adapter keeps the endpoint configurable and accepts several
common field aliases so the downstream project receives stable flight records.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from filename_utils import sanitize_filename
from domestic_fare_rules import get_aircraft_name
from log_utils import safe_log
from sources.base import FlightSource
from subscription_preflight import shanghai_today


CACHE_TTL_MINUTES = 15
QUOTA_CODES = {
    code.strip()
    for code in os.getenv("JUHE_QUOTA_CODES", "112,10012").split(",")
    if code.strip()
}

AIRCRAFT_NAMES = {
    "73U": "波音737",
    "738": "波音737-800",
    "737": "波音737",
    "32N": "空客A320neo",
    "320": "空客A320",
    "321": "空客A321",
    "32J": "空客A321",
    "333": "空客A330",
    "33": "空客A330",
    "787": "波音787",
    "789": "波音787-9",
}


def _to_float(value):
    if value in (None, ""):
        return None
    try:
        if isinstance(value, str):
            value = value.replace(",", "").replace("￥", "").replace("¥", "").strip()
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(item: dict, *keys, default=""):
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return default


def _duration_minutes(value) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        minutes = int(value)
        return minutes if minutes > 0 else None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        minutes = int(text)
        return minutes if minutes > 0 else None
    hours = 0
    minutes = 0
    hour_match = re.search(r"(\d+)\s*(?:h|hr|hour|hours|\u5c0f\u65f6|\u5c0f\u6642)", text, re.IGNORECASE)
    minute_match = re.search(r"(\d+)\s*(?:m|min|minute|minutes|\u5206\u949f|\u5206\u9418)", text, re.IGNORECASE)
    if hour_match:
        hours = int(hour_match.group(1))
    if minute_match:
        minutes = int(minute_match.group(1))
    total = hours * 60 + minutes
    return total if total > 0 else None


def _as_flight_items(raw: dict) -> list[dict]:
    result = raw.get("result") if isinstance(raw, dict) else []
    if isinstance(result, dict) and isinstance(result.get("flightInfo"), list):
        return [item for item in result["flightInfo"] if isinstance(item, dict)]
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        for key in ("list", "flights", "data", "items"):
            value = result.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    data = raw.get("data") if isinstance(raw, dict) else []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _cache_dir() -> Path:
    return Path("data") / "cache"


def _format_departure_date(date_str: str) -> str:
    text = str(date_str or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text.replace("/", "-")


def _parse_departure_date(date_str: str) -> date | None:
    text = _format_departure_date(date_str)
    try:
        return date.fromisoformat(str(text)[:10])
    except (TypeError, ValueError):
        return None


def _is_success_response(raw) -> bool:
    if not isinstance(raw, dict):
        return False
    if "error_code" in raw and str(raw.get("error_code")) != "0":
        return False
    if "resultcode" in raw and str(raw.get("resultcode")) not in {"0", "200"}:
        return False
    return True


def _is_invalid_date_response(raw) -> bool:
    if not isinstance(raw, dict):
        return False
    return str(raw.get("error_code")) == "281801"


def _response_codes(raw: dict) -> tuple[str, str]:
    resultcode = str((raw or {}).get("resultcode") or "").strip()
    error_code = str((raw or {}).get("error_code") or "").strip()
    return resultcode, error_code


def _is_quota_response(raw) -> bool:
    if not isinstance(raw, dict):
        return False
    return any(code in QUOTA_CODES for code in _response_codes(raw) if code)


def _quota_error_text(raw: dict) -> str:
    resultcode, error_code = _response_codes(raw)
    code = resultcode if resultcode in QUOTA_CODES else error_code
    return f"配额不足({code or 'unknown'})"


def _combine_date_time(date_value, time_value) -> str:
    date_text = str(date_value or "").strip()
    time_text = str(time_value or "").strip()
    if date_text and time_text:
        return f"{date_text} {time_text}"
    return time_text or date_text


def _to_int(value, default=0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_codeshare(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes", "y"}


class JuheSource(FlightSource):
    name = "juhe"
    role = "search"

    def preflight_skip(
        self,
        origin: str,
        dest: str,
        date_str: str,
        cabin_class: str = "economy",
    ) -> dict | None:
        departure_date = _parse_departure_date(date_str)
        if departure_date is None or departure_date >= shanghai_today():
            return None
        return {
            "flights": [],
            "source": self.name,
            "raw": {},
            "source_status": "skipped_past_date",
            "skipped_reason": "过去日期不可售",
        }

    def fetch(
        self, origin: str, dest: str, date_str: str, cabin_class: str = "economy"
    ) -> dict:
        skipped = self.preflight_skip(origin, dest, date_str, cabin_class)
        if skipped is not None:
            safe_log(f"[juhe] skip past date {date_str}")
            return skipped

        key = os.getenv("JUHE_FLIGHT_KEY")
        safe_log(f"[juhe] key_exists={bool(key)}, start {origin}->{dest} {date_str}")
        if not key:
            safe_log("[juhe] warning: JUHE_FLIGHT_KEY is not configured, skipped")
            return {
                "flights": [],
                "source": self.name,
                "raw": {},
                "source_status": "not_configured",
            }

        collected_at = datetime.now().isoformat(timespec="seconds")
        cached = self._read_cache(origin, dest, date_str, cabin_class)
        if cached is not None:
            flights = self.normalize(self.parse(cached), collected_at=collected_at)
            return {
                "flights": flights,
                "source": self.name,
                "raw": cached,
                "source_status": "cache",
                "collected_at": collected_at,
            }

        endpoint = os.getenv("JUHE_FLIGHT_ENDPOINT", "https://apis.juhe.cn/flight/query")
        import requests

        params = self.build_request_params(origin, dest, date_str, key)
        debug_params = dict(params)
        debug_params["key"] = "***"
        safe_log(f"[juhe] request params: {debug_params}")
        response = requests.get(endpoint, params=params, timeout=20)
        safe_log(f"[juhe raw] status_code={response.status_code}")
        safe_log(f"[juhe raw] {response.text[:2000]}")
        response.raise_for_status()
        raw = response.json()
        if _is_invalid_date_response(raw):
            safe_log(f"[juhe] invalid date skipped {date_str}")
            return {
                "flights": [],
                "source": self.name,
                "raw": raw,
                "source_status": "invalid_date",
                "skipped_reason": "日期无效或已过期",
                "collected_at": collected_at,
            }
        if _is_quota_response(raw):
            resultcode, error_code = _response_codes(raw)
            error = _quota_error_text(raw)
            safe_log(f"[juhe] failed quota {date_str}: {error}")
            return {
                "flights": [],
                "source": self.name,
                "raw": raw,
                "source_status": "failed_quota",
                "error": error,
                "resultcode": resultcode,
                "error_code": error_code,
                "reason": raw.get("reason") or raw.get("message") or "",
                "quota_code": resultcode or error_code,
                "collected_at": collected_at,
            }
        if not _is_success_response(raw):
            resultcode, error_code = _response_codes(raw)
            reason = raw.get("reason") or raw.get("message") or "返回失败"
            error = f"{reason}({resultcode or error_code or 'unknown'})"
            safe_log(f"[juhe] failed {date_str}: {error}")
            return {
                "flights": [],
                "source": self.name,
                "raw": raw,
                "source_status": "failed",
                "error": error,
                "resultcode": resultcode,
                "error_code": error_code,
                "reason": reason,
                "collected_at": collected_at,
            }
        if _is_success_response(raw):
            self._write_cache(origin, dest, date_str, cabin_class, raw)
        flights = self.normalize(self.parse(raw), collected_at=collected_at)
        return {
            "flights": flights,
            "source": self.name,
            "raw": raw,
            "source_status": "success",
            "collected_at": collected_at,
        }

    def build_request_params(
        self, origin: str, dest: str, date_str: str, key: str
    ) -> dict[str, str]:
        return {
            "key": key,
            "departure": str(origin or "").upper(),
            "arrival": str(dest or "").upper(),
            "departureDate": _format_departure_date(date_str),
        }

    def parse(self, raw: dict) -> list[dict]:
        return list(_as_flight_items(raw or {}))

    def normalize(self, parsed: list[dict], collected_at: str | None = None) -> list[dict]:
        normalized = []
        collected_at = collected_at or datetime.now().isoformat(timespec="seconds")
        for item in parsed or []:
            if _is_codeshare(item.get("isCodeShare")):
                continue

            flight_no = str(_first(item, "flightNo", "flight_no", "flightNumber")).strip()
            price = _to_float(_first(item, "ticketPrice", "price", "adultPrice"))
            if price <= 0:
                continue

            equipment = str(_first(item, "equipment", "planeType", "aircraft")).strip()
            aircraft = get_aircraft_name(equipment)
            transfer_num = _to_int(item.get("transferNum"), 1)
            stops = max(0, transfer_num - 1)
            dep_airport = _first(item, "departure", "departureAirport", "depAirport")
            arr_airport = _first(item, "arrival", "arrivalAirport", "arrAirport")
            departure_time = _combine_date_time(
                _first(item, "departureDate"), _first(item, "departureTime")
            )
            arrival_time = _combine_date_time(
                _first(item, "arrivalDate"), _first(item, "arrivalTime")
            )
            duration_text = _first(item, "duration")
            duration_min = _duration_minutes(duration_text)
            segment = {
                "flight_no": flight_no,
                "airline": _first(item, "airlineName", "airline"),
                "dep_airport": dep_airport,
                "arr_airport": arr_airport,
                "dep_time": departure_time,
                "arr_time": arrival_time,
                "aircraft": aircraft,
            }
            row = {
                "flight_no": flight_no,
                "flight_combo": flight_no,
                "airline": _first(item, "airline", "airlineCode"),
                "airline_name": _first(item, "airlineName"),
                "aircraft": aircraft,
                "aircraft_code": equipment,
                "departure_airport": dep_airport,
                "arrival_airport": arr_airport,
                "departure_name": _first(item, "departureName"),
                "arrival_name": _first(item, "arrivalName"),
                "departure_date": _first(item, "departureDate"),
                "arrival_date": _first(item, "arrivalDate"),
                "_source_raw_departure_time": _first(item, "departureTime"),
                "departure_time": departure_time,
                "arrival_time": arrival_time,
                "duration_str": duration_text,
                "duration_min": duration_min,
                "total_duration_min": duration_min,
                "stops": stops,
                "transfer_num": transfer_num,
                "price": price,
                "ticket_price": price,
                "is_codeshare": _is_codeshare(item.get("isCodeShare")),
                "data_source": self.name,
                "source": self.name,
                "source_role": self.role,
                "domestic_realtime_quote": True,
                "collected_at": collected_at,
                "price_note": "票面价，实付以支付页为准",
                "price_includes": "票面价，不含机建燃油拆分",
                "segments": [segment],
                "layovers": [],
                "raw": item,
            }
            normalized.append(row)
        return normalized

    def _cache_path(self, origin: str, dest: str, date_str: str, cabin_class: str) -> Path:
        safe = sanitize_filename("_".join([origin.upper(), dest.upper(), date_str, cabin_class]))
        return _cache_dir() / f"juhe_{safe}.json"

    def _read_cache(self, origin: str, dest: str, date_str: str, cabin_class: str):
        path = self._cache_path(origin, dest, date_str, cabin_class)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(payload.get("fetched_at", ""))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if datetime.now() - fetched_at > timedelta(minutes=CACHE_TTL_MINUTES):
            return None
        raw = payload.get("raw")
        if not _is_success_response(raw):
            safe_log("[juhe] cached response is not successful, ignoring cache")
            return None
        return raw

    def _write_cache(self, origin: str, dest: str, date_str: str, cabin_class: str, raw):
        path = self._cache_path(origin, dest, date_str, cabin_class)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "raw": raw,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
