"""Persist and evaluate privacy-safe runtime-backup readiness evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from atomic_json_store import JsonStoreReadError, read_json, update_json
from log_utils import safe_log
from readonly_snapshot import sha256_file


BACKUP_STATUS_VERSION = "backup_status_v1"
DEFAULT_MAX_BACKUP_EVIDENCE_AGE_DAYS = 30


class BackupEvidenceError(RuntimeError):
    """Base error for backup-readiness evidence operations."""


class OffDiskCopyMissing(BackupEvidenceError):
    """The local archive or requested off-disk copy is absent."""


class OffDiskCopyMismatch(BackupEvidenceError):
    """The off-disk copy does not match the local archive."""


class BackupStatusMismatch(BackupEvidenceError):
    """The status file describes a different archive."""


def _utc_now(value: datetime | None = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return _utc_now(value).isoformat().replace("+00:00", "Z")


def _empty_copy() -> dict:
    return {
        "verified": False,
        "verified_at": None,
        "destination_kind": None,
        "copied_sha256": None,
    }


def _base_status(backup_id: str, archive_sha256: str) -> dict:
    return {
        "status_version": BACKUP_STATUS_VERSION,
        "backup_id": str(backup_id),
        "archive_sha256": str(archive_sha256),
        "verified_restore_at": None,
        "off_disk_copy": _empty_copy(),
    }


def _same_archive(payload, *, backup_id: str, archive_sha256: str) -> bool:
    return bool(
        isinstance(payload, dict)
        and payload.get("backup_id") == str(backup_id)
        and payload.get("archive_sha256") == str(archive_sha256)
    )


def record_backup_created(
    status_path: str | Path,
    *,
    backup_id: str,
    archive_sha256: str,
) -> dict:
    """Start evidence for a newly created archive and invalidate older proof."""

    return update_json(
        status_path,
        lambda _current: _base_status(backup_id, archive_sha256),
    )


def record_restore_verified(
    status_path: str | Path,
    *,
    backup_id: str,
    archive_sha256: str,
    verified_at: datetime | None = None,
) -> dict:
    """Record a completed isolated restore without storing private paths."""

    def mutate(current):
        payload = (
            dict(current)
            if _same_archive(
                current,
                backup_id=backup_id,
                archive_sha256=archive_sha256,
            )
            else _base_status(backup_id, archive_sha256)
        )
        payload["status_version"] = BACKUP_STATUS_VERSION
        payload["backup_id"] = str(backup_id)
        payload["archive_sha256"] = str(archive_sha256)
        payload["verified_restore_at"] = _timestamp(verified_at)
        payload.setdefault("off_disk_copy", _empty_copy())
        return payload

    return update_json(status_path, mutate)


def verify_off_disk_copy(
    local_archive: str | Path,
    copied_archive: str | Path,
    *,
    status_path: str | Path,
    backup_id: str,
    destination_kind: str,
    verified_at: datetime | None = None,
) -> dict:
    """Verify an external copy byte-for-byte, then atomically record evidence."""

    local = Path(local_archive)
    copied = Path(copied_archive)
    if not local.is_file():
        raise OffDiskCopyMissing(f"本地归档不存在: {local}")
    if not copied.is_file():
        raise OffDiskCopyMissing(f"异盘副本不存在: {copied}")
    if local.resolve() == copied.resolve():
        raise OffDiskCopyMismatch("异盘副本不能是同一文件")
    local_hash = sha256_file(local)
    copied_hash = sha256_file(copied)
    if local_hash != copied_hash:
        raise OffDiskCopyMismatch("异盘副本SHA256与本地归档不一致")

    def mutate(current):
        if not _same_archive(
            current,
            backup_id=backup_id,
            archive_sha256=local_hash,
        ):
            raise BackupStatusMismatch("backup_status与待核验本地归档不一致")
        payload = dict(current)
        payload["off_disk_copy"] = {
            "verified": True,
            "verified_at": _timestamp(verified_at),
            "destination_kind": str(destination_kind or "external_path"),
            "copied_sha256": copied_hash,
        }
        return payload

    return update_json(status_path, mutate)


def load_backup_status(status_path: str | Path) -> dict:
    path = Path(status_path)
    if not path.is_file():
        return {}
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise ValueError("backup_status.json必须是JSON对象")
    return payload


def _parse_timestamp(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def evaluate_backup_evidence(
    status: dict | None,
    *,
    now: datetime | None = None,
    max_age_days: int = DEFAULT_MAX_BACKUP_EVIDENCE_AGE_DAYS,
) -> dict:
    payload = status if isinstance(status, dict) else {}
    current_time = _utc_now(now)
    max_age = max(0, int(max_age_days))
    restored_at = _parse_timestamp(payload.get("verified_restore_at"))
    copied = payload.get("off_disk_copy")
    copied = copied if isinstance(copied, dict) else {}
    archive_hash = str(payload.get("archive_sha256") or "")
    copied_hash = str(copied.get("copied_sha256") or "")
    copy_verified = bool(
        copied.get("verified")
        and archive_hash
        and copied_hash
        and archive_hash == copied_hash
    )
    copied_at = _parse_timestamp(copied.get("verified_at"))
    age_days = None
    fresh = False
    if copy_verified and copied_at is not None:
        age_days = (current_time - copied_at).total_seconds() / 86400
        fresh = 0 <= age_days <= max_age

    checks = {
        "backup_restore_verified": restored_at is not None,
        "off_disk_copy_verified": copy_verified,
        "off_disk_copy_fresh": fresh,
    }
    reasons = {}
    if not checks["backup_restore_verified"]:
        reasons["backup_restore_verified"] = "尚无成功隔离恢复证据"
    if not checks["off_disk_copy_verified"]:
        reasons["off_disk_copy_verified"] = "异盘副本未核验或SHA256不一致"
    if not checks["off_disk_copy_fresh"]:
        if copied_at is None:
            reasons["off_disk_copy_fresh"] = "异盘副本核验时刻缺失或无效"
        elif age_days is not None and age_days < 0:
            reasons["off_disk_copy_fresh"] = "异盘副本核验时刻位于未来"
        else:
            reasons["off_disk_copy_fresh"] = (
                f"异盘副本证据已过期,请在{max_age}天内重新核验"
            )
    return {
        "checks": checks,
        "reasons": reasons,
        "current": {
            "backup_id": payload.get("backup_id"),
            "verified_restore_at": payload.get("verified_restore_at"),
            "off_disk_copy_verified": bool(copied.get("verified")),
            "off_disk_copy_destination_kind": copied.get("destination_kind"),
            "off_disk_copy_verified_at": copied.get("verified_at"),
            "off_disk_copy_age_days": age_days,
        },
        "requirements": {"max_backup_age_days": max_age},
    }


def load_backup_evidence(
    status_path: str | Path,
    *,
    now: datetime | None = None,
    max_age_days: int = DEFAULT_MAX_BACKUP_EVIDENCE_AGE_DAYS,
) -> dict:
    try:
        status = load_backup_status(status_path)
    except (JsonStoreReadError, ValueError) as exc:
        safe_log(f"[研究采样门] backup_status读取失败 原因={type(exc).__name__}:{exc}")
        status = {}
    return evaluate_backup_evidence(status, now=now, max_age_days=max_age_days)
