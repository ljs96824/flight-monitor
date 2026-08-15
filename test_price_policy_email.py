import sys
import types
import inspect
import contextlib
import io
import unittest
from unittest.mock import patch

sys.modules.setdefault("httpx", types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None))

from analyzer import analyze_round_trip, build_excluded_roundtrip_combos, determine_push_type
import email_notifier
from notifier import (
    _display_channel_price_rows,
    _email_detail_charts_body,
    build_notification_payload,
    render_email,
)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    module = sys.modules[__name__]
    for name, function in inspect.getmembers(module, inspect.isfunction):
        if name.startswith("test_") and function.__module__ == __name__:
            suite.addTest(unittest.FunctionTestCase(function, description=name))
    return suite


def test_push_type_uses_transaction_price_when_display_price_only_looks_good():
    meta = determine_push_type(
        6522,
        target_price=7994,
        max_budget=9000,
        price_history=[6971, 6530, 6527, 6522],
        last_push_price=6874,
        analysis_result={
            "decision_prices": {
                "display_price": 6522,
                "transaction_price": 7182,
                "verify_price": 6848,
            }
        },
    )

    assert meta["type"] == "值得验证"
    assert "搜索参考价达标，但预估实付价高于验证购买价（你的设置）" in meta["reasons"]
    assert all("100%" not in reason for reason in meta["reasons"])


def test_email_no_primary_uses_candidate_pool_reference_without_purchase_signals():
    payload = {
        "push_type": "值得验证",
        "route": "上海 → 大阪",
        "recommendation": "值得验证，不建议直接下单",
        "price_policy_reason": "搜索参考价达标，但预估实付价高于验证购买价",
        "display_price": 6522,
        "transaction_price": 7182,
        "verify_price": 6848,
        "ideal_price": 7994,
        "max_price": 9000,
        "buy_condition": "支付页≤¥6,848且含托运行李",
        "price_signal": {"label": "低价", "summary": "建议验证支付页价格"},
        "execution_advice": {"label": "可验证购买", "summary": "预估实付价不高于验证价"},
        "candidate_price_summary": {
            "lowest": 932,
            "count": 5,
            "reason": "时间窗口不符",
            "price_scope": "per_person_roundtrip",
        },
        "no_primary_reason": "本次无方案主因是【去程时间】:最早航班晚于到达上限。",
        "no_primary_diagnosis": {
            "primary_cause": "outbound_time",
            "max_bottleneck": {
                "label": "去程时间",
                "count": 5,
                "ratio": 100.0,
                "pool_scope": "去程池",
            },
        },
        "plan_status_change": {
            "status": "partial_unavailable",
            "msg": "上次推荐航班本次未获取到报价。",
        },
        "trend_summary": "近3次采集持平约暂无报价，建议验证支付页价格。",
        "confidence": "中高",
        "source_count": 2,
        "freshness_minutes": 15,
        "trigger_reason": [
            "搜索参考价进入你的理想入手区间",
            "较上次提醒：下降¥352",
            "当前搜索价处于相似历史样本低价区间",
        ],
        "recommended_plans": [],
        "price_history": [],
        "buy_risk": ["行李/退改签待确认", "购买链路需验证"],
        "wait_risk": ["理想价再次出现不确定"],
        "action_range": {"ranges": []},
        "detail_url": "https://example.com/detail",
        "form_url": "https://example.com/",
        "feedback_url": "https://example.com/feedback",
        "collected_at": "2026-05-28 14:32",
    }

    subject, html = render_email(payload)

    assert "【无符合方案】上海 → 大阪｜提供0个备选" == subject
    assert "候选池参考" in html
    assert "候选中最低¥932 单人往返(时间窗口不符,不可购)" in html
    assert "理想入手价" in html and "¥7,994" in html
    assert "最高可接受价" in html and "¥9,000" in html
    assert "最大卡点:去程时间(去程池)排除最多(5个,占比100.0%)" in html
    assert "上次方案航班本次未获报价,趋势暂缺" in html
    assert "价格口径与信号" not in html
    assert "执行建议" not in html
    assert "预估实付价" not in html
    assert "本次验证价" not in html
    assert "本次方案验证价" not in html
    assert "可验证购买" not in html
    assert "建议验证支付页" not in html


def test_no_primary_payload_clears_purchase_only_price_fields():
    outbound = {
        "flight_no": "MU5099",
        "flight_combo": "MU5099",
        "price": 932,
        "departure_airport": "SHA",
        "arrival_airport": "PEK",
        "departure_time": "07:00",
        "arrival_time": "09:15",
        "departure_date": "2026-08-11",
        "arrival_date": "2026-08-11",
    }
    return_flight = {
        "flight_no": "MU5170",
        "flight_combo": "MU5170",
        "price": 2492,
        "departure_airport": "PEK",
        "arrival_airport": "SHA",
        "departure_time": "21:00",
        "arrival_time": "23:15",
        "departure_date": "2026-08-11",
        "arrival_date": "2026-08-11",
    }
    analysis = {
        "all_flights": [outbound],
        "return_analysis": {"all_flights": [return_flight]},
        "round_trip_analysis": {
            "top_combinations": [],
            "same_day_time_conflict": True,
            "total_min": 3424,
            "filter_counts": {
                "total_candidates": 2,
                "valid_price_count": 2,
                "outbound_collected": 1,
                "return_collected": 1,
                "after_meeting_outbound": 0,
                "after_meeting_return": 1,
                "return_after_lowerbound": 1,
                "same_day_combos": 0,
            },
        },
    }
    route_info = {
        "origin": "上海",
        "destination": "北京",
        "depart_date": "2026-08-11",
        "return_date": "2026-08-11",
        "round_trip": True,
        "target_price": 1200,
        "max_budget": 4000,
    }
    subscription = {
        "id": "no-primary-purchase-signal",
        "basic": {"route_type": "domestic", "passenger_count": 1},
        "preferences": {"passengers": {"adult": 1, "child": 0, "elderly": 0, "infant": 0}},
        "constraints": {
            "same_day_round_trip": True,
            "business_start": "10:30",
            "business_end": "17:00",
            "budget_scope": "per_person",
            "max_budget": 4000,
            "target_price": 1200,
        },
    }

    with patch("notifier.get_last_push_price", return_value=None), patch(
        "notifier.get_last_push_snapshot", return_value=None
    ), patch("notifier.track_plan_status", return_value=None):
        payload = build_notification_payload(
            analysis,
            outbound_analysis=analysis,
            return_analysis=analysis["return_analysis"],
            route_info=route_info,
            subscription=subscription,
        )

    assert payload["recommended_plans"] == []
    assert payload["display_price"] is None
    assert payload["transaction_price"] is None
    assert payload["verify_price"] is None
    assert payload["budget_compare_price"] is None
    assert payload["price_signal"]
    assert payload["price_signal"]["label"] == "中"
    assert payload["price_signal"]["summary"] == "搜索参考价仍高于理想入手价（你的设置）"
    assert payload["price_signal"]["sample_n"] == 0
    assert payload["execution_advice"] == {}
    assert payload["purchase_budget_decision"]["status"] == "not_applicable"


def test_email_roundtrip_excluded_single_leg_is_not_compared_to_roundtrip_total():
    payload = {
        "push_type": "值得验证",
        "route": "上海 → 大阪",
        "is_roundtrip": True,
        "recommendation": "值得验证，不建议直接下单",
        "display_price": 6522,
        "transaction_price": 7182,
        "verify_price": 6848,
        "ideal_price": 7994,
        "max_price": 9000,
        "buy_condition": "支付页≤¥6,848且含托运行李",
        "confidence": "中高",
        "source_count": 2,
        "recommended_plans": [
            {
                "label": "方案A",
                "variant": "推荐",
                "is_roundtrip": True,
                "price": 6522,
                "estimated_price": 7182,
                "outbound_line": "去程:9C6575｜春秋航空\n浦东(PVG) 08:05(上海当地) → 关西(KIX) 11:20(大阪当地)\n直飞｜A320",
                "return_line": "返程:9C6582｜春秋航空\n关西(KIX) 19:30(大阪当地) → 浦东(PVG) 21:00(上海当地)\n直飞｜A320",
                "baggage_line": "行李:支付页需确认",
                "purchase_mode": "两个单程拼接",
                "links": {},
            }
        ],
        "trigger_reason": [],
        "price_history": [],
        "excluded_plans": [
            {
                "scope": "outbound",
                "price": 2887,
                "flight_combo": "KE888+KE721",
                "reason": "用户设置必须直飞",
                "flight": {
                    "price": 2887,
                    "flight_combo": "KE888+KE721",
                    "airline_summary": "大韩航空",
                    "stops": 1,
                    "total_duration_min": 460,
                    "segments": [
                        {"flight_no": "KE888", "airline": "大韩航空", "dep_airport": "PVG", "dep_time": "2026-10-01 08:00", "arr_airport": "ICN", "arr_time": "2026-10-01 11:00", "aircraft": "A330"},
                        {"flight_no": "KE721", "airline": "大韩航空", "dep_airport": "ICN", "dep_time": "2026-10-01 13:00", "arr_airport": "KIX", "arr_time": "2026-10-01 15:40", "aircraft": "A321"},
                    ],
                    "layovers": [{"airport": "ICN", "city": "首尔仁川", "wait_minutes": 120}],
                },
            }
        ],
        "action_range": {"ranges": []},
        "checklist": [],
        "detail_url": "https://example.com/detail",
        "form_url": "https://example.com/",
        "feedback_url": "https://example.com/feedback",
    }

    _, html = render_email(payload)

    assert "已排除的更低价去程方案" in html
    assert "此为去程单段价，非往返总价" in html
    assert "比推荐便宜¥3,635" not in html
    assert "KE888+KE721" in html
    assert "border:1px solid #f0d0d0" in html
    assert "background:#fdf8f8" in html
    assert "font-size:13px;line-height:1.6" in html
    assert "width:80px" in html
    assert "#b91c1c" in html


def test_roundtrip_analysis_builds_excluded_roundtrip_combos():
    outbound_ok = {
        "price": 3400,
        "flight_combo": "9C6575",
        "stops": 0,
        "total_duration_min": 195,
        "segments": [
            {"flight_no": "9C6575", "airline": "春秋航空", "dep_airport": "PVG", "dep_time": "2026-10-01 08:05", "arr_airport": "KIX", "arr_time": "2026-10-01 11:20", "aircraft": "A320"}
        ],
    }
    return_ok = {
        "price": 3100,
        "flight_combo": "9C6582",
        "stops": 0,
        "total_duration_min": 210,
        "segments": [
            {"flight_no": "9C6582", "airline": "春秋航空", "dep_airport": "KIX", "dep_time": "2026-10-06 19:30", "arr_airport": "PVG", "arr_time": "2026-10-06 21:00", "aircraft": "A320"}
        ],
    }
    outbound_excluded = {
        "price": 2500,
        "flight_combo": "KE888+KE721",
        "stops": 1,
        "total_duration_min": 460,
        "segments": [
            {"flight_no": "KE888", "airline": "大韩航空", "dep_airport": "PVG", "dep_time": "2026-10-01 08:00", "arr_airport": "ICN", "arr_time": "2026-10-01 11:00", "aircraft": "A330"},
            {"flight_no": "KE721", "airline": "大韩航空", "dep_airport": "ICN", "dep_time": "2026-10-01 13:00", "arr_airport": "KIX", "arr_time": "2026-10-01 15:40", "aircraft": "A321"},
        ],
        "layovers": [{"airport": "ICN", "city": "首尔仁川", "wait_minutes": 120}],
    }
    outbound_analysis = {
        "economy_recommendations": [outbound_ok],
        "all_flights": [outbound_ok],
        "excluded_flights": [
            {
                "scope": "outbound",
                "price": 2500,
                "flight": outbound_excluded,
                "reason": "用户设置必须直飞",
            }
        ],
    }
    return_analysis = {
        "economy_recommendations": [return_ok],
        "all_flights": [return_ok],
        "excluded_flights": [],
    }

    result = analyze_round_trip(outbound_analysis, return_analysis)

    excluded = result.get("excluded_roundtrip_combos") or []
    assert excluded
    assert excluded[0]["scope"] == "roundtrip"
    assert excluded[0]["total_price"] == 5600
    assert excluded[0]["diff"] == 900
    assert excluded[0]["outbound"]["flight_combo"] == "KE888+KE721"
    assert excluded[0]["return"]["flight_combo"] == "9C6582"
    assert "去程" in excluded[0]["reason"]
    assert "用户设置必须直飞" in excluded[0]["reason"]


def test_excluded_roundtrip_combos_dedupes_reason_and_limits_debug_output():
    return_ok = {
        "price": 3200,
        "flight_combo": "9C6582",
        "segments": [
            {
                "flight_no": "9C6582",
                "dep_airport": "KIX",
                "dep_time": "2026-10-06 19:30",
                "arr_airport": "PVG",
            }
        ],
    }
    outbound_items = []
    for idx, price in enumerate((2400, 2450, 2500, 2550), start=1):
        outbound_items.append(
            {
                "scope": "outbound",
                "price": price,
                "flight": {
                    "price": price,
                    "flight_combo": f"KE{idx}+KE{idx + 10}",
                    "stops": 1,
                    "segments": [
                        {
                            "flight_no": f"KE{idx}",
                            "dep_airport": "PVG",
                            "dep_time": f"2026-10-01 0{idx}:00",
                            "arr_airport": "ICN",
                            "aircraft": "A330",
                        }
                    ],
                },
                "reason": "中转时间过长",
            }
        )

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        combos = build_excluded_roundtrip_combos(
            {"excluded_flights": outbound_items},
            {"economy_recommendations": [return_ok], "all_flights": [return_ok]},
            recommended_total=7000,
            max_show=3,
        )

    assert len(combos) == 1
    assert combos[0]["outbound"]["flight_combo"] == "KE1+KE11"
    assert stdout.getvalue().count("[排除组合]") == len(combos)


def test_email_roundtrip_excluded_combo_shows_both_legs():
    payload = {
        "push_type": "值得验证",
        "route": "上海 → 大阪",
        "is_roundtrip": True,
        "recommendation": "值得验证",
        "current_price": 6500,
        "display_price": 6500,
        "transaction_price": 6500,
        "verify_price": 6900,
        "ideal_price": 7800,
        "max_price": 9000,
        "buy_condition": "支付页≤¥6,900且含托运行李",
        "confidence": "中高",
        "recommended_plans": [],
        "trigger_reason": [],
        "price_history": [],
        "excluded_plans": [
            {
                "scope": "roundtrip",
                "is_roundtrip": True,
                "total_price": 5600,
                "diff": 900,
                "reason": "去程：用户设置必须直飞",
                "outbound": {
                    "price": 2500,
                    "flight_combo": "KE888+KE721",
                    "stops": 1,
                    "total_duration_min": 460,
                    "segments": [
                        {"flight_no": "KE888", "airline": "大韩航空", "dep_airport": "PVG", "dep_time": "2026-10-01 08:00", "arr_airport": "ICN", "arr_time": "2026-10-01 11:00", "aircraft": "A330"},
                        {"flight_no": "KE721", "airline": "大韩航空", "dep_airport": "ICN", "dep_time": "2026-10-01 13:00", "arr_airport": "KIX", "arr_time": "2026-10-01 15:40", "aircraft": "A321"},
                    ],
                    "layovers": [{"airport": "ICN", "city": "首尔仁川", "wait_minutes": 120}],
                },
                "return": {
                    "price": 3100,
                    "flight_combo": "9C6582",
                    "airline_summary": "春秋航空",
                    "stops": 0,
                    "total_duration_min": 210,
                    "departure_airport": "KIX",
                    "departure_time": "2026-10-06 19:30",
                    "arrival_airport": "PVG",
                    "arrival_time": "2026-10-06 21:00",
                    "aircraft": "A320",
                },
            }
        ],
        "action_range": {"ranges": []},
        "checklist": [],
        "detail_url": "https://example.com/detail",
        "form_url": "https://example.com/",
        "feedback_url": "https://example.com/feedback",
    }

    _, html = render_email(payload)

    assert "已排除的更低价往返组合" in html
    assert "比推荐便宜¥900" in html
    assert "KE888" in html
    assert "KE721" in html
    assert "9C6582" in html
    assert "✈ 去程" in html
    assert "✈ 返程" in html
    assert html.index("KE888") < html.index("9C6582")
    assert "关西(KIX)" in html
    assert "19:30" in html
    assert "A320" in html
    assert "返程" in html
    assert "单段价，非往返总价" not in html


def test_email_detail_charts_dedupe_channels_and_skip_empty_plan_rows():
    payload = {
        "push_type": "值得验证",
        "route": "上海 → 大阪",
        "recommendation": "值得验证，不建议直接下单",
        "display_price": 6522,
        "transaction_price": 7182,
        "verify_price": 6848,
        "ideal_price": 7994,
        "max_price": 9000,
        "buy_condition": "支付页≤¥6,848且含托运行李",
        "confidence": "中高",
        "recommended_plans": [],
        "trigger_reason": [],
        "price_history": [],
        "action_range": {"ranges": []},
        "checklist": ["支付页最终价是否≤¥6,848"],
        "channel_price_rows": [
            {"label": "Google Flights(via SerpAPI)", "value": 3402},
            {"label": "Google Flights(via HasData)", "value": 3402},
            {"label": "携程", "value": 3450},
        ],
        "plan_price_rows": [
            {"label": "方案A", "value": 6522, "note": "推荐"},
            {"label": "方案B", "value": None, "note": "暂无符合条件的备选"},
        ],
        "detail_url": "https://example.com/detail",
        "form_url": "https://example.com/",
        "feedback_url": "https://example.com/feedback",
    }

    html = _email_detail_charts_body(payload)

    # 直接核对结构化图表行，避免被详情页其他说明干扰。
    channel_rows = _display_channel_price_rows(payload)
    assert [row["label"] for row in channel_rows] == ["Google Flights", "携程"]
    assert channel_rows[0]["note"] == "SerpAPI、HasData 2个数据源一致"
    assert html.count("Google Flights") == 1
    assert "SerpAPI、HasData 2个数据源一致" in html
    assert "方案A:¥6,522,推荐" in html
    assert "方案B" not in html
    assert "¥6,522 B" not in html


def test_email_uses_section_cards_and_plan_table_layout():
    payload = {
        "push_type": "值得验证",
        "route": "上海 → 大阪",
        "recommendation": "值得验证，不建议直接下单",
        "display_price": 6522,
        "transaction_price": 7182,
        "verify_price": 6848,
        "ideal_price": 7994,
        "max_price": 9000,
        "buy_condition": "支付页≤¥6,848且含托运行李",
        "confidence": "中高",
        "source_count": 2,
        "recommended_plans": [
            {
                "label": "方案A",
                "variant": "推荐",
                "is_roundtrip": True,
                "price": 6522,
                "estimated_price": 7182,
                "outbound_line": "去程:9C6575｜春秋航空｜PVG 08:05 → KIX 11:20｜直飞｜A320",
                "return_line": "返程:9C6582｜春秋航空｜KIX 19:30 → PVG 21:00｜直飞｜A320",
                "outbound_flight": {
                    "flight_combo": "9C6575",
                    "airline_summary": "春秋航空",
                    "stops": 0,
                    "total_duration_min": 195,
                    "segments": [
                        {
                            "flight_no": "9C6575",
                            "airline": "春秋航空",
                            "dep_airport": "PVG",
                            "dep_time": "2026-10-01 08:05",
                            "arr_airport": "KIX",
                            "arr_time": "2026-10-01 11:20",
                            "aircraft": "A320",
                        }
                    ],
                },
                "return_flight": {
                    "flight_combo": "9C6582",
                    "airline_summary": "春秋航空",
                    "stops": 0,
                    "total_duration_min": 210,
                    "segments": [
                        {
                            "flight_no": "9C6582",
                            "airline": "春秋航空",
                            "dep_airport": "KIX",
                            "dep_time": "2026-10-06 19:30",
                            "arr_airport": "PVG",
                            "arr_time": "2026-10-06 21:00",
                            "aircraft": "A320",
                        }
                    ],
                },
                "purchase_mode": "两个单程拼接",
                "baggage_line": "行李:支付页需确认",
                "links": {"outbound": '<a href="https://example.com">Trip.com</a>'},
            }
        ],
        "trigger_reason": ["搜索参考价进入你的理想入手区间"],
        "price_history": [],
        "buy_risk": ["最终支付价需确认"],
        "wait_risk": ["继续等待可能错过低价"],
        "action_range": {"ranges": []},
        "checklist": ["支付页最终价是否≤¥6,848"],
        "detail_url": "https://example.com/detail",
        "form_url": "https://example.com/",
        "feedback_url": "https://example.com/feedback",
    }

    _, html = render_email(payload)

    assert "background:#fff;border:1px solid #e5e7eb;border-radius:10px" in html
    assert html.count("background:#fff;border:1px solid #e5e7eb;border-radius:10px") >= 6
    assert "<table style='width:100%;font-size:14px;" in html
    assert "width:90px;" in html
    assert "方案A ｜ 首选推荐" in html
    assert "✈ ━━ 去程 ━━" in html
    assert "✈ ━━ 返程 ━━" in html
    assert "起飞</td>" in html
    assert "到达</td>" in html
    assert "中转</td>" in html
    assert "机型</td>" in html
    assert "background:#f5f7fa;padding:4px 8px;border-radius:4px" in html


def test_email_plan_reads_google_nested_flights_fields_for_ca():
    payload = {
        "push_type": "值得验证",
        "route": "上海 → 北京",
        "recommendation": "值得验证",
        "display_price": 1880,
        "transaction_price": 1880,
        "verify_price": 2000,
        "ideal_price": 1900,
        "max_price": 2500,
        "buy_condition": "支付页≤¥2,000且含托运行李",
        "confidence": "中高",
        "recommended_plans": [
            {
                "label": "方案A",
                "variant": "推荐",
                "is_roundtrip": False,
                "price": 1880,
                "estimated_price": 1880,
                "outbound_flight": {
                    "price": 1880,
                    "flight_combo": "CA1234",
                    "stops": 0,
                    "total_duration_min": 135,
                    "flights": [
                        {
                            "flight_number": "CA1234",
                            "airline": "中国国际航空",
                            "airplane": "Airbus A321",
                            "departure_airport": {
                                "id": "PVG",
                                "name": "Shanghai Pudong",
                                "time": "2026-10-01 08:30",
                            },
                            "arrival_airport": {
                                "id": "PEK",
                                "name": "Beijing Capital",
                                "time": "2026-10-01 10:45",
                            },
                            "duration": 135,
                        }
                    ],
                },
                "baggage_line": "行李:支付页需确认",
                "purchase_mode": "单程",
                "links": {},
            }
        ],
        "trigger_reason": [],
        "price_history": [],
        "action_range": {"ranges": []},
        "checklist": [],
        "detail_url": "https://example.com/detail",
        "form_url": "https://example.com/",
        "feedback_url": "https://example.com/feedback",
    }

    _, html = render_email(payload)

    assert "CA1234" in html
    assert "中国国际航空" in html
    assert "浦东(PVG)" in html
    assert "08:30" in html
    assert "首都(PEK)" in html
    assert "10:45" in html
    assert "Airbus A321" in html
    assert "机型待确认" not in html


def test_trend_png_source_sets_date_axis_labels():
    source = inspect.getsource(email_notifier.build_trend_png)

    assert "set_xticks" in source
    assert "set_xticklabels" in source
    assert "rotation=45" in source
    assert 'bbox_inches="tight"' in source


if __name__ == "__main__":
    test_push_type_uses_transaction_price_when_display_price_only_looks_good()
    test_email_top_summary_separates_display_transaction_and_verify_prices()
    test_email_roundtrip_excluded_single_leg_is_not_compared_to_roundtrip_total()
    test_roundtrip_analysis_builds_excluded_roundtrip_combos()
    test_excluded_roundtrip_combos_dedupes_reason_and_limits_debug_output()
    test_email_roundtrip_excluded_combo_shows_both_legs()
    test_email_detail_charts_dedupe_channels_and_skip_empty_plan_rows()
    test_email_uses_section_cards_and_plan_table_layout()
    test_email_plan_reads_google_nested_flights_fields_for_ca()
    test_trend_png_source_sets_date_axis_labels()
