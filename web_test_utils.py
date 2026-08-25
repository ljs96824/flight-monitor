"""Web 测试专用的 CSRF 会话辅助函数。"""

from __future__ import annotations

import re


_CSRF_PATTERN = re.compile(r'name="csrf_token" value="([^"]+)"')


def enable_csrf(client, *, path: str = "/") -> str:
    """建立测试会话并让后续写请求自动携带 CSRF 请求头。"""

    response = client.get(path)
    match = _CSRF_PATTERN.search(response.get_data(as_text=True))
    if match is None:
        raise AssertionError(f"{path} 未渲染 csrf_token")
    token = match.group(1)
    client.environ_base["HTTP_X_CSRF_TOKEN"] = token
    return token
