# 按工作负载分类的研究储备（2026-08-27）

## 目的

旧储备把定时用户监控、研究篮子、人工活体验证和 canary 混在同一日聚合中。人工验证
高峰会被当成未来每天都会重复的固定负担，因而把用户监控储备抬到超过剩余额度。本策略
只修配额估算口径，不改变采集计划、用户监控优先级、源调用次数或任何历史台账记录。

## 入账合同

每条新 `api_usage.entries` 必须在真实调用入口携带以下 `workload_class` 之一：

| class | 来源 | 配额处理 |
|---|---|---|
| `scheduled_user_monitor` | 定时批量订阅轮 | 进入用户监控储备 P90 |
| `research_cohort` | 研究篮子 | 走研究预算 |
| `manual_live` | Web 手动触发 | 不进 P90，受 30 次人工缓冲约束 |
| `canary` | `basket_collect.py --canary` | 独立披露，不进 P90 |
| `unknown` | 无法证明来源或历史记录 | 按下述冷启动日型处理 |

重试继承原请求的 class，不另立类别。入口同时记录 `entrypoint` 供审计；class 由调用链
显式传入，不从 `round_id` 或时间事后猜测。历史 entry 不回填、不改写；缺少字段时只在
内存中解释为 `unknown`。

## 七日窗口与日型

窗口始终是最近 7 个已经结束的 Asia/Shanghai 完整自然日，当天不进入估算。每个来源的
每日样本按 entry 证据分为四类：

| 日型 | 证据 | P90样本值 | 计入冷启动退出进度 |
|---|---|---:|---|
| `fully_classified` | 当日该源全部实际调用 entry 均有明确 class | 实测 `scheduled_user_monitor` | 是 |
| `pure_unknown` | 有实际记录且全部为 unknown | 每日下限10 | 否 |
| `mixed` | 同日兼有已分类与 unknown | `max(scheduled + unknown实际量, 10)` | 否 |
| `telemetry_missing` | 当日无该源台账记录 | 每日下限10 | 否 |

`dates` 日聚合中没有对应 entry 的调用量也视作 unknown。缺失或非法 class 不会被猜测为
定时用户轮。只有连续窗口内 7 天全部为 `fully_classified` 才退出冷启动；任一 unknown、
混合或缺失日都会继续保持冷启动，防止台账损坏造成错误放行。

## 储备公式

```text
observed_raw_p90 = nearest_rank_P90(最近7个完整日的日型样本值)
effective_scheduled_p90 = max(observed_raw_p90, scheduled_daily_floor=10)
monitoring_reserve = ceil(effective_scheduled_p90 * 距2026-10-01剩余天数 * 1.2) + 30
research_available = current_remaining - monitoring_reserve
```

`research_available` 保留负值供诊断，不用零截断掩盖缺口。30 次是人工活体验证缓冲，
也是一个完整研究批次（6 请求 × 5 个有效日）的最低启动额度。测试由公式推导期望值，
禁止把某个日期下恰好得到的具体储备金额冻结成政策常量。

## 冷启动披露

冷启动是估算政策，不是历史数据迁移。readiness 必须同时输出：

`reserve_window_days`、`fully_classified_days`、`pure_unknown_days`、`mixed_days`、
`telemetry_missing_days`、`observed_raw_p90`、`effective_scheduled_p90`、
`scheduled_daily_floor`、`cold_start_active`、`cold_start_reason`、
`cold_start_estimated`、`cold_start_exit_condition`、`cold_start_expected_exit_at`、
`monitoring_reserve`、`research_available`。

`cold_start_expected_exit_at` 根据窗口末尾已经连续形成的完全分类日动态计算，并假设后续
每天都能形成完整分类证据；它是最早可能退出日，不是承诺。冷启动时固定披露：

```text
冷启动期:最近7个完整日尚未形成完整工作负载分类,其中X日为历史unknown;储备暂按每日10次下限估算,非实测结论。连续获得7个完整分类日后自动退出该规则。
```

## 人工与 Canary 储备纪元

`reserve.epoch_started_at` 定义人工活体验证与 canary 缓冲的消费纪元。readiness 同时披露
两类调用的 `lifetime`、`in_epoch` 和 `buffer_remaining`；硬门与自动暂停只消费
`in_epoch`，因此数月前的开发验证不会永久阻断当前研究批次。当前纪元从
`2026-08-27T15:39:15+08:00`（workload 分类正式上线）开始，人工缓冲为30次，canary
缓冲为12次。

这是一条估算政策，不是历史回填：旧 entry 原样保留，`lifetime` 永不归零。纪元切换只
改变 `in_epoch` 的统计起点。缺时间戳的旧 entry 若日期早于纪元日可明确排除；同日或
日期也不可判定时保守计入当前纪元，防止低估真实消耗。旧配置缺少纪元字段时维持兼容，
把全部历史视作当前纪元，直到管理员显式配置新纪元。

## 自动暂停

以下任一条件成立，只暂停研究采样，用户监控继续：

1. `remaining <= monitoring_reserve`；
2. `research_available < 30`，不启动下一批；
3. 最近两个完整上海日的 `scheduled_user_monitor` 均大于 12；
4. 当前储备纪元内 `manual_live` 累计大于 30；
5. 当前储备纪元内 `canary` 累计大于 12；
6. `ledger_degraded` 时沿用既有合同，本轮不推进 `probe_valid_n`。

停用状态写入研究运行态并只通知一次。恢复必须由人复核 readiness 后显式处理；任何暂停
都不改变用户订阅轮执行权。研究执行权由 `RESEARCH_BASKET_ENABLED` 与
`RESEARCH_BASKET_STRATEGY` 显式控制；关闭态不回退 legacy，也不修改 cohort 序列。

当前分支基于轮末汇总台账。此前独立开发中的“真实调用返回后立即入账”改动合并时，
必须把同一轮上下文中的 `workload_class` 与 `entrypoint` 原样传给逐调用写入函数；不得
退回默认 `unknown`。
