"""按订阅航线身份键列出或清理历史克隆；默认只读。"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from atomic_json_store import read_json, update_json

from sync_subscriptions import route_subscription_key


DEFAULT_SUBSCRIPTIONS_PATH = ROOT / "data" / "subscriptions.json"


def _subscription_list(payload) -> list:
    if not isinstance(payload, list):
        raise ValueError("subscriptions.json 顶层必须是数组")
    return payload


def _load_subscriptions(path: Path) -> list:
    return _subscription_list(read_json(path))


def _parse_timestamp(value) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _latest_score(subscription: dict, index: int) -> tuple[float, int]:
    for field in ("updated_at", "created_at"):
        timestamp = _parse_timestamp(subscription.get(field))
        if timestamp is not None:
            return timestamp, index
    return float("-inf"), index


def _latest_text(subscription: dict) -> str:
    return str(subscription.get("updated_at") or subscription.get("created_at") or "未知")


def find_duplicate_clusters(subscriptions: list) -> list[dict]:
    grouped: dict[str, list[int]] = defaultdict(list)
    for index, subscription in enumerate(subscriptions):
        if isinstance(subscription, dict):
            grouped[route_subscription_key(subscription)].append(index)

    clusters = []
    for key, indices in grouped.items():
        if len(indices) < 2:
            continue
        keep_index = max(
            indices,
            key=lambda index: _latest_score(subscriptions[index], index),
        )
        keep = subscriptions[keep_index]
        clusters.append(
            {
                "key": key,
                "indices": indices,
                "keep_index": keep_index,
                "keep_timestamp": _latest_text(keep),
                "origin": keep.get("origin") or "?",
                "destination": keep.get("destination") or "?",
                "depart_date": keep.get("depart_date") or "-",
                "return_date": keep.get("return_date") or "-",
            }
        )
    return sorted(clusters, key=lambda cluster: (-len(cluster["indices"]), cluster["key"]))


def deduplicate_subscriptions(subscriptions: list) -> tuple[list, list[dict]]:
    clusters = find_duplicate_clusters(subscriptions)
    removed = {
        index
        for cluster in clusters
        for index in cluster["indices"]
        if index != cluster["keep_index"]
    }
    return [item for index, item in enumerate(subscriptions) if index not in removed], clusters


def run(
    path: str | Path = DEFAULT_SUBSCRIPTIONS_PATH,
    *,
    execute: bool = False,
    now: datetime | None = None,
    stream: TextIO | None = None,
) -> dict:
    output = stream or sys.stdout
    subscriptions_path = Path(path)
    backup_path = None

    if execute:
        state: dict = {}

        def mutate(payload):
            subscriptions = _subscription_list(payload)
            cleaned, clusters = deduplicate_subscriptions(subscriptions)
            removed = len(subscriptions) - len(cleaned)
            local_backup = None
            if removed:
                effective_now = now or datetime.now()
                suffix = effective_now.strftime("%Y%m%dT%H%M%S%f")
                local_backup = subscriptions_path.with_name(
                    f"{subscriptions_path.name}.bak.{suffix}"
                )
                shutil.copy2(subscriptions_path, local_backup)
            state.update(
                {
                    "subscriptions": subscriptions,
                    "cleaned": cleaned,
                    "clusters": clusters,
                    "removed": removed,
                    "backup_path": local_backup,
                }
            )
            return cleaned

        update_json(subscriptions_path, mutate)
        subscriptions = state["subscriptions"]
        cleaned = state["cleaned"]
        clusters = state["clusters"]
        removed = state["removed"]
        backup_path = state["backup_path"]
    else:
        subscriptions = _load_subscriptions(subscriptions_path)
        cleaned, clusters = deduplicate_subscriptions(subscriptions)
        removed = len(subscriptions) - len(cleaned)

    for cluster in clusters:
        print(
            "[重复簇] "
            f"键={cluster['key']} 数量={len(cluster['indices'])} "
            f"索引={cluster['indices']} 保留建议={cluster['keep_index']} "
            f"最新时间={cluster['keep_timestamp']}",
            file=output,
        )

    if execute and removed:
        print(
            f"[去重执行] 前={len(subscriptions)} 后={len(cleaned)} "
            f"删除={removed} 备份={backup_path}",
            file=output,
        )
    elif execute:
        print(f"[去重执行] 无重复项，计数保持={len(subscriptions)}", file=output)
    else:
        print(
            f"[只读预览] 前={len(subscriptions)} 去重后={len(cleaned)} "
            f"可删除={removed}；未修改文件。确认后使用 --execute。",
            file=output,
        )

    return {
        "before": len(subscriptions),
        "after": len(cleaned),
        "removed": removed,
        "clusters": clusters,
        "backup_path": str(backup_path) if backup_path is not None else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=DEFAULT_SUBSCRIPTIONS_PATH)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="显式执行：先备份原文件，再按每簇最新记录清理",
    )
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    run(args.path, execute=args.execute)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
