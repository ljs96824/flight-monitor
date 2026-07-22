import unittest
from datetime import date
from unittest.mock import MagicMock, call, patch


def _subscription(index):
    return {
        "_index": index,
        "origin": "上海",
        "destination": "北京",
        "origin_airports_active": ["SHA"],
        "destination_airports_active": ["PEK"],
        "depart_date": "2026-08-20",
        "round_trip": False,
        "route_type": "domestic",
    }


class FakePlan:
    request_keys = {("juhe", "SHA", "PEK", "2026-08-20", "1_0_0_0", "economy")}

    def __init__(self, events):
        self.events = events

    def log_summary(self, **kwargs):
        self.events.append(("summary", kwargs))

    def execute(self):
        self.events.append(("execute", {}))
        return MagicMock(actual_requests=1)


class CollectionOrchestrationTest(unittest.TestCase):
    def test_main_plans_all_active_subscriptions_before_processing_any(self):
        import main

        subscriptions = [_subscription(1), _subscription(2)]
        preflight = {"skip": False, "collection_dates": [date(2026, 8, 20)]}
        events = []
        plan = FakePlan(events)

        def process_side_effect(sub, **kwargs):
            events.append(("process", sub["_index"]))
            self.assertFalse(kwargs["manage_collection_round"])
            return True

        with (
            patch("main.init_db"),
            patch("main.load_file_subscriptions", return_value=subscriptions),
            patch("main.evaluate_subscription_preflight", return_value=preflight),
            patch("main.build_collection_plan", return_value=plan) as build_plan,
            patch("main.process_subscription", side_effect=process_side_effect),
            patch("main.activate_collection_plan") as activate,
            patch("main.deactivate_collection_plan") as deactivate,
            patch("main.set_current_round"),
            patch("main.clear_current_round"),
            patch("main.start_request_cache_round"),
            patch("main.print_request_cache_stats"),
            patch("main._shanghai_today", return_value=date(2026, 7, 22)),
            patch("main._make_collection_round_id", return_value="collection_test"),
            patch(
                "main._collection_plan_log_options",
                return_value={
                    "quota_budgets": {"juhe": 550},
                    "quota_low_remaining_threshold": 50,
                    "usage_snapshot": {"today": {}, "cumulative": {}},
                },
            ),
        ):
            main.run(sync_remote=False)

        build_plan.assert_called_once()
        self.assertEqual(build_plan.call_args.kwargs["subscriptions"], subscriptions)
        activate.assert_called_once_with(plan.request_keys)
        deactivate.assert_called_once()
        self.assertEqual([event[0] for event in events], ["summary", "execute", "process", "process"])

    def test_single_subscription_path_prepares_its_own_plan(self):
        import main

        plan = FakePlan([])
        empty_data = {"flights": []}
        with (
            patch("main.evaluate_subscription_preflight", return_value={"skip": False}),
            patch("main.init_db"),
            patch("main._make_round_id", return_value="single_test"),
            patch("main.set_current_round"),
            patch("main.clear_current_round"),
            patch("main.start_request_cache_round"),
            patch("main.print_request_cache_stats"),
            patch("main.build_collection_plan", return_value=plan) as build_plan,
            patch("main.activate_collection_plan") as activate,
            patch("main.deactivate_collection_plan") as deactivate,
            patch("main._collection_plan_log_options", return_value={}),
            patch("main.build_default_sources", return_value=([], [])),
            patch("main.collect_for_airport_matrix", return_value=empty_data),
            patch("main._log_subscription_failure"),
        ):
            ok = main.process_subscription(_subscription(1), ensure_db=False)

        self.assertFalse(ok)
        build_plan.assert_called_once()
        activate.assert_called_once_with(plan.request_keys)
        deactivate.assert_called_once()

    def test_preflight_failure_isolated_before_plan_build(self):
        import main

        subscriptions = [_subscription(1), _subscription(2)]
        plan = FakePlan([])

        def preflight_side_effect(sub, today=None):
            if sub["_index"] == 1:
                raise ValueError("bad preflight")
            return {"skip": False, "collection_dates": [date(2026, 8, 20)]}

        with (
            patch("main.init_db"),
            patch("main.load_file_subscriptions", return_value=subscriptions),
            patch("main.evaluate_subscription_preflight", side_effect=preflight_side_effect),
            patch("main._log_subscription_failure") as log_failure,
            patch("main.build_collection_plan", return_value=plan) as build_plan,
            patch("main.process_subscription", return_value=True),
            patch("main.activate_collection_plan"),
            patch("main.deactivate_collection_plan"),
            patch("main.set_current_round"),
            patch("main.clear_current_round"),
            patch("main.start_request_cache_round"),
            patch("main.print_request_cache_stats"),
            patch("main._shanghai_today", return_value=date(2026, 7, 22)),
            patch("main._make_collection_round_id", return_value="collection_test"),
            patch("main._collection_plan_log_options", return_value={}),
        ):
            main.run(sync_remote=False)

        self.assertEqual(build_plan.call_args.kwargs["subscriptions"], [subscriptions[1]])
        log_failure.assert_called_once()
        self.assertIn("bad preflight", log_failure.call_args.kwargs["reason"])


if __name__ == "__main__":
    unittest.main()
