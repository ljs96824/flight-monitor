import ast
import inspect
import io
import json
import sys
import tempfile
import textwrap
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


class RoundLogArchiveTest(unittest.TestCase):
    def test_round_archive_appends_boundary_logs_and_redacted_evidence(self):
        from log_utils import (
            append_round_evidence,
            end_round_log_archive,
            safe_log,
            start_round_log_archive,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = start_round_log_archive(
                "20260812T210000_sub",
                root_dir=root,
                now=datetime(2026, 8, 12, 21, 0, 0),
            )
            self.addCleanup(end_round_log_archive)
            safe_log("[测试轮档] 普通日志")
            append_round_evidence(
                "[源响应证据] 源=juhe raw=",
                {"api_key": "secret-value", "reason": "HTTP成功但空结果"},
            )
            append_round_evidence(
                "[source evidence] ",
                {"url": "https://example.test?a=1&access_token=other-secret"},
            )
            safe_log("[邮件失败] recipient=private@example.com")
            append_round_evidence(
                "[通知证据] ",
                {
                    "email": "owner@example.com",
                    "reason": "SMTP rejected owner@example.com",
                },
            )
            end_round_log_archive(status="ok")

            content = path.read_text(encoding="utf-8")
            self.assertIn("round_id=20260812T210000_sub", content)
            self.assertIn("[测试轮档] 普通日志", content)
            self.assertIn("HTTP成功但空结果", content)
            self.assertIn('"api_key": "***"', content)
            self.assertNotIn("secret-value", content)
            self.assertNotIn("other-secret", content)
            self.assertNotIn("private@example.com", content)
            self.assertNotIn("owner@example.com", content)
            self.assertIn("<EMAIL>", content)
            self.assertIn("status=ok", content)

    def test_round_archive_is_append_only_for_same_day(self):
        from log_utils import end_round_log_archive, safe_log, start_round_log_archive

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = start_round_log_archive(
                "round-a",
                root_dir=root,
                now=datetime(2026, 8, 12, 20, 0, 0),
            )
            safe_log("first")
            end_round_log_archive(status="ok")
            second = start_round_log_archive(
                "round-b",
                root_dir=root,
                now=datetime(2026, 8, 12, 21, 0, 0),
            )
            safe_log("second")
            end_round_log_archive(status="ok")

            self.assertEqual(first, second)
            content = second.read_text(encoding="utf-8")
            self.assertIn("round_id=round-a", content)
            self.assertIn("round_id=round-b", content)
            self.assertIn("first", content)
            self.assertIn("second", content)


class RoundEvidenceBoundaryTest(unittest.TestCase):
    def setUp(self):
        import log_utils

        self.module = log_utils

    def capture(self, function, records):
        stream = io.StringIO()
        state = {"file": stream, "lock": threading.RLock()}
        with patch.dict(function.__globals__, {"_round_log_state": state}):
            for prefix, payload in records:
                self.assertIs(function(prefix, payload), True)
        return stream.getvalue()

    def assert_record_boundaries(self, function):
        records = [("[first] ", {"id": 1}), ("[second] ", {"id": 2})]
        content = self.capture(function, records)
        self.assertEqual(content.count("\n"), 2, "record_lf_count")
        self.assertEqual(content.count(r"\n"), 0, "control_literal_terminator")
        lines = content.splitlines()
        self.assertEqual(len(lines), 2, "independent_record_lines")
        for line, (prefix, payload) in zip(lines, records):
            self.assertTrue(line.startswith(prefix))
            self.assertEqual(json.loads(line[len(prefix):]), payload)

    def assert_raw_output(self, function):
        content = self.capture(function, [("[evidence] ", {"z": "中文", "a": 1})])
        self.assertEqual(content, '[evidence] {"a": 1, "z": "中文"}\n', "raw_output_contract")

    def assert_payload_newline(self, function):
        value = "a\nb"
        content = self.capture(function, [("[evidence] ", {"value": value})])
        self.assertEqual(content.count("\n"), 1, "payload_lf_count")
        self.assertTrue(content.endswith("\n"))
        payload = json.loads(content[len("[evidence] "):])
        self.assertEqual(payload["value"], value, "payload_newline_roundtrip")

    def test_two_evidence_records_have_independent_lines(self):
        self.assert_record_boundaries(self.module.append_round_evidence)

    def test_raw_output_preserves_unicode_and_key_order(self):
        self.assert_raw_output(self.module.append_round_evidence)

    def test_payload_newline_remains_json_data(self):
        self.assert_payload_newline(self.module.append_round_evidence)

    def test_literal_backslash_n_roundtrips_unchanged(self):
        value = r"a\nb"
        content = self.capture(self.module.append_round_evidence, [("[evidence] ", {"value": value})])
        # This is a data-value sentinel; the boundary contracts check the suffix separately.
        payload, _ = json.JSONDecoder().raw_decode(content[len("[evidence] "):])
        self.assertEqual(payload["value"], value)
        self.assertNotEqual(payload["value"], "a\nb")

    def test_inactive_round_returns_false_without_encoding(self):
        with patch.object(self.module, "_round_log_state", None), patch.object(self.module.json, "dumps") as encode:
            self.assertIs(self.module.append_round_evidence("[unused] ", {"value": 1}), False)
            encode.assert_not_called()

    def test_real_archive_keeps_evidence_and_safe_log_on_separate_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(self.module, "_round_log_state", None), patch.object(sys, "stdout", io.StringIO()), patch.object(sys, "stderr", io.StringIO()):
                stdout, stderr = sys.stdout, sys.stderr
                try:
                    path = self.module.start_round_log_archive(
                        "synthetic-round", root_dir=Path(tmp), now=datetime(2026, 9, 20, 12, 0, 0)
                    )
                    self.module.append_round_evidence("[synthetic-a] ", {"id": 1})
                    self.module.append_round_evidence("[synthetic-b] ", {"id": 2})
                    self.module.safe_log("[ordinary] after")
                finally:
                    self.module.end_round_log_archive(status="ok")
                self.assertIs(sys.stdout, stdout)
                self.assertIs(sys.stderr, stderr)
            raw = path.read_bytes()
            self.assertNotIn(b"\r", raw, "archive_lf_not_platform_crlf")
            lines = raw.splitlines()
            first = lines.index(b'[synthetic-a] {"id": 1}') if b'[synthetic-a] {"id": 1}' in lines else -1
            self.assertGreaterEqual(first, 0, "archive_independent_first_record")
            self.assertEqual(lines[first + 1], b'[synthetic-b] {"id": 2}')
            self.assertEqual(lines[first + 2], b"[ordinary] after")
            self.assertEqual(json.loads(lines[first][len(b"[synthetic-a] "):]), {"id": 1})
            self.assertEqual(json.loads(lines[first + 1][len(b"[synthetic-b] "):]), {"id": 2})
            self.assertIn("[轮档开始]", lines[0].decode("utf-8"))
            self.assertIn("[轮档结束]", lines[-1].decode("utf-8"))
            self.assertIn(b"status=ok", lines[-1])

    def mutated(self, kind):
        source = textwrap.dedent(inspect.getsource(self.module.append_round_evidence))
        tree = ast.parse(source)
        original = ast.dump(tree, include_attributes=False)
        self.assertEqual(len(tree.body), 1)
        self.assertEqual(tree.body[0].name, "append_round_evidence")
        matches = 0
        if kind in {"literal_terminator", "missing_terminator"}:
            writes = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Attribute) and n.func.attr == "write"]
            self.assertEqual(len(writes), 1, "mutation_write_match")
            value = writes[0].args[0]
            self.assertIsInstance(value, ast.JoinedStr)
            tail = value.values[-1]
            self.assertIsInstance(tail, ast.Constant)
            self.assertEqual(tail.value, "\n", "mutation_terminator_match")
            tail.value = r"\n" if kind == "literal_terminator" else ""
            matches = 1
        elif kind in {"ensure_ascii", "sort_keys"}:
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and ast.unparse(n.func) == "json.dumps"]
            self.assertEqual(len(calls), 1, "mutation_encoder_match")
            for keyword in calls[0].keywords:
                if keyword.arg == kind:
                    self.assertEqual(keyword.value.value, kind == "sort_keys")
                    keyword.value = ast.Constant(value=kind == "ensure_ascii")
                    matches += 1
        elif kind == "unescape_payload_newline":
            for i, node in enumerate(tree.body[0].body):
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and ast.unparse(node.targets[0]) == "encoded":
                    self.assertEqual(ast.unparse(node.value.func), "json.dumps")
                    replacement = ast.Assign(targets=[ast.Name(id="encoded", ctx=ast.Store())],
                        value=ast.Call(func=ast.Attribute(value=ast.Name(id="encoded", ctx=ast.Load()), attr="replace", ctx=ast.Load()),
                                       args=[ast.Constant(value=r"\n"), ast.Constant(value="\n")], keywords=[]))
                    tree.body[0].body.insert(i + 1, replacement)
                    matches += 1
                    break
        self.assertEqual(matches, 1, "mutation_exact_match")
        self.assertNotEqual(ast.dump(tree, include_attributes=False), original, "mutation_ast_changed")
        changed_source = ast.unparse(ast.fix_missing_locations(tree))
        self.assertNotEqual(changed_source, ast.unparse(ast.parse(source)), "mutation_source_changed")
        compiled = compile(changed_source, "<round-evidence-mutation>", "exec")
        namespace = dict(self.module.append_round_evidence.__globals__)
        exec(compiled, namespace)
        return namespace["append_round_evidence"]

    def test_mutation_literal_terminator_is_rejected(self):
        changed = self.mutated("literal_terminator")
        with self.assertRaisesRegex(AssertionError, "record_lf_count"):
            self.assert_record_boundaries(changed)

    def test_mutation_missing_terminator_is_rejected(self):
        changed = self.mutated("missing_terminator")
        with self.assertRaisesRegex(AssertionError, "record_lf_count"):
            self.assert_record_boundaries(changed)

    def test_mutation_ensure_ascii_is_rejected(self):
        changed = self.mutated("ensure_ascii")
        with self.assertRaisesRegex(AssertionError, "raw_output_contract"):
            self.assert_raw_output(changed)

    def test_mutation_sort_keys_is_rejected(self):
        changed = self.mutated("sort_keys")
        with self.assertRaisesRegex(AssertionError, "raw_output_contract"):
            self.assert_raw_output(changed)

    def test_mutation_payload_unescape_is_rejected(self):
        changed = self.mutated("unescape_payload_newline")
        with self.assertRaisesRegex(AssertionError, "payload_lf_count"):
            self.assert_payload_newline(changed)


if __name__ == "__main__":
    unittest.main()
