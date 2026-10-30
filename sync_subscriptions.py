"""Sync subscriptions from PythonAnywhere to the local data directory."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

from atomic_json_store import read_json, update_json
from log_utils import safe_log
from ops_alert import alert_subscription_input_missing
from subscription_identity import ensure_subscription_id, subscription_id


BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOCAL_SUBSCRIPTIONS = DATA_DIR / "subscriptions.json"
PYTHONANYWHERE_FILE_PATH = (
    "/home/{user}/flight-monitor/data/subscriptions.json"
)


load_dotenv(BASE_DIR / ".env", encoding="utf-8")

# 订阅输入缺失时命令入口的退出码(R9-1)。
EXIT_SUBSCRIPTIONS_MISSING = 2


class SubscriptionInputMissing(RuntimeError):
    """本地订阅文件缺失：运行入口不得把它当作空列表，同步也不得据此重建(R9-1)。"""


class _LocalFileAbsent:
    """R21：交给 update_json 的缺文件标记。JSON 解析不会产出这个类型，同一把锁内据此区分"文件已消失"与"现有内容为 null"。"""


def require_subscriptions_file(path: Path) -> None:
    """只确认订阅文件存在且是文件；内容是否合法由读取方按既有规则显式报错。"""
    target = Path(path)
    if not target.is_file():
        raise SubscriptionInputMissing(f"订阅文件缺失: {target}")


def subscription_input_missing_line(exc: SubscriptionInputMissing) -> str:
    return (
        f"[订阅输入缺失] {exc}；本轮停止：不同步、不初始化、不采集。"
        "首次使用请先执行 scripts/initialize_subscriptions.py 显式建立空订阅文件"
    )


def _load_json_list(path: Path) -> list[dict]:
    if not path.exists():
        # R21：远端为空时只读本地文件；此时文件已消失同样按缺失处理，不当作空列表(R9-1)。
        raise SubscriptionInputMissing(f"订阅文件缺失: {path}")
    data = read_json(path)
    if not isinstance(data, list):
        raise ValueError("subscriptions.json 格式错误，应为订阅数组")
    return data


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
    stable_id = subscription_id(subscription)
    if stable_id:
        return f"id:{stable_id}"
    if subscription.get("created_at"):
        return f"created_at:{subscription['created_at']}"
    return route_subscription_key(subscription)


def plan_remote_ingest(
    local_subscriptions: list[dict],
    remote_subscriptions: list[dict],
) -> dict:
    """规划 PA 只新增摄入；任何本地命中都不覆盖。"""
    merged = list(local_subscriptions)
    identities = {}
    routes = {}
    for index, subscription in enumerate(merged):
        if not isinstance(subscription, dict):
            continue
        identities.setdefault(_subscription_key(subscription), index)
        routes.setdefault(route_subscription_key(subscription), index)

    decisions = []
    for remote_index, subscription in enumerate(remote_subscriptions):
        if not isinstance(subscription, dict):
            continue
        identity_key = _subscription_key(subscription)
        route_key = route_subscription_key(subscription)
        if identity_key in identities:
            decisions.append(
                {
                    "remote_index": remote_index,
                    "action": "skip_identity",
                    "identity_key": identity_key,
                    "route_key": route_key,
                    "local_index": identities[identity_key],
                }
            )
            continue
        if route_key in routes:
            decisions.append(
                {
                    "remote_index": remote_index,
                    "action": "skip_route",
                    "identity_key": identity_key,
                    "route_key": route_key,
                    "local_index": routes[route_key],
                }
            )
            continue

        incoming = dict(subscription)
        ensure_subscription_id(incoming)
        local_index = len(merged)
        merged.append(incoming)
        identities[_subscription_key(incoming)] = local_index
        routes[route_key] = local_index
        decisions.append(
            {
                "remote_index": remote_index,
                "action": "append",
                "identity_key": _subscription_key(incoming),
                "route_key": route_key,
                "local_index": local_index,
            }
        )

    return {
        "subscriptions": merged,
        "added": sum(item["action"] == "append" for item in decisions),
        "skipped_identity": sum(
            item["action"] == "skip_identity" for item in decisions
        ),
        "skipped_route": sum(item["action"] == "skip_route" for item in decisions),
        "decisions": decisions,
    }


def merge_subscriptions(
    local_subscriptions: list[dict],
    remote_subscriptions: list[dict],
) -> tuple[list[dict], int]:
    """兼容旧调用方，执行 PA 只新增摄入计划。"""
    plan = plan_remote_ingest(local_subscriptions, remote_subscriptions)
    return plan["subscriptions"], plan["added"]


def download_remote_subscriptions() -> list[dict]:
    if os.environ.get("NO_LIVE_API") == "1":
        safe_log("[sync] NO_LIVE_API=1，已阻止真实 PythonAnywhere 下载")
        return []
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
    """Download remote subscriptions and merge them into local subscriptions.json.

    本地文件缺失时在下载前抛出 SubscriptionInputMissing；写入阶段发现文件已消失同样抛出，不按空集合重建(R9-1)。
    R21：远端为空时的读取阶段发现文件已消失同样抛出；现有内容为 JSON null 或其他非数组时抛 ValueError(格式错误)，不写入。
    """
    require_subscriptions_file(LOCAL_SUBSCRIPTIONS)
    remote_subscriptions = download_remote_subscriptions()
    if not remote_subscriptions:
        return {"synced": False, "added": 0, "total": len(_load_json_list(LOCAL_SUBSCRIPTIONS))}

    state: dict = {}

    def mutate(payload):
        if isinstance(payload, _LocalFileAbsent):
            raise SubscriptionInputMissing(f"订阅文件缺失: {LOCAL_SUBSCRIPTIONS}")
        if isinstance(payload, list):
            local_subscriptions = payload
        else:
            # R21：现有内容为 JSON null 与其他非数组内容同属格式错误，不按缺失处理(与远端为空时的读取、主入口加载同一口径)。
            raise ValueError("subscriptions.json 格式错误，应为订阅数组")
        plan = plan_remote_ingest(local_subscriptions, remote_subscriptions)
        state.update(plan)
        return plan["subscriptions"]

    update_json(LOCAL_SUBSCRIPTIONS, mutate, missing=_LocalFileAbsent())
    plan = state
    merged = plan["subscriptions"]
    added = plan["added"]

    for decision in plan["decisions"]:
        safe_log(
            "[同步决策] "
            f"PA[{decision['remote_index']}] action={decision['action']} "
            f"identity={decision['identity_key']} route={decision['route_key']} "
            f"local_index={decision['local_index']}"
        )

    safe_log(
        f"[sync] PA仅新增摄入：新增={added} "
        f"身份命中跳过={plan['skipped_identity']} "
        f"航线命中跳过={plan['skipped_route']} 本地共={len(merged)}"
    )
    return {
        "synced": True,
        "added": added,
        "skipped_identity": plan["skipped_identity"],
        "skipped_route": plan["skipped_route"],
        "total": len(merged),
    }


def main() -> int:
    try:
        require_subscriptions_file(LOCAL_SUBSCRIPTIONS)
        # R21：同步途中文件消失与开头即缺失同样处理(记一行、尝试告警、退出码 2、不采集)，与 main.run_cli 一致；其他异常照旧向上抛。
        sync_subscriptions()
    except SubscriptionInputMissing as exc:
        safe_log(subscription_input_missing_line(exc))
        try:
            alert_subscription_input_missing(LOCAL_SUBSCRIPTIONS)
        except Exception as alert_exc:  # 告警失败不改变缺失结论(R9-2)
            safe_log(f"[运维告警] 调用失败 原因={type(alert_exc).__name__}")
        return EXIT_SUBSCRIPTIONS_MISSING
    from main import run_cli

    return run_cli(sync_remote=False)


if __name__ == "__main__":
    raise SystemExit(main())
