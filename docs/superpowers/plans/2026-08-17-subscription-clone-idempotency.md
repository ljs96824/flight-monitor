# 订阅克隆幂等修复实施计划

## 根因

- 编辑 POST 仍正确携带 `subscription_index`，本地保存会覆盖原位置，计数不增加。
- `build_subscription()` 每次提交都会生成新的 `created_at`，编辑保存未保留旧身份字段。
- `sync_subscriptions.py` 以 `id`、其次 `created_at` 为身份键，并只追加远端新键、不更新同键内容。每次编辑后的新 `created_at` 因而被本地同步成一条新订阅。
- 该组合从 `4654c551`（2026-05-27，引入 PythonAnywhere 同步）起存在；UX 3.0 `7917406` 的完整页仍渲染了 `subscription_index`，不是起病提交。

## TDD 步骤

1. 增加编辑重存契约：走 Flask POST 和真实临时订阅文件，断言总数不变，原 `id/created_at` 保留。
2. 增加同步契约：同身份远端记录原位更新；连续同步两次总数不变。
3. 最小修改 `save_subscription()`：编辑时保留既有身份字段，不改变新建订阅行为。
4. 最小修改 `merge_subscriptions()`：远端同身份记录原位更新，远端新身份才追加；不自动清理历史重复行。
5. 新增 `scripts/dedupe_subscriptions.py`：默认只读列簇；仅 `--execute` 时先备份，再按现有 route fallback 身份键保留最新记录。
6. 增加脚本 dry-run、备份、保留最新、幂等测试。
7. 重跑快照、定向测试、双收集器、冻结邮件回归，核对 API 台账哈希不变。

## 数据边界

- 本任务不自动执行真实订阅去重。
- 去重脚本不级联删除 payload、价格历史或约束指纹纪元；这些历史文件继续保留为审计证据，删除后的订阅记录只是不再进入后续批量轮。
- 现有部分历史槽位在无稳定 `id` 时以列表 `_index` 兜底。清理会重排索引，因此旧 payload、价格历史和指纹纪元只能视为保留的审计孤儿；清理后的首轮不得把索引碰巧相同的旧记录当作连续同一订阅。
- PythonAnywhere 是订阅真值源。只清理本地文件会在下次同步时重新引入远端克隆；确认后应先在 PA 的订阅文件运行同一只读预览与 `--execute`，再同步本地副本。
- route fallback 身份键为 `origin|destination|depart_date|return_date|round_trip`。同航线同日期但有意建立的多份订阅也会进入同一预览簇，因此必须由用户核对只读清单后再显式执行。
