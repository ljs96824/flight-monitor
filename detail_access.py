"""详情页 UUID 与可选共享令牌的单一访问边界。"""

from __future__ import annotations

import copy
import hmac
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID


_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}$"
)


def canonical_detail_uuid(value) -> str | None:
    """只接受带连字符的规范 UUID，拒绝数字索引和任意字符串。"""
    text = str(value or "").strip()
    if not _UUID_PATTERN.fullmatch(text):
        return None
    try:
        return str(UUID(text))
    except (ValueError, AttributeError):
        return None


def shared_detail_token() -> str:
    """返回可选共享令牌；空值表示关闭二次校验。"""
    return str(os.environ.get("SHARED_DETAIL_TOKEN") or "").strip()


def detail_token_authorized(candidate, *, configured: str | None = None) -> bool:
    """令牌关闭时放行；开启后采用常量时间比较并对失败统一返回假。"""
    expected = shared_detail_token() if configured is None else str(configured or "")
    if not expected:
        return True
    supplied = str(candidate or "")
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def delivery_payload_with_detail_token(
    payload: dict,
    *,
    token: str | None = None,
) -> dict:
    """仅在发送副本的详情链接中附令牌，绝不修改待落库 payload。"""
    expected = shared_detail_token() if token is None else str(token or "")
    detail_url = str((payload or {}).get("detail_url") or "").strip()
    if not expected or not detail_url:
        return payload

    parts = urlsplit(detail_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["token"] = expected
    copied = copy.deepcopy(payload)
    copied["detail_url"] = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )
    return copied
