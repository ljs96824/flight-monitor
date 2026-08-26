"""Create a private, verifiable backup of flight-monitor runtime state."""

from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from typing import Callable
from uuid import uuid4

from collection_singleflight import (
    acquire_collection_singleflight,
    resolve_collection_lock_path,
)
from local_file_lock import file_lock
from readonly_snapshot import create_readonly_snapshot, sha256_file


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data"
MANIFEST_VERSION = "runtime_backup_manifest_v1"

RUNTIME_BACKUP_SPEC = {
    "version": "runtime_backup_v1",
    "required_core": (
        "prices.db",
        "observations.sqlite3",
        "subscriptions.json",
        "api_usage.json",
    ),
    "business_state": (
        "feedback.json",
        "price_calendar",
        "pushed_plans",
        "basket_state.json",
        "basket_sentinel.json",
        "signals_history.jsonl",
    ),
    "evidence": (
        "payloads",
        "logs/rounds",
    ),
    "diagnostics": (
        "monitor.log",
        "analysis_log.jsonl",
        "notifications_log.txt",
    ),
    "excluded_exact": (
        "basket.log",
        "debug_response.json",
        "form_normalization_baseline_before_ux31.json",
        "last_signals.json",
        "live_run_console.txt",
        "p7_form_capture_candidate.json",
        "page_results.json",
        "raw_responses.jsonl",
        "run_latest.lo",
        "run_latest.log",
        "serpapi_audit_dry_run.json",
        "serpapi_capability_audit_20260814.json",
        "source_health.json",
        "two_phase_basket_final_20260722.log",
        "two_phase_live_20260722.log",
        "two_phase_live_final_20260722.log",
        "two_phase_live_final_after_fix_20260722.log",
        "two_phase_live_replay_20260722.log",
        "ui_smoke_latest.log",
        "ux31_preview_stderr.log",
        "ux31_preview_stdout.log",
        "ux3_preview_stderr.log",
        "ux3_preview_stdout.log",
    ),
    "excluded_prefixes": (
        "cache",
        "snapshots",
        "ux31_preview",
    ),
    "excluded_patterns": (
        "*.lock",
        "*.bak",
        "*.bak.*",
        "*.tmp",
        "*.partial-*",
        "snapshot*.json",
        "*_diff.json",
    ),
}

_ROUND_LOG_PATTERN = re.compile(r"^(\d{8})\.log$")


class RuntimeBackupError(RuntimeError):
    """Base error for runtime backup operations."""


class RequiredRuntimeStateMissing(RuntimeBackupError):
    """A required runtime artifact is absent."""


class UnknownRuntimePathsError(RuntimeBackupError):
    """Strict classification found runtime data outside the spec."""


class InvalidBackupOutput(RuntimeBackupError):
    """The output directory can recurse into the project or is ambiguous."""


class RuntimeStateValidationError(RuntimeBackupError):
    """A source artifact is unreadable, invalid, or unsafe to capture."""


@dataclass(frozen=True)
class CaptureItem:
    source: Path
    source_rel: str
    archive_path: str
    kind: str
    required: bool
    is_directory: bool = False


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_output_directory(
    output_dir: str | Path,
    *,
    project_root: str | Path = PROJECT_ROOT,
    data_root: str | Path = DEFAULT_DATA_ROOT,
) -> Path:
    supplied = Path(output_dir).expanduser()
    if not supplied.is_absolute():
        raise InvalidBackupOutput("--output-dir必须是绝对路径")
    output = supplied.resolve()
    project = Path(project_root).resolve()
    data = Path(data_root).resolve()
    if _is_within(output, project) or _is_within(output, data):
        raise InvalidBackupOutput("--output-dir不得位于project_root或data_root内")
    return output


def _matches_pattern(relative: str, pattern: str) -> bool:
    name = PurePosixPath(relative).name
    return PurePosixPath(name).match(pattern) or PurePosixPath(relative).match(pattern)


def _classify(relative: str) -> str | None:
    value = relative.strip("/")
    top = value.split("/", 1)[0]
    if value in RUNTIME_BACKUP_SPEC["required_core"]:
        return "required_core"
    for configured in RUNTIME_BACKUP_SPEC["business_state"]:
        if value == configured or value.startswith(f"{configured}/"):
            return "business_state"
    for configured in RUNTIME_BACKUP_SPEC["evidence"]:
        if value == configured or value.startswith(f"{configured}/"):
            return "evidence"
    if value == "logs":
        return "evidence_parent"
    if value in RUNTIME_BACKUP_SPEC["diagnostics"]:
        return "diagnostics"
    if value in RUNTIME_BACKUP_SPEC["excluded_exact"]:
        return "excluded"
    if top in RUNTIME_BACKUP_SPEC["excluded_prefixes"]:
        return "excluded"
    if any(
        _matches_pattern(value, pattern)
        for pattern in RUNTIME_BACKUP_SPEC["excluded_patterns"]
    ):
        return "excluded"
    return None


def _archive_path_for(relative: str, tier: str) -> str:
    if tier == "required_core":
        if relative == "subscriptions.json":
            return "state/subscriptions.json"
        return f"core_snapshot/{relative}"
    if tier == "business_state":
        return f"state/{relative}"
    if relative == "payloads" or relative.startswith("payloads/"):
        suffix = relative.removeprefix("payloads").lstrip("/")
        return "delivery/payloads" + (f"/{suffix}" if suffix else "")
    if relative == "logs/rounds" or relative.startswith("logs/rounds/"):
        suffix = relative.removeprefix("logs/rounds").lstrip("/")
        return "diagnostics/round_logs" + (f"/{suffix}" if suffix else "")
    return f"diagnostics/{relative}"


def _relative_paths(data_root: Path) -> list[tuple[Path, str, bool]]:
    rows = []
    for current, directories, files in os.walk(data_root, followlinks=False):
        current_path = Path(current)
        for name in sorted(directories):
            path = current_path / name
            relative = path.relative_to(data_root).as_posix()
            rows.append((path, relative, True))
        for name in sorted(files):
            path = current_path / name
            relative = path.relative_to(data_root).as_posix()
            rows.append((path, relative, False))
    return rows


def _round_log_selected(relative: str, *, today, round_log_days: int) -> bool:
    name = PurePosixPath(relative).name
    match = _ROUND_LOG_PATTERN.fullmatch(name)
    if match is None or round_log_days <= 0:
        return False
    try:
        observed_day = datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return False
    cutoff = today - timedelta(days=max(0, int(round_log_days) - 1))
    return cutoff <= observed_day <= today


def scan_runtime_state(
    data_root: str | Path,
    *,
    include_payloads: bool = True,
    round_log_days: int = 7,
    include_diagnostics: bool = False,
    strict: bool = True,
    today=None,
) -> dict:
    root = Path(data_root)
    if not root.is_dir():
        raise RequiredRuntimeStateMissing(f"运行数据目录不存在: {root}")
    missing = [
        name
        for name in RUNTIME_BACKUP_SPEC["required_core"]
        if not (root / name).is_file()
    ]
    if missing:
        raise RequiredRuntimeStateMissing("缺少必需运行状态: " + ", ".join(missing))

    current_day = today or datetime.now().astimezone().date()
    selected: list[CaptureItem] = []
    unknown: list[str] = []
    classified: dict[str, str] = {}
    present_configured = set()

    for path, relative, is_directory in _relative_paths(root):
        tier = _classify(relative)
        if tier is None:
            unknown.append(relative)
            continue
        classified[relative] = tier
        if path.is_symlink():
            unknown.append(f"{relative} (symlink)")
            continue
        for configured in RUNTIME_BACKUP_SPEC["business_state"]:
            if relative == configured or relative.startswith(f"{configured}/"):
                present_configured.add(configured)

        include = False
        if tier in {"required_core", "business_state"}:
            include = True
        elif tier == "evidence":
            if relative == "payloads" or relative.startswith("payloads/"):
                include = include_payloads
            elif relative == "logs/rounds":
                include = round_log_days > 0
            elif relative.startswith("logs/rounds/"):
                include = _round_log_selected(
                    relative,
                    today=current_day,
                    round_log_days=round_log_days,
                )
        elif tier == "diagnostics":
            include = include_diagnostics
        if include:
            selected.append(
                CaptureItem(
                    source=path,
                    source_rel=relative,
                    archive_path=_archive_path_for(relative, tier),
                    kind=tier,
                    required=tier == "required_core",
                    is_directory=is_directory,
                )
            )

    if strict and unknown:
        raise UnknownRuntimePathsError(
            "data目录存在未分类路径: " + ", ".join(sorted(set(unknown)))
        )

    absent = []
    for relative in RUNTIME_BACKUP_SPEC["business_state"]:
        if relative not in present_configured and not (root / relative).exists():
            absent.append(
                {
                    "path": _archive_path_for(relative, "business_state"),
                    "source_rel": relative,
                    "kind": "business_state",
                    "required": False,
                    "present": False,
                    "status": "absent",
                    "entry_type": "directory" if "." not in Path(relative).name else "file",
                    "bytes": None,
                    "sha256": None,
                    "integrity_check": None,
                    "user_version": None,
                    "table_rows": None,
                }
            )
    for relative, included in (
        ("payloads", include_payloads),
        ("logs/rounds", round_log_days > 0),
    ):
        if not included or not (root / relative).exists():
            status = "absent" if included else "omitted_by_config"
            absent.append(
                {
                    "path": _archive_path_for(relative, "evidence"),
                    "source_rel": relative,
                    "kind": "evidence",
                    "required": False,
                    "present": False,
                    "status": status,
                    "entry_type": "directory",
                    "bytes": None,
                    "sha256": None,
                    "integrity_check": None,
                    "user_version": None,
                    "table_rows": None,
                }
            )
    for relative in RUNTIME_BACKUP_SPEC["diagnostics"]:
        if not include_diagnostics or not (root / relative).exists():
            status = "absent" if include_diagnostics else "omitted_by_config"
            absent.append(
                {
                    "path": _archive_path_for(relative, "diagnostics"),
                    "source_rel": relative,
                    "kind": "diagnostics",
                    "required": False,
                    "present": False,
                    "status": status,
                    "entry_type": "file",
                    "bytes": None,
                    "sha256": None,
                    "integrity_check": None,
                    "user_version": None,
                    "table_rows": None,
                }
            )
    return {
        "selected": selected,
        "absent": absent,
        "unknown": sorted(set(unknown)),
        "classified": classified,
    }


def _strict_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeStateValidationError(
            f"JSON解析失败: {path.name}: {type(exc).__name__}"
        ) from exc


def _strict_jsonl(path: Path) -> int:
    count = 0
    line_number = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                json.loads(line)
                count += 1
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeStateValidationError(
            f"JSONL解析失败: {path.name}:{line_number}: {type(exc).__name__}"
        ) from exc
    return count


def inspect_sqlite(path: str | Path) -> dict:
    source = Path(path)
    uri = f"{source.resolve().as_uri()}?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=3)) as connection:
            connection.execute("PRAGMA query_only=ON")
            integrity = str(
                connection.execute("PRAGMA integrity_check").fetchone()[0]
            )
            if integrity.lower() != "ok":
                raise RuntimeStateValidationError(
                    f"SQLite完整性检查失败: {source.name}={integrity}"
                )
            user_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            table_names = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            ]
            rows = {}
            for table_name in table_names:
                quoted = table_name.replace('"', '""')
                rows[table_name] = int(
                    connection.execute(
                        f'SELECT COUNT(*) FROM "{quoted}"'
                    ).fetchone()[0]
                )
    except sqlite3.Error as exc:
        raise RuntimeStateValidationError(
            f"SQLite验证失败: {source.name}: {type(exc).__name__}"
        ) from exc
    return {
        "integrity_check": integrity,
        "user_version": user_version,
        "table_rows": rows,
    }


def _entry_for_file(
    path: Path,
    *,
    archive_path: str,
    source_rel: str,
    kind: str,
    required: bool,
) -> dict:
    integrity = None
    user_version = None
    table_rows = None
    validation = None
    if path.name in {"prices.db", "observations.sqlite3"}:
        sqlite_info = inspect_sqlite(path)
        integrity = sqlite_info["integrity_check"]
        user_version = sqlite_info["user_version"]
        table_rows = sqlite_info["table_rows"]
        validation = "sqlite_integrity_ok"
    elif path.suffix.lower() == ".json":
        _strict_json(path)
        validation = "json_parsed"
    elif path.suffix.lower() == ".jsonl":
        line_count = _strict_jsonl(path)
        validation = f"jsonl_parsed:{line_count}"
    return {
        "path": archive_path,
        "source_rel": source_rel,
        "kind": kind,
        "required": bool(required),
        "present": True,
        "status": "captured",
        "entry_type": "file",
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "integrity_check": integrity,
        "user_version": user_version,
        "table_rows": table_rows,
        "validation": validation,
    }


def _entry_for_directory(item: CaptureItem) -> dict:
    return {
        "path": item.archive_path,
        "source_rel": item.source_rel,
        "kind": item.kind,
        "required": item.required,
        "present": True,
        "status": "captured",
        "entry_type": "directory",
        "bytes": 0,
        "sha256": None,
        "integrity_check": None,
        "user_version": None,
        "table_rows": None,
        "validation": None,
    }


def _copy_item(item: CaptureItem, staging: Path) -> None:
    destination = staging / PurePosixPath(item.archive_path)
    if item.is_directory:
        destination.mkdir(parents=True, exist_ok=True)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(item.source, destination, follow_symlinks=False)


def _git_commit(project_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return completed.stdout.strip() or "unknown"


def build_permission_quality_metadata(
    snapshot_dir: Path,
    *,
    data_root: str | Path = DEFAULT_DATA_ROOT,
) -> dict:
    from scripts.audit_permission_pollution import AFFECTED_ROUND_IDS, build_audit

    logs_dir = Path(data_root) / "logs" / "rounds"
    if not logs_dir.is_dir():
        return {
            "permission_quality_round_ids": list(AFFECTED_ROUND_IDS),
            "permission_quality_cells": [],
        }
    audit = build_audit(
        observations_db=snapshot_dir / "observations.sqlite3",
        prices_db=snapshot_dir / "prices.db",
        logs_dir=logs_dir,
        round_ids=AFFECTED_ROUND_IDS,
    )
    return {
        "permission_quality_round_ids": list(AFFECTED_ROUND_IDS),
        "permission_quality_cells": list(audit.get("affected_cells") or []),
    }


def build_replay_reports(
    snapshot_dir: Path,
    output_dir: Path,
    route: str,
    pair=None,
) -> dict[str, str]:
    from scripts.forecast_report import generate_report as generate_forecast
    from scripts.tcurve_report import generate_report as generate_tcurve

    output_dir.mkdir(parents=True, exist_ok=True)
    tcurve_text = generate_tcurve(
        db_path=snapshot_dir,
        route=route,
        airport_pair=pair,
    )
    forecast_text, _ = generate_forecast(
        db_path=snapshot_dir,
        route=route,
        airport_pair=pair,
    )
    payloads = {
        "tcurve_source.txt": tcurve_text.encode("utf-8"),
        "forecast_source.txt": forecast_text.encode("utf-8"),
    }
    hashes = {}
    for name, payload in payloads.items():
        path = output_dir / name
        path.write_bytes(payload)
        hashes[name] = sha256_file(path)
    return hashes


def _manifest(
    *,
    backup_id: str,
    created_at_utc: str,
    project_root: Path,
    files: list[dict],
    replay: dict | None = None,
) -> dict:
    return {
        "manifest_version": MANIFEST_VERSION,
        "runtime_backup_spec_version": RUNTIME_BACKUP_SPEC["version"],
        "backup_id": backup_id,
        "created_at_utc": created_at_utc,
        "git_commit": _git_commit(project_root),
        "python_version": platform.python_version(),
        "capture_consistency": {
            "collection_singleflight": True,
            "sqlite_online_backup": True,
            "json_locked_reads": True,
        },
        "files": files,
        "replay": replay or {"enabled": False},
    }


def _write_manifest(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _add_core_entries(staging: Path, entries: list[dict]) -> None:
    for name in ("prices.db", "observations.sqlite3", "api_usage.json"):
        entries.append(
            _entry_for_file(
                staging / "core_snapshot" / name,
                archive_path=f"core_snapshot/{name}",
                source_rel=name,
                kind="required_core",
                required=True,
            )
        )
    entries.append(
        _entry_for_file(
            staging / "core_snapshot" / "snapshot_manifest.json",
            archive_path="core_snapshot/snapshot_manifest.json",
            source_rel="generated:snapshot_manifest.json",
            kind="core_metadata",
            required=True,
        )
    )


def _refresh_copied_entries(
    staging: Path,
    selected: list[CaptureItem],
) -> list[dict]:
    entries = []
    core_names = {"prices.db", "observations.sqlite3", "api_usage.json"}
    for item in selected:
        if item.source_rel in core_names:
            continue
        target = staging / PurePosixPath(item.archive_path)
        if item.is_directory:
            if not any(
                other.source_rel.startswith(f"{item.source_rel}/")
                for other in selected
            ):
                entries.append(_entry_for_directory(item))
            continue
        entries.append(
            _entry_for_file(
                target,
                archive_path=item.archive_path,
                source_rel=item.source_rel,
                kind=item.kind,
                required=item.required,
            )
        )
    return entries


def _atomic_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}.partial-{uuid4().hex}")
    try:
        with temporary.open("w", encoding="ascii", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _publish_archive(staging: Path, output: Path, backup_id: str) -> tuple[Path, Path, str]:
    final_archive = output / f"flight-monitor-{backup_id}.tar.gz"
    temporary = output / f".{final_archive.name}.partial-{uuid4().hex}"
    try:
        with tarfile.open(temporary, "w:gz", format=tarfile.PAX_FORMAT, encoding="utf-8") as bundle:
            for path in sorted(staging.rglob("*"), key=lambda item: item.as_posix()):
                bundle.add(
                    path,
                    arcname=path.relative_to(staging).as_posix(),
                    recursive=False,
                )
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, final_archive)
    finally:
        if temporary.exists():
            temporary.unlink()
    archive_hash = sha256_file(final_archive)
    checksum_path = Path(str(final_archive) + ".sha256")
    try:
        _atomic_text(
            checksum_path,
            f"{archive_hash}  {final_archive.name}\n",
            mode=0o600,
        )
    except Exception:
        checksum_path.unlink(missing_ok=True)
        final_archive.unlink(missing_ok=True)
        raise
    return final_archive, checksum_path, archive_hash


def _capture_phase_a(
    *,
    staging: Path,
    project_root: Path,
    data_root: Path,
    inventory: dict,
    backup_id: str,
    created_at_utc: str,
    permission_metadata_builder: Callable[[Path], dict],
) -> dict:
    entries: list[dict] = []
    feedback = data_root / "feedback.json"
    lock_paths = [
        data_root / "api_usage.json",
        data_root / "subscriptions.json",
        feedback,
    ]

    with ExitStack() as locks:
        for path in lock_paths:
            locks.enter_context(file_lock(path))
        _strict_json(data_root / "api_usage.json")
        _strict_json(data_root / "subscriptions.json")
        if feedback.exists():
            _strict_json(feedback)

        create_readonly_snapshot(
            "core_snapshot",
            source_dir=data_root,
            output_root=staging,
            generated_at=created_at_utc,
            metadata_builder=permission_metadata_builder,
        )
        for item in inventory["selected"]:
            if item.source_rel in {"prices.db", "observations.sqlite3", "api_usage.json"}:
                continue
            _copy_item(item, staging)
        _add_core_entries(staging, entries)
        entries.extend(_refresh_copied_entries(staging, inventory["selected"]))
        entries.extend(inventory["absent"])
        payload = _manifest(
            backup_id=backup_id,
            created_at_utc=created_at_utc,
            project_root=project_root,
            files=sorted(entries, key=lambda item: item["path"]),
        )
        _write_manifest(staging / "manifest.json", payload)
    return payload


def _backup_id(timestamp: datetime) -> str:
    return f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"


def create_runtime_backup(
    *,
    output_dir: str | Path,
    project_root: str | Path = PROJECT_ROOT,
    data_root: str | Path | None = None,
    include_payloads: bool = True,
    round_log_days: int = 7,
    include_diagnostics: bool = False,
    strict: bool = True,
    replay_route: str | None = None,
    replay_pair: str | None = None,
    generated_at: str | None = None,
    permission_metadata_builder=None,
    report_builder=None,
    lock_path: str | Path | None = None,
    before_archive=None,
    _existing_gate=None,
) -> dict:
    project = Path(project_root).resolve()
    data = Path(data_root or project / "data").resolve()
    output = validate_output_directory(
        output_dir,
        project_root=project,
        data_root=data,
    )
    timestamp = (
        datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if generated_at
        else datetime.now(timezone.utc)
    )
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    timestamp = timestamp.astimezone(timezone.utc)
    created_at_utc = timestamp.isoformat().replace("+00:00", "Z")
    backup_id = _backup_id(timestamp)
    inventory = scan_runtime_state(
        data,
        include_payloads=include_payloads,
        round_log_days=round_log_days,
        include_diagnostics=include_diagnostics,
        strict=strict,
        today=timestamp.astimezone().date(),
    )

    owns_gate = _existing_gate is None
    gate = _existing_gate
    if gate is None:
        effective_lock_path = lock_path or resolve_collection_lock_path(base_dir=project)
        gate = acquire_collection_singleflight(
            f"backup_{backup_id}",
            lock_path=effective_lock_path,
            heartbeat_interval_seconds=0,
        )
    if not gate.acquired:
        return {
            "status": "busy",
            "exit_code": 2,
            "backup_id": backup_id,
            "archive_path": None,
            "checksum_path": None,
            "holder": dict(gate.holder or {}),
        }

    try:
        # The preflight scan protects the lock-free failure path; this second scan
        # freezes the exact payload and round-log member set under single-flight.
        inventory = scan_runtime_state(
            data,
            include_payloads=include_payloads,
            round_log_days=round_log_days,
            include_diagnostics=include_diagnostics,
            strict=strict,
            today=timestamp.astimezone().date(),
        )
        output.mkdir(parents=True, exist_ok=True)
        os.chmod(output, 0o700)
        temporary = tempfile.TemporaryDirectory(prefix="runtime-backup-", dir=output)
        staging = Path(temporary.name)
    except Exception:
        if owns_gate:
            gate.release()
        raise
    try:
        metadata_builder = permission_metadata_builder
        if metadata_builder is None:
            metadata_builder = lambda snapshot: build_permission_quality_metadata(
                snapshot,
                data_root=data,
            )
        try:
            manifest = _capture_phase_a(
                staging=staging,
                project_root=project,
                data_root=data,
                inventory=inventory,
                backup_id=backup_id,
                created_at_utc=created_at_utc,
                permission_metadata_builder=metadata_builder,
            )
        finally:
            if owns_gate:
                gate.release()

        replay = {"enabled": False}
        if replay_route:
            builder = report_builder or build_replay_reports
            report_hashes = builder(
                staging / "core_snapshot",
                staging / "replay",
                replay_route,
                replay_pair,
            )
            replay = {
                "enabled": True,
                "route": replay_route,
                "pair": replay_pair,
                "source_report_sha256": report_hashes,
            }
            for name in sorted(report_hashes):
                manifest["files"].append(
                    _entry_for_file(
                        staging / "replay" / name,
                        archive_path=f"replay/{name}",
                        source_rel=f"generated:{name}",
                        kind="replay_evidence",
                        required=False,
                    )
                )
        manifest["replay"] = replay
        manifest["files"] = sorted(manifest["files"], key=lambda item: item["path"])
        _write_manifest(staging / "manifest.json", manifest)

        if before_archive is not None:
            before_archive(staging)
        archive, checksum, archive_hash = _publish_archive(
            staging,
            output,
            backup_id,
        )
        present_files = [item for item in manifest["files"] if item.get("present")]
        return {
            "status": "created",
            "exit_code": 0,
            "backup_id": backup_id,
            "archive_path": str(archive),
            "checksum_path": str(checksum),
            "archive_sha256": archive_hash,
            "file_count": len(present_files),
            "total_bytes": sum(int(item.get("bytes") or 0) for item in present_files),
            "sqlite_integrity": all(
                item.get("integrity_check") in (None, "ok") for item in present_files
            ),
            "json_valid": all(
                not str(item.get("validation") or "").startswith(("json_error", "jsonl_error"))
                for item in present_files
            ),
            "source_report_sha256": replay.get("source_report_sha256", {}),
            "real_api_calls": 0,
        }
    finally:
        if owns_gate and not getattr(gate, "_released", True):
            gate.release()
        temporary.cleanup()


def sanitized_backup_summary(
    result: dict,
    *,
    restored: dict | None = None,
    replay: dict | None = None,
    production_state_changed: bool = False,
) -> dict:
    if result.get("status") == "busy":
        return {
            "status": "busy",
            "backup_id": result.get("backup_id"),
            "production_state_changed": False,
            "real_api_calls": 0,
        }
    return {
        "status": result.get("status"),
        "backup_id": result.get("backup_id"),
        "archive_sha256": result.get("archive_sha256"),
        "file_count": result.get("file_count"),
        "total_bytes": result.get("total_bytes"),
        "sqlite_integrity": (
            (restored or {}).get("sqlite_integrity")
            if restored is not None
            else result.get("sqlite_integrity")
        ),
        "json_valid": (
            (restored or {}).get("json_valid")
            if restored is not None
            else result.get("json_valid")
        ),
        "replay_sha256": (replay or {}).get("restored_report_sha256", {}),
        "production_state_changed": bool(production_state_changed),
        "real_api_calls": 0,
    }
