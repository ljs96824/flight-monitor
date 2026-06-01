"""SMTP email notification helpers."""

from __future__ import annotations

import os
import socket
import smtplib
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - runtime dependency is optional here
    load_dotenv = None


BASE_DIR = Path(__file__).parent
if load_dotenv:
    load_dotenv(BASE_DIR / ".env", encoding="utf-8")


PROVIDERS = {
    "qq": {"host": "smtp.qq.com", "port": 465, "ssl": True},
    "163": {"host": "smtp.163.com", "port": 465, "ssl": True},
    "gmail": {"host": "smtp.gmail.com", "port": 465, "ssl": True},
}


def _smtp_config() -> dict:
    """Resolve SMTP provider config, keeping SMTP_HOST/SMTP_PORT as overrides."""
    provider = os.getenv("SMTP_PROVIDER", "qq").strip().lower()
    config = dict(PROVIDERS.get(provider, PROVIDERS["qq"]))
    if os.getenv("SMTP_HOST"):
        config["host"] = os.getenv("SMTP_HOST", config["host"])
    if os.getenv("SMTP_PORT"):
        config["port"] = int(os.getenv("SMTP_PORT", str(config["port"])))
    if os.getenv("SMTP_SSL"):
        config["ssl"] = os.getenv("SMTP_SSL", "true").strip().lower() not in {
            "0",
            "false",
            "no",
        }
    config["provider"] = provider if provider in PROVIDERS else "qq"
    return config


def send_email(to_email: str, subject: str, html_content: str) -> bool:
    """Send one HTML email through the configured SMTP account."""
    config = _smtp_config()
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")

    # Gmail notes:
    # - Enable 2-Step Verification first, then create an App Password.
    # - smtp.gmail.com is often unreachable from mainland China without proxy
    #   or overseas deployment.
    # - If this times out while using Gmail, it is usually a network access
    #   problem rather than an application bug.

    if not to_email:
        print("[邮件] 未提供收件邮箱")
        return False
    if not smtp_user or not smtp_pass:
        print("[邮件] SMTP_USER 或 SMTP_PASS 未配置")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = str(Header(subject or "航班监控通知", "utf-8"))
    msg["From"] = smtp_user
    msg["To"] = to_email
    msg.attach(MIMEText(html_content or "", "html", "utf-8"))

    server = None
    try:
        if config["ssl"]:
            server = smtplib.SMTP_SSL(config["host"], config["port"], timeout=30)
        else:
            server = smtplib.SMTP(config["host"], config["port"], timeout=30)
            server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [to_email], msg.as_string())
        print(f"[邮件] 发送成功 → {to_email}")
        return True
    except (socket.timeout, TimeoutError, ConnectionError):
        print("[邮件] 连接SMTP服务器超时。如使用Gmail，国内需代理访问")
        return False
    except OSError as exc:
        print(f"[邮件] 连接SMTP服务器失败: {exc}")
        if config["provider"] == "gmail":
            print("[邮件] Gmail在国内网络通常无法直连，请使用代理或海外部署")
        return False
    except smtplib.SMTPAuthenticationError:
        print("[邮件] SMTP认证失败，请确认邮箱账号和授权码，不要使用登录密码")
        return False
    except Exception as exc:
        print(f"[邮件] 发送失败: {exc}")
        return False
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass
