"""Multi-source flight result aggregator."""

from __future__ import annotations

import os

from sources.base import FlightSource


def normalize_combo(combo: str) -> str:
    return combo.replace(" ", "").upper()


def build_default_sources() -> list[FlightSource]:
    sources = []
    if os.environ.get("SERPAPI_KEY"):
        from sources.serpapi_source import SerpAPISource

        sources.append(SerpAPISource())
    if os.environ.get("SEARCHAPI_KEY"):
        from sources.searchapi_source import SearchAPISource

        sources.append(SearchAPISource())
    if os.environ.get("DUFFEL_TOKEN"):
        from sources.duffel_source import DuffelSource

        sources.append(DuffelSource())
    return sources


class FlightAggregator:
    def __init__(self, sources: list[FlightSource]):
        self.sources = sources

    def collect(
        self, origin: str, dest: str, date_str: str, target_combo: str
    ) -> dict | None:
        successful_results = []
        errors = []
        source_stats = {}

        for source in self.sources:
            try:
                result = source.fetch(origin, dest, date_str)
            except Exception as exc:
                error = str(exc)
                print(f"[{source.name}] 失败：{error}")
                errors.append({"source": source.name, "error": error})
                source_stats[source.name] = {
                    "count": 0,
                    "status": f"失败: {error[:50]}",
                }
                continue

            flights = result.get("flights") or []
            if not flights:
                print(f"[{source.name}] 失败：no flights")
                errors.append({"source": source.name, "error": "no flights"})
                source_stats[source.name] = {
                    "count": 0,
                    "status": "失败: no flights",
                }
                continue

            print(f"[{source.name}] 成功，返回{len(flights)}个航班")
            source_stats[source.name] = {
                "count": len(flights),
                "status": "成功",
            }
            successful_results.append(result)

        if not successful_results:
            return None

        price_insights = None
        for result in successful_results:
            if result.get("price_insights"):
                price_insights = result["price_insights"]
                break

        sources_used = [result.get("source") for result in successful_results]
        merged_flights = self._merge_flights(successful_results)
        source_stats["total_raw"] = sum(
            info.get("count", 0)
            for info in source_stats.values()
            if isinstance(info, dict) and info.get("status") == "成功"
        )
        source_stats["after_dedup"] = len(merged_flights)
        return {
            "flights": merged_flights,
            "price_insights": price_insights or {},
            "source": "+".join(source for source in sources_used if source),
            "sources_used": sources_used,
            "source_errors": errors,
            "source_stats": source_stats,
            "price_anomalies": self._find_price_anomalies(successful_results),
            "raw_by_source": {
                result.get("source"): result.get("raw") for result in successful_results
            },
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
