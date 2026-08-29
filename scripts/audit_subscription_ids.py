"""只读审计实时 subscriptions.json 中稳定订阅 ID 的完整性与唯一性。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TextIO


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from subscription_identity import (
    mask_subscription_id as _masked_id,
    persisted_subscription_id as _persisted_subscription_id,
)


DEFAULT_SUBSCRIPTIONS_PATH = ROOT / "data" / "subscriptions.json"


def _resolve_live_subscriptions_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_dir():
        candidate = candidate / "subscriptions.json"

    if candidate.name != "subscriptions.json":
        raise ValueError(
            "仅允许审计实时 subscriptions.json；备份与锁文件不在扫描范围内"
        )
    return candidate


def _load_subscriptions(path: Path) -> list:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("subscriptions.json 顶层必须是数组")
    return payload


def audit_subscription_ids(subscriptions: list) -> dict:
    positions_by_id: dict[str, list[int]] = {}
    records_with_id = 0

    for index, record in enumerate(subscriptions):
        stable_id = _persisted_subscription_id(record)
        if not stable_id:
            continue
        records_with_id += 1
        positions_by_id.setdefault(stable_id, []).append(index)

    duplicate_groups = [
        {
            "masked_id": _masked_id(stable_id),
            "indexes": indexes,
        }
        for stable_id, indexes in positions_by_id.items()
        if len(indexes) > 1
    ]

    return {
        "total_records": len(subscriptions),
        "records_with_id": records_with_id,
        "missing_id_records": len(subscriptions) - records_with_id,
        "duplicate_group_count": len(duplicate_groups),
        "duplicate_groups": duplicate_groups,
    }


def _emit_report(report: dict, stream: TextIO) -> None:
    print(f"记录总数={report['total_records']}", file=stream)
    print(f"有ID记录数={report['records_with_id']}", file=stream)
    print(f"缺失ID记录数={report['missing_id_records']}", file=stream)
    print(f"重复ID组数={report['duplicate_group_count']}", file=stream)
    for group_number, group in enumerate(report["duplicate_groups"], start=1):
        print(
            f"重复组{group_number}: 标识={group['masked_id']} "
            f"索引={group['indexes']}",
            file=stream,
        )


def run(
    path: str | Path = DEFAULT_SUBSCRIPTIONS_PATH,
    *,
    stream: TextIO | None = None,
) -> dict:
    subscriptions_path = _resolve_live_subscriptions_path(path)
    report = audit_subscription_ids(_load_subscriptions(subscriptions_path))
    _emit_report(report, stream or sys.stdout)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=DEFAULT_SUBSCRIPTIONS_PATH)
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    run(args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
