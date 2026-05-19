"""Multi-source flight result aggregator."""

from __future__ import annotations

import os

from sources.base import FlightSource


def normalize_combo(combo: str) -> str:
    return combo.replace(" ", "").upper()


def build_default_sources() -> tuple[list[FlightSource], list[FlightSource]]:
    """分别构建搜索源和补充源。"""
    search_sources = []
    enrichment_sources = []
    if os.environ.get("SERPAPI_KEY"):
        from sources.serpapi_source import SerpAPISource

        search_sources.append(SerpAPISource())
    if os.environ.get("SEARCHAPI_KEY"):
        from sources.searchapi_source import SearchAPISource

        search_sources.append(SearchAPISource())
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
        self, origin: str, dest: str, date_str: str, target_combo: str | None = None
    ) -> dict | None:
        source_stats = {}
        all_flights = []
        successful_results = []
        source_errors = []
        price_insights = None
        raw_by_source = {}

        for source in self.search_sources:
            source_name = getattr(source, "name", type(source).__name__)
            try:
                result = source.fetch(origin, dest, date_str)
                flights = result.get("flights", []) or []

                for flight in flights:
                    if not flight.get("source"):
                        flight["source"] = source_name
                    if not flight.get("data_source"):
                        flight["data_source"] = source_name

                source_stats[source_name] = {
                    "count": len(flights),
                    "status": "成功",
                }
                all_flights.extend(flights)
                successful_results.append(
                    {
                        **result,
                        "source": source_name,
                        "flights": flights,
                    }
                )
                raw_by_source[source_name] = result.get("raw")
                print(f"[{source_name}] 成功，返回 {len(flights)} 个方案")

                if not price_insights and result.get("price_insights"):
                    price_insights = result["price_insights"]

            except Exception as exc:
                error = str(exc)
                source_stats[source_name] = {
                    "count": 0,
                    "status": "失败",
                }
                source_errors.append({"source": source_name, "error": error})
                print(f"[{source_name}] 失败：{error}")

        total_raw = len(all_flights)
        seen = {}
        sources_by_combo = {}
        for flight in all_flights:
            combo = flight.get("flight_combo", "")
            if not combo:
                continue

            normalized_combo = normalize_combo(combo)
            source_name = flight.get("data_source") or flight.get("source")
            sources_by_combo.setdefault(normalized_combo, [])
            if source_name and source_name not in sources_by_combo[normalized_combo]:
                sources_by_combo[normalized_combo].append(source_name)

            if normalized_combo not in seen or flight.get("price", 99999) < seen[
                normalized_combo
            ].get("price", 99999):
                seen[normalized_combo] = flight

        unique_flights = list(seen.values())
        for flight in unique_flights:
            normalized_combo = normalize_combo(flight.get("flight_combo", ""))
            sources = sources_by_combo.get(normalized_combo, [])
            if sources:
                flight["data_source"] = "+".join(sources)

        unique_flights = sorted(
            unique_flights, key=lambda flight: flight.get("price", 99999)
        )

        enrichment_data = {}
        for source in self.enrichment_sources:
            source_name = getattr(source, "name", type(source).__name__)
            try:
                result = source.fetch(origin, dest, date_str)
                enrichment_flights = result.get("flights", []) or []
                source_stats[source_name] = {
                    "count": len(enrichment_flights),
                    "status": "成功（仅用于行李退改信息）",
                }
                print(
                    f"[{source_name}] 成功，返回 {len(enrichment_flights)} 个行李退改候选"
                )

                for flight in enrichment_flights:
                    combo = normalize_combo(flight.get("flight_combo", ""))
                    if combo and flight.get("extra"):
                        enrichment_data[combo] = flight["extra"]

            except Exception as exc:
                error = str(exc)
                source_stats[source_name] = {
                    "count": 0,
                    "status": "失败（行李退改信息不可用）",
                }
                source_errors.append({"source": source_name, "error": error})
                print(f"[{source_name}] 失败：{error}")

        enriched_count = 0
        for flight in unique_flights:
            combo = normalize_combo(flight.get("flight_combo", ""))
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
                    merged_by_combo[combo] = {**flight, "data_source": data_source}
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
                        merged_by_combo[combo] = {**flight, "data_source": data_source}

                if data_source and data_source not in source_order_by_combo[combo]:
                    source_order_by_combo[combo].append(data_source)

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
