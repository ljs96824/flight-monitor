"""Minimal Flask form for flight monitor subscriptions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, redirect, render_template_string, request, url_for


BASE_DIR = Path(__file__).parent
SUBSCRIPTIONS_PATH = BASE_DIR / "data" / "subscriptions.json"

app = Flask(__name__)

COMMON_ORIGINS = [
    ("PVG", "上海 PVG / 浦东"),
    ("SHA", "上海 SHA / 虹桥"),
    ("PEK", "北京 PEK / 首都"),
    ("PKX", "北京 PKX / 大兴"),
    ("CAN", "广州 CAN"),
    ("SZX", "深圳 SZX"),
    ("CTU", "成都 CTU"),
    ("HGH", "杭州 HGH"),
    ("NKG", "南京 NKG"),
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
    body {
      font-family: Arial, sans-serif;
      max-width: 560px;
      margin: 24px auto;
      padding: 0 16px 32px;
      color: #222;
      line-height: 1.5;
    }
    fieldset {
      border: 1px solid #ddd;
      border-radius: 8px;
      margin: 18px 0;
      padding: 16px;
    }
    legend { font-weight: bold; padding: 0 6px; }
    label { display: block; margin-top: 14px; font-weight: bold; }
    input, select, button {
      width: 100%;
      box-sizing: border-box;
      padding: 11px;
      margin-top: 6px;
      border: 1px solid #ccc;
      border-radius: 6px;
      font-size: 16px;
    }
    .choice label {
      display: flex;
      align-items: center;
      gap: 8px;
      font-weight: normal;
      margin-top: 8px;
    }
    .choice input { width: auto; margin: 0; }
    button {
      margin-top: 20px;
      background: #1a73e8;
      color: white;
      border: 0;
      font-weight: bold;
      cursor: pointer;
    }
    .hint { color: #666; font-size: 13px; margin-top: 4px; }
    #return-date-wrap { display: none; }
  </style>
</head>
<body>
  <h1>航班监控订阅</h1>
  <form method="post" action="{{ url_for('subscribe') }}">
    <fieldset>
      <legend>基础信息</legend>

      <label for="origin">出发地</label>
      <select id="origin" name="origin_select">
        {% for code, label in origins %}
        <option value="{{ code }}">{{ label }}</option>
        {% endfor %}
      </select>
      <input name="origin_manual" placeholder="或手动输入IATA代码，例如 PVG">

      <label for="destination">目的地</label>
      <input id="destination" name="destination" placeholder="例如 MCO 或 Orlando / 奥兰多" required>

      <label for="depart_date">出发日期</label>
      <input id="depart_date" name="depart_date" type="date" required>

      <label>是否往返</label>
      <div class="choice">
        <label><input type="radio" name="round_trip" value="false" checked> 单程</label>
        <label><input type="radio" name="round_trip" value="true"> 往返</label>
      </div>

      <div id="return-date-wrap">
        <label for="return_date">返程日期</label>
        <input id="return_date" name="return_date" type="date">
      </div>

      <label>日期是否灵活</label>
      <select name="date_flexibility">
        <option value="0">不灵活</option>
        <option value="1">前后1天</option>
        <option value="3">前后3天</option>
        <option value="7">前后7天</option>
      </select>

      <label for="budget">预算上限（CNY）</label>
      <input id="budget" name="budget" type="number" min="1" step="1" required>
    </fieldset>

    <fieldset>
      <legend>偏好设置</legend>

      <label>是否必须直飞</label>
      <select name="direct_only">
        <option value="must">必须直飞</option>
        <option value="flexible" selected>可以中转</option>
        <option value="cheap_ok">便宜很多可以中转</option>
      </select>

      <label>红眼/过早航班</label>
      <select name="red_eye">
        <option value="reject" selected>不接受</option>
        <option value="flexible">可以接受</option>
        <option value="cheap_ok">便宜很多可以接受</option>
      </select>
      <div class="hint">红眼定义：起飞时间在23:00-06:00之间。</div>

      <label>是否需要托运行李</label>
      <select name="need_baggage">
        <option value="required" selected>必须</option>
        <option value="not_needed">不需要</option>
        <option value="unknown">不确定</option>
      </select>
    </fieldset>

    <fieldset>
      <legend>场景信息</legend>

      <label>出行类型</label>
      <select name="trip_type">
        <option value="business_meeting">商务会议</option>
        <option value="tourism" selected>旅游</option>
        <option value="family_visit">探亲</option>
        <option value="student_return">学生返校</option>
        <option value="family_elder">家庭老人同行</option>
        <option value="other">其他</option>
      </select>

      <label>希望系统做什么</label>
      <div class="choice">
        <label><input type="checkbox" name="goals" value="price_drop_alert" checked> 跌价提醒</label>
        <label><input type="checkbox" name="goals" value="buy_timing" checked> 判断现在该不该买</label>
        <label><input type="checkbox" name="goals" value="cheaper_date"> 找更便宜日期</label>
        <label><input type="checkbox" name="goals" value="best_overall" checked> 找综合最合适航班</label>
      </div>
    </fieldset>

    <button type="submit">开始监控</button>
  </form>

  <script>
    const radios = document.querySelectorAll('input[name="round_trip"]');
    const returnWrap = document.getElementById('return-date-wrap');
    function toggleReturnDate() {
      const selected = document.querySelector('input[name="round_trip"]:checked').value;
      returnWrap.style.display = selected === 'true' ? 'block' : 'none';
    }
    radios.forEach(radio => radio.addEventListener('change', toggleReturnDate));
    toggleReturnDate();
  </script>
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
  <style>
    body { font-family: Arial, sans-serif; max-width: 560px; margin: 32px auto; padding: 0 16px; line-height: 1.7; }
    .summary { background: #f7f9fc; border-radius: 8px; padding: 14px; }
  </style>
</head>
<body>
  <h1>订阅成功，将通过PushPlus推送监控结果</h1>
  <div class="summary">
    <p><b>订阅摘要</b></p>
    <p>{{ sub.origin }} → {{ sub.destination }}</p>
    <p>出发日期：{{ sub.depart_date }}</p>
    {% if sub.round_trip %}
    <p>返程日期：{{ sub.return_date }}</p>
    {% endif %}
    <p>预算上限：¥{{ sub.budget }}</p>
    <p>预计首次推送时间：{{ first_push_time }} 或下一次定时采集完成后</p>
  </div>
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


def parse_bool(value: str) -> bool:
    return value == "true"


def parse_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def first_push_text() -> str:
    next_time = datetime.now() + timedelta(minutes=10)
    return next_time.strftime("%Y-%m-%d %H:%M")


@app.get("/")
def index():
    return render_template_string(FORM_TEMPLATE, origins=COMMON_ORIGINS)


@app.post("/subscribe")
def subscribe():
    round_trip = parse_bool(request.form.get("round_trip", "false"))
    origin = (
        request.form.get("origin_manual", "").strip().upper()
        or request.form.get("origin_select", "").strip().upper()
    )
    subscription = {
        "origin": origin,
        "destination": normalize_destination(request.form.get("destination", "")),
        "depart_date": request.form.get("depart_date", "").strip(),
        "return_date": request.form.get("return_date", "").strip() if round_trip else None,
        "round_trip": round_trip,
        "date_flexibility": parse_int(request.form.get("date_flexibility"), 0),
        "budget": parse_int(request.form.get("budget"), 0),
        "direct_only": request.form.get("direct_only", "flexible"),
        "red_eye": request.form.get("red_eye", "reject"),
        "need_baggage": request.form.get("need_baggage", "unknown"),
        "trip_type": request.form.get("trip_type", "tourism"),
        "goals": request.form.getlist("goals"),
        "status": "active",
        "created_at": datetime.now().isoformat(),
    }
    save_subscription(subscription)
    return redirect(url_for("success", index=len(load_subscriptions()) - 1))


@app.get("/success")
def success():
    subscriptions = load_subscriptions()
    try:
        index = int(request.args.get("index", len(subscriptions) - 1))
    except ValueError:
        index = len(subscriptions) - 1
    subscription = subscriptions[index] if subscriptions else {}
    return render_template_string(
        SUCCESS_TEMPLATE,
        sub=subscription,
        first_push_time=first_push_text(),
    )
