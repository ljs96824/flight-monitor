"""固定机场篮子的每日新鲜采集入口，只采集并写入观测库。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from log_utils import configure_stdio_utf8, safe_log
from observations_store import (
    DEFAULT_DB_PATH,
    clear_current_round,
    count_observations_for_round,
    set_current_round,
)
from request_cache import print_request_cache_stats, reset_request_cache
from sources.aggregator import FlightAggregator, build_default_sources


BASE_DIR = Path(__file__).parent
DEFAULT_STATE_PATH = BASE_DIR / "data" / "basket_state.json"

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
    missing = [name for name in required if name not in available]
    if missing:
        raise RuntimeError(f"缺少配置源:{','.join(missing)}")
    selected = [available[name] for name in required]
    return aggregator_factory(selected, [], route_type=route["route_type"])


def make_round_id(now: datetime) -> str:
    return now.strftime("basket_%Y%m%dT%H%M%S")


def run_basket(
    *,
    today: date | None = None,
    now: datetime | None = None,
    state_path: str | Path = DEFAULT_STATE_PATH,
    db_path: str | Path = DEFAULT_DB_PATH,
    source_builder: Callable = build_default_sources,
    aggregator_factory: Callable = FlightAggregator,
) -> dict:
    today = today or date.today()
    now = now or datetime.now()
    round_id = make_round_id(now)
    state = load_or_create_state(state_path, today)
    if renew_expired_queues(state, today):
        _write_state(Path(state_path), state)

    reset_request_cache()
    set_current_round(round_id, db_path=db_path)
    safe_log(f"[篮子轮次] round_id={round_id}")

    queues = 0
    success = 0
    failed = 0
    try:
        for route in BASKET_ROUTES:
            route_name = _route_name(route)
            queue_dates = (state.get("routes") or {}).get(route_name) or {}
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

            for queue_name in ("A", "B"):
                queues += 1
                depart_date = str(queue_dates.get(queue_name) or "")
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
                        force_fresh=True,
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
        try:
            written = count_observations_for_round(round_id, db_path)
        except Exception as exc:
            written = 0
            safe_log(f"[篮子失败] route=summary 原因=观测计数失败:{exc}")
        print_request_cache_stats()
        clear_current_round()

    summary = {
        "round_id": round_id,
        "queues": queues,
        "success": success,
        "failed": failed,
        "written": written,
    }
    safe_log(f"[篮子完成] 队列={queues} 成功={success} 失败={failed} 总写入={written}")
    return summary


def _load_environment() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise RuntimeError("缺少python-dotenv,无法读取数据源配置") from exc
    load_dotenv(BASE_DIR / ".env")


def main() -> int:
    configure_stdio_utf8()
    try:
        _load_environment()
        summary = run_basket()
    except Exception as exc:
        safe_log(f"[篮子失败] route=bootstrap 原因={exc}")
        return 1
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
