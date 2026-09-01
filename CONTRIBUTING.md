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

截至 2026-09-01，CDP 驱动已专项验证 `/price_hint` 的最终请求参数、无数据 JSON 与 DOM 回退，并锁定 route type 徽章、标签及隐藏字段；有数据价格显示文案尚未裁决。`/feedback` 仍未覆盖。smoke 绿不等于 Web 全绿：需要验证新的或尚未覆盖的行为时，必须单独扩展 smoke 驱动与交互断言，不能把路由可访问等同于已经验证。

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

## 交付声明与证据要求

本节是未来交付声明的规范性合同。`docs/codex-operational-evidence-audit-2026-08-30.md`
仅作本合同形成过程的历史出处；后续规则更新只改 `CONTRIBUTING.md`，不回写历史审计报告。
真实输出属于每次交付报告，不写进静态规范。

### 1. 声明：已创建 PR

- 命令：`gh pr view <N> --repo ljs96824/flight-monitor --json number,state,url,baseRefOid,headRefOid,headRefName,commits,files`
- 必填字段：`number`、`state`、`url`、`baseRefOid`、`headRefOid`、`headRefName`、`commits`、`files`、任务基线、本地提交 SHA。
- **通过条件**：`state=OPEN`、`baseRefOid == 任务基线`、`headRefOid == 本地提交 SHA`、`len(commits) == 1`。

### 2. 声明：已推送

- 命令：`LOCAL=$(git rev-parse HEAD)`；`REMOTE=$(git ls-remote --heads origin refs/heads/<branch> | awk '{print $1}')`。
- 必填字段：`LOCAL`、`REMOTE`、`branch`。
- **通过条件**：`LOCAL == REMOTE`；命令有输出不等于声明成立，必须比较两个 SHA。

### 3. 声明：main 为 X

- 命令：依次执行 `git fetch --prune origin`、`git branch --show-current`、`git rev-parse refs/heads/main`、`git rev-parse origin/main`。
- 必填字段：当前分支、`refs/heads/main` SHA、`origin/main` SHA、声明值 `X`。
- **通过条件**：当前分支为 `main`，且 `refs/heads/main == origin/main == X`。

### 4. 声明：CI 全绿

- 命令：`gh run view <RUN_ID> --repo ljs96824/flight-monitor --json databaseId,event,headBranch,headSha,status,conclusion,jobs`。
- 必填字段：`run_id`、`event`、`head_branch`、`head_sha`、`status`、`conclusion`、`jobs[].name/status/conclusion`、被验收提交 SHA、required job 清单。
- **通过条件**：`run.head_sha == 被验收提交 SHA`，且全部 required jobs 为 `completed/success`。PR 分支 checks 不等于 main post-merge checks，两类声明必须分别取证。

### 5. 声明：哈希不变

- 命令：在同一静默窗口开始和结束时，分别用 `Get-FileHash -Algorithm SHA256 <path>` 或 `sha256sum <path>` 生成本轮 `before` 与 `after`。
- 必填字段：静默窗口起止、每个文件的存在状态、字节数、`before` SHA、`after` SHA；运行态至少关注 `prices.db`、`observations.sqlite3`、`api_usage.json`。
- **通过条件**：本轮同一文件存在状态、字节数与 `before/after` SHA 一致；不与历史数值比较，因为 cohort 运行会合法改变 `prices.db`、`observations.sqlite3`、`api_usage.json`。

### 6. 声明：某文件无消费者

- 命令：执行 `rg -n "<symbol-or-path>" .` 及任务所需的 import、subprocess、workflow、文档与动态引用扫描；仓库外消费者由维护者另行核验。
- 必填字段：扫描命令、仓库内命中数与分类、仓库外核验范围、维护者确认、`evidence_level`。
- **通过条件**：仓库内可执行消费者命中数为 0，仓库外由维护者确认并标为 `user_reported`；两类证据必须分开记录，任一未核验都不能声明“无消费者”。

### 7. 声明：worktree 合规

- 命令：任务开始和结束均执行 `git worktree list --porcelain`，并记录任务 worktree 的绝对规范化路径。
- 必填字段：固定路径、`HEAD`、分支、项目目录、`data/` 目录、结束时清理结果。
- **通过条件**：worktree 位于项目目录与 `data/` 目录之外的固定路径；任务结束清理，并附结束后的 `git worktree list --porcelain`。

### 8. 声明：提交身份已核对

- 命令：提交前执行 `git config --get user.name` 与 `git config --get user.email`。
- 必填字段：`user.name`、`user.email`、预期身份、核对时刻。
- **通过条件**：提交前已确认两项配置与预期提交身份一致；公开报告只记录匹配或不匹配，不复述真实邮箱值。

### 9. 声明：可以删除远端 PR 分支

- 命令：执行 `gh pr view <N> --repo ljs96824/flight-monitor --json state,mergeCommit,headRefName,headRefOid`、`git fetch --prune origin`、`git merge-base --is-ancestor <MERGE_SHA> origin/main`、`git ls-remote --heads origin refs/heads/<branch>`，并用 `gh pr list --repo ljs96824/flight-monitor --state open --head <branch> --json number,state,headRefName` 检查其他 PR。
- 必填字段：PR `state`、`MERGE_SHA`、`origin/main`、已验收 head、远端分支 SHA、使用该分支的 open PR 数量。
- **通过条件**：`state=MERGED`，`git merge-base --is-ancestor <MERGE_SHA> origin/main` 返回 0，远端分支仍等于已验收 head，且无其他 open PR 使用该分支。不得以网页提示语或本地 `git pull` 单独作为依据；本地 `main` 同步是独立收尾动作。

### 10. 声明：冻结 SHA 未漂移

- 命令：执行该 fixture 的生成命令，再以 `Get-Item <output>` 或等价命令取字节数，并以 `Get-FileHash -Algorithm SHA256 <output>` 或 `sha256sum <output>` 计算 SHA-256。
- 必填字段：fixture 路径、生成命令、字节数、完整 64 位 SHA、期望 SHA。
- **通过条件**：实跑值与同一 fixture 的期望值一致；不同时先判定是 fixture 不同还是基线漂移，不得直接更新期望值。
