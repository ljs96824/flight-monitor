# `format_comparison_message` F821 审计

审计日期：2026-08-26
代码基线：`191652d`
范围：只修 `notifier.format_comparison_message`；其余 F821 仅登记，不在本批顺手修改。

## 1. Ruff 基线与分类

基线命令：

```powershell
python -m ruff check . --select F821 --output-format json
```

- 修复前：61 个 F821 位置、46 个唯一 `(文件, 作用域, 符号)` 三元组。
- 本函数：7 个位置、5 个唯一符号：`_days_to_depart`、`_city_label`、`_plan_title`、`_money`、`_summary_text`。
- 修复后：54 个位置、41 个唯一三元组；`scripts/check_f821.py` 输出 `[F821] exact debt matched: 41 scope triples`。

证据类别没有混用：

| 类别 | 结论 |
| --- | --- |
| Ruff F821 | 上述五个名称均为静态未定义名。 |
| `inspect.getclosurevars(...).unbound` | 包含上述五名；同时会把 `append/get/join/now` 等属性名列入，后者不是 F821，未误报为债务。 |
| 动态注入 | 生产代码没有 `setattr/getattr/__all__/注册器` 提供这些名称；旧测试曾用 `patch(..., create=True)` 注入四名，现已删除该遮蔽。 |
| 分支局部作用域 | 函数内没有这些名称的赋值，不属于“某分支未赋值”的局部变量错误。 |

## 2. 双向历史溯源

对每个名称分别执行 `git log -S`，并执行：

```powershell
git log --all --oneline -G "days_to_depart|city_label|plan_title|summary_text|_money" -- notifier.py
```

五名均在 `61b9109` 的 notifier 大改中删除，但 `format_comparison_message` 被遗留。分类如下：

| 名称 | 分类 | 历史证据 | 当前结论 |
| --- | --- | --- | --- |
| `_days_to_depart` | `removed_helper` | 父版本 `notifier.py:1101`；读取显式天数或用 `date.today()` 计算。 | 当前没有同签名 helper；不在降级文本中推算。 |
| `_city_label` | `removed_helper` | 父版本 `notifier.py:400`；IATA 经 `get_airport_city` 转中文城市。 | 输入与空值语义清晰，以新作用域 helper 复用主机场字典。 |
| `_plan_title` | `removed_helper` | 父版本 `notifier.py:1458`；拆 tag 并生成中文序号纯文本。 | 当前卡片标题是 HTML 且标签体系不同，不复活旧编号规则。 |
| `_money` | `removed_helper` | 父版本 `notifier.py:1167`；仅代理 `_price_text`。 | 数值格式可复用 `_price_text`，但旧输入无单人/全员元数据，标注“沿用输入口径”。 |
| `_summary_text` | `removed_helper` | 父版本 `notifier.py:1547`，依赖同批删除的 `format_summary_advice`。 | 旧实现含“抓住低价期/尽快购买/建议等待”等主观购买建议，不能无证据恢复。 |

## 3. 相似 helper 口径比对

| 候选 | 输入口径 | 时区/日期 | 单程/往返与价格 | HTML/纯文本 | 空值行为 | 是否复用 |
| --- | --- | --- | --- | --- | --- | --- |
| `get_airport_city` | IATA 主字典 | 无日期计算 | 与价格无关 | 纯文本 | 未知码原样回退 | 是，仅用于路线标签。 |
| `_price_text` | 调用方提供的单个数值 | 无 | 不自行判断人均/全员、单程/往返 | 纯文本 | 无效价为“暂无报价” | 是，旁加“沿用输入口径”。 |
| `_card_title` | 当前 payload 的 label/variant/primary | 无 | 与价格无关 | HTML `<div>` | label 由调用方保证 | 否；旧函数返回纯文本且编号规则不同。 |
| `generate_decision_summary` | 最低价、理想价、最高价、置信度、执行等级 | 无 | 明确预算比较语义，返回结构化 dict | 结构化数据 | 缺价有专门分支 | 否；旧函数只有 analysis+days，缺少必要预算与执行证据。 |
| `_next_step_guidance` | 完整通知 payload | 无 | 使用当前展示价/预算/场景 | 结构化 dict，另有 HTML 渲染 | 无 items 时为空 | 否；comparison 旧输入不足，复用会猜造字段。 |

## 4. 可达性证据

静态搜索未发现生产调用、导入、回调、注册器、`getattr` 或 `patch` 间接引用；`main.py` 的通知链使用 `build_notification_payload`、`render_email`、`render_pushplus_sections` 和 `render_detail_html`。

测试侧哨兵探针分别完整渲染：

| 场景 | `format_comparison_message` 命中数 |
| --- | ---: |
| 标准通知 | 0 |
| 无符合方案 | 0 |
| 数据不完整 | 0 |
| 脱敏冻结复放 | 0 |

“命中 0”只证明当前四份夹具不走该入口。该函数仍是模块公开函数，现有数据模型可直接构造调用；基线直接调用稳定触发 `NameError: name '_days_to_depart' is not defined`。因此不满足不可达四条件，按可达路径处理。

## 5. 处置

`_summary_text` 的当前语义无法从旧两参数输入无损恢复，所以没有复活旧购买建议，也没有猜用相似 helper：

1. 私有 `_format_comparison_details` 抛出领域异常 `ComparisonMessageUnavailable(RuntimeError)`。
2. 公开 `format_comparison_message` 作为直接兼容入口捕获该异常并记录 `[方案对比降级]` 结构化日志。
3. 降级文本只回显输入已有的路线、日期、显式 conclusion、最低参考价、首个方案概要、数据源和合法详情链接。
4. 用户侧固定显示“方案对比详情暂不可用,核心推荐不受影响”；不输出价格位置、涨跌判断或购买建议。
5. 输入对象不原地修改，`NameError` 不再泄漏。

## 6. 保留的精确债务

以下 41 个三元组由 `scripts/check_f821.py::KNOWN_F821_DEBT` 固定；键不含行号，也没有使用 `# noqa` 隐藏：

| 文件/作用域 | 符号 |
| --- | --- |
| `notifier.py::_append_detailed_analysis_section` | `_append_multi_window_analysis`, `_append_price_anomaly_lines`, `_append_price_references`, `_append_purchase_checklist`, `_append_system_health_lines` |
| `notifier.py::_append_round_trip_block` | `_append_nearby_dates` |
| `notifier.py::_append_round_trip_recommendations` | `_round_trip_city_code`, `_round_trip_date_text` |
| `notifier.py::_booking_link` | `_google_flights_url` |
| `notifier.py::_format_structured_html_message` | `_append_low_option_count_notice`, `_append_price_explanation_lines`, `_append_push_trend_linechart` |
| `notifier.py::_round_trip_score_line` | `_flight_slot_label`, `_round_trip_time_range` |
| `notifier.py::format_html_message.build_message` | `_append_best_overall_summary`, `_append_compact_flight`, `_append_low_option_count_notice`, `_append_multi_window_analysis`, `_append_nearby_dates`, `_append_price_anomaly_lines`, `_append_price_drop_alert`, `_append_price_explanation_lines`, `_append_price_references`, `_append_purchase_checklist`, `_append_system_health_lines`, `_cabin_price_range_text`, `_city_label`, `_companions_label`, `_evidence_text`, `_goals`, `_history_prices`, `_percentile_position_text`, `_price_sensitivity_label`, `_primary_goal`, `_refund_rigidity_tip`, `_sort_rule_text`, `_trend_arrow_line`, `_trip_rigidity_guidance` |
| `notifier.py::format_alternative_message` | `_display_route_summary` |
| `notifier.py::generate_neutral_summary` | `_plain_price_position` |
| `test_price_policy_email.py::<module>` | `test_email_top_summary_separates_display_transaction_and_verify_prices` |

新增或消失任一三元组都会使 CI 失败，并打印 `unregistered findings` 或 `resolved debt still registered`；清债必须显式更新集合。
