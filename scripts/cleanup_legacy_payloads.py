"""PA 旧详情 payload 清理工具；默认 dry-run，执行前要求备份归档。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from detail_access import canonical_detail_uuid


def inspect_payloads(root: str | Path) -> dict:
    root_path = Path(root).resolve()
    payload_dir = root_path / "data" / "payloads"
    files = sorted(payload_dir.glob("*.json")) if payload_dir.is_dir() else []
    uuid_files = [
        path
        for path in files
        if canonical_detail_uuid(path.stem) == path.stem.lower()
    ]
    legacy_files = [path for path in files if path not in uuid_files]
    page_results = root_path / "data" / "page_results.json"
    return {
        "root": root_path,
        "payload_dir": payload_dir,
        "all_files": files,
        "uuid_files": uuid_files,
        "legacy_files": legacy_files,
        "page_results": page_results,
        "page_results_exists": page_results.is_file(),
    }


def cleanup_legacy_payloads(
    root: str | Path,
    *,
    execute: bool = False,
    backup_archive: str | Path | None = None,
) -> dict:
    """默认只读；execute 时删除非 UUID payload 和旧聚合索引。"""
    state = inspect_payloads(root)
    if execute:
        archive = Path(backup_archive).expanduser() if backup_archive else None
        if archive is None or not archive.is_file():
            raise ValueError("--execute 前必须提供已存在的 --backup-archive")
        for path in state["legacy_files"]:
            path.unlink()
        if state["page_results_exists"]:
            state["page_results"].unlink()
    return {
        **state,
        "execute": bool(execute),
        "deleted_payloads": len(state["legacy_files"]) if execute else 0,
        "deleted_page_results": int(execute and state["page_results_exists"]),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="列出或清理 PA 上非 UUID 详情 payload"
    )
    parser.add_argument("--root", default=str(BASE_DIR), help="项目根目录")
    parser.add_argument("--execute", action="store_true", help="显式执行删除")
    parser.add_argument(
        "--backup-archive",
        help="执行前已生成的 payloads 备份归档路径",
    )
    args = parser.parse_args(argv)
    result = cleanup_legacy_payloads(
        args.root,
        execute=args.execute,
        backup_archive=args.backup_archive,
    )
    mode = "execute" if args.execute else "dry-run"
    print(
        f"[PA详情清理] mode={mode} 总数={len(result['all_files'])} "
        f"保留UUID={len(result['uuid_files'])} "
        f"待清理非UUID={len(result['legacy_files'])} "
        f"page_results={int(result['page_results_exists'])}"
    )
    for path in result["legacy_files"]:
        print(f"[PA详情清理] 待清理={path.name}")
    if args.execute:
        print(
            f"[PA详情清理] 已删除payload={result['deleted_payloads']} "
            f"page_results={result['deleted_page_results']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
