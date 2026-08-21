"""数据保留窗的只读报告与显式手工清理。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable


DEFAULT_RETENTION_DAYS = {
    "payloads": 90,
    "round_archives": 90,
    "backups": 180,
}


def load_retention_policy(config_path: str | Path | None = None) -> dict[str, int]:
    """从 config.yaml 读取保留天数；读取失败时使用保守默认值。"""
    payload = {}
    if config_path is not None:
        try:
            import yaml

            payload = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        except (ImportError, OSError, ValueError, TypeError):
            payload = {}
    raw = payload.get("retention_days") or {}
    return {
        key: max(0, int(raw.get(key, default)))
        for key, default in DEFAULT_RETENTION_DAYS.items()
    }


def _category_files(root: Path) -> dict[str, list[Path]]:
    data_dir = root / "data"
    payloads = sorted((data_dir / "payloads").glob("*.json"))
    rounds = sorted((data_dir / "logs" / "rounds").glob("*.log"))
    backups = []
    if data_dir.exists():
        for path in data_dir.rglob("*"):
            if not path.is_file():
                continue
            relative_parts = {part.lower() for part in path.relative_to(data_dir).parts}
            if ".bak" in path.name.lower() or any(
                "backup" in part for part in relative_parts
            ):
                backups.append(path)
    return {
        "payloads": payloads,
        "round_archives": rounds,
        "backups": sorted(set(backups)),
    }


def collect_retention_candidates(
    root: str | Path,
    policy: dict[str, int] | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    """只读收集超过保留窗的文件；0 天表示永久保留。"""
    root_path = Path(root).resolve()
    effective = {
        **DEFAULT_RETENTION_DAYS,
        **{key: max(0, int(value)) for key, value in (policy or {}).items()},
    }
    stamp = now or datetime.now()
    files = _category_files(root_path)
    items: dict[str, list[Path]] = {}
    for category, paths in files.items():
        days = effective[category]
        if days == 0:
            items[category] = []
            continue
        threshold = (stamp - timedelta(days=days)).timestamp()
        items[category] = [
            path for path in paths if path.stat().st_mtime < threshold
        ]
    counts = {category: len(paths) for category, paths in items.items()}
    return {
        "root": root_path,
        "policy": effective,
        "items": items,
        "counts": counts,
        "expired_total": sum(counts.values()),
    }


def format_retention_report(result: dict) -> str:
    counts = result.get("counts") or {}
    return (
        "[保留窗] dry-run "
        f"payloads到期={counts.get('payloads', 0)} "
        f"round_archives到期={counts.get('round_archives', 0)} "
        f"backups到期={counts.get('backups', 0)} "
        f"总计={result.get('expired_total', 0)}"
    )


def run_retention_cleanup(
    root: str | Path,
    policy: dict[str, int] | None = None,
    *,
    now: datetime | None = None,
    execute: bool = False,
) -> dict:
    """默认只报告；仅显式 execute 才删除已到期文件。"""
    result = collect_retention_candidates(root, policy, now=now)
    root_path = result["root"]
    deleted = 0
    if execute:
        for paths in result["items"].values():
            for path in paths:
                resolved = path.resolve()
                try:
                    resolved.relative_to(root_path)
                except ValueError as exc:
                    raise ValueError(f"拒绝删除工作区外文件:{resolved}") from exc
                resolved.unlink(missing_ok=True)
                deleted += 1
    return {**result, "execute": bool(execute), "deleted": deleted}


def log_retention_dry_run(
    root: str | Path,
    *,
    config_path: str | Path | None = None,
    logger: Callable[[str], object] | None = None,
) -> dict:
    """轮末只做 dry-run；失败时记录并返回空报告，不中断交付。"""
    if logger is None:
        from log_utils import safe_log

        logger = safe_log
    try:
        policy = load_retention_policy(config_path)
        result = run_retention_cleanup(root, policy, execute=False)
        logger(format_retention_report(result))
        return result
    except Exception as exc:
        logger(f"[保留窗] dry-run失败 原因={type(exc).__name__}:{exc}")
        return {
            "counts": {key: 0 for key in DEFAULT_RETENTION_DAYS},
            "expired_total": 0,
            "deleted": 0,
            "error": f"{type(exc).__name__}:{exc}",
        }
