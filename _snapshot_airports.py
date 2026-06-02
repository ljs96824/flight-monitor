"""Snapshot current airport helper outputs to before.json."""

from __future__ import annotations

import json
from pathlib import Path

import airports


def main() -> None:
    snapshot = {
        "airports": {},
        "city_airports": {
            city: list(codes)
            for city, codes in sorted(airports.CITY_AIRPORTS.items())
        },
    }

    for code in sorted(airports.AIRPORT_NAMES):
        snapshot["airports"][code] = {
            "get_airport_name": airports.get_airport_name(code),
            "get_airport_short_name": airports.get_airport_short_name(code),
            "get_airport_city": airports.get_airport_city(code),
            "get_airport_city_en": airports.get_airport_city_en(code),
            "get_airport_timezone": airports.get_airport_timezone(code),
            "format_airport": airports.format_airport(code),
        }

    output_path = Path(__file__).with_name("before.json")
    output_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
