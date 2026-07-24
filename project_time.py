"""项目统一时区入口。"""

from datetime import timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROJECT_TIMEZONE_NAME = "Asia/Shanghai"


def load_project_timezone():
    """优先使用 IANA 数据，缺失时退回上海固定 UTC+8。"""

    try:
        return ZoneInfo(PROJECT_TIMEZONE_NAME)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=8), PROJECT_TIMEZONE_NAME)


SHANGHAI_TZ = load_project_timezone()
