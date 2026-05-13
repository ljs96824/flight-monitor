"""Base interface for flight data sources."""

from __future__ import annotations


class FlightSource:
    name: str

    def fetch(self, origin: str, dest: str, date_str: str) -> dict:
        """Return standardized flight search results."""
        raise NotImplementedError
