# 订阅文件单文件恢复规程

> 适用场景：`data/subscriptions.json` 被误删或丢失，运行入口以退出码 2 停止(R9-1)，其余运行数据完好。

本规程只把**一份经明确选择、校验通过的运行备份**中的 `subscriptions.json` 写回生产 `data/`，
不回滚 `prices.db`、`observations.sqlite3`、`api_usage.json`、`runtime_config.yaml` 或任何其他状态，
不新增 PythonAnywhere 恢复命令，也不启动采集。

## 边界

- 只读来源：备份归档及其 `.sha256` 旁路文件。工具先把整包隔离恢复到系统临时目录并完整核验
  (归档 SHA-256、成员安全性、manifest 逐文件 SHA、JSON/SQLite 校验)，用完即删。
- 预览与回填都**不写 `backup_status.json`**。需要更新恢复核验证据时，另行执行
  [运行数据备份手册](runtime-backup-and-restore.md) 第 2 步。
- 回填只写 `data/subscriptions.json`(以及订阅文件锁 `data/subscriptions.json.lock`，平时已存在)；
  回填期间持有采集单飞锁，结束即释放。
- 只缺订阅文件时，**不得**用 `scripts/runtime_backup.py restore --force-production` 整体恢复代替：
  它会按 manifest 把数据库、配额台账和运行配置一并切换回备份时刻。
- 目标文件已存在(包括在维护期间被本机 Web 表单新建订阅或 `scripts/initialize_subscriptions.py` 重建)时一律拒绝，
  不覆盖；先核对那份文件，再决定是否删除后重来。
- 预览输出含订阅编号、状态、航线和日期(不含邮箱、预算与令牌)，只在本机终端核对，
  不要贴进 Git、PR 或 issue。

## 1. 进入维护窗口

在项目根目录的 PowerShell 中列出会运行采集或同步的计划任务，核对名单与 Task Scheduler 中的实际任务一致
(用 `.bat` 包装的任务需人工补入)，再全部停用；任务以最高权限创建时，需以管理员身份运行 PowerShell：

```powershell
$tasks = Get-ScheduledTask | Where-Object { (($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join ' ') -match '\b(main|basket_collect|sync_subscriptions)\.py\b' }
$tasks | Select-Object TaskName, State
$tasks | Disable-ScheduledTask
```

同时关闭本机正在运行的 `run_web.py`。等正在运行的采集轮结束(工具拿不到采集单飞锁时会拒绝回填)。

## 2. 只读预览并选定备份

从最近的备份开始逐份预览，直到找到成员与预期一致的一份：

```powershell
python -X utf8 scripts/restore_subscriptions_from_backup.py --archive "E:\flight-monitor-backups\flight-monitor-<backup_id>.tar.gz"
```

逐项核对输出：

| 字段 | 核对内容 |
| --- | --- |
| `backup_id`、`backup_created_at_utc`、`archive_sha256` | 就是你选定的那一份备份 |
| `subscription_count`、`active_count`、`active_ids`、`status_counts` | 与丢失前的订阅规模相符；`active_count` 按各采集入口的规则统计，缺 `status` 的订阅也算(`status` 显示为 `(missing)`) |
| `subscriptions` | 逐条的 `id`、`status`、`collected_by`(会把它当作 active 的采集入口)、航线与日期；`preflight_skip` 非空表示下次采集会跳过(如 `expired`) |
| `needs_attention` | 非对象条目、缺少编号的条目，以及状态写法不规范却会被采集的条目(`status_nonstandard`)；它们会原样写回，需在解除维护前处理 |
| `target` | 必须是主工作目录下的 `data\subscriptions.json`；从其他检出目录运行会写到那个目录 |
| `target_exists` | 必须为 `false`；为 `true` 时先核对现有文件，不要继续 |
| `isolated_copy_left` | 正常为 `null`；不为空时是未能删除的隔离副本路径(内含订阅与数据库副本)，核对完手动删除 |
| `subscriptions_sha256` | 回填时用 `--expect-sha256` 绑定的值 |

备份时刻之后新建、修改或删除的订阅不在备份里；PA 上的新订阅会在恢复采集后由只新增同步补回，
本机新增的需要重新录入。

## 3. 回填

把预览输出的 `apply_arguments` 原样接在同一命令之后执行，即同时提供 `--apply`、
`--confirm RESTORE-SUBSCRIPTIONS` 与 `--expect-sha256 <预览中的 subscriptions_sha256>`：

```powershell
python -X utf8 scripts/restore_subscriptions_from_backup.py --archive "E:\flight-monitor-backups\flight-monitor-<backup_id>.tar.gz" --apply --confirm RESTORE-SUBSCRIPTIONS --expect-sha256 <subscriptions_sha256>
```

工具在采集单飞锁内重新隔离恢复并核对哈希，在订阅文件锁内再次确认目标不存在，然后把备份中的原始字节
原子写入 `data/subscriptions.json`，读回核对 SHA-256。恢复动作不启动采集。

| 退出码 | 含义 | 生产文件 |
| --- | --- | --- |
| `0` | 已回填，`published_sha256` 等于 `--expect-sha256` | 只新增 `subscriptions.json` |
| `2` | 拒绝：`reason=` 为 `target_exists`、`sha256_mismatch`、`confirmation_mismatch`、`expected_sha256_invalid`、`collection_busy`、`subscriptions_lock_busy` 或 `apply_flag_missing`；参数写错时 argparse 也以 2 退出(打印用法，没有 `reason=`) | 未改动 |
| `1` | 失败：归档损坏或旁路 SHA 不符、manifest 中没有唯一的订阅文件、订阅内容不是数组等 | 未改动 |
| `3` | 已写入未核验：订阅文件已原子写入，但读回失败或 SHA-256 不符 | 已写入，须立即人工核对；再次回填会因目标已存在而拒绝 |

## 4. 解除维护前核对 active 状态

1. 以回填输出的 `active_ids` 与 `subscriptions` 中 `collected_by` 非空的条目为清单，逐条决定是否继续采集；
   行程已取消、已出票或 `preflight_skip` 非空的订阅，先在本机管理页(重新启动 `run_web.py`)暂停或删除。
2. 处理 `needs_attention` 中的条目。
3. 恢复第 1 步停用的计划任务：

```powershell
$tasks | Enable-ScheduledTask
```

若已关闭第 1 步的 PowerShell 窗口，按第 1 步的筛选重新取得 `$tasks` 后再执行。
