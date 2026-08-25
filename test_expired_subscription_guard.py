import logging
import sys
import types
import unittest
from datetime import date
from unittest.mock import patch


sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None),
)
logging.basicConfig = lambda *a, **k: None

import main


def _subscription(**overrides):
    subscription = {
        "_index": 0,
        "origin": "PVG",
        "destination": "ABQ",
        "origin_airports": ["PVG"],
        "origin_airports_active": ["PVG"],
        "destination_airports": ["ABQ"],
        "destination_airports_active": ["ABQ"],
        "depart_date": "2026-07-03",
        "round_trip": False,
        "date_flexibility": 3,
        "cabin_classes": ["economy"],
        "route_type": "international",
    }
    subscription.update(overrides)
    return subscription


class _CountingSource:
    def __init__(self, name, calls):
        self.name = name
        self.calls = calls

    def fetch(self, origin, destination, date_str, cabin_class="economy"):
        self.calls.append((self.name, origin, destination, date_str, cabin_class))
        return {"flights": [], "source": self.name}


class _InvokingAggregator:
    def __init__(self, search_sources, enrichment_sources, route_type=None):
        self.sources = list(search_sources) + list(enrichment_sources)
        self.last_source_errors = []

    def collect(
        self,
        origin,
        destination,
        date_str,
        cabin_classes=None,
        passengers=None,
        **_kwargs,
    ):
        for source in self.sources:
            source.fetch(origin, destination, date_str, "economy")
        return None


class _ErrorAggregator:
    def __init__(self, _search_sources, _enrichment_sources, route_type=None):
        self.last_source_errors = [
            {
                "source": "hasdata",
                "cabin_class": "economy",
                "error": "HTTP 422 api_key=secret",
            },
            {
                "source": "duffel",
                "cabin_class": "economy",
                "error": "HTTP 422 invalid departure date",
            },
        ]

    def collect(
        self,
        origin,
        destination,
        date_str,
        cabin_classes=None,
        passengers=None,
        **_kwargs,
    ):
        return None


class ExpiredSubscriptionGuardTest(unittest.TestCase):
    def test_expired_subscription_skips_before_round_and_all_sources(self):
        calls = []
        sources = [
            _CountingSource("hasdata", calls),
            _CountingSource("juhe", calls),
            _CountingSource("duffel", calls),
        ]

        with (
            patch.object(main, "_shanghai_today", return_value=date(2026, 7, 21), create=True),
            patch("main.build_default_sources", return_value=(sources[:2], sources[2:])),
            patch("main.FlightAggregator", _InvokingAggregator),
            patch("main.set_current_round") as set_round,
            patch("main.start_request_cache_round") as start_round,
            patch("main.print_request_cache_stats"),
            patch("main.safe_log") as log,
        ):
            ok = main.process_subscription(_subscription(), ensure_db=False)

        self.assertTrue(ok)
        self.assertEqual(calls, [])
        set_round.assert_not_called()
        start_round.assert_not_called()
        lines = [str(call.args[0]) for call in log.call_args_list]
        skip_line = next(line for line in lines if line.startswith("[订阅前置校验]"))
        self.assertIn("订阅=0", skip_line)
        self.assertIn("航线=PVG->ABQ", skip_line)
        self.assertIn("结果=跳过", skip_line)
        self.assertIn("最晚=2026-07-06", skip_line)

    def test_departure_today_is_not_skipped(self):
        evaluator = getattr(main, "evaluate_subscription_preflight", None)
        self.assertIsNotNone(evaluator)

        result = evaluator(
            _subscription(depart_date="2026-07-21", date_flexibility=0),
            today=date(2026, 7, 21),
        )

        self.assertFalse(result["skip"])
        self.assertEqual(result["latest_date"], date(2026, 7, 21))

    def test_partially_future_flexible_dates_are_not_skipped(self):
        evaluator = getattr(main, "evaluate_subscription_preflight", None)
        self.assertIsNotNone(evaluator)

        result = evaluator(
            _subscription(depart_date="2026-07-20", date_flexibility=3),
            today=date(2026, 7, 21),
        )

        self.assertFalse(result["skip"])
        self.assertGreater(result["latest_date"], date(2026, 7, 21))

    def test_two_source_errors_reach_subscription_failure_line(self):
        with (
            patch.object(main, "_shanghai_today", return_value=date(2026, 7, 21), create=True),
            patch("main.build_default_sources", return_value=([object()], [object()])),
            patch("main.FlightAggregator", _ErrorAggregator),
            patch("main.set_current_round"),
            patch("main.start_request_cache_round"),
            patch("main.reset_current_round"),
            patch("main.print_request_cache_stats"),
            patch("main.safe_log") as log,
        ):
            ok = main.process_subscription(
                _subscription(depart_date="2026-08-03", date_flexibility=0),
                ensure_db=False,
                manage_collection_round=False,
            )

        self.assertFalse(ok)
        lines = [str(call.args[0]) for call in log.call_args_list]
        failure_line = next(line for line in lines if line.startswith("[订阅处理失败]"))
        self.assertIn("订阅=0", failure_line)
        self.assertIn("航线=PVG->ABQ", failure_line)
        self.assertIn("hasdata:HTTP 422 api_key=***", failure_line)
        self.assertIn("duffel:HTTP 422 invalid departure date", failure_line)
        self.assertNotIn("未知", failure_line)

    def test_single_subscription_path_prints_preflight_summary(self):
        with (
            patch.object(main, "_shanghai_today", return_value=date(2026, 7, 21), create=True),
            patch("main.safe_log") as log,
        ):
            ok = main.process_subscription(_subscription(), ensure_db=False)

        self.assertTrue(ok)
        lines = [str(call.args[0]) for call in log.call_args_list]
        self.assertTrue(
            any(line.startswith("[订阅前置校验] 本轮检查=1 跳过=1") for line in lines)
        )

    def test_failed_web_trigger_sends_short_failure_notification(self):
        subscription = _subscription(
            depart_date="2026-08-03",
            date_flexibility=0,
            notification_goals={"method": "pushplus"},
        )
        with (
            patch.object(main, "_shanghai_today", return_value=date(2026, 7, 21), create=True),
            patch("main.build_default_sources", return_value=([object()], [])),
            patch("main.FlightAggregator", _ErrorAggregator),
            patch("main.set_current_round"),
            patch("main.start_request_cache_round"),
            patch("main.reset_current_round"),
            patch("main.print_request_cache_stats"),
            patch("main.send", return_value=True) as push_send,
        ):
            ok = main.process_subscription(
                subscription,
                ensure_db=False,
                web_trigger=True,
                manage_collection_round=False,
            )

        self.assertFalse(ok)
        content = push_send.call_args.args[0]
        self.assertIn("本次采集失败", content)
        self.assertIn("hasdata:HTTP 422 api_key=***", content)
        self.assertIn("订阅已保留,下轮自动重试", content)


if __name__ == "__main__":
    unittest.main()
