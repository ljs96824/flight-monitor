"""Airport display name helpers."""

from __future__ import annotations

import re


EXPECTED_AIRPORT_CODES = frozenset(
    """
    ABQ AMS ATL BKK CAN CDG CTU DCA DFW DMK DOH DWC DXB EWR FLL FRA
    GMP HGH HKG HND IAD ICN ITM JFK KIX LAX LGA LGW LHR MCO MDW MIA
    NKG NRT OAK ORD ORY PEK PKX PVG SEA SFB SFO SHA SIN SJC STN SZX
    TFU TPE TSA YTZ YVR YYZ
    """.split()
)


AIRPORTS = {
    "PVG": {
        "name": "上海浦东",
        "short": "浦东",
        "city": "上海",
        "city_en": "Shanghai",
        "tz": "北京",
    },
    "SHA": {
        "name": "上海虹桥",
        "short": "虹桥",
        "city": "上海",
        "city_en": "Shanghai",
        "tz": "北京",
    },
    "PEK": {
        "name": "北京首都",
        "short": "首都",
        "city": "北京",
        "city_en": "Beijing",
        "tz": "北京",
    },
    "PKX": {
        "name": "北京大兴",
        "short": "大兴",
        "city": "北京",
        "city_en": "Beijing",
        "tz": "北京",
    },
    "NRT": {
        "name": "东京成田",
        "short": "成田",
        "city": "东京",
        "city_en": "Tokyo",
        "tz": "日本",
    },
    "HND": {
        "name": "东京羽田",
        "short": "羽田",
        "city": "东京",
        "city_en": "Tokyo",
        "tz": "日本",
    },
    "KIX": {
        "name": "关西国际机场",
        "short": "关西",
        "city": "大阪",
        "city_en": "Osaka",
        "tz": "日本",
    },
    "ITM": {
        "name": "大阪伊丹",
        "short": "伊丹",
        "city": "大阪",
        "city_en": "Osaka",
        "tz": "日本",
    },
    "ICN": {
        "name": "首尔仁川",
        "short": "仁川",
        "city": "首尔",
        "city_en": "Seoul",
        "tz": "韩国",
    },
    "GMP": {
        "name": "首尔金浦",
        "short": "金浦",
        "city": "首尔",
        "city_en": "Seoul",
        "tz": "韩国",
    },
    "JFK": {
        "name": "纽约肯尼迪",
        "short": "肯尼迪",
        "city": "纽约",
        "city_en": "New York",
        "tz": "美东",
    },
    "EWR": {
        "name": "纽约纽瓦克",
        "short": "纽瓦克",
        "city": "纽约",
        "city_en": "New York",
        "tz": "美东",
    },
    "LGA": {
        "name": "纽约拉瓜迪亚",
        "short": "拉瓜迪亚",
        "city": "纽约",
        "city_en": "New York",
        "tz": "美东",
    },
    "LAX": {
        "name": "洛杉矶",
        "short": "洛杉矶",
        "city": "洛杉矶",
        "city_en": "Los Angeles",
        "tz": "美西",
    },
    "SFO": {
        "name": "旧金山",
        "short": "旧金山",
        "city": "旧金山",
        "city_en": "San Francisco",
        "tz": "美西",
    },
    "OAK": {
        "name": "奥克兰",
        "short": "奥克兰",
        "city": "旧金山",
        "city_en": "San Francisco",
        "tz": "美西",
    },
    "SJC": {
        "name": "圣何塞",
        "short": "圣何塞",
        "city": "旧金山",
        "city_en": "San Francisco",
        "tz": "美西",
    },
    "LHR": {
        "name": "伦敦希思罗",
        "short": "希思罗",
        "city": "伦敦",
        "city_en": "London",
        "tz": "伦敦",
    },
    "LGW": {
        "name": "伦敦盖特威克",
        "short": "盖特威克",
        "city": "伦敦",
        "city_en": "London",
        "tz": "伦敦",
    },
    "STN": {
        "name": "伦敦斯坦斯特德",
        "short": "斯坦斯特德",
        "city": "伦敦",
        "city_en": "London",
        "tz": "伦敦",
    },
    "CDG": {
        "name": "巴黎戴高乐",
        "short": "戴高乐",
        "city": "巴黎",
        "city_en": "Paris",
        "tz": "巴黎",
    },
    "ORY": {
        "name": "巴黎奥利",
        "short": "奥利",
        "city": "巴黎",
        "city_en": "Paris",
        "tz": "巴黎",
    },
    "HKG": {
        "name": "香港",
        "short": "香港",
        "city": "香港",
        "city_en": "Hong Kong",
        "tz": "香港",
    },
    "TPE": {
        "name": "台北桃园",
        "short": "桃园",
        "city": "台北",
        "city_en": "Taipei",
        "tz": "台北",
    },
    "TSA": {
        "name": "台北松山",
        "short": "松山",
        "city": "台北",
        "city_en": "Taipei",
        "tz": "台北",
    },
    "SIN": {
        "name": "新加坡樟宜",
        "short": "樟宜",
        "city": "新加坡",
        "city_en": "Singapore",
        "tz": "新加坡",
    },
    "BKK": {
        "name": "曼谷素万那普",
        "short": "素万那普",
        "city": "曼谷",
        "city_en": "Bangkok",
        "tz": "曼谷",
    },
    "DMK": {
        "name": "曼谷廊曼",
        "short": "廊曼",
        "city": "曼谷",
        "city_en": "Bangkok",
        "tz": "曼谷",
    },
    "CAN": {
        "name": "广州白云",
        "short": "白云",
        "city": "广州",
        "city_en": "Guangzhou",
        "tz": "北京",
    },
    "SZX": {
        "name": "深圳宝安",
        "short": "宝安",
        "city": "深圳",
        "city_en": "Shenzhen",
        "tz": "北京",
    },
    "CTU": {
        "name": "成都双流",
        "short": "双流",
        "city": "成都",
        "city_en": "Chengdu",
        "tz": "北京",
    },
    "TFU": {
        "name": "成都天府",
        "short": "天府",
        "city": "成都",
        "city_en": "Chengdu",
        "tz": "北京",
    },
    "HGH": {
        "name": "杭州萧山",
        "short": "萧山",
        "city": "杭州",
        "city_en": "Hangzhou",
        "tz": "北京",
    },
    "NKG": {
        "name": "南京禄口",
        "short": "禄口",
        "city": "南京",
        "city_en": "Nanjing",
        "tz": "北京",
    },
    "MCO": {
        "name": "奥兰多",
        "short": "奥兰多",
        "city": "奥兰多",
        "city_en": "Orlando",
        "tz": "美东",
    },
    "SFB": {
        "name": "奥兰多桑福德",
        "short": "桑福德",
        "city": "奥兰多",
        "city_en": "Orlando",
        "tz": "美东",
    },
    "ORD": {
        "name": "芝加哥奥黑尔",
        "short": "奥黑尔",
        "city": "芝加哥",
        "city_en": "Chicago",
        "tz": "美中",
    },
    "MDW": {
        "name": "芝加哥中途",
        "short": "中途",
        "city": "芝加哥",
        "city_en": "Chicago",
        "tz": "美中",
    },
    "IAD": {
        "name": "华盛顿杜勒斯",
        "short": "杜勒斯",
        "city": "华盛顿",
        "city_en": "Washington",
        "tz": "美东",
    },
    "DCA": {
        "name": "华盛顿里根",
        "short": "里根",
        "city": "华盛顿",
        "city_en": "Washington",
        "tz": "美东",
    },
    "MIA": {
        "name": "迈阿密",
        "short": "迈阿密",
        "city": "迈阿密",
        "city_en": "Miami",
        "tz": "美东",
    },
    "FLL": {
        "name": "劳德代尔堡",
        "short": "劳德代尔",
        "city": "迈阿密",
        "city_en": "Miami",
        "tz": "美东",
    },
    "SEA": {
        "name": "西雅图",
        "short": "西雅图",
        "city": "西雅图",
        "city_en": "Seattle",
        "tz": "美西",
    },
    "YYZ": {
        "name": "多伦多皮尔逊",
        "short": "皮尔逊",
        "city": "多伦多",
        "city_en": "Toronto",
        "tz": "美东",
    },
    "YTZ": {
        "name": "多伦多岛",
        "short": "多伦多岛",
        "city": "多伦多",
        "city_en": "Toronto",
        "tz": "美东",
    },
    "YVR": {
        "name": "温哥华",
        "short": "温哥华",
        "city": "温哥华",
        "city_en": "Vancouver",
        "tz": "美西",
    },
    "DXB": {
        "name": "迪拜",
        "short": "迪拜",
        "city": "迪拜",
        "city_en": "Dubai",
        "tz": "迪拜",
    },
    "DWC": {
        "name": "迪拜世界中心",
        "short": "迪拜世界中心",
        "city": "迪拜",
        "city_en": "Dubai",
        "tz": "迪拜",
    },
    "ABQ": {
        "name": "阿尔伯克基",
        "short": "阿尔伯克基",
        "city": "阿尔伯克基",
        "city_en": "Albuquerque",
        "tz": "美西",
    },
    "DFW": {
        "name": "达拉斯沃斯堡",
        "short": "达拉斯",
        "city": "达拉斯",
        "city_en": "Dallas",
        "tz": "美中",
    },
    "ATL": {
        "name": "亚特兰大",
        "short": "亚特兰大",
        "city": "亚特兰大",
        "city_en": "Atlanta",
        "tz": "美东",
    },
    "FRA": {
        "name": "法兰克福",
        "short": "法兰克福",
        "city": "法兰克福",
        "city_en": "Frankfurt",
        "tz": "法兰克福",
    },
    "AMS": {
        "name": "阿姆斯特丹",
        "short": "阿姆斯特丹",
        "city": "阿姆斯特丹",
        "city_en": "Amsterdam",
        "tz": "阿姆斯特丹",
    },
    "DOH": {
        "name": "多哈",
        "short": "多哈",
        "city": "多哈",
        "city_en": "Doha",
        "tz": "多哈",
    },
}


AIRPORT_NAMES = {code: item["name"] for code, item in AIRPORTS.items()}
AIRPORT_SHORT_NAMES = {code: item["short"] for code, item in AIRPORTS.items()}
AIRPORT_CITY = {code: item["city"] for code, item in AIRPORTS.items()}
AIRPORT_CITY_EN = {code: item["city_en"] for code, item in AIRPORTS.items()}
AIRPORT_TIMEZONE = {code: item["tz"] for code, item in AIRPORTS.items()}

CITY_AIRPORTS = {}
for code, item in AIRPORTS.items():
    CITY_AIRPORTS.setdefault(item["city"], []).append(code)

AIRPORT_TO_CITY = {}
for city, airport_codes in CITY_AIRPORTS.items():
    for code in airport_codes:
        AIRPORT_TO_CITY[code] = city


CITY_ALIASES = {
    "大版": "大阪",
    "东京都": "东京",
    "首尔市": "首尔",
    "首儿": "首尔",
    "新泻": "新潟",
}


def validate_airports():
    """Validate airport table completeness and derived mappings."""
    assert set(AIRPORTS) == EXPECTED_AIRPORT_CODES, (
        f"AIRPORTS code set changed: expected {len(EXPECTED_AIRPORT_CODES)}, "
        f"got {len(AIRPORTS)}"
    )

    required_fields = {"name", "short", "city", "city_en", "tz"}
    for code, item in AIRPORTS.items():
        assert re.fullmatch(r"[A-Z]{2,4}", code), f"Invalid IATA code: {code}"
        missing = required_fields - set(item)
        assert not missing, f"{code} missing fields: {sorted(missing)}"
        for field in required_fields:
            assert str(item.get(field) or "").strip(), f"{code}.{field} is empty"

    for city, airport_codes in CITY_AIRPORTS.items():
        assert airport_codes, f"{city} has empty airport list"
        for code in airport_codes:
            assert code in AIRPORTS, f"{city} references unknown airport {code}"

    for code in AIRPORTS:
        assert code in AIRPORT_TO_CITY, f"{code} missing AIRPORT_TO_CITY mapping"
        assert code in CITY_AIRPORTS[AIRPORT_TO_CITY[code]], (
            f"{code} AIRPORT_TO_CITY mismatch"
        )

    return True


def get_airport_name(iata_code):
    """Return the Chinese airport name, falling back to the original IATA code."""
    code = str(iata_code or "").strip().upper()
    if not code:
        return ""
    return AIRPORT_NAMES.get(code, code)


def get_airport_city(iata_code):
    """Return a generic Chinese city name for booking-site searches."""
    code = str(iata_code or "").strip().upper()
    if not code:
        return ""
    if code in AIRPORT_TO_CITY:
        return AIRPORT_TO_CITY[code]
    return AIRPORT_CITY.get(code, code)


def get_airport_city_en(iata_code):
    """Return a generic English city name for Google Flights searches."""
    code = str(iata_code or "").strip().upper()
    if not code:
        return ""
    return AIRPORT_CITY_EN.get(code, code)


def get_airport_short_name(iata_code):
    """Return a compact Chinese airport label for UI tags."""
    code = str(iata_code or "").strip().upper()
    if not code:
        return ""
    return AIRPORT_SHORT_NAMES.get(code, AIRPORT_NAMES.get(code, code))


def resolve_location(value):
    """Resolve a city name or airport code into a display name and airport list."""
    text = str(value or "").strip()
    if not text:
        return {"value": "", "type": "airport", "airports": []}
    resolved_text = CITY_ALIASES.get(text, text)
    if resolved_text != text:
        print(f"[地点纠错] {text} → {resolved_text}")
    upper = text.upper()
    if resolved_text in CITY_AIRPORTS:
        return {
            "value": resolved_text,
            "type": "city",
            "airports": CITY_AIRPORTS[resolved_text],
        }
    if 2 <= len(upper) <= 4 and upper.isascii() and upper.isalpha():
        return {"value": upper, "type": "airport", "airports": [upper]}
    return {"value": resolved_text, "type": "unknown", "airports": []}


def location_error_message(field, info):
    """Return a user-facing error for an unresolved origin/destination."""
    labels = {"origin": "出发地", "destination": "目的地"}
    label = labels.get(str(field or "").strip(), "地点")
    value = str((info or {}).get("value") or "").strip()
    if value:
        return f"无法识别{label} {value},请输入机场三字码或已支持的城市"
    return f"{label}不能为空,请输入机场三字码或已支持的城市"


def get_airport_timezone(iata_code):
    """Return a short user-facing timezone label for an airport."""
    code = str(iata_code or "").strip().upper()
    return AIRPORT_TIMEZONE.get(code, "当地")


def format_airport(iata_code):
    """Return display text as 中文名(IATA), or the raw code if unknown."""
    code = str(iata_code or "").strip().upper()
    if not code:
        return ""
    name = AIRPORT_NAMES.get(code)
    return f"{name}({code})" if name else code


if __name__ == "__main__":
    validate_airports()
    print("airports validation passed")
