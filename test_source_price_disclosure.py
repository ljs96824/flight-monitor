import io
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch


sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(get=lambda *args, **kwargs: None, post=lambda *args, **kwargs: None),
)

from notifier import (
    _display_channel_price_rows,
    build_notification_payload,
    render_detail_html,
    render_email,
    render_pushplus,
)


DISCLOSURE = "渠道价差&gt;15%"


class SourcePriceDisclosureTest(unittest.TestCase):
    def _payload(self, diff_pct):
        outbound = {
            "flight_combo": "MU730",
            "flight_no": "MU730",
            "price": 4153,
            "price_source": "juhe",
            "data_source": "hasdata+juhe",
            "departure_airport": "PVG",
            "arrival_airport": "KIX",
            "departure_time": "2026-10-01 18:10",
            "arrival_time": "2026-10-01 21:20",
            "stops": 0,
            "source_price_details": [
                {"source": "hasdata", "price": 12137},
                {"source": "juhe", "price": 4153},
            ],
        }
        return_flight = {
            "flight_combo": "JL891",
            "flight_no": "JL891",
            "price": 7220,
            "price_source": "juhe",
            "data_source": "hasdata+juhe",
            "departure_airport": "KIX",
            "arrival_airport": "PVG",
            "departure_time": "2026-10-06 10:10",
            "arrival_time": "2026-10-06 12:05",
            "stops": 0,
            "source_price_details": [
                {"source": "hasdata", "price": 7268},
                {"source": "juhe", "price": 7220},
            ],
        }
        analysis = {
            "round_trip_analysis": {
                "top_combinations": [
                    {
                        "outbound": outbound,
                        "return": return_flight,
                        "outbound_price": 4153,
                        "return_price": 7220,
                        "total_price": 11373,
                    }
                ]
            },
            "dual_source_price_anomalies": [
                {
                    "flight_combo": "MU730",
                    "min_price": 4153,
                    "max_price": 12137,
                    "diff_pct": diff_pct,
                    "sources": [
                        {"source": "hasdata", "price": 12137},
                        {"source": "juhe", "price": 4153},
                    ],
                }
            ],
        }
        with (
            patch("notifier.get_last_push_price", return_value=None),
            patch("notifier.get_last_push_snapshot", return_value=None),
            patch("notifier.track_plan_status", return_value=None),
        ):
            return build_notification_payload(
                analysis,
                route_info={
                    "round_trip": True,
                    "origin": "PVG",
                    "destination": "KIX",
                    "depart_date": "2026-10-01",
                    "return_date": "2026-10-06",
                },
                subscription={
                    "id": "source-gap-disclosure",
                    "basic": {"route_type": "international"},
                    "preferences": {
                        "passengers": {"adult": 1, "child": 0, "elderly": 0, "infant": 0}
                    },
                },
            )

    def test_large_source_price_gap_is_disclosed_in_all_channels(self):
        payload = self._payload(192.2)

        _subject, email_html = render_email(payload)
        detail_html = render_detail_html(payload)
        pushplus_html = render_pushplus(payload)

        self.assertIn(DISCLOSURE, email_html)
        self.assertIn(DISCLOSURE, detail_html)
        self.assertIn(DISCLOSURE, pushplus_html)
        self.assertEqual(email_html.count(DISCLOSURE), 1)
        self.assertEqual(detail_html.count(DISCLOSURE), 1)
        self.assertEqual(pushplus_html.count(DISCLOSURE), 1)

    def test_small_source_price_gap_is_not_disclosed(self):
        payload = self._payload(10.0)

        _subject, email_html = render_email(payload)
        detail_html = render_detail_html(payload)
        pushplus_html = render_pushplus(payload)

        self.assertNotIn(DISCLOSURE, email_html)
        self.assertNotIn(DISCLOSURE, detail_html)
        self.assertNotIn(DISCLOSURE, pushplus_html)

    def test_primary_roundtrip_plan_always_shows_leg_source_prices(self):
        outbound = {
            "flight_combo": "MU225",
            "flight_no": "MU225",
            "price": 4954,
            "price_source": "juhe",
            "data_source": "hasdata+juhe",
            "departure_airport": "PVG",
            "arrival_airport": "KIX",
            "departure_time": "2026-10-01 09:00",
            "arrival_time": "2026-10-01 12:00",
            "stops": 0,
            "source_price_details": [
                {"source": "hasdata", "price": 5131},
                {"source": "juhe", "price": 4954},
            ],
        }
        return_flight = {
            "flight_combo": "JL891",
            "flight_no": "JL891",
            "price": 7220,
            "price_source": "juhe",
            "data_source": "hasdata+juhe",
            "departure_airport": "KIX",
            "arrival_airport": "PVG",
            "departure_time": "2026-10-06 10:10",
            "arrival_time": "2026-10-06 12:05",
            "stops": 0,
            "source_price_details": [
                {"source": "hasdata", "price": 7268},
                {"source": "juhe", "price": 7220},
            ],
        }
        analysis = {
            "round_trip_analysis": {
                "top_combinations": [
                    {
                        "outbound": outbound,
                        "return": return_flight,
                        "outbound_price": 4954,
                        "return_price": 7220,
                        "total_price": 12174,
                    }
                ]
            }
        }
        with (
            patch("notifier.get_last_push_price", return_value=None),
            patch("notifier.get_last_push_snapshot", return_value=None),
            patch("notifier.track_plan_status", return_value=None),
        ):
            payload = build_notification_payload(
                analysis,
                route_info={
                    "round_trip": True,
                    "origin": "PVG",
                    "destination": "KIX",
                    "depart_date": "2026-10-01",
                    "return_date": "2026-10-06",
                },
                subscription={
                    "id": "source-channel-block",
                    "basic": {"route_type": "international"},
                    "preferences": {
                        "passengers": {"adult": 1, "child": 0, "elderly": 0, "infant": 0}
                    },
                },
            )

        rows = payload["channel_price_rows"]
        by_leg_source = {
            (row.get("direction"), row.get("source")): (
                row.get("value"),
                row.get("selected"),
            )
            for row in rows
        }
        self.assertEqual(
            by_leg_source,
            {
                ("outbound", "hasdata"): (5131, False),
                ("outbound", "juhe"): (4954, True),
                ("return", "hasdata"): (7268, False),
                ("return", "juhe"): (7220, True),
            },
        )

        expected_outbound = "去程 MU225:Google CNY5,131 / OTA CNY4,954(入池OTA)"
        expected_return = "返程 JL891:Google CNY7,268 / OTA CNY7,220(入池OTA)"
        _subject, email_html = render_email(payload)
        detail_html = render_detail_html(payload)
        pushplus_html = render_pushplus(payload)
        for rendered in (email_html, detail_html, pushplus_html):
            self.assertIn(expected_outbound, rendered)
            self.assertIn(expected_return, rendered)

    def test_empty_channel_rows_do_not_emit_idle_diagnostic(self):
        output = io.StringIO()
        with redirect_stdout(output):
            rows = _display_channel_price_rows(
                {"is_roundtrip": True, "channel_price_rows": []}
            )

        self.assertEqual(rows, [])
        self.assertNotIn("[渠道对比]", output.getvalue())


if __name__ == "__main__":
    unittest.main()
