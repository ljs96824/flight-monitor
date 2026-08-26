# PVG-KIX T曲线混合采样 v2

## 目标与边界

本方案只改变研究篮子的采样几何，不改变订阅采集、价格、推荐或通知逻辑。代码合入时
`RESEARCH_COHORT_V2=false`，在三项启用硬门全部通过前不得打开，也不得以运行
`basket_collect.py` 的方式试探。

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

## 全系统配额模拟

只读入口为 `scripts/research_quota_simulation.py`。它只构造
`CollectionPlan` 请求键并读取本地台账，不调用 `plan.execute()`，因此不会产生
外部API请求或观测写入。

输出必须同时包含：

- `basket_planned_unique`
- `basket_normal_actual`
- `basket_retry_ceiling`
- `subscription_planned_unique`
- `other_scheduled_calls`
- `combined_daily_expected`
- `combined_daily_worst_case`
- `estimated_days_remaining`

篮子是独立进程且强制新鲜，不能把与订阅相同的键当作进程内复用；两者分别计入正常值。
本机订阅轮固定在09:00、15:00、21:00，代表计划之外的两次边际调用以
`other_scheduled_calls=2` 明示。最坏值在完整正常基线上，额外计入六个研究篮子键
各一次 OSError 重试；这与本批新增调用的风险边界一致。

## 启用硬门

以下三项必须同时成立，才能由用户手工把 `RESEARCH_COHORT_V2` 置为 `true`：

1. 已生成并成功恢复验证 runtime backup，且复制到另一块物理盘或私有加密云目录，
   配置证据 `off_disk_copy=true`；
2. 上述全系统配额模拟完整输出，不能只报篮子6次；
3. observations 时间归属迁移、collection_cells 请求台账迁移和 prices round lineage
   迁移均已在生产库完成，旧数据可只读查询。时区列只属于第一项；collection_cells
   与 prices 的 round_id 列共同构成第二项，不得把缺失的台账误报成时区迁移失败。

运行入口会在 `start_request_cache_round` 之前重新检查硬门。缺一项即返回
`status=blocked`，零源调用。

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
| other_scheduled_calls | 2 |
| combined_daily_expected | 10 |
| combined_daily_worst_case | 16 |
| estimated_days_remaining | 9 |

该值来自本地台账累计余量、当日活跃订阅计划键与三个已启用订阅任务时段；
当前 Juhe 本地估算余量为94。未调用任何源。模拟前后三个生产状态文件 SHA-256
均未变化。

提交1时间迁移审计为 ready。提交2显式执行
`migrate_round_lineage.py --section collection_cells --write` 后，
`observations` 历史行数保持 86,227，新增 `collection_cells` 为0行；prices三表
`round_id` 列已存在且历史值保持 NULL。由此迁移与旧数据可读硬门已通过。

当前唯一未通过项为 `off_disk_copy`。因此配置仍为
`RESEARCH_COHORT_V2=false`，不得自动启用；待用户完成异盘或私有加密云复制并把
`research_cohort_v2_gates.off_disk_copy` 明确置为 `true` 后，再由用户手工启用。
