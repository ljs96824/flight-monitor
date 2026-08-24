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

## 图表基线

生效版本是第二版 `build_trend_png`：`6×2.8`、全日期旋转标签、当前价标注、`bbox_inches="tight"`。删除前记录：

- 生效函数源码 SHA-256：`21f2ba56d736dc088f9df3d8457332cb542c3af9d952e918afb385342a9eae34`
- 固定输入 PNG SHA-256（本机同环境）：`bcae1e376da38e95c4785bb6814c5effc8a620bf64f939375f37a4f05dced328`
- 固定输入 PNG 字节数：`25671`

源码指纹进入跨平台合同；PNG 哈希用于本机改前改后对照，避免字体后端差异造成跨平台假红。
