"""廉价航空名录与按执飞航司判定工具。"""

from __future__ import annotations

import re


EXPECTED_LCC_CARRIER_CODES = frozenset(
    """
    9C AQ PN 8L KN
    MM GK IJ ZG
    7C LJ BX RS TW
    UO IT
    TR AK D7 FD QZ Z2 VJ VZ 5J SL
    """.split()
)

LCC_POLICIES = frozenset({"any", "exclude_lcc", "lcc_only"})

LCC_CARRIERS = {
    "9C": {"name_cn": "春秋航空", "region": "中国大陆", "note": "低成本航空"},
    "AQ": {"name_cn": "九元航空", "region": "中国大陆", "note": "低成本航空"},
    "PN": {"name_cn": "西部航空", "region": "中国大陆", "note": "低成本航空"},
    "8L": {"name_cn": "祥鹏航空", "region": "中国大陆", "note": "低成本航空"},
    "KN": {
        "name_cn": "中国联合航空",
        "region": "中国大陆",
        "note": "2016年起转型低成本航空",
    },
    "MM": {"name_cn": "乐桃航空", "region": "日本", "note": "低成本航空"},
    "GK": {"name_cn": "捷星日本", "region": "日本", "note": "低成本航空"},
    "IJ": {"name_cn": "春秋日本", "region": "日本", "note": "低成本航空"},
    "ZG": {"name_cn": "ZIPAIR", "region": "日本", "note": "低成本航空"},
    "7C": {"name_cn": "济州航空", "region": "韩国", "note": "低成本航空"},
    "LJ": {"name_cn": "真航空", "region": "韩国", "note": "低成本航空"},
    "BX": {"name_cn": "釜山航空", "region": "韩国", "note": "低成本航空"},
    "RS": {"name_cn": "首尔航空", "region": "韩国", "note": "低成本航空"},
    "TW": {"name_cn": "德威航空", "region": "韩国", "note": "低成本航空"},
    "UO": {"name_cn": "香港快运", "region": "中国香港", "note": "低成本航空"},
    "IT": {"name_cn": "台湾虎航", "region": "中国台湾", "note": "低成本航空"},
    "TR": {"name_cn": "酷航", "region": "东南亚", "note": "低成本航空"},
    "AK": {"name_cn": "亚洲航空", "region": "东南亚", "note": "亚航系"},
    "D7": {"name_cn": "亚航长途", "region": "东南亚", "note": "亚航系"},
    "FD": {"name_cn": "泰国亚洲航空", "region": "东南亚", "note": "亚航系"},
    "QZ": {"name_cn": "印尼亚洲航空", "region": "东南亚", "note": "亚航系"},
    "Z2": {"name_cn": "菲律宾亚洲航空", "region": "东南亚", "note": "亚航系"},
    "VJ": {"name_cn": "越捷航空", "region": "东南亚", "note": "低成本航空"},
    "VZ": {"name_cn": "泰越捷航空", "region": "东南亚", "note": "低成本航空"},
    "5J": {"name_cn": "宿务太平洋航空", "region": "东南亚", "note": "低成本航空"},
    "SL": {"name_cn": "泰国狮航", "region": "东南亚", "note": "低成本航空"},
}

HYBRID_NOTES = {
    "GJ": {
        "name_cn": "长龙航空",
        "region": "中国大陆",
        "note": "商业模式存在混合型争议，当前不纳入廉航名录。",
    },
}

_CODE_FIELDS = (
    "iata_code",
    "iataCode",
    "airline_code",
    "airlineCode",
    "carrier_code",
    "carrierCode",
    "code",
)
_OPERATING_FIELDS = (
    "opAirline",
    "op_airline",
    "operatingAirline",
    "operating_airline",
    "operatingCarrier",
    "operating_carrier",
    "operated_by",
)
_MARKETING_FIELDS = (
    "marketingAirline",
    "marketing_airline",
    "marketingCarrier",
    "marketing_carrier",
    "airlineCode",
    "airline_code",
    "carrierCode",
    "carrier_code",
    "airline",
)
_FLIGHT_NUMBER_FIELDS = (
    "flightNo",
    "flight_no",
    "flightNumber",
    "flight_number",
)


def validate_lcc_carriers() -> bool:
    """校验名录代码集合和字段完整性。"""
    assert set(LCC_CARRIERS) == EXPECTED_LCC_CARRIER_CODES, (
        "LCC_CARRIERS code set changed: "
        f"expected {len(EXPECTED_LCC_CARRIER_CODES)}, got {len(LCC_CARRIERS)}"
    )
    required = {"name_cn", "region", "note"}
    for code, item in LCC_CARRIERS.items():
        assert re.fullmatch(r"[A-Z0-9]{2}", code), f"Invalid carrier code: {code}"
        missing = required - set(item)
        assert not missing, f"{code} missing fields: {sorted(missing)}"
        for field in required:
            assert str(item.get(field) or "").strip(), f"{code}.{field} is empty"
    assert not (set(HYBRID_NOTES) & set(LCC_CARRIERS)), (
        "HYBRID_NOTES must not overlap LCC_CARRIERS"
    )
    return True


def resolve_lcc_policy(data: dict | None, default=None):
    """按统一优先级读取廉航策略；顶层字段是规范化后的单一真值。"""
    data = data if isinstance(data, dict) else {}
    advanced = data.get("advanced_rules")
    advanced = advanced if isinstance(advanced, dict) else {}
    airline_rules = advanced.get("airlines")
    airline_rules = airline_rules if isinstance(airline_rules, dict) else {}
    candidates = [
        data.get("lcc_policy"),
        (data.get("hard_constraints") or {}).get("lcc_policy")
        if isinstance(data.get("hard_constraints"), dict)
        else None,
        (data.get("constraints") or {}).get("lcc_policy")
        if isinstance(data.get("constraints"), dict)
        else None,
        (data.get("preferences") or {}).get("lcc_policy")
        if isinstance(data.get("preferences"), dict)
        else None,
        airline_rules.get("lcc_policy"),
    ]
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return default


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _carrier_code_from_text(value) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    compact = re.sub(r"[\s_-]+", "", text)
    if re.fullmatch(r"[A-Z0-9]{2}", compact):
        return compact
    match = re.match(r"^([A-Z0-9]{2})(?=0*\d)", compact)
    return match.group(1) if match else ""


def _carrier_code(value) -> str:
    if isinstance(value, dict):
        for field in _CODE_FIELDS:
            code = _carrier_code_from_text(value.get(field))
            if code:
                return code
        for field in _FLIGHT_NUMBER_FIELDS:
            code = _carrier_code_from_text(value.get(field))
            if code:
                return code
        return ""
    return _carrier_code_from_text(value)


def _first_carrier_code(segment: dict, fields: tuple[str, ...]) -> str:
    for field in fields:
        code = _carrier_code(segment.get(field))
        if code:
            return code
    return ""


def _marketing_carrier_code(segment: dict) -> str:
    code = _first_carrier_code(segment, _MARKETING_FIELDS)
    if code:
        return code
    return _first_carrier_code(segment, _FLIGHT_NUMBER_FIELDS)


def classify_segment(segment: dict | None) -> dict:
    """按执飞航司判定单航段；无法取得执飞码时回退市场承运。"""
    segment = segment if isinstance(segment, dict) else {}
    operating_code = _first_carrier_code(segment, _OPERATING_FIELDS)
    explicit_basis = str(segment.get("carrier_basis") or "").strip().lower()
    if explicit_basis not in {"operating", "marketing_fallback"}:
        explicit_basis = ""
    is_codeshare = _as_bool(
        segment.get("isCodeShare")
        if "isCodeShare" in segment
        else segment.get("is_codeshare")
        if "is_codeshare" in segment
        else segment.get("codeshare")
    )
    if operating_code:
        carrier_code = operating_code
        basis = "operating"
    else:
        carrier_code = _marketing_carrier_code(segment)
        basis = explicit_basis or ("marketing_fallback" if is_codeshare else "operating")
    return {
        "is_lcc": carrier_code in LCC_CARRIERS,
        "carrier_code": carrier_code,
        "basis": basis,
    }


def _itinerary_segments(itinerary: dict) -> list[dict]:
    for key in ("segments", "flights", "legs"):
        value = itinerary.get(key)
        if isinstance(value, list) and value:
            return [item for item in value if isinstance(item, dict)]
    return [itinerary] if itinerary else []


def classify_itinerary(itinerary: dict | None) -> dict:
    """汇总组合是否含廉航段，以及是否全部航段均为廉航。"""
    itinerary = itinerary if isinstance(itinerary, dict) else {}
    segments = _itinerary_segments(itinerary)
    results = []
    matched = []
    for index, segment in enumerate(segments):
        result = classify_segment(segment)
        results.append(result)
        if not result["is_lcc"]:
            continue
        flight_no = next(
            (
                str(segment.get(field) or "").strip()
                for field in _FLIGHT_NUMBER_FIELDS
                if str(segment.get(field) or "").strip()
            ),
            "",
        )
        matched.append(
            {
                "index": index,
                "flight_no": flight_no,
                "carrier_code": result["carrier_code"],
                "basis": result["basis"],
                "name_cn": LCC_CARRIERS[result["carrier_code"]]["name_cn"],
            }
        )
    return {
        "has_lcc": bool(matched),
        "all_lcc": bool(results) and all(item["is_lcc"] for item in results),
        "matched_segments": matched,
        "segment_results": results,
    }


def lcc_filter_value(summary: dict | None) -> str:
    """生成过滤日志可核对的航段和承运人值。"""
    summary = summary or {}
    parts = []
    for item in summary.get("matched_segments") or []:
        flight_no = str(item.get("flight_no") or f"segment{int(item.get('index') or 0) + 1}")
        carrier_code = str(item.get("carrier_code") or "unknown")
        basis = str(item.get("basis") or "operating")
        parts.append(f"{flight_no}:{carrier_code}({basis})")
    return ",".join(parts) or "lcc_segments=0"


validate_lcc_carriers()
