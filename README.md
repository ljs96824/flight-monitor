# Flight Monitor

定时监控 `config.yaml` 中配置的航班价格，使用 SerpAPI Google Flights 获取数据，并通过 PushPlus 推送通知。

## 本地运行

1. 安装依赖：

```bash
pip install -r requirements.txt
```

2. 配置 `.env`：

```env
SERPAPI_KEY=你的SerpAPI key
PUSHPLUS_TOKEN=你的PushPlus token
```

3. 手动运行：

```bash
python main.py
```

查看本地状态：

```bash
python check.py
```

## GitHub Actions 部署

1. 把 `flight_monitor` 项目推送到 GitHub 仓库。

2. 在仓库中进入 `Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`，新增：

```text
SERPAPI_KEY
PUSHPLUS_TOKEN
```

3. 确认 workflow 文件存在：

```text
.github/workflows/monitor.yml
```

4. GitHub Actions 会每天按北京时间运行三次：

```text
09:00
15:00
21:00
```

也可以在 GitHub 仓库的 `Actions` 页面手动点击 `Run workflow` 立即执行一次。

## 注意

GitHub Actions 的运行环境是临时的，本地生成的 `data/prices.db`、`data/last_signals.json`、`data/analysis_log.jsonl` 不会自动跨运行持久保存。当前部署方式适合定时采集和推送；如果需要长期历史分析，需要后续接入外部数据库或把数据作为 artifact/commit 回仓库。
