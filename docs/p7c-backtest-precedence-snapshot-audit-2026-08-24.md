# P7-C 回测、防线优先级与快照语义审计

日期：2026-08-24
范围：预测诊断层、只读快照与部署文档；不修改采样、面板、金额、排序或推送资格。

## 1. 折内训练集构造

`forecast.walk_forward_backtest()` 对每个目标观测日和 `k=1/3/7` 独立计算截止日，并在任何 shape、level 或基线计算之前截断：

```python
cutoff = date.fromisoformat(target_day) - timedelta(days=int(horizon))
cutoff_text = cutoff.isoformat()
fit = [item for item in usable if str(item["observed_day"]) <= cutoff_text]
```

结论：原算法已经逐折截断，不是先按全局 `as_of` 拟合后回填历史折。本次没有改变模型公式，只给每个 case 增加 `depart_date`、`target_t`、`fit_n` 与 `fit_training_signature`，使历史折训练输入可逐值对账。回归对 `k=1/3/7` 分别在“折截止日之后、全局 as_of 之前”插入极端价格观测；历史折训练指纹、训练数量、训练观测日和该折 MAPE 必须保持不变。故意把未来观测日塞入 `fit_observed_days` 会被 `assert_no_walk_forward_leakage()` 拒绝。

## 2. 六状态优先级

统一裁决的硬优先级为：

1. `lineage_incomplete`
2. `skill_gate_failed`
3. `regime_insufficient`
4. `shape_sample_insufficient`
5. `source_degraded`
6. `eligible`

`status` 与 `primary_reason` 取最高优先命中项，`reason_codes` 保留全部失败证据；`eligible` 是独立布尔字段，仅在所有硬门通过时为 `True`，此时 `primary_reason=None`。例如日型样本不足、shape 样本不足与源退化同时发生时，主状态必须是 `regime_insufficient`，另外两项仍留在 `reason_codes`，不得被隐藏。level 不可靠归入 `shape_sample_insufficient` 主状态，同时保留 `level_unreliable` 细因。优先级同时写入函数 docstring 与契约矩阵；高分项不能平均或补偿低分项。

`forecast.assess_overall_reliability()` 原实现仍为：

```python
value = min(item["value"] for item in components.values())
```

因此其本身已经是严格最短板语义，本次不修改。

## 3. 快照技术核实

- `create_readonly_snapshot()` 每次尝试只调用一次 `_open_sqlite_watchers()`；前后两次 `_sqlite_data_versions(watchers)` 使用同一组连接对象。
- `_open_sqlite_watchers()` 与 `_backup_sqlite()` 的源连接都在读取或 backup 前执行 `PRAGMA query_only=ON`。
- 任一输入在复制期间变化会触发整组重试，输出目录仍通过 `os.replace` 原子发布。
- manifest 的一致性标签由 `stable_group_data_version` 收窄为 `file_level_stable_inputs`。

语义边界：快照保证各输入在复制期间文件级稳定，并保证报告可对固定文件复放；它不证明三个文件属于同一逻辑采集 round。精确跨库轮次一致性仍依赖已登记的 `round_id lineage`。

## 4. 预测门控安全等价

P7-A 的结论统一称为“预测门控安全等价 (forecast gating safety equivalence)”。邮件、PushPlus、网页详情和推荐判定行为不同，但当前四条路径都不消费未过门预测；这不是渠道行为等价声明。

## 5. PA Reload 判据

实际 import 链为：

```text
run_web.py -> web_form.py --后台首次处理时延迟导入--> main.py -> forecast.py
scripts/forecast_report.py -> patterns.py
```

因此 `forecast.py` 改动会进入已加载后台链路，PA Web 进程需要 Reload；`patterns.py` 当前仅用于离线报告，单独修改它不要求 Web Reload。统一判据是“Web进程是否import改动模块”，不是“是否修改 web_form.py”。

## 6. 文档路径与缓存目录

仓库 Markdown 扫描未发现带 Windows 盘符的工作区绝对路径；新增契约会持续阻止此类路径进入文档。`.pytest_cache` 的拒绝访问 ACL 已由用户授权后删除，`git status --porcelain` 不再产生该警告。
