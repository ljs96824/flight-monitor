"""Run the real startup block in Node against a loopback-only CDP stub."""

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
  const {block, scenario} = JSON.parse(input);
  const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
  // Compile before starting the server: syntax errors are never mutation kills.
  const run = new AsyncFunction("fetch", "sleep", "cdpPort", "AbortSignal", "performance",
    block + "\nreturn target;");
  const good = {type: "page", id: "ready", webSocketDebuggerUrl: "ws://canary.invalid/page"};
  let attempts = 0, responses = 0, aborts = 0;
  const sleeps = [], requestLimits = [];
  const server = http.createServer((request, response) => {
    responses++;
    if (scenario === "hang" || scenario === "body_hang") {
      if (scenario === "body_hang") response.writeHead(200).write("[");
      return;
    }
    if (scenario === "errors" && responses === 1) return request.socket.destroy();
    if (scenario === "errors" && responses === 2) return response.end("not JSON");
    let targets = [good];
    if (scenario === "empty" || (scenario === "eventual" && responses <= 3)) targets = [];
    if (scenario === "non_page") targets = [
      {type: "background_page", url: "url-canary", title: "title-canary"},
      {type: "service_worker", webSocketDebuggerUrl: "ws-canary"},
    ];
    if (scenario === "invalid_ws" && responses <= 4) targets = [
      {type: "page", webSocketDebuggerUrl: ["", "   ", null, 42][responses - 1]},
    ];
    response.setHeader("Content-Type", "application/json");
    response.end(JSON.stringify(targets));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  const nativeFetch = globalThis.fetch;
  const controlledFetch = (url, options) => {
    if (url !== `http://127.0.0.1:${port}/json/list`) throw new Error("NON_LOOPBACK_REQUEST");
    attempts++;
    options?.signal?.addEventListener("abort", () => aborts++, {once: true});
    return nativeFetch(url, options);
  };
  const signal = {timeout(ms) { requestLimits.push(ms); return AbortSignal.timeout(ms); }};
  const sleep = ms => { sleeps.push(ms); return new Promise(resolve => setTimeout(resolve, ms)); };
  const start = performance.now();
  let watchdog;
  const outcome = await Promise.race([
    run(controlledFetch, sleep, String(port), signal, performance).then(
      target => ({kind: "success", target}), error => ({kind: "error", error: error.message})),
    new Promise(resolve => { watchdog = setTimeout(() => resolve({
      kind: "watchdog", reason: scenario.includes("hang")
        ? "FETCH_TIMEOUT_MISSING" : "PAGE_TARGET_DEADLINE_MISSING",
    }), 2600); }),
  ]);
  clearTimeout(watchdog);
  const result = {...outcome, compiled: true, attempts, responses, aborts, sleeps,
    requestLimits, elapsedMs: performance.now() - start};
  server.closeAllConnections();
  server.close();
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
        # Shorten only injectable test deadlines, never the fixed polling interval.
        return block.replace("await waitForPageTarget()", "await waitForPageTarget(1100, 120)")

    def _run(self, scenario, block=None):
        result = subprocess.run(
            [self.node, "-e", NODE_HARNESS],
            input=json.dumps({"block": self._block() if block is None else block, "scenario": scenario}),
            text=True, encoding="utf-8", capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        result = json.loads(result.stdout)
        self.assertTrue(result["compiled"])
        return result

    def _assert_success(self, result, attempts):
        self.assertEqual(result["kind"], "success", result)
        ws_url = result["target"].get("webSocketDebuggerUrl")
        self.assertTrue(isinstance(ws_url, str) and bool(ws_url.strip()), "INVALID_PAGE_TARGET")
        self.assertEqual(result["target"]["id"], "ready")
        self.assertEqual(result["attempts"], attempts)
        self.assertEqual(result["sleeps"], [150] * (attempts - 1))

    def _assert_deadline(self, result, types):
        self.assertEqual(result["kind"], "error", result)
        self.assertGreater(result["attempts"], 1)
        self.assertGreaterEqual(result["elapsedMs"], 1100)
        self.assertLess(result["elapsedMs"], 2300)
        self.assertIn(f'attempts={result["attempts"]}', result["error"])
        self.assertRegex(result["error"], r"elapsed_ms=\d+")
        self.assertIn("target_types=" + types, result["error"])
        for forbidden in ("url-canary", "title-canary", "ws-canary", "127.0.0.1", "webSocketDebuggerUrl"):
            self.assertNotIn(forbidden, result["error"])

    def _assert_fetch_timeout(self, result):
        self._assert_deadline(result, "unavailable")
        self.assertGreaterEqual(result["aborts"], 2)
        self.assertTrue(all(0 < limit <= 120 for limit in result["requestLimits"]))

    def test_empty_then_page(self):
        self._assert_success(self._run("eventual"), 4)

    def test_non_page_deadline(self):
        self._assert_deadline(self._run("non_page"), '{"background_page":1,"service_worker":1}')

    def test_empty_deadline(self):
        self._assert_deadline(self._run("empty"), "{}")

    def test_hanging_fetch_is_aborted(self):
        self._assert_fetch_timeout(self._run("hang"))

    def test_hanging_json_body_is_aborted(self):
        self._assert_fetch_timeout(self._run("body_hang"))

    def test_page_requires_nonempty_websocket_url(self):
        self._assert_success(self._run("invalid_ws"), 5)

    def test_first_page_has_no_polling_delay(self):
        self._assert_success(self._run("first"), 1)

    def test_fetch_and_json_errors_retry(self):
        self._assert_success(self._run("errors"), 3)

    def test_production_limits_and_call_are_explicit(self):
        block = self._block()
        self.assertIn("waitForPageTarget(timeoutMs = 15000, fetchTimeoutMs = 2000)", block)
        self.assertEqual(block.count("const target = await waitForPageTarget(1100, 120);"), 1)

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
                self.assertEqual(result["kind"], "watchdog")
                self.assertEqual(result["reason"], "PAGE_TARGET_DEADLINE_MISSING")
                self.assertGreater(result["attempts"], 1)
                with self.assertRaisesRegex(AssertionError, "PAGE_TARGET_DEADLINE_MISSING"):
                    self._assert_deadline(result, types)

    def test_mutation_missing_fetch_timeout_is_rejected(self):
        block = self._mutated_block("signal: AbortSignal.timeout(requestLimit)", "signal: undefined")
        result = self._run("hang", block)
        self.assertEqual(result["kind"], "watchdog")
        self.assertEqual(result["reason"], "FETCH_TIMEOUT_MISSING")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["aborts"], 0)
        with self.assertRaisesRegex(AssertionError, "FETCH_TIMEOUT_MISSING"):
            self._assert_fetch_timeout(result)

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
