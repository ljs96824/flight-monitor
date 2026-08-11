# P6 Forecast Evidence Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在只读观测面板上实现可解释的分解预测、累计走前回测、航班规律报告与严格技能门通知小节。

**Architecture:** 预测层复用 `tcurve.py` 的城市级 global_min 日格和 degraded 判定，规律层复用 `provenance.py` 的只读观测查询。独立报告总是可见，通知仅在技能门、level 可靠性和 T 覆盖三重条件均通过时接入 payload。

**Tech Stack:** Python 3.13、标准库 `sqlite3/statistics/datetime/hashlib`、现有 Flask/通知渲染、unittest/pytest。

---

### Task 1: 冻结基线与只读不变量

**Files:**
- Read: `data/api_usage.json`
- Read: `data/observations.sqlite3`
- Generate: `before_forecast.json`
- Modify: `.gitignore`

- [ ] **Step 1: 记录台账和面板哈希**

Run:

```powershell
Get-FileHash data\api_usage.json -Algorithm SHA256
Get-FileHash data\observations.sqlite3 -Algorithm SHA256
```

Expected: 两个哈希均有值，记录到执行日志；不得修改文件。

- [ ] **Step 2: 生成业务基线快照**

Run:

```powershell
python -X utf8 scripts\snapshot_run.py --output before_forecast.json
```

Expected: 正常退出、`skipped_items=0`。

- [ ] **Step 3: 忽略回测输出**

在 `.gitignore` 增加：

```gitignore
data/forecast_backtest.json
```

- [ ] **Step 4: 提交基线配置**

```powershell
git add .gitignore
git commit -m "chore: ignore forecast backtest artifact"
```

### Task 2: 机场国别与静态假日事实层

**Files:**
- Create: `holidays.py`
- Modify: `airports.py`
- Modify: `method_registry.py`
- Test: `test_holidays.py`
- Test: `test_airports.py`
- Test: `test_provenance.py`

- [ ] **Step 1: 写机场 country 完备性失败测试**

```python
def test_all_airports_have_country():
    from airports import AIRPORTS, EXPECTED_AIRPORT_CODES, validate_airports
    assert set(AIRPORTS) == set(EXPECTED_AIRPORT_CODES)
    assert all(str(item.get("country") or "").strip() for item in AIRPORTS.values())
    validate_airports()
```

- [ ] **Step 2: 写假日标签与冻结摘要失败测试**

```python
def test_holiday_labels_include_country_name_and_relative_day():
    from datetime import date
    from holidays import holiday_labels_for_route
    labels = holiday_labels_for_route("PVG", "KIX", date(2026, 10, 1), shoulder_days=1)
    assert "中国·国庆(当天)" in labels

def test_holiday_registry_digest_is_frozen():
    from holidays import HOLIDAY_DATA_DIGEST, EXPECTED_HOLIDAY_DATA_DIGEST
    assert HOLIDAY_DATA_DIGEST == EXPECTED_HOLIDAY_DATA_DIGEST
```

- [ ] **Step 3: 运行 RED**

```powershell
python -X utf8 -m pytest test_holidays.py test_airports.py -q
```

Expected: 缺少 `country`、`holidays.py` 或 registry key 而失败。

- [ ] **Step 4: 实现 54 机场 country 单一真值**

为每个 `AIRPORTS` 条目增加非空 `country`；新增：

```python
def get_airport_country(code: str | None) -> str:
    return str((AIRPORTS.get(str(code or "").upper()) or {}).get("country") or "")
```

`validate_airports()` 对 `name/short/city/city_en/tz/country` 逐项必填校验。

- [ ] **Step 5: 实现 holidays.py**

公开接口固定为：

```python
HOLIDAY_SHOULDER_DAYS = positive_int_env("HOLIDAY_SHOULDER_DAYS", 1)

def holiday_labels_for_country(country: str, target: date, *, shoulder_days: int = HOLIDAY_SHOULDER_DAYS) -> list[dict]: ...
def holiday_labels_for_route(origin_iata: str, dest_iata: str, target: date, *, shoulder_days: int = HOLIDAY_SHOULDER_DAYS) -> list[str]: ...
```

每个静态条目包含 `country/name/start/end/note/source_title/source_url`。规范化 JSON 计算 SHA256，并与 `EXPECTED_HOLIDAY_DATA_DIGEST` 比较。

- [ ] **Step 6: 注册版本并冻结键集合**

在 `method_registry.py` 增加：

```python
"holiday_calendar": "holiday_calendar_v1",
"forecast": "forecast_v1",
"patterns": "patterns_v1",
```

同步 `_STAT_FAMILY_METHODS` 与注册表契约测试。

- [ ] **Step 7: 运行 GREEN**

```powershell
python -X utf8 -m pytest test_holidays.py test_airports.py test_provenance.py -q
```

Expected: 全绿。

- [ ] **Step 8: 提交**

```powershell
git add holidays.py airports.py method_registry.py test_holidays.py test_airports.py test_provenance.py
git commit -m "feat: add versioned holiday facts"
```

### Task 3: 分解模型 shape 与 level

**Files:**
- Create: `forecast.py`
- Modify: `tcurve.py`
- Test: `test_forecast.py`

- [ ] **Step 1: 写 shape 精确造例失败测试**

```python
def test_shape_normalizes_each_departure_trajectory_before_pooling():
    cells = fixture_cells({
        "2026-10-01": [(10, 100), (9, 120), (8, 140)],
        "2026-10-08": [(10, 200), (9, 240), (8, 280)],
    })
    shape = build_shape(cells)
    assert shape[9]["median"] == 1
    assert shape[10]["median"] == pytest.approx(5 / 6)
    assert shape[8]["median"] == pytest.approx(7 / 6)
```

- [ ] **Step 2: 写 level、区间与禁外推失败测试**

```python
def test_level_and_prediction_require_exact_shape_t():
    level = estimate_level(observed_cells, shape, min_obs=4)
    assert level["reliable"] is True
    prediction = predict_price(level, shape, target_t=8)
    assert prediction["median"] == pytest.approx(level["value"] * shape[8]["median"])
    assert predict_price(level, shape, target_t=7)["status"] == "无可用shape"
```

- [ ] **Step 3: 运行 RED**

```powershell
python -X utf8 -m pytest test_forecast.py -q
```

- [ ] **Step 4: 在 tcurve.py 暴露只读日格适配器**

新增公开函数：

```python
def load_tcurve_daily_cells(db_path=DEFAULT_DB_PATH, *, route, airport_pair=None, timeout=3.0) -> list[dict]:
    rows = _load_route_rows(...)
    return fold_tcurve_daily_cells(rows)
```

不改变 `build_tcurve()` 现有输出或算法。

- [ ] **Step 5: 实现 forecast.py 核心函数**

固定接口：

```python
MIN_OBS_FOR_LEVEL = positive_int_env("MIN_OBS_FOR_LEVEL", 4)

def build_shape(cells: list[dict], *, cutoff_day: str | None = None) -> dict[int, dict]: ...
def estimate_level(cells: list[dict], shape: dict[int, dict], *, depart_date: str, min_obs: int = MIN_OBS_FOR_LEVEL, cutoff_day: str | None = None) -> dict: ...
def predict_price(level: dict, shape: dict[int, dict], *, target_t: int) -> dict: ...
```

所有分位数复用 `tcurve.percentile_linear`，输出统一 `_clean_number`。

- [ ] **Step 6: 加入假日解释但不修正 level**

`estimate_level` 只附加 `holiday_labels` 与 explanation，任何数值计算不得读取假日字段。

- [ ] **Step 7: 运行 GREEN 并回归 tcurve**

```powershell
python -X utf8 -m pytest test_forecast.py test_tcurve.py -q
```

- [ ] **Step 8: 提交**

```powershell
git add forecast.py tcurve.py test_forecast.py
git commit -m "feat: add decomposed route price model"
```

### Task 4: 累计走前回测与技能门

**Files:**
- Modify: `forecast.py`
- Test: `test_forecast.py`

- [ ] **Step 1: 写泄漏红线失败测试**

测试通过注入 `fit_observed_days` 断言每个案例均满足：

```python
assert max(case["fit_observed_days"]) <= case["cutoff_day"]
assert case["target_day"] > case["cutoff_day"]
```

并构造一个错误 fitter 读取 D+1，断言 `assert_no_walk_forward_leakage()` 抛出 `AssertionError`。

- [ ] **Step 2: 写双基线与技能门边界失败测试**

```python
def test_skill_gate_requires_five_cases_and_ten_percent_improvement():
    assert evaluate_skill_gate(model_mape=9, naive_mape=10, case_n=4)["passed"] is False
    assert evaluate_skill_gate(model_mape=9, naive_mape=10, case_n=5)["passed"] is True
    assert evaluate_skill_gate(model_mape=9.1, naive_mape=10, case_n=5)["passed"] is False
```

- [ ] **Step 3: 运行 RED**

```powershell
python -X utf8 -m pytest test_forecast.py -q
```

- [ ] **Step 4: 实现累计回测**

```python
MIN_BACKTEST_CASES = positive_int_env("MIN_BACKTEST_CASES", 5)
SKILL_GATE_IMPROVEMENT = positive_float_env("SKILL_GATE_IMPROVEMENT", 0.10)

def walk_forward_backtest(cells, *, horizons=(1, 3, 7)) -> dict: ...
def evaluate_skill_gate(*, model_mape, naive_mape, case_n, min_cases=MIN_BACKTEST_CASES, improvement=SKILL_GATE_IMPROVEMENT) -> dict: ...
```

每个 horizon 只在模型、朴素和静态 T 曲线三者均有结果时纳入公平比较。案例集合从最早观测日至最新观测日累计，不做窗口裁剪。

- [ ] **Step 5: 实现回测工件写出函数**

```python
def write_backtest_report(report: dict, path: str | Path) -> None:
    atomic_json_write(path, report)
```

生产通知不调用写出函数；仅独立报告脚本按需写 `data/forecast_backtest.json`。

- [ ] **Step 6: 运行 GREEN**

```powershell
python -X utf8 -m pytest test_forecast.py -q
```

- [ ] **Step 7: 提交**

```powershell
git add forecast.py test_forecast.py
git commit -m "feat: add walk-forward forecast backtest"
```

### Task 5: 航班规律 patterns_v1

**Files:**
- Create: `patterns.py`
- Test: `test_patterns.py`

- [ ] **Step 1: 写四指标失败测试**

覆盖：组合出现率 80%/20% 边界、标签与 `%·n` 并排、市场承运航司中位与最低日占比、星期分布按 depart_date 去重、直飞/中转比。

- [ ] **Step 2: 写字段缺口失败测试**

```python
def test_departure_period_explains_obs_store_v1_gap():
    result = build_patterns(rows, min_n=5)
    assert result["departure_period"]["status"] == "字段不可得"
    assert "面板未存起飞时刻(obs_store v1)" in result["departure_period"]["reason"]
    assert "待schema扩展后自动点亮" in result["departure_period"]["reason"]
```

- [ ] **Step 3: 运行 RED**

```powershell
python -X utf8 -m pytest test_patterns.py -q
```

- [ ] **Step 4: 实现 patterns.py**

```python
MIN_PATTERN_N = positive_int_env("MIN_PATTERN_N", 5)
REGULAR_RATE = 0.80
OCCASIONAL_RATE = 0.20

def build_patterns(rows: list[dict], *, min_n: int = MIN_PATTERN_N) -> dict: ...
def build_route_patterns(db_path, *, route, airport_pair=None, min_n=MIN_PATTERN_N) -> dict: ...
```

读数使用 `provenance.load_route_observations`。`stops` 缺失时才按规范化 combo 的 `+` 段数推断，并记录 `basis="基于组合结构"`。

- [ ] **Step 5: 运行 GREEN**

```powershell
python -X utf8 -m pytest test_patterns.py -q
```

- [ ] **Step 6: 提交**

```powershell
git add patterns.py test_patterns.py
git commit -m "feat: describe route flight patterns"
```

### Task 6: 只读 forecast_report

**Files:**
- Create: `scripts/forecast_report.py`
- Test: `test_forecast_report.py`

- [ ] **Step 1: 写报告失败测试**

断言报告包含 shape、level、k=1/3/7 成绩单、技能门、未来7天区间、规律摘要、假日标签；无数据时包含明确原因且不出现空表头。

- [ ] **Step 2: 运行 RED**

```powershell
python -X utf8 -m pytest test_forecast_report.py -q
```

- [ ] **Step 3: 实现报告脚本**

公开接口：

```python
def generate_report(*, db_path=DEFAULT_DB_PATH, route: str, airport_pair=None, as_of_day=None) -> tuple[str, dict]: ...
```

CLI：

```powershell
python -X utf8 scripts\forecast_report.py --route 上海-大阪
```

脚本只 import `forecast/patterns/holidays/tcurve/airports`，禁止 import `main/notifier/aggregator`。

- [ ] **Step 4: 只读字节断言**

测试在运行脚本前后比较临时 SQLite SHA256，必须相同。

- [ ] **Step 5: 运行 GREEN**

```powershell
python -X utf8 -m pytest test_forecast_report.py -q
```

- [ ] **Step 6: 提交**

```powershell
git add scripts/forecast_report.py test_forecast_report.py
git commit -m "feat: add offline forecast report"
```

### Task 7: 依据信封与通知三重闸门

**Files:**
- Modify: `provenance.py`
- Modify: `main.py`
- Modify: `notifier.py`
- Test: `test_forecast_notification.py`
- Test: `test_provenance.py`

- [ ] **Step 1: 写三重闸门失败测试**

分别构造：技能门失败、level不可靠、T缺失、三者全通过。前三种断言 payload 无 `forecast` 且邮件字节不变；最后一种断言小节出现。

- [ ] **Step 2: 写市场层信封失败测试**

```python
assert envelope["method_version"] == "forecast_v1"
assert "市场最低参考价·单人单程·与用户筛选无关" in envelope["bucket"]
assert "约束=" not in envelope["bucket"]
assert envelope["backtest"]["horizon"] == 3
```

- [ ] **Step 3: 运行 RED**

```powershell
python -X utf8 -m pytest test_forecast_notification.py test_provenance.py -q
```

- [ ] **Step 4: 实现通知适配器**

在 `forecast.py` 增加：

```python
def build_notification_forecast(route_info: dict, *, db_path=DEFAULT_DB_PATH, as_of_day=None) -> dict:
    # 返回 eligible/reason/report；不直接修改 payload。
```

`main.py` 仅当 `eligible is True` 时设置 `route_info["forecast"]`；否则调用 `safe_log`，不添加空字段。

- [ ] **Step 5: 实现邮件/详情小节**

`notifier.py` 增加 `_email_forecast_body(payload)`，只消费已过门 payload。内容必须含：未来7天中位与双区间、k=3回测、非承诺说明、市场层声明及当前筛选方案价与市场参考下限（两者均有时）。禁止指令词。

- [ ] **Step 6: 扩展 P5 信封收集**

`provenance.attach_payload_provenance` 收集 forecast 点信封；详情“数据依据”节展示 forecast_v1。门未过 payload 无 forecast，因此无依据缺失日志。

- [ ] **Step 7: 运行 GREEN**

```powershell
python -X utf8 -m pytest test_forecast_notification.py test_provenance.py -q
```

- [ ] **Step 8: 提交**

```powershell
git add forecast.py provenance.py main.py notifier.py test_forecast_notification.py test_provenance.py
git commit -m "feat: gate experimental forecast evidence"
```

### Task 8: 待办登记与快照回归

**Files:**
- Modify: `docs/superpowers/specs/2026-08-11-forecast-layer-design.md`
- Generate: `after_forecast.json`

- [ ] **Step 1: 确认 obs_store v2 待办完整**

设计文档必须保留：nullable `departure_time`、版本升级、自升级日起积累、达到 n-gate 自动点亮；不得出现 P6 已实现该字段的表述。

- [ ] **Step 2: 生成 after 快照**

```powershell
python -X utf8 scripts\snapshot_run.py --output after_forecast.json
```

- [ ] **Step 3: 对比 before/after**

门未过 fixture 允许差异仅为 `computed_at`；若 fixture 过门，只允许新增 forecast 小节、forecast provenance 和 versions。金额、排序、判定字段必须逐项相同。

### Task 9: 全量验证、真实只读报告与发布

**Files:**
- Read: `data/api_usage.json`
- Read: `data/observations.sqlite3`

- [ ] **Step 1: 跑专项测试**

```powershell
python -X utf8 -m pytest test_forecast.py test_patterns.py test_holidays.py test_forecast_report.py test_forecast_notification.py -q
```

- [ ] **Step 2: 跑双收集器**

```powershell
C:\ProgramData\anaconda3\python.exe -X utf8 -m pytest -q
C:\Users\admin\AppData\Local\Programs\Python\Python313\python.exe -X utf8 -m unittest discover
```

- [ ] **Step 3: 跑真实只读报告**

```powershell
python -X utf8 scripts\forecast_report.py --route 上海-大阪
```

Expected: shape、level、三档成绩单、技能门和规律全文；技能门通过或未过均可，但必须如实。

- [ ] **Step 4: 核对零副作用**

重新计算台账和面板 SHA256，必须与 Task 1 完全一致。`git diff --check` 必须退出0。

- [ ] **Step 5: 上海—大阪离线复放**

门未过时 payload/HTML 除 `computed_at` 外逐字节一致；所有金额与口径校验保持原值。

- [ ] **Step 6: 推送 feature commits 并核验 CI**

```powershell
git push origin main
```

GitHub Actions Ubuntu/Windows 的 pytest 与 unittest 四格必须全部 success；失败则读取公开日志、修复并如实记录迭代。

