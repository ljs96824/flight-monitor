"""只读模拟研究篮子与订阅轮的 Juhe 全系统日配额。"""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import date, datetime, time, timezone
import io
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from atomic_json_store import read_json  # noqa: E402
from backup_status import load_backup_evidence  # noqa: E402
from basket_collect import (  # noqa: E402
    _load_active_subscriptions_for_research,
    _simulate_runtime_quota,
)
from collection_plan import load_collection_settings  # noqa: E402
from research_cohort import (  # noqa: E402
    active_user_monitor_dates,
    evaluate_research_hard_gates,
    inspect_research_migrations,
    prepare_research_requests,
)
from project_time import SHANGHAI_TZ  # noqa: E402
from sources.aggregator import build_default_sources  # noqa: E402
from subscription_preflight import shanghai_today  # noqa: E402


def _read_state(path: Path) -> dict:
    if not path.is_file():
        return {}
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise ValueError("basket_state.json 格式错误，应为对象")
    return payload


def _build_report_inputs(
    *,
    today: date,
    config_path: str | Path,
    state_path: str | Path,
    subscriptions_path: str | Path,
    observations_path: str | Path,
    prices_path: str | Path,
    usage_path: str | Path,
    source_builder=build_default_sources,
    other_scheduled_calls: int | None = None,
) -> tuple[dict, dict, dict, list[dict]]:
    settings = load_collection_settings(config_path)
    if other_scheduled_calls is not None:
        settings = deepcopy(settings)
        gates = dict(settings.get("research_cohort_v2_gates") or {})
        gates["other_scheduled_calls"] = max(0, int(other_scheduled_calls))
        settings["research_cohort_v2_gates"] = gates

    subscriptions = _load_active_subscriptions_for_research(
        subscriptions_path,
        today=today,
    )
    state = deepcopy(_read_state(Path(state_path)))
    schedule = prepare_research_requests(
        state,
        today=today,
        user_monitor_dates=active_user_monitor_dates(
            subscriptions,
            origin="PVG",
            dest="KIX",
        ),
    )
    quota = _simulate_runtime_quota(
        research_requests=schedule.requests,
        subscriptions=subscriptions,
        settings=settings,
        source_builder=source_builder,
        usage_path=usage_path,
        db_path=observations_path,
        today=today,
    )
    migrations = inspect_research_migrations(observations_path, prices_path)
    return quota, migrations, settings, schedule.requests


def build_report(
    *,
    today: date,
    config_path: str | Path,
    state_path: str | Path,
    subscriptions_path: str | Path,
    observations_path: str | Path,
    prices_path: str | Path,
    usage_path: str | Path,
    backup_status_path: str | Path | None = None,
    source_builder=build_default_sources,
    other_scheduled_calls: int | None = None,
) -> dict:
    """Build the gate report without executing or persisting a request."""
    quota, migrations, settings, requests = _build_report_inputs(
        today=today,
        config_path=config_path,
        state_path=state_path,
        subscriptions_path=subscriptions_path,
        observations_path=observations_path,
        prices_path=prices_path,
        usage_path=usage_path,
        source_builder=source_builder,
        other_scheduled_calls=other_scheduled_calls,
    )
    gate_config = settings.get("research_cohort_v2_gates") or {}
    max_age_days = int(gate_config.get("backup_evidence_max_age_days", 30))
    evidence_path = Path(
        backup_status_path
        or Path(state_path).resolve().parent / "backup_status.json"
    )
    report_now = datetime.combine(today, time.max, tzinfo=SHANGHAI_TZ).astimezone(
        timezone.utc
    )
    backup = load_backup_evidence(
        evidence_path,
        now=report_now,
        max_age_days=max_age_days,
    )
    hard_gate = evaluate_research_hard_gates(
        backup_evidence=backup,
        quota_simulation=quota,
        migration_status=migrations,
        minimum_expected_days=int(gate_config.get("minimum_expected_days", 30)),
        minimum_worst_case_days=int(
            gate_config.get("minimum_worst_case_days", 20)
        ),
    )
    return {
        "today": today.isoformat(),
        "research_switch_enabled": bool(settings.get("research_basket_enabled")),
        "research_basket_enabled": bool(settings.get("research_basket_enabled")),
        "research_basket_strategy": settings.get("research_basket_strategy"),
        "research_request_count": len(requests),
        "sample_roles": {
            role: sum(1 for item in requests if item.get("sample_role") == role)
            for role in ("trajectory_anchor", "cross_sectional_probe")
        },
        "quota": quota,
        "backup": backup,
        "migrations": migrations,
        "hard_gate": hard_gate,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument(
        "--state",
        type=Path,
        default=ROOT / "data" / "basket_state.json",
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
    parser.add_argument(
        "--prices",
        type=Path,
        default=ROOT / "data" / "prices.db",
    )
    parser.add_argument(
        "--usage",
        type=Path,
        default=ROOT / "data" / "api_usage.json",
    )
    parser.add_argument(
        "--backup-status",
        type=Path,
        default=ROOT / "data" / "backup_status.json",
    )
    parser.add_argument("--today", type=date.fromisoformat, default=None)
    parser.add_argument("--other-scheduled-calls", type=int, default=None)
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    diagnostics = io.StringIO()
    with redirect_stdout(diagnostics):
        report = build_report(
            today=args.today or shanghai_today(),
            config_path=args.config,
            state_path=args.state,
            subscriptions_path=args.subscriptions,
            observations_path=args.observations,
            prices_path=args.prices,
            usage_path=args.usage,
            backup_status_path=args.backup_status,
            other_scheduled_calls=args.other_scheduled_calls,
        )
    if diagnostics.getvalue():
        print(diagnostics.getvalue(), file=sys.stderr, end="")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
