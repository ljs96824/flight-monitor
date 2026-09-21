import ast
import inspect
import io
import json
import sys
import tempfile
import textwrap
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import FunctionType, SimpleNamespace
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


class _StartMarkerStream(io.StringIO):
    def __init__(self, fault=None, close_error=None):
        super().__init__()
        self.fault = fault
        self.failure = OSError("SYNTHETIC_START_" + str(fault).upper())
        self.close_error = close_error
        self.marker_hits = 0
        self.flush_calls = 0
        self.close_calls = 0

    def write(self, value):
        if "[轮档开始]" in value:
            self.marker_hits += 1
            if self.fault == "write":
                raise self.failure
        return super().write(value)

    def flush(self):
        self.flush_calls += 1
        if self.fault == "flush" and self.marker_hits:
            raise self.failure
        return super().flush()

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error
        return super().close()


class RoundArchiveStartRollbackTest(unittest.TestCase):
    @contextmanager
    def isolated(self, streams, *, existing_tee=False):
        import log_utils

        real_pair = (sys.stdout, sys.stderr)
        console, error_console, run_file = io.StringIO(), io.StringIO(), io.StringIO()
        stdout = log_utils._Utf8TeeStream(console, run_file, threading.RLock()) if existing_tee else console
        private_sys = SimpleNamespace(stdout=stdout, stderr=error_console,
                                      __stdout__=io.StringIO(), __stderr__=io.StringIO())
        messages = []

        def private_print(*args, **kwargs):
            messages.append(args)
            return print(*args, file=private_sys.stdout, **kwargs)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            with patch.object(log_utils, "sys", private_sys), patch.object(log_utils, "_round_log_state", None), \
                    patch.object(log_utils, "print", private_print, create=True), \
                    patch.object(Path, "open", side_effect=streams) as opened:
                env = SimpleNamespace(module=log_utils, sys=private_sys, root=Path(tmp), opened=opened,
                                      stdout=stdout, stderr=error_console, messages=messages, run_file=run_file)
                try:
                    yield env
                finally:
                    # Bypass injected close failures only when disposing synthetic test buffers.
                    for stream in streams:
                        io.StringIO.close(stream)
                    for stream in (console, error_console, run_file, private_sys.__stdout__, private_sys.__stderr__):
                        io.StringIO.close(stream)
                    self.assertIs(sys.stdout, real_pair[0], "REAL_STDOUT_UNCHANGED")
                    self.assertIs(sys.stderr, real_pair[1], "REAL_STDERR_UNCHANGED")

    def start(self, env, function=None, round_id="synthetic-start"):
        return (function or env.module.start_round_log_archive)(
            round_id, root_dir=env.root, now=datetime(2026, 9, 21, 12))

    def assert_rollback(self, function=None, *, fault="write", existing_tee=False, close_error=None):
        archive = _StartMarkerStream(fault, close_error)
        with self.isolated([archive], existing_tee=existing_tee) as env:
            with self.assertRaises(OSError, msg="ORIGINAL_START_ERROR_MUST_PROPAGATE") as caught:
                self.start(env, function)
            self.assertIs(caught.exception, archive.failure, "ORIGINAL_START_ERROR_IDENTITY")
            self.assertEqual(str(caught.exception), "SYNTHETIC_START_" + fault.upper())
            self.assertEqual(archive.marker_hits, 1, "START_MARKER_INJECTION_HIT")
            self.assertEqual(archive.flush_calls, int(fault == "flush"), "START_FAILURE_PHASE")
            self.assertIs(env.sys.stdout, env.stdout, "RESTORE_SAVED_STDOUT")
            self.assertIs(env.sys.stderr, env.stderr, "RESTORE_SAVED_STDERR")
            self.assertIsNone(env.module._round_log_state, "CLEAR_NEW_ROUND_STATE")
            self.assertEqual(archive.close_calls, 1, "ATTEMPT_ARCHIVE_CLOSE")
            env.opened.assert_called_once_with("a", encoding="utf-8", errors="strict", newline="", buffering=1)
            self.assertEqual(archive.closed, close_error is None)
            if existing_tee:
                self.assertIsInstance(env.sys.stdout, env.module._Utf8TeeStream)
                self.assertIsNot(env.sys.stdout, env.sys.__stdout__)
                self.assertFalse(env.run_file.closed, "KEEP_PRIOR_RUN_TEE")

    def test_normal_start_end_restores_streams_and_closes_once(self):
        archive = _StartMarkerStream()
        with self.isolated([archive]) as env:
            path = self.start(env)
            self.assertEqual(path, env.root / "20260921.log")
            self.assertIs(env.module._round_log_state["file"], archive)
            self.assertIsInstance(env.sys.stdout, env.module._Utf8TeeStream)
            env.module.end_round_log_archive(status="ok")
            self.assertIs(env.sys.stdout, env.stdout)
            self.assertIs(env.sys.stderr, env.stderr)
            self.assertIsNone(env.module._round_log_state)
            self.assertEqual(archive.close_calls, 1)
            self.assertTrue(archive.closed)

    def test_start_write_failure_rolls_back(self):
        self.assert_rollback(fault="write")

    def test_start_flush_failure_rolls_back(self):
        self.assert_rollback(fault="flush")

    def test_start_failure_restores_existing_run_tee(self):
        self.assert_rollback(existing_tee=True)

    def test_close_failure_does_not_replace_start_failure(self):
        for close_error in (OSError("SYNTHETIC_CLOSE"), KeyboardInterrupt("SYNTHETIC_CLOSE_INTERRUPT")):
            with self.subTest(close_error=type(close_error).__name__):
                self.assert_rollback(close_error=close_error)

    def test_failed_start_can_be_followed_by_successful_start(self):
        failed, good = _StartMarkerStream("write"), _StartMarkerStream()
        with self.isolated([failed, good]) as env:
            with self.assertRaises(OSError) as caught:
                self.start(env)
            self.assertIs(caught.exception, failed.failure)
            with patch.object(env.module, "end_round_log_archive", wraps=env.module.end_round_log_archive) as end:
                self.start(env, round_id="synthetic-restart")
            end.assert_not_called()
            self.assertIs(env.module._round_log_state["file"], good)
            self.assertEqual(env.module._round_log_state["round_id"], "synthetic-restart")
            env.module.end_round_log_archive()
            self.assertIs(env.sys.stdout, env.stdout)
            self.assertIs(env.sys.stderr, env.stderr)
            self.assertIsNone(env.module._round_log_state)
            self.assertEqual((failed.close_calls, good.close_calls), (1, 1))

    def test_new_start_failure_does_not_revive_previous_round(self):
        old, failed = _StartMarkerStream(), _StartMarkerStream("write")
        with self.isolated([old, failed], existing_tee=True) as env:
            self.start(env, round_id="synthetic-old")
            old_state = env.module._round_log_state
            with patch.object(env.module, "end_round_log_archive", wraps=env.module.end_round_log_archive) as end:
                with self.assertRaises(OSError) as caught:
                    self.start(env, round_id="synthetic-new")
            self.assertIs(caught.exception, failed.failure)
            end.assert_called_once_with(status="interrupted")
            self.assertEqual(old.close_calls, 1)
            self.assertTrue(old.closed)
            self.assertIs(env.sys.stdout, env.stdout, "RESTORE_POST_END_STREAM")
            self.assertIs(env.sys.stderr, env.stderr)
            self.assertIsNone(env.module._round_log_state, "NO_OLD_ROUND_REVIVAL")
            self.assertIsNot(env.module._round_log_state, old_state)
            self.assertEqual(failed.close_calls, 1)

    def mutated_start(self, kind):
        import log_utils

        source = inspect.getsource(log_utils.start_round_log_archive)
        tree = ast.parse(textwrap.dedent(source))
        original = ast.dump(tree)
        function = tree.body[0]
        blocks = [node for node in function.body if isinstance(node, ast.Try)
                  and any(isinstance(n, ast.Call) and ast.unparse(n.func) == "safe_log" for n in ast.walk(node))]
        self.assertEqual(len(blocks), 1, "mutation_start_block_match")
        block = blocks[0]
        self.assertEqual(len(block.handlers), 1)
        rollback = block.handlers[0]
        matches = 0
        if kind == "no_rollback":
            function.body[function.body.index(block):function.body.index(block) + 1] = block.body
            matches = 1
        else:
            for node in list(rollback.body):
                if kind == "wrong_stdout" and isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "sys.stdout":
                    self.assertEqual(ast.unparse(node.value), "original_stdout")
                    node.value = ast.Attribute(value=ast.Name(id="sys", ctx=ast.Load()), attr="__stdout__", ctx=ast.Load())
                    matches += 1
                elif kind == "swallow" and isinstance(node, ast.Raise) and node.exc is None:
                    rollback.body.remove(node)
                    matches += 1
                elif kind == "stale_state" and isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "_round_log_state":
                    self.assertIsNone(node.value.value)
                    rollback.body.remove(node)
                    matches += 1
        self.assertEqual(matches, 1, "mutation_exact_match")
        ast.fix_missing_locations(tree)
        self.assertNotEqual(ast.dump(tree), original, "mutation_ast_changed")
        changed = ast.unparse(tree)
        self.assertNotEqual(changed, ast.unparse(ast.parse(source)), "mutation_source_changed")
        compiled = compile(changed, "<round-start-mutation>", "exec")
        namespace = dict(log_utils.__dict__)
        exec(compiled, namespace)
        candidate = namespace[function.name]
        candidate = FunctionType(candidate.__code__, log_utils.__dict__, candidate.__name__, candidate.__defaults__)
        candidate.__kwdefaults__ = namespace[function.name].__kwdefaults__
        return candidate

    def test_mutation_missing_rollback_is_rejected(self):
        changed = self.mutated_start("no_rollback")
        with self.assertRaisesRegex(AssertionError, "RESTORE_SAVED_STDOUT"):
            self.assert_rollback(changed)

    def test_mutation_builtin_stdout_restore_is_rejected(self):
        changed = self.mutated_start("wrong_stdout")
        with self.assertRaisesRegex(AssertionError, "RESTORE_SAVED_STDOUT"):
            self.assert_rollback(changed, existing_tee=True)

    def test_mutation_swallowed_start_error_is_rejected(self):
        changed = self.mutated_start("swallow")
        with self.assertRaisesRegex(AssertionError, "ORIGINAL_START_ERROR_MUST_PROPAGATE"):
            self.assert_rollback(changed)

    def test_mutation_stale_round_state_is_rejected(self):
        changed = self.mutated_start("stale_state")
        with self.assertRaisesRegex(AssertionError, "CLEAR_NEW_ROUND_STATE"):
            self.assert_rollback(changed)


if __name__ == "__main__":
    unittest.main()
