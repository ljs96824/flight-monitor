# 运行数据备份、恢复与复放手册

> 只有成功恢复过的备份才算有效备份。

这套工具只读取运行数据，不调用任何航班 API。它先在短暂的采集单飞锁窗口内冻结 SQLite、JSON 与证据文件，再在锁外压缩；恢复时先校验整包和逐文件哈希，最后用同一份冻结快照复放 T 曲线与预测报告。

## 安全边界

- `--output-dir` 必须是绝对路径，而且必须位于项目目录与 `data/` 目录之外。命令没有默认输出目录，避免把上一份归档递归打入下一份。
- 归档包含邮箱、预算、路线、订阅和 payload。**未加密归档不得上传公共或共享云目录**。
- 需要离机保存时，使用系统加密盘、age 或 7-Zip AES；项目不自研加密。
- `.env`、锁文件、缓存、旧备份、临时文件与 `data/snapshots/` 永不入档。
- 默认恢复到新建临时目录，不覆盖生产、不发 API、不自动 Reload。
- 成功摘要中的 `real_api_calls` 必须为 `0`。

## 备份内容

| 级别 | 内容 | 缺失语义 |
| --- | --- | --- |
| 必需核心 | `prices.db`、`observations.sqlite3`、`subscriptions.json`、`api_usage.json` | 立即失败 |
| 业务状态 | feedback、低价日历、已推方案、篮子状态与信号历史 | 存在则必备；缺失记入 manifest |
| 证据 | payload、最近 N 日轮档 | 按命令配置 |
| 诊断 | 主日志、分析日志、通知日志 | 仅显式启用 |

严格扫描会拒绝 `data/` 中任何尚未进入版本化清单的路径。遇到该错误时，先人工判断它属于运行状态、证据、诊断还是禁止项，再修改清单；不要用通配兜底绕过。

## 1. 创建备份

以下示例把归档写到与项目不同的本地目录。请把路径替换为你的专用备份盘。

```powershell
python -X utf8 scripts/runtime_backup.py --output-dir "E:\flight-monitor-backups"
```

默认包含 payload 和最近 7 日轮档，不包含超大的可选诊断日志。常用开关：

```powershell
python -X utf8 scripts/runtime_backup.py --output-dir "E:\flight-monitor-backups" --label weekly
python -X utf8 scripts/runtime_backup.py --output-dir "E:\flight-monitor-backups" --round-log-days 14 --include-diagnostics
python -X utf8 scripts/runtime_backup.py --output-dir "E:\flight-monitor-backups" --no-payloads
```

`--label` 仅接受 1-40 位 ASCII 字母、数字、点、下划线或连字符，并进入本地归档名；
不要在标签中写路线、邮箱或其他个人信息。成功摘要打印归档路径、SHA-256、文件数与总字节数。

若已有采集轮运行，命令返回 `status=busy`、exit code `2`，且不产生归档或最终 manifest。它不会等待，也不会接管正在持有的 OS 文件锁。

成功后同目录出现：

- `flight-monitor-<backup_id>.tar.gz`
- `flight-monitor-<backup_id>.tar.gz.sha256`

`.sha256` 是整包校验旁路文件，不能丢失，也不写入归档内部。

创建成功后，工具原子更新 `data/backup_status.json`，只记录 `backup_id`、归档
SHA-256、恢复核验时刻与异盘副本核验结果，不记录归档路径或业务字段。新归档会使上一份
恢复/异盘证据失效；该状态文件属于本机门禁元数据，不进入备份归档，也不会触发 strict
扫描失败。

## 2. 隔离恢复并完整核验

恢复入口在临时目录解压、完整核验后删除临时恢复结果：

```powershell
python -X utf8 scripts/runtime_restore.py --archive "E:\flight-monitor-backups\flight-monitor-<backup_id>.tar.gz"
```

校验顺序是：归档 SHA-256、tar 路径与成员类型、文件数和总展开大小、manifest 逐文件 SHA、JSON 解析、SQLite `integrity_check`、`user_version` 与表行数。
全部通过后才原子写入 `backup_status.json.verified_restore_at`。

解压器拒绝绝对路径、`..`、symlink、hardlink、device、重复成员以及越界路径，
不使用裸 `extractall`。归档缺失、旁路 SHA 不一致、归档损坏或 manifest 校验失败均返回非零退出码。

## 3. 核验异盘副本与查看状态

先完成第 2 步，再把归档复制到另一块物理盘或私有加密云目录。以下命令读取副本、
计算 SHA-256，并与创建备份时记录在 `backup_status.json` 中的本地归档 SHA 比对：

```powershell
python -X utf8 scripts/runtime_restore.py --verify-off-disk "F:\private-backups\flight-monitor-<backup_id>.tar.gz" --off-disk-kind physical_disk
```

只有副本存在且 SHA 与记录值完全一致时，`backup_status.json` 才写入
`off_disk_copy.verified=true`。可用 `destination_kind` 为
`physical_disk`、`private_encrypted_cloud` 或团队约定的其他非敏感类别；它是介质类型声明，
不会记录私有路径。

查看 readiness 使用的全部备份证据：

```powershell
python -X utf8 scripts/runtime_restore.py --status
```

三个推荐入口都提供完整 `--help`，失败返回非零退出码。根目录的 `runtime_backup.py`
与 `runtime_restore.py` 仅保留纯逻辑；直接运行会明确提示改用 `scripts/`，不再静默退出。
原 `scripts/runtime_backup.py create|verify|restore|rehearse` 子命令继续兼容已有自动化。

## 4. 创建、恢复并复放

这是日常推荐的完整验收。`--route` 只进入归档内的私有 manifest，不进入脱敏控制台摘要。

```powershell
python -X utf8 scripts/runtime_backup.py rehearse --output-dir "E:\flight-monitor-backups" --route "<城市A>-<城市B>"
```

流程使用捕获时的 `core_snapshot/` 先生成 `tcurve_source.txt` 与 `forecast_source.txt`，打包后恢复到新目录，再从 `restored/core_snapshot/` 生成同名报告。两组 SHA-256 必须逐项一致。双方共同读取已冻结的 `snapshot_manifest.json`，包括 `permission_quality_cells`，不回头读取继续变化的活日志。

脱敏成功摘要只允许出现：`backup_id`、归档 SHA、文件数、总字节数、SQLite/JSON 校验结果、复放 SHA、`production_state_changed` 和 `real_api_calls`。它不显示订阅文件名、payload UUID、路线、日期、邮箱或数据库实值。

## 5. 高级生产恢复

生产覆盖功能仅用于确认磁盘损坏后的人工恢复，本手册的例行演练不得使用它。它要求双确认，并先在项目外创建一份已验证的 pre-restore backup：

```powershell
python -X utf8 scripts/runtime_backup.py restore --archive "E:\flight-monitor-backups\flight-monitor-<backup_id>.tar.gz" --force-production --confirm-production-restore RESTORE --pre-restore-output-dir "E:\flight-monitor-pre-restore"
```

流程持有 collection single-flight，按既定顺序锁定 JSON，验证候选后把旧状态移入 rollback 目录再切换；任一步失败立即恢复旧状态。命令不会自动 Reload。生产恢复后仍需人工核对页面版本信标，再决定是否 Reload。

## 6. 周期与记录

- 每周至少一次执行完整 `rehearse`。
- 每次重大改动前必须执行一次完整 `rehearse`。
- 归档移动到其他介质后，先隔离恢复，再运行独立 `--verify-off-disk` 核验传输 SHA。
- 记录脱敏摘要即可；不要把私有 manifest、路线、订阅、payload 标识或归档本身提交到 Git。
- `status=busy` 不算备份失败，但也不算已有新备份；待当前采集结束后重新执行。

## 归档结构

```text
core_snapshot/
  observations.sqlite3
  prices.db
  api_usage.json
  snapshot_manifest.json
state/
delivery/payloads/
diagnostics/round_logs/
replay/
manifest.json
```

`core_snapshot/` 与现有只读报告 `--db` 接口兼容。顶层 `manifest.json` 是私有清单；整包 SHA 位于归档旁路文件，避免自引用。
