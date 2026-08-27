# 运行配置分层记录（2026-08-27）

## 结论

代码版本、长期政策与实时运行证据不再共存于一个跟踪文件：

- `config.defaults.yaml`：Git 跟踪的阈值、保留窗、硬门和占位值；
- `config.yaml`：政策兼容副本，不再被生产入口读取，映射必须与 defaults 完全相同；
- `data/runtime_config.yaml`：Git 忽略的额度包、控制台对账、储备纪元、
  研究开关、暂停路线和本地订阅；
- `config.example.yaml`：新环境初始化模板，不含控制台实值或真实订阅。

生产采集、篮子、通知配额展示、readiness 与研究控制统一严格合并前两层。
`data/runtime_config.yaml` 缺失、YAML 损坏、根节点错误或关键额度事实不完整时，
在获取采集单飞锁、打开轮档或调用任何外部源之前抛出 `RuntimeConfigError`。
诊断夹具仍可显式传入一个完整的单文件配置，避免测试依赖真实运行状态。

## 一次性迁移

入口：

`python -X utf8 scripts/migrate_runtime_config.py`

默认只计算拆分结果与 SHA，不写文件。明确加 `--write` 时，脚本先生成带 UTC
时间戳的旧配置备份，再以临时文件、`fsync` 和 `os.replace` 写入 defaults 与 runtime。
重复执行相同输入返回 `status=unchanged`。

本次迁移记录：

- dry-run：`merged_equal=true`；
- write：`status=written`；
- 迁移生成的 defaults 映射 SHA-256：
  `ac6349a915b0b702492f20f283f4cf13eeb6541fd864cce92627adb591c95cea`；
- 加入说明注释后的跟踪文件 SHA-256：
  `1d7ddc6fc316abfd731e22daa90cf3bb9213cb72a526bd135864932aa7b7ef51`
  （注释不改变 YAML 映射）；
- runtime SHA-256：`c1652e99d6f0a6892303016065bd2f5ec2dffe66b9d502e4f6d5f1c6555d1664`；
- 深度合并后的映射与迁移前映射逐字段相同。

哈希只用于确认迁移输入输出，没有把运行配置内容复制进本文。

## 备份与恢复

`runtime_backup_v2` 将 `runtime_config.yaml` 列为必需核心状态，归档路径为
`state/runtime_config.yaml`。捕获时它与 API 台账、订阅和反馈一起在单飞窗口内加锁读取；
恢复时同时验证文件 SHA、YAML 可解析性和运行配置关键字段。

因此，只有包含本地运行配置且成功恢复验证过的归档，才算可用备份。旧
`runtime_backup_v1` 归档仍可按其原 manifest 验证，但不满足当前完整运行状态合同。

## 契约

- 跟踪配置不得出现控制台使用量、控制台余量、未入账调整或真实订阅数组；
- 跟踪配置中的目标日期只能是空值或占位；
- `config.yaml` 与 `config.defaults.yaml` 的 YAML 映射必须一致；
- 运行配置不存在或损坏时，生产采集禁止真实 API；
- 迁移 dry-run 无副作用，write 前备份且重复执行幂等；
- runtime backup 缺少 `runtime_config.yaml` 必须失败。

## 部署

本次修改触及 `main.py`、`basket_collect.py`、`notifier.py` 及其常驻 import 链。
PythonAnywhere pull 后需要 Reload，并先在部署机创建、核对
`data/runtime_config.yaml`；否则严格加载会按设计阻断采集。
