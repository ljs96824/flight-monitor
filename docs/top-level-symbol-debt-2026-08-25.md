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
3. 全仓未发现 `from notifier import format_flight_detail`、属性/getattr、`__all__`、patch、注册器、装饰器或默认参数表达式持有旧对象。模块内共有 6 个调用点：4 个三参数路径消费当前版本，2 个五参数旧路径按当前签名会抛 `TypeError`；本次只锁定现状，不修复这些路径。
4. 删除前矩阵锁定国内/国际直飞、一次中转、多航段、缺时刻与机型、空 segments、仅 combo、`None`、特殊字符的纯文本与后续 HTML 转义边界、票规/行李缺失、返回类型、字段顺序、日期参数现状、入参不变及五参数异常类型；删除后必须逐字一致。

## 图表基线

生效版本是第二版 `build_trend_png`：`6×2.8`、全日期旋转标签、当前价标注、`bbox_inches="tight"`。删除前记录：

- 生效函数源码 SHA-256：`21f2ba56d736dc088f9df3d8457332cb542c3af9d952e918afb385342a9eae34`
- 固定输入 PNG SHA-256（本机同环境）：`bcae1e376da38e95c4785bb6814c5effc8a620bf64f939375f37a4f05dced328`
- 固定输入 PNG 字节数：`25671`

源码指纹进入跨平台合同；PNG 哈希用于本机改前改后对照，避免字体后端差异造成跨平台假红。
