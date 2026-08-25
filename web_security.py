"""Web 会话密钥与全局 CSRF 防护。"""

from __future__ import annotations

from datetime import datetime
import hashlib
import hmac
import os
import secrets
import time
from typing import Callable, Mapping

from flask import abort, current_app, request, session


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
DEFAULT_CSRF_TTL_SECONDS = 2 * 60 * 60
MAX_CSRF_TOKEN_LENGTH = 512


def _env_bool(value: object, *, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_positive_int(value: object, *, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def configure_session_security(
    app,
    *,
    environ: Mapping[str, str] | None = None,
    logger: Callable[[str], None] = print,
    secret_factory: Callable[[], str] | None = None,
) -> dict:
    """设置会话边界；无固定密钥时仅允许本地开发临时兜底。"""

    values = os.environ if environ is None else environ
    configured_secret = str(values.get("FLASK_SECRET_KEY") or "").strip()
    temporary_secret = not configured_secret
    if temporary_secret:
        factory = secret_factory or (lambda: secrets.token_urlsafe(48))
        configured_secret = factory()
        logger(
            "[Web安全][高可见告警] FLASK_SECRET_KEY 未配置，"
            "已生成进程内临时会话密钥；此路径仅限本地开发兜底，"
            "PythonAnywhere验收前必须配置固定值。"
        )

    app.secret_key = configured_secret
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_env_bool(values.get("SESSION_COOKIE_SECURE")),
        CSRF_TOKEN_TTL_SECONDS=_env_positive_int(
            values.get("CSRF_TOKEN_TTL_SECONDS"),
            default=DEFAULT_CSRF_TTL_SECONDS,
        ),
    )
    return {"temporary_secret": temporary_secret}


def _clock_epoch() -> int:
    clock = current_app.config.get("CSRF_CLOCK") or time.time
    value = clock()
    if isinstance(value, datetime):
        return int(value.timestamp())
    return int(value)


def _secret_bytes() -> bytes:
    secret = current_app.secret_key
    if isinstance(secret, bytes):
        return secret
    return str(secret or "").encode("utf-8")


def issue_csrf_token() -> str:
    """签发绑定当前会话、带签发时间的 CSRF token。"""

    nonce = str(session.get("_csrf_nonce") or "")
    if not nonce:
        nonce = secrets.token_urlsafe(32)
        session["_csrf_nonce"] = nonce
    issued_at = _clock_epoch()
    payload = f"{issued_at}:{nonce}"
    signature = hmac.new(
        _secret_bytes(),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}:{signature}"


def validate_csrf_token(token: object) -> tuple[bool, str]:
    """恒定时间校验签名与会话 nonce，并执行固定 TTL。"""

    text = str(token or "")
    if not text or len(text) > MAX_CSRF_TOKEN_LENGTH:
        return False, "malformed"
    try:
        issued_text, supplied_nonce, supplied_signature = text.split(":", 2)
        issued_at = int(issued_text)
        supplied_nonce_bytes = supplied_nonce.encode("ascii")
        supplied_signature_bytes = supplied_signature.encode("ascii")
    except (TypeError, UnicodeEncodeError, ValueError):
        return False, "malformed"

    expected_nonce = str(session.get("_csrf_nonce") or "")
    try:
        expected_nonce_bytes = expected_nonce.encode("ascii")
    except UnicodeEncodeError:
        return False, "invalid"
    payload = f"{issued_at}:{supplied_nonce}"
    expected_signature_bytes = hmac.new(
        _secret_bytes(),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest().encode("ascii")
    nonce_valid = bool(expected_nonce) and secrets.compare_digest(
        supplied_nonce_bytes,
        expected_nonce_bytes,
    )
    signature_valid = secrets.compare_digest(
        supplied_signature_bytes,
        expected_signature_bytes,
    )
    if not nonce_valid or not signature_valid:
        return False, "invalid"

    age_seconds = _clock_epoch() - issued_at
    ttl_seconds = int(
        current_app.config.get(
            "CSRF_TOKEN_TTL_SECONDS",
            DEFAULT_CSRF_TTL_SECONDS,
        )
    )
    if age_seconds < 0:
        return False, "issued_in_future"
    if age_seconds > ttl_seconds:
        return False, "expired"
    return True, "ok"


def install_csrf_protection(app, *, logger: Callable[[str], None] = print) -> None:
    """全局拦截所有非安全 HTTP 方法，不依赖逐路由装饰器。"""

    app.jinja_env.globals["csrf_token"] = issue_csrf_token

    @app.before_request
    def _enforce_global_csrf():
        method = request.method.upper()
        if method in SAFE_METHODS:
            return None
        if method not in UNSAFE_METHODS:
            return None
        token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        valid, reason = validate_csrf_token(token)
        if valid:
            return None
        logger(
            f"[CSRF] 拒绝 method={method} path={request.path} reason={reason}"
        )
        abort(403, description="CSRF token 缺失、无效或已过期")
