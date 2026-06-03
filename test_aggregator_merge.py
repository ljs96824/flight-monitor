from sources.aggregator import FlightAggregator


def test_merge_flights_keeps_lower_price_and_richer_segments():
    aggregator = FlightAggregator([], [])
    sparse_cheaper = {
        "flight_combo": "CA1234",
        "price": 1800,
        "data_source": "hasdata",
        "segments": [],
    }
    rich_more_expensive = {
        "flight_combo": "CA1234",
        "price": 1900,
        "data_source": "serpapi",
        "segments": [
            {
                "flight_no": "CA1234",
                "airline": "中国国际航空",
                "aircraft": "Airbus A321",
                "dep_airport": "PVG",
                "dep_time": "2026-10-01 08:30",
                "arr_airport": "PEK",
                "arr_time": "2026-10-01 10:45",
            }
        ],
        "layovers": [],
    }

    merged = aggregator._merge_flights(
        [
            {"source": "hasdata", "flights": [sparse_cheaper]},
            {"source": "serpapi", "flights": [rich_more_expensive]},
        ]
    )

    assert len(merged) == 1
    flight = merged[0]
    assert flight["price"] == 1800
    assert flight["segments"][0]["flight_no"] == "CA1234"
    assert flight["segments"][0]["aircraft"] == "Airbus A321"
    assert flight["segments"][0]["dep_time"] == "2026-10-01 08:30"
    assert flight["data_source"] == "hasdata+serpapi"


if __name__ == "__main__":
    test_merge_flights_keeps_lower_price_and_richer_segments()
