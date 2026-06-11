"""Airport ground-transport estimates used for effective travel cost."""

from __future__ import annotations


AIRPORT_LOGISTICS = {
    "PVG": {
        "city": "上海",
        "to_center_min": 60,
        "taxi_cost": 160,
        "transit_cost": 8,
        "transit_min": 90,
        "note": "浦东离市区远",
    },
    "SHA": {
        "city": "上海",
        "to_center_min": 30,
        "taxi_cost": 60,
        "transit_cost": 5,
        "transit_min": 40,
        "note": "虹桥近市区",
    },
    "PEK": {
        "city": "北京",
        "to_center_min": 50,
        "taxi_cost": 110,
        "transit_cost": 25,
        "transit_min": 60,
        "note": "首都机场",
    },
    "PKX": {
        "city": "北京",
        "to_center_min": 70,
        "taxi_cost": 150,
        "transit_cost": 35,
        "transit_min": 80,
        "note": "大兴离市区远",
    },
    "CTU": {
        "city": "成都",
        "to_center_min": 35,
        "taxi_cost": 70,
        "transit_cost": 7,
        "transit_min": 45,
        "note": "双流离市区较近",
    },
    "TFU": {
        "city": "成都",
        "to_center_min": 70,
        "taxi_cost": 150,
        "transit_cost": 10,
        "transit_min": 75,
        "note": "天府离市区较远",
    },
    "CAN": {
        "city": "广州",
        "to_center_min": 45,
        "taxi_cost": 100,
        "transit_cost": 7,
        "transit_min": 55,
        "note": "白云机场",
    },
    "SZX": {
        "city": "深圳",
        "to_center_min": 45,
        "taxi_cost": 100,
        "transit_cost": 8,
        "transit_min": 55,
        "note": "宝安机场",
    },
    "HGH": {
        "city": "杭州",
        "to_center_min": 45,
        "taxi_cost": 110,
        "transit_cost": 7,
        "transit_min": 60,
        "note": "萧山机场",
    },
    "NKG": {
        "city": "南京",
        "to_center_min": 45,
        "taxi_cost": 110,
        "transit_cost": 7,
        "transit_min": 60,
        "note": "禄口机场",
    },
    "XIY": {
        "city": "西安",
        "to_center_min": 55,
        "taxi_cost": 120,
        "transit_cost": 16,
        "transit_min": 70,
        "note": "咸阳机场",
    },
    "CKG": {
        "city": "重庆",
        "to_center_min": 40,
        "taxi_cost": 80,
        "transit_cost": 6,
        "transit_min": 55,
        "note": "江北机场",
    },
    "WUH": {
        "city": "武汉",
        "to_center_min": 45,
        "taxi_cost": 90,
        "transit_cost": 7,
        "transit_min": 55,
        "note": "天河机场",
    },
}

MEGA_AIRPORTS = {"PVG", "PEK", "PKX", "CAN", "CTU", "TFU", "SZX"}
CITY_AIRPORTS = {"SHA"}
MEDIUM_AIRPORTS = set(AIRPORT_LOGISTICS) - MEGA_AIRPORTS - CITY_AIRPORTS


def _airport_buffer_defaults(code: str) -> dict:
    if code in MEGA_AIRPORTS:
        return {"size": "mega", "arrival_buffer_min": 120, "checkin_buffer_min": 110}
    if code in CITY_AIRPORTS:
        return {"size": "city", "arrival_buffer_min": 75, "checkin_buffer_min": 75}
    return {"size": "medium", "arrival_buffer_min": 90, "checkin_buffer_min": 90}


def get_airport_logistics(iata: str) -> dict:
    code = str(iata or "").strip().upper()
    default = {
        "city": "",
        "to_center_min": 45,
        "taxi_cost": 100,
        "transit_cost": 10,
        "transit_min": 60,
        "note": "机场交通待估",
    }
    result = {**default, **AIRPORT_LOGISTICS.get(code, {})}
    for key, value in _airport_buffer_defaults(code).items():
        result.setdefault(key, value)
    return result
