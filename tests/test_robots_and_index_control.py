"""Flask response policy, not an access-control or platform-wide guarantee."""

import ast
from contextlib import ExitStack, redirect_stdout
import importlib
import io
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

from flask import Flask


ROOT = Path(__file__).resolve().parents[1]
DISALLOWED = {
    "/detail", "/subscriptions", "/subscription/", "/settings", "/success", "/feedback",
}
SECRET = "index-contract-session-only-" + "s" * 40
TOKEN = "index-contract-management-only-" + "m" * 40
DETAIL_TOKEN = "index-contract-detail-only-" + "d" * 40


class RobotsAndIndexControlTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {
            "NO_LIVE_API": "1", "PYTHON_DOTENV_DISABLED": "1",
            "FLASK_SECRET_KEY": SECRET, "SHARED_DETAIL_TOKEN": DETAIL_TOKEN,
            "MANAGEMENT_TOKEN": TOKEN, "MANAGEMENT_AUTH_REQUIRED": "1",
        }, clear=True))
        self.stack.enter_context(patch("dotenv.load_dotenv", return_value=False))
        self.stack.enter_context(patch("dotenv.dotenv_values", return_value={}, create=True))
        self.denials = {}
        for name in (
            "socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto",
            "socket.create_connection", "socket.getaddrinfo", "smtplib.SMTP",
            "smtplib.SMTP_SSL", "sqlite3.connect", "requests.sessions.Session.request",
            "httpx.get", "httpx.post", "httpx.request", "httpx.Client", "httpx.AsyncClient",
        ):
            self.denials[name] = self.stack.enter_context(
                patch(name, side_effect=AssertionError(name), create=True)
            )
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.web = importlib.import_module("web_form")
        for name, leaf in (("SUBSCRIPTIONS_PATH", "subscriptions.json"),
                           ("FEEDBACK_PATH", "feedback.json"), ("PAGE_PAYLOADS_DIR", "payloads")):
            self.stack.enter_context(patch.object(self.web, name, self.root / leaf))
        self.stack.enter_context(patch.dict(self.web.app.config, self._config()))
        self.stack.enter_context(patch.object(self.web, "load_calendar", return_value={}))
        for name in ("_subscription_repository", "load_subscriptions",
                     "start_background_collection", "save_feedback", "notify_feedback_author"):
            self.denials[name] = self.stack.enter_context(
                patch.object(self.web, name, side_effect=AssertionError(name))
            )
        self.client = self.web.app.test_client()
        self.addCleanup(self._assert_no_io)

    def _config(self):
        return {"TESTING": True, "SECRET_KEY": SECRET, "SESSION_COOKIE_SECURE": False,
                "CSRF_CLOCK": lambda: 1800000000, "MANAGEMENT_CLOCK": lambda: 1800000000}

    def _assert_no_io(self):
        for name, mock in self.denials.items():
            self.assertEqual(mock.call_count, 0, name)
        self.assertEqual(list(self.root.rglob("*")), [])
        for secret in (TOKEN, DETAIL_TOKEN, SECRET):
            self.assertNotIn(secret, self.output.getvalue())

    def _csrf(self, client):
        response = client.get("/unlock")
        self.assertEqual(response.status_code, 200)
        return re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True)).group(1)

    def _assert_index(self, response):
        self.assertEqual(response.headers.getlist("X-Robots-Tag"), ["noindex"], "missing_noindex")

    def _assert_robots(self, response):
        self.assertEqual(response.status_code, 200, "robots_anonymous_status")
        self.assertEqual(response.mimetype, "text/plain", "robots_mimetype")
        fields = []
        for line in response.get_data(as_text=True).splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            key, separator, value = line.partition(":")
            self.assertEqual(separator, ":", "invalid_robots_line")
            fields.append((key.strip().lower(), value.strip()))
        self.assertNotIn(("disallow", "/"), fields, "disallow_all")
        self.assertNotIn("noindex", [key for key, _ in fields], "body_noindex")
        self.assertCountEqual(fields, [("user-agent", "*")] + [
            ("disallow", path) for path in DISALLOWED], "robots_directives")
        self._assert_index(response)

    def _assert_cache(self, response):
        self.assertEqual(response.headers.get("Cache-Control"), "no-store", "auth_cache_changed")

    def test_robots_body_and_headers(self):
        self._assert_robots(self.client.get("/robots.txt"))

    def test_robots_anonymous_with_management_enabled(self):
        with self.client.session_transaction() as state:
            self.assertNotIn("_management_auth", state)
        self.assertEqual(os.environ["MANAGEMENT_AUTH_REQUIRED"], "1")
        for method in ("get", "head", "options"):
            with self.subTest(method=method):
                response = getattr(self.client, method)("/robots.txt")
                self.assertEqual(response.status_code, 200, "robots_anonymous_status")
                self._assert_index(response)

    def test_public_flask_responses_have_noindex(self):
        for path, status in (("/", 200), ("/unlock", 200), ("/price_hint", 200), ("/favicon.ico", 204)):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, status)
                self._assert_index(response)

    def test_errors_and_redirects_have_noindex(self):
        for path in ("/not-a-route", "/subscriptions", "/detail"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)
                self._assert_index(response)
        with self.subTest(path="/lock", status=403):
            response = self.client.post("/lock")
            self.assertEqual(response.status_code, 403)
            self._assert_index(response)
        with self.subTest(path="/unlock", status=302):
            response = self.client.post("/unlock", data={"csrf_token": self._csrf(self.client), "token": TOKEN})
            self.assertEqual(response.status_code, 302)
            self._assert_index(response)

    def test_authentication_cache_and_hook_order(self):
        for method, path in (("get", "/unlock"), ("post", "/unlock"), ("post", "/lock")):
            self._assert_cache(getattr(self.client, method)(path))
        hooks = self.web.app.after_request_funcs[None]
        names = [hook.__name__ for hook in hooks]
        self.assertIn("_indexing_noindex", names, "index_hook_not_registered")
        self.assertLess(names.index("_indexing_noindex"), names.index("_authentication_no_store"))
        calls = []
        def tracked(hook):
            def run(response):
                calls.append(hook.__name__)
                return hook(response)
            return run
        with patch.dict(self.web.app.after_request_funcs, {None: [tracked(hook) for hook in hooks]}):
            response = self.client.get("/unlock")
        self.assertEqual(calls, list(reversed(names)))
        self._assert_cache(response)
        self._assert_index(response)

    def test_index_hook_only_adds_header(self):
        hooks = [hook for hook in self.web.app.after_request_funcs[None] if hook.__name__ == "_indexing_noindex"]
        self.assertEqual(len(hooks), 1, "index_hook_not_registered")
        response = self.web.app.response_class(b"synthetic-body", status=202, mimetype="application/json")
        response.headers["Cache-Control"] = "private, max-age=7"
        before = (response.status_code, response.get_data(), list(response.headers))
        self.assertIs(hooks[0](response), response)
        self.assertEqual((response.status_code, response.get_data(),
                          [(k, v) for k, v in response.headers if k != "X-Robots-Tag"]), before)
        self._assert_index(response)

    def test_existing_response_and_authorization_semantics(self):
        response = self.client.get("/price_hint")
        self.assertEqual(response.mimetype, "application/json")
        self.assertEqual(response.get_json(), {"has_data": False, "scope": "oneway",
                                               "route_type": "", "route_type_label": "\u5f85\u8bc6\u522b"})
        self.assertEqual(self.client.get("/subscriptions").status_code, 404)
        self.assertEqual(self.client.get("/detail").status_code, 404)
        self.assertEqual(self.client.post("/subscribe").status_code, 403)
        self.assertEqual(self.client.post("/subscribe", data={"csrf_token": self._csrf(self.client)}).status_code, 404)
        response = self.client.post("/unlock", data={"csrf_token": self._csrf(self.client), "token": TOKEN})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/subscriptions")
        self._assert_cache(response)
        url = "/detail?sub=00000000-0000-0000-0000-000000000001"
        with patch.object(self.web, "_load_payload_result", return_value={"html": "<p>synthetic</p>", "payload": {}}) as load:
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.get(url + "&token=" + TOKEN).status_code, 404)
            load.assert_not_called()
            self.assertEqual(self.client.get(url + "&token=" + DETAIL_TOKEN).status_code, 200)
            load.assert_called_once()
        response = self.client.post("/lock", data={"csrf_token": self._csrf(self.client)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/unlock")
        self._assert_cache(response)

    def _mutant_app(self, mutation):
        import management_access as access
        from web_security import install_csrf_protection

        tree = ast.parse((ROOT / "web_form.py").read_text(encoding="utf-8"))
        selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name in {"_indexing_noindex", "robots_txt"}]
        self.assertCountEqual([node.name for node in selected], ["_indexing_noindex", "robots_txt"], "mutation_target_missing")
        tree = ast.Module(body=selected, type_ignores=[])
        if mutation == "public_entry":
            source = ast.parse((ROOT / "management_access.py").read_text(encoding="utf-8"))
            selected = [node for node in source.body if isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "PUBLIC_METHODS" for t in node.targets)]
            self.assertEqual(len(selected), 1, "mutation_target_missing")
            tree = ast.Module(body=selected, type_ignores=[])
        before = ast.unparse(tree)
        matches = 0
        if mutation == "remove_hook":
            for node in tree.body:
                if node.name == "_indexing_noindex":
                    self.assertEqual(len(node.decorator_list), 1)
                    node.decorator_list = []
                    matches += 1
        elif mutation in {"disallow_all", "body_noindex"}:
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("User-agent: *\n"):
                    node.value += "Disallow: /\n" if mutation == "disallow_all" else "Noindex: /\n"
                    matches += 1
        elif mutation == "public_entry":
            mapping = tree.body[0].value
            for index, key in enumerate(mapping.keys):
                if isinstance(key, ast.Constant) and key.value == "robots_txt":
                    del mapping.keys[index]
                    del mapping.values[index]
                    matches += 1
                    break
        elif mutation == "cache_override":
            for node in tree.body:
                if node.name == "_indexing_noindex":
                    node.body.insert(-1, ast.parse('response.headers["Cache-Control"] = "public"').body[0])
                    matches += 1
        else:
            self.fail("unknown_mutation")
        self.assertEqual(matches, 1, "mutation_match_count")
        ast.fix_missing_locations(tree)
        after = ast.unparse(tree)
        self.assertNotEqual(before, after, "mutation_did_not_change_source")
        compiled = compile(tree, "<index-policy-mutation>", "exec")
        self.mutation_receipt = {"mutation": mutation, "node_matches": matches,
                                 "source_changed": before != after, "compiled": True}
        app = Flask("index-policy-mutation")
        app.config.update(self._config())
        namespace = {**vars(self.web), "app": app}
        if mutation == "public_entry":
            exec(compiled, namespace)
            self.stack.enter_context(patch.object(access, "PUBLIC_METHODS", namespace["PUBLIC_METHODS"]))
            # Register the unchanged real policy nodes, not a second policy implementation.
            original = ast.parse((ROOT / "web_form.py").read_text(encoding="utf-8"))
            nodes = [node for node in original.body if isinstance(node, ast.FunctionDef)
                     and node.name in {"_indexing_noindex", "robots_txt"}]
            exec(compile(ast.Module(body=nodes, type_ignores=[]), "<real-index-policy>", "exec"), namespace)
        else:
            exec(compiled, namespace)
        install_csrf_protection(app, logger=Mock())
        access.install_management_access(app)
        app.add_url_rule("/unlock", "unlock", self.web.unlock, methods=["GET", "POST"])
        return app

    def _assert_mutation_rejected(self, mutation, reason, check):
        app = self._mutant_app(mutation)
        with self.assertRaisesRegex(AssertionError, reason) as caught:
            check(app.test_client())
        self.mutation_receipt.update(expected_reason=reason, actual_error=str(caught.exception), killed=True)

    def test_mutation_index_hook_removed(self):
        self._assert_mutation_rejected("remove_hook", "missing_noindex",
                                       lambda client: self._assert_index(client.get("/unlock")))

    def test_mutation_disallow_all(self):
        self._assert_mutation_rejected("disallow_all", "disallow_all",
                                       lambda client: self._assert_robots(client.get("/robots.txt")))

    def test_mutation_body_noindex(self):
        self._assert_mutation_rejected("body_noindex", "body_noindex",
                                       lambda client: self._assert_robots(client.get("/robots.txt")))

    def test_mutation_robots_public_entry_removed(self):
        self._assert_mutation_rejected("public_entry", "robots_anonymous_status",
                                       lambda client: self._assert_robots(client.get("/robots.txt")))

    def test_mutation_cache_overwritten(self):
        self._assert_mutation_rejected("cache_override", "auth_cache_changed",
                                       lambda client: self._assert_cache(client.get("/unlock")))


if __name__ == "__main__":
    unittest.main()
