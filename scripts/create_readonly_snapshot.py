"""创建供 forecast/tcurve 只读复放使用的固定输入快照。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from readonly_snapshot import (
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_SOURCE_DIR,
    create_readonly_snapshot,
)


def _permission_quality_metadata(snapshot_dir: Path) -> dict:
    """在发布前把依赖轮档的质量审计结果固化，报告复放不再读活日志。"""
    from scripts.audit_permission_pollution import (
        AFFECTED_ROUND_IDS,
        DEFAULT_LOGS_DIR,
        build_audit,
    )

    audit = build_audit(
        observations_db=snapshot_dir / "observations.sqlite3",
        prices_db=snapshot_dir / "prices.db",
        logs_dir=DEFAULT_LOGS_DIR,
        round_ids=AFFECTED_ROUND_IDS,
    )
    return {
        "permission_quality_round_ids": list(AFFECTED_ROUND_IDS),
        "permission_quality_cells": audit["affected_cells"],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="创建三文件只读验证快照")
    parser.add_argument("--label", required=True, help="快照标签，如 p7-gate-20260824")
    parser.add_argument("--source-dir", default=str(DEFAULT_SOURCE_DIR), help="源 data 目录")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="快照根目录")
    args = parser.parse_args(argv)

    result = create_readonly_snapshot(
        args.label,
        source_dir=args.source_dir,
        output_root=args.output_root,
        metadata_builder=_permission_quality_metadata,
    )
    print(
        f"[只读快照] label={result['label']} 生成时刻={result['generated_at']} "
        f"目录={result['path']}"
    )
    for name, item in result["files"].items():
        print(
            f"[只读快照] 文件={name} SHA256={item['sha256']} "
            f"源SHA256={item['source_sha256']} bytes={item['bytes']}"
        )
    manifest = result["manifest"]
    print(
        f"[只读快照] 文件=snapshot_manifest.json "
        f"SHA256={manifest['sha256']} bytes={manifest['bytes']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
