"""详情快照发布回执(R8-2)：本地保存、PA 上传与查看目标分别记录，再显式映射为 page_only 是否送达。

查看目标只按本次详情地址解析出的主机名判断(Q1)：回环地址为本地；`<账户>.pythonanywhere.com` 且账户与
实际上传账户一致为 PA；PA 域名但账户不一致为配置冲突；自定义域名、其他主机或无法解析的地址为目标不明确。
地址带用户信息(`@`)或反斜杠、端口非法，或 PA 地址用了 80/443 以外的端口，一律判为不明确(浏览器与解析器可能看到不同主机)。
不做字符串包含匹配，不访问目标网站，不做 DNS 探测。
PA 目标在上传未取得成功回执时，仅当同进程已加载的 web_form 读取目录、本次发布目录与该账户的 PA 部署
payload 目录三者规范化后一致，才认定为 PA 同目录直写(Q2)；不凭平台名、路径外观或请求 Host 头判断。
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import sys
from typing import Callable
from urllib.parse import urlsplit


TARGET_LOCAL = "local"
TARGET_PYTHONANYWHERE = "pythonanywhere"
TARGET_PA_ACCOUNT_CONFLICT = "pa_account_conflict"
TARGET_UNKNOWN = "unknown"

METHOD_LOCAL_PAGE = "local_page"
METHOD_PA_UPLOAD = "pa_upload"
METHOD_PA_SAME_DIRECTORY = "pa_same_directory"

UPLOAD_SUCCEEDED = "succeeded"
UPLOAD_DECLINED = "declined"
UPLOAD_ERROR = "error"
UPLOAD_NOT_ATTEMPTED = "not_attempted"

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
PA_SITE_SUFFIX = ".pythonanywhere.com"
PA_SITE_PORTS = frozenset({None, 80, 443})


def detail_view_target(detail_url, pa_user: str) -> tuple[str, str]:
    """返回(查看目标, 规范化主机名)。"""

    try:
        parts = urlsplit(str(detail_url or "").strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return TARGET_UNKNOWN, ""
    if parts.scheme not in {"http", "https"} or not host:
        return TARGET_UNKNOWN, host
    if "@" in parts.netloc or "\\" in parts.netloc:
        return TARGET_UNKNOWN, host
    if host in LOOPBACK_HOSTS:
        return TARGET_LOCAL, host
    if host.endswith(PA_SITE_SUFFIX) and port in PA_SITE_PORTS:
        account = host[: -len(PA_SITE_SUFFIX)]
        if account and "." not in account:
            if account == str(pa_user or "").strip().lower():
                return TARGET_PYTHONANYWHERE, host
            return TARGET_PA_ACCOUNT_CONFLICT, host
    return TARGET_UNKNOWN, host


def pa_payload_directory(path_template: str, pa_user: str) -> PurePosixPath:
    """PA 上该账户的部署 payload 目录(与上传目标同一模板)。"""

    return PurePosixPath(path_template.format(user=pa_user, filename="payload.json")).parent


def pa_same_directory(published_dir, pa_payload_dir, *, web_module=None) -> bool:
    """同进程 web_form 的读取目录、本次发布目录与 PA 部署 payload 目录规范化后是否为同一目录。"""

    module = sys.modules.get("web_form") if web_module is None else web_module
    reader_dir = getattr(module, "PAGE_PAYLOADS_DIR", None) if module is not None else None
    if reader_dir is None:
        return False
    expected = Path(str(pa_payload_dir))
    if not expected.is_absolute():
        return False
    try:
        published = Path(published_dir).resolve()
        reader = Path(reader_dir).resolve()
        expected = expected.resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return published == reader == expected


def build_detail_publish_receipt(
    *,
    local_saved: bool,
    upload: str,
    detail_url,
    pa_user: str,
    same_directory_check: Callable[[], bool],
) -> dict:
    """三项分别记录；未执行的检查记为 None，不填成成功。"""

    target, host = detail_view_target(detail_url, pa_user)
    same_directory = None
    method = None
    if local_saved:
        if target == TARGET_LOCAL:
            method = METHOD_LOCAL_PAGE
        elif target == TARGET_PYTHONANYWHERE:
            if upload == UPLOAD_SUCCEEDED:
                method = METHOD_PA_UPLOAD
            else:
                try:
                    same_directory = same_directory_check() is True
                except Exception:
                    same_directory = False
                if same_directory:
                    method = METHOD_PA_SAME_DIRECTORY
    return {
        "local_saved": bool(local_saved),
        "upload": upload,
        "target": target,
        "target_host": host,
        "same_directory": same_directory,
        "method": method,
        "target_reached": method is not None,
    }


def page_only_delivered(receipt) -> bool:
    """page_only 的送达结论：只认回执里明确的 target_reached=True。"""

    return isinstance(receipt, dict) and receipt.get("target_reached") is True
