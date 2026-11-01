"""基于本地配额台账与当日轮档的篮子任务缺勤哨兵。

台账里没有当日篮子条目时，再读当日轮档里篮子轮次的开始/结束标记，区分：未运行、已运行(零请求)、
已运行但被阻断、未正常结束、运行证据不完整、运行证据不可读。只读，不向台账或轮档写任何内容。
结束标记之前须有同一 round_id 的开始标记，轮次编号里的日期须与当日轮档一致，否则该轮次不能证明当日运行情况(R14)。
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
_ROUND_DAY = re.compile(r"^basket_(\d{8})T")
_MARKER_BOUNDARY = re.compile(r"(?====== \[轮档(?:开始|结束)\] )")
_RAN_STATUSES = {"ok", "partial"}
_DUE_REASONS = {
    "none": "今日篮子未运行",
    "blocked": "今日篮子已运行但被阻断",
    "abnormal": "今日篮子未正常结束",
    "incomplete": "今日篮子运行证据不完整",
    "unreadable": "今日篮子运行情况无法确认",
}


def _new_round(round_id: str, day: date, *, start_seen: bool) -> dict:
    match = _ROUND_DAY.match(round_id)
    return {
        "round_id": round_id,
        "status": None,
        "reason": "",
        "start_seen": start_seen,
        "day_matches": bool(match) and match.group(1) == f"{day:%Y%m%d}",
    }


def _round_evidence_problem(item: dict) -> str:
    """轮次证据不完整的原因(R14)；空串表示完整。不含 start_seen/day_matches 字段的旧格式证据按完整处理。"""
    if item.get("start_seen") is False:
        return "缺少开始标记"
    if item.get("day_matches") is False:
        return "日期与当日轮档不符"
    return ""


def read_basket_round_evidence(archive_root: str | Path, day: date) -> dict:
    """只读解析当日轮档中的篮子轮次：结束状态(None=未见结束标记)与第一条跳过/冲突/中断原因行。

    每个轮次另记 start_seen(结束标记之前见过同一 round_id 的开始标记)与 day_matches(round_id 中的日期等于 day)，
    供分类判断证据是否完整(R14)；只记录，不补写轮档、不推断缺失的标记。
    标记按整段匹配：上一个写入者在行中途中断、下一轮的标记接在残行之后时，先在标记起点切开再匹配；
    原因行只归属到尚未见到自己结束标记的那个篮子轮次(按完整 round_id 判断)。
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
    for raw_line in text.splitlines():
        for line in _MARKER_BOUNDARY.split(raw_line):
            start = _ROUND_START.match(line)
            if start:
                round_id = start.group(1)
                current = round_id if round_id.startswith("basket_") else None
                if current and current not in rounds:
                    rounds[current] = _new_round(current, day, start_seen=True)
                continue
            end = _ROUND_END.match(line)
            if end:
                end_id = end.group(1)
                if end_id.startswith("basket_"):
                    item = rounds.setdefault(end_id, _new_round(end_id, day, start_seen=False))
                    item["status"] = end.group(2)
                if end_id == current:
                    current = None
                continue
            if current and not rounds[current]["reason"] and _ROUND_REASON.match(line):
                rounds[current]["reason"] = line[:160]
    return {"readable": True, "path": str(path), "rounds": list(rounds.values())}


def classify_basket_rounds(evidence: dict | None) -> tuple[str, dict | None]:
    """ran=当日有证据完整的篮子轮次以 ok/partial 结束；blocked/abnormal 取当日最后一个证据完整的篮子轮次；
    incomplete=当日只有缺开始标记或日期不符的篮子轮次；none=无轮次；unreadable=读不到。
    """
    if evidence is None:
        return "none", None
    if not evidence.get("readable", True):
        return "unreadable", None
    rounds = [item for item in (evidence.get("rounds") or []) if isinstance(item, dict)]
    if not rounds:
        return "none", None
    complete = [item for item in rounds if not _round_evidence_problem(item)]
    for item in complete:
        if item.get("status") in _RAN_STATUSES:
            return "ran", item
    if not complete:
        return "incomplete", rounds[-1]
    last = complete[-1]
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
    elif run_class == "incomplete":
        content = (f"今日没有篮子采集台账记录；当日轮档中的篮子轮次 {round_id} "
                   f"{_round_evidence_problem(basket_round) or '证据不完整'}"
                   f"(结束状态={basket_round.get('status') or '未见结束标记'})，不能作为今日运行证据；"
                   "请查看当日轮档与 basket.log。")
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
    """读取哨兵去重状态；缺失、读不到、不是合法 UTF-8 JSON 或不是对象时一律按今日未通知处理。

    非法 UTF-8 与坏 JSON 同口径(R13)：到点照常发一次，发送成功后整份重写；不做容错解码，
    不把部分可读的损坏内容当作可信的去重记录。合法 UTF-8 但 json 拒收的内容(整数位数超限、嵌套过深)同样处理(R13b)。
    """
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        # R13b：ValueError 涵盖坏 JSON(JSONDecodeError)、非法 UTF-8(UnicodeDecodeError)与整数位数超限；RecursionError 为嵌套过深。
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
        # R13b：清理临时文件失败只记一行，不掩盖写入阶段的原始错误。
        try:
            temp_path.unlink(missing_ok=True)
        except OSError as exc:
            safe_log(f"[篮子哨兵] 临时文件清理失败 path={temp_path} 原因={type(exc).__name__}:{exc}")


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
    """到点检查今日篮子是否运行，需要时发一次提醒，发送成功后写去重状态。

    结果为 evaluate_basket_sentinel 的结论，另带 notified；到点时再带 status(already_notified / notified / notify_failed)。
    R13b：notified 为 True 时另带 state_saved——去重状态写入成功为 True，写入遇 OSError 为 False(记一行，不向外抛)。
    先发后记，所以状态写不进去期间，同日之后再到点且条件仍成立时会再提醒(至少一次)。
    """
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
        try:
            _write_state(
                path,
                {
                    "last_notified_day": result["date"],
                    "notified_at": current.isoformat(timespec="seconds"),
                },
            )
        except OSError as exc:
            # R13b：提醒已发出，只是去重记录没写进去——照实记一行，结论仍为 notified，不再让主入口记"检查失败"；
            # 先发后记的既有顺序不变，同日之后再到点且条件仍成立时会再提醒一次，直到状态能写入为止。
            result["state_saved"] = False
            safe_log(f"[篮子哨兵] 已通知，但去重状态写入失败 原因={type(exc).__name__}:{exc}")
        else:
            result["state_saved"] = True
    return result
