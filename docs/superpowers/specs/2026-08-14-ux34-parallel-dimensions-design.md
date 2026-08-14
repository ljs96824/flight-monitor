# UX 3.4 并行维度恢复设计

## 问题定性

服务端真值一直支持三条并行输入：`travel_scenarios` 是列表，`companions` 是由乘客构成派生的单枚举，`companion_constraints` 是列表。UX 3.0 之后的快速页却把 `travel_scenario` 强制渲染为单选，并且没有渲染、提交后还清空 `companion_constraints`。因此“旅游 + 家庭/亲子 + 有老人同行”会只剩第一个“旅游”，推荐依据和默认规则随之失真。

## 最小修复

1. 快速页和完整页都沿用现有 `select multiple`，让 `travel_scenario` 与 `companion_constraints` 各自成为独立多选表面；不增加 schema 值。
2. `companions` 继续由现有乘客构成规则单点派生：儿童和老人并存为 `with_elderly_child`，仅儿童为 `with_child`，仅老人为 `with_elderly`，其余多人为 `multiple`，单人为 `solo`。场景多选不会反向伪造乘客类型。
3. 快速模式保留用户显式提交的 `companion_constraints`，其余既有 quick 默认清洗保持不变。
4. 航线类型不是输入项。页面只显示只读徽章，复用 `/price_hint` 的精确地点解析与服务端 `infer_route_type()` 返回自动分类；提交端仍以 IATA 自动推导为唯一真值。
5. 商务 `<details>` 常驻 DOM，仅由第三条白名单显隐契约 `business-scenario` 控制；选择商务时显示，取消时隐藏。编辑态命中商务场景时显示并展开。
6. 快速页新增同行维度后仍守住“可见控件不超过12项”：高级的预算方式仅留在完整设置，快速提交继续使用既有服务端默认 `explicit`；预算数值与两个口径控件不变。

## 不变量

- 不改订阅 schema、规范化字段、默认规则、推荐排序、金额、采集或通知正文。
- 既有九个 POST 夹具逐字段不变；新增第十个多场景与老幼同行夹具。
- 不增加 route_type 手动控件，不复制机场分类规则到前端。
- 前端 JS 仅新增商务组显隐和只读徽章更新；地点候选、邮箱、老幼显隐契约保持原行为。
- 全程离线，不访问外部 API，不占用用户的 `:5000`。
