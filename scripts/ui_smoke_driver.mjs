import {createHash} from "node:crypto";
import {mkdir, readFile, writeFile} from "node:fs/promises";
import {dirname, join} from "node:path";
import {isDeepStrictEqual} from "node:util";

const [baseUrl, cdpPort, artifactDir, subscriptionsPath] = process.argv.slice(2);
if (!baseUrl || !cdpPort || !subscriptionsPath) {
  throw new Error("缺少 baseUrl/cdpPort/subscriptionsPath");
}

const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const targets = await fetch(`http://127.0.0.1:${cdpPort}/json/list`).then(r => r.json());
const target = targets.find(item => item.type === "page");
if (!target) throw new Error("CDP 未找到 page target");
const pageTargetId = target.id;
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  ws.addEventListener("open", resolve, {once: true});
  ws.addEventListener("error", reject, {once: true});
});
let nextId = 1;
const pending = new Map();
const browserErrors = [];
const priceHintResponses = [];
let pageLoadGeneration = 0;

function handleCdpMessage(event) {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const {resolve, reject} = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(JSON.stringify(message.error)));
    else resolve(message.result || {});
    return;
  }
  if (message.method === "Page.loadEventFired") {
    pageLoadGeneration += 1;
  }
  if (message.method === "Network.responseReceived") {
    const responseUrl = new URL(message.params.response.url);
    if (responseUrl.origin === new URL(baseUrl).origin && responseUrl.pathname === "/price_hint") {
      priceHintResponses.push({
        requestId: message.params.requestId,
        url: responseUrl.href,
        status: message.params.response.status,
        finished: false,
        failed: false,
        body: null,
      });
    }
  }
  if (message.method === "Network.loadingFinished") {
    const response = priceHintResponses.find(item => item.requestId === message.params.requestId);
    if (response) response.finished = true;
  }
  if (message.method === "Network.loadingFailed") {
    const response = priceHintResponses.find(item => item.requestId === message.params.requestId);
    if (response) {
      response.failed = true;
      response.failureType = message.params.errorText || "Network.loadingFailed";
    }
  }
  if (message.method === "Runtime.exceptionThrown") {
    browserErrors.push(message.params.exceptionDetails?.text || "Runtime.exceptionThrown");
  }
  if (message.method === "Runtime.consoleAPICalled" && message.params.type === "error") {
    const details = (message.params.args || [])
      .map(argument => argument.value ?? argument.description ?? argument.type)
      .join(" ");
    browserErrors.push(details ? `console.error: ${details}` : "console.error");
  }
  if (message.method === "Log.entryAdded" && message.params.entry?.level === "error") {
    browserErrors.push(message.params.entry.text || "Log.error");
  }
}

ws.addEventListener("message", handleCdpMessage);

function command(method, params = {}, timeout = 12000) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`CDP命令超时: ${method}`));
    }, timeout);
    pending.set(id, {
      resolve: value => {
        clearTimeout(timer);
        resolve(value);
      },
      reject: error => {
        clearTimeout(timer);
        reject(error);
      },
    });
    try {
      ws.send(JSON.stringify({id, method, params}));
    } catch (error) {
      clearTimeout(timer);
      pending.delete(id);
      reject(error);
    }
  });
}

async function reattachPage(label, expectedPath, previousLoadGeneration) {
  const expectedUrl = new URL(expectedPath, baseUrl).href;
  await waitForPageLoad(previousLoadGeneration, `${label}加载事件`, 15000);
  await waitForTargetUrl(expectedUrl, `${label}导航`, 15000);
  await waitFor("document.readyState === 'complete'", `${label}加载`);
}

async function evaluate(expression) {
  const result = await command("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "evaluate failed");
  return result.result?.value;
}

async function waitFor(expression, label, timeout = 12000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await sleep(100);
  }
  throw new Error(`等待超时: ${label}`);
}

function clearPriceHintTrace() {
  priceHintResponses.length = 0;
}

async function waitForPriceHintResponse(origin, dest, timeout = 12000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const response = priceHintResponses.findLast(item => {
      const url = new URL(item.url);
      return url.pathname === "/price_hint"
        && url.searchParams.get("origin") === origin
        && url.searchParams.get("dest") === dest;
    });
    if (response?.body) return response;
    if (response?.failed) {
      throw new Error(`PRICE_HINT_RESPONSE_NOT_CAPTURED: ${response.failureType}`);
    }
    if (response?.finished) {
      const captured = await command("Network.getResponseBody", {
        requestId: response.requestId,
      });
      const bodyText = captured.base64Encoded
        ? Buffer.from(captured.body, "base64").toString("utf8")
        : captured.body;
      response.body = JSON.parse(bodyText);
      return response;
    }
    await sleep(100);
  }
  throw new Error(`PRICE_HINT_RESPONSE_NOT_CAPTURED: origin=${origin} dest=${dest}`);
}

async function filePresenceAndSha256(path) {
  try {
    const content = await readFile(path);
    return {
      exists: true,
      sha256: createHash("sha256").update(content).digest("hex"),
    };
  } catch (error) {
    if (error?.code === "ENOENT") return {exists: false, sha256: null};
    throw error;
  }
}

async function priceHintStorageState() {
  const dataDir = dirname(subscriptionsPath);
  return {
    subscriptions: await filePresenceAndSha256(subscriptionsPath),
    observations: await filePresenceAndSha256(join(dataDir, "observations.sqlite3")),
    prices: await filePresenceAndSha256(join(dataDir, "prices.db")),
  };
}

async function waitForTargetUrl(expectedUrl, label, timeout = 12000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const currentTargets = await fetch(`http://127.0.0.1:${cdpPort}/json/list`).then(response => response.json());
    if (currentTargets.some(item =>
      item.type === "page"
      && item.id === pageTargetId
      && item.url === expectedUrl
    )) {
      return;
    }
    await sleep(100);
  }
  throw new Error(`等待超时: ${label}`);
}

async function waitForPageLoad(previousGeneration, label, timeout = 12000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (pageLoadGeneration > previousGeneration) return;
    await sleep(100);
  }
  throw new Error(`等待超时: ${label}`);
}

async function navigate(path) {
  const expectedUrl = new URL(path, baseUrl).href;
  const previousLoadGeneration = pageLoadGeneration;
  await command("Page.navigate", {url: expectedUrl});
  await waitForPageLoad(previousLoadGeneration, `加载事件${path}`);
  await waitForTargetUrl(expectedUrl, `导航${path}`);
  await waitFor("document.readyState === 'complete'", `加载${path}`);
}

async function pressKey(key, code, windowsVirtualKeyCode) {
  const params = {key, code, windowsVirtualKeyCode, nativeVirtualKeyCode: windowsVirtualKeyCode};
  await command("Input.dispatchKeyEvent", {type: "keyDown", ...params});
  await command("Input.dispatchKeyEvent", {type: "keyUp", ...params});
}

async function clickSelector(selector) {
  const previousLoadGeneration = pageLoadGeneration;
  const point = await evaluate(`(() => {
    const element = document.querySelector(${JSON.stringify(selector)});
    if (!element) throw new Error('missing ' + ${JSON.stringify(selector)});
    element.scrollIntoView({block: 'center'});
    const rect = element.getBoundingClientRect();
    return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
  })()`);
  await command("Input.dispatchMouseEvent", {type: "mousePressed", x: point.x, y: point.y, button: "left", clickCount: 1});
  await command("Input.dispatchMouseEvent", {type: "mouseReleased", x: point.x, y: point.y, button: "left", clickCount: 1});
  return previousLoadGeneration;
}

async function clickFormButton(action, expectedFields = {}) {
  const previousLoadGeneration = pageLoadGeneration;
  const scheduled = await evaluate(`(() => {
    const action = ${JSON.stringify(action)};
    const expectedFields = ${JSON.stringify(expectedFields)};
    const forms = [...document.querySelectorAll('form')];
    const form = forms.find(candidate => {
      if (candidate.getAttribute('action') !== action) return false;
      return Object.entries(expectedFields).every(([name, value]) =>
        candidate.querySelector('[name="' + name + '"][value="' + value + '"]')
      );
    });
    if (!form) throw new Error('missing form ' + action);
    const button = form.querySelector('button[type="submit"], input[type="submit"]');
    if (!button) throw new Error('missing submit button ' + action);
    button.scrollIntoView({block: 'center'});
    setTimeout(() => button.click(), 0);
    return true;
  })()`);
  if (!scheduled) throw new Error(`未能点击表单按钮: ${action}`);
  return previousLoadGeneration;
}

async function captureSuccessSubscriptionId(label) {
  const locationState = await evaluate(`(() => {
    const url = new URL(location.href);
    return {
      pathname: url.pathname,
      search: url.search,
      keys: [...url.searchParams.keys()],
      subscriptionId: url.searchParams.get('subscription_id') || '',
    };
  })()`);
  if (
    locationState.pathname !== "/success"
    || !uuidPattern.test(locationState.subscriptionId)
    || locationState.search !== `?subscription_id=${locationState.subscriptionId}`
    || JSON.stringify(locationState.keys) !== JSON.stringify(["subscription_id"])
  ) {
    throw new Error(`${label} success UUID合同失败: ${JSON.stringify(locationState)}`);
  }
  return locationState.subscriptionId;
}

async function readSubscriptions() {
  const payload = JSON.parse(await readFile(subscriptionsPath, "utf8"));
  if (!Array.isArray(payload)) throw new Error("临时subscriptions根节点不是数组");
  return payload;
}

function subscriptionById(subscriptions, subscriptionId, label) {
  const matches = subscriptions.filter(item => item?.subscription_id === subscriptionId);
  if (matches.length !== 1) {
    throw new Error(`${label} subscription_id匹配数=${matches.length}`);
  }
  return matches[0];
}

function assertDeepEqual(actual, expected, label) {
  if (!isDeepStrictEqual(actual, expected)) {
    throw new Error(`${label}逐字段不一致`);
  }
}

function withOnlyChange(baseline, mutate) {
  const expected = structuredClone(baseline);
  mutate(expected);
  return expected;
}

async function subscriptionCardState(subscriptionId) {
  const toggleAction = `/subscriptions/${subscriptionId}/toggle`;
  return evaluate(`(() => {
    const subscriptionId = ${JSON.stringify(subscriptionId)};
    const toggleAction = ${JSON.stringify(toggleAction)};
    const toggleForm = [...document.querySelectorAll('form')]
      .find(form => form.getAttribute('action') === toggleAction);
    const card = toggleForm?.closest('.card');
    if (!card) throw new Error('missing subscription card ' + subscriptionId);
    return {
      editHref: card.querySelector('a[href^="/?edit="]')?.getAttribute('href') || '',
      toggleAction: toggleForm.getAttribute('action') || '',
      toggleText: toggleForm.querySelector('button[type="submit"]')?.textContent.trim() || '',
      deleteHref: card.querySelector('a.danger')?.getAttribute('href') || '',
      statusClass: card.querySelector('.status')?.className || '',
      statusText: card.querySelector('.status')?.textContent.trim() || '',
    };
  })()`);
}

async function assertSubscriptionCardActions(subscriptionId) {
  const state = await subscriptionCardState(subscriptionId);
  const expected = {
    editHref: `/?edit=${subscriptionId}`,
    toggleAction: `/subscriptions/${subscriptionId}/toggle`,
    deleteHref: `/subscription/${subscriptionId}/delete`,
  };
  for (const [field, value] of Object.entries(expected)) {
    if (state[field] !== value) {
      throw new Error(`UUID action错误: ${field} expected=${value} actual=${state[field]}`);
    }
  }
  return state;
}

async function openSubscriptionEditorFromCard(subscriptionId) {
  await navigate("/subscriptions");
  const state = await assertSubscriptionCardActions(subscriptionId);
  await assertNoNumericSubscriptionActions();
  const previousLoadGeneration = await clickSelector(`a[href="${state.editHref}"]`);
  await reattachPage(
    `UUID编辑重定向${subscriptionId.slice(0, 8)}`,
    `/settings?edit=${subscriptionId}`,
    previousLoadGeneration,
  );
}

async function assertNoNumericSubscriptionActions() {
  const violations = await evaluate(`(() => {
    const values = [...document.querySelectorAll('a[href], form[action]')]
      .map(element => element.getAttribute('href') || element.getAttribute('action') || '');
    const patterns = [
      /^\\/(settings)?\\?edit=\\d+$/,
      /^\\/subscriptions\\/\\d+\\/(toggle|quick-update)$/,
      /^\\/subscription\\/\\d+\\/delete$/,
      /^\\/success\\?index=\\d+$/,
    ];
    return values.filter(value => patterns.some(pattern => pattern.test(value)));
  })()`);
  if (violations.length) {
    throw new Error(`新控件仍使用数字index: ${JSON.stringify(violations)}`);
  }
}

async function waitForSubscriptions(predicate, label, timeout = 12000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const subscriptions = await readSubscriptions();
    if (predicate(subscriptions)) return subscriptions;
    await sleep(100);
  }
  throw new Error(`等待超时: ${label}`);
}

async function deleteSubscriptionThroughConfirmation(subscriptionId, label, expectedBeforeCount) {
  await navigate("/subscriptions");
  const card = await assertSubscriptionCardActions(subscriptionId);
  await assertNoNumericSubscriptionActions();
  const before = await readSubscriptions();
  if (before.length !== expectedBeforeCount) {
    throw new Error(`${label}删除前记录数 expected=${expectedBeforeCount} actual=${before.length}`);
  }

  await navigate(card.deleteHref);
  const deleteConfirmation = await evaluate(`(() => ({
    pathname: location.pathname,
    title: document.body.textContent.includes('确认删除这条监控'),
    csrfToken: Boolean(document.querySelector('form input[name="csrf_token"][type="hidden"]')?.value),
    explicitConfirmation: document.querySelector('input[name="confirm_delete"]')?.value === 'yes',
  }))()`);
  if (
    deleteConfirmation.pathname !== `/subscription/${subscriptionId}/delete`
    || !deleteConfirmation.title
    || !deleteConfirmation.csrfToken
    || !deleteConfirmation.explicitConfirmation
  ) {
    throw new Error(`${label}删除确认页契约失败: ${JSON.stringify(deleteConfirmation)}`);
  }
  assertDeepEqual(await readSubscriptions(), before, `${label}删除GET产生副作用`);

  await navigate("/subscriptions");
  const afterGetCount = await evaluate("document.querySelectorAll('.card').length");
  if (afterGetCount !== expectedBeforeCount) {
    throw new Error(`${label}删除GET页面计数错误: before=${expectedBeforeCount} after=${afterGetCount}`);
  }

  await navigate(card.deleteHref);
  const previousLoadGeneration = await clickSelector('form button[type="submit"]');
  await reattachPage(`${label}删除POST完成`, "/subscriptions", previousLoadGeneration);
  const after = await waitForSubscriptions(
    subscriptions => subscriptions.length === expectedBeforeCount - 1,
    `${label}删除落盘`,
  );
  if (await evaluate("location.pathname") !== "/subscriptions") {
    throw new Error(`${label}删除POST未返回/subscriptions`);
  }
  const afterPostCount = await evaluate("document.querySelectorAll('.card').length");
  if (afterPostCount !== expectedBeforeCount - 1) {
    throw new Error(`${label}删除POST页面计数错误: before=${expectedBeforeCount} after=${afterPostCount}`);
  }
  console.log(`[UI smoke] 服务端删除确认=PASS ${label}；GET零写入；POST含CSRF+confirm_delete=yes后删除`);
  return after;
}
function checkboxSelector(name, value) {
  return `input[name="${name}"][value="${value}"]`;
}

async function checkedValues(name) {
  return evaluate(`Array.from(document.querySelectorAll(${JSON.stringify(`input[name="${name}"]`)})).filter(element => element.checked).map(element => element.value)`);
}

async function setCheckboxValuesByClick(name, values) {
  const desired = new Set(values);
  const states = await evaluate(`Array.from(document.querySelectorAll(${JSON.stringify(`input[name="${name}"]`)})).map(element => ({value: element.value, checked: element.checked}))`);
  if (!states.length) throw new Error(`复选组不存在: ${name}`);
  for (const state of states) {
    if (state.checked !== desired.has(state.value)) {
      await clickSelector(checkboxSelector(name, state.value));
    }
  }
  const selected = await checkedValues(name);
  const expected = states.map(state => state.value).filter(value => desired.has(value));
  if (JSON.stringify(selected) !== JSON.stringify(expected)) {
    throw new Error(`复选组双选失败: ${name} expected=${JSON.stringify(expected)} actual=${JSON.stringify(selected)}`);
  }
  return selected;
}

async function assertPersistedCheckboxValues(name, values, label) {
  const actual = await checkedValues(name);
  if (JSON.stringify(actual) !== JSON.stringify(values)) {
    throw new Error(`${label}回读失败: ${name} expected=${JSON.stringify(values)} actual=${JSON.stringify(actual)}`);
  }
}


async function chooseNotificationMethod(value) {
  const targetIndex = await evaluate(`(() => {
    const element = document.querySelector('[name="notification_method"]');
    return [...element.options].findIndex(option => option.value === ${JSON.stringify(value)});
  })()`);
  if (targetIndex < 0) throw new Error(`通知方式不存在: ${value}`);
  await clickSelector('[name="notification_method"]');
  await pressKey("Home", "Home", 36);
  for (let index = 0; index < targetIndex; index += 1) {
    await pressKey("ArrowDown", "ArrowDown", 40);
  }
  await pressKey("Enter", "Enter", 13);
  await waitFor(
    `document.querySelector('[name="notification_method"]').value === ${JSON.stringify(value)}`,
    `通知方式切换为${value}`,
  );
  return evaluate(`(() => {
    const wrapper = document.querySelector('[data-visibility-contract="notification-email"]');
    const input = document.querySelector('[name="notification_email"]');
    const style = getComputedStyle(wrapper);
    const rect = input.getBoundingClientRect();
    const visible = !wrapper.hidden && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    if (visible) input.focus();
    return {method: ${JSON.stringify(value)}, hidden: wrapper.hidden, visible, focusable: visible && document.activeElement === input};
  })()`);
}

async function chooseSelectValue(name, value) {
  const targetIndex = await evaluate(`(() => {
    const element = document.querySelector('[name="${name}"]');
    return [...element.options].findIndex(option => option.value === ${JSON.stringify(value)});
  })()`);
  if (targetIndex < 0) throw new Error(`选项不存在: ${name}=${value}`);
  await clickSelector(`[name="${name}"]`);
  await pressKey("Home", "Home", 36);
  for (let index = 0; index < targetIndex; index += 1) {
    await pressKey("ArrowDown", "ArrowDown", 40);
  }
  await pressKey("Enter", "Enter", 13);
  await waitFor(
    `document.querySelector('[name="${name}"]').value === ${JSON.stringify(value)}`,
    `${name}切换为${value}`,
  );
}

async function transferDetailsState() {
  return evaluate(`(() => {
    const wrapper = document.querySelector('[data-visibility-contract="transfer-details"]');
    if (!wrapper) throw new Error('missing transfer-details');
    const controls = [...wrapper.querySelectorAll('input,select,textarea')];
    const style = getComputedStyle(wrapper);
    const rect = wrapper.getBoundingClientRect();
    return {
      hidden: wrapper.hidden,
      visible: !wrapper.hidden && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0,
      controlCount: controls.length,
      allDisabled: controls.length > 0 && controls.every(control => control.disabled),
      noneDisabled: controls.length > 0 && controls.every(control => !control.disabled),
    };
  })()`);
}

async function mixedCabinState() {
  return evaluate(`(() => {
    const wrapper = document.querySelector('[data-visibility-contract="mixed-cabin"]');
    if (!wrapper) throw new Error('missing mixed-cabin');
    const controls = [...wrapper.querySelectorAll('input[name="cabin_business_types"]')];
    const style = getComputedStyle(wrapper);
    const counts = Object.fromEntries([...wrapper.querySelectorAll('[data-cabin-type-count]')].map(element => [element.dataset.cabinTypeCount, element.textContent.trim()]));
    const rect = wrapper.getBoundingClientRect();
    const scope = name => {
      const items = [...document.querySelectorAll('[name="' + name + '"]')];
      const selected = items.find(item => item.type !== 'radio' || item.checked);
      return selected?.value || '';
    };
    return {
      hidden: wrapper.hidden,
      visible: !wrapper.hidden && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0,
      controlCount: controls.length,
      allDisabled: controls.length > 0 && controls.every(control => control.disabled),
      noneDisabled: controls.length > 0 && controls.every(control => !control.disabled),
      status: document.querySelector('[data-cabin-allocation-status]')?.textContent.trim() || '',
      counts,
      selectedTypes: controls.filter(control => control.checked).map(control => control.value),
      budgetScope: scope('budget_scope'),
      maxBudgetScope: scope('max_budget_scope'),
      targetPriceScope: scope('target_price_scope'),
    };
  })()`);
}

async function setBooleanCheckboxByClick(name, checked) {
  const current = await evaluate(`document.querySelector('[name="${name}"]').checked`);
  if (current !== checked) await clickSelector(`[name="${name}"]`);
  await waitFor(`document.querySelector('[name="${name}"]').checked === ${checked}`, `${name}=${checked}`);
}

async function captureFailureArtifacts(error) {
  if (!artifactDir) return;
  await mkdir(artifactDir, {recursive: true});
  const captureErrors = [];
  let html;
  try {
    html = await evaluate("document.documentElement.outerHTML");
  } catch (captureError) {
    captureErrors.push(`html: ${captureError}`);
    html = `<!doctype html><meta charset="utf-8"><title>UI smoke failure</title><pre>${String(error?.stack || error)}</pre>`;
  }
  await writeFile(join(artifactDir, "failure.html"), html, "utf8");

  try {
    await command("Page.enable");
    const screenshot = await command("Page.captureScreenshot", {
      format: "png",
      fromSurface: true,
      captureBeyondViewport: true,
    });
    if (!screenshot.data) throw new Error("CDP 未返回截图数据");
    await writeFile(join(artifactDir, "failure.png"), Buffer.from(screenshot.data, "base64"));
  } catch (captureError) {
    captureErrors.push(`screenshot: ${captureError}`);
  }

  await writeFile(
    join(artifactDir, "browser-console.json"),
    JSON.stringify({
      failure: {
        name: error?.name || "Error",
        message: String(error?.message || error),
        stack: String(error?.stack || ""),
      },
      browser_errors: browserErrors,
      capture_errors: captureErrors,
    }, null, 2) + "\n",
    "utf8",
  );
}

async function runSmoke() {
await command("Runtime.enable");
await command("Page.enable");
await command("Log.enable");
await command("Network.enable");
await command("Page.bringToFront");
await command("Page.navigate", {url: baseUrl});
await waitFor("document.readyState === 'complete'", "快速页加载");

const quickContract = await evaluate(`(() => {
  const form = document.querySelector('form[data-page-mode="quick"]');
  const visible = element => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return !element.disabled && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const required = [...form.querySelectorAll('[required]')];
  const multiGroups = [...form.querySelectorAll('[data-multi-checkbox-group]')].filter(visible);
  const controls = [...form.querySelectorAll('input[name]:not([type="hidden"]):not([type="submit"]),select[name],textarea[name]')]
    .filter(visible)
    .filter(control => !control.closest('[data-multi-checkbox-group]'));
  return {
    page: document.body.dataset.pageMode,
    form: Boolean(form),
    requiredCount: required.length,
    allRequiredVisible: required.every(visible),
    visibleControlCount: controls.length + multiGroups.length,
    visibleControlNames: [...controls.map(control => control.name), ...multiGroups.map(group => group.dataset.multiCheckboxGroup)],
    csrfToken: Boolean(form.querySelector('input[name="csrf_token"][type="hidden"]')?.value),
  };
})()`);
if (quickContract.page !== "quick" || !quickContract.form || !quickContract.allRequiredVisible || quickContract.visibleControlCount > 12 || !quickContract.csrfToken) {
  throw new Error(`快速页契约失败: ${JSON.stringify(quickContract)}`);
}
console.log(`[UI smoke] 页1必填=${quickContract.requiredCount} 可见可交互=True 可见控件=${quickContract.visibleControlCount}`);
console.log("[UI smoke] 页1 CSRF隐藏字段=PASS");

await evaluate(`document.querySelector('[data-mode-link="full"]').click(); true`);
await waitFor("location.pathname === '/settings'", "页1进入完整设置");
await evaluate(`document.querySelector('[data-mode-link="quick"]').click(); true`);
await waitFor("location.pathname === '/'", "页2返回快速创建");
console.log("[UI smoke] 双页互链=PASS 页1→页2→页1");

const depart = new Date();
depart.setDate(depart.getDate() + 21);
const returned = new Date(depart);
returned.setDate(returned.getDate() + 3);
const localDate = date => [
  date.getFullYear(),
  String(date.getMonth() + 1).padStart(2, "0"),
  String(date.getDate()).padStart(2, "0"),
].join("-");
const priceHintStorageBefore = await priceHintStorageState();
clearPriceHintTrace();
await evaluate(`(() => {
  const set = (name, value) => {
    const element = document.querySelector('[name="' + name + '"]');
    if (!element) throw new Error('missing ' + name);
    element.value = value;
    element.dispatchEvent(new Event('input', {bubbles:true}));
    element.dispatchEvent(new Event('change', {bubbles:true}));
  };
  set('origin_select', '上海');
  set('destination', '北京');
  set('depart_date', '${localDate(depart)}');
  set('return_date', '${localDate(returned)}');
  set('round_trip', 'true');
  set('passenger_count', '1');
  return true;
})()`);
await setCheckboxValuesByClick("travel_scenario", ["tourism", "family"]);
console.log("[UI smoke] 页1多选=PASS 场景2项");
const priceHintResponse = await waitForPriceHintResponse("PVG", "PEK");
const priceHintUrl = new URL(priceHintResponse.url);
const priceHintPayload = priceHintResponse.body;
if (
  priceHintResponse.status !== 200
  || priceHintUrl.pathname !== "/price_hint"
  || priceHintUrl.searchParams.size !== 2
  || priceHintUrl.searchParams.get("origin") !== "PVG"
  || priceHintUrl.searchParams.get("dest") !== "PEK"
  || priceHintPayload.has_data !== false
  || priceHintPayload.scope !== "oneway"
  || priceHintPayload.route_type !== "domestic"
  || priceHintPayload.route_type_label !== "国内"
) {
  throw new Error(`PRICE_HINT_RESPONSE_CONTRACT_MISMATCH: ${JSON.stringify(priceHintResponse)}`);
}
await waitFor("document.querySelector('[data-route-type-badge=\"true\"]').dataset.routeType === 'domestic' && document.querySelector('[data-route-type-label]').textContent.trim() === '国内'", "航线类型自动识别");
console.log("[UI smoke] 航线类型徽章=PASS 上海→北京识别为国内");
const priceHintDom = await evaluate(`(() => ({
  text: document.getElementById('price-hint')?.textContent.trim() || '',
  hiddenRouteType: document.querySelector('[name="route_type"]')?.value || '',
}))()`);
if (priceHintDom.text !== "暂无历史价格参考" || priceHintDom.hiddenRouteType !== "domestic") {
  throw new Error(`PRICE_HINT_FALLBACK_TEXT_NOT_ASSERTED: ${JSON.stringify(priceHintDom)}`);
}
assertDeepEqual(
  await priceHintStorageState(),
  priceHintStorageBefore,
  "/price_hint阶段临时subscriptions/observations/prices零写入",
);
console.log("[UI smoke] /price_hint请求与无数据回退=PASS URL=/price_hint?origin=PVG&dest=PEK status=200 has_data=false scope=oneway hidden_route_type=domestic");
await evaluate(`document.querySelector('form[data-page-mode="quick"]').requestSubmit(); true`);
await waitFor("location.pathname === '/success'", "快速页提交确认", 15000);
const quickId = await captureSuccessSubscriptionId("快速页订阅");
console.log("[UI smoke] 页1提交=PASS 已抵达/success");
console.log("[UI smoke] 页1 success UUID=PASS");

const quickConfirmation = await evaluate(`(() => {
  const text = document.body.textContent;
  return text.includes('旅游 + 家庭/亲子');
})()`);
if (!quickConfirmation) throw new Error('页1确认页未完整回读场景');
await openSubscriptionEditorFromCard(quickId);
await assertPersistedCheckboxValues(
  "travel_scenario",
  ["tourism", "family"],
  "页1场景",
);
await navigate("/settings");
const fullContract = await evaluate(`(() => {
  const ids = ['section-where','section-when','section-who','section-budget','section-flight-preferences','section-notifications'];
  const groupIds = ['group-business-travel','group-feasibility'];
  const visible = element => {
    if (!element) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const controlsByName = new Map();
  for (const control of document.querySelectorAll('input[name],select[name],textarea[name]')) {
    if (!controlsByName.has(control.name)) controlsByName.set(control.name, []);
    controlsByName.get(control.name).push(control);
  }
  const duplicates = [...controlsByName.entries()]
    .filter(([, controls]) => {
      if (controls.length === 1) return false;
      const repeatableChoiceGroup = controls.every(control => control.tagName === 'INPUT' && ['radio', 'checkbox'].includes(control.type));
      const uniqueValues = new Set(controls.map(control => control.value)).size === controls.length;
      return !repeatableChoiceGroup || !uniqueValues;
    })
    .map(([name]) => name);
  return {
    page: document.body.dataset.pageMode,
    sectionsVisible: ids.every(id => visible(document.getElementById(id))),
    sectionCount: ids.length,
    anchorCount: ids.filter(id => document.querySelector('a[href="#' + id + '"]')).length,
    groupCount: groupIds.filter(id => document.getElementById(id)?.tagName === 'DETAILS').length,
    groupAnchorCount: groupIds.filter(id => document.querySelector('a[href="#' + id + '"]')).length,
    groupsClosed: groupIds.every(id => !document.getElementById(id)?.hasAttribute('open')),
    businessInitiallyHidden: document.getElementById('group-business-travel')?.hidden || false,
    duplicates,
    buildMarker: document.querySelector('[data-build-marker="true"]')?.textContent.trim() || '',
    csrfToken: Boolean(document.querySelector('form[data-page-mode="full"] input[name="csrf_token"][type="hidden"]')?.value),
  };
})()`);
if (fullContract.page !== "full" || !fullContract.sectionsVisible || fullContract.anchorCount !== 6 || fullContract.groupCount !== 2 || fullContract.groupAnchorCount !== 2 || !fullContract.groupsClosed || !fullContract.businessInitiallyHidden || fullContract.duplicates.length || !fullContract.buildMarker || !fullContract.csrfToken) {
  throw new Error(`完整页契约失败: ${JSON.stringify(fullContract)}`);
}
for (const id of ["section-where","section-when","section-who","section-budget","section-flight-preferences","section-notifications"]) {
  await evaluate(`document.querySelector('a[href="#${id}"]').click(); true`);
  await waitFor(`location.hash === '#${id}' && Boolean(document.getElementById('${id}'))`, `锚点${id}`);
}
console.log(`[UI smoke] 页2六节=${fullContract.sectionCount} 全可见=True 目录锚点=${fullContract.anchorCount} 次级组锚点=${fullContract.groupAnchorCount} 重复name=0`);
console.log(`[UI smoke] 版本信标=${fullContract.buildMarker}`);
console.log("[UI smoke] 页2 CSRF隐藏字段=PASS");

async function businessGroupVisible() {
  return evaluate(`(() => {
  const group = document.getElementById('group-business-travel');
  const style = getComputedStyle(group);
  const rect = group.getBoundingClientRect();
  return !group.hidden && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
})()`);
}
await setCheckboxValuesByClick("travel_scenario", ["business"]);
const shown = await businessGroupVisible();
await setCheckboxValuesByClick("travel_scenario", ["tourism"]);
const hidden = !(await businessGroupVisible());
await setCheckboxValuesByClick("travel_scenario", ["business", "tourism"]);
const finalVisible = await businessGroupVisible();
const businessVisibility = {shown, hidden, finalVisible};
if (!businessVisibility.shown || !businessVisibility.hidden || !businessVisibility.finalVisible) {
  throw new Error(`商务组显隐契约失败: ${JSON.stringify(businessVisibility)}`);
}
console.log("[UI smoke] 商务场景显隐=PASS 勾选商务→显示；取消商务→隐藏；DOM常驻");

const initialTransfer = await transferDetailsState();
await chooseSelectValue("transfer_policy", "direct_only");
const directTransfer = await transferDetailsState();
await chooseSelectValue("transfer_policy", "price_first");
const allowedTransfer = await transferDetailsState();
if (
  !initialTransfer.visible || !initialTransfer.noneDisabled
  || !directTransfer.hidden || !directTransfer.allDisabled
  || !allowedTransfer.visible || !allowedTransfer.noneDisabled
) {
  throw new Error(`中转族显隐契约失败: ${JSON.stringify({initialTransfer, directTransfer, allowedTransfer})}`);
}
console.log("[UI smoke] 中转族显隐=PASS 必须直飞→隐藏禁用；允许中转→显示启用；DOM常驻");

const initialMixedCabin = await mixedCabinState();
await chooseSelectValue("cabin_arrangement", "mixed");
await evaluate(`(() => {
  const set = (name, value) => {
    const element = document.querySelector('[name="' + name + '"]');
    if (!element) throw new Error('missing ' + name);
    element.value = value;
    element.dispatchEvent(new Event('input', {bubbles:true}));
    element.dispatchEvent(new Event('change', {bubbles:true}));
  };
  set('adult_count', '2');
  set('child_count', '1');
  set('elderly_count', '2');
  set('infant_count', '0');
  return true;
})()`);
await setCheckboxValuesByClick("cabin_business_types", ["adult"]);
await waitFor("document.querySelector('[data-cabin-allocation-status]').textContent.includes('商务:成人×2 / 经济:儿童×1+老人×2')", "混舱类型分配");
const filledMixedCabin = await mixedCabinState();
if (!initialMixedCabin.hidden || !initialMixedCabin.allDisabled || !filledMixedCabin.visible || !filledMixedCabin.noneDisabled ||
    filledMixedCabin.controlCount !== 4 || JSON.stringify(filledMixedCabin.selectedTypes) !== JSON.stringify(['adult']) ||
    filledMixedCabin.counts.adult !== '×2' || filledMixedCabin.counts.child !== '×1' || filledMixedCabin.counts.elderly !== '×2' ||
    filledMixedCabin.budgetScope !== 'total' ||
    filledMixedCabin.maxBudgetScope !== 'total' || filledMixedCabin.targetPriceScope !== 'total') {
  throw new Error(`混舱显隐或预算口径错误: initial=${JSON.stringify(initialMixedCabin)} filled=${JSON.stringify(filledMixedCabin)}`);
}
await evaluate(`(() => {
  const child = document.querySelector('[name="child_count"]');
  child.value = '2';
  child.dispatchEvent(new Event('input', {bubbles:true}));
  child.dispatchEvent(new Event('change', {bubbles:true}));
  return true;
})()`);
await waitFor("document.querySelector('[data-cabin-type-count=\"child\"]').textContent.trim() === '×2'", "混舱人数标签随动到2");
await evaluate(`(() => {
  const child = document.querySelector('[name="child_count"]');
  child.value = '1';
  child.dispatchEvent(new Event('input', {bubbles:true}));
  child.dispatchEvent(new Event('change', {bubbles:true}));
  return true;
})()`);
await waitFor("document.querySelector('[data-cabin-type-count=\"child\"]').textContent.trim() === '×1'", "混舱人数标签恢复到1");
console.log("[UI smoke] 混舱显隐=PASS 默认隐藏禁用→混舱显示启用；预算三项强制全员总价");
console.log("[UI smoke] 混舱类型勾选=PASS 成人×2商务；儿童×1+老人×2经济；人数随动");

await chooseSelectValue("short_transfer_limit", "total_18");
await setBooleanCheckboxByClick("accept_overnight_transfer", true);
await setBooleanCheckboxByClick("accept_self_transfer", true);
const transferInput = await evaluate(`(() => ({
  policy: document.querySelector('[name="transfer_policy"]').value,
  limit: document.querySelector('[name="short_transfer_limit"]').value,
  overnight: document.querySelector('[name="accept_overnight_transfer"]').checked,
  selfTransfer: document.querySelector('[name="accept_self_transfer"]').checked,
}))()`);
if (transferInput.policy !== "price_first" || transferInput.limit !== "total_18" || !transferInput.overnight || !transferInput.selfTransfer) {
  throw new Error(`中转细节填写失败: ${JSON.stringify(transferInput)}`);
}

await clickSelector('#group-business-travel > summary');
await waitFor("document.getElementById('group-business-travel').hasAttribute('open')", '商务出行展开');
const businessBranch = await evaluate(`(() => {
  const group = document.getElementById('group-business-travel');
  const names = ['meeting_importance','trip_natures','user_level','reimburse_per_person','same_day_round_trip','business_start','business_end'];
  return names.every(name => group?.querySelector('[name="' + name + '"]'));
})()`);
if (!businessBranch) throw new Error('商务出行分支缺少专属控件');
await clickSelector('#group-business-travel > summary');
await waitFor("!document.getElementById('group-business-travel').hasAttribute('open')", '商务出行闭合');
console.log("[UI smoke] 商务场景分支=PASS 原生details开合+商务专属控件归组");

await clickSelector('#group-feasibility > summary');
await waitFor("document.getElementById('group-feasibility').hasAttribute('open')", '可行性参数展开');
await clickSelector('#group-feasibility > summary');
await waitFor("!document.getElementById('group-feasibility').hasAttribute('open')", '可行性参数闭合');
console.log("[UI smoke] 原生details开合=PASS 可行性参数");

await clickSelector('[data-time-window-group="custom"] > summary');
await waitFor("document.querySelector('[data-time-window-group=\"custom\"]').hasAttribute('open')", '自定义时间窗展开');
await clickSelector('[data-time-window-group="directional"] > summary');
await waitFor("document.querySelector('[data-time-window-group=\"directional\"]').hasAttribute('open')", '分方向时间窗展开');
const timeWindowControls = await evaluate(`(() => {
  const directional = document.querySelector('[data-time-window-group="directional"]');
  const names = ['outbound_departure_window_start','outbound_departure_window_end','return_departure_window_start','return_departure_window_end'];
  return names.every(name => directional?.querySelector('[name="' + name + '"]'));
})()`);
if (!timeWindowControls) throw new Error('分方向时间窗缺少规范控件');
console.log("[UI smoke] 分层时间窗=PASS 分方向完整时间窗>通用完整时间窗>时段偏好");

await command("Input.dispatchKeyEvent", {type: "keyDown", key: "f", code: "KeyF", modifiers: 2, windowsVirtualKeyCode: 70, nativeVirtualKeyCode: 70});
await command("Input.dispatchKeyEvent", {type: "keyUp", key: "f", code: "KeyF", modifiers: 2, windowsVirtualKeyCode: 70, nativeVirtualKeyCode: 70});
const findProbe = await evaluate(`(() => {
  const group = document.getElementById('group-business-travel');
  const text = '需要增值税专票';
  const textPresent = group?.textContent.includes(text) || false;
  const matched = window.find(text, false, false, true, false, false, false);
  return {textPresent, matched, initiallyClosed: !group?.hasAttribute('open')};
})()`);
await pressKey("Escape", "Escape", 27);
if (!findProbe.textPresent || !findProbe.matched || !findProbe.initiallyClosed) {
  throw new Error(`闭合details内文不可查找: ${JSON.stringify(findProbe)}`);
}
console.log("[UI smoke] details内文查找可达=PASS Ctrl+F键序列+浏览器内核find 需要增值税专票");

const pushplusVisibility = await chooseNotificationMethod("pushplus");
const emailVisibility = await chooseNotificationMethod("email");
const bothVisibility = await chooseNotificationMethod("both");
if (!pushplusVisibility.hidden || pushplusVisibility.visible) {
  throw new Error(`微信渠道应隐藏邮箱: ${JSON.stringify(pushplusVisibility)}`);
}
for (const state of [emailVisibility, bothVisibility]) {
  if (state.hidden || !state.visible || !state.focusable) {
    throw new Error(`邮件渠道应显示且可聚焦邮箱: ${JSON.stringify(state)}`);
  }
}
console.log("[UI smoke] 渠道三态转换=PASS 微信→邮箱隐藏；邮件→邮箱可见可聚焦；两者→邮箱可见可聚焦");

await setCheckboxValuesByClick("travel_scenario", ["business", "tourism"]);
await chooseSelectValue("transfer_policy", "reasonable");
console.log("[UI smoke] 页2多选=PASS 场景2项");
await evaluate(`(() => {
  const set = (name, value) => {
    const element = document.querySelector('[name="' + name + '"]');
    if (!element) throw new Error('missing ' + name);
    element.value = value;
    element.dispatchEvent(new Event('input', {bubbles:true}));
    element.dispatchEvent(new Event('change', {bubbles:true}));
  };
  const check = (name, checked) => {
    const element = document.querySelector('[name="' + name + '"]');
    if (!element) throw new Error('missing ' + name);
    element.checked = checked;
    element.dispatchEvent(new Event('change', {bubbles:true}));
  };
  set('origin_select', '上海');
  set('destination', '北京');
  set('depart_date', '${localDate(depart)}');
  set('round_trip', 'true');
  set('return_date', '${localDate(depart)}');
  set('adult_count', '2');
  set('child_count', '1');
  set('elderly_count', '2');
  set('infant_count', '0');
  set('business_start', '10:30');
  set('business_end', '17:00');
  set('buffer_hours', '1.5');
  set('transport_mode', 'taxi');
  set('user_transport_min', '25');
  set('redundancy_min', '15');
  set('outbound_set_off', '06:30');
  set('shared_departure_window_start', '08:00');
  set('shared_departure_window_end', '12:00');
  set('outbound_departure_window_start', '06:30');
  set('outbound_departure_window_end', '08:30');
  set('return_departure_window_start', '18:00');
  set('return_departure_window_end', '21:00');

  set('notification_email', 'ux31@example.com');
  check('same_day_round_trip', true);
  return true;
})()`);
await evaluate(`document.querySelector('form[data-page-mode="full"]').requestSubmit(); true`);
await waitFor("location.pathname === '/success'", "完整页提交确认", 15000);
const fullId = await captureSuccessSubscriptionId("完整页订阅");
const confirmation = await evaluate(`(() => {
  const text = document.body.textContent;
  return {
    email: text.includes('ux31@example.com'),
    meetingStart: text.includes('10:30'),
    meetingEnd: text.includes('17:00'),
    outboundWindow: text.includes('06:30-08:30'),
    returnWindow: text.includes('18:00-21:00'),
    scenarios: text.includes('商务/会议 + 旅游'),
    companionConstraints: text.includes('需要尽量直飞 + 不接受红眼/凌晨到达'),
    companionText: document.querySelector('[data-confirmed-companion-constraints]')?.textContent.trim() || '',
    transfer: text.includes('中转设置：合理中转 · 总时长不超18小时'),
    transferText: document.querySelector('[data-confirmed-transfer]')?.textContent.trim() || '',
    mixedCabin: text.includes('商务:成人×2 / 经济:儿童×1+老人×2'),
  };
})()`);
if (!confirmation.email || !confirmation.meetingStart || !confirmation.meetingEnd || !confirmation.outboundWindow || !confirmation.returnWindow || !confirmation.scenarios || !confirmation.companionConstraints || !confirmation.transfer || !confirmation.mixedCabin) {
  throw new Error(`完整页回读失败: ${JSON.stringify(confirmation)}`);
}
console.log("[UI smoke] 页2邮箱提交=PASS value=ux31@example.com");
console.log("[UI smoke] 页2当天往返会议=PASS 10:30-17:00");
console.log("[UI smoke] 分方向时间窗回读=PASS 去程06:30-08:30 返程18:00-21:00");
console.log("[UI smoke] 中转细节回读=PASS 价格优先态验证过夜+自行中转；提交态合理中转+总时长18小时");
console.log("[UI smoke] 同行派生=PASS 飞行偏好→旧schema");
console.log("[UI smoke] 混舱分配回读=PASS 商务:成人×2 / 经济:儿童×1+老人×2");
console.log("[UI smoke] 页2 success UUID=PASS");

await openSubscriptionEditorFromCard(fullId);
const editGroups = await evaluate(`(() => ({
  business: document.getElementById('group-business-travel')?.hasAttribute('open') || false,
  feasibility: document.getElementById('group-feasibility')?.hasAttribute('open') || false,
  customTime: document.querySelector('[data-time-window-group="custom"]')?.hasAttribute('open') || false,
  directionalTime: document.querySelector('[data-time-window-group="directional"]')?.hasAttribute('open') || false,
  transferDetails: document.querySelector('[data-visibility-contract="transfer-details"]')?.hidden === false,
  mixedCabin: document.querySelector('[data-visibility-contract="mixed-cabin"]')?.hidden === false,
}))()`);
if (!editGroups.business || !editGroups.feasibility || !editGroups.customTime || !editGroups.directionalTime || !editGroups.transferDetails || !editGroups.mixedCabin) {
  throw new Error(`编辑态次级组未自动展开: ${JSON.stringify(editGroups)}`);
}
console.log("[UI smoke] 编辑态details自动展开=PASS 商务出行+可行性参数+分层时间窗+混舱分配");
await assertPersistedCheckboxValues(
  "travel_scenario",
  ["business", "tourism"],
  "页2场景",
);
await assertPersistedCheckboxValues("cabin_business_types", ["adult"], "混舱商务类型");
const derivedCompanion = await evaluate(`(() => ({
  legacyControlCount: document.querySelectorAll('[name="companion_constraints"]').length,
  seed: (document.querySelector('[name="companion_constraints_seed"]')?.value || '').split(',').filter(Boolean),
}))()`);
if (
  derivedCompanion.legacyControlCount !== 0
  || JSON.stringify(derivedCompanion.seed) !== JSON.stringify(['direct_preferred', 'no_redeye'])
) {
  throw new Error(`同行派生回读失败: ${JSON.stringify(derivedCompanion)}`);
}
console.log("[UI smoke] 多选POST回读=PASS 场景getlist双值");


await navigate("/settings");
await chooseSelectValue("transfer_policy", "direct_only");
const hiddenBeforeSubmit = await transferDetailsState();
if (!hiddenBeforeSubmit.hidden || !hiddenBeforeSubmit.allDisabled) {
  throw new Error(`直飞隐藏态提交前契约失败: ${JSON.stringify(hiddenBeforeSubmit)}`);
}
await chooseSelectValue("notification_method", "pushplus");
await evaluate(`(() => {
  const set = (name, value) => {
    const element = document.querySelector('[name="' + name + '"]');
    if (!element) throw new Error('missing ' + name);
    element.value = value;
    element.dispatchEvent(new Event('input', {bubbles:true}));
    element.dispatchEvent(new Event('change', {bubbles:true}));
  };
  set('origin_select', '上海');
  set('destination', '北京');
  set('depart_date', '${localDate(depart)}');
  set('round_trip', 'true');
  set('return_date', '${localDate(returned)}');
  document.querySelector('form[data-page-mode="full"]').requestSubmit();
  return true;
})()`);
await waitFor("location.pathname === '/success'", "直飞隐藏态提交确认", 15000);
const directId = await captureSuccessSubscriptionId("直飞隐藏态订阅");
const hiddenConfirmation = await evaluate(`(() => {
  const text = document.body.textContent;
  return {
    directOnly: text.includes('中转设置：必须直飞'),
    noStaleDetails: !text.includes('总时长不超18小时') && !text.includes('接受过夜中转') && !text.includes('接受非联程自行中转'),
  };
})()`);
if (!hiddenConfirmation.directOnly || !hiddenConfirmation.noStaleDetails) {
  throw new Error(`中转隐藏提交确认失败: ${JSON.stringify(hiddenConfirmation)}`);
}
console.log("[UI smoke] 直飞隐藏态 success UUID=PASS");
await openSubscriptionEditorFromCard(directId);
const hiddenPersisted = await evaluate(`(() => {
  const wrapper = document.querySelector('[data-visibility-contract="transfer-details"]');
  return {
    policy: document.querySelector('[name="transfer_policy"]').value,
    hidden: wrapper.hidden,
    allDisabled: [...wrapper.querySelectorAll('input,select,textarea')].every(control => control.disabled),
    limit: document.querySelector('[name="short_transfer_limit"]').value,
    overnight: document.querySelector('[name="accept_overnight_transfer"]').checked,
    selfTransfer: document.querySelector('[name="accept_self_transfer"]').checked,
  };
})()`);
if (hiddenPersisted.policy !== 'direct_only' || !hiddenPersisted.hidden || !hiddenPersisted.allDisabled || hiddenPersisted.limit !== 'extra_6' || hiddenPersisted.overnight || hiddenPersisted.selfTransfer) {
  throw new Error(`中转隐藏提交默认回填失败: ${JSON.stringify(hiddenPersisted)}`);
}
console.log("[UI smoke] 中转隐藏提交默认=PASS 直飞态不提交细节；服务端回填extra_6/false/false");

await navigate("/subscriptions");
const createdCardCount = await evaluate("document.querySelectorAll('.card').length");
if (createdCardCount !== 3) {
  throw new Error(`三条fixture页面计数错误: ${createdCardCount}`);
}
for (const subscriptionId of [quickId, fullId, directId]) {
  await assertSubscriptionCardActions(subscriptionId);
}
await assertNoNumericSubscriptionActions();
console.log("[UI smoke] UUID action合同=PASS edit入口/?edit=<uuid>→/settings?edit=<uuid>；toggle/quick-update/delete精确路由；数字index控件=0");

const remainingAfterDirectDelete = await deleteSubscriptionThroughConfirmation(
  directId,
  "直飞隐藏态订阅C",
  3,
);
if (remainingAfterDirectDelete.length !== 2) {
  throw new Error(`删除C后记录数错误: ${remainingAfterDirectDelete.length}`);
}
const aBaseline = structuredClone(
  subscriptionById(remainingAfterDirectDelete, quickId, "A baseline"),
);
const bBaseline = structuredClone(
  subscriptionById(remainingAfterDirectDelete, fullId, "B baseline"),
);
if (remainingAfterDirectDelete.some(item => item?.subscription_id === directId)) {
  throw new Error("删除C后临时JSON仍含directId");
}
if (aBaseline.status !== "active") {
  throw new Error(`A初始status不是active: ${String(aBaseline.status)}`);
}
console.log("[UI smoke] 临时JSON oracle=PASS 删除C后仅余A/B并保存完整基线");

await navigate("/subscriptions");
const initialAState = await assertSubscriptionCardActions(quickId);
if (!initialAState.statusClass.includes("active") || !initialAState.statusText.includes("监控中") || initialAState.toggleText !== "暂停") {
  throw new Error(`A暂停前页面状态错误: ${JSON.stringify(initialAState)}`);
}
const toggleAction = `/subscriptions/${quickId}/toggle`;
const pauseLoadGeneration = await clickFormButton(toggleAction);
await reattachPage("A暂停", "/subscriptions", pauseLoadGeneration);
const pausedSubscriptions = await waitForSubscriptions(
  subscriptions => subscriptionById(subscriptions, quickId, "A paused").status === "paused",
  "A暂停落盘",
);
await waitFor(`(() => {
  const form = [...document.querySelectorAll('form')].find(item => item.getAttribute('action') === ${JSON.stringify(`/subscriptions/${quickId}/toggle`)});
  const card = form?.closest('.card');
  return location.pathname === '/subscriptions'
    && card?.querySelector('.status.paused')?.textContent.includes('已暂停')
    && form?.querySelector('button[type="submit"]')?.textContent.trim() === '恢复';
})()`, "A暂停页面回读");
if (pausedSubscriptions.length !== 2) {
  throw new Error(`A暂停后总数错误: ${pausedSubscriptions.length}`);
}
assertDeepEqual(
  subscriptionById(pausedSubscriptions, quickId, "A paused"),
  withOnlyChange(aBaseline, record => { record.status = "paused"; }),
  "A暂停仅status变化",
);
assertDeepEqual(
  subscriptionById(pausedSubscriptions, fullId, "B during A pause"),
  bBaseline,
  "A暂停时B",
);
console.log("[UI smoke] UUID暂停A=PASS 页面已暂停/恢复按钮；JSON仅status active→paused；B不变");

const pausedAState = await assertSubscriptionCardActions(quickId);
if (!pausedAState.statusClass.includes("paused") || !pausedAState.statusText.includes("已暂停") || pausedAState.toggleText !== "恢复") {
  throw new Error(`A恢复前页面状态错误: ${JSON.stringify(pausedAState)}`);
}
const resumeLoadGeneration = await clickFormButton(toggleAction);
await reattachPage("A恢复", "/subscriptions", resumeLoadGeneration);
const resumedSubscriptions = await waitForSubscriptions(
  subscriptions => subscriptionById(subscriptions, quickId, "A resumed").status === "active",
  "A恢复落盘",
);
await waitFor(`(() => {
  const form = [...document.querySelectorAll('form')].find(item => item.getAttribute('action') === ${JSON.stringify(`/subscriptions/${quickId}/toggle`)});
  const card = form?.closest('.card');
  return location.pathname === '/subscriptions'
    && card?.querySelector('.status.active')?.textContent.includes('监控中')
    && form?.querySelector('button[type="submit"]')?.textContent.trim() === '暂停';
})()`, "A恢复页面回读");
if (resumedSubscriptions.length !== 2) {
  throw new Error(`A恢复后总数错误: ${resumedSubscriptions.length}`);
}
assertDeepEqual(
  subscriptionById(resumedSubscriptions, quickId, "A resumed"),
  aBaseline,
  "A恢复完整基线",
);
assertDeepEqual(
  subscriptionById(resumedSubscriptions, fullId, "B after A resume"),
  bBaseline,
  "A恢复时B",
);
console.log("[UI smoke] UUID恢复A=PASS 页面监控中/暂停按钮；A/B逐字段回归基线");

if (aBaseline.soft_preferences?.airline_policy !== "any") {
  throw new Error(`A quick-update前airline_policy不是any: ${String(aBaseline.soft_preferences?.airline_policy)}`);
}
await navigate(`/success?subscription_id=${quickId}`);
if (await captureSuccessSubscriptionId("A quick-update入口") !== quickId) {
  throw new Error("A quick-update入口subscription_id漂移");
}
const quickUpdateAction = `/subscriptions/${quickId}/quick-update`;
const quickUpdateContract = await evaluate(`(() => {
  const action = ${JSON.stringify(quickUpdateAction)};
  const forms = [...document.querySelectorAll('form')].filter(form => form.getAttribute('action') === action);
  const target = forms.find(form =>
    form.querySelector('[name="field"][value="airline_policy"]')
    && form.querySelector('[name="value"][value="prefer_full_service"]')
  );
  return {
    action: target?.getAttribute('action') || '',
    csrfToken: Boolean(target?.querySelector('[name="csrf_token"]')?.value),
    field: target?.querySelector('[name="field"]')?.value || '',
    value: target?.querySelector('[name="value"]')?.value || '',
  };
})()`);
if (
  quickUpdateContract.action !== quickUpdateAction
  || !quickUpdateContract.csrfToken
  || quickUpdateContract.field !== "airline_policy"
  || quickUpdateContract.value !== "prefer_full_service"
) {
  throw new Error(`A quick-update表单合同失败: ${JSON.stringify(quickUpdateContract)}`);
}
const quickUpdateLoadGeneration = await clickFormButton(quickUpdateAction, {
  field: "airline_policy",
  value: "prefer_full_service",
});
await reattachPage(
  "A quick-update",
  `/success?subscription_id=${quickId}`,
  quickUpdateLoadGeneration,
);
const quickUpdatedSubscriptions = await waitForSubscriptions(
  subscriptions => subscriptionById(subscriptions, quickId, "A quick-updated").soft_preferences?.airline_policy === "prefer_full_service",
  "A quick-update落盘",
);
await waitFor(`location.pathname === '/success' && location.search === ${JSON.stringify(`?subscription_id=${quickId}`)}`, "A quick-update重定向");
const expectedQuickUpdatedA = withOnlyChange(aBaseline, record => {
  record.soft_preferences.airline_policy = "prefer_full_service";
});
if (quickUpdatedSubscriptions.length !== 2) {
  throw new Error(`A quick-update后总数错误: ${quickUpdatedSubscriptions.length}`);
}
assertDeepEqual(
  subscriptionById(quickUpdatedSubscriptions, quickId, "A quick-updated"),
  expectedQuickUpdatedA,
  "A quick-update仅soft_preferences.airline_policy变化",
);
assertDeepEqual(
  subscriptionById(quickUpdatedSubscriptions, fullId, "B after A quick-update"),
  bBaseline,
  "A quick-update时B",
);
console.log("[UI smoke] UUID quick-update A=PASS 真实按钮；总数不变；仅airline_policy any→prefer_full_service；B不变");

const remainingAfterADelete = await deleteSubscriptionThroughConfirmation(
  quickId,
  "快速页订阅A",
  2,
);
if (remainingAfterADelete.length !== 1) {
  throw new Error(`最终记录数错误: ${remainingAfterADelete.length}`);
}
assertDeepEqual(
  subscriptionById(remainingAfterADelete, fullId, "final B"),
  bBaseline,
  "最终唯一记录B",
);
await assertSubscriptionCardActions(fullId);
await assertNoNumericSubscriptionActions();
const finalPageState = await evaluate(`(() => ({
  cardCount: document.querySelectorAll('.card').length,
  hasA: Boolean(document.querySelector('form[action="/subscriptions/${quickId}/toggle"]')),
  hasC: Boolean(document.querySelector('form[action="/subscriptions/${directId}/toggle"]')),
  hasB: Boolean(document.querySelector('form[action="/subscriptions/${fullId}/toggle"]')),
}))()`);
if (finalPageState.cardCount !== 1 || finalPageState.hasA || finalPageState.hasC || !finalPageState.hasB) {
  throw new Error(`最终页面唯一B合同失败: ${JSON.stringify(finalPageState)}`);
}
console.log("[UI smoke] UUID删除A=PASS 最终临时JSON与页面均仅余B且逐字段不变");

await sleep(350);
if (browserErrors.length) throw new Error(`浏览器错误: ${browserErrors.join(' | ')}`);
console.log("[UI smoke] console error=0");
}

try {
  await runSmoke();
} catch (error) {
  try {
    await captureFailureArtifacts(error);
  } catch (artifactError) {
    console.error(`[UI smoke] 失败产物写入失败: ${artifactError}`);
  }
  throw error;
} finally {
  if (ws.readyState === WebSocket.OPEN) ws.close();
}
