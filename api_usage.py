"""本地 API 配额用量台账。仅记录真实 HTTP 请求次数。"""

from __future__ import annotations

import json
import uuid
from datetime import date
from pathlib import Path


DEFAULT_USAGE_PATH = Path(__file__).parent / "data" / "api_usage.json"


def load_usage(path: str | Path = DEFAULT_USAGE_PATH) -> dict:
    usage_path = Path(path)
    if not usage_path.exists():
        return {"version": 1, "dates": {}}
    try:
        payload = json.loads(usage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "dates": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "dates": {}}
    dates = payload.get("dates")
    if not isinstance(dates, dict):
        dates = {}
    return {"version": 1, "dates": dates}


def record_actual_requests(
    counts: dict[str, int],
    *,
    path: str | Path = DEFAULT_USAGE_PATH,
    day: str | None = None,
) -> dict:
    usage_path = Path(path)
    payload = load_usage(usage_path)
    day_key = str(day or date.today().isoformat())
    day_counts = payload["dates"].setdefault(day_key, {})
    for source, raw_count in (counts or {}).items():
        count = max(0, int(raw_count or 0))
        if count:
            day_counts[str(source)] = int(day_counts.get(str(source), 0) or 0) + count

    usage_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = usage_path.with_suffix(f"{usage_path.suffix}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temp_path.replace(usage_path)
    return payload


def usage_snapshot(payload: dict, *, day: str | None = None) -> dict:
    day_key = str(day or date.today().isoformat())
    dates = (payload or {}).get("dates") or {}
    today_counts = {
        str(source): int(count or 0)
        for source, count in (dates.get(day_key) or {}).items()
    }
    cumulative: dict[str, int] = {}
    for source_counts in dates.values():
        if not isinstance(source_counts, dict):
            continue
        for source, count in source_counts.items():
            cumulative[str(source)] = cumulative.get(str(source), 0) + int(count or 0)
    return {"today": today_counts, "cumulative": cumulative}
