# 隐私 Phase 1 TDD 计划

## 1. 基线

- 运行 `scripts/snapshot_run.py --output before.json`。
- 记录 `data/api_usage.json` SHA-256。
- 不启动服务器、不访问外部航班 API。

## 2. 详情访问 RED/GREEN

- 先改写 `test_detail_payload_storage.py`：合法 UUID 200、数字/任意字符串/缺失文件 404、共享令牌开关、保存端不生成旧索引。
- 新增 UUID 访问辅助模块。
- 收紧 `web_form.py` 路由与 `main.py` 保存端。
- 验证默认关闭令牌时合法 UUID 可访问，令牌开启时未带或错误令牌均 404。

## 3. 外发参数契约 RED/GREEN

- 新增 `test_privacy_outbound_contract.py`，捕获三源请求对象。
- 新增递归契约断言，拒绝乘客类型计数与分舱结构。
- 不改变三源生产请求语义。

## 4. 日志脱敏 RED/GREEN

- 扩展轮档测试，先证明邮箱会泄漏。
- 统一 `safe_log`、tee 和轮档证据的邮箱脱敏。
- 新增 `scripts/scrub_pii.py` 及 tmpdir 测试：dry-run 不改、execute 先备份、原子替换、重复执行幂等。

## 5. 通知分级 RED/GREEN

- 新增 `test_notification_privacy_levels.py`：`full` 与缺字段逐字节相同；`redacted` 无精确金额/乘客；`minimal` 只保留航线和变动事实。
- 页 2 新增完整性契约字段并验证编辑回填。
- 非 `full` 走独立精简渲染器，详情页仍保留完整内容。

## 6. 保留窗 RED/GREEN

- 新增 `test_retention_policy.py`：三类文件、边界时间、`0` 永久、dry-run 与 execute。
- 轮末挂只读报告。
- 新增手工 `scripts/retention_cleanup.py`，禁止自动删除。

## 7. 回归与交付

- 运行相关专项测试。
- 运行双收集器全量测试。
- 生成 `after.json` 并与 `before.json` 比较；默认 `full` 不应产生通知结构变化。
- 核对冻结邮件哈希不变。
- 核对 API 台账 SHA-256 不变。
- 创建本地独立提交，不 push。
- 交付 PA 备份、清理、Reload 和 curl 验证序列，等待用户人工验收。
