"""Domestic China fare-rule defaults inferred from airline and cabin code.

Juhe domestic flight data does not return live baggage/refund details, so these
helpers provide conservative, clearly labeled standard-rule estimates.
"""

from __future__ import annotations

LCC_AIRLINES = {
    "9C": "春秋航空",
    "HO": "吉祥航空",
    "PN": "西部航空",
    "KN": "中国联合航空",
    "BK": "奥凯航空",
    "JD": "首都航空",
    "GS": "天津航空",
}

FULL_SERVICE = {
    "CA": "国航",
    "MU": "东航",
    "CZ": "南航",
    "HU": "海航",
    "MF": "厦航",
    "ZH": "深航",
    "SC": "山航",
    "3U": "川航",
    "FM": "上航",
    "GJ": "长龙",
    "EU": "成都航空",
}

FLEXIBLE_CABINS = {"Y", "B", "M", "U", "H", "W"}
MEDIUM_CABINS = {"K", "L", "N", "R", "S", "V", "Q"}

AIRCRAFT_NAMES = {
    "33L": "空客A330",
    "33J": "空客A330",
    "33E": "空客A330",
    "332": "空客A330-200",
    "333": "空客A330-300",
    "339": "空客A330-900",
    "33": "空客A330",
    "32L": "空客A321",
    "32Q": "空客A321neo",
    "32J": "空客A321",
    "321": "空客A321",
    "32A": "空客A320",
    "32N": "空客A320neo",
    "320": "空客A320",
    "73L": "波音737",
    "73J": "波音737",
    "73E": "波音737",
    "73U": "波音737",
    "737": "波音737",
    "738": "波音737-800",
    "789": "波音787-9",
    "788": "波音787-8",
    "787": "波音787",
    "773": "波音777-300",
    "772": "波音777-200",
    "77W": "波音777-300ER",
    "77L": "波音777-200LR",
    "778": "波音777-8",
    "779": "波音777-9",
    "763": "波音767-300",
    "764": "波音767-400",
    "762": "波音767-200",
    "752": "波音757-200",
    "753": "波音757-300",
    "359": "空客A350",
    "351": "空客A350",
    "343": "空客A340-300",
    "346": "空客A340-600",
    "388": "空客A380",
    "319": "空客A319",
    "318": "空客A318",
    "223": "空客A220-300",
    "221": "空客A220-100",
    "E90": "巴航E190",
    "E95": "巴航E195",
    "ER4": "巴航ERJ145",
    "CR9": "庞巴迪CRJ900",
    "CRK": "庞巴迪CRJ1000",
    "AT7": "ATR72",
    "AT5": "ATR42",
    "919": "国产C919",
    "909": "国产ARJ21",
}


def get_aircraft_name(code) -> str:
    text = str(code or "").strip()
    if not text:
        return ""
    if any(token in text for token in ("空客", "波音", "国产", "Airbus", "Boeing", "C919", "ARJ")):
        return text
    key = text.upper()
    if key in AIRCRAFT_NAMES:
        return AIRCRAFT_NAMES[key]
    if 2 <= len(key) <= 4 and re_match_aircraft_code(key):
        return f"机型代码{key}(以航司为准)"
    return text


def re_match_aircraft_code(value: str) -> bool:
    return all(ch.isalnum() for ch in value)


def airline_code_from_flight(flight: dict | None) -> str:
    flight = flight or {}
    for key in ("airline", "airline_code", "carrier"):
        value = str(flight.get(key) or "").strip().upper()
        if value:
            return value[:2] if value[0].isdigit() else value[:2]
    flight_no = str(
        flight.get("flight_no")
        or flight.get("flight_number")
        or flight.get("flight_combo")
        or ""
    ).strip().upper()
    return flight_no[:2] if flight_no else ""


def get_domestic_baggage(airline_code, cabin_code=None):
    code = str(airline_code or "").strip().upper()
    if code in FULL_SERVICE:
        return {
            "carry_on_kg": 5,
            "checked_kg": 20,
            "checked_pieces": 1,
            "included": True,
            "note": "经济舱通常含20kg托运",
            "level": "标准",
        }
    if code in LCC_AIRLINES:
        return {
            "carry_on_kg": 5,
            "checked_kg": 0,
            "checked_pieces": 0,
            "included": False,
            "note": "廉航，托运需另购（约¥50-150）",
            "level": "需加购",
        }
    return {
        "carry_on_kg": None,
        "checked_kg": None,
        "checked_pieces": None,
        "included": None,
        "note": "行李规则待确认，以支付页为准",
        "level": "待确认",
    }


def get_domestic_refund(cabin_code, ticket_price=None, full_fare=None):
    if not cabin_code:
        return {"level": "中", "label": "退改适中", "note": "退改规则以支付页为准"}
    cabin = str(cabin_code or "").strip().upper()[:1]
    if cabin in FLEXIBLE_CABINS:
        return {
            "level": "高",
            "label": "退改友好",
            "note": "高舱位，退改费较低，适合行程可能变化",
        }
    if cabin in MEDIUM_CABINS:
        return {
            "level": "中",
            "label": "退改适中",
            "note": "中等舱位，退改有一定费用",
        }
    return {
        "level": "低",
        "label": "退改严格",
        "note": "特价舱，退改费高或不可退，适合确定行程",
    }


def standardize_domestic_fare_rules(flight: dict | None) -> dict:
    flight = flight or {}
    airline_code = airline_code_from_flight(flight)
    cabin_code = (
        flight.get("cabin_code")
        or flight.get("booking_class")
        or flight.get("fare_basis")
        or flight.get("cabin")
    )
    baggage = get_domestic_baggage(airline_code, cabin_code)
    refund = get_domestic_refund(cabin_code, flight.get("price"), flight.get("full_fare"))
    return {
        "flight_combo": flight.get("flight_combo") or flight.get("flight_no"),
        "baggage": baggage,
        "refund": refund,
        "change": {
            "allowed": refund.get("level") in {"高", "中"},
            "fee": None,
            "conditions": refund.get("note"),
        },
        "cabin_class": flight.get("cabin_class") or "economy",
        "cabin_code": cabin_code,
        "source": "国内标准规则推断",
        "source_note": "国内标准规则推断，实付和具体条款以支付页为准",
    }
