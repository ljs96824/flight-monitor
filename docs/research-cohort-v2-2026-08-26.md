# PVG-KIX T曲线混合采样 v2

## 目标与边界

本方案只改变研究篮子的采样几何，不改变订阅采集、价格、推荐或通知逻辑。执行与策略
分开配置：`RESEARCH_BASKET_ENABLED=false` 时零请求、零状态写入；启用后仅按
`RESEARCH_BASKET_STRATEGY=cohort_v2|legacy` 选择显式策略。旧键
`RESEARCH_COHORT_V2=true` 只作一期兼容迁移，等价于启用 `cohort_v2`，并打印迁移日志。

## 六槽结构

六槽总数包含两个固定锚点和四个滚动探针，不在其上叠加旧篮子：

| 槽 | 角色 | 日期或T序列 |
|---|---|---|
| anchor_normal | trajectory_anchor | 2026-09-08 |
| anchor_holiday | trajectory_anchor | 2026-10-01 |
| probe_1 | cross_sectional_probe | 7 → 10 → 3 → 5 |
| probe_2 | cross_sectional_probe | 14 → 21 → 17 → 24 |
| probe_3 | cross_sectional_probe | 28 → 35 → 49 → 63 |
| probe_4 | cross_sectional_probe | 42 → 56 → 70 → 84 |

探针仅在日格状态为 `valid` 时累计 `probe_valid_n`；`failed`、`empty`、
`degraded`、`missing` 和配额跳过均不计数。达到5个有效样本后切换同槽的下一个
目标T；序列用尽后停止该槽，不循环重采。

固定锚点在 `today < depart_date` 时继续采，在 `today == depart_date` 时先采
T=0再完成，在 `today > depart_date` 时停止，绝不续到 `today+60`。

## 去重优先级

日期冲突优先级固定为：

1. 固定轨迹锚点；
2. 正在运行的用户监控日期；
3. 横截面探针。

探针撞固定锚点或用户监控日期时不增加请求，立即切换该槽的下一目标T，并在篮子状态
中记录 `deduped_with_anchor` 或 `deduped_with_user_monitor`。
`CollectionPlan` 仍以请求键做最终去重，并按
`trajectory_anchor > user_monitor > cross_sectional_probe > legacy` 保留角色。

## 暂停路线

`SHA->PEK` 与 `PVG->HKG` 的研究采样配置保留但暂停：

- reason: `quota_concentration_for_pvg_kix_tcurve`
- resume_when: `pvg_kix_core_t_points_reach_min_n`

历史 observations 不修改。

## 台账降级轮

`CollectionPlan.execute()` 返回的 `PlanExecutionReport.ledger_degraded` 是研究进度的硬闸。
该值为 `true` 时，API 与观测结果仍按事实汇总，但不调用
`apply_research_round_outcomes`，不增加 `probe_valid_n`，也不完成 T=0 锚点。篮子摘要记为
`status=partial`、`ledger_degraded=true`、`research_progress_applied=false`，研究状态的
`last_round` 同步登记该证据缺口；只有台账正常的轮次才推进研究状态。
## 全系统配额模拟

只读入口为 `scripts/research_quota_simulation.py`。它只构造
`CollectionPlan` 请求键并读取本地台账，不调用 `plan.execute()`，因此不会产生
外部API请求或观测写入。

输出必须同时包含：

- `basket_planned_unique`
- `basket_normal_actual`
- `basket_retry_ceiling`
- `subscription_planned_unique`
- `scheduled_subscription_runs_per_day`
- `subscription_daily_expected`
- `subscription_daily_worst_case`
- `other_non_subscription_calls_per_day`
- `combined_daily_expected`
- `combined_daily_worst_case`
- `estimated_days_remaining`

篮子是独立进程且强制新鲜，不能把与订阅相同的键当作进程内复用；两者分别计入正常值。
本机订阅轮固定在09:00、15:00、21:00，因此单轮订阅唯一请求数乘以
`scheduled_subscription_runs_per_day=3` 得到日预期值。该模型保守地把三轮都按完整唯一
请求计，暂不分别建模每轮面板复用与缓存新鲜度。`other_non_subscription_calls_per_day`
只容纳确有证据的非订阅、非篮子定时调用，当前为0。最坏值对篮子、三轮订阅和其他调用
都应用同一轮内重试上限；篮子与订阅即使日期相同，也因不同进程与强制新鲜语义分别计费。

## 启用硬门

以下三项必须同时成立，启用 `RESEARCH_BASKET_ENABLED=true` 且选择
`RESEARCH_BASKET_STRATEGY=cohort_v2` 后才会执行研究采样：

1. `data/backup_status.json` 证明 runtime backup 已成功隔离恢复、异盘副本 SHA 与本地归档
   一致、生产状态与副本位于不同设备，且异盘核验距今不超过
   `backup_evidence_max_age_days`（默认30天）；配置布尔与同盘不同目录都不能代替设备证据；
2. 全系统配额模拟完整输出，且 `expected_days_remaining >= 30`、
   `worst_case_days_remaining >= 20`、`remaining_after_research >= monitoring_reserve`；
3. observations 时间归属迁移、collection_cells 请求台账迁移和 prices round lineage
   迁移均已在生产库完成，旧数据可只读查询。时区列只属于第一项；collection_cells
   与 prices 的 round_id 列共同构成第二项，不得把缺失的台账误报成时区迁移失败。

运行入口会在 `start_request_cache_round` 之前重新检查硬门。缺一项即返回
`status=blocked`，零源调用。

只读 readiness 入口一次打印十一项硬门的当前值：台账健康一项、配额三项、备份四项、迁移三项。

```powershell
python -X utf8 scripts/research_readiness.py
```

备份四项分别是 `backup_restore_verified`、`off_disk_copy_verified`、
`different_device_verified` 与 `off_disk_copy_fresh`。状态文件缺失、SHA 不一致、设备证据缺失或证据过期都会明确列入“还差”，不会回退
读取 `config.yaml` 的人工布尔。该报告只读本地 JSON/SQLite 与计划键，不执行采集计划、
不调用外部 API，也不修改研究开关。

运行态锁存不再靠手工编辑 `data/basket_state.json` 恢复。只读查看开关、停用原因与
全部硬门：

```powershell
python -X utf8 scripts/research_control.py status
```

人工停用会保留原因与时刻，且不影响用户订阅监控：

```powershell
python -X utf8 scripts/research_control.py disable --reason "<原因>"
```

重新启用必须重新计算完整 readiness；任一硬门为红都会拒绝写入：

```powershell
python -X utf8 scripts/research_control.py enable --confirm ENABLE
```

状态写入统一走 `atomic_json_store.update_json()`。readiness 计算发生在文件锁外，
只有通过后的短写入进入锁内；旧配额守卫元数据进入 `quota_guard_history`，不删除审计
证据。直接改 JSON 既可能绕过硬门，也可能与采集进程竞争，不属于受支持的恢复路径。

## 版本与消费纪律

- `trajectory_anchor`、`user_monitor`、`legacy` 可进入 forecast shape；
- `cross_sectional_probe` 默认从 forecast shape 排除，避免“每个出发日只有一个观测”
  在按出发日中位数归一后伪造全平 shape；
- 原始 T 曲线可包含全部角色，但报告必须披露角色构成；
- 状态与结果以 `collection_cells` 的五态为准，不从 observation 行数猜测成功。

## 2026-08-27 本地启用审计

只读全系统模拟结果：

| 字段 | 值 |
|---|---:|
| basket_planned_unique | 6 |
| basket_normal_actual | 6 |
| basket_retry_ceiling | 12 |
| subscription_planned_unique | 2 |
| scheduled_subscription_runs_per_day | 3 |
| subscription_daily_expected | 6 |
| other_non_subscription_calls_per_day | 0 |
| combined_daily_expected | 12 |
| combined_daily_worst_case | 24 |
| estimated_days_remaining | 7 |

该表按当前三轮模型重算；旧模型曾把三轮订阅误写成“单轮唯一请求+固定2次”，得到
10/16/9天，现已废弃。新值来自本地台账累计余量、当日活跃订阅计划键与三个已启用
订阅任务时段；
当前 Juhe 本地估算余量为94。未调用任何源。模拟前后三个生产状态文件 SHA-256
均未变化。

提交1时间迁移审计为 ready。提交2显式执行
`migrate_round_lineage.py --section collection_cells --write` 后，
`observations` 历史行数保持 86,227，新增 `collection_cells` 为0行；prices三表
`round_id` 列已存在且历史值保持 NULL。由此迁移与旧数据可读硬门已通过。

上表是充值前的历史审计快照，已由
`docs/juhe-quota-epoch-2026-08-27.md` 的 1,100 次买断纪元与动态储备口径取代，禁止继续
用“550减全历史累计”判断余量。关闭研究必须设置 `RESEARCH_BASKET_ENABLED=false`；
关闭态不会回退到 legacy 篮子。启用后仍由 readiness 硬门决定是否实际执行。
