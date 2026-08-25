import queue
import unittest
from pathlib import Path
from unittest.mock import ANY, Mock, call, patch

import main
import web_form
from web_test_utils import enable_csrf


class WebSubmitSideEffectContractTest(unittest.TestCase):
    def setUp(self):
        web_form.app.config.update(TESTING=True)
        self.client = web_form.app.test_client()
        enable_csrf(self.client)

    def test_successful_submit_saves_before_starting_background_collection(self):
        subscription = {
            "origin": "PVG",
            "destination": "KIX",
            "notification_goals": {
                "method": "both",
                "email": "user@example.com",
            },
        }
        background_subscription = {**subscription, "_index": 72}
        lifecycle = Mock()

        with (
            patch.object(web_form, "build_subscription", return_value=subscription),
            patch.object(
                web_form,
                "save_subscription",
                side_effect=lambda item, index: (
                    lifecycle.save(item, index),
                    72,
                )[1],
            ) as save_subscription,
            patch.object(
                web_form,
                "start_background_collection",
                side_effect=lambda item: (
                    lifecycle.start(item),
                    {"status": "started", "entrypoint": "web"},
                )[1],
            ) as start_background_collection,
        ):
            response = self.client.post(
                "/subscribe",
                data={"subscription_index": "72", "form_page": "full"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.headers["Location"].endswith("/success?index=72")
        )
        save_subscription.assert_called_once_with(subscription, 72)
        start_background_collection.assert_called_once_with(background_subscription)
        self.assertEqual(
            lifecycle.mock_calls,
            [call.save(subscription, 72), call.start(background_subscription)],
        )

    def test_notification_dispatch_calls_only_channels_selected_by_method(self):
        expected = {
            "email": (1, 0),
            "pushplus": (0, 1),
            "both": (1, 1),
        }
        for method, (email_calls, pushplus_calls) in expected.items():
            with self.subTest(method=method):
                subscription = {
                    "_index": 72,
                    "notification_goals": {
                        "method": method,
                        "email": "user@example.com",
                    },
                }
                payload = {
                    "push_type": "测试提醒",
                    "route": "上海→大阪",
                }
                with (
                    patch.object(main, "build_notification_payload", return_value=payload),
                    patch.object(main, "feedback_acknowledgement", return_value=None),
                    patch.object(main, "render_email", return_value=("主题", "<p>正文</p>", {})),
                    patch.object(main, "render_detail_html", return_value="<p>详情</p>"),
                    patch.object(main, "_save_result_for_page"),
                    patch.object(main, "persist_notification_payload"),
                    patch.object(main, "render_pushplus_sections", return_value=object()),
                    patch.object(main, "send_email", return_value=True) as send_email,
                    patch.object(main, "send", return_value=True) as send_pushplus,
                ):
                    sent = main._deliver_notification(
                        subscription,
                        "上海->大阪",
                        {"route_info": {}},
                    )

                self.assertTrue(sent)
                self.assertEqual(send_email.call_count, email_calls)
                self.assertEqual(send_pushplus.call_count, pushplus_calls)

    def test_background_thread_targets_single_subscription_runner(self):
        subscription = {"_index": 72}
        thread = Mock()

        def report_started():
            startup_queue = thread_class.call_args.kwargs["args"][1]
            startup_queue.put({"status": "started", "entrypoint": "web"})

        thread.start.side_effect = report_started
        with patch.object(web_form.threading, "Thread", return_value=thread) as thread_class:
            result = web_form.start_background_collection(subscription)

        kwargs = thread_class.call_args.kwargs
        self.assertIs(kwargs["target"], web_form.run_single_subscription)
        self.assertEqual(kwargs["args"][0], subscription)
        self.assertIsInstance(kwargs["args"][1], queue.Queue)
        self.assertTrue(kwargs["daemon"])
        thread.start.assert_called_once_with()
        self.assertEqual(result["status"], "started")
    def test_single_subscription_runner_normalizes_and_enables_web_failure_notification(self):
        subscription = {"_index": 72}
        normalized = {
            "_index": 72,
            "origin": "PVG",
            "destination": "KIX",
        }
        with (
            patch.object(main, "_normalize_subscription", return_value=normalized) as normalize,
            patch.object(main, "process_subscription", return_value=True) as process,
            patch.object(web_form, "record_last_attempt"),
        ):
            web_form.run_single_subscription(subscription)

        normalize.assert_called_once_with(subscription)
        process.assert_called_once_with(
            normalized,
            ensure_db=True,
            web_trigger=True,
            startup_callback=ANY,
        )

    def test_contribution_rules_reserve_port_5000_for_user_process(self):
        path = Path(__file__).parent / "CONTRIBUTING.md"
        self.assertTrue(path.is_file(), "缺少 CONTRIBUTING.md 端口所有权规则")
        text = path.read_text(encoding="utf-8")
        self.assertIn("用户拥有 `:5000`", text)
        self.assertIn("任务预览必须使用隔离端口", text)
        self.assertIn("禁止停止、重启或占用用户的 `:5000`", text)


if __name__ == "__main__":
    unittest.main()
