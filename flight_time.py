"""Explicit flight datetimes and compatible elapsed layover minutes."""

from datetime import datetime


def parse_flight_datetime(value) -> datetime | None:
    """Preserve explicit offsets without inferring a date or timezone."""
    text = str(value or "").strip()
    if not text:
        return None

    for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"]:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed
    except (ValueError, TypeError):
        return None


def calculate_layover_minutes(arrival, departure) -> int:
    """Keep zero as the fallback for invalid, mixed or negative intervals."""
    arr = parse_flight_datetime(arrival)
    dep = parse_flight_datetime(departure)
    if not arr or not dep:
        return 0

    if (arr.utcoffset() is None) != (dep.utcoffset() is None):
        return 0

    try:
        diff = (dep - arr).total_seconds() / 60
        return max(0, int(diff))
    except Exception:
        return 0
