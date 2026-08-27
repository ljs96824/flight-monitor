"""Split legacy config.yaml into tracked policy defaults and local runtime facts."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import shutil
import sys
from uuid import uuid4

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import (  # noqa: E402
    RuntimeConfigError,
    deep_merge,
    load_merged_config,
    split_legacy_config,
)


def _yaml_bytes(payload: dict) -> bytes:
    return yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    try:
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _contains_runtime_facts(payload: dict) -> bool:
    juhe = ((payload.get("source_quota_budget") or {}).get("juhe") or {})
    reserve = juhe.get("reserve") or {}
    return any(
        (
            bool(juhe.get("packs")),
            bool(juhe.get("reconciliation")),
            bool(reserve.get("epoch_started_at")),
            bool(reserve.get("target_date")),
            bool(payload.get("paused_research_routes")),
            bool(payload.get("subscriptions")),
            bool(payload.get("RESEARCH_BASKET_ENABLED")),
        )
    )


def migrate_runtime_config(
    source_path: str | Path,
    *,
    defaults_path: str | Path,
    runtime_path: str | Path,
    write: bool = False,
    now: datetime | None = None,
) -> dict:
    source = Path(source_path)
    defaults_target = Path(defaults_path)
    runtime_target = Path(runtime_path)
    try:
        legacy = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeConfigError(f"旧配置不存在: {source}") from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RuntimeConfigError(
            f"旧配置读取失败: {source} ({type(exc).__name__})"
        ) from exc
    if not isinstance(legacy, dict):
        raise RuntimeConfigError("旧配置根节点必须是对象")

    if not _contains_runtime_facts(legacy):
        if not defaults_target.is_file() or not runtime_target.is_file():
            raise RuntimeConfigError(
                "旧配置已不含运行事实且本地runtime缺失；"
                "请从config.example.yaml初始化，不得以空占位覆盖"
            )
        load_merged_config(defaults_target, runtime_target)
        defaults_bytes = defaults_target.read_bytes()
        runtime_bytes = runtime_target.read_bytes()
        return {
            "status": "already-migrated",
            "source": str(source),
            "defaults_path": str(defaults_target),
            "runtime_path": str(runtime_target),
            "defaults_sha256": _sha256(defaults_bytes),
            "runtime_sha256": _sha256(runtime_bytes),
            "merged_equal": True,
            "backup_path": None,
        }

    defaults, runtime = split_legacy_config(legacy)
    defaults_bytes = _yaml_bytes(defaults)
    runtime_bytes = _yaml_bytes(runtime)
    result = {
        "status": "dry-run",
        "source": str(source),
        "defaults_path": str(defaults_target),
        "runtime_path": str(runtime_target),
        "defaults_sha256": _sha256(defaults_bytes),
        "runtime_sha256": _sha256(runtime_bytes),
        "merged_equal": deep_merge(defaults, runtime) == legacy,
        "backup_path": None,
    }
    if not write:
        return result

    current_defaults = (
        defaults_target.read_bytes() if defaults_target.is_file() else None
    )
    current_runtime = runtime_target.read_bytes() if runtime_target.is_file() else None
    if current_defaults == defaults_bytes and current_runtime == runtime_bytes:
        result["status"] = "unchanged"
        return result

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    backup = source.with_name(f"{source.name}.pre-runtime-split-{stamp}.bak")
    if backup.exists():
        backup = source.with_name(
            f"{source.name}.pre-runtime-split-{stamp}-{uuid4().hex[:8]}.bak"
        )
    shutil.copy2(source, backup)
    _atomic_write(defaults_target, defaults_bytes)
    _atomic_write(runtime_target, runtime_bytes)
    result.update(status="written", backup_path=str(backup))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "config.yaml")
    parser.add_argument(
        "--defaults-output", type=Path, default=ROOT / "config.defaults.yaml"
    )
    parser.add_argument(
        "--runtime-output", type=Path, default=ROOT / "data" / "runtime_config.yaml"
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="执行前备份旧配置，再原子写入两个新文件；默认只预览",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    result = migrate_runtime_config(
        args.source,
        defaults_path=args.defaults_output,
        runtime_path=args.runtime_output,
        write=args.write,
    )
    print(f"mode={'write' if args.write else 'dry-run'} status={result['status']}")
    print(f"merged_equal={str(result['merged_equal']).lower()}")
    print(f"defaults={result['defaults_path']} sha256={result['defaults_sha256']}")
    print(f"runtime={result['runtime_path']} sha256={result['runtime_sha256']}")
    if result.get("backup_path"):
        print(f"backup={result['backup_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
