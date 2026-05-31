"""Airport display name helpers."""

AIRPORT_NAMES = {
    "PVG": "上海浦东",
    "SHA": "上海虹桥",
    "PEK": "北京首都",
    "PKX": "北京大兴",
    "CAN": "广州白云",
    "SZX": "深圳宝安",
    "CTU": "成都天府",
    "HGH": "杭州萧山",
    "NKG": "南京禄口",
    "KIX": "关西国际机场",
    "ITM": "大阪伊丹",
    "NRT": "东京成田",
    "HND": "东京羽田",
    "ICN": "首尔仁川",
    "TPE": "台北桃园",
    "HKG": "香港",
    "BKK": "曼谷素万那普",
    "SIN": "新加坡樟宜",
    "LAX": "洛杉矶",
    "JFK": "纽约肯尼迪",
    "SFO": "旧金山",
    "ORD": "芝加哥奥黑尔",
    "DFW": "达拉斯沃斯堡",
    "MCO": "奥兰多",
    "MIA": "迈阿密",
    "ATL": "亚特兰大",
    "SEA": "西雅图",
    "YVR": "温哥华",
    "YYZ": "多伦多皮尔逊",
    "LHR": "伦敦希思罗",
    "CDG": "巴黎戴高乐",
    "FRA": "法兰克福",
    "AMS": "阿姆斯特丹",
    "DXB": "迪拜",
    "DOH": "多哈",
    "ABQ": "阿尔伯克基",
    "GMP": "首尔金浦",
    "EWR": "纽约纽瓦克",
    "LGA": "纽约拉瓜迪亚",
    "OAK": "奥克兰",
    "SJC": "圣何塞",
    "LGW": "伦敦盖特威克",
    "STN": "伦敦斯坦斯特德",
    "ORY": "巴黎奥利",
    "TSA": "台北松山",
    "DMK": "曼谷廊曼",
    "TFU": "成都天府",
    "SFB": "奥兰多桑福德",
    "MDW": "芝加哥中途",
    "IAD": "华盛顿杜勒斯",
    "DCA": "华盛顿里根",
    "FLL": "劳德代尔堡",
    "YTZ": "多伦多岛",
    "DWC": "迪拜世界中心",
}


AIRPORT_TIMEZONE = {
    "PVG": "北京",
    "SHA": "北京",
    "PEK": "北京",
    "CAN": "北京",
    "NRT": "日本",
    "HND": "日本",
    "KIX": "日本",
    "ITM": "日本",
    "ICN": "韩国",
    "TPE": "台北",
    "LAX": "美西",
    "SFO": "美西",
    "SEA": "美西",
    "JFK": "美东",
    "MCO": "美东",
    "MIA": "美东",
    "ATL": "美东",
    "DFW": "美中",
    "ORD": "美中",
    "LHR": "伦敦",
    "CDG": "巴黎",
    "FRA": "法兰克福",
    "DXB": "迪拜",
    "DOH": "多哈",
    "SIN": "新加坡",
    "BKK": "曼谷",
    "HKG": "香港",
    "GMP": "韩国",
    "EWR": "美东",
    "LGA": "美东",
    "OAK": "美西",
    "SJC": "美西",
    "LGW": "伦敦",
    "STN": "伦敦",
    "ORY": "巴黎",
    "TSA": "台北",
    "DMK": "曼谷",
    "TFU": "北京",
    "SFB": "美东",
    "MDW": "美中",
    "IAD": "美东",
    "DCA": "美东",
    "FLL": "美东",
    "YTZ": "美东",
    "DWC": "迪拜",
}


CITY_AIRPORTS = {
    "上海": ["PVG", "SHA"],
    "北京": ["PEK", "PKX"],
    "东京": ["NRT", "HND"],
    "大阪": ["KIX", "ITM"],
    "首尔": ["ICN", "GMP"],
    "纽约": ["JFK", "EWR", "LGA"],
    "洛杉矶": ["LAX"],
    "旧金山": ["SFO", "OAK", "SJC"],
    "伦敦": ["LHR", "LGW", "STN"],
    "巴黎": ["CDG", "ORY"],
    "香港": ["HKG"],
    "台北": ["TPE", "TSA"],
    "新加坡": ["SIN"],
    "曼谷": ["BKK", "DMK"],
    "广州": ["CAN"],
    "深圳": ["SZX"],
    "成都": ["CTU", "TFU"],
    "杭州": ["HGH"],
    "南京": ["NKG"],
    "奥兰多": ["MCO", "SFB"],
    "芝加哥": ["ORD", "MDW"],
    "华盛顿": ["IAD", "DCA"],
    "迈阿密": ["MIA", "FLL"],
    "西雅图": ["SEA"],
    "多伦多": ["YYZ", "YTZ"],
    "温哥华": ["YVR"],
    "迪拜": ["DXB", "DWC"],
    "阿尔伯克基": ["ABQ"],
}


AIRPORT_TO_CITY = {}
for city, airports in CITY_AIRPORTS.items():
    for code in airports:
        AIRPORT_TO_CITY[code] = city


AIRPORT_CITY = {
    "PVG": "上海",
    "SHA": "上海",
    "PEK": "北京",
    "PKX": "北京",
    "CAN": "广州",
    "SZX": "深圳",
    "CTU": "成都",
    "HGH": "杭州",
    "NKG": "南京",
    "KIX": "大阪",
    "ITM": "大阪",
    "NRT": "东京",
    "HND": "东京",
    "ICN": "首尔",
    "TPE": "台北",
    "HKG": "香港",
    "BKK": "曼谷",
    "SIN": "新加坡",
    "LAX": "洛杉矶",
    "JFK": "纽约",
    "SFO": "旧金山",
    "ORD": "芝加哥",
    "DFW": "达拉斯",
    "MCO": "奥兰多",
    "MIA": "迈阿密",
    "ATL": "亚特兰大",
    "SEA": "西雅图",
    "YVR": "温哥华",
    "YYZ": "多伦多",
    "LHR": "伦敦",
    "CDG": "巴黎",
    "FRA": "法兰克福",
    "AMS": "阿姆斯特丹",
    "DXB": "迪拜",
    "DOH": "多哈",
    "ABQ": "阿尔伯克基",
}


AIRPORT_CITY_EN = {
    "PVG": "Shanghai",
    "SHA": "Shanghai",
    "PEK": "Beijing",
    "PKX": "Beijing",
    "CAN": "Guangzhou",
    "SZX": "Shenzhen",
    "CTU": "Chengdu",
    "HGH": "Hangzhou",
    "NKG": "Nanjing",
    "KIX": "Osaka",
    "ITM": "Osaka",
    "NRT": "Tokyo",
    "HND": "Tokyo",
    "ICN": "Seoul",
    "TPE": "Taipei",
    "HKG": "Hong Kong",
    "BKK": "Bangkok",
    "SIN": "Singapore",
    "LAX": "Los Angeles",
    "JFK": "New York",
    "SFO": "San Francisco",
    "ORD": "Chicago",
    "DFW": "Dallas",
    "MCO": "Orlando",
    "MIA": "Miami",
    "ATL": "Atlanta",
    "SEA": "Seattle",
    "YVR": "Vancouver",
    "YYZ": "Toronto",
    "LHR": "London",
    "CDG": "Paris",
    "FRA": "Frankfurt",
    "AMS": "Amsterdam",
    "DXB": "Dubai",
    "DOH": "Doha",
    "ABQ": "Albuquerque",
    "GMP": "Seoul",
    "EWR": "New York",
    "LGA": "New York",
    "OAK": "San Francisco",
    "SJC": "San Francisco",
    "LGW": "London",
    "STN": "London",
    "ORY": "Paris",
    "TSA": "Taipei",
    "DMK": "Bangkok",
    "TFU": "Chengdu",
    "SFB": "Orlando",
    "MDW": "Chicago",
    "IAD": "Washington",
    "DCA": "Washington",
    "FLL": "Miami",
    "YTZ": "Toronto",
    "DWC": "Dubai",
}

AIRPORT_SHORT_NAMES = {
    "PVG": "浦东",
    "SHA": "虹桥",
    "PEK": "首都",
    "PKX": "大兴",
    "CAN": "白云",
    "SZX": "宝安",
    "CTU": "天府",
    "TFU": "天府",
    "HGH": "萧山",
    "NKG": "禄口",
    "KIX": "关西",
    "ITM": "伊丹",
    "NRT": "成田",
    "HND": "羽田",
    "ICN": "仁川",
    "GMP": "金浦",
    "TPE": "桃园",
    "TSA": "松山",
    "HKG": "香港",
    "BKK": "素万那普",
    "DMK": "廊曼",
    "SIN": "樟宜",
    "MCO": "奥兰多",
    "SFB": "桑福德",
    "LAX": "洛杉矶",
    "JFK": "肯尼迪",
    "EWR": "纽瓦克",
    "LGA": "拉瓜迪亚",
    "SFO": "旧金山",
    "OAK": "奥克兰",
    "SJC": "圣何塞",
    "ORD": "奥黑尔",
    "MDW": "中途",
    "DFW": "达拉斯",
    "SEA": "西雅图",
    "MIA": "迈阿密",
    "FLL": "劳德代尔",
    "ATL": "亚特兰大",
    "YVR": "温哥华",
    "YYZ": "皮尔逊",
    "YTZ": "多伦多岛",
    "LHR": "希思罗",
    "LGW": "盖特威克",
    "STN": "斯坦斯特德",
    "CDG": "戴高乐",
    "ORY": "奥利",
    "FRA": "法兰克福",
    "AMS": "阿姆斯特丹",
    "DXB": "迪拜",
    "DWC": "迪拜世界中心",
    "DOH": "多哈",
    "ABQ": "阿尔伯克基",
}


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
    upper = text.upper()
    if text in CITY_AIRPORTS:
        return {"value": text, "type": "city", "airports": CITY_AIRPORTS[text]}
    if 2 <= len(upper) <= 4 and upper.isascii() and upper.isalpha():
        return {"value": upper, "type": "airport", "airports": [upper]}
    return {"value": upper, "type": "airport", "airports": [upper]}


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
