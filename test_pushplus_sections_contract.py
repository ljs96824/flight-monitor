import html
import inspect
import re
import unittest
from unittest.mock import patch

import main
import notifier
from pushplus_sections import (
    PushRender,
    PushSection,
    prepare_push_render,
    render_push_render,
)


KNOWN_MOJIBAKE = (
    "璐拱鍓嶈纭",
    "鍘嗗彶骞冲潎",
    "浠锋牸涓婃定姒傜巼",
    "绁ㄨ鏍￠獙",
)


def _section(section_id, priority, text, *, mandatory=False):
    return PushSection(section_id, priority, text, mandatory)


def _assert_html_contract(testcase: unittest.TestCase, content: str):
    testcase.assertIsNone(re.search(r"<[^>]*$", content))
    for attributes in re.findall(r"<a\b([^>]*)>", content, flags=re.I):
        testcase.assertRegex(attributes, r'\bhref="[^"]+"')
    for mojibake in KNOWN_MOJIBAKE:
        testcase.assertNotIn(mojibake, content)
    testcase.assertNotIn("�", content)


class PushPlusSectionContractTest(unittest.TestCase):
    def test_unstructured_24999_characters_are_byte_identical(self):
        content = "短" * 24999
        self.assertEqual(notifier._prepare_pushplus_content(content), content)

    def test_unstructured_25001_characters_use_generic_safe_template(self):
        content = "告" * 25001
        prepared = notifier._prepare_pushplus_content(content)
        self.assertNotEqual(prepared, content)
        self.assertIn("通知内容过长", prepared)
        self.assertNotIn(content[:100], prepared)
        _assert_html_contract(self, prepared)

    def test_just_over_compact_threshold_drops_p3_as_a_whole(self):
        render = PushRender(
            title="测试",
            sections=(
                _section("current_price", 0, "当前价:CNY100", mandatory=True),
                _section("technical", 3, "技" * 25000),
            ),
            detail_url=None,
        )
        prepared = prepare_push_render(render)
        self.assertEqual(prepared.mode, "compact")
        self.assertEqual(prepared.kept_section_ids, ("current_price", "compact_notice"))
        self.assertNotIn("技", prepared.content)

    def test_compaction_drops_p2_only_when_p3_removal_is_still_over_limit(self):
        render = PushRender(
            title="测试",
            sections=(
                _section("current_price", 0, "当前价:CNY100", mandatory=True),
                _section("risk", 1, "风" * 16000),
                _section("alternative", 2, "备" * 16000),
                _section("technical", 3, "技" * 100),
            ),
            detail_url=None,
        )
        prepared = prepare_push_render(render)
        self.assertIn("risk", prepared.kept_section_ids)
        self.assertNotIn("alternative", prepared.kept_section_ids)
        self.assertNotIn("technical", prepared.kept_section_ids)

    def test_compaction_drops_p1_after_lower_priorities_are_insufficient(self):
        render = PushRender(
            title="测试",
            sections=(
                _section("current_price", 0, "当前价:CNY100", mandatory=True),
                _section("risk", 1, "风" * 31000),
                _section("alternative", 2, "备" * 100),
                _section("technical", 3, "技" * 100),
            ),
            detail_url=None,
        )
        prepared = prepare_push_render(render)
        self.assertNotIn("risk", prepared.kept_section_ids)
        self.assertNotIn("alternative", prepared.kept_section_ids)
        self.assertNotIn("technical", prepared.kept_section_ids)
        self.assertIn("current_price", prepared.kept_section_ids)

    def test_oversized_p0_uses_minimal_template_and_keeps_price_and_link(self):
        detail_url = "https://example.com/detail/abc?x=1&y=2"
        render = PushRender(
            title="【价格提醒】上海到大阪",
            sections=(
                _section("current_judgment", 0, "判" * 31000, mandatory=True),
                _section("current_price", 0, "当前价:CNY9049 单人往返", mandatory=True),
                _section(
                    "detail_link",
                    0,
                    '<a href="https://example.com/detail/abc?x=1&amp;y=2">网页详情</a>',
                    mandatory=True,
                ),
                _section("disclaimer", 0, "价格以支付页为准", mandatory=True),
            ),
            detail_url=detail_url,
        )
        prepared = prepare_push_render(render)
        self.assertEqual(prepared.mode, "minimal")
        self.assertIn("CNY9049 单人往返", prepared.content)
        self.assertIn("https://example.com/detail/abc?x=1&amp;y=2", prepared.content)
        self.assertNotIn("判" * 100, prepared.content)
        _assert_html_contract(self, prepared.content)

    def test_payload_builder_returns_required_p0_sections_and_valid_detail_link(self):
        payload = {
            "push_type": "价格提醒",
            "route": "上海 → 大阪",
            "display_price": 9049,
            "current_price": 9049,
            "recommendation": "继续监控",
            "buy_condition": "支付页单人价≤CNY8000(单人往返)",
            "recommended_plans": [{"label": "方案A", "price": 9049}],
            "detail_url": "https://example.com/detail/abc",
        }
        render = notifier.render_pushplus_sections(payload)
        self.assertIsInstance(render, PushRender)
        ids = {section.section_id for section in render.sections}
        self.assertTrue(
            {
                "current_judgment",
                "current_price",
                "purchase_condition",
                "primary_plan",
                "detail_link",
                "data_freshness",
                "disclaimer",
            }.issubset(ids)
        )
        content = render_push_render(render)
        self.assertIn("https://example.com/detail/abc", html.unescape(content))
        _assert_html_contract(self, content)

    def test_invalid_detail_url_uses_plain_no_link_copy(self):
        render = notifier.render_pushplus_sections(
            {
                "push_type": "价格提醒",
                "route": "上海 → 大阪",
                "display_price": 9049,
                "current_price": 9049,
                "recommendation": "继续监控",
                "buy_condition": "以支付页为准",
                "recommended_plans": [{"label": "方案A", "price": 9049}],
                "detail_url": "javascript:alert(1)",
            }
        )
        content = render_push_render(render)
        self.assertIn("网页详情未配置,完整结果见本通知", content)
        self.assertNotIn('href=""', content)
        self.assertNotIn("javascript:", content)


    def test_chinese_and_emoji_survive_whole_section_compaction(self):
        render = PushRender(
            title="航班价格提醒",
            sections=(
                _section(
                    "current_judgment",
                    0,
                    "当前判断:价格变化🛫",
                    mandatory=True,
                ),
                _section(
                    "current_price",
                    0,
                    "当前价:CNY9049 单人往返",
                    mandatory=True,
                ),
                _section("technical", 3, "技术数据" * 7000),
            ),
            detail_url=None,
        )

        prepared = prepare_push_render(render)

        self.assertEqual(prepared.mode, "compact")
        self.assertIn("当前判断:价格变化🛫", prepared.content)
        self.assertNotIn("技术数据", prepared.content)
        _assert_html_contract(self, prepared.content)
    def test_renderer_is_direct_from_payload_and_old_mojibake_rules_are_gone(self):
        source = inspect.getsource(notifier.render_pushplus_sections)
        self.assertNotIn("render_pushplus(", source)
        notifier_source = inspect.getsource(notifier)
        self.assertNotIn("content[:keep]", notifier_source)
        for mojibake in KNOWN_MOJIBAKE:
            self.assertNotIn(mojibake, notifier_source)

    def test_collection_failure_generic_pushplus_call_is_unchanged(self):
        subscription = {
            "origin": "PVG",
            "destination": "KIX",
            "notification_goals": {"method": "pushplus"},
        }
        with patch("main.send", return_value=True) as send_mock:
            self.assertTrue(
                main._notify_subscription_failure(
                    subscription,
                    reason="juhe:PermissionError(errno=5)",
                )
            )
        content = (
            "本次采集失败: PVG->KIX<br>"
            "原因: juhe:PermissionError(errno=5)<br>"
            "订阅已保留,下轮自动重试。"
        )
        send_mock.assert_called_once_with(
            content,
            title="【航班监控采集失败】PVG->KIX",
        )

    def test_basket_sentinel_generic_pushplus_call_is_unchanged(self):
        subscriptions = [
            {"notification_goals": {"method": "pushplus"}}
        ]
        with patch("main.send", return_value=True) as send_mock:
            self.assertTrue(
                main._notify_system_alert(
                    subscriptions,
                    "[篮子哨兵] 今日篮子未运行",
                    "今天20:00后仍未见篮子采集记录。",
                )
            )
        send_mock.assert_called_once_with(
            "今天20:00后仍未见篮子采集记录。",
            title="[篮子哨兵] 今日篮子未运行",
        )


    def test_extreme_calendar_and_excluded_sections_are_removed_whole(self):
        render = PushRender(
            title="价格提醒",
            sections=(
                _section(
                    "current_judgment",
                    0,
                    "当前判断:继续监控",
                    mandatory=True,
                ),
                _section(
                    "current_price",
                    0,
                    "当前价:CNY9049 单人往返",
                    mandatory=True,
                ),
                _section("calendar", 3, "日历明细" * 7000),
                _section("excluded_plans", 3, "排除方案" * 7000),
            ),
            detail_url=None,
        )

        prepared = prepare_push_render(render)

        self.assertEqual(prepared.mode, "compact")
        self.assertNotIn("calendar", prepared.kept_section_ids)
        self.assertNotIn("excluded_plans", prepared.kept_section_ids)
        self.assertNotIn("日历明细", prepared.content)
if __name__ == "__main__":
    unittest.main()
