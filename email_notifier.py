"""SMTP email notification helpers."""

from __future__ import annotations

import os
import socket
import smtplib
import io
from email.header import Header
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from log_utils import safe_log

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


def render_email(payload: dict):
    """Render the full email HTML from the unified notification payload."""
    from notifier import render_email as _render_email

    subject, html = _render_email(payload)
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


def send_email(to_email: str, subject: str, html_content: str, inline_images: dict | None = None) -> bool:
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

    msg = MIMEMultipart("related")
    msg["Subject"] = str(Header(subject or "航班监控通知", "utf-8"))
    msg["From"] = smtp_user
    msg["To"] = to_email
    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(html_content or "", "html", "utf-8"))
    msg.attach(alternative)
    for cid, image_bytes in (inline_images or {}).items():
        if not image_bytes:
            continue
        img = MIMEImage(image_bytes)
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline")
        msg.attach(img)

    server = None
    try:
        if config["ssl"]:
            server = smtplib.SMTP_SSL(config["host"], config["port"], timeout=30)
        else:
            server = smtplib.SMTP(config["host"], config["port"], timeout=30)
            server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [to_email], msg.as_string())
        safe_log(f"[邮件] 发送成功 → {to_email}")
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
