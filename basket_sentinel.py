"""基于本地配额台账的篮子任务缺勤哨兵。"""

from __future__ import annotations

import json
import os
from datetime import datetime, time as clock_time
from pathlib import Path
import uuid

from log_utils import safe_log


DEFAULT_BASKET_SENTINEL_THRESHOLD = "20:00"


def _parse_threshold(value: str) -> clock_time:
    text = str(value or DEFAULT_BASKET_SENTINEL_THRESHOLD).strip()
    try:
        return datetime.strptime(text, "%H:%M").time()
    except ValueError:
        return datetime.strptime(DEFAULT_BASKET_SENTINEL_THRESHOLD, "%H:%M").time()


def _entry_is_today_basket(entry: dict, day_text: str) -> bool:
    round_id = str((entry or {}).get("round_id") or "")
    if not round_id.startswith("basket_"):
        return False
    recorded_day = str((entry or {}).get("recorded_at") or "")[:10]
    if recorded_day == day_text:
        return True
    return round_id.startswith(f"basket_{day_text.replace('-', '')}")


def evaluate_basket_sentinel(
    usage_payload: dict | None,
    *,
    now: datetime | None = None,
    threshold: str = DEFAULT_BASKET_SENTINEL_THRESHOLD,
) -> dict:
    current = now or datetime.now()
    day_text = current.date().isoformat()
    has_basket = any(
        _entry_is_today_basket(entry, day_text)
        for entry in ((usage_payload or {}).get("entries") or [])
        if isinstance(entry, dict)
    )
    after_threshold = current.time() > _parse_threshold(threshold)
    due = bool(after_threshold and not has_basket)
    return {
        "due": due,
        "has_basket": has_basket,
        "date": day_text,
        "threshold": threshold,
        "reason": "今日篮子未运行" if due else "",
    }


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def run_basket_sentinel(
    *,
    usage_payload: dict | None,
    notifier,
    state_path: str | Path,
    now: datetime | None = None,
    threshold: str = DEFAULT_BASKET_SENTINEL_THRESHOLD,
) -> dict:
    current = now or datetime.now()
    result = evaluate_basket_sentinel(
        usage_payload,
        now=current,
        threshold=threshold,
    )
    result["notified"] = False
    if not result["due"]:
        return result
    path = Path(state_path)
    state = _load_state(path)
    if str(state.get("last_notified_day") or "") == result["date"]:
        result["status"] = "already_notified"
        return result
    title = "[篮子哨兵] 今日篮子未运行"
    content = "今日固定篮子尚无采集台账记录，请检查 Windows 任务计划与 basket.log。"
    safe_log(title)
    try:
        notified = bool(notifier(title, content))
    except Exception as exc:
        safe_log(f"[篮子哨兵] 通知失败 原因={exc}")
        result["status"] = "notify_failed"
        return result
    result["notified"] = notified
    result["status"] = "notified" if notified else "notify_failed"
    if notified:
        _write_state(
            path,
            {
                "last_notified_day": result["date"],
                "notified_at": current.isoformat(timespec="seconds"),
            },
        )
    return result
