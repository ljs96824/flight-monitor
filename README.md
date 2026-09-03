# Flight Monitor

[![tests](../../actions/workflows/tests.yml/badge.svg)](../../actions/workflows/tests.yml)

> An evidence-first flight monitoring system with explicit price scopes, source provenance, and offline regression contracts.

Flight Monitor 是一个本地优先的航班采集、约束过滤与通知系统。它输出的是可核验的参考数据，不代替航司、OTA、支付页或用户作购买决定。

> [!IMPORTANT]
> 票价、行李、退改与舱位库存都可能在支付前变化。系统会标出人数、往返、含税与数据来源口径；无法取得数据时应显示缺口，而不是把采集失败解释成售罄、停飞或市场涨价。

## 1. 定位

本项目适合需要持续观察固定航线、日期与约束的人。它把以下工作串成一条可审计链路：

- 采集单人单程参考价与航班结构；
- 按直飞、中转、时间、行李、航司、廉航、乘客和预算约束过滤；
- 对往返、多人和混舱场景组装明确口径的参考总价；
- 通过邮件、PushPlus 或两者发送结构化结果；
- 把观测、轮次证据、配额使用和统计依据留在本地。

它不是比价网站、订票代理、自动交易器，也不保证覆盖所有航班或最低可售价格。

## 2. 设计哲学

1. **诚实优先于“有结果”**：空结果、配额保护、缓存复用、源退化和样本不足都必须显式披露。
2. **价格比较必须同口径**：单人不能与全员混比，单程不能与往返混比；儿童、婴儿等估算必须带说明。
3. **时间比较必须带日期**：跨午夜和次日航班按完整时间比较，国际航段按机场时区锚定。
4. **证据可追溯**：统计值携带样本数、窗口、来源与方法版本；历史条件变化后开启新的约束纪元。
5. **只陈述客观权衡**：系统并列展示方案和事实，不使用复合主观评分替用户下结论。
6. **失败不静默**：备选与主方案采用相同的去返、人数、预算和验证要求；缺腿或缺价时明确降级。
7. **PII 与密钥不进仓库**：真实邮箱、token、API key、个人标识只放本机或部署环境的 `.env`，不得写入文档、夹具、日志或提交。

## 3. 功能清单

以下均来自当前代码与已落地契约：

- 两张表单页面：快速创建与完整设置；严格地点解析，IATA 码来自静态机场表。
- 单程、普通往返、当天往返与商务会议时间窗；去返均按完整日期时间判断。
- 成人、儿童、老人、婴儿人数；单人/全员预算口径；国内与国际乘客费率分开。
- 全员经济、全员商务及按乘客类型分配的混舱监控。混舱只接受同一航班经济舱与商务舱都可匹配的组合。
- 直飞/中转、红眼、起降时间、行李、退改、航司、廉航和舱位约束。
- 主方案、完整往返备选、排除原因、低价日历、方案追踪、渠道与数据时点披露。
- 邮件、PushPlus、两者同时发送；失败时保留本地 payload 与轮次日志。
- 本轮请求计划、请求级缓存、当日面板复用、源熔断、配额保护和调用统计。
- 追加式 SQLite 观测库、固定篮子、提前购买曲线、统计依据信封、航班规律和实验性预测。
- 预测只在累计走前回测技能门通过时展示，且描述市场最低参考价，不输出“购买/等待”指令。

## 4. 架构

```text
                         +---------------------------+
                         | GitHub                    |
                         | source + offline CI       |
                         | no production scheduler   |
                         +-------------+-------------+
                                      ^
                                 pull | push
                                      v
+-------------------------------------+--+       +---------------------------+
| Local machine                          |<----->| PythonAnywhere (optional)  |
| web form / collection / analysis       | sync  | web form / subscriptions  |
| SQLite observations / logs / scheduler |       | payload detail hosting    |
+----------------------------------------+       +---------------------------+
```

采用这个三角而不是把所有工作塞进一个平台，原因是：

- **本机**拥有持久 SQLite、任务计划、浏览器 smoke 环境和实际出站采集条件，是默认运行主体。
- **PythonAnywhere**只作为可选表单、订阅与详情同步节点。其出站访问受账户套餐和白名单约束，不应未经验证就承担采集或 SMTP 发送。
- **GitHub**保存代码并运行 Ubuntu/Windows 离线测试。CI workflow 不接收生产 secrets，也不承担定时采集；临时 runner 不适合作为长期观测库。

## 5. 数据源与配额经济学

当前源策略的单一真值见 [source_profiles.py](source_profiles.py)。阈值与硬门政策见
[config.defaults.yaml](config.defaults.yaml)；额度包、控制台对账和研究运行态属于运行事实，
只保存在被 Git 忽略的 `data/runtime_config.yaml`。

现行 Web CRUD、订阅采集、尝试状态与 PA 同步的权威持久化源为 `data/subscriptions.json`。
`data/runtime_config.yaml` 中的 `subscriptions: []` 目前仍是配置校验与 legacy 迁移所需的空兼容占位；
在 6b 完成前必须保持为空数组，不得写入真实订阅，也不得提前删除该字段（删除会导致 `validate_runtime_config` 失败）。

| 数据源 | 当前职责 | 本地额度口径 | 明确限制 |
| --- | --- | --- | --- |
| 聚合数据（Juhe） | 国内、国际及港澳台经济舱主列表源 | 550 次/包；当前总额度与对账值只存本地运行配置 | 返回空或错误时不能推断为售罄；最终价格以支付页为准 |
| SerpAPI | 国际及港澳台商务舱列表源，仅在商务/混舱主日期请求 | 250 次/月，预留 30 次后触发配额保护 | 经济舱交叉核对默认关闭；展示价的税费构成未拆分 |
| Duffel | 行李、退改等规则富化 | 本系统不设本地额度上限 | 只富化已存在候选，不作为当前推荐池定价来源；供应商自身限制仍适用 |
| HasData | 已退役，仅保留历史代码与既有观测解释 | 2026-08-14 起不再计划新请求 | 退役原因是 403/订阅终止；历史 global_min 数据不删除、不改写 |

调用成本通过“先计划、后执行”控制：同一轮中相同的源、机场对、日期、乘客和舱位键只实际请求一次；订阅轮可复用新鲜面板，固定篮子仍强制新鲜。`data/api_usage.json` 只是本地估算台账，供应商控制台才是最终额度依据。

所有价格先锚定为单人单程 CNY 参考价，再由统一金额树组装往返与多人展示。多人低价舱库存不足、儿童/婴儿票规、税费、服务费、行李和混舱库存都可能使支付页金额不同。

## 6. 快速开始

### 6.1 环境

- Python 3.13。
- Windows 或 Linux；本地 UI smoke 需要 Edge、Chrome 或 Chromium，并需要 Node.js 22+。
- 在项目根目录执行：

```bash
python --version
python -m pip install -r requirements.txt -r requirements-dev.txt
python -X utf8 scripts/initialize_api_usage.py
# pytest 已由 requirements-dev.txt 锁定
```

生产部署只需安装 [requirements.txt](requirements.txt)；本地开发与测试同时安装 [requirements-dev.txt](requirements-dev.txt)。

直接依赖只在 [requirements.in](requirements.in) 与 [requirements-dev.in](requirements-dev.in) 中维护。锁文件由 pip-compile 生成，勿手改。修改输入文件后，在干净的 Python 3.13 环境执行：

```bash
python -m piptools compile --allow-unsafe --generate-hashes --no-emit-index-url --no-emit-trusted-host --output-file requirements.txt --strip-extras requirements.in
python -m piptools compile --allow-unsafe --generate-hashes --no-emit-index-url --no-emit-trusted-host --output-file requirements-dev.txt --strip-extras requirements-dev.in
```

### 6.2 创建本地运行配置

将 [config.example.yaml](config.example.yaml) 复制为
`data/runtime_config.yaml`，再在本机填写已购额度包、控制台核对时刻与余量、储备纪元、
目标日期与研究开关。示例文件故意不携带任何控制台实值；缺字段、损坏或缺失
都会在真实请求前失败，不会回退成空预算。其中本地 `reconciliation` 对象至少需要 `checked_at` 与
`console_remaining`；它们只写入被忽略的 runtime 文件，避免把控制台证据模板化进
公开配置。

生产配置事实源固定为 Git 跟踪的 `config.defaults.yaml` 与本机的
`data/runtime_config.yaml`，加载器严格合并这两层。升级用户持有的 legacy 单文件配置时，
必须显式提供源文件路径：

```bash
python -X utf8 scripts/migrate_runtime_config.py --source <path-to-legacy-config>
```

该命令默认仅 dry-run；明确加 `--write` 才会先备份显式指定的旧文件，再原子写入两层配置。

### 6.3 创建 `.env`

下面的跨平台命令只在 `.env` 不存在时复制 [.env.example](.env.example)，不会覆盖已有文件：

```bash
python -c "from pathlib import Path; src=Path('.env.example'); dst=Path('.env'); dst.exists() or dst.write_bytes(src.read_bytes())"
```

随后在本地编辑 `.env`。不要把真实值粘贴到 README、Issue、测试或日志。

`NO_LIVE_API` 仅用于 CI 与受控离线验证，不是全局断网开关；各外部网络 gateway 的实际覆盖状态见 [覆盖清单](docs/external-network-no-live-api-coverage-2026-09-03.md)。

**必需层**

| 变量 | 解锁能力 |
| --- | --- |
| `JUHE_FLIGHT_KEY` | 当前经济舱主采集；没有它，首次监控通常无法形成经济舱候选池 |
| `FLASK_SECRET_KEY` | PA Web部署必须配置固定高熵值；本地缺失时仅以进程临时密钥兜底并高可见告警 |
| `PUSHPLUS_TOKEN` 或 `SMTP_USER` + `SMTP_PASS` | 至少一种外部通知渠道；缺失时仍可检查本地 payload 和日志 |

**可选层**

| 变量 | 解锁能力 |
| --- | --- |
| `SERPAPI_KEY` / `SERPAPI_API_KEY` / `SERP_API_KEY` | 三者只填一个；国际及港澳台商务舱、混舱监控 |
| `DUFFEL_TOKEN` | 已有候选的行李与退改规则富化 |
| `SMTP_PROVIDER`、`SMTP_HOST`、`SMTP_PORT`、`SMTP_SSL` | 邮件服务商与连接参数覆盖 |
| `PYTHONANYWHERE_TOKEN`、`PYTHONANYWHERE_USER` | 可选的订阅和详情 payload 同步 |
| `FEEDBACK_NOTIFY_EMAIL` | 表单反馈通知收件地址 |
| `COLLECTION_LOCK_PATH` | 同机多进程采集单飞锁；建议配置为主运行目录中的绝对路径 |
| `SESSION_COOKIE_SECURE` | 本地HTTP默认`0`；PA HTTPS部署必须置`1` |
| `CSRF_TOKEN_TTL_SECONDS`、`COLLECTION_STARTUP_TIMEOUT_SECONDS` | 写操作token有效期与首次采集启动状态等待上限 |

其余可调阈值与诊断开关已按用途分组列在 [.env.example](.env.example)。SerpAPI 密钥别名的解析实现见 [serpapi_credentials.py](serpapi_credentials.py)。

### 6.4 启动网页

```bash
python -u -X utf8 run_web.py
```

访问 `http://127.0.0.1:5000`。页脚的 `build ... · 启动 ... · :5000` 是版本信标；若页面行为与代码不一致，先核对该信标。用户的 `:5000` 实例受 [CONTRIBUTING.md](CONTRIBUTING.md) 的端口主权规则保护。

首个订阅只需三步：

1. 在“快速创建监控”填写出发地、目的地、日期/往返、乘客、预算和场景。
2. 选择邮件、PushPlus 或两者；邮件渠道同时填写收件邮箱。完整设置页可调整时间、航司、行李、商务或混舱约束。
3. 提交。保存成功后会触发一次后台采集；检查页面版本信标、通知渠道以及 `data/run_latest.log` 中对应轮次的结果。

### 6.4 运行离线测试

```bash
python -X utf8 -m pytest -q
python -X utf8 -m unittest discover
```

真实浏览器交互同时在本机验收，并由公开 CI 的独立阻断式 `ui-smoke` job 运行。CI 用锁定版本的 Playwright 供应 Chromium，测试驱动仍是现有 CDP 脚本；覆盖边界见 [CONTRIBUTING.md](CONTRIBUTING.md)。启动器自动探测 Edge、Chrome 与 Chromium；也可用 `BROWSER_PATH` 指定，Windows 继续兼容 `EDGE_PATH`。失败时才写入所给日志与产物目录，成功不落盘：

```bash
python -X utf8 scripts/ui_smoke.py --log-path data/ui-smoke-artifacts/ui-smoke.log --artifact-dir data/ui-smoke-artifacts
```

### 6.5 手动采集与篮子定时

以下两个命令会调用已配置的数据源并**消耗配额**：

```bash
python -u -X utf8 main.py
python -u -X utf8 basket_collect.py
```

Windows Task Scheduler 示例（在项目根目录的 PowerShell 中执行；任务触发时会消耗配额，并会创建系统任务）：

```powershell
$project = (Get-Location).Path
$taskName = "flight_basket"
$action = "cmd /c cd /d `"$project`" && python -u -X utf8 basket_collect.py >> data\basket.log 2>&1"
schtasks.exe /Query /TN $taskName 2>$null
if ($LASTEXITCODE -ne 0) {
    schtasks.exe /Create /TN $taskName /SC DAILY /ST 09:30 /TR $action
}
```

Linux cron 示例（每天 09:30；执行时会消耗配额）：

```cron
30 9 * * * cd "$HOME/flight-monitor" && python3.13 -u -X utf8 basket_collect.py >> data/basket.log 2>&1
```

固定篮子使用机场级日期队列并强制新鲜采集。编辑 [basket_collect.py](basket_collect.py) 中的队列前，应先评估每日调用量。

### 6.6 PythonAnywhere 可选部署

PythonAnywhere 不是必需组件。若用它承载表单或详情页，在其 Bash console 中：

```bash
cd ~/flight-monitor
git pull --ff-only
python3.13 -m pip install --user -r requirements.txt
```

然后在 Web 面板点击 **Reload**。Reload 前必须在 PA 私有 `.env` 配置固定 `FLASK_SECRET_KEY` 与 `SESSION_COOKIE_SECURE=1`；缺少固定密钥的临时兜底不算生产验收通过，多 worker 会因会话密钥不同产生随机 403。生产密钥不要放入 GitHub。部署前先核验聚合数据、SerpAPI、Duffel 和 SMTP 端点是否允许出站；若受套餐或白名单限制，让 PythonAnywhere 只承载表单/同步，本机继续负责采集和发送。完整边界见 [Web写操作安全](docs/web-write-security.md)。

`Reload判据=Web进程是否import改动模块,非是否改web_form.py`。当前 Web 链路由 `run_web.py` 导入 `web_form.py`；后台处理首次运行时再由 `web_form.py` 延迟导入 `main.py`，而 `main.py` 导入 `forecast.py`，因此修改 `forecast.py` 后已加载该链路的 Web 进程需要 Reload。`patterns.py` 当前只由离线 `scripts/forecast_report.py` 使用，单独修改它不会进入 Web 进程。

## 7. 日常运行

### 采集与证据

- `main.py`：先做订阅前置校验和全轮请求计划，再采集、分析、生成 payload 并分发通知；会消耗配额。
- `basket_collect.py`：独立固定篮子，只采集和落观测库，不分析、不推送；会消耗配额。
- 最新进程日志写入 `data/run_latest.log`；每轮证据追加到 `data/logs/rounds/YYYYMMDD.log`。
- API实际请求写入 `data/api_usage.json`，观测写入 `data/observations.sqlite3`，通知详情写入 `data/payloads/`。这些都是运行时文件，不进入 Git。

### 只读体检

下列 `--help` 命令不调用真实航班 API：

```bash
python -X utf8 scripts/list_expired_subs.py --help
python -X utf8 scripts/list_unresolvable_subs.py --help
python -X utf8 scripts/list_incomplete_notification_subs.py --help
python -X utf8 scripts/tcurve_report.py --help
python -X utf8 scripts/provenance_report.py --help
python -X utf8 scripts/forecast_report.py --help
```

生成离线全链路快照：

```bash
python -X utf8 scripts/snapshot_run.py --output data/snapshot_check.json
```

该脚本使用固定夹具，不发起真实 API；`skipped_items` 非空时必须逐项解释，不能把残缺快照当作通过。

## 8. 工程纪律

- **双收集器**：提交前同时运行 `pytest` 与 `unittest discover`；GitHub Actions 在 Ubuntu、Windows 两个平台执行同一套离线测试，配置见 [.github/workflows/tests.yml](.github/workflows/tests.yml)。
- **冻结邮件基线**：脱敏 payload 与期望哈希位于 [tests/fixtures/frozen_email/](tests/fixtures/frozen_email/)，变更纪律见 [docs/email-regression-baseline.md](docs/email-regression-baseline.md)。基线更替必须记录旧哈希、新哈希、原因、日期和批准人。
- **快照模式**：业务逻辑变更前后运行 [scripts/snapshot_run.py](scripts/snapshot_run.py)，只接受预期字段差异。
- **契约族**：价格口径、时间日期、表单规范化、渲染完整性、通知小节、副作用、源策略、约束指纹和文档准确性都有回归测试。
- **版本信标**：网页页脚显示 Git短哈希、进程启动时间和端口，避免把旧进程当成新代码验收。
- **轮档与台账**：轮档追加且脱敏；台账只记录实际 API 请求，缓存命中、面板复用和源级跳过不得冒充消耗。
- **约束纪元**：约束指纹变化后，价格走势和历史位置从新桶积累；不得把筛选变化叙述成市场涨跌。
- **离线默认**：测试、快照、报告和文档校验不得发起真实外部 API；审计脚本只有显式 `--execute` 才允许消耗配额。
- **敏感信息**：`.env`、用户订阅、真实 payload、观测库和日志均不提交；测试夹具必须先脱敏。

## 9. 目录导览

| 路径 | 作用 |
| --- | --- |
| [run_web.py](run_web.py)、[web_form.py](web_form.py) | Web入口、表单、订阅管理与详情页 |
| [main.py](main.py) | 订阅轮编排、前置校验、采集计划、分析和通知 |
| [basket_collect.py](basket_collect.py) | 固定篮子采集入口 |
| [source_profiles.py](source_profiles.py)、[sources/](sources/) | 路线/舱位源策略与各数据源适配器 |
| [analyzer.py](analyzer.py)、[pricing.py](pricing.py)、[price_estimator.py](price_estimator.py) | 约束分析、价格口径和金额树 |
| [notifier.py](notifier.py)、[email_notifier.py](email_notifier.py) | 统一 payload、邮件与PushPlus渲染/发送 |
| [observations_store.py](observations_store.py)、[storage.py](storage.py) | 追加式观测库与历史/快照存储 |
| [scripts/](scripts/) | 快照、体检、只读统计报告和本地 UI smoke |
| [docs/runtime-backup-and-restore.md](docs/runtime-backup-and-restore.md) | 运行数据备份、隔离恢复与报告复放手册 |
| [analytics/](analytics/) | 只读描述统计报告 |
| [tests/fixtures/](tests/fixtures/) | 脱敏响应、表单规范化与冻结邮件夹具 |
| [docs/](docs/) | 设计、计划、审计和回归纪律入口 |

建议先读：

- [商务舱源能力审计](docs/cabin-capability-audit-2026-08-13.md)
- [SerpAPI能力审计](docs/serpapi-capability-audit-2026-08-14.md)
- [邮件回归基线纪律](docs/email-regression-baseline.md)
- [采集轮并发与锁顺序](docs/collection-concurrency.md)
- [设计规格目录](docs/superpowers/specs/)
- [实施计划目录](docs/superpowers/plans/)

## 10. 限制与非目标

- 系统不订票、不支付、不锁舱、不保证最低价，也不替用户决定购买时点。
- 搜索价不是支付承诺。税费、服务费、行李、退改和多人库存应在支付页逐项核实。
- 儿童、婴儿和混舱金额包含规则估算；国际儿童票与商务舱儿童票可能明显不同。
- 混舱 v1 要求同一航班两舱都可见；缺少商务舱匹配时不跨航班拼凑，不生成可订组合价。
- 提前购买曲线、历史位置和预测受样本量、来源覆盖和观测窗口限制；覆盖外不外推，技能门未过不进入推送。
- 会议交通与冗余是可追溯估算，不是地图导航或准点承诺。
- 本地 SQLite 与文件同步适合个人/小规模运行，不是多租户、高可用云服务。
- 数据源可能空返回、限额、变更字段或退役；系统的责任是披露，不是补造数据。

### License

本项目采用 [MIT License](LICENSE)。

价格数据仅供参考,以各渠道支付页为准。

### 开发方式

本项目由三方协作构建:人类所有者负责产品判断、真实环境验证与最终放行;AI 架构师(Claude)负责诊断、任务规格与验收判读;AI 执行器(OpenAI Codex)负责代码实现与本地验证。协作围绕一条核心纪律:任何一方的声明都不是证据——用户 shell 的原始输出是本地事实的唯一来源,公开 CI 是跨环境终审,磁盘副作用一律隔离,真实 API 调用必须显式授权并逐笔记账。docs/ 目录保留全部设计规格与 TDD 计划,提交史即协作史。这些门禁的存在不是因为信任缺失,而是因为信任需要可验证。
