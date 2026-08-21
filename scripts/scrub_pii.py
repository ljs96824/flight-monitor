"""历史日志邮箱脱敏工具；默认 dry-run，--execute 前先备份。"""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import shutil
import sys


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from log_utils import EMAIL_PATTERN


def default_scrub_paths(root: str | Path) -> list[Path]:
    root_path = Path(root).resolve()
    data_dir = root_path / "data"
    candidates = []
    candidates.extend((data_dir / "logs" / "rounds").glob("*.log"))
    candidates.extend(data_dir.glob("*.log"))
    candidates.extend(data_dir.glob("*.bak*"))
    return sorted({path.resolve() for path in candidates if path.is_file()})


def _atomic_write_text(path: Path, text: str) -> None:
    temp_path = path.with_name(f"{path.name}.scrub.tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as file_obj:
        file_obj.write(text)
        file_obj.flush()
        os.fsync(file_obj.fileno())
    os.replace(temp_path, path)


def scrub_files(
    paths,
    *,
    root: str | Path,
    execute: bool = False,
    now: datetime | None = None,
) -> dict:
    """扫描或脱敏指定文件；报告只含路径与计数，不复制 PII 原文。"""
    root_path = Path(root).resolve()
    matched = []
    matched_emails = 0
    for raw_path in paths:
        path = Path(raw_path).resolve()
        try:
            path.relative_to(root_path)
        except ValueError as exc:
            raise ValueError(f"拒绝处理工作区外文件:{path}") from exc
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="strict")
        count = len(EMAIL_PATTERN.findall(text))
        if count:
            matched.append((path, text, count))
            matched_emails += count

    backup_dir = None
    if execute and matched:
        stamp = now or datetime.now()
        backup_dir = (
            root_path
            / "data"
            / "pii_scrub_backups"
            / stamp.strftime("%Y%m%dT%H%M%S")
        )
        for path, _text, _count in matched:
            backup_path = backup_dir / path.relative_to(root_path)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup_path)
        for path, text, _count in matched:
            _atomic_write_text(path, EMAIL_PATTERN.sub("<EMAIL>", text))

    return {
        "execute": bool(execute),
        "matched_files": len(matched),
        "matched_emails": matched_emails,
        "backup_dir": str(backup_dir) if backup_dir else "",
        "paths": [str(path.relative_to(root_path)) for path, _text, _count in matched],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="扫描或脱敏历史日志中的邮箱")
    parser.add_argument("--root", default=str(BASE_DIR), help="项目根目录")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="显式执行：先备份，再把邮箱替换为<EMAIL>",
    )
    args = parser.parse_args(argv)
    paths = default_scrub_paths(args.root)
    result = scrub_files(paths, root=args.root, execute=args.execute)
    mode = "execute" if args.execute else "dry-run"
    print(
        f"[PII清理] mode={mode} 文件={result['matched_files']} "
        f"邮箱命中={result['matched_emails']}"
    )
    for path in result["paths"]:
        print(f"[PII清理] 文件={path}")
    if result["backup_dir"]:
        print(f"[PII清理] 备份={result['backup_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
