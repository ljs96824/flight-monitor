"""显式持久化订阅默认字段迁移；默认仅预览。"""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from atomic_json_store import read_json, update_json
from web_form import migrate_subscription_defaults


DEFAULT_SUBSCRIPTIONS_PATH = ROOT / "data" / "subscriptions.json"


def _emit(message: str, stream) -> None:
    print(message, file=stream or sys.stdout)


def _subscription_list(payload) -> list[dict]:
    if not isinstance(payload, list):
        raise ValueError("subscriptions.json 格式错误，应为订阅数组")
    return payload


def _migrate(payload: list[dict]) -> tuple[list[dict], dict]:
    migrated, details = migrate_subscription_defaults(payload)
    changed_indices = sorted(
        {
            int(item["index"])
            for group in ("budget_scopes", "lcc_policies")
            for item in details[group]
        }
    )
    return migrated, {**details, "changed_indices": changed_indices}


def _write_backup(path: Path, backup_path: Path) -> None:
    original = path.read_bytes()
    with backup_path.open("wb") as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())


def run(
    path: str | Path = DEFAULT_SUBSCRIPTIONS_PATH,
    *,
    write: bool = False,
    now: datetime | None = None,
    stream=None,
) -> dict:
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"订阅文件不存在: {target}")

    if not write:
        subscriptions = _subscription_list(read_json(target))
        _migrated, details = _migrate(subscriptions)
        result = {
            "total": len(subscriptions),
            "changed": len(details["changed_indices"]),
            "written": False,
            "backup_path": None,
            "details": details,
        }
        _emit(
            f"[订阅默认迁移] mode=dry-run 总数={result['total']} "
            f"待迁移={result['changed']}",
            stream,
        )
        return result

    state: dict = {}

    def mutate(payload):
        subscriptions = _subscription_list(payload)
        migrated, details = _migrate(subscriptions)
        changed = len(details["changed_indices"])
        backup_path = None
        if changed:
            timestamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
            backup_path = target.with_name(f"{target.name}.backup-{timestamp}")
            _write_backup(target, backup_path)
        state.update(
            {
                "total": len(subscriptions),
                "changed": changed,
                "written": bool(changed),
                "backup_path": str(backup_path) if backup_path else None,
                "details": details,
            }
        )
        return migrated

    update_json(target, mutate)
    _emit(
        f"[订阅默认迁移] mode=write 总数={state['total']} "
        f"迁移={state['changed']} backup={state['backup_path'] or '无'}",
        stream,
    )
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_SUBSCRIPTIONS_PATH,
        help="subscriptions.json 路径",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="备份后持久化；缺省仅预览",
    )
    args = parser.parse_args()
    run(args.path, write=args.write)


if __name__ == "__main__":
    main()
