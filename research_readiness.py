"""Render the nine evidence gates required before research collection starts."""

from __future__ import annotations


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


def render_readiness_summary(hard_gate: dict) -> str:
    summary = build_readiness_summary(hard_gate)
    lines = [f"[研究就绪] ready={summary['ready']}"]
    labels = {"quota": "配额", "backup": "备份", "migration": "迁移"}
    for group, rows in summary["groups"].items():
        for item in rows:
            state = "通过" if item["passed"] else "未通过"
            current = "未知" if item["current"] is None else item["current"]
            detail = f" 原因={item['reason']}" if item.get("reason") else ""
            lines.append(
                f"[{labels[group]}] {item['name']}={state} 当前={current}{detail}"
            )
    missing = ",".join(summary["missing"]) if summary["missing"] else "无"
    lines.append(f"[研究就绪] 还差={missing}")
    return "\n".join(lines)
