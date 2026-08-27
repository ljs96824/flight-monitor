# 低价日历新鲜度语义整改

日期: 2026-08-27

## 状态合同

每个日期格记录 `status`、`min_price`、`last_attempt_at`、
`last_success_at`、`error_type`、`stale_after` 与 `round_id`。

- `success`: 本次有有效报价，且尚未超过 `stale_after`。
- `empty`: 本次请求成功，但没有有效报价。旧成功价仅保留作历史参考。
- `failed`: 本次请求失败。结构化源异常保留源 `error_type`。
- `stale`: 最近成功价已过期，只能灰显为历史参考。

只有未过期的 `success` 格可进入省钱提示、最低日期、星期规律、
表单价格提示和 provenance 统计信封。`empty`、`failed`、`stale`
必须在邮件与详情中如实显示状态，不得沿用旧价格作当前结论。

旧格式记录在读取时只做内存规范化，不静默改写磁盘；下一次采集尝试后
才按新结构原子落盘。JSON 损坏由 `JsonStoreReadError` 明确失败，不再
伪装为空日历。

## 上海自然日审计

以下实时日期判断改为统一调用 `subscription_preflight.shanghai_today()`:

- `price_calendar.py`: 查询日期、过去日期过滤、日历统计。
- `collection_plan.py`: 弹性日期与日历补采判定。
- `analyzer.py`: 提前天数、星期归属、往返趋势图当天行。
- `main.py`: 去返程 `days_to_departure`。
- `notifier.py`: 日历未来日期筛选。
- `provenance.py`: 历史一致度与信封窗口终点。
- `tcurve.py`: 当前 T 值。
- `scripts/snapshot_run.py`: 离线快照相对日期默认值。

生产 Python 文件已建立源码合同，禁止重新引入依赖宿主机时区的
`date.today()` 或 `datetime.now().date()`。时间戳、缓存 TTL 与耗时计算
仍可使用带时区时间或绝对时间，它们不属于自然日归属判断。

## 存储与并发

日历写入统一经过 `atomic_json_store.update_json()`，写临时文件、
`fsync` 后 `os.replace`。替换失败时原文件逐字节不变。采集单飞机制
继续负责跨进程的整轮互斥；本次不新增另一套锁顺序。
