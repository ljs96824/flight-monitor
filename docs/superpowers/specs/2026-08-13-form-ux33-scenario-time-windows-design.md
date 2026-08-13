# UX 3.3 场景分支与时间窗归束设计

## 目标与边界

本次只调整完整设置页的概念归属和表单表面。订阅 schema、规范化输出、分析判定、金额、采集、通知正文和舱位业务语义均不改变。页面继续只使用浏览器原生 `<details>` 做折叠，不增加新的 JavaScript 显隐或状态机。

## 场景分支

`travel_scenario` 是完整页唯一的场景父控件。`trip_natures` 从原 `travel_context` 概念拆成 `business_nature`，与以下商务专属概念一起进入紧跟父控件的“商务出行”原生 `<details>`：

| 场景范围 | 概念 | 规范控件 |
| --- | --- | --- |
| 通用 | `travel_context` | `travel_scenario` |
| 商务 | `business_nature` | `trip_natures` |
| 商务 | `business_level` | `user_level` |
| 商务 | `team_arrangement` | `team_passenger_count`, `team_date_flexibility`, `same_flight_required` |
| 商务 | `reimbursement` | `reimburse_per_person` |
| 商务 | `invoice` | 发票四字段 |
| 商务 | `same_day_round_trip`、`meeting_*`、`same_day_execution` | 当天往返和会议字段 |

`reserve_overrides` 仍归“可行性参数”：UX 3.2 已明确它是通用可行性高级覆盖，不因字段名中含“会议”而改变业务归属。当前审计没有仅服务旅游场景的规范控件，因此不创建空的“旅游偏好”组。

每个 `CONCEPTS` 条目必须声明 `scenario_scope=common|business|tourism`。守卫冻结商务概念集合，并验证同一概念不能混入不同场景范围。编辑存量订阅时，商务组内有非默认值或父场景包含 `business` 时由服务端渲染 `open`。

## 时间偏好单一表面

完整页只呈现以下规范控件：

- 顶层：`time_preference`（不限/白天优先）、`allow_redeye`、`arrival_preference`；
- 通用自定义窗：`shared_departure_window_start/end`、`shared_arrival_window_start/end`；
- 分方向覆盖：八个 `outbound_*_window_*` / `return_*_window_*` 控件。

通用四字段放入“自定义时间窗”原生 `<details>`；分方向八字段放入其内嵌套 `<details>`。优先级固定为“分方向完整起止对 > 通用完整起止对 > 顶层偏好”。任一半开窗口不生效，也不覆盖下一层。

旧 `departure_time_start/end`、`arrival_time_start/end` 及八个分方向 `*_time_start/end` 不进入 `canonical_input_names`，也不出现在 HTML；`derive_time_concept_fields()` 从新控件派生原有 slots、windows、policy 和旧字段值。旧客户端仍可提交旧字段作为兼容回退，但页面不再渲染它们。

`time_preference` 用标准 radio 组呈现。渲染完整性按“一个概念表面”校验：普通控件出现一次；radio 同名选项可出现多次，但值必须唯一且只能属于时间概念。

## 舱位归位

`cabin` 继续位于“飞行偏好”，仅增加说明：“当前按全员同舱监控；混舱（如成人商务+儿童经济）为规划中特性”。本轮不新增、修改或启用任何混舱计算、筛选或派生。

## 兼容与验证

1. 现有八场景 POST 基线逐字段相等；新增第九场景锁定分方向覆盖优先级。
2. 旧订阅经 `subscription_to_form_values()` 投影、完整页重提后保持幂等。
3. 上海—大阪固定 payload 邮件 HTML 字节不变。
4. UI smoke 只点击原生 `<details>`，验证商务组、通用时间窗和分方向时间窗可达并可提交。
5. `:5000` 属于用户；smoke 使用隔离端口。全程不调用真实外部 API，API 台账不得变化。
