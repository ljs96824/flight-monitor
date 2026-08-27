import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


class ApiUsageShanghaiDayTest(unittest.TestCase):
    def test_default_ledger_day_uses_project_timezone(self):
        from api_usage import load_usage, record_actual_requests
        from project_time import SHANGHAI_TZ

        class FixedDatetime:
            @classmethod
            def now(cls, timezone=None):
                if timezone is not None:
                    self.assertIs(timezone, SHANGHAI_TZ)
                return datetime(2026, 8, 28, 0, 5, tzinfo=timezone or SHANGHAI_TZ)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "api_usage.json"
            with patch("api_usage.datetime", FixedDatetime):
                record_actual_requests({"juhe": 1}, path=path)
            payload = load_usage(path)

        self.assertEqual(payload["entries"][0]["day"], "2026-08-28")


if __name__ == "__main__":
    unittest.main()
