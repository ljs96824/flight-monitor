"""G15：用量台账并发测试的临时目录清理失败，不得再把测试变成 ERROR，也不得掩盖真实的计数丢失。

CI 样本(run 38571165776 第 1 次运行，Windows)：一次临时文件替换被拒，台账整轮重做；被拒那次的
api_usage.json.*.tmp 删不掉而残留，TemporaryDirectory 退出清理时 rmtree 抛 PermissionError(WinError 32)，
断言没有执行。这里在任何平台上复现同一形态：替换被拒、残留的 .tmp 删不掉。
"""

import io
import os
import shutil
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import test_api_usage_concurrency as target


TARGET_TEST = "test_two_threads_record_one_hundred_entries_each_without_loss"


class SharingViolation:
    """模拟 Windows 上被其他进程占用的台账临时文件：初始化之后的 N 次替换被拒，且 .tmp 一律删不掉。

    被拒的替换都落在第一个遇到拒绝的线程上，因此 N=2 时恰好用尽同一笔记录的整轮重做。
    """

    def __init__(self, replace_failures):
        self.lock = threading.Lock()
        self.passthrough = 1  # 台账初始化那一次照常写入；初始化本身没有重做逻辑，不在模拟范围内
        self.remaining = replace_failures
        self.rejected = 0
        self.owner = None
        self.dirs = set()
        self.real_replace = os.replace
        self.real_unlink = os.unlink

    @staticmethod
    def is_ledger_temp(path):
        name = os.path.basename(os.fspath(path))
        return name.startswith("api_usage.json.") and name.endswith(".tmp")

    def replace(self, src, dst, *args, **kwargs):
        if self.is_ledger_temp(src):
            with self.lock:
                self.dirs.add(os.path.dirname(os.fspath(src)))
                if self.passthrough > 0:
                    self.passthrough -= 1
                elif self.remaining > 0 and self.owner in (None, threading.get_ident()):
                    self.owner = threading.get_ident()
                    self.remaining -= 1
                    self.rejected += 1
                    raise PermissionError(13, "simulated sharing violation", os.fspath(src))
        return self.real_replace(src, dst, *args, **kwargs)

    def unlink(self, path, *args, **kwargs):
        if self.is_ledger_temp(path):
            raise PermissionError(13, "simulated sharing violation", os.fspath(path))
        return self.real_unlink(path, *args, **kwargs)


def run_target(replace_failures):
    violation = SharingViolation(replace_failures)
    result = unittest.TestResult()
    try:
        with (
            patch.object(os, "replace", violation.replace),
            patch.object(os, "unlink", violation.unlink),
            redirect_stdout(io.StringIO()),
        ):
            target.ApiUsageConcurrencyTest(TARGET_TEST).run(result)
        leftovers = [name for folder in violation.dirs if os.path.isdir(folder)
                     for name in os.listdir(folder) if SharingViolation.is_ledger_temp(name)]
    finally:
        for folder in violation.dirs:
            shutil.rmtree(folder, ignore_errors=True)
    return result, violation, leftovers


class ApiUsageCleanupResilienceTest(unittest.TestCase):
    def test_locked_leftover_temp_no_longer_errors(self):
        result, violation, leftovers = run_target(replace_failures=1)
        self.assertEqual(violation.rejected, 1, "replace_rejected_once")
        self.assertTrue(leftovers, "locked_temp_left_behind")
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.errors, [], "cleanup_error_not_raised")
        self.assertEqual(result.failures, [], "ledger_retry_kept_all_entries")

    def test_real_ledger_loss_still_fails(self):
        result, violation, _leftovers = run_target(replace_failures=2)
        self.assertEqual(violation.rejected, 2, "retry_exhausted_for_one_entry")
        self.assertEqual(result.errors, [], "loss_reported_as_failure_not_cleanup_error")
        self.assertEqual(len(result.failures), 1, "loss_not_masked")


if __name__ == "__main__":
    unittest.main()
