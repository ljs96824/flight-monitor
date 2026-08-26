# Collection ledger 与 round lineage 迁移

本次 schema 变化分成两个互不依赖的区段，历史数据不做时间近似回填。

## 区段 A：observations.sqlite3

新增 `collection_cells`。每个计划内唯一请求先写 `planned`，随后转为
`running` 与终态；轮末仍停留在 `planned/running` 的记录转为
`interrupted`。`sample_role` 的历史缺席按 `legacy` 消费。

```powershell
python -X utf8 scripts\migrate_round_lineage.py --section collection_cells
python -X utf8 scripts\migrate_round_lineage.py --section collection_cells --write
```

回滚：在确认不再有新代码写入后可 `DROP TABLE collection_cells`；生产处置优先
从迁移前已验证备份恢复，避免误删后来积累的执行证据。

## 区段 B：prices.db

`flight_details`、`roundtrip_price_history`、`push_snapshots` 各新增可空
`round_id`。新记录从当前采集 ContextVar 精确取值；无上下文保持 `NULL`。
历史记录保持 `NULL`，禁止按 `snapshot_time` 猜测归属。该区段只由下列
显式迁移命令执行；通用 `storage.init_db()` 不会在测试或普通启动时偷偷
改造既有生产库。

```powershell
python -X utf8 scripts\migrate_round_lineage.py --section price_lineage
python -X utf8 scripts\migrate_round_lineage.py --section price_lineage --write
```

SQLite 删除列需要重建表，因此区段 B 的回滚以迁移前已验证备份恢复为准。
若必须保留迁移后的新记录，应先导出再按旧 schema 重建，不直接覆盖生产库。

## 消费语义

- T 曲线仍可读取全部样本角色，并在报告中披露角色构成。
- `forecast_v2` 只消费 `trajectory_anchor`、`user_monitor`、`legacy`，默认排除
  `cross_sectional_probe`，防止单点横截面被归一成伪平坦轨迹。
- 日格执行状态由 `collection_cells` 推导为
  `missing/failed/empty/degraded/valid`。跳过项保留独立原因字段，不混入错误类型。
- 台账写失败时，采集继续并写结构化轮证据；该轮显式标记 `ledger_degraded`。

## 本地迁移审计

2026-08-26 首次全量回归暴露出：若 `round_id` 的 `ALTER TABLE` 挂在通用
`storage.init_db()`，测试导入主流程也会提前修改生产 `prices.db`。本地库因此
已提前新增三个可空列；审计确认三表历史记录的 `round_id` 非空数均为 0，没有按
时间猜测回填。提交前已将该变化收口到显式迁移入口，并新增旧 schema 合同；修复后
双收集器运行前后生产价格库哈希保持不变。
