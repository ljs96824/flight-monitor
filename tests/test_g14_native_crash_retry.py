"""G14：原生模式 Node 子进程以 0xC0000409 快速失败时，只按完全吻合的特征重跑一次。

两个样本(2026-10-13 本机 C2、2026-10-16 本机 A3 基线)特征一致：Node v24.15.0，
test_hanging_json_body_is_aborted(原生 body_hang)，退出码 3221226505，阶段只收到 1–4，stdout 为空。
重跑只用于原生模式、只一次、输入不变；重跑仍失败或特征有任何不符，照常失败，不掩盖。
"""

import json
import subprocess
import unittest
from unittest.mock import patch

from tests import test_ui_smoke_page_target as target


FAST_FAIL = 3221226505
SUCCESS_STDOUT = json.dumps({"kind": "success", "compiled": True})


def phases(*sequences, mode="native"):
    return "".join(
        "G14_PHASE " + json.dumps({"sequence": number, "phase": target.PHASE_NAMES[number - 1],
                                   "node": "v24.15.0", "mode": mode}) + "\n"
        for number in sequences)


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["node", "-e", "HARNESS"], returncode, stdout, stderr)


G14_CRASH = completed(FAST_FAIL, "", phases(1, 2, 3, 4))
SUCCESS = completed(0, SUCCESS_STDOUT, phases(1, 2, 3, 4, 5, 6, 7, 8))


class NativeCrashRetryTest(unittest.TestCase):
    def setUp(self):
        self.runner = target.UiSmokePageTargetTest("test_first_page_has_no_polling_delay")
        self.runner.node = "node"

    def run_with(self, results, mode="native", scenario="body_hang"):
        with patch.object(subprocess, "run", side_effect=list(results)) as run:
            try:
                outcome = self.runner._run(scenario, block="const target = await waitForPageTarget();", mode=mode)
            except AssertionError as error:
                outcome = error
        return outcome, run

    def report(self, error):
        prefix = "NODE_SUBPROCESS_FAILURE "
        self.assertIsInstance(error, AssertionError, "failure_not_raised")
        self.assertTrue(str(error).startswith(prefix))
        return json.loads(str(error)[len(prefix):])

    def test_g14_crash_then_success_is_retried_once_with_warning(self):
        with self.assertWarnsRegex(UserWarning, "^G14_RETRY NODE_SUBPROCESS_FAILURE ") as caught:
            outcome, run = self.run_with([G14_CRASH, SUCCESS])
        self.assertEqual(outcome, {"kind": "success", "compiled": True})
        self.assertEqual(run.call_count, 2, "retried_once")
        self.assertEqual(run.call_args_list[0], run.call_args_list[1], "same_input")
        first = json.loads(str(caught.warning).split("NODE_SUBPROCESS_FAILURE ", 1)[1])
        self.assertEqual((first["returncode"], first["mode"], first["scenario"]), (FAST_FAIL, "native", "body_hang"))
        self.assertEqual([phase["sequence"] for phase in first["received_phases"]], [1, 2, 3, 4])

    def test_g14_crash_twice_fails_with_retry_marker(self):
        with self.assertWarnsRegex(UserWarning, "^G14_RETRY "):
            outcome, run = self.run_with([G14_CRASH, G14_CRASH])
        report = self.report(outcome)
        self.assertEqual(run.call_count, 2)
        self.assertEqual((report["failure_category"], report["returncode"]), ("NONZERO_EXIT", FAST_FAIL))
        self.assertIs(report["g14_retry"], True, "retry_visible_in_failure")

    def test_non_matching_failures_are_not_retried(self):
        cases = (
            ("other_returncode", completed(1, "", phases(1, 2, 3, 4))),
            ("reached_outcome_phase", completed(FAST_FAIL, "", phases(1, 2, 3, 4, 5))),
            ("missing_phase", completed(FAST_FAIL, "", phases(1, 2, 4))),
            ("stdout_present", completed(FAST_FAIL, '{"kind":"success"}', phases(1, 2, 3, 4))),
            ("non_phase_stderr", completed(FAST_FAIL, "", phases(1, 2, 3, 4) + "Assertion failed: canary\n")),
        )
        for label, result in cases:
            with self.subTest(case=label):
                outcome, run = self.run_with([result, SUCCESS])
                report = self.report(outcome)
                self.assertEqual(run.call_count, 1, "not_retried")
                self.assertNotIn("g14_retry", report)

    def test_controlled_mode_is_never_retried(self):
        outcome, run = self.run_with([G14_CRASH, SUCCESS], mode="controlled")
        self.report(outcome)
        self.assertEqual(run.call_count, 1)

    def test_timeout_is_not_retried(self):
        timeout = subprocess.TimeoutExpired(["node"], 30, output="", stderr=phases(1, 2, 3, 4))
        outcome, run = self.run_with([timeout, SUCCESS])
        report = self.report(outcome)
        self.assertEqual((run.call_count, report["failure_category"]), (1, "SUBPROCESS_TIMEOUT"))


if __name__ == "__main__":
    unittest.main()
