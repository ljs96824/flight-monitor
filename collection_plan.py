"""先计划后执行的采集请求调度器。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from config_loader import (
    RUNTIME_CONFIG_PATH,
    load_merged_config,
    load_standalone_config,
)
from log_utils import safe_log
from quota_policy import MONTHLY, metrics as quota_metrics
from request_cache import (
    cache_key,
    cached_fetch,
    get_request_cache_stats,
    panel_reuse_result,
    record_planned_source_skip,
)
from source_profiles import source_supports_cabin
from subscription_identity import subscription_id
from subscription_preflight import shanghai_today


RESEARCH_BASKET_STRATEGIES = frozenset({"cohort_v2", "legacy"})


def _research_basket_config(payload: dict) -> dict:
    explicit = (
        "RESEARCH_BASKET_ENABLED" in payload
        or "RESEARCH_BASKET_STRATEGY" in payload
    )
    migrated = not explicit and "RESEARCH_COHORT_V2" in payload
    if explicit:
        enabled = _as_bool(payload.get("RESEARCH_BASKET_ENABLED"))
        strategy = str(
            payload.get("RESEARCH_BASKET_STRATEGY") or "cohort_v2"
        ).strip().lower()
    elif migrated:
        enabled = _as_bool(payload.get("RESEARCH_COHORT_V2"))
        strategy = "cohort_v2"
        safe_log(
            f"[配置迁移] RESEARCH_COHORT_V2={str(enabled).lower()} "
            "映射为 RESEARCH_BASKET_ENABLED+RESEARCH_BASKET_STRATEGY=cohort_v2"
        )
    else:
        enabled = False
        strategy = "cohort_v2"
    if strategy not in RESEARCH_BASKET_STRATEGIES:
        allowed = ",".join(sorted(RESEARCH_BASKET_STRATEGIES))
        raise ValueError(
            f"RESEARCH_BASKET_STRATEGY={strategy!r} 无效,允许值={allowed}"
        )
    return {
        "research_basket_enabled": enabled,
        "research_basket_strategy": strategy,
        "research_basket_migrated_from_legacy": migrated,
        # 一期兼容只供尚未迁移的只读消费方；执行入口不再读取该别名。
        "research_cohort_v2": enabled and strategy == "cohort_v2",
    }


@dataclass
class PlannedRequest:
    source: object
    origin: str
    dest: str
    date_str: str
    passengers: dict | None = None
    cabin_class: str = "economy"
    force_fresh: bool = False
    panel_only: bool = False
    persist: bool = True
    ttl_seconds: int = 15 * 60
    conditional: str | None = None
    group: str | None = None
    groups: set[str] = field(default_factory=set)
    consumers: set[str] = field(default_factory=set)
    reasons: set[str] = field(default_factory=set)
    route_type: str | None = None
    cohort_id: str | None = None
    sample_role: str = "legacy"

    @property
    def key(self) -> tuple:
        return cache_key(
            self.source,
            self.origin,
            self.dest,
            self.date_str,
            self.passengers,
            self.cabin_class,
        )


@dataclass(frozen=True)
class RequestOutcome:
    """Per-request evidence; result is a borrowed read-only view.

    Frozen fields prevent reassignment but do not freeze nested payloads. Consumers
    must copy result before mutation.
    """

    request_key: tuple
    source: str
    origin: str
    destination: str
    depart_date: str
    cabin_class: str
    execution_status: str
    cache_status: str
    reuse_kind: str | None
    skip_reason_code: str | None
    error_type: str | None
    error_code: str | None
    quota_status: str | None
    raw_result_count: int
    valid_result_count: int
    route_type: str
    cohort_id: str | None
    sample_role: str
    consumers: tuple[str, ...]
    groups: tuple[str, ...]
    reasons: tuple[str, ...]
    # Borrowed read-only view of CollectionPlan._results. Copy before mutation.
    result: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class PlanExecutionReport:
    actual_requests: int
    retries: int
    cache_hits: int
    panel_reused: int
    source_skips: int
    conditional_skipped: int
    ledger_degraded: bool = False
    outcomes: tuple[RequestOutcome, ...] = ()


_SAMPLE_ROLE_PRIORITY = {
    "legacy": 0,
    "cross_sectional_probe": 1,
    "user_monitor": 2,
    "trajectory_anchor": 3,
}


def _positive_flights(result) -> list[dict]:
    if not isinstance(result, dict):
        return []
    flights = []
    for item in result.get("flights") or []:
        if not isinstance(item, dict):
            continue
        try:
            if float(item.get("price")) > 0:
                flights.append(item)
        except (TypeError, ValueError):
            continue
    return flights


def _build_request_outcome(
    request: PlannedRequest,
    result,
    *,
    cache_status,
    reuse_kind=None,
    skip_reason_code=None,
) -> RequestOutcome:
    from collection_ledger import classify_collection_result

    classified = classify_collection_result(
        result,
        cache_status=cache_status,
        reuse_kind=reuse_kind,
        skip_reason_code=skip_reason_code,
    )
    return RequestOutcome(
        request_key=request.key,
        source=str(
            getattr(request.source, "name", type(request.source).__name__)
        ).lower(),
        origin=request.origin,
        destination=request.dest,
        depart_date=request.date_str,
        cabin_class=request.cabin_class,
        execution_status=classified["execution_status"],
        cache_status=classified["cache_status"],
        reuse_kind=classified["reuse_kind"],
        skip_reason_code=classified["skip_reason_code"],
        error_type=classified["error_type"],
        error_code=classified["error_code"],
        quota_status=classified["quota_status"],
        raw_result_count=classified["raw_result_count"],
        valid_result_count=classified["valid_result_count"],
        route_type=str(
            request.route_type
            or getattr(request.source, "route_type", None)
            or "unknown"
        ),
        cohort_id=request.cohort_id,
        sample_role=request.sample_role,
        consumers=tuple(sorted(request.consumers)),
        groups=tuple(sorted(request.groups)),
        reasons=tuple(sorted(request.reasons)),
        result=result,
    )


class CollectionPlan:
    def __init__(
        self,
        *,
        subscription_count: int = 0,
        basket_date_count: int = 0,
        freshness_hours: float = 6,
        fresh_scope: str = "primary_only",
    ):
        self.subscription_count = int(subscription_count or 0)
        self.basket_date_count = int(basket_date_count or 0)
        self.freshness_hours = max(0.0, float(freshness_hours or 0))
        self.fresh_scope = (
            "all" if str(fresh_scope or "").strip().lower() == "all" else "primary_only"
        )
        self.expanded_total = 0
        self._requests: dict[tuple, PlannedRequest] = {}
        self._results: dict[tuple, dict] = {}
        self._quota_protected_keys: set[tuple] = set()
        self._quota_protection_reasons: dict[tuple, str] = {}

    @property
    def unique_count(self) -> int:
        return len(self._requests)

    @property
    def reuse_saved(self) -> int:
        return max(0, self.expanded_total - self.unique_count)

    @property
    def request_keys(self) -> set[tuple]:
        return set(self._requests)

    @property
    def panel_only_keys(self) -> set[tuple]:
        return {
            key
            for key, request in self._requests.items()
            if request.panel_only and not request.force_fresh
        }

    @property
    def source_counts(self) -> dict[str, int]:
        return dict(Counter(key[0] for key in self._requests))

    @property
    def cabin_counts(self) -> dict[str, int]:
        return dict(
            Counter(
                str(request.cabin_class or "economy")
                for request in self._requests.values()
                if "行李退改补充" not in request.reasons
            )
        )

    @property
    def enrichment_cabin_counts(self) -> dict[str, int]:
        return dict(
            Counter(
                str(request.cabin_class or "economy")
                for request in self._requests.values()
                if "行李退改补充" in request.reasons
            )
        )

    @property
    def conditional_count(self) -> int:
        return sum(1 for request in self._requests.values() if request.conditional)

    def add_request(
        self,
        source,
        origin: str,
        dest: str,
        date_str: str,
        passengers: dict | None = None,
        cabin_class: str = "economy",
        *,
        force_fresh: bool = False,
        panel_only: bool = False,
        persist: bool = True,
        ttl_seconds: int = 15 * 60,
        conditional: str | None = None,
        group: str | None = None,
        consumer: str | None = None,
        reason: str | None = None,
        route_type: str | None = None,
        cohort_id: str | None = None,
        sample_role: str = "legacy",
    ) -> PlannedRequest:
        self.expanded_total += 1
        candidate = PlannedRequest(
            source=source,
            origin=str(origin).upper(),
            dest=str(dest).upper(),
            date_str=str(date_str),
            passengers=passengers,
            cabin_class=str(cabin_class or "economy"),
            force_fresh=bool(force_fresh),
            panel_only=bool(panel_only),
            persist=bool(persist),
            ttl_seconds=max(0, int(ttl_seconds)),
            conditional=conditional,
            group=group,
            route_type=str(route_type) if route_type else None,
            cohort_id=str(cohort_id) if cohort_id else None,
            sample_role=str(sample_role or "legacy"),
        )
        if candidate.sample_role not in _SAMPLE_ROLE_PRIORITY:
            raise ValueError(f"unknown sample_role: {candidate.sample_role}")
        if group:
            candidate.groups.add(str(group))
        existing = self._requests.get(candidate.key)
        if existing is None:
            existing = candidate
            self._requests[candidate.key] = existing
        else:
            existing.force_fresh = existing.force_fresh or candidate.force_fresh
            # 同一请求只要被主日期或篮子消费，就不能被弹性日期降成只读。
            existing.panel_only = existing.panel_only and candidate.panel_only
            existing.persist = existing.persist or candidate.persist
            existing.ttl_seconds = max(existing.ttl_seconds, candidate.ttl_seconds)
            if existing.conditional and not candidate.conditional:
                existing.conditional = None
            existing.group = existing.group or candidate.group
            existing.groups.update(candidate.groups)
            existing.route_type = existing.route_type or candidate.route_type
            if _SAMPLE_ROLE_PRIORITY[candidate.sample_role] > _SAMPLE_ROLE_PRIORITY[
                existing.sample_role
            ]:
                existing.sample_role = candidate.sample_role
                existing.cohort_id = candidate.cohort_id
            elif not existing.cohort_id:
                existing.cohort_id = candidate.cohort_id
        if consumer:
            existing.consumers.add(str(consumer))
        if reason:
            existing.reasons.add(str(reason))
        return existing

    def _panel_reuse_keys(self) -> set[tuple]:
        reusable = set()
        for key, request in self._requests.items():
            if request.force_fresh or key[0] not in {"juhe", "hasdata", "serpapi"}:
                continue
            if panel_reuse_result(
                request.source,
                request.origin,
                request.dest,
                request.date_str,
                request.passengers,
                request.cabin_class,
                freshness_hours=self.freshness_hours,
            ) is not None:
                reusable.add(key)
        return reusable

    def log_summary(
        self,
        *,
        quota_budgets: dict[str, object] | None = None,
        quota_low_remaining_threshold: int = 50,
        usage_snapshot: dict | None = None,
        freshness_hours: float | None = None,
        fresh_scope: str | None = None,
    ) -> None:
        counts = self.source_counts
        cabin_counts = self.cabin_counts
        enrichment_cabin_counts = self.enrichment_cabin_counts
        panel_reuse_keys = self._panel_reuse_keys()
        panel_reuse_by_source = Counter(key[0] for key in panel_reuse_keys)
        panel_only_missing_by_source = Counter(
            key[0]
            for key in self.panel_only_keys
            if key not in panel_reuse_keys
        )
        safe_log(
            "[采集计划] "
            f"唯一请求={self.unique_count} "
            f"juhe={counts.get('juhe', 0)} "
            f"hasdata={counts.get('hasdata', 0)} "
            f"serpapi={counts.get('serpapi', 0)} "
            f"duffel={counts.get('duffel', 0)} "
            f"分舱定价=economy:{cabin_counts.get('economy', 0)},"
            f"business:{cabin_counts.get('business', 0)} "
            f"富化=economy:{enrichment_cabin_counts.get('economy', 0)},"
            f"business:{enrichment_cabin_counts.get('business', 0)} "
            f"订阅数={self.subscription_count} 篮子日期={self.basket_date_count} "
            f"复用节省={self.reuse_saved} 条件项={self.conditional_count} "
            f"预计面板复用={len(panel_reuse_keys)}"
        )
        snapshot = usage_snapshot or {"today": {}, "month": {}, "cumulative": {}}
        self._quota_protected_keys.clear()
        self._quota_protection_reasons.clear()
        for source, raw_budget in (quota_budgets or {}).items():
            planned_keys = {
                key
                for key in self._requests
                if key[0] == source
                and key not in panel_reuse_keys
                and key not in self.panel_only_keys
            }
            planned = len(planned_keys)
            quota = quota_metrics(raw_budget, snapshot, source)
            remaining_after = quota["remaining"] - planned
            if quota["kind"] == MONTHLY:
                usable_limit = max(0, quota["total_limit"] - quota["reserve"])
                usable_after = usable_limit - quota["used"] - planned
                if quota["used"] + planned > usable_limit:
                    reason = (
                        f"月度配额保护(本月已用={quota['used']},计划={planned},"
                        f"可用上限={usable_limit})"
                    )
                    self._quota_protected_keys.update(planned_keys)
                    self._quota_protection_reasons.update(
                        {key: reason for key in planned_keys}
                    )
                    safe_log(
                        f"[配额保护] 源={source} 本月已用={quota['used']} "
                        f"计划消耗={planned} 月预算={quota['total_limit']} "
                        f"预留={quota['reserve']} 结果=计划项转源级跳过"
                    )
                elif usable_after < int(quota_low_remaining_threshold):
                    safe_log(
                        f"⚠ [采集计划] 源={source} 计划消耗={planned} "
                        f"本月已用={quota['used']} 月预算={quota['total_limit']} "
                        f"预留={quota['reserve']} 预计可用余量={usable_after}"
                    )
                continue

            if remaining_after < 0:
                safe_log(
                    f"⚠ [采集计划] 源={source} 计划消耗={planned} "
                    f"本epoch已用={quota['used']} 预算={quota['total_limit']} "
                    f"预计超出={-remaining_after}"
                )
            elif remaining_after < int(quota_low_remaining_threshold):
                safe_log(
                    f"⚠ [采集计划] 源={source} 计划消耗={planned} "
                    f"本epoch已用={quota['used']} 预算={quota['total_limit']} "
                    f"预计余量={remaining_after} "
                    f"余量低于阈值{int(quota_low_remaining_threshold)}"
                )

    def execute(self) -> PlanExecutionReport:
        from collection_ledger import CollectionLedgerSession
        from observations_store import get_current_round

        before = get_request_cache_stats()
        conditional_skipped = 0
        outcomes_by_key: dict[tuple, RequestOutcome] = {}

        ordered = list(self._requests.values())
        round_id, db_path = get_current_round()
        ledger = (
            CollectionLedgerSession(round_id=round_id, db_path=db_path)
            if round_id
            else None
        )
        if ledger:
            ledger.plan(ordered)

        def execute_request(request):
            if request.key in self._quota_protected_keys:
                result = record_planned_source_skip(
                    request.source,
                    request.origin,
                    request.dest,
                    request.date_str,
                    request.passengers,
                    request.cabin_class,
                    reason=self._quota_protection_reasons[request.key],
                )
                if ledger:
                    ledger.finish(
                        request,
                        result,
                        cache_status="skipped",
                        skip_reason_code="quota",
                    )
                outcomes_by_key[request.key] = _build_request_outcome(
                    request,
                    result,
                    cache_status="skipped",
                    skip_reason_code="quota",
                )
                return result
            if ledger:
                ledger.start(request)
            try:
                result, cache_status, reuse_kind = cached_fetch(
                    request.source,
                    request.origin,
                    request.dest,
                    request.date_str,
                    request.passengers,
                    request.cabin_class,
                    persist=request.persist,
                    ttl_seconds=request.ttl_seconds,
                    force_fresh=request.force_fresh,
                    include_cache_status=True,
                    include_cache_details=True,
                    request_reason="/".join(sorted(request.reasons)),
                    panel_only=request.panel_only,
                )
            except Exception as exc:
                if ledger:
                    ledger.fail_exception(request, exc)
                raise
            if ledger:
                ledger.finish(
                    request,
                    result,
                    cache_status=cache_status,
                    reuse_kind=reuse_kind,
                )
            outcomes_by_key[request.key] = _build_request_outcome(
                request,
                result,
                cache_status=cache_status,
                reuse_kind=reuse_kind,
            )
            return result

        try:
            for request in ordered:
                if request.conditional:
                    continue
                self._results[request.key] = execute_request(request)

            groups_with_candidates = {
                group
                for request in ordered
                if not request.conditional
                and _positive_flights(self._results.get(request.key))
                for group in request.groups
            }
            for request in ordered:
                if not request.conditional:
                    continue
                if (
                    request.conditional == "search_has_candidates"
                    and not (request.groups & groups_with_candidates)
                ):
                    conditional_skipped += 1
                    prefix = (
                        "[enrichment跳过]"
                        if "行李退改补充" in request.reasons
                        else "[条件采集跳过]"
                    )
                    reason_label = "/".join(sorted(request.reasons)) or request.conditional
                    safe_log(
                        f"{prefix} 原因=无列表候选 类型={reason_label} "
                        f"航线={request.origin}->{request.dest} 日期={request.date_str}"
                    )
                    result = {
                        "flights": [],
                        "source_status": "skipped_conditional",
                        "skipped_reason": "无列表候选",
                    }
                    if ledger:
                        ledger.finish(
                            request,
                            result,
                            cache_status="skipped",
                            skip_reason_code="conditional",
                        )
                    self._results[request.key] = result
                    outcomes_by_key[request.key] = _build_request_outcome(
                        request,
                        result,
                        cache_status="skipped",
                        skip_reason_code="conditional",
                    )
                    continue
                self._results[request.key] = execute_request(request)
        finally:
            if ledger:
                ledger.finalize()

        after = get_request_cache_stats()
        ordered_outcomes = tuple(
            outcomes_by_key[request.key]
            for request in ordered
        )
        report = PlanExecutionReport(
            actual_requests=int(after.get("actual", 0)) - int(before.get("actual", 0)),
            retries=int(after.get("retries", 0)) - int(before.get("retries", 0)),
            cache_hits=int(after.get("hits", 0)) - int(before.get("hits", 0)),
            panel_reused=int(after.get("panel_reused", 0))
            - int(before.get("panel_reused", 0)),
            source_skips=int(after.get("skipped", 0)) - int(before.get("skipped", 0)),
            conditional_skipped=conditional_skipped,
            ledger_degraded=bool(ledger and ledger.degraded),
            outcomes=ordered_outcomes,
        )
        accounted = (
            report.actual_requests
            - report.retries
            + report.cache_hits
            + report.panel_reused
            + report.source_skips
            + report.conditional_skipped
        )
        safe_log(
            f"[采集执行] 计划唯一={self.unique_count} 实际请求={report.actual_requests} "
            f"重试={report.retries} 缓存命中={report.cache_hits} "
            f"面板复用={report.panel_reused} "
            f"源级跳过={report.source_skips} "
            f"条件跳过={report.conditional_skipped} "
            f"计划恒等式={accounted == self.unique_count} "
            f"台账降级={report.ledger_degraded}"
        )
        return report


def _value(subscription: dict, key: str):
    value = subscription.get(key)
    if value not in (None, ""):
        return value
    for section_name in ("preferences", "hard_constraints", "constraints", "basic"):
        section = subscription.get(section_name)
        if isinstance(section, dict) and section.get(key) not in (None, ""):
            return section.get(key)
    return None


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _airports(subscription: dict, active_key: str, all_key: str, fallback_key: str) -> list[str]:
    raw = subscription.get(active_key) or subscription.get(all_key) or _value(subscription, fallback_key)
    if isinstance(raw, str):
        raw = [item.strip() for item in raw.replace("、", ",").split(",") if item.strip()]
    return [str(item).strip().upper() for item in (raw or []) if str(item).strip()]


def _cabin_classes(subscription: dict) -> list[str]:
    raw = _value(subscription, "cabin_classes") or ["economy"]
    if isinstance(raw, str):
        raw = [raw]
    return list(dict.fromkeys(str(item or "economy") for item in raw)) or ["economy"]


def _passengers(subscription: dict) -> dict:
    raw = _value(subscription, "passengers")
    return dict(raw) if isinstance(raw, dict) else {"adult": 1}


def _flex_dates(center_text: str, flexibility) -> list[str]:
    try:
        center = date.fromisoformat(str(center_text))
        days = max(0, int(flexibility or 0))
    except (TypeError, ValueError):
        return []
    offsets = set()
    for stage in (1, 3, 7):
        if days >= stage:
            offsets.update(range(-stage, stage + 1))
    offsets.discard(0)
    today = shanghai_today()
    return [
        (center + timedelta(days=offset)).isoformat()
        for offset in sorted(offsets)
        if center + timedelta(days=offset) >= today
    ]


def _calendar_dates(target_date: str, origin: str, dest: str, cache_hours: int = 6) -> list[str]:
    from price_calendar import (
        _query_dates,
        calendar_record_is_eligible,
        load_calendar,
    )

    cached_dates = (
        load_calendar(
            f"{origin}-{dest}",
            legacy_stale_hours=cache_hours,
        ).get("dates")
        or {}
    )
    result = []
    for item in _query_dates(target_date):
        date_str = item.isoformat()
        cached = cached_dates.get(date_str)
        if isinstance(cached, dict) and calendar_record_is_eligible(
            cached,
            legacy_stale_hours=cache_hours,
        ):
            continue
        result.append(date_str)
    return result


def _source_name(source) -> str:
    return str(getattr(source, "name", type(source).__name__)).lower()


def _subscription_consumer_ref(subscription: dict, index: int) -> str:
    stable_id = subscription_id(subscription)
    if stable_id:
        return f"subscription:{stable_id}"
    legacy_index = subscription.get("_index")
    if legacy_index in (None, ""):
        legacy_index = index
    return f"subscription-legacy:{legacy_index}"


def basket_consumer_ref(item: dict, index: int) -> str:
    cohort_id = str(item.get("cohort_id") or "").strip()
    if cohort_id:
        return f"research:{cohort_id}"
    return f"basket:legacy-{index}"


def _add_direction_requests(
    plan: CollectionPlan,
    *,
    search_sources: list,
    enrichment_sources: list,
    origin: str,
    dest: str,
    depart_date: str,
    cabins: list[str],
    passengers: dict,
    consumer: str,
    group_prefix: str,
    flex_dates: list[str],
    include_calendar: bool,
    fresh_scope: str,
    route_type: str | None,
) -> None:
    for cabin in cabins:
        group = f"{group_prefix}:{origin}:{dest}:{depart_date}:{cabin}"
        cabin_search_sources = [
            source for source in search_sources if source_supports_cabin(source, cabin)
        ]
        cabin_enrichment_sources = [
            source for source in enrichment_sources if source_supports_cabin(source, cabin)
        ]
        for source in cabin_search_sources:
            plan.add_request(
                source,
                origin,
                dest,
                depart_date,
                passengers,
                cabin,
                group=group,
                consumer=consumer,
                reason="主行程",
                route_type=route_type,
                sample_role="user_monitor",
            )
        for source in cabin_enrichment_sources:
            plan.add_request(
                source,
                origin,
                dest,
                depart_date,
                {"adult": 1},
                cabin,
                conditional="search_has_candidates",
                group=group,
                consumer=consumer,
                reason="行李退改补充",
                route_type=route_type,
                sample_role="user_monitor",
            )
        # 月度估算：篮子0次商务请求；每个含商务席位的往返订阅约2次/日，
        # SerpAPI 250次/月由既有reserve门继续保护。
        # 商务舱是稀缺月配额：v1保留主日期规则富化，不进弹性与日历。
        if cabin == "business":
            continue
        for flex_date in flex_dates:
            for source in cabin_search_sources:
                plan.add_request(
                    source,
                    origin,
                    dest,
                    flex_date,
                    passengers,
                    cabin,
                    conditional="search_has_candidates",
                    panel_only=fresh_scope == "primary_only",
                    group=group,
                    consumer=consumer,
                    reason="弹性日期",
                    route_type=route_type,
                    sample_role="user_monitor",
                )
        if include_calendar and cabin_search_sources:
            calendar_source = cabin_search_sources[0]
            for calendar_date in _calendar_dates(depart_date, origin, dest):
                plan.add_request(
                    calendar_source,
                    origin,
                    dest,
                    calendar_date,
                    passengers,
                    cabin,
                    conditional="search_has_candidates",
                    panel_only=fresh_scope == "primary_only",
                    ttl_seconds=6 * 60 * 60,
                    group=group,
                    consumer=consumer,
                    reason="低价日历",
                    route_type=route_type,
                    sample_role="user_monitor",
                )


def build_collection_plan(
    *,
    subscriptions: list[dict] | None = None,
    basket_requests: list[dict] | None = None,
    source_builder=None,
    include_calendars: bool = True,
    freshness_hours: float = 6,
    fresh_scope: str = "primary_only",
) -> CollectionPlan:
    if source_builder is None:
        from sources.aggregator import build_default_sources

        source_builder = build_default_sources

    subscriptions = list(subscriptions or [])
    basket_requests = list(basket_requests or [])
    plan = CollectionPlan(
        subscription_count=len(subscriptions),
        basket_date_count=len(basket_requests),
        freshness_hours=freshness_hours,
        fresh_scope=fresh_scope,
    )

    for index, subscription in enumerate(subscriptions):
        origins = _airports(subscription, "origin_airports_active", "origin_airports", "origin")
        dests = _airports(
            subscription,
            "destination_airports_active",
            "destination_airports",
            "destination",
        )
        depart_date = str(_value(subscription, "depart_date") or "")
        if not origins or not dests or not depart_date:
            continue
        origin, dest = origins[0], dests[0]
        route_type = str(_value(subscription, "route_type") or "") or None
        try:
            search_sources, enrichment_sources = source_builder(
                origin,
                dest,
                route_type=route_type,
            )
        except Exception as exc:
            consumer = str(subscription.get("_index", subscription.get("id", index)))
            safe_log(
                f"[采集计划跳过] 订阅={consumer} 航线={origin}->{dest} "
                f"原因={type(exc).__name__}: {exc}"
            )
            continue
        cabins = _cabin_classes(subscription)
        passengers = _passengers(subscription)
        consumer = _subscription_consumer_ref(subscription, index)
        domestic_calendar = bool(include_calendars and route_type == "domestic")
        _add_direction_requests(
            plan,
            search_sources=list(search_sources),
            enrichment_sources=list(enrichment_sources),
            origin=origin,
            dest=dest,
            depart_date=depart_date,
            cabins=cabins,
            passengers=passengers,
            consumer=consumer,
            group_prefix=f"sub:{consumer}:outbound",
            flex_dates=_flex_dates(depart_date, _value(subscription, "date_flexibility")),
            include_calendar=domestic_calendar,
            fresh_scope=plan.fresh_scope,
            route_type=route_type,
        )

        same_day = _as_bool(_value(subscription, "same_day_round_trip"))
        round_trip = _as_bool(_value(subscription, "round_trip")) or same_day
        return_date = str(_value(subscription, "return_date") or (depart_date if same_day else ""))
        if round_trip and return_date:
            _add_direction_requests(
                plan,
                search_sources=list(search_sources),
                enrichment_sources=list(enrichment_sources),
                origin=dest,
                dest=origin,
                depart_date=return_date,
                cabins=cabins,
                passengers=passengers,
                consumer=consumer,
                group_prefix=f"sub:{consumer}:return",
                flex_dates=_flex_dates(
                    return_date,
                    _value(subscription, "return_date_flexibility"),
                ),
                include_calendar=domestic_calendar,
                fresh_scope=plan.fresh_scope,
                route_type=route_type,
            )

    for index, item in enumerate(basket_requests):
        origin = str(item.get("origin") or "").upper()
        dest = str(item.get("dest") or item.get("destination") or "").upper()
        depart_date = str(item.get("depart_date") or item.get("date") or "")
        if not origin or not dest or not depart_date:
            continue
        route_type = item.get("route_type")
        try:
            search_sources, _ = source_builder(origin, dest, route_type=route_type)
        except Exception as exc:
            safe_log(
                f"[采集计划跳过] 篮子={item.get('queue', index)} 航线={origin}->{dest} "
                f"原因={type(exc).__name__}: {exc}"
            )
            continue
        required = {str(name).lower() for name in (item.get("sources") or [])}
        cabin_class = str(item.get("cabin_class") or "economy")
        # 固定篮子只维护经济舱市场曲线；商务舱由启用该舱位的订阅主日期采集。
        if cabin_class == "business":
            safe_log(
                f"[采集计划跳过] 篮子={item.get('queue', index)} 航线={origin}->{dest} "
                "原因=固定篮子不采商务舱"
            )
            continue
        search_sources = [
            source
            for source in search_sources
            if source_supports_cabin(source, cabin_class)
        ]
        if required:
            search_sources = [source for source in search_sources if _source_name(source) in required]
        for source in search_sources:
            consumer = str(
                item.get("_consumer_ref") or basket_consumer_ref(item, index)
            )
            plan.add_request(
                source,
                origin,
                dest,
                depart_date,
                {"adult": 1},
                cabin_class,
                force_fresh=True,
                consumer=consumer,
                reason="固定篮子",
                route_type=str(route_type or "unknown"),
                cohort_id=item.get("cohort_id"),
                sample_role=str(item.get("sample_role") or "legacy"),
            )
    return plan


def load_collection_settings(
    path: str | Path,
    *,
    runtime_path: str | Path | None = None,
    require_runtime: bool = False,
) -> dict:
    if require_runtime or runtime_path is not None:
        payload = load_merged_config(
            path,
            runtime_path or RUNTIME_CONFIG_PATH,
        )
    else:
        # Explicit one-file fixtures remain supported; production callers always
        # pass require_runtime=True and therefore cannot fall back to an empty budget.
        payload = load_standalone_config(path)
    research_basket = _research_basket_config(payload)
    return {
        "source_quota_budget": dict(payload.get("source_quota_budget") or {}),
        "source_quota_low_remaining_threshold": int(
            payload.get("source_quota_low_remaining_threshold") or 50
        ),
        "freshness_hours": max(0.0, float(payload.get("FRESHNESS_HOURS") or 6)),
        "sub_round_fresh_scope": (
            "all"
            if str(payload.get("SUB_ROUND_FRESH_SCOPE") or "").strip().lower() == "all"
            else "primary_only"
        ),
        "serpapi_economy_cross_check": _as_bool(
            payload.get("SERPAPI_ECONOMY_CROSS_CHECK")
        ),
        **research_basket,
        "research_cohort_v2_gates": dict(
            payload.get("research_cohort_v2_gates") or {}
        ),
        "paused_research_routes": list(
            payload.get("paused_research_routes") or []
        ),
    }
