# UX 3.1 Render Completeness Implementation Plan

**Goal:** 让双页表单对规范控件同时具备“不得缺席、不得重复”的合同，并补齐当天往返执行参数与首屏模式入口。

**Boundary:** 仅表单表现、既有可选字段接线、成功页回读和测试；业务判定、金额、采集与通知输出不改。

### Task 1: 先锁红灯

- [x] 新增概念规范 name、完整页与快速页 `count == 1` 测试。
- [x] 新增首屏互链、静态会议小节、邮箱/会议确认回读测试。
- [x] 运行定向测试并记录 6 failure + 2 error。

### Task 2: 补齐声明与渲染

- [x] 给每个概念增加 `canonical_input_names` 并扩展注册表守卫。
- [x] 将会议执行字段归入“什么时候”，补 `buffer_hours` 与 `transport_mode`。
- [x] 增加页头双向模式链接与静态会议小节。
- [x] 保持显隐白名单不变。

### Task 3: 接通既有可选字段与回读

- [x] 仅在用户明确填写时写入 `buffer_hours` / `transport_mode`。
- [x] 编辑时从既有约束回填。
- [x] 成功确认页显示真实通知邮箱和当天会议时间。

### Task 4: 扩展兼容基线与真实浏览器

- [x] 新增第八个“当天往返会议完整参数”场景。
- [x] 确认旧七场景逐字段差异为零。
- [x] 扩展 Edge smoke 覆盖双向互链、email 提交和会议提交。

### Task 5: 全量验证与交付

- [ ] 运行本地 Edge smoke。
- [ ] 运行双收集器全量测试。
- [ ] 比较 before/after 快照、上海—大阪邮件字节和 API/观测文件哈希。
- [ ] 独立提交、推送，并确认 GitHub Actions 四格全绿。
