"""Unreadable detail payload contracts with real views and synthetic auth."""
import ast
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import inspect
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from types import FunctionType, SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse


BROKEN_ID = "123e4567-e89b-12d3-a456-426614174040"
VALID_ID = "123e4567-e89b-12d3-a456-426614174041"
MANAGEMENT_TOKEN = "synthetic-management-only-" + "m" * 40
DETAIL_TOKEN = "synthetic-detail-only-" + "d" * 40
NOW = datetime(2026, 9, 1, 2, 10, tzinfo=timezone.utc)
EXPECTED_DECISION = "值得验证(¥680) · 10分钟前"
DETAIL_BASELINE_SHA256 = "7f94b0e9c8324bbde96bfcc44f634cb1c128bfa28f8a6897f150f09de6a02ed3"


class SyntheticReadFailure(RuntimeError):
    pass


class SubscriptionCards(HTMLParser):
    """Associate a rendered decision with the detail link in its own card."""
    def __init__(self):
        super().__init__()
        self.cards = []
        self.depth = 0
        self.card = None
        self.decision_depth = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            self.depth += 1
            classes = attrs.get("class", "").split()
            if "card" in classes:
                self.card = {"depth": self.depth, "ids": [], "decision": ""}
            if self.card is not None and "decision" in classes:
                self.decision_depth = self.depth
        if tag == "a" and self.card is not None:
            url = urlparse(attrs.get("href", ""))
            if url.path == "/detail":
                self.card["ids"].extend(parse_qs(url.query).get("sub", []))

    def handle_data(self, data):
        if self.card is not None and self.decision_depth is not None:
            self.card["decision"] += data

    def handle_endtag(self, tag):
        if tag != "div":
            return
        if self.decision_depth == self.depth:
            self.decision_depth = None
        if self.card is not None and self.card["depth"] == self.depth:
            self.cards.append(self.card)
            self.card = None
        self.depth -= 1


class DetailPayloadDecodeTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        self.stack.enter_context(patch.dict(os.environ, {
            "NO_LIVE_API": "1", "PYTHON_DOTENV_DISABLED": "1",
            "FLASK_SECRET_KEY": "synthetic-session-only-" + "s" * 40,
            "MANAGEMENT_AUTH_REQUIRED": "1", "MANAGEMENT_TOKEN": MANAGEMENT_TOKEN,
            "SHARED_DETAIL_TOKEN": DETAIL_TOKEN,
        }, clear=True))
        self.stack.enter_context(patch.dict(sys.modules, {"dotenv": SimpleNamespace(
            load_dotenv=lambda *a, **k: False, dotenv_values=lambda *a, **k: {})}))
        self.denials = [self.stack.enter_context(patch(name, side_effect=AssertionError(name)))
                       for name in ("socket.socket.connect", "socket.socket.connect_ex",
                                    "socket.socket.sendto", "socket.getaddrinfo",
                                    "smtplib.SMTP", "smtplib.SMTP_SSL", "sqlite3.connect")]
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        import web_form
        self.web = web_form
        self.payloads = self.root / "payloads"
        self.payloads.mkdir()
        for name, path in (("PAGE_PAYLOADS_DIR", self.payloads),
                           ("SUBSCRIPTIONS_PATH", self.root / "subscriptions.json"),
                           ("FEEDBACK_PATH", self.root / "feedback.json")):
            self.stack.enter_context(patch.object(self.web, name, path))
        self.stack.enter_context(patch.dict(self.web.app.config, {
            "TESTING": True, "SECRET_KEY": "synthetic-session-only-" + "s" * 40,
            "SESSION_COOKIE_SECURE": False,
            "CSRF_CLOCK": lambda: 1800000000, "MANAGEMENT_CLOCK": lambda: 1800000000,
        }))
        real_relative = self.web._relative_time_label
        self.stack.enter_context(patch.object(self.web, "_relative_time_label",
                                             side_effect=lambda value: real_relative(value, now=NOW)))
        self.stack.enter_context(patch.object(self.web, "load_calendar", return_value={}))
        for name in ("start_background_collection", "notify_feedback_author"):
            self.denials.append(self.stack.enter_context(
                patch.object(self.web, name, side_effect=AssertionError(name))))
        self.client = self.web.app.test_client()

    def tearDown(self):
        for denial in self.denials:
            denial.assert_not_called()

    def record(self):
        return {"subscription_id": VALID_ID, "created_at": "2026-09-01T10:00:00+08:00",
                "html": "<div>synthetic detail preserved</div>",
                "payload": {"push_type": "值得验证", "current_price": 680}}

    def _write(self, subscription_id, raw):
        path = self.payloads / f"{subscription_id}.json"
        path.write_bytes(raw)
        return path

    def load_observation(self, raw, subscription_id=VALID_ID):
        path = self.payloads / f"{VALID_ID}.json"
        if raw is not None:
            self._write(VALID_ID, raw)
        before = {p.name: p.read_bytes() for p in self.payloads.iterdir()}
        value, error = None, None
        try:
            value = self.web._load_payload_result(subscription_id)
        except Exception as exc:
            error = exc
        self.assertEqual({p.name: p.read_bytes() for p in self.payloads.iterdir()}, before,
                         "PAYLOAD_BYTES_UNCHANGED")
        if raw is None:
            self.assertFalse(path.exists(), "MISSING_NOT_CREATED")
        return value, error

    def assert_unreadable(self, raw, subscription_id=VALID_ID):
        value, error = self.load_observation(raw, subscription_id)
        self.assertIsNone(error, "NO_DECODE_ESCAPE")
        self.assertIsNone(value, "UNREADABLE_NONE")

    def test_valid_object_content_is_preserved(self):
        expected = self.record()
        value, error = self.load_observation(json.dumps(expected, ensure_ascii=False).encode("utf-8"))
        self.assertIsNone(error)
        self.assertEqual(value, expected)

    def test_invalid_json_is_unreadable(self):
        self.assert_unreadable(b'{"payload":')

    def test_non_object_payloads_are_unreadable(self):
        for raw in (b"null", b"[]", b'"text"', b"42"):
            with self.subTest(raw=raw):
                self.assert_unreadable(raw)

    def test_missing_file_and_invalid_uuid_are_unreadable(self):
        self.assert_unreadable(None)
        self.assert_unreadable(b"{}", "not-a-uuid")

    def test_invalid_utf8_is_unreadable(self):
        self.assert_unreadable(b"\xff")

    def test_replacement_decodable_object_is_still_rejected(self):
        self.assert_unreadable(b'{"payload": {"x": "\xff"}}')

    def _login(self):
        self.assertEqual(self.client.get("/subscriptions").status_code, 404)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True)).group(1)
        response = self.client.post("/unlock", data={"csrf_token": token, "token": MANAGEMENT_TOKEN})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/subscriptions")

    def list_observation(self, broken_raw=b"\xff"):
        records = [{"id": sid, "subscription_id": sid, "status": "active",
                    "basic": {"origin": "上海", "destination": "大阪", "route_type": "international",
                              "departure_date": "2026-10-01"}}
                   for sid in (BROKEN_ID, VALID_ID)]
        self.web.SUBSCRIPTIONS_PATH.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        self._write(BROKEN_ID, broken_raw)
        self._write(VALID_ID, json.dumps(self.record(), ensure_ascii=False).encode("utf-8"))
        before = {p.name: p.read_bytes() for p in self.payloads.iterdir()}
        self._login()
        response, error = None, None
        try:
            response = self.client.get("/subscriptions")
        except Exception as exc:
            error = exc
        self.assertEqual({p.name: p.read_bytes() for p in self.payloads.iterdir()}, before)
        return response, error

    def assert_list_cards(self, response):
        self.assertEqual(response.status_code, 200)
        parser = SubscriptionCards()
        parser.feed(response.get_data(as_text=True))
        self.assertEqual(len(parser.cards), 2, "TWO_DISTINCT_CARDS")
        for card in parser.cards:
            self.assertEqual(len(card["ids"]), 1)
        cards = {card["ids"][0]: card["decision"].strip() for card in parser.cards}
        self.assertEqual(set(cards), {BROKEN_ID, VALID_ID})
        self.assertEqual(cards[BROKEN_ID], "最近判断: 暂无", "BROKEN_CARD_FALLBACK")
        self.assertEqual(cards[VALID_ID], "最近判断: " + EXPECTED_DECISION, "NORMAL_CARD_UNCHANGED")
        return cards

    def test_authenticated_list_keeps_each_decision_on_its_card(self):
        response, error = self.list_observation()
        self.assertIsNone(error, "LIST_NO_DECODE_ESCAPE")
        self.assert_list_cards(response)

    def detail_observation(self, raw):
        path = self._write(VALID_ID, raw)
        response, error = None, None
        with patch.object(self.web, "_load_payload_result", wraps=self.web._load_payload_result) as reader:
            try:
                response = self.client.get("/detail", query_string={"sub": VALID_ID, "token": DETAIL_TOKEN})
            except Exception as exc:
                error = exc
            reader.assert_called_once_with(VALID_ID)
        self.assertEqual(path.read_bytes(), raw, "DETAIL_INPUT_BYTES_UNCHANGED")
        return response, error

    def test_authorized_detail_invalid_utf8_returns_404(self):
        response, error = self.detail_observation(b"\xff")
        self.assertIsNone(error, "DETAIL_NO_DECODE_ESCAPE")
        self.assertEqual(response.status_code, 404)

    def test_authorized_detail_valid_payload_matches_baseline(self):
        response, error = self.detail_observation(json.dumps(self.record(), ensure_ascii=False).encode("utf-8"))
        self.assertIsNone(error)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(hashlib.sha256(response.data).hexdigest(), DETAIL_BASELINE_SHA256)

    def test_unlisted_read_exception_still_propagates(self):
        self._write(VALID_ID, b"{}")
        original = SyntheticReadFailure("synthetic unexpected read failure")
        with patch.object(Path, "read_text", side_effect=original):
            with self.assertRaises(SyntheticReadFailure, msg="UNLISTED_ERROR_MUST_PROPAGATE") as caught:
                self.web._load_payload_result(VALID_ID)
        self.assertIs(caught.exception, original)
        self.assertEqual((self.payloads / f"{VALID_ID}.json").read_bytes(), b"{}")

    def _mutated_loader(self, kind):
        source = inspect.getsource(self.web._load_payload_result)
        tree = ast.parse(source)
        old = ast.dump(tree)
        if kind in {"remove_unicode", "broad_catch"}:
            targets = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)
                       and isinstance(node.type, ast.Tuple)
                       and [ast.unparse(item) for item in node.type.elts] == ["json.JSONDecodeError", "OSError", "UnicodeError"]]
            self.assertEqual(len(targets), 1, "MUTATION_MATCH")
            if kind == "remove_unicode":
                targets[0].type.elts.pop()
            else:
                targets[0].type = ast.Name(id="Exception", ctx=ast.Load())
        elif kind == "replace_decode":
            targets = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                       and isinstance(node.func, ast.Attribute) and node.func.attr == "read_text"]
            self.assertEqual(len(targets), 1, "MUTATION_MATCH")
            self.assertEqual([item.arg for item in targets[0].keywords], ["encoding"])
            targets[0].keywords.append(ast.keyword(arg="errors", value=ast.Constant(value="replace")))
        else:
            self.fail("UNKNOWN_MUTATION")
        ast.fix_missing_locations(tree)
        self.assertNotEqual(ast.dump(tree), old, "MUTATION_AST_CHANGED")
        self.assertNotEqual(ast.unparse(tree), ast.unparse(ast.parse(source)), "MUTATION_SOURCE_CHANGED")
        namespace = dict(self.web.__dict__)
        exec(compile(tree, "<detail-payload-mutation>", "exec"), namespace)
        compiled = namespace["_load_payload_result"]
        return FunctionType(compiled.__code__, self.web.__dict__, compiled.__name__)

    def _assert_mutation(self, kind, cases):
        mutant = self._mutated_loader(kind)
        with patch.object(self.web, "_load_payload_result", mutant):
            for method, marker in cases.items():
                with self.subTest(method=method):
                    result = unittest.TestResult()
                    type(self)(method).run(result)
                    self.assertEqual(result.errors, [], "MUTATION_NO_UNRELATED_ERRORS")
                    self.assertTrue(result.failures, "MUTATION_BEHAVIOR_REJECTED")
                    self.assertIn(marker, "\n".join(text for _, text in result.failures))

    def test_mutation_unicode_catch_removed_is_rejected(self):
        self._assert_mutation("remove_unicode", {
            "test_invalid_utf8_is_unreadable": "NO_DECODE_ESCAPE",
            "test_replacement_decodable_object_is_still_rejected": "NO_DECODE_ESCAPE",
            "test_authenticated_list_keeps_each_decision_on_its_card": "LIST_NO_DECODE_ESCAPE",
            "test_authorized_detail_invalid_utf8_returns_404": "DETAIL_NO_DECODE_ESCAPE",
        })

    def test_mutation_replacement_decode_is_rejected(self):
        self._assert_mutation("replace_decode", {
            "test_replacement_decodable_object_is_still_rejected": "UNREADABLE_NONE"})

    def test_mutation_broad_catch_is_rejected(self):
        self._assert_mutation("broad_catch", {
            "test_unlisted_read_exception_still_propagates": "UNLISTED_ERROR_MUST_PROPAGATE"})


if __name__ == "__main__":
    unittest.main()
