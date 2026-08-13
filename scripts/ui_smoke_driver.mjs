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

async function pressKey(key, code, windowsVirtualKeyCode) {
  const params = {key, code, windowsVirtualKeyCode, nativeVirtualKeyCode: windowsVirtualKeyCode};
  await command("Input.dispatchKeyEvent", {type: "keyDown", ...params});
  await command("Input.dispatchKeyEvent", {type: "keyUp", ...params});
}

async function clickSelector(selector) {
  const point = await evaluate(`(() => {
    const element = document.querySelector(${JSON.stringify(selector)});
    if (!element) throw new Error('missing ' + ${JSON.stringify(selector)});
    element.scrollIntoView({block: 'center'});
    const rect = element.getBoundingClientRect();
    return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
  })()`);
  await command("Input.dispatchMouseEvent", {type: "mousePressed", x: point.x, y: point.y, button: "left", clickCount: 1});
  await command("Input.dispatchMouseEvent", {type: "mouseReleased", x: point.x, y: point.y, button: "left", clickCount: 1});
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
      const radioGroup = controls.every(control => control.tagName === 'INPUT' && control.type === 'radio');
      const uniqueValues = new Set(controls.map(control => control.value)).size === controls.length;
      return !radioGroup || !uniqueValues;
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
    duplicates,
    buildMarker: document.querySelector('[data-build-marker="true"]')?.textContent.trim() || '',
  };
})()`);
if (fullContract.page !== "full" || !fullContract.sectionsVisible || fullContract.anchorCount !== 6 || fullContract.groupCount !== 2 || fullContract.groupAnchorCount !== 2 || !fullContract.groupsClosed || fullContract.duplicates.length || !fullContract.buildMarker) {
  throw new Error(`完整页契约失败: ${JSON.stringify(fullContract)}`);
}
for (const id of ["section-where","section-when","section-who","section-budget","section-flight-preferences","section-notifications"]) {
  await evaluate(`document.querySelector('a[href="#${id}"]').click(); true`);
  await waitFor(`location.hash === '#${id}' && Boolean(document.getElementById('${id}'))`, `锚点${id}`);
}
console.log(`[UI smoke] 页2六节=${fullContract.sectionCount} 全可见=True 目录锚点=${fullContract.anchorCount} 次级组锚点=${fullContract.groupAnchorCount} 重复name=0`);
console.log(`[UI smoke] 版本信标=${fullContract.buildMarker}`);

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
  set('outbound_set_off', '06:30');
  set('shared_departure_window_start', '08:00');
  set('shared_departure_window_end', '12:00');
  set('outbound_departure_window_start', '06:30');
  set('outbound_departure_window_end', '08:30');
  set('return_departure_window_start', '18:00');
  set('return_departure_window_end', '21:00');

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
    outboundWindow: text.includes('06:30-08:30'),
    returnWindow: text.includes('18:00-21:00'),
  };
})()`);
if (!confirmation.email || !confirmation.meetingStart || !confirmation.meetingEnd || !confirmation.outboundWindow || !confirmation.returnWindow) {
  throw new Error(`完整页回读失败: ${JSON.stringify(confirmation)}`);
}
console.log("[UI smoke] 页2邮箱提交=PASS value=ux31@example.com");
console.log("[UI smoke] 页2当天往返会议=PASS 10:30-17:00");
console.log("[UI smoke] 分方向时间窗回读=PASS 去程06:30-08:30 返程18:00-21:00");

await navigate("/settings?edit=1");
const editGroups = await evaluate(`(() => ({
  business: document.getElementById('group-business-travel')?.hasAttribute('open') || false,
  feasibility: document.getElementById('group-feasibility')?.hasAttribute('open') || false,
  customTime: document.querySelector('[data-time-window-group="custom"]')?.hasAttribute('open') || false,
  directionalTime: document.querySelector('[data-time-window-group="directional"]')?.hasAttribute('open') || false,
}))()`);
if (!editGroups.business || !editGroups.feasibility || !editGroups.customTime || !editGroups.directionalTime) {
  throw new Error(`编辑态次级组未自动展开: ${JSON.stringify(editGroups)}`);
}
console.log("[UI smoke] 编辑态details自动展开=PASS 商务出行+可行性参数+分层时间窗");

await sleep(350);
if (browserErrors.length) throw new Error(`浏览器错误: ${browserErrors.join(' | ')}`);
console.log("[UI smoke] console error=0");
ws.close();