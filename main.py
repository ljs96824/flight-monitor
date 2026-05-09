import json
import logging
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# 加载环境变量
load_dotenv(BASE_DIR / ".env", encoding="utf-8")

import yaml

from analyzer import analyze_combined
from collector import collect_and_classify, save_raw_response
from notifier import format_message, health_report, send, should_notify
from storage import DB_PATH, init_db, save_snapshots


# 日志配置
LOG_PATH = DATA_DIR / "monitor.log"
logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)

# 信号记录文件路径
SIGNALS_PATH = DATA_DIR / "last_signals.json"
ANALYSIS_LOG = DATA_DIR / "analysis_log.jsonl"


def load_last_signals():
    if SIGNALS_PATH.exists():
        return json.loads(SIGNALS_PATH.read_text(encoding="utf-8"))
    return {}


def save_last_signals(signals):
    SIGNALS_PATH.write_text(
        json.dumps(signals, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run():
    # 初始化
    init_db()
    config = yaml.safe_load(
        (BASE_DIR / "config.yaml").read_text(encoding="utf-8")
    )
    last_signals = load_last_signals()
    results = []

    for sub in config["subscriptions"]:
        route = f"{sub['origin']}-{sub['destination']}"
        logging.info(f"开始处理 {route}")

        try:
            # 采集
            data = collect_and_classify(
                sub["origin"],
                sub["destination"],
                sub["depart_date"],
                sub["target_combo"],
            )

            if data is None:
                logging.error(f"{route} 采集返回空")
                results.append({"route": route, "status": "collect_failed"})
                continue

            # 存储
            records = []
            if data["target"]:
                records.append(
                    {
                        **data["target"],
                        "is_target": 1,
                        "route": route,
                        "depart_date": sub["depart_date"],
                    }
                )
            for alt in data["alternatives"]:
                records.append(
                    {
                        **alt,
                        "is_target": 0,
                        "route": route,
                        "depart_date": sub["depart_date"],
                    }
                )

            if records:
                save_snapshots(records)
                logging.info(f"{route} 存储{len(records)}条")

            # 保存原始响应
            save_raw_response(route, sub["depart_date"], data)

            # 分析
            analysis = analyze_combined(
                str(DB_PATH),
                route,
                sub["depart_date"],
                sub["target_combo"],
                data.get("price_insights", {}),
            )

            # 记录分析日志
            log_entry = {**analysis, "logged_at": datetime.now().isoformat()}
            with ANALYSIS_LOG.open("a", encoding="utf-8") as file:
                file.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

            # 推送判断
            prev_signal = last_signals.get(route, "none")
            notify, reason = should_notify(analysis, prev_signal)

            if notify:
                msg = format_message(analysis, reason)
                send(msg)
                logging.info(f"{route} 推送通知: {reason}")

            # 更新信号
            last_signals[route] = analysis.get("signal", "unknown")
            results.append(
                {
                    "route": route,
                    "status": "ok",
                    "price": analysis.get("current_price"),
                    "current_price": analysis.get("current_price"),
                    "signal": analysis.get("signal"),
                }
            )

        except Exception as e:
            logging.error(f"{route} 处理失败: {e}", exc_info=True)
            results.append({"route": route, "status": f"error: {e}"})

    # 保存信号记录
    save_last_signals(last_signals)

    # 发送健康报告
    try:
        health_report(results)
    except Exception as e:
        logging.error(f"健康报告发送失败: {e}")

    logging.info("本轮执行完成")


if __name__ == "__main__":
    run()
