"""Render the nine evidence gates required before research collection starts."""

from __future__ import annotations

from typing import Mapping


READINESS_GROUPS = {
    "quota": (
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
            f"reserve_basis={int(row.get('reserve_basis') or 0)}"
        )
    if reserve_details:
        lines.append(
            f"[配额推导] P90={reserve_details.get('scheduled_daily_p90')} "
            f"下限{reserve_details.get('minimum_daily_p90')}生效="
            f"{bool(reserve_details.get('minimum_floor_applied'))} "
            f"剩余天数={reserve_details.get('days_remaining')} "
            f"储备={reserve_details.get('monitoring_reserve')} "
            f"research_available={reserve_details.get('research_available')} "
            f"下一批可启动={bool(reserve_details.get('next_batch_can_start'))} "
            f"manual_live={reserve_details.get('manual_live_used')}/"
            f"{reserve_details.get('manual_live_buffer')} "
            f"scheduled异常={bool(reserve_details.get('scheduled_anomaly'))}"
        )
    missing = ",".join(summary["missing"]) if summary["missing"] else "无"
    lines.append(f"[研究就绪] 还差={missing}")
    return "\n".join(lines)
