# UX 3.0 Two-Page Form Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 用快速静态页和完整静态页替换 UX2 机械交互，并建立真实 Edge 浏览器交互契约。

**Architecture:** 保留 `form_concepts.py` 和 `build_subscription()`，新增轻量页面契约模块描述双页字段与六节。`web_form.py` 只负责服务端渲染、两处条件显隐和确认回读；本地脚本以隔离 Flask 子进程和 Edge CDP 完成端到端冒烟。

**Tech Stack:** Python 3.13、Flask/Jinja、原生 JavaScript、Microsoft Edge headless、Node 24 内建 WebSocket。

---

### Task 1: 锁定双页结构契约

**Files:**
- Create: `test_form_ux3_two_pages.py`
- Modify: `test_form_ux_phase1.py`
- Modify: `test_form_ux_quick_finish.py`

- [x] 写失败测试，要求 `/` 只有快速页、`/settings` 有六个静态章节和锚点目录。
- [x] 写失败测试，要求旧手风琴、面包屑、芯片和折叠标记从两页消失。
- [x] 写失败测试，要求完整页具名控件唯一、条件显隐白名单仅含乘客画像和邮箱。
- [x] 运行 `python -X utf8 -m unittest test_form_ux3_two_pages -v`，确认因新路由和模板尚不存在而失败。

### Task 2: 建立页面契约数据

**Files:**
- Create: `form_pages.py`
- Test: `test_form_ux3_two_pages.py`

- [x] 定义 `QUICK_CONCEPTS`、六节 `FULL_SECTION_CONCEPTS`、内部概念白名单和两项显隐白名单。
- [x] 增加守卫，确保每个用户可编辑概念只属于一个页面章节。
- [x] 增加快速页可见概念数量上限测试。
- [x] 运行定向测试并确认页面契约数据通过。

### Task 3: 渲染快速页与完整页

**Files:**
- Modify: `web_form.py`
- Test: `test_form_ux3_two_pages.py`

- [x] 将旧模板替换为 form_pages 中共享模板的 quick/full 两种服务端上下文，共享地点候选、基础样式和最小脚本。
- [x] 新增 `/settings`；让 `/?edit=N` 重定向到完整页。
- [x] 快速页提交隐藏 `monitor_mode=quick`；完整页新建为 `precise`，编辑时使用存量值。
- [x] 完整页六节全部静态可见，目录使用普通 `href="#section-id"`。
- [x] 页尾确认回读使用 `constraint_summary` 同源函数，并为每行附所属章节链接。
- [x] 运行双页测试，直到结构契约通过。

### Task 4: 守住七场景与编辑幂等

**Files:**
- Modify: `test_form_ux3_two_pages.py`
- Modify: `test_notification_channel_regression.py`（仅在旧结构断言需要迁移时）

- [x] 将七场景逐一通过快速/完整 POST 适配器提交，断言规范化结果等于固定夹具。
- [x] 对七场景做 `subscription_to_form_values()` 完整页重提，断言逐字段幂等。
- [x] 覆盖 email/pushplus/both 通知渠道回归。
- [x] 运行上述契约测试并确认通过。

### Task 5: 增加真实浏览器冒烟

**Files:**
- Create: `scripts/ui_smoke.py`
- Create: `scripts/ui_smoke_driver.mjs`
- Modify: test_form_ux3_two_pages.py
- Modify: `.github/workflows/tests.yml`
- Modify: `README.md`

- [x] 写失败测试检查脚本入口、Edge headless 参数、CDP console error 采集和归档路径。
- [x] 实现临时订阅存储、后台采集空替身和本地 Flask 子进程。
- [x] 用 Node 内建 WebSocket 驱动 Edge，完成快速页提交与完整页六节/锚点/字段唯一性检查。
- [x] 在 workflow 添加明确 `if: ${{ false }}` 的 local-only UI smoke step。
- [x] 将 `python -X utf8 scripts/ui_smoke.py` 加入 README 本地验收清单。
- [x] 本机运行并保存全绿原文到 `data/ui_smoke_latest.log`。

### Task 6: 全量回归与提交

**Files:**
- Create: `after_ux3.json`（忽略文件）
- Create: `after_ux3_form_normalization.json`（忽略文件）

- [x] 运行七场景 before/after 差异，要求为零。
- [x] 复放上海—大阪邮件并比较 SHA256，要求字节不变。
- [x] 运行 `python -X utf8 -m unittest discover`。
- [x] 运行 `python -X utf8 -m pytest -q -p no:cacheprovider`。
- [x] 重跑快照并仅忽略 `computed_at` 比较。
- [x] 核对 API 台账和观测库 SHA256 前后不变。
- [x] 精确暂存本任务文件并提交 `feat: replace form wizard with two static pages`。
