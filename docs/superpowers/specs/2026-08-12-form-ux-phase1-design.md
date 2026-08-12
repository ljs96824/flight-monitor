# UX 一期：六站表单结构设计

## 目标与边界

把现有“快速/精准”入口分叉改成一条六站主线。快速与精准继续作为服务端兼容口径存在，但不再由用户在入口二选一：新订阅默认 `quick`，用户展开飞行偏好或任一进阶折叠段后派生为 `precise`；编辑旧订阅时以存量值初始化。

本次只改表现层与共享文案函数。订阅字段名、JSON schema、`build_subscription` 输出、`main.normalize_subscription` 输出、分析/金额/采集逻辑均保持不变。

## 兼容基线

`scripts/capture_form_normalization_baseline.py` 通过 Flask `POST /subscribe` 捕获五类代表场景，并在临时目录中保存后调用 `main.normalize_subscription`：

1. 老人+儿童旅游
2. 商务会议
3. 当天往返
4. 单人极简
5. 团队出行

固定夹具为 `tests/fixtures/form_normalization_baseline_v1.json`。产品改动后必须对五份 `normalized_subscription` 逐字段相等。

## 六站结构

`form_structure.py` 是表单信息架构的单一真值：

1. `where` / 去哪：出发地、目的地、航线类型、机场偏好。
2. `when` / 什么时候：单程/往返、日期、日期弹性、当天往返及会议事实。
3. `who` / 谁去：场景、乘客构成、老人/儿童追问、最早动身时间、交通冗余折叠段。
4. `budget` / 预算：价格策略、最高价、理想价、价格容忍度及显式口径。
5. `flight_preferences` / 飞行偏好：场景预设芯片；展开后显示直飞、红眼、时间窗、行李、退改、航司、廉航、舱位及商务/团队/发票条件。
6. `notifications` / 怎么提醒：主目标、渠道、频率、阈值、摘要时间，并承载提交前确认入口。

模板输出 `data-station-id`、`data-field-owners`、`data-visibility-rules`、`data-advanced-depth`。原生 JavaScript 只读取属性、切换已有 input、显隐和更新服务器返回文本，不包含场景业务默认表。

## 条件显隐

`form_structure.py` 声明字段归属与可见性规则，现有 `data-show-if` 继续作为 DOM 层执行协议。隐藏条件块中的控件保持禁用，因此不会提交；服务端继续由既有 `build_subscription` 默认分支补值。

老人/儿童追问仅在对应人数或场景出现；会议、团队、当天往返、发票字段分别由对应条件控制。关闭进阶折叠不等同于条件隐藏：编辑旧订阅时即使折叠收起，已回填值仍可原样重提。

## 模式派生

模板只保留一个隐藏的 `monitor_mode` input：

- 新建且从未展开进阶区：`quick`。
- 展开第五站完整偏好或任一 `data-advanced-depth` 段：`precise`。
- 编辑旧订阅：先采用存量 `monitor_mode`；用户展开进阶区后保持 `precise`。

`monitor_mode` 的下游行为保持原样：

- `web_form.build_subscription` 决定是否接受高级字段并为 quick 清理残留。
- `analyzer.apply_default_rules` 为 quick 强制安全默认，为 precise 尊重显式设置。
- `main._normalize_subscription` 只透传该值。
- 采集器和 notifier 不直接用它决定采集深度。

## 场景预设芯片与段头摘要

`POST /defaults_preview` 接收当前表单，不保存。有效表单经 `build_subscription` 和 `apply_default_rules` 得到同一份默认结果，再由 `form_structure.build_default_chips` 生成芯片；响应同时返回 `defaults_applied`，便于契约测试证明同源。

芯片不维护独立状态。点击芯片只切换其 `field/value` 指向的底层 input，并在该 input 上标记用户显式选择；默认预览只回读并渲染，不写回或覆盖任何控件。

六站摘要由 `form_structure.summarize_stations` 生成。JavaScript 仅把响应中的文本放进对应 `data-station-summary` 节点。

## 同源约束回读

新增 `constraint_summary.py`，抽取现有 `analyzer._roundtrip_exclusion_basis` 的纯文案逻辑。分析器保留原函数名作为兼容包装，notifier 继续消费 payload 中同一列表，因此邮件输出不变。

`/defaults_preview` 在表单完整时同时返回同一函数生成的 `constraint_summary`；提交确认页只展示该字符串，不在模板中重拼规则。

## 失败与降级

- 地点或必填项未完整时，预览路由仍返回可计算的站点摘要；默认芯片和约束依据可为空，不保存任何数据。
- `/price_hint` 与地点候选校验完全保留原路由和调用方式。
- 预览请求失败时只保留当前 DOM，不阻止用户继续填写或提交。

## 二期边界

第五站仅留注释占位，未来可接 patterns、五档参考价和低价日历反馈；一期不渲染任何历史数据反馈控件。
