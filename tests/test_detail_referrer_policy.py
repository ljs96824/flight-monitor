"""Detail-only referrer policy with real views and deterministic authorization."""
import ast
from contextlib import ExitStack, contextmanager
import copy
import inspect
import unittest
from unittest.mock import patch

from tests import test_sensitive_response_no_store as support


HOOK = "_detail_no_referrer"
DETAIL_CASES = tuple(name for name in support.CASES if name.startswith("detail_"))
OTHER_PATHS = ("/", "/price_hint", "/robots.txt", "/static/synthetic-cache.txt")


class DetailReferrerPolicyTest(unittest.TestCase):
    def setUp(self):
        # Reuse the prior contract's clocks, nonce, signatures and isolated inputs.
        self.fixture = support.SensitiveResponseNoStoreTest(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.web = self.fixture.web

    @contextmanager
    def baseline_hooks(self):
        hooks = self.web.app.after_request_funcs[None]
        preserved = [h for h in hooks if h.__name__ != HOOK]
        self.assertIn("_sensitive_response_no_store", [h.__name__ for h in preserved])
        with patch.dict(self.web.app.after_request_funcs, {None: preserved}):
            yield

    def assert_policy(self, name):
        result = self.fixture.request_case(name)
        self.assertEqual([v for k, v in result["headers"] if k.lower() == "referrer-policy"],
                         ["no-referrer"], "DETAIL_REFERRER_POLICY_" + name)

    def test_detail_valid_no_referrer(self):
        self.assert_policy("detail_valid")

    def test_detail_wrong_no_referrer(self):
        self.assert_policy("detail_wrong")

    def test_detail_missing_token_no_referrer(self):
        self.assert_policy("detail_missing_token")

    def test_detail_missing_payload_no_referrer(self):
        self.assert_policy("detail_missing_payload")

    def test_detail_invalid_uuid_no_referrer(self):
        self.assert_policy("detail_invalid_uuid")

    def test_detail_status_and_body_match_baseline_bytes(self):
        for name in DETAIL_CASES:
            with self.subTest(case=name):
                with self.baseline_hooks():
                    baseline = self.fixture.request_case(name)
                actual = self.fixture.request_case(name)
                self.assertEqual(actual["status"], baseline["status"], "DETAIL_STATUS_CHANGED")
                self.assertEqual(actual["body"], baseline["body"], "DETAIL_BODY_BYTES_CHANGED")

    def test_detail_keeps_no_store_and_noindex(self):
        for name in DETAIL_CASES:
            with self.subTest(case=name):
                headers = dict(self.fixture.request_case(name)["headers"])
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Robots-Tag"], "noindex")

    def assert_list_unchanged(self, name):
        with self.baseline_hooks():
            baseline = self.fixture.request_case(name)
        actual = self.fixture.request_case(name)
        self.assertNotIn("Referrer-Policy", dict(actual["headers"]), "LIST_REFERRER_POLICY_CHANGED")
        self.assertEqual(actual["headers"], baseline["headers"], "LIST_HEADERS_CHANGED")

    def test_subscription_headers_unchanged(self):
        for name in ("list_populated", "list_empty", "list_denied"):
            with self.subTest(case=name):
                self.assert_list_unchanged(name)

    def assert_other_headers(self, path):
        def request():
            return self.fixture.snapshot(self.fixture.client().get(path, base_url=support.BASE_URL))
        with self.baseline_hooks():
            baseline = request()
        actual = request()
        self.assertEqual(baseline["status"], 200, "BASELINE_SUCCESS_CONTROL")
        self.assertEqual(actual["status"], 200, "SUCCESS_CONTROL")
        self.assertEqual(actual["headers"], baseline["headers"], "OTHER_HEADERS_CHANGED")
        if path.startswith("/static/"):
            self.assertEqual(actual["body"], self.fixture.asset.read_bytes())
            self.assertIn("Cache-Control", dict(actual["headers"]))
            self.assertIn("Last-Modified", dict(actual["headers"]))

    def test_other_endpoints_keep_baseline_headers(self):
        for path in OTHER_PATHS:
            with self.subTest(path=path):
                self.assert_other_headers(path)

    def assert_old_hook_separate(self):
        with self.web.app.test_request_context("/detail", base_url=support.BASE_URL):
            self.assertEqual(self.web.request.endpoint, "detail", "VALID_DETAIL_CONTEXT")
            response = self.web.app.response_class(b"synthetic-body", status=202)
            response.headers["X-Robots-Tag"] = "noindex"
            self.assertIs(self.web._sensitive_response_no_store(response), response)
            self.assertNotIn("Referrer-Policy", response.headers, "OLD_HOOK_CHANGED_REFERRER_POLICY")
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_old_cache_hook_does_not_set_referrer_policy(self):
        self.assert_old_hook_separate()

    def assert_new_hook_separate(self):
        with self.web.app.test_request_context("/detail", base_url=support.BASE_URL):
            self.assertEqual(self.web.request.endpoint, "detail", "VALID_DETAIL_CONTEXT")
            response = self.web.app.response_class(b"synthetic-body", status=202)
            response.headers["Cache-Control"] = "private, max-age=7"
            response.headers["X-Robots-Tag"] = "noindex"
            before = (response.status_code, response.get_data(), list(response.headers))
            self.assertIs(getattr(self.web, HOOK)(response), response)
            self.assertEqual(response.headers["Cache-Control"], "private, max-age=7", "NEW_HOOK_CHANGED_CACHE_CONTROL")
            self.assertEqual((response.status_code, response.get_data(),
                              [(k, v) for k, v in response.headers if k != "Referrer-Policy"]), before)
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")

    def test_new_hook_only_changes_referrer_policy(self):
        self.assert_new_hook_separate()

    def test_after_request_registration_order(self):
        self.assertEqual([h.__name__ for h in self.web.app.after_request_funcs[None]],
                         ["_indexing_noindex", "_sensitive_response_no_store", HOOK, "_authentication_no_store"])

    @contextmanager
    def mutant(self, kind, value=None):
        tree = ast.parse(inspect.getsource(getattr(self.web, HOOK)))
        function = tree.body[0]
        self.assertEqual(function.name, HOOK, "MUTATION_NODE_MATCH")
        self.assertEqual(len(function.decorator_list), 1, "MUTATION_NODE_MATCH")
        function.decorator_list = []
        checks = [n for n in function.body if isinstance(n, ast.If)]
        self.assertEqual(len(checks), 1, "MUTATION_NODE_MATCH")
        self.assertEqual(len(checks[0].body), 1, "MUTATION_NODE_MATCH")
        assignment = checks[0].body[0]
        self.assertIsInstance(assignment, ast.Assign)
        self.assertEqual(ast.unparse(assignment.targets[0]), "response.headers['Referrer-Policy']")
        before = ast.unparse(tree)
        replacements = [HOOK]
        if kind == "remove":
            function.body = [ast.Return(value=ast.Name(id="response", ctx=ast.Load()))]
        elif kind == "merge_into_cache":
            old = ast.parse(inspect.getsource(self.web._sensitive_response_no_store)).body[0]
            self.assertEqual(old.name, "_sensitive_response_no_store", "MUTATION_NODE_MATCH")
            self.assertIsInstance(old.body[-1], ast.Return)
            old.decorator_list = []
            old.body.insert(-1, copy.deepcopy(checks[0]))
            tree.body.append(old)
            replacements.append(old.name)
            function.body = [ast.Return(value=ast.Name(id="response", ctx=ast.Load()))]
        elif kind == "all_endpoints":
            checks[0].test = ast.Constant(True)
        elif kind == "only_200":
            checks[0].test = ast.BoolOp(op=ast.And(), values=[checks[0].test,
                ast.Compare(left=ast.Attribute(value=ast.Name(id="response", ctx=ast.Load()), attr="status_code", ctx=ast.Load()),
                            ops=[ast.Eq()], comparators=[ast.Constant(200)])])
        elif kind == "wrong_value":
            self.assertIn(value, ("same-origin", "strict-origin-when-cross-origin"))
            assignment.value = ast.Constant(value)
        elif kind == "also_cache":
            extra = copy.deepcopy(assignment)
            extra.targets[0].slice = ast.Constant("Cache-Control")
            extra.value = ast.Constant("public")
            checks[0].body.append(extra)
        else:
            self.fail("UNKNOWN_MUTATION")
        ast.fix_missing_locations(tree)
        self.assertNotEqual(ast.unparse(tree), before, "MUTATION_SOURCE_CHANGED")
        namespace = dict(vars(self.web))
        exec(compile(tree, "<detail-referrer-policy-mutation>", "exec"), namespace)
        with ExitStack() as stack:
            hooks = self.web.app.after_request_funcs[None]
            for name in replacements:
                self.assertEqual(sum(h.__name__ == name for h in hooks), 1, "MUTATION_REGISTERED_MATCH")
                stack.enter_context(patch.object(self.web, name, namespace[name]))
            stack.enter_context(patch.dict(self.web.app.after_request_funcs, {
                None: [namespace[h.__name__] if h.__name__ in replacements else h for h in hooks]}))
            yield

    def test_mutation_removed_hook_is_rejected(self):
        with self.mutant("remove"):
            for name in DETAIL_CASES:
                with self.subTest(case=name), self.assertRaisesRegex(AssertionError, "DETAIL_REFERRER_POLICY_" + name):
                    self.assert_policy(name)

    def test_mutation_merged_into_cache_is_rejected(self):
        with self.mutant("merge_into_cache"):
            with self.assertRaisesRegex(AssertionError, "OLD_HOOK_CHANGED_REFERRER_POLICY"):
                self.assert_old_hook_separate()

    def test_mutation_all_endpoints_is_rejected(self):
        with self.mutant("all_endpoints"):
            for name in ("list_populated", "list_empty", "list_denied"):
                with self.subTest(case=name), self.assertRaisesRegex(AssertionError, "LIST_REFERRER_POLICY_CHANGED"):
                    self.assert_list_unchanged(name)
            for path in OTHER_PATHS:
                with self.subTest(path=path), self.assertRaisesRegex(AssertionError, "OTHER_HEADERS_CHANGED"):
                    self.assert_other_headers(path)

    def test_mutation_success_only_is_rejected(self):
        with self.mutant("only_200"):
            for name in DETAIL_CASES:
                if support.CASES[name][2] == 404:
                    with self.subTest(case=name), self.assertRaisesRegex(AssertionError, "DETAIL_REFERRER_POLICY_" + name):
                        self.assert_policy(name)

    def test_mutation_weaker_values_are_rejected(self):
        for value in ("same-origin", "strict-origin-when-cross-origin"):
            with self.mutant("wrong_value", value):
                for name in DETAIL_CASES:
                    with self.subTest(value=value, case=name), self.assertRaisesRegex(AssertionError, "DETAIL_REFERRER_POLICY_" + name):
                        self.assert_policy(name)

    def test_mutation_new_hook_changes_cache_is_rejected(self):
        with self.mutant("also_cache"):
            with self.assertRaisesRegex(AssertionError, "NEW_HOOK_CHANGED_CACHE_CONTROL"):
                self.assert_new_hook_separate()


if __name__ == "__main__":
    unittest.main()
