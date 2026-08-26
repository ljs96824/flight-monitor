# F821 存量债务清理记录（2026-08-26）

## 范围与纪律

基线为 `d45a954`，Ruff F821 精确债务集合起始为 41 个
`(文件, 作用域, 符号)` 三元组。本批只清理已有证据的历史渲染债务；不恢复
`61b9109` 已删除的 helper，也不以更新冻结基线接受行为漂移。

四个现行主链函数在批次开始时的 `inspect.getsource()` SHA-256：

| 函数 | 基线源码 SHA-256 |
|---|---|
| `build_notification_payload` | `a1be138bf0890283b2ef3c95054702139481a2bf1365d197408dfea897f21d11` |
| `render_email` | `6413876b28753cb526592764a4aea1b045ed15aefc282c207281b68626e8da98` |
| `render_detail_html` | `357034e6ab893184051c8b24b6587f6ad31c25e9c63f105ed28045dc995af3dc` |
| `render_pushplus_sections` | `47afa385aef2e2d6c8d163edd29e33f5ad4e1351fbaeaeb7318713f70000aa9a` |

## 提交 1：改名残留

### `_display_route_summary` → `format_route_summary`

`git show 61b9109^:notifier.py` 与 `git show 61b9109:notifier.py` 表明该提交只将
helper 改名：函数体逐行相同。旧签名为
`_display_route_summary(route_summary: str | None) -> str`，现签名为
`format_route_summary(route_summary) -> str`；两者均只有一个无默认值的位置或关键字参数。
类型注解的移除不改变 Python 调用合同。

| 合同维度 | 历史旧名 | 现名 | 结论 |
|---|---|---|---|
| 返回类型 | 始终 `str` | 始终 `str` | 一致 |
| `None` / 空串 | `""` | `""` | 一致 |
| 路线格式 | 大写 IATA 替换为 `中文名(IATA)` | 相同正则与 `city_name` | 一致 |
| 入参修改 | 只读取并构造新字符串 | 相同 | 不修改 |
| 日志 / 异常 | 无新增日志；沿用 `str()` 与 `city_name` 异常语义 | 相同函数体 | 一致 |

`format_alternative_message` 是现行旧文本兼容入口，其唯一错误是仍调用旧名；本次改为
调用现名，不增加适配器。

### 测试模块旧自调用

`faec1c0` 先将测试函数
`test_email_top_summary_separates_display_transaction_and_verify_prices` 改名为
`test_email_no_primary_summary_uses_no_result_title_and_keeps_price_layers`；`784d8bb`
又将它改为当前名称
`test_email_no_primary_uses_candidate_pool_reference_without_purchase_signals`，但模块底部
`__main__` 手工执行列表两次都未同步。本次直接改到当前真实定义。它验证的是测试脚本
直接运行时对现有生产邮件行为的断言，并非生产代码对旧测试符号的依赖。

### 删除记录

本提交未删除函数；仅修复两个明确的改名残留。F821 精确集合预期由 41 缩减为 39。

## 逐笔状态（提交 1 时点）

| 提交 | 状态 | 清理依据 | F821 剩余 |
|---|---|---|---:|
| 1 改名残留 | implemented in this commit | `61b9109` / `faec1c0` / `784d8bb` 历史 diff + characterization | 39 |
| 2 `format_html_message` 终止 return 后死尾 | pending | 待控制流五证齐备 | 39 |
| 3 孤立 legacy renderers | pending | 待两层调用图证明 | 39 |
| 4 孤立 legacy notification helpers | pending | 待完整旧子图证明 | 39 |

## 提交 2：`format_html_message` 终止 return 后死尾

提交 1 已落地为 `a823175`。删除前 AST 给出的外层函数体序列为：

```text
Expr, Assign, If, Return@15061,
Assign, Assign, Assign, Assign, Assign, Assign,
FunctionDef(build_message), Assign, If, Return
```

五项控制流证据：

1. `return message` 是 `format_html_message.body` 的直接子节点，不在条件、循环、
   `try/finally` 或其他控制结构中。
2. 排除嵌套函数作用域后，外层 `Yield` / `YieldFrom` 集合为空，外层不是生成器。
3. Python 在该无条件 return 后离开函数；尾部不存在异常处理入口、循环回边、标签或
   其他可跳入机制。
4. 删除前活跃前缀（函数定义起点至该 return 行末）SHA-256 为
   `b3868a6da7ad7c54d5f9dc74a51a46683c9582ac78bebed7fcfea0cd4dec63eb`；删除后相同。
5. 删除前后 `format_html_message` 的短消息、长消息以及现行
   `render_email` / `render_detail_html` / `render_pushplus_sections` 输出由专项与冻结回归
   验证；现行主链四函数源码 SHA 亦保持批次基线值。

删除边界严格为首个直接 `return message` 后第一条语句至函数体结束。共删除 284 个
源码行（含空行），Ruff 中 `format_html_message.build_message` 的 24 个 F821 三元组随之
消失；债务计数由 39 降为 15。

### 删除记录

| 被删函数 | 原功能域 | 相关旧 helper 消失提交 | 删除依据 | 删除前后输出 |
|---|---|---|---|---|
| `format_html_message.build_message`（嵌套） | 旧单程/往返 HTML 文本拼装、趋势与购买清单 | 其依赖的 24 个 helper 统一消失于 `61b9109` | 位于外层无条件 return 后，控制流不可达 | 完全相同 |

结构合同 `assert_no_statements_after_terminal_return(...)` 现在固定：首个直接终止 return
之后不得再出现任何语句，并固定活跃前缀 SHA。

## 逐笔状态（提交 2 后）

| 提交 | 状态 | 清理提交 | F821 剩余 |
|---|---|---|---:|
| 1 改名残留 | complete | `a823175` | 39 |
| 2 终止 return 后死尾 | implemented in this commit | this commit | 15 |
| 3 孤立 legacy renderers | pending | - | 15 |
| 4 孤立 legacy notification helpers | pending | - | 15 |

## 提交 3：孤立 legacy renderer 调用图裁决

提交 2 已落地为 `1c7d5ed`。本笔对全部已跟踪 Python 文件做 AST 审计，并以文本搜索
补齐模板、CLI 与文档引用。审计覆盖：静态名称调用、`from notifier import`、
`notifier.<name>`、`getattr`、`patch` 字符串、`__all__`、默认参数、模块注册器、返回值与
回调传递。

| 符号 | 生产/脚本/测试调用方 | 动态或间接引用 | 裁决 |
|---|---|---|---|
| `format_html_message` | `test_full.py` 在可执行诊断路径中直接导入并调用；characterization 测试直接调用 | 无 `getattr`/注册器；测试不替换该符号 | 仍是可达兼容入口 |
| `_format_structured_html_message` | `format_html_message` 三处直接调用 | characterization 测试两处 `patch` 仅隔离短/长分派 | `needs_manual_adjudication` |
| `_append_detailed_analysis_section` | `_format_structured_html_message` 一处直接调用 | 无其他动态引用 | `needs_manual_adjudication` |
| `_append_round_trip_block` | 删除前仅有定义；第 2 笔删除死尾后无名称调用 | 无 import/属性/getattr/patch/注册器/模板/CLI/回调 | 可删除 |

### 两层可达性结论

第一层不满足孤立条件：`format_html_message` 虽不在当前
`build_notification_payload → render_email/render_detail_html/render_pushplus_sections`
主链，但仍由 `test_full.py` 的命令行诊断路径调用；同时它是无下划线的历史兼容入口，
仓库证据不足以断言不存在外部调用者。

第二层也不满足删除条件：

- `_format_structured_html_message` 在进入 `detail_level == "short"` 分支前，无条件调用
  已消失的 `_append_push_trend_linechart`；公开兼容入口可直接构造到该 F821。
- 非 short 路径直接调用 `_append_detailed_analysis_section`。后者无条件触达已消失的
  `_append_purchase_checklist` 与 `_append_system_health_lines`；单程、非 compact 且有
  `current_min` 时还触达另外三个 F821。它不是孤立函数。

因此本笔不恢复 helper，也不删除这两个仍有活跃上游的 renderer；其 8 个 F821 精确
三元组继续登记为 `needs_manual_adjudication`。这不是把“当前夹具未触达”误写成死代码。

### 删除记录

| 被删函数 | 原功能域 | 相关旧 helper 消失提交 | 删除依据 | 删除前后输出 |
|---|---|---|---|---|
| `_append_round_trip_block` | 旧往返总价、Top3、全部方案与附近日期 HTML 区块 | `_append_nearby_dates` 消失于 `61b9109` | 全仓调用图只剩定义，且无动态/外部契约证据 | 完全相同 |

全仓零引用合同会扫描已跟踪 Python 文件的定义、导入、名称/属性读取和字符串式动态引用；
未来重新引入该私有 renderer 或调用即失败。本笔 F821 由 15 降为 14。

## 逐笔状态（提交 3 后）

| 提交 | 状态 | 清理提交 | F821 剩余 |
|---|---|---|---:|
| 1 改名残留 | complete | `a823175` | 39 |
| 2 终止 return 后死尾 | complete | `1c7d5ed` | 15 |
| 3 孤立 legacy renderers | complete | `735c37d` | 14 |
| 4 孤立 legacy notification helpers | implemented in this commit | this commit | 9 |

## 提交 4：孤立 legacy notification helper 子图

提交 3 已落地为 `735c37d`。本笔沿用提交 3 的全仓 AST 与已跟踪文本扫描，
覆盖定义、名称/属性读取、导入、`getattr`、`patch`、`__all__`、默认参数、注册器、
回调、模板、CLI、测试与脚本。扫描没有发现这些私有子图之外的引用：

| 旧子图 | 直接调用图 | 外部/动态引用 | 裁决 |
|---|---|---|---|
| `_booking_link` | 无调用方 | 无 | 删除 |
| `_append_simple_top3` → `_append_round_trip_recommendations` | 只有子图内这一条边；入口无调用方 | 无 | 两者一并删除 |
| `_append_round_trip_score_top3` → `_round_trip_score_line` | 只有子图内两处循环调用；入口无调用方 | 无 | 两者一并删除 |
| `generate_neutral_summary` | 仓内无调用方 | 无动态引用，但它是无下划线的模块级可导入兼容 API | `needs_manual_adjudication` |

前三个私有子图没有生产、脚本、测试或动态上游；专项合同在删除后要求五个符号的
定义与全部引用同时为零。`generate_neutral_summary` 不满足删除条件：调用者可传入非空
`trend.current_position`，稳定触发已消失的 `_plain_price_position`；characterization
锁定该可构造 `NameError` 与精确债务，避免把公开兼容面误判为死代码。

### 删除记录

| 被删函数 | 原功能域 | 相关旧 helper 消失提交 | 删除依据 | 删除前后输出 |
|---|---|---|---|---|
| `_booking_link` | 旧 Google Flights 单渠道 HTML 链接 | `_google_flights_url` 消失于 `61b9109` | 私有函数全仓只有定义，无任何静态或动态上游 | 完全相同 |
| `_append_simple_top3` | 旧 Top3 文本入口 | 下游路线 helper 消失于 `61b9109` | 私有入口全仓只有定义 | 完全相同 |
| `_append_round_trip_recommendations` | 旧往返路线标题与推荐行 | `_round_trip_city_code`、`_round_trip_date_text` 消失于 `61b9109` | 仅由同笔删除的孤立入口调用 | 完全相同 |
| `_append_round_trip_score_top3` | 旧去返程综合评分 Top3 入口 | 下游时段 helper 消失于 `61b9109` | 私有入口全仓只有定义 | 完全相同 |
| `_round_trip_score_line` | 旧评分航班明细行 | `_round_trip_time_range`、`_flight_slot_label` 消失于 `61b9109` | 仅由同笔删除的孤立入口调用 | 完全相同 |

本笔 F821 由 14 个精确三元组降为 9 个。余项不是漏清：

- 8 项属于仍由公开兼容入口 `format_html_message` 可达的
  `_format_structured_html_message` / `_append_detailed_analysis_section` 链；
- 1 项属于公开兼容函数 `generate_neutral_summary`。

它们均标记 `needs_manual_adjudication`；本批不恢复 61b9109 已删除的 helper，也不编造
替代语义。
