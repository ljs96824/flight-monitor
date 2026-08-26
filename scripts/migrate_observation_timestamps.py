"""Audit or explicitly backfill canonical observation timestamps."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from observations_store import (  # noqa: E402
    DEFAULT_DB_PATH,
    audit_observation_timestamps,
    migrate_observation_timestamps,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="审计观测时间；默认只读，--write 才写入 canonical 字段。"
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--write", action="store_true")
    parser.add_argument(
        "--assume-naive-shanghai",
        action="store_true",
        help="仅在来源审计确认后，把 naive 历史时间按 Asia/Shanghai 解释。",
    )
    return parser


def run(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.write:
        result = migrate_observation_timestamps(
            args.db,
            assume_naive_shanghai=args.assume_naive_shanghai,
        )
        mode = "write"
    else:
        result = audit_observation_timestamps(
            args.db,
            assume_naive_shanghai=args.assume_naive_shanghai,
        )
        mode = "dry-run"
    print(f"[观测时间迁移] mode={mode}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
