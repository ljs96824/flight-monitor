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

CITY_LABELS = {
    "PVG": "上海PVG",
    "SHA": "上海SHA",
    "PEK": "北京PEK",
    "PKX": "北京PKX",
    "CAN": "广州CAN",
    "SZX": "深圳SZX",
    "CTU": "成都CTU",
    "HGH": "杭州HGH",
    "NKG": "南京NKG",
    "MCO": "奥兰多MCO",
    "LAX": "洛杉矶LAX",
    "JFK": "纽约JFK",
    "SFO": "旧金山SFO",
    "NRT": "东京NRT",
    "BKK": "曼谷BKK",
}

DATE_FLEX_LABELS = {
    0: "日期不灵活",
    1: "前后1天均可",
    3: "前后3天均可",
    7: "前后7天均可",
}

BUDGET_MODE_LABELS = {
    "fixed": "预算上限：¥{budget:,}",
    "unknown": "预算不确定，系统判断合理价格",
    "low_zone": "进入低价区间时提醒",
}

TRANSFER_LABELS = {
    "direct_only": "必须直飞",
    "short_ok": "可以短中转",
    "cheap_ok": "便宜很多可以中转",
    "price_first": "价格优先，中转也可以",
}

RED_EYE_LABELS = {
    "not_allowed": "不接受红眼航班",
    "allowed": "可以接受红眼航班",
    "cheap_ok": "便宜很多可以接受红眼航班",
}

BAGGAGE_LABELS = {
    "required": "必须托运行李",
    "not_needed": "不需要托运行李",
    "unknown": "托运行李不确定",
}

REFUND_LABELS = {
    "not_needed": "不需要退改签灵活性",
    "preferred": "最好可以改签",
    "required": "必须可退改",
    "unknown": "退改签需求不确定",
}

TRIP_TYPE_LABELS = {
    "business_meeting": "商务会议",
    "tourism": "旅游",
    "family_visit": "探亲",
    "student_return": "学生返校",
    "family_elder": "家庭老人同行",
    "other": "其他",
}

PRIMARY_GOAL_LABELS = {
    "price_drop_alert": "跌到合适价格时提醒我",
    "buy_timing": "判断现在该不该买",
    "cheaper_date": "帮我找更便宜的日期",
    "best_overall": "帮我找最合适航班",
}

PRIMARY_GOAL_SUMMARY = {
    "price_drop_alert": "跌到合适价格时提醒",
    "buy_timing": "判断现在是否值得买",
    "cheaper_date": "寻找更便宜日期",
    "best_overall": "寻找最合适航班",
}

SECONDARY_GOAL_LABELS = {
    "low_price_alert": "异常低价提醒",
    "price_risk_alert": "涨价风险提醒",
    "cheaper_date": "前后日期更便宜提醒",
    "better_same_day": "同日更优方案提醒",
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
      max-width: 600px;
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
      <legend>行程信息</legend>

      <label for="origin">出发地</label>
      <select id="origin" name="origin_select">
        {% for code, label in origins %}
        <option value="{{ code }}">{{ label }}</option>
        {% endfor %}
      </select>
      <input name="origin_manual" placeholder="或手动输入IATA代码，例如 PVG">

      <label for="destination">目的地</label>
      <input id="destination" name="destination" placeholder="例如 MCO 或 Orlando / 奥兰多" required>

      <label>单程 / 往返</label>
      <div class="choice">
        <label><input type="radio" name="round_trip" value="false" checked> 单程</label>
        <label><input type="radio" name="round_trip" value="true"> 往返</label>
      </div>

      <label for="depart_date">出发日期</label>
      <input id="depart_date" name="depart_date" type="date" required>

      <div id="return-date-wrap">
        <label for="return_date">返程日期</label>
        <input id="return_date" name="return_date" type="date">
      </div>
    </fieldset>

    <fieldset>
      <legend>行程弹性</legend>

      <label>日期灵活度</label>
      <div class="choice">
        <label><input type="radio" name="date_flexibility" value="0" checked> 不灵活</label>
        <label><input type="radio" name="date_flexibility" value="1"> 前后1天</label>
        <label><input type="radio" name="date_flexibility" value="3"> 前后3天</label>
        <label><input type="radio" name="date_flexibility" value="7"> 前后7天</label>
      </div>
    </fieldset>

    <fieldset>
      <legend>硬约束和偏好</legend>

      <label>预算上限</label>
      <input id="budget" name="budget" type="number" min="1" step="1" placeholder="例如 8000">
      <div class="choice">
        <label><input type="radio" name="budget_mode" value="fixed" checked> 输入具体金额</label>
        <label><input type="radio" name="budget_mode" value="unknown"> 不确定，帮我判断合理价格</label>
        <label><input type="radio" name="budget_mode" value="low_zone"> 只要进入低价区间就提醒我</label>
      </div>

      <label>中转接受程度</label>
      <div class="choice">
        <label><input type="radio" name="transfer_policy" value="direct_only"> 必须直飞</label>
        <label><input type="radio" name="transfer_policy" value="short_ok" checked> 可以短中转（总时长不能太长）</label>
        <label><input type="radio" name="transfer_policy" value="cheap_ok"> 便宜很多可以中转</label>
        <label><input type="radio" name="transfer_policy" value="price_first"> 价格优先，中转也可以</label>
      </div>

      <label>红眼/过早航班</label>
      <div class="choice">
        <label><input type="radio" name="red_eye_policy" value="not_allowed" checked> 不接受</label>
        <label><input type="radio" name="red_eye_policy" value="allowed"> 可以接受</label>
        <label><input type="radio" name="red_eye_policy" value="cheap_ok"> 便宜很多可以接受</label>
      </div>
      <div class="hint">红眼定义：起飞时间在23:00-06:00之间。</div>

      <label>是否需要托运行李</label>
      <div class="choice">
        <label><input type="radio" name="baggage" value="required" checked> 必须</label>
        <label><input type="radio" name="baggage" value="not_needed"> 不需要</label>
        <label><input type="radio" name="baggage" value="unknown"> 不确定</label>
      </div>

      <label>退改签灵活性</label>
      <div class="choice">
        <label><input type="radio" name="refund_flexibility" value="not_needed"> 不需要，确定会出行</label>
        <label><input type="radio" name="refund_flexibility" value="preferred" checked> 最好可以改签</label>
        <label><input type="radio" name="refund_flexibility" value="required"> 必须可退改</label>
        <label><input type="radio" name="refund_flexibility" value="unknown"> 不确定</label>
      </div>
    </fieldset>

    <fieldset>
      <legend>场景和目标</legend>

      <label>出行类型</label>
      <select name="trip_type">
        <option value="business_meeting">商务会议</option>
        <option value="tourism" selected>旅游</option>
        <option value="family_visit">探亲</option>
        <option value="student_return">学生返校</option>
        <option value="family_elder">家庭老人同行</option>
        <option value="other">其他</option>
      </select>

      <label>主目标（必填）</label>
      <div class="choice">
        <label><input type="radio" name="primary_goal" value="price_drop_alert" required> 跌到合适价格时提醒我</label>
        <label><input type="radio" name="primary_goal" value="buy_timing" checked required> 判断现在该不该买</label>
        <label><input type="radio" name="primary_goal" value="cheaper_date" required> 帮我找更便宜的日期</label>
        <label><input type="radio" name="primary_goal" value="best_overall" required> 帮我找最合适航班</label>
      </div>

      <label>附加关注（选填）</label>
      <div class="choice">
        <label><input type="checkbox" name="secondary_goals" value="low_price_alert"> 异常低价提醒</label>
        <label><input type="checkbox" name="secondary_goals" value="price_risk_alert"> 涨价风险提醒</label>
        <label><input type="checkbox" name="secondary_goals" value="cheaper_date"> 前后日期更便宜提醒</label>
        <label><input type="checkbox" name="secondary_goals" value="better_same_day"> 同日更优方案提醒</label>
      </div>
    </fieldset>

    <button type="submit">开始监控</button>
  </form>

  <script>
    const tripRadios = document.querySelectorAll('input[name="round_trip"]');
    const returnWrap = document.getElementById('return-date-wrap');
    const budgetRadios = document.querySelectorAll('input[name="budget_mode"]');
    const budgetInput = document.getElementById('budget');

    function toggleReturnDate() {
      const selected = document.querySelector('input[name="round_trip"]:checked').value;
      returnWrap.style.display = selected === 'true' ? 'block' : 'none';
    }

    function toggleBudgetRequired() {
      const selected = document.querySelector('input[name="budget_mode"]:checked').value;
      budgetInput.required = selected === 'fixed';
    }

    tripRadios.forEach(radio => radio.addEventListener('change', toggleReturnDate));
    budgetRadios.forEach(radio => radio.addEventListener('change', toggleBudgetRequired));
    toggleReturnDate();
    toggleBudgetRequired();
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
    body { font-family: Arial, sans-serif; max-width: 600px; margin: 32px auto; padding: 0 16px; line-height: 1.7; }
    .summary { background: #f7f9fc; border-radius: 8px; padding: 16px; }
    ul { padding-left: 22px; }
  </style>
</head>
<body>
  <h1>订阅成功，将通过PushPlus推送监控结果</h1>
  <div class="summary">
    <p><b>已开始监控：{{ summary.route }}</b></p>
    <p><b>核心约束：</b></p>
    <ul>
      {% for item in summary.constraints %}
      <li>{{ item }}</li>
      {% endfor %}
    </ul>
    <p>系统将重点：{{ summary.primary_goal }}</p>
    <p>附加关注：{{ summary.secondary_goals }}</p>
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


def city_label(code: str) -> str:
    code = (code or "").strip().upper()
    return CITY_LABELS.get(code, code)


def parse_bool(value: str) -> bool:
    return value == "true"


def parse_int(value: str | None, default: int = 0) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def parse_optional_budget(value: str | None, budget_mode: str) -> int | None:
    if budget_mode != "fixed":
        return parse_int(value, 0) or None
    return parse_int(value, 0)


def first_push_text() -> str:
    next_time = datetime.now() + timedelta(minutes=10)
    return next_time.strftime("%Y-%m-%d %H:%M")


def build_subscription(form) -> dict:
    round_trip = parse_bool(form.get("round_trip", "false"))
    origin = (
        form.get("origin_manual", "").strip().upper()
        or form.get("origin_select", "").strip().upper()
    )
    budget_mode = form.get("budget_mode", "fixed")
    return {
        "origin": origin,
        "destination": normalize_destination(form.get("destination", "")),
        "depart_date": form.get("depart_date", "").strip(),
        "return_date": form.get("return_date", "").strip() if round_trip else None,
        "round_trip": round_trip,
        "date_flexibility": parse_int(form.get("date_flexibility"), 0),
        "hard_constraints": {
            "budget": parse_optional_budget(form.get("budget"), budget_mode),
            "budget_mode": budget_mode,
            "transfer_policy": form.get("transfer_policy", "short_ok"),
            "red_eye_policy": form.get("red_eye_policy", "not_allowed"),
            "baggage": form.get("baggage", "required"),
            "refund_flexibility": form.get("refund_flexibility", "preferred"),
        },
        "soft_preferences": {
            "trip_type": form.get("trip_type", "tourism"),
        },
        "notification_goals": {
            "primary": form.get("primary_goal", "buy_timing"),
            "secondary": form.getlist("secondary_goals"),
        },
        "status": "active",
        "created_at": datetime.now().isoformat(),
    }


def build_summary(subscription: dict) -> dict:
    hard = subscription.get("hard_constraints", {})
    goals = subscription.get("notification_goals", {})
    budget_mode = hard.get("budget_mode", "fixed")
    budget = hard.get("budget")
    budget_text = BUDGET_MODE_LABELS.get(budget_mode, "预算设置未知").format(
        budget=budget or 0
    )
    secondary = [
        SECONDARY_GOAL_LABELS.get(goal, goal)
        for goal in goals.get("secondary", [])
    ]
    return {
        "route": f"{city_label(subscription.get('origin'))} → {city_label(subscription.get('destination'))}",
        "constraints": [
            DATE_FLEX_LABELS.get(subscription.get("date_flexibility", 0), "日期弹性未知"),
            budget_text,
            TRANSFER_LABELS.get(hard.get("transfer_policy"), "中转偏好未知"),
            RED_EYE_LABELS.get(hard.get("red_eye_policy"), "红眼偏好未知"),
            BAGGAGE_LABELS.get(hard.get("baggage"), "行李需求未知"),
            REFUND_LABELS.get(hard.get("refund_flexibility"), "退改签需求未知"),
        ],
        "primary_goal": PRIMARY_GOAL_SUMMARY.get(
            goals.get("primary"), goals.get("primary", "未设置")
        ),
        "secondary_goals": "、".join(secondary) if secondary else "无",
    }


@app.get("/")
def index():
    return render_template_string(FORM_TEMPLATE, origins=COMMON_ORIGINS)


@app.post("/subscribe")
def subscribe():
    subscription = build_subscription(request.form)
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
        summary=build_summary(subscription) if subscription else {},
        first_push_time=first_push_text(),
    )
