# 贡献约定

## 本地服务端口

- 用户拥有 `:5000`。它代表用户正在使用和验收的本地服务实例。
- 任务预览必须使用隔离端口，并在任务回报中写明预览端口和页面版本信标。
- 禁止停止、重启或占用用户的 `:5000`，也不得把测试服务绑定到该端口。
- 浏览器 smoke 应继续使用脚本分配的临时端口；测试结束后由脚本清理自己的进程。

这条规则用于保证代码版本、进程实例和用户看到的页面可以一一对应。遇到页面行为与测试不一致时，先核对页脚 `build ... · 启动 ... · :port` 信标，再排查业务逻辑。

## 浏览器 smoke 观察模式

公开 CI 的 `ui-smoke` job 使用固定版本 Playwright 供应 Chromium，但测试驱动仍是现有 CDP 脚本。该 job 暂以 `continue-on-error` 观察模式运行；观察计数以 Step Summary 中的 `steps.smoke.outcome=success` 为准，不以 workflow 总结论代替。

零真实 API 的实际隔离来自四层：mock `start_background_collection`、mock `load_calendar`、临时数据目录和临时端口。`NO_LIVE_API=1` 只是明示合同，不能单独作为零 API 证据；验收还必须确认生产三库与配额台账哈希不变。

观察模式退出必须同时满足：

1. 连续 7 次 `steps.smoke.outcome=success`。
2. 至少 1 次由 `pull_request` 触发。
3. 至少 1 次由 `workflow_dispatch` 触发。
4. 期间没有浏览器安装、启动、端口或日期时区类随机失败。

全部条件满足后，用独立提交删除 `continue-on-error`，让浏览器 smoke 成为阻断性合同。

## F821 未定义名称硬门

`scripts/check_f821.py` 是绝对零门：任何 F821 命中都会使本地检查、push 与 pull
request CI 失败，并打印文件、所在作用域和符号。仓库不再维护“已知 F821 债务”基线；
历史清理记录只用于审计，不代表仍允许登记新债务。

禁止用 bare `# noqa`、`# noqa: F821`、Ruff `per-file-ignores` 或
`extend-per-file-ignores` 隐藏命中。确有静态分析无法识别的动态场景时，必须先单独
审计并在精确三元组中说明文件、符号和原因；当前允许集合为空。CI 顺序固定为
F821 零门、模块导入、pytest、unittest，未定义名称应在行为测试之前失败。
