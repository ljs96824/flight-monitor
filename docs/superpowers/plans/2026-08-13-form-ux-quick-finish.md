# UX 1.5 四步提前收尾 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将一期六站表单改成四步必填后即可收尾、其余设置默认折叠的快速体验，同时保持规范化订阅逐字段不变。

**Architecture:** `form_structure.py` 提供站点深度、可选段摘要和编辑展开状态；`web_form.py` 模板只渲染元数据，原生 JavaScript 复用既有默认预览、确认和提交链。测试以一期五场景固定夹具为兼容宪法。

**Tech Stack:** Python 3.13、Flask `render_template_string`、原生 JavaScript、`unittest`、`pytest`。

---

### Task 1: 冻结表现层契约

**Files:**
- Create: `test_form_ux_quick_finish.py`
- Modify: `test_form_ux_phase1.py`

- [ ] **Step 1: 写站点深度和可选段摘要失败测试**

断言站1至站4为 `required`、站5至站6为 `optional/default_collapsed`；断言可行性参数默认摘要和自定义值摘要不同。

- [ ] **Step 2: 运行测试确认按预期失败**

Run: `python -X utf8 -m unittest test_form_structure test_form_ux_phase1`

Expected: 因站点深度元数据、快速收尾 DOM 和选择性展开函数尚不存在而失败。

- [ ] **Step 3: 写编辑选择性展开失败测试**

构造默认 `precise`、自定义动身时间、自定义廉航/航司和自定义提醒四组表单值，断言只展开对应段。

### Task 2: 扩展 Python 表单结构元数据

**Files:**
- Modify: `form_structure.py`

- [ ] **Step 1: 为六站补 `depth` 与 `default_collapsed`**

站1至站4输出 `required/False`；站5至站6输出 `optional/True`，保持编号、字段归属不变。

- [ ] **Step 2: 新增可行性摘要与编辑展开纯函数**

实现 `summarize_optional_sections(values)` 与 `edit_expanded_sections(values, editing)`；默认比较表只描述现有 DOM 默认值，不调用分析器或默认规则。

- [ ] **Step 3: 让 `form_structure_payload` 输出新元数据**

输出 `required_station_count=4`、`optional_sections` 和编辑展开列表。

- [ ] **Step 4: 运行结构测试确认通过**

Run: `python -X utf8 -m unittest test_form_structure`

Expected: PASS。

### Task 3: 渲染四步收尾与默认折叠

**Files:**
- Modify: `web_form.py`

- [ ] **Step 1: 在模板中增加长度承诺和站点深度属性**

顶部渲染“必填4步 · 其余可选”；每站渲染 `data-station-depth`、`data-default-collapsed` 和“必经/可选”标签。

- [ ] **Step 2: 在站4增加快速收尾区**

渲染 `quick-finish-button`、场景芯片容器和 `optional-settings-toggle`；移除站5旧芯片容器，保证页面只有一个同名 ID。

- [ ] **Step 3: 原位折叠站3可行性参数与站5/站6内容**

字段名、默认值和 `data-form-section` 不变；折叠头显示 Python 摘要。

- [ ] **Step 4: 复用既有确认链路**

抽取预览按钮现有处理为一个函数，让 `preview-button`、`quick-finish-button` 和移动端摘要入口调用同一函数。

- [ ] **Step 5: 调整移动端四步终点**

未展开可选设置时第4步为终点；打开可选设置后才允许进入第5、6步，`monitor_mode` 派生规则不变。

- [ ] **Step 6: 编辑时消费选择性展开元数据**

只展开 `data-edit-expanded=true` 的段并加高亮类；未改值的可选段保持折叠。

- [ ] **Step 7: 运行UX定向测试确认通过**

Run: `python -X utf8 -m unittest test_form_structure test_form_ux_phase1 test_price_hint test_location_resolution_strict`

Expected: PASS。

### Task 4: 兼容与全量验证

**Files:**
- Generate: `after_ux15.json`
- Generate: `after_ux15_form_normalization.json`

- [ ] **Step 1: 重跑五场景捕获并逐字节比较**

Run: `python -X utf8 scripts/capture_form_normalization_baseline.py --output after_ux15_form_normalization.json`

Expected: 与 `tests/fixtures/form_normalization_baseline_v1.json` SHA256 相同。

- [ ] **Step 2: 重跑离线快照并审查 diff**

Run: `python -X utf8 scripts/snapshot_run.py --output after_ux15.json`

Expected: 业务字段不变。

- [ ] **Step 3: 运行双收集器全量测试**

Run: `python -X utf8 -m unittest discover`

Run: `C:\ProgramData\anaconda3\python.exe -X utf8 -m pytest -q -p no:cacheprovider`

Expected: 两者全部通过。

- [ ] **Step 4: 核对真实数据零副作用**

比较 `data/api_usage.json` 与 `data/observations.sqlite3` 的任务前后 SHA256。

- [ ] **Step 5: 创建独立提交**

Run: `git add <本任务明确文件> && git commit -m "feat: streamline quick subscription flow"`

Expected: 新提交直接位于⑦补丁提交 `2fcb1fb` 之后。
