"""Real Flask responses, synthetic credentials, and byte-exact policy controls."""
import ast
from contextlib import ExitStack, contextmanager, redirect_stdout
import copy
from datetime import datetime, timezone
import inspect
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


HOOK = "_sensitive_response_no_store"
SUB_ID = "123e4567-e89b-12d3-a456-426614174050"
MISSING_ID = "123e4567-e89b-12d3-a456-426614174051"
MANAGER = "synthetic-cache-manager-" + "m" * 40
DETAIL = "synthetic-cache-detail-" + "d" * 40
SECRET = "synthetic-cache-session-" + "s" * 40
NONCE = "synthetic-cache-nonce"
EPOCH = 1790151000
NOW = datetime(2026, 9, 23, 8, 10, tzinfo=timezone.utc)
BASE_URL = "http://cache-contract.test"
CASES = {
    "list_populated": ("/subscriptions", True, 200),
    "list_empty": ("/subscriptions", True, 200),
    "list_denied": ("/subscriptions", False, 404),
    "detail_valid": (f"/detail?sub={SUB_ID}&token={DETAIL}", False, 200),
    "detail_wrong": (f"/detail?sub={SUB_ID}&token=synthetic-wrong", False, 404),
    "detail_missing_token": (f"/detail?sub={SUB_ID}", False, 404),
    "detail_missing_payload": (f"/detail?sub={MISSING_ID}&token={DETAIL}", False, 404),
    "detail_invalid_uuid": (f"/detail?sub=invalid&token={DETAIL}", False, 404),
}


class SensitiveResponseNoStoreTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        self.stack.enter_context(patch.dict(os.environ, {
            "NO_LIVE_API": "1", "PYTHON_DOTENV_DISABLED": "1",
            "FLASK_SECRET_KEY": SECRET, "MANAGEMENT_TOKEN": MANAGER,
            "MANAGEMENT_AUTH_REQUIRED": "1", "SHARED_DETAIL_TOKEN": DETAIL,
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
        for name, leaf in (("SUBSCRIPTIONS_PATH", "subscriptions.json"),
                           ("PAGE_PAYLOADS_DIR", "payloads"), ("FEEDBACK_PATH", "feedback.json")):
            self.stack.enter_context(patch.object(self.web, name, self.root / leaf))
        self.web.PAGE_PAYLOADS_DIR.mkdir()
        self.subscriptions = [{"id": SUB_ID, "subscription_id": SUB_ID, "status": "active",
                               "basic": {"origin": "上海", "destination": "大阪", "route_type": "international",
                                         "departure_date": "2026-10-01"}}]
        self.web.SUBSCRIPTIONS_PATH.write_text(json.dumps(self.subscriptions, ensure_ascii=False), encoding="utf-8")
        (self.web.PAGE_PAYLOADS_DIR / f"{SUB_ID}.json").write_text(json.dumps({
            "subscription_id": SUB_ID, "created_at": "2026-09-23T16:00:00+08:00",
            "html": "<p>synthetic readable detail</p>",
            "payload": {"push_type": "值得验证", "current_price": 680},
        }, ensure_ascii=False), encoding="utf-8")
        self.stack.enter_context(patch.dict(self.web.app.config, {
            "TESTING": True, "SECRET_KEY": SECRET, "SESSION_COOKIE_SECURE": False,
            "CSRF_CLOCK": lambda: EPOCH, "MANAGEMENT_CLOCK": lambda: EPOCH,
        }))
        self.stack.enter_context(patch("web_security.secrets.token_urlsafe", return_value=NONCE))
        self.stack.enter_context(patch("itsdangerous.timed.TimestampSigner.get_timestamp", return_value=EPOCH))
        relative = self.web._relative_time_label
        self.stack.enter_context(patch.object(self.web, "_relative_time_label",
                                             side_effect=lambda value, **_: relative(value, now=NOW)))
        self.stack.enter_context(patch.object(self.web, "load_calendar", return_value={}))
        for name in ("start_background_collection", "notify_feedback_author"):
            self.denials.append(self.stack.enter_context(patch.object(self.web, name, side_effect=AssertionError(name))))
        static = self.root / "static"
        static.mkdir()
        self.asset = static / "synthetic-cache.txt"
        self.asset.write_bytes(b"synthetic static response\n")
        os.utime(self.asset, (EPOCH, EPOCH))
        self.stack.enter_context(patch.object(self.web.app, "_static_folder", str(static)))
        from werkzeug.http import http_date
        self.stack.enter_context(patch("werkzeug.wrappers.response.http_date",
                                     side_effect=lambda timestamp=None: http_date(EPOCH if timestamp is None else timestamp)))
        self.before = self.file_bytes()

    def file_bytes(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in [self.web.SUBSCRIPTIONS_PATH, *self.web.PAGE_PAYLOADS_DIR.rglob("*")]
                if p.is_file()}

    def tearDown(self):
        for denial in self.denials:
            denial.assert_not_called()
        self.assertEqual(self.file_bytes(), self.before, "INPUT_FILE_SET_AND_BYTES_UNCHANGED")
        for secret in (MANAGER, DETAIL, SECRET):
            self.assertNotIn(secret, self.output.getvalue())

    def client(self):
        client = self.web.app.test_client()
        with client.session_transaction(base_url=BASE_URL) as session:
            session["_csrf_nonce"] = NONCE
        return client

    def csrf(self, client):
        response = client.get("/unlock", base_url=BASE_URL)
        self.assertEqual(response.status_code, 200)
        return re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True)).group(1)

    def login(self, client):
        response = client.post("/unlock", base_url=BASE_URL,
                               data={"csrf_token": self.csrf(client), "token": MANAGER})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/subscriptions")
        return response

    @staticmethod
    def snapshot(response):
        try:
            return {"status": response.status_code, "body": response.get_data(), "headers": list(response.headers)}
        finally:
            response.close()

    @contextmanager
    def baseline_hooks(self):
        # The baseline pipeline differs only by absence of this newly authorized hook.
        hooks = self.web.app.after_request_funcs[None]
        with patch.dict(self.web.app.after_request_funcs, {None: [h for h in hooks if h.__name__ != HOOK]}):
            yield

    def request_case(self, name):
        path, authenticated, status = CASES[name]
        original = self.web.SUBSCRIPTIONS_PATH.read_bytes()
        try:
            if name == "list_empty":
                self.web.SUBSCRIPTIONS_PATH.write_text("[]", encoding="utf-8")
            client = self.client()
            if authenticated:
                self.login(client)
            result = self.snapshot(client.get(path, base_url=BASE_URL))
            self.assertEqual(result["status"], status, name + "_STATUS")
            return result
        finally:
            self.web.SUBSCRIPTIONS_PATH.write_bytes(original)

    def assert_no_store(self, name):
        result = self.request_case(name)
        self.assertEqual([v for k, v in result["headers"] if k.lower() == "cache-control"],
                         ["no-store"], "MISSING_NO_STORE_" + name)

    def test_list_populated_no_store(self):
        self.assert_no_store("list_populated")

    def test_list_empty_no_store(self):
        self.assert_no_store("list_empty")

    def test_list_denied_no_store(self):
        self.assert_no_store("list_denied")

    def test_detail_valid_no_store(self):
        self.assert_no_store("detail_valid")

    def test_detail_wrong_no_store(self):
        self.assert_no_store("detail_wrong")

    def test_detail_missing_token_no_store(self):
        self.assert_no_store("detail_missing_token")

    def test_detail_missing_payload_no_store(self):
        self.assert_no_store("detail_missing_payload")

    def test_detail_invalid_uuid_no_store(self):
        self.assert_no_store("detail_invalid_uuid")

    def test_status_and_body_match_baseline_bytes(self):
        for name in CASES:
            with self.subTest(case=name):
                with self.baseline_hooks():
                    baseline = self.request_case(name)
                actual = self.request_case(name)
                self.assertEqual(actual["status"], baseline["status"], "STATUS_CHANGED_" + name)
                self.assertEqual(actual["body"], baseline["body"], "BODY_BYTES_CHANGED_" + name)
                if name == "list_populated":
                    self.assertIn(b'name="csrf_token"', actual["body"])
                    self.assertIn(NONCE.encode("ascii"), actual["body"])

    def test_noindex_survives_sensitive_policy(self):
        for name in CASES:
            with self.subTest(case=name):
                self.assertEqual(dict(self.request_case(name)["headers"])["X-Robots-Tag"], "noindex")

    def test_unlock_lock_keep_no_store(self):
        client = self.client()
        responses = [client.get("/unlock", base_url=BASE_URL),
                     client.post("/unlock", base_url=BASE_URL),
                     client.post("/lock", base_url=BASE_URL), self.login(client),
                     client.post("/lock", base_url=BASE_URL, data={"csrf_token": self.csrf(client)})]
        self.assertEqual([r.status_code for r in responses], [200, 403, 403, 302, 302])
        for response in responses:
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            response.close()

    def assert_other_headers(self, path):
        with self.baseline_hooks():
            baseline = self.snapshot(self.client().get(path, base_url=BASE_URL))
        actual = self.snapshot(self.client().get(path, base_url=BASE_URL))
        self.assertEqual(baseline["status"], 200, "BASELINE_SUCCESS_CONTROL")
        self.assertEqual(actual["status"], 200, "SUCCESS_CONTROL")
        self.assertEqual(actual["headers"], baseline["headers"], "OTHER_HEADERS_CHANGED_" + path)
        if path.startswith("/static/"):
            self.assertEqual(actual["body"], self.asset.read_bytes())
            self.assertIn("Cache-Control", dict(actual["headers"]))
            self.assertIn("Last-Modified", dict(actual["headers"]))

    def test_other_endpoints_keep_baseline_headers(self):
        for path in ("/", "/price_hint", "/robots.txt", "/static/synthetic-cache.txt"):
            with self.subTest(path=path):
                self.assert_other_headers(path)

    def assert_index_header_only(self, path, endpoint):
        with self.web.app.test_request_context(path, base_url=BASE_URL):
            self.assertEqual(self.web.request.endpoint, endpoint, "VALID_TARGET_CONTEXT")
            response = self.web.app.response_class(b"synthetic-body", status=202, mimetype="application/json")
            response.headers["Cache-Control"] = "private, max-age=7"
            before = (response.status_code, response.get_data(), list(response.headers))
            self.assertIs(self.web._indexing_noindex(response), response)
            self.assertEqual(response.headers["Cache-Control"], "private, max-age=7", "INDEX_CHANGED_CACHE_CONTROL")
            self.assertEqual((response.status_code, response.get_data(),
                              [(k, v) for k, v in response.headers if k != "X-Robots-Tag"]), before)
            self.assertEqual(response.headers["X-Robots-Tag"], "noindex")

    def test_index_hook_only_changes_index_header_in_target_contexts(self):
        for path, endpoint in (("/subscriptions", "subscription_list"), ("/detail", "detail")):
            with self.subTest(endpoint=endpoint):
                self.assert_index_header_only(path, endpoint)

    @contextmanager
    def mutant(self, kind):
        hook = getattr(self.web, HOOK)
        tree = ast.parse(inspect.getsource(hook))
        function = tree.body[0]
        self.assertEqual(function.name, HOOK, "MUTATION_MATCH")
        self.assertEqual(len(function.decorator_list), 1, "MUTATION_MATCH")
        function.decorator_list = []
        checks = [n for n in function.body if isinstance(n, ast.If)]
        self.assertEqual(len(checks), 1, "MUTATION_MATCH")
        before = ast.unparse(tree)
        index_tree = None
        if kind == "remove":
            function.body = [ast.Return(value=ast.Name(id="response", ctx=ast.Load()))]
        elif kind == "move_to_index":
            index_tree = ast.parse(inspect.getsource(self.web._indexing_noindex))
            index = index_tree.body[0]
            self.assertEqual(index.name, "_indexing_noindex")
            index.decorator_list = []
            index.body.insert(-1, copy.deepcopy(checks[0]))
            function.body = [ast.Return(value=ast.Name(id="response", ctx=ast.Load()))]
            tree.body.append(index)
        elif kind == "all_endpoints":
            checks[0].test = ast.Constant(True)
        elif kind == "only_200":
            checks[0].test = ast.BoolOp(op=ast.And(), values=[checks[0].test,
                ast.Compare(left=ast.Attribute(value=ast.Name(id="response", ctx=ast.Load()), attr="status_code", ctx=ast.Load()),
                            ops=[ast.Eq()], comparators=[ast.Constant(200)])])
        else:
            self.fail("UNKNOWN_MUTATION")
        ast.fix_missing_locations(tree)
        self.assertNotEqual(ast.unparse(tree), before, "MUTATION_SOURCE_CHANGED")
        namespace = dict(vars(self.web))
        exec(compile(tree, "<sensitive-response-policy-mutation>", "exec"), namespace)
        replacements = {HOOK: namespace[HOOK]}
        if index_tree is not None:
            replacements["_indexing_noindex"] = namespace["_indexing_noindex"]
        with ExitStack() as stack:
            hooks = self.web.app.after_request_funcs[None]
            self.assertEqual(sum(h.__name__ == HOOK for h in hooks), 1, "MUTATION_REGISTERED_MATCH")
            stack.enter_context(patch.dict(self.web.app.after_request_funcs, {
                None: [replacements.get(h.__name__, h) for h in hooks]}))
            for name, value in replacements.items():
                stack.enter_context(patch.object(self.web, name, value))
            yield

    def test_mutation_removed_hook_is_rejected(self):
        with self.mutant("remove"):
            for name in CASES:
                with self.subTest(case=name), self.assertRaisesRegex(AssertionError, "MISSING_NO_STORE_" + name):
                    self.assert_no_store(name)

    def test_mutation_moved_policy_changes_index_cache_header(self):
        with self.mutant("move_to_index"):
            for path, endpoint in (("/subscriptions", "subscription_list"), ("/detail", "detail")):
                with self.subTest(endpoint=endpoint), self.assertRaisesRegex(AssertionError, "INDEX_CHANGED_CACHE_CONTROL"):
                    self.assert_index_header_only(path, endpoint)

    def test_mutation_all_endpoints_is_rejected(self):
        with self.mutant("all_endpoints"):
            for path in ("/", "/price_hint", "/robots.txt", "/static/synthetic-cache.txt"):
                with self.subTest(path=path):
                    with self.assertRaisesRegex(AssertionError, "OTHER_HEADERS_CHANGED"):
                        self.assert_other_headers(path)

    def test_mutation_success_only_misses_denials(self):
        with self.mutant("only_200"):
            for name in CASES:
                if CASES[name][2] == 404:
                    with self.subTest(case=name), self.assertRaisesRegex(AssertionError, "MISSING_NO_STORE_" + name):
                        self.assert_no_store(name)


if __name__ == "__main__":
    unittest.main()
