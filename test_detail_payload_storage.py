import tempfile
import unittest
import logging
import sys
import types
from pathlib import Path

sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
sys.modules.setdefault("httpx", types.SimpleNamespace(post=lambda *a, **k: None))
logging.basicConfig = lambda *a, **k: None

import main

try:
    import web_form
except ModuleNotFoundError as exc:
    if exc.name == "flask":
        web_form = None
    else:
        raise


class DetailPayloadStorageTest(unittest.TestCase):
    def test_save_result_writes_payload_file_and_legacy_index(self):
        old_main_data_dir = main.DATA_DIR
        old_main_payloads_dir = main.PAGE_PAYLOADS_DIR

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            main.DATA_DIR = tmp_path
            main.PAGE_PAYLOADS_DIR = tmp_path / "payloads"

            try:
                main._save_result_for_page(
                    "sub-123",
                    "<div>detail ok</div>",
                    {"route": "上海 → 大阪"},
                )
                payload_path = tmp_path / "payloads" / "sub-123.json"
                legacy_path = tmp_path / "page_results.json"
                payload_record = payload_path.read_text(encoding="utf-8")
                legacy_record = legacy_path.read_text(encoding="utf-8")
            finally:
                main.DATA_DIR = old_main_data_dir
                main.PAGE_PAYLOADS_DIR = old_main_payloads_dir

        self.assertIn("detail ok", payload_record)
        self.assertIn("sub-123", legacy_record)

    @unittest.skipIf(web_form is None, "Flask is not installed in this test runtime")
    def test_detail_route_reads_payload_by_subscription_id(self):
        old_main_data_dir = main.DATA_DIR
        old_main_payloads_dir = main.PAGE_PAYLOADS_DIR
        old_web_results_path = web_form.PAGE_RESULTS_PATH
        old_web_payloads_dir = web_form.PAGE_PAYLOADS_DIR

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            main.DATA_DIR = tmp_path
            main.PAGE_PAYLOADS_DIR = tmp_path / "payloads"
            web_form.PAGE_RESULTS_PATH = tmp_path / "page_results.json"
            web_form.PAGE_PAYLOADS_DIR = tmp_path / "payloads"

            try:
                main._save_result_for_page(
                    "sub-123",
                    "<div>detail ok</div>",
                    {"route": "上海 → 大阪"},
                )
                response = web_form.app.test_client().get("/detail?sub=sub-123")
            finally:
                main.DATA_DIR = old_main_data_dir
                main.PAGE_PAYLOADS_DIR = old_main_payloads_dir
                web_form.PAGE_RESULTS_PATH = old_web_results_path
                web_form.PAGE_PAYLOADS_DIR = old_web_payloads_dir

        self.assertEqual(response.status_code, 200)
        self.assertIn("detail ok", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
