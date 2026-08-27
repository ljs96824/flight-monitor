"""保留窗手工工具；默认 dry-run，只有 --execute 才删除。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from retention import (
    format_retention_report,
    load_retention_policy,
    run_retention_cleanup,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="报告或手工删除超过保留窗的文件")
    parser.add_argument("--root", default=str(BASE_DIR), help="项目根目录")
    parser.add_argument(
        "--config",
        default=str(BASE_DIR / "config.defaults.yaml"),
        help="保留窗配置文件",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="显式删除已到期文件；省略时只报告",
    )
    args = parser.parse_args(argv)
    policy = load_retention_policy(args.config)
    result = run_retention_cleanup(
        args.root,
        policy,
        execute=args.execute,
    )
    print(format_retention_report(result))
    if args.execute:
        print(f"[保留窗] execute 删除={result['deleted']}")
    else:
        print("[保留窗] 未删除任何文件；如确认清理请显式添加--execute")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
