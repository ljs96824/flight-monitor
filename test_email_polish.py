import sys
import types
import unittest


sys.modules.setdefault(
    "httpx",
    types.SimpleNamespace(get=lambda *args, **kwargs: None, post=lambda *args, **kwargs: None),
)


def _flight(
    flight_no="MU225",
    *,
    destination="KIX",
    price=4954,
    source="juhe",
    collected_at="2026-07-21T09:30:00",
):
    return {
        "flight_no": flight_no,
        "flight_combo": flight_no,
        "price": price,
        "price_source": source,
        "data_source": source,
        "departure_airport": "PVG",
        "arrival_airport": destination,
        "departure_time": "2026-10-01 09:00",
        "arrival_time": "2026-10-01 12:00",
        "total_duration_min": 180,
        "stops": 0,
        "collected_at": collected_at,
    }


def _plan(*, link_token="verify-token"):
    flight = _flight()
    return {
        "label": "方案A",
        "tier": "首选推荐",
        "route_type": "international",
        "is_roundtrip": False,
        "price": 4954,
        "estimated_price": 4954,
        "main_flight": flight,
        "summary": "MU225 PVG09:00→KIX12:00",
        "baggage_line": "支付页需确认",
        "buy_condition": "以支付页为准",
        "links": {
            "main": (
                f'<a href="https://example.com/{link_token}" target="_blank">携程</a>'
            )
        },
    }


def _payload(plan=None):
    plan = plan or _plan()
    return {
        "push_type": "价格提醒",
        "route": "上海 → 大阪",
        "route_type": "international",
        "recommendation": "继续观察",
        "price_policy_reason": "当前价格供参考",
        "buy_condition": "以支付页为准",
        "current_price": 4954,
        "display_price": 4954,
        "transaction_price": 4954,
        "ideal_price": 4500,
        "max_price": 6000,
        "recommended_plans": [plan],
        "trigger_reason": ["当前出现新的可验证方案"],
        "collected_at": "2026-07-21 09:30",
        "detail_url": "https://example.com/detail",
        "form_url": "https://example.com/edit",
    }


class EmailPolishTest(unittest.TestCase):
    def test_dual_source_attribution_uses_real_counts_and_global_min(self):
        from notifier import _email_source_rows

        body = "".join(
            _email_source_rows(
                {
                    "route_type": "international",
                    "source_stats": {
                        "hasdata": {"count": 16, "status": "成功"},
                        "juhe": {"count": 201, "status": "成功"},
                        "duffel": {"count": 83, "status": "成功"},
                    },
                }
            )
        )

        self.assertIn("主源:Google Flights(HasData)—16个方案", body)
        self.assertIn("交叉/OTA:聚合数据—201个方案", body)
        self.assertIn("入池价:按全局最低(global_min)", body)
        self.assertNotIn("Google Flights 多源(SerpAPI、HasData)", body)

    def test_excluded_reason_prefers_exact_filter_detail(self):
        from analyzer import _attach_filter_reason_details, _excluded_flight_summary
        from notifier import _excluded_reason_details

        cases = (
            ("用户不接受红眼/过早航班", "red_eye"),
            ("超过合理中转最长可接受总行程时间", "max_total_duration"),
            ("托运行李要求未满足", "need_baggage"),
        )
        for reason, expected_code in cases:
            with self.subTest(reason=reason):
                flight = {
                    **_flight(),
                    "exclude_reason": reason,
                    "fare_rules": {"baggage": {"included": False}},
                }
                _attach_filter_reason_details([flight], {"need_baggage": "required"})
                item = _excluded_flight_summary([flight])[0]
                details = _excluded_reason_details(item)
                self.assertEqual(item["filter_reason_code"], expected_code)
                self.assertIn(expected_code, details[0])
                self.assertNotEqual(details[0], reason)

    def test_large_channel_gap_is_only_disclosed_inside_channel_comparison(self):
        from notifier import _render_payload_plan_card, _source_channel_comparison_lines

        plan = _plan()
        flight = plan["main_flight"]
        flight.update(
            {
                "price": 4153,
                "source_price_anomaly": {
                    "flight_combo": "MU225",
                    "diff_pct": 192.2,
                    "sources": [
                        {"source": "hasdata", "price": 12137},
                        {"source": "juhe", "price": 4153},
                    ],
                },
            }
        )
        payload = _payload(plan)
        payload["is_roundtrip"] = False
        payload["channel_price_rows"] = [
            {
                "direction": "outbound",
                "flight_combo": "MU225",
                "provider": "Google",
                "source": "hasdata",
                "value": 12137,
                "selected": False,
            },
            {
                "direction": "outbound",
                "flight_combo": "MU225",
                "provider": "OTA",
                "source": "juhe",
                "value": 4153,
                "selected": True,
            },
        ]
        payload["dual_source_price_anomalies"] = [
            {"direction": "outbound", **flight["source_price_anomaly"]}
        ]

        lines = _source_channel_comparison_lines(payload)
        card = _render_payload_plan_card(plan)

        self.assertEqual(len(lines), 1)
        self.assertIn("⚠ 渠道价差>15%", lines[0])
        self.assertNotIn("渠道参考价:Google", card)

    def test_email_keeps_validation_links_only_in_plan_card(self):
        from notifier import render_email

        _subject, body = render_email(_payload())

        self.assertEqual(body.count("verify-token"), 1)
        self.assertNotIn("快速验证首选方案A", body)
        self.assertIn("验证此方案", body)

    def test_tracking_reason_and_fourteen_run_trend_are_not_duplicated(self):
        from notifier import _non_price_change_reasons, render_email

        payload = _payload()
        payload.update(
            {
                "trigger_reason": [
                    "上次推荐的MU225本次仍可获取报价",
                    "当前出现新的可验证方案",
                ],
                "plan_status_change": {"msg": "上次推荐的MU225本次仍可获取报价"},
                "trend_summary": "近14次下降¥54",
                "price_history": [
                    {"price": 5100},
                    {"price": 5050},
                    {"price": 4954},
                ],
            }
        )

        reasons = _non_price_change_reasons(payload)
        _subject, body = render_email(payload)

        self.assertNotIn("上次推荐的MU225本次仍可获取报价", reasons)
        self.assertEqual(body.count("近14次下降¥54"), 1)

    def test_punctuality_and_international_fare_wording_are_compact_and_truthful(self):
        from analyzer import verify_fare_rules
        from notifier import _plan_punctuality_line, _plan_refund_line

        outbound = _flight()
        outbound["punctuality"] = {"level": "较稳", "note": "历史参考"}
        outbound["fare_rules"] = {
            "source": "国内标准规则推断",
            "source_note": "国内标准规则推断，具体条款以支付页为准",
            "refund": {"label": "退改需确认"},
        }
        return_flight = {
            **_flight("JL891", destination="PVG"),
            "departure_airport": "KIX",
            "arrival_airport": "PVG",
            "punctuality": {"level": "一般", "note": "历史参考"},
        }
        plan = {
            "route_type": "international",
            "is_roundtrip": True,
            "outbound_flight": outbound,
            "return_flight": return_flight,
        }

        punctuality = _plan_punctuality_line(plan)
        refund = _plan_refund_line(plan)
        verification = verify_fare_rules(outbound, {})

        self.assertIn("去程:", punctuality)
        self.assertIn("返程:", punctuality)
        self.assertNotIn("<br>", punctuality)
        self.assertIn("标准规则推断(国际线)", refund)
        self.assertNotIn("国内标准规则推断", refund)
        self.assertIn("标准规则推断(国际线)", " ".join(verification["matches"]))

    def test_airport_reference_keeps_itm_and_marks_price_source_and_time(self):
        from analyzer import build_airport_cost_comparison
        from notifier import _airport_section_title, _email_airport_cost_comparison_body

        flights = [
            _flight("MU225", destination="KIX", price=2882, source="juhe"),
            _flight(
                "NH980",
                destination="ITM",
                price=3100,
                source="hasdata",
                collected_at="2026-07-21T09:31:00",
            ),
        ]
        rows = build_airport_cost_comparison(flights, preferences={}, limit=4)
        payload = {"airport_cost_comparison": rows}
        body = _email_airport_cost_comparison_body(payload)

        self.assertEqual({row["arrival_airport"] for row in rows}, {"KIX", "ITM"})
        self.assertIn("price_source", rows[0])
        self.assertIn("collected_at", rows[0])
        self.assertIn("ITM", body)
        self.assertIn("来源:", body)
        self.assertIn("09:30", body)
        self.assertEqual(_airport_section_title({"airport_cost_comparison": rows[:1]}), "机场参考")
        self.assertEqual(_airport_section_title(payload), "机场选择对比")

    def test_domestic_calendar_warns_that_reference_floor_may_miss_time_window(self):
        from notifier import _email_price_calendar_body

        body = _email_price_calendar_body(
            {
                "route_type": "domestic",
                "is_roundtrip": True,
                "price_calendar": {
                    "scope": "roundtrip",
                    "return_date": "2026-08-01",
                    "return_min_price": 615,
                    "rows": [
                        {
                            "date": "2026-07-31",
                            "weekday": "周五",
                            "min_price": 1200,
                            "outbound_min_price": 585,
                            "return_min_price": 615,
                            "selected": True,
                        }
                    ],
                },
            }
        )

        self.assertIn(
            "下限班次可能不满足你的时间窗(如返程¥615班次),备选按可行时间选取",
            body,
        )


if __name__ == "__main__":
    unittest.main()
