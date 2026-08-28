# SubscriptionRepository M0 接缝

## 决策

M0 将 Web 订阅 CRUD 从数组位置改为稳定 `subscription_id`，存储仍为现有
`subscriptions.json` 数组。所有仓储写入使用 `atomic_json_store.update_json()`，
完整 read-modify-write 位于同一个跨进程文件锁内。

owner 身份只能由服务端注入。本地部署固定使用 `local-owner`；表单、查询参数、
请求体和请求头均不能提供或覆盖 owner。M0 只建立边界，不开放多用户、不增加认证、
不引入 SQLite，也不向 JSON 记录增加 owner 字段。

## 接口与错误

- `list_for_owner(owner_id)`：非本地 owner 返回空列表。
- `get(owner_id, subscription_id)`：不存在或 owner 不匹配返回 `None`。
- `create(owner_id, subscription)`：返回带稳定身份的已保存记录；非本地 owner
  抛 `SubscriptionOwnerScopeError`，因为创建没有“未命中”语义。
- `update(owner_id, subscription_id, subscription)`：并发删除或 owner 不匹配时
  返回 `None`，不以异常表达正常竞争。
- `delete(owner_id, subscription_id)`：不存在或 owner 不匹配返回 `False`。

仓储身份只使用 `subscription_id`（并兼容读取旧 `id`）。PA 同步的航线键仍只承担
“仅新增摄入”的去重政策，不成为仓储 CRUD 身份，也不覆盖本地正典。
编辑仅有旧 `id` 的记录时，沿用原保存路径语义补写同值 `subscription_id`。

## M0 兼容入口

`?edit=<index>` 仅在 M0 存在。服务端先在仓储锁内读取该位置；若旧记录尚无稳定
身份，则只补一次 UUID；随后立即按 `subscription_id` 重新读取和保存，并打印
`[订阅编辑迁移]`。新生成的编辑、暂停、快捷更新、成功页链接全部使用稳定 ID。
订阅列表是另一个明确的 M0 升级入口：若发现无 ID 的旧记录，列表渲染前先持久化
UUID，再生成稳定 ID 链接，避免升级后旧记录失去编辑或暂停入口。

隐藏字段名 `subscription_index` 暂不更名，以保持既有表单渲染与十一场景规范化契约；
其新值是一个不透明 `subscription_id`，不再驱动位置写入。M1 必须删除数字入口与该旧名。

## 验证边界

Flask test client 覆盖列表、稳定 ID 编辑保存、暂停/启用、删除确认与执行、快捷更新、
CSRF 零副作用、后台启动四态和 JSON 锁先释放后启动。暂停与快捷更新使用仓储内
单锁字段级 mutation，避免覆盖并发写入的 `last_attempt`。浏览器 `ui_smoke` 当前也
覆盖列表、编辑和删除，但不覆盖暂停/启用；不能把 smoke 绿色解释为完整 CRUD 证明。
