# SubscriptionRepository M0 接缝

## 决策

M0 将 Web 订阅 CRUD 从数组位置改为稳定 `subscription_id`，存储仍为现有
`subscriptions.json` 数组。所有仓储写入使用 `atomic_json_store.update_json()`，
完整 read-modify-write 位于同一个跨进程文件锁内。
读取现有文件也短持同一锁，避免 Windows 在写侧 `os.replace` 窗口产生
`PermissionError`；文件不存在时仍直接返回空列表。

owner 身份只能由服务端注入。本地部署固定使用 `local-owner`；表单、查询参数、
请求体和请求头均不能提供或覆盖 owner。M0 只建立边界，不开放多用户、不增加认证、
不引入 SQLite，也不向 JSON 记录增加 owner 字段。

## 接口与错误

- `list_for_owner(owner_id)`：非本地 owner 返回空列表。
- `get(owner_id, subscription_id)`：不存在或 owner 不匹配返回 `None`。
- `create(owner_id, subscription)`：返回带稳定身份的已保存记录；非本地 owner
  抛 `SubscriptionOwnerScopeError`，因为创建没有“未命中”语义。
- `update(owner_id, subscription_id, subscription)`：把输入视为字段补丁，在同一
  锁内读取最新记录并递归合并；并发删除或 owner 不匹配时返回 `None`，不以异常
  表达正常竞争。不同字段的并发编辑均保留，同一叶子字段仍由锁获取顺序决定后写值。
- `delete(owner_id, subscription_id)`：不存在或 owner 不匹配返回 `False`。

仓储身份只使用已持久化的 `subscription_id`（底层身份读取仍兼容历史 `id`，只供
迁移和旧数据识别）。PA 同步的航线键仍只承担“仅新增摄入”的去重政策，不成为仓储
CRUD 身份，也不覆盖本地正典。新建保存路径负责为新记录生成并持久化 ID；读取路径
绝不临时生成 UUID。

## M0 兼容入口

`?edit=<index>` 仅在 M0 存在。服务端按完整 JSON 数组位置只读解析已经持久化的
`subscription_id`，随后按该 ID 重新读取和保存，并打印 `[订阅编辑迁移]`。新生成的
编辑、暂停、快捷更新、成功页链接全部使用稳定 ID。数字入口与订阅列表若发现缺失
规范 `subscription_id`，返回 503 并提示先运行迁移，文件字节保持不变；这是部署
次序错误的显式阻断，不是运行时迁移兜底。

隐藏字段名 `subscription_index` 暂不更名，以保持既有表单渲染与十一场景规范化契约；
其新值是一个不透明 `subscription_id`，不再驱动位置写入。M1 必须删除数字入口与该旧名。
`_index` 继续表示完整 JSON 数组位置；M0 owner 过滤只发生在读取后，禁止按 owner
子集重新编号，以免详情页、推送与既有价格历史串号。

## 部署前置

本地与 PA 均须在部署本提交前执行：

    python -X utf8 scripts/migrate_subscription_ids.py
    python -X utf8 scripts/migrate_subscription_ids.py --execute
    python -X utf8 scripts/migrate_subscription_ids.py

首行是 dry-run；第二行在有缺口时先备份再补 UUID；第三行应显示 `待补发=0`。
部署前的旧版本尚未提供 `--write`，所以 PA 在 pull 前使用当时已有且语义相同的
`--execute`；本提交合入后 `--write` 成为正式名称，`--execute` 仅保留为兼容别名。
PA 的正确顺序是迁移完成、确认全部记录有 ID，再 `git pull --ff-only` 与 Reload。

## 验证边界

Flask test client 覆盖列表、稳定 ID 编辑保存、暂停/启用、删除确认与执行、快捷更新、
CSRF 零副作用、后台启动四态和 JSON 锁先释放后启动。暂停与快捷更新使用仓储内
单锁字段级 mutation，避免覆盖并发写入的 `last_attempt`。浏览器 `ui_smoke` 当前也
覆盖列表、编辑和删除，但不覆盖暂停/启用；不能把 smoke 绿色解释为完整 CRUD 证明。
仓储测试另以 Windows `spawn` 双进程覆盖不同订阅更新、同订阅不同字段合并及
update/delete 竞争；`update()` 未命中返回 `None`，Web 显示“订阅已删除”而不启动采集。
