# 返程采集 PermissionError 诊断与修复

日期：2026-08-24
证据源：`data/logs/rounds/20260823.log` 及历史轮档（只读扫描）
约束：全程离线，未发起真实航班 API 请求

## 1. 结论

8 月 23 日轮次 `collection_20260823T210014035475` 的返程并非“市场无票”。4 个返程机场组合均已收到聚合数据 HTTP 200 且响应非空，但 `JuheSource._write_cache()` 在写本地缓存前执行相对路径 `data/cache` 的目录创建时触发 `PermissionError: [WinError 5]`。异常随后被请求缓存层转成空航班的失败结果；主流程只保留了无方向的 `source_errors`，通知层又看到去程 `juhe` 有 319 个有效方案，因而误判源仍可用，最终回落到“无符合方案”的市场筛选文案。

根因是订阅计划任务未设置工作目录：`FlightMonitor_2100` 直接执行仓库内 `main.py`，但 `Start In` 为空；篮子任务则显式 `cd /d` 到仓库。因此相对缓存目录在订阅轮中解析到计划任务的受保护工作目录，而篮子轮不受影响。

## 2. 8 月 23 日证据

返程 4 个组合均出现同一顺序：真实请求成功、原始响应非空、随后缓存目录写入失败并作为采集失败入池。

| 返程组合 | 轮档行 | 结果 |
|---|---:|---|
| KIX→PVG | 312-318 | HTTP 200 后 `PermissionError [WinError 5]: 'data'` |
| KIX→SHA | 736-742 | 同上 |
| ITM→PVG | 783-789 | 同上 |
| ITM→SHA | 830-836 | 同上 |

轮档第 872 行显示返程采集结果为 0；之后通知走“无符合方案”。原轮档和 `monitor.log` **没有保存 Python traceback**：异常被 `cached_fetch()` 捕获后只通过 `safe_log` 记录为 `[采集失败入池]` 文本行，并未调用 `logging.exception`。所以不能把后来重建的栈伪称为现场原始栈。用同一旧函数离线注入目录拒绝后重建的完整栈如下，精确失败点为修复前 `sources/juhe_source.py:430`：

```text
Traceback (most recent call last):
  File "<offline-replay>", line 1, in <module>
  File "<repo>/sources/juhe_source.py", line 430, in _write_cache
    path.parent.mkdir(parents=True, exist_ok=True)
  File "pathlib.py", line 1116, in mkdir
PermissionError: [Errno 13] 拒绝访问。: 'data'
```

## 3. 为什么未触发源退化披露

1. `request_cache.cached_fetch()` 捕获了通用异常并返回 `source_status=failed`，所以异常没有继续向上抛出。
2. `FlightAggregator.collect()` 将其加入 `source_errors`，失败对用户不可见但被内部记录。
3. 返程失败没有被建模成带方向的失败对象；`source_stats` 仍含去程 `juhe.count=319`。
4. `_build_source_degradation_context()` 只按整轮源计数判断，看到 319 后认为该源仍可用。
5. 无返程组合的后续路径因此执行普通无结果诊断，把数据故障误写成市场筛选结论。

修复后新增 `collection_failures`，明确记录去程/返程、日期、源错误、errno 和脱敏路径。该对象的优先级高于无方案诊断。

## 4. 历史频次

只读扫描 `data/logs/rounds/*.log`，以 `[采集失败入池]` 且包含 `PermissionError` 为口径：

| 日期 | 次数 |
|---|---:|
| 2026-08-13 | 16 |
| 2026-08-14 | 17 |
| 2026-08-15 | 9 |
| 2026-08-17 | 11 |
| 2026-08-19 | 4 |
| 2026-08-21 | 10 |
| 2026-08-23 | 4 |
| **合计** | **71** |

共命中 10 个订阅采集轮次，时间集中在 15:00/21:00 的订阅计划任务；篮子轮次为 0。该问题自 8 月 13 日起持续复现，属于常驻配置/路径问题，不是偶发文件占用。

当前开发机 `.pytest_cache` 的拒绝 ACL 与此问题无时间或调用链关联：历史故障发生在生产订阅任务的 `data/cache` 相对路径，且早于本次开发环境缓存目录异常。两者仅共享 `PermissionError` 表象。

## 5. 修复边界

- `juhe` 私有缓存目录锚定仓库绝对路径，不再依赖进程当前工作目录。
- 缓存写入改为同目录临时文件、`fsync`、`os.replace` 原子替换。
- 本地缓存写失败退避重试一次；第二次仍失败时保留已经采集到的航班，只记录缓存失败。
- 源采集阶段的 `OSError/PermissionError` 统一退避重试一次；仍失败则输出显式源失败，携带 `error_type/errno/retry_count` 和脱敏路径。
- 返程全空且有源错误时写入方向化 `collection_failures`。
- 通知改为“数据不完整，本轮结论不可用”，披露“本轮返程采集失败……结论不代表市场无票”，并抑制筛选漏斗、最大卡点、市场无票及价格位置判断。
- 计划唯一请求数不因重试增加；实际物理请求与本地配额台账仍如实计入重试，统计新增 `retries` 字段。

## 6. 回归证据

脱敏轮档夹具：`tests/fixtures/collection_failure_20260823_v1.json`。

回归覆盖：

- 缓存路径必须是仓库绝对路径；
- 缓存写失败只重试一次且不丢已采航班；
- 源级 `PermissionError` 只重试一次，失败元数据与路径脱敏正确；
- 返程失败即激活数据不完整披露；
- 8/23 脱敏复放不再出现“无符合方案”“最大卡点”或市场筛选结论；
- canonical 通知小节在数据不完整分支齐全。