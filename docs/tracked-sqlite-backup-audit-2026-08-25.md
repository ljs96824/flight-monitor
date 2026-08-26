# 跟踪 SQLite 备份只读审计（2026-08-25）

实际执行日期: `2026-08-26`。本报告只列结构、计数、哈希与风险类别，不复制任何数据库字段值。

## 范围

`git ls-files` 共发现 `3` 个 SQLite 类制品。

| 路径 | Git blob | SHA-256 | 字节数 | 首次引入提交 | 最后修改提交 |
| --- | --- | --- | ---: | --- | --- |
| `observations.combo-normalize-20260709162848.sqlite3.bak` | `84327ff034c87fe0429dd61b55416743e9321878` | `ff51d9dbf1c427eb3c5e24e9714d3f4b3a3c44227857eb4898b0d7982f851891` | 479232 | `76750cb6dc91e2500cc093becda5f3dd156d2712` | `76750cb6dc91e2500cc093becda5f3dd156d2712` |
| `observations.combo-normalize-20260709164309.sqlite3.bak` | `84327ff034c87fe0429dd61b55416743e9321878` | `ff51d9dbf1c427eb3c5e24e9714d3f4b3a3c44227857eb4898b0d7982f851891` | 479232 | `76750cb6dc91e2500cc093becda5f3dd156d2712` | `76750cb6dc91e2500cc093becda5f3dd156d2712` |
| `observations.combo-normalize-20260709164334.sqlite3.bak` | `84327ff034c87fe0429dd61b55416743e9321878` | `ff51d9dbf1c427eb3c5e24e9714d3f4b3a3c44227857eb4898b0d7982f851891` | 479232 | `76750cb6dc91e2500cc093becda5f3dd156d2712` | `76750cb6dc91e2500cc093becda5f3dd156d2712` |

三份文件逐字节一致，因此内容只审计一份；所有路径仍单独列示。

## 只读方法

- SQLite URI 使用 `mode=ro&immutable=1`。
- 连接后立即设置 `PRAGMA query_only=ON`，并由 `closing()` 显式关闭。
- 审计前后核对 SHA-256、纳秒级 mtime 与 `-wal/-shm/-journal` sidecar 状态。
- 合同结果: `通过`；审计前后 sidecar 数 `0 -> 0`。

## 结构与完整性

- `integrity_check`: `ok`
- `user_version`: `0`
- `sqlite_schema` 对象数: `1`
- 触发器数: `0`；视图数: `0`；虚拟表数: `0`

### 表 `observations`

- 行数: `2245`
- 外键数: `0`；索引数: `1`；CHECK约束数: `0`
- 虚拟表: `否`

| 列名 | 类型 | 约束 |
| --- | --- | --- |
| id | INTEGER | PK#1 |
| observed_at | TEXT | NOT NULL |
| round_id | TEXT | NOT NULL |
| route_type | TEXT | NOT NULL |
| origin_airport | TEXT | NOT NULL |
| dest_airport | TEXT | NOT NULL |
| depart_date | TEXT | NOT NULL |
| days_to_departure | INTEGER | NOT NULL |
| cabin_class | TEXT | NOT NULL |
| source | TEXT | NOT NULL |
| flight_combo | TEXT | NOT NULL |
| airline | TEXT | 无 |
| stops | INTEGER | 无 |
| price_cny | REAL | NOT NULL |
| method_version | TEXT | NOT NULL |


## 敏感性分类

内部扫描检查可疑列名及邮箱、电话、高熵串、路线/日期、订阅约束与强凭据模式；这里只公开是否存在和计数。

| 字段类别 | 是否存在 | 命中行数 | 命中列数 | 风险等级 |
| --- | --- | ---: | ---: | --- |
| 秘密凭据 | 否 | 0 | 0 | 严重 |
| 直接个人信息 | 否 | 0 | 0 | 高 |
| 个人行程元数据 | 是 | 2245 | 3 | 高 |
| 匿名市场观测 | 是 | 2245 | 6 | 低 |
| 高熵标识符 | 否 | 0 | 0 | 中 |

| 内容模式 | 是否存在 | 命中行数 |
| --- | --- | ---: |
| 邮箱样式 | 否 | 0 |
| 电话样式 | 否 | 0 |
| 高熵字符串 | 否 | 0 |
| 路线或日期记录 | 是 | 2245 |
| 订阅约束结构 | 否 | 0 |
| 强凭据模式 | 否 | 0 |

## 来源判定

- 分类: **个人行程元数据**。
- 依据: 存在路线、日期、订阅或约束类记录；无直接身份字段不足以证明其为纯匿名数据。
- 该分类不以“未发现邮箱”推断无风险；数据库缺少直接身份键，也不能证明路线与日期和个人计划无关。

## 推荐处置

- 本审计不移动、不删除任何制品，也不改写 Git 历史。
- 若所有者确认这些快照承载个人行程元数据，比例适当的后续方案是：从当前分支删除三份跟踪文件、保留 `.gitignore` 防线、**不改写历史**。历史仍可见是已知残余风险，是否接受由所有者决定。
- 若日后证明内容纯合成，也不建议保留三份相同二进制；更干净的方向是运行时生成或使用 `.sql`/`.json` fixture。该转换不属于本笔。
- 只有发现真实秘密凭据时才值得优先讨论轮换与历史清理；本报告生成本身即证明未触发秘密凭据硬闸。

## 已知限制

- 内容模式扫描用于风险分级，不是法证级凭据发现器，也不能证明数据主体身份。
- Git blob 与提交记录只覆盖当前可达历史；远端缓存、fork 与第三方镜像不在本地审计范围。
- `immutable=1` 适用于静态备份审计；生产数据库未被本脚本读取或写入。

## 处置记录

- 裁决：用户批准删除当前分支三份备份，不重写 Git 历史。
- 执行提交 SHA：本报告所在提交；可用 `git log -1 --format=%H -- docs/tracked-sqlite-backup-audit-2026-08-25.md` 获取。Git 提交无法在自身内容中稳定嵌入最终 SHA，最终值同时记入交付报告。
- 本地副本：未额外留存；工作树中的三份物理文件随 `git rm` 删除，唯一历史 blob 仍由 Git 历史保留。
- 当前树体积：跟踪数据库类制品由 `1,437,696` 字节降至 `0`；因三份文件原本共用同一 blob 且本次不重写历史，Git 对象库不会回收该历史对象。
- 不重写理由：当前结论为中等风险的个人行程元数据，未发现秘密凭据或直接个人信息；相较强制重写全部 SHA、破坏现有克隆与提交引用的成本，本次只清理当前分支。
- 已知限制：文件仍存在于76750cb及其后的历史提交中,通过git历史仍可取得;若将来判定风险升级,可再评估filter-repo
