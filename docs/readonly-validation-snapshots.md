# 只读验证快照

预测与 T 曲线报告默认读取持续增长的生产数据。为了让一次验证在后台计划任务照常运行时仍可复现，先创建固定输入快照：

```powershell
python -X utf8 scripts/create_readonly_snapshot.py --label p7-gate-20260824
```

脚本读取 `data/prices.db`、`data/observations.sqlite3` 与 `data/api_usage.json`，原子发布到：

```text
data/snapshots/p7-gate-20260824/
```

`data/snapshots/` 已被 `.gitignore` 忽略。快照不调用 API，也不写生产数据库。

## 一致性语义

- 两个 SQLite 文件使用 SQLite online backup API，复制已提交事务并包含 WAL 中已提交但尚未 checkpoint 的页；未提交事务不会进入快照。
- 复制前同时打开两个只读监视连接，并在整组复制前后由同一批连接比较各库 `PRAGMA data_version`；源连接均先执行 `PRAGMA query_only=ON`。任一库在复制期间出现外部提交，两个数据库与 JSON 会整组重试。
- `api_usage.json` 按文件复制。三个源主文件在整组快照前后分别计算 SHA-256；JSON 或主文件变化同样触发整组重试。`data_version` 专门补足 WAL 提交不会改变主文件 SHA 的盲区。
- 默认重试一次，仍不稳定就失败，不发布半成品；manifest 的 `capture` 块记录尝试次数、稳定的 data version 与一致性策略。
- 快照先写临时目录，完整性检查通过后用 `os.replace` 原子发布。
- `snapshot_manifest.json` 保存生成时刻、源文件 SHA、快照文件 SHA，以及创建时冻结的 PermissionError 质量格清单。T 曲线复放只读该清单，不再读取会继续增长的轮档。

SQLite online backup 会生成逻辑一致的新数据库文件，因此 SQLite 的快照 SHA 不要求与源主文件 SHA 相同；脚本会同时打印二者供审计。

快照保证每个输入在复制期间文件级稳定，并保证后续报告使用固定文件可复放；它不保证 `prices.db`、`observations.sqlite3` 与 `api_usage.json` 属于同一逻辑采集 round。精确的跨库轮次一致性依赖 `round_id lineage`，该缺口已登记为后续 schema 任务。manifest 因此使用 `file_level_stable_inputs`，不再声称组级逻辑快照。

## 固定输入运行

两份报告都接受快照目录作为 `--db`：

```powershell
python -X utf8 scripts/forecast_report.py --route 上海-大阪 --db data/snapshots/p7-gate-20260824
python -X utf8 scripts/tcurve_report.py --route 上海-大阪 --db data/snapshots/p7-gate-20260824
```

报告统一读取目录内的 `observations.sqlite3`；T 曲线的权限事故清单来自同目录 manifest，且同目录必须同时含 `prices.db` 与 `api_usage.json`。验证期间生产任务可以照常运行，报告输入仍固定，不再需要“外部并发写入可能改变结果”的免责声明。

快照含真实本地观测与台账，仅供本机验证，不得移出已忽略目录或提交到版本库。
