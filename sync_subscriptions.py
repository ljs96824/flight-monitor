"""Sync subscriptions from PythonAnywhere to the local data directory."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv


BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOCAL_SUBSCRIPTIONS = DATA_DIR / "subscriptions.json"
PYTHONANYWHERE_FILE_PATH = (
    "/home/{user}/flight-monitor/data/subscriptions.json"
)


load_dotenv(BASE_DIR / ".env", encoding="utf-8")


def _load_json_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def route_subscription_key(subscription: dict) -> str:
    """返回无身份字段时沿用的航线级订阅键。"""
    parts = [
        subscription.get("origin", ""),
        subscription.get("destination", ""),
        subscription.get("depart_date", ""),
        subscription.get("return_date", ""),
        str(subscription.get("round_trip", "")),
    ]
    return "route:" + "|".join(str(part).strip().upper() for part in parts)


def _subscription_key(subscription: dict) -> str:
    if subscription.get("id"):
        return f"id:{subscription['id']}"
    if subscription.get("created_at"):
        return f"created_at:{subscription['created_at']}"
    return route_subscription_key(subscription)


def merge_subscriptions(local_subscriptions: list[dict], remote_subscriptions: list[dict]) -> tuple[list[dict], int]:
    """按身份键同步远端订阅；同键更新，新键追加。"""
    merged = list(local_subscriptions)
    existing = {}
    for index, subscription in enumerate(merged):
        if not isinstance(subscription, dict):
            continue
        existing.setdefault(_subscription_key(subscription), index)
    added = 0
    for subscription in remote_subscriptions:
        if not isinstance(subscription, dict):
            continue
        key = _subscription_key(subscription)
        if key in existing:
            merged[existing[key]] = subscription
            continue
        merged.append(subscription)
        existing[key] = len(merged) - 1
        added += 1
    return merged, added


def download_remote_subscriptions() -> list[dict]:
    token = os.environ.get("PYTHONANYWHERE_TOKEN", "").strip()
    user = os.environ.get("PYTHONANYWHERE_USER", "").strip() or "ljs96824"
    if not token or "your" in token.lower() or "你的" in token or "token" == token.lower():
        print("[sync] 未配置 PYTHONANYWHERE_TOKEN，跳过远程订阅同步")
        return []

    remote_path = PYTHONANYWHERE_FILE_PATH.format(user=user)
    url = (
        f"https://www.pythonanywhere.com/api/v0/user/{user}"
        f"/files/path{remote_path}"
    )
    response = httpx.get(
        url,
        headers={"Authorization": f"Token {token}"},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        print("[sync] PythonAnywhere subscriptions.json 不是数组，已跳过")
        return []
    return data


def sync_subscriptions() -> dict:
    """Download remote subscriptions and merge them into local subscriptions.json."""
    remote_subscriptions = download_remote_subscriptions()
    if not remote_subscriptions:
        return {"synced": False, "added": 0, "total": len(_load_json_list(LOCAL_SUBSCRIPTIONS))}

    local_subscriptions = _load_json_list(LOCAL_SUBSCRIPTIONS)
    merged, added = merge_subscriptions(local_subscriptions, remote_subscriptions)

    DATA_DIR.mkdir(exist_ok=True)
    LOCAL_SUBSCRIPTIONS.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[sync] 已同步 PythonAnywhere 订阅：新增 {added} 条，本地共 {len(merged)} 条")
    return {"synced": True, "added": added, "total": len(merged)}


def main() -> None:
    sync_subscriptions()
    from main import run

    run(sync_remote=False)


if __name__ == "__main__":
    main()
