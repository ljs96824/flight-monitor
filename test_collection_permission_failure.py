import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class AlwaysPermissionErrorSource:
    name = "juhe"
    role = "search"
    route_type = "international"

    def __init__(self, denied_path: Path):
        self.calls = 0
        self.denied_path = denied_path

    def fetch(self, origin, dest, date_str, cabin_class="economy"):
        self.calls += 1
        error = PermissionError(13, "拒绝访问。", str(self.denied_path))
        error.winerror = 5
        raise error


class CollectionPermissionFailureTest(unittest.TestCase):
    def setUp(self):
        from request_cache import reset_for_tests

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._cache_dir = Path(self._tmp.name) / self._testMethodName
        reset_for_tests(self._cache_dir)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        from request_cache import reset_for_tests

        reset_for_tests(None)
        self._tmp.cleanup()

    def test_juhe_private_cache_is_anchored_to_project(self):
        from sources.juhe_source import _cache_dir

        cache_dir = _cache_dir()

        self.assertTrue(cache_dir.is_absolute())
        self.assertEqual(cache_dir.name, "cache")
        self.assertEqual(cache_dir.parent.name, "data")
        self.assertEqual(cache_dir.parent.parent, Path(__file__).resolve().parent)

    def test_juhe_cache_write_retries_local_io_once_without_losing_flights(self):
        from sources.juhe_source import JuheSource

        error = PermissionError(13, "拒绝访问。", "data")
        with patch(
            "sources.juhe_source._write_cache_once",
            side_effect=[error, None],
        ) as write_once, patch("sources.juhe_source.time.sleep") as sleep, patch(
            "sources.juhe_source.safe_log"
        ) as log:
            stored = JuheSource()._write_cache(
                "KIX",
                "PVG",
                "2026-10-06",
                "economy",
                {"result": {"flightInfo": [{"flightNo": "MU516"}]}},
            )

        self.assertTrue(stored)
        self.assertEqual(write_once.call_count, 2)
        sleep.assert_called_once()
        self.assertTrue(any("[缓存写入重试]" in call.args[0] for call in log.call_args_list))

    def test_juhe_cache_write_failure_after_retry_is_nonfatal(self):
        from sources.juhe_source import JuheSource

        error = PermissionError(13, "拒绝访问。", "data")
        with patch(
            "sources.juhe_source._write_cache_once",
            side_effect=[error, error],
        ) as write_once, patch("sources.juhe_source.time.sleep"), patch(
            "sources.juhe_source.safe_log"
        ) as log:
            stored = JuheSource()._write_cache(
                "KIX",
                "PVG",
                "2026-10-06",
                "economy",
                {"result": {"flightInfo": [{"flightNo": "MU516"}]}},
            )

        self.assertFalse(stored)
        self.assertEqual(write_once.call_count, 2)
        self.assertTrue(any("[缓存写入失败]" in call.args[0] for call in log.call_args_list))
        self.assertTrue(any("已保留本轮采集结果" in call.args[0] for call in log.call_args_list))

    def test_permission_error_retries_once_then_becomes_explicit_source_failure(self):
        from request_cache import cached_fetch, get_request_cache_stats

        denied_path = Path(self._tmp.name) / "private" / "cache.json"
        source = AlwaysPermissionErrorSource(denied_path)
        logs = []

        with patch("request_cache.safe_log", side_effect=logs.append), patch(
            "request_cache.time.sleep"
        ) as sleep:
            result, cache_status = cached_fetch(
                source,
                "KIX",
                "PVG",
                "2026-10-06",
                cabin_class="economy",
                ttl_seconds=0,
                persist=False,
                force_fresh=True,
                include_cache_status=True,
            )

        self.assertEqual(source.calls, 2)
        sleep.assert_called_once()
        self.assertEqual(cache_status, "fresh")
        self.assertEqual(result["source_status"], "failed")
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(result["errno"], 5)
        self.assertIn("PermissionError", result["error"])
        self.assertIn("errno=5", result["error"])
        self.assertIn("path=<local>/cache.json", result["error"])
        self.assertNotIn(str(self._tmp.name), result["error"])
        self.assertEqual(sum("[采集重试]" in line for line in logs), 1)
        self.assertEqual(sum("[采集失败入池]" in line for line in logs), 1)
        stats = get_request_cache_stats()
        self.assertEqual(stats["actual"], 2)
        self.assertEqual(stats["retries"], 1)
        self.assertEqual(stats["by_source"]["juhe"]["retries"], 1)

    def test_collection_plan_keeps_retry_out_of_logical_unique_count(self):
        from collection_plan import CollectionPlan
        from request_cache import activate_collection_plan, deactivate_collection_plan

        source = AlwaysPermissionErrorSource(
            Path(self._tmp.name) / "private" / "cache.json"
        )
        plan = CollectionPlan(subscription_count=1)
        plan.add_request(
            source,
            "KIX",
            "PVG",
            "2026-10-06",
            persist=False,
        )
        activate_collection_plan(plan.request_keys)
        self.addCleanup(deactivate_collection_plan)
        plan_logs = []

        with patch("request_cache.safe_log"), patch(
            "request_cache.time.sleep"
        ), patch("collection_plan.safe_log", side_effect=plan_logs.append):
            report = plan.execute()

        self.assertEqual(source.calls, 2)
        self.assertEqual(report.actual_requests, 2)
        self.assertEqual(report.retries, 1)
        self.assertTrue(any("计划唯一=1" in line for line in plan_logs))
        self.assertTrue(any("重试=1" in line for line in plan_logs))
        self.assertTrue(any("计划恒等式=True" in line for line in plan_logs))

    def test_return_collection_failure_activates_data_incomplete_disclosure(self):
        from notifier import _build_source_degradation_context

        failure = {
            "direction": "返程",
            "direction_code": "return",
            "date": "2026-10-06",
            "source_errors": [
                {
                    "source": "juhe",
                    "error": "PermissionError(errno=5,path=<relative>/data):拒绝访问。",
                }
            ],
        }
        context = _build_source_degradation_context(
            source_stats={"juhe": {"count": 319, "status": "成功"}},
            last_snapshot={"channels": '["juhe"]', "push_type": "价格提醒"},
            source_errors=failure["source_errors"],
            collection_failures=[failure],
        )

        self.assertTrue(context["active"])
        self.assertTrue(context["data_incomplete"])
        self.assertEqual(context["push_type"], "数据不完整")
        self.assertIn("本轮返程采集失败", context["reason"])
        self.assertIn("结论不代表市场无票", context["reason"])

    def test_main_builds_directional_collection_failure_from_source_errors(self):
        from main import _build_collection_leg_failure

        failure = _build_collection_leg_failure(
            "return",
            "2026-10-06",
            ["KIX", "ITM"],
            ["PVG", "SHA"],
            [
                {
                    "source": "juhe",
                    "error": "PermissionError(errno=5,path=<relative>/data):拒绝访问。",
                }
            ],
        )

        self.assertEqual(failure["direction"], "返程")
        self.assertEqual(failure["direction_code"], "return")
        self.assertEqual(failure["date"], "2026-10-06")
        self.assertEqual(failure["origin_airports"], ["KIX", "ITM"])
        self.assertEqual(failure["destination_airports"], ["PVG", "SHA"])
        self.assertIn("juhe:PermissionError", failure["reason"])

    def test_collection_failure_bypasses_low_price_silent_gate(self):
        from main import _should_skip_low_price_alert

        subscription = {
            "hard_constraints": {"budget_strategy": "low_price_alert"}
        }
        healthy_analysis = {
            "low_price_alert_triggered": False,
            "collection_failures": [],
        }
        failed_analysis = {
            **healthy_analysis,
            "collection_failures": [{"direction": "返程"}],
        }

        self.assertTrue(
            _should_skip_low_price_alert(subscription, healthy_analysis)
        )
        self.assertFalse(
            _should_skip_low_price_alert(subscription, failed_analysis)
        )

    def test_build_payload_routes_return_failure_to_data_incomplete_contract(self):
        from notification_sections import missing_notification_sections
        from notifier import build_notification_payload, render_email

        fixture_path = (
            Path(__file__).resolve().parent
            / "tests"
            / "fixtures"
            / "collection_failure_20260823_v1.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        source_error = fixture["source_error"]
        failure = fixture["collection_failure"]
        source_stats = fixture["source_stats"]
        analysis = {
            "round_trip_analysis": {"top_combinations": []},
            "source_stats": source_stats,
            "source_errors": [source_error],
            "collection_failures": [failure],
            "decision": {"conclusion": "无符合方案", "confidence": "低"},
        }
        route_info = {
            "round_trip": True,
            "origin": "PVG",
            "destination": "KIX",
            "depart_date": "2026-10-01",
            "return_date": "2026-10-06",
            "route_type": "international",
            "source_stats": source_stats,
            "source_errors": [source_error],
            "collection_failures": [failure],
        }
        subscription = {
            "id": "permission-failure-replay-20260823",
            "basic": {"route_type": "international", "passenger_count": 1},
            "preferences": {
                "passengers": {
                    "adult": 1,
                    "child": 0,
                    "elderly": 0,
                    "infant": 0,
                }
            },
        }

        with patch("notifier.get_last_push_price", return_value=None), patch(
            "notifier.get_last_push_snapshot",
            return_value={"channels": '["juhe"]', "push_type": "价格提醒"},
        ), patch("notifier.track_plan_status", return_value=None):
            payload = build_notification_payload(
                analysis,
                route_info=route_info,
                subscription=subscription,
            )
        with patch(
            "notifier._quota_overview_text",
            return_value="[配额总览] 离线测试",
        ):
            subject, email_html = render_email(payload)

        self.assertEqual(payload["push_type"], "数据不完整")
        self.assertTrue(payload["source_degradation"]["data_incomplete"])
        self.assertEqual(payload["collection_failures"], [failure])
        self.assertEqual(payload["recommended_plans"], [])
        self.assertEqual(payload["excluded_plans"], [])
        self.assertEqual(payload["single_leg_rejections"], [])
        self.assertIn("数据不完整,本轮结论不可用", payload["no_primary_reason"])
        self.assertEqual(
            missing_notification_sections(
                email_html,
                "",
                trigger_type="data_incomplete",
            ),
            [],
        )
        self.assertIn("【数据不完整】", subject)
        self.assertNotIn("无符合方案", email_html)
        self.assertNotIn("最大卡点", email_html)

    def test_data_incomplete_rendering_suppresses_market_no_result_claims(self):
        from notifier import render_email, render_pushplus

        reason = (
            "本轮返程采集失败(原因=juhe:PermissionError"
            "(errno=5,path=<relative>/data):拒绝访问。),结论不代表市场无票"
        )
        payload = {
            "push_type": "数据不完整",
            "route": "上海 → 大阪",
            "is_roundtrip": True,
            "recommended_plans": [],
            "alternative_plans": [],
            "same_day_alternatives": [],
            "no_primary_reason": reason,
            "source_degradation": {
                "active": True,
                "data_incomplete": True,
                "push_type": "数据不完整",
                "reason": reason,
            },
            "collection_failures": [
                {"direction": "返程", "reason": reason}
            ],
            "source_stats": {"juhe": {"count": 319, "status": "成功"}},
            "source_errors": [
                {
                    "source": "juhe",
                    "error": "PermissionError(errno=5,path=<relative>/data):拒绝访问。",
                }
            ],
            "collected_at": "2026-08-23T21:00:31",
        }

        subject, email_html = render_email(payload)
        push_html = render_pushplus(payload)
        rendered = "\n".join((subject, email_html, push_html))

        self.assertIn("【数据不完整】", subject)
        self.assertIn("本轮返程采集失败", rendered)
        self.assertIn("结论不代表市场无票", rendered)
        self.assertIn("数据不完整,本轮结论不可用", rendered)
        self.assertNotIn("无符合方案", rendered)
        self.assertNotIn("直飞/基础筛选排除", rendered)
        self.assertNotIn("最大卡点", rendered)

        for privacy_level in ("redacted", "minimal"):
            private_payload = {**payload, "privacy_level": privacy_level}
            private_subject, private_email = render_email(private_payload)
            private_push = render_pushplus(private_payload)
            private_rendered = "\n".join(
                (private_subject, private_email, private_push)
            )
            self.assertIn("【数据不完整】", private_subject)
            self.assertIn("数据不完整,本轮结论不可用", private_rendered)
            self.assertIn("本轮返程采集失败", private_rendered)
            self.assertIn("技术细节已隐藏", private_rendered)
            self.assertNotIn("PermissionError", private_rendered)
            self.assertNotIn("<relative>/data", private_rendered)


if __name__ == "__main__":
    unittest.main()
