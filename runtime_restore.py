"""Safely verify, restore, and replay runtime backup archives."""

from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import tarfile
import tempfile
from uuid import uuid4

from backup_status import record_restore_verified
from collection_singleflight import (
    acquire_collection_singleflight,
    resolve_collection_lock_path,
)
from local_file_lock import file_lock
from readonly_snapshot import sha256_file
from runtime_backup import (
    MANIFEST_VERSION,
    PROJECT_ROOT,
    RuntimeStateValidationError,
    _archive_path_for,
    _classify,
    _strict_json,
    _strict_jsonl,
    build_replay_reports,
    create_runtime_backup,
    inspect_sqlite,
)


DEFAULT_MAX_FILES = 100_000
DEFAULT_MAX_TOTAL_BYTES = 20 * 1024 * 1024 * 1024


class RuntimeRestoreError(RuntimeError):
    """Base error for runtime restore operations."""


class ArchiveChecksumMismatch(RuntimeRestoreError):
    """The archive differs from its out-of-band checksum."""


class UnsafeArchiveError(RuntimeRestoreError):
    """The tar archive contains unsafe paths, types, or resource usage."""


class ManifestVerificationError(RuntimeRestoreError):
    """Restored content does not satisfy its private manifest."""


class RestoreDestinationExists(RuntimeRestoreError):
    """Default restore never overwrites an existing path."""


class ProductionRestoreNotConfirmed(RuntimeRestoreError):
    """Production restore requires both the flag and exact confirmation word."""


def verify_archive_checksum(
    archive_path: str | Path,
    checksum_path: str | Path | None = None,
) -> str:
    archive = Path(archive_path)
    sidecar = Path(checksum_path or f"{archive}.sha256")
    try:
        expected = sidecar.read_text(encoding="ascii").strip().split()[0].lower()
    except (OSError, UnicodeError, IndexError) as exc:
        raise ArchiveChecksumMismatch("归档SHA256旁路文件缺失或无效") from exc
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise ArchiveChecksumMismatch("归档SHA256格式无效")
    try:
        actual = sha256_file(archive)
    except OSError as exc:
        raise ArchiveChecksumMismatch("归档文件不可读") from exc
    if not secrets.compare_digest(actual, expected):
        raise ArchiveChecksumMismatch("归档SHA256不匹配，拒绝解压")
    return actual


def _safe_member_name(name: str) -> str:
    value = str(name or "").replace("\\", "/")
    if not value or "\x00" in value:
        raise UnsafeArchiveError("归档包含空路径或NUL")
    while value.startswith("./"):
        value = value[2:]
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts:
        raise UnsafeArchiveError("归档包含绝对路径或父目录穿越")
    if pure.parts and ":" in pure.parts[0]:
        raise UnsafeArchiveError("归档包含盘符路径")
    normalized = pure.as_posix()
    if normalized in {"", "."}:
        raise UnsafeArchiveError("归档包含无效路径")
    return normalized


def _validated_members(
    bundle: tarfile.TarFile,
    *,
    max_files: int,
    max_total_bytes: int,
) -> list[tuple[tarfile.TarInfo, str]]:
    members = bundle.getmembers()
    if len(members) > int(max_files):
        raise UnsafeArchiveError("归档文件数超过上限")
    total = 0
    seen = set()
    validated = []
    for member in members:
        normalized = _safe_member_name(member.name)
        if normalized in seen:
            raise UnsafeArchiveError("归档包含重复成员路径")
        seen.add(normalized)
        if not (member.isfile() or member.isdir()):
            raise UnsafeArchiveError("归档包含链接、设备或其他非普通成员")
        if member.size < 0:
            raise UnsafeArchiveError("归档成员大小无效")
        total += int(member.size)
        if total > int(max_total_bytes):
            raise UnsafeArchiveError("归档展开总大小超过上限")
        validated.append((member, normalized))
    return validated


def _extract_validated(
    bundle: tarfile.TarFile,
    members: list[tuple[tarfile.TarInfo, str]],
    staging: Path,
) -> None:
    staging_resolved = staging.resolve()
    for member, normalized in members:
        target = (staging / PurePosixPath(normalized)).resolve()
        try:
            target.relative_to(staging_resolved)
        except ValueError as exc:
            raise UnsafeArchiveError("归档成员越过恢复目录") from exc
        bundle.extract(member, path=staging, filter="data")


def _load_private_manifest(root: Path) -> dict:
    path = root / "manifest.json"
    try:
        payload = _strict_json(path)
    except RuntimeStateValidationError as exc:
        raise ManifestVerificationError("JSON manifest解析失败") from exc
    if not isinstance(payload, dict) or payload.get("manifest_version") != MANIFEST_VERSION:
        raise ManifestVerificationError("manifest版本缺失或不兼容")
    if not isinstance(payload.get("files"), list):
        raise ManifestVerificationError("manifest files结构无效")
    return payload


def verify_restored_runtime(root: str | Path) -> dict:
    restored = Path(root)
    manifest = _load_private_manifest(restored)
    expected_files = {"manifest.json"}
    sqlite_ok = True
    json_ok = True
    verified_count = 0
    total_bytes = 0

    for item in manifest["files"]:
        archive_path = _safe_member_name(item.get("path"))
        target = restored / PurePosixPath(archive_path)
        if not item.get("present"):
            if target.exists():
                raise ManifestVerificationError(
                    f"manifest标记缺失但恢复目录存在: {archive_path}"
                )
            continue
        if item.get("entry_type") == "directory":
            if not target.is_dir():
                raise ManifestVerificationError(f"恢复目录缺失: {archive_path}")
            continue
        expected_files.add(archive_path)
        if not target.is_file():
            raise ManifestVerificationError(f"恢复文件缺失: {archive_path}")
        actual_bytes = target.stat().st_size
        if actual_bytes != int(item.get("bytes") or 0):
            raise ManifestVerificationError(f"文件字节数不匹配: {archive_path}")
        actual_hash = sha256_file(target)
        if actual_hash != item.get("sha256"):
            raise ManifestVerificationError(f"文件SHA256不匹配: {archive_path}")

        validation = str(item.get("validation") or "")
        if validation == "sqlite_integrity_ok":
            try:
                current = inspect_sqlite(target)
            except RuntimeStateValidationError as exc:
                raise ManifestVerificationError(f"SQLite验证失败: {archive_path}") from exc
            if current["integrity_check"].lower() != "ok":
                sqlite_ok = False
                raise ManifestVerificationError(f"SQLite integrity失败: {archive_path}")
            if current["user_version"] != item.get("user_version"):
                raise ManifestVerificationError(f"SQLite user_version不匹配: {archive_path}")
            if current["table_rows"] != item.get("table_rows"):
                raise ManifestVerificationError(f"SQLite table_rows不匹配: {archive_path}")
        elif validation == "json_parsed":
            try:
                _strict_json(target)
            except RuntimeStateValidationError as exc:
                json_ok = False
                raise ManifestVerificationError(f"JSON验证失败: {archive_path}") from exc
        elif validation.startswith("jsonl_parsed:"):
            try:
                _strict_jsonl(target)
            except RuntimeStateValidationError as exc:
                json_ok = False
                raise ManifestVerificationError(f"JSONL验证失败: {archive_path}") from exc
        verified_count += 1
        total_bytes += actual_bytes

    actual_files = {
        path.relative_to(restored).as_posix()
        for path in restored.rglob("*")
        if path.is_file()
    }
    extras = sorted(actual_files - expected_files)
    if extras:
        raise ManifestVerificationError("恢复目录包含manifest外文件: " + ", ".join(extras))
    return {
        "manifest": manifest,
        "sqlite_integrity": sqlite_ok,
        "json_valid": json_ok,
        "file_count": verified_count,
        "total_bytes": total_bytes,
    }


def restore_runtime_backup(
    archive_path: str | Path,
    *,
    checksum_path: str | Path | None = None,
    destination: str | Path | None = None,
    max_files: int = DEFAULT_MAX_FILES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    status_path: str | Path | None = None,
    verified_at=None,
) -> dict:
    archive = Path(archive_path)
    archive_hash = verify_archive_checksum(archive, checksum_path)
    managed_parent = None
    if destination is None:
        managed_parent = Path(tempfile.mkdtemp(prefix="flight-monitor-restore-root-"))
        target = managed_parent / "restored"
    else:
        target = Path(destination).expanduser().resolve()
    if target.exists():
        raise RestoreDestinationExists(f"恢复目标已存在: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.partial-{uuid4().hex}"
    staging.mkdir()
    try:
        try:
            with tarfile.open(archive, "r:gz", encoding="utf-8") as bundle:
                members = _validated_members(
                    bundle,
                    max_files=max_files,
                    max_total_bytes=max_total_bytes,
                )
                _extract_validated(bundle, members, staging)
        except (tarfile.TarError, OSError) as exc:
            raise UnsafeArchiveError("归档无法安全读取或解压") from exc
        verification = verify_restored_runtime(staging)
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        if managed_parent is not None and managed_parent.exists():
            try:
                managed_parent.rmdir()
            except OSError:
                pass
        raise
    if status_path is not None:
        record_restore_verified(
            status_path,
            backup_id=verification["manifest"].get("backup_id"),
            archive_sha256=archive_hash,
            verified_at=verified_at,
        )
    return {
        "status": "verified",
        "path": str(target),
        "archive_sha256": archive_hash,
        "sqlite_integrity": verification["sqlite_integrity"],
        "json_valid": verification["json_valid"],
        "file_count": verification["file_count"],
        "total_bytes": verification["total_bytes"],
        "manifest": verification["manifest"],
        "real_api_calls": 0,
    }


def rehearse_runtime_backup(
    archive_path: str | Path,
    *,
    checksum_path: str | Path | None = None,
    route: str,
    pair: str | None = None,
    restore_destination: str | Path | None = None,
    report_builder=None,
    status_path: str | Path | None = None,
    verified_at=None,
) -> dict:
    restored = restore_runtime_backup(
        archive_path,
        checksum_path=checksum_path,
        destination=restore_destination,
        status_path=status_path,
        verified_at=verified_at,
    )
    root = Path(restored["path"])
    source_dir = root / "replay"
    source_hashes = {}
    for name in ("tcurve_source.txt", "forecast_source.txt"):
        path = source_dir / name
        if not path.is_file():
            raise ManifestVerificationError(f"归档缺少复放源报告: {name}")
        source_hashes[name] = sha256_file(path)
    with tempfile.TemporaryDirectory(prefix="flight-monitor-replay-") as directory:
        builder = report_builder or build_replay_reports
        restored_hashes = builder(
            root / "core_snapshot",
            Path(directory),
            route,
            pair,
        )
    replay_match = source_hashes == restored_hashes
    if not replay_match:
        raise ManifestVerificationError("恢复后的报告复放SHA256不一致")
    return {
        **restored,
        "replay_match": True,
        "source_report_sha256": source_hashes,
        "restored_report_sha256": restored_hashes,
    }


def _top_source_root(source_rel: str) -> str | None:
    if source_rel.startswith("generated:"):
        return None
    try:
        normalized = _safe_member_name(source_rel)
    except UnsafeArchiveError as exc:
        raise ManifestVerificationError("manifest source_rel包含越界路径") from exc
    if _classify(normalized) not in {
        "required_core",
        "business_state",
        "evidence",
        "diagnostics",
    }:
        raise ManifestVerificationError("manifest source_rel不属于可恢复运行状态")
    for prefix in ("price_calendar", "pushed_plans", "payloads", "logs/rounds"):
        if normalized == prefix or normalized.startswith(f"{prefix}/"):
            return prefix
    return normalized.split("/", 1)[0]


def _candidate_path(restored: Path, source_root: str) -> Path:
    if source_root in {"prices.db", "observations.sqlite3", "api_usage.json"}:
        return restored / "core_snapshot" / source_root
    return restored / PurePosixPath(
        _archive_path_for(
            source_root,
            "business_state"
            if source_root
            in {
                "feedback.json",
                "price_calendar",
                "pushed_plans",
                "basket_state.json",
                "basket_sentinel.json",
                "signals_history.jsonl",
            }
            else "evidence"
            if source_root in {"payloads", "logs/rounds"}
            else "required_core"
            if source_root == "subscriptions.json"
            else "diagnostics",
        )
    )


def _production_mappings(restored: Path, manifest: dict, data_root: Path):
    roots = []
    for item in manifest.get("files") or []:
        if not item.get("present"):
            continue
        root = _top_source_root(str(item.get("source_rel") or ""))
        if root and root not in roots:
            roots.append(root)
    mappings = []
    for root in roots:
        candidate = _candidate_path(restored, root)
        if candidate.exists():
            mappings.append((candidate, data_root / PurePosixPath(root)))
    return mappings


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _copy_for_switch(source: Path, destination: Path) -> Path:
    temporary = destination.parent / f".{destination.name}.restore-{uuid4().hex}"
    if source.is_dir():
        shutil.copytree(source, temporary)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, temporary)
    return temporary


def restore_to_production(
    archive_path: str | Path,
    *,
    force_production: bool,
    confirmation: str,
    project_root: str | Path = PROJECT_ROOT,
    pre_restore_output_dir: str | Path,
    checksum_path: str | Path | None = None,
    lock_path: str | Path | None = None,
    permission_metadata_builder=None,
    switch_hook=None,
) -> dict:
    if not force_production or confirmation != "RESTORE":
        raise ProductionRestoreNotConfirmed(
            "生产恢复必须同时提供--force-production和--confirm-production-restore RESTORE"
        )
    project = Path(project_root).resolve()
    data_root = project / "data"
    effective_lock_path = lock_path or resolve_collection_lock_path(base_dir=project)
    restore_id = f"restore_{uuid4().hex[:12]}"
    gate = acquire_collection_singleflight(
        restore_id,
        lock_path=effective_lock_path,
        heartbeat_interval_seconds=0,
    )
    if not gate.acquired:
        return {"status": "busy", "exit_code": 2, "real_api_calls": 0}

    pre_output = Path(pre_restore_output_dir).expanduser().resolve()
    restored_root = pre_output / f"candidate-{uuid4().hex}"
    rollback_root = pre_output / f"rollback-{uuid4().hex}"
    moved = []
    installed = []
    try:
        pre_backup = create_runtime_backup(
            output_dir=pre_output,
            project_root=project,
            data_root=data_root,
            permission_metadata_builder=permission_metadata_builder,
            _existing_gate=gate,
        )
        candidate = restore_runtime_backup(
            archive_path,
            checksum_path=checksum_path,
            destination=restored_root,
        )
        restored = Path(candidate["path"])
        mappings = _production_mappings(restored, candidate["manifest"], data_root)
        rollback_root.mkdir(parents=True, exist_ok=False)
        os.chmod(rollback_root, 0o700)

        lock_targets = [data_root / "api_usage.json", data_root / "subscriptions.json"]
        if (data_root / "feedback.json").exists():
            lock_targets.append(data_root / "feedback.json")
        with ExitStack() as locks:
            for target in lock_targets:
                locks.enter_context(file_lock(target))
            try:
                for index, (source, destination) in enumerate(mappings):
                    relative = destination.relative_to(data_root)
                    rollback = rollback_root / relative
                    rollback.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists():
                        os.replace(destination, rollback)
                        moved.append((rollback, destination))
                    temporary = _copy_for_switch(source, destination)
                    os.replace(temporary, destination)
                    installed.append(destination)
                    if switch_hook is not None:
                        switch_hook(source, destination, index)
            except Exception:
                for path in reversed(installed):
                    _remove_path(path)
                for rollback, destination in reversed(moved):
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(rollback, destination)
                raise
    finally:
        gate.release()

    return {
        "status": "restored_to_production",
        "exit_code": 0,
        "pre_restore_backup_id": pre_backup["backup_id"],
        "rollback_path": str(rollback_root),
        "production_state_changed": True,
        "real_api_calls": 0,
    }
