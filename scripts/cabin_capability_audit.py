"""独立审计 Juhe 与 Duffel 的舱位报价能力。

脚本默认只输出计划。只有显式传入 ``--execute`` 才会发起真实请求；每个源
默认只探测一次，且硬限制为 Juhe 不超过 3 次、Duffel 不超过 3 次、总计不超过
6 次。脚本不接入采集、分析或推送链路，也不保存原始响应与认证信息。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping
from uuid import uuid4

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
from scripts.manual_live_guard import (
    no_live_api_enabled,
    prepare_manual_live_execution,
)
from workload_class import MANUAL_LIVE


JUHE_URL = "https://apis.juhe.cn/flight/query"
DUFFEL_URL = "https://api.duffel.com/air/offer_requests"
DEFAULT_ORIGIN = "PVG"
DEFAULT_DESTINATION = "KIX"
DEFAULT_DEPART_DATE = "2026-10-01"
DEFAULT_CABIN = "business"
MAX_TOTAL_CALLS = 6
SOURCE_CALL_LIMITS = {"juhe": 3, "duffel": 3}
ENTRYPOINT = "cabin_capability_audit"
OFFICIAL_DOCS = {
    "juhe": "https://www.juhe.cn/docs/api/id/818",
    "duffel_offer_requests": "https://duffel.com/docs/api/v2/offer-requests",
    "duffel_offers": "https://duffel.com/docs/api/offers/schema",
    "duffel_test_mode": "https://duffel.com/docs/api/overview/test-mode/duffel-airways",
}

_CABIN_MARKERS = ("cabin", "bookingclass", "booking_class", "farebrand", "fare_brand", "舱")
_PRICE_MARKERS = ("price", "amount", "fare", "票价")


class AuditBudgetExceeded(RuntimeError):
    """审计请求将超过用户授权预算。"""


class AuditBudget:
    """只计真实 HTTP 尝试的硬预算闸门。"""

    def __init__(
        self,
        *,
        total_limit: int = MAX_TOTAL_CALLS,
        source_limits: Mapping[str, int] | None = None,
    ):
        self.total_limit = int(total_limit)
        self.source_limits = dict(source_limits or SOURCE_CALL_LIMITS)
        self.counts: Counter[str] = Counter()

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def reserve(self, source: str) -> None:
        source_name = str(source).lower()
        if self.total >= self.total_limit:
            raise AuditBudgetExceeded(f"总调用预算已达 {self.total_limit} 次")
        source_limit = int(self.source_limits.get(source_name, 0))
        if source_limit <= 0 or self.counts[source_name] >= source_limit:
            raise AuditBudgetExceeded(
                f"{source_name} 调用预算已达 {source_limit} 次"
            )
        self.counts[source_name] += 1


def _juhe_flights(payload: dict) -> list[dict]:
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, dict) and isinstance(result.get("flightInfo"), list):
        return [item for item in result["flightInfo"] if isinstance(item, dict)]
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    return []


def _walk_fields(value, *, prefix=""):
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield path, key, child
            yield from _walk_fields(child, prefix=path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_fields(child, prefix=f"{prefix}[{index}]")


def _matching_paths(value, markers) -> list[str]:
    paths = {
        path
        for path, key, _child in _walk_fields(value)
        if any(marker in str(key).lower() for marker in markers)
    }
    return sorted(paths)


def _matching_values(value, markers) -> list[str]:
    values = set()
    for _path, key, child in _walk_fields(value):
        if not any(marker in str(key).lower() for marker in markers):
            continue
        if isinstance(child, (str, int, float, bool)) and child not in ("", None):
            values.add(str(child).strip().lower())
    return sorted(values)


def _has_bound_cabin_price_record(value) -> bool:
    if isinstance(value, dict):
        keys = [str(key).lower() for key in value]
        has_cabin = any(any(marker in key for marker in _CABIN_MARKERS) for key in keys)
        has_price = any(any(marker in key for marker in _PRICE_MARKERS) for key in keys)
        if has_cabin and has_price:
            return True
        return any(_has_bound_cabin_price_record(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_bound_cabin_price_record(child) for child in value)
    return False


def summarize_juhe_response(payload: dict) -> dict:
    """仅提取能力判断所需字段，不回传完整原始响应。"""
    payload = payload if isinstance(payload, dict) else {}
    flights = _juhe_flights(payload)
    cabin_paths = _matching_paths(flights, _CABIN_MARKERS)
    cabin_values = _matching_values(flights, _CABIN_MARKERS)
    price_samples = [
        item.get("ticketPrice")
        for item in flights[:5]
        if item.get("ticketPrice") not in (None, "")
    ]
    code = payload.get("error_code", payload.get("resultcode"))
    success = code in (None, 0, "0", 200, "200")

    if not success:
        capability = "unavailable"
        conclusion = "请求失败，无法验证商务舱能力"
    elif not flights:
        capability = "partial"
        conclusion = "HTTP成功但航班列表为空，无法验证未文档化舱位字段"
    elif not cabin_paths:
        capability = "unavailable"
        conclusion = "仅返回单一参考票价，未发现舱位或分舱价格字段"
    elif _has_bound_cabin_price_record(flights):
        capability = "available"
        conclusion = "发现舱位与价格绑定记录，可进一步接入验证"
    else:
        capability = "partial"
        conclusion = "发现舱位相关字段，但未验证其与价格逐项绑定"

    flight_keys = sorted({str(key) for item in flights for key in item})
    return {
        "http_contract": "官方参数无 cabin；响应文档仅一个 ticketPrice",
        "error_code": code,
        "reason": str(payload.get("reason") or ""),
        "flight_count": len(flights),
        "flight_keys": flight_keys,
        "price_samples": price_samples,
        "cabin_field_paths": cabin_paths,
        "cabin_field_values": cabin_values,
        "capability": capability,
        "conclusion": conclusion,
    }


def _as_decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _offer_owner(offer: dict) -> dict:
    owner = offer.get("owner") or {}
    return {
        "iata_code": str(owner.get("iata_code") or ""),
        "name": str(owner.get("name") or ""),
    }


def summarize_duffel_response(payload: dict) -> dict:
    """提取 Duffel 环境、商务舱与含税总价证据。"""
    payload = payload if isinstance(payload, dict) else {}
    data = payload.get("data") or {}
    offers = data.get("offers") or []
    offers = [offer for offer in offers if isinstance(offer, dict)]
    observed_cabins = _matching_values(offers, ("cabin_class",))
    priced_offers = [
        offer for offer in offers if _as_decimal(offer.get("total_amount")) is not None
    ]
    minimum = min(
        priced_offers,
        key=lambda offer: _as_decimal(offer.get("total_amount")),
        default=None,
    )
    minimum_offer = None
    if minimum is not None:
        minimum_offer = {
            "total_amount": str(minimum.get("total_amount")),
            "total_currency": str(minimum.get("total_currency") or ""),
            "tax_amount": (
                None
                if minimum.get("tax_amount") is None
                else str(minimum.get("tax_amount"))
            ),
            "tax_currency": str(minimum.get("tax_currency") or ""),
            "tax_included_in_total": True,
            "owner": _offer_owner(minimum),
        }

    live_mode = data.get("live_mode")
    has_business = "business" in observed_cabins
    if live_mode is True and has_business and minimum_offer:
        capability = "available"
        conclusion = "live 模式返回商务舱含税总价，可作为独立报价候选"
        market_price_usable = True
    elif live_mode is False and has_business:
        capability = "partial"
        conclusion = "test 模式技术链路可用，但时刻与价格不具市场真实性"
        market_price_usable = False
    elif not offers:
        capability = "partial"
        conclusion = "请求被接受但无商务舱 offer，不能据此判定源不支持商务舱"
        market_price_usable = False
    else:
        capability = "unavailable"
        conclusion = "响应未提供可核验的商务舱含税报价"
        market_price_usable = False

    return {
        "live_mode": live_mode,
        "offer_count": len(offers),
        "observed_cabins": observed_cabins,
        "minimum_offer": minimum_offer,
        "price_scope": "单成人单程总价，含税，不含后加服务",
        "market_price_usable": market_price_usable,
        "capability": capability,
        "conclusion": conclusion,
    }


def _redact_error(error, secrets=()) -> str:
    text = str(error)
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "***")
    text = re.sub(r"(?i)(key=)[^&\s]+", r"\1***", text)
    text = re.sub(r"(?i)(bearer\s+)[^\s]+", r"\1***", text)
    return text


def _public_parameters(source: str, origin: str, dest: str, depart_date: str) -> dict:
    common = {"origin": origin, "destination": dest, "depart_date": depart_date}
    if source == "duffel":
        return {
            **common,
            "cabin_class": DEFAULT_CABIN,
            "passengers": ["adult"],
            "return_offers": True,
        }
    return {
        **common,
        "cabin_parameter": None,
        "note": "官方接口无舱位请求参数，本次仅检查响应是否自带多舱字段",
    }


def _recommend_route(results: dict) -> dict:
    juhe = (results.get("juhe") or {}).get("capability")
    duffel = (results.get("duffel") or {}).get("capability")
    duffel_market = bool((results.get("duffel") or {}).get("market_price_usable"))
    if juhe == "available":
        return {
            "route": "A",
            "reason": "Juhe 单次响应可区分经济舱与商务舱价格",
            "incremental_calls_per_fixed_roundtrip": 0,
        }
    if duffel == "available" and duffel_market:
        return {
            "route": "B",
            "reason": "Juhe 提供经济舱，Duffel live offer 提供商务舱",
            "incremental_calls_per_fixed_roundtrip": 2,
        }
    return {
        "route": "C",
        "reason": "当前源不能同时提供可用于市场监控的经济舱与商务舱价格",
        "incremental_calls_per_fixed_roundtrip": 0,
    }


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
    sources=("juhe", "duffel"),
    env: Mapping[str, str] | None = None,
    usage_path: str | Path = DEFAULT_USAGE_PATH,
    http_get: Callable | None = None,
    http_post: Callable | None = None,
    round_id: str | None = None,
    timeout: float = 45,
) -> dict:
    """执行至多每源一次的审计；HTTP 客户端可注入以便完全离线测试。"""
    environment = env if env is not None else os.environ
    get = http_get or requests.get
    post = http_post or requests.post
    selected = tuple(dict.fromkeys(str(source).lower() for source in sources))
    unknown = [source for source in selected if source not in SOURCE_CALL_LIMITS]
    if unknown:
        raise ValueError(f"未知审计源: {unknown}")

    audit_round = round_id or (
        "audit_cabin_"
        + datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
        + "_"
        + uuid4().hex[:8]
    )
    report = {
        "round_id": audit_round,
        "mode": "execute" if execute else "dry_run",
        "route": f"{origin.upper()}->{dest.upper()}",
        "depart_date": depart_date,
        "requested_cabin": DEFAULT_CABIN,
        "budget": {"total": MAX_TOTAL_CALLS, "per_source": SOURCE_CALL_LIMITS},
        "official_docs": OFFICIAL_DOCS,
        "calls": [],
        "results": {},
        "actual_calls": {},
    }
    if not execute:
        report["ledger_before"] = _usage_brief(usage_path, strict=False)
        report["planned_calls"] = [
            _public_parameters(source, origin, dest, depart_date) for source in selected
        ]
        report["ledger_after"] = report["ledger_before"]
        report["recommendation"] = {
            "route": "pending",
            "reason": "dry-run 未产生能力证据",
        }
        return report

    planned_counts = {source: 1 for source in selected}
    gate = prepare_manual_live_execution(
        environment=environment,
        depart_date=depart_date,
        planned_counts=planned_counts,
        usage_path=usage_path,
        round_id=audit_round,
    )
    report.update(gate.report_fields())
    report["ledger_before"] = gate.ledger_snapshot
    if not gate.allowed:
        report["ledger_after"] = gate.ledger_snapshot
        report["recommendation"] = {
            "route": "blocked",
            "reason": gate.gate_reason,
        }
        return report

    plan_text = ",".join(f"{source}:1" for source in selected) or "none"
    print(
        f"[审计计划] entrypoint={ENTRYPOINT} 计划调用={len(selected)} "
        f"明细={plan_text} 总上限={MAX_TOTAL_CALLS} "
        f"源上限=juhe:{SOURCE_CALL_LIMITS['juhe']},"
        f"duffel:{SOURCE_CALL_LIMITS['duffel']}"
    )
    try:
        budget = AuditBudget()
        secrets = (
            environment.get("JUHE_FLIGHT_KEY"),
            environment.get("DUFFEL_TOKEN"),
        )
        for source in selected:
            credential_name = (
                "JUHE_FLIGHT_KEY" if source == "juhe" else "DUFFEL_TOKEN"
            )
            credential = environment.get(credential_name)
            public_params = _public_parameters(source, origin, dest, depart_date)
            call_record = {"source": source, "parameters": public_params}
            if not credential:
                call_record.update(
                    {"status": "skipped", "reason": f"缺少 {credential_name}"}
                )
                report["calls"].append(call_record)
                continue

            budget.reserve(source)
            report["actual_calls"][source] = (
                report["actual_calls"].get(source, 0) + 1
            )
            try:
                if source == "juhe":
                    response = get(
                        JUHE_URL,
                        params={
                            "key": credential,
                            "departure": origin.upper(),
                            "arrival": dest.upper(),
                            "departureDate": depart_date,
                            "flightNo": "",
                            "maxSegments": "0",
                        },
                        timeout=timeout,
                    )
                else:
                    response = post(
                        DUFFEL_URL,
                        params={"return_offers": "true"},
                        json={
                            "data": {
                                "slices": [
                                    {
                                        "origin": origin.upper(),
                                        "destination": dest.upper(),
                                        "departure_date": depart_date,
                                    }
                                ],
                                "passengers": [{"type": "adult"}],
                                "cabin_class": DEFAULT_CABIN,
                            }
                        },
                        headers={
                            "Authorization": f"Bearer {credential}",
                            "Duffel-Version": "v2",
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                        },
                        timeout=timeout,
                    )

                call_record["http_status"] = int(response.status_code)
                response.raise_for_status()
                payload = response.json()
                summary = (
                    summarize_juhe_response(payload)
                    if source == "juhe"
                    else summarize_duffel_response(payload)
                )
                call_record.update({"status": "completed", "response": summary})
                report["results"][source] = summary
            except Exception as exc:
                message = _redact_error(exc, secrets)
                call_record.update({"status": "failed", "error": message})
                report["results"][source] = {
                    "capability": "unavailable",
                    "conclusion": f"探测失败: {message}",
                }
            finally:
                record_actual_requests(
                    {source: 1},
                    path=usage_path,
                    round_id=audit_round,
                    workload_class=MANUAL_LIVE,
                    entrypoint=ENTRYPOINT,
                )
                report["calls"].append(call_record)

        report["ledger_after"] = _usage_brief(usage_path, strict=True)
        report["recommendation"] = _recommend_route(report["results"])
        report["budget_used"] = {
            "total": budget.total,
            "per_source": dict(budget.counts),
        }
        report["status"] = "completed"
        report["exit_code"] = (
            1 if any(call.get("status") == "failed" for call in report["calls"]) else 0
        )
        return report
    finally:
        gate.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="显式允许真实 API 探测")
    parser.add_argument("--source", action="append", choices=sorted(SOURCE_CALL_LIMITS))
    parser.add_argument("--origin", default=DEFAULT_ORIGIN)
    parser.add_argument("--dest", default=DEFAULT_DESTINATION)
    parser.add_argument("--date", default=DEFAULT_DEPART_DATE)
    parser.add_argument("--usage-path", default=str(DEFAULT_USAGE_PATH))
    parser.add_argument("--round-id")
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    if args.execute and not no_live_api_enabled(os.environ):
        load_dotenv(ROOT / ".env", override=False)
    report = run_audit(
        execute=args.execute,
        origin=args.origin,
        dest=args.dest,
        depart_date=args.date,
        sources=args.source or ("juhe", "duffel"),
        usage_path=args.usage_path,
        round_id=args.round_id,
        timeout=args.timeout,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    return int(report.get("exit_code") or 0)


if __name__ == "__main__":
    raise SystemExit(main())
