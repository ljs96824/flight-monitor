from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")

import os

print("=== API Key 状态 ===")
print(f"SERPAPI: {'已配置' if os.environ.get('SERPAPI_KEY') else '未设置'}")
print(f"SEARCHAPI: {'已配置' if os.environ.get('SEARCHAPI_KEY') else '未设置'}")
print(f"DUFFEL: {'已配置' if os.environ.get('DUFFEL_TOKEN') else '未设置'}")

print("\n=== 逐个测试数据源 ===")

from sources.serpapi_source import SerpAPISource

try:
    s = SerpAPISource()
    result = s.fetch("PVG", "MCO", "2026-06-20")
    flights = result.get("flights", [])
    print(f"[SerpAPI] 成功，返回 {len(flights)} 个航班")
except Exception as e:
    print(f"[SerpAPI] 失败：{e}")

from sources.searchapi_source import SearchAPISource

try:
    s = SearchAPISource()
    result = s.fetch("PVG", "MCO", "2026-06-20")
    flights = result.get("flights", [])
    print(f"[SearchAPI] 成功，返回 {len(flights)} 个航班")
except Exception as e:
    print(f"[SearchAPI] 失败：{e}")

try:
    from sources.duffel_source import DuffelSource

    s = DuffelSource()
    result = s.fetch("PVG", "MCO", "2026-06-20")
    flights = result.get("flights", [])
    print(f"[Duffel] 成功，返回 {len(flights)} 个航班")
except Exception as e:
    print(f"[Duffel] 失败：{e}")
