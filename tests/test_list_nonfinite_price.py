"""R6：详情 payload 价格非有限时，"我的监控"列表整页失败。

基线(4bca6fc)：采集端 main._save_result_for_page 以 json.dumps 默认 allow_nan=True 落盘并同步详情 payload，
inf / NaN 写成 Infinity / NaN，"1e400" 读回即 inf；web_form._subscription_last_decision 用 int(price) 拼"(¥…)"，
int(inf) 抛 OverflowError、int(nan) 抛 ValueError，GET /subscriptions 对全部订阅整页失败。
口径：显示价格的条件与采集入口 main._valid_price 一致(float 转换失败含溢出、非有限、不大于 0 即无效)，
只认 int/float 沿用基线；无效时只不显示价格，判断文字与时间照常；正有限价格的文字逐字节不变(含 int 截断)；
取值顺序、落盘与同步都不改。
"""

from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import main
import web_form


SUB_ID = "123e4567-e89b-12d3-a456-426614174060"
NAN_ID = "123e4567-e89b-12d3-a456-426614174061"
OK_ID = "123e4567-e89b-12d3-a456-426614174062"
HUGE_INT = "1" + "0" * 400

# 详情 payload 中 current_price 的 JSON 原文 → 列表"最近判断"(created_at 为空，不带时间)
RAW_CASES = (
    ("Infinity", "值得验证"),
    ("-Infinity", "值得验证"),
    ("NaN", "值得验证"),
    ("1e400", "值得验证"),
    (HUGE_INT, "值得验证"),
    ("-5", "值得验证"),
    ("-0.5", "值得验证"),
    ("0", "值得验证"),
    ("680", "值得验证(¥680)"),
    ("1234.0", "值得验证(¥1,234)"),
    ("680.7", "值得验证(¥680)"),
    ('"680"', "值得验证"),
)


def write_raw_payload(root: Path, subscription_id: str, raw_price: str) -> None:
    (root / f"{subscription_id}.json").write_text(
        '{"created_at": "", "payload": {"push_type": "值得验证", "current_price": %s}}' % raw_price,
        encoding="utf-8",
    )


def subscription(subscription_id: str) -> dict:
    return {
        "id": subscription_id,
        "subscription_id": subscription_id,
        "status": "active",
        "round_trip": False,
        "basic": {"origin": "上海", "destination": "大阪", "route_type": "international",
                  "departure_date": "2026-10-01"},
        "soft_preferences": {"travel_scenarios": ["tourism"]},
    }


class ListPriceTextTest(unittest.TestCase):
    def test_last_decision_matrix(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(web_form, "PAGE_PAYLOADS_DIR", Path(tmp)):
            for raw, expected in RAW_CASES:
                with self.subTest(raw=raw[:12]):
                    write_raw_payload(Path(tmp), SUB_ID, raw)
                    self.assertEqual(web_form._subscription_last_decision(subscription(SUB_ID), 0), expected)

    def test_numeric_validity_matches_collection_entry(self):
        for raw, _expected in RAW_CASES:
            value = json.loads(raw)
            if not isinstance(value, (int, float)):
                continue
            with self.subTest(raw=raw[:12]):
                self.assertIs(bool(web_form._list_price_text(value)), main._valid_price(value))


class ListPageWithWriterOutputTest(unittest.TestCase):
    def test_list_page_renders_when_writer_emits_infinity_and_nan(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            payloads = root / "payloads"
            subscriptions_path = root / "subscriptions.json"
            subscriptions_path.write_text(
                json.dumps([subscription(SUB_ID), subscription(NAN_ID), subscription(OK_ID)], ensure_ascii=False),
                encoding="utf-8",
            )
            upload = Mock(return_value=False)
            stack.enter_context(patch.object(main, "PAGE_PAYLOADS_DIR", payloads))
            stack.enter_context(patch.object(main, "_upload_payload_to_pythonanywhere", upload))
            stack.enter_context(patch.object(web_form, "PAGE_PAYLOADS_DIR", payloads))
            stack.enter_context(patch.object(web_form, "SUBSCRIPTIONS_PATH", subscriptions_path))
            stack.enter_context(patch.dict(web_form.app.config, {"TESTING": True}))
            with redirect_stdout(io.StringIO()):
                main._save_result_for_page(SUB_ID, "<p>x</p>", {"push_type": "已生成", "current_price": float("inf")})
                main._save_result_for_page(NAN_ID, "<p>x</p>", {"push_type": "待核验", "current_price": float("nan")})
                main._save_result_for_page(OK_ID, "<p>x</p>", {"push_type": "值得验证", "current_price": 680})
            written = {sid: (payloads / f"{sid}.json").read_text(encoding="utf-8") for sid in (SUB_ID, NAN_ID)}
            response = web_form.app.test_client().get("/subscriptions")
            body = response.get_data(as_text=True)

        self.assertIn('"current_price": Infinity', written[SUB_ID], "writer_emits_infinity")
        self.assertIn('"current_price": NaN', written[NAN_ID], "writer_emits_nan")
        self.assertEqual(upload.call_count, 3)
        self.assertEqual(response.status_code, 200)
        self.assertIn("最近判断: 已生成 · ", body)
        self.assertIn("最近判断: 待核验 · ", body)
        self.assertIn("最近判断: 值得验证(¥680) · ", body)
        for leaked in ("¥inf", "¥nan", "¥-inf"):
            self.assertNotIn(leaked, body.lower())


if __name__ == "__main__":
    unittest.main()
