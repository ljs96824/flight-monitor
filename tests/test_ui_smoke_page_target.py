"""Separate virtual-time startup logic from native loopback integration."""

import json
from pathlib import Path
import shutil
import subprocess
import unittest


DRIVER = Path(__file__).resolve().parents[1] / "scripts" / "ui_smoke_driver.mjs"
NODE_HARNESS = r'''
const http = require("node:http");
const {performance} = require("node:perf_hooks");
let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", chunk => input += chunk);
process.stdin.on("end", async () => {
  const {block, scenario, mode} = JSON.parse(input);
  const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
  // Extraction/compilation failures are infrastructure errors, never mutation kills.
  const run = new AsyncFunction("fetch", "sleep", "cdpPort", "AbortSignal", "performance",
    block + "\nreturn target;");
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
  const result = {...outcome, compiled: true, mode, attempts, responses, sleeps,
    requestLimits, requestStarts, operations, abortEvents, elapsedMs: clock.now() - start,
    wallElapsedMs: performance.now() - wallStart};
  server?.closeAllConnections();
  server?.close();
  // Stop only this isolated process, including intentionally infinite mutants.
  process.stdout.write(JSON.stringify(result), () => process.exit(0));
});
'''


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
        except subprocess.TimeoutExpired:
            self.fail("TEST_NOT_FINISHED: SUBPROCESS_TIMEOUT")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = json.loads(result.stdout)
        self.assertTrue(result["compiled"])
        return result

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


if __name__ == "__main__":
    unittest.main()
