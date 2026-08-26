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

## 逐笔状态

| 提交 | 状态 | 清理依据 | F821 剩余 |
|---|---|---|---:|
| 1 改名残留 | implemented in this commit | `61b9109` / `faec1c0` / `784d8bb` 历史 diff + characterization | 39 |
| 2 `format_html_message` 终止 return 后死尾 | pending | 待控制流五证齐备 | 39 |
| 3 孤立 legacy renderers | pending | 待两层调用图证明 | 39 |
| 4 孤立 legacy notification helpers | pending | 待完整旧子图证明 | 39 |
