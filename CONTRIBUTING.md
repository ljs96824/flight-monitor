# 贡献约定

## 本地服务端口

- 用户拥有 `:5000`。它代表用户正在使用和验收的本地服务实例。
- 任务预览必须使用隔离端口，并在任务回报中写明预览端口和页面版本信标。
- 禁止停止、重启或占用用户的 `:5000`，也不得把测试服务绑定到该端口。
- 浏览器 smoke 应继续使用脚本分配的临时端口；测试结束后由脚本清理自己的进程。

这条规则用于保证代码版本、进程实例和用户看到的页面可以一一对应。遇到页面行为与测试不一致时，先核对页脚 `build ... · 启动 ... · :port` 信标，再排查业务逻辑。

## 浏览器 smoke 阻断模式

截至 2026-08-31，`main` 分支保护将 `ui-smoke` 列为 required check。公开 CI 使用固定版本 Playwright 供应 Chromium，但测试驱动仍是现有 CDP 脚本。

workflow 不使用 `continue-on-error`；浏览器 smoke 失败会直接阻断 workflow。失败时继续上传浏览器截图、页面 HTML、浏览器控制台和服务日志等现有证据；启动前故障只保存实际能够生成的证据，不伪造不存在的截图或页面产物。

零真实 API 的实际隔离来自四层：mock `start_background_collection`、mock `load_calendar`、临时数据目录和临时端口。`NO_LIVE_API=1` 只是明示合同，不能单独作为零 API 证据；验收还必须确认生产三库与配额台账哈希不变。

`scripts/ui_smoke.py` 的临时服务器运行完整 `web_form.app`，并非只挂载少数路由。当前 CDP 驱动覆盖 `/`、`/settings`（含编辑回填与提交）、POST `/subscribe`、`/success`、`/subscriptions`、GET/POST `/subscription/<subscription_id>/delete`、POST `/subscriptions/<subscription_id>/toggle` 的暂停与恢复，以及 POST `/subscriptions/<subscription_id>/quick-update`。

2026-08-31 的本机 smoke 服务日志还显示页面间接访问 `/price_hint` 并返回 200；当前驱动只证明这次间接访问，没有专项业务语义断言。`/feedback` 在该次 smoke 中未访问。smoke 绿不等于 Web 全绿：需要验证新的或尚未覆盖的行为时，必须单独扩展 smoke 驱动与交互断言，不能把路由可访问等同于已经验证。

## F821 未定义名称硬门

`scripts/check_f821.py` 是绝对零门：任何 F821 命中都会使本地检查、push 与 pull
request CI 失败，并打印文件、所在作用域和符号。仓库不再维护“已知 F821 债务”基线；
历史清理记录只用于审计，不代表仍允许登记新债务。

禁止用 bare `# noqa`、`# noqa: F821`、Ruff `per-file-ignores` 或
`extend-per-file-ignores` 隐藏命中。确有静态分析无法识别的动态场景时，必须先单独
审计并在精确三元组中说明文件、符号和原因；当前允许集合为空。CI 顺序固定为
F821 零门、模块导入、pytest、unittest，未定义名称应在行为测试之前失败。

## 运行配置事实源

生产配置只由 Git 跟踪的 `config.defaults.yaml` 与本机的
`data/runtime_config.yaml` 严格合并。仓库根目录不保留兼容 `config.yaml`，也不得把本机额度、
控制台对账、研究开关或订阅复制回跟踪文件。

迁移用户持有的 legacy 单文件配置时必须显式给出源路径：

```bash
python -X utf8 scripts/migrate_runtime_config.py --source <path-to-legacy-config>
```

命令默认 dry-run；只有显式增加 `--write` 才会先备份源文件并写入两层配置。
