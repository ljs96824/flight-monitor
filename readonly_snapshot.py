"""为只读分析创建固定、可复现的本地输入快照。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "data"
DEFAULT_OUTPUT_ROOT = DEFAULT_SOURCE_DIR / "snapshots"
SNAPSHOT_FILENAMES = (
    "prices.db",
    "observations.sqlite3",
    "api_usage.json",
)
SQLITE_FILENAMES = ("prices.db", "observations.sqlite3")
_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SQLITE_BACKUP_LOCK_TIMEOUT_SECONDS = 3.0


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_observations_db(path: str | Path) -> Path:
    """让报告的 --db 同时接受观测库文件或完整快照目录。"""
    candidate = Path(path)
    return candidate / "observations.sqlite3" if candidate.is_dir() else candidate


def resolve_snapshot_member(path: str | Path, filename: str) -> Path | None:
    """若 path 指向快照目录或其观测库，返回同快照内指定成员。"""
    candidate = Path(path)
    if candidate.is_dir():
        member = candidate / filename
        return member if member.exists() else None
    if candidate.name == "observations.sqlite3":
        member = candidate.parent / filename
        return member if member.exists() else None
    return None


def _validate_label(label: str) -> str:
    value = str(label or "").strip()
    if not _LABEL_PATTERN.fullmatch(value):
        raise ValueError("快照label仅允许字母、数字、点、下划线和连字符")
    return value


def _backup_sqlite(source: Path, destination: Path) -> None:
    """用 SQLite 在线备份读取已提交事务，兼容 rollback journal 与 WAL。"""
    deadline = time.monotonic() + SQLITE_BACKUP_LOCK_TIMEOUT_SECONDS

    def progress(status, _remaining, _total):
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"SQLite快照等待写锁超时: {source.name}")

    source_connection = sqlite3.connect(
        f"{source.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=3,
    )
    destination_connection = None
    try:
        destination_connection = sqlite3.connect(destination)
        source_connection.execute("PRAGMA query_only=ON")
        source_connection.backup(
            destination_connection,
            pages=256,
            progress=progress,
            sleep=0.05,
        )
        integrity = destination_connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]
        destination_connection.commit()
        if str(integrity).lower() != "ok":
            raise RuntimeError(
                f"SQLite快照完整性检查失败: {source.name}={integrity}"
            )
    finally:
        if destination_connection is not None:
            destination_connection.close()
        source_connection.close()


def _copy_inputs(sources: dict[str, Path], staging: Path) -> None:
    for name, source in sources.items():
        destination = staging / name
        if name in SQLITE_FILENAMES:
            _backup_sqlite(source, destination)
        else:
            shutil.copyfile(source, destination)


def _open_sqlite_watchers(sources: dict[str, Path]) -> dict[str, sqlite3.Connection]:
    """在整组复制期间监视外部提交，覆盖 WAL 中主文件哈希不可见的变化。"""
    watchers = {}
    try:
        for name in SQLITE_FILENAMES:
            source = sources[name]
            connection = sqlite3.connect(
                f"{source.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=3,
            )
            watchers[name] = connection
            connection.execute("PRAGMA query_only=ON")
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        return watchers
    except Exception:
        for connection in watchers.values():
            connection.close()
        raise


def _sqlite_data_versions(
    watchers: dict[str, sqlite3.Connection],
) -> dict[str, int]:
    return {
        name: int(connection.execute("PRAGMA data_version").fetchone()[0])
        for name, connection in watchers.items()
    }


def create_readonly_snapshot(
    label: str,
    *,
    source_dir: str | Path = DEFAULT_SOURCE_DIR,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    generated_at: str | None = None,
    retries: int = 1,
    metadata_builder=None,
) -> dict:
    """复制同一稳定时段内的已提交输入，并原子发布自包含快照目录。"""
    safe_label = _validate_label(label)
    source_root = Path(source_dir)
    output = Path(output_root)
    sources = {name: source_root / name for name in SNAPSHOT_FILENAMES}
    missing = [str(path) for path in sources.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("缺少快照输入: " + ", ".join(missing))

    output.mkdir(parents=True, exist_ok=True)
    target = output / safe_label
    if target.exists():
        raise FileExistsError(f"快照目录已存在: {target}")
    staging = output / f".{safe_label}.partial-{uuid.uuid4().hex}"
    staging.mkdir()

    timestamp = generated_at or datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    try:
        source_hashes = None
        snapshot_hashes = None
        metadata = {}
        capture_attempts = 0
        stable_data_versions = None
        for _attempt in range(max(0, int(retries)) + 1):
            capture_attempts = _attempt + 1
            watchers = _open_sqlite_watchers(sources)
            try:
                versions_before = _sqlite_data_versions(watchers)
                before = {
                    name: sha256_file(path) for name, path in sources.items()
                }
                for child in staging.iterdir():
                    child.unlink()
                _copy_inputs(sources, staging)
                metadata = (
                    dict(metadata_builder(staging) or {})
                    if metadata_builder
                    else {}
                )
                versions_after = _sqlite_data_versions(watchers)
                after = {
                    name: sha256_file(path) for name, path in sources.items()
                }
                copied = {
                    name: sha256_file(staging / name)
                    for name in SNAPSHOT_FILENAMES
                }
            finally:
                for connection in watchers.values():
                    connection.close()
            if before == after and versions_before == versions_after:
                source_hashes = before
                snapshot_hashes = copied
                stable_data_versions = versions_before
                break
        if source_hashes is None or snapshot_hashes is None:
            raise RuntimeError("快照期间输入发生变化，请稍后重试")
        manifest = {
            "label": safe_label,
            "generated_at": timestamp,
            "source_sha256": source_hashes,
            "snapshot_sha256": snapshot_hashes,
            "capture": {
                "attempts": capture_attempts,
                "sqlite_data_versions": stable_data_versions,
                "consistency": "file_level_stable_inputs",
            },
            **metadata,
        }
        manifest_path = staging / "snapshot_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        manifest_hash = sha256_file(manifest_path)
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return {
        "label": safe_label,
        "generated_at": timestamp,
        "path": str(target.resolve()),
        "files": {
            name: {
                "path": str((target / name).resolve()),
                "sha256": snapshot_hashes[name],
                "source_sha256": source_hashes[name],
                "bytes": (target / name).stat().st_size,
            }
            for name in SNAPSHOT_FILENAMES
        },
        "manifest": {
            "path": str((target / "snapshot_manifest.json").resolve()),
            "sha256": manifest_hash,
            "bytes": (target / "snapshot_manifest.json").stat().st_size,
        },
    }
