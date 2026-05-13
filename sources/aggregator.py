"""Multi-source flight result aggregator."""

from __future__ import annotations

from sources.base import FlightSource


def normalize_combo(combo: str) -> str:
    return combo.replace(" ", "").upper()


class FlightAggregator:
    def __init__(self, sources: list[FlightSource]):
        self.sources = sources

    def collect(
        self, origin: str, dest: str, date_str: str, target_combo: str
    ) -> dict | None:
        successful_results = []
        errors = []

        for source in self.sources:
            try:
                result = source.fetch(origin, dest, date_str)
            except Exception as exc:
                errors.append({"source": source.name, "error": str(exc)})
                continue

            flights = result.get("flights") or []
            if not flights:
                errors.append({"source": source.name, "error": "no flights"})
                continue

            successful_results.append(result)

        if not successful_results:
            return None

        primary = successful_results[0]
        price_insights = None
        for result in successful_results:
            if result.get("price_insights"):
                price_insights = result["price_insights"]
                break

        return {
            "flights": primary.get("flights", []),
            "price_insights": price_insights or {},
            "source": primary.get("source"),
            "sources_used": [result.get("source") for result in successful_results],
            "source_errors": errors,
            "price_anomalies": self._find_price_anomalies(successful_results),
            "raw_by_source": {
                result.get("source"): result.get("raw") for result in successful_results
            },
        }

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
