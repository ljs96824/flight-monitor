"""Explicitly initialize an empty API usage ledger without overwriting history."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api_usage import (  # noqa: E402
    DEFAULT_USAGE_PATH,
    UsageLedgerAlreadyExists,
    initialize_usage_ledger,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=DEFAULT_USAGE_PATH)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        initialize_usage_ledger(args.path)
    except UsageLedgerAlreadyExists as exc:
        print(f"[配额台账初始化] 拒绝覆盖: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(
            f"[配额台账初始化] 失败 error={type(exc).__name__}:{exc}",
            file=sys.stderr,
        )
        return 1
    print(f"[配额台账初始化] 完成 path={args.path} version=2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
