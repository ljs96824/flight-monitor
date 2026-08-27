"""Verify runtime backups and maintain research-readiness evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from atomic_json_store import JsonStoreReadError  # noqa: E402
from backup_status import (  # noqa: E402
    BackupEvidenceError,
    load_backup_status,
    verify_off_disk_copy_from_status,
)
from runtime_restore import RuntimeRestoreError, restore_runtime_backup  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "隔离恢复并核验运行备份，或核验异盘副本并查看backup_status。"
            "所有操作均不调用航班API。"
        )
    )
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument(
        "--archive",
        metavar="ARCHIVE",
        help="恢复到临时目录，完成integrity/JSON/manifest逐文件SHA核验",
    )
    operation.add_argument(
        "--verify-off-disk",
        metavar="ARCHIVE",
        help="按backup_status中的归档SHA核验异盘副本",
    )
    operation.add_argument(
        "--status",
        action="store_true",
        help="只读打印backup_status.json全部字段",
    )
    parser.add_argument("--checksum", help="可选SHA256旁路文件，默认<archive>.sha256")
    parser.add_argument(
        "--backup-status",
        type=Path,
        default=ROOT / "data" / "backup_status.json",
        help="状态文件路径，默认data/backup_status.json",
    )
    parser.add_argument(
        "--off-disk-kind",
        default="external_path",
        help="异盘介质类别，不记录实际私有路径",
    )
    return parser


def _print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _restore_summary(result: dict) -> dict:
    return {
        "operation": "restore",
        "passed": result.get("status") == "verified",
        "archive_sha256": result.get("archive_sha256"),
        "file_count": result.get("file_count"),
        "total_bytes": result.get("total_bytes"),
        "sqlite_integrity": bool(result.get("sqlite_integrity")),
        "json_valid": bool(result.get("json_valid")),
        "status_fields_written": ["verified_restore_at"],
        "temporary_restore_cleaned": True,
        "real_api_calls": 0,
    }


def _off_disk_summary(status: dict) -> dict:
    copied = status.get("off_disk_copy") or {}
    return {
        "operation": "verify_off_disk",
        "passed": bool(copied.get("verified")),
        "archive_sha256": status.get("archive_sha256"),
        "destination_kind": copied.get("destination_kind"),
        "verified_at": copied.get("verified_at"),
        "status_fields_written": ["off_disk_copy"],
        "real_api_calls": 0,
    }


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.archive:
            with tempfile.TemporaryDirectory(
                prefix="flight-monitor-restore-cli-"
            ) as directory:
                result = restore_runtime_backup(
                    args.archive,
                    checksum_path=args.checksum,
                    destination=Path(directory) / "restored",
                    status_path=args.backup_status,
                )
                summary = _restore_summary(result)
            _print(summary)
            return 0

        if args.verify_off_disk:
            status = verify_off_disk_copy_from_status(
                args.verify_off_disk,
                status_path=args.backup_status,
                destination_kind=args.off_disk_kind,
            )
            _print(_off_disk_summary(status))
            return 0

        if not args.backup_status.is_file():
            raise BackupEvidenceError("backup_status.json不存在")
        status = load_backup_status(args.backup_status)
        if not status:
            raise BackupEvidenceError("backup_status.json为空或无有效证据")
        _print({"operation": "status", "passed": True, "backup_status": status})
        return 0
    except (
        RuntimeRestoreError,
        BackupEvidenceError,
        JsonStoreReadError,
        OSError,
        ValueError,
    ) as exc:
        print(f"[运行恢复失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
