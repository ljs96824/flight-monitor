"""Run the minimal subscription form service."""

import ipaddress
import os
from pathlib import Path
import sys

from log_utils import configure_run_logging


def _is_loopback_host(host) -> bool:
    normalized = str(host or "").strip()
    if normalized.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _resolve_bind_settings(environ=None) -> tuple[str, int]:
    source = os.environ if environ is None else environ
    host = str(source.get("WEB_HOST", "127.0.0.1") or "").strip()
    raw_port = source.get("WEB_PORT", "5000")
    try:
        port = int(str(raw_port).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("WEB_PORT must be an integer from 1 to 65535") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("WEB_PORT must be an integer from 1 to 65535")
    if not _is_loopback_host(host) and source.get("ALLOW_PUBLIC_WEB_BIND") != "1":
        raise RuntimeError(
            "WEB_HOST is not loopback; set ALLOW_PUBLIC_WEB_BIND=1 "
            "to allow an intentional non-loopback bind"
        )
    return host, port


def main() -> int:
    host, port = _resolve_bind_settings()
    configure_run_logging(Path(__file__).resolve().parent / "data" / "run_latest.log")

    from web_form import app

    if not _is_loopback_host(host):
        print(
            "[Web安全][高可见告警] "
            f"正在监听非回环地址 {host}:{port}；当前应用没有身份认证；"
            "CSRF 不等于身份认证；服务可能被同网段客户端直接访问。",
            file=sys.stderr,
        )
    app.run(host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

