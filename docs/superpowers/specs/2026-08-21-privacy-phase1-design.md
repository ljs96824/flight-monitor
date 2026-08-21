# 隐私 Phase 1 设计

## 目标

在不改变航班采集、价格判断和默认通知内容的前提下，关闭 PythonAnywhere 详情页的可猜测访问面，并为外发参数、日志、通知降档和数据保留建立可测试契约。

## 不变量

- 不调用真实航班 API。
- 详情页只接受规范 UUID；数字索引、任意字符串、旧索引和“最新一条”回退全部失效。
- `notification_privacy_level` 缺失或为 `full` 时，现有邮件和 PushPlus 渲染路径不变。
- 历史文件清理和保留窗删除默认只报告；只有用户显式运行 `--execute` 才修改磁盘。
- 乘客类型计数和 `cabin_allocation` 不进入 juhe、SerpAPI、Duffel 的外发请求。
- 共享详情令牌只从 `SHARED_DETAIL_TOKEN` 读取，默认关闭，值不写 payload、不进日志。

## 详情访问

新增独立的详情访问边界：

1. 将 `sub` 解析为 UUID 并规范化为小写字符串。
2. 非 UUID 立即返回 404。
3. 仅读取 `data/payloads/<uuid>.json`；不存在或 JSON 无效均返回 404。
4. 不读取 `data/page_results.json`，不枚举 payload 文件，也不回退最新结果。
5. 配置 `SHARED_DETAIL_TOKEN` 后，查询参数 `token` 必须使用常量时间比较命中；错误时仍返回 404。

保存端同步停止生成 `page_results.json`，且非 UUID 的订阅标识不再写 payload 或上传 PA。通知中的详情链接仅在订阅有合法 UUID 时出现。

## PA 存量清理

代码部署并 Reload 后，由用户按报告中的命令执行：先生成带时间戳 tar 备份，再删除纯数字 JSON 与 `page_results.json`，最后分别验证三个旧数字 URL 为 404、现役 UUID 为 200。命令不打印 UUID、token 或 payload 内容。

## 乘客构成外发契约

测试在请求构造边界捕获三源的真实参数对象：

- juhe：只含航线、日期和 API 凭据。
- SerpAPI：只含航线、日期、舱位和查询配置。
- Duffel：保留固定单成人请求，用于运价规则富化；不得携带订阅乘客构成或分舱分配。

公共断言递归拒绝 `child`、`elderly`、`infant`、`passenger_count`、`cabin_allocation`、`business_seats`、`economy_seats` 等订阅级字段。

## 日志与历史脱敏

`log_utils` 提供单一文本/结构脱敏入口：秘密字段继续替换为 `***`，邮箱替换为 `<EMAIL>`。控制台、`run_latest.log`、轮档和 `safe_log` 共用同一入口；失败通知中的原因文本也因此受保护。

`scripts/scrub_pii.py` 默认 dry-run，仅扫描轮档、运行日志和备份。`--execute` 先把所有将修改的文件复制到带时间戳的隔离备份目录，再原子替换原文件。脚本不自动执行。

## 通知隐私级别

- `full`：默认，沿用现有完整邮件和 PushPlus 路径，保证存量零漂移。
- `redacted`：只展示航线、事件类型和千元价格区间；隐藏乘客构成与精确金额，并引导到本地详情。
- `minimal`：只展示“航线有变动”，不展示金额、乘客、日期、约束或详情链接。

页 2 增加三态选择器。为了维持旧订阅 JSON，默认 `full` 不落额外字段；只有显式降档才写入 `notification_goals.privacy_level`。

## 保留窗

配置新增：

```yaml
retention_days:
  payloads: 90
  round_archives: 90
  backups: 180
```

`0` 表示永久保留。轮末只打印各类到期数量；`scripts/retention_cleanup.py` 默认 dry-run，只有 `--execute` 才删除到期文件。

## 部署闸门

本地实现可以提交，但在以下事实全部由用户确认前不得 push：

1. PA 已部署新代码并 Reload。
2. PA payloads 已备份并清除数字索引与 `page_results.json`。
3. 三个旧数字详情 URL 匿名访问均为 404。
4. 一个现役 UUID 详情 URL 按当前令牌配置返回 200。
