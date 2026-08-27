"""Inspect and explicitly reconcile pending API usage evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api_usage import (  # noqa: E402
    DEFAULT_USAGE_PATH,
    UsageReconciliationError,
    list_reconciliation_evidence,
    reconcile_usage_evidence,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读列出或显式处理API配额pending证据。"
    )
    parser.add_argument("--usage-path", default=str(DEFAULT_USAGE_PATH))
    parser.add_argument("--conflict-log-path")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="只读列出全部pending证据")

    apply_parser = subparsers.add_parser("apply", help="按证据中的精确counts入账")
    apply_parser.add_argument("--evidence-id", required=True)
    apply_parser.add_argument("--confirm", required=True)

    dismiss_parser = subparsers.add_parser("dismiss", help="说明原因并关闭证据")
    dismiss_parser.add_argument("--evidence-id", required=True)
    dismiss_parser.add_argument("--reason", required=True)
    dismiss_parser.add_argument("--confirm", required=True)
    return parser


def _conflict_path(args) -> Path:
    if args.conflict_log_path:
        return Path(args.conflict_log_path)
    return Path(args.usage_path).parent / "api_usage_conflict.log"


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    conflict_path = _conflict_path(args)
    try:
        if args.command == "list":
            rows = list_reconciliation_evidence(conflict_path)
            print(f"pending_reconciliation={len(rows)}")
            for row in rows:
                print(
                    " ".join(
                        [
                            f"evidence_id={row.get('evidence_id', '')}",
                            f"round_id={row.get('round_id', 'unknown')}",
                            f"counts={json.dumps(row.get('counts') or {}, ensure_ascii=False, sort_keys=True)}",
                            f"workload_class={row.get('workload_class', 'unknown')}",
                            f"entrypoint={row.get('entrypoint', 'unknown')}",
                            f"recorded_at={row.get('recorded_at', 'unknown')}",
                        ]
                    )
                )
            return 0

        required_confirmation = "APPLY" if args.command == "apply" else "DISMISS"
        if args.confirm != required_confirmation:
            print(
                f"拒绝修改：请使用 --confirm {required_confirmation}",
                file=sys.stderr,
            )
            return 2
        result = reconcile_usage_evidence(
            args.evidence_id,
            action=args.command,
            reason=getattr(args, "reason", None),
            usage_path=args.usage_path,
            conflict_log_path=conflict_path,
        )
        backup = result.get("backup") or {}
        print(
            " ".join(
                [
                    f"status={result['status']}",
                    f"action={result['action']}",
                    f"evidence_id={result['evidence_id']}",
                    f"backup_sha256={backup.get('sha256', 'unchanged')}",
                ]
            )
        )
        return 0
    except UsageReconciliationError as exc:
        print(f"对账失败:{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
