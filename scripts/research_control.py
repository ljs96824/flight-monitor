"""Safely inspect, disable, or re-enable the research cohort runtime latch."""

from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from atomic_json_store import JsonStoreReadError  # noqa: E402
from config_loader import DEFAULT_CONFIG_PATH, RUNTIME_CONFIG_PATH  # noqa: E402
from project_time import SHANGHAI_TZ  # noqa: E402
from research_state_store import (  # noqa: E402
    ResearchStateConflict,
    load_research_state,
    update_research_state,
)
from scripts.research_quota_simulation import build_report  # noqa: E402
from subscription_preflight import shanghai_today  # noqa: E402


def _now_iso(now: str | None = None) -> str:
    return str(
        now
        or datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
    )


def _load_state(path: str | Path) -> dict:
    target = Path(path)
    if not target.exists():
        return {}
    return load_research_state(target)


def _cohort_for_update(payload, *, state_path: Path) -> tuple[dict, dict]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise JsonStoreReadError(f"研究运行态根节点不是对象: {state_path}")
    cohort = payload.setdefault("research_cohort_v2", {})
    if not isinstance(cohort, dict):
        raise JsonStoreReadError(
            f"research_cohort_v2不是对象: {state_path}"
        )
    return payload, cohort


def _update_control_state(target: Path, mutator, *, attempts: int = 5) -> dict:
    for _attempt in range(attempts):
        current = load_research_state(target)
        try:
            return update_research_state(target, current["revision"], mutator)
        except ResearchStateConflict:
            continue
    raise ResearchStateConflict(f"研究运行态连续{attempts}次发生并发冲突: {target}")


def runtime_control_status(state_path: str | Path, readiness_report: dict) -> dict:
    payload = _load_state(state_path)
    cohort = payload.get("research_cohort_v2") or {}
    if not isinstance(cohort, dict):
        raise JsonStoreReadError("research_cohort_v2不是对象")
    control = cohort.get("runtime_control") or {}
    guard = cohort.get("quota_guard") or {}
    hard_gate = (readiness_report or {}).get("hard_gate") or {}
    return {
        "runtime_enabled": bool(cohort.get("runtime_enabled", False)),
        "disabled_reason": (
            control.get("reason")
            if control.get("action") == "disable"
            else ",".join(guard.get("reason_codes") or []) or None
        ),
        "disabled_at": (
            control.get("at")
            if control.get("action") == "disable"
            else guard.get("disabled_at")
        ),
        "hard_gate": hard_gate,
    }


def disable_research(
    state_path: str | Path,
    *,
    reason: str,
    now: str | None = None,
) -> dict:
    normalized_reason = str(reason or "").strip()
    if not normalized_reason:
        raise ValueError("disable必须提供非空reason")
    target = Path(state_path)
    at = _now_iso(now)

    def mutate(payload):
        payload, cohort = _cohort_for_update(payload, state_path=target)
        cohort["runtime_enabled"] = False
        cohort["user_monitoring_enabled"] = True
        cohort["runtime_control"] = {
            "action": "disable",
            "reason": normalized_reason,
            "at": at,
        }
        return payload

    _update_control_state(target, mutate)
    return {
        "status": "disabled",
        "runtime_enabled": False,
        "reason": normalized_reason,
        "at": at,
    }


def enable_research(
    state_path: str | Path,
    readiness_report: dict,
    *,
    now: str | None = None,
) -> dict:
    hard_gate = (readiness_report or {}).get("hard_gate") or {}
    missing = list(hard_gate.get("missing") or [])
    if not bool(hard_gate.get("ready")):
        current = _load_state(state_path)
        cohort = current.get("research_cohort_v2") or {}
        return {
            "status": "blocked",
            "runtime_enabled": bool(
                isinstance(cohort, dict) and cohort.get("runtime_enabled", False)
            ),
            "missing": missing,
        }

    target = Path(state_path)
    at = _now_iso(now)

    def mutate(payload):
        payload, cohort = _cohort_for_update(payload, state_path=target)
        guard = cohort.pop("quota_guard", None)
        if isinstance(guard, dict) and guard:
            history = cohort.setdefault("quota_guard_history", [])
            if not isinstance(history, list):
                raise JsonStoreReadError(
                    f"quota_guard_history不是数组: {target}"
                )
            archived = dict(guard)
            archived["cleared_at"] = at
            history.append(archived)
        cohort["runtime_enabled"] = True
        cohort["user_monitoring_enabled"] = True
        cohort["runtime_control"] = {
            "action": "enable",
            "reason": "readiness_passed",
            "at": at,
            "checked_gates": sorted((hard_gate.get("checks") or {}).keys()),
        }
        return payload

    _update_control_state(target, mutate)
    return {
        "status": "enabled",
        "runtime_enabled": True,
        "missing": [],
        "at": at,
    }


def _add_common_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config-defaults", type=Path, default=DEFAULT_CONFIG_PATH
    )
    parser.add_argument(
        "--runtime-config", type=Path, default=RUNTIME_CONFIG_PATH
    )
    parser.add_argument(
        "--state", type=Path, default=ROOT / "data" / "basket_state.json"
    )
    parser.add_argument(
        "--subscriptions",
        type=Path,
        default=ROOT / "data" / "subscriptions.json",
    )
    parser.add_argument(
        "--observations",
        type=Path,
        default=ROOT / "data" / "observations.sqlite3",
    )
    parser.add_argument("--prices", type=Path, default=ROOT / "data" / "prices.db")
    parser.add_argument(
        "--usage", type=Path, default=ROOT / "data" / "api_usage.json"
    )
    parser.add_argument(
        "--backup-status",
        type=Path,
        default=ROOT / "data" / "backup_status.json",
    )
    parser.add_argument("--today", type=date.fromisoformat, default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    _add_common_paths(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="只读显示运行态与全部硬门")
    disable_parser = subparsers.add_parser("disable", help="人工停用研究采样")
    disable_parser.add_argument("--reason", required=True)
    enable_parser = subparsers.add_parser(
        "enable", help="全部readiness硬门通过后原子启用"
    )
    enable_parser.add_argument("--confirm", required=True)
    return parser


def _readiness_report(args) -> dict:
    return build_report(
        today=args.today or shanghai_today(),
        config_path=args.config_defaults,
        runtime_config_path=args.runtime_config,
        state_path=args.state,
        subscriptions_path=args.subscriptions,
        observations_path=args.observations,
        prices_path=args.prices,
        usage_path=args.usage,
        backup_status_path=args.backup_status,
    )


def _print_status(result: dict) -> None:
    print(f"runtime_enabled={str(bool(result['runtime_enabled'])).lower()}")
    print(f"disabled_reason={result.get('disabled_reason') or 'none'}")
    print(f"disabled_at={result.get('disabled_at') or 'none'}")
    hard_gate = result.get("hard_gate") or {}
    print(f"readiness_ready={str(bool(hard_gate.get('ready'))).lower()}")
    reasons = hard_gate.get("reasons") or {}
    for name, passed in (hard_gate.get("checks") or {}).items():
        suffix = "" if passed else f" reason={reasons.get(name, '未通过')}"
        print(f"gate={name} status={'pass' if passed else 'fail'}{suffix}")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "disable":
            result = disable_research(args.state, reason=args.reason)
            print(
                f"status={result['status']} reason={result['reason']} at={result['at']}"
            )
            return 0

        report = _readiness_report(args)
        if args.command == "status":
            _print_status(runtime_control_status(args.state, report))
            return 0

        if args.confirm != "ENABLE":
            print("拒绝启用：请使用 --confirm ENABLE", file=sys.stderr)
            return 2
        result = enable_research(args.state, report)
        if result["status"] == "blocked":
            print(
                "启用被拒：readiness未通过 "
                f"missing={','.join(result['missing'])}",
                file=sys.stderr,
            )
            return 2
        print(f"status=enabled at={result['at']} readiness=true")
        return 0
    except (JsonStoreReadError, OSError, ValueError) as exc:
        print(f"研究运行态操作失败:{type(exc).__name__}:{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
