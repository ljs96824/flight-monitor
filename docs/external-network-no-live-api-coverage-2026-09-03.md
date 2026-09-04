# 外部网络出口 `NO_LIVE_API` 覆盖清单

## 快照与总声明

本清单首次核实于 `2026-09-03T11:18:26+08:00`（项目统一 `Asia/Shanghai` 口径），依据提交 `c59b8bc16041df97cad8baa7650b5f211a846870`。本轮网络执行点粒度复核于 `2026-09-04T16:52:43+08:00`，`audited_code_sha=714ccdd6c25e13c85187a2260807cefa04c958d4`。它是该审计提交点的静态快照；`source_profiles`、新增适配器或任一 gateway 实现发生变化后，必须重新核实。

> `NO_LIVE_API` 目前是 SMTP 与 PushPlus 公共发送 gateway 的硬门，不是全局网络防火墙。设置该变量不保证进程内不会发生其他真实外部连接。

上一笔 PushPlus gateway 的 PA 部署闭环由维护者确认：Web 已 Reload，页面信标为 `build c59b8bc`，启动时刻对应本次 Reload。该项证据等级为 `user_reported`，不是仓库代码可独立证明的运行事实。

## 范围完备性扫描

扫描范围是该快照下全部受 Git 跟踪的生产 Python 与可执行运维 Python，包括根模块、`sources/` 和 `scripts/`；主扫描覆盖 `httpx`、`requests`、`smtplib`、SerpAPI SDK、`urllib.request`、`socket.create_connection`，并复核 Client/Session/request 形态、第三方 SDK 调用及外部 URL 与调用点的对应关系。测试文件不进入主表；测试中的 mock 目标、socket denial 和合成 URL 也不作为生产出口。

范围完备性扫描表按可区分的网络执行点记录；现役网络路径表按逻辑 service_id 汇总；完整的 gateway 级机器事实源由后续网络出口漂移合同建立。

| gateway_id | file | scope | primitive | endpoint_or_operation | static_callsite_count | runtime_attempt_semantics | active_status | call_path | credential_source |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `smtp_email.transport` | `email_notifier.py` | `send_email` | `smtplib.SMTP_SSL` / `smtplib.SMTP` / `starttls` / `login` / `sendmail` / `quit` | `SMTP transport lifecycle` | `6` | SSL 分支构造客户端后依次登录、发送并退出；非 SSL 分支额外执行 STARTTLS；任一步失败可提前终止后续操作 | `active_operational` | `main` 或 Web 通知调用方 -> `send_email` | `SMTP_USER`、`SMTP_PASS` 及 SMTP 配置环境变量 |
| `pushplus.delivery` | `notifier.py` | `_post_pushplus` | `httpx.post` | `POST https://www.pushplus.plus/send` | `1` | 公共 `send` 每次调用该静态点一次；结构化内容返回 `None` 且最小内容不同才会再次调用，故运行时至多两次 | `active_operational` | 公共 `send` -> 私有 `_post_pushplus` | `PUSHPLUS_TOKEN` |
| `pa.subscription_download` | `sync_subscriptions.py` | `download_remote_subscriptions` | `httpx.get` | `GET PythonAnywhere Files API` | `1` | 有用户与 token 时每次同步至多一次；缺配置时不发请求 | `active_operational` | `main.run` -> `sync_subscriptions` -> download；脚本直接入口也可调用 | `PYTHONANYWHERE_TOKEN`、`PYTHONANYWHERE_USER` |
| `pa.payload_upload` | `main.py` | `_upload_payload_to_pythonanywhere` | `httpx.post` | `POST PythonAnywhere Files API` | `1` | httpx、用户与 token 均可用时每次 payload 保存至多一次；缺配置时不发请求 | `active_operational` | `_deliver_notification` -> `_save_result_for_page` -> upload | `PYTHONANYWHERE_TOKEN`、`PYTHONANYWHERE_USER` |
| `juhe.flight_query` | `sources/juhe_source.py` | `JuheSource.fetch` | `requests.get` | `GET /flight/query` | `1` | 通过 source 前置条件后每次 `fetch` 一次；缺 key、缓存复用或上游跳过时为零 | `active_operational` | route-aware source profile -> `FlightAggregator` -> `cached_fetch` -> `fetch` | `JUHE_FLIGHT_KEY` |
| `serpapi.google_flights` | `sources/serpapi_source.py` | `SerpAPISource.fetch` | `GoogleSearch.get_dict` | `GET SerpAPI Google Flights search` | `1` | 有解析后的 key 且进入 source 时每次 `fetch` 一次；缺 key 或上游跳过时为零 | `active_operational` | eligible route/cabin source profile -> `FlightAggregator` -> `cached_fetch` -> `fetch` | `SERPAPI_KEY_ALIASES` 经 `resolve_serpapi_key` |
| `duffel.offer_request` | `sources/duffel_source.py` | `DuffelSource.fetch` | `httpx.post` | `POST /air/offer_requests` | `1` | 成功构造 source 并进入 `fetch` 后每次一次；缺 token 时构造阶段拒绝 | `active_operational` | enrichment source profile -> `FlightAggregator` -> `cached_fetch` -> `fetch` | `DUFFEL_TOKEN` |
| `serpapi.capability_audit` | `scripts/serpapi_capability_audit.py` | `run_audit` | injectable `requests.get` | `GET /search.json` | `1` | 默认零请求；显式 `--execute` 且全部 guard 通过后，经济舱与商务舱计划各调用该静态点一次，硬上限为两次 | `active_operational` | 显式 `--execute` -> manual-live guard -> 每项审计尝试 | `SERPAPI_KEY_ALIASES` 经 `resolve_serpapi_key` |
| `cabin.capability_audit` | `scripts/cabin_capability_audit.py` | `run_audit` | injectable `requests.get` / `requests.post` | `GET /flight/query and POST /air/offer_requests` | `2` | 默认零请求；显式 `--execute` 且 guard 通过后，每个去重后的显式选择源调用对应静态点一次，硬上限合计两次 | `active_operational` | 显式 `--execute` -> manual-live guard -> 每个显式选择源一次 | `JUHE_FLIGHT_KEY`、`DUFFEL_TOKEN` |
| `hasdata.google_flights` | `sources/hasdata_source.py` | `HasDataSource.fetch` | `httpx.get` | `GET /scrape/google/flights` | `1` | 每次直接 `fetch` 一次；已退出 route-aware profile，默认生产路线不会调度 | `retired` | 已退出 route-aware profile；兼容构造路径和直接调用能力仍在 | `HASDATA_KEY` |
| `searchapi.primary_query_auth` | `sources/searchapi_source.py` | `SearchAPISource.fetch` | `httpx.get` | `GET /api/v1/search (query api_key)` | `1` | 每次直接 `fetch` 先执行一次 query-parameter 认证主请求 | `inactive_by_profile` | 不在当前 route-aware profile；兼容构造路径和直接调用能力仍在 | `SEARCHAPI_KEY` |
| `searchapi.header_auth_fallback` | `sources/searchapi_source.py` | `SearchAPISource.fetch` | `httpx.get` | `GET /api/v1/search (Bearer header fallback)` | `1` | 仅主请求返回 400、401 或 403 时，在等待后执行一次 Bearer-header fallback | `inactive_by_profile` | 不在当前 route-aware profile；兼容构造路径和直接调用能力仍在 | `SEARCHAPI_KEY` |
| `skyscanner.flight_search` | `sources/skyscanner_source.py` | `SkyscannerSource.fetch` | `httpx.get` | `GET /flights/searchFlights` | `1` | 出发地与目的地都解析到 airport ID 后每次 `fetch` 一次；任一解析失败则不执行 | `inactive_by_profile` | 不在当前 route-aware profile；兼容构造路径和直接调用能力仍在 | `RAPIDAPI_KEY` |
| `skyscanner.airport_lookup` | `sources/skyscanner_source.py` | `SkyscannerSource._get_airport_id` | `httpx.get` | `GET /flights/searchAirport` | `1` | `fetch` 对出发地和目的地各调用 helper 一次；已知机场零请求，未知机场按 IATA 与 fallback 查询词循环并在精确匹配时提前返回；当前映射每个 helper 至多三次、一次 `fetch` 至多六次 | `inactive_by_profile` | 不在当前 route-aware profile；兼容构造路径和直接调用能力仍在 | `RAPIDAPI_KEY` |
| `travelpayouts.prices_for_dates` | `sources/travelpayouts_source.py` | `TravelpayoutsSource.fetch` | `httpx.get` | `GET /aviasales/v3/prices_for_dates` | `1` | 每次直接 `fetch` 在第一个独立 `try` 中调用一次 | `inactive_by_profile` | 不在当前 route-aware profile；兼容构造路径和直接调用能力仍在 | `TRAVELPAYOUTS_TOKEN` |
| `travelpayouts.direct_prices` | `sources/travelpayouts_source.py` | `TravelpayoutsSource.fetch` | `httpx.get` | `GET /v1/prices/direct` | `1` | 每次直接 `fetch` 在第二个独立 `try` 中调用一次；第一个端点失败也不会跳过本端点 | `inactive_by_profile` | 不在当前 route-aware profile；兼容构造路径和直接调用能力仍在 | `TRAVELPAYOUTS_TOKEN` |

`static_callsite_count` 是对应记录中 AST 可见的网络操作调用节点数，不等于一次业务调用必然发生的请求数；条件 fallback、循环和 helper 重复调用由 `runtime_attempt_semantics` 单独说明。该表仍有有意的服务级合并：SMTP 一行合并客户端构造、TLS、登录、发送与退出操作；`cabin.capability_audit` 一行合并 Juhe GET 与 Duffel POST。因此不能宣称整份文档已经 gateway 粒度化。

扫描排除项经过语义复核：`scripts/ui_smoke.py` 的 `socket` 与 `urllib.request.urlopen` 只访问临时回环服务器；`collection_singleflight.py` 的 `socket` 只读取主机标识，互斥本体是本地文件锁；`scripts/snapshot_run.py` 注入离线 `httpx.post` 替身；静态资料 URL 与生成给用户的预订链接不是本进程网络执行点。未发现 `socket.create_connection` 或额外 Client/Session 网络调用。

测试文件另行扫描后未发现直接持有真实网络调用的异常项；已有 `test_test_module_entrypoint_safety.py::TestModuleLiveEntrypointSafetyTest::test_repository_has_zero_test_module_live_entrypoint_debt` 也在本快照通过。这个结论只覆盖当前仓库测试代码，不推断未来新增测试或仓库外程序。

## 现役网络路径

| service_id | direction | gateway_function / network_primitive | gate_status / gate_location | bypass_paths | failure_semantics / behavior_delta | operational_controls | runtime_contracts | evidence_basis | evidence_level |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `smtp_email` | SMTP | `email_notifier.send_email` / `SMTP_SSL` 或 `SMTP` | `gateway_enforced`；`send_email` 第一条业务分支精确判断 effective `NO_LIVE_API == "1"` | 未发现绕开 `send_email` 的生产 SMTP 构造点；尚无与 PushPlus 同等级的全仓 SMTP 调用图合同 | 命中门时在配置解析、凭据读取、MIME 构造和连接前返回 `False`；其他值保持原发送语义 | 缺收件人或凭据时前置拒绝；连接异常转 `False` | `test_email_no_live_api_sink.py` 锁定首分支、精确值矩阵、配置前短路及正常 SSL/非 SSL 行为 | `email_notifier.py:123-188`；对应 sink 合同测试 | `direct_code_evidence` + `direct_contract_evidence` |
| `pushplus` | POST | `notifier.send` -> `_post_pushplus` / `httpx.post` | `gateway_enforced_with_call_graph`；公共 `send` 第一条业务分支精确判断 effective `NO_LIVE_API == "1"` | 调用图合同确认仓库内 PushPlus URL 和 `httpx` 出口只在 `_post_pushplus`，且私有 gateway 只由 `send` 调用 | 命中门时在 token 读取、内容处理和 POST 前返回 `False`；其他值保持原发送及最小模板重试语义 | 缺 token 时仅写本地通知日志；发送失败按原路径降级 | `test_pushplus_no_live_api_sink.py` 锁定首分支、精确值矩阵、零出口和私有 gateway 调用图 | `notifier.py:206-224,1238-1275`；对应 gateway 合同测试 | `direct_code_evidence` + `direct_contract_evidence` |
| `pa_subscription_download` | GET | `sync_subscriptions.download_remote_subscriptions` / `httpx.get` | `absent`；函数及其网络调用前无 `NO_LIVE_API` 判断 | `sync_subscriptions.py` 可直接执行下载，再调用采集；正常 `main.run` 路径也会下载 | `NO_LIVE_API` 对该 GET 无直接行为差异；缺 token 返回空列表，HTTP 错误向上抛，由上游运行路径处理 | 正常采集路径受 collection single-flight；只新增合并规则限制写入影响 | 无出口级 `NO_LIVE_API` runtime contract | `sync_subscriptions.py:136-180,207-215`；`main.py` single-flight 调用链 | `direct_code_evidence` |
| `pa_payload_upload` | POST | `main._upload_payload_to_pythonanywhere` / `httpx.post` | `absent`；函数及其网络调用前无 `NO_LIVE_API` 判断 | 由详情 payload 保存路径直接进入；没有统一 PA Files gateway | `NO_LIVE_API` 对该 POST 无直接行为差异；缺库或 token 返回 `False`，HTTP 错误由 `_save_result_for_page` 捕获，本地保存仍成功 | 本地 payload 先落盘；上游通知流程测试可 mock 上传 | 无出口级 `NO_LIVE_API` runtime contract | `main.py:1004-1057` | `direct_code_evidence` |
| `juhe` | GET | `JuheSource.fetch` / `requests.get` | `upstream_controls_only`；source gateway 无 `NO_LIVE_API` 判断 | `JuheSource.fetch` 可被直接调用；`scripts/cabin_capability_audit.py` 还直接使用 `requests.get`，不经过该 source 方法 | `NO_LIVE_API` 对 source 方法无直接行为差异；缺凭据或前置条件不满足时返回跳过结果；异常由缓存层归类 | 正常路径有 source preflight、缓存、配额台账严格读取、每次尝试即时记账、single-flight、一次 I/O 重试上限；manual-live 路径另有显式 guard 和调用上限 | 上游与审计测试提供隔离，不是出口级 runtime contract | `sources/juhe_source.py:223-263`；`request_cache.py:256-269,788-1034`；manual-live audit | `direct_code_evidence` + `direct_contract_evidence` |
| `serpapi` | SDK | `SerpAPISource.fetch` / `GoogleSearch.get_dict()` | `upstream_controls_only`；source gateway 无 `NO_LIVE_API` 判断 | `SerpAPISource.fetch` 可被直接调用；`scripts/serpapi_capability_audit.py` 还直接使用 `requests.get`，不经过该 source 方法 | `NO_LIVE_API` 对 source 方法无直接行为差异；缺凭据返回未配置结果；SDK 异常由缓存层归类 | 正常路径有 profile 选择、缓存、配额台账严格读取、每次尝试即时记账、single-flight；manual-live audit 默认 dry-run，真实模式有 guard、预算和逐尝试记账 | 上游与审计测试提供隔离，不是出口级 runtime contract | `sources/serpapi_source.py:51-103`；`request_cache.py:256-269,788-1034`；manual-live audit | `direct_code_evidence` + `direct_contract_evidence` |
| `duffel` | POST | `DuffelSource.fetch` / `httpx.post` | `upstream_controls_only`；source gateway 无 `NO_LIVE_API` 判断 | `DuffelSource.fetch` 可被直接调用；`scripts/cabin_capability_audit.py` 还直接使用 `requests.post`，不经过该 source 方法 | `NO_LIVE_API` 对 source 方法无直接行为差异；缺凭据在构造阶段失败；请求异常由缓存层归类 | 正常路径有 profile 限定、缓存、配额台账严格读取、每次尝试即时记账、single-flight；manual-live 路径另有 guard 和调用上限 | 上游与审计测试提供隔离，不是出口级 runtime contract | `sources/duffel_source.py:58-85`；`request_cache.py:256-269,788-1034`；manual-live audit | `direct_code_evidence` + `direct_contract_evidence` |

`gate_status` 描述出口函数自身是否执行 `NO_LIVE_API` 短路；`operational_controls` 描述调用链上的其他约束。二者不能互换：single-flight、配额预检、缓存或台账存在，并不把一个无环境变量判断的出口升级为 `gateway_enforced`。

## 非现役或退役适配器

| adapter | active_status | 不在现役路径的依据 | 仍可直接 import / 实例化 / 调用 | `NO_LIVE_API` 门 | 当前调用方 | evidence_level |
| --- | --- | --- | --- | --- | --- | --- |
| HasData | `retired` | `source_profiles.py` 将其登记为退役元数据，不再列入 route-aware listing sources | 是；类与 `fetch` 实现仍在 | 无 | 当前跟踪的 route-aware 生产调用方为 0；无 route context 的兼容构造分支仍保留 | `direct_code_evidence` |
| SearchAPI | `inactive_by_profile` | 不在当前 route-aware source profile | 是；类与 `fetch` 实现仍在 | 无 | 当前跟踪的 route-aware 生产调用方为 0；兼容构造分支仍保留 | `direct_code_evidence` |
| Skyscanner | `inactive_by_profile` | 不在当前 route-aware source profile | 是；类与 `fetch` 实现仍在 | 无 | 当前跟踪的 route-aware 生产调用方为 0；兼容构造分支仍保留 | `direct_code_evidence` |
| Travelpayouts | `inactive_by_profile` | 不在当前 route-aware source profile | 是；类与 `fetch` 实现仍在 | 无 | 当前跟踪的 route-aware 生产调用方为 0；兼容构造分支仍保留 | `direct_code_evidence` |

“当前调用方为 0”只指本快照中已跟踪的 route-aware 生产调用链。`sources/aggregator.py:649-721` 仍保留无 route context 的兼容构造路径，因此“不在默认 profile”绝不等于“代码不可执行”。

## 控制层分类

| layer | 本快照中的控制 | 能证明什么 | 不能证明什么 |
| --- | --- | --- | --- |
| `prevention` | SMTP/PushPlus 出口级 `NO_LIVE_API` 门；测试 mock/spy；专项 socket denial；缺凭据前置拒绝 | 在各自明确覆盖的入口和条件下，网络构造或发送不会发生 | SMTP/PushPlus 门不能阻止 PA 或供应商 source；mock 与 socket denial 只保护执行它们的测试进程 |
| `containment` | collection single-flight；配额预检；请求缓存；manual-live 调用次数上限 | 缩小并发、预算和重复请求的范围 | 这些控制不是出口门；通过控制后仍可能真实联网 |
| `detection` | `api_usage` 台账；五个状态文件前后 SHA；mock 调用计数；日志和审计报告 | 发现或对账调用与状态变化 | detection 只能观察，不能阻止已经发起的外部调用 |

## runtime_contracts 与 documentation_contract

| contract | 实际守护的主张 | 不守护的主张 |
| --- | --- | --- |
| SMTP `runtime_contracts` | `test_email_no_live_api_sink.py` 证明 effective 值精确为 `"1"` 时，`email_notifier.send_email` 在配置解析及 SMTP 构造前返回 `False`；非 `"1"` 值继续原语义 | 不证明其他 sink 受保护，也不建立 SMTP 以外的调用图 |
| PushPlus `runtime_contracts` | `test_pushplus_no_live_api_sink.py` 证明 effective 值精确为 `"1"` 时，公共 `notifier.send` 在 token 读取及 POST 前返回 `False` | 不证明 PA、供应商 source 或仓库外调用受保护 |
| PushPlus 私有 gateway 调用图合同 | 同一测试文件以 AST 锁定仓库内 PushPlus URL / `httpx` 出口只位于 `_post_pushplus`，且 `_post_pushplus` 只由公共 `send` 调用 | 只覆盖仓库内 Python；不覆盖外部 WSGI 配置、计划任务、其他语言或未来未扫描文件 |
| 本文 `documentation_contract` | `test_docs_accuracy.py::DocsAccuracyTest::test_external_network_no_live_api_coverage_contract` 锁定文档存在、七个 service ID、章节边界、缺口和“非全局防火墙”声明 | 它只防止文档结构静默退化，不会在运行时阻止网络 |

证据等级在本文中严格区分：源码与调用图观察为 `direct_code_evidence`；自动化合同实际断言为 `direct_contract_evidence`；本轮 GitHub run 等现场执行结果为 `runtime_observed`；维护者现场确认是 `user_reported`；当前证据面无法核实的事项标为 `unverifiable`。每个结论同时保留 `evidence_basis`，不使用笼统的 `direct_evidence` 混写不同证据面。

## 当前缺口

- **PA 订阅下载**：当前无门，依赖上游控制及调用方测试隔离。需要另案决定是在函数首部还是统一 PA Files gateway 建立严格短路。
- **PA payload 上传**：当前无门，依赖上游控制及调用方测试隔离。其“本地保存成功、远端同步失败可降级”语义需要在加门时保持。
- **Juhe**：当前无门，依赖上游控制及调用方测试隔离。正常 source 路径与 manual-live 直接路径都需纳入未来调用图裁决。
- **SerpAPI**：当前无门，依赖上游控制及调用方测试隔离。SDK source 与直接 HTTP audit 是两个执行路径。
- **Duffel**：当前无门，依赖上游控制及调用方测试隔离。source adapter 与 manual-live audit 都可构造 POST。

## 后续待办

是否以及何时为上述五个缺口建立出口门，由维护者排期，证据等级为 `user_reported`。后续工作应先裁决各函数既有失败返回语义、导入时环境装载、调用图和 manual-live 旁路，再分 sink 建立运行合同；不能把某个计划日期写成源码事实，也不能一次性宣称 `NO_LIVE_API` 已成为全局防火墙。

## 已知边界

- **process / `.env` / effective**：模块 import 时可能调用 dotenv，把 `.env` 中的值补入 `os.environ`。因此 shell process 未显式设置，不代表运行时 effective 值一定不是 `"1"`；判断必须以实际进程中的 effective 值为准。
- **import 与 patch target**：`from module import name` 会在 import 时绑定本地名称；测试 patch target 必须指向调用方运行时查找位置。延迟 import、模块属性访问和已绑定名称不能混为一谈。
- **仓库外配置**：外部 WSGI、计划任务和私人启动方式不由仓库内 AST 完全证明；没有现场证据时应标 `unverifiable`，不能由当前调用图外推。
- **调用图范围**：PushPlus 私有 gateway 合同只覆盖仓库内 Python 代码；它不覆盖其他语言、外部脚本或未纳入仓库的调用方。
- **运行态证据**：基线 GitHub run `33653130361` 的三个 required jobs 在该快照上为成功，属于 `runtime_observed`；它证明测试执行结果，不证明五个缺口具有运行时网络门。
