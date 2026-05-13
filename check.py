"""Terminal comparison view for latest collected flight options."""

from __future__ import annotations

from pathlib import Path

from analyzer import analyze_all_flights, city_name
from storage import get_latest_flights, init_db


BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"


def _duration_text(minutes: int | float | None) -> str:
    if minutes is None:
        return "-"
    minutes = int(minutes)
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _load_config() -> dict:
    subscriptions = []
    current = {}
    for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            if current:
                subscriptions.append(current)
            current = {}
            stripped = stripped[2:].strip()
        if ":" not in stripped or stripped == "subscriptions:":
            continue
        key, value = stripped.split(":", 1)
        current[key.strip()] = value.strip().strip('"').strip("'")
    if current:
        subscriptions.append(current)
    return {"subscriptions": subscriptions}


def _print_recommendation(rec: dict) -> None:
    flight = rec["flight"]
    print(rec["tag"])
    print(f"  价格: ¥{flight['price']:,.0f}")
    print(f"  航班: {flight['flight_combo']}")
    print(f"  路线: {flight['route_summary']}")
    print(f"  时长: {flight['total_hours']}小时 | 中转: {flight['stops']}次")
    print(f"  理由: {rec['reason']}")
    print()


def _print_all_flights(flights: list[dict]) -> None:
    print("━━━ 最新方案列表 ━━━")
    print("序号  航班组合                         价格      路线                  总时长    中转")
    print("-" * 92)
    for index, flight in enumerate(flights[:10], 1):
        combo = flight.get("flight_combo", "-")
        price = flight.get("price", 0)
        route = flight.get("route_summary", "-")
        duration = _duration_text(flight.get("total_duration_min"))
        stops = flight.get("stops", 0)
        print(f"{index:<4}  {combo:<31} ¥{price:<7,.0f} {route:<21} {duration:<8} {stops}次")


def main() -> None:
    init_db()
    config = _load_config()
    subscriptions = config.get("subscriptions", [])
    if not subscriptions:
        print("暂无订阅配置")
        return

    has_data = False
    for sub in subscriptions:
        route = f"{sub['origin']}-{sub['destination']}"
        flights = get_latest_flights(route, sub["depart_date"])
        if not flights:
            continue

        has_data = True
        analysis = analyze_all_flights(flights)
        print("========================================")
        print(
            f"航线: {city_name(sub['origin'])} → {city_name(sub['destination'])}"
            f" | 出发日: {sub['depart_date']}"
        )
        print(f"共找到{analysis['total_options']}个方案")
        print("========================================")
        print()
        print("━━━ 推荐方案 ━━━")
        print()
        for rec in analysis["recommendations"]:
            _print_recommendation(rec)
        _print_all_flights(analysis["all_flights"])
        print("========================================")

    if not has_data:
        print("暂无数据，请先运行 python main.py 采集")


if __name__ == "__main__":
    main()
