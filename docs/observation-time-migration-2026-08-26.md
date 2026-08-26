# 观测时间 v2 迁移审计

## 结论

`observations.observed_at` 的历史 `86,227` 行全部为无 offset 的 naive 时间；未发现带 `Z`、offset 或无法解析的值。按 `Asia/Shanghai` 解释后，历史 `days_to_departure` 与 canonical 上海观测日逐行重算的差异为 `0`。

## 来源依据

- 历史写入点使用本机 `datetime.now()`，未附 offset。
- `103` 个历史 round 均来自本项目的订阅、collection 或 `basket_*` 本地采集入口。
- [collection-concurrency.md](collection-concurrency.md) 明确 PythonAnywhere 只承载表单、订阅与详情同步，不执行真实航班采集。
- Windows 计划任务与篮子均在上海本机运行；项目统一时区入口为 `project_time.SHANGHAI_TZ` (`Asia/Shanghai`)。

因此本次允许通过显式参数 `--assume-naive-shanghai` 解释历史 naive 行。迁移代码默认不作该推断；没有该参数时只读审计会把 naive 行计入待判定项，写模式会拒绝执行。

## 字段语义

- `observed_at`: 兼容字段；新写入保存带 offset 的上海时刻。
- `observed_at_utc`: 同一时刻的 UTC ISO 文本。
- `observed_day_shanghai`: T 值与日格折叠的权威上海自然日。
- `legacy_time_ambiguous`: 来源无法确认或时间不可解析时为 `1`；正式 T 曲线默认排除。

T 曲线读取顺序为 canonical 上海日、已审计 legacy fallback；明确标记 ambiguous 的行不进入正式曲线。方法版本同步提升为 `obs_store v2` 与 `tcurve_v2`。

## 命令

只读审计：

```powershell
python -X utf8 scripts/migrate_observation_timestamps.py --db data/observations.sqlite3 --assume-naive-shanghai
```

显式写入：

```powershell
python -X utf8 scripts/migrate_observation_timestamps.py --db data/observations.sqlite3 --assume-naive-shanghai --write
```

生产写入前应先生成可验证备份。回滚以迁移前备份恢复为准；不通过时间文本反推旧值，也不删除历史行。
