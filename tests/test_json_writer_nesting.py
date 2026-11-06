"""R29：另外 5 处自行写 JSON 文件的地方写之前按读取口径检查嵌套层数，读回时会被拒绝的内容不写，原有文件不动。

基线(R28 合并后的 main，a0e801cd，tree f66d4e44)：下列写入方写之前都不查嵌套层数，超过 100 层的内容照样写出——
- main._save_result_for_page(详情载荷)：web_form._load_payload_result 读回时按读不了处理(R26)，详情页 404，回执却记"已保存"
  并照常上传 PythonAnywhere；
- basket_sentinel._write_state、api_usage._atomic_write_json(配额台账)、plan_tracker.save_pushed_plans(已推送方案)：
  各自的读取方自 R26、R27 起同样拒绝超过 100 层的内容；
- forecast.write_backtest_report(回测报告)。
口径(用户 2026-11-05"开工")：五处写之前调用 check_json_nesting(R28)，检查在建目录与临时文件之前；不通过时不写、原有文件
不动，按各写入方已有的失败口径收尾——详情载荷走非 UUID 订阅标识被拒时的回执路径(未保存、不上传)；哨兵状态抛
SentinelStateWriteRefused(ValueError 的子类)，调用方与写盘失败同样记"已通知，但去重状态写入失败"，其他 ValueError 照旧
向外抛(R13b)；配额台账抛 UsageLedgerWriteRefused，record_actual_requests 不重试，与读不了台账一样先留一条待对账审计再
抛出；已推送方案记一行并返回 {}；回测报告抛 ValueError。哨兵状态与配额台账的内容由程序自己组装，实际不会超过 100 层，
这两处检查只为口径统一。本机时钟固定；嵌套用循环构造。
"""

from contextlib import redirect_stdout
from datetime import datetime
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_usage
import atomic_json_store
import basket_sentinel
import forecast
import main
import plan_tracker
import web_form


NOW = datetime(2026, 11, 5, 22, 45, 0)
TOO_DEEP = "JSON 嵌套超过 100 层"
SUBSCRIPTION_ID = "123e4567-e89b-12d3-a456-426614174290"
PA_USER = "ljs96824"
DETAIL_URL = f"https://{PA_USER}.pythonanywhere.com/detail?sub={SUBSCRIPTION_ID}"
# 未保存、未尝试上传时对 PA 详情地址的回执(与非 UUID 订阅标识被拒时相同)
REFUSED_RECEIPT = {
    "local_saved": False,
    "upload": "not_attempted",
    "target": "pythonanywhere",
    "target_host": f"{PA_USER}.pythonanywhere.com",
    "same_directory": None,
    "method": None,
    "target_reached": False,
}
SENTINEL_AFTER = datetime(2026, 10, 18, 21, 0)
SENTINEL_LEDGER = {"entries": [{"round_id": "collection_20261018T090000", "recorded_at": "2026-10-18T09:05:00+08:00"}]}
SENTINEL_ROUNDS = {"readable": True, "rounds": []}


class FixedDateTime(datetime):
    """固定本机时钟：被测模块的 datetime.now() 一律返回 NOW(测试不读宿主时钟)。"""

    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.replace(tzinfo=tz)


def nested(levels: int):
    """levels 层嵌套的空数组，循环构造：nested(1) == []，nested(3) == [[[]]]。"""

    node = []
    for _ in range(levels - 1):
        node = [node]
    return node


def valid_ledger():
    """每次新建一份最小合法台账(record_actual_requests 会原地改它)。"""

    return {"version": 2, "dates": {}, "entries": []}


class TempDirTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)

    def temp_files(self):
        return sorted(path.name for path in self.dir.rglob("*") if path.name.endswith(".tmp"))

    def messages(self, log):
        return [call.args[0] for call in log.call_args_list]

    def spy_open(self):
        """记录期间经 Path.open 打开的文件(两处写入方都用 Path.open 建临时文件)。"""

        return patch.object(Path, "open", autospec=True, side_effect=Path.open)


class DetailPayloadWriteTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.payload_dir = self.dir / "page_payloads"
        for module in (main, web_form):
            self.enterContext(patch.object(module, "PAGE_PAYLOADS_DIR", self.payload_dir))
        self.enterContext(patch.object(main, "datetime", FixedDateTime))
        self.enterContext(patch.dict(os.environ, {"PYTHONANYWHERE_USER": PA_USER}))
        self.upload = self.enterContext(patch.object(main, "_upload_payload_to_pythonanywhere", return_value=False))
        self.log = self.enterContext(patch.object(main, "safe_log"))
        self.log_json = self.enterContext(patch.object(main, "safe_log_json"))
        self.path = self.payload_dir / f"{SUBSCRIPTION_ID}.json"

    def save(self, levels):
        # 写入的整份内容：外层对象(1) + payload 对象(2) + x 下 levels-2 层数组 = levels 层
        return main._save_result_for_page(SUBSCRIPTION_ID, "<html></html>", {"detail_url": DETAIL_URL, "x": nested(levels - 2)})

    def test_over_deep_detail_gets_the_not_saved_receipt_and_touches_nothing(self):
        receipt = self.save(101)
        self.assertEqual(receipt, REFUSED_RECEIPT)
        self.log_json.assert_called_once_with("[详情发布回执] ", receipt)
        self.assertEqual(self.messages(self.log), [f"[详情存储拒绝] 订阅 {SUBSCRIPTION_ID} 的详情内容不写入: {TOO_DEEP}"])
        self.upload.assert_not_called()
        self.assertFalse(self.payload_dir.exists())
        # 与非 UUID 订阅标识被拒时同一条回执路径
        self.assertEqual(main._save_result_for_page("not-a-uuid", "<html></html>", {"detail_url": DETAIL_URL}), receipt)

    def test_previous_detail_survives_a_refusal(self):
        self.assertTrue(self.save(50)["local_saved"])
        before = self.path.read_bytes()
        self.upload.reset_mock()
        self.assertEqual(self.save(101), REFUSED_RECEIPT)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.temp_files(), [])
        self.upload.assert_not_called()
        self.assertEqual(web_form._load_payload_result(SUBSCRIPTION_ID)["payload"], {"detail_url": DETAIL_URL, "x": nested(48)})

    def test_boundary_detail_is_written_and_readable_by_the_detail_page(self):
        receipt = self.save(100)
        self.assertTrue(receipt["local_saved"])
        self.upload.assert_called_once_with(self.path)
        self.assertEqual(web_form._load_payload_result(SUBSCRIPTION_ID)["payload"], {"detail_url": DETAIL_URL, "x": nested(98)})


class SentinelStateWriteTest(TempDirTest):
    def run_sentinel(self, path, calls):
        return basket_sentinel.run_basket_sentinel(
            usage_payload=SENTINEL_LEDGER, now=SENTINEL_AFTER, threshold="20:00", state_path=path,
            round_evidence=SENTINEL_ROUNDS, notifier=lambda title, content: calls.append(title) or True)

    def test_over_deep_state_is_refused_before_any_disk_access(self):
        path = self.dir / "basket_sentinel_state.json"
        path.write_text('{"last_notified_day": "2026-10-17"}', encoding="utf-8")
        before = path.read_bytes()
        with self.spy_open() as opened, self.assertRaises(basket_sentinel.SentinelStateWriteRefused) as caught:
            basket_sentinel._write_state(path, {"last_notified_day": "2026-10-18", "x": nested(100)})
        opened.assert_not_called()
        self.assertEqual(str(caught.exception), TOO_DEEP)
        self.assertIsInstance(caught.exception, ValueError)
        self.assertIs(type(caught.exception.__cause__), ValueError)
        self.assertEqual(path.read_bytes(), before)
        missing = self.dir / "missing" / "basket_sentinel_state.json"
        with self.assertRaises(basket_sentinel.SentinelStateWriteRefused):
            basket_sentinel._write_state(missing, {"x": nested(100)})
        self.assertFalse(missing.parent.exists())
        self.assertEqual(self.temp_files(), [])
        basket_sentinel._write_state(path, {"last_notified_day": "2026-10-18", "x": nested(99)})
        self.assertEqual(basket_sentinel._load_state(path)["x"], nested(99))

    def test_refused_state_write_is_reported_like_a_failed_write(self):
        path = self.dir / "basket_sentinel_state.json"
        calls, logs = [], []
        with redirect_stdout(io.StringIO()), patch.object(basket_sentinel, "safe_log", side_effect=logs.append), \
                patch.object(basket_sentinel, "check_json_nesting", side_effect=ValueError(TOO_DEEP)):
            result = self.run_sentinel(path, calls)
        self.assertEqual((result["notified"], result["status"], result["state_saved"]), (True, "notified", False))
        self.assertEqual(len(calls), 1)
        self.assertIn(f"[篮子哨兵] 已通知，但去重状态写入失败 原因=SentinelStateWriteRefused:{TOO_DEEP}", logs)
        self.assertFalse(path.exists())
        self.assertEqual(self.temp_files(), [])

    def test_other_value_errors_from_writing_still_propagate(self):
        # R13b：写状态只接住 OSError；R29 只多接住自己的 SentinelStateWriteRefused，序列化失败等其他 ValueError 照旧向外抛。
        path = self.dir / "basket_sentinel_state.json"
        calls = []
        with redirect_stdout(io.StringIO()), patch.object(basket_sentinel, "safe_log"), \
                patch.object(basket_sentinel.json, "dump", side_effect=ValueError("Circular reference detected")):
            with self.assertRaisesRegex(ValueError, "Circular reference detected") as caught:
                self.run_sentinel(path, calls)
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(len(calls), 1)
        self.assertFalse(path.exists())
        self.assertEqual(self.temp_files(), [])


class UsageLedgerWriteTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.ledger = self.dir / "api_usage.json"
        self.ledger.write_text(json.dumps(valid_ledger()), encoding="utf-8")
        self.conflict_log = self.dir / "api_usage_conflict.log"

    def test_over_deep_ledger_is_refused_before_any_disk_access(self):
        before = self.ledger.read_bytes()
        with self.spy_open() as opened, self.assertRaises(api_usage.UsageLedgerWriteRefused) as caught:
            api_usage._atomic_write_json(self.ledger, {**valid_ledger(), "x": nested(100)})
        opened.assert_not_called()
        self.assertEqual(str(caught.exception), f"配额台账拒绝写入: {self.ledger}: {TOO_DEEP}")
        self.assertIs(type(caught.exception.__cause__), ValueError)
        self.assertEqual(self.ledger.read_bytes(), before)
        missing = self.dir / "missing" / "api_usage.json"
        with self.assertRaises(api_usage.UsageLedgerWriteRefused):
            api_usage._atomic_write_json(missing, {**valid_ledger(), "x": nested(100)})
        self.assertFalse(missing.parent.exists())
        self.assertEqual(self.temp_files(), [])
        api_usage._atomic_write_json(self.ledger, {**valid_ledger(), "x": nested(99)})
        self.assertEqual(api_usage.load_usage_strict(self.ledger)["x"], nested(99))

    def test_refused_recording_is_not_retried_and_leaves_reconciliation_evidence(self):
        before = self.ledger.read_bytes()
        deep_ledger = {**valid_ledger(), "x": nested(100)}
        with patch.object(api_usage, "load_usage_strict", return_value=deep_ledger) as load, \
                patch.object(api_usage, "safe_log") as log:
            with self.assertRaises(api_usage.UsageLedgerWriteRefused):
                api_usage.record_actual_requests(
                    {"juhe": 2}, path=self.ledger, round_id="r29", day="2026-11-05",
                    recorded_at="2026-11-05T22:45:00+08:00", conflict_log_path=self.conflict_log)
        self.assertEqual(load.call_count, 1)
        log.assert_not_called()
        self.assertEqual(self.ledger.read_bytes(), before)
        evidence = api_usage.list_reconciliation_evidence(self.conflict_log)
        self.assertEqual([(row["round_id"], row["counts"], row["status"]) for row in evidence],
                         [("r29", {"juhe": 2}, "pending_reconciliation")])
        self.assertTrue(evidence[0]["reason"].startswith(f"配额台账拒绝写入: {self.ledger}: "))


class PushedPlansWriteTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(plan_tracker, "datetime", FixedDateTime))
        self.log = self.enterContext(patch.object(plan_tracker, "safe_log"))
        self.store = self.dir / "pushed_plans"
        self.path = plan_tracker._storage_path("sub-r29", self.store)

    def plan(self, levels):
        # 写入的整份内容：外层对象(1) + last_pushed(2) + plan_a(3) + price_tiers(4) + x 下 levels-4 层数组 = levels 层
        return {"is_roundtrip": True, "outbound_flight": {"flight_no": "MU101", "price": 1000},
                "return_flight": {"flight_no": "MU102", "price": 900}, "price": 1900,
                "price_tiers": {"x": nested(levels - 4)}}

    def save(self, levels):
        return plan_tracker.save_pushed_plans("sub-r29", [self.plan(levels)], self.store)

    def test_over_deep_record_is_not_written(self):
        self.assertEqual(self.save(101), {})
        self.assertFalse(self.store.exists())
        self.assertEqual(self.messages(self.log), [f"[方案追踪] 已推送方案不写入 {self.path.name}: {TOO_DEEP}"])

    def test_previous_record_survives_and_the_boundary_is_written(self):
        saved = self.save(100)
        self.assertEqual(saved["last_pushed"]["plan_a"]["price_tiers"]["x"], nested(96))
        self.assertEqual(saved["last_pushed"]["plan_a"]["pushed_at"], "2026-11-05T22:45:00")
        self.assertEqual(plan_tracker.load_pushed_plans("sub-r29", self.store), saved)
        before = self.path.read_bytes()
        self.assertEqual(self.save(101), {})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(plan_tracker.load_pushed_plans("sub-r29", self.store), saved)


class BacktestReportWriteTest(TempDirTest):
    def test_over_deep_report_is_refused_and_the_boundary_is_written(self):
        path = self.dir / "reports" / "backtest.json"
        with self.assertRaisesRegex(ValueError, TOO_DEEP):
            forecast.write_backtest_report({"x": nested(100)}, path)
        self.assertFalse(path.parent.exists())
        forecast.write_backtest_report({"x": nested(99)}, path)
        before = path.read_bytes()
        self.assertEqual(atomic_json_store.parse_json_text(path.read_text(encoding="utf-8")), {"x": nested(99)})
        with self.assertRaisesRegex(ValueError, TOO_DEEP):
            forecast.write_backtest_report({"x": nested(100)}, path)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.temp_files(), [])


if __name__ == "__main__":
    unittest.main()
