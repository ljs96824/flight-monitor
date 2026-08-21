# 隐私曝露面审计（Phase 0）

审计日期：2026-08-21

范围：`flight-monitor` 当前工作树、Git 全史、本机运行数据、PythonAnywhere（PA）详情存储与公开详情路由

原则：先量测后整改；本轮不改业务算法、不删除用户数据、不调用任何航班数据源 API。

## 1. 结论

最高优先级问题不是航班查询接口，而是 PA 详情链路：详情路由当前无认证、接受可预测的旧数字索引，并在缺少 `sub` 时回退到 `page_results.json` 的最近记录。匿名抽样证实，旧数字索引均以 HTTP 200 返回真实详情。远端 `payloads` 目录现有 118 个文件，其中 107 个是数字索引文件。

第二类问题是数据最小化不足。航班源没有收到真实乘客构成，但 PushPlus、SMTP 与 PA 详情 payload 会携带完整行程、价格、预算、乘客画像、约束与方案明细；PA payload 同时保存 HTML 和结构化 payload，形成重复曝露。

第三类问题是本机留存和日志脱敏。轮档、历史活体日志及订阅备份累计出现大量邮箱文本；现有脱敏仅覆盖 key/token/password，不覆盖邮箱、收件人、订阅标识、反馈文本和地点。

立即整改顺序应为：先收紧详情路由并移除回退，再备份并 dry-run 识别旧文件，最后执行清理和匿名 404 验证。UUID 只能降低枚举风险，不能替代认证或签名访问。

## 2. 方法与边界

- 航班源字段来自实际 `fetch`/请求构造代码，不按文档猜测。
- 表单字段来自离线渲染后的 HTML `name` 集合：快速页 20 个，完整页 132 个。
- payload 字段来自本机 129 个详情文件和冻结夹具的结构并集：107 个顶层字段、8,756 个实际观测到的嵌套路径。
- PA 存量通过管理员 Files API 只读枚举；匿名访问仅对详情页做 GET 状态码和响应长度核验，未读取或复制正文。
- 公库使用当前受控文件扫描和 `git log -G` 全史路径扫描；邮箱只做占位符分类，不在本报告记录原值。
- 本机 PII 扫描只输出文件位置、计数和 JSON 字段路径；不复制匹配值。
- 本轮没有调用 juhe、SerpAPI、Duffel、PushPlus 或 SMTP；PA 只读量测是本任务 B 项要求，不计入航班 API 台账。

分类标记：

- `N`：完成当前功能所需，但仍应遵循最小留存。
- `S`：功能所需且敏感，应限制传输、日志和保留期。
- `R`：应脱敏、散列、签名或改成不可预测标识。
- `D`：可去除或由服务端派生，不应跨边界重复传输。

## 3. A：数据流清单

### 3.1 航班数据源

| 管道 | 实际发送字段 | 真实乘客构成 | 分类与结论 |
|---|---|---:|---|
| juhe GET | `key`、`departure`、`arrival`、`departureDate` | 否 | 航线和日期为 `S`；`key` 为 `R`；其余均为请求必要字段。当前没有舱位或人数参数。 |
| SerpAPI GET | `engine`、`departure_id`、`arrival_id`、`outbound_date`、`type`、`currency`、`hl`、`gl`、`sort`、`stops`、`travel_class`、`api_key` | 否 | 航线、日期、舱位为 `S`；`api_key` 为 `R`；语言、地区、币种、排序和停站条件为 `N`。 |
| Duffel POST | `data.slices[].origin`、`destination`、`departure_date`，`data.passengers=[{"type":"adult"}]`，`data.cabin_class`、`data.return_offers`；请求头含 Bearer token、API 版本、内容类型 | 否 | 发送的是固定单成人，不是订阅乘客构成；航线、日期、舱位为 `S`，token 为 `R`。固定单成人是可复用的规则富化请求。 |

代码证据：`sources/juhe_source.py:234-245,310-318`、`sources/serpapi_source.py:63-77`、`sources/duffel_source.py:64-85`。

结论：当前外部航班源没有收到成人/儿童/老人/婴儿人数，也没有收到预算、邮箱、会议地点或通知偏好。Phase 1 的“乘客归一”应针对通知与远端详情，不应误改航班查询定价口径。

### 3.2 通知通道

| 管道 | 实际发送字段 | 分类与最小化建议 |
|---|---|---|
| PushPlus | JSON：`token`、`title`、`content`、`template=html` | token 为 `R`；标题和完整 HTML 为 `S`。正文可包含路线、日期、预算、乘客画像、约束、航班、价格、验证链接和详情链接。Phase 1 应支持“摘要通知”，默认不发送会议、发票和精确乘客明细。 |
| SMTP | SMTP 认证用户名/密码；信封发件人/收件人；`Subject`、`From`、`To`；完整 HTML；可选内嵌价格图 | 凭据与邮箱为 `R`，正文与图为 `S`。邮件可保留富内容，但应获得明确选择，并禁止在日志打印完整收件人。 |
| 反馈通知 | `subscription_id`、反馈类型、不可购买原因、自由文本、时间、User-Agent，通过 SMTP 发给作者 | 标识与自由文本为 `S/R`；User-Agent 通常可去除或截短。当前反馈发送成功日志也会打印作者邮箱。 |

代码证据：`notifier.py:276-287`、`email_notifier.py:169-213`、`web_form.py:649-688`。

### 3.3 PA 详情 payload 上传

本机保存并上传的记录结构是：

```text
subscription_id
created_at
html
payload
```

上传使用 PA Files API：Authorization token、远端用户名/路径、multipart 文件名和完整 JSON 文件。`html` 与 `payload` 大量重复，且远端文件名暴露订阅标识。代码位置：`main.py:943-988`。

107 个顶层 payload 字段按处理建议分组如下；同一行内每个字段继承行首分类：

**N：用户详情展示所需**

```text
action_range, adjustment_required_plans, airport_cost_comparison, alert_policy,
alternative_plans, budget_gap, buy_condition, buy_condition_explanation, buy_risk,
cabin_policy_summary, channel_price_rows, checklist, collected_at, current_price,
data_freshness, days_to_dept, depart_date, destination_airports, display_price,
dual_source_agreement, dual_source_price_anomalies, excluded_plans, execution_advice,
frequency, ideal_price, is_roundtrip, max_price, mixed_cabin, nearby_date_prices,
next_step_guidance, no_primary_diagnosis, no_primary_reason, origin_airports,
price_calendar, price_history, price_references, price_signal, price_tiers,
purchase_budget_decision, push_type, recommendation, recommendation_basis,
recommended_plans, return_date, risk_summary, route, route_airports, route_info,
route_type, same_day_alternatives, same_day_no_feasible_note, scenario_recommendation,
sorting_logic, source_count, source_degradation, source_retirement, source_stats,
tcurve, time_filter_note, transaction_price, trend_fallback, trend_summary,
trigger_reason, trip_type, verify_price, wait_risk
```

**S/R：敏感或可关联，应认证访问、脱敏日志、设保留期**

```text
constraint_change, constraint_fingerprint, constraint_fingerprint_short,
destination_airports_active, detail_url, feedback_url, form_url, invoice_preferences,
origin_airports_active, passenger_pricing, passenger_profile, passenger_rules,
plan_status_change, snapshot, subscription_id, travel_profile,
travel_profile_explanation, travel_scenarios
```

**D：内部比较值、重复派生或诊断字段，远端详情未消费时应移除**

```text
budget_compare_price, budget_compare_scope, budget_input_ideal_price,
budget_input_max_price, budget_scope, candidate_price_summary, confidence,
confidence_details, confidence_dimensions, diff_from_last, freshness_minutes,
last_push_price, limits, max_budget_pp_oneway, max_budget_scope, plan_price_rows,
price_policy_reason, provenance, source_errors, target_compare_scope,
target_price_pp_oneway, target_price_scope, versions
```

上述 `D` 是曝露面分类，不是本轮删除决定。Phase 1 应先以详情模板实际读取键做 allowlist，再移除未消费字段；不得直接删字段破坏历史详情。

### 3.4 表单 POST 与 PA 订阅存储

`POST /subscribe` 当前构建完整订阅、写入 `data/subscriptions.json`，随后触发后台采集。若页面部署在 PA，以下字段会由浏览器直接发送到 PA。当前路由未见登录鉴权或 CSRF token。代码位置：`web_form.py:2252-2277`。

快速页实际 20 字段：

```text
[S] depart_date, destination, max_budget, origin_select, passenger_count,
    return_date, target_price, travel_scenario
[N] destination_airports_active, max_budget_scope, origin_airports_active,
    round_trip, target_price_scope
[D] companion_constraints_seed, companion_constraints_seed_present,
    derive_companion_constraints, form_page, monitor_mode, route_type
[R] subscription_index
```

完整页实际 132 字段：

```text
[S] adult_count, budget_scope, business_end, business_start, child_count,
    companions, depart_date, destination, elderly_count, infant_count,
    invoice_context, invoice_needed, invoice_special_vat, max_budget,
    meeting_end, meeting_location, meeting_start, notification_email,
    origin_manual, origin_select, passenger_count, reimburse_per_person,
    return_date, target_price, travel_scenario, trip_natures

[R] subscription_index

[D] business_seats, cabin_allocation_ui, cabin_business_types,
    companion_constraints_seed, companion_constraints_seed_present,
    derive_companion_constraints, economy_seats, form_page, monitor_mode,
    route_type, ux2_concept_form, ux2_original_arrival_time_policy,
    ux2_original_departure_time_policy, ux2_time_touched

[N] accept_overnight_transfer, accept_self_transfer, airline_policy,
    airport_advance_min, allow_redeye, arrival_exit_min, arrival_preference,
    baggage, blocked_airlines_common, buffer_hours, cabin_arrangement,
    cabin_business_adult, cabin_business_child, cabin_business_elderly,
    cabin_business_infant, cabin_economy_adult, cabin_economy_child,
    cabin_economy_elderly, cabin_economy_infant, cabin_policy, child_type,
    custom_redundancy_min, date_flexibility, day_trip_period,
    delay_buffer_min, destination_airports_active, destination_transport_min,
    digest_time, elderly_condition, exclude_airlines, invoice_cabin_limit,
    lcc_policy, max_budget_mode, max_budget_scope, meeting_importance,
    mobility_limited, notification_frequency, notification_frequency_rule,
    notification_method, origin_airports_active, origin_transport_min,
    outbound_allow_redeye, outbound_arrival_preference,
    outbound_arrival_window_end, outbound_arrival_window_start,
    outbound_departure_window_end, outbound_departure_window_start,
    outbound_set_off, outbound_time_preference, post_meeting_buffer_min,
    pre_meeting_buffer_min, price_change_threshold, price_sensitivity,
    price_strategy, price_tolerance_custom, price_tolerance_mode, primary_goal,
    redundancy_min, refund_flexibility, remember_preferences,
    return_allow_redeye, return_arrival_preference,
    return_arrival_window_end, return_arrival_window_start,
    return_date_flexibility, return_departure_window_end,
    return_departure_window_start, return_set_off, return_time_preference,
    round_trip, same_day_round_trip, same_flight_required, secondary_goals,
    separate_direction_times, shared_arrival_window_end,
    shared_arrival_window_start, shared_departure_window_end,
    shared_departure_window_start, short_transfer_limit, solo_travel,
    target_price_mode, target_price_scope, team_date_flexibility,
    team_passenger_count, time_preference, transfer_policy,
    transport_margin_mode, transport_mode, trip_rigidity, user_level,
    user_transport_min
```

注意：`D` 中部分字段是兼容镜像，不应简单从 schema 删除；建议只从浏览器 POST 去除，由服务端按规范控件派生。`subscription_index` 应改用 UUID 或签名编辑令牌，不能继续作为可预测编辑身份。

PA 同步方向当前是 PA → 本地的“仅新增摄入”，只发送管理员 GET 和 Token 头，不把本地订阅上传回 PA；返回体是 PA 的完整订阅数组。代码位置：`sync_subscriptions.py:137-198`。

## 4. B：PA 存量曝露（最高优先）

### 4.1 只读库存

PA `data/payloads/` 当前只读枚举结果：

| 类别 | 数量 | 风险 |
|---|---:|---|
| 旧数字索引 JSON | 107 | 可预测、可批量枚举 |
| UUID JSON | 1 | 不易枚举，但仍是无认证 capability URL |
| 航线/日期式旧文件名 | 9 | 文件名自身泄露行程元数据 |
| 测试式其他文件名 | 1 | 非生产身份格式 |
| 合计 | 118 | 远端留存无保留窗 |

数字索引范围覆盖 0 至 137，中间有 31 个空洞；本报告不记录任何 UUID 或航线文件名。

### 4.2 匿名访问实测

对 3 个数字索引样本、1 个不存在控制值和无参数入口做匿名 GET：

| 请求 | HTTP | 响应长度 | 判读 |
|---|---:|---:|---|
| 数字索引样本 A | 200 | 25,590 bytes | 返回真实详情 |
| 数字索引样本 B | 200 | 28,595 bytes | 返回真实详情 |
| 数字索引样本 C | 200 | 49,681 bytes | 返回真实详情 |
| 不存在控制值 | 200 | 1,079 bytes | 空模板仍为 200 |
| 不带 `sub` | 200 | 1,045 bytes | 当前样本为空模板；代码仍有“最新记录”回退 |

三个真实响应长度和内容哈希彼此不同，且显著大于空控制页。未解析或复制响应正文。

### 4.3 根因

`web_form.py:2375-2390` 的 `/detail`：

1. 没有认证或授权检查。
2. `sub` 经文件名净化后直接映射 JSON 文件，没有 UUID 格式限制。
3. 文件未命中时继续搜索 `page_results.json`。
4. 不带 `sub` 时返回最近记录。
5. 日志打印全部现存 storage key。

因此，“仅改成 UUID 文件名”不足以完成隐私整改；至少还需严格格式、移除回退和访问控制。

### 4.4 整改序列（本轮不执行）

顺序不可颠倒：

1. 部署路由修复：仅接受严格 UUID；数字、航线式、测试式、缺参和未命中均返回 404；移除 `page_results.json` 回退与 storage key 全量日志。
2. 增加真实访问控制。最低限度使用带过期时间的 HMAC 签名链接；更稳妥的是登录态。裸 UUID 仅视为临时 capability token。
3. PA Reload，先验证旧数字索引和无参数入口已不可读。
4. 备份远端目录和 `page_results.json`。
5. dry-run 分类文件，确认只保留 UUID 文件。
6. 经人工确认后删除旧数字、航线/日期式和测试式文件，并清空或重建 `page_results.json`。
7. 再次匿名验证：旧索引、缺参、未知 UUID 均为 404；授权 UUID 按设计返回。
8. 为 payload 和备份增加保留窗与审计日志。

PA 备份与 dry-run 命令示例：

```bash
cd ~/flight-monitor
stamp=$(date +%Y%m%dT%H%M%S)
backup="$HOME/privacy-backups/flight-monitor-$stamp"
mkdir -p "$backup"
cp -a data/payloads "$backup/"
cp -a data/page_results.json "$backup/"

python - <<'PY'
from pathlib import Path
import re

root = Path.home() / "flight-monitor" / "data" / "payloads"
uuid_name = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\.json$",
    re.I,
)
legacy = [path for path in root.glob("*.json") if not uuid_name.fullmatch(path.name)]
print(f"DRY-RUN legacy={len(legacy)} keep_uuid={sum(uuid_name.fullmatch(p.name) is not None for p in root.glob('*.json'))}")
for path in sorted(legacy):
    print(path.name)
PY
```

执行删除应在上述路由修复、Reload、备份和 dry-run 人工确认之后另行运行；Phase 0 不提供自动删除默认值。

## 5. C：公库考古

### 5.1 当前工作树

扫描口径：日期族为已知真实行程日期签名；预算为预算字段名和中文预算文案；乘客为人数/类型字段和中文人群文案；邮箱为标准邮箱正则。

| 类别 | 当前命中文件 | docs 文件 | 结论 |
|---|---:|---:|---|
| 真实日期族 | 62 | 3（5 行） | 能与航线、预算和乘客夹具拼接成真实行程画像 |
| 预算语义 | 68 | 7（15 行） | 多数为测试或设计规格，但根目录生成物含真实场景数字 |
| 乘客构成 | 71 | 8（17 行） | 多数为 schema/测试；与日期、预算同现时敏感度上升 |
| 邮箱 | 7 | 0 | 29 个当前匹配全部是测试/示例占位符，未发现真实邮箱 |

docs 命中位置：

| 文件 | 日期行 | 预算行 | 乘客行 | 建议 |
|---|---:|---:|---:|---|
| `docs/cabin-capability-audit-2026-08-13.md` | 3 | 1 | 0 | 改为相对日期和占位预算 |
| `docs/serpapi-capability-audit-2026-08-14.md` | 1 | 0 | 1 | 审计夹具改为匿名乘客构成 |
| `docs/superpowers/plans/2026-08-11-forecast-layer.md` | 1 | 0 | 0 | 改相对日期 |
| `docs/superpowers/plans/2026-08-12-source-degradation-evidence.md` | 0 | 1 | 0 | 改占位预算 |
| `docs/superpowers/specs/2026-08-12-form-ux-phase1-design.md` | 0 | 1 | 3 | 保留 schema，替换真实组合示例 |
| `docs/superpowers/specs/2026-08-13-form-ux-quick-finish-design.md` | 0 | 1 | 0 | 改占位预算 |
| `docs/superpowers/specs/2026-08-13-form-ux2-concept-wizard-design.md` | 0 | 7 | 4 | 替换为统一匿名夹具 |
| `docs/superpowers/specs/2026-08-13-form-ux3-two-page-contract-design.md` | 0 | 3 | 2 | 替换为统一匿名夹具 |
| `docs/superpowers/specs/2026-08-13-form-ux31-render-completeness-design.md` | 0 | 0 | 1 | 保留结构，匿名化人数 |
| `docs/superpowers/specs/2026-08-13-form-ux33-scenario-time-windows-design.md` | 0 | 0 | 2 | 保留结构，匿名化人数 |
| `docs/superpowers/specs/2026-08-14-ux34-parallel-dimensions-design.md` | 0 | 1 | 2 | 替换为统一匿名夹具 |
| `docs/superpowers/specs/2026-08-14-ux37-quota-companion-cabin-design.md` | 0 | 0 | 2 | 保留分舱结构，改人数占位 |

根目录高风险生成物包括 `before*.json`、`after*.json`、`check.json`、`snapshot_run_test.json`、上海—大阪 HTML、诊断日志和 diff JSON。这些文件同时包含日期、预算、乘客构成和方案文本，优先级高于单独的设计文档。建议迁移成脱敏 fixture，或从当前树移除并保留哈希契约。

### 5.2 Git 全史

`git log -G` 的历史去重路径数：日期 62、预算 70、乘客构成 71、邮箱 9。邮箱历史路径是 `.env.example`、表单基线/烟测脚本、相关测试和旧 `web_form.py`。对 `.env.example` 的 9 个历史版本做值级分类：5 次匹配、3 个唯一值，全部符合占位符规则；未发现可确认的真实邮箱或密钥。

历史改写评估：

- 对真实密钥、真实邮箱等直接身份信息，应使用 `git filter-repo` 改史并轮换凭据。
- 本次未发现这类秘密；日期、预算、乘客组合属于可关联隐私，但分散在 259 个提交和大量回归夹具中。
- 现在改写全史会改变全部后续 commit hash、破坏链接/基线并要求所有克隆重新同步，成本高。
- 建议先清理当前树并建立匿名 fixture 契约；只有在确认这些行程数字可识别具体个人、且仓库确为公开时，再单独审批历史改写。

## 6. D：本机 PII 与脱敏缺口

### 6.1 只读计数

| 位置 | 文件数 | 邮箱匹配 | 手机号匹配 | 判读 |
|---|---:|---:|---:|---|
| `data/logs/rounds/*.log` | 6 | 410 | 0 | 轮档重复记录通知收件人 |
| `data/two_phase_live*.log` | 4 | 198 | 0 | 历史活体日志保留收件人 |
| `data/subscriptions.json.bak*` | 3 | 84 | 0 | 完整订阅备份，无保留窗 |
| `data/ui_smoke_latest.log` | 1 | 1 | 0 | 测试占位邮箱 |
| 当前 `data/subscriptions.json` | 1 | 1 | 0 | 业务必要，但明文静态存储 |
| `data/payloads/*.json` | 129 | 0 | 0 | 当前样本没有邮箱/手机号，但含完整行程画像 |
| `data/page_results.json` | 1 | 0 | 0 | 原始正则的 3 个“手机号”均位于指纹字段，是误报 |

三个 SQLite 备份的二进制字节曾产生 6 个手机号正则命中；结构化复核未发现手机号字段，不计为 PII 证据。

### 6.2 现有脱敏能力

- `log_utils._redact_round_evidence` 只按 key/token/password/authorization/secret 等键名和查询参数脱敏。
- `request_cache._redact_error_text` 只覆盖 API key、access token、token、authorization。
- `sources.aggregator._redact_api_key` 只截断 `api_key=`。
- `email_notifier.send_email` 成功日志直接打印完整收件邮箱。
- `web_form.notify_feedback_author` 成功日志直接打印作者邮箱。
- `/detail` 日志会打印全部订阅 storage key。

### 6.3 `_redact` 扩编方案（Phase 1）

1. 建立一个递归 `redact_log_value` 单一入口，供 `safe_log`、轮档证据、source error、HTTP 响应摘要共同调用。
2. 密钥：保留现有规则，并覆盖 header、JSON、query、Bearer、cookie、SMTP 凭据。
3. 邮箱与手机号：默认替换为 `<EMAIL>`、`<PHONE>`；需要关联排障时只保留不可逆短哈希，不保留域名前本地部分。
4. 订阅标识：日志显示 `<SUB:hash8>`；URL 中 `sub` 参数同样处理。不得打印 storage key 全集。
5. 自由文本：反馈 comment、错误响应 body、User-Agent 走长度限制和字段 allowlist，禁止整段入轮档。
6. 行程：普通运行日志可保留 IATA 与日期用于对账；公开 issue/CI artifact 默认将日期相对化。会议地点、发票文本不进入诊断日志。
7. 发送日志：只写通道、成功/失败和收件人哈希，不写完整邮箱。
8. 保留窗：轮档、payload、page_results、订阅备份分别定义期限，先 dry-run 报告再清理。

## 7. E：结论矩阵与整改分期

| 排序 | 曝露方 | 严重度 | 修复成本 | 处置 |
|---:|---|---|---|---|
| 1 | PA 无认证详情 + 可预测数字索引 + latest 回退 | 严重 | 中 | **立即 B 类**：严格 UUID/签名或登录、404、移除回退、备份后清旧文件 |
| 2 | PA 完整 HTML + 107 字段 payload 重复上传 | 高 | 中 | Phase 1：远端 allowlist、去掉 HTML/结构重复、最小化详情 |
| 3 | 公开 `/subscribe` 无鉴权/CSRF，提交会触发后台采集 | 高 | 中 | 立即安全整改：登录、CSRF、速率限制；与隐私 Phase 1 协同 |
| 4 | 轮档/活体日志明文邮箱 | 高 | 低 | **立即可做**：发送日志改哈希；随后扩编统一 redactor |
| 5 | 订阅与备份明文邮箱、无保留窗 | 高 | 中 | Phase 1：文件权限、备份加密或脱敏、保留窗 |
| 6 | PushPlus/SMTP 默认发送完整详情 | 中高 | 中 | **Phase 1 算法**：通知分级，摘要默认、富详情显式选择 |
| 7 | 精确乘客画像在远端详情和通知中传播 | 中高 | 中 | **Phase 1 算法**：乘客归一；外发默认只给总人数/必要票价分组 |
| 8 | 公库保留真实日期、预算、乘客组合和生成 HTML | 中 | 低至高 | 先清当前树；Git 改史仅在可识别风险确认后审批 |
| 9 | 反馈自由文本、User-Agent、订阅 ID 进入本地与邮件 | 中 | 低 | 字段 allowlist、截短、哈希标识、保留窗 |
| 10 | 航班源接收 IATA、日期、舱位 | 低且必要 | 高 | 现状已最小化；不发送真实人数、预算、邮箱，维持 |

Phase 1 算法改动边界：

- 乘客归一：只改变通知和远端详情的呈现/传输，不改变 `price_estimator` 的人群费率或预算判定。
- 通知分级：PushPlus 默认摘要，SMTP 可选富详情；敏感字段按用户选择和通道能力分层。
- 保留窗：对轮档、详情 payload、page_results、备份和反馈分别设定期限，所有删除先 dry-run。

立即整改边界：PA 详情路由、旧文件清理顺序、日志邮箱脱敏不需要改价格、采集或推荐算法。

## 8. 后续验收建议

Phase 1 应增加以下契约测试：

1. 匿名数字索引、缺参、未知 UUID 均为 404。
2. 已签名 UUID 只能读取自身 payload，过期签名失败。
3. PA payload allowlist 不含邮箱、会议地点、发票文本和内部诊断字段。
4. 日志夹具包含邮箱/手机号/token/订阅 ID 时，输出只剩占位符或短哈希。
5. PushPlus 摘要不含精确乘客构成、会议地点和发票信息；富邮件需显式通道配置。
6. 保留窗 dry-run 与 execute 分离，删除前有备份和计数。
7. 公库测试拒绝真实邮箱及受控真实行程 fixture，统一使用匿名相对日期。

本报告本身未复制邮箱、手机号、UUID、token、远端旧文件名或详情正文。


## 9. Phase 1 实施与 PA 操作记录

### 9.1 本地实施边界

本轮实现以下边界，尚未执行 PA 删除：

- `/detail` 只接受规范 UUID，且对应 `data/payloads/<uuid>.json` 必须存在；数字、航线式、缺参、未知 UUID 一律 404。
- `page_results.json`、最近结果回退、旧列表猜测路径和 storage key 全量日志已移除。
- `SHARED_DETAIL_TOKEN` 为可选二次校验，默认关闭；启用后令牌只进入通知发送副本中的详情链接，不写 payload、不写日志。
- 三个航班源的外发参数继续禁止携带儿童、老人、婴儿人数或 `cabin_allocation`。
- 新日志统一把邮箱替换为 `<EMAIL>`；历史文件只由 `scripts/scrub_pii.py --execute` 在先备份后手工清洗。
- 通知隐私等级为 `full/redacted/minimal`，默认 `full` 且不新增旧订阅字段；降档必须由用户显式选择。
- 保留窗为 payload 90 天、轮档 90 天、备份 180 天，0 表示永久；轮末只打印 dry-run 到期计数，自动删除未启用。

PA 实际执行状态：**已完成部署、Reload、匿名 curl 验证与存量清理**。本节证据已回填，可以按用户授权发布审计与 Phase 1 修复。

### 9.2 PA 部署、备份、清理与验证命令

#### 9.2.1 2026-08-21 实际执行证据

以下结果来自用户 PA Bash 原始回执，报告不记录现役 UUID 原文：

| 验证项 | 实际结果 |
|---|---|
| 清理执行 | `mode=execute`，删除非 UUID payload 117 个，`page_results` 删除数 0 |
| 清理后库存 | 总数 1，UUID 1，非 UUID 0 |
| 备份归档 | `/home/ljs96824/payloads_backup_20260821T084004.tar.gz`，2.2M |
| 旧数字匿名访问 | `1`、`2`、`79` 均返回 HTTP 404 |
| 现役 UUID 匿名访问 | HTTP 200 |

实战暴露两项交付缺陷：裸 `--execute` 因缺少 `--backup-archive` 三次被
`ValueError` 拦截，但错误被长清单淹没；dry-run 默认逐条打印 117 行。现已改为：

- CLI 缺少或给出无效备份归档时，返回码 2，并单独打印正确用法示例；
- 默认仅列前 10 条，再打印“另有 N 条”；只有 `--verbose` 才打印全部。

#### 9.2.2 可重复执行序列

以下命令必须在 **修复代码已经同步到 PA 且 Web 页已 Reload** 后执行。删除工具默认 dry-run；执行态要求已有备份归档。

先在 PA Bash 控制台设置站点和现役 UUID。不要把真实 UUID 或共享令牌复制进本报告：

```bash
cd ~/flight-monitor
APP_BASE="https://<your-pythonanywhere-site>"
ACTIVE_UUID="<current-active-uuid>"

git status --short
git rev-parse --short HEAD
```

在删除任何文件前，从远端库存中保存三个旧数字索引作为 404 验证样本，并验证新路由已经生效：

```bash
readarray -t OLD_IDS < <(
  python - <<'PY'
from pathlib import Path
for path in sorted(
    (Path("data/payloads")).glob("*.json"),
    key=lambda item: int(item.stem) if item.stem.isdigit() else 10**12,
):
    if path.stem.isdigit():
        print(path.stem)
PY
)

if [ "${#OLD_IDS[@]}" -lt 3 ]; then
  echo "旧数字索引不足3个，停止清理"
  exit 1
fi

detail_status() {
  if [ -n "${SHARED_DETAIL_TOKEN:-}" ]; then
    curl -sS -o /dev/null -w '%{http_code}' --get \
      --data-urlencode "sub=$1" \
      --data-urlencode "token=$SHARED_DETAIL_TOKEN" \
      "$APP_BASE/detail"
  else
    curl -sS -o /dev/null -w '%{http_code}' --get \
      --data-urlencode "sub=$1" \
      "$APP_BASE/detail"
  fi
}

for id in "${OLD_IDS[@]:0:3}"; do
  printf '旧数字 %s -> ' "$id"
  detail_status "$id"
  printf '\n'
done
printf '现役UUID -> '
detail_status "$ACTIVE_UUID"
printf '\n'
```

此时三个旧数字必须均为 404，现役 UUID 必须为 200。若不满足，停止，不备份、不删除，先检查 Reload 和页脚 build 信标。

路由验证通过后生成时间戳备份并执行清理工具的 dry-run：

```bash
stamp=$(date +%Y%m%dT%H%M%S)
backup_dir="$HOME/privacy-backups/flight-monitor-$stamp"
mkdir -p "$backup_dir"
tar -C data -czf "$backup_dir/payloads.tgz" payloads
if [ -f data/page_results.json ]; then
  cp -a data/page_results.json "$backup_dir/page_results.json"
fi

python -X utf8 scripts/cleanup_legacy_payloads.py
ls -lh "$backup_dir/payloads.tgz"
```

人工核对 dry-run 清单后，禁止运行不带备份参数的裸命令
`python -X utf8 scripts/cleanup_legacy_payloads.py --execute`。唯一正确执行形式必须同时带
`--backup-archive`：

```bash
python -X utf8 scripts/cleanup_legacy_payloads.py \
  --execute \
  --backup-archive "$backup_dir/payloads.tgz"

python -X utf8 scripts/cleanup_legacy_payloads.py
```

最后重复匿名验证并打印清理后类别计数：

```bash
for id in "${OLD_IDS[@]:0:3}"; do
  printf '旧数字 %s -> ' "$id"
  detail_status "$id"
  printf '\n'
done
printf '现役UUID -> '
detail_status "$ACTIVE_UUID"
printf '\n'

python - <<'PY'
from pathlib import Path
from detail_access import canonical_detail_uuid

files = sorted(Path("data/payloads").glob("*.json"))
uuid_files = [p for p in files if canonical_detail_uuid(p.stem)]
legacy_files = [p for p in files if p not in uuid_files]
print(
    f"清理后总数={len(files)} UUID={len(uuid_files)} "
    f"非UUID={len(legacy_files)} page_results={int(Path('data/page_results.json').exists())}"
)
PY
```

### 9.3 远端执行结果回填

| 项目 | 修复前 | 修复后 |
|---|---:|---:|
| payload 总数 | 118（Phase 0 只读库存） | 待回填 |
| 数字索引 | 107（Phase 0 只读库存） | 待回填 |
| 其他非 UUID | 10（Phase 0 只读库存） | 待回填 |
| UUID | 1（Phase 0 只读库存） | 待回填 |
| `page_results.json` | 1 | 待回填 |
| 旧数字匿名 HTTP | 200 | 待回填（目标 404） |
| 现役 UUID 匿名/带令牌 HTTP | 未记录正文 | 待回填（目标 200） |

清理清单、备份归档路径、页脚 build 信标与 curl 原始状态码由用户 shell 输出作为唯一远端事实；本报告不复制真实 UUID、令牌或 payload 文件名。
