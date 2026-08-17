# Local Canonical Subscription Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让本地订阅表成为正典，PA 仅摄入真正新增订阅，并为全部本地订阅建立稳定 `subscription_id`。

**Architecture:** 同步先生成逐条决策计划：身份键命中或航线键命中均跳过，只有两者都未命中才追加；绝不覆盖或删除本地记录。身份迁移由独立脚本执行，默认只读，`--execute` 时先备份再原子写入。

**Tech Stack:** Python 3.13、Flask、unittest/pytest、JSON 文件存储。

---

### Task 1: 锁定同步语义

**Files:**
- Modify: `test_subscription_clone_idempotency.py`
- Modify: `sync_subscriptions.py`

- [x] 写失败测试：同 `subscription_id`、同 `created_at`、同航线键三类远端记录都不得覆盖或追加。
- [x] 写失败测试：真正新远端记录追加一次且获得 `subscription_id`；本地缺席项不删除。
- [x] 运行定向测试，确认旧实现分别暴露覆盖和克隆复发。
- [x] 实现纯函数同步计划及逐条日志，再运行定向测试至绿色。

### Task 2: 保存路径身份正名

**Files:**
- Modify: `web_form.py`
- Modify: `test_subscription_clone_idempotency.py`

- [x] 写失败测试：新建订阅自动生成 UUID 格式的 `subscription_id`。
- [x] 写失败测试：编辑保留既有 `subscription_id`，订阅总数不变。
- [x] 在 `save_subscription()` 单点补发身份并输出 `[身份迁移]` 日志。
- [x] 运行表单与提交副作用契约测试。

### Task 3: 一次性迁移本地正典

**Files:**
- Create: `scripts/migrate_subscription_ids.py`
- Modify: `test_subscription_clone_idempotency.py`

- [x] 写失败测试：dry-run 字节不变；执行时九条均获不同 UUID、先生成完整备份；重复执行不改文件。
- [x] 实现默认 dry-run 与显式 `--execute` 原子迁移。
- [x] 在真实本地 9 条上先 dry-run，再执行并核对 UUID 数量为 9。

### Task 4: 回归与交付

**Files:**
- Create: `data/snapshot_sync_semantics_after.json`（忽略文件）

- [x] 用 PA 四条身份与本地九条运行新语义 dry-run，逐条输出应全部为跳过。
- [x] 运行双收集器、冻结邮件和 before/after 快照对比。
- [x] 核对 API 台账哈希不变。
- [x] 提供 PA 端“备份→清空→验证空表”的确切命令，不在本机执行。
- [x] 创建独立提交，不 push。
