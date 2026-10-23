"""不依赖订阅列表的运维告警(R9-2)。

订阅文件缺失时，系统告警原本只能经订阅自带的通知渠道发出(main._notify_system_alert)，订阅为 0 就谁也通知不到。
这里改用全局 PushPlus 渠道(notifier.send，读取环境变量 PUSHPLUS_TOKEN，不需要收件人)尽力发出一次运维告警。

口径：
- effective NO_LIVE_API == "1" 或未配置 PUSHPLUS_TOKEN 时不尝试发送，分别记为 blocked_offline / not_configured，不写去重状态；
- 去重键为"原因 + 规范化路径"，在上海自然日内最多自动尝试一次；"检查并登记本次尝试"在去重状态文件的文件锁内完成；
- 去重状态不可读或无法登记时进入降级：本次调用最多尝试一次，记 dedup_unavailable，不修复、不覆盖损坏的状态文件；
  只有状态文件确实不存在时才初始化，现有内容为 JSON null 与其他非对象值一样按格式不对处理(R16)；
- 发送受理、发送失败、未配置分别记录；告警内容只含原因、路径与时间，不含 token、订阅内容或异常原文；
- 任何告警失败都不改变调用方原有的失败结论。
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path

from atomic_json_store import update_json
from log_utils import safe_log
from project_time import SHANGHAI_TZ


OPS_ALERT_STATE_PATH = Path(__file__).parent / "data" / "ops_alert_state.json"
SUBSCRIPTIONS_MISSING = "subscriptions_missing"
SUBSCRIPTIONS_MISSING_TITLE = "[运维告警] 订阅文件缺失"


def _default_sender(content: str, title: str) -> bool:
    from notifier import send

    return bool(send(content, title=title))


def _claim_today(state_path: Path, key: str, day: str, attempted_at: str) -> bool:
    """在文件锁内检查并登记今天的尝试；今天已尝试过返回 False。

    只有状态文件不存在时才以 {} 初始化(由 update_json 在同一把锁内判定)；状态文件不可读或格式不对
    (含现有内容为 JSON null)时抛错且不写入。
    """

    claimed = {"value": False}

    def mutate(payload):
        if not isinstance(payload, dict):
            raise ValueError("运维告警去重状态不是对象")
        entry = payload.get(key)
        if isinstance(entry, dict) and entry.get("date") == day:
            return payload
        claimed["value"] = True
        return {**payload, key: {"date": day, "status": "attempting", "attempted_at": attempted_at}}

    update_json(state_path, mutate, missing={})
    return claimed["value"]


def _record_result(state_path: Path, key: str, day: str, status: str) -> None:
    def mutate(payload):
        if not isinstance(payload, dict) or not isinstance(payload.get(key), dict):
            raise ValueError("运维告警去重状态缺少本次登记")
        if payload[key].get("date") != day:
            return payload
        return {**payload, key: {**payload[key], "status": status}}

    update_json(state_path, mutate)


def alert_subscription_input_missing(path, *, now=None, state_path=None, sender=None) -> dict:
    """订阅文件缺失的运维告警；返回 {"status", "dedup"}，从不抛出。"""

    target = str(Path(path).resolve())
    current = (now or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
    day = current.date().isoformat()
    status, dedup = "not_attempted", "not_used"
    try:
        if os.environ.get("NO_LIVE_API") == "1":
            status = "blocked_offline"
        elif not os.environ.get("PUSHPLUS_TOKEN", "").strip():
            status = "not_configured"
        else:
            store = Path(state_path) if state_path is not None else OPS_ALERT_STATE_PATH
            key = f"{SUBSCRIPTIONS_MISSING}|{target}"
            try:
                proceed = _claim_today(store, key, day, current.isoformat(timespec="seconds"))
                dedup = "normal" if proceed else "already_attempted_today"
            except Exception as exc:
                proceed, dedup = True, "dedup_unavailable"
                safe_log(f"[运维告警] 去重状态不可用，本次调用最多尝试一次 原因={type(exc).__name__}")
            if not proceed:
                status = "skipped"
            else:
                content = (
                    "本机监控入口发现订阅文件缺失，本轮已停止：不同步、不初始化、不采集。\n"
                    f"路径：{target}\n"
                    f"时间：{current.isoformat(timespec='seconds')}\n"
                    "处理：确认文件是否被误删；首次使用执行 scripts/initialize_subscriptions.py；"
                    "需要恢复时按备份恢复规程操作。"
                )
                try:
                    status = "sent" if (sender or _default_sender)(content, SUBSCRIPTIONS_MISSING_TITLE) else "send_failed"
                except Exception as exc:
                    status = "send_failed"
                    safe_log(f"[运维告警] 发送异常 原因={type(exc).__name__}")
                if dedup == "normal":
                    try:
                        _record_result(store, key, day, status)
                    except Exception as exc:
                        safe_log(f"[运维告警] 去重结果登记失败 原因={type(exc).__name__}")
    except Exception as exc:
        safe_log(f"[运维告警] 告警流程异常 原因={type(exc).__name__}")
    safe_log(f"[运维告警] 订阅文件缺失 结果={status} 去重={dedup}")
    return {"status": status, "dedup": dedup}
