"""Render the nine evidence gates required before research collection starts."""

from __future__ import annotations

from typing import Mapping


READINESS_GROUPS = {
    "quota": (
        "quota_ledger_healthy",
        "expected_days_remaining",
        "worst_case_days_remaining",
        "monitoring_reserve",
    ),
    "backup": (
        "backup_restore_verified",
        "off_disk_copy_verified",
        "off_disk_copy_fresh",
    ),
    "migration": (
        "timestamp_migration",
        "lineage_migration",
        "old_data_readable",
    ),
}

COLD_START_MACHINE_FIELDS = (
    "reserve_window_days",
    "fully_classified_days",
    "pure_unknown_days",
    "mixed_days",
    "telemetry_missing_days",
    "observed_raw_p90",
    "effective_scheduled_p90",
    "scheduled_daily_floor",
    "cold_start_active",
    "cold_start_reason",
    "cold_start_estimated",
    "cold_start_exit_condition",
    "cold_start_expected_exit_at",
    "monitoring_reserve",
    "research_available",
)


def build_readiness_summary(hard_gate: dict) -> dict:
    checks = hard_gate.get("checks") or {}
    current = hard_gate.get("current") or {}
    requirements = hard_gate.get("requirements") or {}
    reasons = hard_gate.get("reasons") or {}
    groups = {}
    for group, names in READINESS_GROUPS.items():
        groups[group] = [
            {
                "name": name,
                "passed": bool(checks.get(name)),
                "current": current.get(name),
                "reason": reasons.get(name),
            }
            for name in names
        ]
    return {
        "ready": bool(hard_gate.get("ready")),
        "groups": groups,
        "requirements": requirements,
        "missing": [
            item["name"]
            for rows in groups.values()
            for item in rows
            if not item["passed"]
        ],
    }


def _workload_reserve_details(hard_gate: dict) -> dict:
    current = hard_gate.get("current") or {}
    monitoring = current.get("monitoring_reserve") or {}
    if not isinstance(monitoring, Mapping):
        return {}
    details = monitoring.get("reserve_details") or {}
    return dict(details) if isinstance(details, Mapping) else {}


def render_readiness_summary(hard_gate: dict) -> str:
    summary = build_readiness_summary(hard_gate)
    lines = [f"[研究就绪] ready={summary['ready']}"]
    labels = {"quota": "配额", "backup": "备份", "migration": "迁移"}
    for group, rows in summary["groups"].items():
        for item in rows:
            state = "通过" if item["passed"] else "未通过"
            current_value = item["current"]
            if item["name"] == "monitoring_reserve" and isinstance(
                current_value, Mapping
            ):
                current = {
                    "remaining_after_research": current_value.get(
                        "remaining_after_research"
                    ),
                    "required_reserve": current_value.get("required_reserve"),
                }
            else:
                current = "未知" if current_value is None else current_value
            detail = f" 原因={item['reason']}" if item.get("reason") else ""
            lines.append(
                f"[{labels[group]}] {item['name']}={state} 当前={current}{detail}"
            )

    reserve_details = _workload_reserve_details(hard_gate)
    for row in reserve_details.get("daily_counts") or []:
        lines.append(
            f"[配额推导] {row.get('day')} "
            f"scheduled={int(row.get('scheduled_user_monitor') or 0)} "
            f"unknown={int(row.get('unknown') or 0)} "
            f"reserve_basis={int(row.get('reserve_basis') or 0)} "
            f"day_type={row.get('day_type') or 'legacy'} "
            f"sample_value={int(row.get('sample_value') or 0)}"
        )
    if reserve_details:
        lines.append(
            f"[配额推导] P90={reserve_details.get('scheduled_daily_p90')} "
            f"原始P90={reserve_details.get('observed_raw_p90')} "
            f"下限{reserve_details.get('minimum_daily_p90')}生效="
            f"{bool(reserve_details.get('minimum_floor_applied'))} "
            f"剩余天数={reserve_details.get('days_remaining')} "
            f"储备={reserve_details.get('monitoring_reserve')} "
            f"research_available={reserve_details.get('research_available')} "
            f"下一批可启动={bool(reserve_details.get('next_batch_can_start'))} "
            f"储备纪元={reserve_details.get('reserve_epoch_started_at') or '未配置'} "
            f"manual_live={reserve_details.get('manual_live_in_epoch', reserve_details.get('manual_live_used'))}/"
            f"{reserve_details.get('manual_live_buffer')} "
            f"剩余={reserve_details.get('manual_live_buffer_remaining')} "
            f"lifetime={reserve_details.get('manual_live_lifetime')} "
            f"canary={reserve_details.get('canary_in_epoch', reserve_details.get('canary_used'))}/"
            f"{reserve_details.get('canary_buffer')} "
            f"剩余={reserve_details.get('canary_buffer_remaining')} "
            f"lifetime={reserve_details.get('canary_lifetime')} "
            f"scheduled异常={bool(reserve_details.get('scheduled_anomaly'))}"
        )
    if "reserve_window_days" in reserve_details:
        for field in COLD_START_MACHINE_FIELDS:
            lines.append(f"[配额机器字段] {field}={reserve_details.get(field)}")
        if reserve_details.get("cold_start_active"):
            unknown_days = len(reserve_details.get("pure_unknown_days") or [])
            floor = int(reserve_details.get("scheduled_daily_floor") or 0)
            lines.append(
                "冷启动期:最近7个完整日尚未形成完整工作负载分类,"
                f"其中{unknown_days}日为历史unknown;"
                f"储备暂按每日{floor}次下限估算,非实测结论。"
                "连续获得7个完整分类日后自动退出该规则。"
            )
        else:
            lines.append(
                "冷启动期已结束:最近7个完整日均具备完整工作负载分类,"
                "储备按实测P90计算。"
            )
    missing = ",".join(summary["missing"]) if summary["missing"] else "无"
    lines.append(f"[研究就绪] 还差={missing}")
    return "\n".join(lines)
