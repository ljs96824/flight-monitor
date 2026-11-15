"""Duration and explicit flight-clock evidence without a timezone database."""

import math
import re

from flight_time import parse_flight_datetime


FIXED_UTC_OFFSET_MINUTES_BY_IATA = {
    "PVG": 480, "SHA": 480, "PEK": 480, "PKX": 480, "CAN": 480, "SZX": 480,
    "CTU": 480, "TFU": 480, "HGH": 480, "NKG": 480, "XIY": 480, "CKG": 480,
    "WUH": 480, "CSX": 480, "TAO": 480, "XMN": 480, "FOC": 480, "KMG": 480,
    "URC": 480, "CGO": 480, "TSN": 480, "DLC": 480, "SHE": 480, "HRB": 480,
    "SYX": 480, "HAK": 480, "INC": 480, "KWL": 480, "NNG": 480, "TNA": 480,
    "WNZ": 480, "NGB": 480, "WUX": 480, "ZUH": 480, "JJN": 480, "SWA": 480,
    "HFE": 480, "KHN": 480, "TYN": 480, "LHW": 480,
    "HKG": 480, "MFM": 480, "TPE": 480, "TSA": 480, "KHH": 480, "RMQ": 480,
    "TNN": 480, "HUN": 480, "CYI": 480, "MZG": 480, "KNH": 480,
    "SIN": 480, "KUL": 480, "BKI": 480, "PEN": 480, "MNL": 480, "CEB": 480,
    "DPS": 480, "PER": 480,
    "NRT": 540, "HND": 540, "KIX": 540, "ITM": 540, "UKB": 540, "NGO": 540,
    "FUK": 540, "CTS": 540, "OKA": 540, "KMI": 540, "KMJ": 540, "KOJ": 540,
    "SDJ": 540, "HIJ": 540, "TAK": 540, "FSZ": 540, "MYJ": 540, "OIT": 540,
    "NGS": 540, "ICN": 540, "GMP": 540, "PUS": 540, "CJU": 540, "TAE": 540,
    "CJJ": 540, "MWX": 540,
    "BKK": 420, "DMK": 420, "HKT": 420, "CNX": 420, "KBV": 420, "USM": 420,
    "SGN": 420, "HAN": 420, "DAD": 420, "CXR": 420, "PQC": 420, "CGK": 420,
    "SUB": 420,
    "DXB": 240, "AUH": 240, "SHJ": 240, "MCT": 240,
    "DOH": 180, "RUH": 180, "JED": 180, "DMM": 180, "IST": 180, "SAW": 180,
    "SVO": 180, "DME": 180, "VKO": 180,
}

SAME_LOCAL_CLOCK_AIRPORT_GROUPS = (
    {"LHR", "LGW", "STN", "LTN", "LCY"}, {"CDG", "ORY", "BVA"},
    {"JFK", "LGA", "EWR"}, {"ORD", "MDW"}, {"DCA", "IAD", "BWI"},
    {"DAL", "DFW"}, {"IAH", "HOU"}, {"LAX", "BUR", "LGB", "ONT", "SNA"},
    {"SFO", "OAK", "SJC"}, {"MXP", "LIN", "BGY"}, {"FCO", "CIA"},
    {"GRU", "CGH", "VCP"},
)


def parse_duration_minutes(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return max(0, round(value)) or None if math.isfinite(value) and value > 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if re.fullmatch(r"\d+", text):
        try:
            return int(text) or None
        except ValueError:
            return None
    if re.fullmatch(r"\d+\.\d+", text):
        number = float(text)
        return round(number) or None if math.isfinite(number) and number > 0 else None
    colon = re.fullmatch(r"(\d{1,3}):([0-5]\d)", text)
    if colon:
        return (int(colon[1]) * 60 + int(colon[2])) or None
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", text, re.IGNORECASE)
    if match and any(match.groups()):
        return (int(match[1] or 0) * 60 + int(match[2] or 0)) or None
    match = re.fullmatch(
        r"(?:(\d+)\s*(?:hours?|hrs?|h|小时|小時|时|時))?\s*"
        r"(?:(\d+)\s*(?:minutes?|mins?|m|分钟|分鐘|分))?", text, re.IGNORECASE,
    )
    if match and any(match.groups()):
        return (int(match[1] or 0) * 60 + int(match[2] or 0)) or None
    match = re.fullmatch(r"(\d+)\s*(?:小时|小時|时|時)半", text)
    return int(match[1]) * 60 + 30 if match else None


def resolve_fixed_utc_offset_minutes(airport_code) -> int | None:
    return FIXED_UTC_OFFSET_MINUTES_BY_IATA.get(str(airport_code or "").strip().upper())


def are_same_local_clock_airports(airport_a, airport_b) -> bool:
    a, b = str(airport_a or "").strip().upper(), str(airport_b or "").strip().upper()
    if a == b:
        return True
    if not a or not b:
        return False
    off_a, off_b = resolve_fixed_utc_offset_minutes(a), resolve_fixed_utc_offset_minutes(b)
    return (off_a is not None and off_a == off_b) or any(a in group and b in group for group in SAME_LOCAL_CLOCK_AIRPORT_GROUPS)


def _complete_datetime(value):
    if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", str(value or "")):
        return None
    return parse_flight_datetime(value)


def compute_endpoint_elapsed_minutes(dep_time, dep_airport, arr_time, arr_airport) -> int | None:
    dep, arr = _complete_datetime(dep_time), _complete_datetime(arr_time)
    if dep is None or arr is None:
        return None
    if dep.utcoffset() is not None and arr.utcoffset() is not None:
        elapsed = (arr - dep).total_seconds() / 60
    elif dep.utcoffset() is None and arr.utcoffset() is None:
        off_dep = resolve_fixed_utc_offset_minutes(dep_airport)
        off_arr = resolve_fixed_utc_offset_minutes(arr_airport)
        if off_dep is None or off_arr is None:
            return None
        elapsed = (arr - dep).total_seconds() / 60 - off_arr + off_dep
    else:
        return None
    return int(elapsed) if elapsed >= 1 else None


def build_connection_layover(previous: dict, following: dict, *, mark_computed_zero=True) -> dict:
    """Preserve signed clock evidence before legacy zero-fallback consumers run."""
    a = str(previous.get("arr_airport") or "").strip().upper()
    b = str(following.get("dep_airport") or "").strip().upper()
    layover = {"city": previous.get("arr_city") or a, "airport": a}
    if a and b and a != b:
        layover.update(airport_change=True, next_airport=b)
    arr, dep = _complete_datetime(previous.get("arr_time")), _complete_datetime(following.get("dep_time"))
    reason = None
    if arr is None or dep is None:
        reason = "timestamp_missing"
    elif (arr.utcoffset() is None) != (dep.utcoffset() is None) or (arr.utcoffset() is None and not are_same_local_clock_airports(a, b)):
        reason = "timezone_unverified"
    else:
        diff = (dep - arr).total_seconds()
        if diff < 0:
            reason = "segment_time_inverted"
        else:
            layover["wait_minutes"] = int(diff // 60)
            if layover["wait_minutes"] == 0 and mark_computed_zero:
                layover["_wait_computed"] = True
    if reason:
        layover["wait_unverified_reason"] = reason
    return layover
