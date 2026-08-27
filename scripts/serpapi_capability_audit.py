"""SerpAPI Google Flights 舱位能力审计。

默认 dry-run。只有显式传入 ``--execute`` 才会读取受支持的 SerpAPI 密钥
环境变量并发起
真实请求。审计预算硬限制为总计 6 次、SerpAPI 最多 3 次；当前方案固定执行
商务舱和经济舱各 1 次。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api_usage import (
    DEFAULT_USAGE_PATH,
    load_usage_for_diagnostics,
    load_usage_strict,
    record_actual_requests,
    usage_snapshot,
)
from serpapi_credentials import (
    SERPAPI_KEY_ALIASES,
    dotenv_variable_names,
    resolve_serpapi_key,
)


SERPAPI_URL = "https://serpapi.com/search.json"
DEFAULT_ORIGIN = "PVG"
DEFAULT_DESTINATION = "KIX"
DEFAULT_DEPART_DATE = "2026-10-01"
MAX_TOTAL_CALLS = 6
MAX_SERPAPI_CALLS = 3
CABIN_REQUESTS = (("business", 3), ("economy", 1))


class AuditBudgetExceeded(RuntimeError):
    pass


@dataclass
class AuditBudget:
    total_limit: int = MAX_TOTAL_CALLS
    source_limit: int = MAX_SERPAPI_CALLS
    total: int = 0
    counts: Counter = field(default_factory=Counter)

    def reserve(self, source: str) -> None:
        if self.total >= self.total_limit:
            raise AuditBudgetExceeded(f"真实 API 审计总预算已达 {self.total_limit} 次")
        if self.counts[source] >= self.source_limit:
            raise AuditBudgetExceeded(
                f"{source} 审计预算已达 {self.source_limit} 次"
            )
        self.total += 1
        self.counts[source] += 1


def public_parameters(origin: str, dest: str, depart_date: str, travel_class: int) -> dict:
    return {
        "engine": "google_flights",
        "departure_id": origin.upper(),
        "arrival_id": dest.upper(),
        "outbound_date": depart_date,
        "type": "2",
        "currency": "CNY",
        "hl": "zh-cn",
        "gl": "cn",
        "travel_class": int(travel_class),
        "adults": 1,
    }


def _positive_price(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def summarize_response(payload: dict, *, requested_cabin: str) -> dict:
    best = list(payload.get("best_flights") or [])
    other = list(payload.get("other_flights") or [])
    itineraries = best + other
    search_parameters = dict(payload.get("search_parameters") or {})
    airlines = set()
    travel_classes = set()
    codeshare_basis = set()
    layover_samples = []
    segment_samples = []
    prices = []

    for itinerary in itineraries:
        price = _positive_price(itinerary.get("price"))
        if price is not None:
            prices.append(price)
        layovers = list(itinerary.get("layovers") or [])
        if layovers and len(layover_samples) < 3:
            layover_samples.append(
                [
                    {
                        "airport": item.get("id"),
                        "duration_min": item.get("duration"),
                        "overnight": bool(item.get("overnight")),
                    }
                    for item in layovers
                ]
            )
        for segment in itinerary.get("flights") or []:
            airline = str(segment.get("airline") or "").strip()
            if airline:
                airlines.add(airline)
            cabin = str(segment.get("travel_class") or "").strip()
            if cabin:
                travel_classes.add(cabin)
            if segment.get("plane_and_crew_by"):
                codeshare_basis.add("plane_and_crew_by")
            if segment.get("ticket_also_sold_by"):
                codeshare_basis.add("ticket_also_sold_by")
            if len(segment_samples) < 5:
                segment_samples.append(
                    {
                        "airline": airline,
                        "flight_number": segment.get("flight_number"),
                        "travel_class": cabin,
                        "departure": dict(segment.get("departure_airport") or {}),
                        "arrival": dict(segment.get("arrival_airport") or {}),
                        "plane_and_crew_by": segment.get("plane_and_crew_by"),
                        "ticket_also_sold_by": segment.get("ticket_also_sold_by"),
                    }
                )

    minimum_price = min(prices) if prices else None
    requested_business = requested_cabin == "business"
    cabin_matches = any("business" in item.lower() for item in travel_classes)
    production_gate_passed = bool(
        requested_business and minimum_price and airlines and cabin_matches
    )
    capability = (
        "available"
        if (not requested_business and minimum_price and airlines)
        or production_gate_passed
        else "unavailable"
    )
    return {
        "requested_cabin": requested_cabin,
        "best_flights_count": len(best),
        "other_flights_count": len(other),
        "itinerary_count": len(itineraries),
        "airlines": sorted(airlines),
        "travel_classes": sorted(travel_classes),
        "minimum_price": minimum_price,
        "currency": search_parameters.get("currency"),
        "price_scope": (
            "单成人单程展示价；SerpAPI Google Flights 主结果未提供独立税额字段，"
            "是否含全部税费需以预订页核实"
        ),
        "segment_samples": segment_samples,
        "layover_samples": layover_samples,
        "codeshare_basis": sorted(codeshare_basis),
        "production_gate_passed": production_gate_passed,
        "capability": capability,
    }


def _redact_error(error, secret: str | None) -> str:
    text = str(error)
    if secret:
        text = text.replace(secret, "***")
    return re.sub(r"(?i)(api_key=)[^&\s]+", r"\1***", text)


def _usage_brief(path, *, strict: bool) -> dict:
    if strict:
        return usage_snapshot(load_usage_strict(path))
    diagnostic = load_usage_for_diagnostics(path)
    if diagnostic["healthy"]:
        return usage_snapshot(diagnostic["usage"])
    return {
        "healthy": False,
        "error_type": diagnostic["error_type"],
        "error": diagnostic["error"],
    }


def run_audit(
    *,
    execute: bool,
    origin: str = DEFAULT_ORIGIN,
    dest: str = DEFAULT_DESTINATION,
    depart_date: str = DEFAULT_DEPART_DATE,
    env: Mapping[str, str] | None = None,
    env_path: str | Path = ROOT / ".env",
    usage_path: str | Path = DEFAULT_USAGE_PATH,
    http_get: Callable | None = None,
    round_id: str | None = None,
    timeout: float = 60,
) -> dict:
    environment = env if env is not None else os.environ
    get = http_get or requests.get
    audit_round = round_id or (
        "audit_serpapi_" + datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    )
    report = {
        "round_id": audit_round,
        "mode": "execute" if execute else "dry_run",
        "route": f"{origin.upper()}->{dest.upper()}",
        "depart_date": depart_date,
        "budget": {"total": MAX_TOTAL_CALLS, "serpapi": MAX_SERPAPI_CALLS},
        "ledger_before": _usage_brief(usage_path, strict=execute),
        "actual_calls": {},
        "calls": [],
        "results": {},
    }
    planned = [
        public_parameters(origin, dest, depart_date, travel_class)
        for _, travel_class in CABIN_REQUESTS
    ]
    if not execute:
        report["planned_calls"] = planned
        report["ledger_after"] = report["ledger_before"]
        report["production_gate_passed"] = False
        report["gate_reason"] = "dry-run 未产生商务舱能力证据"
        return report

    api_key, _ = resolve_serpapi_key(environment)
    if not api_key:
        report["ledger_after"] = report["ledger_before"]
        report["production_gate_passed"] = False
        aliases = "/".join(SERPAPI_KEY_ALIASES)
        available_names = ", ".join(dotenv_variable_names(env_path))
        report["gate_reason"] = (
            f"缺少 SerpAPI 密钥（已检查 {aliases}）；"
            f".env 实际变量名=[{available_names}]；未发起请求"
        )
        return report

    budget = AuditBudget()
    for cabin_name, travel_class in CABIN_REQUESTS:
        params = public_parameters(origin, dest, depart_date, travel_class)
        call = {"source": "serpapi", "cabin": cabin_name, "parameters": params}
        budget.reserve("serpapi")
        report["actual_calls"]["serpapi"] = (
            report["actual_calls"].get("serpapi", 0) + 1
        )
        try:
            response = get(
                SERPAPI_URL,
                params={**params, "api_key": api_key},
                timeout=timeout,
            )
            call["http_status"] = int(response.status_code)
            response.raise_for_status()
            payload = response.json()
            if payload.get("error"):
                raise RuntimeError(str(payload["error"]))
            summary = summarize_response(payload, requested_cabin=cabin_name)
            call.update({"status": "completed", "response": summary})
            report["results"][cabin_name] = summary
        except Exception as exc:
            error = _redact_error(exc, api_key)
            call.update({"status": "failed", "error": error})
            report["results"][cabin_name] = {
                "requested_cabin": cabin_name,
                "capability": "unavailable",
                "production_gate_passed": False,
                "error": error,
            }
        finally:
            record_actual_requests(
                {"serpapi": 1}, path=usage_path, round_id=audit_round
            )
            report["calls"].append(call)

    business = report["results"].get("business") or {}
    report["production_gate_passed"] = bool(
        business.get("production_gate_passed")
    )
    report["gate_reason"] = (
        "商务舱返回真实航司、正价与 Business 舱位字段，可进入生产适配"
        if report["production_gate_passed"]
        else "商务舱未返回可核验的真实航司正价，按协议停止生产接线"
    )
    report["separate_call_required"] = (
        "是。travel_class 是请求级参数，经济舱与商务舱需分别调用。"
    )
    report["codeshare_note"] = (
        "响应可通过 plane_and_crew_by 表示实际提供飞机与机组的航司；"
        "缺失时只能按市场承运回退。"
    )
    report["budget_used"] = {"total": budget.total, "serpapi": budget.counts["serpapi"]}
    report["ledger_after"] = _usage_brief(usage_path, strict=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="显式允许真实 API 审计")
    parser.add_argument("--origin", default=DEFAULT_ORIGIN)
    parser.add_argument("--dest", default=DEFAULT_DESTINATION)
    parser.add_argument("--date", default=DEFAULT_DEPART_DATE)
    parser.add_argument("--usage-path", default=str(DEFAULT_USAGE_PATH))
    parser.add_argument("--round-id")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--output")
    args = parser.parse_args()

    if args.execute:
        load_dotenv(ROOT / ".env", override=False)
    report = run_audit(
        execute=args.execute,
        origin=args.origin,
        dest=args.dest,
        depart_date=args.date,
        usage_path=args.usage_path,
        round_id=args.round_id,
        timeout=args.timeout,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    if args.execute and not report.get("production_gate_passed"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
