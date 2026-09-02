"""Flask form for flight monitor subscriptions."""

from __future__ import annotations

# 本笔关闭的是仓库自带的 direct script 与 module execution 启动旁路;操作者仍可用 flask CLI / Gunicorn / Waitress 等外部 WSGI 方式自行绑定地址,该行为不在本笔控制范围内,须作为 residual risk 明列。
if __name__ == "__main__":
    raise SystemExit(
        "请使用 python run_web.py 启动本地 Web 服务；"
        "run_web.py 提供回环默认绑定与显式公网授权门。"
    )

import json
import os
import queue
import re
import threading
import traceback
from datetime import datetime, timedelta, timezone
import html
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template_string, request, session, url_for
from atomic_json_store import read_json, update_json

from airports import (
    AIRPORTS,
    CITY_AIRPORTS,
    CITY_ALIASES,
    EXACT_LOCATION_AIRPORTS,
    format_airport,
    location_error_message,
    resolve_location,
)
from analyzer import apply_default_rules, build_price_hint_from_calendar
from airlines import LCC_POLICIES, canonicalize_airline_lcc_policy, resolve_lcc_policy
from build_info import PROCESS_BUILD_INFO
from cabin_allocation import (
    cabin_allocation_detail_label,
    cabin_allocation_from_form,
    validate_cabin_allocation,
)
from companion_constraints import derive_companion_constraints
from constraint_summary import build_constraint_summary, format_constraint_summary
from form_pages import FORM_PAGE_TEMPLATE, ROUTE_TYPE_LABELS, build_form_page_context
from form_structure import (
    build_default_chips,
    derive_time_concept_fields,
    subscription_to_form_values,
    summarize_optional_sections,
    summarize_stations,
)
from pricing import passenger_rate_sum

from detail_access import canonical_detail_uuid, detail_token_authorized
from log_utils import safe_log
from subscription_identity import subscription_id as stable_subscription_id
from subscription_repository import (
    LOCAL_OWNER_ID,
    DuplicateSubscriptionIdError,
    SubscriptionIdentityMigrationRequired,
    SubscriptionRepository,
)
from subscription_attempts import attempt_time as _attempt_time, record_subscription_attempt
from notification_config import (
    DEFAULT_NOTIFICATION_METHOD,
    DEFAULT_NOTIFICATION_PRIVACY_LEVEL,
    normalize_notification_goals,
)
from price_calendar import load_calendar
from project_time import SHANGHAI_TZ
from web_security import configure_session_security, install_csrf_protection


BASE_DIR = Path(__file__).parent
SUBSCRIPTIONS_PATH = BASE_DIR / "data" / "subscriptions.json"
FEEDBACK_PATH = BASE_DIR / "data" / "feedback.json"
PAGE_PAYLOADS_DIR = BASE_DIR / "data" / "payloads"
load_dotenv(BASE_DIR / ".env", encoding="utf-8")

app = Flask(__name__)
configure_session_security(app, logger=safe_log)
install_csrf_protection(app, logger=safe_log)
app.config.setdefault(
    "COLLECTION_STARTUP_TIMEOUT_SECONDS",
    float(os.environ.get("COLLECTION_STARTUP_TIMEOUT_SECONDS") or 3.0),
)

STARTUP_HANDSHAKE_SESSION_KEY = "_collection_startup_handshakes"
LEGACY_INDEX_EDIT_COMPATIBILITY = "M0_REMOVE_IN_M1"
ALLOWED_STARTUP_STATUSES = {
    "started",
    "busy",
    "startup_error",
    "confirming",
    "success",
    "failed",
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
    0: "不能调，就这天",
    1: "前后1天可以",
    3: "前后3天都行",
    7: "前后一周都行",
}

BUDGET_MODE_LABELS = {
    "fixed": "输入具体金额",
    "none": "不确定，帮我判断",
    "unknown": "不确定，帮我判断合理价格",
    "low_zone": "没有明确预算，进入低价区间时提醒",
}

TRANSFER_LABELS = {
    "direct_only": "必须直飞",
    "reasonable": "可以接受合理中转",
    "short_ok": "可以中转，但总耗时别太长",
    "cheap_ok": "便宜很多的话可以中转",
    "price_first": "价格优先，中转也可以",
}

DEPARTURE_TIME_LABELS = {
    "any": "不限制",
    "no_redeye": "不接受红眼凌晨",
    "daytime": "希望白天出行",
}

ARRIVAL_TIME_LABELS = {
    "any": "不限制",
    "no_midnight": "不接受凌晨到达",
    "daytime_only": "必须白天到达",
}

GREATER_CHINA_AIRPORTS = {"HKG", "MFM", "TPE", "TSA"}
DOMESTIC_ROUTE_AIRPORTS = {
    "PVG", "SHA", "PEK", "PKX", "CAN", "SZX", "CTU", "TFU", "HGH", "NKG",
    "XIY", "CKG", "WUH", "CSX", "TAO", "XMN", "FOC", "KMG", "SHE", "DLC",
    "TSN", "CGO", "URC", "HRB",
}

TIME_SLOT_LABELS = {
    "dawn": "清晨 06:00-09:00",
    "morning": "早上 09:00-12:00",
    "noon": "中午 12:00-14:00",
    "afternoon": "下午 14:00-17:00",
    "evening": "傍晚 17:00-20:00",
    "night": "晚上 20:00-23:00",
    "redeye": "凌晨/红眼 23:00-06:00",
}

ARRIVAL_SLOT_LABELS = {
    "dawn": "清晨 06:00-09:00",
    "morning": "早上 09:00-12:00",
    "noon": "中午 12:00-14:00",
    "afternoon": "下午 14:00-17:00",
    "evening": "傍晚 17:00-20:00",
    "night": "晚上 20:00-23:00",
    "redeye": "凌晨/红眼 23:00-06:00",
}

TIME_SEGMENTS = {
    "dawn": ["06:00", "09:00"],
    "morning": ["09:00", "12:00"],
    "noon": ["12:00", "14:00"],
    "afternoon": ["14:00", "17:00"],
    "evening": ["17:00", "20:00"],
    "night": ["20:00", "23:00"],
    "redeye": ["23:00", "06:00"],
}

DEFAULT_DEPARTURE_SLOTS = ["dawn", "morning", "noon", "afternoon", "evening", "night"]
DEFAULT_ARRIVAL_SLOTS = ["dawn", "morning", "noon", "afternoon", "evening", "night"]
ALL_TIME_SLOTS = ["dawn", "morning", "noon", "afternoon", "evening", "night", "redeye"]
DAYTIME_TIME_SLOTS = ["dawn", "morning", "noon", "afternoon", "evening"]

BAGGAGE_LABELS = {
    "required": "必须托运行李",
    "not_needed": "不需要托运行李",
    "unknown": "不确定",
}

REFUND_LABELS = {
    "not_needed": "不重要，便宜优先",
    "preferred": "最好能改签日期",
    "required": "必须能退票或改签",
    "unknown": "不确定",
}

TRIP_TYPE_LABELS = {
    "business_meeting": "商务会议",
    "tourism": "旅游",
    "family_visit": "探亲",
    "student_return": "学生返校",
    "family_elder": "家庭老人同行",
    "other": "其他",
}

COMPANION_LABELS = {
    "solo": "仅本人",
    "couple_friends": "情侣/朋友",
    "with_child": "有儿童",
    "with_elderly": "有老人",
    "with_elderly_child": "老人和儿童都有",
    "group": "多人同行",
}

TRAVEL_SCENARIO_LABELS = {
    "personal": "个人出行",
    "business": "商务/会议",
    "tourism": "旅游",
    "family_visit": "探亲/回家",
    "family": "家庭/亲子",
    "elderly": "有老人同行",
    "important": "重要事项",
    "price_first": "价格优先",
}

COMPANION_CONSTRAINT_LABELS = {
    "direct_preferred": "需要尽量直飞",
    "no_redeye": "不接受红眼/凌晨到达",
    "avoid_long_layover": "不适合长时间中转",
    "need_baggage": "需要托运行李",
    "need_refund_change": "需要可退改",
    "daytime_arrival": "希望白天到达",
    "limited_mobility": "有行动不便，不适合长时间步行/换乘",
}

PRICE_SENSITIVITY_LABELS = {
    "low": "不接受明显不方便，时间和稳定更重要",
    "medium": "便宜200元左右，可以接受早晚班",
    "high": "便宜500元以上，可以接受中转或更长耗时",
    "max": "价格优先，只要显著便宜都可以看",
}

AIRLINE_POLICY_LABELS = {
    "any": "不限制",
    "prefer_full_service": "偏好全服务航司",
    "no_lcc": "不接受廉航",
    "exclude_airlines": "排除指定航司",
}

TRIP_RIGIDITY_LABELS = {
    "confirmed": "铁定出发，不会变",
    "mostly": "可能微调日期",
    "flexible": "不太确定，有可能取消",
}

PRIMARY_GOAL_LABELS = {
    "price_drop_alert": "找到合适价格时提醒我",
    "buy_timing": "判断现在该不该买",
    "cheaper_date": "帮我找更便宜的日期",
    "best_overall": "帮我找最合适航班",
}

SECONDARY_GOAL_LABELS = {
    "low_price_alert": "异常低价提醒",
    "price_risk_alert": "涨价风险提醒",
    "cheaper_date": "前后日期更便宜提醒",
    "better_same_day": "同日更优方案提醒",
}

GOAL_TO_ALERTS = {
    "price_alert": ["low_price_alert", "price_risk_alert"],
    "price_drop_alert": ["low_price_alert", "price_risk_alert"],
    "buy_timing": ["price_risk_alert", "better_same_day"],
    "cheaper_date": ["cheaper_date"],
    "best_overall": ["better_same_day"],
}


FORM_TEMPLATE = FORM_PAGE_TEMPLATE

SUCCESS_TEMPLATE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>已创建监控</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 620px; margin: 32px auto; padding: 0 16px; line-height: 1.7; color: #222; }
    .card { background: #f7f9fc; border: 1px solid #dbe5f6; border-radius: 8px; padding: 18px; }
    ul { padding-left: 22px; }
    a { display: inline-block; margin-top: 18px; color: #1a73e8; font-weight: bold; }
    .quick-actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 4px; }
    .quick-actions form { margin: 0; }
    .quick-actions button {
      border: 1px solid #c8d6f0;
      border-radius: 999px;
      background: #fff;
      color: #1a73e8;
      padding: 8px 12px;
      cursor: pointer;
    }
    .secondary-link { margin-left: 12px; color: #555; font-weight: normal; }
  </style>
</head>
<body>
  <div class="card">
    <h1>✅ 监控已创建: {{ summary.route }}</h1>
    {% if startup_status == "started" %}
    <p><b>接下来系统会:</b></p>
    <ol>
      <li>立即进行第一次采集和购买判断(约30秒-1分钟)</li>
      <li data-confirmed-notification="true">结果将推送到: {{ summary.notification_text or "你的邮箱 / PushPlus微信" }}</li>
      <li>之后发现低价、涨价风险或更优方案时自动提醒</li>
      <li>可随时在「我的监控」暂停或修改</li>
    </ol>
    <p>💡 第一次判断稍后到达,你可以关掉此页,留意邮箱/微信推送。</p>
    {% else %}
    <p data-startup-status="{{ startup_status }}"><b>{{ startup_message }}</b></p>
    <p data-confirmed-notification="true">结果提醒渠道：{{ summary.notification_text or "你的邮箱 / PushPlus微信" }}</p>
    <p>订阅已保留；你可以关掉此页，系统会按后续轮次继续处理。</p>
    {% endif %}
    <p><a href="{{ url_for('subscription_list') }}">查看我的所有监控</a> <a class="secondary-link" href="{{ url_for('index') }}">再创建一个</a></p>
    <p><b>{{ summary.route }}</b></p>
    {% if summary.scenario_text %}<p data-confirmed-scenarios="true"><b>出行场景：</b>{{ summary.scenario_text }}</p>{% endif %}
    {% if summary.companion_constraints_text %}<p data-confirmed-companion-constraints="true"><b>同行约束：</b>{{ summary.companion_constraints_text }}</p>{% endif %}
    {% if summary.cabin_text %}<p data-confirmed-cabin-allocation="true"><b>舱位安排：</b>{{ summary.cabin_text }}</p>{% endif %}
    {% if summary.meeting_text %}<p data-confirmed-meeting="true">{{ summary.meeting_text }}</p>{% endif %}
    {% if summary.time_window_text %}<p data-confirmed-time-windows="true">{{ summary.time_window_text }}</p>{% endif %}
    {% if summary.transfer_text %}<p data-confirmed-transfer="true">{{ summary.transfer_text }}</p>{% endif %}
    {% if summary.airport_coverage %}
    <p>{{ summary.airport_coverage }}</p>
    {% endif %}

    {% if summary.defaults_applied %}
    <p><b>系统默认规则：</b></p>
    <ul>
      {% for item in summary.defaults_applied %}
      <li>{{ item }}</li>
      {% endfor %}
    </ul>
    {% endif %}

    <p><b>系统会在以下情况提醒你：</b></p>
    <ul>
      {% for item in summary.reminders %}
      <li>{{ item }}</li>
      {% endfor %}
    </ul>

    <p><b>以下方案不会推荐给你：</b></p>
    <ul>
      {% for item in summary.exclusions %}
      <li>{{ item }}</li>
      {% endfor %}
    </ul>

    {% if startup_status == "started" %}<p><b>{{ startup_message }}</b></p>{% endif %}
  </div>
  <a href="{{ url_for('index') }}">继续添加订阅</a>
  <a class="secondary-link" href="{{ url_for('subscription_list') }}">查看我的所有监控 →</a>
  {% if subscription_id %}
  <div class="card" style="margin-top:16px;">
    <p><b>想让推荐更准确？你还可以补充（可选）：</b></p>
    <p>这些不影响监控运行，只让推荐排序更贴合你的需求。</p>
    <div class="quick-actions">
      <form method="post" action="{{ url_for('quick_update_subscription', subscription_id=subscription_id) }}">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="field" value="time_preference">
        <input type="hidden" name="value" value="no_redeye">
        <button type="submit">不接受红眼</button>
      </form>
      <form method="post" action="{{ url_for('quick_update_subscription', subscription_id=subscription_id) }}">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="field" value="airline_policy">
        <input type="hidden" name="value" value="prefer_full_service">
        <button type="submit">偏好全服务航司</button>
      </form>
      <form method="post" action="{{ url_for('quick_update_subscription', subscription_id=subscription_id) }}">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="field" value="accept_self_transfer">
        <input type="hidden" name="value" value="false">
        <button type="submit">不接受非联程</button>
      </form>
      <form method="post" action="{{ url_for('quick_update_subscription', subscription_id=subscription_id) }}">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="field" value="refund_flexibility">
        <input type="hidden" name="value" value="preferred">
        <button type="submit">最好可改签</button>
      </form>
    </div>
  </div>
  <a class="secondary-link" href="{{ url_for('index', edit=subscription_id) }}">修改这条监控的偏好</a>
  {% endif %}
</body>
</html>
"""


LIST_TEMPLATE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>我的航班监控</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 760px; margin: 28px auto; padding: 0 16px 40px; color: #222; line-height: 1.6; background: #fff; }
    .topbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 18px; }
    h1 { margin: 0; font-size: 24px; }
    .new-link, .button-link, button { border: 1px solid #c8d6f0; border-radius: 8px; background: #f7f9fc; color: #1a73e8; padding: 8px 12px; font-weight: 700; text-decoration: none; cursor: pointer; }
    .new-link { background: #1a73e8; color: #fff; border-color: #1a73e8; }
    .card { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 16px; margin: 14px 0; }
    .route { font-size: 17px; font-weight: 700; margin-bottom: 6px; }
    .meta, .decision { color: #555; font-size: 14px; margin: 4px 0; }
    .status { display: inline-flex; align-items: center; gap: 5px; font-weight: 700; }
    .status.active { color: #188038; }
    .status.paused { color: #777; }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
    .actions form { margin: 0; }
    .danger { color: #b91c1c; border-color: #fecaca; background: #fff7f7; }
    .empty { border: 1px dashed #c8d6f0; border-radius: 10px; padding: 24px; background: #f7f9fc; text-align: center; }
    @media (max-width: 560px) {
      .topbar { align-items: stretch; flex-direction: column; }
      .actions { flex-direction: column; }
      .actions a, .actions button { width: 100%; box-sizing: border-box; text-align: center; }
    }
  </style>
</head>
<body>
  <div class="topbar">
    <h1>我的航班监控</h1>
    <a class="new-link" href="{{ url_for('index') }}">+ 新建监控</a>
  </div>

  {% if not items %}
  <div class="empty">
    <p><b>还没有监控。</b></p>
    <p>比如：监控上海→北京，低于¥800提醒我。</p>
    <a class="new-link" href="{{ url_for('index') }}">创建第一个监控</a>
  </div>
  {% endif %}

  {% for item in items %}
  <div class="card">
    <div class="route">{{ item.route }} · {{ item.route_type_label }} · {{ item.trip }}</div>
    <div class="meta">{{ item.dates }} · 出行场景: {{ item.scenario }}</div>
    <div class="meta">
      状态:
      <span class="status {{ item.status }}">
        {{ "🟢 监控中" if item.status == "active" else "⏸ 已暂停" }}
      </span>
    </div>
    <div class="decision">最近判断: {{ item.last_decision }}</div>
    <div class="actions">
      <a class="button-link" href="{{ item.detail_url }}">查看详情</a>
      {% if item.subscription_id %}<a class="button-link" href="{{ url_for('index', edit=item.subscription_id) }}">编辑</a>{% endif %}
      {% if item.subscription_id %}<form method="post" action="{{ url_for('toggle_subscription', subscription_id=item.subscription_id) }}">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <button type="submit">{{ "暂停" if item.status == "active" else "恢复" }}</button>
      </form>{% endif %}
      {% if item.delete_url %}<a class="button-link danger" href="{{ item.delete_url }}">删除</a>{% endif %}
    </div>
  </div>
  {% endfor %}
</body>
</html>
"""


DELETE_CONFIRM_TEMPLATE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>确认删除监控</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 620px; margin: 32px auto; padding: 0 16px; line-height: 1.7; color: #222; }
    .card { border: 1px solid #fecaca; border-radius: 8px; padding: 18px; background: #fff7f7; }
    .actions { display: flex; gap: 10px; align-items: center; }
    button, a { padding: 9px 14px; border-radius: 8px; text-decoration: none; }
    button { border: 1px solid #b91c1c; background: #b91c1c; color: #fff; cursor: pointer; }
    a { border: 1px solid #c8d6f0; color: #1a73e8; }
    .error { color: #b91c1c; font-weight: 700; }
  </style>
</head>
<body>
  <div class="card">
    <h1>确认删除这条监控？</h1>
    <p>{{ route }}</p>
    {% if error %}<p class="error" role="alert">{{ error }}</p>{% endif %}
    <div class="actions">
      <form method="post">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="confirm_delete" value="yes">
        <button type="submit">确认删除</button>
      </form>
      <a href="{{ url_for('subscription_list') }}">取消</a>
    </div>
  </div>
</body>
</html>
"""


FEEDBACK_TEMPLATE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>提醒反馈</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 620px; margin: 32px auto; padding: 0 16px; line-height: 1.7; color: #222; }
    .card { background: #f7f9fc; border: 1px solid #dbe5f6; border-radius: 8px; padding: 18px; }
    label { display: block; margin: 10px 0; }
    textarea { width: 100%; min-height: 90px; box-sizing: border-box; }
    button { width: 100%; padding: 12px; border: 0; border-radius: 8px; background: #1a73e8; color: white; font-size: 16px; margin-top: 12px; }
    select, input[type=text] { width: 100%; padding: 10px; box-sizing: border-box; }
  </style>
</head>
<body>
  <div class="card">
    <h1>{{ "✅ 已收到反馈" if saved else "这条提醒有用吗？" }}</h1>
    {% if saved %}
      <p>感谢反馈。系统会在下次采集时重新核实这条，并在推送中告知你核实结果。</p>
      <a href="{{ url_for('subscription_list') }}">返回我的监控</a>
    {% else %}
      <form method="post">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="subscription_id" value="{{ subscription_id }}">
        <p>订阅：{{ subscription_id or "未指定" }}</p>
        <label><input type="radio" name="feedback_type" value="useful" required> 有用</label>
        <label><input type="radio" name="feedback_type" value="price_changed"> 价格变了</label>
        <label><input type="radio" name="feedback_type" value="unavailable"> 买不到（无票）</label>
        <label><input type="radio" name="feedback_type" value="no_baggage"> 不含行李</label>
        <label><input type="radio" name="feedback_type" value="link_failed"> 跳转失败</label>
        <label><input type="radio" name="feedback_type" value="mismatch"> 不符合需求</label>
        <label><input type="radio" name="feedback_type" value="mute_similar"> 不想再提醒这类</label>

        <label>
          如果是“买不到”，具体原因：
          <select name="unavailable_reason">
            <option value="">请选择（可选）</option>
            <option value="price_changed">价格变化</option>
            <option value="sold_out">无票</option>
            <option value="no_baggage">不含行李</option>
            <option value="link_failed">跳转失败</option>
            <option value="payment_failed">支付失败</option>
            <option value="fare_rule_mismatch">票规不一致</option>
          </select>
        </label>
        <label>
          补充说明（可选）：
          <textarea name="comment" placeholder="比如实际支付页价格、平台名称、哪里不符合需求"></textarea>
        </label>
        <button type="submit">提交反馈</button>
      </form>
    {% endif %}
  </div>
</body>
</html>
"""


def _subscription_route_label(sub: dict) -> str:
    basic = sub.get("basic") if isinstance(sub.get("basic"), dict) else {}
    return f"{basic.get('origin') or sub.get('origin') or ''}->{basic.get('destination') or sub.get('destination') or ''}"


def migrate_budget_scopes(subscriptions: list[dict]) -> tuple[list[dict], list[dict]]:
    """Fill explicit price-scope fields for legacy subscriptions.

    Missing scope is intentionally migrated to per_person because historical
    budget_scope semantics were inconsistent across quick and precise modes.
    """
    migrated = []
    for index, sub in enumerate(subscriptions):
        if not isinstance(sub, dict):
            continue
        containers = [sub]
        for key in ("constraints", "hard_constraints", "soft_preferences", "preferences"):
            section = sub.get(key)
            if isinstance(section, dict):
                containers.append(section)
        existing = next(
            (
                container.get("max_budget_scope")
                for container in containers
                if container.get("max_budget_scope")
            ),
            None,
        )
        if existing:
            normalized_max = normalize_price_scope(existing)
            normalized_target = normalize_price_scope(
                next(
                    (
                        container.get("target_price_scope")
                        for container in containers
                        if container.get("target_price_scope")
                    ),
                    normalized_max,
                )
            )
        else:
            normalized_max = "per_person"
            normalized_target = "per_person"
            migrated.append({
                "index": index,
                "id": sub.get("id") or sub.get("subscription_id") or "",
                "route": _subscription_route_label(sub),
            })
        for container in containers:
            container["budget_scope"] = normalized_max
            container["max_budget_scope"] = normalized_max
            container["target_price_scope"] = normalized_target
    return subscriptions, migrated


def migrate_lcc_policies(subscriptions: list[dict]) -> tuple[list[dict], list[dict]]:
    """补齐廉航策略，并把旧no_lcc别名归一到lcc_policy。"""
    migrated = []
    for index, sub in enumerate(subscriptions):
        if not isinstance(sub, dict):
            continue

        soft = sub.get("soft_preferences")
        soft = soft if isinstance(soft, dict) else {}
        hard = sub.get("hard_constraints")
        hard = hard if isinstance(hard, dict) else {}
        constraints = sub.get("constraints")
        constraints = constraints if isinstance(constraints, dict) else {}
        preferences = sub.get("preferences")
        preferences = preferences if isinstance(preferences, dict) else {}
        advanced = sub.get("advanced_rules")
        advanced = advanced if isinstance(advanced, dict) else {}
        airline_rules = advanced.get("airlines")
        airline_rules = airline_rules if isinstance(airline_rules, dict) else {}

        legacy_airline_policy = (
            soft.get("airline_policy")
            or hard.get("airline_policy")
            or constraints.get("airline_policy")
            or airline_rules.get("preference")
            or sub.get("airline_policy")
            or "any"
        )
        existing_lcc_policy = resolve_lcc_policy(sub)
        airline_policy, lcc_policy, legacy_alias_migrated = (
            canonicalize_airline_lcc_policy(
                legacy_airline_policy,
                existing_lcc_policy or "any",
            )
        )
        changed = not existing_lcc_policy or sub.get("lcc_policy") != lcc_policy
        sub["lcc_policy"] = lcc_policy

        if legacy_alias_migrated:
            for container in (sub, soft, hard, constraints):
                if container.get("airline_policy") == "no_lcc":
                    container["airline_policy"] = airline_policy
                    changed = True
            if airline_rules.get("preference") == "no_lcc":
                airline_rules["preference"] = airline_policy
                changed = True

            for container in (hard, constraints, preferences):
                if "lcc_policy" in container:
                    if container.get("lcc_policy") != lcc_policy:
                        changed = True
                    container["lcc_policy"] = lcc_policy
            if airline_rules:
                if airline_rules.get("lcc_policy") != lcc_policy:
                    changed = True
                airline_rules["lcc_policy"] = lcc_policy

        if changed:
            migrated.append(
                {
                    "index": index,
                    "id": sub.get("id") or sub.get("subscription_id") or "",
                    "route": _subscription_route_label(sub),
                    "airline_policy": airline_policy,
                    "lcc_policy": lcc_policy,
                }
            )
    return subscriptions, migrated

def migrate_subscription_defaults(subscriptions: list[dict]) -> tuple[list[dict], dict]:
    """在内存中补齐当前读取路径支持的订阅默认字段。"""

    subscriptions, budget_migrated = migrate_budget_scopes(subscriptions)
    subscriptions, lcc_migrated = migrate_lcc_policies(subscriptions)
    return subscriptions, {
        "budget_scopes": budget_migrated,
        "lcc_policies": lcc_migrated,
    }


def _subscription_list(payload, *, allow_missing: bool = False) -> list[dict]:
    if payload is None and allow_missing:
        return []
    if not isinstance(payload, list):
        raise ValueError("subscriptions.json 格式错误，应为订阅数组")
    return payload


def _migrated_subscription_list(payload, *, allow_missing: bool = False) -> list[dict]:
    subscriptions = _subscription_list(payload, allow_missing=allow_missing)
    subscriptions, _details = migrate_subscription_defaults(subscriptions)
    return subscriptions



def load_subscriptions() -> list[dict]:
    data = _subscription_repository().list_for_owner(LOCAL_OWNER_ID)
    data, migration = migrate_subscription_defaults(data)
    budget_migrated = migration["budget_scopes"]
    lcc_migrated = migration["lcc_policies"]
    if budget_migrated:
        print(
            f"[预算口径迁移] 内存补齐{len(budget_migrated)}条旧订阅"
            f"默认scope=per_person(文件未写入): {budget_migrated}"
        )
    if lcc_migrated:
        safe_log(
            f"[口径迁移] 内存归一{len(lcc_migrated)}条旧订阅的廉航策略"
            f"(文件未写入): {lcc_migrated}"
        )
    return data


def _subscription_repository() -> SubscriptionRepository:
    """Bind the repository to the currently configured server-side path."""

    return SubscriptionRepository(SUBSCRIPTIONS_PATH)


def _subscription_position(subscription_id_value: str) -> int | None:
    """Return the legacy read-only position used by downstream payloads."""

    for index, item in enumerate(
        _subscription_repository().list_for_owner(LOCAL_OWNER_ID)
    ):
        if stable_subscription_id(item) == subscription_id_value:
            return index
    return None


def _resolve_edit_subscription_id(
    reference,
    *,
    log_legacy: bool = True,
) -> str | None:
    """Resolve the M0 numeric edit compatibility input to a stable identity."""

    value = str(reference or "").strip()
    if not value:
        return None
    if not value.isdigit():
        return value
    index = int(value)
    subscription = _subscription_repository().resolve_legacy_index(
        LOCAL_OWNER_ID,
        index,
    )
    if subscription is None:
        return None
    resolved = stable_subscription_id(subscription)
    if resolved and log_legacy:
        safe_log(
            "[订阅编辑迁移] "
            f"M0兼容 index={index} subscription_id={resolved}; M1删除该入口"
        )
    return resolved or None


def _require_persisted_subscription_identities(
    subscriptions: list[dict],
) -> list[dict]:
    """Fail closed when the deployment preflight migration was skipped."""

    missing = [
        index
        for index, subscription in enumerate(subscriptions)
        if not isinstance(subscription, dict)
        or not str(subscription.get("subscription_id") or "").strip()
    ]
    if missing:
        safe_log(
            "[身份迁移] 阻断读取 "
            f"缺少subscription_id数量={len(missing)} indexes={missing}"
        )
        raise SubscriptionIdentityMigrationRequired(
            "订阅身份迁移未完成；"
            "请先运行 scripts/migrate_subscription_ids.py --write"
        )
    return subscriptions


def _identity_migration_required_response(
    error: SubscriptionIdentityMigrationRequired,
):
    safe_log(f"[身份迁移] {error}")
    return (
        "订阅身份迁移未完成。请先运行 "
        "scripts/migrate_subscription_ids.py --write，再重试。",
        503,
    )


@app.errorhandler(SubscriptionIdentityMigrationRequired)
def _handle_identity_migration_required(
    error: SubscriptionIdentityMigrationRequired,
):
    return _identity_migration_required_response(error)


def _duplicate_subscription_id_response(error: DuplicateSubscriptionIdError):
    operation = str(request.endpoint or "unknown")
    safe_log(
        "[订阅身份冲突] "
        f"operation={operation} id={error.masked_id} "
        f"indexes=[{error.first_index}, {error.second_index}]"
    )
    return (
        "订阅身份状态异常，当前操作已安全阻断。"
        "请先运行只读订阅身份审计并人工处理重复记录。",
        503,
    )


@app.errorhandler(DuplicateSubscriptionIdError)
def _handle_duplicate_subscription_id(error: DuplicateSubscriptionIdError):
    return _duplicate_subscription_id_response(error)


def load_feedback() -> list[dict]:
    if not FEEDBACK_PATH.exists():
        return []
    data = read_json(FEEDBACK_PATH)
    if not isinstance(data, list):
        raise ValueError("feedback.json 格式错误，应为反馈数组")
    return data


def save_feedback(record: dict) -> None:
    def mutate(payload):
        if payload is None:
            records = []
        elif isinstance(payload, list):
            records = payload
        else:
            raise ValueError("feedback.json 格式错误，应为反馈数组")
        records.append(record)
        return records

    update_json(FEEDBACK_PATH, mutate)


def notify_feedback_author(record: dict) -> bool:
    """Send a best-effort feedback notification to the configured author email."""
    author_email = os.environ.get("FEEDBACK_NOTIFY_EMAIL", "").strip()
    if not author_email:
        return False
    subject = f"[航班监控反馈] {record.get('feedback_type', '')} - 订阅{record.get('subscription_id', '')}"
    body = "\n".join(
        [
            "收到一条用户反馈:",
            "",
            f"订阅ID:{record.get('subscription_id', '')}",
            f"反馈类型:{record.get('feedback_type', '')}",
            f"买不到原因:{record.get('unavailable_reason', '')}",
            f"补充说明:{record.get('comment', '')}",
            f"时间:{record.get('created_at', '')}",
            f"User-Agent:{record.get('user_agent', '')}",
        ]
    )
    html_body = "<br>".join(html.escape(line) for line in body.splitlines())
    try:
        from email_notifier import send_email

        ok = bool(send_email(author_email, subject, html_body, {}))
        if ok:
            safe_log(f"[反馈] 已发送到作者邮箱 {author_email}")
        else:
            safe_log("[反馈] 邮件发送失败(已存本地): send_email返回False")
        return ok
    except Exception as exc:
        safe_log(f"[反馈] 邮件发送失败(已存本地): {exc}")
        return False


def _load_payload_result(subscription_id: str) -> dict | None:
    canonical_id = canonical_detail_uuid(subscription_id)
    if canonical_id is None:
        return None
    path = PAGE_PAYLOADS_DIR / f"{canonical_id}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


ROUTE_TYPE_LABELS = {
    "domestic": "国内",
    "international": "国际",
    "greater_china": "港澳台",
}

SCENARIO_LABELS = {
    "personal": "个人出行",
    "business": "商务/会议",
    "tourism": "旅游",
    "family_visit": "探亲/回家",
    "visit_family": "探亲/回家",
    "family": "家庭/亲子",
    "elderly": "有老人同行",
    "with_elderly": "有老人同行",
    "important": "重要事项",
    "price_first": "价格优先",
}


def _sub_value(sub: dict, key: str, default=""):
    basic = sub.get("basic") or {}
    return basic.get(key) or sub.get(key) or default


def _subscription_route_text(sub: dict) -> str:
    origin = _sub_value(sub, "origin", "未设置")
    destination = _sub_value(sub, "destination", "未设置")
    return f"{origin} → {destination}"


def _subscription_dates_text(sub: dict) -> str:
    depart = _sub_value(sub, "departure_date") or sub.get("depart_date") or "未设置日期"
    return_date = _sub_value(sub, "return_date") or sub.get("return_date")
    if return_date:
        return f"{depart} 出发 · {return_date} 返回"
    return f"{depart} 出发"


def _subscription_scenario_text(sub: dict) -> str:
    soft = sub.get("soft_preferences") or {}
    prefs = sub.get("preferences") or {}
    scenarios = soft.get("travel_scenarios") or prefs.get("travel_scenarios") or sub.get("travel_scenarios")
    if isinstance(scenarios, str):
        scenarios = [item.strip() for item in scenarios.split(",") if item.strip()]
    if not scenarios:
        scenario = soft.get("travel_scenario") or prefs.get("travel_scenario") or sub.get("travel_scenario")
        scenarios = [scenario] if scenario else []
    labels = [SCENARIO_LABELS.get(str(item), str(item)) for item in scenarios if item]
    return " + ".join(labels) if labels else "未设置"


def _parse_display_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.astimezone(timezone.utc)


def _relative_time_label(value: str, *, now: datetime | None = None) -> str:
    if not value:
        return ""
    try:
        created = _parse_display_time(value)
    except ValueError:
        return str(value)
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    minutes = max(0, int((current - created).total_seconds() // 60))
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return f"{minutes}分钟前"
    if minutes < 24 * 60:
        return f"{minutes // 60}小时前"
    return f"{minutes // (24 * 60)}天前"


def _subscription_decision_text(value) -> str:
    if isinstance(value, dict):
        return str(value.get("conclusion") or value.get("label") or "")
    return str(value or "")


def _subscription_last_decision(sub: dict, index: int) -> str:
    subscription_id = stable_subscription_id(sub) or str(index)
    record = _load_payload_result(subscription_id) or {}
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
    if not isinstance(payload, dict) or not payload:
        return "暂无"
    decision = _subscription_decision_text(
        payload.get("execution_advice")
        or payload.get("recommendation")
        or payload.get("push_type")
        or "已生成"
    )
    price = payload.get("current_price") or payload.get("display_price") or payload.get("price")
    price_text = f"(¥{int(price):,})" if isinstance(price, (int, float)) and price else ""
    time_text = _relative_time_label(str(record.get("created_at") or payload.get("created_at") or ""))
    return f"{decision}{price_text}" + (f" · {time_text}" if time_text else "")


def build_subscription_list_items(subscriptions: list[dict]) -> list[dict]:
    items = []
    for index, sub in enumerate(subscriptions):
        route_type = _sub_value(sub, "route_type", "domestic")
        round_trip = bool(sub.get("round_trip") or _sub_value(sub, "trip_type") == "round_trip")
        subscription_id = stable_subscription_id(sub)
        detail_subscription_id = canonical_detail_uuid(subscription_id)
        items.append(
            {
                "index": index,
                "subscription_id": subscription_id or "",
                "route": _subscription_route_text(sub),
                "route_type": route_type,
                "route_type_label": ROUTE_TYPE_LABELS.get(route_type, route_type),
                "trip": "往返" if round_trip else "单程",
                "dates": _subscription_dates_text(sub),
                "status": sub.get("status", "active"),
                "last_decision": _subscription_last_decision(sub, index),
                "scenario": _subscription_scenario_text(sub),
                "detail_url": (
                    url_for("detail", sub=detail_subscription_id)
                    if detail_subscription_id
                    else ""
                ),
                "delete_url": (
                    url_for(
                        "delete_subscription",
                        subscription_id=detail_subscription_id,
                    )
                    if detail_subscription_id
                    else ""
                ),
            }
        )
    return items


DETAIL_TEMPLATE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>航班监控详情</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin:0; background:#f8fafc; color:#111827; }
    main { max-width: 820px; margin: 0 auto; padding: 20px 14px 40px; }
    .panel { background:white; border:1px solid #e5e7eb; border-radius:10px; padding:16px; }
    .muted { color:#6b7280; font-size:13px; }
    a { color:#2563eb; }
  </style>
</head>
<body>
<main>
  <h1>航班监控详情</h1>
  {% if result %}
    <p class="muted">订阅：{{ result.subscription_id }} | 生成时间：{{ result.created_at }}</p>
    <div class="panel">{{ result.html|safe }}</div>
  {% else %}
    <div class="panel">
      <p><b>该详情可能还在同步中。</b></p>
      <p class="muted">本地采集后通常约1-2分钟同步到网页详情。你收到的邮件/微信推送已包含完整方案信息。</p>
      <p>
        <a href="{{ url_for('subscription_list') }}">返回我的监控</a>
        ·
        <a href="{{ request.url }}">刷新重试</a>
      </p>
    </div>
  {% endif %}
</main>
</body>
</html>
"""


def update_subscription_preference(
    subscription_id_value: str,
    field: str,
    value: str,
) -> bool:
    if field not in {
        "time_preference",
        "airline_policy",
        "accept_self_transfer",
        "refund_flexibility",
    }:
        return False
    repository = _subscription_repository()

    def mutate(subscription):
        hard = subscription.setdefault("hard_constraints", {})
        soft = subscription.setdefault("soft_preferences", {})

        if field == "time_preference":
            normalized = normalize_time_preference_mode(value)
            hard["time_preference"] = normalized
            hard["time_preference_mode"] = normalized
            soft["time_preference"] = normalized
            soft["time_preference_mode"] = normalized
            if normalized == "no_redeye":
                hard["departure_time_policy"] = "no_redeye"
                hard["arrival_time_policy"] = "no_midnight"
                soft["departure_time_windows"] = [["06:00", "23:00"]]
                soft["arrival_time_windows"] = [["06:00", "23:00"]]
                soft["red_eye_allowed"] = False
                soft["early_morning_allowed"] = True
            elif normalized == "daytime":
                hard["departure_time_policy"] = "daytime"
                hard["arrival_time_policy"] = "daytime_only"
                soft["departure_time_windows"] = [["06:00", "20:00"]]
                soft["arrival_time_windows"] = [["06:00", "20:00"]]
                soft["red_eye_allowed"] = False
                soft["early_morning_allowed"] = True
            elif normalized == "unlimited":
                hard["departure_time_policy"] = "any"
                hard["arrival_time_policy"] = "any"
                soft["departure_time_windows"] = []
                soft["arrival_time_windows"] = []
                soft["red_eye_allowed"] = True
                soft["early_morning_allowed"] = True
        elif field == "airline_policy":
            soft["airline_policy"] = value
        elif field == "accept_self_transfer":
            accepted = parse_bool(value)
            hard["accept_self_transfer"] = accepted
            soft["allow_self_transfer"] = accepted
        else:
            soft["refund_flexibility"] = value
        return subscription

    return (
        repository.mutate(
            LOCAL_OWNER_ID,
            subscription_id_value,
            mutate,
        )
        is not None
    )


def record_last_attempt(
    subscription: dict | str,
    *,
    status: str,
    holder_round_id=None,
    entrypoint: str = "web",
    at: str | None = None,
) -> bool:
    """按稳定订阅身份原子记录一次启动/执行状态。"""

    return record_subscription_attempt(
        subscription,
        status=status,
        holder_round_id=holder_round_id,
        entrypoint=entrypoint,
        at=at,
        path=SUBSCRIPTIONS_PATH,
        logger=safe_log,
    )


def _record_last_attempt_safely(
    subscription: dict | str,
    *,
    status: str,
    holder_round_id=None,
    entrypoint: str = "web",
    at: str | None = None,
) -> bool:
    try:
        return record_last_attempt(
            subscription,
            status=status,
            holder_round_id=holder_round_id,
            entrypoint=entrypoint,
            at=at,
        )
    except Exception as exc:
        safe_log(
            f"[采集启动握手] 状态落盘失败 status={status} "
            f"原因={type(exc).__name__}:{exc}"
        )
        return False


def _startup_result(payload: dict | None, *, default_status: str) -> dict:
    source = payload if isinstance(payload, dict) else {}
    result = {
        "status": str(source.get("status") or default_status),
        "at": _attempt_time(source.get("at")),
        "holder_round_id": source.get("holder_round_id"),
        "entrypoint": "web",
    }
    if "persisted" in source:
        result["persisted"] = bool(source["persisted"])
    return result


def _attempt_timestamp(value) -> float:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _remember_startup_handshake(subscription: dict, result: dict) -> None:
    subscription_id = stable_subscription_id(subscription)
    if not subscription_id:
        return
    normalized = _startup_result(result, default_status="confirming")
    if normalized["status"] not in ALLOWED_STARTUP_STATUSES:
        return
    handshakes = session.get(STARTUP_HANDSHAKE_SESSION_KEY)
    if not isinstance(handshakes, dict):
        handshakes = {}
    session[STARTUP_HANDSHAKE_SESSION_KEY] = {
        **handshakes,
        subscription_id: normalized,
    }


def _take_startup_handshake(subscription: dict) -> dict:
    subscription_id = stable_subscription_id(subscription)
    handshakes = session.get(STARTUP_HANDSHAKE_SESSION_KEY)
    if not subscription_id or not isinstance(handshakes, dict):
        return {}
    remaining = dict(handshakes)
    result = remaining.pop(subscription_id, {})
    if remaining:
        session[STARTUP_HANDSHAKE_SESSION_KEY] = remaining
    else:
        session.pop(STARTUP_HANDSHAKE_SESSION_KEY, None)
    return result if isinstance(result, dict) else {}


def _select_startup_attempt(stored_attempt: dict, session_attempt: dict) -> dict:
    stored = (
        stored_attempt
        if isinstance(stored_attempt, dict)
        and str(stored_attempt.get("status") or "") in ALLOWED_STARTUP_STATUSES
        else {}
    )
    current = (
        session_attempt
        if isinstance(session_attempt, dict)
        and str(session_attempt.get("status") or "") in ALLOWED_STARTUP_STATUSES
        else {}
    )
    if not stored:
        return current
    if not current:
        return stored
    if _attempt_timestamp(current.get("at")) > _attempt_timestamp(stored.get("at")):
        return current
    return stored


def startup_status_message(status: str) -> str:
    messages = {
        "started": "订阅已保存。系统正在采集第一批数据，预计1-2分钟内收到首次推送。",
        "busy": "已有采集轮正在执行，本次不重复启动，系统将在下一轮处理该订阅。",
        "startup_error": "订阅已保存，但首次采集未能启动，系统将在下一轮重试。",
        "confirming": "订阅已保存，首次采集状态正在确认。",
        "failed": "订阅已保存，首次采集已启动但本轮未完成，系统将在下一轮重试。",
        "success": "订阅已保存，首次采集已完成。",
    }
    return messages.get(str(status or "started"), messages["started"])


def run_single_subscription(subscription: dict, startup_queue=None) -> None:
    """Run one subscription collection in a background thread."""
    print("[后台] 线程已启动")
    startup_status = None
    startup_holder_round_id = None

    def report_startup(payload):
        nonlocal startup_holder_round_id, startup_status
        result = _startup_result(payload, default_status="startup_error")
        startup_status = result["status"]
        startup_holder_round_id = result["holder_round_id"]
        if startup_queue is not None:
            try:
                startup_queue.put_nowait({**result, "persisted": False})
            except queue.Full:
                pass
        result["persisted"] = _record_last_attempt_safely(
            subscription,
            status=result["status"],
            holder_round_id=result["holder_round_id"],
            entrypoint=result["entrypoint"],
            at=result["at"],
        )

    try:
        print("[后台] 开始导入 main 处理函数")
        from main import _normalize_subscription, process_subscription

        print("[后台] main 处理函数导入完成")
        print("[后台] 开始规范化订阅")
        normalized_subscription = _normalize_subscription(subscription)
        print(
            "[后台] 订阅规范化完成: "
            f"{normalized_subscription.get('origin')}→"
            f"{normalized_subscription.get('destination')} "
            f"{normalized_subscription.get('depart_date')}"
        )

        print("[后台] 开始采集、分析并推送")
        ok = process_subscription(
            normalized_subscription,
            ensure_db=True,
            web_trigger=True,
            startup_callback=report_startup,
        )
        if startup_status is None:
            report_startup(
                {
                    "status": "startup_error",
                    "holder_round_id": None,
                    "entrypoint": "single_subscription",
                }
            )
        if startup_status == "started":
            _record_last_attempt_safely(
                subscription,
                status="success" if ok is True else "failed",
                holder_round_id=startup_holder_round_id,
                entrypoint="web",
            )
        print(f"[后台] 采集分析推送结束: ok={ok}")
    except Exception as exc:
        if startup_status is None:
            report_startup(
                {
                    "status": "startup_error",
                    "holder_round_id": None,
                    "entrypoint": "single_subscription",
                }
            )
        elif startup_status == "started":
            _record_last_attempt_safely(
                subscription,
                status="failed",
                holder_round_id=startup_holder_round_id,
                entrypoint="web",
            )
        print(f"[后台] 执行失败: {exc}")
        traceback.print_exc()


def start_background_collection(
    subscription: dict,
    *,
    timeout_seconds: float | None = None,
) -> dict:
    """启动后台采集，并有界等待 single-flight 启动结果。"""

    startup_results = queue.Queue(maxsize=1)
    timeout = (
        float(timeout_seconds)
        if timeout_seconds is not None
        else float(app.config["COLLECTION_STARTUP_TIMEOUT_SECONDS"])
    )
    try:
        print("[后台] 准备启动采集线程")
        thread = threading.Thread(
            target=run_single_subscription,
            args=(subscription, startup_results),
            daemon=True,
        )
        thread.start()
        print(f"[后台] 采集线程已启动: {thread.name}")
    except Exception as exc:
        result = _startup_result(None, default_status="startup_error")
        result["persisted"] = _record_last_attempt_safely(
            subscription,
            status=result["status"],
            holder_round_id=result["holder_round_id"],
            entrypoint=result["entrypoint"],
            at=result["at"],
        )
        print(f"[后台] 启动线程失败: {exc}")
        traceback.print_exc()
        return result

    try:
        return startup_results.get(timeout=max(0.0, timeout))
    except queue.Empty:
        result = _startup_result(None, default_status="confirming")
        result["persisted"] = _record_last_attempt_safely(
            subscription,
            status=result["status"],
            holder_round_id=result["holder_round_id"],
            entrypoint=result["entrypoint"],
            at=result["at"],
        )
        safe_log(
            f"[采集启动握手] 等待超时 subscription_id="
            f"{stable_subscription_id(subscription)} timeout={timeout}s"
        )
        return result


def normalize_destination(value: str) -> str:
    return value.strip()


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


def normalize_price_scope(value: str | None) -> str:
    text = str(value or "per_person").strip().lower()
    if text in {"all", "total", "all_passengers", "all_passenger", "??", "??"}:
        return "all"
    return "per_person"


def parse_float(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def parse_count_alias(form, *names: str) -> int:
    for name in names:
        value = form.get(name)
        if value not in (None, ""):
            return parse_int(value, 0)
    return 0


def derive_trip_type_from_scenarios(scenarios: list[str]) -> str:
    values = [str(item).strip() for item in scenarios if str(item).strip()]
    if "business" in values:
        return "business"
    if "tourism" in values:
        return "tourism"
    if "family_visit" in values or "visit_family" in values:
        return "visit_family"
    if "family" in values or "elderly" in values:
        return "family"
    if "important" in values:
        return "important"
    if "price_first" in values:
        return "price_first"
    return "other"


def parse_optional_budget(value: str | None, budget_mode: str) -> int | None:
    if budget_mode != "fixed":
        return parse_int(value, 0) or None
    return parse_int(value, 0) or None


def infer_max_budget(max_budget: int | None, target_price: int | None) -> int | None:
    if max_budget:
        return max_budget
    if target_price:
        return round(target_price * 1.5)
    return None


def parse_price_tolerance(form) -> int:
    mode = form.get("price_tolerance_mode", "100")
    if mode == "custom":
        return parse_int(form.get("price_tolerance_custom"), 100)
    return parse_int(mode, 100)


def parse_short_transfer_limit(value: str | None) -> tuple[int | None, int | None]:
    if value == "extra_6":
        return 6, None
    if value == "total_18":
        return None, 18
    if value == "total_24":
        return None, 24
    return 3, None


def parse_active_airports(raw: str | None, fallback: list[str]) -> list[str]:
    airports = [
        item.strip().upper()
        for item in str(raw or "").split(",")
        if item.strip()
    ]
    fallback_set = {item.upper() for item in fallback or []}
    if fallback_set:
        airports = [item for item in airports if item in fallback_set]
    return airports or list(fallback or [])


def infer_route_type(origin_airports: list[str], destination_airports: list[str]) -> str:
    airports = {str(code or "").strip().upper() for code in (origin_airports or []) + (destination_airports or [])}
    if airports & GREATER_CHINA_AIRPORTS:
        return "greater_china"
    origin_domestic = bool(origin_airports) and all(
        str(code or "").strip().upper() in DOMESTIC_ROUTE_AIRPORTS for code in origin_airports
    )
    destination_domestic = bool(destination_airports) and all(
        str(code or "").strip().upper() in DOMESTIC_ROUTE_AIRPORTS for code in destination_airports
    )
    if origin_domestic and destination_domestic:
        return "domestic"
    return "international"


def time_slots_from_preference(form, field_name: str, default_slots: list[str]) -> list[str]:
    preference = form.get("time_preference", "any")
    if preference in {"any", "unlimited"}:
        return list(ALL_TIME_SLOTS)
    if preference == "daytime":
        return list(DAYTIME_TIME_SLOTS)
    if preference == "no_redeye":
        return list(DEFAULT_DEPARTURE_SLOTS)
    return form.getlist(field_name) or list(default_slots)


def normalize_time_preference_mode(value: str | None) -> str:
    mode = value or "unlimited"
    return "unlimited" if mode == "any" else mode


def time_windows_from_slots(slots: list[str]) -> list[list[str]]:
    windows = []
    for slot in slots:
        window = TIME_SEGMENTS.get(slot)
        if window:
            windows.append(list(window))
    return windows


def precise_window(form, start_name: str, end_name: str) -> list[list[str]] | None:
    start = (form.get(start_name) or "").strip()
    end = (form.get(end_name) or "").strip()
    if start and end:
        return [[start, end]]
    return None


def time_windows_from_preference(
    form,
    field_name: str,
    default_slots: list[str],
    start_name: str,
    end_name: str,
) -> list[list[str]]:
    mode = normalize_time_preference_mode(form.get("time_preference"))
    if mode == "unlimited":
        return []
    if mode == "daytime":
        return [["06:00", "20:00"]]
    if mode == "no_redeye":
        return [["06:00", "23:00"]]
    custom_precise = precise_window(form, start_name, end_name)
    if custom_precise:
        return custom_precise
    return time_windows_from_slots(form.getlist(field_name) or list(default_slots))


def red_eye_allowed_from_windows(mode: str, windows: list[list[str]]) -> bool:
    if mode == "unlimited":
        return True
    if mode in {"daytime", "no_redeye"}:
        return False
    return any(window == ["23:00", "06:00"] for window in windows)


def early_morning_allowed_from_windows(mode: str, windows: list[list[str]]) -> bool:
    if mode in {"unlimited", "daytime", "no_redeye"}:
        return True
    return any((window[0] <= "08:00" < window[1]) or window[0] == "06:00" for window in windows)


def first_push_text() -> str:
    next_time = datetime.now() + timedelta(minutes=10)
    return next_time.strftime("%Y-%m-%d %H:%M")


def build_subscription(form) -> dict:
    round_trip = parse_bool(form.get("round_trip", "false"))
    same_day_round_trip = parse_bool(form.get("same_day_round_trip", "false"))
    if same_day_round_trip:
        round_trip = True
    monitor_mode = form.get("monitor_mode", "quick")
    origin_manual = form.get("origin_manual", "").strip()
    origin_select = form.get("origin_select", "").strip()
    origin_input = origin_manual or ("" if origin_select == "OTHER" else origin_select)
    origin_info = resolve_location(origin_input)
    destination_info = resolve_location(normalize_destination(form.get("destination", "")))
    if origin_info.get("type") == "unknown":
        raise ValueError(location_error_message("origin", origin_info))
    if destination_info.get("type") == "unknown":
        raise ValueError(location_error_message("destination", destination_info))
    origin_airports_active = parse_active_airports(
        form.get("origin_airports_active"), origin_info["airports"]
    )
    destination_airports_active = parse_active_airports(
        form.get("destination_airports_active"), destination_info["airports"]
    )
    submitted_route_type = (form.get("route_type") or "").strip()
    route_type = infer_route_type(
        origin_airports_active,
        destination_airports_active,
    )
    if submitted_route_type and submitted_route_type != route_type:
        safe_log(
            "[路由分类修正] "
            f"表单值={submitted_route_type} 与机场组合冲突，采用IATA分类={route_type}"
        )
    excluded_airports = sorted(
        (
            set(origin_info["airports"])
            | set(destination_info["airports"])
        )
        - (set(origin_airports_active) | set(destination_airports_active))
    )
    budget_strategy = form.get("price_strategy") or form.get("budget_strategy", "explicit")
    max_budget_mode = "fixed"
    target_price_mode = "fixed"
    if budget_strategy == "auto_judge":
        max_budget_mode = "none"
        target_price_mode = "auto"
    elif budget_strategy == "low_price_alert":
        max_budget_mode = "none"
        target_price_mode = "low_zone"
    target_price = (
        parse_optional_budget(form.get("target_price"), target_price_mode)
        if budget_strategy == "explicit"
        else None
    )
    price_tolerance = parse_price_tolerance(form)
    max_budget = None
    if budget_strategy == "explicit" and max_budget_mode == "fixed":
        max_budget = infer_max_budget(parse_int(form.get("max_budget"), 0), target_price)
    # 旧版快速表单的 budget_scope=total 是隐式默认值，不能当作用户明确选择。
    max_budget_scope = normalize_price_scope(form.get("max_budget_scope") or "per_person")
    target_price_scope = normalize_price_scope(form.get("target_price_scope") or max_budget_scope)
    budget_scope = max_budget_scope
    day_trip_period = form.get("day_trip_period", "morning").strip() or "morning"
    if day_trip_period not in {"morning", "afternoon", "full_day"}:
        day_trip_period = "morning"
    business_start = form.get("business_start", "").strip()
    business_end = form.get("business_end", "").strip()
    meeting_location = form.get("meeting_location", "").strip()
    meeting_importance = form.get("meeting_importance", "important").strip() or "important"
    if meeting_importance not in {"normal", "important", "critical"}:
        meeting_importance = "important"
    buffer_hours = parse_float(form.get("buffer_hours"), 0.0)
    transport_mode = form.get("transport_mode", "").strip().lower()
    if transport_mode not in {"", "taxi", "transit"}:
        raise ValueError(f"transport_mode取值无效: {transport_mode}")
    outbound_set_off = form.get("outbound_set_off", "").strip()
    return_set_off = form.get("return_set_off", "").strip()
    user_transport_min = parse_int(form.get("user_transport_min"), 0)
    transport_margin_mode = form.get("transport_margin_mode", "standard").strip() or "standard"
    if transport_margin_mode not in {"tight", "standard", "loose"}:
        transport_margin_mode = "standard"
    redundancy_min = parse_int(form.get("redundancy_min"), 25)
    origin_transport_min = parse_int(form.get("origin_transport_min"), 0)
    destination_transport_min = parse_int(form.get("destination_transport_min"), 0)
    airport_advance_min = parse_int(form.get("airport_advance_min"), 0)
    arrival_exit_min = parse_int(form.get("arrival_exit_min"), 0)
    delay_buffer_min = parse_int(form.get("delay_buffer_min"), 0)
    pre_meeting_buffer_min = parse_int(form.get("pre_meeting_buffer_min"), 0)
    post_meeting_buffer_min = parse_int(form.get("post_meeting_buffer_min"), 0)
    custom_redundancy_min = parse_int(form.get("custom_redundancy_min"), 0)
    transfer_policy = form.get("transfer_policy", "reasonable")
    max_extra_duration_hours = None
    max_total_duration_hours = None
    if transfer_policy in {"reasonable", "short_ok", "price_first"}:
        max_extra_duration_hours, max_total_duration_hours = parse_short_transfer_limit(
            form.get("short_transfer_limit") or "extra_6"
        )
    accept_overnight_transfer = (
        transfer_policy == "price_first"
        and parse_bool(form.get("accept_overnight_transfer", "false"))
    )
    accept_self_transfer = (
        transfer_policy == "price_first"
        and parse_bool(form.get("accept_self_transfer", "false"))
    )
    time_mode = normalize_time_preference_mode(form.get("time_preference"))
    departure_slots = time_slots_from_preference(
        form, "departure_slots", DEFAULT_DEPARTURE_SLOTS
    )
    arrival_slots = time_slots_from_preference(
        form, "arrival_slots", DEFAULT_ARRIVAL_SLOTS
    )
    outbound_departure_slots = time_slots_from_preference(
        form, "outbound_departure_slots", DEFAULT_DEPARTURE_SLOTS
    )
    outbound_arrival_slots = time_slots_from_preference(
        form, "outbound_arrival_slots", DEFAULT_ARRIVAL_SLOTS
    )
    return_departure_slots = time_slots_from_preference(
        form, "return_departure_slots", DEFAULT_DEPARTURE_SLOTS
    )
    return_arrival_slots = time_slots_from_preference(
        form, "return_arrival_slots", DEFAULT_ARRIVAL_SLOTS
    )
    departure_time_windows = time_windows_from_preference(
        form, "departure_slots", DEFAULT_DEPARTURE_SLOTS, "departure_time_start", "departure_time_end"
    )
    arrival_time_windows = time_windows_from_preference(
        form, "arrival_slots", DEFAULT_ARRIVAL_SLOTS, "arrival_time_start", "arrival_time_end"
    )
    outbound_departure_time_windows = time_windows_from_preference(
        form, "outbound_departure_slots", DEFAULT_DEPARTURE_SLOTS, "departure_time_start", "departure_time_end"
    )
    outbound_arrival_time_windows = time_windows_from_preference(
        form, "outbound_arrival_slots", DEFAULT_ARRIVAL_SLOTS, "arrival_time_start", "arrival_time_end"
    )
    return_departure_time_windows = time_windows_from_preference(
        form, "return_departure_slots", DEFAULT_DEPARTURE_SLOTS, "departure_time_start", "departure_time_end"
    )
    return_arrival_time_windows = time_windows_from_preference(
        form, "return_arrival_slots", DEFAULT_ARRIVAL_SLOTS, "arrival_time_start", "arrival_time_end"
    )
    ux2_time_fields = None
    departure_time_policy = form.get("departure_time_policy", "no_redeye")
    arrival_time_policy = form.get("arrival_time_policy", "any")
    if parse_bool(form.get("ux2_concept_form", "false")):
        ux2_time_fields = derive_time_concept_fields(form, round_trip=round_trip)
        time_mode = ux2_time_fields["time_preference"]
        departure_time_policy = ux2_time_fields["departure_time_policy"]
        arrival_time_policy = ux2_time_fields["arrival_time_policy"]
        if not parse_bool(form.get("ux2_time_touched", "false")):
            departure_time_policy = (
                form.get("ux2_original_departure_time_policy")
                or departure_time_policy
            )
            arrival_time_policy = (
                form.get("ux2_original_arrival_time_policy")
                or arrival_time_policy
            )
        departure_slots = ux2_time_fields["departure_slots"]
        arrival_slots = ux2_time_fields["arrival_slots"]
        outbound_departure_slots = ux2_time_fields["outbound_departure_slots"]
        outbound_arrival_slots = ux2_time_fields["outbound_arrival_slots"]
        return_departure_slots = ux2_time_fields["return_departure_slots"]
        return_arrival_slots = ux2_time_fields["return_arrival_slots"]
        departure_time_windows = ux2_time_fields["departure_time_windows"]
        arrival_time_windows = ux2_time_fields["arrival_time_windows"]
        outbound_departure_time_windows = ux2_time_fields["outbound_departure_time_windows"]
        outbound_arrival_time_windows = ux2_time_fields["outbound_arrival_time_windows"]
        return_departure_time_windows = ux2_time_fields["return_departure_time_windows"]
        return_arrival_time_windows = ux2_time_fields["return_arrival_time_windows"]
    all_time_windows = (
        departure_time_windows
        + arrival_time_windows
        + outbound_departure_time_windows
        + outbound_arrival_time_windows
        + return_departure_time_windows
        + return_arrival_time_windows
    )
    if monitor_mode != "precise":
        time_mode = "no_redeye"
        departure_slots = list(DEFAULT_DEPARTURE_SLOTS)
        arrival_slots = list(DEFAULT_ARRIVAL_SLOTS)
        outbound_departure_slots = list(DEFAULT_DEPARTURE_SLOTS)
        outbound_arrival_slots = list(DEFAULT_ARRIVAL_SLOTS)
        return_departure_slots = list(DEFAULT_DEPARTURE_SLOTS)
        return_arrival_slots = list(DEFAULT_ARRIVAL_SLOTS)
        departure_time_windows = [["06:00", "23:00"]]
        arrival_time_windows = [["06:00", "23:00"]]
        outbound_departure_time_windows = [["06:00", "23:00"]]
        outbound_arrival_time_windows = [["06:00", "23:00"]]
        return_departure_time_windows = [["06:00", "23:00"]]
        return_arrival_time_windows = [["06:00", "23:00"]]
        all_time_windows = (
            departure_time_windows
            + arrival_time_windows
            + outbound_departure_time_windows
            + outbound_arrival_time_windows
            + return_departure_time_windows
            + return_arrival_time_windows
        )
    time_constraints = {
        "departure_slots": departure_slots,
        "arrival_slots": arrival_slots,
        "preferred_departure_slots": departure_slots,
        "preferred_arrival_slots": arrival_slots,
        "time_preference_mode": time_mode,
        "departure_time_windows": departure_time_windows,
        "arrival_time_windows": arrival_time_windows,
    }
    if round_trip:
        time_constraints.update(
            {
                "outbound_departure_slots": outbound_departure_slots,
                "outbound_arrival_slots": outbound_arrival_slots,
                "return_departure_slots": return_departure_slots,
                "return_arrival_slots": return_arrival_slots,
                "preferred_departure_slots": outbound_departure_slots,
                "preferred_arrival_slots": outbound_arrival_slots,
                "outbound_departure_time_windows": outbound_departure_time_windows,
                "outbound_arrival_time_windows": outbound_arrival_time_windows,
                "return_departure_time_windows": return_departure_time_windows,
                "return_arrival_time_windows": return_arrival_time_windows,
            }
        )
    frequency_aliases = {
        "daily_summary": "daily_digest",
        "every_change": "price_change",
    }
    notification_frequency_raw = (
        form.get("notification_frequency")
        or form.get("notification_frequency_rule")
        or "important_only"
    )
    notification_frequency = frequency_aliases.get(
        notification_frequency_raw,
        notification_frequency_raw,
    )
    primary_goal = form.get("primary_goal", "buy_timing")
    if monitor_mode == "precise":
        secondary_goals = form.getlist("secondary_goals") or list(
            GOAL_TO_ALERTS.get(primary_goal, [])
        )
    else:
        secondary_goals = list(GOAL_TO_ALERTS.get(primary_goal, []))
    blocked_airlines = [
        item.strip()
        for item in form.get("exclude_airlines", "").replace("，", ",").split(",")
        if item.strip()
    ]
    for item in form.getlist("blocked_airlines_common"):
        if item and item not in blocked_airlines:
            blocked_airlines.append(item)
    airline_policy = form.get("airline_policy", "any")
    lcc_policy = str(form.get("lcc_policy") or "any").strip()
    if lcc_policy not in LCC_POLICIES:
        raise ValueError(f"lcc_policy取值无效: {lcc_policy}")
    airline_policy, lcc_policy, legacy_lcc_migrated = canonicalize_airline_lcc_policy(
        airline_policy,
        lcc_policy,
    )
    if legacy_lcc_migrated:
        safe_log(
            "[口径迁移] airline_policy=no_lcc已归一为"
            f"airline_policy={airline_policy}, lcc_policy={lcc_policy}"
        )
    if monitor_mode != "precise":
        airline_policy = "any"
        blocked_airlines = []
    precise_passengers = {
        "adult": parse_count_alias(form, "adult_count", "passenger_adult"),
        "child": parse_count_alias(form, "child_count", "passenger_child"),
        "elderly": parse_count_alias(form, "elderly_count", "passenger_elderly"),
        "infant": parse_count_alias(form, "infant_count", "passenger_infant"),
    }
    quick_passenger_count = max(1, parse_int(form.get("passenger_count"), 1))
    if monitor_mode == "precise":
        if not any(precise_passengers.values()):
            precise_passengers["adult"] = quick_passenger_count
        passenger_count = sum(precise_passengers.values())
        travel_scenarios = form.getlist("travel_scenario")
    else:
        passenger_count = quick_passenger_count
        precise_passengers = {"adult": passenger_count, "child": 0, "elderly": 0, "infant": 0}
        travel_scenarios = form.getlist("travel_scenario")
    if not travel_scenarios:
        legacy_scenario = form.get("travel_scenario", "personal")
        if isinstance(legacy_scenario, list):
            travel_scenarios = legacy_scenario
        else:
            travel_scenarios = [legacy_scenario]
    travel_scenarios = [str(item).strip() for item in travel_scenarios if str(item).strip()]
    if not travel_scenarios:
        travel_scenarios = ["personal"]
    travel_scenario = travel_scenarios[0]
    derived_trip_type = derive_trip_type_from_scenarios(travel_scenarios)
    if monitor_mode != "precise":
        if not same_day_round_trip:
            business_start = ""
            business_end = ""
            meeting_location = ""
        outbound_set_off = ""
        return_set_off = ""
        user_transport_min = 0
        origin_transport_min = 0
        destination_transport_min = 0
        airport_advance_min = 0
        arrival_exit_min = 0
        delay_buffer_min = 0
        pre_meeting_buffer_min = 0
        post_meeting_buffer_min = 0
        custom_redundancy_min = 0
        buffer_hours = 0.0
        transport_mode = ""
        transport_margin_mode = "standard"
        redundancy_min = 25
    if precise_passengers.get("child") and precise_passengers.get("elderly"):
        companions = "with_elderly_child"
    elif precise_passengers.get("child"):
        companions = "with_child"
    elif precise_passengers.get("elderly"):
        companions = "with_elderly"
    elif passenger_count > 1:
        companions = "multiple"
    else:
        companions = form.get("companions", "solo")
    explicit_companion_constraints = form.getlist("companion_constraints")
    elderly_condition = form.get("elderly_condition", "normal").strip()
    child_type = form.get("child_type", "").strip()
    solo_travel = parse_bool(form.get("solo_travel", "false"))
    no_late_arrival = parse_bool(
        ux2_time_fields["no_late_arrival"]
        if ux2_time_fields is not None
        else form.get("no_late_arrival", "false")
    )
    prefer_daytime_arrival = parse_bool(
        ux2_time_fields["prefer_daytime_arrival"]
        if ux2_time_fields is not None
        else form.get("prefer_daytime_arrival", "false")
    )
    invoice_context = parse_bool(form.get("invoice_context", "false"))
    invoice_needed = parse_bool(form.get("invoice_needed", "false"))
    invoice_special_vat = parse_bool(form.get("invoice_special_vat", "false"))
    invoice_cabin_limit = parse_bool(form.get("invoice_cabin_limit", "false"))
    raw_trip_natures = form.getlist("trip_natures") if hasattr(form, "getlist") else []
    refund_flexibility = form.get("refund_flexibility", "preferred")
    price_sensitivity = form.get("price_sensitivity", "low")
    if monitor_mode != "precise":
        elderly_condition = ""
        child_type = ""
        solo_travel = False
        no_late_arrival = False
        prefer_daytime_arrival = False
        invoice_context = False
        invoice_needed = False
        invoice_special_vat = False
        invoice_cabin_limit = False
        raw_trip_natures = []
        refund_flexibility = "preferred"
        price_sensitivity = "low"
    if not raw_trip_natures:
        legacy_trip_nature = form.get("trip_nature", "").strip()
        raw_trip_natures = [legacy_trip_nature] if legacy_trip_nature else []

    mobility_limited = parse_bool(form.get("mobility_limited", "false"))
    companion_constraints_seed = [
        item.strip() for item in str(form.get("companion_constraints_seed") or "").split(",") if item.strip()
    ]
    if ux2_time_fields is not None:
        departure_slot_fields = (
            ("outbound_departure_slots", "return_departure_slots")
            if round_trip
            else ("departure_slots",)
        )
        companion_allows_redeye = any(
            "redeye" in (ux2_time_fields.get(field_name) or [])
            for field_name in departure_slot_fields
        )
    else:
        companion_allows_redeye = parse_bool(form.get("allow_redeye", "false"))
    if explicit_companion_constraints:
        companion_constraints = explicit_companion_constraints
    elif parse_bool(form.get("companion_constraints_seed_present", "false")):
        companion_constraints = companion_constraints_seed
    elif parse_bool(form.get("derive_companion_constraints", "false")):
        companion_constraints = derive_companion_constraints(
            {
                "transfer_policy": transfer_policy,
                "short_transfer_limit": form.get("short_transfer_limit", "extra_6"),
                "departure_time_policy": departure_time_policy,
                "time_preference": time_mode,
                "allow_redeye": companion_allows_redeye,
                "arrival_time_policy": arrival_time_policy,
                "arrival_preference": form.get("arrival_preference"),
                "prefer_daytime_arrival": prefer_daytime_arrival,
                "baggage": form.get("baggage", "required"),
                "refund_flexibility": refund_flexibility,
                "mobility_limited": mobility_limited,
            }
        )
    else:
        companion_constraints = []
    trip_nature_map = {
        "business_meeting": "meeting",
        "business_trip": "business",
        "team_building": "team_building",
        "business": "business",
        "meeting": "meeting",
    }
    trip_natures = []
    for item in raw_trip_natures:
        value = trip_nature_map.get(str(item or "").strip(), str(item or "").strip())
        if value and value not in trip_natures:
            trip_natures.append(value)
    business_context = monitor_mode == "precise" and (
        "business" in travel_scenarios
        or same_day_round_trip
        or (route_type == "domestic" and (invoice_context or invoice_needed or invoice_special_vat or invoice_cabin_limit))
        or any(item in {"business", "meeting", "team_building"} for item in trip_natures)
    )
    if not business_context:
        trip_natures = []
        invoice_needed = False
        invoice_special_vat = False
        invoice_cabin_limit = False
    elif route_type == "domestic" and invoice_context:
        invoice_needed = True
    trip_nature = "meeting" if "meeting" in trip_natures else trip_natures[0] if trip_natures else ""
    meeting_start = form.get("meeting_start", "").strip()
    meeting_end = form.get("meeting_end", "").strip()
    if "meeting" in trip_natures and (meeting_start or meeting_end):
        same_day_round_trip = True
        round_trip = True
        business_start = meeting_start or business_start
        business_end = meeting_end or business_end
    team_passenger_count = parse_int(form.get("team_passenger_count"), 0)
    if team_passenger_count > 0:
        passenger_count = team_passenger_count
    cabin_arrangement = form.get("cabin_arrangement", "economy_all").strip() or "economy_all"
    cabin_policy = form.get("cabin_policy", "economy_only").strip() or "economy_only"
    user_level = form.get("user_level", "staff").strip() or "staff"
    business_seats = parse_int(form.get("business_seats"), 0)
    economy_seats = parse_int(form.get("economy_seats"), 0)
    cabin_allocation = {}
    explicit_cabin_allocation = False
    if cabin_arrangement == "business_all" and passenger_count:
        business_seats = passenger_count
        economy_seats = 0
        cabin_policy = "business_allowed"
        budget_scope = max_budget_scope = target_price_scope = "all"
    elif cabin_arrangement == "economy_all" and passenger_count:
        business_seats = 0
        economy_seats = passenger_count
    elif cabin_arrangement == "mixed":
        cabin_allocation, explicit_cabin_allocation = cabin_allocation_from_form(
            form, precise_passengers
        )
        if explicit_cabin_allocation:
            allocation_result = validate_cabin_allocation(
                cabin_allocation,
                precise_passengers,
            )
            cabin_allocation = allocation_result["allocation"]
            business_seats = allocation_result["business_seats"]
            economy_seats = allocation_result["economy_seats"]
            passenger_count = business_seats + economy_seats
            cabin_policy = "business_allowed"
            budget_scope = max_budget_scope = target_price_scope = "all"
        elif business_seats + economy_seats > 0:
            # 旧版存量仅有两舱总人数，无法无损反推出各乘客类型，继续原样保存。
            passenger_count = business_seats + economy_seats
        else:
            raise ValueError("混舱分配尚未填写，请为每类乘客选择商务舱或经济舱")
    team_date_flexibility = form.get("team_date_flexibility", "fixed").strip() or "fixed"
    same_flight_required = parse_bool(form.get("same_flight_required", "false"))
    reimburse_per_person = parse_int(form.get("reimburse_per_person"), 0)
    if monitor_mode != "precise":
        meeting_start = ""
        meeting_end = ""
        team_passenger_count = 0
        cabin_arrangement = "economy_all"
        cabin_policy = "economy_only"
        user_level = "staff"
        business_seats = 0
        economy_seats = passenger_count
        team_date_flexibility = "fixed"
        same_flight_required = False
        reimburse_per_person = 0
        explicit_cabin_allocation = False
    elif not business_context:
        meeting_start = ""
        meeting_end = ""
        team_passenger_count = 0
        team_date_flexibility = "fixed"
        same_flight_required = False
        reimburse_per_person = 0
    use_hourly_time = monitor_mode == "precise" and time_mode == "custom"
    notification_goals = normalize_notification_goals(
        {
            "primary": primary_goal,
            "secondary": secondary_goals,
            "method": form.get("notification_method") or DEFAULT_NOTIFICATION_METHOD,
            "email": form.get("notification_email", "").strip(),
            "privacy_level": (
                form.get("notification_privacy_level")
                or DEFAULT_NOTIFICATION_PRIVACY_LEVEL
            ),
            "frequency": notification_frequency,
        }
    )

    def optional_minutes(value):
        return value if value and value > 0 else None

    subscription = {
        "basic": {
            "origin": origin_info["value"],
            "origin_airports": origin_info["airports"],
            "origin_airports_active": origin_airports_active,
            "destination": destination_info["value"],
            "dest_airports": destination_info["airports"],
            "destination_airports": destination_info["airports"],
            "destination_airports_active": destination_airports_active,
            "route_type": route_type,
            "trip_type": "round_trip" if round_trip else "one_way",
            "departure_date": form.get("depart_date", "").strip(),
            "return_date": (
                form.get("depart_date", "").strip()
                if same_day_round_trip
                else form.get("return_date", "").strip() if round_trip else None
            ),
            "passenger_count": passenger_count,
        },
        "constraints": {
            "route_type": route_type,
            "budget_strategy": budget_strategy,
            "max_price": max_budget,
            "ideal_price": target_price,
            "budget_scope": budget_scope,
            "max_budget_scope": max_budget_scope,
            "target_price_scope": target_price_scope,
            "date_flexibility_days": parse_int(form.get("date_flexibility"), 0),
            "transfer_policy": transfer_policy,
            "checked_baggage_required": form.get("baggage", "required") == "required",
            "lcc_policy": lcc_policy,
            "same_day_round_trip": same_day_round_trip,
            "day_trip_period": day_trip_period if same_day_round_trip else "",
            "business_start": business_start if same_day_round_trip else "",
            "business_end": business_end if same_day_round_trip else "",
            "meeting_location": meeting_location if same_day_round_trip else "",
            "meeting_importance": meeting_importance if same_day_round_trip else "",
            "outbound_set_off": outbound_set_off,
            "return_set_off": return_set_off if round_trip else "",
            "user_transport_min": optional_minutes(user_transport_min),
            "origin_transport_min": optional_minutes(origin_transport_min),
            "destination_transport_min": optional_minutes(destination_transport_min),
            "airport_advance_min": optional_minutes(airport_advance_min),
            "arrival_exit_min": optional_minutes(arrival_exit_min),
            "delay_buffer_min": optional_minutes(delay_buffer_min),
            "pre_meeting_buffer_min": optional_minutes(pre_meeting_buffer_min),
            "post_meeting_buffer_min": optional_minutes(post_meeting_buffer_min),
            "custom_redundancy_min": optional_minutes(custom_redundancy_min),
            "transport_margin_mode": transport_margin_mode,
            "redundancy_min": redundancy_min,
            "time_source": "meeting_derived" if same_day_round_trip and business_start and business_end else "user_defined",
            "trip_nature": trip_nature,
            "trip_natures": trip_natures,
            "meeting_start": meeting_start,
            "meeting_end": meeting_end,
            "team_date_flexibility": team_date_flexibility,
            "same_flight_required": same_flight_required,
            "team_passenger_count": team_passenger_count or None,
            "cabin_arrangement": cabin_arrangement,
            "cabin_policy": cabin_policy,
            "user_level": user_level,
            "business_seats": business_seats,
            "economy_seats": economy_seats,
            **({"cabin_allocation": cabin_allocation} if explicit_cabin_allocation else {}),
            "reimburse_per_person": reimburse_per_person or None,
        },
        "preferences": {
            "travelers": companions,
            "passengers": precise_passengers,
            "passenger_count": passenger_count,
            "travel_purposes": travel_scenarios,
            "travel_scenario": travel_scenario,
            "travel_scenarios": travel_scenarios,
            "companion_constraints": companion_constraints,
            "elderly_condition": elderly_condition,
            "child_type": child_type,
            "mobility_limited": mobility_limited or "limited_mobility" in companion_constraints,
            "solo_travel": solo_travel,
            "no_late_arrival": no_late_arrival,
            "prefer_daytime_arrival": prefer_daytime_arrival,
            "invoice_needed": invoice_needed,
            "invoice_special_vat": invoice_special_vat,
            "invoice_cabin_limit": invoice_cabin_limit,
            "time_pref": time_mode,
            "refund_policy": refund_flexibility,
            "price_sensitivity": price_sensitivity,
            "travel_type": derived_trip_type,
        },
        "advanced_rules": {
            "time_windows": {
                "departure": departure_time_windows,
                "arrival": arrival_time_windows,
                "outbound_departure": outbound_departure_time_windows,
                "outbound_arrival": outbound_arrival_time_windows,
                "return_departure": return_departure_time_windows,
                "return_arrival": return_arrival_time_windows,
                "hourly": {
                    "departure_start": form.get("departure_time_start", "") if use_hourly_time else "",
                    "departure_end": form.get("departure_time_end", "") if use_hourly_time else "",
                    "arrival_start": form.get("arrival_time_start", "") if use_hourly_time else "",
                    "arrival_end": form.get("arrival_time_end", "") if use_hourly_time else "",
                },
            },
            "transfer": {
                "max_total_duration": max_total_duration_hours,
                "max_extra_duration_hours": max_extra_duration_hours,
                "overnight_transfer": accept_overnight_transfer,
                "self_transfer": accept_self_transfer,
            },
            "airlines": {
                "preference": airline_policy,
                "blocked": blocked_airlines,
                "lcc_policy": lcc_policy,
            },
            "alerts": {
                "frequency": notification_frequency,
                "types": secondary_goals,
                "price_change_threshold": form.get("price_change_threshold", "down_100"),
                "digest_time": form.get("digest_time", "09:00"),
            },
        },
        "origin": origin_info["value"],
        "origin_type": origin_info["type"],
        "origin_airports": origin_info["airports"],
        "origin_airports_active": origin_airports_active,
        "destination": destination_info["value"],
        "destination_type": destination_info["type"],
        "destination_airports": destination_info["airports"],
        "destination_airports_active": destination_airports_active,
        "route_type": route_type,
        "excluded_airports": excluded_airports,
        "monitor_mode": monitor_mode,
        "depart_date": form.get("depart_date", "").strip(),
        "return_date": (
            form.get("depart_date", "").strip()
            if same_day_round_trip
            else form.get("return_date", "").strip() if round_trip else None
        ),
        "round_trip": round_trip,
        "lcc_policy": lcc_policy,
        "same_day_round_trip": same_day_round_trip,
        "passenger_count": passenger_count,
        **({"cabin_allocation": cabin_allocation} if explicit_cabin_allocation else {}),
        **(
            {
                "budget_scope": budget_scope,
                "max_budget_scope": max_budget_scope,
                "target_price_scope": target_price_scope,
            }
            if explicit_cabin_allocation else {}
        ),
        "date_flexibility": parse_int(form.get("date_flexibility"), 0),
        "return_date_flexibility": (
            parse_int(form.get("return_date_flexibility"), 0) if round_trip else 0
        ),
        "hard_constraints": {
            "budget_strategy": budget_strategy,
            "max_budget": max_budget,
            "max_budget_mode": max_budget_mode,
            "target_price": target_price,
            "target_price_mode": target_price_mode,
            "budget_scope": budget_scope,
            "max_budget_scope": max_budget_scope,
            "target_price_scope": target_price_scope,
            "transfer_policy": transfer_policy,
            "route_type": route_type,
            "same_day_round_trip": same_day_round_trip,
            "day_trip_period": day_trip_period if same_day_round_trip else "",
            "business_start": business_start if same_day_round_trip else "",
            "business_end": business_end if same_day_round_trip else "",
            "meeting_location": meeting_location if same_day_round_trip else "",
            "meeting_importance": meeting_importance if same_day_round_trip else "",
            "outbound_set_off": outbound_set_off,
            "return_set_off": return_set_off if round_trip else "",
            "user_transport_min": optional_minutes(user_transport_min),
            "origin_transport_min": optional_minutes(origin_transport_min),
            "destination_transport_min": optional_minutes(destination_transport_min),
            "airport_advance_min": optional_minutes(airport_advance_min),
            "arrival_exit_min": optional_minutes(arrival_exit_min),
            "delay_buffer_min": optional_minutes(delay_buffer_min),
            "pre_meeting_buffer_min": optional_minutes(pre_meeting_buffer_min),
            "post_meeting_buffer_min": optional_minutes(post_meeting_buffer_min),
            "custom_redundancy_min": optional_minutes(custom_redundancy_min),
            "transport_margin_mode": transport_margin_mode,
            "redundancy_min": redundancy_min,
            "time_source": "meeting_derived" if same_day_round_trip and business_start and business_end else "user_defined",
            "trip_nature": trip_nature,
            "trip_natures": trip_natures,
            "meeting_start": meeting_start,
            "meeting_end": meeting_end,
            "team_date_flexibility": team_date_flexibility,
            "same_flight_required": same_flight_required,
            "team_passenger_count": team_passenger_count or None,
            "cabin_arrangement": cabin_arrangement,
            "cabin_policy": cabin_policy,
            "user_level": user_level,
            "business_seats": business_seats,
            "economy_seats": economy_seats,
            **({"cabin_allocation": cabin_allocation} if explicit_cabin_allocation else {}),
            "reimburse_per_person": reimburse_per_person or None,
            "max_extra_duration_hours": max_extra_duration_hours,
            "max_total_duration_hours": max_total_duration_hours,
            "departure_time_policy": departure_time_policy,
            "arrival_time_policy": arrival_time_policy,
            "time_preference": time_mode,
            **time_constraints,
            "baggage": form.get("baggage", "required"),
            "lcc_policy": lcc_policy,
            "origin_airport_preference": form.get("origin_airport_preference", "all"),
            "accept_overnight_transfer": accept_overnight_transfer,
            "accept_self_transfer": accept_self_transfer,
        },
        "soft_preferences": {
            "trip_type": derived_trip_type,
            "time_preference": time_mode,
            "time_preference_mode": time_mode,
            "departure_time_windows": departure_time_windows,
            "arrival_time_windows": arrival_time_windows,
            "outbound_departure_time_windows": outbound_departure_time_windows,
            "outbound_arrival_time_windows": outbound_arrival_time_windows,
            "return_departure_time_windows": return_departure_time_windows,
            "return_arrival_time_windows": return_arrival_time_windows,
            "red_eye_allowed": red_eye_allowed_from_windows(time_mode, all_time_windows),
            "early_morning_allowed": early_morning_allowed_from_windows(time_mode, all_time_windows),
            "travel_scenario": travel_scenario,
            "travel_scenarios": travel_scenarios,
            "travel_purposes": travel_scenarios,
            "passengers": precise_passengers,
            "passenger_count": passenger_count,
            "travelers": companions,
            "companions": companions,
            "companion_constraints": companion_constraints,
            "elderly_condition": elderly_condition,
            "child_type": child_type,
            "mobility_limited": mobility_limited or "limited_mobility" in companion_constraints,
            "solo_travel": solo_travel,
            "no_late_arrival": no_late_arrival,
            "prefer_daytime_arrival": prefer_daytime_arrival,
            "invoice_needed": invoice_needed,
            "invoice_special_vat": invoice_special_vat,
            "invoice_cabin_limit": invoice_cabin_limit,
            "price_sensitivity": price_sensitivity,
            "trip_rigidity": form.get("trip_rigidity", "confirmed"),
            "refund_flexibility": refund_flexibility,
            "airline_policy": airline_policy,
            "exclude_airlines": blocked_airlines,
            "target_price": target_price,
            "target_price_mode": target_price_mode,
            "price_tolerance": price_tolerance,
            "max_budget": max_budget,
            "budget_scope": budget_scope,
            "max_budget_scope": max_budget_scope,
            "target_price_scope": target_price_scope,
        },
        "notification_goals": notification_goals,
        "status": "active",
        "created_at": datetime.now().isoformat(),
    }
    if buffer_hours > 0:
        subscription["constraints"]["buffer_hours"] = buffer_hours
        subscription["hard_constraints"]["buffer_hours"] = buffer_hours
    if transport_mode:
        subscription["constraints"]["transport_mode"] = transport_mode
        subscription["hard_constraints"]["transport_mode"] = transport_mode
    return subscription


def _first_time_window_text(value) -> str:
    if not isinstance(value, (list, tuple)) or not value:
        return ""
    first = value[0]
    if not isinstance(first, (list, tuple)) or len(first) < 2:
        return ""
    start = str(first[0] or "").strip()
    end = str(first[1] or "").strip()
    return f"{start}-{end}" if start and end else ""


def _success_time_window_text(hard: dict) -> str:
    items = []
    for label, key in (
        ("去程出发", "outbound_departure_time_windows"),
        ("去程到达", "outbound_arrival_time_windows"),
        ("返程出发", "return_departure_time_windows"),
        ("返程到达", "return_arrival_time_windows"),
    ):
        text = _first_time_window_text(hard.get(key))
        if text:
            items.append(f"{label}{text}")
    if not items:
        for label, key in (
            ("出发", "departure_time_windows"),
            ("到达", "arrival_time_windows"),
        ):
            text = _first_time_window_text(hard.get(key))
            if text:
                items.append(f"{label}{text}")
    return f"时间窗：{'；'.join(items)}" if items else ""

def _success_transfer_text(hard: dict) -> str:
    policy = str(hard.get("transfer_policy") or "reasonable")
    policy_text = {
        "direct_only": "必须直飞",
        "reasonable": "合理中转",
        "short_ok": "短中转",
        "price_first": "价格优先",
    }.get(policy, "合理中转")
    parts = [policy_text]
    if policy == "direct_only":
        return f"中转设置：{' · '.join(parts)}"

    max_total = hard.get("max_total_duration_hours")
    max_extra = hard.get("max_extra_duration_hours")
    if max_total not in (None, ""):
        parts.append(f"总时长不超{max_total}小时")
    elif max_extra not in (None, ""):
        parts.append(f"中转最多多{max_extra}小时")
    if hard.get("accept_overnight_transfer"):
        parts.append("接受过夜中转")
    if hard.get("accept_self_transfer"):
        parts.append("接受非联程自行中转")
    return f"中转设置：{' · '.join(parts)}"


def build_success_summary(subscription: dict) -> dict:
    subscription_with_defaults = apply_default_rules(subscription)
    hard = subscription.get("hard_constraints", {})
    soft = subscription.get("soft_preferences") or {}
    preferences = subscription.get("preferences") or {}
    notification_goals = normalize_notification_goals(
        subscription.get("notification_goals")
    )
    method = notification_goals["method"]
    notification_email = str(notification_goals.get("email") or "").strip()
    notification_labels = {
        "pushplus": "PushPlus微信",
        "email": "你的邮箱",
        "both": "你的邮箱 / PushPlus微信",
        "page_only": "我的监控详情页",
    }
    reminders = [
        "当前价格进入历史低价区间",
        "判断继续等待的涨价风险变高时",
        "前后日期出现显著更便宜方案",
        "出现异常低价或短时放票",
    ]

    exclusions = ["不满足你时间要求的航班"]
    departure_slots = (
        hard.get("outbound_departure_slots")
        or hard.get("departure_slots")
        or hard.get("preferred_departure_slots")
        or []
    )
    arrival_slots = (
        hard.get("outbound_arrival_slots")
        or hard.get("arrival_slots")
        or hard.get("preferred_arrival_slots")
        or []
    )
    departure_policy = hard.get("departure_time_policy")
    if departure_slots and "redeye" not in departure_slots:
        exclusions.append("23:00-06:00起飞的红眼航班")
    elif departure_policy == "no_redeye":
        exclusions.append("23:00-06:00起飞的红眼航班")
    elif departure_policy == "daytime":
        exclusions.append("不符合白天出行要求的航班")
    if arrival_slots and "redeye" not in arrival_slots:
        exclusions.append("23:00-06:00到达的凌晨航班")
    if hard.get("baggage") == "required":
        exclusions.append("不含免费托运的方案")
    if hard.get("transfer_policy") == "direct_only":
        exclusions.append("需要中转的方案")
    budget = hard.get("max_budget", hard.get("budget"))
    if budget:
        exclusions.append(f"超出¥{budget:,}预算的方案")

    origin_airports = (
        subscription.get("origin_airports_active")
        or subscription.get("origin_airports")
        or [subscription.get("origin")]
    )
    destination_airports = (
        subscription.get("destination_airports_active")
        or subscription.get("destination_airports")
        or [
        subscription.get("destination")
    ])
    coverage = ""
    if len(origin_airports) > 1 or len(destination_airports) > 1:
        origin_text = "、".join(format_airport(code) for code in origin_airports if code)
        destination_text = "、".join(
            format_airport(code) for code in destination_airports if code
        )
        coverage = f"覆盖出发机场：{origin_text}；到达机场：{destination_text}"

    notification_text = notification_labels.get(method, "你的邮箱 / PushPlus微信")
    if method in {"email", "both"} and notification_email:
        notification_text = f"{notification_text}（{notification_email}）"
    scenario_text = _subscription_scenario_text(subscription)
    if scenario_text == "未设置":
        scenario_text = ""
    companion_constraints = (
        soft.get("companion_constraints")
        or preferences.get("companion_constraints")
        or hard.get("companion_constraints")
        or []
    )
    if isinstance(companion_constraints, str):
        companion_constraints = [item.strip() for item in companion_constraints.split(",") if item.strip()]
    companion_constraints_text = " + ".join(
        COMPANION_CONSTRAINT_LABELS.get(str(item), str(item)) for item in companion_constraints
    )
    cabin_text = ""
    cabin_allocation = (
        subscription.get("cabin_allocation")
        or hard.get("cabin_allocation")
        or {}
    )
    if hard.get("cabin_arrangement") == "mixed" and cabin_allocation:
        cabin_text = cabin_allocation_detail_label(cabin_allocation)
    meeting_text = ""
    if hard.get("same_day_round_trip"):
        meeting_start = hard.get("business_start") or hard.get("meeting_start")
        meeting_end = hard.get("business_end") or hard.get("meeting_end")
        if meeting_start or meeting_end:
            meeting_text = f"当天往返会议：{meeting_start or '未填'}-{meeting_end or '未填'}"

    return {
        "route": f"{city_label(subscription.get('origin'))} → {city_label(subscription.get('destination'))}",
        "airport_coverage": coverage,
        "defaults_applied": subscription_with_defaults.get("defaults_applied", []),
        "reminders": reminders,
        "exclusions": exclusions,
        "notification_text": notification_text,
        "meeting_text": meeting_text,
        "time_window_text": _success_time_window_text(hard),
        "scenario_text": scenario_text,
        "companion_constraints_text": companion_constraints_text,
        "cabin_text": cabin_text,
        "transfer_text": _success_transfer_text(hard),
    }


def _form_template_context(page_mode: str, values=None, *, edit_index=None, form_error="") -> dict:
    data = values or {}
    return {
        "page": build_form_page_context(page_mode, data, edit_index=edit_index),
        "city_airports": CITY_AIRPORTS,
        "city_aliases": CITY_ALIASES,
        "airport_codes": sorted(AIRPORTS),
        "exact_location_airports": EXACT_LOCATION_AIRPORTS,
        "form_error": form_error,
        "build_marker": PROCESS_BUILD_INFO.format_marker(
            request.environ.get("SERVER_PORT"),
        ),
    }


def _render_form_page(page_mode: str, values=None, *, edit_index=None, form_error=""):
    return render_template_string(
        FORM_TEMPLATE,
        **_form_template_context(
            page_mode,
            values,
            edit_index=edit_index,
            form_error=form_error,
        ),
    )


def _submitted_form_values(form) -> dict:
    values = {}
    for key in form.keys():
        items = form.getlist(key)
        values[key] = items if len(items) > 1 else (items[0] if items else "")
    return values


@app.get("/")
def index():
    edit_arg = request.args.get("edit")
    if edit_arg not in (None, ""):
        return redirect(url_for("settings", edit=edit_arg))
    return _render_form_page("quick")


@app.get("/settings")
def settings():
    edit_values = {}
    edit_index = None
    edit_arg = request.args.get("edit")
    if edit_arg not in (None, ""):
        try:
            edit_subscription_id = _resolve_edit_subscription_id(edit_arg)
        except SubscriptionIdentityMigrationRequired as exc:
            return _identity_migration_required_response(exc)
        if edit_subscription_id is None:
            abort(404)
        subscription = _subscription_repository().get(
            LOCAL_OWNER_ID,
            edit_subscription_id,
        )
        if subscription is None:
            abort(404)
        migrated, _details = migrate_subscription_defaults([subscription])
        edit_values = subscription_to_form_values(
            {**migrated[0], "_index": edit_subscription_id}
        )
        edit_index = edit_subscription_id
    return _render_form_page("full", edit_values, edit_index=edit_index)

@app.get("/favicon.ico")
def favicon():
    return "", 204

@app.post("/defaults_preview")
def defaults_preview():
    """只读预览现有默认规则、六站摘要和共用约束依据。"""
    station_summaries = summarize_stations(request.form)
    optional_section_summaries = summarize_optional_sections(request.form)
    try:
        subscription = build_subscription(request.form)
        subscription_with_defaults = apply_default_rules(subscription)
        hard = subscription_with_defaults.get("hard_constraints") or {}
        soft = subscription_with_defaults.get("soft_preferences") or {}
        passengers = soft.get("passengers") or {}
        route_type = (
            subscription_with_defaults.get("route_type")
            or (subscription_with_defaults.get("basic") or {}).get("route_type")
        )
        max_budget = hard.get("max_budget")
        scope = str(
            hard.get("max_budget_scope")
            or hard.get("budget_scope")
            or "per_person"
        ).strip()
        comparison_budget = max_budget
        if max_budget not in (None, "") and scope == "per_person":
            comparison_budget = float(max_budget) * passenger_rate_sum(
                passengers,
                route_type,
            )
        constraint_parts = build_constraint_summary(
            hard,
            max_budget=comparison_budget,
            passengers=passengers,
            route_type=route_type,
        )
        return jsonify(
            {
                "ok": True,
                "station_summaries": station_summaries,
                "optional_section_summaries": optional_section_summaries,
                "defaults_applied": subscription_with_defaults.get(
                    "defaults_applied",
                    [],
                ),
                "chips": build_default_chips(subscription_with_defaults),
                "constraint_summary": constraint_parts,
                "constraint_summary_text": format_constraint_summary(constraint_parts),
            }
        )
    except (TypeError, ValueError) as exc:
        return jsonify(
            {
                "ok": False,
                "error": str(exc),
                "station_summaries": station_summaries,
                "optional_section_summaries": optional_section_summaries,
                "defaults_applied": [],
                "chips": [],
                "constraint_summary": [],
                "constraint_summary_text": format_constraint_summary([]),
            }
        )


@app.get("/price_hint")
def price_hint():
    origin_info = resolve_location(request.args.get("origin", ""))
    dest_info = resolve_location(request.args.get("dest", ""))
    if origin_info.get("type") == "unknown" or dest_info.get("type") == "unknown":
        return jsonify(
            {
                "has_data": False,
                "scope": "oneway",
                "route_type": "",
                "route_type_label": "待识别",
            }
        )
    route_type = infer_route_type(
        list(origin_info.get("airports") or []),
        list(dest_info.get("airports") or []),
    )
    route_meta = {
        "route_type": route_type,
        "route_type_label": ROUTE_TYPE_LABELS[route_type],
    }
    for origin in origin_info.get("airports") or []:
        for dest in dest_info.get("airports") or []:
            for route in (f"{origin}-{dest}", f"{origin}_{dest}", f"{origin}→{dest}"):
                hint = build_price_hint_from_calendar(load_calendar(route))
                if hint.get("has_data"):
                    hint["route"] = route
                    return jsonify({**hint, **route_meta})
    return jsonify({"has_data": False, "scope": "oneway", **route_meta})


@app.post("/subscribe")
def subscribe():
    try:
        print(
            "[form debug] "
            f"adult={request.form.get('adult_count') or request.form.get('passenger_adult')}, "
            f"child={request.form.get('child_count') or request.form.get('passenger_child')}, "
            f"elderly={request.form.get('elderly_count') or request.form.get('passenger_elderly')}, "
            f"infant={request.form.get('infant_count') or request.form.get('passenger_infant')}, "
            f"passenger_count={request.form.get('passenger_count')}"
        )
        print("[表单] 开始构建订阅")
        subscription = build_subscription(request.form)
        print("[表单] 订阅构建完成")

        print("[表单] 开始保存订阅")
        repository = _subscription_repository()
        edit_reference = str(request.form.get("subscription_index") or "").strip()
        edit_subscription_id = _resolve_edit_subscription_id(edit_reference)
        if edit_reference:
            if edit_subscription_id is None:
                raise ValueError("订阅已删除或编辑链接已失效，请返回列表重试。")
            saved_subscription = repository.update(
                LOCAL_OWNER_ID,
                edit_subscription_id,
                subscription,
            )
            if saved_subscription is None:
                raise ValueError("订阅已删除或不属于当前用户，请返回列表重试。")
        else:
            had_identity = bool(stable_subscription_id(subscription))
            saved_subscription = repository.create(LOCAL_OWNER_ID, subscription)
            if not had_identity:
                safe_log(
                    "[身份迁移] 保存路径 "
                    f"subscription_id={stable_subscription_id(saved_subscription)}"
                )
        saved_subscription_id = stable_subscription_id(saved_subscription)
        if not saved_subscription_id:
            raise ValueError("订阅保存后缺少稳定身份。")
        index = _subscription_position(saved_subscription_id)
        print(
            "[表单] 订阅保存完成: "
            f"subscription_id={saved_subscription_id} index={index}"
        )

        print("[表单] 开始触发后台采集")
        background_subscription = {**saved_subscription, "_index": index}
        startup_result = _startup_result(
            start_background_collection(background_subscription),
            default_status="confirming",
        )
        print(f"[表单] 后台采集触发完成: status={startup_result['status']}")
        _remember_startup_handshake(background_subscription, startup_result)

        return redirect(
            url_for("success", subscription_id=saved_subscription_id)
        )
    except ValueError as exc:
        print(f"[表单] 提交订阅失败: {exc}")
        page_mode = "full" if request.form.get("form_page") == "full" else "quick"
        values = _submitted_form_values(request.form)
        edit_index = str(request.form.get("subscription_index") or "").strip() or None
        return _render_form_page(
            page_mode,
            values,
            edit_index=edit_index,
            form_error=str(exc),
        ), 400
    except SubscriptionIdentityMigrationRequired as exc:
        return _identity_migration_required_response(exc)
    except DuplicateSubscriptionIdError as exc:
        return _duplicate_subscription_id_response(exc)
    except Exception as exc:
        print(f"[表单] 提交订阅失败: {exc}")
        traceback.print_exc()
        raise


@app.get("/subscriptions")
def subscription_list():
    try:
        subscriptions = _require_persisted_subscription_identities(
            load_subscriptions()
        )
    except SubscriptionIdentityMigrationRequired as exc:
        return _identity_migration_required_response(exc)
    return render_template_string(
        LIST_TEMPLATE,
        items=build_subscription_list_items(subscriptions),
    )


@app.post("/subscriptions/<subscription_id>/toggle")
def toggle_subscription(subscription_id: str):
    repository = _subscription_repository()

    def mutate(subscription):
        current = subscription.get("status", "active")
        subscription["status"] = "paused" if current == "active" else "active"
        return subscription

    if repository.mutate(
        LOCAL_OWNER_ID,
        subscription_id,
        mutate,
    ) is None:
        abort(404)
    return redirect(url_for("subscription_list"))


@app.route("/subscription/<subscription_id>/delete", methods=["GET", "POST"])
def delete_subscription(subscription_id: str):
    canonical_id = canonical_detail_uuid(subscription_id)
    if canonical_id is None:
        abort(404)
    repository = _subscription_repository()
    matched = repository.get(
        LOCAL_OWNER_ID,
        canonical_id,
    )
    if matched is None:
        abort(404)
    route = _subscription_route_text(matched)
    if request.method == "GET":
        return render_template_string(
            DELETE_CONFIRM_TEMPLATE,
            route=route,
            error="",
        )
    if request.form.get("confirm_delete") != "yes":
        return render_template_string(
            DELETE_CONFIRM_TEMPLATE,
            route=route,
            error="必须明确确认后才能删除这条监控。",
        ), 400

    if not repository.delete(LOCAL_OWNER_ID, canonical_id):
        abort(404)
    return redirect(url_for("subscription_list"))


@app.post("/subscriptions/<subscription_id>/quick-update")
def quick_update_subscription(subscription_id: str):
    field = request.form.get("field", "")
    value = request.form.get("value", "")
    ok = update_subscription_preference(subscription_id, field, value)
    print(
        "[表单] 快捷更新偏好: "
        f"subscription_id={subscription_id}, field={field}, ok={ok}"
    )
    if not ok:
        abort(404)
    return redirect(url_for("success", subscription_id=subscription_id))


@app.get("/success")
def success():
    subscriptions = load_subscriptions()
    requested_id = str(request.args.get("subscription_id") or "").strip()
    index = None
    subscription = {}
    if requested_id:
        subscription = _subscription_repository().get(
            LOCAL_OWNER_ID,
            requested_id,
        ) or {}
        if subscription:
            migrated, _details = migrate_subscription_defaults([subscription])
            subscription = migrated[0]
            index = _subscription_position(requested_id)
    elif subscriptions:
        try:
            index = int(request.args.get("index", len(subscriptions) - 1))
        except ValueError:
            index = len(subscriptions) - 1
        if 0 <= index < len(subscriptions):
            subscription = subscriptions[index]
            requested_id = stable_subscription_id(subscription) or ""
    stored_attempt = subscription.get("last_attempt") or {}
    session_attempt = _take_startup_handshake(subscription)
    selected_attempt = _select_startup_attempt(stored_attempt, session_attempt)
    display_status = str(selected_attempt.get("status") or "started")
    return render_template_string(
        SUCCESS_TEMPLATE,
        summary=build_success_summary(subscription) if subscription else {},
        first_push_time=first_push_text(),
        index=index,
        subscription_id=requested_id or None,
        startup_status=display_status,
        startup_message=startup_status_message(display_status),
    )


@app.route("/feedback", methods=["GET", "POST"])
def feedback():
    subscription_id = request.values.get("sub") or request.values.get("subscription_id") or ""
    if request.method == "POST":
        record = {
            "subscription_id": request.form.get("subscription_id") or subscription_id,
            "feedback_type": request.form.get("feedback_type", ""),
            "unavailable_reason": request.form.get("unavailable_reason", ""),
            "comment": request.form.get("comment", "").strip(),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "user_agent": request.headers.get("User-Agent", ""),
        }
        save_feedback(record)
        notify_feedback_author(record)
        return render_template_string(
            FEEDBACK_TEMPLATE,
            saved=True,
            subscription_id=record["subscription_id"],
        )
    return render_template_string(
        FEEDBACK_TEMPLATE,
        saved=False,
        subscription_id=subscription_id,
    )


@app.route("/detail")
def detail():
    subscription_id = canonical_detail_uuid(request.args.get("sub", ""))
    if subscription_id is None:
        abort(404)
    if not detail_token_authorized(request.args.get("token")):
        abort(404)
    matched = _load_payload_result(subscription_id)
    if matched is None:
        abort(404)
    return render_template_string(DETAIL_TEMPLATE, result=matched)
