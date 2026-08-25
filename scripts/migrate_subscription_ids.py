"""为订阅补发稳定 UUID；默认只读，--execute 才写入。"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from atomic_json_store import read_json, update_json
from log_utils import safe_log
from subscription_identity import ensure_subscription_id


DEFAULT_SUBSCRIPTIONS_PATH = ROOT / "data" / "subscriptions.json"


def _subscription_list(payload) -> list:
    if not isinstance(payload, list):
        raise ValueError("subscriptions.json 顶层必须是数组")
    return payload


def _load_subscriptions(path: Path) -> list:
    return _subscription_list(read_json(path))


def _emit(message: str, stream: TextIO | None) -> None:
    if stream is None:
        safe_log(message)
    else:
        print(message, file=stream)


def migrate_subscription_ids(subscriptions: list) -> list[dict]:
    migrated = []
    for index, subscription in enumerate(subscriptions):
        if not isinstance(subscription, dict):
            continue
        stable_id, changed = ensure_subscription_id(subscription)
        if changed:
            migrated.append({"index": index, "subscription_id": stable_id})
    return migrated


def run(
    path: str | Path = DEFAULT_SUBSCRIPTIONS_PATH,
    *,
    execute: bool = False,
    now: datetime | None = None,
    stream: TextIO | None = None,
) -> dict:
    subscriptions_path = Path(path)
    backup_path = None
    if execute:
        state: dict = {}

        def mutate(payload):
            subscriptions = _subscription_list(payload)
            migrated = migrate_subscription_ids(subscriptions)
            local_backup = None
            if migrated:
                timestamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S%f")
                local_backup = subscriptions_path.with_name(
                    f"{subscriptions_path.name}.bak.identity.{timestamp}"
                )
                shutil.copy2(subscriptions_path, local_backup)
            state.update(
                {
                    "subscriptions": subscriptions,
                    "migrated": migrated,
                    "backup_path": local_backup,
                }
            )
            return subscriptions

        update_json(subscriptions_path, mutate)
        subscriptions = state["subscriptions"]
        migrated = state["migrated"]
        backup_path = state["backup_path"]
    else:
        subscriptions = _load_subscriptions(subscriptions_path)
        migrated = migrate_subscription_ids(subscriptions)

    for item in migrated:
        _emit(
            f"[身份迁移] index={item['index']} "
            f"subscription_id={item['subscription_id']}",
            stream,
        )

    if execute and migrated:
        _emit(
            f"[身份迁移执行] 总数={len(subscriptions)} 补发={len(migrated)} "
            f"备份={backup_path}",
            stream,
        )
    elif execute:
        _emit(f"[身份迁移执行] 无需补发，总数={len(subscriptions)}", stream)
    else:
        _emit(
            f"[身份迁移预览] 总数={len(subscriptions)} 待补发={len(migrated)} "
            "未修改文件；确认后使用 --execute。",
            stream,
        )

    return {
        "before": len(subscriptions),
        "migrated": len(migrated),
        "backup_path": str(backup_path) if backup_path is not None else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=DEFAULT_SUBSCRIPTIONS_PATH)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="显式执行：先备份，再为缺失身份的订阅补 UUID",
    )
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    run(args.path, execute=args.execute)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
