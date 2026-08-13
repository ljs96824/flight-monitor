# UX 2.0 概念归一与单开线性向导设计

## 目标与边界

本次只调整订阅表单的结构、显隐与表单值适配。订阅 schema、`build_subscription()` 的规范化结果、分析判定、金额、采集和通知正文均不改变。

页面从“schema 字段镜像”改为“概念表面”：每个用户概念只有一个规范控件，历史同义字段由服务端适配器派生。旧订阅编辑时先投影为规范控件，提交后再派生回原有字段，保证七场景夹具和编辑幂等。

## 控件唯一性的口径

HTML 的 radio/checkbox 选项依法共享同一 `name`。因此“`input name` 只出现一次”由渲染后的 DOM 直接校验：普通 input/select/textarea 同名只能有一个元素；radio/checkbox 同名选项必须值唯一、只归属一个 `CONCEPTS` 概念，不得跨概念出现第二表面。

这能同时拦住 `business_start/business_end` 现有的双表面，又不破坏标准 radio/checkbox 语义。

## 概念审计表

| 概念名 | 规范控件 | 派生/兼容字段 | 所在站 |
|---|---|---|---|
| ?????? | ?????? | `subscription_index` | ??? |
| 出发地点 | 城市/机场选择器 | `origin_select`, `origin_manual` -> `origin`, `origin_type` | ①去哪 |
| 出发机场范围 | 机场复选组 | `origin_airports_active` -> `origin_airports*` | ①去哪 |
| 到达地点 | 城市/机场选择器 | `destination` -> `destination`, `destination_type` | ①去哪 |
| 到达机场范围 | 机场复选组 | `destination_airports_active` -> `destination_airports*` | ①去哪 |
| 航线类型 | 路线类型选择 | `route_type` | ①去哪 |
| 出发日期 | 日期 | `depart_date` | ②什么时候 |
| 出发弹性 | 天数选择 | `date_flexibility` | ②什么时候 |
| 行程方向 | 单程/往返 | `round_trip` | ②什么时候 |
| 返程日期 | 日期 | `return_date` | ②什么时候 |
| 返程弹性 | 天数选择 | `return_date_flexibility` | ②什么时候 |
| 当天往返 | 开关 | `same_day_round_trip` | ②什么时候 |
| 当天时段 | 时段选择 | `day_trip_period` | ②什么时候 |
| 会议时段 | 开始/结束一组 | `business_start`, `business_end`, `meeting_start`, `meeting_end` | ②什么时候 |
| 会议地点 | 文本 | `meeting_location` | ②什么时候 |
| 会议重要度 | 三档选择 | `meeting_importance` | ②什么时候 |
| 出行场景 | 场景与目的组 | `travel_scenario`, `trip_natures` | ③谁去 |
| 商务层级 | 层级选择 | `user_level` | ③谁去 |
| 同行形态 | 同行/独行组 | `companions`, `solo_travel` | ③谁去 |
| 乘客构成 | 成人/儿童/老人/婴儿计数器 | `passenger_count`, `adult_count`, `child_count`, `elderly_count`, `infant_count` | ③谁去 |
| 儿童画像 | 类型选择 | `child_type` | ③谁去 |
| 老人画像 | 情况选择 | `elderly_condition` | ③谁去 |
| 同行约束 | 复选组 | `companion_constraints` | ③谁去 |
| 最早动身 | 去返时间组 | `outbound_set_off`, `return_set_off` | ③谁去 |
| 交通估算 | 分钟输入组 | `user_transport_min`, `origin_transport_min`, `destination_transport_min` | ③谁去 |
| 交通冗余 | 模式与分钟组 | `transport_margin_mode`, `redundancy_min` | ③谁去 |
| 商务冗余覆盖 | 分项分钟组 | `airport_advance_min`, `arrival_exit_min`, `delay_buffer_min`, `pre_meeting_buffer_min`, `post_meeting_buffer_min`, `custom_redundancy_min` | ③谁去 |
| 团队安排 | 人数/日期/同班组 | `team_passenger_count`, `team_date_flexibility`, `same_flight_required` | ③谁去 |
| 价格策略 | 自动/明确 | `price_strategy` | ④预算 |
| 最高预算 | 金额+口径组 | `max_budget_mode`, `max_budget`, `max_budget_scope` | ④预算 |
| 理想价 | 金额+口径组 | `target_price_mode`, `target_price`, `target_price_scope` | ④预算 |
| 历史预算口径 | 服务端兼容别名 | `budget_scope`（由规范口径派生） | ④预算 |
| 价格容忍 | 模式+自定义金额 | `price_tolerance_mode`, `price_tolerance_custom` | ④预算 |
| 报销上限 | 金额 | `reimburse_per_person` | ④预算 |
| 发票要求 | 发票一组 | `invoice_needed`, `invoice_context`, `invoice_special_vat`, `invoice_cabin_limit` | ④预算 |
| ?????? | ????????? | `monitor_mode` | ????? |
| UX2 ??????? | ?????? | `ux2_concept_form`, `ux2_time_touched`, `ux2_original_departure_time_policy`, `ux2_original_arrival_time_policy` | ????? |
| 中转偏好 | 芯片组+进阶项 | `transfer_policy`, `short_transfer_limit`, `accept_overnight_transfer`, `accept_self_transfer` -> `direct_only`, `advanced_rules.transfer` | ⑤飞行偏好 |
| 时间偏好 | 统一时间组 | 规范值派生 `time_preference`, `departure_time_policy`, 六组 `*_slots`, 六组 `*_time_windows`, `red_eye*`, `no_late_arrival`, `prefer_daytime_arrival`, `early_morning_allowed` | ⑤飞行偏好 |
| 行李 | 芯片组 | `baggage` -> `need_baggage`, `checked_baggage_required` | ⑤飞行偏好 |
| 退改 | 芯片组 | `refund_flexibility` -> `refund_policy` | ⑤飞行偏好 |
| 价格敏感度 | 芯片组 | `price_sensitivity` | ⑤飞行偏好 |
| 航司约束 | 偏好+排除组 | `airline_policy`, `exclude_airlines`, `blocked_airlines_common` -> `advanced_rules.airlines` | ⑤飞行偏好 |
| 廉航约束 | 三态芯片组 | `lcc_policy` -> `advanced_rules.airlines.lcc_policy` | ⑤飞行偏好 |
| 舱位安排 | 舱位一组 | `cabin_policy`, `cabin_arrangement`, `business_seats`, `economy_seats` -> `cabin_classes` | ⑤飞行偏好 |
| 行程确定性 | 芯片组 | `trip_rigidity` | ⑤飞行偏好 |
| 提醒主目标 | 单选组 | `primary_goal` | ⑥怎么提醒 |
| 提醒渠道 | 渠道+邮箱组 | `notification_method`, `notification_email` -> `notification_goals.method/email` | ⑥怎么提醒 |
| 提醒频率 | 单选组 | `notification_frequency`, `notification_frequency_rule` -> `notification_goals.frequency`, `advanced_rules.alerts.frequency` | ⑥怎么提醒 |
| 价格阈值 | 选择 | `price_change_threshold` -> `advanced_rules.alerts.price_change_threshold` | ⑥怎么提醒 |
| 提醒附加目标 | 复选组 | `secondary_goals` -> `notification_goals.secondary`, `advanced_rules.alerts.types` | ⑥怎么提醒 |
| 摘要时间 | 时间 | `digest_time` -> `advanced_rules.alerts.digest_time` | ⑥怎么提醒 |
| 记住偏好 | 开关 | `remember_preferences` | ⑥怎么提醒 |

## 概念层结构

`form_structure.CONCEPTS` 是唯一注册表。每项包含：

- `station_id`：唯一所属站；
- `canonical_control`：用户看到的唯一规范控件；
- `fields`：现有表单字段与服务端兼容别名；
- `derived_schema_fields`：该概念会写入的规范化 schema 路径。

`validate_concepts()` 在导入与测试中校验：所有 `FIELD_OWNERS` 字段恰好归属一个概念、概念站点与字段站点一致、无重复字段。新增字段若未显式更新注册表会失败。

## 时段适配

页面只显示以下规范控件：

- 出发时段：不限 / 白天 / 自定义；
- 红眼开关；
- 到达偏好：不限 / 白天到达 / 不晚到 / 自定义；
- 去返分别设置开关；
- 自定义时才显示对应起止时间。

服务端适配器把规范值写回原有 slot/window/policy 字段。旧测试或旧客户端若仍提交历史 slot 字段，保持原路径；只有带 UX2 规范标记的提交才走新适配，避免改变七份基线输入的含义。
???????????? slot/policy???????????? policy ? touched ???????????????????????????????????????????????????? schema?

## 单开线性向导

- 六站均为手风琴面板，桌面和移动端都只有 `currentStep` 对应站展开；
- 顶部面包屑显示 `完成✓ / 当前▶ / 未到○` 与同源站摘要，可点击跳转；
- 站④保留“完成创建（使用下方预设）”快速收尾；⑤⑥只有用户展开后才使 `monitor_mode=precise`；
- 编辑旧订阅时先投影所有值；面包屑标出有非默认进阶值的站，但仍只展开当前站，避免多面板同时打开；
- 确认页每行的“修改”链接持有 `data-summary-station` 和站点锚点，调用同一 `openWizardStation()`。

## 芯片唯一表面

场景预设不再生成第二套可点击按钮。⑤站的规范控件本身使用芯片样式；站④的预设区只展示“已预设”摘要，并通过同一个控件注册表更新⑤站控件。一个字段只有一组真实 input。

## 通知唯一表面

⑥站是通知配置唯一可编辑表面。`notification_goals` 与 `advanced_rules.alerts` 继续由 `build_subscription()` 从这组规范字段同时派生，不新增第二套 alerts 控件。

## 兼容与验证

1. 先跑现有七场景 POST 基线；重构后逐字段相等。
2. 邮件渲染不改；上海—大阪 payload 的邮件 HTML 字节哈希必须不变。
3. 编辑投影后原样提交，规范化结果逐字段相等。
4. `price_hint`、地点候选拒绝和通知渠道三态沿用原路由。
5. 全程不触发外部 API，台账和 observations 哈希不变。
