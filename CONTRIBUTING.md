# 贡献约定

## 本地服务端口

- 用户拥有 `:5000`。它代表用户正在使用和验收的本地服务实例。
- 任务预览必须使用隔离端口，并在任务回报中写明预览端口和页面版本信标。
- 禁止停止、重启或占用用户的 `:5000`，也不得把测试服务绑定到该端口。
- 浏览器 smoke 应继续使用脚本分配的临时端口；测试结束后由脚本清理自己的进程。

这条规则用于保证代码版本、进程实例和用户看到的页面可以一一对应。遇到页面行为与测试不一致时，先核对页脚 `build ... · 启动 ... · :port` 信标，再排查业务逻辑。

## 浏览器 smoke 阻断模式

公开 CI 的 `ui-smoke` job 使用固定版本 Playwright 供应 Chromium，但测试驱动仍是现有 CDP 脚本。观察期已经完成：连续成功计数达到 `7/7`，并包含 `pull_request`、`workflow_dispatch` 与主分支 push 三类触发，期间未再出现浏览器安装、启动、端口或日期时区类随机失败。

`continue-on-error` 已移除；此后浏览器 smoke 失败会直接阻断 workflow。失败时继续上传浏览器截图、页面 HTML、浏览器控制台和服务日志等现有证据；启动前故障只保存实际能够生成的证据，不伪造不存在的截图或页面产物。

零真实 API 的实际隔离来自四层：mock `start_background_collection`、mock `load_calendar`、临时数据目录和临时端口。`NO_LIVE_API=1` 只是明示合同，不能单独作为零 API 证据；验收还必须确认生产三库与配额台账哈希不变。

`scripts/ui_smoke.py` 的临时服务器运行完整 `web_form.app`，并非只挂载少数路由。当前 CDP 驱动覆盖 `/`、`/settings`（含编辑回填与提交）、`/subscribe`、`/success`、`/subscriptions` 以及删除确认 GET/POST；它尚未覆盖 `/price_hint`、`/feedback`、暂停等其余 CRUD 路由。smoke 绿不等于 Web 全绿：需要验证新的或尚未覆盖的 CRUD 行为时，必须单独扩展 smoke 驱动与交互断言，不能把路由可访问等同于已经验证。

## F821 未定义名称硬门

`scripts/check_f821.py` 是绝对零门：任何 F821 命中都会使本地检查、push 与 pull
request CI 失败，并打印文件、所在作用域和符号。仓库不再维护“已知 F821 债务”基线；
历史清理记录只用于审计，不代表仍允许登记新债务。

禁止用 bare `# noqa`、`# noqa: F821`、Ruff `per-file-ignores` 或
`extend-per-file-ignores` 隐藏命中。确有静态分析无法识别的动态场景时，必须先单独
审计并在精确三元组中说明文件、符号和原因；当前允许集合为空。CI 顺序固定为
F821 零门、模块导入、pytest、unittest，未定义名称应在行为测试之前失败。
