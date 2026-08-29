# 生产日志完整对象 sink 收口（2026-08-29）

## 1. 范围与基线

- 基线：`614d114b698ed09e671090e694c0657ceb03c5bf`（PR #8 merge commit）。
- 基线公开检查：Ubuntu、Windows、`ui-smoke` 均为 `success`。
- 本笔只处理 `notifier.py` 新确认的完整 `subscription` 与 `flight` 对象日志，并增强既有 `log_utils.redact_value()`。
- 不读取真实 `.env`、真实凭据或生产日志正文；测试只使用专用假 canary。
- 不改变采集、分析、推荐、价格、排序、渲染、通知发送或持久化语义。

## 2. 扫描方法

对 `notifier.py` 同时执行了文本与 AST 扫描，覆盖：

- `print(...)`、`safe_log(...)`、`safe_log_json(...)`；
- `json.dumps(...)`、`repr(...)`；
- 上述调用内的 f-string、对象字段访问与间接序列化；
- 不经上述函数的文件写入（`_log_notification()`）。

扫描还扩到全仓同类模式，用于登记 `notifier.py` 之外的完整对象日志，但未扩大本笔修改范围。

## 3. notifier.py sink 清单

以下行号对应本笔实现后的文件。分组列出了全部直接命中；同一组只合并相同数据形态，不省略调用点。

| 行号 | sink | 数据形态 | 判定与本笔处置 |
| --- | --- | --- | --- |
| 192, 199, 203, 219, 225 | `print` | PushPlus模式、长度、空响应状态 | 标量诊断，不含领域原对象；不改。 |
| 224 | `print` | PushPlus响应文本前200字符 | 第三方响应文本，不是订阅/航班对象；登记，未在本笔改写。 |
| 449, 465, 494 | `safe_log` | 金额树与取整一致性标量 | 不含领域原对象；不改。 |
| 1245, 1256, 1259, 1267 | `print` | PushPlus配置/长度/成功状态 | 不含领域原对象；不改。 |
| 1261 | `print` | PushPlus解析结果对象 | 第三方响应对象；登记，未在本笔改写。 |
| 5396 | `safe_log` | 规范化后的航班组合标签（最多10项） | 不是原始 `flight` 对象；不改。 |
| 7506 | `print` | `basic.passenger_count` | 单一总数字段；不改。 |
| 7510 | `print` | `preferences.passengers` | 字段级乘客构成，属2026-08-21已登记的日志隐私风险；不是本笔新增的完整对象 sink，登记待后续分级处置。 |
| 7514 | `safe_log_json` | `_subscription_log_summary(subscription)` | 原完整订阅 dump；本笔改为精确白名单摘要。 |
| 7518 | `print` | 推送使用的总人数 | 单一总数字段；不改。 |
| 7519, 7523, 8362 | `print` | 场景代码字段 | 字段级行程元数据；不是完整对象 sink，登记待后续分级处置。 |
| 7901 | `safe_log` | 预算判断与数值一致性 | 标量诊断；不改。 |
| 8928 | `print` | 无方案主因分类键 | 枚举诊断；不改。 |
| 10957 | `print` | 航班标识 | 白名单允许字段；不改。 |
| 10963 | `safe_log_json` | `_flight_log_summary(flight, ...)` | 原完整航班 dump；本笔改为精确白名单摘要。 |
| 11902 | `print` + `repr` | `route_airports` 子结构 | payload字段级路线元数据；不是完整payload dump，登记待后续分级处置。 |
| 11958, 12852, 12860, 14832 | `safe_log` | 异常文本 | 经过有限文本脱敏；不是领域原对象。本笔未改异常控制流。 |
| 12869 | `json.dumps` | provenance值转显示文本 | 返回给渲染器，不是日志 sink；不改。 |
| 12890 | `safe_log` | 缺失统计键 | 标量键名；不改。 |
| 13543, 13547 | `print` | 价格数值数组 | 数值诊断；不改。 |
| 13867 | `print` | 日历乘客构成字段 | 字段级乘客构成；登记待后续分级处置。 |
| 13882, 13888, 14074 | `safe_log` | T曲线与渲染计数 | 标量诊断；不改。 |

`_log_notification()` 写入的是已经渲染的通知正文，不直接序列化 `subscription`、`flight`、`payload` 或 `plan` 原对象。本笔不改变该交付归档语义。

## 4. 两个已修 sink

### 4.1 订阅摘要

允许字段固定为：`subscription_id`（复用共享掩码）、`origin`、`destination`、`depart_date`、`return_date`、`route_type`、`passenger_count`（仅总数）、`notification_method`（仅枚举）。

不进入摘要：场景代码、邮箱、地址、token、乘客类型构成、完整约束、完整通知配置及未知字段。

### 4.2 航班摘要

允许字段固定为：`flight_combo`、`source`、`price`、`origin`、`destination`、`departure_time`、`arrival_time`、`stops`、`missing_fields`。

不进入摘要：`raw`、`booking_options`、链接或URL、`segments` 原文、供应商原始响应及未知字段。

非dict输入只记录 `summary_unavailable` 与 `input_type`，不调用对象的 `repr()` 或 `str()`。

## 5. notifier.py 外登记项

| 文件 | 调用点 | 对象 | 本笔处置 |
| --- | --- | --- | --- |
| `plan_tracker.py` | 754, 797, 1001, 1027 | 上次/本次方案对象 | 登记；不修改。需另案设计方案追踪白名单摘要，避免影响既有追踪诊断。 |
| `sources/aggregator.py` | 1292-1295 | 完整 `source_stats` | 登记；不修改。需另案区分源健康计数与源响应证据。 |
| `test_full.py` | 181 | 测试fixture的 `source_stats` | 测试专用输出，不是生产 sink。 |

未确认真实凭据曾通过这些路径泄露；只能确认这些路径存在输出完整结构的能力。

## 6. 脱敏合同

- 结构化值先经 `redact_value()`，再进入确定性单行JSON；禁止 `default=str`。
- 凭据、邮箱、电话分别输出 `***`、`<EMAIL>`、`<PHONE>`。
- key标准化覆盖大小写、camelCase与连字符；`route_key`、`request_key` 不因泛化 `_key` 规则被误脱敏。
- 未知对象输出 `<OBJECT:ClassName>`；循环与超深结构输出安全标记。
- 超过长度上限时输出 `truncated/chars/redacted_sha256` 元数据，不做字符切片。
- AST合同禁止 `notifier.py` 的 `print/safe_log` 再直接插值或序列化完整领域对象。

## 7. RED/GREEN证据

- RED：新增合同在基线上为 `13 failed`，精确命中原 `subscription` 与 `flight` 的两个 `json.dumps(..., default=str)`。
- GREEN：隐私、轮档与主链指纹定向合同为 `21 passed`。
- 基线全量：`pytest 1400 passed`；`unittest` 返回0。
- 主链源码指纹中仅 `build_notification_payload` 因授权的日志调用替换而更新；`render_email`、`render_detail_html` 与 `render_pushplus_sections` 指纹不变。本项不是冻结基线重生成。

## 8. 事实与边界

可以确认：完整 `subscription` 曾被序列化到诊断；完整 `flight` 可能携带 `raw`、`booking_options` 与预订URL；结构丢失后的文本脱敏不能可靠恢复敏感键语义。

不能据此声称：每条订阅都含PushPlus token、真实密钥已经泄露、或历史日志已经清理。

本提交是 forward-only 防线。既有 `run_latest.log`、轮档、`notifications_log` 与备份归档均未删除、改写或自动清洗；历史清理由独立的默认dry-run流程另行裁决。

## 9. 最终验收

- 完整 `pytest`：`1414 passed`。
- 完整 `unittest`：`Ran 1414 tests`，`OK`。
- 脱敏冻结邮件：`sha256=1dd353d2b7bcafc02087957ccccb6661478e20472a22fb3e15dae6a56473ee6b`，`53307 bytes`。
- PushPlus短档：当前与基线 `614d114` 均为 `sha256=2ac0e80d9f82dbe7ebccf524c0832f09d42d916212b6178316b0b7bcd60574ff`，`3769 chars`。
- 零网络合同：socket拒绝器触发数 `0`；`httpx` 与SMTP mock调用均为 `0`；`NO_LIVE_API=1`。
- 十一场景与冻结双档定向回归：`114 passed`。

第一次验收哈希窗口与本机21:00计划采集重叠：生产文件在 `21:00:45` 至 `21:01:08` 被该外部轮次写入，同时存在 `21:00:01` 启动的Python采集进程。因此该次变化不作为本提交的零副作用证据。待进程自然结束后重新建立静稳基线，并在同一命令中重跑双收集器和比较前后哈希，结果如下：

| 文件 | 静稳窗口SHA-256 | 前后 |
| --- | --- | --- |
| `data/prices.db` | `1b5790a201b6ae0fc53aa33ab99bfe3ad268017fd812474cb51c2fd14c7022a2` | 相同 |
| `data/observations.sqlite3` | `0a68b3d45d1ded2c53e02e6378fc090906c2a8bddd5173d6332e90a66a5788d0` | 相同 |
| `data/api_usage.json` | `67aa7acdfe990e63fb2689d3af40cf8c0e3705c3051dc361c0775b4ed50e71dd` | 相同 |
| `data/subscriptions.json` | `9100f64c224eb7db0a2d18c4fe0e37221e26ff8d6f250b822285f6be4a957af5` | 相同 |
| `data/runtime_config.yaml` | `c1652e99d6f0a6892303016065bd2f5ec2dffe66b9d502e4f6d5f1c6555d1664` | 相同 |

静稳窗口结论：`production_state_changed=false`，`api_usage`未增加，真实API调用为0。
