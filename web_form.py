"""Minimal Flask form for flight monitor subscriptions."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from flask import Flask, redirect, render_template_string, request, url_for


BASE_DIR = Path(__file__).parent
SUBSCRIPTIONS_PATH = BASE_DIR / "data" / "subscriptions.json"

app = Flask(__name__)

COMMON_ORIGINS = [
    ("PVG", "上海浦东 PVG"),
    ("PEK", "北京首都 PEK"),
    ("PKX", "北京大兴 PKX"),
    ("CAN", "广州 CAN"),
    ("SZX", "深圳 SZX"),
    ("HKG", "香港 HKG"),
    ("CTU", "成都 CTU"),
    ("TFU", "成都天府 TFU"),
]

DESTINATION_ALIASES = {
    "ORLANDO": "MCO",
    "奥兰多": "MCO",
    "LOS ANGELES": "LAX",
    "洛杉矶": "LAX",
    "NEW YORK": "JFK",
    "纽约": "JFK",
    "SAN FRANCISCO": "SFO",
    "旧金山": "SFO",
    "TOKYO": "NRT",
    "东京": "NRT",
    "BANGKOK": "BKK",
    "曼谷": "BKK",
}


FORM_TEMPLATE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>航班监控订阅</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 520px; margin: 40px auto; padding: 0 16px; }
    label { display: block; margin-top: 16px; font-weight: bold; }
    input, select, button { width: 100%; box-sizing: border-box; padding: 10px; margin-top: 6px; }
    button { margin-top: 24px; background: #1a73e8; color: white; border: 0; cursor: pointer; }
  </style>
</head>
<body>
  <h1>航班监控订阅</h1>
  <form method="post" action="{{ url_for('subscribe') }}">
    <label for="origin">出发地</label>
    <select id="origin" name="origin" required>
      {% for code, label in origins %}
      <option value="{{ code }}">{{ label }}</option>
      {% endfor %}
    </select>

    <label for="destination">目的地</label>
    <input id="destination" name="destination" placeholder="例如 MCO 或 Orlando" required>

    <label for="depart_date">出发日期</label>
    <input id="depart_date" name="depart_date" type="date" required>

    <label for="budget">预算上限（CNY）</label>
    <input id="budget" name="budget" type="number" min="1" step="1" required>

    <button type="submit">提交订阅</button>
  </form>
</body>
</html>
"""


SUCCESS_TEMPLATE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>订阅成功</title>
</head>
<body>
  <h1>订阅成功，将通过PushPlus推送监控结果</h1>
  <p><a href="{{ url_for('index') }}">继续添加订阅</a></p>
</body>
</html>
"""


def load_subscriptions() -> list[dict]:
    if not SUBSCRIPTIONS_PATH.exists():
        return []
    try:
        data = json.loads(SUBSCRIPTIONS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def save_subscription(subscription: dict) -> None:
    SUBSCRIPTIONS_PATH.parent.mkdir(exist_ok=True)
    subscriptions = load_subscriptions()
    subscriptions.append(subscription)
    SUBSCRIPTIONS_PATH.write_text(
        json.dumps(subscriptions, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def normalize_destination(value: str) -> str:
    text = value.strip().upper()
    return DESTINATION_ALIASES.get(text, text)


@app.get("/")
def index():
    return render_template_string(FORM_TEMPLATE, origins=COMMON_ORIGINS)


@app.post("/subscribe")
def subscribe():
    subscription = {
        "origin": request.form.get("origin", "").strip().upper(),
        "destination": normalize_destination(request.form.get("destination", "")),
        "depart_date": request.form.get("depart_date", "").strip(),
        "budget": int(request.form.get("budget", "0") or 0),
        "created_at": datetime.now().isoformat(),
        "status": "active",
    }
    save_subscription(subscription)
    return redirect(url_for("success"))


@app.get("/success")
def success():
    return render_template_string(SUCCESS_TEMPLATE)
