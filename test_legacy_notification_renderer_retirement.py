import ast
import hashlib
import inspect
import unittest
from pathlib import Path
from unittest.mock import patch

import email_notifier
import notifier


ROOT = Path(__file__).resolve().parent
EXPECTED_SIGNATURE = (
    "(analysis_result=None, route_info=None, source_stats=None, "
    "price_insights=None, outbound_analysis=None, return_analysis=None, "
    "detail_level=None, enforce_pushplus_limit=True)"
)
EXPECTED_MAIN_CHAIN_SHA256 = {
    # Authorized 2026-08-29 log-only change: full subscription dump -> whitelist summary.
    "build_notification_payload": "6ec18a811b7cb6353af949b74237ffae1352147eb7cd6c170141ae75942d6912",
    "render_email": "d877278daa9099b02455fd9e26ad221452ec14b1264da3af380f4d1b0fb5a27d",
    "render_detail_html": "2dbafb52012c71315357a34a5a85138c0456e650e7897b545449322ad21f9aa1",
    "render_pushplus_sections": "666472ec424df694891027abf2a125e47a8c0ba7d7d92b0400d64419a2ee56ef",
}
RETIRED_PRIVATE_RENDERERS = {
    "_format_structured_html_" + "message",
    "_append_detailed_analysis_" + "section",
}


def _notifier_tree_and_source():
    source = (ROOT / "notifier.py").read_text(encoding="utf-8")
    return ast.parse(source), source


def _module_function(tree, name):
    matches = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected one module function {name}, found {len(matches)}")
    return matches[0]


class LegacyNotificationRendererRetirementTest(unittest.TestCase):
    def test_public_signature_is_preserved_exactly(self):
        self.assertEqual(str(inspect.signature(notifier.format_html_message)), EXPECTED_SIGNATURE)

    def test_public_entry_raises_domain_error_without_side_effects(self):
        sensitive_values = {
            "email": "private-person@example.test",
            "token": "private-token-sentinel",
            "route": "PRIVATE-ROUTE-SENTINEL",
        }
        exception_type = getattr(notifier, "LegacyNotificationRendererUnavailable")

        with (
            patch.object(notifier, "send") as pushplus_send,
            patch.object(notifier, "_post_pushplus") as pushplus_post,
            patch.object(notifier, "persist_notification_payload") as persist_payload,
            patch.object(notifier, "save_push_snapshot") as save_snapshot,
            patch.object(notifier, "save_pushed_plans") as save_plans,
            patch.object(notifier.httpx, "post") as http_post,
            patch.object(email_notifier, "send_email") as email_send,
        ):
            with self.assertRaisesRegex(
                exception_type,
                r"render_email|render_detail_html|render_pushplus_sections",
            ):
                notifier.format_html_message()
            with self.assertRaises(exception_type) as raised:
                notifier.format_html_message(
                    analysis_result=sensitive_values,
                    route_info={"origin": sensitive_values["route"]},
                )

        self.assertNotIsInstance(raised.exception, NameError)
        message = str(raised.exception)
        self.assertRegex(
            message,
            r"build_notification_payload.*(render_email|render_detail_html|render_pushplus_sections)",
        )
        for value in sensitive_values.values():
            self.assertNotIn(value, message)
        for mocked in (
            pushplus_send,
            pushplus_post,
            persist_payload,
            save_snapshot,
            save_plans,
            http_post,
            email_send,
        ):
            mocked.assert_not_called()

    def test_private_renderer_subgraph_has_no_definition_or_reference(self):
        tree, _ = _notifier_tree_and_source()
        definitions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name in RETIRED_PRIVATE_RENDERERS
        }
        references = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in RETIRED_PRIVATE_RENDERERS
        }
        attributes = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and node.attr in RETIRED_PRIVATE_RENDERERS
        }
        self.assertEqual(definitions, set())
        self.assertEqual(references | attributes, set())

    def test_f821_debt_is_empty(self):
        from scripts.check_f821 import KNOWN_F821_DEBT, scan_f821

        expected = frozenset()
        self.assertEqual(KNOWN_F821_DEBT, expected)
        self.assertEqual(scan_f821(), expected)

    def test_current_notification_main_chain_source_is_unchanged(self):
        tree, source = _notifier_tree_and_source()
        actual = {}
        for name in EXPECTED_MAIN_CHAIN_SHA256:
            node = _module_function(tree, name)
            segment = ast.get_source_segment(source, node)
            actual[name] = hashlib.sha256(segment.encode()).hexdigest()
        self.assertEqual(actual, EXPECTED_MAIN_CHAIN_SHA256)


if __name__ == "__main__":
    unittest.main()
