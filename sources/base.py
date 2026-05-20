"""Base interface for flight data sources."""

from __future__ import annotations


class FlightSource:
    name: str

    def fetch(
        self, origin: str, dest: str, date_str: str, cabin_class: str = "economy"
    ) -> dict:
        """Return standardized flight search results."""
        raise NotImplementedError
