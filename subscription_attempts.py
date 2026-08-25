"""订阅采集尝试状态的原子写入。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from atomic_json_store import update_json
from subscription_identity import subscription_id as stable_subscription_id


BASE_DIR = Path(__file__).parent
DEFAULT_SUBSCRIPTIONS_PATH = BASE_DIR / "data" / "subscriptions.json"
MAX_ATTEMPT_FUTURE_SKEW = timedelta(minutes=5)


def _parse_attempt_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def attempt_time(value: str | None = None) -> str:
    parsed = _parse_attempt_datetime(value)
    if parsed is not None:
        return parsed.isoformat(timespec="microseconds")
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def record_subscription_attempt(
    subscription: dict | str,
    *,
    status: str,
    holder_round_id=None,
    entrypoint: str,
    at: str | None = None,
    path: Path = DEFAULT_SUBSCRIPTIONS_PATH,
    logger: Callable[[str], None] = print,
) -> bool:
    """按稳定订阅身份原子覆盖 last_attempt。"""

    subscription_key = (
        stable_subscription_id(subscription)
        if isinstance(subscription, dict)
        else str(subscription or "").strip()
    )
    if not subscription_key:
        logger(f"[采集启动握手] 状态未落盘 status={status} 原因=无subscription_id")
        return False

    attempt = {
        "status": str(status),
        "at": attempt_time(at),
        "holder_round_id": holder_round_id,
        "entrypoint": str(entrypoint),
    }
    result = {
        "updated": False,
        "newer_at": None,
        "recovered_future_at": None,
    }

    def mutate(payload):
        if payload is None:
            subscriptions = []
        elif isinstance(payload, list):
            subscriptions = payload
        else:
            raise ValueError("subscriptions.json 格式错误，应为订阅数组")
        for item in subscriptions:
            if stable_subscription_id(item) != subscription_key:
                continue
            existing = item.get("last_attempt")
            existing_at = _parse_attempt_datetime(
                existing.get("at") if isinstance(existing, dict) else None
            )
            incoming_at = _parse_attempt_datetime(attempt["at"])
            now_utc = datetime.now(timezone.utc)
            current_clock_ceiling = now_utc + MAX_ATTEMPT_FUTURE_SKEW
            existing_is_anomalous_future = (
                existing_at is not None
                and incoming_at is not None
                and existing_at > current_clock_ceiling
                and incoming_at <= current_clock_ceiling
            )
            if (
                existing_at is not None
                and incoming_at is not None
                and existing_at > incoming_at
                and not existing_is_anomalous_future
            ):
                result["newer_at"] = existing_at.isoformat(
                    timespec="microseconds"
                )
                break
            if existing_is_anomalous_future:
                result["recovered_future_at"] = existing_at.isoformat(
                    timespec="microseconds"
                )
            item["last_attempt"] = dict(attempt)
            result["updated"] = True
            break
        return subscriptions

    update_json(path, mutate)
    if result["newer_at"]:
        logger(
            f"[采集启动握手] 忽略迟到状态 status={status} "
            f"at={attempt['at']} newer_at={result['newer_at']}"
        )
    if result["recovered_future_at"]:
        logger(
            "[采集启动握手] 修复异常未来状态 "
            f"existing_at={result['recovered_future_at']} "
            f"incoming_at={attempt['at']}"
        )
    return bool(result["updated"])
