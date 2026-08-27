"""本地 API 配额用量台账。仅记录真实 HTTP 请求次数。

任务执行未经用户明示授权不得发起真实外部API调用;获授权时须报告执行前后台账值。
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from local_file_lock import FileLockTimeout, file_lock
from log_utils import safe_log
from project_time import SHANGHAI_TZ
from quota_policy import metrics as quota_metrics
from workload_class import UNKNOWN, normalize_workload_class


DEFAULT_USAGE_PATH = Path(__file__).parent / "data" / "api_usage.json"
DEFAULT_LOCK_TIMEOUT_SECONDS = 3.0
DEFAULT_LOCK_RETRIES = 1


UsageLockTimeout = FileLockTimeout


class UsageLedgerReadError(RuntimeError):
    """Raised when the quota ledger cannot be trusted for production use."""


class UsageLedgerAlreadyExists(RuntimeError):
    """Raised when explicit initialization would overwrite an existing ledger."""


def _empty_usage_ledger() -> dict:
    return {"version": 2, "dates": {}, "entries": []}


@contextmanager
def _usage_lock(
    usage_path: str | Path,
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
):
    """用进程内锁和平台文件锁覆盖完整读改写临界区。"""

    with file_lock(usage_path, timeout=timeout):
        yield


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _append_conflict_audit(
    *,
    usage_path: Path,
    conflict_log_path: str | Path | None,
    round_id: str | None,
    reason: str,
    counts: dict[str, int],
    workload_class: str,
    entrypoint: str,
) -> None:
    audit_path = Path(conflict_log_path or usage_path.parent / "api_usage_conflict.log")
    row = {
        "evidence_id": uuid.uuid4().hex,
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "round_id": str(round_id or "unknown"),
        "status": "pending_reconciliation",
        "usage_path": str(usage_path),
        "reason": str(reason),
        "counts": dict(counts),
        "workload_class": str(workload_class),
        "entrypoint": str(entrypoint),
    }
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        descriptor = os.open(
            audit_path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0),
        )
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        safe_log(
            f"[配额台账] 冲突审计失败 round={round_id or 'unknown'} 原因={exc}"
        )


def _validate_usage_payload(payload, *, path: Path) -> dict:
    if not isinstance(payload, dict):
        raise UsageLedgerReadError(f"配额台账根节点不是对象: {path}")
    missing = {"version", "dates", "entries"} - set(payload)
    if missing:
        raise UsageLedgerReadError(
            f"配额台账缺少必需字段 {','.join(sorted(missing))}: {path}"
        )
    if payload.get("version") != 2:
        raise UsageLedgerReadError(
            f"配额台账版本不受支持 version={payload.get('version')!r}: {path}"
        )
    dates = payload.get("dates")
    entries = payload.get("entries")
    if not isinstance(dates, dict) or not isinstance(entries, list):
        raise UsageLedgerReadError(f"配额台账 dates/entries 结构无效: {path}")
    for day, source_counts in dates.items():
        if not isinstance(day, str) or not isinstance(source_counts, dict):
            raise UsageLedgerReadError(f"配额台账日期桶结构无效: {path}")
        for source, count in source_counts.items():
            if (
                not isinstance(source, str)
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
            ):
                raise UsageLedgerReadError(f"配额台账日期计数无效: {path}")
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("counts"), dict):
            raise UsageLedgerReadError(f"配额台账明细结构无效: {path}")
        for source, count in entry["counts"].items():
            if (
                not isinstance(source, str)
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
            ):
                raise UsageLedgerReadError(f"配额台账明细计数无效: {path}")
    return payload


def load_usage_strict(path: str | Path = DEFAULT_USAGE_PATH) -> dict:
    """Load a production ledger without inventing an empty replacement."""

    usage_path = Path(path)
    if not usage_path.exists():
        raise UsageLedgerReadError(f"配额台账不存在: {usage_path}")
    try:
        payload = json.loads(usage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageLedgerReadError(f"配额台账读取失败: {usage_path}: {exc}") from exc
    return _validate_usage_payload(payload, path=usage_path)


def initialize_usage_ledger(path: str | Path = DEFAULT_USAGE_PATH) -> dict:
    """Create a new empty ledger only through this explicit initialization path."""

    usage_path = Path(path)
    with _usage_lock(usage_path):
        if usage_path.exists():
            raise UsageLedgerAlreadyExists(f"配额台账已存在: {usage_path}")
        payload = _empty_usage_ledger()
        _atomic_write_json(usage_path, payload)
    return payload


def load_usage_for_diagnostics(path: str | Path = DEFAULT_USAGE_PATH) -> dict:
    """Read without mutation and expose damage explicitly to diagnostic callers."""

    try:
        return {
            "healthy": True,
            "usage": load_usage_strict(path),
            "error_type": None,
            "error": None,
        }
    except UsageLedgerReadError as exc:
        return {
            "healthy": False,
            "usage": None,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def _pending_reconciliation_rows(conflict_log_path: Path) -> tuple[list[dict], str | None]:
    if not conflict_log_path.exists():
        return [], None
    pending: dict[str, dict] = {}
    reconciled: set[str] = set()
    try:
        lines = conflict_log_path.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"line {line_number} root is not an object")
            evidence_id = str(row.get("evidence_id") or f"legacy-line-{line_number}")
            status = str(row.get("status") or "")
            if status in {"pending_reconciliation", "write_conflict"}:
                pending[evidence_id] = row
            elif status == "reconciled":
                reconciled.add(evidence_id)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return [], f"冲突审计读取失败:{type(exc).__name__}:{exc}"
    return [row for key, row in pending.items() if key not in reconciled], None


def usage_ledger_health(
    path: str | Path = DEFAULT_USAGE_PATH,
    *,
    conflict_log_path: str | Path | None = None,
) -> dict:
    diagnostic = load_usage_for_diagnostics(path)
    audit_path = Path(conflict_log_path or Path(path).parent / "api_usage_conflict.log")
    pending, audit_error = _pending_reconciliation_rows(audit_path)
    healthy = bool(diagnostic["healthy"]) and not pending and audit_error is None
    return {
        **diagnostic,
        "healthy": healthy,
        "pending_reconciliation_count": len(pending),
        "pending_reconciliation": pending,
        "audit_error": audit_error,
    }


def entry_workload_class(entry) -> str:
    """Read workload metadata without rewriting historical ledger rows."""

    if not isinstance(entry, dict):
        return UNKNOWN
    return normalize_workload_class(entry.get("workload_class"))


def record_actual_requests(
    counts: dict[str, int],
    *,
    path: str | Path = DEFAULT_USAGE_PATH,
    day: str | None = None,
    round_id: str | None = None,
    recorded_at: str | None = None,
    workload_class: str = UNKNOWN,
    entrypoint: str = "unknown",
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    lock_retries: int = DEFAULT_LOCK_RETRIES,
    conflict_log_path: str | Path | None = None,
) -> dict:
    usage_path = Path(path)
    normalized_workload = normalize_workload_class(workload_class)
    normalized_entrypoint = str(entrypoint or "unknown")
    actual_counts = {}
    for source, raw_count in (counts or {}).items():
        count = max(0, int(raw_count or 0))
        if count:
            actual_counts[str(source)] = count
    attempts = max(0, int(lock_retries)) + 1
    last_error = None
    for _attempt in range(attempts):
        try:
            with _usage_lock(usage_path, timeout=lock_timeout):
                payload = load_usage_strict(usage_path)
                day_key = str(day or datetime.now(SHANGHAI_TZ).date().isoformat())
                day_counts = payload["dates"].setdefault(day_key, {})
                for source_name, count in actual_counts.items():
                    day_counts[source_name] = (
                        int(day_counts.get(source_name, 0) or 0) + count
                    )

                if actual_counts:
                    payload["entries"].append(
                        {
                            "recorded_at": str(
                                recorded_at
                                or datetime.now().astimezone().isoformat(
                                    timespec="seconds"
                                )
                            ),
                            "round_id": str(round_id or "unknown"),
                            "day": day_key,
                            "workload_class": normalized_workload,
                            "entrypoint": normalized_entrypoint,
                            "counts": actual_counts,
                        }
                    )

                _atomic_write_json(usage_path, payload)
                return payload
        except UsageLockTimeout as exc:
            last_error = exc
        except UsageLedgerReadError as exc:
            _append_conflict_audit(
                usage_path=usage_path,
                conflict_log_path=conflict_log_path,
                round_id=round_id,
                reason=str(exc),
                counts=actual_counts,
                workload_class=normalized_workload,
                entrypoint=normalized_entrypoint,
            )
            raise

    safe_log(
        f"[配额台账] 写入冲突 round={round_id or 'unknown'} "
        f"重试={max(0, int(lock_retries))} 原因={last_error}，已放弃本笔"
    )
    _append_conflict_audit(
        usage_path=usage_path,
        conflict_log_path=conflict_log_path,
        round_id=round_id,
        reason=str(last_error or "等待锁超时"),
        counts=actual_counts,
        workload_class=normalized_workload,
        entrypoint=normalized_entrypoint,
    )
    return load_usage_strict(usage_path)


def usage_snapshot(payload: dict, *, day: str | None = None) -> dict:
    day_key = str(day or datetime.now(SHANGHAI_TZ).date().isoformat())
    dates = (payload or {}).get("dates") or {}
    today_counts = {
        str(source): int(count or 0)
        for source, count in (dates.get(day_key) or {}).items()
    }
    month_key = day_key[:7]
    month_counts: dict[str, int] = {}
    cumulative: dict[str, int] = {}
    for observed_day, source_counts in dates.items():
        if not isinstance(source_counts, dict):
            continue
        for source, count in source_counts.items():
            source_name = str(source)
            value = int(count or 0)
            cumulative[source_name] = cumulative.get(source_name, 0) + value
            if str(observed_day).startswith(f"{month_key}-"):
                month_counts[source_name] = month_counts.get(source_name, 0) + value
    return {
        "today": today_counts,
        "month": month_counts,
        "cumulative": cumulative,
    }


def round_actual_counts(payload: dict, round_id: str | None) -> dict[str, int]:
    """Sum physical API attempts already persisted for one round."""

    target = str(round_id or "unknown")
    counts: dict[str, int] = {}
    for entry in (payload or {}).get("entries") or []:
        if not isinstance(entry, dict) or str(entry.get("round_id")) != target:
            continue
        for source, raw_count in (entry.get("counts") or {}).items():
            value = max(0, int(raw_count or 0))
            if value:
                source_name = str(source)
                counts[source_name] = counts.get(source_name, 0) + value
    return counts


def format_quota_overview(
    payload: dict,
    quota_budgets: dict,
    *,
    day: str | None = None,
) -> str:
    """用既有台账快照生成轮末与详情页共用的配额总览。"""
    snapshot = usage_snapshot(payload, day=day)
    budgets = quota_budgets or {}
    juhe = quota_metrics(
        budgets.get("juhe") or 0,
        snapshot,
        "juhe",
        usage_payload=payload,
        as_of=day,
    )
    serpapi = quota_metrics(
        budgets.get("serpapi") or 0,
        snapshot,
        "serpapi",
        usage_payload=payload,
        as_of=day,
    )

    return (
        f"[配额总览] juhe 本epoch已用={juhe['used']}/预算{juhe['total_limit']} "
        f"余量估算={juhe['remaining']} 储备={juhe['reserve']} "
        f"研究可用={juhe['research_available']}(以聚合数据控制台为准) · "
        f"serpapi 本月已用={serpapi['used']}/{serpapi['total_limit']} "
        f"余量估算={serpapi['remaining']}(reserve={serpapi['reserve']}) · "
        "duffel=不限额"
    )
