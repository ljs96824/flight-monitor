"""Create, verify, restore, or rehearse a private runtime backup."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_backup import (
    PROJECT_ROOT,
    RuntimeBackupError,
    create_runtime_backup,
    sanitized_backup_summary,
)
from runtime_restore import (
    RuntimeRestoreError,
    rehearse_runtime_backup,
    restore_runtime_backup,
    restore_to_production,
)


def _capture_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output-dir",
        required=True,
        help="绝对路径，且必须位于项目与data目录之外",
    )
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--data-root")
    parser.add_argument("--no-payloads", action="store_true")
    parser.add_argument("--round-log-days", type=int, default=7)
    parser.add_argument("--include-diagnostics", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="创建并验证 flight-monitor 私有运行数据备份"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="创建原子备份归档")
    _capture_arguments(create)
    create.add_argument("--route", help="可选：同时冻结该城市航线的两份分析报告")
    create.add_argument("--pair", help="可选机场对，如 PVG-KIX")

    verify = commands.add_parser("verify", help="解压到临时目录并完整核验后删除")
    verify.add_argument("--archive", required=True)
    verify.add_argument("--checksum")

    restore = commands.add_parser("restore", help="恢复到新目录，默认使用临时目录")
    restore.add_argument("--archive", required=True)
    restore.add_argument("--checksum")
    restore.add_argument("--destination")
    restore.add_argument("--force-production", action="store_true")
    restore.add_argument("--confirm-production-restore", default="")
    restore.add_argument("--project-root", default=str(PROJECT_ROOT))
    restore.add_argument("--pre-restore-output-dir")

    rehearse = commands.add_parser(
        "rehearse",
        help="创建、隔离恢复并比较T曲线/预测报告SHA",
    )
    _capture_arguments(rehearse)
    rehearse.add_argument("--route", required=True)
    rehearse.add_argument("--pair")
    rehearse.add_argument("--restore-destination")
    return parser


def _print_summary(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _create_kwargs(args) -> dict:
    return {
        "output_dir": args.output_dir,
        "project_root": args.project_root,
        "data_root": args.data_root,
        "include_payloads": not args.no_payloads,
        "round_log_days": args.round_log_days,
        "include_diagnostics": args.include_diagnostics,
    }


def _verified_summary(result: dict) -> dict:
    manifest = result.get("manifest") or {}
    return {
        "status": result.get("status"),
        "backup_id": manifest.get("backup_id"),
        "archive_sha256": result.get("archive_sha256"),
        "file_count": result.get("file_count"),
        "total_bytes": result.get("total_bytes"),
        "sqlite_integrity": result.get("sqlite_integrity"),
        "json_valid": result.get("json_valid"),
        "replay_sha256": {},
        "production_state_changed": False,
        "real_api_calls": 0,
    }


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            result = create_runtime_backup(
                **_create_kwargs(args),
                replay_route=args.route,
                replay_pair=args.pair,
            )
            _print_summary(sanitized_backup_summary(result))
            return int(result.get("exit_code", 0))

        if args.command == "verify":
            with tempfile.TemporaryDirectory(prefix="flight-monitor-verify-") as directory:
                result = restore_runtime_backup(
                    args.archive,
                    checksum_path=args.checksum,
                    destination=Path(directory) / "restored",
                )
                _print_summary(_verified_summary(result))
            return 0

        if args.command == "restore":
            if args.force_production:
                if not args.pre_restore_output_dir:
                    parser.error("--force-production需要--pre-restore-output-dir")
                result = restore_to_production(
                    args.archive,
                    checksum_path=args.checksum,
                    force_production=True,
                    confirmation=args.confirm_production_restore,
                    project_root=args.project_root,
                    pre_restore_output_dir=args.pre_restore_output_dir,
                )
                _print_summary(
                    {
                        "status": result.get("status"),
                        "backup_id": result.get("pre_restore_backup_id"),
                        "archive_sha256": None,
                        "file_count": None,
                        "total_bytes": None,
                        "sqlite_integrity": result.get("status") == "restored_to_production",
                        "json_valid": result.get("status") == "restored_to_production",
                        "replay_sha256": {},
                        "production_state_changed": result.get("status")
                        == "restored_to_production",
                        "real_api_calls": 0,
                    }
                )
                return int(result.get("exit_code", 0))
            result = restore_runtime_backup(
                args.archive,
                checksum_path=args.checksum,
                destination=args.destination,
            )
            _print_summary(_verified_summary(result))
            return 0

        if args.command == "rehearse":
            backup = create_runtime_backup(
                **_create_kwargs(args),
                replay_route=args.route,
                replay_pair=args.pair,
            )
            if backup.get("status") == "busy":
                _print_summary(sanitized_backup_summary(backup))
                return 2
            replay = rehearse_runtime_backup(
                backup["archive_path"],
                checksum_path=backup["checksum_path"],
                route=args.route,
                pair=args.pair,
                restore_destination=args.restore_destination,
            )
            _print_summary(
                sanitized_backup_summary(
                    backup,
                    restored=replay,
                    replay=replay,
                    production_state_changed=False,
                )
            )
            return 0
    except (RuntimeBackupError, RuntimeRestoreError, OSError, ValueError) as exc:
        print(f"[运行备份失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    raise AssertionError(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
