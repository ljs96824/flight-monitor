"""Separate virtual-time startup logic from native loopback integration."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import traceback
import unittest
from unittest.mock import patch


DRIVER = Path(__file__).resolve().parents[1] / "scripts" / "ui_smoke_driver.mjs"
NODE_HARNESS = r'''
const http = require("node:http");
const {performance} = require("node:perf_hooks");
function emitPhase(sequence, phase, mode = "unknown") {
  try {
    process.stderr.write("G14_PHASE " + JSON.stringify({sequence, phase, node: process.version,
      mode: ["controlled", "native"].includes(mode) ? mode : "unknown"}) + "\n");
  } catch {
    // An unavailable diagnostic stream must not replace the execution's outcome.
  }
}
emitPhase(1, "harness_started");
let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", chunk => input += chunk);
process.stdin.on("end", async () => {
  emitPhase(2, "input_received");
  const {block, scenario, mode} = JSON.parse(input);
  const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
  // Extraction/compilation failures are infrastructure errors, never mutation kills.
  const run = new AsyncFunction("fetch", "sleep", "cdpPort", "AbortSignal", "performance",
    block + "\nreturn target;");
  emitPhase(3, "compiled", mode);
  const good = {type: "page", id: "ready", webSocketDebuggerUrl: "ws://canary.invalid/page"};
  let attempts = 0, responses = 0, virtualNow = 0, eventId = 0;
  const sleeps = [], requestLimits = [], requestStarts = [], operations = [], abortEvents = [];
  const events = [], signals = new WeakMap();
  const clock = mode === "controlled" ? {now: () => virtualNow} : performance;
  const schedule = (delay, fire) => events.push({at: virtualNow + delay, id: eventId++, fire});
  function targetsFor(number) {
    let targets = [good];
    if (["empty", "single_attempt"].includes(scenario)
      || (scenario === "eventual" && number <= 3)) targets = [];
    if (scenario === "non_page") targets = [
      {type: "background_page", url: "url-canary", title: "title-canary"},
      {type: "service_worker", webSocketDebuggerUrl: "ws-canary"},
    ];
    if (scenario === "invalid_ws" && number <= 4) targets = [
      {type: "page", webSocketDebuggerUrl: ["", "   ", null, 42][number - 1]},
    ];
    return targets;
  }
  const timeoutSignal = {timeout(ms) {
    requestLimits.push(ms);
    let signal;
    if (mode === "controlled") {
      const controller = new AbortController();
      signal = controller.signal;
      // Cancellation and performance.now share this event queue, not native timers.
      schedule(ms, () => controller.abort());
    } else {
      signal = AbortSignal.timeout(ms);
    }
    const id = requestLimits.length;
    signals.set(signal, id);
    signal.addEventListener("abort", () => abortEvents.push({id, at: clock.now()}), {once: true});
    return signal;
  }};
  async function observe(stage, signal, operation) {
    const record = {stage, attempt: attempts, signalId: signals.get(signal) ?? null,
      signalReceived: signals.has(signal), pending: true, pendingAtAbort: false,
      settlement: null, rejectedAfterAbort: false, startedAt: clock.now()};
    operations.push(record);
    const onAbort = () => { record.pendingAtAbort = record.pending; record.abortAt = clock.now(); };
    signal?.addEventListener("abort", onAbort, {once: true});
    if (signal?.aborted) onAbort();
    try {
      const value = await operation();
      record.settlement = "resolved";
      return value;
    } catch (error) {
      record.settlement = "rejected";
      record.rejectedAfterAbort = record.pendingAtAbort && signal?.aborted === true;
      record.errorName = error.name;
      throw error;
    } finally {
      record.pending = false;
      record.settledAt = clock.now();
      signal?.removeEventListener("abort", onAbort);
    }
  }
  const pendingUntilAbort = signal => new Promise((resolve, reject) => {
    // No signal means no automatic completion: the independent executor guard must detect it.
    const rejectAbort = () => reject(Object.assign(new Error("controlled cancellation"), {name: "AbortError"}));
    if (signal?.aborted) rejectAbort();
    else signal?.addEventListener("abort", rejectAbort, {once: true});
  });
  let port = 12345, server;
  if (mode === "native") {
    server = http.createServer((request, response) => {
      responses++;
      if (scenario === "hang") return;
      if (scenario === "body_hang") { response.writeHead(200).write("["); return; }
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify(targetsFor(responses)));
    });
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    port = server.address().port;
  }
  const fetch = async (url, options) => {
    if (url !== `http://127.0.0.1:${port}/json/list`) throw new Error("NON_LOOPBACK_REQUEST");
    attempts++;
    requestStarts.push(clock.now() - start);
    const signal = options?.signal;
    const response = await observe("fetch", signal, () => {
      if (mode === "native") return globalThis.fetch(url, options);
      if (scenario === "hang") return pendingUntilAbort(signal);
      if (scenario === "errors" && attempts === 1) throw new Error("fetch-canary");
      responses++;
      return {json: () => {
        if (scenario === "body_hang") return pendingUntilAbort(signal);
        if (scenario === "errors" && attempts === 2) throw new SyntaxError("json-canary");
        return targetsFor(responses);
      }};
    });
    return {json: () => observe("body", signal, () => response.json())};
  };
  const sleep = ms => {
    sleeps.push(ms);
    return new Promise(resolve => {
      if (mode === "native") setTimeout(resolve, ms);
      else schedule(scenario === "single_attempt" ? 1100 : ms, resolve);
    });
  };
  const start = clock.now(), wallStart = performance.now();
  let settled, watchdog;
  emitPhase(4, "execution_enter", mode);
  const execution = run(fetch, sleep, String(port), timeoutSignal, clock).then(
    target => settled = {kind: "success", target},
    error => settled = {kind: "error", error: error.message});
  let outcome;
  if (mode === "controlled") {
    // Yield for Promise continuations without advancing virtual time from host elapsed time.
    for (let step = 0; step < 200; step++) {
      await new Promise(resolve => setImmediate(resolve));
      if (settled) { outcome = settled; break; }
      events.sort((left, right) => left.at - right.at || left.id - right.id);
      const event = events.shift();
      if (!event) { outcome = {kind: "guard", reason: "PENDING_OPERATION_WITHOUT_CANCELLATION"}; break; }
      virtualNow = event.at;
      event.fire();
    }
    outcome ??= {kind: "guard", reason: "CONTROLLED_STEP_LIMIT"};
  } else {
    // Production budgets (15s/2s); 22s is executor protection, never a passing deadline result.
    outcome = await Promise.race([execution, new Promise(resolve => {
      watchdog = setTimeout(() => resolve({kind: "guard", reason: "NATIVE_WATCHDOG"}), 22000);
    })]);
  }
  clearTimeout(watchdog);
  emitPhase(5, "outcome_ready", mode);
  const result = {...outcome, compiled: true, mode, attempts, responses, sleeps,
    requestLimits, requestStarts, operations, abortEvents, elapsedMs: clock.now() - start,
    wallElapsedMs: performance.now() - wallStart};
  server?.closeAllConnections();
  server?.close();
  // Stop only this isolated process, including intentionally infinite mutants.
  const serialized = JSON.stringify(result);
  emitPhase(6, "json_serialized", mode);
  emitPhase(7, "stdout_write_enter", mode);
  process.stdout.write(serialized, () => {
    emitPhase(8, "stdout_callback_enter", mode);
    process.exit(0);
  });
});
'''


PHASE_NAMES = (
    "harness_started", "input_received", "compiled", "execution_enter",
    "outcome_ready", "json_serialized", "stdout_write_enter", "stdout_callback_enter",
)


def _captured_output(stdout, stderr):
    def display_text(value):
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value if isinstance(value, str) else ""

    if stdout is None:
        json_status = "absent"
    elif stdout in ("", b""):
        json_status = "empty"
    else:
        try:
            json.loads(stdout)
        except (ValueError, TypeError):
            json_status = "parse_failed"
        else:
            json_status = "parsed"

    phases, summaries = [], []
    invalid_phase_lines = 0
    for line in display_text(stderr).splitlines():
        if not line.startswith("G14_PHASE "):
            if line:
                summaries.append("[non-phase stderr redacted]")
            continue
        try:
            candidate = json.loads(line[len("G14_PHASE "):])
        except (ValueError, TypeError):
            candidate = None
        if (not isinstance(candidate, dict)
                or type(candidate.get("sequence")) is not int
                or candidate["sequence"] not in range(1, 9)
                or candidate.get("phase") not in PHASE_NAMES):
            invalid_phase_lines += 1
            summaries.append("[invalid phase marker redacted]")
            continue
        version = candidate.get("node")
        phase = {
            "sequence": candidate["sequence"], "phase": candidate["phase"],
            "node": version if isinstance(version, str) and len(version) <= 64
            and re.fullmatch(r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version) else "unverified",
            "mode": candidate.get("mode") if candidate.get("mode") in ("controlled", "native", "unknown") else "unverified",
        }
        phases.append(phase)
        summaries.append(json.dumps(phase, ensure_ascii=True))

    # Extract every received phase before limiting the display summary. Never reorder evidence.
    sequences = [phase["sequence"] for phase in phases]
    duplicates = list(dict.fromkeys(number for index, number in enumerate(sequences) if number in sequences[:index]))
    summary = "\n".join(summaries)
    truncated = len(summary) > 512
    if truncated:
        marker = "[summary truncated]"
        summary = summary[:512 - len(marker)] + marker
    return {
        "stdout_present": stdout is not None,
        "stdout_nonempty": bool(display_text(stdout)),
        "stdout_complete_json": json_status == "parsed", "stdout_json_status": json_status,
        "received_phases": phases, "missing_phase_sequences": [n for n in range(1, 9) if n not in sequences],
        "duplicate_phase_sequences": duplicates,
        "phase_order_anomaly": any(right <= left for left, right in zip(sequences, sequences[1:]))
        or any(PHASE_NAMES[phase["sequence"] - 1] != phase["phase"] for phase in phases),
        "invalid_phase_lines": invalid_phase_lines,
        "stderr_present": stderr is not None, "stderr_summary": summary,
        "stderr_summary_truncated": truncated,
    }


class UiSmokePageTargetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise RuntimeError("Node.js is required for the CDP startup contract")

    def _block(self):
        source = DRIVER.read_text(encoding="utf-8")
        start = 'const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));\n'
        end = "const pageTargetId = target.id;"
        self.assertEqual(source.count(start), 1)
        self.assertEqual(source.count(end), 1)
        block = source.split(start, 1)[1].split(end, 1)[0]
        return block

    def _run(self, scenario, block=None, mode="controlled"):
        block = self._block() if block is None else block
        if mode == "controlled":
            self.assertEqual(block.count("await waitForPageTarget()"), 1)
            block = block.replace("await waitForPageTarget()", "await waitForPageTarget(1100, 120)")
        try:
            result = subprocess.run(
                [self.node, "-e", NODE_HARNESS],
                input=json.dumps({"block": block, "scenario": scenario, "mode": mode}),
                text=True, encoding="utf-8", capture_output=True, timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            message = self._failure_message("SUBPROCESS_TIMEOUT", mode, scenario,
                                            exc.stdout, exc.stderr, timeout=exc.timeout)
            raise self.failureException(message) from None
        if result.returncode != 0:
            message = self._failure_message("NONZERO_EXIT", mode, scenario,
                                            result.stdout, result.stderr, returncode=result.returncode)
            raise self.failureException(message) from None
        try:
            decoded = json.loads(result.stdout)
        except (ValueError, TypeError):
            message = self._failure_message("JSON_PARSE_ERROR", mode, scenario,
                                            result.stdout, result.stderr, returncode=result.returncode)
            raise self.failureException(message) from None
        result = decoded
        self.assertTrue(result["compiled"])
        return result

    def _failure_message(self, category, mode, scenario, stdout, stderr, **status):
        report = {"failure_category": category, "test_id": self.id(), "mode": mode, "scenario": scenario, **status}
        try:
            report.update(_captured_output(stdout, stderr))
        except Exception:
            # The primary failure is still raised even if diagnosis itself cannot be formatted.
            report.update(diagnostic_status="CAPTURE_FORMATTING_FAILED", received_phases=None,
                          stdout_present=stdout is not None, stdout_complete_json=None,
                          stdout_json_status="unknown", stderr_summary="[capture formatting failed]",
                          stderr_summary_truncated=False)
        return "NODE_SUBPROCESS_FAILURE " + json.dumps(report, ensure_ascii=True)

    def _assert_success(self, result, attempts=None):
        self._assert_normal_end(result)
        self.assertEqual(result["kind"], "success", result)
        self.assertEqual(result["target"]["type"], "page")
        ws_url = result["target"].get("webSocketDebuggerUrl")
        self.assertTrue(isinstance(ws_url, str) and bool(ws_url.strip()), "INVALID_PAGE_TARGET")
        self.assertEqual(result["target"]["id"], "ready")
        self.assertGreaterEqual(result["attempts"], 1)
        if attempts is not None:
            self.assertEqual(result["attempts"], attempts)
        self.assertEqual(result["sleeps"], [150] * (result["attempts"] - 1))

    def _assert_normal_end(self, result):
        self.assertNotEqual(result["kind"], "guard", "TEST_NOT_FINISHED: " + result.get("reason", ""))

    def _assert_request_budgets(self, result):
        self.assertEqual(result["mode"], "controlled")
        self.assertEqual(len(result["requestStarts"]), result["attempts"])
        self.assertEqual(len(result["requestLimits"]), result["attempts"])
        for started, limit in zip(result["requestStarts"], result["requestLimits"]):
            self.assertLess(started, 1100, "FETCH_STARTED_AFTER_DEADLINE")
            self.assertEqual(limit, min(120, 1100 - started), "REQUEST_BUDGET_MISMATCH")

    def _assert_deadline(self, result, types):
        self._assert_normal_end(result)
        self.assertEqual(result["kind"], "error", result)
        if result["mode"] == "controlled":
            self.assertGreaterEqual(result["elapsedMs"], 1100, "DEADLINE_EARLY_EXIT")
            self._assert_request_budgets(result)
        # Native elapsed time is diagnostic only; guards never count as normal termination.
        self.assertGreaterEqual(result["attempts"], 1)
        self.assertTrue(result["error"].startswith("CDP 未找到 page target:"))
        self.assertIn(f'attempts={result["attempts"]}', result["error"])
        self.assertRegex(result["error"], r"elapsed_ms=\d+")
        self.assertIn("target_types=" + types, result["error"])
        for forbidden in ("url-canary", "title-canary", "ws-canary", "canary.invalid",
                          "fetch-canary", "json-canary", "127.0.0.1", "webSocketDebuggerUrl"):
            self.assertNotIn(forbidden, result["error"])

    def _assert_fetch_timeout(self, result, stage="fetch"):
        self._assert_deadline(result, "unavailable")
        cancelled = [record for record in result["operations"]
                     if record["stage"] == stage and record["pendingAtAbort"]]
        self.assertTrue(cancelled, "NO_PENDING_" + stage.upper() + "_CANCELLATION")
        for record in cancelled:
            self.assertTrue(record["signalReceived"], "SIGNAL_NOT_FORWARDED")
            self.assertEqual(record["signalId"], record["attempt"])
            self.assertEqual(record["settlement"], "rejected", "PENDING_OPERATION_NOT_REJECTED")
            self.assertTrue(record["rejectedAfterAbort"])
            self.assertLessEqual(record["startedAt"], record["abortAt"])
            self.assertLessEqual(record["abortAt"], record["settledAt"])
            events = [event for event in result["abortEvents"] if event["id"] == record["signalId"]]
            self.assertEqual(len(events), 1)
            self.assertLessEqual(events[0]["at"], record["abortAt"])
        # The same loop has handled rejection and reached its own error above.

    def test_empty_then_page(self):
        self._assert_success(self._run("eventual"), 4)

    def test_non_page_deadline(self):
        self._assert_deadline(self._run("non_page"), '{"background_page":1,"service_worker":1}')

    def test_empty_deadline(self):
        self._assert_deadline(self._run("empty"), "{}")

    def test_hanging_fetch_is_aborted(self):
        self._assert_fetch_timeout(self._run("hang", mode="native"))

    def test_hanging_json_body_is_aborted(self):
        self._assert_fetch_timeout(self._run("body_hang", mode="native"), "body")

    def test_controlled_fetch_and_body_cancellation(self):
        for scenario, stage in (("hang", "fetch"), ("body_hang", "body")):
            with self.subTest(stage=stage):
                result = self._run(scenario)
                self._assert_fetch_timeout(result, stage)
                self.assertGreater(result["attempts"], 1)
                self.assertEqual(result["sleeps"], [150] * result["attempts"])

    def test_native_fetch_reads_page_list(self):
        result = self._run("first", mode="native")
        self._assert_success(result)
        self.assertGreaterEqual(result["responses"], 1)
        self.assertEqual([record["settlement"] for record in result["operations"][-2:]], ["resolved", "resolved"])

    def test_native_deadline_diagnostics_are_private(self):
        self._assert_deadline(self._run("non_page", mode="native"),
                              '{"background_page":1,"service_worker":1}')

    def test_controlled_remaining_request_budget(self):
        result = self._run("empty")
        self._assert_deadline(result, "{}")
        self.assertEqual(result["requestStarts"], [0, 150, 300, 450, 600, 750, 900, 1050])
        self.assertEqual(result["requestLimits"], [120] * 7 + [50])
        self.assertEqual(result["sleeps"], [150] * 8)
        self.assertEqual(result["elapsedMs"], 1200)

    def test_page_requires_nonempty_websocket_url(self):
        self._assert_success(self._run("invalid_ws"), 5)

    def test_first_page_has_no_polling_delay(self):
        self._assert_success(self._run("first"), 1)

    def test_fetch_and_json_errors_retry(self):
        result = self._run("errors")
        self._assert_success(result, 3)
        self.assertEqual(result["responses"], 2)

    def test_single_attempt_deadline_rejects_old_count_assumption(self):
        result = self._run("single_attempt")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["sleeps"], [150])
        self.assertEqual(result["elapsedMs"], 1100)
        with self.assertRaisesRegex(AssertionError, "1 not greater than 1"):
            self.assertGreater(result["attempts"], 1)
        self._assert_deadline(result, "{}")

    def test_production_limits_and_call_are_explicit(self):
        block = self._block()
        self.assertIn("waitForPageTarget(timeoutMs = 15000, fetchTimeoutMs = 2000)", block)
        self.assertEqual(block.count("const target = await waitForPageTarget();"), 1)

    def test_later_failure_does_not_restart_target_acquisition(self):
        result = self._run("first", self._block() + '\nthrow new Error("AFTER_TARGET_FAILURE");')
        self.assertEqual(result["kind"], "error")
        self.assertEqual(result["error"], "AFTER_TARGET_FAILURE")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["sleeps"], [])

    def _mutated_block(self, before, after):
        block = self._block()
        self.assertEqual(block.count(before), 1, "MUTATION_MATCH_COUNT")
        mutated = block.replace(before, after, 1)
        self.assertNotEqual(mutated, block, "MUTATION_DID_NOT_CHANGE_SOURCE")
        return mutated

    def test_mutation_missing_deadline_is_rejected(self):
        block = self._mutated_block("while (performance.now() < deadline)", "while (true)")
        for scenario, types in (("empty", "{}"), ("non_page", '{"background_page":1,"service_worker":1}')):
            with self.subTest(scenario=scenario):
                result = self._run(scenario, block)
                self.assertEqual(result["kind"], "guard")
                self.assertEqual(result["reason"], "CONTROLLED_STEP_LIMIT")
                self.assertGreater(result["attempts"], 1)
                self.assertTrue(any(start >= 1100 for start in result["requestStarts"]))
                with self.assertRaisesRegex(AssertionError, "TEST_NOT_FINISHED: CONTROLLED_STEP_LIMIT"):
                    self._assert_deadline(result, types)

    def test_mutation_missing_fetch_timeout_is_rejected(self):
        block = self._mutated_block("signal: AbortSignal.timeout(requestLimit)", "signal: undefined")
        for scenario, stage in (("hang", "fetch"), ("body_hang", "body")):
            with self.subTest(stage=stage):
                result = self._run(scenario, block)
                self.assertEqual(result["kind"], "guard")
                self.assertEqual(result["reason"], "PENDING_OPERATION_WITHOUT_CANCELLATION")
                self.assertEqual(result["attempts"], 1)
                self.assertEqual(result["abortEvents"], [])
                pending = [record for record in result["operations"] if record["stage"] == stage]
                self.assertEqual(len(pending), 1)
                self.assertTrue(pending[0]["pending"])
                self.assertFalse(pending[0]["signalReceived"])
                self.assertIsNone(pending[0]["settlement"])
                with self.assertRaisesRegex(AssertionError, "TEST_NOT_FINISHED: PENDING_OPERATION_WITHOUT_CANCELLATION"):
                    self._assert_fetch_timeout(result, stage)

    def test_mutation_skipped_loop_is_rejected(self):
        block = self._mutated_block("while (performance.now() < deadline)", "while (false)")
        result = self._run("empty", block)
        self.assertEqual(result["kind"], "error")
        self.assertEqual(result["attempts"], 0)
        with self.assertRaisesRegex(AssertionError, "DEADLINE_EARLY_EXIT"):
            self._assert_deadline(result, "unavailable")

    def test_mutation_remaining_budget_is_rejected(self):
        block = self._mutated_block("Math.min(fetchTimeoutMs, deadline - performance.now())", "fetchTimeoutMs")
        result = self._run("empty", block)
        self.assertEqual(result["kind"], "error")
        with self.assertRaisesRegex(AssertionError, "REQUEST_BUDGET_MISMATCH"):
            self._assert_deadline(result, "{}")

    def test_mutation_type_only_predicate_is_rejected(self):
        block = self._mutated_block(
            '&& typeof item.webSocketDebuggerUrl === "string" && item.webSocketDebuggerUrl.trim().length > 0',
            "&& true",
        )
        result = self._run("invalid_ws", block)
        self.assertEqual(result["kind"], "success")
        self.assertEqual(result["attempts"], 1)
        with self.assertRaisesRegex(AssertionError, "INVALID_PAGE_TARGET"):
            self._assert_success(result, 5)


class NodeFailureDiagnosticsTest(unittest.TestCase):
    phases = (
        "harness_started", "input_received", "compiled", "execution_enter",
        "outcome_ready", "json_serialized", "stdout_write_enter", "stdout_callback_enter",
    )

    def setUp(self):
        self.runner = UiSmokePageTargetTest("test_first_page_has_no_polling_delay")
        self.runner.node = "node"

    def _phase(self, sequence, **extra):
        return "G14_PHASE " + json.dumps({
            "sequence": sequence, "phase": self.phases[sequence - 1],
            "node": "v24.15.0", "mode": "controlled", **extra,
        }) + "\n"

    def _invoke_failure(self, result):
        options = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
        with patch.object(subprocess, "run", **options) as run:
            try:
                self.runner._run("hang", mode="controlled")
            except Exception as error:
                captured = error
                standard_text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            else:
                self.fail("SUBPROCESS_FAILURE_WAS_ACCEPTED")
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["scenario"], "hang")
        return captured, standard_text

    def _report(self, result, category):
        error, standard_text = self._invoke_failure(result)
        self.assertIsInstance(error, AssertionError, "ORIGINAL_FAILURE_WAS_MASKED")
        prefix = "NODE_SUBPROCESS_FAILURE "
        self.assertTrue(str(error).startswith(prefix), "DIAGNOSTIC_REPORT_MISSING")
        report = json.loads(str(error)[len(prefix):])
        self.assertEqual(report["failure_category"], category)
        self.assertEqual(report["test_id"], self.runner.id())
        self.assertEqual((report["mode"], report["scenario"]), ("controlled", "hang"))
        return report, standard_text

    def test_phase_emit_sites_are_single_and_ordered(self):
        positions = []
        for sequence, name in enumerate(self.phases, 1):
            call = f'emitPhase({sequence}, "{name}"'
            self.assertEqual(NODE_HARNESS.count(call), 1, "PHASE_EMIT_SITE_COUNT:" + name)
            positions.append(NODE_HARNESS.index(call))
        self.assertEqual(positions, sorted(positions))
        self.assertLess(positions[0], NODE_HARNESS.index('process.stdin.on("data"'))
        self.assertLess(NODE_HARNESS.index('process.stdin.on("end"'), positions[1])
        self.assertLess(NODE_HARNESS.index('block + "\\nreturn target;"'), positions[2])
        self.assertLess(positions[3], NODE_HARNESS.index("const execution = run("))
        self.assertLess(NODE_HARNESS.index("clearTimeout(watchdog)"), positions[4])
        self.assertLess(NODE_HARNESS.index("server?.close();"), positions[5])
        self.assertLess(NODE_HARNESS.index("JSON.stringify(result)"), positions[5])
        self.assertLess(positions[6], NODE_HARNESS.index("process.stdout.write("))
        self.assertLess(NODE_HARNESS.index("process.stdout.write("), positions[7])
        self.assertLess(positions[7], NODE_HARNESS.index("process.exit(0)"))
        emitter = NODE_HARNESS.split("function emitPhase(", 1)[1].split("\n}", 1)[0]
        self.assertIn("process.stderr.write(", emitter)
        self.assertIn("process.version", emitter)
        self.assertNotIn("await", emitter)
        self.assertNotIn("writeFile", emitter)

    def test_controlled_success_keeps_result_contract(self):
        self.runner.node = shutil.which("node")
        self.assertIsNotNone(self.runner.node)
        actual_run = subprocess.run
        captured = []

        def record(*args, **kwargs):
            result = actual_run(*args, **kwargs)
            captured.append(result)
            return result

        with patch.object(subprocess, "run", side_effect=record):
            result = self.runner._run("first")
        self.runner._assert_success(result, 1)
        self.assertEqual(len(captured), 1)
        self.assertEqual(json.loads(captured[0].stdout), result)
        self.assertEqual(set(result), {
            "kind", "target", "compiled", "mode", "attempts", "responses", "sleeps",
            "requestLimits", "requestStarts", "operations", "abortEvents", "elapsedMs", "wallElapsedMs",
        })

    def test_timeout_run_retains_captured_evidence(self):
        stdout = b'{"kind":"error"}'
        stderr = (self._phase(1) + self._phase(2) + self._phase(4)).encode()
        failure = subprocess.TimeoutExpired(["node", "-e", "COMMAND_CANARY"], 30, output=stdout, stderr=stderr)
        self.assertIs(failure.stdout, failure.output)
        report, _ = self._report(failure, "SUBPROCESS_TIMEOUT")
        self.assertEqual(report["timeout"], 30)
        self.assertTrue(report["stdout_present"])
        self.assertTrue(report["stdout_complete_json"])
        self.assertEqual([phase["sequence"] for phase in report["received_phases"]], [1, 2, 4])
        self.assertEqual(report["missing_phase_sequences"], [3, 5, 6, 7, 8])
        self.assertEqual(report["received_phases"][0]["node"], "v24.15.0")
        self.assertNotIn("output", report)

    def test_nonzero_run_keeps_complete_stdout_and_exitcode(self):
        completed = subprocess.CompletedProcess(["node", "-e", "COMMAND_CANARY"], 3221226505,
                                                '{"kind":"success","private":"STDOUT_CANARY"}', self._phase(8))
        report, text = self._report(completed, "NONZERO_EXIT")
        self.assertEqual(report["returncode"], 3221226505)
        self.assertTrue(report["stdout_present"])
        self.assertTrue(report["stdout_complete_json"])
        self.assertEqual(report["received_phases"][0]["phase"], "stdout_callback_enter")
        self.assertNotIn("STDOUT_CANARY", text)
        self.assertNotIn("COMMAND_CANARY", text)

    def test_invalid_json_run_retains_context(self):
        completed = subprocess.CompletedProcess(["node"], 0, "complete but invalid JSON", self._phase(6))
        report, _ = self._report(completed, "JSON_PARSE_ERROR")
        self.assertEqual(report["returncode"], 0)
        self.assertTrue(report["stdout_present"])
        self.assertFalse(report["stdout_complete_json"])
        self.assertEqual(report["stdout_json_status"], "parse_failed")
        self.assertEqual(report["received_phases"][0]["phase"], "json_serialized")

    def test_diagnostic_inputs_do_not_mask_failure(self):
        streams = {"none": None, "empty": "", "invalid_utf8": b"\xff\xfe", "partial_marker": 'G14_PHASE {"sequence":'}
        for category in ("SUBPROCESS_TIMEOUT", "NONZERO_EXIT", "JSON_PARSE_ERROR"):
            for case, stream in streams.items():
                with self.subTest(category=category, stream=case):
                    failure = (subprocess.TimeoutExpired(["node"], 30, output=stream, stderr=stream)
                               if category == "SUBPROCESS_TIMEOUT" else
                               subprocess.CompletedProcess(["node"], 1 if category == "NONZERO_EXIT" else 0, stream, stream))
                    report, _ = self._report(failure, category)
                    self.assertEqual(report["received_phases"], [])
                    self.assertEqual(report["stdout_present"], stream is not None)
                    self.assertFalse(report["stdout_complete_json"])
                    self.assertEqual(report["invalid_phase_lines"], int(case == "partial_marker"))

    def test_phase_reception_keeps_gaps_duplicates_and_order_before_summary_limit(self):
        stderr = "\n".join(["SECRET_CANARY https://secret.invalid/?token=synthetic"] * 80) + "\n"
        stderr += self._phase(1) + self._phase(4) + self._phase(2) + self._phase(4)
        stderr += self._phase(6, node="VERSION_CANARY", mode="MODE_CANARY", extra="EXTRA_CANARY")
        report, text = self._report(subprocess.CompletedProcess(["node"], 7, "", stderr), "NONZERO_EXIT")
        self.assertEqual([phase["sequence"] for phase in report["received_phases"]], [1, 4, 2, 4, 6])
        self.assertEqual(report["missing_phase_sequences"], [3, 5, 7, 8])
        self.assertEqual(report["duplicate_phase_sequences"], [4])
        self.assertTrue(report["phase_order_anomaly"])
        self.assertLessEqual(len(report["stderr_summary"]), 512)
        self.assertTrue(report["stderr_summary_truncated"])
        self.assertIn("[summary truncated]", report["stderr_summary"])
        for canary in ("SECRET_CANARY", "secret.invalid", "VERSION_CANARY", "MODE_CANARY", "EXTRA_CANARY"):
            self.assertNotIn(canary, text)

    def test_standard_failure_text_does_not_expose_command_or_stderr_canary(self):
        canary = "NODE_COMMAND_BODY_CANARY_38_2"
        failure = subprocess.TimeoutExpired(["node", "-e", canary], 30,
                                            output=b'{"private":"STDOUT_CANARY"}',
                                            stderr=(self._phase(3) + "STDERR_CANARY").encode())
        self.assertIn(canary, str(failure))
        error, text = self._invoke_failure(failure)
        self.assertIsInstance(error, AssertionError)
        for forbidden in (canary, "STDERR_CANARY", "STDOUT_CANARY", "During handling of the above exception"):
            self.assertNotIn(forbidden, text)
        self.assertTrue(error.__suppress_context__)

    def test_diagnostic_formatter_failure_keeps_primary_failure(self):
        failures = (
            ("SUBPROCESS_TIMEOUT", subprocess.TimeoutExpired(["node"], 30)),
            ("NONZERO_EXIT", subprocess.CompletedProcess(["node"], 7, "{}", "")),
            ("JSON_PARSE_ERROR", subprocess.CompletedProcess(["node"], 0, "invalid", "")),
        )
        for category, failure in failures:
            with self.subTest(category=category):
                with patch(__name__ + "._captured_output", side_effect=ValueError("FORMATTER_CANARY")):
                    report, text = self._report(failure, category)
                self.assertEqual(report["diagnostic_status"], "CAPTURE_FORMATTING_FAILED")
                self.assertIsNone(report["received_phases"])
                self.assertIsNone(report["stdout_complete_json"])
                self.assertEqual(report.get("timeout", report.get("returncode")),
                                 30 if category == "SUBPROCESS_TIMEOUT" else failure.returncode)
                self.assertNotIn("FORMATTER_CANARY", text)


if __name__ == "__main__":
    unittest.main()
