"""Unknown explicit IDs are not success; repository failures stay failures."""

from contextlib import ExitStack, redirect_stdout
import ast
from copy import deepcopy
import importlib
import inspect
import io
import json
import os
from pathlib import Path
import re
import tempfile
from types import FunctionType
import unittest
from unittest.mock import patch


KNOWN_ID = "00000000-0000-0000-0000-000000000071"
UNKNOWN_ID = "00000000-0000-0000-0000-000000000072"
TOKEN = "success-contract-only-" + "m" * 40
SECRET = "success-session-only-" + "s" * 40
FIXED_EPOCH = 1800000000
CASES = (
    ("unknown_uuid", {"subscription_id": UNKNOWN_ID}, 404),
    ("malformed_id", {"subscription_id": "not-a-uuid"}, 404),
    ("existing", {"subscription_id": KNOWN_ID}, 200),
    ("no_id", {}, 200),
    ("index_out_of_bounds", {"index": "999"}, 200),
)


def subscription():
    return {
        "subscription_id": KNOWN_ID, "status": "active",
        "origin": "PVG", "destination": "KIX", "depart_date": "2026-10-01",
        "round_trip": False, "route_type": "international",
        "basic": {"origin": "PVG", "destination": "KIX",
                  "departure_date": "2026-10-01", "route_type": "international"},
        "hard_constraints": {"max_budget": 1000}, "soft_preferences": {},
        "notification_goals": {"method": "page_only", "email": ""},
    }


class SuccessUnknownSubscriptionTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        self.path = self.root / "subscriptions.json"
        self.stack.enter_context(patch.dict(os.environ, {
            "NO_LIVE_API": "1", "PYTHON_DOTENV_DISABLED": "1",
            "FLASK_SECRET_KEY": SECRET, "MANAGEMENT_TOKEN": TOKEN,
            "MANAGEMENT_AUTH_REQUIRED": "1",
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
                patch(name, side_effect=AssertionError(name), create=True))
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.web = importlib.import_module("web_form")
        for name, value in (
            ("SUBSCRIPTIONS_PATH", self.path), ("FEEDBACK_PATH", self.root / "feedback.json"),
            ("PAGE_PAYLOADS_DIR", self.root / "payloads"),
        ):
            self.stack.enter_context(patch.object(self.web, name, value))
        self.stack.enter_context(patch.dict(self.web.app.config, {
            "TESTING": True, "PROPAGATE_EXCEPTIONS": False, "SECRET_KEY": SECRET,
            "SESSION_COOKIE_SECURE": False, "CSRF_CLOCK": lambda: FIXED_EPOCH,
            "MANAGEMENT_CLOCK": lambda: FIXED_EPOCH,
        }))
        self.stack.enter_context(patch.object(self.web.app, "log_exception"))
        self.stack.enter_context(patch.object(self.web, "safe_log"))
        for name in ("start_background_collection", "load_calendar", "save_feedback",
                     "notify_feedback_author"):
            self.denials[name] = self.stack.enter_context(
                patch.object(self.web, name, side_effect=AssertionError(name)))
        self._write([subscription()])
        self.addCleanup(self._assert_no_external_actions)

    def _assert_no_external_actions(self):
        for name, spy in self.denials.items():
            self.assertEqual(spy.call_count, 0, name)
        self.assertNotIn(TOKEN, self.output.getvalue())
        self.assertNotIn(SECRET, self.output.getvalue())

    def _write(self, payload):
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def _csrf(self, client):
        response = client.get("/unlock")
        self.assertEqual(response.status_code, 200)
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
        self.assertIsNotNone(match)
        return match.group(1)

    def _client(self, enforced, *, login=True):
        self.stack.enter_context(patch.dict(os.environ, {
            "MANAGEMENT_AUTH_REQUIRED": "1" if enforced else "0",
            "MANAGEMENT_TOKEN": TOKEN if enforced else "",
        }))
        client = self.web.app.test_client()
        if enforced and login:
            response = client.post("/unlock", data={"csrf_token": self._csrf(client), "token": TOKEN})
            self.assertEqual(response.status_code, 302)
        return client

    def _assert_case(self, client, case):
        name, query, expected = case
        before = self.path.read_bytes()
        with patch.object(self.web, "render_template_string", wraps=self.web.render_template_string) as render:
            response = client.get("/success", query_string=query)
        self.last_response = response
        self.assertEqual(response.status_code, expected, f"success_status:{name}")
        self.assertEqual(self.path.read_bytes(), before)
        if expected == 404:
            render.assert_not_called()
        else:
            render.assert_called_once()
            context = render.call_args.kwargs
            self.assertEqual(context["startup_status"], "started", "started_default")
            if name in {"existing", "no_id"}:
                self.assertEqual(context["subscription_id"], KNOWN_ID)
                self.assertEqual(context["summary"]["route"], "\u4e0a\u6d77PVG \u2192 KIX")
                self.assertIn("\u4e0a\u6d77PVG \u2192 KIX", response.get_data(as_text=True))
                self.assertIn(KNOWN_ID, response.get_data(as_text=True))
            else:
                self.assertEqual(context["summary"], {})

    def test_core_matrix(self):
        for enforced in (False, True):
            client = self._client(enforced)
            for case in CASES:
                with self.subTest(enforced=enforced, case=case[0]):
                    self._assert_case(client, case)

    def test_empty_list_without_id_still_renders(self):
        self._write([])
        for enforced in (False, True):
            with self.subTest(enforced=enforced):
                client = self._client(enforced)
                self._assert_case(client, ("empty_list", {}, 200))

    def test_owner_mismatch_returns_not_found(self):
        client = self._client(False)
        repository = self.web.SubscriptionRepository(self.path, local_owner_id="other-synthetic-owner")
        self.assertIsNone(repository.get(self.web.LOCAL_OWNER_ID, KNOWN_ID))
        with patch.object(self.web, "_subscription_repository", return_value=repository):
            self._assert_case(client, ("owner_mismatch", {"subscription_id": KNOWN_ID}, 404))

    def _assert_repository_failure(self, client, kind, phase):
        from subscription_repository import DuplicateSubscriptionIdError, SubscriptionIdentityMigrationRequired
        legacy = subscription()
        legacy.pop("subscription_id")
        invalid, exception, expected = {
            "migration": ([legacy], SubscriptionIdentityMigrationRequired, 503),
            "duplicate": ([subscription(), deepcopy(subscription())], DuplicateSubscriptionIdError, 503),
            "array": ({"not": "an array"}, ValueError, 500),
        }[kind]
        self._write(invalid)
        with self.assertRaises(exception):
            self.web._subscription_repository().get(self.web.LOCAL_OWNER_ID, KNOWN_ID)
        invalid_bytes = self.path.read_bytes()
        with ExitStack() as stack:
            if phase == "get":
                self._write([subscription()])
                original_load = self.web.load_subscriptions

                def change_after_list():
                    snapshot = original_load()
                    self._write(invalid)
                    return snapshot

                stack.enter_context(patch.object(self.web, "load_subscriptions", side_effect=change_after_list))
            response = client.get("/success", query_string={"subscription_id": KNOWN_ID})
        self.last_response = response
        self.assertEqual(response.status_code, expected, f"repository_failure:{kind}:{phase}")
        self.assertEqual(self.path.read_bytes(), invalid_bytes)
        if kind == "migration":
            self.assertIn("migrate_subscription_ids.py --write", response.get_data(as_text=True))

    def test_repository_failures_keep_existing_http_status(self):
        for enforced in (False, True):
            client = self._client(enforced)
            for kind in ("migration", "duplicate", "array"):
                for phase in ("load", "get"):
                    with self.subTest(enforced=enforced, kind=kind, phase=phase):
                        self._assert_repository_failure(client, kind, phase)

    def test_quick_update_redirect_still_reaches_success(self):
        for enforced in (False, True):
            with self.subTest(enforced=enforced):
                self._write([subscription()])
                client = self._client(enforced)
                response = client.post(f"/subscriptions/{KNOWN_ID}/quick-update", data={
                    "csrf_token": self._csrf(client), "field": "time_preference", "value": "daytime"})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], f"/success?subscription_id={KNOWN_ID}")
                saved = json.loads(self.path.read_bytes())
                self.assertEqual(saved[0]["hard_constraints"]["time_preference"], "daytime")
                self.assertEqual(saved[0]["subscription_id"], KNOWN_ID)
                self._assert_case(client, CASES[2])

    def test_locked_management_gate_remains_404_before_repository(self):
        client = self._client(True, login=False)
        with patch.object(self.web, "_subscription_repository", side_effect=AssertionError("repository_reached")) as factory:
            for case in CASES:
                with self.subTest(case=case[0]):
                    response = client.get("/success", query_string=case[1])
                    self.assertEqual(response.status_code, 404)
            factory.assert_not_called()

    def _mutated_success(self, kind):
        tree = ast.parse(inspect.getsource(self.web.success))
        function = tree.body[0]
        function.decorator_list = []
        original = ast.unparse(tree)
        explicit = [n for n in function.body if isinstance(n, ast.If) and ast.unparse(n.test) == "requested_id"]
        self.assertEqual(len(explicit), 1, "mutation_explicit_branch_count")
        branch = explicit[0]
        matches = 0
        if kind == "missing_gate":
            gates = [n for n in branch.body if isinstance(n, ast.If) and ast.unparse(n.test) == "subscription is None"]
            self.assertEqual(len(gates), 1, "mutation_gate_count")
            branch.body.remove(gates[0])
            assignment = branch.body[0]
            self.assertIsInstance(assignment, ast.Assign)
            self.assertEqual(ast.unparse(assignment.targets[0]), "subscription")
            assignment.value = ast.BoolOp(op=ast.Or(), values=[assignment.value, ast.Dict(keys=[], values=[])])
            matches = 1
        elif kind == "no_id_gate":
            function.body.insert(function.body.index(branch), ast.parse("if not requested_id:\n    abort(404)").body[0])
            matches = 1
        elif kind == "mask_exception":
            assignment = branch.body[0]
            self.assertIsInstance(assignment, ast.Assign)
            self.assertEqual(ast.unparse(assignment.targets[0]), "subscription")
            wrapper = ast.parse("try:\n    pass\nexcept Exception:\n    abort(404)").body[0]
            wrapper.body = [assignment]
            branch.body[0] = wrapper
            matches = 1
        elif kind == "started_default":
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and ast.unparse(node.func) == "str"
                        and len(node.args) == 1
                        and ast.unparse(node.args[0]) == "selected_attempt.get('status') or 'started'"):
                    node.args[0] = node.args[0].values[0]
                    matches += 1
        self.assertEqual(matches, 1, f"mutation_node_count:{kind}")
        self.assertNotEqual(ast.unparse(tree), original, f"mutation_source_changed:{kind}")
        code = compile(ast.fix_missing_locations(tree), f"<success-mutation-{kind}>", "exec")
        namespace = dict(vars(self.web))
        exec(code, namespace)
        self.assertTrue(callable(namespace["success"]))
        # Keep the real view's runtime lookups, including late test spies.
        return FunctionType(namespace["success"].__code__, vars(self.web))

    def test_mutation_missing_gate_is_rejected(self):
        client = self._client(False)
        with patch.dict(self.web.app.view_functions, {"success": self._mutated_success("missing_gate")}):
            with self.assertRaisesRegex(AssertionError, "success_status:unknown_uuid"):
                self._assert_case(client, CASES[0])
        self.assertEqual(self.last_response.status_code, 200)

    def test_mutation_no_id_gate_is_rejected(self):
        client = self._client(False)
        with patch.dict(self.web.app.view_functions, {"success": self._mutated_success("no_id_gate")}):
            with self.assertRaisesRegex(AssertionError, "success_status:no_id"):
                self._assert_case(client, CASES[3])
        self.assertEqual(self.last_response.status_code, 404)

    def test_mutation_masked_repository_failure_is_rejected(self):
        client = self._client(False)
        with patch.dict(self.web.app.view_functions, {"success": self._mutated_success("mask_exception")}):
            with self.assertRaisesRegex(AssertionError, "repository_failure:migration:get"):
                self._assert_repository_failure(client, "migration", "get")
        self.assertEqual(self.last_response.status_code, 404)

    def test_mutation_started_default_removed_is_rejected(self):
        client = self._client(False)
        with patch.dict(self.web.app.view_functions, {"success": self._mutated_success("started_default")}):
            with self.assertRaisesRegex(AssertionError, "started_default"):
                self._assert_case(client, CASES[2])
        self.assertEqual(self.last_response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
