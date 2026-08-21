import tempfile
import unittest
from datetime import datetime
from pathlib import Path


class RoundLogArchiveTest(unittest.TestCase):
    def test_round_archive_appends_boundary_logs_and_redacted_evidence(self):
        from log_utils import (
            append_round_evidence,
            end_round_log_archive,
            safe_log,
            start_round_log_archive,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = start_round_log_archive(
                "20260812T210000_sub",
                root_dir=root,
                now=datetime(2026, 8, 12, 21, 0, 0),
            )
            self.addCleanup(end_round_log_archive)
            safe_log("[测试轮档] 普通日志")
            append_round_evidence(
                "[源响应证据] 源=juhe raw=",
                {"api_key": "secret-value", "reason": "HTTP成功但空结果"},
            )
            append_round_evidence(
                "[source evidence] ",
                {"url": "https://example.test?a=1&access_token=other-secret"},
            )
            safe_log("[邮件失败] recipient=private@example.com")
            append_round_evidence(
                "[通知证据] ",
                {
                    "email": "owner@example.com",
                    "reason": "SMTP rejected owner@example.com",
                },
            )
            end_round_log_archive(status="ok")

            content = path.read_text(encoding="utf-8")
            self.assertIn("round_id=20260812T210000_sub", content)
            self.assertIn("[测试轮档] 普通日志", content)
            self.assertIn("HTTP成功但空结果", content)
            self.assertIn('"api_key": "***"', content)
            self.assertNotIn("secret-value", content)
            self.assertNotIn("other-secret", content)
            self.assertNotIn("private@example.com", content)
            self.assertNotIn("owner@example.com", content)
            self.assertIn("<EMAIL>", content)
            self.assertIn("status=ok", content)

    def test_round_archive_is_append_only_for_same_day(self):
        from log_utils import end_round_log_archive, safe_log, start_round_log_archive

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = start_round_log_archive(
                "round-a",
                root_dir=root,
                now=datetime(2026, 8, 12, 20, 0, 0),
            )
            safe_log("first")
            end_round_log_archive(status="ok")
            second = start_round_log_archive(
                "round-b",
                root_dir=root,
                now=datetime(2026, 8, 12, 21, 0, 0),
            )
            safe_log("second")
            end_round_log_archive(status="ok")

            self.assertEqual(first, second)
            content = second.read_text(encoding="utf-8")
            self.assertIn("round_id=round-a", content)
            self.assertIn("round_id=round-b", content)
            self.assertIn("first", content)
            self.assertIn("second", content)


if __name__ == "__main__":
    unittest.main()
