"""先计划后执行的采集请求调度器。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from log_utils import safe_log
from request_cache import cache_key, cached_fetch, get_request_cache_stats


@dataclass
class PlannedRequest:
    source: object
    origin: str
    dest: str
    date_str: str
    passengers: dict | None = None
    cabin_class: str = "economy"
    force_fresh: bool = False
    persist: bool = True
    ttl_seconds: int = 15 * 60
    conditional: str | None = None
    group: str | None = None
    groups: set[str] = field(default_factory=set)
    consumers: set[str] = field(default_factory=set)
    reasons: set[str] = field(default_factory=set)

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
class PlanExecutionReport:
    actual_requests: int
    cache_hits: int
    source_skips: int
    conditional_skipped: int


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


class CollectionPlan:
    def __init__(self, *, subscription_count: int = 0, basket_date_count: int = 0):
        self.subscription_count = int(subscription_count or 0)
        self.basket_date_count = int(basket_date_count or 0)
        self.expanded_total = 0
        self._requests: dict[tuple, PlannedRequest] = {}
        self._results: dict[tuple, dict] = {}

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
    def source_counts(self) -> dict[str, int]:
        return dict(Counter(key[0] for key in self._requests))

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
        persist: bool = True,
        ttl_seconds: int = 15 * 60,
        conditional: str | None = None,
        group: str | None = None,
        consumer: str | None = None,
        reason: str | None = None,
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
            persist=bool(persist),
            ttl_seconds=max(0, int(ttl_seconds)),
            conditional=conditional,
            group=group,
        )
        if group:
            candidate.groups.add(str(group))
        existing = self._requests.get(candidate.key)
        if existing is None:
            existing = candidate
            self._requests[candidate.key] = existing
        else:
            existing.force_fresh = existing.force_fresh or candidate.force_fresh
            existing.persist = existing.persist or candidate.persist
            existing.ttl_seconds = max(existing.ttl_seconds, candidate.ttl_seconds)
            if existing.conditional and not candidate.conditional:
                existing.conditional = None
            existing.group = existing.group or candidate.group
            existing.groups.update(candidate.groups)
        if consumer:
            existing.consumers.add(str(consumer))
        if reason:
            existing.reasons.add(str(reason))
        return existing

    def log_summary(
        self,
        *,
        quota_budgets: dict[str, int] | None = None,
        quota_low_remaining_threshold: int = 50,
        usage_snapshot: dict | None = None,
    ) -> None:
        counts = self.source_counts
        safe_log(
            "[采集计划] "
            f"唯一请求={self.unique_count} "
            f"juhe={counts.get('juhe', 0)} "
            f"hasdata={counts.get('hasdata', 0)} "
            f"duffel={counts.get('duffel', 0)} "
            f"订阅数={self.subscription_count} 篮子日期={self.basket_date_count} "
            f"复用节省={self.reuse_saved} 条件项={self.conditional_count}"
        )
        snapshot = usage_snapshot or {"today": {}, "cumulative": {}}
        for source, raw_budget in (quota_budgets or {}).items():
            budget = int(raw_budget or 0)
            cumulative = int((snapshot.get("cumulative") or {}).get(source, 0) or 0)
            planned = int(counts.get(source, 0) or 0)
            remaining_after = budget - cumulative - planned
            if cumulative + planned > budget:
                safe_log(
                    f"⚠ [采集计划] 源={source} 计划消耗={planned} 累计已用={cumulative} "
                    f"预算={budget} 预计超出={-remaining_after}"
                )
            elif remaining_after < int(quota_low_remaining_threshold):
                safe_log(
                    f"⚠ [采集计划] 源={source} 计划消耗={planned} 累计已用={cumulative} "
                    f"预算={budget} 预计余量={remaining_after} "
                    f"余量低于阈值{int(quota_low_remaining_threshold)}"
                )

    def execute(self) -> PlanExecutionReport:
        before = get_request_cache_stats()
        conditional_skipped = 0

        ordered = list(self._requests.values())
        for request in ordered:
            if request.conditional:
                continue
            self._results[request.key] = cached_fetch(
                request.source,
                request.origin,
                request.dest,
                request.date_str,
                request.passengers,
                request.cabin_class,
                persist=request.persist,
                ttl_seconds=request.ttl_seconds,
                force_fresh=request.force_fresh,
            )

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
                continue
            self._results[request.key] = cached_fetch(
                request.source,
                request.origin,
                request.dest,
                request.date_str,
                request.passengers,
                request.cabin_class,
                persist=request.persist,
                ttl_seconds=request.ttl_seconds,
                force_fresh=request.force_fresh,
            )

        after = get_request_cache_stats()
        report = PlanExecutionReport(
            actual_requests=int(after.get("actual", 0)) - int(before.get("actual", 0)),
            cache_hits=int(after.get("hits", 0)) - int(before.get("hits", 0)),
            source_skips=int(after.get("skipped", 0)) - int(before.get("skipped", 0)),
            conditional_skipped=conditional_skipped,
        )
        accounted = (
            report.actual_requests
            + report.cache_hits
            + report.source_skips
            + report.conditional_skipped
        )
        safe_log(
            f"[采集执行] 计划唯一={self.unique_count} 实际请求={report.actual_requests} "
            f"缓存命中={report.cache_hits} 源级跳过={report.source_skips} "
            f"条件跳过={report.conditional_skipped} "
            f"计划恒等式={accounted == self.unique_count}"
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
    today = date.today()
    return [
        (center + timedelta(days=offset)).isoformat()
        for offset in sorted(offsets)
        if center + timedelta(days=offset) >= today
    ]


def _calendar_dates(target_date: str, origin: str, dest: str, cache_hours: int = 6) -> list[str]:
    from price_calendar import _query_dates, is_stale, load_calendar

    cached_dates = (load_calendar(f"{origin}-{dest}").get("dates") or {})
    result = []
    for item in _query_dates(target_date):
        date_str = item.isoformat()
        cached = cached_dates.get(date_str)
        if isinstance(cached, dict) and not is_stale(cached.get("updated_at"), cache_hours):
            continue
        result.append(date_str)
    return result


def _source_name(source) -> str:
    return str(getattr(source, "name", type(source).__name__)).lower()


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
) -> None:
    for cabin in cabins:
        group = f"{group_prefix}:{origin}:{dest}:{depart_date}:{cabin}"
        for source in search_sources:
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
            )
        for source in enrichment_sources:
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
            )
        for flex_date in flex_dates:
            for source in search_sources:
                plan.add_request(
                    source,
                    origin,
                    dest,
                    flex_date,
                    passengers,
                    cabin,
                    conditional="search_has_candidates",
                    group=group,
                    consumer=consumer,
                    reason="弹性日期",
                )
        if include_calendar and search_sources:
            calendar_source = search_sources[0]
            for calendar_date in _calendar_dates(depart_date, origin, dest):
                plan.add_request(
                    calendar_source,
                    origin,
                    dest,
                    calendar_date,
                    passengers,
                    cabin,
                    conditional="search_has_candidates",
                    ttl_seconds=6 * 60 * 60,
                    group=group,
                    consumer=consumer,
                    reason="低价日历",
                )


def build_collection_plan(
    *,
    subscriptions: list[dict] | None = None,
    basket_requests: list[dict] | None = None,
    source_builder=None,
    include_calendars: bool = True,
) -> CollectionPlan:
    if source_builder is None:
        from sources.aggregator import build_default_sources

        source_builder = build_default_sources

    subscriptions = list(subscriptions or [])
    basket_requests = list(basket_requests or [])
    plan = CollectionPlan(
        subscription_count=len(subscriptions),
        basket_date_count=len(basket_requests),
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
        consumer = str(subscription.get("_index", subscription.get("id", index)))
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
        if required:
            search_sources = [source for source in search_sources if _source_name(source) in required]
        for source in search_sources:
            plan.add_request(
                source,
                origin,
                dest,
                depart_date,
                {"adult": 1},
                str(item.get("cabin_class") or "economy"),
                force_fresh=True,
                consumer=f"basket:{item.get('queue', index)}",
                reason="固定篮子",
            )
    return plan


def load_collection_settings(path: str | Path) -> dict:
    try:
        import yaml
    except ModuleNotFoundError:
        return {"source_quota_budget": {}, "source_quota_low_remaining_threshold": 50}
    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        payload = {}
    return {
        "source_quota_budget": dict(payload.get("source_quota_budget") or {}),
        "source_quota_low_remaining_threshold": int(
            payload.get("source_quota_low_remaining_threshold") or 50
        ),
    }
