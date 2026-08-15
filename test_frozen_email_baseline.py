import hashlib
import io
import json
import re
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from notifier import render_email


FIXTURE_DIR = Path(__file__).resolve().parent / "tests" / "fixtures" / "frozen_email"
PAYLOAD_PATH = FIXTURE_DIR / "economy_payload.json"
EXPECTED_PATH = FIXTURE_DIR / "economy_expected.json"


def _walk(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


class FrozenEmailBaselineTest(unittest.TestCase):
    def test_sanitized_economy_email_matches_frozen_sha256(self):
        payload = json.loads(PAYLOAD_PATH.read_text(encoding="utf-8"))
        expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))

        with redirect_stdout(io.StringIO()):
            _, html = render_email(payload)
        actual_sha256 = hashlib.sha256(html.encode(expected["encoding"])).hexdigest()

        self.assertEqual(expected["renderer"], "notifier.render_email")
        self.assertEqual(expected["algorithm"], "sha256")
        self.assertEqual(
            expected["previous_local_sha256"],
            "d3350c1b9fd1ec8374a7e10dbb3649446a09ad5a9a77b523d598c6ab48b65b12",
        )
        self.assertEqual(expected["reason"], "脱敏入库")
        self.assertEqual(expected["approved_by"], "用户")
        self.assertEqual(actual_sha256, expected["sha256"])

    def test_frozen_payload_keeps_only_sanitized_identifiers(self):
        payload = json.loads(PAYLOAD_PATH.read_text(encoding="utf-8"))
        serialized = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(payload["subscription_id"], "<SUBSCRIPTION_ID>")
        self.assertEqual(payload["snapshot"]["subscription_id"], "<SUBSCRIPTION_ID>")
        self.assertEqual(payload["diff_from_last"]["last_snapshot"]["id"], 0)
        self.assertEqual(
            payload["detail_url"],
            "https://example.invalid/detail/<SUBSCRIPTION_ID>",
        )
        self.assertEqual(
            payload["form_url"],
            "https://example.invalid/form/<SUBSCRIPTION_ID>",
        )
        self.assertEqual(
            payload["feedback_url"],
            "https://example.invalid/feedback/<SUBSCRIPTION_ID>",
        )
        self.assertIsNone(
            re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", serialized),
            "冻结夹具不得包含邮箱地址",
        )

        token_values = []
        for key, value in _walk(payload):
            if key != "url" or not isinstance(value, str):
                continue
            for query_key, query_value in parse_qsl(
                urlsplit(value).query,
                keep_blank_values=True,
            ):
                if query_key.lower() in {"token", "key", "signature", "auth"}:
                    token_values.append(query_value)

        self.assertGreater(len(token_values), 0)
        self.assertEqual(set(token_values), {"<TOKEN>"})


if __name__ == "__main__":
    unittest.main()
