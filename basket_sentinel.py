"""基于本地配额台账与当日轮档的篮子任务缺勤哨兵。

台账里没有当日篮子条目时，再读当日轮档里篮子轮次的开始/结束标记，区分：未运行、已运行(零请求)、
已运行但被阻断、未正常结束、运行证据不可读。只读，不向台账或轮档写任何内容。
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, time as clock_time
from pathlib import Path
import uuid

from log_utils import safe_log
from research_cohort import research_runtime_enabled


DEFAULT_BASKET_SENTINEL_THRESHOLD = "20:00"

_ROUND_START = re.compile(r"^===== \[轮档开始\] round_id=(\S+) started_at=\S+ =====$")
_ROUND_END = re.compile(r"^===== \[轮档结束\] round_id=(\S+) status=(\S+) =====$")
_ROUND_REASON = re.compile(r"^\[(?:篮子跳过|研究状态冲突|篮子中断)\]")
_RAN_STATUSES = {"ok", "partial"}
_DUE_REASONS = {
    "none": "今日篮子未运行",
    "blocked": "今日篮子已运行但被阻断",
    "abnormal": "今日篮子未正常结束",
    "unreadable": "今日篮子运行情况无法确认",
}


def read_basket_round_evidence(archive_root: str | Path, day: date) -> dict:
    """只读解析当日轮档中的篮子轮次：结束状态(None=未见结束标记)与第一条跳过/冲突/中断原因行。

    文件不存在视为当日没有轮次；读取失败时 readable=False 并记下异常类型，不抛错。
    """
    path = Path(archive_root) / f"{day:%Y%m%d}.log"
    if not path.exists():
        return {"readable": True, "path": str(path), "rounds": []}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"readable": False, "path": str(path), "rounds": [], "error": type(exc).__name__}
    rounds: dict[str, dict] = {}
    current = None
    for line in text.splitlines():
        start = _ROUND_START.match(line)
        if start:
            round_id = start.group(1)
            current = round_id if round_id.startswith("basket_") else None
            if current:
                rounds.setdefault(current, {"round_id": current, "status": None, "reason": ""})
            continue
        end = _ROUND_END.match(line)
        if end:
            if end.group(1).startswith("basket_"):
                rounds.setdefault(end.group(1), {"round_id": end.group(1), "status": None, "reason": ""})
                rounds[end.group(1)]["status"] = end.group(2)
            current = None
            continue
        if current and not rounds[current]["reason"] and _ROUND_REASON.match(line):
            rounds[current]["reason"] = line[:160]
    return {"readable": True, "path": str(path), "rounds": list(rounds.values())}


def classify_basket_rounds(evidence: dict | None) -> tuple[str, dict | None]:
    """ran=当日有篮子轮次以 ok/partial 结束；blocked/abnormal 取当日最后一个篮子轮次；none=无轮次；unreadable=读不到。"""
    if evidence is None:
        return "none", None
    if not evidence.get("readable", True):
        return "unreadable", None
    rounds = [item for item in (evidence.get("rounds") or []) if isinstance(item, dict)]
    if not rounds:
        return "none", None
    for item in rounds:
        if item.get("status") in _RAN_STATUSES:
            return "ran", item
    last = rounds[-1]
    return ("blocked" if last.get("status") == "blocked" else "abnormal"), last


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
    round_evidence: dict | None = None,
) -> dict:
    current = now or datetime.now()
    day_text = current.date().isoformat()
    has_basket = any(
        _entry_is_today_basket(entry, day_text)
        for entry in ((usage_payload or {}).get("entries") or [])
        if isinstance(entry, dict)
    )
    run_class, basket_round = ("ledger", None) if has_basket else classify_basket_rounds(round_evidence)
    after_threshold = current.time() > _parse_threshold(threshold)
    due = bool(after_threshold and run_class in _DUE_REASONS)
    return {
        "due": due,
        "has_basket": has_basket,
        "date": day_text,
        "threshold": threshold,
        "reason": _DUE_REASONS[run_class] if due else "",
        "run_class": run_class,
        "basket_round": basket_round,
    }


def _sentinel_message(result: dict, round_evidence: dict | None, expectation_reason: str) -> tuple[str, str]:
    run_class = result["run_class"]
    basket_round = result.get("basket_round") or {}
    round_id = basket_round.get("round_id") or ""
    title = f"[篮子哨兵] {result['reason']}"
    if run_class == "blocked":
        content = (f"今日篮子轮次 {round_id} 已运行，以 blocked 结束，没有产生采集台账记录；"
                   f"原因：{basket_round.get('reason') or '轮档未记录'}。这不是任务计划未运行。")
    elif run_class == "abnormal":
        status = basket_round.get("status") or "未见结束标记"
        content = (f"今日篮子轮次 {round_id} 未正常结束(状态={status})"
                   + (f"，{basket_round['reason']}" if basket_round.get("reason") else "")
                   + "；请查看当日轮档与 basket.log。")
    elif run_class == "unreadable":
        content = (f"今日没有篮子采集台账记录，且当日轮档无法读取"
                   f"({(round_evidence or {}).get('error') or '未知错误'})，无法判断篮子是否运行。")
    else:
        content = "今日固定篮子尚无采集台账记录，请检查 Windows 任务计划与 basket.log。"
    if expectation_reason == "settings_unavailable":
        content += "另：篮子配置不可读，今日是否应运行未能确认。"
    return title, content


def basket_run_expectation(settings: dict | None, state: dict | None) -> dict:
    """按配置判断今日是否应有篮子台账，口径与 basket_collect 的配置/运行态跳过条件一致。

    settings 或 state 不可读时传 None：此时无法证明篮子被有意停用，按应运行处理，哨兵照常检查。
    """
    if settings is None:
        return {"expected": True, "reason": "settings_unavailable"}
    if not bool(settings.get("research_basket_enabled")):
        return {"expected": False, "reason": "research_basket_disabled"}
    strategy = str(settings.get("research_basket_strategy") or "cohort_v2").strip().lower()
    if strategy == "cohort_v2" and state is not None and not research_runtime_enabled(state, True):
        return {"expected": False, "reason": "research_runtime_disabled"}
    return {"expected": True, "reason": ""}


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
    round_evidence: dict | None = None,
    expectation_reason: str = "",
) -> dict:
    current = now or datetime.now()
    result = evaluate_basket_sentinel(
        usage_payload,
        now=current,
        threshold=threshold,
        round_evidence=round_evidence,
    )
    result["notified"] = False
    if not result["due"]:
        return result
    path = Path(state_path)
    state = _load_state(path)
    if str(state.get("last_notified_day") or "") == result["date"]:
        result["status"] = "already_notified"
        return result
    title, content = _sentinel_message(result, round_evidence, expectation_reason)
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
