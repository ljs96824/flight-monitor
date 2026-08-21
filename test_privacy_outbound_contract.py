import os
import unittest
from unittest.mock import patch

from privacy_contracts import assert_no_passenger_composition
from sources.duffel_source import DuffelSource
from sources.juhe_source import JuheSource
from sources.serpapi_source import SerpAPISource


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"data": {"offers": []}}


class _GoogleSearch:
    captured = None

    def __init__(self, params):
        type(self).captured = dict(params)

    def get_dict(self):
        return {"best_flights": [], "other_flights": []}


class PrivacyOutboundContractTest(unittest.TestCase):
    def test_juhe_request_contains_no_subscription_passenger_composition(self):
        params = JuheSource().build_request_params(
            "PVG", "KIX", "2026-10-01", "test-key"
        )
        assert_no_passenger_composition(params, source="juhe")

    def test_serpapi_request_contains_no_subscription_passenger_composition(self):
        _GoogleSearch.captured = None
        with patch(
            "sources.serpapi_source.resolve_serpapi_key",
            return_value=("test-key", "SERPAPI_KEY"),
        ), patch("serpapi.GoogleSearch", _GoogleSearch):
            SerpAPISource().fetch("PVG", "KIX", "2026-10-01", "business")

        self.assertIsNotNone(_GoogleSearch.captured)
        assert_no_passenger_composition(_GoogleSearch.captured, source="serpapi")

    def test_duffel_uses_fixed_single_adult_without_subscription_composition(self):
        captured = {}

        def fake_post(_url, *, json, headers, timeout):
            captured["payload"] = json
            return _Response()

        with patch.dict(os.environ, {"DUFFEL_TOKEN": "test-token"}), patch(
            "sources.duffel_source.httpx.post", side_effect=fake_post
        ):
            DuffelSource().fetch("PVG", "KIX", "2026-10-01", "economy")

        self.assertEqual(
            captured["payload"]["data"]["passengers"],
            [{"type": "adult"}],
        )
        assert_no_passenger_composition(captured["payload"], source="duffel")


if __name__ == "__main__":
    unittest.main()
