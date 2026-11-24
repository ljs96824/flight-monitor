"""SMTP email notification helpers."""

from __future__ import annotations

import os
import socket
import smtplib
import io
from collections.abc import Mapping
from email.header import Header
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from log_utils import redact_text, safe_log
from notification_config import DEFAULT_NOTIFICATION_PRIVACY_LEVEL, resolve_notification_privacy_level

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


def _payload_privacy_level(payload: Mapping | None) -> str:
    if isinstance(payload, Mapping):
        route = payload.get("route_info")
        for scope in (payload, payload.get("notification_goals"),
                      route.get("notification_goals") if isinstance(route, Mapping) else None):
            if isinstance(scope, Mapping) and (scope.get("privacy_level") or scope.get("notification_privacy_level")):
                return resolve_notification_privacy_level(scope)
    return DEFAULT_NOTIFICATION_PRIVACY_LEVEL


def _is_private_email_html(html_content: str | None) -> bool:
    return any(marker in (html_content or "") for marker in (
        "【航班监控·已脱敏】", "<b>航班监控有变动</b>", "乘客构成与精确金额已隐藏",
        "乘客构成、精确金额与技术细节已隐藏",
    ))


def _filter_inline_images(html_content: str | None, inline_images: Mapping | None,
                          *, privacy_level: str | None = None) -> dict:
    level = resolve_notification_privacy_level({"privacy_level": privacy_level})
    if level != DEFAULT_NOTIFICATION_PRIVACY_LEVEL or _is_private_email_html(html_content):
        return {}
    html = html_content or ""
    return {cid: data for cid, data in (inline_images or {}).items() if data and f"cid:{cid}" in html}


def render_email(payload: dict):
    """Render the full email HTML from the unified notification payload."""
    from notifier import render_email as _render_email

    level = _payload_privacy_level(payload)
    # The shared HTML renderer reads top-level goals; keep the caller's payload untouched.
    if level != DEFAULT_NOTIFICATION_PRIVACY_LEVEL and resolve_notification_privacy_level(payload) != level:
        payload = {**payload, "privacy_level": level}
    subject, html = _render_email(payload)
    if level != DEFAULT_NOTIFICATION_PRIVACY_LEVEL or _is_private_email_html(html) or "cid:trendchart" not in (html or ""):
        return subject, html, {}
    image = build_trend_png(
        payload.get("price_history") or [],
        payload.get("ideal_price"),
        payload.get("max_price"),
        payload.get("current_price"),
    )
    inline_images = {"trendchart": image} if image else {}
    return subject, html, inline_images


def build_trend_png(price_history, ideal_price=None, max_price=None, current_price=None):
    """Build a PNG trend chart for email CID embedding, with explicit date labels."""
    rows = [
        item for item in (price_history or [])
        if isinstance(item, dict) and item.get("price") and item.get("price") > 0
    ][-14:]
    prices = [row["price"] for row in rows]
    if len(rows) < 3 or len(set(round(float(price), 2) for price in prices)) < 2:
        return None
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[邮件] matplotlib 未安装，跳过趋势图PNG")
        return None

    dates = [str(row.get("date") or "") for row in rows]
    ideal = float(ideal_price) if ideal_price else None
    current = float(current_price) if current_price else prices[-1]

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(6, 2.8), dpi=110)
    x = list(range(len(dates)))
    ax.plot(x, prices, color="#3b82f6", marker="o", linewidth=2)
    if ideal:
        ax.axhline(ideal, color="#16a34a", linestyle="--", linewidth=1.2, label="理想价")
    current_color = "#16a34a" if ideal and current <= ideal else "#3b82f6"
    ax.scatter([x[-1]], [prices[-1]], color=current_color, zorder=5, s=60)
    ax.annotate(
        f"¥{prices[-1]:,.0f}",
        (x[-1], prices[-1]),
        textcoords="offset points",
        xytext=(0, 8),
        ha="center",
        fontsize=8,
    )
    ax.set_xticks(x)
    ax.set_xticklabels(dates, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("价格(元)", fontsize=9)
    ax.set_title("近期价格走势", fontsize=11)
    if ideal:
        ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    ax.tick_params(axis="y", labelsize=8)
    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _sanitize_smtp_error(exc: object, smtp_pass: str = "") -> str:
    text = redact_text(str(exc))
    password = smtp_pass or os.getenv("SMTP_PASS", "")
    if password:
        text = text.replace(password, "***").replace(redact_text(password), "***")
    return text


def _smtp_host(value: str, *, trim: bool = False) -> str:
    if isinstance(value, str) and "\r" not in value and "\n" not in value:
        host = value.strip() if trim else value
        if host and not any(ch.isspace() for ch in host):
            return host
    raise ValueError("SMTP_HOST 配置无效")


def _smtp_port(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError("SMTP_PORT 超出有效端口范围(1-65535)")
    return value


def _smtp_config() -> dict:
    """Resolve SMTP provider config, keeping SMTP_HOST/SMTP_PORT as overrides."""
    provider = os.getenv("SMTP_PROVIDER", "qq").strip().lower()
    config = dict(PROVIDERS.get(provider, PROVIDERS["qq"]))
    raw_host = os.getenv("SMTP_HOST")
    if raw_host not in (None, ""):
        config["host"] = _smtp_host(raw_host, trim=True)
    raw_port = os.getenv("SMTP_PORT")
    if raw_port not in (None, ""):
        config["port"] = _smtp_port(int(raw_port.strip()))
    if os.getenv("SMTP_SSL"):
        config["ssl"] = os.getenv("SMTP_SSL", "true").strip().lower() not in {
            "0",
            "false",
            "no",
        }
    config["provider"] = provider if provider in PROVIDERS else "qq"
    return config


def send_email(to_email: str, subject: str, html_content: str, inline_images: dict | None = None,
               *, privacy_level: str | None = None) -> bool:
    """Send one HTML email through the configured SMTP account."""
    # 本笔只保证 email_notifier.send_email() 在 effective NO_LIVE_API 精确等于 "1" 时拒绝 SMTP 发送。
    # 它不证明 PushPlus、PythonAnywhere Files、Juhe、SerpAPI、Duffel 或任何其他 sink 同样受该变量保护。
    if os.environ.get("NO_LIVE_API") == "1":
        safe_log("[邮件] NO_LIVE_API=1，已阻止真实 SMTP 发送")
        return False

    try:
        config = _smtp_config()
        if not isinstance(config, Mapping):
            raise ValueError("SMTP 配置不是映射")
        host = _smtp_host(config["host"])
        port = _smtp_port(config["port"])
        use_ssl = bool(config["ssl"])
        provider = config["provider"]
        if provider not in PROVIDERS:
            raise ValueError("SMTP_PROVIDER 配置无效")
    except Exception as exc:
        safe_log(f"[邮件] SMTP配置无效: {_sanitize_smtp_error(exc)}")
        return False
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")

    # Gmail notes:
    # - Enable 2-Step Verification first, then create an App Password.
    # - smtp.gmail.com is often unreachable from mainland China without proxy
    #   or overseas deployment.
    # - If this times out while using Gmail, it is usually a network access
    #   problem rather than an application bug.

    if not isinstance(to_email, str) or not to_email.strip():
        print("[邮件] 未提供收件邮箱")
        return False
    recipient = to_email.strip()
    if "\r" in to_email or "\n" in to_email:
        safe_log("[邮件] 收件邮箱包含换行，已取消发送")
        return False
    if not smtp_user or not smtp_pass:
        print("[邮件] SMTP_USER 或 SMTP_PASS 未配置")
        return False

    if "\r" in smtp_user or "\n" in smtp_user:
        safe_log("[邮件] 发件邮箱包含换行，已取消发送")
        return False

    try:
        msg = MIMEMultipart("related")
        msg["Subject"] = str(Header(subject or "航班监控通知", "utf-8"))
        msg["From"] = smtp_user
        msg["To"] = recipient
        alternative = MIMEMultipart("alternative")
        alternative.attach(MIMEText(html_content or "", "html", "utf-8"))
        msg.attach(alternative)
        for cid, image_bytes in _filter_inline_images(html_content, inline_images, privacy_level=privacy_level).items():
            img = MIMEImage(image_bytes)
            img.add_header("Content-ID", f"<{cid}>")
            img.add_header("Content-Disposition", "inline")
            msg.attach(img)
        msg_string = msg.as_string()
    except Exception as exc:
        safe_log(f"[邮件] 邮件构造失败: {_sanitize_smtp_error(exc, smtp_pass)}")
        return False

    server = None
    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [recipient], msg_string)
        safe_log(f"[邮件] 发送成功 → {recipient}")
        return True
    except (socket.timeout, TimeoutError, ConnectionError):
        print("[邮件] 连接SMTP服务器超时。如使用Gmail，国内需代理访问")
        return False
    except OSError as exc:
        safe_log(f"[邮件] 连接SMTP服务器失败: {_sanitize_smtp_error(exc, smtp_pass)}")
        if provider == "gmail":
            print("[邮件] Gmail在国内网络通常无法直连，请使用代理或海外部署")
        return False
    except smtplib.SMTPAuthenticationError:
        print("[邮件] SMTP认证失败，请确认邮箱账号和授权码，不要使用登录密码")
        return False
    except Exception as exc:
        safe_log(f"[邮件] 发送失败: {_sanitize_smtp_error(exc, smtp_pass)}")
        return False
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass
