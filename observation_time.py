"""Observation timestamp normalization through the project timezone."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from project_time import SHANGHAI_TZ


@dataclass(frozen=True)
class CanonicalObservationTime:
    observed_at_utc: str
    observed_at_shanghai: str
    observed_day_shanghai: str


def parse_observed_at(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))


def timestamp_kind(value: str | datetime) -> str:
    try:
        parsed = parse_observed_at(value)
    except (TypeError, ValueError):
        return "invalid"
    return "aware" if parsed.tzinfo is not None else "naive"


def canonicalize_observed_at(
    value: str | datetime | None = None,
    *,
    assume_naive_shanghai: bool = True,
) -> CanonicalObservationTime:
    parsed = datetime.now(timezone.utc) if value is None else parse_observed_at(value)
    if parsed.tzinfo is None:
        if not assume_naive_shanghai:
            raise ValueError("naive timestamp requires assume_naive_shanghai=True")
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    observed_at_utc = parsed.astimezone(timezone.utc)
    observed_at_shanghai = observed_at_utc.astimezone(SHANGHAI_TZ)
    return CanonicalObservationTime(
        observed_at_utc=observed_at_utc.isoformat(timespec="seconds"),
        observed_at_shanghai=observed_at_shanghai.isoformat(timespec="seconds"),
        observed_day_shanghai=observed_at_shanghai.date().isoformat(),
    )


def resolve_observed_day_shanghai(row: dict) -> tuple[str | None, str]:
    """Resolve the T-curve day and identify its evidence path."""
    if bool(row.get("legacy_time_ambiguous")):
        return None, "legacy_time_ambiguous"
    explicit = str(row.get("observed_day_shanghai") or "").strip()
    if explicit:
        try:
            return datetime.fromisoformat(explicit).date().isoformat(), "canonical"
        except ValueError:
            return None, "legacy_time_ambiguous"
    observed_at = row.get("observed_at")
    kind = timestamp_kind(observed_at)
    if kind == "invalid":
        return None, "legacy_time_ambiguous"
    canonical = canonicalize_observed_at(
        observed_at,
        assume_naive_shanghai=True,
    )
    return canonical.observed_day_shanghai, "legacy_fallback"
