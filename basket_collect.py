"""固定机场篮子的每日新鲜采集入口，只采集并写入观测库。"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from api_usage import load_usage, usage_snapshot
from backup_status import load_backup_evidence
from atomic_json_store import read_json
from collection_plan import build_collection_plan, load_collection_settings
from collection_singleflight import (
    acquire_collection_singleflight,
    collection_busy_status,
)
from log_utils import configure_stdio_utf8, end_round_log_archive, safe_log, start_round_log_archive
from quota_policy import metrics as quota_metrics
from observations_store import (
    DEFAULT_DB_PATH,
    count_observations_for_round,
    reset_current_round,
    set_current_round,
)
from retention import log_retention_dry_run
from request_cache import (
    activate_collection_plan,
    deactivate_collection_plan,
    print_request_cache_stats,
    reset_request_cache,
    start_request_cache_round,
)
from research_cohort import (
    active_user_monitor_dates,
    apply_research_quota_guard,
    apply_research_round_outcomes,
    record_research_ledger_degraded,
    evaluate_research_hard_gates,
    inspect_research_migrations,
    load_research_round_ids,
    prepare_research_requests,
    research_runtime_enabled,
    simulate_research_quota,
)
from source_profiles import retired_listing_sources
from sources.aggregator import FlightAggregator, build_default_sources
from subscription_preflight import evaluate_subscription_preflight, shanghai_today
from workload_class import CANARY, RESEARCH_COHORT


BASE_DIR = Path(__file__).parent
DEFAULT_STATE_PATH = BASE_DIR / "data" / "basket_state.json"
CONFIG_PATH = BASE_DIR / "config.yaml"
API_USAGE_PATH = BASE_DIR / "data" / "api_usage.json"

BASKET_ROUTES = (
    {
        "route": "SHA->PEK",
        "origin": "SHA",
        "dest": "PEK",
        "route_type": "domestic",
        "sources": ("juhe",),
        "queue_a_date": "2026-07-31",
    },
    {
        "route": "PVG->HKG",
        "origin": "PVG",
        "dest": "HKG",
        "route_type": "greater_china",
        "sources": ("juhe", "hasdata"),
        "queue_a_offset_days": 45,
    },
    {
        "route": "PVG->KIX",
        "origin": "PVG",
        "dest": "KIX",
        "route_type": "international",
        "sources": ("hasdata", "juhe"),
        "queue_a_date": "2026-10-01",
    },
)


def _route_name(route: dict) -> str:
    return str(route.get("route") or f"{route['origin']}->{route['dest']}")


def _initial_queue_a(route: dict, today: date) -> str:
    fixed = route.get("queue_a_date")
    if fixed:
        return str(fixed)
    offset_days = int(route.get("queue_a_offset_days") or 45)
    return (today + timedelta(days=offset_days)).isoformat()


def build_initial_state(today: date) -> dict:
    queue_b = (today + timedelta(days=60)).isoformat()
    return {
        "version": 1,
        "created_on": today.isoformat(),
        "routes": {
            _route_name(route): {
                "A": _initial_queue_a(route, today),
                "B": queue_b,
            }
            for route in BASKET_ROUTES
        },
    }


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temp_path.replace(path)


def load_or_create_state(state_path: str | Path, today: date) -> dict:
    path = Path(state_path)
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"篮子状态文件不可读: {path}: {exc}") from exc
        if not isinstance(state.get("routes"), dict):
            raise RuntimeError(f"篮子状态文件缺少routes: {path}")
        return state
    state = build_initial_state(today)
    _write_state(path, state)
    return state


def renew_expired_queues(state: dict, today: date) -> list[dict]:
    renewals = []
    new_date = (today + timedelta(days=60)).isoformat()
    for route_name, queues in (state.get("routes") or {}).items():
        for queue_name, depart_text in list((queues or {}).items()):
            try:
                days_left = (date.fromisoformat(str(depart_text)) - today).days
            except ValueError as exc:
                raise RuntimeError(
                    f"篮子日期无效: route={route_name} queue={queue_name} date={depart_text}"
                ) from exc
            if days_left >= 1:
                continue
            queues[queue_name] = new_date
            renewal = {
                "route": route_name,
                "queue": queue_name,
                "old": str(depart_text),
                "new": new_date,
            }
            renewals.append(renewal)
            safe_log(f"[队列续期] route={route_name} 旧={depart_text} 新={new_date}")
    return renewals


def _source_name(source) -> str:
    return str(getattr(source, "name", type(source).__name__)).lower()


def _build_route_aggregator(
    route: dict,
    source_builder: Callable,
    aggregator_factory: Callable,
):
    search_sources, _enrichment_sources = source_builder(
        route["origin"],
        route["dest"],
        route_type=route["route_type"],
    )
    available = {_source_name(source): source for source in search_sources}
    required = list(route["sources"])
    retired = {
        str(item.get("name") or "").strip().lower()
        for item in retired_listing_sources(route["route_type"])
    }
    missing = [
        name
        for name in required
        if name not in available and name not in retired
    ]
    if missing:
        raise RuntimeError(f"缺少配置源:{','.join(missing)}")
    selected = [available[name] for name in required if name in available]
    if not selected:
        raise RuntimeError("当前源策略无可用列表源")
    return aggregator_factory(selected, [], route_type=route["route_type"])


def make_round_id(now: datetime) -> str:
    return now.strftime("basket_%Y%m%dT%H%M%S")


def _basket_requests(state: dict) -> list[dict]:
    requests = []
    for route in BASKET_ROUTES:
        route_name = _route_name(route)
        queue_dates = (state.get("routes") or {}).get(route_name) or {}
        for queue_name in ("A", "B"):
            requests.append(
                {
                    "origin": route["origin"],
                    "dest": route["dest"],
                    "depart_date": str(queue_dates.get(queue_name) or ""),
                    "route_type": route["route_type"],
                    "sources": route["sources"],
                    "queue": f"{route_name}:{queue_name}",
                    "cabin_class": "economy",
                }
            )
    return requests


def _load_active_subscriptions_for_research(
    path: str | Path,
    *,
    today: date,
) -> list[dict]:
    target = Path(path)
    if not target.is_file():
        return []
    payload = read_json(target)
    if not isinstance(payload, list):
        raise ValueError("subscriptions.json 格式错误，应为订阅数组")
    active = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "active").strip().lower()
        if status not in {"active", "enabled"}:
            continue
        if evaluate_subscription_preflight(item, today=today).get("skip"):
            continue
        active.append(item)
    return active


def _juhe_plan_keys(plan) -> set[tuple]:
    return {key for key in plan.request_keys if key and key[0] == "juhe"}


def _simulate_runtime_quota(
    *,
    research_requests: list[dict],
    subscriptions: list[dict],
    settings: dict,
    source_builder: Callable,
    usage_path: str | Path,
    db_path: str | Path | None = None,
    today: date | None = None,
) -> dict:
    basket_plan = build_collection_plan(
        subscriptions=[],
        basket_requests=research_requests,
        source_builder=source_builder,
        freshness_hours=settings.get("freshness_hours", 6),
        fresh_scope=settings.get("sub_round_fresh_scope", "primary_only"),
    )
    subscription_plan = build_collection_plan(
        subscriptions=subscriptions,
        basket_requests=[],
        source_builder=source_builder,
        freshness_hours=settings.get("freshness_hours", 6),
        fresh_scope=settings.get("sub_round_fresh_scope", "primary_only"),
    )
    usage_payload = load_usage(usage_path)
    snapshot = usage_snapshot(usage_payload, day=(today or shanghai_today()).isoformat())
    juhe_policy = (settings.get("source_quota_budget") or {}).get("juhe") or 0
    research_ids = load_research_round_ids(db_path) if db_path else set()
    policy = quota_metrics(
        juhe_policy,
        snapshot,
        "juhe",
        usage_payload=usage_payload,
        as_of=today,
        research_round_ids=research_ids,
    )
    gate_config = settings.get("research_cohort_v2_gates") or {}
    other_calls_declared = "other_scheduled_calls" in gate_config
    quota = simulate_research_quota(
        basket_keys=_juhe_plan_keys(basket_plan),
        subscription_keys=_juhe_plan_keys(subscription_plan),
        other_scheduled_calls=int(gate_config.get("other_scheduled_calls") or 0),
        quota_remaining=policy["remaining"],
        retries_per_request=1,
        monitoring_reserve=policy["reserve"],
    )
    reserve_details = dict(policy.get("reserve_details") or {})
    quota.update(
        {
            "quota_total_limit": policy["total_limit"],
            "quota_used": policy["used"],
            "research_available": policy["research_available"],
            "research_batch_calls": int(reserve_details.get("research_batch_calls") or 30),
            "scheduled_anomaly": bool(reserve_details.get("scheduled_anomaly")),
            "manual_live_used": int(reserve_details.get("manual_live_used") or 0),
            "manual_live_buffer": int(reserve_details.get("manual_live_buffer") or 30),
            "next_batch_can_start": bool(
                reserve_details.get("next_batch_can_start", True)
            ),
            "reserve_details": reserve_details,
        }
    )
    quota["complete"] = bool(
        other_calls_declared
        and policy["total_limit"] > 0
        and quota["basket_planned_unique"] == len(research_requests)
    )
    return quota


def _default_quota_guard_notifier(
    state_path: str | Path,
    title: str,
    content: str,
) -> bool:
    subscriptions_path = Path(state_path).resolve().parent / "subscriptions.json"
    try:
        payload = read_json(subscriptions_path)
    except (OSError, ValueError):
        payload = []
    if isinstance(payload, dict):
        subscriptions = payload.get("subscriptions") or []
    else:
        subscriptions = payload or []
    try:
        from main import _notify_system_alert

        return bool(_notify_system_alert(list(subscriptions), title, content))
    except Exception as exc:
        safe_log(f"[配额守卫] 通知失败 原因={type(exc).__name__}:{exc}")
        return False


def _prepare_research_basket(
    state: dict,
    *,
    today: date,
    state_path: str | Path,
    db_path: str | Path,
    usage_path: str | Path,
    settings: dict,
    source_builder: Callable,
    quota_guard_notifier=None,
) -> tuple[dict, list[dict], dict, dict]:
    subscriptions_path = Path(state_path).resolve().parent / "subscriptions.json"
    prices_path = Path(state_path).resolve().parent / "prices.db"
    subscriptions = _load_active_subscriptions_for_research(
        subscriptions_path,
        today=today,
    )
    staged_state = deepcopy(state)
    user_dates = active_user_monitor_dates(
        subscriptions,
        origin="PVG",
        dest="KIX",
    )
    schedule = prepare_research_requests(
        staged_state,
        today=today,
        user_monitor_dates=user_dates,
    )
    quota = _simulate_runtime_quota(
        research_requests=schedule.requests,
        subscriptions=subscriptions,
        settings=settings,
        source_builder=source_builder,
        usage_path=usage_path,
        db_path=db_path,
        today=today,
    )
    guard = apply_research_quota_guard(
        staged_state,
        quota,
        notifier=quota_guard_notifier,
    )
    quota["guard_triggered"] = bool(guard.get("triggered"))
    migration = inspect_research_migrations(db_path, prices_path)
    gate_config = settings.get("research_cohort_v2_gates") or {}
    backup_evidence = load_backup_evidence(
        Path(state_path).resolve().parent / "backup_status.json",
        max_age_days=int(gate_config.get("backup_evidence_max_age_days", 30)),
    )
    gate = evaluate_research_hard_gates(
        backup_evidence=backup_evidence,
        quota_simulation=quota,
        migration_status=migration,
        minimum_expected_days=int(gate_config.get("minimum_expected_days", 30)),
        minimum_worst_case_days=int(
            gate_config.get("minimum_worst_case_days", 20)
        ),
    )
    if guard.get("triggered"):
        gate["checks"]["quota_guard"] = False
        if "quota_guard" not in gate["missing"]:
            gate["missing"].insert(0, "quota_guard")
        gate["ready"] = False
    safe_log(
        "[研究配额模拟] "
        f"basket_planned_unique={quota.get('basket_planned_unique')} "
        f"basket_normal_actual={quota.get('basket_normal_actual')} "
        f"basket_retry_ceiling={quota.get('basket_retry_ceiling')} "
        f"subscription_planned_unique={quota.get('subscription_planned_unique')} "
        f"other_scheduled_calls={quota.get('other_scheduled_calls')} "
        f"combined_daily_expected={quota.get('combined_daily_expected')} "
        f"combined_daily_worst_case={quota.get('combined_daily_worst_case')} "
        f"expected_days_remaining={quota.get('expected_days_remaining')} "
        f"worst_case_days_remaining={quota.get('worst_case_days_remaining')} "
        f"remaining_after_research={quota.get('remaining_after_research')} "
        f"monitoring_reserve={quota.get('monitoring_reserve')}"
    )
    safe_log(
        f"[研究采样门] ready={gate['ready']} "
        f"missing={','.join(gate['missing']) if gate['missing'] else 'none'}"
    )
    if not gate["ready"]:
        guarded_state = staged_state if guard.get("triggered") else state
        return guarded_state, [], gate, quota
    for item in settings.get("paused_research_routes") or []:
        safe_log(
            f"[研究采样] 已暂停 route={item.get('route')} "
            f"reason={item.get('reason')} resume_when={item.get('resume_when')}"
        )
    for event in schedule.events:
        safe_log(
            f"[研究采样] 事件={event.get('kind')} slot={event.get('slot')} "
            f"T={event.get('target_t')} 日期={event.get('depart_date')}"
        )
    return staged_state, schedule.requests, gate, quota


def run_basket(
    *,
    today: date | None = None,
    now: datetime | None = None,
    state_path: str | Path = DEFAULT_STATE_PATH,
    db_path: str | Path = DEFAULT_DB_PATH,
    usage_path: str | Path = API_USAGE_PATH,
    source_builder: Callable = build_default_sources,
    aggregator_factory: Callable = FlightAggregator,
    singleflight_lock_path: str | Path | None = None,
    quota_guard_notifier=None,
    workload_class: str = RESEARCH_COHORT,
) -> dict:
    today = today or shanghai_today()
    now = now or datetime.now()
    round_id = make_round_id(now)
    singleflight = acquire_collection_singleflight(
        round_id,
        lock_path=singleflight_lock_path,
    )
    if not singleflight.acquired:
        busy = collection_busy_status(singleflight, entrypoint="basket")
        return {
            **busy,
            "round_id": round_id,
            "queues": 0,
            "success": 0,
            "failed": 0,
            "written": 0,
            "skipped": True,
            "reason": "singleflight_busy",
        }
    try:
        return _run_basket_locked(
            today=today,
            now=now,
            state_path=state_path,
            db_path=db_path,
            usage_path=usage_path,
            source_builder=source_builder,
            aggregator_factory=aggregator_factory,
            quota_guard_notifier=quota_guard_notifier,
            workload_class=workload_class,
        )
    finally:
        singleflight.release()


def _run_basket_locked(
    *,
    today: date | None = None,
    now: datetime | None = None,
    state_path: str | Path = DEFAULT_STATE_PATH,
    db_path: str | Path = DEFAULT_DB_PATH,
    usage_path: str | Path = API_USAGE_PATH,
    source_builder: Callable = build_default_sources,
    aggregator_factory: Callable = FlightAggregator,
    quota_guard_notifier=None,
    workload_class: str = RESEARCH_COHORT,
) -> dict:
    today = today or shanghai_today()
    now = now or datetime.now()
    round_id = make_round_id(now)
    round_archive_started = False
    round_log_root = Path(state_path).resolve().parent / "logs" / "rounds"
    try:
        start_round_log_archive(round_id, root_dir=round_log_root, now=now)
        round_archive_started = True
    except Exception as exc:
        safe_log(f"[轮档失败] round_id={round_id} 原因={type(exc).__name__}:{exc}")
    settings = load_collection_settings(CONFIG_PATH)
    state = load_or_create_state(state_path, today)
    research_configured = bool(settings.get("research_cohort_v2"))
    research_enabled = research_runtime_enabled(state, research_configured)
    if research_configured and not research_enabled:
        safe_log("[篮子跳过] 原因=研究采样运行态已停用,用户监控继续")
        if round_archive_started:
            end_round_log_archive(status="blocked")
        return {
            "status": "blocked",
            "reason": "research_runtime_disabled",
            "user_monitoring_enabled": True,
            "round_id": round_id,
            "queues": 0,
            "success": 0,
            "failed": 0,
            "written": 0,
        }
    if research_enabled:
        notifier = quota_guard_notifier or (
            lambda title, content: _default_quota_guard_notifier(
                state_path, title, content
            )
        )
        state, basket_requests, research_gate, quota = _prepare_research_basket(
            state,
            today=today,
            state_path=state_path,
            db_path=db_path,
            usage_path=usage_path,
            settings=settings,
            source_builder=source_builder,
            quota_guard_notifier=notifier,
        )
        if not research_gate["ready"]:
            if quota.get("guard_triggered"):
                _write_state(Path(state_path), state)
            safe_log("[篮子跳过] 原因=研究采样硬门未通过")
            if round_archive_started:
                try:
                    end_round_log_archive(status="blocked")
                except Exception as exc:
                    safe_log(
                        f"[轮档失败] round_id={round_id} "
                        f"关闭失败={type(exc).__name__}:{exc}"
                    )
            return {
                "status": "blocked",
                "reason": "research_hard_gate",
                "user_monitoring_enabled": True,
                "round_id": round_id,
                "queues": 0,
                "success": 0,
                "failed": 0,
                "written": 0,
            }
        _write_state(Path(state_path), state)
    else:
        if renew_expired_queues(state, today):
            _write_state(Path(state_path), state)
        basket_requests = _basket_requests(state)

    reset_request_cache()
    round_context_tokens = None
    start_request_cache_round(
        round_id,
        track_usage=True,
        usage_path=usage_path,
        quota_budgets=settings["source_quota_budget"],
        workload_class=workload_class,
        entrypoint="basket_canary" if workload_class == CANARY else "research_basket",
    )
    safe_log(f"[篮子轮次] round_id={round_id}")

    queues = 0
    success = 0
    failed = 0
    plan_active = False
    execution_report = None
    ledger_degraded = False
    research_progress_applied = False
    try:
        round_context_tokens = set_current_round(round_id, db_path=db_path)
        plan = build_collection_plan(
            subscriptions=[],
            basket_requests=basket_requests,
            source_builder=source_builder,
            freshness_hours=settings.get("freshness_hours", 6),
            fresh_scope=settings.get("sub_round_fresh_scope", "primary_only"),
        )
        activate_collection_plan(
            plan.request_keys,
            panel_only_keys=plan.panel_only_keys,
            freshness_hours=plan.freshness_hours,
            fresh_scope=plan.fresh_scope,
        )
        plan_active = True
        plan.log_summary(
            quota_budgets=settings["source_quota_budget"],
            quota_low_remaining_threshold=settings[
                "source_quota_low_remaining_threshold"
            ],
            usage_snapshot=usage_snapshot(load_usage(usage_path)),
            freshness_hours=settings.get("freshness_hours", 6),
            fresh_scope=settings.get("sub_round_fresh_scope", "primary_only"),
        )
        execution_report = plan.execute()
        ledger_degraded = bool(execution_report.ledger_degraded)

        if research_enabled and ledger_degraded:
            record_research_ledger_degraded(
                state,
                round_id=round_id,
                today=today,
                actual_requests=execution_report.actual_requests,
            )
            _write_state(Path(state_path), state)
            safe_log(
                f"[研究采样告警] round={round_id} collection ledger降级,"
                f"研究进度未推进 实际请求={execution_report.actual_requests}"
            )
        elif research_enabled:
            outcomes = apply_research_round_outcomes(
                state,
                requests=basket_requests,
                round_id=round_id,
                today=today,
                db_path=db_path,
            )
            research_progress_applied = True
            _write_state(Path(state_path), state)
            for outcome in outcomes:
                safe_log(
                    f"[研究采样结果] slot={outcome['slot']} state={outcome['state']}"
                )

        collection_routes = (
            (
                {
                    "route": "PVG->KIX",
                    "origin": "PVG",
                    "dest": "KIX",
                    "route_type": "international",
                    "sources": ("juhe",),
                },
                basket_requests,
            ),
        ) if research_enabled else tuple(
            (
                route,
                [
                    item
                    for item in basket_requests
                    if item["origin"] == route["origin"] and item["dest"] == route["dest"]
                ],
            )
            for route in BASKET_ROUTES
        )

        for route, route_requests in collection_routes:
            route_name = _route_name(route)
            try:
                aggregator = _build_route_aggregator(
                    route,
                    source_builder,
                    aggregator_factory,
                )
                route_error = None
            except Exception as exc:
                aggregator = None
                route_error = exc

            for item in route_requests:
                queues += 1
                queue_name = str(item.get("queue") or "")
                if not research_enabled and ":" in queue_name:
                    queue_name = queue_name.rsplit(":", 1)[-1]
                depart_date = str(item.get("depart_date") or "")
                try:
                    if route_error is not None:
                        raise route_error
                    result = aggregator.collect(
                        route["origin"],
                        route["dest"],
                        depart_date,
                        cabin_classes=["economy"],
                        route_type=route["route_type"],
                        passengers={"adult": 1, "child": 0, "elderly": 0, "infant": 0},
                        force_fresh=False,
                    )
                    if not result or not result.get("flights"):
                        raise RuntimeError("未返回有效航班")
                    success += 1
                except Exception as exc:
                    failed += 1
                    safe_log(
                        f"[篮子失败] route={route_name} queue={queue_name} "
                        f"date={depart_date} 原因={exc}"
                    )
    finally:
        if round_context_tokens is not None:
            reset_current_round(round_context_tokens)
        try:
            written = count_observations_for_round(round_id, db_path)
        except Exception as exc:
            written = 0
            safe_log(f"[篮子失败] route=summary 原因=观测计数失败:{exc}")
        print_request_cache_stats()
        if plan_active:
            deactivate_collection_plan()

        round_status = "ok" if failed == 0 and not ledger_degraded else "partial"
        safe_log(f"[篮子完成] 队列={queues} 成功={success} 失败={failed} 总写入={written}")
        log_retention_dry_run(BASE_DIR, config_path=CONFIG_PATH)
        if round_archive_started:
            try:
                end_round_log_archive(status=round_status)
            except Exception as exc:
                safe_log(f"[轮档失败] round_id={round_id} 关闭失败={type(exc).__name__}:{exc}")

    summary = {
        "round_id": round_id,
        "queues": queues,
        "success": success,
        "failed": failed,
        "written": written,
    }
    if research_enabled:
        summary.update(
            {
                "status": round_status,
                "ledger_degraded": ledger_degraded,
                "research_progress_applied": research_progress_applied,
                "plan_actual_requests": (
                    int(execution_report.actual_requests)
                    if execution_report is not None
                    else 0
                ),
            }
        )
    return summary


def _load_environment() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise RuntimeError("缺少python-dotenv,无法读取数据源配置") from exc
    load_dotenv(BASE_DIR / ".env")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canary", action="store_true", help="将本轮记为canary工作负载")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    configure_stdio_utf8()
    try:
        _load_environment()
        summary = run_basket(workload_class=CANARY if args.canary else RESEARCH_COHORT)
    except Exception as exc:
        safe_log(f"[篮子失败] route=bootstrap 原因={exc}")
        return 1
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
