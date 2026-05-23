import json
import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env", encoding="utf-8")

from serpapi import GoogleSearch


key = os.environ.get("SERPAPI_KEY", "")
print(f"SERPAPI_KEY: {key[:12]}..." if len(key) > 12 else f"SERPAPI_KEY: {key}")

if not key or "填" in key or "your" in key.lower():
    print("key还是占位符，请去 serpapi.com 注册拿真实key")
    exit(1)

params = {
    "engine": "google_flights",
    "departure_id": "PVG",
    "arrival_id": "MCO",
    "outbound_date": "2026-06-20",
    "type": "2",
    "currency": "CNY",
    "hl": "zh-CN",
    "api_key": key,
}

safe_params = {**params, "api_key": "***"}
print(f"\n请求参数: {json.dumps(safe_params, indent=2)}")
print("正在调用SerpAPI...\n")

search = GoogleSearch(params)
results = search.get_dict()

# 保存完整响应到文件方便查看
with (DATA_DIR / "debug_response.json").open("w", encoding="utf-8") as file:
    json.dump(results, file, ensure_ascii=False, indent=2)
print("完整响应已保存到 data/debug_response.json")

# 检查是否有错误
if "error" in results:
    print(f"API错误: {results['error']}")
    exit(1)

# 检查返回了哪些字段
print(f"\n返回的顶层字段: {list(results.keys())}")
print(f"best_flights数量: {len(results.get('best_flights', []))}")
print(f"other_flights数量: {len(results.get('other_flights', []))}")
print(f"price_insights: {results.get('price_insights', '无')}")

# 如果有航班，打印第一个的结构
for category in ["best_flights", "other_flights"]:
    flights = results.get(category, [])
    if flights:
        print(f"\n{category}[0] 的结构:")
        print(json.dumps(flights[0], ensure_ascii=False, indent=2)[:2000])
        break
