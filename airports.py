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
}


def get_airport_name(iata_code):
    """Return the Chinese airport name, falling back to the original IATA code."""
    code = str(iata_code or "").strip().upper()
    if not code:
        return ""
    return AIRPORT_NAMES.get(code, code)


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
