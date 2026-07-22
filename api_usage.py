"""本地 API 配额用量台账。仅记录真实 HTTP 请求次数。

任务执行未经用户明示授权不得发起真实外部API调用;获授权时须报告执行前后台账值。
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from pathlib import Path


DEFAULT_USAGE_PATH = Path(__file__).parent / "data" / "api_usage.json"


def load_usage(path: str | Path = DEFAULT_USAGE_PATH) -> dict:
    usage_path = Path(path)
    if not usage_path.exists():
        return {"version": 2, "dates": {}, "entries": []}
    try:
        payload = json.loads(usage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 2, "dates": {}, "entries": []}
    if not isinstance(payload, dict):
        return {"version": 2, "dates": {}, "entries": []}
    dates = payload.get("dates")
    if not isinstance(dates, dict):
        dates = {}
    entries = payload.get("entries")
    if not isinstance(entries, list):
        entries = []
    return {"version": 2, "dates": dates, "entries": entries}


def record_actual_requests(
    counts: dict[str, int],
    *,
    path: str | Path = DEFAULT_USAGE_PATH,
    day: str | None = None,
    round_id: str | None = None,
    recorded_at: str | None = None,
) -> dict:
    usage_path = Path(path)
    payload = load_usage(usage_path)
    day_key = str(day or date.today().isoformat())
    day_counts = payload["dates"].setdefault(day_key, {})
    actual_counts = {}
    for source, raw_count in (counts or {}).items():
        count = max(0, int(raw_count or 0))
        if count:
            source_name = str(source)
            actual_counts[source_name] = count
            day_counts[source_name] = int(day_counts.get(source_name, 0) or 0) + count

    if actual_counts:
        payload["entries"].append(
            {
                "recorded_at": str(
                    recorded_at
                    or datetime.now().astimezone().isoformat(timespec="seconds")
                ),
                "round_id": str(round_id or "unknown"),
                "day": day_key,
                "counts": actual_counts,
            }
        )

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
