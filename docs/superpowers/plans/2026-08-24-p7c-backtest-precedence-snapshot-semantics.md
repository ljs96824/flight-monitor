# P7-C Backtest, Eligibility, And Snapshot Semantics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 锁住 walk-forward 折内无泄漏、统一六状态优先级，并把只读快照的技术保证收敛为准确的文件级语义。

**Architecture:** 保留 P4/P5 日格、现有预测算法与通知闸门，只在 `forecast.py` 的唯一裁决入口定死优先级并补机器字段。折内泄漏由真实 walk-forward 回归和故意泄漏反例共同证明；快照继续使用同一 watcher 连接与 SQLite online backup，只补契约测试、manifest 术语和文档边界。

**Tech Stack:** Python 3.13、标准库 `sqlite3`/`unittest`、pytest、Markdown。

---

### Task 1: 锁住逐折训练集截断

**Files:**
- Modify: `test_forecast.py`
- Modify: `forecast.py`

- [ ] **Step 1: 写 k=1/3/7 折内不变量测试**

构造多个出发日与连续观测日，保存一个历史折；插入 `cutoff_day < observed_day <= global_as_of` 的极端价格后重跑，逐项断言该折的 `fit_observed_days`、训练签名、预测误差与所属 horizon MAPE 不变。

- [ ] **Step 2: 运行红测**

```powershell
python -X utf8 -m unittest test_forecast.ForecastTest.test_later_observation_does_not_change_historical_fold
```

预期：因历史 case 尚无稳定训练签名而失败。

- [ ] **Step 3: 最小实现折证据**

在每个 case 中记录排序后的训练行键与价格签名；训练集仍严格来自：

```python
fit = [item for item in usable if str(item["observed_day"]) <= cutoff_text]
```

扩展 `assert_no_walk_forward_leakage()`，令故意包含未来行的反例抛 `AssertionError`。

- [ ] **Step 4: 运行定向测试**

```powershell
python -X utf8 -m unittest test_forecast
```

预期：k=1/3/7 正例与故意泄漏反例均通过。

### Task 2: 定死六状态优先级

**Files:**
- Modify: `forecast.py`
- Modify: `scripts/forecast_report.py`
- Modify: `test_forecast_eligibility.py`

- [ ] **Step 1: 写优先级矩阵红测**

覆盖单一失败与多重失败，期望：

```text
lineage_incomplete > skill_gate_failed > regime_insufficient >
shape_sample_insufficient > source_degraded > eligible
```

同时断言 `reason_codes` 保留全部失败、`eligible` 为布尔值、`primary_reason` 与 status 对应。

- [ ] **Step 2: 运行红测**

```powershell
python -X utf8 -m unittest test_forecast_eligibility
```

预期：现行 lineage/regime 顺序和缺失字段导致失败。

- [ ] **Step 3: 最小修改统一裁决**

在 `evaluate_forecast_eligibility()` docstring 与常量表中登记唯一优先级；status 取首个命中项，其余失败全部保留。报告只适配新 shape 状态名，不复制门控逻辑。

- [ ] **Step 4: 运行门控相关测试**

```powershell
python -X utf8 -m unittest test_forecast_eligibility test_forecast_gate_evidence test_forecast_asof_and_evidence test_forecast_notification
```

预期：全部通过，技能门未过时冻结邮件输出不变。

### Task 3: 收口快照语义

**Files:**
- Modify: `readonly_snapshot.py`
- Modify: `test_readonly_snapshot.py`
- Modify: `test_readonly_snapshot_group_consistency.py`
- Modify: `docs/readonly-validation-snapshots.md`

- [ ] **Step 1: 写连接身份与 query_only 契约测试**

用记录连接身份的 fake watcher 断言同一连接先后读取 `data_version`；检查 watcher 与 online-backup 源连接均执行 `PRAGMA query_only=ON`。

- [ ] **Step 2: 运行测试确认当前事实**

```powershell
python -X utf8 -m unittest test_readonly_snapshot test_readonly_snapshot_group_consistency
```

预期：技术事实已满足；manifest 旧术语测试先红。

- [ ] **Step 3: 修改 metadata 与文档**

把 `stable_group_data_version` 改为不暗示跨库同轮的 `file_level_stable_inputs`。文档明确：快照保证各文件复制期间稳定与复放一致，不保证多库属于同一逻辑采集 round；精确轮次一致性依赖 `round_id lineage`。

- [ ] **Step 4: 运行快照测试**

```powershell
python -X utf8 -m unittest test_readonly_snapshot test_readonly_snapshot_group_consistency test_readonly_snapshot_integrity
```

预期：全部通过。

### Task 4: 审计与部署文档卫生

**Files:**
- Modify: `docs/p7-forecast-gating-equivalence-audit-2026-08-24.md`
- Create: `docs/p7c-backtest-precedence-snapshot-audit-2026-08-24.md`
- Modify: `README.md`
- Modify: `test_docs_accuracy.py`

- [ ] **Step 1: 写 Markdown 路径与部署判据测试**

扫描受版本控制 Markdown，拒绝带 Windows 盘符的工作区绝对路径；断言部署文档含“是否 import 改动模块”的 Reload 判据。

- [ ] **Step 2: 运行红测**

```powershell
python -X utf8 -m unittest test_docs_accuracy
```

- [ ] **Step 3: 更新审计与 README**

把结论统一命名为“预测门控安全等价 (forecast gating safety equivalence)”。记录 `run_web -> web_form -> main -> forecast` 的懒加载链与 `patterns` 仅由离线报告消费的事实。

- [ ] **Step 4: 清理 `.pytest_cache` ACL 警告**

只处理已验证位于仓库内的 `.pytest_cache`；不触碰其他目录。若当前令牌无法取得所有权，记录阻塞并要求用户以管理员权限清理，不以绕行命令规避。

### Task 5: 全量验证与独立提交

**Files:**
- Create ignored: `data/snapshots/p7c-after.json`
- Create ignored: `data/snapshots/<label>/`

- [ ] **Step 1: 生成 after 快照并 diff**

```powershell
python -X utf8 scripts/snapshot_run.py --output data/snapshots/p7c-after.json
```

预期：业务快照无非预期变化。

- [ ] **Step 2: 用固定只读快照运行两份报告**

```powershell
python -X utf8 scripts/create_readonly_snapshot.py --label p7c-final
python -X utf8 scripts/forecast_report.py --route 上海-大阪 --db data/snapshots/p7c-final
python -X utf8 scripts/tcurve_report.py --route 上海-大阪 --db data/snapshots/p7c-final
```

- [ ] **Step 3: 冻结邮件与双收集器**

```powershell
python -X utf8 -m unittest test_frozen_email_regression
python -X utf8 -m unittest discover
python -X utf8 -m pytest -q -p no:cacheprovider
```

- [ ] **Step 4: 核对零副作用并提交**

复核 `api_usage.json`、`observations.sqlite3`、`prices.db` SHA-256 不变；审阅 diff 后创建独立提交。未获用户 push 指令时不推送，公开 CI 状态如实标为待 push 终审。
