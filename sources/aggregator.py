"""Multi-source flight result aggregator."""

from __future__ import annotations

import json
import os
from datetime import datetime

from flight_combo_utils import normalize_combo
from log_utils import safe_log
from source_profiles import get_source_profile, normalize_route_type
from request_cache import cached_fetch
from sources.base import FlightSource

OPTIONAL_SOURCE_THRESHOLD = 8

CN_AIRPORTS = {
    "PVG",
    "SHA",
    "PEK",
    "PKX",
    "CAN",
    "SZX",
    "CTU",
    "TFU",
    "HGH",
    "NKG",
    "XIY",
    "CKG",
    "WUH",
    "CSX",
    "TAO",
    "XMN",
    "FOC",
    "KMG",
    "URC",
    "CGO",
    "TSN",
    "DLC",
    "SHE",
    "HRB",
    "SYX",
    "HAK",
}

GREATER_CHINA_AIRPORTS = {
    "HKG",
    "MFM",
    "TPE",
    "TSA",
    "KHH",
    "RMQ",
    "TNN",
    "HUN",
    "CYI",
    "MZG",
    "KNH",
}



def _redact_api_key(text: str) -> str:
    return text.split("api_key=")[0] + "api_key=***" if "api_key=" in text else text


def _source_names(value: str | None) -> list[str]:
    return [
        source.strip().lower()
        for source in str(value or "").split("+")
        if source.strip()
    ]


def _append_sources(flight: dict, sources: list[str]) -> None:
    current_sources = _source_names(flight.get("data_source") or flight.get("source"))
    for source in sources:
        if source and source not in current_sources:
            current_sources.append(source)
    if current_sources:
        flight["data_source"] = "+".join(current_sources)


def _merge_booking_options(target: dict, source: dict) -> None:
    options = list(target.get("booking_options") or [])
    seen = {
        (option.get("platform"), option.get("url"), option.get("price"))
        for option in options
        if isinstance(option, dict)
    }
    for option in source.get("booking_options") or []:
        if not isinstance(option, dict):
            continue
        key = (option.get("platform"), option.get("url"), option.get("price"))
        if key not in seen:
            seen.add(key)
            options.append(option)
    if options:
        target["booking_options"] = options


def _non_empty(value) -> bool:
    return value not in (None, "", [], {})


def _segment_score(segments) -> int:
    score = 0
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        score += 1
        for key in (
            "flight_no",
            "flight_number",
            "airline",
            "aircraft",
            "airplane",
            "dep_airport",
            "departure_airport",
            "dep_time",
            "departure_time",
            "arr_airport",
            "arrival_airport",
            "arr_time",
            "arrival_time",
        ):
            if segment.get(key):
                score += 1
    return score


def _merge_flight_fields(target: dict, source: dict) -> dict:
    """Merge duplicate flight records without letting sparse lower-price rows erase details."""
    for key in ("segments", "layovers", "flights", "legs"):
        source_value = source.get(key)
        target_value = target.get(key)
        if key in ("segments", "flights", "legs"):
            if _segment_score(source_value) > _segment_score(target_value):
                target[key] = source_value
        elif len(source_value or []) > len(target_value or []):
            target[key] = source_value

    for key, value in source.items():
        if key in {
            "price",
            "source",
            "data_source",
            "source_price_details",
            "booking_options",
            "reference_only",
            "reference_reason",
            "source_role",
            "data_quality",
        }:
            continue
        if not _non_empty(target.get(key)) and _non_empty(value):
            target[key] = value

    _merge_booking_options(target, source)
    for entry in source.get("source_price_details") or []:
        _append_source_price(target, entry.get("source"), entry.get("price"))
    return target


def _append_source_price(target: dict, source_name: str | None, price) -> None:
    try:
        value = float(price)
    except (TypeError, ValueError):
        return
    if value <= 0:
        return

    entries = list(target.get("source_price_details") or [])
    source_label = source_name or target.get("data_source") or target.get("source") or "unknown"
    for entry in entries:
        if entry.get("source") == source_label and float(entry.get("price") or 0) == value:
            return
    entries.append({"source": source_label, "price": value})
    target["source_price_details"] = entries


def _valid_price(value) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _log_combo_normalization_once(
    raw_combo: str | None,
    normalized_combo: str,
    source_name: str,
    logged_sources: set[str],
) -> None:
    if not raw_combo or not normalized_combo or source_name in logged_sources:
        return
    raw_text = str(raw_combo)
    raw_compact = raw_text.replace(" ", "").upper()
    if "+" not in normalized_combo or raw_compact == normalized_combo:
        return
    logged_sources.add(source_name)
    safe_log(f"[\u53bb\u91cd\u6838\u5bf9] raw={raw_text} norm={normalized_combo} \u6e90={source_name}")


def _source_price_map(flight: dict) -> dict[str, float]:
    prices = {}
    for entry in flight.get("source_price_details") or []:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source") or "").lower()
        try:
            price = float(entry.get("price"))
        except (TypeError, ValueError):
            continue
        if source and price > 0:
            prices[source] = price
    return prices


def _log_dual_source_price_checks(flights: list[dict]) -> list[dict]:
    anomalies = []
    for flight in flights:
        sources = _source_names(flight.get("data_source") or flight.get("source"))
        if not {"hasdata", "juhe"}.issubset(set(sources)):
            continue
        combo = flight.get("flight_combo") or _flight_key(flight)
        safe_log(f"[\u53bb\u91cd\u6838\u5bf9] combo={combo} \u6765\u6e90={'+'.join(sources)}")
        prices = _source_price_map(flight)
        hasdata_price = prices.get("hasdata")
        juhe_price = prices.get("juhe")
        if not hasdata_price or not juhe_price:
            continue
        min_price = min(hasdata_price, juhe_price)
        diff_pct = abs(hasdata_price - juhe_price) / min_price * 100 if min_price else 0
        safe_log(
            f"[\u6e90\u4ef7\u5bf9\u6bd4] combo={combo} hasdata=CNY{hasdata_price:g} "
            f"juhe=CNY{juhe_price:g} \u5dee\u5f02%={diff_pct:.1f}"
        )
        if diff_pct > 15:
            anomalies.append(
                {
                    "flight_combo": combo,
                    "min_price": min_price,
                    "max_price": max(hasdata_price, juhe_price),
                    "diff_pct": round(diff_pct, 1),
                    "sources": [
                        {"source": "hasdata", "flight_combo": combo, "price": hasdata_price},
                        {"source": "juhe", "flight_combo": combo, "price": juhe_price},
                    ],
                }
            )
    return anomalies


def _normalize_cabin_classes(cabin_classes) -> list[str]:
    if not cabin_classes:
        return ["economy"]
    if isinstance(cabin_classes, str):
        return [cabin_classes]
    return list(cabin_classes)


def _flight_key(flight: dict) -> str:
    combo = normalize_combo(flight.get("flight_combo", ""))
    cabin_class = flight.get("cabin_class") or "economy"
    return f"{combo}::{cabin_class}"


def is_domestic_route(origin: str, dest: str) -> bool:
    return str(origin or "").upper() in CN_AIRPORTS and str(dest or "").upper() in CN_AIRPORTS


def classify_route_with_rule(origin: str, dest: str) -> tuple[str, str]:
    origin_code = str(origin or "").upper()
    dest_code = str(dest or "").upper()
    origin_cn = origin_code in CN_AIRPORTS
    dest_cn = dest_code in CN_AIRPORTS
    origin_gc = origin_code in GREATER_CHINA_AIRPORTS
    dest_gc = dest_code in GREATER_CHINA_AIRPORTS
    if origin_cn and dest_cn:
        return "domestic", "both_mainland_cn"
    if (origin_cn or dest_cn) and (origin_gc or dest_gc):
        return "greater_china", "mainland_to_hk_mo_tw"
    if origin_gc and dest_gc:
        return "greater_china", "hk_mo_tw_internal"
    return "international", "default_international"


def classify_route(origin: str, dest: str) -> str:
    return classify_route_with_rule(origin, dest)[0]


def route_type_for_with_rule(origin: str, dest: str, route_type: str | None = None) -> tuple[str, str]:
    explicit = normalize_route_type(route_type)
    if explicit:
        inferred_type, inferred_rule = classify_route_with_rule(origin, dest)
        if inferred_type == explicit:
            return explicit, f"explicit/{inferred_rule}"
        return explicit, f"explicit/overrides_{inferred_type}_{inferred_rule}"
    return classify_route_with_rule(origin, dest)


def route_type_for(origin: str, dest: str, route_type: str | None = None) -> str:
    return route_type_for_with_rule(origin, dest, route_type)[0]


def _source_name(source) -> str:
    return str(getattr(source, "name", type(source).__name__)).lower()


def _profile_source_specs(route_type: str) -> list[dict]:
    return list((get_source_profile(route_type) or {}).get("sources") or [])


def _profile_query(route_type: str) -> dict:
    return dict((get_source_profile(route_type) or {}).get("query") or {})


def _search_source_names(route_type: str) -> list[str]:
    return [
        str(item.get("name") or "").lower()
        for item in _profile_source_specs(route_type)
        if item.get("role") != "enrichment"
    ]


def _enrichment_source_names(route_type: str) -> list[str]:
    return [
        str(item.get("name") or "").lower()
        for item in _profile_source_specs(route_type)
        if item.get("role") == "enrichment"
    ]


def _apply_source_spec(source: FlightSource, spec: dict, route_type: str) -> FlightSource:
    source.role = spec.get("role") or "reference"
    source.weight = float(spec.get("weight") or 0)
    source.query_overrides = _profile_query(route_type)
    source.route_type = route_type
    return source


def _apply_route_source_roles(sources: list[FlightSource], route_type: str) -> list[FlightSource]:
    specs = {
        str(item.get("name") or "").lower(): item
        for item in _profile_source_specs(route_type)
    }
    ordered = []
    for name in _search_source_names(route_type):
        for source in sources:
            if _source_name(source) == name and name in specs:
                ordered.append(_apply_source_spec(source, specs[name], route_type))
                break
    return ordered


def _instantiate_source(source_name: str):
    source_name = str(source_name or "").lower()
    try:
        if source_name == "juhe" and os.environ.get("JUHE_FLIGHT_KEY"):
            from sources.juhe_source import JuheSource

            return JuheSource()
        if source_name == "serpapi" and os.environ.get("SERPAPI_KEY"):
            from sources.serpapi_source import SerpAPISource

            return SerpAPISource()
        if source_name == "hasdata" and os.environ.get("HASDATA_KEY"):
            from sources.hasdata_source import HasDataSource

            return HasDataSource()
        if source_name == "duffel" and os.environ.get("DUFFEL_TOKEN"):
            from sources.duffel_source import DuffelSource

            return DuffelSource()
        if source_name == "searchapi" and os.environ.get("SEARCHAPI_KEY"):
            from sources.searchapi_source import SearchAPISource

            return SearchAPISource()
        if source_name == "travelpayouts" and os.environ.get("TRAVELPAYOUTS_TOKEN"):
            from sources.travelpayouts_source import TravelpayoutsSource

            return TravelpayoutsSource()
        if source_name == "skyscanner" and os.environ.get("RAPIDAPI_KEY"):
            from sources.skyscanner_source import SkyscannerSource

            return SkyscannerSource()
    except Exception as exc:
        print(f"[source-profile] skip {source_name}: {exc}")
    return None


def _flight_primary_priority(flight: dict, is_domestic: bool) -> tuple[int, float]:
    sources = set(_source_names(flight.get("data_source") or flight.get("source")))
    role = str(flight.get("source_role") or "").lower()
    weight = float(flight.get("source_weight") or 0)
    if is_domestic:
        if "juhe" in sources:
            return (0, -weight)
        if role == "cross_check":
            return (1, -weight)
        return (2, -weight)
    google_sources = {"serpapi", "searchapi", "hasdata"}
    if sources & google_sources:
        return (0, -weight)
    if role == "primary":
        return (1, -weight)
    return (2, -weight)


def _should_replace_flight(current: dict, incoming: dict, is_domestic: bool) -> bool:
    current_priority = _flight_primary_priority(current, is_domestic)
    incoming_priority = _flight_primary_priority(incoming, is_domestic)
    if incoming_priority != current_priority:
        return incoming_priority < current_priority
    try:
        return float(incoming.get("price")) < float(current.get("price"))
    except (TypeError, ValueError):
        return False


def _primary_source_for_sources(sources: list[str], is_domestic: bool) -> str:
    normalized = [str(source or "").lower() for source in sources if source]
    if is_domestic and "juhe" in normalized:
        return "juhe"
    for source in ("hasdata", "serpapi", "searchapi", "juhe"):
        if source in normalized:
            return source
    return normalized[0] if normalized else ""


def build_default_sources(
    origin: str | None = None,
    dest: str | None = None,
    route_type: str | None = None,
) -> tuple[list[FlightSource], list[FlightSource]]:
    """Build search sources and enrichment sources separately."""
    search_sources = []
    enrichment_sources = []
    resolved_route_type = None
    route_rule = "fallback"
    if origin and dest:
        resolved_route_type, route_rule = route_type_for_with_rule(origin, dest, route_type)
    else:
        resolved_route_type = normalize_route_type(route_type)
        route_rule = "explicit" if resolved_route_type else "fallback"

    if resolved_route_type:
        if origin and dest:
            print(f"[\u8def\u7531\u5206\u7c7b] origin={origin} dest={dest} route_type={resolved_route_type} \u547d\u4e2d\u89c4\u5219={route_rule}")
        profile = get_source_profile(resolved_route_type)
        specs = list(profile.get("sources") or [])
        for spec in specs:
            source = _instantiate_source(spec.get("name"))
            if source is None:
                continue
            source = _apply_source_spec(source, spec, resolved_route_type)
            if spec.get("role") == "enrichment":
                enrichment_sources.append(source)
            else:
                search_sources.append(source)
        print(
            f"[源策略] {resolved_route_type} 启用: "
            f"{[(source.name, source.role) for source in search_sources + enrichment_sources]}"
        )
        return search_sources, enrichment_sources

    # Backward-compatible fallback for callers that have not supplied route context.
    if os.environ.get("JUHE_FLIGHT_KEY"):
        from sources.juhe_source import JuheSource

        search_sources.append(JuheSource())

    if os.environ.get("SERPAPI_KEY"):
        from sources.serpapi_source import SerpAPISource

        search_sources.append(SerpAPISource())

    if os.environ.get("SEARCHAPI_KEY"):
        from sources.searchapi_source import SearchAPISource

        search_sources.append(SearchAPISource())

    if os.environ.get("HASDATA_KEY"):
        from sources.hasdata_source import HasDataSource

        search_sources.append(HasDataSource())

    if os.environ.get("TRAVELPAYOUTS_TOKEN"):
        from sources.travelpayouts_source import TravelpayoutsSource

        search_sources.append(TravelpayoutsSource())

    if os.environ.get("RAPIDAPI_KEY"):
        from sources.skyscanner_source import SkyscannerSource

        search_sources.append(SkyscannerSource())

    if os.environ.get("DUFFEL_TOKEN"):
        from sources.duffel_source import DuffelSource

        enrichment_sources.append(DuffelSource())

    return search_sources, enrichment_sources


class FlightAggregator:
    def __init__(
        self,
        search_sources: list[FlightSource],
        enrichment_sources: list[FlightSource] | None = None,
        route_type: str | None = None,
    ):
        self.search_sources = search_sources
        self.enrichment_sources = enrichment_sources or []
        self.route_type = normalize_route_type(route_type)

    def collect(
        self,
        origin: str,
        dest: str,
        date_str: str,
        target_combo: str | None = None,
        cabin_classes=None,
        route_type: str | None = None,
        passengers: dict | None = None,
    ) -> dict | None:
        cabin_classes = _normalize_cabin_classes(cabin_classes)
        run_collected_at = datetime.now().isoformat(timespec="seconds")
        source_stats = {}
        all_flights = []
        successful_results = []
        source_errors = []
        price_insights = None
        raw_by_source = {}
        resolved_route_type, route_rule = route_type_for_with_rule(
            origin, dest, route_type or self.route_type
        )
        search_sources = self._ordered_search_sources(origin, dest, resolved_route_type)
        domestic_route = resolved_route_type == "domestic"
        print(f"[\u8def\u7531\u5206\u7c7b] origin={origin} dest={dest} route_type={resolved_route_type} \u547d\u4e2d\u89c4\u5219={route_rule}")
        print(f"[source-route] {origin}->{dest} route_type={resolved_route_type}")
        print(
            "[source-route] enabled sources: "
            + str([getattr(source, "name", type(source).__name__) for source in search_sources])
        )
        combo_normalization_logged_sources: set[str] = set()

        for source in search_sources:
            source_name = getattr(source, "name", type(source).__name__)
            source_role = getattr(source, "role", None) or (
                "reference" if str(source_name).lower() == "travelpayouts" else "search"
            )
            source_weight = float(getattr(source, "weight", 0) or 0)
            source_optional = len(all_flights) >= OPTIONAL_SOURCE_THRESHOLD
            source_count = 0
            source_succeeded = False
            source_statuses = []
            cabin_counts = {}

            for cabin_class in cabin_classes:
                try:
                    result = cached_fetch(
                        source,
                        origin,
                        dest,
                        date_str,
                        passengers,
                        cabin_class,
                        ttl_seconds=15 * 60,
                    )
                    source_status = result.get("source_status")
                    if source_status:
                        source_statuses.append(source_status)
                    raw_flights = result.get("flights", []) or []
                    flights = [
                        flight for flight in raw_flights if _valid_price(flight.get("price"))
                    ]
                    print(
                        f"[价格检查] {source_name} {cabin_class} 有效价格航班: "
                        f"{len(flights)}/{len(raw_flights)}"
                    )

                    if source_status in {"not_configured", "skipped"}:
                        cabin_counts[cabin_class] = 0
                        raw_by_source[f"{source_name}:{cabin_class}"] = result.get("raw")
                        print(f"[{source_name}] {cabin_class} skipped: {source_status}")
                        continue

                    for flight in flights:
                        flight["collected_at"] = (
                            flight.get("collected_at")
                            or result.get("collected_at")
                            or run_collected_at
                        )
                        if not flight.get("source"):
                            flight["source"] = source_name
                        if not flight.get("data_source"):
                            flight["data_source"] = source_name
                        raw_combo = flight.get("flight_combo") or flight.get("flight_no")
                        normalized_combo = normalize_combo(raw_combo)
                        if normalized_combo:
                            if raw_combo and str(raw_combo) != normalized_combo:
                                flight.setdefault("raw_flight_combo", str(raw_combo))
                            _log_combo_normalization_once(
                                str(raw_combo or ""),
                                normalized_combo,
                                str(source_name).lower(),
                                combo_normalization_logged_sources,
                            )
                            flight["flight_combo"] = normalized_combo
                        flight["cabin_class"] = flight.get("cabin_class") or cabin_class
                        flight["source_role"] = flight.get("source_role") or source_role
                        flight["source_weight"] = flight.get("source_weight") or source_weight
                        flight["route_type"] = resolved_route_type
                        if source_role == "primary":
                            flight["primary_source"] = source_name
                        if source_role == "reference":
                            flight["reference_only"] = True
                            flight["reference_reason"] = (
                                flight.get("reference_reason")
                                or "Travelpayouts仅提供缓存价格，缺少航段时间和机型"
                            )

                    source_count += len(flights)
                    cabin_counts[cabin_class] = len(flights)
                    source_succeeded = source_succeeded or bool(flights)
                    all_flights.extend(flights)
                    successful_results.append(
                        {
                            **result,
                            "source": source_name,
                            "flights": flights,
                        }
                    )
                    raw_by_source[f"{source_name}:{cabin_class}"] = result.get("raw")
                    print(
                        f"[{source_name}] {cabin_class} 成功，返回 {len(flights)} 个方案"
                    )

                    if not price_insights and result.get("price_insights"):
                        price_insights = result["price_insights"]

                except Exception as exc:
                    error = _redact_api_key(str(exc))
                    if not source_optional:
                        source_errors.append(
                            {
                                "source": source_name,
                                "cabin_class": cabin_class,
                                "error": error,
                            }
                        )
                    status_label = "可选失败" if source_optional else "失败"
                    print(f"[{source_name}] {cabin_class} {status_label}：{error}")

            if source_succeeded:
                status = "成功"
            elif "not_configured" in source_statuses:
                status = "not_configured"
            elif "cache" in source_statuses or "success" in source_statuses:
                status = "empty"
            elif source_optional:
                status = "可选失败"
            else:
                status = "失败"

            source_stats[source_name] = {
                "count": source_count,
                "cabin_counts": cabin_counts,
                "status": status,
                "optional": source_optional,
                "role": source_role,
                "weight": source_weight,
                "route_type": resolved_route_type,
            }

        total_raw = len(all_flights)
        seen = {}
        sources_by_combo = {}
        for flight in all_flights:
            if not _valid_price(flight.get("price")):
                continue
            combo = flight.get("flight_combo", "")
            if not combo:
                continue

            normalized_combo = _flight_key(flight)
            source_name = flight.get("data_source") or flight.get("source")
            new_sources = _source_names(source_name)
            sources_by_combo.setdefault(normalized_combo, [])
            for source in new_sources:
                if source not in sources_by_combo[normalized_combo]:
                    sources_by_combo[normalized_combo].append(source)

            current_flight = seen.get(normalized_combo, {})
            if normalized_combo in seen:
                _append_sources(seen[normalized_combo], new_sources)
                _merge_flight_fields(seen[normalized_combo], flight)
                _append_source_price(seen[normalized_combo], source_name, flight.get("price"))
            if normalized_combo not in seen or _should_replace_flight(
                current_flight, flight, domestic_route
            ):
                previous = seen.get(normalized_combo)
                seen[normalized_combo] = dict(flight)
                _append_source_price(seen[normalized_combo], source_name, flight.get("price"))
                if previous:
                    _merge_flight_fields(seen[normalized_combo], previous)
                _append_sources(seen[normalized_combo], sources_by_combo[normalized_combo])

        unique_flights = list(seen.values())
        for flight in unique_flights:
            flight["collected_at"] = flight.get("collected_at") or run_collected_at
            normalized_combo = _flight_key(flight)
            sources = sources_by_combo.get(normalized_combo, [])
            if sources:
                flight["data_source"] = "+".join(sources)
                flight["primary_source"] = flight.get("primary_source") or _primary_source_for_sources(
                    sources, domestic_route
                )

        unique_flights = sorted(
            unique_flights, key=lambda flight: float(flight.get("price") or 99999)
        )
        dual_source_price_anomalies = _log_dual_source_price_checks(unique_flights)

        enrichment_data = {}
        for source in self.enrichment_sources:
            source_name = getattr(source, "name", type(source).__name__)
            enrichment_count = 0
            enrichment_succeeded = False
            cabin_counts = {}

            for cabin_class in cabin_classes:
                try:
                    result = cached_fetch(
                        source,
                        origin,
                        dest,
                        date_str,
                        passengers,
                        cabin_class,
                        ttl_seconds=15 * 60,
                    )
                    enrichment_flights = result.get("flights", []) or []
                    enrichment_count += len(enrichment_flights)
                    cabin_counts[cabin_class] = len(enrichment_flights)
                    enrichment_succeeded = True
                    print(
                        f"[{source_name}] {cabin_class} 成功，返回 "
                        f"{len(enrichment_flights)} 个行李退改候选"
                    )

                    for flight in enrichment_flights:
                        flight["cabin_class"] = flight.get("cabin_class") or cabin_class
                        combo = _flight_key(flight)
                        if combo and flight.get("extra"):
                            enrichment_data[combo] = flight["extra"]

                except Exception as exc:
                    error = _redact_api_key(str(exc))
                    source_errors.append(
                        {"source": source_name, "cabin_class": cabin_class, "error": error}
                    )
                    print(f"[{source_name}] {cabin_class} 失败：{error}")

            source_stats[source_name] = {
                "count": enrichment_count,
                "cabin_counts": cabin_counts,
                "status": (
                    "成功（仅用于行李退改信息）"
                    if enrichment_succeeded
                    else "失败（行李退改信息不可用）"
                ),
                "role": "enrichment",
                "weight": 0.0,
                "route_type": resolved_route_type,
            }

        enriched_count = 0
        for flight in unique_flights:
            combo = _flight_key(flight)
            if combo in enrichment_data:
                extra = flight.get("extra") or {}
                extra.update(enrichment_data[combo])
                flight["extra"] = extra
                flight["has_baggage_info"] = True
                enriched_count += 1
            else:
                flight["has_baggage_info"] = False

        source_stats["total_raw"] = total_raw
        source_stats["after_dedup"] = len(unique_flights)
        source_stats["after_dedup_by_cabin"] = {
            cabin_class: sum(
                1
                for flight in unique_flights
                if (flight.get("cabin_class") or "economy") == cabin_class
            )
            for cabin_class in cabin_classes
        }
        source_stats["enriched_count"] = enriched_count

        print(
            "source_stats: "
            + json.dumps(source_stats, ensure_ascii=False, indent=2)
        )

        if not unique_flights:
            return None

        sources_used = [
            key
            for key, value in source_stats.items()
            if isinstance(value, dict) and "成功" in value.get("status", "")
        ]

        return {
            "flights": unique_flights,
            "price_insights": price_insights or {},
            "source": "+".join(sources_used),
            "source_stats": source_stats,
            "sources_used": "+".join(sources_used),
            "source_errors": source_errors,
            "price_anomalies": self._find_price_anomalies(successful_results) + dual_source_price_anomalies,
            "raw_by_source": raw_by_source,
            "collected_at": run_collected_at,
        }

    def _ordered_search_sources(
        self, origin: str, dest: str, route_type: str | None = None
    ) -> list[FlightSource]:
        sources = list(self.search_sources)
        resolved_route_type = route_type_for(origin, dest, route_type or self.route_type)
        if resolved_route_type == "domestic" and not any(
            _source_name(source) == "juhe" for source in sources
        ):
            source = _instantiate_source("juhe")
            if source is not None:
                sources.append(source)
        return _apply_route_source_roles(sources, resolved_route_type)

    def _merge_flights(self, results: list[dict], is_domestic: bool = False) -> list[dict]:
        merged_by_combo = {}
        source_order_by_combo = {}

        for result in results:
            source = result.get("source")
            for flight in result.get("flights", []):
                combo = normalize_combo(flight.get("flight_combo", ""))
                if not combo:
                    continue

                data_source = flight.get("data_source") or source
                normalized_flight = {**flight, "flight_combo": combo}
                raw_combo = flight.get("flight_combo")
                if raw_combo and str(raw_combo) != combo:
                    normalized_flight.setdefault("raw_flight_combo", str(raw_combo))
                if combo not in merged_by_combo:
                    merged_by_combo[combo] = {
                        **normalized_flight,
                        "data_source": "+".join(_source_names(data_source)),
                    }
                    source_order_by_combo[combo] = []
                else:
                    if _should_replace_flight(merged_by_combo[combo], flight, is_domestic):
                        previous = merged_by_combo[combo]
                        merged_by_combo[combo] = {
                            **normalized_flight,
                            "data_source": "+".join(_source_names(data_source)),
                        }
                        _merge_flight_fields(merged_by_combo[combo], previous)
                    else:
                        _merge_flight_fields(merged_by_combo[combo], flight)

                for source_name in _source_names(data_source):
                    if source_name not in source_order_by_combo[combo]:
                        source_order_by_combo[combo].append(source_name)

        for combo, sources in source_order_by_combo.items():
            if sources:
                merged_by_combo[combo]["data_source"] = "+".join(sources)
                merged_by_combo[combo]["primary_source"] = merged_by_combo[combo].get(
                    "primary_source"
                ) or _primary_source_for_sources(sources, is_domestic)

        return list(merged_by_combo.values())

    def _find_price_anomalies(self, results: list[dict]) -> list[dict]:
        prices_by_combo = {}
        for result in results:
            source = result.get("source")
            for flight in result.get("flights", []):
                combo = normalize_combo(flight.get("flight_combo", ""))
                price = flight.get("price")
                if not combo or price is None:
                    continue
                prices_by_combo.setdefault(combo, []).append(
                    {
                        "source": source,
                        "flight_combo": flight.get("flight_combo"),
                        "price": float(price),
                    }
                )

        anomalies = []
        for entries in prices_by_combo.values():
            if len(entries) < 2:
                continue

            prices = [entry["price"] for entry in entries]
            min_price = min(prices)
            max_price = max(prices)
            if min_price <= 0:
                continue

            diff_pct = ((max_price - min_price) / min_price) * 100
            if diff_pct > 15:
                anomalies.append(
                    {
                        "flight_combo": entries[0]["flight_combo"],
                        "min_price": min_price,
                        "max_price": max_price,
                        "diff_pct": round(diff_pct, 1),
                        "sources": entries,
                    }
                )

        return anomalies
