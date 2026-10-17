"""G16：用量台账并发用例的线程等待预算与超时诊断。

CI 样本(run 39031291688 第 1 次运行，Windows)：线程未在 join(timeout=15) 内结束，失败信息只有
"True is not false"，分不清是写得慢还是卡住了；同一提交重跑即通过(第 2 次运行)。
现改为两个线程合计的等待预算 JOIN_TIMEOUT_SECONDS(60 秒)；超时时失败信息写明仍在运行的线程、
已用时间、预算与各线程已写入条数。这里用不写盘的假写入(进度与机器快慢无关)让两个线程各完成 2 笔后卡住，
验证诊断内容，并确认卡住的线程在测试后可回收。
"""

import re
import threading
import unittest
from unittest.mock import patch

import api_usage
import test_api_usage_concurrency as target


TARGET_TEST = "test_two_threads_record_one_hundred_entries_each_without_loss"
STALL_AT = {"a-2", "b-2"}  # 两个线程都卡在各自第 3 次写入之前


class ConcurrencyJoinBudgetTest(unittest.TestCase):
    def test_budget_is_sixty_seconds_total(self):
        self.assertEqual(target.JOIN_TIMEOUT_SECONDS, 60)

    def test_stalled_writers_report_elapsed_budget_and_progress(self):
        released = threading.Event()

        def stalling_record(*args, **kwargs):
            if kwargs.get("round_id") in STALL_AT:
                released.wait(30)
                raise RuntimeError("synthetic stall released")  # 释放后不再做任何文件读写
            return {}  # 前两笔不写盘，直接视为已记录

        result = unittest.TestResult()
        try:
            with (
                patch.object(target, "JOIN_TIMEOUT_SECONDS", 1.0),
                patch.object(api_usage, "record_actual_requests", side_effect=stalling_record),
            ):
                target.ApiUsageConcurrencyTest(TARGET_TEST).run(result)
        finally:
            released.set()
            for thread in threading.enumerate():
                if thread.name in {"ledger-a", "ledger-b"}:
                    thread.join(30)
        self.assertFalse([t for t in threading.enumerate() if t.name in {"ledger-a", "ledger-b"}],
                         "stalled_threads_reclaimed")
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.errors, [], "reported_as_failure_not_error")
        self.assertEqual(len(result.failures), 1, "stall_detected")
        text = result.failures[0][1]
        self.assertIn("threads_still_running=['ledger-a', 'ledger-b']", text)
        self.assertIn("budget=1.0s", text)
        self.assertIn("progress={'a': 2, 'b': 2}", text)
        elapsed = float(re.search(r"elapsed=(\d+\.\d)s", text).group(1))
        self.assertGreaterEqual(elapsed, 0.9, "waited_for_whole_budget")


if __name__ == "__main__":
    unittest.main()
