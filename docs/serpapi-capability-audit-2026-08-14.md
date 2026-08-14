# SerpAPI 商务舱能力审计

## 结论

本轮未获得能力证据。执行环境缺少 `SERPAPI_KEY`，审计脚本在发起 HTTP 请求前
安全停止，真实 API 消耗为 0。按接入门约定，生产源适配、月度配额治理、源策略和
分析层改动均未开始。

补齐本地 `.env` 的 `SERPAPI_KEY` 后，执行：

```powershell
python -X utf8 scripts\serpapi_capability_audit.py --execute \
  --output data\serpapi_capability_audit_20260814.json
```

脚本固定查询 `PVG -> KIX / 2026-10-01`，依次请求商务舱与经济舱各一次，
总消耗 2 次；硬限制为总计不超过 6 次、SerpAPI 不超过 3 次。

## 官方契约

- Google Flights API: <https://serpapi.com/google-flights-api>
- 舱位参数 `travel_class`: `1=Economy`、`2=Premium economy`、`3=Business`、`4=First`。
- `best_flights` 与 `other_flights` 使用同一结构；每个行程含 `flights`、`layovers`、
  `total_duration` 与所选货币的 `price`。
- 航段可包含 `plane_and_crew_by`，可作为实际提供飞机与机组航司的证据；字段缺失时
  只能回退到市场承运口径。
- 主结果没有独立税额字段。因此即使返回 `currency=CNY` 与正价，也只能标为
  “单成人单程展示价，税费口径以预订页为准”。

## 能力矩阵

| 源 | 经济舱 | 商务舱 | 当前结论 | 生产可用性 |
|---|---|---|---|---|
| Juhe | 已有单人经济舱参考价 | 请求无舱位参数 | 仅经济舱 | 经济舱主源 |
| Duffel | 可返回 offer | test token 可返回商务舱但非真实市场价 | 部分 | 仅规则富化 |
| SerpAPI | 待审计 | 待审计 | 缺少密钥，未调用 | 暂不接入 |

## 接入路线门

只有 SerpAPI 商务舱响应同时满足以下条件才进入生产接线：

1. `best_flights` 或 `other_flights` 至少有一条行程；
2. 行程价为正数；
3. 至少有真实航司名；
4. 航段 `travel_class` 明确包含 `Business`。

审计通过后的候选路线为：Juhe 继续承担经济舱，SerpAPI 仅承担商务舱，Duffel 继续
承担行李与退改规则富化。审计未通过时保持现有生产行为不变。
