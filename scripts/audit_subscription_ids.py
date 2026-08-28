"""只读审计 subscriptions.json 中持久 subscription_id 的完整性。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sys
from typing import Sequence
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from atomic_json_store import read_json


@dataclass(frozen=True)
class DuplicateIdGroup:
    masked_id: str
    indexes: tuple[int, ...]


@dataclass(frozen=True)
class SubscriptionIdAudit:
    total_records: int
    records_with_id: int
    records_missing_id: int
    duplicate_groups: tuple[DuplicateIdGroup, ...]


def _subscription_id(record) -> str:
    if not isinstance(record, dict):
        return ""
    return str(record.get("subscription_id") or "").strip()


def _masked_id(identity: str) -> str:
    try:
        UUID(identity)
    except (ValueError, AttributeError):
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
        return f"sha256:{digest}-****"
    return f"{identity[:8]}-****"


def _excluded_artifact(path: Path) -> bool:
    name = path.name.lower()
    return ".bak." in name or name.endswith(".lock")


def audit_subscription_ids(path: str | Path) -> SubscriptionIdAudit:
    target = Path(path)
    if _excluded_artifact(target):
        raise ValueError("备份与锁文件不在订阅身份审计范围")

    records = read_json(target)
    if not isinstance(records, list):
        raise ValueError("subscriptions.json 顶层必须是数组")

    positions_by_id: dict[str, list[int]] = {}
    records_with_id = 0
    for index, record in enumerate(records):
        identity = _subscription_id(record)
        if not identity:
            continue
        records_with_id += 1
        positions_by_id.setdefault(identity, []).append(index)

    duplicate_groups = tuple(
        DuplicateIdGroup(
            masked_id=_masked_id(identity),
            indexes=tuple(indexes),
        )
        for identity, indexes in positions_by_id.items()
        if len(indexes) > 1
    )
    return SubscriptionIdAudit(
        total_records=len(records),
        records_with_id=records_with_id,
        records_missing_id=len(records) - records_with_id,
        duplicate_groups=duplicate_groups,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        default=ROOT / "data" / "subscriptions.json",
        help="要审计的 subscriptions.json；不会扫描相邻备份或锁文件",
    )
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    report = audit_subscription_ids(args.path)
    print(f"记录总数: {report.total_records}")
    print(f"有ID记录数: {report.records_with_id}")
    print(f"缺失ID记录数: {report.records_missing_id}")
    print(f"重复ID组数: {len(report.duplicate_groups)}")
    for number, group in enumerate(report.duplicate_groups, start=1):
        print(
            f"重复组 {number}: 标识={group.masked_id} "
            f"位置={list(group.indexes)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
