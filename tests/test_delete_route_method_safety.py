"""Delete-method safety independent of optional management authentication."""

from contextlib import ExitStack, redirect_stdout
import ast
import importlib
import inspect
import io
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

from werkzeug.exceptions import NotFound


SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000001"
DELETE_PATH = f"/subscription/{SUBSCRIPTION_ID}/delete"
TOKEN = "delete-method-contract-only-" + "m" * 40
SECRET = "delete-session-contract-only-" + "s" * 40
FIXED_EPOCH = 1800000000
CASES = (
    ("compatibility_head", False, False, "HEAD", False, True, 404, 0),
    ("anonymous_head", True, False, "HEAD", False, True, 404, 0),
    ("authenticated_head", True, True, "HEAD", False, True, 404, 0),
    ("post_without_csrf", True, True, "POST", False, True, 403, 0),
    ("anonymous_post", True, False, "POST", True, True, 404, 0),
    ("post_without_confirmation", True, True, "POST", True, False, 400, 0),
    ("confirmed_post", True, True, "POST", True, True, 302, 1),
)


class DeleteRouteMethodSafetyTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
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
                patch(name, side_effect=AssertionError(name), create=True)
            )
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.web = importlib.import_module("web_form")
        for name, value in (
            ("SUBSCRIPTIONS_PATH", self.root / "subscriptions.json"),
            ("FEEDBACK_PATH", self.root / "feedback.json"),
            ("PAGE_PAYLOADS_DIR", self.root / "payloads"),
        ):
            self.stack.enter_context(patch.object(self.web, name, value))
        self.stack.enter_context(patch.dict(self.web.app.config, {
            "TESTING": True, "SECRET_KEY": SECRET, "SESSION_COOKIE_SECURE": False,
            "CSRF_CLOCK": lambda: FIXED_EPOCH, "MANAGEMENT_CLOCK": lambda: FIXED_EPOCH,
        }))
        self.repository = Mock(spec_set=["get", "delete"])
        self.repository.get.side_effect = self._lookup
        self.repository.delete.side_effect = self._delete
        self.factory = self.stack.enter_context(patch.object(
            self.web, "_subscription_repository", return_value=self.repository
        ))
        for name in ("load_subscriptions", "start_background_collection", "save_feedback",
                     "notify_feedback_author", "read_json", "update_json"):
            self.denials[name] = self.stack.enter_context(
                patch.object(self.web, name, side_effect=AssertionError(name))
            )
        self.addCleanup(self._assert_no_io)

    def _lookup(self, owner, subscription_id):
        self.assertEqual((owner, subscription_id), (self.web.LOCAL_OWNER_ID, SUBSCRIPTION_ID))
        return {"id": SUBSCRIPTION_ID, "origin": "PVG", "dest": "PEK"}

    def _delete(self, owner, subscription_id):
        self.assertEqual((owner, subscription_id), (self.web.LOCAL_OWNER_ID, SUBSCRIPTION_ID))
        return True

    def _assert_no_io(self):
        for name, spy in self.denials.items():
            self.assertEqual(spy.call_count, 0, name)
        self.assertEqual(list(self.root.rglob("*")), [])
        self.assertNotIn(TOKEN, self.output.getvalue())
        self.assertNotIn(SECRET, self.output.getvalue())

    def _csrf(self, client):
        response = client.get("/unlock")
        self.assertEqual(response.status_code, 200)
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
        self.assertIsNotNone(match)
        return match.group(1)

    def _login(self, client):
        response = client.post("/unlock", data={"csrf_token": self._csrf(client), "token": TOKEN})
        self.assertEqual(response.status_code, 302)
        # Login clears the old session; requests must use the newly issued nonce.
        return self._csrf(client)

    def _assert_case(self, case):
        name, enforced, logged_in, method, csrf, confirmed, status, deletes = case
        with patch.dict(os.environ, {
            "MANAGEMENT_AUTH_REQUIRED": "1" if enforced else "0",
            "MANAGEMENT_TOKEN": TOKEN if enforced else "",
        }):
            client = self.web.app.test_client()
            token = self._login(client) if logged_in else self._csrf(client)
            self.factory.reset_mock()
            self.repository.reset_mock()
            data = {"confirm_delete": "yes"} if confirmed else {}
            if csrf:
                data["csrf_token"] = token
            response = getattr(client, method.lower())(DELETE_PATH, data=data)
            self.assertEqual(
                (response.status_code, self.repository.delete.call_count),
                (status, deletes), f"delete_contract:{name}",
            )
            if deletes:
                self.assertEqual(response.headers["Location"], "/subscriptions")
                self.repository.delete.assert_called_once_with(self.web.LOCAL_OWNER_ID, SUBSCRIPTION_ID)
            if status in {403, 404}:
                self.factory.assert_not_called()
                self.repository.get.assert_not_called()
            else:
                self.factory.assert_called_once_with()
                self.repository.get.assert_called_once_with(self.web.LOCAL_OWNER_ID, SUBSCRIPTION_ID)

    def test_seven_case_matrix(self):
        for case in CASES:
            with self.subTest(case=case[0]):
                self._assert_case(case)

    def test_direct_view_rejects_head_before_repository(self):
        with self.web.app.test_request_context(DELETE_PATH, method="HEAD", data={"confirm_delete": "yes"}):
            with self.assertRaises(NotFound):
                self.web.delete_subscription(SUBSCRIPTION_ID)
        self.factory.assert_not_called()
        self.repository.get.assert_not_called()
        self.repository.delete.assert_not_called()

    def test_get_confirmation_does_not_delete_and_supplies_valid_post(self):
        client = self.web.app.test_client()
        self._login(client)
        response = client.get(DELETE_PATH, data={"confirm_delete": "yes"})
        self.assertEqual(response.status_code, 200)
        self.factory.assert_called_once_with()
        self.repository.get.assert_called_once_with(self.web.LOCAL_OWNER_ID, SUBSCRIPTION_ID)
        self.repository.delete.assert_not_called()
        html = response.get_data(as_text=True)
        self.assertIn('<form method="post">', html)
        fields = dict(re.findall(r'name="(csrf_token|confirm_delete)" value="([^"]+)"', html))
        self.assertEqual(set(fields), {"csrf_token", "confirm_delete"})
        self.assertEqual(fields["confirm_delete"], "yes")
        response = client.post(DELETE_PATH, data=fields)
        self.assertEqual((response.status_code, response.headers["Location"]), (302, "/subscriptions"))
        self.repository.delete.assert_called_once_with(self.web.LOCAL_OWNER_ID, SUBSCRIPTION_ID)

    def _mutated_delete_view(self, kind):
        tree = ast.parse(inspect.getsource(self.web.delete_subscription))
        function = tree.body[0]
        self.assertIsInstance(function, ast.FunctionDef)
        function.decorator_list = []
        predicates = {
            "method_gate": 'request.method not in {"GET", "POST"}',
            "confirmation": 'request.form.get("confirm_delete") != "yes"',
        }
        expected = ast.dump(ast.parse(predicates[kind], mode="eval").body)
        matches = [index for index, node in enumerate(function.body)
                   if isinstance(node, ast.If) and ast.dump(node.test) == expected]
        self.assertEqual(len(matches), 1, f"mutation_node_count:{kind}")
        original = ast.unparse(tree)
        del function.body[matches[0]]
        self.assertNotEqual(ast.unparse(tree), original, f"mutation_source_changed:{kind}")
        namespace = dict(vars(self.web))
        exec(compile(ast.fix_missing_locations(tree), f"<delete-mutation-{kind}>", "exec"), namespace)
        mutated = namespace["delete_subscription"]
        self.assertTrue(callable(mutated))
        return mutated

    def test_mutation_missing_method_gate_is_rejected(self):
        mutated = self._mutated_delete_view("method_gate")
        with patch.dict(self.web.app.view_functions, {"delete_subscription": mutated}):
            with self.assertRaisesRegex(AssertionError, "delete_contract:compatibility_head"):
                self._assert_case(CASES[0])
        self.repository.delete.assert_called_once_with(self.web.LOCAL_OWNER_ID, SUBSCRIPTION_ID)

    def test_mutation_missing_confirmation_is_rejected(self):
        mutated = self._mutated_delete_view("confirmation")
        with patch.dict(self.web.app.view_functions, {"delete_subscription": mutated}):
            with self.assertRaisesRegex(AssertionError, "delete_contract:post_without_confirmation"):
                self._assert_case(CASES[5])
        self.repository.delete.assert_called_once_with(self.web.LOCAL_OWNER_ID, SUBSCRIPTION_ID)


if __name__ == "__main__":
    unittest.main()
