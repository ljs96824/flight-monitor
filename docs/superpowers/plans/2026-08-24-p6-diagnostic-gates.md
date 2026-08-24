# P6 诊断层门控统一与语义修正实施计划

> 日期：2026-08-24
> 范围：只读诊断与报告层；不改采样、金额、推荐判定或现有推送技能门。

## 目标

1. 统一 `forecast` shape 与 `tcurve` 的最小样本门槛。
2. 默认诊断报告不展示低样本伪精度；`--diagnostic` 仅供内部查看原始值。
3. shape 按日型分池，禁止跨日型借样本。
4. 为每个出发日输出整体可靠性分量和瓶颈。
5. 修正 level、组合供给、出现率和市场最低参考价的语义。
6. 在 T 曲线报告显式列出缺失格与 degraded 格。
7. 补齐 `prices.db` round_id lineage 待办的第三触发条件。

## 约束

- 零真实 API；所有报告仅只读本地数据。
- `data/observations.sqlite3`、`data/prices.db`、`data/page_results.json` 与
  `data/api_usage.json` 前后哈希必须一致。
- 不修改 `build_notification_forecast` 的技能门规则与用户可见金额。
- 默认输出隐藏 `n < MIN_SHAPE_N` 的中位数与分位数。
- 诊断原始值不得被标成可用于判断。

## TDD 步骤

### 1. Shape 门

文件：`test_forecast.py`、`forecast.py`

- 新增 `n=4` 阻断与 `n=5` 放行测试。
- 断言 `MIN_SHAPE_N is MIN_SAMPLE_FOR_TCURVE` 的同源关系。
- shape 点保留内部 raw 统计，但可用统计仅在达门槛时暴露。
- `predict_price` 遇到 shape 不足返回明确状态。

### 2. Regime 分层

文件：`test_forecast.py`、`forecast.py`、`scripts/forecast_report.py`

- 新增 `normal/weekend/holiday_eve/holiday/holiday_return` 分类测试。
- 用 `holidays.py` 的既有标签与 `depart_date.weekday()` 派生，不引入数据源。
- 新增按 regime 构建 shape 的入口；测试禁止跨 regime 借样本。
- 报告按 regime 分节，level 与预测只读目标日期所属 regime。

### 3. 整体可靠性

文件：`test_forecast.py`、`test_forecast_report.py`、`forecast.py`、
`scripts/forecast_report.py`

- 定义 level、shape、backtest、source coverage、regime match 五个分量。
- `overall_reliability = min(components)`；输出所有分量和首要瓶颈。
- 任一分量未达标时，预测行固定为“暂不提供预测”，并列未达项。
- 报告头固定说明内部诊断、技能门和是否进入用户推送。
- `--diagnostic` 才展示 raw 统计，并标“原始值，不可用于判断”。

### 4. 语义修正

文件：`test_patterns.py`、`test_forecast_report.py`、`patterns.py`、
`scripts/forecast_report.py`

- level 改为 `价格基准 level=CNY...`。
- 组合供给改为“候选组合结构”，补搜索组合非库存声明。
- 100% 出现率改为“在 N 次有效观测中均出现”。
- 所有 global_min 引用补“市场最低参考价·与用户筛选无关”。

### 5. T 曲线质量清单

文件：`test_tcurve.py`、`scripts/tcurve_report.py`

- degraded 格从日格数据直接列出。
- 缺失格通过现有 PermissionError 只读审计结果注入报告。
- 清单明确“缺失不参与趋势判断”。

### 6. Round lineage 待办

文件：`docs/permission-error-observation-pollution-audit-2026-08-24.md`、
`scripts/audit_permission_pollution.py`

- 保留现有两个触发条件。
- 新增：任何基于轮次/约束纪元的预测或自动建议进入用户推送前必须实现。

## 验证

1. 定向测试：forecast、forecast report、patterns、tcurve。
2. `python -X utf8 scripts\forecast_report.py --route 上海-大阪`。
3. `python -X utf8 scripts\tcurve_report.py --route 上海-大阪`。
4. 双收集器全量测试。
5. before/after snapshot diff。
6. economy 冻结邮件回归。
7. 复核台账与三库哈希不变。
