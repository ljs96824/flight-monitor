"""Explicitly initialize an empty local subscriptions file without overwriting an existing one."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from atomic_json_store import write_text_atomic  # noqa: E402
from local_file_lock import file_lock  # noqa: E402


DEFAULT_SUBSCRIPTIONS_PATH = ROOT / "data" / "subscriptions.json"


class SubscriptionsFileAlreadyExists(RuntimeError):
    """订阅文件已存在(不论内容是否可读)：显式初始化拒绝覆盖。"""


def initialize_subscriptions_file(path: str | Path = DEFAULT_SUBSCRIPTIONS_PATH) -> Path:
    """在与其他写入方相同的文件锁内确认目标不存在，再原子写入空数组(R9-1)。"""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(target):
        if target.exists():
            raise SubscriptionsFileAlreadyExists(f"订阅文件已存在: {target}")
        write_text_atomic(target, json.dumps([], ensure_ascii=False, indent=2))
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=DEFAULT_SUBSCRIPTIONS_PATH)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        initialize_subscriptions_file(args.path)
    except SubscriptionsFileAlreadyExists as exc:
        print(f"[订阅初始化] 拒绝覆盖: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"[订阅初始化] 失败 error={type(exc).__name__}:{exc}", file=sys.stderr)
        return 1
    print(f"[订阅初始化] 完成 path={args.path} 内容=[]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
