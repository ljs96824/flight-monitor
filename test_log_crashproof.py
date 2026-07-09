import io
import sys
import unittest

from log_utils import safe_log
from sources.aggregator import _log_dual_source_price_checks


class LogCrashproofTest(unittest.TestCase):
    def test_dual_source_price_log_survives_gbk_stdout(self):
        buffer = io.BytesIO()
        gbk_stdout = io.TextIOWrapper(buffer, encoding="gbk", errors="strict")
        original_stdout = sys.stdout
        sys.stdout = gbk_stdout
        try:
            anomalies = _log_dual_source_price_checks(
                [
                    {
                        "flight_combo": "BR705+BR182",
                        "data_source": "hasdata+juhe",
                        "source_price_details": [
                            {"source": "hasdata", "price": 2000},
                            {"source": "juhe", "price": 2400},
                        ],
                    }
                ]
            )
            gbk_stdout.flush()
        finally:
            sys.stdout = original_stdout
            gbk_stdout.detach()

        self.assertEqual(len(anomalies), 1)
        output = buffer.getvalue().decode("gbk", errors="replace")
        self.assertIn("CNY2000", output)
        self.assertIn("juhe=CNY2400", output)
        self.assertNotIn("?", output)

    def test_safe_log_degrades_unencodable_text_under_gbk(self):
        buffer = io.BytesIO()
        gbk_stdout = io.TextIOWrapper(buffer, encoding="gbk", errors="strict")
        original_stdout = sys.stdout
        sys.stdout = gbk_stdout
        try:
            safe_log("price=\u00a52000")
            gbk_stdout.flush()
        finally:
            sys.stdout = original_stdout
            gbk_stdout.detach()

        output = buffer.getvalue().decode("gbk", errors="replace")
        self.assertIn("price=\\xa52000", output)


if __name__ == "__main__":
    unittest.main()
