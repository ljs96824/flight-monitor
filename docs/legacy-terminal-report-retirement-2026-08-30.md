# Legacy 终端报告退役审计（2026-08-30）

## 1. 范围与结论

- 固定基线：`1240fd121a40b5c8d5e7b5b0c34a6db7ca563dbf`（PR #13 merge commit）。
- 处置：从当前分支删除根目录 `check.py`，不迁移、不重写其报告逻辑。
- 原因：入口读取非权威订阅来源，并在名义只读查看路径中调用具备建库、建表和 schema 变更能力的 `init_db()`。
- 边界：未修改生产采集、Web、通知、配置加载、cohort、T 曲线、forecast、分析模块或 SQLite 核心存储语义。
- 验证环境：`NO_LIVE_API=1`；本笔真实生产供应商 API 调用为 0。

## 2. 消费者审计

| 分类 | 结果 | 证据边界 |
|---|---:|---|
| `executable_reference` | 0 | 扫描 Python 直接/动态 import、subprocess、shell、scripts、workflow、BAT/CMD/PowerShell 与测试 |
| `active_operational_documentation` | 0 | README、CONTRIBUTING 与现行操作手册均无 `check.py` 执行指引 |
| `current_test_reference` | 0 | 删除前无测试调用该入口；本笔仅新增退役合同 |
| `historical_documentation_mention` | 0 | 当前历史文档无命中；Git 历史不改写 |

`analyzer.py` 仍有一处只存在于 docstring 的非执行性文字提及。它不是 import、调用、注册器或命令入口；本笔按范围铁律不修改分析模块。

`run.bat` 与 `run_once.bat` 均只执行 `python main.py`。本机四个 flight 相关 Windows Task Scheduler 动作也只执行 `main.py` 或 `basket_collect.py`。

维护者给出的仓外核验结论：

```yaml
external_usage:
  interactive: false
  windows_task: false
  pythonanywhere_task: false
  private_runbook: false
  pa_web_wsgi: false
  verified: true
```

- 维护者确认自 2026-07-21 起的协作记录中无手工执行痕迹。
- PythonAnywhere 为免费账号，Scheduled tasks 与 Always-on tasks 功能未启用。
- 私人 runbook、notebook 与快捷方式无该入口；PA WSGI 仅 import `web_form`。

## 3. 删除前缺陷事实

1. `_load_config()` 调用 `load_merged_config(DEFAULT_CONFIG_PATH, RUNTIME_CONFIG_PATH)`。
2. `main()` 从合并结果读取 `config.get("subscriptions", [])`，而现行 Web CRUD 与订阅管理的权威来源是 `data/subscriptions.json`。
3. `main()` 在读取配置前直接调用 `init_db()`。
4. `storage.get_latest_flights()` 内部再次调用 `init_db()`。
5. `storage.init_db()` 可创建数据库目录、数据库文件与表，因此该查看入口具备写能力。

这些事实不表示报告输出必然为空，也不表示每次执行都必然改变 `prices.db` 的 SHA。违规点是：订阅来源不权威，且只读查看路径包含写能力。

## 4. 隔离副作用证据

删除前以合成订阅和系统临时目录运行旧入口，生产数据库未参与：

```text
temporary_db_created=true
main_direct_init_db_calls=1
get_latest_flights_nested_init_db_calls=1
production_db_used=false
```

该探针证明数据库不存在时旧路径能够创建 `prices.db`，同时分别命中 `main()` 的直接初始化和 `get_latest_flights()` 的内部初始化。

## 5. TDD 证据

- 退役 RED：初始 5 个聚焦合同中仅 `test_root_terminal_report_entrypoint_is_absent` 因根目录 `check.py` 仍存在而失败。
- 退役 GREEN：删除 `check.py` 后，路径、可执行引用、当前文档与 BAT 合同共 5 项全部通过。
- 扫描器加固 RED：新增一个对抗性合同后，6 个正向引用形态中有 5 个暴露为既有漏检；首轮修复后，无关动态导入造例又精确暴露一次误报；复审分别证明跨作用域同名误报、调用后重赋值漏报、模块常量与闭包常量两项外层解析漏报，以及函数参数遮蔽误报。
- 最终 GREEN：6 个聚焦测试方法全部通过；同一扫描合同同时锁定 9 个正向引用形态与 4 个负例，并按调用点的词法作用域、源码时序、词法父链及参数遮蔽规则解析变量绑定。
- 历史文档不纳入当前操作文档集合，历史提及不会制造假红。

## 6. PA 与后续边界

- `check.py` 不在 Web 常驻 import 链，也不被 `main.py` 或 `basket_collect.py` 导入。
- 本笔不需要 PA Reload，也不要求立即 pull；日后为 checkout 一致性 pull 时，删除该文件属于预期。
- PA pull 前应运行 `git status --short -- check.py` 与 `git diff -- check.py`；若存在本地修改，先停止并人工裁决。
- 文件仍存在于 Git 历史中；本笔不改写历史。
- 本笔结束后暂停，不开始订阅事实源文档对齐任务。
