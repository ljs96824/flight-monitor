import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
import web_form
from detail_access import delivery_payload_with_detail_token


VALID_ID = "123e4567-e89b-12d3-a456-426614174000"
MISSING_ID = "123e4567-e89b-12d3-a456-426614174001"


class DetailPayloadStorageTest(unittest.TestCase):
    def setUp(self):
        web_form.app.config.update(TESTING=True)

    def _install_tmp_payload_dir(self, tmp_path: Path):
        self.main_data_dir = main.DATA_DIR
        self.main_payloads_dir = main.PAGE_PAYLOADS_DIR
        self.web_payloads_dir = web_form.PAGE_PAYLOADS_DIR
        main.DATA_DIR = tmp_path
        main.PAGE_PAYLOADS_DIR = tmp_path / "payloads"
        web_form.PAGE_PAYLOADS_DIR = tmp_path / "payloads"

    def _restore_payload_dir(self):
        main.DATA_DIR = self.main_data_dir
        main.PAGE_PAYLOADS_DIR = self.main_payloads_dir
        web_form.PAGE_PAYLOADS_DIR = self.web_payloads_dir

    def test_save_result_writes_uuid_payload_without_legacy_index(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            main, "_upload_payload_to_pythonanywhere", return_value=True
        ):
            tmp_path = Path(tmp)
            self._install_tmp_payload_dir(tmp_path)
            try:
                saved = main._save_result_for_page(
                    VALID_ID,
                    "<div>detail ok</div>",
                    {"route": "上海 → 大阪"},
                )
                payload_path = tmp_path / "payloads" / f"{VALID_ID}.json"
            finally:
                self._restore_payload_dir()

            self.assertTrue(saved)
            self.assertIn("detail ok", payload_path.read_text(encoding="utf-8"))
            self.assertFalse((tmp_path / "page_results.json").exists())

    def test_save_result_rejects_non_uuid_identifier(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            main, "_upload_payload_to_pythonanywhere"
        ) as upload:
            tmp_path = Path(tmp)
            self._install_tmp_payload_dir(tmp_path)
            try:
                saved = main._save_result_for_page(
                    "107",
                    "<div>must not exist</div>",
                    {},
                )
            finally:
                self._restore_payload_dir()

            self.assertFalse(saved)
            self.assertFalse((tmp_path / "payloads" / "107.json").exists())
            upload.assert_not_called()

    def test_detail_route_reads_only_existing_uuid_payload(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            main, "_upload_payload_to_pythonanywhere", return_value=True
        ), patch.dict(os.environ, {"SHARED_DETAIL_TOKEN": ""}, clear=False):
            tmp_path = Path(tmp)
            self._install_tmp_payload_dir(tmp_path)
            try:
                main._save_result_for_page(
                    VALID_ID,
                    "<div>detail ok</div>",
                    {"route": "上海 → 大阪"},
                )
                client = web_form.app.test_client()
                response = client.get(f"/detail?sub={VALID_ID}")
                numeric = client.get("/detail?sub=107")
                arbitrary = client.get("/detail?sub=PVG-KIX")
                missing = client.get(f"/detail?sub={MISSING_ID}")
                latest_fallback = client.get("/detail")
            finally:
                self._restore_payload_dir()

        self.assertEqual(response.status_code, 200)
        self.assertIn("detail ok", response.get_data(as_text=True))
        self.assertEqual(numeric.status_code, 404)
        self.assertEqual(arbitrary.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(latest_fallback.status_code, 404)

    def test_shared_detail_token_is_optional_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            main, "_upload_payload_to_pythonanywhere", return_value=True
        ), patch.dict(
            os.environ,
            {"SHARED_DETAIL_TOKEN": "shared-secret"},
            clear=False,
        ):
            tmp_path = Path(tmp)
            self._install_tmp_payload_dir(tmp_path)
            try:
                main._save_result_for_page(VALID_ID, "<div>secured</div>", {})
                client = web_form.app.test_client()
                missing = client.get(f"/detail?sub={VALID_ID}")
                wrong = client.get(f"/detail?sub={VALID_ID}&token=wrong")
                accepted = client.get(
                    f"/detail?sub={VALID_ID}&token=shared-secret"
                )
                stored = (tmp_path / "payloads" / f"{VALID_ID}.json").read_text(
                    encoding="utf-8"
                )
            finally:
                self._restore_payload_dir()

        self.assertEqual(missing.status_code, 404)
        self.assertEqual(wrong.status_code, 404)
        self.assertEqual(accepted.status_code, 200)
        self.assertNotIn("shared-secret", stored)


    def test_delivery_copy_carries_shared_token_without_mutating_payload(self):
        payload = {"detail_url": f"https://example.test/detail?sub={VALID_ID}"}

        delivered = delivery_payload_with_detail_token(payload, token="a token&value")

        self.assertEqual(
            delivered["detail_url"],
            f"https://example.test/detail?sub={VALID_ID}&token=a+token%26value",
        )
        self.assertEqual(
            payload["detail_url"],
            f"https://example.test/detail?sub={VALID_ID}",
        )

if __name__ == "__main__":
    unittest.main()
