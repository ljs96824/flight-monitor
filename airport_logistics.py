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

ROUTE_TYPE_BUFFER_LABELS = {
    "domestic": "值机安检",
    "international": "值机+出境边检海关",
    "greater_china": "值机+出入境查验",
}



MEETING_IMPORTANCE_DEFAULTS = {
    "normal": {
        "label": "普通商务",
        "airport_advance_min": 90,
        "road_margin_ratio": 0.20,
        "road_margin_min": 15,
        "arrival_exit_min": 25,
        "delay_buffer_min": 30,
        "pre_meeting_buffer_min": 30,
        "post_meeting_buffer_min": 30,
        "checked_baggage_extra_min": 15,
    },
    "important": {
        "label": "重要会议",
        "airport_advance_min": 105,
        "road_margin_ratio": 0.30,
        "road_margin_min": 30,
        "arrival_exit_min": 35,
        "delay_buffer_min": 45,
        "pre_meeting_buffer_min": 60,
        "post_meeting_buffer_min": 30,
        "checked_baggage_extra_min": 20,
    },
    "critical": {
        "label": "不可迟到",
        "airport_advance_min": 120,
        "road_margin_ratio": 0.40,
        "road_margin_min": 35,
        "arrival_exit_min": 45,
        "delay_buffer_min": 90,
        "pre_meeting_buffer_min": 90,
        "post_meeting_buffer_min": 30,
        "checked_baggage_extra_min": 30,
    },
}


def get_meeting_importance_defaults(importance: str | None) -> dict:
    key = str(importance or "important").strip().lower()
    if key not in MEETING_IMPORTANCE_DEFAULTS:
        key = "important"
    return {"key": key, **MEETING_IMPORTANCE_DEFAULTS[key]}

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


def _airport_size(iata: str) -> str:
    return str(get_airport_logistics(iata).get("size") or "medium")


def _normalize_route_type(route_type: str | None) -> str:
    route = str(route_type or "domestic").strip().lower()
    if route not in {"domestic", "international", "greater_china"}:
        return "domestic"
    return route


def get_departure_buffer(iata: str, route_type: str | None) -> int:
    """Departure-side buffer: check-in + security (+ border/customs when needed)."""
    size = _airport_size(iata)
    route = _normalize_route_type(route_type)
    table = {
        "domestic": {"mega": 110, "medium": 90, "city": 75},
        "international": {"mega": 180, "medium": 150, "city": 150},
        "greater_china": {"mega": 150, "medium": 120, "city": 120},
    }
    return table[route].get(size, table[route]["medium"])


def get_arrival_buffer(iata: str, route_type: str | None) -> int:
    """Arrival-side buffer: deplaning + exit (+ immigration/baggage/customs when needed)."""
    size = _airport_size(iata)
    route = _normalize_route_type(route_type)
    table = {
        "domestic": {"mega": 120, "medium": 90, "city": 60},
        "international": {"mega": 170, "medium": 130, "city": 120},
        "greater_china": {"mega": 130, "medium": 100, "city": 90},
    }
    return table[route].get(size, table[route]["medium"])


def route_type_buffer_label(route_type: str | None, side: str = "departure") -> str:
    route = _normalize_route_type(route_type)
    if side == "arrival":
        if route == "international":
            return "入境边检+提行李+海关"
        if route == "greater_china":
            return "入境/入境查验+提行李"
        return "下机出站"
    return ROUTE_TYPE_BUFFER_LABELS[route]
