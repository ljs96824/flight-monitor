# 按工作负载分类的研究储备（2026-08-27）

## 目的

旧储备把定时用户监控、研究篮子、人工活体验证和 canary 混在同一日聚合中。人工验证
高峰会被当成未来每天都会重复的固定负担，因而把用户监控储备抬到超过剩余额度。本次
只修配额证据口径，不改变任何采集计划、用户监控优先级或源调用次数。

## 入账合同

每条新 `api_usage.entries` 必须在真实调用入口携带以下 `workload_class` 之一：

| class | 来源 | 配额处理 |
|---|---|---|
| `scheduled_user_monitor` | 定时批量订阅轮 | 进入用户监控储备 P90 |
| `research_cohort` | 研究篮子 | 走研究预算 |
| `manual_live` | Web 手动触发 | 不进 P90，受 30 次人工缓冲约束 |
| `canary` | `basket_collect.py --canary` | 独立披露，不进 P90 |
| `unknown` | 无法证明来源或历史记录 | 保守进入用户监控储备 |

重试继承原请求的 class，不另立类别。入口同时记录 `entrypoint` 供审计；class 由调用链
显式传入，不从 `round_id` 或时间事后猜测。历史 entry 不回填、不改写；缺少字段时只在
内存中解释为 `unknown`。

## 储备公式

使用最近 7 个完整上海自然日，不含正在进行的当天：

```text
reserve_basis(day) = scheduled_user_monitor(day) + unknown(day)
scheduled_daily_p90 = max(nearest_rank_P90(reserve_basis), 10)
monitoring_reserve = ceil(scheduled_daily_p90 * 距2026-10-01剩余天数 * 1.2) + 30
research_available = current_remaining - monitoring_reserve
```

`research_available` 保留负值以便诊断，不再用零截断掩盖缺口。30 次是人工活体验证缓冲，
也是一个完整研究批次（6 请求 × 5 个有效日）的最低启动额度。

## 自动暂停

以下任一条件成立，只暂停研究采样，用户监控继续：

1. `remaining <= monitoring_reserve`；
2. `research_available < 30`，不启动下一批；
3. 最近两个完整上海日的 `scheduled_user_monitor` 均大于 12；
4. 新分类台账中的 `manual_live` 累计大于 30；
5. `ledger_degraded` 时沿用既有合同，本轮不推进 `probe_valid_n`。

停用状态写入研究运行态并只通知一次。恢复必须由人复核 readiness 后显式处理；任何暂停
都不改变用户订阅轮执行权。

## 冷启动与历史限制

历史记录按合同一律是 `unknown`，因此上线后的前 7 个完整日可能仍被过去人工高峰抬高。
这是保守证据，不是算法失效。随着窗口逐日被显式分类的新 entry 替换，manual/canary
自然退出 P90。readiness 逐日打印 `scheduled`、`unknown` 与 `reserve_basis`，不得为追求
约 450 的预期值而按 round 名称猜测或回填历史。

当前分支基于轮末汇总台账。此前独立开发中的“真实调用返回后立即入账”改动合并时，
必须把同一轮上下文中的 `workload_class` 与 `entrypoint` 原样传给逐调用写入函数；不得
退回默认 `unknown`。
