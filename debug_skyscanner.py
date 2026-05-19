import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv


load_dotenv(Path(__file__).parent / ".env")
key = os.environ.get("RAPIDAPI_KEY", "")
headers = {
    "X-RapidAPI-Key": key,
    "X-RapidAPI-Host": "sky-scrapper.p.rapidapi.com",
}
base = "https://sky-scrapper.p.rapidapi.com/api/v1"


def search_airport(queries):
    for query in queries:
        response = httpx.get(
            f"{base}/flights/searchAirport",
            headers=headers,
            params={"query": query, "locale": "en-US"},
            timeout=10,
        )
        data = response.json().get("data", [])
        print(f"query={query} 状态: {response.status_code}, {len(data)}条")
        if data:
            return response, data, query
    return None, [], None


print("=== 第1步：查PVG机场ID ===")
response_pvg, pvg_data, pvg_query = search_airport(["PVG", "Shanghai Pudong", "Shanghai"])
print(f"使用query: {pvg_query}")
if len(pvg_data) > 1:
    print("Shanghai Pudong 完整数据:")
    print(json.dumps(pvg_data[1], indent=2, ensure_ascii=False))
elif pvg_data:
    print("第一条完整数据:")
    print(json.dumps(pvg_data[0], indent=2, ensure_ascii=False))

pvg_entity = None
for item in pvg_data:
    print(json.dumps(item, indent=2, ensure_ascii=False)[:600])
    print("---")
    navigation = item.get("navigation", {})
    flight_params = navigation.get("relevantFlightParams", {})
    if flight_params.get("skyId") == "PVG":
        pvg_entity = flight_params.get("entityId")

print(f"PVG entityId: {pvg_entity}")

print("\n=== 第2步：查MCO机场ID ===")
response_mco, mco_data, mco_query = search_airport(["MCO", "Orlando International"])
print(f"使用query: {mco_query}")
mco_entity = None
for item in mco_data:
    navigation = item.get("navigation", {})
    flight_params = navigation.get("relevantFlightParams", {})
    print(
        f"  {item.get('presentation', {}).get('title', '')} | "
        f"skyId={flight_params.get('skyId', '')} "
        f"entityId={flight_params.get('entityId', '')} "
        f"type={navigation.get('entityType', '')}"
    )
    if flight_params.get("skyId") == "MCO":
        mco_entity = flight_params.get("entityId")

if not mco_entity:
    for item in mco_data:
        navigation = item.get("navigation", {})
        if navigation.get("entityType") == "AIRPORT":
            mco_entity = navigation.get("entityId")
            break

print(f"MCO entityId: {mco_entity}")

if not pvg_entity or not mco_entity:
    print("\n机场ID获取失败，无法继续")
    exit(1)

print(f"\n=== 第3步：搜索航班 PVG({pvg_entity}) → MCO({mco_entity}) ===")
params = {
    "originSkyId": "PVG",
    "destinationSkyId": "MCO",
    "originEntityId": pvg_entity,
    "destinationEntityId": mco_entity,
    "date": "2026-06-20",
    "cabinClass": "economy",
    "adults": "1",
    "currency": "CNY",
    "market": "CN",
    "countryCode": "CN",
}
print(f"请求参数: {json.dumps(params, indent=2)}")

response_flights = httpx.get(
    f"{base}/flights/searchFlights",
    headers=headers,
    params=params,
    timeout=20,
)
print(f"状态: {response_flights.status_code}")
print(f"响应前500字: {response_flights.text[:500]}")

flight_data = response_flights.json()
itineraries = flight_data.get("data", {}).get("itineraries", [])
print(f"航班数: {len(itineraries)}")

if itineraries:
    first = itineraries[0]
    print(f"第一个方案价格: {first.get('price', {}).get('raw', 'N/A')}")
