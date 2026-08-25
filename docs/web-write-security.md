# Web 写操作安全与启动握手

## 会话密钥边界

`FLASK_SECRET_KEY` 必须从私有 `.env` 读取，不得写入仓库、日志或错误响应。缺失时应用会生成进程内临时值并打印高可见告警；这只用于本地开发。PythonAnywhere 部署验收前必须配置固定高熵值，否则多 worker 会使用不同密钥，session 在 worker 间失效并表现为随机 403。

Cookie 固定使用 `HttpOnly` 与 `SameSite=Lax`。本地 HTTP 的 `SESSION_COOKIE_SECURE` 默认关闭；PythonAnywhere HTTPS 部署必须设置为 `1`。

## CSRF 边界

所有 `POST`、`PUT`、`PATCH`、`DELETE` 请求统一经过全局拦截。Token 绑定当前 session nonce，包含签发时间和 HMAC-SHA256 签名；默认有效期为两小时，可用 `CSRF_TOKEN_TTL_SECONDS` 调整。服务端接受表单字段 `csrf_token` 或请求头 `X-CSRF-Token`，缺失、伪造、来自未来或过期均返回 403，且在任何 JSON 写入、后台线程或采集单飞锁之前终止。

CSRF 防的是第三方站点借用户浏览器发请求，不等于身份认证；直接向公开 POST 端点发请求不受 CSRF 阻挡。认证按既定触发条件再评估：PythonAnywhere 开始保存真实订阅、开放第二位用户，或管理页包含真实个人数据。

删除订阅使用严格 UUID 路由。`GET /subscription/<uuid>/delete` 只渲染确认页，不写数据；`POST` 同路径必须同时携带有效 CSRF token 与 `confirm_delete=yes`。

## 首次采集启动握手

Web 保存订阅时遵循以下顺序：

1. 在 JSON 锁内原子保存订阅；
2. 释放 JSON 锁；
3. 启动后台线程；
4. 后台线程尝试 collection single-flight，并通过有界队列回报状态；
5. 请求线程按真实状态渲染成功页。

状态语义如下：

| 状态 | 用户可见语义 | 计数语义 |
| --- | --- | --- |
| `started` | 首次采集已启动 | 后续按真实结果写成功或失败 |
| `busy` | 已有采集轮运行，本次不重复启动 | 独立状态，不计成功或失败 |
| `startup_error` | 订阅已保存，但首次采集未能启动 | 等待下一轮重试 |
| `confirming` | 有界等待超时，启动状态仍在确认 | 不伪装成已启动 |

四态按 `subscription_id` 原子写入 `last_attempt={status,at,holder_round_id,entrypoint}`。PID、hostname 与 lease ID 只进入内部日志。后续正常启动或完成会覆盖旧的 `busy`/`confirming` 状态；写入在同一原子临界区内按 `at` 保持单调，因锁等待而迟到的旧状态会被忽略。若现存 `at` 超出当前时钟 5 分钟以上，而新事件仍处于当前时钟范围，则按异常未来时间恢复；无时区 ISO 时间一律按 UTC 解释。

启动结果先写入有界 Queue，再尽力持久化 `last_attempt`，因此 JSON 锁等待不会把已经取得 single-flight 的 `started` 误报为 `confirming`。请求线程同时把当前握手写入 Flask 签名 session；成功页按时间选择持久状态或本次 session 状态，并在读取后删除该 session 项。URL 中的 `startup`、`startup_persisted` 等查询参数不参与状态裁决，旧链接不能覆盖当前状态。

锁顺序仍为 `collection single-flight -> api_usage lock -> subscription/feedback JSON lock`。Web 保存路径先释放 JSON 锁再启动线程，因此不会持 JSON 锁等待 single-flight。完整并发合同见 [collection-concurrency.md](collection-concurrency.md)。
