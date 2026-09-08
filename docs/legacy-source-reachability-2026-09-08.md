# 退役／未启用源的可实例化边界

本文是边界说明与证据索引，不复制调用库存，不新增扫描或实例化验证。

## 边界结论

### 现役调度边界

在 `audited_code_sha` 对应代码、当前 profile 及已登记的标准定时订阅 / Web 触发 / 篮子路径下，仅保留四个旧密钥，不会使对应适配器重新进入现役源集合。依据第一阶段取证及下列第二阶段合同：受控入口参数、真实工厂构造计数、定向调用结构。此结论不扩大到任意仓外调用或未来修改后的代码。

### 兼容能力边界

无有效 route context 的兼容工厂及私有按名称工厂 `_instantiate_source(name)` 仍存在。在对应环境变量可用、依赖可导入且构造成功的条件下，这些入口仍可能实例化对应适配器。实例化不等于已执行 `fetch`、已产生网络请求，也不等于远端凭据或服务已验证可用。依据第一阶段取证 A1/A5 及 `sources/aggregator.py:565`、`:685` 的条件构造代码。

## 分支条件

合法机场上下文或合法显式 `route_type` 均可进入 profile 分支；缺少 origin/dest 本身不等于进入兼容分支。只有无法解析出合法 route context 时，才执行按环境变量构造的兼容分支。route-aware 分支即使未构造出任何可用现役源也直接返回，不自动回退兼容分支。

源码事实：`sources/aggregator.py:657` 解析上下文，`:665` 进入 profile，`:683` 直接返回，`:685` 才是兼容分支。受控测试覆盖：`ActiveFactoryMatrixTest.test_three_profiles_times_three_key_states_and_explicit_only_context`（文件见证据索引），含机场上下文、无机场但合法显式 route type、仅旧密钥和现役密钥阳性对照。

## 密钥与适配器映射

| 环境变量 | 适配器 | 当前状态 |
| --- | --- | --- |
| `HASDATA_KEY` | `HasDataSource` | 未启用，且有明确退役元数据 |
| `SEARCHAPI_KEY` | `SearchAPISource` | 未启用 |
| `TRAVELPAYOUTS_TOKEN` | `TravelpayoutsSource` | 未启用 |
| `RAPIDAPI_KEY` | `SkyscannerSource` | 未启用 |

源码事实：`sources/aggregator.py:579`、`:587`、`:591`、`:595` 的四项条件构造与兼容分支映射一致。依据第一阶段扫描，仓内未发现 RapidAPISource 类；本笔不新增扫描或实例化验证来扩大该结论。

## 状态与兼容入口

HasData 有 `retired_sources` 登记及退役注释（`source_profiles.py:43`、`:79`）；其余三者仅为未启用，无同等退役元数据，不能把四者统称为已退役。依据第一阶段取证 A3 与当前 profile 源码事实。

`collector.get_aggregator` 是保留的兼容转发入口，转发可空参数，不套用现役上下文要求（`collector.py:21`）。`SourceFactoryInventoryTest.test_compatible_tests_comments_and_historical_names_are_not_active_roots` 持续保护此分类；它不证明兼容入口不能实例化旧适配器。

## 证据与快照限制

`audited_code_sha=21e03027f69d3d448d577c2cc0c7367119b4baed`；核实时刻：2026-09-08T19:03:24+08:00（Asia/Shanghai）。本笔只核对下列既有证据与所引用源码，不重跑可达性审计。

所有第二阶段测试 ID 均位于 [test_source_factory_reachability.py](../test_source_factory_reachability.py)。

| 证据类型 | 具体条目 / 测试 ID | 支持范围 |
| --- | --- | --- |
| 源码事实 | 上述工厂、profile、collector 源码位置 | 条件分支、四项映射、退役元数据及可空转发；不验证远端服务 |
| 第一阶段取证结论 | A1/A5 工厂分支与兼容残留；A3 状态；A4 标准链；B1 适配器与类名核查 | 沿用前序取证结论，不冒充本笔新增扫描 |
| 受控测试覆盖 | `ActiveFactoryMatrixTest.test_three_profiles_times_three_key_states_and_explicit_only_context` | 合法上下文、仅四旧假密钥时 search/enrichment 均为空，四旧构造计数为零；阳性场景保留 |
| 受控测试覆盖 | `ActiveFactoryMatrixTest.test_empty_result_cannot_hide_an_old_constructor_call` | 即使返回空列表，旧构造器已调用仍被拒绝 |
| 受控测试覆盖 | `ActiveRootParametersTest.test_scheduled_run_reaches_real_locked_processor_with_route_context`；`ActiveRootParametersTest.test_web_worker_reaches_real_processor_and_is_joined_before_assertions`；`ActiveRootParametersTest.test_run_basket_legacy_and_cohort_route_context_and_available_only_selection` | 这些受控定时、Web、legacy/cohort 篮子执行提供合法上下文；不证明任意运行时参数非空 |
| 受控测试覆盖 | `SourceFactoryInventoryTest.test_registered_inventory_and_private_scopes_are_exact`；`SourceFactoryInventoryTest.test_profile_reactivation_and_default_binding_changes_are_rejected`；上述 collector 分类测试 | 已登记调用数量、参数表达式、绑定及 profile 排除的结构合同；不证明任意 Python 调用方式不可达 |

第一阶段取证支撑本次快照结论；第二阶段合同持续保护其明确覆盖的调用结构与受控场景；两者均不覆盖未经审计的仓外调用方式。source profile、工厂、适配器或标准入口连接方式变化后需重新核实。

## 不改变的事实

旧 [网络出口快照](external-network-no-live-api-coverage-2026-09-03.md) 记录适配器与网络调用点仍存在，本文记录当前调度路径不构造它们，两者不矛盾。T 曲线历史输出中的 `hasdata,juhe` 期望源仍属历史质量口径，不因现役排除而修改。

本文不声称退役源已彻底移除、任意调用方式均不可达，或 `NO_LIVE_API` 覆盖了这些出口。文档存在性与内存反例合同只保护本文关键陈述和映射，不是生产故障或全进程网络防火墙的证明。
