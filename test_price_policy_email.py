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


if __name__ == "__main__":
    test_push_type_uses_transaction_price_when_display_price_only_looks_good()
    test_email_top_summary_separates_display_transaction_and_verify_prices()
