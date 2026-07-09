"""Flight-combo normalization helpers shared by collectors and storage."""

from __future__ import annotations

import re

_SEPARATOR_RE = re.compile("[+|/,\uFF0C\u3001]+")
_FLIGHT_NO_RE = re.compile(r"^([A-Z0-9]{1,3}?)(0*\d+)([A-Z]?)$")


def normalize_flight_no(value: str | None) -> str:
    """Normalize one flight number without losing airline-code semantics."""
    text = re.sub(r"\s+", "", str(value or "").upper())
    if not text:
        return ""
    match = _FLIGHT_NO_RE.match(text)
    if not match:
        return text
    prefix, number, suffix = match.groups()
    try:
        normalized_number = str(int(number))
    except ValueError:
        normalized_number = number.lstrip("0") or "0"
    return f"{prefix}{normalized_number}{suffix}"


def normalize_combo(combo: str | None) -> str:
    """Normalize a flight-combo key across sources.

    This treats separators as formatting only. Ticketing semantics such as
    through-ticket/self-transfer should be carried in explicit fields, not in
    the separator character.
    """
    raw = str(combo or "").strip()
    if not raw:
        return ""
    compact = re.sub(r"\s+", "", raw.upper())
    parts = [part for part in _SEPARATOR_RE.split(compact) if part]
    if not parts:
        return ""
    return "+".join(normalize_flight_no(part) for part in parts)
