"""Shared preflight for explicitly authorized manual live API audits."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Mapping

import config_loader
import quota_policy
from api_usage import (
    UsageLedgerReadError,
    load_usage_strict,
    usage_ledger_health,
    usage_snapshot,
)
from config_loader import RuntimeConfigError
from subscription_preflight import shanghai_today


NO_LIVE_API_VALUES = frozenset({"1", "true", "yes", "on"})

EXIT_NO_LIVE_API = 3
EXIT_LEDGER_UNHEALTHY = 4
EXIT_QUOTA_BLOCKED = 5
EXIT_SINGLEFLIGHT_BUSY = 6
EXIT_INVALID_DATE = 7
EXIT_CONFIG_UNHEALTHY = 8


@dataclass
class ManualLiveGateResult:
    allowed: bool
    gate_code: str
    gate_reason: str
    exit_code: int
    ledger_snapshot: dict | None = None
    quota_checks: tuple[dict, ...] = ()
    usage_payload: dict | None = field(default=None, repr=False, compare=False)
    lock_gate: object | None = field(default=None, repr=False, compare=False)

    def report_fields(self) -> dict:
        return {
            "status": "ready" if self.allowed else "blocked",
            "gate_code": self.gate_code,
            "gate_reason": self.gate_reason,
            "exit_code": self.exit_code,
            "quota_preflight": [dict(row) for row in self.quota_checks],
        }

    def release(self) -> None:
        gate = self.lock_gate
        if gate is not None:
            gate.release()
            self.lock_gate = None


def no_live_api_enabled(environment: Mapping[str, str] | None = None) -> bool:
    values = environment or {}
    return str(values.get("NO_LIVE_API") or "").strip().lower() in NO_LIVE_API_VALUES


def _blocked(
    code: str,
    reason: str,
    exit_code: int,
    *,
    usage_payload: dict | None = None,
    ledger_snapshot: dict | None = None,
    quota_checks: tuple[dict, ...] = (),
) -> ManualLiveGateResult:
    return ManualLiveGateResult(
        allowed=False,
        gate_code=code,
        gate_reason=reason,
        exit_code=exit_code,
        usage_payload=usage_payload,
        ledger_snapshot=ledger_snapshot,
        quota_checks=quota_checks,
    )


def _validate_departure_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("出发日期必须为 YYYY-MM-DD") from exc
    if parsed < shanghai_today():
        raise ValueError("出发日期早于 Asia/Shanghai 今日")
    return parsed


def _quota_preflight(
    *,
    config: Mapping,
    usage_payload: dict,
    planned_counts: Mapping[str, int],
    as_of: date,
) -> tuple[tuple[dict, ...], str | None]:
    budgets = config.get("source_quota_budget")
    if not isinstance(budgets, Mapping):
        raise RuntimeConfigError("source_quota_budget 必须为对象")

    snapshot = usage_snapshot(usage_payload, day=as_of.isoformat())
    rows = []
    blocked = []
    for source, raw_count in sorted(planned_counts.items()):
        planned = max(0, int(raw_count or 0))
        if not planned:
            continue
        policy = budgets.get(str(source))
        if policy is None:
            rows.append(
                {
                    "source": str(source),
                    "planned": planned,
                    "quota_kind": "unlimited",
                    "allowed": True,
                }
            )
            continue

        values = quota_policy.metrics(
            policy,
            snapshot,
            str(source),
            usage_payload=usage_payload,
            as_of=as_of,
        )
        remaining = int(values.get("remaining") or 0)
        reserve = int(values.get("reserve") or 0)
        remaining_after = remaining - planned
        reserve_details = values.get("reserve_details") or {}
        manual_remaining = reserve_details.get("manual_live_buffer_remaining")
        allowed = remaining_after >= reserve
        if manual_remaining is not None:
            allowed = allowed and int(manual_remaining) >= planned
        row = {
            "source": str(source),
            "planned": planned,
            "quota_kind": str(values.get("kind") or "unknown"),
            "remaining": remaining,
            "reserve": reserve,
            "remaining_after": remaining_after,
            "allowed": bool(allowed),
        }
        if manual_remaining is not None:
            row["manual_live_buffer_remaining"] = int(manual_remaining)
        rows.append(row)
        if not allowed:
            blocked.append(str(source))

    reason = None
    if blocked:
        reason = "配额或储备预检未通过(source=" + ",".join(blocked) + ")"
    return tuple(rows), reason


def _acquire_singleflight(round_id: str):
    from collection_singleflight import acquire_collection_singleflight

    return acquire_collection_singleflight(str(round_id))


def prepare_manual_live_execution(
    *,
    environment: Mapping[str, str],
    depart_date: str,
    planned_counts: Mapping[str, int],
    usage_path: str | Path,
    round_id: str,
) -> ManualLiveGateResult:
    """Fail closed before credentials or HTTP, then acquire the shared lock."""

    if no_live_api_enabled(environment):
        return _blocked(
            "no_live_api",
            "NO_LIVE_API 已启用，拒绝真实 API 审计",
            EXIT_NO_LIVE_API,
        )

    try:
        _validate_departure_date(depart_date)
    except ValueError as exc:
        return _blocked("past_departure_date", str(exc), EXIT_INVALID_DATE)
    as_of = shanghai_today()

    try:
        usage_payload = load_usage_strict(usage_path)
    except UsageLedgerReadError:
        return _blocked(
            "quota_ledger_unhealthy",
            "配额台账不可严格读取，拒绝真实 API 审计",
            EXIT_LEDGER_UNHEALTHY,
        )
    health = usage_ledger_health(usage_path)
    ledger_snapshot = usage_snapshot(usage_payload, day=as_of.isoformat())
    if not health.get("healthy"):
        return _blocked(
            "quota_ledger_unhealthy",
            "配额台账存在未解决差异或待对账证据，拒绝真实 API 审计",
            EXIT_LEDGER_UNHEALTHY,
            usage_payload=usage_payload,
            ledger_snapshot=ledger_snapshot,
        )

    try:
        merged_config = config_loader.load_merged_config()
        quota_checks, quota_reason = _quota_preflight(
            config=merged_config,
            usage_payload=usage_payload,
            planned_counts=planned_counts,
            as_of=as_of,
        )
    except (RuntimeConfigError, OSError, ValueError, TypeError):
        return _blocked(
            "runtime_config_unhealthy",
            "运行配置不可严格读取，拒绝真实 API 审计",
            EXIT_CONFIG_UNHEALTHY,
            usage_payload=usage_payload,
            ledger_snapshot=ledger_snapshot,
        )
    if quota_reason:
        return _blocked(
            "quota_or_reserve",
            quota_reason,
            EXIT_QUOTA_BLOCKED,
            usage_payload=usage_payload,
            ledger_snapshot=ledger_snapshot,
            quota_checks=quota_checks,
        )

    gate = _acquire_singleflight(str(round_id))
    if not gate.acquired:
        return _blocked(
            "singleflight_busy",
            "已有采集轮正在运行，审计未启动",
            EXIT_SINGLEFLIGHT_BUSY,
            usage_payload=usage_payload,
            ledger_snapshot=ledger_snapshot,
            quota_checks=quota_checks,
        )
    return ManualLiveGateResult(
        allowed=True,
        gate_code="ready",
        gate_reason="台账、配额与单飞锁预检通过",
        exit_code=0,
        ledger_snapshot=ledger_snapshot,
        quota_checks=quota_checks,
        usage_payload=usage_payload,
        lock_gate=gate,
    )
