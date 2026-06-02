import sys
import types

sys.modules.setdefault("httpx", types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None))

from analyzer import determine_push_type
from notifier import render_email


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
    assert "搜索参考价达标，但预估实付价高于验证购买价" in meta["reasons"]
    assert all("100%" not in reason for reason in meta["reasons"])


def test_email_top_summary_separates_display_transaction_and_verify_prices():
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

    assert "【值得验证】上海 → 大阪 搜索价¥6,522，需确认实付价" == subject
    assert "当前判断：</b>值得验证，不建议直接下单" in html
    assert "原因：</b>搜索参考价达标，但预估实付价高于验证购买价" in html
    assert "搜索参考价：</b>¥6,522" in html
    assert "预估实付价：</b>¥7,182" in html
    assert "本次方案验证价：</b>支付页≤¥6,848" in html
    assert "你的理想入手价：</b>¥7,994" in html
    assert "最高可接受价：</b>¥9,000" in html
    assert "当前价：</b>" not in html


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

    _, html = render_email(payload)

    assert html.count("Google Flights") == 1
    assert "SerpAPI、HasData 2个数据源一致" in html
    assert "方案A: ¥6,522 推荐" in html
    assert "方案B" not in html
    assert "¥6,522 B" not in html


if __name__ == "__main__":
    test_push_type_uses_transaction_price_when_display_price_only_looks_good()
    test_email_top_summary_separates_display_transaction_and_verify_prices()
    test_email_roundtrip_excluded_single_leg_is_not_compared_to_roundtrip_total()
    test_email_detail_charts_dedupe_channels_and_skip_empty_plan_rows()
