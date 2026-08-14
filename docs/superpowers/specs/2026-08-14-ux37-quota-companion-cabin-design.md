# UX 3.7 配额总览、同行约束归并与混舱简化

## 修改边界

- 配额总览只读取现有 `api_usage` 台账与 `source_quota_budget`，不新增计数器。
- 同行约束保留 schema 与旧 POST 兼容，但页面只呈现规范飞行偏好。
- 混舱保留完整 `cabin_allocation` schema；新页面仅支持“整类乘客归入商务舱”。
- 不修改采集、分析排序、金额计算和通知邮件正文。

## 同行约束审计

| 旧值 | 现有概念 | 处置 | 服务端派生依据 | 页面位置 |
| --- | --- | --- | --- | --- |
| `direct_preferred` | 中转偏好 | 删除重复 UI | `transfer_policy=reasonable/direct_only` | 飞行偏好·中转政策 |
| `no_redeye` | 时间偏好 | 删除重复 UI | 出发政策不允许红眼 | 飞行偏好·时间偏好 |
| `avoid_long_layover` | 中转细节 | 删除重复 UI | `short_transfer_limit=extra_3` | 飞行偏好·中转细节 |
| `need_baggage` | 行李 | 删除重复 UI | `baggage=required` | 飞行偏好·行李 |
| `need_refund_change` | 退改 | 删除重复 UI | `refund_flexibility=required` | 飞行偏好·退改 |
| `daytime_arrival` | 到达偏好 | 删除重复 UI | 到达政策为白天 | 飞行偏好·时间偏好 |
| `limited_mobility` | 无等价概念 | 迁为正规控件 | `mobility_limited=true` | 飞行偏好·行动便利 |

旧客户端直接 POST `companion_constraints` 时，显式值优先并原样接受；新双页表单不提交该字段，由上表规范控件派生。推荐依据继续消费派生后的旧 schema 字段，因此既有中文文案保持不变。

## 混舱兼容

新页面提交 `cabin_allocation_ui=types` 与可重复字段 `cabin_business_types`。选中的乘客类型整类进入商务舱，其余进入经济舱。旧八字段矩阵仍由服务端接受；编辑历史细粒度分配时以隐藏字段无损回存，直至用户主动改用类型勾选。

## 配额总览

统一格式由 `api_usage` 基于 `usage_snapshot` 生成。`juhe` 使用累计买断额度，`serpapi` 使用自然月额度并单列 reserve，`duffel` 标记不限额。轮末日志与详情页调用同一格式函数。
