# P7-A/B 预测门控安全等价审计

日期：2026-08-24
范围：邮件、PushPlus、网页详情、推荐判定四条消费路径；不修改推送业务判断、采集、金额或排序。

本文所称“等价”均指预测门控安全等价 (forecast gating safety equivalence)：四条路径的展示行为并不相同，但都不会消费未过门预测。

## 1. 第 0 步：整体可靠性是否为最短板

`forecast.assess_overall_reliability()` 把 level、shape、回测、源覆盖、regime 五个证据门转换为 `0/1`，原实现的组合源码为：

```python
value = min(item["value"] for item in components.values())
```

结论：当前实现是严格最短板语义。任一低分量都会把整体值压到 `0`，高分量不能平均、加权或补偿低分量；本任务未修改该函数。`round_id lineage` 是独立硬闸，能拒绝预测，但不会篡改上述五分量的最短板结果。

## 2. 计算与传递链

1. `main._notification_forecast()` 调用 `forecast.build_notification_forecast()`。
2. 只有 `result["eligible"]` 为真时，`main.py` 才把结果写入 `route_info["forecast"]`。
3. `notifier.build_notification_payload()` 当前复制 `route_info["tcurve"]`，但未复制 `route_info["forecast"]`。
4. `_email_forecast_body()` 虽具备读取 `payload["forecast"]` 的渲染能力，现生产统一 payload 不携带该字段。

这是“未消费”，不是“未过门预测绕过门控”。本任务没有接通预测展示。

## 3. 四条消费路径审计

| 消费路径 | 读取预测值 | 读取 T 曲线派生值 | shape n>=5 | 技能门 | 低样本/未过门实际输出 | 结论 |
|---|---:|---:|---|---|---|---|
| 邮件 | 渲染器具备能力，但统一 payload 当前未传 `forecast` | 是，`_email_tcurve_body()` 只读合格 T 格 | T 格由 `point.sufficient` 执行 n>=5；整节另需至少 3 个合格格 | 通知预测构建器通过统一裁决检查 k=3 技能门 | T 曲线不足时整节跳过并记录日志；预测当前不进入 payload | 当前不消费未过门预测 |
| PushPlus | 否 | 否 | 不适用 | 不适用 | 不输出预测或 T 曲线 | 当前不消费 |
| 网页详情 | 否 | 否 | 不适用 | 不适用 | 不输出预测或 T 曲线 | 当前不消费 |
| 推荐判定 | 否 | 否 | 不适用 | 不适用 | 推荐、预算、排序不读取诊断统计 | 当前不消费 |

## 4. 统一裁决与真实证据

`forecast.evaluate_forecast_eligibility()` 是唯一资格裁决入口，返回 `status`、`bottleneck`、`reason_codes`、`human_text` 与原始最短板明细。报告和通知构建器只负责计算并传入事实证据：

- shape：当前 T 与未来 7 天每个精确 T 格均须 n>=5；中间缺 T 不插值。
- level：目标出发日有效观测数达到门槛。
- 技能门：累计全量走前回测 k=3 门，不做滑动窗口替代。
- 源覆盖：目标出发日使用的日格不得为 degraded。
- regime：只统计与目标日型相同的出发日；默认绝不跨 regime 借样本。
- lineage：参与裁决的日格必须带非空 `round_id` lineage。

显式历史复放时，报告与通知构建器会先把全部日格截断到 `observed_day <= as_of_day`，再把截断后的集合交给走前回测；报告的航班规律查询也按同一截止日过滤 `observed_at`。未来样本不能混入历史技能门或规律区；通知侧 source 与 lineage 的正反证均由真实日格字段计算，测试不再把这两项 mock 为通过。

实现审阅曾发现“调用统一函数但传入写死真值”仍会形成实质绕过；现已改为从日格、日型和来源覆盖中计算真实证据，并由回归测试锁定。

## 5. 日型禁回退

默认路径仅使用目标 regime 的 shape。同日型样本不足时返回 `regime_insufficient`，不按 `exact -> adjacent -> all` 回退。`--diagnostic` 只列出其他 regime 的原始候选，并固定标注“原始值，不可用于判断”；候选不会进入预测数组。

## 6. 审计结论

当前四条用户消费路径满足预测门控安全等价 (forecast gating safety equivalence)：均不消费未过门预测，但不声称四条路径行为相同。报告与通知预测构建器已统一消费 `evaluate_forecast_eligibility()`，且通知侧使用真实 shape、技能门、源覆盖、regime 与 lineage 证据。预测 payload 的现有未接通状态保持不变。
