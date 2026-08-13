# P7 混舱监控 Phase 0 源能力审计

审计日期：2026-08-13<br>
审计航线：PVG -> KIX<br>
审计日期参数：2026-10-01<br>
目标舱位：business<br>
审计轮次：`audit_cabin_20260813_phase0`

## 结论

当前选择 **路线 C**：现有生产源不能同时提供可用于市场监控的经济舱与商务舱价格，暂不进入混舱 Phase 1。

- Juhe 可返回经济舱用途的单一参考票价，但官方请求参数和真实响应都没有舱位维度，无法确认商务舱价。
- Duffel API 技术上支持 `business`，本次也返回了商务舱 offer；但当前凭据的响应为 `live_mode=false`，价格来自测试航司 Duffel Airways，不具市场真实性。
- 若后续取得 Duffel live 凭据，可按同一脚本再审计一次。只有 live offer 成功并通过价格口径复核后，路线 B 才具备立项条件。

## 官方接口契约

### Juhe

[聚合数据航班订票查询接口](https://www.juhe.cn/docs/api/id/818)列出的请求参数只有 `departure`、`arrival`、`departureDate`、`flightNo`、`maxSegments` 和 `key`，没有 cabin/舱位参数。响应文档只有单一 `ticketPrice`，也没有舱位或分舱票价字段。

### Duffel

[Duffel Offer Requests](https://duffel.com/docs/api/v2/offer-requests)明确支持 `cabin_class=business`，响应的 `live_mode` 可区分 live/test 环境。<br>
[Duffel Offer schema](https://duffel.com/docs/api/offers/schema)说明 `total_amount` 是所有乘客的含税总价，不含之后追加的服务；`tax_amount` 单独列示。<br>
[Duffel test mode](https://duffel.com/docs/api/overview/test-mode/duffel-airways)明确说明测试环境的时刻和价格不真实。

## `weight=0` 历史原因

`weight=0` 不是 Duffel 报价质量评分，而是“只富化、不入价池”的角色标记：

1. `f922304` 最初接入 Duffel，提交说明已标注“待 live token 激活”。
2. `3f26b0b` 将 Duffel 从搜索源改为补充源，只提供行李和退改信息；其报价不参与推荐池。
3. `af02a2b` 在路线源档案中正式写为 `role=enrichment, weight=0.0`。

因此当前生产代码即使能拿到 Duffel offer，也只按航班组合补充 `extra`，不会把 Duffel 价格放入推荐池。

## 授权真实调用记录

预算：总计不超过 6 次，Juhe 不超过 3 次，Duffel 不超过 3 次。<br>
实际：总计 2 次，Juhe 1 次，Duffel 1 次；无重试。

### 调用 1：Juhe

- 参数：`PVG -> KIX`、`2026-10-01`、无舱位参数、`maxSegments=0`
- HTTP：200，`error_code=0`，`reason=成功`
- 返回航班：143
- 价格样本：CNY 1796、1796、1796、1796、1799
- 航班字段：`airline`、`flightNo`、`departureDate`、`departureTime`、`arrivalDate`、`arrivalTime`、`equipment`、`segments`、`ticketPrice` 等
- 舱位字段：0 个
- 判断：**商务舱不可用**。真实响应只有单一参考票价，未发现未文档化多舱字段。

### 调用 2：Duffel

- 参数：`PVG -> KIX`、`2026-10-01`、`cabin_class=business`、1 adult、`return_offers=true`
- HTTP：201
- 环境：`live_mode=false`
- 返回 offer：79
- 观测舱位：`business`
- 最低样本：USD 352.05，其中税 USD 53.70；`total_amount` 按官方定义含税、不含后加服务
- offer owner：`ZZ Duffel Airways`
- 判断：**部分可用**。技术链路支持商务舱，但当前是测试环境，时刻和价格不能用于市场监控。

## 能力矩阵

| 源 | 经济舱 | 商务舱 | 市场价格可用性 | 当前生产角色 |
| --- | --- | --- | --- | --- |
| Juhe | 可用，单一参考票价 | 不可用，无请求/响应舱位维度 | 经济舱参考可用，支付页为准 | primary，入价池 |
| Duffel test | 技术可查 | 技术可查，本次 79 个 offer | 不可用，测试价格不真实 | enrichment，`weight=0` |
| Duffel live | 接口契约支持 | 接口契约支持 | 未审计，不能先验认定 | 当前凭据未启用 |
| HasData | 历史可用 | 未在本轮重审 | 已退役 | 不参与新采集 |

## Phase 1 路线裁决

### 路线 A：Juhe 双舱

当前不可行。接口既无 cabin 参数，也不返回多舱价格。若供应商未来扩展为单次响应含多舱，固定往返相对现有采集可为 `+0` 次；若改成按舱位分别查询，则调用量需按新契约重算。

### 路线 B：Juhe 经济舱 + Duffel 商务舱

当前不可行，因为 Duffel 仍是 test 环境。取得 live 凭据并通过同脚本复审后，可重新提交用户决策。

若未来复审通过，最小增量公式为：

`每日增量 Duffel 调用 = 需要商务舱报价的唯一 (方向, 日期) 数 K`

固定日期往返的最小 `K=2`，即去程 1 次、返程 1 次。弹性日期不得隐式扩张，应继续遵循采集计划器与面板复用规则。Duffel `total_amount` 是独立可售 offer 的含税总价，不是 OTA 支付页报价；展示必须保留“以最终支付页/订单确认页为准”，且后加行李、座位等服务另计。

### 路线 C：当前源均不能提供真实混舱市场价

**本轮采用。** Phase 1 不启动，因此日增调用为 `0`。后续选择只有两类：

1. 获取 Duffel live 凭据并重新审计，满足后转路线 B；
2. 引入或恢复具备真实分舱报价的 listing 源；否则放弃混舱价格监控。

## 配额台账

| 时点 | Juhe 今日 | Duffel 今日 | HasData 今日 |
| --- | ---: | ---: | ---: |
| 审计前 | 12 | 8 | 14 |
| 审计后 | 13 | 9 | 14 |
| 变化 | +1 | +1 | 0 |

累计值：Juhe 325 -> 326，Duffel 68 -> 69，HasData 187 -> 187。

## 边界与后续决策

- 本审计没有修改采集、分析、金额、排序或推送代码。
- 未保存完整原始响应、认证信息、offer ID 或可预订链接。
- 测试 Duffel offer 只能证明字段与流程能力，不能证明真实航班覆盖、库存或价格质量。
- 在用户明确决定取得 live Duffel 报价或引入新源之前，混舱功能应保持未实现状态。
