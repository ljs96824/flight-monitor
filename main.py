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

from analyzer import analyze_all_flights
from collector import _normalize_detail_flight, save_raw_response
from notifier import format_html_message, send
from sources.aggregator import FlightAggregator, build_default_sources
from storage import (
    get_lowest_price_history,
    get_previous_snapshot_prices,
    init_db,
    save_flight_details,
)


# 日志配置
LOG_PATH = DATA_DIR / "monitor.log"
logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)

ANALYSIS_LOG = DATA_DIR / "analysis_log.jsonl"


def run():
    # 初始化
    init_db()
    config = yaml.safe_load(
        (BASE_DIR / "config.yaml").read_text(encoding="utf-8")
    )

    for sub in config["subscriptions"]:
        route = f"{sub['origin']}-{sub['destination']}"
        logging.info(f"开始处理 {route}")

        try:
            agg = FlightAggregator(build_default_sources())
            data = agg.collect(
                sub["origin"],
                sub["destination"],
                sub["depart_date"],
            )

            if data is None or not data.get("flights"):
                logging.error(f"{route} 采集返回空")
                continue

            flights = [
                _normalize_detail_flight(
                    flight, flight.get("data_source") or flight.get("source")
                )
                for flight in data.get("flights", [])
            ]
            data["flights"] = flights
            data["total_count"] = len(flights)

            save_flight_details(route, sub["depart_date"], flights)
            previous_prices = get_previous_snapshot_prices(route, sub["depart_date"])
            lowest_price_history = get_lowest_price_history(
                route, sub["depart_date"], limit=14
            )
            save_raw_response(route, sub["depart_date"], data)
            logging.info(f"{route} 存储{data.get('total_count', 0)}个航班方案")

            analysis = analyze_all_flights(
                flights,
                data.get("price_insights"),
                mode=sub.get("mode", "balanced"),
                priorities=sub.get("priorities"),
            )

            log_entry = {**analysis, "logged_at": datetime.now().isoformat()}
            with ANALYSIS_LOG.open("a", encoding="utf-8") as file:
                file.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

            msg = format_html_message(
                analysis_result=analysis,
                route_info={
                    "origin": sub["origin"],
                    "destination": sub["destination"],
                    "depart_date": sub["depart_date"],
                    "mode": sub.get("mode", "balanced"),
                    "priorities": sub.get("priorities"),
                    "previous_prices": previous_prices,
                    "lowest_price_history": lowest_price_history,
                    "source_stats": data.get("source_stats", {}),
                },
                source_stats=data.get("source_stats"),
                price_insights=data.get("price_insights"),
            )
            send(msg)
            logging.info(f"{route} 已推送方案对比表")

        except Exception as e:
            logging.error(f"{route} 处理失败: {e}", exc_info=True)

    logging.info("本轮执行完成")


if __name__ == "__main__":
    run()
