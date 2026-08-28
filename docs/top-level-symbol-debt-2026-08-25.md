# 顶层符号重名基线审计

审计日期：2026-08-25  
基线：`e435f1b81f5a27d56a43ee443ccf2b4793eb28ac`

扫描器遍历模块级控制流分支（`If`、`Try`、`Match`、循环与 `With`），但不进入函数和类的局部作用域；函数、异步函数与类共享同一模块命名空间。显式平台条件白名单当前为空。

## 基线结果

基线共命中 8 组重复顶层符号：

| 文件 | 符号 | 定义位置 | 本次处置 |
| --- | --- | --- | --- |
| `analyzer.py` | `travel_profile_explanation` | 3374, 3414 | 登记待办，不在本提交修改 |
| `analyzer.py` | `transfer_risk` | 5412, 5538 | 登记待办，不在本提交修改 |
| `analyzer.py` | `verify_fare_rules` | 6437, 6765 | 登记待办，不在本提交修改 |
| `analyzer.py` | `calc_confidence` | 7103, 7168, 7229 | 登记待办，不在本提交修改 |
| `analyzer.py` | `determine_push_type` | 7431, 8178 | 登记待办，不在本提交修改 |
| `email_notifier.py` | `build_trend_png` | 50, 98 | 删除第一版死代码，保留第二版原文 |
| `notifier.py` | `format_flight_detail` | 2085, 5226 | 登记待办，不在本提交修改 |
| `price_calendar.py` | `analyze_date_savings` | 179, 225 | 登记待办，不在本提交修改 |

其余 7 组是已知历史债务，不是白名单。合同固定这份债务集合：新增重名会使测试失败；清理历史债务时必须同步删除对应记录。

## 第七批串行清理进度

### 1. 订阅列表结论文本渲染

- 状态：`fixed_rendering_defect`；本项不是顶层重名，精确债务集合仍为 7 组。
- 基线：`1947e35`；引入提交：`8d12faa`；生效位置：`web_form.py::_subscription_last_decision`。
- 清理提交：本提交 `fix: render subscription conclusion text`；精确 SHA 由提交后闸门报告记录（提交无法在自身树中保存自身 SHA）。
- 特征测试：`test_subscription_decision_text_normalizes_current_and_legacy_shapes`、`test_subscription_list_renders_decision_dict_as_text_not_python_repr`。
- 删除前后：旧字符串输出完全相同；当前字典 payload 从 Python `dict` repr 修正为 `conclusion`，无 `conclusion` 时取 `label`。
- 剩余顶层重复符号：7。

完整考古结论：

1. 这里没有多个定义版本。新增的服务端规范化签名为 `(value) -> str`，无默认参数、装饰器或原地修改。完整字典优先 `conclusion`，仅 `conclusion` 取该值，仅 `label` 取该值，无两字段取空串；旧字符串保持 `str(value)`，`None` 取空串。价格、相对时间、排序、日志和异常路径不变。
2. `_subscription_last_decision` 在模块加载完成后才由 `build_subscription_list_items` 调用。两者之间不存在模块级调用；全仓也没有循环导入在 `web_form.py` 执行完成前读取该局部辅助符号。
3. 全仓引用只存在于订阅列表构建路径和对应测试；未发现 `from ... import`、`getattr`、`__all__`、patch、模块级注册器、装饰器或默认参数表达式持有旧行为。
4. 基线 RED 同时捕获缺失规范化函数和页面泄露 `{'label': ...}`；GREEN 锁定五种输入形态、完整页面文本、价格格式与不得出现 Python 字典 repr。

### 2. `price_calendar.analyze_date_savings`

- 状态：`removed_as_shadowed`。
- 基线：`f399b26`；第一版引入提交：`fa57a7b`；第二版引入提交：`c809e5f`。
- 清理提交：本提交 `refactor: remove shadowed date savings implementation`；精确 SHA 由提交后闸门报告记录。
- 生效版本位置：清理后 `price_calendar.py::analyze_date_savings` 唯一定义。
- 特征测试：`test_analyze_date_savings_characterizes_active_single_leg_implementation`、`test_analyze_date_savings_characterizes_invalid_inputs_and_exceptions`。
- 删除前后输出：完全相同；生效版提示末尾保留 `/单程`。
- 剩余顶层重复符号：6。

完整考古结论：

1. 两版签名、默认参数、返回类型、过滤条件、排序、截断、空值和异常行为相同，均不原地修改输入。唯一运行差异是第一版 `tip` 以 `省¥X` 结尾，第二版以 `省¥X/单程` 结尾；第二版文档字符串也明确单程口径。
2. 两个定义相邻，中间没有模块级调用。`analyzer.py` 仅在 `price_calendar.py` 完整执行后通过别名导入该函数；模块不存在反向导入，未发现循环导入提前读取第一版。
3. 外部引用只有 `analyzer.py` 的静态别名导入和测试调用；未发现 `getattr`、`__all__`、patch、注册器、装饰器或默认参数表达式持有第一版对象。
4. 删除前特征测试锁定无效当前价、过去日期、目标日期、阈值边界、节省额倒序、`limit`、完整字段和值类型、`/单程` 文案、输入不变以及异常类型；删除后同组测试输出完全一致。

### 3. `notifier.format_flight_detail`

- 状态：`removed_as_shadowed`。
- 基线：`50951d4`；第一版最初引入提交：`df5c383`，后由 `8b8e502` 和 `61b9109` 扩展；第二版引入提交：`61b9109`。
- 清理提交：本提交 `refactor: remove shadowed flight detail formatter`；精确 SHA 由提交后闸门报告记录。
- 生效版本位置：清理后 `notifier.py::format_flight_detail` 唯一定义，委托 `_payload_plan_leg`。
- 特征测试：`FormatFlightDetailCharacterizationTest` 的 6 项逐字输出矩阵。
- 删除前后输出：完全相同；生效版继续返回三参数纯文本摘要，不恢复第一版富 HTML 详情。
- 剩余顶层重复符号：5。

完整考古结论：

1. 第一版签名为 `(flight, date_str=None, label=None, route_info=None, analysis_result=None) -> str`，第二版为 `(flight, date_str=None, prefix="") -> str`，均无装饰器。第一版生成带 `<br>` 的富详情，包含状态、建议、路线、日期、时区、经停、时长、机型、估价、渠道、价差、新鲜度和风险；第二版只委托 `_payload_plan_leg`，输出固定顺序的纯文本时刻/经停/价格摘要。第二版会规范化 `None`，忽略 `date_str`，不展示行李与退改；五参数调用按当前行为抛 `TypeError`。两版均无排序、日志和入参原地修改。
2. 第一版之后只有函数定义和常量赋值，没有模块级调用或对象捕获；所有调用都在函数体内，运行时从模块全局读取最终绑定。`notifier.py` 的本地导入链未发现反向导入 `notifier`，不存在循环导入在模块执行完成前暴露第一版。
3. 全仓未发现 `from notifier import format_flight_detail`、属性/getattr、`__all__`、patch、注册器、装饰器或默认参数表达式持有旧对象。模块内共有 6 个调用点：4 个三参数路径消费当前版本，2 个五参数旧路径按当前签名会抛 `TypeError`；后续于 2026-08-26 按下方“旧调用约定审计修正”完成修复。
4. 删除前矩阵锁定国内/国际直飞、一次中转、多航段、缺时刻与机型、空 segments、仅 combo、`None`、特殊字符的纯文本与后续 HTML 转义边界、票规/行李缺失、返回类型、字段顺序、日期参数现状、入参不变及五参数异常类型；删除后必须逐字一致。

### 4. `analyzer.travel_profile_explanation`

- 状态：`removed_as_shadowed`。
- 基线：`f008335`；第一版引入提交：`6307ca6`；第二版引入提交：`0279a8b`。
- 清理提交：本提交 `refactor: remove shadowed travel profile explanation`；精确 SHA 由提交后闸门报告记录。
- 生效版本位置：清理后 `analyzer.py::travel_profile_explanation` 唯一定义。
- 特征测试：`TravelProfileExplanationCharacterizationTest` 的 6 项完整字典矩阵。
- 删除前后输出：完全相同；多场景顺序、冲突取舍和中文标点保持原样。
- 剩余顶层重复符号：4。

完整考古结论：

1. 两版签名均为 `(profile: dict | None) -> dict`，无默认参数或装饰器。第一版只解释单个 `scenario`，返回 5 个字段；第二版按输入顺序规范化 `scenarios`，返回 7 个字段，新增 `scenarios` 与 `tradeoff`，并改写单场景依据文案。未知场景在第一版标签回退“个人出行”，第二版保留原值；`None`/假值走默认画像，真值非映射均抛 `AttributeError`。两版都不排序、不记录日志、不原地修改输入。
2. 两个定义相邻，中间没有任何模块级节点；内部调用只在 `analyze_all_flights` 和 `analyze_round_trip` 函数体内，均在运行时读取最终全局绑定。`notifier.py` 在 `analyzer.py` 完整加载后静态导入，且 `analyzer.py` 不反向导入 `notifier`，不存在循环导入提前暴露第一版。
3. 外部引用只有 `notifier.py` 的静态导入与 payload 消费；未发现属性/getattr、`__all__`、patch、注册器、装饰器或默认参数表达式保存第一版对象。
4. 删除前矩阵锁定默认个人画像、个人/商务/旅游/家庭亲子/老人同行/价格优先、未知场景、多场景拼接顺序、场景首项优先、四类取舍分支优先级、完整返回字段与类型、维度映射、库存透传、中文标点、入参不变及异常类型；删除后必须逐字一致。

### 5. `analyzer.transfer_risk`

- 状态：`removed_as_shadowed`。
- 基线：`95cd952`；第一版引入提交：`58fba3b`；生效版 helper 与兼容包装器引入提交：`d1bf1bb`。
- 清理提交：本提交 `refactor: remove shadowed transfer risk implementation`；精确 SHA 由提交后闸门报告记录。
- 生效版本位置：清理后 `analyzer.py::calc_transfer_risk` 与唯一的 `analyzer.py::transfer_risk` 兼容包装器。
- 特征测试：`TransferRiskCharacterizationTest` 的 7 项完整风险字典矩阵。
- 删除前后输出：完全相同；风险等级、分数、现行原因码载体 `factors` 的值与顺序均保持原样。
- 剩余顶层重复符号：3。

完整考古结论：

1. 两个 `transfer_risk` 签名均为 `(flight: dict) -> dict`，无默认参数或装饰器。第一版直接返回 `level/label/notes`，使用 `green/yellow/red`，短中转阈值为 75 分钟，并专门提示美国转机；生效版包装器委托 `calc_transfer_risk`，返回 `level/label/score/factors`，使用 `none/low/medium/high`，按停站数、中转分钟、航司集合和特定过境机场累计分数。生效版会对航司去重排序，并按“停站→中转分钟→跨航司→过境机场”固定顺序生成 `factors`；不读取换机场、显式非联程、过夜标志或老幼场景。两版都不记录日志、不原地修改输入；空字典在生效版视为直飞，`None` 或非字典 layover 按现状抛 `AttributeError`。
2. 第一版之后只有 `calc_transfer_risk` 的函数定义，随后最终包装器立即覆盖第一版；两处之间没有模块级调用、注册或对象捕获。生产调用位于后续函数体内，运行时读取最终全局绑定；外部模块均在 `analyzer.py` 完整执行后导入，且未发现反向循环导入提前读取第一版。
3. 全仓外部只消费航班 payload 内已经生成的 `transfer_risk` 字段；未发现 `from analyzer import transfer_risk`、`analyzer.transfer_risk`、`getattr`、`__all__`、patch、模块级注册器、装饰器或默认参数表达式保存第一版对象。模块内直接调用仅在 `analyze_all_flights` 后段；`calc_execution_grade` 直接调用当前 helper，不经过旧定义。
4. 删除前矩阵锁定直飞、合理中转、短/长中转及 90/120/480 分钟边界、跨航司非联程、显式自行中转、换机场、过夜、信息缺失、多次中转、过境机场、老幼场景、完整字段顺序与类型、分数、`factors` 原因顺序、入参不变及异常类型；显式字段当前被忽略的行为也被锁定，删除后必须逐字一致。

### 6. `analyzer.verify_fare_rules`

- 状态：`removed_as_shadowed`。
- 基线：`b2f9cbf`；第一版引入提交：`7c63d43`；生效版引入提交：`048e502`，国际线推断措辞后由 `784d8bb` 修订。
- 清理提交：本提交 `refactor: remove shadowed fare rule verifier`；精确 SHA 由提交后闸门报告记录。
- 生效版本位置：清理后 `analyzer.py::verify_fare_rules` 唯一定义。
- 特征测试：`VerifyFareRulesCharacterizationTest` 的 8 项返回与票规写回矩阵。
- 删除前后输出：完全相同；返回字段、顺序、文案、证据字段与国内票规写回行为均保持原样。
- 剩余顶层重复符号：2。

完整考古结论：

1. 两版签名均为 `(flight, hard_constraints)`，无默认参数、装饰器或返回注解，返回键均为 `level/label/matches/issues`。第一版只消费已有票规；生效版先把假值航班归一为空字典，并为国内航班调用 `_ensure_domestic_fare_rules`，用航司/舱位标准规则覆盖写回 `flight["fare_rules"]`。生效版额外识别 `baggage.included`、退改等级与 `required` 偏好，使用票规标签/备注，区分国内与国际的系统推断出处；行李、退改、基础舱和跨航司分支顺序固定。两版都不排序、不记录日志，也不修改约束；生效版国际输入不变，国内输入会原地写回票规，`None` 返回完全匹配，真值非映射输入按现状抛 `AttributeError`。
2. 两定义之间只有 10 个函数定义，没有模块级调用、赋值、注册或旧对象捕获；生产调用在后续 `analyze_all_flights` 函数体内，运行时读取最终绑定。`domestic_fare_rules.py` 不反向导入 `analyzer.py`，也未发现其他循环导入在模块执行完成前读取第一版。
3. 外部静态导入只在 `test_domestic_fare_rules.py` 和 `test_email_polish.py`，均发生于模块完整加载后；未发现 `analyzer.verify_fare_rules`、`getattr`、`__all__`、patch、注册器、装饰器或默认参数表达式保存第一版对象。通知层只消费 `fare_verification` payload，不持有函数引用。
4. 删除前矩阵锁定完整票规、明确/缺失行李、明确/缺失退改、来源冲突、仅系统推断、支付页待确认、廉航、多人混舱、基础舱、跨航司、空值与异常；同时锁定返回键类型与顺序、重复标签/原因现状、国际不变、国内原地覆盖，以及 `source/source_note/baggage.level/refund.level/change.allowed` 证据字段，删除后必须逐字一致。

### 7. `analyzer.calc_confidence`

- 状态：`removed_as_shadowed`。
- 基线：`fa4ebe4`；三版分别引入于 `f36cf90`、`8c77a24`、`225a2a8`，第一版年龄解析后由 `66755776` 修订。
- 清理提交：本提交 `refactor: remove shadowed confidence calculator`；精确 SHA 由提交后闸门报告记录。
- 生效版本位置：清理后 `analyzer.py::calc_confidence` 唯一定义。
- 生效函数删除前源码 SHA-256：`69059cdb0a89bec29d52bc54db9db889425ccd6ad2882f458d52abcb4a7568a7`。
- 特征测试：`CalcConfidenceCharacterizationTest` 的 8 项五维置信度与来源覆盖矩阵。
- 删除前后输出：完全相同；最终等级、五个分项、细节原因、字段顺序、无独立 `reason_codes` 的现状及异常类型均保持原样。
- 剩余顶层重复符号：1。

完整考古结论：

1. 三版签名均为 `(flight: dict, source_stats=None, price_history=None) -> dict`，无装饰器，返回键均为 `overall/dimensions/details`。第一版把 `likely_available` 记为“高”、采用 availability 自带标签，以“高”分项数量计算总体等级，渠道与票规措辞较短；第二版把可购买性改为“中高/中/低”及固定支付页措辞，渠道说明改为“可交叉验证”，总体等级改按“中及以上”分项数量；第三版完整继承第二版，再按 `route_type/data_source/primary_source` 覆盖国内聚合主源、国内仅 Google、国际 Google 多源的渠道与可购买性说明。三版均不排序、不记录日志、不原地修改输入；假值输入归一为空字典，年龄转换错误降为未知，真值非映射航班抛 `AttributeError`，真值且不可取长度的历史样本抛 `TypeError`。
2. 当前实际生效为第三版。AST 显示三定义在模块顶层连续出现，定义之间没有调用、赋值、注册或旧对象捕获；两个生产调用均位于后续函数体，运行时读取最终全局绑定。`notifier.py` 在 `analyzer.py` 完整加载后静态导入最终符号，`analyzer.py` 不反向导入 notifier，未发现循环导入提前读取前两版。
3. 外部生产引用只有 `notifier.py` 的静态导入；模块内有两个运行期调用。未发现 `analyzer.calc_confidence`、`getattr`、`__all__`、patch、注册器、装饰器或默认参数表达式保存旧对象。通知层优先消费分析 payload 的 `confidence_breakdown`，缺失时才调用最终绑定。
4. 删除前矩阵锁定价格新鲜度 30/31/120/121 分钟边界、历史样本 4/5/13/14 边界、数据源 1/2/3 边界及三层回退、票规完整/部分/缺失、可购买性三态、国内聚合主源、国内仅 Google、国际多源、采集失败与退化字段当前不产生独立分项或 `reason_codes` 的事实、总体“高/中高/中”阈值、完整字段类型与顺序、细节原因、入参不变及异常类型。

### 8. `analyzer.determine_push_type`

- 状态：`removed_as_shadowed`。
- 基线：`0a2b4a4`；第一版引入提交：`8503b92`；生效版引入提交：`8c77a24`，同日冲突、预算口径理由、文案与历史样本门控后由 `87d6bc36`、`784d8bb`、`dd379ef2`、`952a01da` 修订。
- 清理提交：本提交 `refactor: remove shadowed push type decision`；精确 SHA 由提交后闸门报告记录。
- 生效版本位置：清理后 `analyzer.py::determine_push_type` 唯一定义。
- 生效函数删除前源码 SHA-256：`3961f1d0b6fb124496e1731cdac8d75409f85ba5a43bd119b8d6f466190beb68`。
- 特征测试：`DeterminePushTypeCharacterizationTest` 的 8 项完整返回、优先级与异常矩阵。
- 删除前后输出：完全相同；返回类型、字段顺序、九类触发、理由顺序、价格角色、样本门控、异常类型及空候选池的既有理由均保持原样。
- 剩余顶层重复符号：0。

完整考古结论：

1. 两版签名均为 `(current_price, target_price=None, max_budget=None, price_history=None, days_to_dept=None, last_push_price=None, analysis_result=None) -> dict`，无装饰器，返回键顺序均为 `type/reasons/price_change/percentile/historical_30_price`，均不原地修改输入。第一版只用 `current_price`，分支优先级为价格失效、历史低位、目标价、邻近日、同日更优、涨价风险；生效版从 `decision_prices` 区分展示、预算比较、交易与验证价格，分支优先级固定为时间冲突、价格缺失、主价格过期、异常低价、值得验证、进入低价区间、前后日期更便宜、同日更优、价格下降、涨价风险，默认仍为同日更优。`max_budget` 在两版中都只解析、不参与独立返回分支；超预算、继续等待、采集失败与价格持平目前也不是独立 `type`，本次如实锁定而不修正。
2. AST 证实第一版之后到生效版之间只有函数定义，没有模块级调用、赋值、注册或旧对象捕获；生产调用均在后续函数体内，运行时读取最终全局绑定。`notifier.py` 在 `analyzer.py` 完整执行后静态导入，`analyzer.py` 不反向导入 notifier，不存在循环导入提前读取第一版。
3. 全仓外部引用只有 `notifier.py` 的静态导入和模块加载完成后的生产调用；未发现 `analyzer.determine_push_type` 属性缓存、`getattr`、`__all__`、patch、模块级注册器、装饰器或默认参数表达式保存第一版对象。
4. 删除前矩阵锁定四类价格角色、历史价格展平与 n=5 门槛、九类实际返回、全部 `elif` 优先级、邻近日与同日组合、上涨/下降/持平、时间冲突、价格缺失与过期、超预算和数据不完整当前的默认行为、完整字段类型与顺序、最多四条理由、入参不变及异常类型。生效版 `_matched_constraint_reasons` 在空候选池仍追加“符合你设置的直飞条件”的既有行为也被精确锁定，本批不借清死代码改变业务。

## 旧调用约定审计修正（2026-08-26）

### 定性

本次缺陷不是“删除被覆盖定义后，调用从正常变成 TypeError”。Python 模块加载完成后，相关调用早已解析到最后一个生效定义；真正问题是部分调用点仍按历史旧签名传参，只因路径尚未被现有夹具触达而潜伏。运行时探针只用于确认基线可达性，最终生产代码不保留探针。

| 符号 | 当前生效契约 | 调用与返回消费审计 | 结论 |
| --- | --- | --- | --- |
| price_calendar.analyze_date_savings | (calendar, target_date, current_price, *, threshold=100, limit=3) -> list[dict] | 生产仅 analyzer.analyze_price_calendar 三位置参数调用；结果作为 savings 透传，通知层读取当前字段 date/weekday/price/save/tip，未传旧关键字或消费旧字段。 | 无旧约定。 |
| notifier.format_flight_detail | (flight, date_str=None, prefix="") -> str | 6 个生产调用中原有 4 个符合契约；_round_trip_option_line 与 format_comparison_message 原传 5 个位置参数，已统一改为 3 参数。测试中仍保留一次直接 5 参数调用，用于锁定公开函数会拒绝历史签名，不是生产调用。 | 发现并修复 2 处。 |
| analyzer.travel_profile_explanation | (profile) -> dict | 分析层 2 处、通知回退 2 处均传 1 参数；通知消费当前字段 dimensions/scenario_label/basis/tradeoff，未按旧版字段读取。 | 无旧约定。 |
| analyzer.transfer_risk | (flight) -> dict | 生产仅 1 参数调用；下游消费当前 level/factors，未读取旧版 notes。 | 无旧约定。 |
| analyzer.verify_fare_rules | (flight, hard_constraints) -> dict | 生产调用完整传入 2 个必填参数；下游消费两版共有的 level/label/matches/issues，无遗留参数或字段。 | 无旧约定。 |
| analyzer.calc_confidence | (flight, source_stats=None, price_history=None) -> dict | 三个历史版本签名完全相同，顶层返回结构均为 overall/dimensions/details；分析层 2 处和通知回退 2 处均传 3 参数，下游只消费这 3 个当前字段。三版差异仅在分项值、文案和渠道覆盖逻辑。 | 无旧约定。 |
| analyzer.determine_push_type | 7 参数，后 6 个均有默认值；返回 type/reasons/price_change/percentile/historical_30_price | 通知层 2 处均传 7 个位置参数；下游读取当前字段，后续新增的 source_degradation 由通知层显式附加，不是假定旧返回字段。 | 无旧约定。 |

间接引用审计未发现上述七个符号经 patch()、getattr()、__all__、模块级注册器、装饰器或默认参数表达式保留旧对象；测试构造的唯一旧签名是 format_flight_detail(..., 5 args) 的有意异常契约。

### 可达性裁决

- `_round_trip_option_line` 的完整链为 `_append_simple_top3` → `_append_round_trip_recommendations` → 非空 `flights[:limit]` → `_round_trip_option_line`。当前仓内没有 `_append_simple_top3` 调用方，但非空航班列表在数据模型中可以成立，也没有 negative test 证明永不执行；结论为“无法确定，按可达处理”。
- `format_comparison_message` 当前没有仓内调用方，但它是模块公开函数；当 `analysis_result.recommendations` 非空时进入旧调用。直接运行还会先遇到未定义的 `_days_to_depart/_city_label/_plan_title/_summary_text`；隔离 characterization 显式替代这些独立依赖后，确认能抵达并触发五参数 `TypeError`。结论同为“无法确定，按可达处理”。
- 临时探针分别放在两处旧调用前。标准通知、无符合方案、数据不完整和脱敏冻结复放四个完整渲染入口均成功，两个探针在每个入口的命中数均为 `0`；这只证明当前主渲染器未消费旧 helper，不满足“当前数据模型下触发条件不可成立”等四项不可达条件，不能据此删除路径。
- 隔离调用 `_round_trip_option_line` 与替代独立依赖后的 `format_comparison_message` 时，两个探针各命中 `1` 次，随后均在旧五参数调用抛出同一 `TypeError`。判定结束后探针已从生产代码删除。

### RED / GREEN 证据

1. 两条测试先分别锁定基线五参数调用的 `TypeError: format_flight_detail() takes from 1 to 3 positional arguments but 5 were given`。
2. 测试改为期待两条路径的完整三参数输出后，修复前两条均按同一 `TypeError` 失败（RED）。
3. `_round_trip_option_line` 改为 `format_flight_detail(flight, date_str, _option_label(index))`，`format_comparison_message` 改为 `format_flight_detail(flight, depart_date, "")`；两条完整输出与输入不变断言均通过（GREEN）。
4. AST 调用契约扫描 `notifier.py`，生产调用若超过 3 个位置参数、使用展开参数或传入未知关键字即失败。
5. 全仓 AST/`inspect.signature.bind` 审计其余六个符号的生产和测试调用，未发现位置参数、关键字、必填参数或展开参数违约；唯一违约是 characterization 中有意保留的直接五参数异常测试。`calc_confidence` 清理前三版签名均为 `(flight, source_stats=None, price_history=None)`，顶层返回键均为 `overall/dimensions/details`。

## 图表基线

生效版本是第二版 `build_trend_png`：`6×2.8`、全日期旋转标签、当前价标注、`bbox_inches="tight"`。删除前记录：

- 生效函数源码 SHA-256：`21f2ba56d736dc088f9df3d8457332cb542c3af9d952e918afb385342a9eae34`
- 固定输入 PNG SHA-256（本机同环境）：`bcae1e376da38e95c4785bb6814c5effc8a620bf64f939375f37a4f05dced328`
- 固定输入 PNG 字节数：`25671`

源码指纹进入跨平台合同；PNG 哈希用于本机改前改后对照，避免字体后端差异造成跨平台假红。

## F821 旧通知子图后续裁决（2026-08-26）

此前对 `_append_simple_top3` 链的“无法确定”结论已由更完整的全仓调用图审计取代：
已跟踪 Python AST 与全部已跟踪文本均只发现
`_append_simple_top3` → `_append_round_trip_recommendations` 的子图内调用，未发现生产、
脚本、测试、模板、CLI、导入、属性读取、`getattr`、`patch`、注册器、默认参数或回调
上游。删除后的 negative contract 要求两个符号连定义与字符串式动态引用都为零。

同样证据支持删除另外两个私有孤立子图：`_booking_link`，以及
`_append_round_trip_score_top3` → `_round_trip_score_line`。五个被删函数及 61b9109
消失的依赖逐项记录在 `docs/f821-debt-cleanup-2026-08-26.md`。

`generate_neutral_summary` 虽无仓内调用方，但属于模块级公开兼容 API，且非空
`trend.current_position` 可构造触发 `_plain_price_position` 的 F821；因此保留为
`needs_manual_adjudication`。另有 8 项位于仍可由 `format_html_message` 兼容入口触达的
旧 structured renderer 链，同样保留待单独裁决。当前 F821 精确债务集合为 9 项。

## 旧 HTML renderer 退役（2026-08-28）

后续审计确认该链的仓内可执行上游只剩 `test_full.py` 与历史 characterization 测试，
现行 payload 通知主链没有引用。公共入口 `format_html_message` 现保留原签名并确定性抛出
`LegacyNotificationRendererUnavailable`；诊断脚本迁移到现行三渠道 renderer，两个私有旧
renderer 已删除。F821 精确债务由 9 项降为 1 项，仅余
`generate_neutral_summary::_plain_price_position`，完整调用图与源码指纹见
`docs/f821-debt-cleanup-2026-08-26.md`。

## neutral summary 最后一项 F821 清偿（2026-08-28）

后续从 `61b9109^` 找回了 `_plain_price_position` 的完整、无歧义历史实现；该 helper
消失于 `61b9109`，调用点漏迁移。基于 `89d777a` 的当前调用图确认仓内生产与动态
调用方均为 0，但仓外兼容调用不能证明不存在，因此没有删除公开函数。修复保留原签名
与 `list[str]` 返回结构，只把四种状态标记清洗语义内联到函数中，不复活旧 helper
子图。F821 精确债务集合由 1 项清空；完整 RED/GREEN、调用图与副作用合同见
`docs/f821-debt-cleanup-2026-08-26.md`。
