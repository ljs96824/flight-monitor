const [baseUrl, cdpPort] = process.argv.slice(2);
if (!baseUrl || !cdpPort) throw new Error("缺少 baseUrl/cdpPort");

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const targets = await fetch(`http://127.0.0.1:${cdpPort}/json/list`).then(r => r.json());
const target = targets.find(item => item.type === "page");
if (!target) throw new Error("CDP 未找到 page target");

const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  ws.addEventListener("open", resolve, {once: true});
  ws.addEventListener("error", reject, {once: true});
});
let nextId = 1;
const pending = new Map();
const browserErrors = [];
ws.addEventListener("message", event => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const {resolve, reject} = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(JSON.stringify(message.error)));
    else resolve(message.result || {});
    return;
  }
  if (message.method === "Runtime.exceptionThrown") {
    browserErrors.push(message.params.exceptionDetails?.text || "Runtime.exceptionThrown");
  }
  if (message.method === "Runtime.consoleAPICalled" && message.params.type === "error") {
    browserErrors.push("console.error");
  }
  if (message.method === "Log.entryAdded" && message.params.entry?.level === "error") {
    browserErrors.push(message.params.entry.text || "Log.error");
  }
});

function command(method, params = {}) {
  const id = nextId++;
  ws.send(JSON.stringify({id, method, params}));
  return new Promise((resolve, reject) => pending.set(id, {resolve, reject}));
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

async function navigate(path) {
  await command("Page.navigate", {url: new URL(path, baseUrl).href});
  await waitFor("document.readyState === 'complete'", `加载${path}`);
}

await command("Runtime.enable");
await command("Page.enable");
await command("Log.enable");
await waitFor("document.readyState === 'complete'", "快速页加载");

const quickContract = await evaluate(`(() => {
  const form = document.querySelector('form[data-page-mode="quick"]');
  const visible = element => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return !element.disabled && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const required = [...form.querySelectorAll('[required]')];
  const controls = [...form.querySelectorAll('input[name]:not([type="hidden"]):not([type="submit"]),select[name],textarea[name]')].filter(visible);
  return {
    page: document.body.dataset.pageMode,
    form: Boolean(form),
    requiredCount: required.length,
    allRequiredVisible: required.every(visible),
    visibleControlCount: controls.length,
  };
})()`);
if (quickContract.page !== "quick" || !quickContract.form || !quickContract.allRequiredVisible || quickContract.visibleControlCount > 12) {
  throw new Error(`快速页契约失败: ${JSON.stringify(quickContract)}`);
}
console.log(`[UI smoke] 页1必填=${quickContract.requiredCount} 可见可交互=True 可见控件=${quickContract.visibleControlCount}`);

await evaluate(`document.querySelector('[data-mode-link="full"]').click(); true`);
await waitFor("location.pathname === '/settings'", "页1进入完整设置");
await evaluate(`document.querySelector('[data-mode-link="quick"]').click(); true`);
await waitFor("location.pathname === '/'", "页2返回快速创建");
console.log("[UI smoke] 双页互链=PASS 页1→页2→页1");

const depart = new Date();
depart.setDate(depart.getDate() + 21);
const returned = new Date(depart);
returned.setDate(returned.getDate() + 3);
const iso = date => date.toISOString().slice(0, 10);
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
  set('depart_date', '${iso(depart)}');
  set('return_date', '${iso(returned)}');
  set('round_trip', 'true');
  set('passenger_count', '1');
  set('price_strategy', 'auto_judge');
  const scenario = document.querySelector('[name="travel_scenario"]');
  [...scenario.options].forEach(option => option.selected = option.value === 'personal');
  scenario.dispatchEvent(new Event('change', {bubbles:true}));
  document.querySelector('form[data-page-mode="quick"]').requestSubmit();
  return true;
})()`);
await waitFor("location.pathname === '/success'", "快速页提交确认", 15000);
console.log("[UI smoke] 页1提交=PASS 已抵达/success");

await navigate("/settings");
const fullContract = await evaluate(`(() => {
  const ids = ['section-where','section-when','section-who','section-budget','section-flight-preferences','section-notifications'];
  const visible = element => {
    if (!element) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const names = [...document.querySelectorAll('input[name],select[name],textarea[name]')].map(x => x.name);
  const duplicates = names.filter((name, index) => names.indexOf(name) !== index);
  return {
    page: document.body.dataset.pageMode,
    sectionsVisible: ids.every(id => visible(document.getElementById(id))),
    sectionCount: ids.length,
    anchorCount: ids.filter(id => document.querySelector('a[href="#' + id + '"]')).length,
    duplicates: [...new Set(duplicates)],
  };
})()`);
if (fullContract.page !== "full" || !fullContract.sectionsVisible || fullContract.anchorCount !== 6 || fullContract.duplicates.length) {
  throw new Error(`完整页契约失败: ${JSON.stringify(fullContract)}`);
}
for (const id of ["section-where","section-when","section-who","section-budget","section-flight-preferences","section-notifications"]) {
  await evaluate(`document.querySelector('a[href="#${id}"]').click(); true`);
  await waitFor(`location.hash === '#${id}' && Boolean(document.getElementById('${id}'))`, `锚点${id}`);
}
console.log(`[UI smoke] 页2六节=${fullContract.sectionCount} 全可见=True 目录锚点=${fullContract.anchorCount} 重复name=0`);

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
  set('depart_date', '${iso(depart)}');
  set('round_trip', 'true');
  set('return_date', '${iso(depart)}');
  set('adult_count', '1');
  set('child_count', '0');
  set('elderly_count', '0');
  set('infant_count', '0');
  set('business_start', '10:30');
  set('business_end', '17:00');
  set('buffer_hours', '1.5');
  set('transport_mode', 'taxi');
  set('user_transport_min', '25');
  set('redundancy_min', '15');
  set('notification_method', 'email');
  set('notification_email', 'ux31@example.com');
  const scenario = document.querySelector('[name="travel_scenario"]');
  [...scenario.options].forEach(option => option.selected = option.value === 'business');
  scenario.dispatchEvent(new Event('change', {bubbles:true}));
  check('same_day_round_trip', true);
  document.querySelector('form[data-page-mode="full"]').requestSubmit();
  return true;
})()`);
await waitFor("location.pathname === '/success'", "完整页提交确认", 15000);
const confirmation = await evaluate(`(() => {
  const text = document.body.textContent;
  return {
    email: text.includes('ux31@example.com'),
    meetingStart: text.includes('10:30'),
    meetingEnd: text.includes('17:00'),
  };
})()`);
if (!confirmation.email || !confirmation.meetingStart || !confirmation.meetingEnd) {
  throw new Error(`完整页回读失败: ${JSON.stringify(confirmation)}`);
}
console.log("[UI smoke] 页2邮箱提交=PASS value=ux31@example.com");
console.log("[UI smoke] 页2当天往返会议=PASS 10:30-17:00");

await sleep(350);
if (browserErrors.length) throw new Error(`浏览器错误: ${browserErrors.join(' | ')}`);
console.log("[UI smoke] console error=0");
ws.close();