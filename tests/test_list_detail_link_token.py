"""Real list/detail routes with synthetic, independently scoped credentials."""
import ast
from contextlib import ExitStack, contextmanager, redirect_stdout
import copy
from datetime import datetime, timezone
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
from urllib.parse import parse_qs, unquote_plus, urlsplit


SUB_ID = "123e4567-e89b-12d3-a456-426614174040"
OTHER_ID = "123e4567-e89b-12d3-a456-426614174041"
MANAGEMENT_TOKEN = "synthetic-manager-" + "m" * 40
DETAIL_A = "synthetic-detail-A +&=?#%/"
DETAIL_B = "synthetic-detail-B +&=?#%/"
BASE_URL = "http://list-contract.test"
NOW = datetime(2026, 9, 1, 2, 10, tzinfo=timezone.utc)
BASELINE_ITEMS = (
    {"index": 0, "subscription_id": SUB_ID, "route": "上海 → 大阪",
     "route_type": "international", "route_type_label": "国际", "trip": "单程",
     "dates": "2026-10-01 出发", "status": "active",
     "last_decision": "值得验证(¥680) · 10分钟前", "scenario": "未设置",
     "detail_url": "/detail?sub=" + SUB_ID,
     "delete_url": "/subscription/" + SUB_ID + "/delete"},
    {"index": 1, "subscription_id": OTHER_ID, "route": "上海 → 大阪",
     "route_type": "international", "route_type_label": "国际", "trip": "单程",
     "dates": "2026-10-01 出发", "status": "active",
     "last_decision": "值得验证(¥680) · 10分钟前", "scenario": "未设置",
     "detail_url": "/detail?sub=" + OTHER_ID,
     "delete_url": "/subscription/" + OTHER_ID + "/delete"},
)


class ListCards(HTMLParser):
    """Identify cards through edit links, independently of the detail URL."""
    def __init__(self):
        super().__init__()
        self.cards = []
        self.depth = 0
        self.card = None
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            self.depth += 1
            if "card" in attrs.get("class", "").split():
                self.card = {"depth": self.depth, "edit_ids": [], "links": []}
        if tag == "a" and self.card is not None:
            self.anchor = {"href": attrs.get("href", ""), "text": ""}
            self.card["edit_ids"].extend(parse_qs(urlsplit(self.anchor["href"]).query).get("edit", []))

    def handle_data(self, text):
        if self.anchor is not None:
            self.anchor["text"] += text

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            self.card["links"].append(self.anchor)
            self.anchor = None
        if tag == "div":
            if self.card is not None and self.depth == self.card["depth"]:
                self.cards.append(self.card)
                self.card = None
            self.depth -= 1


class ListDetailLinkTokenTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        self.stack.enter_context(patch.dict(os.environ, {
            "NO_LIVE_API": "1", "PYTHON_DOTENV_DISABLED": "1",
            "FLASK_SECRET_KEY": "synthetic-session-" + "s" * 40,
            "MANAGEMENT_TOKEN": MANAGEMENT_TOKEN, "MANAGEMENT_AUTH_REQUIRED": "1",
            "SHARED_DETAIL_TOKEN": DETAIL_A,
        }, clear=True))
        self.stack.enter_context(patch.dict(sys.modules, {"dotenv": SimpleNamespace(
            load_dotenv=lambda *a, **k: False, dotenv_values=lambda *a, **k: {})}))
        self.denials = [self.stack.enter_context(patch(name, side_effect=AssertionError(name)))
                       for name in ("socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto",
                                    "socket.getaddrinfo", "smtplib.SMTP", "smtplib.SMTP_SSL", "sqlite3.connect")]
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        import web_form
        self.web = web_form
        self.payloads = self.root / "payloads"
        self.payloads.mkdir()
        for name, path in (("PAGE_PAYLOADS_DIR", self.payloads),
                           ("SUBSCRIPTIONS_PATH", self.root / "subscriptions.json"),
                           ("FEEDBACK_PATH", self.root / "feedback.json")):
            self.stack.enter_context(patch.object(self.web, name, path))
        self.stack.enter_context(patch.dict(self.web.app.config, {
            "TESTING": True, "SECRET_KEY": "synthetic-session-" + "s" * 40,
            "SESSION_COOKIE_SECURE": False,
            "CSRF_CLOCK": lambda: 1800000000, "MANAGEMENT_CLOCK": lambda: 1800000000,
        }))
        relative = self.web._relative_time_label
        self.stack.enter_context(patch.object(self.web, "_relative_time_label",
                                             side_effect=lambda value, **_: relative(value, now=NOW)))
        self.stack.enter_context(patch.object(self.web, "load_calendar", return_value={}))
        for name in ("start_background_collection", "notify_feedback_author"):
            self.denials.append(self.stack.enter_context(patch.object(self.web, name, side_effect=AssertionError(name))))
        self.subscriptions = [{"id": sid, "subscription_id": sid, "status": "active",
                               "basic": {"origin": "上海", "destination": "大阪", "route_type": "international",
                                         "departure_date": "2026-10-01"}}
                              for sid in (SUB_ID, OTHER_ID)]
        self.web.SUBSCRIPTIONS_PATH.write_text(json.dumps(self.subscriptions, ensure_ascii=False), encoding="utf-8")
        for sid in (SUB_ID, OTHER_ID):
            (self.payloads / f"{sid}.json").write_text(json.dumps({
                "subscription_id": sid, "created_at": "2026-09-01T10:00:00+08:00",
                "html": "<div>synthetic readable detail</div>",
                "payload": {"push_type": "值得验证", "current_price": 680},
            }, ensure_ascii=False), encoding="utf-8")
        self.before = self.file_bytes()
        self.client = self.web.app.test_client()

    def tearDown(self):
        for denial in self.denials:
            denial.assert_not_called()
        self.assertEqual(self.file_bytes(), self.before, "FILE_SET_AND_BYTES_UNCHANGED")
        for token in (MANAGEMENT_TOKEN, DETAIL_A, DETAIL_B):
            self.assertNotIn(token, self.output.getvalue(), "NO_TOKEN_LOGGING")

    def file_bytes(self):
        paths = [self.web.SUBSCRIPTIONS_PATH, *self.payloads.rglob("*")]
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in paths if p.is_file()}

    def login(self):
        self.assertEqual(self.client.get("/subscriptions", base_url=BASE_URL).status_code, 404)
        page = self.client.get("/", base_url=BASE_URL)
        self.assertEqual(page.status_code, 200)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True)).group(1)
        response = self.client.post("/unlock", base_url=BASE_URL,
                                    data={"csrf_token": csrf, "token": MANAGEMENT_TOKEN})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/subscriptions")

    def list_link(self):
        response = self.client.get("/subscriptions", base_url=BASE_URL)
        self.assertEqual(response.status_code, 200)
        parser = ListCards()
        parser.feed(response.get_data(as_text=True))
        self.assertEqual(len(parser.cards), 2)
        cards = [card for card in parser.cards if card["edit_ids"] == [SUB_ID]]
        self.assertEqual(len(cards), 1, "EXACT_SUBSCRIPTION_CARD")
        links = [link["href"] for link in cards[0]["links"] if link["text"].strip() == "查看详情"]
        self.assertEqual(len(links), 1, "EXACT_DETAIL_ANCHOR")
        return links[0], response

    def assert_query(self, href, token):
        parts = urlsplit(href)
        self.assertEqual(parts.path, "/detail")
        query = parse_qs(parts.query, keep_blank_values=True)
        self.assertEqual(query.get("sub"), [SUB_ID], "SINGLE_CANONICAL_SUB")
        self.assertEqual(query.get("token"), [token], "CURRENT_DETAIL_TOKEN")
        self.assertEqual(set(query), {"sub", "token"})

    def test_authenticated_href_is_followed_without_repair(self):
        self.login()
        href, _ = self.list_link()
        response = self.client.get(href, base_url=BASE_URL)
        self.assertEqual(response.status_code, 200, "EXACT_HREF_MUST_OPEN_DETAIL")
        self.assertIn("synthetic readable detail", response.get_data(as_text=True))

    def test_link_query_has_one_canonical_sub_and_current_token(self):
        self.login()
        href, _ = self.list_link()
        self.assert_query(href, DETAIL_A)

    def test_both_management_settings_off_never_exposes_token(self):
        os.environ.pop("MANAGEMENT_TOKEN")
        os.environ["MANAGEMENT_AUTH_REQUIRED"] = "0"
        href, response = self.list_link()
        self.assertNotIn("token", parse_qs(urlsplit(href).query), "UNAUTHENTICATED_LINK_NO_TOKEN")
        self.assertNotIn(DETAIL_A, href)
        self.assertNotIn(DETAIL_A, response.get_data(as_text=True), "UNAUTHENTICATED_BODY_NO_TOKEN")
        self.assertNotIn(DETAIL_A, unquote_plus(response.get_data(as_text=True)), "UNAUTHENTICATED_BODY_NO_ENCODED_TOKEN")
        self.assertEqual(self.client.get(href, base_url=BASE_URL).status_code, 404)

    def test_protected_list_rejects_anonymous_viewer(self):
        self.assertEqual(self.client.get("/subscriptions", base_url=BASE_URL).status_code, 404)
        os.environ["MANAGEMENT_AUTH_REQUIRED"] = "0"
        self.assertEqual(self.client.get("/subscriptions", base_url=BASE_URL).status_code, 404)

    def test_management_session_cannot_replace_detail_token(self):
        self.login()
        self.assertEqual(self.client.get("/detail", base_url=BASE_URL,
                                         query_string={"sub": SUB_ID, "token": DETAIL_A}).status_code, 200)
        self.assertEqual(self.client.get("/detail", base_url=BASE_URL,
                                         query_string={"sub": SUB_ID}).status_code, 404,
                         "MANAGEMENT_SESSION_IS_NOT_DETAIL_AUTH")

    def test_wrong_and_missing_detail_tokens_are_rejected(self):
        self.assertEqual(self.client.get("/detail", base_url=BASE_URL,
                                         query_string={"sub": SUB_ID, "token": DETAIL_A}).status_code, 200)
        for query in ({"sub": SUB_ID}, {"sub": SUB_ID, "token": "synthetic-wrong"}):
            with self.subTest(query_keys=sorted(query)):
                self.assertEqual(self.client.get("/detail", base_url=BASE_URL, query_string=query).status_code, 404)

    def test_rotation_is_visible_in_the_same_process(self):
        self.login()
        self.list_link()
        os.environ["SHARED_DETAIL_TOKEN"] = DETAIL_B
        href, _ = self.list_link()
        self.assert_query(href, DETAIL_B)
        self.assertEqual(self.client.get(href, base_url=BASE_URL).status_code, 200)

    def test_optional_detail_token_keeps_link_usable(self):
        os.environ.pop("SHARED_DETAIL_TOKEN")
        self.login()
        href, _ = self.list_link()
        self.assertNotIn("token", parse_qs(urlsplit(href).query))
        self.assertEqual(self.client.get(href, base_url=BASE_URL).status_code, 200)

    def items(self, **kwargs):
        with self.web.app.test_request_context("/subscriptions", base_url=BASE_URL):
            return self.web.build_subscription_list_items(copy.deepcopy(self.subscriptions), **kwargs)

    @staticmethod
    def field_bytes(items):
        return [{key: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                 for key, value in item.items()} for item in items]

    def test_explicit_attachment_changes_only_detail_url(self):
        attached = self.items(attach_detail_token=True)
        plain = self.items(attach_detail_token=False)
        self.assertEqual(len(attached), len(plain))
        for index, (new, old) in enumerate(zip(attached, plain)):
            self.assertEqual(set(new), set(old))
            self.assertNotEqual(new["detail_url"], old["detail_url"])
            self.assertEqual({k: v for k, v in self.field_bytes([new])[0].items() if k != "detail_url"},
                             {k: v for k, v in self.field_bytes([old])[0].items() if k != "detail_url"})
            self.assertEqual(parse_qs(urlsplit(new["detail_url"]).query),
                             {"sub": [self.subscriptions[index]["id"]], "token": [DETAIL_A]})
        signature = inspect.signature(self.web.build_subscription_list_items)
        parameter = signature.parameters["attach_detail_token"]
        self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(parameter.default, False)

    def test_default_constructor_matches_baseline_field_bytes(self):
        self.assertEqual(self.field_bytes(self.items()), self.field_bytes(BASELINE_ITEMS), "DEFAULT_FIELDS_UNCHANGED")

    def test_rendering_preserves_subscription_and_payload_file_set(self):
        self.login()
        self.list_link()
        self.assertEqual(self.file_bytes(), self.before, "FILE_SET_AND_BYTES_UNCHANGED")

    @contextmanager
    def mutated(self, kind):
        symbol = ("subscription_list" if kind == "unconditional" else
                  "detail" if kind == "session_bypass" else "build_subscription_list_items")
        tree = ast.parse(inspect.getsource(getattr(self.web, symbol)))
        function = tree.body[0]
        function.decorator_list = []
        before = ast.dump(tree)
        before_source = ast.unparse(tree)
        bindings = {}
        if kind == "unconditional":
            nodes = [n for n in ast.walk(function) if isinstance(n, ast.keyword) and n.arg == "attach_detail_token"]
            self.assertEqual(len(nodes), 1, "MUTATION_MATCH")
            self.assertEqual(ast.unparse(nodes[0].value), "management_session_authorized()")
            nodes[0].value = ast.Constant(True)
        elif kind == "remove_attachment":
            nodes = [n for n in ast.walk(function) if isinstance(n, ast.If)
                     and isinstance(n.test, ast.Name) and n.test.id == "attach_detail_token"]
            self.assertEqual(len(nodes), 1, "MUTATION_MATCH")
            nodes[0].body = [ast.Pass()]
        elif kind == "session_bypass":
            nodes = [n for n in ast.walk(function) if isinstance(n, ast.If)
                     and ast.unparse(n.test) == "not detail_token_authorized(request.args.get('token'))"]
            self.assertEqual(len(nodes), 1, "MUTATION_MATCH")
            nodes[0].test = ast.BoolOp(op=ast.And(), values=[nodes[0].test,
                ast.UnaryOp(op=ast.Not(), operand=ast.Call(func=ast.Name(id="management_session_authorized", ctx=ast.Load()), args=[], keywords=[]))])
        elif kind in {"wrong_token", "cached_token"}:
            nodes = [n for n in ast.walk(function) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name) and n.func.id == "delivery_payload_with_detail_token"]
            self.assertEqual(len(nodes), 1, "MUTATION_MATCH")
            self.assertEqual(nodes[0].keywords, [])
            value = ast.Constant("synthetic-wrong-token")
            if kind == "cached_token":
                prefix = ast.parse("from detail_access import shared_detail_token\n_cached_list_detail_token = shared_detail_token()\n").body
                tree.body = prefix + tree.body
                value = ast.Name(id="_cached_list_detail_token", ctx=ast.Load())
            nodes[0].keywords.append(ast.keyword(arg="token", value=value))
        else:
            self.fail("UNKNOWN_MUTATION")
        ast.fix_missing_locations(tree)
        self.assertNotEqual(ast.dump(tree), before, "MUTATION_AST_CHANGED")
        self.assertNotEqual(ast.unparse(tree), before_source, "MUTATION_SOURCE_CHANGED")
        namespace = dict(self.web.__dict__)
        exec(compile(tree, "<list-detail-link-mutation>", "exec"), namespace)
        compiled = namespace[symbol]
        mutant = FunctionType(compiled.__code__, self.web.__dict__, symbol, compiled.__defaults__)
        mutant.__kwdefaults__ = compiled.__kwdefaults__
        if kind == "cached_token":
            bindings["_cached_list_detail_token"] = namespace["_cached_list_detail_token"]
            self.assertEqual(bindings["_cached_list_detail_token"], DETAIL_A)
        with ExitStack() as stack:
            stack.enter_context(patch.object(self.web, symbol, mutant))
            stack.enter_context(patch.dict(self.web.__dict__, bindings))
            if symbol in {"subscription_list", "detail"}:
                stack.enter_context(patch.dict(self.web.app.view_functions, {symbol: mutant}))
            yield

    def assert_mutation_rejected(self, kind, methods):
        with self.mutated(kind):
            for method, marker in methods.items():
                with self.subTest(method=method):
                    result = unittest.TestResult()
                    type(self)(method).run(result)
                    self.assertEqual(result.errors, [], "MUTATION_NO_UNRELATED_ERRORS")
                    self.assertTrue(result.failures, "MUTATION_REJECTED")
                    self.assertIn(marker, "\n".join(trace for _, trace in result.failures))

    def test_mutation_unconditional_attachment_is_rejected(self):
        self.assert_mutation_rejected("unconditional", {
            "test_both_management_settings_off_never_exposes_token": "UNAUTHENTICATED_LINK_NO_TOKEN"})

    def test_mutation_removed_attachment_is_rejected(self):
        self.assert_mutation_rejected("remove_attachment", {
            "test_authenticated_href_is_followed_without_repair": "EXACT_HREF_MUST_OPEN_DETAIL",
            "test_link_query_has_one_canonical_sub_and_current_token": "CURRENT_DETAIL_TOKEN"})

    def test_mutation_management_session_bypass_is_rejected(self):
        self.assert_mutation_rejected("session_bypass", {
            "test_management_session_cannot_replace_detail_token": "MANAGEMENT_SESSION_IS_NOT_DETAIL_AUTH"})

    def test_mutation_wrong_token_is_rejected(self):
        self.assert_mutation_rejected("wrong_token", {
            "test_authenticated_href_is_followed_without_repair": "EXACT_HREF_MUST_OPEN_DETAIL",
            "test_link_query_has_one_canonical_sub_and_current_token": "CURRENT_DETAIL_TOKEN"})

    def test_mutation_cached_token_is_rejected(self):
        self.assert_mutation_rejected("cached_token", {
            "test_rotation_is_visible_in_the_same_process": "CURRENT_DETAIL_TOKEN"})


if __name__ == "__main__":
    unittest.main()
