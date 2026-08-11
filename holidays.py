"""中国大陆与日本法定节假日的静态事实表，只用于解释，不参与预测修正。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, timedelta

from airports import get_airport_country
from method_registry import method_version


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


HOLIDAY_SHOULDER_DAYS = _positive_int_env("HOLIDAY_SHOULDER_DAYS", 1)
HOLIDAY_METHOD_VERSION = method_version("holiday_calendar")

_CHINA_2026_SOURCE = "https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm"
_JAPAN_SOURCE = "https://www8.cao.go.jp/chosei/shukujitsu/gaiyou.html"


def _holiday(country, name, start, end=None, *, note="", source_title, source_url):
    return {
        "country": country,
        "name": name,
        "start": start,
        "end": end or start,
        "note": note,
        "source_title": source_title,
        "source_url": source_url,
    }


HOLIDAYS = (
    _holiday("中国大陆", "元旦", "2026-01-01", "2026-01-03", source_title="国务院办公厅关于2026年部分节假日安排的通知", source_url=_CHINA_2026_SOURCE),
    _holiday("中国大陆", "春节", "2026-02-15", "2026-02-23", source_title="国务院办公厅关于2026年部分节假日安排的通知", source_url=_CHINA_2026_SOURCE),
    _holiday("中国大陆", "清明节", "2026-04-04", "2026-04-06", source_title="国务院办公厅关于2026年部分节假日安排的通知", source_url=_CHINA_2026_SOURCE),
    _holiday("中国大陆", "劳动节", "2026-05-01", "2026-05-05", source_title="国务院办公厅关于2026年部分节假日安排的通知", source_url=_CHINA_2026_SOURCE),
    _holiday("中国大陆", "端午节", "2026-06-19", "2026-06-21", source_title="国务院办公厅关于2026年部分节假日安排的通知", source_url=_CHINA_2026_SOURCE),
    _holiday("中国大陆", "中秋节", "2026-09-25", "2026-09-27", source_title="国务院办公厅关于2026年部分节假日安排的通知", source_url=_CHINA_2026_SOURCE),
    _holiday("中国大陆", "国庆节", "2026-10-01", "2026-10-07", source_title="国务院办公厅关于2026年部分节假日安排的通知", source_url=_CHINA_2026_SOURCE),
    _holiday("中国大陆", "元旦", "2027-01-01", note="调休安排未公布", source_title="全国年节及纪念日放假办法", source_url="https://www.gov.cn/zhengce/zhengceku/202411/content_6986381.htm"),
    _holiday("中国大陆", "春节", "2027-02-05", "2027-02-08", note="调休安排未公布", source_title="全国年节及纪念日放假办法", source_url="https://www.gov.cn/zhengce/zhengceku/202411/content_6986381.htm"),
    _holiday("中国大陆", "清明节", "2027-04-05", note="调休安排未公布", source_title="全国年节及纪念日放假办法", source_url="https://www.gov.cn/zhengce/zhengceku/202411/content_6986381.htm"),
    _holiday("中国大陆", "劳动节", "2027-05-01", "2027-05-02", note="调休安排未公布", source_title="全国年节及纪念日放假办法", source_url="https://www.gov.cn/zhengce/zhengceku/202411/content_6986381.htm"),
    _holiday("中国大陆", "端午节", "2027-06-09", note="调休安排未公布", source_title="全国年节及纪念日放假办法", source_url="https://www.gov.cn/zhengce/zhengceku/202411/content_6986381.htm"),
    _holiday("中国大陆", "中秋节", "2027-09-15", note="调休安排未公布", source_title="全国年节及纪念日放假办法", source_url="https://www.gov.cn/zhengce/zhengceku/202411/content_6986381.htm"),
    _holiday("中国大陆", "国庆节", "2027-10-01", "2027-10-03", note="调休安排未公布", source_title="全国年节及纪念日放假办法", source_url="https://www.gov.cn/zhengce/zhengceku/202411/content_6986381.htm"),
)

_JAPAN_DATES = {
    2026: (("元日", "01-01"), ("成人日", "01-12"), ("建国纪念日", "02-11"), ("天皇诞生日", "02-23"), ("春分日", "03-20"), ("昭和日", "04-29"), ("宪法纪念日", "05-03"), ("绿之日", "05-04"), ("儿童日", "05-05"), ("法定休息日", "05-06"), ("海之日", "07-20"), ("山之日", "08-11"), ("敬老日", "09-21"), ("法定休息日", "09-22"), ("秋分日", "09-23"), ("体育日", "10-12"), ("文化日", "11-03"), ("勤劳感谢日", "11-23")),
    2027: (("元日", "01-01"), ("成人日", "01-11"), ("建国纪念日", "02-11"), ("天皇诞生日", "02-23"), ("春分日", "03-21"), ("法定休息日", "03-22"), ("昭和日", "04-29"), ("宪法纪念日", "05-03"), ("绿之日", "05-04"), ("儿童日", "05-05"), ("海之日", "07-19"), ("山之日", "08-11"), ("敬老日", "09-20"), ("秋分日", "09-23"), ("体育日", "10-11"), ("文化日", "11-03"), ("勤劳感谢日", "11-23")),
}

HOLIDAYS += tuple(
    _holiday("日本", name, f"{year}-{month_day}", source_title="日本内阁府 国民の祝日について", source_url=_JAPAN_SOURCE)
    for year, entries in _JAPAN_DATES.items()
    for name, month_day in entries
)


def _digest() -> str:
    raw = json.dumps(HOLIDAYS, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


HOLIDAY_DATA_DIGEST = _digest()
EXPECTED_HOLIDAY_DATA_DIGEST = "bab6a37361684e542151b884841a74c6bd00460cbf4b535ff394321a41c06abc"


def holiday_labels_for_country(country: str, target: date, *, shoulder_days: int = HOLIDAY_SHOULDER_DAYS) -> list[dict]:
    labels = []
    for item in HOLIDAYS:
        if item["country"] != country:
            continue
        start = date.fromisoformat(item["start"])
        end = date.fromisoformat(item["end"])
        if start <= target <= end:
            relative = "当天"
        elif 0 < (start - target).days <= shoulder_days:
            relative = f"节前{(start - target).days}日"
        elif 0 < (target - end).days <= shoulder_days:
            relative = f"节后{(target - end).days}日"
        else:
            continue
        labels.append({**item, "relative": relative, "method_version": HOLIDAY_METHOD_VERSION})
    return labels


def holiday_labels_for_route(origin_iata: str, dest_iata: str, target: date, *, shoulder_days: int = HOLIDAY_SHOULDER_DAYS) -> list[str]:
    countries = []
    for code in (origin_iata, dest_iata):
        country = get_airport_country(code)
        if country and country not in countries:
            countries.append(country)
    return [
        f"{item['country']}·{item['name']}({item['relative']})"
        for country in countries
        for item in holiday_labels_for_country(country, target, shoulder_days=shoulder_days)
    ]


def validate_holidays() -> bool:
    assert HOLIDAY_DATA_DIGEST == EXPECTED_HOLIDAY_DATA_DIGEST, "holiday data changed without an explicit version/digest update"
    return True
