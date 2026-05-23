"""Multi-source flight result aggregator."""

from __future__ import annotations

import os

from sources.base import FlightSource

OPTIONAL_SOURCE_THRESHOLD = 8
SOURCE_DISPLAY_NAMES = {
    "serpapi": "SerpAPI",
    "searchapi": "SearchAPI",
    "hasdata": "HasData",
    "duffel": "Duffel",
}


def normalize_combo(combo: str) -> str:
    return combo.replace(" ", "").upper()


def _redact_api_key(text: str) -> str:
    return text.split("api_key=")[0] + "api_key=***" if "api_key=" in text else text


def _source_display_name(source: str | None) -> str:
    if not source:
        return ""
    return SOURCE_DISPLAY_NAMES.get(source.lower(), source)


def _source_names(value: str | None) -> list[str]:
    return [
        _source_display_name(source.strip())
        for source in str(value or "").split("+")
        if source.strip()
    ]


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


def build_default_sources() -> tuple[list[FlightSource], list[FlightSource]]:
    """Build search sources and enrichment sources separately."""
    search_sources = []
    enrichment_sources = []

    if os.environ.get("SERPAPI_KEY"):
        from sources.serpapi_source import SerpAPISource

        search_sources.append(SerpAPISource())

    if os.environ.get("SEARCHAPI_KEY"):
        from sources.searchapi_source import SearchAPISource

        search_sources.append(SearchAPISource())

    if os.environ.get("HASDATA_KEY"):
        from sources.hasdata_source import HasDataSource

        search_sources.append(HasDataSource())

    if os.environ.get("DUFFEL_TOKEN"):
        from sources.duffel_source import DuffelSource

        enrichment_sources.append(DuffelSource())

    return search_sources, enrichment_sources


class FlightAggregator:
    def __init__(
        self,
        search_sources: list[FlightSource],
        enrichment_sources: list[FlightSource] | None = None,
    ):
        self.search_sources = search_sources
        self.enrichment_sources = enrichment_sources or []

    def collect(
        self,
        origin: str,
        dest: str,
        date_str: str,
        target_combo: str | None = None,
        cabin_classes=None,
    ) -> dict | None:
        cabin_classes = _normalize_cabin_classes(cabin_classes)
        source_stats = {}
        all_flights = []
        successful_results = []
        source_errors = []
        price_insights = None
        raw_by_source = {}

        for source in self.search_sources:
            source_name = getattr(source, "name", type(source).__name__)
            source_optional = len(all_flights) >= OPTIONAL_SOURCE_THRESHOLD
            source_count = 0
            source_succeeded = False
            cabin_counts = {}

            for cabin_class in cabin_classes:
                try:
                    result = source.fetch(origin, dest, date_str, cabin_class)
                    flights = result.get("flights", []) or []

                    for flight in flights:
                        if not flight.get("source"):
                            flight["source"] = source_name
                        if not flight.get("data_source"):
                            flight["data_source"] = source_name
                        flight["cabin_class"] = flight.get("cabin_class") or cabin_class

                    source_count += len(flights)
                    cabin_counts[cabin_class] = len(flights)
                    source_succeeded = True
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
            elif source_optional:
                status = "可选失败"
            else:
                status = "失败"

            source_stats[source_name] = {
                "count": source_count,
                "cabin_counts": cabin_counts,
                "status": status,
                "optional": source_optional,
            }

        total_raw = len(all_flights)
        seen = {}
        sources_by_combo = {}
        for flight in all_flights:
            combo = flight.get("flight_combo", "")
            if not combo:
                continue

            normalized_combo = _flight_key(flight)
            source_name = flight.get("data_source") or flight.get("source")
            sources_by_combo.setdefault(normalized_combo, [])
            for source in _source_names(source_name):
                if source not in sources_by_combo[normalized_combo]:
                    sources_by_combo[normalized_combo].append(source)

            current_price = seen.get(normalized_combo, {}).get("price", 99999)
            if normalized_combo not in seen or flight.get("price", 99999) < current_price:
                seen[normalized_combo] = flight

        unique_flights = list(seen.values())
        for flight in unique_flights:
            normalized_combo = _flight_key(flight)
            sources = sources_by_combo.get(normalized_combo, [])
            if sources:
                flight["data_source"] = "+".join(sources)

        unique_flights = sorted(
            unique_flights, key=lambda flight: flight.get("price", 99999)
        )

        enrichment_data = {}
        for source in self.enrichment_sources:
            source_name = getattr(source, "name", type(source).__name__)
            enrichment_count = 0
            enrichment_succeeded = False
            cabin_counts = {}

            for cabin_class in cabin_classes:
                try:
                    result = source.fetch(origin, dest, date_str, cabin_class)
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
            "price_anomalies": self._find_price_anomalies(successful_results),
            "raw_by_source": raw_by_source,
        }

    def _merge_flights(self, results: list[dict]) -> list[dict]:
        merged_by_combo = {}
        source_order_by_combo = {}

        for result in results:
            source = result.get("source")
            for flight in result.get("flights", []):
                combo = normalize_combo(flight.get("flight_combo", ""))
                if not combo:
                    continue

                data_source = flight.get("data_source") or source
                if combo not in merged_by_combo:
                    merged_by_combo[combo] = {
                        **flight,
                        "data_source": "+".join(_source_names(data_source)),
                    }
                    source_order_by_combo[combo] = []
                else:
                    current_price = merged_by_combo[combo].get("price")
                    new_price = flight.get("price")
                    if (
                        new_price is not None
                        and (
                            current_price is None
                            or float(new_price) < float(current_price)
                        )
                    ):
                        merged_by_combo[combo] = {
                            **flight,
                            "data_source": "+".join(_source_names(data_source)),
                        }

                for source_name in _source_names(data_source):
                    if source_name not in source_order_by_combo[combo]:
                        source_order_by_combo[combo].append(source_name)

        for combo, sources in source_order_by_combo.items():
            if sources:
                merged_by_combo[combo]["data_source"] = "+".join(sources)

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
