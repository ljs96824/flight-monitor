# 手工真实外部 API 入口审计（2026-08-29）

## 1. 范围与边界

- 固定基线：`eeec2aa3812da22615c507679678f1109a63bf52`（PR #7 merge commit）。
- 执行环境：`NO_LIVE_API=1`；本次审计与测试真实 API 调用为 0。
- 本笔只处理独立调试脚本、能力审计 CLI、合同测试与本文档。
- 未修改生产采集、分析、推荐、去重、预测、通知渲染、source profile、依赖锁或运行配置。
- 扫描能证明仓库内引用；外部消费者结论来自维护者对本机任务、PythonAnywhere 与个人工具的逐项核验。

## 2. Legacy 入口引用矩阵

| 文件 | executable_reference | active_documentation | historical_mention | external_reference_verified_zero | 处置 |
|---|---:|---:|---|---:|---|
| `debug_api.py` | 0 | 0 | Git 历史保留路径 | true | 删除当前分支文件 |
| `debug_skyscanner.py` | 0 | 0 | Git 历史保留路径 | true | 删除当前分支文件 |
| `debug_sources.py` | 0 | 0 | Git 历史保留路径 | true | 删除当前分支文件 |
| `debug_travelpayouts.py` | 0 | 0 | Git 历史保留路径 | true | 删除当前分支文件 |

仓库扫描覆盖 Python import、动态 import 文本、`subprocess`、`os.system`、PowerShell、BAT/CMD、测试、workflow、README、CONTRIBUTING 与现行运维文档。历史审计文本与 Git 历史不为美化现状而改写。

外部消费者核验：

- 本机四个 flight 相关 Windows Task Scheduler 任务仅执行 `main.py` 与 `basket_collect.py`，无 `debug_*.py`。
- PythonAnywhere 免费账号未启用 Scheduled tasks 或 Always-on tasks。
- 维护者确认个人 runbook、notebook、快捷方式与协作记录均无四支脚本的执行引用。

因此四项均满足：可执行引用为 0、活动文档引用为 0、外部引用已核验为 0。

## 3. 删除前能力与风险

| 入口 | 默认行为 | 最大调用数 | 统一保护 | 输出风险 |
|---|---|---:|---|---|
| `debug_api.py` | import 即请求 | 1 | 无 NO_LIVE、单飞、配额预检或台账 | 写完整供应商响应；曾打印密钥前缀 |
| `debug_skyscanner.py` | import 即请求 | 最多 6 | 无统一保护 | 打印响应片段与结构 |
| `debug_sources.py` | import 即依次调用多个 source | 无可靠硬上限 | 无统一保护 | 打印异常与聚合统计 |
| `debug_travelpayouts.py` | import 即请求 | 3 | 无统一保护 | 曾打印 token 前缀 |

这些文件是绕过现行统一保护的独立入口；删除脚本不代表删除对应 source adapter。以下资产明确保留：

- `sources/searchapi_source.py`
- `sources/skyscanner_source.py`
- `sources/travelpayouts_source.py`
- 既有依赖、环境变量模板、source profile 元数据与历史观测解释

## 4. 保留的 manual-live 入口

| 入口 | 默认模式 | 明确计划与硬上限 | NO_LIVE_API | singleflight | 严格台账与配额预检 | 台账标签 | 原始响应/密钥输出 |
|---|---|---|---:|---:|---:|---|---|
| `scripts/serpapi_capability_audit.py` | dry-run | 商务、经济各 1 次；总 6、SerpAPI 3 | 是 | 是 | 是 | `manual_live` / `serpapi_capability_audit` | 仅脱敏摘要；不输出值或前缀 |
| `scripts/cabin_capability_audit.py` | dry-run | 每个显式选择源 1 次；总 6、每源 3 | 是 | 是 | 是 | `manual_live` / `cabin_capability_audit` | 仅脱敏摘要；不输出值或前缀 |

统一执行顺序为：

1. 先判 `NO_LIVE_API`，此时不读取 `.env`、密钥、配额文件或网络。
2. 按 `Asia/Shanghai` 拒绝过去日期。
3. 严格读取 `api_usage.json`，并检查一致性与 pending reconciliation。
4. 使用既有 `quota_policy.metrics()` 检查源预算、储备和 manual-live buffer。
5. 非阻塞获取共享 collection single-flight；busy 独立非零退出。
6. 明示本轮计划与硬上限后才解析凭据并尝试 HTTP。
7. 每个 HTTP 尝试在 `finally` 中即时记账；不允许未记账重试。

全仓候选扫描还命中生产 source adapters、`request_cache.py`、`main.py`、通知发送与 PA 同步，它们属于生产调用链，不是手工 live CLI；`scripts/snapshot_run.py` 使用离线 HTTP stub。未发现第三支需保留的 manual-live 能力审计入口。

## 5. 历史残留只读盘点

未读取以下文件正文；只记录存在性、大小、mtime、SHA-256 与可能类别。

| 路径 | 存在 | 字节 | 修改时刻（UTC） | SHA-256 | 可能含原始供应商响应 |
|---|---:|---:|---|---|---:|
| `data/debug_response.json` | 是 | 57,386 | `2026-05-08T11:12:08.9669270Z` | `6ab9c45e7481481c1bd5748ac618db50f62f5aefc66003ab78bd164bc19aa87f` | 是 |
| `data/run_latest.log` | 是 | 336 | `2026-08-29T04:42:28.4996166Z` | `79c84c86709e11e2b348dfd1b29fa87c6d671ac474b16cf6ca721070e0595683` | 未证实 |
| `data/serpapi_capability_audit_20260814.json` | 是 | 701 | `2026-08-14T07:14:31.5829002Z` | `be7734dc2d398e83685440c0652ffe97a78f7c23754272c5c816705f5397557a` | 摘要产物，非原始响应 |

- `run_latest.log` 与现存轮档的 legacy 脚本标记命中为 0。
- 当前可见 CI artifact 名称中 debug/response/audit 命中为 0。
- `data/debug_response.json` 曾进入 Git 历史：首次仓库提交中存在，后续提交移出当前树；历史 blob 仍可取得。
- 这证明曾存在原始响应公开路径，但没有证据证明可利用密钥已泄露。若私下核验历史响应确认供应商回显认证值，应立即轮换对应凭据；本报告不把风险路径写成已确认泄露。
- 本笔不删除任何本地运行数据，不改写 Git 历史。

## 6. 最终处置

**删除：**四支已核验无消费者的 legacy `debug_*.py`。

**保留并加固：**两支能力审计脚本，保留各自专项调用计划与既有解析结论。

**保留不动：**生产 source adapters、依赖、环境变量模板、运行配置、本地历史产物与 Git 历史。

**阻塞项：**无。外部引用已经维护者逐项核验为 0。

## 7. 验证合同

- 删除路径不存在，`importlib.find_spec()` 返回 `None`，可执行文件中无 legacy 引用。
- 独立 subprocess 安装 socket 拒绝后，两支审计脚本 import 与默认 dry-run 均无网络尝试。
- `NO_LIVE_API`、损坏台账、配额不足、single-flight busy、过去日期均在 HTTP 前阻断。
- mocked live 路径按真实尝试数即时记账，记录含稳定 `manual_live` 与 entrypoint。
- 测试使用唯一假 canary secret；stdout、stderr、异常、报告、JSON 与输出文件均不得出现该值。
