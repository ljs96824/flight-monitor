# UX 一期六站表单实施计划

> 设计已由用户给定并批准；按 TDD 小步执行，所有测试离线。

## 1. 冻结兼容性

- 运行 `scripts/snapshot_run.py --output before_ux_phase1.json`。
- 用 `scripts/capture_form_normalization_baseline.py` 生成五场景 POST 夹具。
- 记录 `data/api_usage.json` 与 `data/observations.sqlite3` SHA256。

## 2. 先写失败契约测试

- 新增 `test_form_structure.py`：六站定义、字段唯一归属、显隐矩阵、摘要、mode 三态。
- 新增 `test_form_ux_phase1.py`：六站 DOM/data 属性、隐藏 mode、defaults preview、地点与 price hint 回归。
- 在 `test_form_ux_phase1.py` 中加入五场景 POST 逐字段相等与编辑往返幂等测试。
- 新增 `test_constraint_summary.py`：共享摘要与 analyzer 兼容包装同串。

## 3. 建纯 Python 单一真值

- 新建 `form_structure.py`：站点、字段归属、可见性、摘要、mode 派生、默认芯片。
- 新建 `constraint_summary.py`：排除依据纯函数。
- `analyzer.py` 改成薄包装，保持旧函数入口与输出。

## 4. 六站模板与原生 JS

- 重排 `FORM_TEMPLATE` 为六个 `data-station-id` fieldset，字段名与默认值不变。
- 移除可见 mode 二选一，保留隐藏 `monitor_mode=quick`。
- 加段头摘要、场景芯片墙、第五站进阶折叠、提交前同源依据区。
- JS 删除硬编码 `scenarioDefaults`，改调 `/defaults_preview`；只同步底层 input 和服务器文本。
- 编辑旧订阅按存量 mode 回填；进阶折叠触发派生 precise。

## 5. 服务端预览

- `index` 向模板注入结构 JSON。
- 新增 `POST /defaults_preview`，只调用纯本地函数，不保存、不采集。
- 复用现有地点与 price hint 逻辑，不改原路由。

## 6. 验证

- 定向运行新增测试和现有 web form 测试。
- 重跑五场景捕获并逐字段 diff。
- 重跑 `snapshot_run.py --output after_ux_phase1.json` 并审阅 diff。
- 运行上海-大阪邮件离线复放，比较稳定化 HTML。
- 运行 pytest 与 unittest 双收集器。
- 复核 API 台账/观测库哈希不变。
- 独立提交；推送后等待 GitHub Actions Ubuntu/Windows × pytest/unittest 四格全绿。
