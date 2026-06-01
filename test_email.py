"""Send a test email for SMTP configuration."""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - runtime dependency is optional here
    load_dotenv = None

from email_notifier import send_email


BASE_DIR = Path(__file__).parent
if load_dotenv:
    load_dotenv(BASE_DIR / ".env", encoding="utf-8")


if __name__ == "__main__":
    to_email = os.getenv("TEST_EMAIL_TO") or os.getenv("SMTP_USER", "")
    if not to_email:
        print("请在 .env 中设置 TEST_EMAIL_TO 或 SMTP_USER")
        raise SystemExit(1)

    ok = send_email(
        to_email,
        "航班监控邮件测试",
        "<p>航班监控邮件测试，如收到说明 SMTP 配置成功。</p>",
    )
    raise SystemExit(0 if ok else 1)
