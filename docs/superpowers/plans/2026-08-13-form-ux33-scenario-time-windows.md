# UX 3.3 Scenario Branch And Time Windows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将商务字段归入场景父控件分支，并用一个分层时间偏好表面替代 12 个 schema 时间窗字段的裸渲染。

**Architecture:** `form_concepts.py` 负责场景范围与时间窗纯派生，`form_structure.py` 负责旧订阅投影，`form_pages.py` 只消费声明式分组并渲染原生 `<details>`。`web_form.py` 继续调用同一派生器写出现有 schema，不增加业务分支。

**Tech Stack:** Python 3.13、Flask/Jinja、HTML `<details>`、现有 Edge CDP smoke、unittest/pytest。

---

### Task 1: 锁定场景范围与商务分组

**Files:**
- Create: `test_form_ux33_scenario_time_windows.py`
- Modify: `form_concepts.py`
- Modify: `form_pages.py`
- Modify: `test_form_ux32_native_details.py`

- [ ] 写失败测试，冻结 `scenario_scope` 合法值和商务概念集合。
- [ ] 写失败测试，要求 `travel_scenario` 在商务组外，全部商务字段只在紧随其后的 `business-travel` 组内。
- [ ] 运行定向测试并确认因缺少 scope/分组而失败。
- [ ] 拆分 `travel_context`/`business_nature`，扩展商务组并实现编辑态自动展开。
- [ ] 重跑定向测试至通过。

### Task 2: 锁定并实现三层时间派生

**Files:**
- Modify: `test_form_ux33_scenario_time_windows.py`
- Modify: `form_concepts.py`
- Modify: `form_structure.py`
- Modify: `web_form.py`

- [ ] 写共享窗、分方向覆盖、半开窗回退、顶层偏好四组失败测试。
- [ ] 运行测试，确认新规范控件尚未被读取。
- [ ] 实现新控件到旧 12 字段/六组 windows 的纯派生，并保留旧输入回退。
- [ ] 更新旧订阅投影，使编辑页读取新控件名。
- [ ] 重跑派生矩阵和既有 UX2 兼容测试。

### Task 3: 渲染原生时间窗层级与舱位说明

**Files:**
- Modify: `test_form_ux33_scenario_time_windows.py`
- Modify: `form_pages.py`
- Modify: `test_form_ux_concepts.py`
- Modify: `test_form_ux31_render_completeness.py`

- [ ] 写失败测试：旧 12 字段在 HTML 中为零，新 12 控件存在，时间偏好为 radio，两个嵌套 `<details>` 存在。
- [ ] 写失败测试：舱位仍在飞行偏好且出现“全员同舱”说明。
- [ ] 实现 time concept 专用声明式布局与 radio 渲染，不增加 JavaScript。
- [ ] 更新渲染完整性契约，使 radio 按唯一概念表面校验。
- [ ] 运行结构与渲染测试至通过。

### Task 4: 增加第九场景与浏览器契约

**Files:**
- Modify: `scripts/capture_form_normalization_baseline.py`
- Modify: `tests/fixtures/form_normalization_baseline_v1.json`
- Modify: `scripts/ui_smoke_driver.mjs`
- Modify: `test_form_ux31_render_completeness.py`
- Modify: `test_form_ux33_scenario_time_windows.py`

- [ ] 先写失败测试，要求夹具包含 `directional_time_windows` 且输出命中三层优先级。
- [ ] 增加第九场景并重新捕获固定规范化结果。
- [ ] 扩展 Edge smoke：点击商务组、通用时间窗和嵌套分方向组，填写后提交并核对确认页。
- [ ] 保留渠道、地点候选、通知副作用和端口主权契约。

### Task 5: 离线回归、快照与提交

**Files:**
- Create: `after_ux33.json`（忽略文件）
- Create: `after_ux33_form_normalization.json`（忽略文件）
- Create: `after_ux33_shanghai_osaka_email.html`（忽略文件）

- [ ] 比较前后九场景；前八逐字段零 diff，第九与固定期望一致。
- [ ] 离线复放上海—大阪邮件，比较 SHA256 与字节数。
- [ ] 运行 `python313 -X utf8 -m unittest discover` 和 Anaconda `pytest -q -p no:cacheprovider`。
- [ ] 在隔离端口运行 `python -X utf8 scripts/ui_smoke.py`。
- [ ] 重跑快照并确认只出现预期的表单结构变化。
- [ ] 核对 `data/api_usage.json` SHA256 前后不变。
- [ ] 精确暂存本任务文件并提交 `feat: branch form scenarios and unify time windows`；不自动占用或停止 `:5000`。
