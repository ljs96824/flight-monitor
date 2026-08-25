# 采集轮并发与锁顺序

## 单飞锁语义

订阅批量轮、Web 单订阅手动触发和固定篮子共用采集单飞锁。获取顺序固定：

1. 非阻塞尝试操作系统文件锁；
2. 操作系统锁获取失败时，无论心跳时间多旧，都返回 `busy`；
3. 只有成功取得操作系统锁后，才读取旧元数据、判断是否属于陈旧轮次并写入新租约；
4. 陈旧接管不删除、不替换锁文件。进程异常退出后由操作系统释放文件锁，新进程随后在同一个文件上取得锁。

心跳使用当前持锁的文件描述符原地更新，禁止对锁文件执行 `os.replace`。元数据包含 `pid`、`round_id`、`heartbeat_at`、`lease_id` 和 `hostname`。心跳与释放前都校验 `lease_id`；租约不匹配时放弃元数据写入。

释放顺序固定为：设置停止事件、等待心跳线程退出、校验租约并写入释放状态、解锁操作系统文件锁、关闭文件描述符。

## 路径与作用域

`COLLECTION_LOCK_PATH` 可配置绝对锁路径。未配置时，普通主工作区使用 `data/collection_singleflight.lock`；linked Git worktree 会解析到主工作区的同一路径，避免不同 worktree 各持一把锁。

该锁只保证**同一台机器、指向同一锁文件路径**的进程互斥。它不协调本机与 PythonAnywhere，也不协调多台设备。PythonAnywhere 维持表单、订阅和详情 payload 同步职责，不执行真实航班采集。

busy轮次记录独立状态：

```text
status=busy holder_pid=... holder_round_id=... holder_heartbeat_at=... entrypoint=...
```

busy 不计入成功或失败，也不得开启请求缓存轮、采集计划、观测上下文或轮档，不写 API 台账、观测、价格和篮子完成状态。

## 全局锁顺序

全局顺序固定为：

```text
collection single-flight -> api_usage lock -> subscription/feedback JSON lock
```

- 禁止反向获取，尤其禁止持有订阅或反馈 JSON 锁后等待采集单飞锁。
- Web 保存订阅必须先完成 JSON 原子写入并释放 JSON 锁，再启动后台采集。
- Web 请求线程启动后台线程后只做有界握手等待；后台线程先争用 collection single-flight，再回报 `started`、`busy` 或 `startup_error`。等待超时单独记为 `confirming`，不得伪装成已启动。
- 四态结果按订阅 UUID 原子写入 `last_attempt`；`busy` 不计入成功或失败，后续正常启动或完成会覆盖旧状态。
- `api_usage`、订阅和反馈等下层文件锁临界区内禁止外部 API 调用和长时间分析。
- collection single-flight 是采集轮的外层准入锁，设计上覆盖整个采集轮；期间不得长期持有任何下层文件锁。

## 已知AST债务合同

`KNOWN_TOP_LEVEL_DUPLICATES` 的精确集合比较属于测试契约加固，用于防止七组既有重复符号静默换位；它不属于single-flight生产实现，也不改变运行行为。
