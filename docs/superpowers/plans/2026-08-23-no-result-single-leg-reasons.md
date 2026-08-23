# 无符合方案：单腿拒因补全与廉航策略归一

日期：2026-08-23

## 1. 8/23 轮档事实

证据轮次：collection_20260823T210014035475。

- 去程 PVG -> KIX 由当日面板复用，常规过滤后仍有 23 个可用航班。
- 9C6581 单人单程 CNY 2,885，stops=0，是直飞；旧文案“直飞要求不符”与航班自身字段矛盾。
- 备选 MM080、9C6575 同为直飞。
- 返程 KIX -> PVG 在该轮出现 PermissionError: [WinError 5] 拒绝访问。: data，未形成可用返程池，因此无法组成完整往返。
- [过滤明细] 中的 direct_only 拒因属于 BR 中转航班，不能借漏斗桶转贴给 9C6581。
- refund_flexibility=required 当前进入票规核验、风险与排序，不是硬过滤条件；本轮“退改导致全灭”假设不成立。

## 2. 数据归属

- 候选最低价原因：只允许按“航班组合 + 起降机场 + 完整起降时间”精确匹配该候选的结构化拒因。
- 精确拒因缺失且无完整往返时：使用该轮真实的配对失败原因，例如返程采集失败、返程候选为空、去返程无法配对。
- 单腿排除表：仅在往返订阅没有结构化排除组合时启用，合并过滤明细与未配对候选，按价格升序去重，最多 10 行。
- 已有完整排除组合时：继续使用原往返排除卡，不额外渲染单腿表。
- 备选卡：每张携带 unmet_reason，邮件与 PushPlus 均显式渲染“未达条件”。

## 3. 廉航与航司策略微审计

| 入口/消费方 | 旧行为 | 归一后 |
|---|---|---|
| 表单 LCC 控件 | 写 lcc_policy | 仍写 lcc_policy，它是廉航筛选单一真值 |
| 表单航司偏好 | airline_policy 曾同时提供 no_lcc | 删除重复 UI 选项；仅保留不限、偏好全服务、排除指定航司 |
| 场景默认 | 写 airline_policy=prefer_full_service 等偏好 | 保持不变，不替代硬廉航筛选 |
| 旧 POST / PA 旧记录 | 可能保存 airline_policy=no_lcc 且 lcc_policy=any | 兼容迁移为 airline_policy=any、lcc_policy=exclude_lcc |
| 明确 lcc_policy | 可能与旧 no_lcc 并存 | 明确值优先；旧别名只被清除，不覆盖 lcc_only/exclude_lcc |
| 过滤消费方 | 旧 airline_policy=no_lcc 使用名称列表；lcc_policy 使用航司码名录 | 新数据只走 lcc_policy 航司码名录；旧字段在载入/分析前归一 |

airline_policy 只表达航司偏好或排除指定航司；lcc_policy 只表达廉航三态。两者不再表达同一个概念。

## 4. 回归锁

脱敏夹具：tests/fixtures/no_result_20260823_v1.json。

回归覆盖：

1. CNY 2,885 的 9C6581 不再显示“直飞要求不符”。
2. 无完整往返时总体主因指向返程采集/配对，不再说“剩余完全匹配”。
3. 每张备选卡均显示“未达条件”。
4. 排除节显示逐航班拒因表，并保留 BR 中转航班自身的 direct_only 拒因。
5. 旧 no_lcc 载入、直接 POST 与分析路径均归一到 lcc_policy。