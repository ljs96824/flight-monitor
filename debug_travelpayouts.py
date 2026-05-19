import os
from pathlib import Path

import httpx
from dotenv import load_dotenv


load_dotenv(Path(__file__).parent / ".env")
token = os.environ.get("TRAVELPAYOUTS_TOKEN", "")
print(f"Token: {token[:12]}..." if len(token) > 12 else f"Token: {token}")

if not token:
    print("未设置 TRAVELPAYOUTS_TOKEN")
    exit(1)

routes = [
    ("PVG", "MCO", "2026-06-20", "上海→奥兰多"),
    ("MOW", "BKK", "2026-06-20", "莫斯科→曼谷"),
    ("PVG", "NRT", "2026-06-20", "上海→东京"),
]

for origin, dest, date, name in routes:
    try:
        response = httpx.get(
            "https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
            params={
                "origin": origin,
                "destination": dest,
                "departure_at": date,
                "one_way": "true",
                "currency": "cny",
                "token": token,
            },
            timeout=15,
        )
        data = response.json().get("data", [])
        print(f"{name}: 状态{response.status_code}, {len(data)}条数据")
        if data:
            print(
                f"  最低价: {data[0].get('price')} CNY, "
                f"航司: {data[0].get('airline')}"
            )
    except Exception as exc:
        print(f"{name}: 失败 - {exc}")
