import sys
import types
import unittest
from unittest.mock import patch


sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(get=lambda *args, **kwargs: None, post=lambda *args, **kwargs: None),
)

from notifier import build_notification_payload, render_detail_html, render_email, render_pushplus


DISCLOSURE = (
    "渠道参考价:Google CNY12137 / OTA CNY4153,"
    "渠道价差较大,运价条款可能不同,以支付页为准"
)


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

    def test_small_source_price_gap_is_not_disclosed(self):
        payload = self._payload(10.0)

        _subject, email_html = render_email(payload)
        detail_html = render_detail_html(payload)
        pushplus_html = render_pushplus(payload)

        self.assertNotIn("渠道参考价:Google", email_html)
        self.assertNotIn("渠道参考价:Google", detail_html)
        self.assertNotIn("渠道参考价:Google", pushplus_html)


if __name__ == "__main__":
    unittest.main()
