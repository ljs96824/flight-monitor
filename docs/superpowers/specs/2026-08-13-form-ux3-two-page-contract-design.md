# UX 3.0 双页化与交互契约层设计

## 目标与边界

在 `cc949c1` 的服务端概念派生层上撤除手风琴、面包屑、芯片墙与前端模式状态，改成两个可预测的静态入口。订阅 schema、`build_subscription()` 的既有输入语义、规范化输出、分析、金额、采集和通知保持不变。

## 页面契约

### 快速建单 `/`

页面是一列静态表单，按出发/目的地、日期与往返、乘客构成、预算、场景排列。页面只渲染快速建单所需概念，不渲染完整偏好控件；场景默认由提交后的 `apply_default_rules()` 静默派生。页面主按钮为“创建监控”，旁边说明时间、航司、行李和提醒采用场景预设，并提供 `/settings` 高级设置链接。

真实可见表单控件上限为 12 个；浏览器契约按可见且可交互的 `input/select/textarea` 逐个计数。乘客中的儿童与老人补充信息是快速页唯一一类业务条件显隐。

### 完整设置 `/settings`

页面使用单长页，六组顺序固定为：去哪、什么时候、谁去、预算、飞行偏好、怎么提醒。全部用户可编辑概念一次性渲染、默认可见；左侧目录是普通锚点，移动端变为顶部横向锚点条，不维护展开状态。每节标题显示服务端摘要。

通知邮箱是完整页唯一另一类业务条件显隐。页尾固定显示同源确认回读，每一行的“修改”链接直接指向所属章节锚点。

`/?edit=N` 兼容入口重定向到 `/settings?edit=N`。新建快速页提交 `monitor_mode=quick`，新建完整页提交 `monitor_mode=precise`；编辑存量订阅时沿用存量 `monitor_mode`，保证不改值重提的幂等契约。

## 控件唯一性

`form_concepts.CONCEPTS` 继续作为字段归属和服务端派生的单一真值。完整页每个字段名只出现于一个语义控件；单选用单个 `select`，多选用单个 `select multiple`，避免同名 DOM 元素。内部兼容字段可用一个 hidden input，但不得出现第二个可编辑表面。

## 保留的前端行为

- 地点精确匹配、候选提示、机场标签和只读 `price_hint`。
- 儿童/老人补充问题显隐。
- 通知邮箱显隐与邮箱格式校验。
- 日期、预算和必填校验。
- 页尾确认回读刷新与锚点链接。

除上述行为外，不允许控件折叠、站点切换、前向导航、芯片状态或由前端推导 `monitor_mode`。

## 废弃清单

- 六站单开手风琴：`openWizardStation`、`goToStep`、`updateStepper`、移动步骤条和前后站按钮。
- 面包屑状态：`station-breadcrumbs`、完成/当前/未到状态及点击跳站逻辑。
- 芯片墙：`scenario-preset-chips`、`canonical-preference-chips`、`renderDefaultChips`、`mountCanonicalPreferenceChips`。
- 快速收尾后再展开⑤⑥：`optional-settings-toggle`、`optionalFlowEnabled`、可选区展开态。
- 偏好折叠与智能面板：`advanced-toggle`、`rules-toggle`、`details` 展开态及存量高亮。
- 保存偏好弹层：本轮不再渲染，以减少第二套前端状态。

## 浏览器交互契约

`scripts/ui_smoke.py` 是统一入口：在临时目录启动隔离 Flask 服务并替换后台采集为空操作，随后启动本机 Edge `--headless=new`。`scripts/ui_smoke_driver.mjs` 使用 Node 内建 WebSocket 驱动 Edge DevTools Protocol，不安装 Selenium 或 WebDriver。

冒烟流程：

1. 快速页加载，必填控件可见可交互，真实可见表单控件不超过 12。
2. 填写快速表单并提交，抵达成功确认页。
3. 完整页加载，六节全部在 DOM 且可见。
4. 六个目录锚点逐一跳转，URL hash 与目标节匹配。
5. 完整页所有具名字段只出现一次。
6. 捕获 `Runtime.exceptionThrown`、`console.error` 和浏览器日志 error，任一命中即失败。

输出追加保存到 `data/ui_smoke_latest.log`。GitHub Actions 没有本项目约定的本地 Edge 环境，因此 workflow 保留一个明确标注 local-only 的 skipped step。

## 回归防线

- 七场景分别走既有直提契约与双页入口，规范化 JSON 逐字段相等。
- 编辑存量订阅不改值重提保持幂等。
- 上海—大阪邮件 HTML 字节哈希不变。
- `before_ux3.json` 与 `after_ux3.json` 仅允许 `computed_at` 动态字段变化。
- API 台账与观测库 SHA256 前后相同，整个任务不发真实 API。
