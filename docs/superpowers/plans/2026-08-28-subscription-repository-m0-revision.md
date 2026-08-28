# SubscriptionRepository M0 修订实施计划

## 实况与边界

- 基线 `e9c05d2` 的 Web 层直接整表读写 `subscriptions.json`，编辑依赖数组 `edit_index`，删除依赖 `pop(index)`；当前主树已引入 JSON-backed `SubscriptionRepository`，本笔只补修订规格暴露的剩余接缝。
- `subscription_id` 必须在部署前由 `scripts/migrate_subscription_ids.py --write` 持久化；列表和数字索引兼容入口只读，禁止在读取路径临时生成 UUID。
- owner 固定由服务端注入 `local-owner`。M0 保留全表 `_index` 语义，不引入认证、SQLite、每 owner 独立序号或用户可见行为变化。
- 全程零真实外部 API；测试只使用临时 JSON、Flask test client 与 mock 后台采集。

## TDD 步骤

1. 为迁移脚本补 `--write` 正式入口，并锁定 dry-run、无待迁移 no-op、备份及幂等行为；保留 `--execute` 兼容别名。
2. 将旧“列表/数字索引读取时补 UUID”测试改成 RED 合同：文件字节不变、缺失稳定身份时不生成 UUID，并提示先执行迁移。
3. 增加同一订阅双进程 `update()` 部分字段并发测试，要求每次在锁内重读当前记录后递归合并，两个更新均保留。
4. 最小修改仓储：`resolve_legacy_index()` 只读；`update()` 在同一次 `update_json()` RMW 中按稳定 ID 定位并合并；五方法边界及 owner scope 不变。
5. 最小修改 Web：移除列表读取时身份写回，改为迁移前置校验；新链接继续使用 UUID，`?edit=<index>` 仅在 M0 解析全表位置并记录迁移日志。
6. 运行本地 UUID dry-run 与 `--write`，记录前后总数、具备 ID 数、备份路径及文件哈希；PA 仅提供同序命令，等待用户在 pull+Reload 前执行并回传原文。
7. 跑仓储/Web 定向测试、十一夹具、冻结邮件、PushPlus、双收集器与 ui-smoke；核对三库、订阅和配额台账哈希，独立提交后 push 并终审公开 CI。

## 并发语义

- `update(owner, id, patch)` 的输入视为字段补丁：在文件锁内读取最新记录，对嵌套映射递归覆盖；未出现在补丁中的字段保留。这样两个进程修改同一订阅的不同字段不会互相抹除。
- 完整表单提交仍提供完整规范化订阅，因此既有保存结果不变；身份字段始终以现存记录为准。
- 两个进程同时修改同一叶子字段时仍采用锁获取顺序决定的后写值。没有 revision/base-version 时，仓储不会假装能自动合并同一字段的冲突。

## 部署顺序

1. 本地和 PA 分别运行迁移 dry-run。
2. PA 的旧部署先用当时已支持且语义相同的 `--execute`；本地及合入后的环境用正式
   `--write`。确认所有记录均有非空 `subscription_id` 并留存备份路径。
3. 再 pull 本提交并在 PA Reload；不得颠倒为“先部署读取硬门、后补 UUID”。
