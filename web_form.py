"""Flask form for flight monitor subscriptions."""

from __future__ import annotations

import json
import re
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template_string, request, url_for

from airports import AIRPORT_SHORT_NAMES, CITY_AIRPORTS, CITY_ALIASES, format_airport, resolve_location
from analyzer import apply_default_rules, build_price_hint_from_calendar
from filename_utils import sanitize_filename
from price_calendar import load_calendar


BASE_DIR = Path(__file__).parent
SUBSCRIPTIONS_PATH = BASE_DIR / "data" / "subscriptions.json"
FEEDBACK_PATH = BASE_DIR / "data" / "feedback.json"
PAGE_RESULTS_PATH = BASE_DIR / "data" / "page_results.json"
PAGE_PAYLOADS_DIR = BASE_DIR / "data" / "payloads"
load_dotenv(BASE_DIR / ".env", encoding="utf-8")

app = Flask(__name__)

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

COMMON_ORIGINS = [
    ("上海", "上海（浦东PVG + 虹桥SHA）"),
    ("北京", "北京（首都PEK + 大兴PKX）"),
    ("广州", "广州（白云CAN）"),
    ("深圳", "深圳（宝安SZX）"),
    ("成都", "成都（天府CTU）"),
    ("杭州", "杭州（萧山HGH）"),
    ("南京", "南京（禄口NKG）"),
    ("OTHER", "其他（手动输入）"),
]

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
      max-width: 640px;
      margin: 24px auto;
      padding: 0 16px 36px;
      color: #222;
      line-height: 1.55;
      background: #fff;
    }
    h1 { font-size: 26px; margin-bottom: 6px; }
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
    .hint { color: #666; font-size: 13px; margin-top: 4px; }
    .muted-option {
      opacity: 0.55;
    }
    .inline-warning {
      display: none;
      color: #777;
      font-size: 13px;
      margin: 8px 0 0;
    }
    .field-error {
      display: none;
      color: #c5221f;
      font-size: 13px;
      margin: 6px 0 0;
    }
    .field-invalid {
      border-color: #c5221f !important;
      background: #fff7f7;
    }
    .inline-suggestion {
      border: 0;
      background: transparent;
      color: #1a73e8;
      cursor: pointer;
      font-size: 13px;
      font-weight: 700;
      margin: 0;
      padding: 0;
      text-decoration: underline;
      width: auto;
    }
    #price-hint {
      border-left: 3px solid #d7e3f7;
      padding-left: 8px;
    }
    .server-error {
      background: #fef2f2;
      border: 1px solid #fecaca;
      border-radius: 8px;
      color: #b91c1c;
      font-size: 14px;
      margin: 0 0 14px;
      padding: 10px 12px;
    }
    #missing-required-warning {
      display: none;
      color: #c5221f;
      font-size: 14px;
      margin: 10px 0 0;
    }
    .template-banner {
      display: none;
      border: 1px solid #c8d6f0;
      border-radius: 8px;
      background: #f7f9fc;
      padding: 10px 12px;
      margin: 12px 0;
      font-size: 14px;
    }
    .template-banner button {
      width: auto;
      margin: 6px 8px 0 0;
      padding: 7px 10px;
      font-size: 14px;
    }
    .required-progress {
      border: 1px solid #d7e3f7;
      border-radius: 8px;
      background: #f7f9fc;
      padding: 10px 12px;
      margin: 12px 0;
      color: #333;
      font-size: 14px;
    }
    .required-progress.incomplete {
      border-color: #f4b4ad;
      background: #fff6f5;
      color: #a50e0e;
    }
    .required-progress-title {
      font-weight: 700;
      margin-bottom: 4px;
    }
    .required-progress ul {
      margin: 6px 0 0;
      padding-left: 20px;
    }
    .required-progress li {
      margin: 2px 0;
    }
    .submit-preview {
      border-left: 4px solid #1a73e8;
      padding: 8px 12px;
      margin: 14px 0 8px;
      background: #f7f9fc;
      color: #333;
      font-size: 14px;
    }
    .default-rules-note {
      border: 1px solid #dbe5f6;
      background: #f7fbff;
      border-radius: 8px;
      padding: 10px 12px;
      margin: 14px 0;
      color: #333;
      font-size: 14px;
    }
    .default-rules-note button {
      margin-top: 8px;
    }
    .quick-mode-top {
      border: 1px solid #dbe5f6;
      background: #f7fbff;
      border-radius: 8px;
      color: #333;
      font-size: 14px;
      margin: 10px 0 12px;
      padding: 10px 12px;
    }
    .quick-mode-top button {
      margin-top: 8px;
    }
    .summary-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: start;
      margin: 6px 0;
    }
    .summary-row-edit {
      border: 1px solid #c8d6f0;
      border-radius: 6px;
      background: #fff;
      color: #1a73e8;
      cursor: pointer;
      font-size: 13px;
      line-height: 1.2;
      margin: 0;
      padding: 6px 9px;
      width: auto;
      white-space: nowrap;
    }
    .summary-highlight {
      outline: 3px solid #fbbc04;
      outline-offset: 4px;
      border-radius: 6px;
      transition: outline-color 0.2s ease;
    }
    .summary-section-title {
      list-style: none;
      margin: 10px 0 4px -18px;
      font-weight: bold;
      color: #1a73e8;
    }
    .summary-default-rule {
      color: #188038;
    }
    .summary-advanced-rule {
      color: #5f3dc4;
    }
    .module-heading {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin: 8px 0 6px;
    }
    .module-heading label,
    .module-heading strong {
      margin: 0;
    }
    .reset-module {
      border: 0;
      background: transparent;
      color: #1a73e8;
      padding: 0;
      font-size: 13px;
      text-decoration: underline;
      cursor: pointer;
    }
    .link-button {
      border: 0;
      background: transparent;
      color: #1a73e8;
      padding: 0;
      font-size: inherit;
      text-decoration: underline;
      cursor: pointer;
    }
    .strict-warning {
      display: none;
      border: 1px solid #f4c542;
      background: #fff8e1;
      color: #7a4f00;
      border-radius: 8px;
      padding: 10px 12px;
      margin: 10px 0;
      font-size: 14px;
    }
    .pref-cards {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
      margin: 12px 0 16px;
    }
    .pref-card {
      border: 1px solid #dbe5f6;
      border-radius: 8px;
      background: #fff;
      padding: 12px;
    }
    .pref-name {
      font-weight: bold;
      color: #1a73e8;
      margin-bottom: 4px;
    }
    .pref-value {
      min-height: 36px;
      color: #555;
      font-size: 13px;
      margin-bottom: 8px;
    }
    .pref-card-detail {
      display: none;
      border-left: 3px solid #dbe5f6;
      padding-left: 10px;
      margin: 10px 0 14px;
    }
    .pref-card-detail.open {
      display: block;
    }
    .quick-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 10px 0;
    }
    .quick-actions form {
      display: inline;
    }
    .secondary-button {
      background: #f5f7fb;
      color: #1a73e8;
      border: 1px solid #c8d6f0;
    }
    .mode-toggle {
      border: 1px solid #d7e3f7;
      border-radius: 10px;
      background: #f7f9fc;
      padding: 12px;
      margin: 16px 0;
    }
    .mode-toggle-title {
      font-weight: bold;
      margin-bottom: 8px;
    }
    .airport-tags {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 8px;
    }
    .airport-tag {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid #c8d6f0;
      border-radius: 999px;
      background: #eef6ff;
      color: #174ea6;
      padding: 5px 9px;
      font-size: 13px;
    }
    .airport-tag button {
      width: auto;
      margin: 0;
      padding: 0 2px;
      border: 0;
      background: transparent;
      color: #174ea6;
      font-size: 14px;
      line-height: 1;
    }
    .airport-tag button:disabled {
      color: #999;
      cursor: not-allowed;
    }
    .smart-panel {
      overflow: hidden;
      max-height: 0;
      opacity: 0;
      transition: max-height 0.25s ease, opacity 0.2s ease;
    }
    .smart-panel.open {
      max-height: 2600px;
      opacity: 1;
    }
    button {
      margin-top: 20px;
      background: #1a73e8;
      color: white;
      border: 0;
      font-weight: bold;
      cursor: pointer;
    }
    #return-date-wrap,
    #advanced-preferences,
    #advanced-rules,
    #summary-card {
      display: none;
    }
    .sub-options {
      border-left: 3px solid #d7e3f7;
      margin: 10px 0 0 10px;
      padding-left: 12px;
    }
    .auto-notice {
      display: none;
      border: 1px solid #cce1ff;
      background: #eef6ff;
      color: #174ea6;
      border-radius: 8px;
      padding: 10px 12px;
      margin: 14px 0;
      font-size: 14px;
    }
    .meeting-handoff-card {
      display: none;
      border: 1px solid #c8d6f0;
      background: #f7f9fc;
      color: #333;
      border-radius: 8px;
      padding: 12px;
      margin: 10px 0 14px;
      font-size: 14px;
    }
    .meeting-handoff-card strong {
      display: block;
      margin-bottom: 6px;
      color: #174ea6;
    }
    .time-controls-disabled {
      opacity: 0.48;
      pointer-events: none;
      filter: grayscale(0.2);
    }
    .auto-suggested {
      color: #174ea6;
      text-decoration: underline;
      text-underline-offset: 3px;
    }
    .time-preferences {
      border-radius: 8px;
      padding: 12px;
      margin: 14px 0;
    }
    .time-preferences strong,
    .time-preferences legend {
      display: block;
      margin-bottom: 8px;
    }
    .time-outbound { background: #eef6ff; }
    .time-return { background: #eefaf3; }
    #round-trip-time-preferences { display: none; }
    #custom-time-options,
    #precise-time-options,
    #overnight-transfer-options,
    #self-transfer-options,
    #email-reminder-wrap,
    #page-only-hint {
      display: none;
    }
    #budget-amount-fields {
      display: block;
    }
    #summary-card {
      border: 1px solid #c8d6f0;
      border-radius: 8px;
      background: #f7f9fc;
      padding: 16px;
      margin-top: 20px;
    }
    #summary-card h2 { margin-top: 0; font-size: 20px; }
    #summary-card ul { padding-left: 22px; }
    #mobile-stepper,
    #step-nav {
      display: none;
    }
    #mobile-stepper {
      border: 1px solid #d7e3f7;
      border-radius: 8px;
      background: #f7f9fc;
      padding: 12px;
      margin: 16px 0;
      text-align: center;
      font-weight: 700;
    }
    .step-dots { letter-spacing: 6px; }
    .button-row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    #mobile-action-bar {
      display: none;
    }
    .step-nav-row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    @media (max-width: 720px) {
      #mobile-stepper,
      #step-nav {
        display: block;
      }
      .form-step {
        display: none;
      }
      .form-step.active {
        display: block;
      }
      #preview-button {
        display: none;
      }
      #preview-button.step-final-visible {
        display: block;
      }
      body {
        padding-bottom: 96px;
      }
      #mobile-action-bar {
        align-items: center;
        background: #fff;
        border-top: 1px solid #e5e7eb;
        bottom: 0;
        box-shadow: 0 -4px 14px rgba(0,0,0,0.08);
        display: flex;
        gap: 10px;
        left: 0;
        padding: 10px 14px;
        position: fixed;
        right: 0;
        z-index: 20;
      }
      #mobile-action-text {
        color: #333;
        flex: 1;
        font-size: 13px;
        line-height: 1.35;
      }
      #mobile-action-bar button {
        flex: 0 0 auto;
        margin: 0;
        padding: 10px 12px;
        width: auto;
      }
      #mobile-summary-button {
        background: #f5f7fb;
        border: 1px solid #c8d6f0;
        color: #1a73e8;
      }
    }
    @media (max-width: 520px) {
      .button-row { grid-template-columns: 1fr; }
      .step-nav-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <h1>航班监控订阅</h1>
  <p><a href="{{ url_for('subscription_list') }}">查看我的所有监控 →</a></p>
  <p class="hint">先填基础需求即可；高级偏好可以按需展开。</p>
  <div id="saved-template-banner" class="template-banner">
    检测到上次的偏好设置，是否套用？
    <div id="saved-template-summary" class="hint"></div>
    <div>
      <button id="apply-template-button" class="secondary-button" type="button">套用</button>
      <button id="ignore-template-button" class="secondary-button" type="button">忽略</button>
      <button id="clear-template-button" class="secondary-button" type="button">清除已保存偏好</button>
    </div>
  </div>

  <div id="mobile-stepper">
    <div class="step-dots" id="step-dots">● ○ ○ ○</div>
    <div id="step-label">第1步/共4步：行程信息</div>
  </div>

  <form id="subscription-form" method="post" action="{{ url_for('subscribe') }}">
    <input type="hidden" id="subscription_index" name="subscription_index" value="{{ edit_index if edit_index is not none else '' }}">
    {% if form_error %}
      <div class="server-error">{{ form_error }}</div>
    {% endif %}
    <div class="mode-toggle">
      <div class="mode-toggle-title">模式</div>
      <div class="choice">
        <label><input type="radio" name="monitor_mode" value="quick" checked> 快速监控</label>
        <label><input type="radio" name="monitor_mode" value="precise"> 精准监控</label>
      </div>
      <p class="hint">快速监控只填写基础信息；精准监控会展开补充偏好和筛选规则。</p>
    </div>
    <div id="quick-defaults-note" class="quick-defaults-note quick-mode-top">
      快速模式已启用默认安全规则：避免红眼、优先含行李、不优先非联程、仅重要变化提醒。需要细调？
      <button id="open-precise-mode" class="secondary-button" type="button">切换精准模式</button>
    </div>
    <div id="required-progress" class="required-progress incomplete">
      <div class="required-progress-title">还需填写：</div>
      <ul id="required-missing-list"></ul>
    </div>

    <fieldset class="form-step active" data-step="1">
      <legend>行程信息</legend>

      <label>航线类型</label>
      <div class="choice">
        <label><input type="radio" name="route_type" value="domestic" checked> 国内</label>
        <label><input type="radio" name="route_type" value="international"> 国际</label>
        <label><input type="radio" name="route_type" value="greater_china"> 港澳台</label>
      </div>
      <p class="hint">系统会根据出发地/目的地自动判断默认值，你也可以手动修改。</p>
      <p class="hint" data-show-if="route_type=domestic">国内航线将重点启用多机场对比、当天往返、准点率、有效出行成本和报销友好提示。</p>
      <p class="hint" data-show-if="route_type=international">国际航线将重点提示中转风险、联程/非联程、过境签、时区时差、行李规则和OTA验证风险。</p>
      <p class="hint" data-show-if="route_type=greater_china">港澳台航线会提示港澳通行证/台湾通行证及有效签注，并保留多机场、行李和渠道验证提示。</p>
      <p class="hint" data-show-if="route_type=international|greater_china">跨境航线会把行李规则、渠道验证和票规差异作为共同重点提醒。</p>
      <p class="hint" data-show-if="route_type=international">国际航线会把行李规则、时区时差、过境签和联程风险作为重点提醒。</p>
      <p class="hint" data-show-if="route_type=greater_china">港澳台航线一般不做过境签判断，渠道建议按国内OTA+航司官网验证。</p>

      <label for="origin">出发地</label>
      <select id="origin" name="origin_select">
        {% for code, label in origins %}
        <option value="{{ code }}">{{ label }}</option>
        {% endfor %}
      </select>
      <input name="origin_manual" placeholder="或手动输入城市名/机场代码，例如上海或PVG">
      <p id="origin-validation-error" class="field-error"></p>
      <p id="origin-airport-hint" class="hint"></p>
      <div id="origin-airport-tags" class="airport-tags"></div>
      <input id="origin_airports_active" name="origin_airports_active" type="hidden">

      <label for="destination">目的地</label>
      <input id="destination" name="destination" placeholder="输入城市名（如大阪、东京）或机场代码（如KIX）" required>
      <p id="destination-validation-error" class="field-error"></p>
      <p id="destination-airport-hint" class="hint"></p>
      <div id="destination-airport-tags" class="airport-tags"></div>
      <input id="destination_airports_active" name="destination_airports_active" type="hidden">

      <label>单程 / 往返</label>
      <div class="choice">
        <label><input type="radio" name="round_trip" value="false" checked> 单程</label>
        <label><input type="radio" name="round_trip" value="true"> 往返</label>
      </div>

      <label for="depart_date">出发日期</label>
      <input id="depart_date" name="depart_date" type="date" required>
      <p id="depart-date-error" class="field-error"></p>
      <div data-show-if="route_type=domestic">
        <label class="hint" style="display:block;margin-top:8px;">
          <input id="same_day_round_trip" name="same_day_round_trip" type="checkbox" value="true">
          当天往返（商务/会议）
        </label>
      </div>
      <div id="same-day-business-fields" data-show-if="same_day_round_trip=true">
        <label>当天往返安排</label>
        <div class="inline-grid">
          <label>会议/办事开始时间 <input name="business_start" type="time" value="10:00"></label>
          <label>会议/办事结束时间 <input name="business_end" type="time" value="16:00"></label>
        </div>
        <p class="hint">快速模式会按机场等级、车程估算和25分钟冗余自动预留。</p>
      </div>

      <div id="trip-feasibility-fields" class="precise-only">
        <label>行程可行性分析(选填,填了才分析)</label>
        <div class="inline-grid">
          <label>去程:计划动身前往机场的时间 <input name="outbound_set_off" type="time"></label>
          <label data-show-if="round_trip=true">返程:计划动身前往机场的时间 <input name="return_set_off" type="time"></label>
        </div>
        <p id="airport-buffer-preview" class="hint">机场标准缓冲:系统按航线类型和机场等级自动设定</p>
        <label>机场车程(分钟,选填)
          <input name="user_transport_min" type="number" min="0" step="5" placeholder="不填则按机场到市区估算">
        </label>
        <label>路途冗余(在车程之上额外预留)</label>
        <div class="choice">
          <label><input type="radio" name="transport_margin_mode" value="tight"> 紧凑 +15%</label>
          <label><input type="radio" name="transport_margin_mode" value="standard" checked> 标准 +30%(推荐)</label>
          <label><input type="radio" name="transport_margin_mode" value="loose"> 宽松 +50%</label>
        </div>
      <p id="transport-margin-hint" class="hint"></p>
      <p class="hint">路途冗余是在车程之上额外留出余量，应对堵车。</p>
        <label>安全余量</label>
        <div class="choice">
          <label><input type="radio" name="redundancy_min" value="15"> 15分钟</label>
          <label><input type="radio" name="redundancy_min" value="25" checked> 25分钟(默认)</label>
          <label><input type="radio" name="redundancy_min" value="40"> 40分钟</label>
        </div>
        <p class="hint" data-show-if="route_type=domestic">国内:系统将叠加值机安检缓冲(按机场75-110分钟)。</p>
        <p class="hint" data-show-if="route_type=international">国际:系统将叠加值机+出境边检海关缓冲(150-180分钟),到达后另提示入境+提行李时间。</p>
        <p class="hint" data-show-if="route_type=greater_china">港澳台:系统将叠加值机+出入境查验缓冲(120-150分钟)。</p>
      </div>

      <div id="return-date-wrap" data-show-if="round_trip=true">
        <label for="return_date">返程日期</label>
        <input id="return_date" name="return_date" type="date">
        <p id="return-date-error" class="field-error"></p>

        <label>返程日期灵活度</label>
        <div class="choice">
          <label><input type="radio" name="return_date_flexibility" value="0" checked> 不灵活</label>
          <label><input type="radio" name="return_date_flexibility" value="1"> 前后1天</label>
          <label><input type="radio" name="return_date_flexibility" value="3"> 前后3天</label>
          <label><input type="radio" name="return_date_flexibility" value="7"> 前后7天</label>
        </div>
      </div>

      <label>出发日期可以调整吗？</label>
      <div class="choice">
        <label><input type="radio" name="date_flexibility" value="0" checked> 不能调，就这天</label>
        <label><input type="radio" name="date_flexibility" value="1"> 前后1天可以</label>
        <label><input type="radio" name="date_flexibility" value="3"> 前后3天都行</label>
        <label><input type="radio" name="date_flexibility" value="7"> 前后一周都行</label>
      </div>
      <p class="hint">用于寻找前后日期的低价航班</p>

    </fieldset>

    <fieldset class="form-step" data-step="2">
      <legend>价格和中转</legend>

      <label>价格策略</label>
      <div class="choice">
        <label><input type="radio" name="price_strategy" value="explicit" checked> 1. 我有明确价格</label>
        <label><input type="radio" name="price_strategy" value="auto_judge"> 2. 我不知道合理价，帮我判断</label>
        <label><input type="radio" name="price_strategy" value="low_price_alert"> 3. 只要进入低价区间就提醒我</label>
      </div>

      <div id="budget-amount-fields" data-show-if="price_strategy=explicit">
      <label>最高可接受价格（超过这个价通常不考虑）</label>
      <input id="max_budget" name="max_budget" type="number" min="1" step="1" placeholder="例如 8000">
      <p class="hint">超过这个价通常不作为主推荐</p>
      <p id="max-budget-error" class="field-error"></p>
      <input type="hidden" name="max_budget_mode" value="fixed">

      <label>理想入手价格（可选，到这个价格就值得买）</label>
      <input id="target_price" name="target_price" type="number" min="1" step="1" placeholder="例如 6000（选填）">
      <p class="hint">到这个价附近就提醒你，不等于最高预算</p>
      <p id="price-validation-error" class="field-error">理想入手价应低于最高可接受价，请确认是否填反了</p>
      <p id="price-hint" class="hint"></p>
      <input type="hidden" name="target_price_mode" value="fixed">
      </div>

      <input type="hidden" name="price_tolerance_mode" value="100">
      <input id="price_tolerance_custom" name="price_tolerance_custom" type="hidden">
      <p class="hint">中转接受程度会影响是否推荐低价中转方案；托运行李会影响实际支付价，不含行李的低价会被降权。</p>

      <label>中转接受程度</label>
      <div class="choice">
        <label><input type="radio" name="transfer_policy" value="direct_only"> 必须直飞</label>
        <label><input type="radio" name="transfer_policy" value="reasonable" checked> 可以接受合理中转</label>
        <label><input type="radio" name="transfer_policy" value="price_first"> 价格优先，中转也可以</label>
      </div>

      <label>是否需要托运行李</label>
      <div class="choice">
        <label><input type="radio" name="baggage" value="required" checked> 必须</label>
        <label><input type="radio" name="baggage" value="not_needed"> 不需要</label>
        <label><input type="radio" name="baggage" value="unknown"> 不确定</label>
      </div>
    </fieldset>

    <fieldset class="form-step" data-step="3">
      <legend>监控目标与提醒</legend>
      <label>主目标</label>
      <div class="choice">
        <label><input type="radio" name="primary_goal" value="price_drop_alert" required> 找到合适价格时提醒我 <small style="color:gray">（适合还没急着买，等低价）</small></label>
        <label><input type="radio" name="primary_goal" value="buy_timing" checked required> 判断现在该不该买 <small style="color:gray">（适合已看到价格，想知道能不能下手）</small></label>
        <label><input type="radio" name="primary_goal" value="cheaper_date" required> 帮我找更便宜的日期 <small style="color:gray">（适合日期可以调整）</small></label>
        <label><input type="radio" name="primary_goal" value="best_overall" required> 帮我找最合适航班 <small style="color:gray">（不只看价格，综合时间/行李/中转）</small></label>
      </div>

      <label>提醒方式</label>
      <div class="choice">
        <label><input type="radio" name="notification_method" value="email"> 邮箱</label>
        <label><input type="radio" name="notification_method" value="pushplus" checked> PushPlus微信推送</label>
        <label><input type="radio" name="notification_method" value="both"> 邮箱 + 微信(PushPlus)都接收</label>
        <label><input type="radio" name="notification_method" value="page_only"> 暂时只在页面查看</label>
      </div>
      <div id="email-reminder-wrap" data-show-if="notification_method=email|both">
        <label for="notification_email">邮箱地址</label>
        <input id="notification_email" name="notification_email" type="email" placeholder="you@example.com">
        <p class="hint">支持任意邮箱。注意：Gmail在国内需翻墙才能查看，推荐用QQ/163/Outlook</p>
        <p id="email-error" class="inline-warning"></p>
      </div>
      <p id="page-only-hint" class="hint" data-show-if="notification_method=page_only">你可以稍后在订阅列表查看监控结果</p>

      <label>提醒频率</label>
      <div class="choice">
        <label><input type="radio" name="notification_frequency" value="important_only" checked> 仅重要变化提醒</label>
        <label><input type="radio" name="notification_frequency" value="daily_digest"> 每天汇总一次</label>
        <label><input type="radio" name="notification_frequency" value="price_change"> 价格变化就提醒</label>
      </div>
      <p class="hint">提醒频率越高越及时，但打扰也更多。</p>
      <input id="notification_frequency_rule_shadow" type="hidden" name="notification_frequency_rule" value="important_only">
      <div class="sub-options" data-show-if="notification_frequency=price_change">
        <label>什么算价格变化？</label>
        <div class="choice">
          <label><input type="radio" name="price_change_threshold" value="any"> 每次变化</label>
          <label><input type="radio" name="price_change_threshold" value="down_100" checked> 降超100元</label>
          <label><input type="radio" name="price_change_threshold" value="down_300"> 降超300元</label>
          <label><input type="radio" name="price_change_threshold" value="low_zone"> 进入低价区间</label>
        </div>
      </div>
      <div class="sub-options" data-show-if="notification_frequency=daily_digest">
        <label>汇总时间</label>
        <div class="choice">
          <label><input type="radio" name="digest_time" value="09:00" checked> 早9点</label>
          <label><input type="radio" name="digest_time" value="12:00"> 中午12点</label>
          <label><input type="radio" name="digest_time" value="20:00"> 晚8点</label>
        </div>
      </div>

      <div class="quick-only">
      <label>购票人数</label>
      <input type="number" name="passenger_count" min="1" value="1">
      </div>

      <label>本次出行场景（可多选）</label>
      <div class="choice">
        <label><input type="checkbox" name="travel_scenario" value="personal" checked> 个人出行</label>
        <label><input type="checkbox" name="travel_scenario" value="business"> 商务/会议</label>
        <label><input type="checkbox" name="travel_scenario" value="tourism"> 旅游</label>
        <label><input type="checkbox" name="travel_scenario" value="family_visit"> 探亲/回家</label>
        <label><input type="checkbox" name="travel_scenario" value="family"> 家庭/亲子</label>
        <label><input type="checkbox" name="travel_scenario" value="elderly"> 有老人同行</label>
        <label><input type="checkbox" name="travel_scenario" value="important"> 重要事项（考试/婚礼/医疗/邮轮等）</label>
        <label><input type="checkbox" name="travel_scenario" value="price_first"> 价格优先</label>
      </div>
      <p class="hint">出行场景用于自动调整价格、时间、风险和舒适度的权重。</p>
      <div id="travel-scenario-notice" class="auto-notice"></div>

      <button id="advanced-toggle" class="secondary-button precise-only" type="button">＋ 补充偏好，让推荐更准确</button>
      <div id="advanced-preferences" class="smart-panel precise-only">
      <fieldset>
        <legend>提高推荐准确度</legend>
        <p class="hint">不填也可以，系统会按普通出行默认规则监控</p>

        <div class="pref-cards">
          <div class="pref-card">
            <div class="pref-name">同行人员</div>
            <div class="pref-value" id="pref-value-companion" data-pref-value="companions">未设置</div>
            <button class="secondary-button pref-card-button" type="button" data-pref-target="companions">补充</button>
          </div>
          <div class="pref-card">
            <div class="pref-name">时间偏好</div>
            <div class="pref-value" id="pref-value-time" data-pref-value="time">使用默认：避免红眼</div>
            <button class="secondary-button pref-card-button" type="button" data-pref-target="time">修改</button>
          </div>
          <div class="pref-card">
            <div class="pref-name">退改签</div>
            <div class="pref-value" id="pref-value-refund" data-pref-value="refund">使用默认：便宜优先，提醒风险</div>
            <button class="secondary-button pref-card-button" type="button" data-pref-target="refund">修改</button>
          </div>
          <div class="pref-card">
            <div class="pref-name">航司偏好</div>
            <div class="pref-value" id="pref-value-airline" data-pref-value="airline">使用默认：不限</div>
            <button class="secondary-button pref-card-button" type="button" data-pref-target="airline">修改</button>
          </div>
          <div class="pref-card">
            <div class="pref-name">非联程中转</div>
            <div class="pref-value" id="pref-value-self-transfer" data-pref-value="self_transfer">使用默认：不接受</div>
            <button class="secondary-button pref-card-button" type="button" data-pref-target="self_transfer">修改</button>
          </div>
        </div>

        <div id="pref-detail-companions" class="pref-card-detail">
        <label>购票人数</label>
        <div class="inline-grid">
          <label>成人 <input type="number" name="adult_count" min="0" value="1"></label>
          <label>儿童 <input type="number" name="child_count" min="0" value="0"></label>
          <label>老人 <input type="number" name="elderly_count" min="0" value="0"></label>
          <label>婴儿 <input type="number" name="infant_count" min="0" value="0"></label>
        </div>
        <input type="hidden" name="companions" value="solo">
        <div>
          <p class="hint">（可选）具体约束，不需要填写年龄或性别，按实际出行限制选择即可。</p>
          <div class="choice">
            <label><input type="checkbox" name="companion_constraints" value="direct_preferred"> 需要尽量直飞</label>
            <label><input type="checkbox" name="companion_constraints" value="no_redeye"> 不接受红眼/凌晨到达</label>
            <label><input type="checkbox" name="companion_constraints" value="avoid_long_layover"> 不适合长时间中转</label>
            <label><input type="checkbox" name="companion_constraints" value="need_baggage"> 需要托运行李</label>
            <label><input type="checkbox" name="companion_constraints" value="need_refund_change"> 需要可退改</label>
            <label><input type="checkbox" name="companion_constraints" value="daytime_arrival"> 希望白天到达</label>
            <label><input type="checkbox" name="companion_constraints" value="limited_mobility"> 有行动不便，不适合长时间步行/换乘</label>
          </div>
        </div>
        <p class="hint" data-show-if="companions=group">多人同行：低价库存可能不足，系统会提高最终支付价校验和库存可购买性权重。</p>
        <label>其他实际需求（可选）</label>
        <div class="choice">
          <label><input type="checkbox" name="solo_travel" value="true"> 独自出行</label>
          <label><input type="checkbox" name="no_late_arrival" value="true"> 不接受深夜到达</label>
          <label><input type="checkbox" name="prefer_daytime_arrival" value="true"> 希望优先白天到达</label>
        </div>
        <div id="auto-preference-notice" class="auto-notice"></div>
        </div>

        <input type="hidden" name="departure_time_policy" value="any">
        <input type="hidden" name="trip_rigidity" value="confirmed">

        <div id="pref-detail-time" class="pref-card-detail">
        <div class="module-heading">
          <label>时间偏好</label>
          <button class="reset-module" type="button" data-reset-module="time">恢复默认</button>
        </div>
        <div id="meeting-time-handoff-card" class="meeting-handoff-card" aria-live="polite">
          <strong>⏱ 时间安排已由会议模式接管</strong>
          <div id="meeting-time-handoff-text">
            系统将按你的会议时间 + 预留时间自动推算航班窗口。
          </div>
          <p class="hint">精确窗口会按目的地机场车程计算，提交后生效。想手动设置时间？取消会议模式。</p>
        </div>
        <div id="time-preference-controls">
        <div class="choice">
          <label><input type="radio" name="time_preference" value="unlimited" checked> 不限制</label>
          <label><input type="radio" name="time_preference" value="daytime"> 白天优先</label>
          <label><input type="radio" name="time_preference" value="no_redeye"> 不接受红眼/凌晨到达</label>
          <label><input type="radio" name="time_preference" value="custom"> 自定义时间段</label>
        </div>

        <div id="custom-time-options" data-show-if="time_preference=custom">
        <p class="hint">这些设置不是必填项。保持默认时，系统会按普通出行规则处理。</p>
        <fieldset id="single-time-preferences" class="time-preferences time-outbound">
          <strong>时段偏好</strong>
          <label>偏好哪些时段起飞？（可多选）</label>
          <div class="choice">
            <label><input type="checkbox" name="departure_slots" value="dawn" checked> 清晨 06:00-09:00</label>
            <label><input type="checkbox" name="departure_slots" value="morning" checked> 早上 09:00-12:00</label>
            <label><input type="checkbox" name="departure_slots" value="noon" checked> 中午 12:00-14:00</label>
            <label><input type="checkbox" name="departure_slots" value="afternoon" checked> 下午 14:00-17:00</label>
            <label><input type="checkbox" name="departure_slots" value="evening" checked> 傍晚 17:00-20:00</label>
            <label><input type="checkbox" name="departure_slots" value="night" checked> 晚上 20:00-23:00</label>
            <label><input type="checkbox" name="departure_slots" value="redeye"> 凌晨/红眼 23:00-06:00</label>
          </div>

          <label>可接受哪些时段到达？（可多选）</label>
          <div class="choice">
            <label><input type="checkbox" name="arrival_slots" value="dawn" checked> 清晨 06:00-09:00</label>
            <label><input type="checkbox" name="arrival_slots" value="morning" checked> 早上 09:00-12:00</label>
            <label><input type="checkbox" name="arrival_slots" value="noon" checked> 中午 12:00-14:00</label>
            <label><input type="checkbox" name="arrival_slots" value="afternoon" checked> 下午 14:00-17:00</label>
            <label><input type="checkbox" name="arrival_slots" value="evening" checked> 傍晚 17:00-20:00</label>
            <label><input type="checkbox" name="arrival_slots" value="night" checked> 晚上 20:00-23:00</label>
            <label><input type="checkbox" name="arrival_slots" value="redeye"> 凌晨/红眼 23:00-06:00</label>
          </div>
        </fieldset>

        <div id="round-trip-time-preferences" data-show-if="round_trip=true">
          <fieldset class="time-preferences time-outbound">
            <strong>━━ 去程时段偏好 ━━</strong>
            <label>去程偏好哪些时段起飞？</label>
            <div class="choice">
              <label><input type="checkbox" name="outbound_departure_slots" value="dawn" checked> 清晨 06:00-09:00</label>
              <label><input type="checkbox" name="outbound_departure_slots" value="morning" checked> 早上 09:00-12:00</label>
              <label><input type="checkbox" name="outbound_departure_slots" value="noon" checked> 中午 12:00-14:00</label>
              <label><input type="checkbox" name="outbound_departure_slots" value="afternoon" checked> 下午 14:00-17:00</label>
              <label><input type="checkbox" name="outbound_departure_slots" value="evening" checked> 傍晚 17:00-20:00</label>
              <label><input type="checkbox" name="outbound_departure_slots" value="night" checked> 晚上 20:00-23:00</label>
              <label><input type="checkbox" name="outbound_departure_slots" value="redeye"> 凌晨/红眼 23:00-06:00</label>
            </div>

            <label>去程可接受哪些时段到达？</label>
            <div class="choice">
              <label><input type="checkbox" name="outbound_arrival_slots" value="dawn" checked> 清晨 06:00-09:00</label>
              <label><input type="checkbox" name="outbound_arrival_slots" value="morning" checked> 早上 09:00-12:00</label>
              <label><input type="checkbox" name="outbound_arrival_slots" value="noon" checked> 中午 12:00-14:00</label>
              <label><input type="checkbox" name="outbound_arrival_slots" value="afternoon" checked> 下午 14:00-17:00</label>
              <label><input type="checkbox" name="outbound_arrival_slots" value="evening" checked> 傍晚 17:00-20:00</label>
              <label><input type="checkbox" name="outbound_arrival_slots" value="night" checked> 晚上 20:00-23:00</label>
              <label><input type="checkbox" name="outbound_arrival_slots" value="redeye"> 凌晨/红眼 23:00-06:00</label>
            </div>
          </fieldset>

          <fieldset class="time-preferences time-return">
            <strong>━━ 返程时段偏好 ━━</strong>
            <label>返程偏好哪些时段起飞？</label>
            <div class="choice">
              <label><input type="checkbox" name="return_departure_slots" value="dawn" checked> 清晨 06:00-09:00</label>
              <label><input type="checkbox" name="return_departure_slots" value="morning" checked> 早上 09:00-12:00</label>
              <label><input type="checkbox" name="return_departure_slots" value="noon" checked> 中午 12:00-14:00</label>
              <label><input type="checkbox" name="return_departure_slots" value="afternoon" checked> 下午 14:00-17:00</label>
              <label><input type="checkbox" name="return_departure_slots" value="evening" checked> 傍晚 17:00-20:00</label>
              <label><input type="checkbox" name="return_departure_slots" value="night" checked> 晚上 20:00-23:00</label>
              <label><input type="checkbox" name="return_departure_slots" value="redeye"> 凌晨/红眼 23:00-06:00</label>
            </div>

            <label>返程可接受哪些时段到达？</label>
            <div class="choice">
              <label><input type="checkbox" name="return_arrival_slots" value="dawn" checked> 清晨 06:00-09:00</label>
              <label><input type="checkbox" name="return_arrival_slots" value="morning" checked> 早上 09:00-12:00</label>
              <label><input type="checkbox" name="return_arrival_slots" value="noon" checked> 中午 12:00-14:00</label>
              <label><input type="checkbox" name="return_arrival_slots" value="afternoon" checked> 下午 14:00-17:00</label>
              <label><input type="checkbox" name="return_arrival_slots" value="evening" checked> 傍晚 17:00-20:00</label>
              <label><input type="checkbox" name="return_arrival_slots" value="night" checked> 晚上 20:00-23:00</label>
              <label><input type="checkbox" name="return_arrival_slots" value="redeye"> 凌晨/红眼 23:00-06:00</label>
            </div>
          </fieldset>
        </div>
        </div>
        </div>
        </div>

        <div id="pref-detail-refund" class="pref-card-detail">
        <label>如果行程变化，你希望机票怎样？</label>
        <div class="choice">
          <label><input type="radio" name="refund_flexibility" value="not_needed"> 不重要，便宜优先</label>
          <label><input type="radio" name="refund_flexibility" value="preferred" checked> 最好能改签日期</label>
          <label><input type="radio" name="refund_flexibility" value="required"> 必须能退票或改签</label>
          <label><input type="radio" name="refund_flexibility" value="unknown"> 不确定</label>
        </div>

        <label>为了便宜，你最多能接受多大不方便？</label>
        <div class="choice">
          <label><input type="radio" name="price_sensitivity" value="low" checked> 不接受明显不方便，时间和稳定更重要</label>
          <label><input type="radio" name="price_sensitivity" value="medium"> 便宜200元左右，可以接受早晚班</label>
          <label><input type="radio" name="price_sensitivity" value="high"> 便宜500元以上，可以接受中转或更长耗时</label>
          <label><input type="radio" name="price_sensitivity" value="max"> 价格优先，只要显著便宜都可以看</label>
        </div>

        <div id="domestic-invoice-trigger" data-show-if="route_type=domestic">
          <label>国内报销/开票需求</label>
          <div class="choice">
            <label><input type="checkbox" name="invoice_context" value="true"> 有发票、行程单或报销相关要求</label>
          </div>
          <p class="hint">勾选后会展开商务/报销规则；具体开票能力仍以渠道支付页为准。</p>
        </div>

        <div id="business-rules-module" data-show-if="business_context=true">
        <label>出行性质（可多选）</label>
        <div class="choice">
          <label><input type="checkbox" name="trip_natures" value="business"> 商务出差</label>
          <label><input type="checkbox" name="trip_natures" value="meeting"> 商务会议</label>
          <label><input type="checkbox" name="trip_natures" value="team_building"> 公司团建</label>
        </div>

        <div data-show-if="trip_natures=business|meeting|team_building">
        <label>同行总人数</label>
        <input name="team_passenger_count" type="number" min="1" step="1" placeholder="例如 8">

        <label>舱位安排</label>
        <div class="choice">
          <label><input type="radio" name="cabin_arrangement" value="economy_all" checked> 全部经济舱</label>
          <label><input type="radio" name="cabin_arrangement" value="business_all"> 全部商务舱</label>
          <label><input type="radio" name="cabin_arrangement" value="mixed"> 混合舱位</label>
        </div>
        <div id="cabin-arrangement-warning" class="field-error">商务舱人数 + 经济舱人数应等于同行总人数</div>

        <div data-show-if="cabin_arrangement=mixed">
          <label>分舱位人数</label>
          <div class="inline-grid">
            <label>商务舱 <input type="number" name="business_seats" min="0" value="0"></label>
            <label>经济舱 <input type="number" name="economy_seats" min="0" value="0"></label>
          </div>
        </div>

        <label>报销/舱位政策</label>
        <div class="choice">
          <label><input type="radio" name="cabin_policy" value="economy_only" checked> 仅经济舱报销</label>
          <label><input type="radio" name="cabin_policy" value="level_based"> 部分职级可商务舱（总监及以上等）</label>
          <label><input type="radio" name="cabin_policy" value="business_allowed"> 均可商务舱</label>
        </div>
        <p class="hint" data-show-if="cabin_policy=level_based">按职级报销时，系统会结合职级和分舱位人数判断是否需要查询商务舱。</p>
        </div>

        <div data-show-if="trip_natures=business">
          <label>你的职级</label>
          <div class="choice">
            <label><input type="radio" name="user_level" value="staff" checked> 普通员工</label>
            <label><input type="radio" name="user_level" value="manager"> 经理</label>
            <label><input type="radio" name="user_level" value="director"> 总监</label>
            <label><input type="radio" name="user_level" value="vp"> 副总裁及以上</label>
          </div>
        </div>

        <div data-show-if="trip_natures=meeting">
          <label>会议时间</label>
          <div class="inline-grid">
            <label>会议开始时间 <input name="meeting_start" type="time" value="10:00"></label>
            <label>会议结束时间 <input name="meeting_end" type="time" value="16:00"></label>
          </div>
          <p class="hint">系统会复用当天往返逻辑，按会议时间 + 车程 + 缓冲反推航班时间窗口。</p>
        </div>

        <div data-show-if="trip_natures=team_building">
          <label>团建日期弹性</label>
          <div class="choice">
            <label><input type="radio" name="team_date_flexibility" value="fixed" checked> 固定</label>
            <label><input type="radio" name="team_date_flexibility" value="flexible"> 可前后调整（找更便宜日期）</label>
          </div>
          <label>是否需要同行程（全员同一航班）</label>
          <div class="choice">
            <label><input type="radio" name="same_flight_required" value="true" checked> 是</label>
            <label><input type="radio" name="same_flight_required" value="false"> 否</label>
          </div>
        </div>

        <label>每人报销上限（可选）</label>
        <input name="reimburse_per_person" type="number" min="0" step="1" placeholder="例如 5000">

        <div data-show-if="route_type=domestic|greater_china">
          <label>（国内）报销需求</label>
          <div class="choice">
            <label><input type="checkbox" name="invoice_needed" value="true"> 需要行程单/发票</label>
            <label><input type="checkbox" name="invoice_special_vat" value="true"> 优先可开专票的渠道</label>
            <label><input type="checkbox" name="invoice_cabin_limit" value="true"> 报销有舱位限制</label>
          </div>
          <p class="hint">国内航线会优先提示航司官网、携程、飞猪、去哪儿等渠道的报销友好度，具体开票能力以平台支付页为准。</p>
        </div>
        </div>
        </div>
        <p class="hint">这些偏好会影响推荐排序，不会影响是否创建监控</p>
      </fieldset>
    </div>

      <button id="rules-toggle" class="secondary-button precise-only" type="button">＋ 更细的筛选规则</button>
      <div id="advanced-rules" class="smart-panel precise-only">
        <fieldset>
          <legend>更细的筛选规则</legend>
          <p class="hint">适合有特定要求的用户，一般用户可跳过</p>

          <div id="transfer-rules-module">
          <div class="module-heading">
            <strong>中转规则</strong>
            <button class="reset-module" type="button" data-reset-module="transfer">恢复默认</button>
          </div>
          <p class="hint">这些设置不是必填项。保持默认时，系统会按普通出行规则处理。</p>
          <div id="short-transfer-options" class="sub-options" data-show-if="transfer_policy=reasonable|price_first">
            <label>最长可接受总行程时间</label>
            <div class="choice">
              <label><input type="radio" name="short_transfer_limit" value="extra_3"> 不超过直飞时间+3小时</label>
              <label><input type="radio" name="short_transfer_limit" value="extra_6" checked> 不超过直飞时间+6小时</label>
              <label><input type="radio" name="short_transfer_limit" value="total_18"> 不超过18小时</label>
              <label><input type="radio" name="short_transfer_limit" value="total_24"> 不超过24小时</label>
            </div>
          </div>

          <p class="hint" data-show-if="transfer_policy=price_first">价格优先时会显示更细的中转规则；国内航线会自动弱化非联程和过境签相关项。</p>

          <div id="overnight-transfer-options" class="sub-options" data-show-if="transfer_policy=price_first">
            <label>是否接受过夜中转</label>
            <div class="choice">
              <label><input type="radio" name="accept_overnight_transfer" value="false" checked> 不接受</label>
              <label><input type="radio" name="accept_overnight_transfer" value="true"> 可以接受</label>
            </div>
          </div>

          <div id="pref-detail-self_transfer" class="pref-card-detail">
          <div id="self-transfer-options" class="sub-options" data-show-if="transfer_policy=price_first">
            <label>是否接受非联程</label>
            <div class="choice">
              <label><input type="radio" name="accept_self_transfer" value="false" checked> 不接受</label>
              <label><input type="radio" name="accept_self_transfer" value="true"> 可以接受</label>
            </div>
          </div>
          </div>
          </div>

          <div id="precise-time-module">
          <div class="module-heading">
            <label>小时级精确设置</label>
          </div>
          <button id="precise-time-toggle" class="secondary-button" type="button" data-show-if="time_preference=custom" style="font-size:13px;padding:8px;margin-top:10px;">需要更精确？按小时设置</button>
          <div id="precise-time-options" class="sub-options">
            <label>最早起飞</label>
            <input type="time" name="departure_time_start">
            <label>最晚起飞</label>
            <input type="time" name="departure_time_end">
            <label>最早到达</label>
            <input type="time" name="arrival_time_start">
            <label>最晚到达</label>
            <input type="time" name="arrival_time_end">
            <p class="hint">填写后会覆盖上面的自然语言时段。</p>
          </div>
          </div>

          <div id="pref-detail-airline" class="pref-card-detail">
          <div class="module-heading">
            <label>航司偏好</label>
            <button class="reset-module" type="button" data-reset-module="airline">恢复默认</button>
          </div>
          <p class="hint">这些设置不是必填项。保持默认时，系统会按普通出行规则处理。</p>
          <div class="choice">
            <label><input type="radio" name="airline_policy" value="any" checked> 不限制</label>
            <label><input type="radio" name="airline_policy" value="prefer_full_service"> 偏好全服务航司</label>
            <label><input type="radio" name="airline_policy" value="no_lcc"> 不接受廉航</label>
            <label><input type="radio" name="airline_policy" value="exclude_airlines"> 有不接受的航司吗？</label>
          </div>

          <div data-show-if="airline_policy=exclude_airlines">
            <label>不接受的航司</label>
            <input name="exclude_airlines" placeholder="选填，多个航司用逗号分隔，例如 Spirit, Frontier">
            <div class="choice">
              <label><input type="checkbox" name="blocked_airlines_common" value="Spirit"> Spirit</label>
              <label><input type="checkbox" name="blocked_airlines_common" value="Frontier"> Frontier</label>
              <label><input type="checkbox" name="blocked_airlines_common" value="春秋航空"> 春秋航空</label>
              <label><input type="checkbox" name="blocked_airlines_common" value="乐桃航空"> 乐桃航空</label>
            </div>
          </div>
          </div>

          <div class="module-heading">
            <label>提醒规则</label>
            <button class="reset-module" type="button" data-reset-module="alerts">恢复默认</button>
          </div>
          <p class="hint">这些设置不是必填项。保持默认时，系统会按普通出行规则处理。</p>
          <label>附加关注</label>
          <div id="alerts-secondary-options" class="choice">
            <label><input type="checkbox" name="secondary_goals" value="low_price_alert"> 异常低价提醒</label>
            <label><input type="checkbox" name="secondary_goals" value="price_risk_alert"> 涨价风险提醒</label>
            <label><input type="checkbox" name="secondary_goals" value="cheaper_date"> 前后日期更便宜提醒</label>
            <label><input type="checkbox" name="secondary_goals" value="better_same_day"> 同日更优方案提醒</label>
          </div>
          <p id="date-flex-warning" class="inline-warning">你选了不可调整，但仍可接收前后日期差价参考</p>

          <p class="hint">规则越严格，可能匹配的方案越少。如果没有结果，系统会提示你放宽哪些条件</p>
        </fieldset>
      </div>
    </fieldset>

    <fieldset class="form-step" data-step="4">
      <legend>完成</legend>
      <p class="hint">请确认下面的需求摘要，确认后系统会保存订阅并开始监控。</p>
    </fieldset>

    <div id="step-nav">
      <div class="step-nav-row">
        <button id="step-prev" class="secondary-button" type="button">上一步</button>
        <button id="step-next" type="button">下一步</button>
      </div>
    </div>

    <div class="submit-preview">
      提交后将生成：当前是否值得买的判断、推荐方案与备选方案、价格置信度拆解、购买前检查清单，以及为什么排除更便宜方案的解释。
    </div>
    <p id="missing-required-warning"></p>
    <div id="strict-rules-warning" class="strict-warning"></div>
    <button id="preview-button" type="button">开始监控</button>

    <div id="summary-card">
      <h2>即将创建的监控：</h2>
      <ul id="summary-list"></ul>
      <p class="hint">开始后，系统会立即生成当前购买判断，并在价格进入低价区间、涨价风险升高或出现异常低价时提醒你。</p>
      <div class="submit-preview">
        提交后将生成：当前是否值得买的判断、推荐方案与备选方案、价格置信度拆解、购买前检查清单，以及为什么排除更便宜方案的解释。
      </div>
      <div class="choice">
        <label><input type="checkbox" id="remember-preferences" name="remember_preferences" value="true"> 记住这组偏好（下次自动填充）</label>
      </div>
      <div class="button-row">
        <button type="submit">确认并开始监控</button>
        <button id="edit-button" class="secondary-button" type="button">返回修改</button>
      </div>
    </div>
  </form>

  <div id="mobile-action-bar">
    <span id="mobile-action-text">还缺:目的地、出发日期</span>
    <button id="mobile-next-button" type="button">下一步</button>
    <button id="mobile-summary-button" class="secondary-button" type="button">查看摘要</button>
    <button id="mobile-submit-button" type="button">确认并开始监控</button>
  </div>

  <script>
    const labels = {
      dateFlex: {"0": "不能调，就这天", "1": "前后1天可以", "3": "前后3天都行", "7": "前后一周都行"},
      maxBudgetMode: {"fixed": "最高可接受", "none": "不确定，帮我判断"},
      targetPriceMode: {"fixed": "理想入手价", "auto": "不确定，帮我判断合理价格", "low_zone": "没有明确预算，进入低价区间时提醒"},
      transfer: {"direct_only": "必须直飞", "reasonable": "可以接受合理中转", "price_first": "价格优先，中转也可以", "short_ok": "可以短中转", "cheap_ok": "便宜很多可以中转"},
      departureSlots: {
        dawn: "清晨 06:00-09:00",
        morning: "早上 09:00-12:00",
        noon: "中午 12:00-14:00",
        afternoon: "下午 14:00-17:00",
        evening: "傍晚 17:00-20:00",
        night: "晚上 20:00-23:00",
        redeye: "凌晨/红眼 23:00-06:00"
      },
      arrivalSlots: {
        dawn: "清晨 06:00-09:00",
        morning: "早上 09:00-12:00",
        noon: "中午 12:00-14:00",
        afternoon: "下午 14:00-17:00",
        evening: "傍晚 17:00-20:00",
        night: "晚上 20:00-23:00",
        redeye: "凌晨/红眼 23:00-06:00"
      },
      baggage: {"required": "必须托运", "not_needed": "不需要托运", "unknown": "不确定"},
      primary: {"price_drop_alert": "找到合适价格时提醒我", "buy_timing": "判断现在该不该买", "cheaper_date": "帮我找更便宜的日期", "best_overall": "帮我找最合适航班"},
      frequency: {"important_only": "仅重要变化时提醒", "daily_digest": "每天汇总推送一次", "price_change": "每次价格变化都提醒"}
    };
    const goalDefaults = {
      price_alert: ["low_price_alert", "price_risk_alert"],
      price_drop_alert: ["low_price_alert", "price_risk_alert"],
      buy_timing: ["price_risk_alert", "better_same_day"],
      cheaper_date: ["cheaper_date"],
      best_overall: ["better_same_day"]
    };
    const cityAirports = {{ city_airports|tojson }};
    const cityAliases = {{ city_aliases|tojson }};
    const airportShortNames = {{ airport_short_names|tojson }};
    const editSubscription = {{ edit_subscription|tojson }};

    const form = document.getElementById('subscription-form');
    const modeRadios = document.querySelectorAll('input[name="monitor_mode"]');
    const budgetStrategyRadios = document.querySelectorAll('input[name="price_strategy"]');
    const budgetAmountFields = document.getElementById('budget-amount-fields');
    const requiredProgress = document.getElementById('required-progress');
    const savedTemplateBanner = document.getElementById('saved-template-banner');
    const savedTemplateSummary = document.getElementById('saved-template-summary');
    const applyTemplateButton = document.getElementById('apply-template-button');
    const ignoreTemplateButton = document.getElementById('ignore-template-button');
    const clearTemplateButton = document.getElementById('clear-template-button');
    const missingRequiredWarning = document.getElementById('missing-required-warning');
    const strictRulesWarning = document.getElementById('strict-rules-warning');
    const quickDefaultsNote = document.getElementById('quick-defaults-note');
    const openPreciseModeButton = document.getElementById('open-precise-mode');
    const originSelect = document.getElementById('origin');
    const originManual = document.querySelector('input[name="origin_manual"]');
    const originValidationError = document.getElementById('origin-validation-error');
    const destinationInput = document.getElementById('destination');
    const destinationValidationError = document.getElementById('destination-validation-error');
    const originAirportHint = document.getElementById('origin-airport-hint');
    const destinationAirportHint = document.getElementById('destination-airport-hint');
    const originAirportTags = document.getElementById('origin-airport-tags');
    const destinationAirportTags = document.getElementById('destination-airport-tags');
    const originAirportsActiveInput = document.getElementById('origin_airports_active');
    const destinationAirportsActiveInput = document.getElementById('destination_airports_active');
    const originAirportHints = {
      "上海": "将搜索以下机场出发的航班：浦东PVG、虹桥SHA",
      "北京": "将搜索以下机场出发的航班：首都PEK、大兴PKX",
      "广州": "将搜索以下机场出发的航班：白云CAN",
      "深圳": "将搜索以下机场出发的航班：宝安SZX",
      "成都": "将搜索以下机场出发的航班：天府CTU",
      "杭州": "将搜索以下机场出发的航班：萧山HGH",
      "南京": "将搜索以下机场出发的航班：禄口NKG",
      "OTHER": "请选择“其他”后在下方手动输入城市名或机场代码"
    };
    const tripRadios = document.querySelectorAll('input[name="round_trip"]');
    const returnWrap = document.getElementById('return-date-wrap');
    const returnDate = document.getElementById('return_date');
    const departDateError = document.getElementById('depart-date-error');
    const returnDateError = document.getElementById('return-date-error');
    const sameDayRoundTrip = document.getElementById('same_day_round_trip');
    const meetingTimeHandoffCard = document.getElementById('meeting-time-handoff-card');
    const meetingTimeHandoffText = document.getElementById('meeting-time-handoff-text');
    const timePreferenceControls = document.getElementById('time-preference-controls');
    const customTimeOptions = document.getElementById('custom-time-options');
    const preciseTimeToggle = document.getElementById('precise-time-toggle');
    const preciseTimeOptions = document.getElementById('precise-time-options');
    const singleTimePreferences = document.getElementById('single-time-preferences');
    const roundTripTimePreferences = document.getElementById('round-trip-time-preferences');
    const timePreferenceRadios = document.querySelectorAll('input[name="time_preference"]');
    const maxBudgetRadios = document.querySelectorAll('input[name="max_budget_mode"]');
    const targetPriceRadios = document.querySelectorAll('input[name="target_price_mode"]');
    const maxBudgetInput = document.getElementById('max_budget');
    const targetPriceInput = document.getElementById('target_price');
    const priceValidationError = document.getElementById('price-validation-error');
    const maxBudgetError = document.getElementById('max-budget-error');
    const priceHint = document.getElementById('price-hint');
    const advanced = document.getElementById('advanced-preferences');
    const advancedToggle = document.getElementById('advanced-toggle');
    const advancedRules = document.getElementById('advanced-rules');
    const rulesToggle = document.getElementById('rules-toggle');
    const previewButton = document.getElementById('preview-button');
    const summaryCard = document.getElementById('summary-card');
    const summaryList = document.getElementById('summary-list');
    const editButton = document.getElementById('edit-button');
    const notificationMethodRadios = document.querySelectorAll('input[name="notification_method"]');
    const notificationFrequencyRadios = document.querySelectorAll('input[name="notification_frequency"]');
    const notificationFrequencyRuleRadios = document.querySelectorAll('input[name="notification_frequency_rule"]');
    const notificationFrequencyRuleShadow = document.getElementById('notification_frequency_rule_shadow');
    const notificationEmailInput = document.getElementById('notification_email');
    const emailReminderWrap = document.getElementById('email-reminder-wrap');
    const emailError = document.getElementById('email-error');
    const pageOnlyHint = document.getElementById('page-only-hint');
    const rememberPreferences = document.getElementById('remember-preferences');
    const transferRadios = document.querySelectorAll('input[name="transfer_policy"]');
    const shortTransferOptions = document.getElementById('short-transfer-options');
    const shortTransferInputs = shortTransferOptions
      ? shortTransferOptions.querySelectorAll('input')
      : [];
    const overnightTransferOptions = document.getElementById('overnight-transfer-options');
    const selfTransferOptions = document.getElementById('self-transfer-options');
    const primaryGoalRadios = document.querySelectorAll('input[name="primary_goal"]');
    const secondaryGoalChecks = document.querySelectorAll('input[name="secondary_goals"]');
    const dateFlexRadios = document.querySelectorAll('input[name="date_flexibility"]');
    const routeTypeRadios = document.querySelectorAll('input[name="route_type"]');
    const dateFlexWarning = document.getElementById('date-flex-warning');
    const cheaperDateCheck = document.querySelector('input[name="secondary_goals"][value="cheaper_date"]');
    const cheaperDateLabel = cheaperDateCheck ? cheaperDateCheck.closest('label') : null;
    const travelScenarioRadios = document.querySelectorAll('input[name="travel_scenario"]');
    const travelScenarioNotice = document.getElementById('travel-scenario-notice');
    const companionRadios = document.querySelectorAll('input[name="companions"]');
    const autoPreferenceNotice = document.getElementById('auto-preference-notice');
    const departurePolicyInput = document.querySelector('input[name="departure_time_policy"]');
    const stepPanels = Array.from(document.querySelectorAll('.form-step'));
    const mobileStepper = document.getElementById('mobile-stepper');
    const stepDots = document.getElementById('step-dots');
    const stepLabel = document.getElementById('step-label');
    const stepPrev = document.getElementById('step-prev');
    const stepNext = document.getElementById('step-next');
    const mobileActionBar = document.getElementById('mobile-action-bar');
    const mobileActionText = document.getElementById('mobile-action-text');
    const mobileNextButton = document.getElementById('mobile-next-button');
    const mobileSummaryButton = document.getElementById('mobile-summary-button');
    const mobileSubmitButton = document.getElementById('mobile-submit-button');
    const stepTimePreferences = document.getElementById('step-time-preferences');
    const prefCardButtons = document.querySelectorAll('.pref-card-button');
    const prefCardDetails = document.querySelectorAll('.pref-card-detail');
    const resetModuleButtons = document.querySelectorAll('.reset-module');
    const stepTitles = ['行程信息', '价格底线', '监控目标', '完成'];
    const greaterChinaAirports = new Set(['HKG', 'MFM', 'TPE', 'TSA']);
    const domesticAirports = new Set([
      'PVG','SHA','PEK','PKX','CAN','SZX','CTU','TFU','HGH','NKG','XIY','CKG',
      'WUH','CSX','TAO','XMN','FOC','KMG','SHE','DLC','TSN','CGO','URC','HRB'
    ]);
    let currentStep = 1;
    let routeTypeTouched = false;

    if (stepTimePreferences && customTimeOptions) {
      stepTimePreferences.appendChild(customTimeOptions);
    }

    function checkedValue(name) {
      const selected = document.querySelector(`input[name="${name}"]:checked`);
      if (selected) return selected.value;
      const field = document.querySelector(`[name="${name}"]`);
      if (field && field.type === 'checkbox') {
        return field.checked ? field.value : '';
      }
      return field ? field.value : "";
    }

    function currentValues(name) {
      if (name === 'business_context') {
        const precise = checkedValue('monitor_mode') === 'precise';
        const scenarios = selectedTravelScenarios();
        const natures = checkedValues('trip_natures');
        const domesticInvoiceContext = checkedValue('route_type') === 'domestic' && (
          Boolean(document.querySelector('input[name="invoice_context"]')?.checked)
          || ['invoice_needed', 'invoice_special_vat', 'invoice_cabin_limit'].some(fieldName =>
            Boolean(document.querySelector(`input[name="${fieldName}"]`)?.checked)
          )
        );
        const active = precise && (
          scenarios.includes('business')
          || natures.some(value => ['business', 'meeting', 'team_building'].includes(value))
          || Boolean(sameDayRoundTrip?.checked)
          || domesticInvoiceContext
        );
        return active ? ['true'] : [];
      }
      const checked = Array.from(document.querySelectorAll(`input[name="${name}"]:checked`))
        .map(input => input.value);
      if (checked.length) return checked;
      const value = checkedValue(name);
      return value ? [value] : [];
    }

    function conditionalVisible(el) {
      let node = el;
      while (node && node !== document.body) {
        if (node.dataset && node.dataset.showIf && node.style.display === 'none') {
          return false;
        }
        node = node.parentElement;
      }
      return true;
    }

    function syncConditionalDisabled() {
      document.querySelectorAll('[data-show-if]').forEach(el => {
        const disabled = !conditionalVisible(el);
        el.querySelectorAll('input, select, textarea').forEach(control => {
          control.disabled = disabled;
        });
      });
      if (checkedValue('monitor_mode') !== 'precise') {
        disablePreciseOnlyFields(true);
      }
      toggleShortTransferOptions();
      syncPreciseTimeDisabled();
    }

    function updateConditionalFields() {
      document.querySelectorAll('[data-show-if]').forEach(el => {
        const rule = el.dataset.showIf || '';
        const shouldShow = rule
          .split(';')
          .map(item => item.trim())
          .filter(Boolean)
          .every(item => {
            const [field, rawValue] = item.split('=');
            const values = String(rawValue || '').split('|');
            const current = currentValues(field);
            return Boolean(field && current.some(value => values.includes(value)));
        });
        el.style.display = shouldShow ? 'block' : 'none';
      });
      syncConditionalDisabled();
    }

    document.querySelectorAll('input, select').forEach(input => {
      input.addEventListener('change', updateConditionalFields);
    });

    const moduleInputContainers = {
      time: ['pref-detail-time'],
      transfer: ['transfer-rules-module'],
      airline: ['pref-detail-airline'],
      alerts: ['alerts-secondary-options']
    };

    function moduleInputs(moduleName) {
      return (moduleInputContainers[moduleName] || [])
        .flatMap(id => Array.from(document.querySelectorAll(`#${id} input, #${id} select`)));
    }

    function captureModuleDefaults() {
      Object.keys(moduleInputContainers).forEach(moduleName => {
        moduleInputs(moduleName).forEach(input => {
          input.dataset.default = (input.type === 'radio' || input.type === 'checkbox')
            ? String(input.checked)
            : input.value;
        });
      });
    }

    function resetInputToDefault(input) {
      if (!input || input.dataset.default === undefined) {
        return;
      }
      if (input.type === 'radio' || input.type === 'checkbox') {
        input.checked = input.dataset.default === 'true';
      } else {
        input.value = input.dataset.default;
      }
    }

    function resetModule(moduleName) {
      moduleInputs(moduleName).forEach(resetInputToDefault);
      if (moduleName === 'alerts') {
        const mainFrequency = document.querySelector('input[name="notification_frequency"][value="important_only"]');
        if (mainFrequency) mainFrequency.checked = true;
      }
      if (moduleName === 'time' && preciseTimeOptions) {
        preciseTimeOptions.style.display = 'none';
      }
      updateConditionalFields();
      toggleTimePreference();
      toggleShortTransferOptions();
      syncNotificationFrequencyFromRule();
      updatePrefCards();
      updateStrictRulesWarning();
      refreshSummaryIfFinalStep();
    }

    function selectedCount(name) {
      return document.querySelectorAll(`input[name="${name}"]:checked`).length;
    }

    function hasNarrowCustomTimeWindow() {
      if (checkedValue('time_preference') !== 'custom') {
        return false;
      }
      const isRoundTrip = checkedValue('round_trip') === 'true';
      const fields = isRoundTrip
        ? ['outbound_departure_slots', 'outbound_arrival_slots', 'return_departure_slots', 'return_arrival_slots']
        : ['departure_slots', 'arrival_slots'];
      return fields.some(name => {
        const count = selectedCount(name);
        return count > 0 && count <= 2;
      });
    }

    function customTimeExcludesRedeye() {
      if (checkedValue('time_preference') !== 'custom') {
        return false;
      }
      const isRoundTrip = checkedValue('round_trip') === 'true';
      const fields = isRoundTrip
        ? ['outbound_departure_slots', 'outbound_arrival_slots', 'return_departure_slots', 'return_arrival_slots']
        : ['departure_slots', 'arrival_slots'];
      return fields.every(name => !Array.from(document.querySelectorAll(`input[name="${name}"]:checked`))
        .some(input => input.value === 'redeye'));
    }

    function strictRuleStatus() {
      let score = 0;
      if (checkedValue('transfer_policy') === 'direct_only') score += 1;
      if (checkedValue('baggage') === 'required') score += 1;
      if (checkedValue('time_preference') === 'no_redeye' || customTimeExcludesRedeye()) score += 1;
      if (checkedValue('airline_policy') === 'no_lcc') score += 1;
      const maxBudget = Number(maxBudgetInput.value || 0);
      const targetPrice = Number(targetPriceInput.value || 0);
      if (maxBudget > 0 && targetPrice > 0 && targetPrice < maxBudget * 0.5) score += 1;
      if (hasNarrowCustomTimeWindow()) score += 1;
      if (checkedValue('date_flexibility') === '0') score += 1;
      return score;
    }

    function updateStrictRulesWarning() {
      if (!strictRulesWarning) return;
      const score = strictRuleStatus();
      if (score >= 6) {
        strictRulesWarning.textContent = '⚠️ 当前规则非常严格，可能很难匹配到方案。建议至少放宽其中1-2项以获得更多推荐。';
        strictRulesWarning.style.display = 'block';
      } else if (score >= 4) {
        strictRulesWarning.textContent = '⚠️ 当前规则较严格，可能减少可推荐方案。如果长期没有结果，可考虑放宽中转、时间或价格限制。';
        strictRulesWarning.style.display = 'block';
      } else {
        strictRulesWarning.textContent = '';
        strictRulesWarning.style.display = 'none';
      }
      if (score >= 6) {
        strictRulesWarning.textContent = '当前条件非常严格，可能较难找到特别低价。系统会优先保证出行稳定性，长期无结果可放宽中转或时间。';
      } else if (score >= 4) {
        strictRulesWarning.textContent = '当前条件较严格，可能较难找到特别低价，系统会优先保证出行稳定性。长期无结果可放宽中转或时间。';
      }
    }

    function isMobileStepper() {
      return window.matchMedia('(max-width: 720px)').matches;
    }

    function updateStepper() {
      const mobile = isMobileStepper();
      stepPanels.forEach(panel => {
        const isActive = Number(panel.dataset.step) === currentStep;
        panel.classList.toggle('active', !mobile || isActive);
      });
      stepDots.textContent = stepTitles
        .map((_, index) => index + 1 === currentStep ? '●' : '○')
        .join(' ');
      stepLabel.textContent = `第${currentStep}步/共4步：${stepTitles[currentStep - 1]}`;
      stepPrev.disabled = currentStep === 1;
      stepNext.style.display = currentStep === stepTitles.length ? 'none' : 'block';
      previewButton.classList.remove('step-final-visible');
      if (mobile && currentStep === stepTitles.length) {
        buildSummary();
        summaryCard.style.display = 'block';
      } else if (mobile) {
        summaryCard.style.display = 'none';
      }
      updateMobileActionBar();
    }

    function updateMobileActionBar() {
      if (!mobileActionBar || !isMobileStepper()) return;
      const missing = missingRequiredLabels(currentStep);
      const finalStep = currentStep === stepTitles.length;
      if (mobileActionText) {
        mobileActionText.textContent = finalStep
          ? '确认摘要后即可开始监控'
          : (missing.length ? `还缺:${humanJoin(missing)}` : '基础项已完成');
      }
      if (mobileNextButton) {
        mobileNextButton.style.display = finalStep ? 'none' : 'inline-block';
      }
      if (mobileSummaryButton) {
        mobileSummaryButton.style.display = finalStep ? 'inline-block' : 'none';
      }
      if (mobileSubmitButton) {
        mobileSubmitButton.style.display = finalStep ? 'inline-block' : 'none';
      }
    }

    function goToStep(step) {
      currentStep = Math.max(1, Math.min(stepTitles.length, step));
      updateStepper();
      if (isMobileStepper()) {
        mobileStepper.scrollIntoView({behavior: 'smooth', block: 'start'});
      }
    }

    function validateCurrentStep() {
      updateRequiredProgress();
      const missing = missingRequiredLabels(currentStep);
      if (missing.length) {
        alert(`请先填写${humanJoin(missing)}`);
        return false;
      }
      toggleBudgetRequired();
      toggleReturnDate();
      if (!validatePriceInputs()) {
        alert('理想入手价应低于最高可接受价，请确认是否填反了');
        targetPriceInput.focus();
        return false;
      }
      if (!validateDateFields()) {
        return false;
      }
      if (!validateLocationField('origin') || !validateLocationField('destination')) {
        return false;
      }
      const currentFieldset = document.querySelector(`.form-step[data-step="${currentStep}"]`);
      if (!currentFieldset) {
        return true;
      }
      const invalidControl = Array.from(
        currentFieldset.querySelectorAll('input, select, textarea')
      ).find(control => !control.disabled && !control.checkValidity());
      if (invalidControl) {
        invalidControl.reportValidity();
        return false;
      }
      return true;
    }

    function humanJoin(items) {
      if (!items.length) return '';
      if (items.length === 1) return items[0];
      return `${items.slice(0, -1).join('、')}和${items[items.length - 1]}`;
    }

    function missingRequiredLabels(maxStep = null) {
      const origin = selectedOrigin();
      const destination = destinationInput.value.trim();
      const isRoundTrip = checkedValue('round_trip') === 'true';
      const items = [
        {done: Boolean(checkedValue('route_type')), label: '航线类型', step: 1},
        {done: Boolean(origin && origin !== '\u5176\u4ed6'), label: '\u51fa\u53d1\u5730', step: 1},
        {done: Boolean(destination), label: '\u76ee\u7684\u5730', step: 1},
        {done: Boolean(checkedValue('round_trip')), label: '\u5355\u7a0b/\u5f80\u8fd4', step: 1},
        {done: Boolean(form.depart_date.value), label: '\u51fa\u53d1\u65e5\u671f', step: 1},
        {done: !isRoundTrip || Boolean(returnDate.value), label: '\u8fd4\u7a0b\u65e5\u671f', step: 1},
        {done: Boolean(checkedValue('date_flexibility')), label: '\u65e5\u671f\u5f39\u6027', step: 2},
        {done: Boolean(checkedValue('price_strategy')), label: '\u4ef7\u683c\u7b56\u7565', step: 2},
        {done: Boolean(checkedValue('transfer_policy')), label: '\u4e2d\u8f6c\u63a5\u53d7\u7a0b\u5ea6', step: 2},
        {done: Boolean(checkedValue('baggage')), label: '\u6258\u8fd0\u884c\u674e', step: 2},
        {done: Boolean(checkedValue('primary_goal')), label: '\u4e3b\u76ee\u6807', step: 3},
        {done: Boolean(checkedValue('notification_method')), label: '\u63d0\u9192\u65b9\u5f0f', step: 3}
      ];
      return items
        .filter(item => !item.done && (maxStep === null || item.step <= maxStep))
        .map(item => item.label);
    }

    function updateRequiredProgress() {
      const missing = missingRequiredLabels();
      if (requiredProgress) {
        if (missing.length) {
          requiredProgress.innerHTML = '<div class="required-progress-title">还需填写：</div><ul id="required-missing-list"></ul>';
          const list = requiredProgress.querySelector('#required-missing-list');
          missing.forEach(label => {
            const item = document.createElement('li');
            item.textContent = label;
            list.appendChild(item);
          });
        } else {
          requiredProgress.innerHTML = '<div class="required-progress-title">基础项已完成 ✓</div>';
        }
        requiredProgress.classList.toggle('incomplete', missing.length > 0);
      }
      if (previewButton) {
        previewButton.textContent = '开始监控';
        previewButton.disabled = false;
      }
      if (missingRequiredWarning) {
        missingRequiredWarning.style.display = missing.length ? 'block' : 'none';
        missingRequiredWarning.textContent = missing.length
          ? `请先填写${humanJoin(missing)}`
          : '';
      }
      updateMobileActionBar();
      return missing;
    }

    function refreshSummaryIfFinalStep() {
      if (summaryCard && summaryCard.style.display === 'block') {
        buildSummary();
        return;
      }
      if (isMobileStepper() && currentStep === stepTitles.length) {
        buildSummary();
      }
    }

    function checkedValues(name) {
      return Array.from(document.querySelectorAll(`input[name="${name}"]:checked`))
        .map(input => input.value);
    }

    function selectedTravelScenarios() {
      const values = checkedValues('travel_scenario');
      return values.length ? values : ['personal'];
    }

    function deriveTripTypeFromScenarios(scenarios) {
      const values = scenarios || [];
      if (values.includes('business')) return 'business';
      if (values.includes('tourism')) return 'tourism';
      if (values.includes('family_visit') || values.includes('visit_family')) return 'visit_family';
      if (values.includes('family') || values.includes('elderly')) return 'family';
      if (values.includes('important')) return 'important';
      if (values.includes('price_first')) return 'price_first';
      return 'other';
    }

    function selectedLabels(name) {
      return Array.from(document.querySelectorAll(`input[name="${name}"]:checked`))
        .map(input => input.parentElement.textContent.trim())
        .filter(Boolean);
    }

    function selectedScenarioLabels() {
      return selectedLabels('travel_scenario');
    }

    function selectedOrigin() {
      const manual = form.origin_manual.value.trim();
      const selected = form.origin_select.value;
      if (manual) {
        return manual;
      }
      return selected === 'OTHER' ? '其他' : selected;
    }

    const airportState = {
      origin: {all: [], active: []},
      destination: {all: [], active: []}
    };

    function airportShortLabel(code) {
      const upper = String(code || '').trim().toUpperCase();
      return `${upper} ${airportShortNames[upper] || upper}`;
    }

    function resolveAirportsForInput(value) {
      const text = String(value || '').trim();
      if (!text) {
        return [];
      }
      const upper = text.toUpperCase();
      if (cityAirports[text]) {
        return cityAirports[text];
      }
      if (/^[A-Z]{2,4}$/.test(upper)) {
        return [upper];
      }
      return [];
    }

    function renderAirportTags(kind) {
      const isOrigin = kind === 'origin';
      const tagsEl = isOrigin ? originAirportTags : destinationAirportTags;
      const hiddenEl = isOrigin ? originAirportsActiveInput : destinationAirportsActiveInput;
      const hintEl = isOrigin ? originAirportHint : destinationAirportHint;
      const state = airportState[kind];
      if (!tagsEl || !hiddenEl) {
        return;
      }
      tagsEl.innerHTML = '';
      hiddenEl.value = state.active.join(',');
      if (hintEl) {
        hintEl.textContent = state.active.length
          ? '系统将搜索这些机场：是否只搜索某个机场？点击标签上的 × 可取消。'
          : '';
      }
      state.active.forEach(code => {
        const tag = document.createElement('span');
        tag.className = 'airport-tag';
        tag.textContent = airportShortLabel(code);
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.textContent = '×';
        remove.disabled = state.active.length <= 1;
        remove.addEventListener('click', () => {
          if (state.active.length <= 1) {
            return;
          }
          state.active = state.active.filter(item => item !== code);
          renderAirportTags(kind);
          autoDetectRouteType();
          refreshPriceHint();
          refreshSummaryIfFinalStep();
        });
        tag.appendChild(remove);
        tagsEl.appendChild(tag);
      });
    }

    function normalizeAirportList(value) {
      if (Array.isArray(value)) {
        return value.map(item => String(item || '').trim().toUpperCase()).filter(Boolean);
      }
      if (typeof value === 'string') {
        return value.split(',').map(item => item.trim().toUpperCase()).filter(Boolean);
      }
      return [];
    }

    function updateAirportTags(kind, value, activeAirports) {
      const airports = resolveAirportsForInput(value);
      const savedActive = normalizeAirportList(activeAirports)
        .filter(code => airports.includes(code));
      airportState[kind].all = airports;
      airportState[kind].active = savedActive.length ? savedActive : airports.slice();
      renderAirportTags(kind);
      autoDetectRouteType();
      refreshPriceHint();
      refreshSummaryIfFinalStep();
    }

    function setFieldError(input, errorEl, message) {
      if (!input || !errorEl) return;
      if (message) {
        input.classList.add('field-invalid');
        errorEl.innerHTML = message;
        errorEl.style.display = 'block';
      } else {
        input.classList.remove('field-invalid');
        errorEl.textContent = '';
        errorEl.style.display = 'none';
      }
    }

    function localDateString(date = new Date()) {
      const year = date.getFullYear();
      const month = String(date.getMonth() + 1).padStart(2, '0');
      const day = String(date.getDate()).padStart(2, '0');
      return `${year}-${month}-${day}`;
    }

    function aliasSuggestion(text) {
      const value = String(text || '').trim();
      if (!value) return '';
      return cityAliases[value] || cityAliases[value.toUpperCase()] || '';
    }

    function validateLocationField(kind) {
      const input = kind === 'origin' ? originManual : destinationInput;
      const errorEl = kind === 'origin' ? originValidationError : destinationValidationError;
      const value = kind === 'origin' ? selectedOrigin() : destinationInput.value.trim();
      if (!value || value === '其他') {
        setFieldError(input, errorEl, '');
        return true;
      }
      const suggestion = aliasSuggestion(value);
      if (suggestion && suggestion !== value) {
        const buttonId = `${kind}-alias-suggestion`;
        setFieldError(
          input,
          errorEl,
          `未识别'${value}',是否指'${suggestion}'? <button id="${buttonId}" class="inline-suggestion" type="button">使用${suggestion}</button> 或输入机场三字码`
        );
        document.getElementById(buttonId)?.addEventListener('click', () => {
          if (kind === 'origin') {
            originManual.value = suggestion;
          } else {
            destinationInput.value = suggestion;
          }
          updateAirportSelection(kind);
          validateLocationField(kind);
          refreshPriceHint();
        });
        return false;
      }
      const airports = resolveAirportsForInput(value);
      if (!airports.length) {
        setFieldError(input, errorEl, `未识别'${value}',请输入机场三字码或已支持的城市`);
        return false;
      }
      setFieldError(input, errorEl, '');
      return true;
    }

    function validateDateFields() {
      const today = localDateString();
      let ok = true;
      if (form.depart_date?.value && form.depart_date.value < today) {
        setFieldError(form.depart_date, departDateError, '出发日期不能是过去');
        ok = false;
      } else {
        setFieldError(form.depart_date, departDateError, '');
      }
      if (checkedValue('round_trip') === 'true' && returnDate?.value && form.depart_date?.value && returnDate.value < form.depart_date.value) {
        setFieldError(returnDate, returnDateError, '返程不能早于出发');
        ok = false;
      } else {
        setFieldError(returnDate, returnDateError, '');
      }
      return ok;
    }

    function firstActiveAirport(kind) {
      return (airportState[kind].active || [])[0] || '';
    }

    let priceHintTimer = null;
    function refreshPriceHint() {
      if (!priceHint) return;
      window.clearTimeout(priceHintTimer);
      priceHintTimer = window.setTimeout(() => {
        const origin = firstActiveAirport('origin');
        const dest = firstActiveAirport('destination');
        const date = form.depart_date?.value || '';
        if (!origin || !dest || !date) {
          priceHint.textContent = '';
          return;
        }
        const params = new URLSearchParams({origin, dest, date});
        fetch(`/price_hint?${params.toString()}`)
          .then(resp => resp.ok ? resp.json() : Promise.reject(new Error('price hint failed')))
          .then(data => {
            if (data && data.has_data) {
              const scope = data.scope === 'roundtrip' ? '往返' : '直飞单程';
              priceHint.textContent = `💡 该航线近期参考区间:¥${data.low} - ¥${data.high}(${scope}), 中位约¥${data.typical}。你的理想价填在低位更容易触发提醒。`;
            } else {
              priceHint.textContent = '暂无该航线历史数据,系统会在采集后积累。';
            }
          })
          .catch(() => {
            priceHint.textContent = '暂无该航线历史数据,系统会在采集后积累。';
          });
      }, 250);
    }

    function updateAirportSelection(kind, activeAirports) {
      const value = kind === 'origin' ? selectedOrigin() : destinationInput.value.trim();
      updateAirportTags(kind, value, activeAirports);
    }

    function updateOriginAirportHint(activeAirports) {
      updateAirportSelection('origin', activeAirports);
    }

    function updateDestinationAirportHint(activeAirports) {
      updateAirportSelection('destination', activeAirports);
    }

    function activeAirportText(kind) {
      const active = airportState[kind].active;
      return active.length ? active.join('、') : '';
    }

    function inferRouteTypeFromAirports(originAirports, destinationAirports) {
      const airports = [...(originAirports || []), ...(destinationAirports || [])]
        .map(code => String(code || '').trim().toUpperCase())
        .filter(Boolean);
      if (airports.some(code => greaterChinaAirports.has(code))) {
        return 'greater_china';
      }
      const originDomestic = (originAirports || []).every(code => domesticAirports.has(String(code || '').toUpperCase()));
      const destDomestic = (destinationAirports || []).every(code => domesticAirports.has(String(code || '').toUpperCase()));
      if (originAirports.length && destinationAirports.length && originDomestic && destDomestic) {
        return 'domestic';
      }
      return 'international';
    }

    function routeTypeLabel(value) {
      return {
        domestic: '国内',
        international: '国际',
        greater_china: '港澳台'
      }[value] || value || '';
    }

    function routeTypeEnabledFeatures(value) {
      if (value === 'domestic') {
        return '已启用：多机场对比、当天往返、准点率、有效出行成本、报销友好';
      }
      if (value === 'greater_china') {
        return '已启用：通行证/签注提示、多机场对比、行李与部分中转风险提示';
      }
      return '已启用：中转风险、联程/非联程、过境签、时区时差、国际OTA验证';
    }

    function autoDetectRouteType(force = false) {
      if (!force && routeTypeTouched) {
        return;
      }
      const inferred = inferRouteTypeFromAirports(airportState.origin.active, airportState.destination.active);
      setRadio('route_type', inferred);
      if (inferred !== 'domestic' && sameDayRoundTrip) {
        sameDayRoundTrip.checked = false;
      }
      updateConditionalFields();
    }

    function setSmartPanel(panel, open) {
      if (!panel) {
        return;
      }
      if (open) {
        panel.style.display = 'block';
        window.requestAnimationFrame(() => panel.classList.add('open'));
        return;
      }
      panel.classList.remove('open');
      window.setTimeout(() => {
        if (!panel.classList.contains('open')) {
          panel.style.display = 'none';
        }
      }, 260);
    }

    function disablePreciseOnlyFields(disabled) {
      document.querySelectorAll('.precise-only').forEach(el => {
        el.querySelectorAll('input, select, textarea, button').forEach(control => {
          control.disabled = disabled;
        });
      });
    }

    function applyMonitorMode() {
      const precise = checkedValue('monitor_mode') === 'precise';
      document.querySelectorAll('.precise-only').forEach(el => {
        el.style.display = precise ? 'block' : 'none';
      });
      disablePreciseOnlyFields(!precise);
      document.querySelectorAll('.quick-only').forEach(el => {
        el.style.display = precise ? 'none' : 'block';
        el.querySelectorAll('input, select, textarea, button').forEach(control => {
          control.disabled = precise;
        });
      });
      if (precise) {
        syncPreciseDefaultsFromQuickScenarios();
      }
      if (quickDefaultsNote) {
        quickDefaultsNote.style.display = precise ? 'none' : 'block';
      }
      [advancedToggle, rulesToggle].forEach(button => {
        if (button) {
          button.style.display = precise ? 'block' : 'none';
        }
      });
      if (advancedToggle) {
        advancedToggle.textContent = precise ? '－ 收起补充偏好' : '＋ 补充偏好，让推荐更准确';
      }
      if (rulesToggle) {
        rulesToggle.textContent = precise ? '－ 收起筛选规则' : '＋ 更细的筛选规则';
      }
      setSmartPanel(advanced, precise);
      setSmartPanel(advancedRules, precise);
      toggleTimePreference();
      updateMeetingTimeHandoff();
      toggleShortTransferOptions();
      updateConditionalFields();
      refreshSummaryIfFinalStep();
    }

    function syncPreciseDefaultsFromQuickScenarios() {
      const scenarios = selectedTravelScenarios();
      const adultInput = document.querySelector('input[name="adult_count"]');
      const childInput = document.querySelector('input[name="child_count"]');
      const elderlyInput = document.querySelector('input[name="elderly_count"]');
      if (adultInput && Number(adultInput.value || 0) < 1) adultInput.value = '1';
      if (scenarios.includes('family') && childInput && Number(childInput.value || 0) < 1) childInput.value = '1';
      if ((scenarios.includes('elderly') || scenarios.includes('with_elderly')) && elderlyInput && Number(elderlyInput.value || 0) < 1) elderlyInput.value = '1';
      syncPrefCards();
    }

    function syncPreciseTimeDisabled() {
      const custom = checkedValue('time_preference') === 'custom'
        && checkedValue('monitor_mode') === 'precise'
        && !meetingHandoffActive();
      const preciseOpen = custom && preciseTimeOptions && preciseTimeOptions.style.display === 'block';
      if (preciseTimeToggle) {
        preciseTimeToggle.disabled = !custom;
      }
      preciseTimeOptions?.querySelectorAll('input, select, textarea').forEach(control => {
        control.disabled = !preciseOpen;
      });
    }

    function toggleTimePreference() {
      const preference = checkedValue('time_preference') || 'unlimited';
      const custom = preference === 'custom'
        && checkedValue('monitor_mode') === 'precise'
        && !meetingHandoffActive();
      if (customTimeOptions) {
        customTimeOptions.style.display = custom ? 'block' : 'none';
      }
      if (!custom && preciseTimeOptions) {
        preciseTimeOptions.style.display = 'none';
      }
      if (departurePolicyInput) {
        departurePolicyInput.value = preference === 'custom' ? 'any' : preference;
      }
      const isRoundTrip = checkedValue('round_trip') === 'true';
      if (singleTimePreferences) {
        singleTimePreferences.style.display = custom && !isRoundTrip ? 'block' : 'none';
      }
      if (roundTripTimePreferences) {
        roundTripTimePreferences.style.display = custom && isRoundTrip ? 'block' : 'none';
      }
      if (preciseTimeToggle) {
        preciseTimeToggle.style.display = custom ? 'block' : 'none';
      }
      syncPreciseTimeDisabled();
    }

    function timePreferenceText() {
      return timePreferenceTextFromValue(checkedValue('time_preference'));
    }

    function addMinutesToTime(value, minutes) {
      if (!value || !value.includes(':')) return '';
      const [hour, minute] = value.split(':').map(Number);
      if (Number.isNaN(hour) || Number.isNaN(minute)) return '';
      const total = (hour * 60 + minute + minutes + 24 * 60) % (24 * 60);
      return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
    }

    function meetingHandoffActive() {
      const start = document.querySelector('input[name="business_start"]')?.value || '';
      const end = document.querySelector('input[name="business_end"]')?.value || '';
      return checkedValue('monitor_mode') === 'precise'
        && Boolean(sameDayRoundTrip?.checked)
        && Boolean(start)
        && Boolean(end);
    }

    function hasManualTimeSettings() {
      if (checkedValue('time_preference') === 'custom') return true;
      return Array.from(document.querySelectorAll('#precise-time-options input[type="time"]'))
        .some(input => Boolean(input.value));
    }

    function airportBufferFor(code) {
      const upper = String(code || '').toUpperCase();
      const routeType = checkedValue('route_type') || 'domestic';
      const mega = new Set(['PVG','PEK','PKX','CAN','CTU','TFU','SZX']);
      const city = new Set(['SHA']);
      const size = mega.has(upper) ? 'mega' : (city.has(upper) ? 'city' : 'medium');
      const departure = {
        domestic: { mega: 110, medium: 90, city: 75 },
        international: { mega: 180, medium: 150, city: 150 },
        greater_china: { mega: 150, medium: 120, city: 120 }
      };
      const arrival = {
        domestic: { mega: 120, medium: 90, city: 60 },
        international: { mega: 170, medium: 130, city: 120 },
        greater_china: { mega: 130, medium: 100, city: 90 }
      };
      const route = departure[routeType] ? routeType : 'domestic';
      return { size, arrival: arrival[route][size], checkin: departure[route][size] };
    }

    function estimatedTransportFor(code) {
      const upper = String(code || '').toUpperCase();
      const table = { PVG: 60, SHA: 30, PEK: 50, PKX: 70, CAN: 45, SZX: 45, CTU: 35, TFU: 70 };
      return table[upper] || 45;
    }

    function firstActiveDestinationAirport() {
      return (airportState.destination.active && airportState.destination.active[0]) || '';
    }

    function meetingReserveParts() {
      const destAirport = firstActiveDestinationAirport();
      const buffers = airportBufferFor(destAirport);
      const transportInput = document.querySelector('input[name="user_transport_min"]');
      const userTransport = Number(transportInput?.value || 0);
      const transport = userTransport > 0 ? userTransport : estimatedTransportFor(destAirport);
      const redundancy = Number(checkedValue('redundancy_min') || 25);
      const marginMode = checkedValue('transport_margin_mode') || 'standard';
      const start = document.querySelector('input[name="business_start"]')?.value || '';
      const end = document.querySelector('input[name="business_end"]')?.value || '';
      const startMinutes = timeToMinutes(start);
      const endMinutes = timeToMinutes(end);
      const outboundTravelHour = startMinutes === null ? null : (startMinutes - transport) / 60;
      const returnTravelHour = endMinutes === null ? null : endMinutes / 60;
      const outboundMargin = transportMarginFor(transport, marginMode, outboundTravelHour);
      const returnMargin = transportMarginFor(transport, marginMode, returnTravelHour);
      return {
        destAirport,
        arrivalBuffer: buffers.arrival,
        checkinBuffer: buffers.checkin,
        transport,
        redundancy,
        marginMode,
        outboundMargin,
        returnMargin,
        outboundReserve: buffers.arrival + transport + outboundMargin.minutes + redundancy,
        returnReserve: transport + returnMargin.minutes + buffers.checkin + redundancy,
      };
    }

    function timeToMinutes(value) {
      if (!value || !value.includes(':')) return null;
      const [hour, minute] = value.split(':').map(Number);
      if (Number.isNaN(hour) || Number.isNaN(minute)) return null;
      return hour * 60 + minute;
    }

    function transportMarginFor(transport, mode, travelHour) {
      const ratios = { tight: 0.15, standard: 0.30, loose: 0.50 };
      let ratio = ratios[mode] || ratios.standard;
      let rush = false;
      if (travelHour !== null && travelHour !== undefined) {
        const hour = Number(travelHour);
        if (!Number.isNaN(hour) && ((hour >= 7 && hour < 9.5) || (hour >= 17 && hour < 19.5))) {
          ratio += 0.10;
          rush = true;
        }
      }
      return {
        minutes: Math.max(Math.round((Number(transport) || 0) * ratio), 15),
        ratio,
        rush
      };
    }

    function marginModeText(mode) {
      const map = { tight: '紧凑15%', standard: '标准30%', loose: '宽松50%' };
      return map[mode] || map.standard;
    }

    function formatReserveMinutes(minutes) {
      const total = Math.max(0, Number(minutes) || 0);
      const hours = Math.floor(total / 60);
      const mins = total % 60;
      if (hours && mins) return `${hours}小时${mins}分`;
      if (hours) return `${hours}小时`;
      return `${mins}分钟`;
    }

    function updateMeetingTimeHandoff() {
      const active = meetingHandoffActive();
      const start = document.querySelector('input[name="business_start"]')?.value || '';
      const end = document.querySelector('input[name="business_end"]')?.value || '';
      const reserve = meetingReserveParts();
      if (meetingTimeHandoffCard) {
        meetingTimeHandoffCard.style.display = active ? 'block' : 'none';
      }
      if (timePreferenceControls) {
        timePreferenceControls.classList.toggle('time-controls-disabled', active);
        timePreferenceControls.querySelectorAll('input, select, button').forEach(input => {
          input.disabled = active;
        });
      }
      const bufferPreview = document.getElementById('airport-buffer-preview');
      if (bufferPreview) {
        const airportText = reserve.destAirport || '目的地机场';
        const routeType = checkedValue('route_type') || 'domestic';
        const departureLabel = routeType === 'international'
          ? '值机+出境边检海关'
          : (routeType === 'greater_china' ? '值机+出入境查验' : '值机安检');
        bufferPreview.textContent = `机场标准缓冲:${airportText} 到达侧${reserve.arrivalBuffer}分钟; 出发侧${departureLabel}${reserve.checkinBuffer}分钟`;
      }
      if (meetingTimeHandoffText && active) {
        const outboundBy = addMinutesToTime(start, -reserve.outboundReserve) || '提交后计算';
        const returnAfter = addMinutesToTime(end, reserve.returnReserve) || '提交后计算';
        const outboundRushText = reserve.outboundMargin.rush ? '+早高峰10%' : '';
        const returnRushText = reserve.returnMargin.rush ? '+晚高峰10%' : '';
        meetingTimeHandoffText.innerHTML =
          `系统将按你的会议时间(${start}-${end})自动推算,通用时间偏好本次不生效:<br>` +
          `• 去程总预留 ≈ ${formatReserveMinutes(reserve.outboundReserve)},构成:<br>` +
          `- 机场到达缓冲:${reserve.arrivalBuffer}分钟<br>` +
          `- 车程:${reserve.transport}分钟<br>` +
          `- 路途冗余:${reserve.outboundMargin.minutes}分钟(${marginModeText(reserve.marginMode)}${outboundRushText})<br>` +
          `- 安全余量:${reserve.redundancy}分钟<br>` +
          `• 返程总预留 ≈ ${formatReserveMinutes(reserve.returnReserve)},其中路途冗余${reserve.returnMargin.minutes}分钟(${marginModeText(reserve.marginMode)}${returnRushText})<br>` +
          `→ 去程需 ${outboundBy} 前到达; 返程需 ${returnAfter} 后出发`;
        meetingTimeHandoffText.insertAdjacentHTML(
          'beforeend',
          '<br><span class="hint">≈ 估算值，提交后按目的地机场精确计算</span>'
        );
      }
      const marginHint = document.getElementById('transport-margin-hint');
      if (marginHint) {
        const hints = [];
        if (reserve.outboundMargin.rush) {
          hints.push('去程车程处于早高峰/晚高峰,系统已自动上浮10%,建议必要时选宽松档。');
        }
        if (reserve.returnMargin.rush) {
          hints.push('返程车程处于早高峰/晚高峰,系统已自动上浮10%,建议必要时选宽松档。');
        }
        marginHint.textContent = hints.join(' ');
      }
    }

    function timePreferenceTextFromValue(value) {
      const map = {
        any: '不限制',
        unlimited: '不限制',
        daytime: '白天优先',
        no_redeye: '不接受红眼/凌晨到达',
        custom: '自定义时间段'
      };
      return map[value] || '';
    }

    function selectedLabel(name) {
      const input = document.querySelector(`input[name="${name}"]:checked`);
      return input ? input.parentElement.textContent.trim() : '';
    }

    const prefCardLabelMaps = {
      companions: {
        solo: '未设置',
        couple_friends: '情侣/朋友',
        with_child: '有儿童',
        with_elderly: '有老人',
        with_elderly_child: '老人和儿童都有',
        with_both: '老人和儿童都有',
        group: '多人同行'
      },
      travel_scenario: {
        personal: '个人出行',
        business: '商务/会议',
        tourism: '旅游',
        family_visit: '探亲/回家',
        family: '家庭/亲子',
        elderly: '有老人同行',
        important: '重要事项',
        price_first: '价格优先'
      },
      time_preference: {
        any: '使用默认：避免红眼',
        unlimited: '使用默认：避免红眼',
        daytime: '白天优先',
        no_redeye: '不接受红眼/凌晨到达',
        custom: '自定义时间段'
      },
      refund_flexibility: {
        not_needed: '便宜优先',
        preferred: '使用默认：便宜优先，提醒风险',
        required: '必须能退票或改签',
        must_refundable: '必须能退票或改签',
        unknown: '不确定'
      },
      airline_policy: {
        any: '使用默认：不限',
        prefer_full_service: '偏好全服务航司',
        no_lcc: '不接受廉航',
        exclude_airlines: '已设置不接受航司'
      },
      accept_self_transfer: {
        false: '使用默认：不接受',
        true: '已设置：可以接受'
      }
    };

    function setPrefValue(key, value) {
      const idMap = {
        companions: 'pref-value-companion',
        time: 'pref-value-time',
        refund: 'pref-value-refund',
        airline: 'pref-value-airline',
        self_transfer: 'pref-value-self-transfer'
      };
      const target = document.getElementById(idMap[key]) || document.querySelector(`[data-pref-value="${key}"]`);
      if (target) {
        target.textContent = value || '未设置';
      }
    }

    function mappedPrefLabel(name, fallback) {
      const value = checkedValue(name);
      const map = prefCardLabelMaps[name] || {};
      if (!value) {
        return fallback || '未设置';
      }
      return map[value] || selectedLabel(name) || value;
    }

    function precisePassengerSummary(includeWeightNote = false) {
      const items = [
        ['成人', Number(document.querySelector('input[name="adult_count"]')?.value || 0)],
        ['儿童', Number(document.querySelector('input[name="child_count"]')?.value || 0)],
        ['老人', Number(document.querySelector('input[name="elderly_count"]')?.value || 0)],
        ['婴儿', Number(document.querySelector('input[name="infant_count"]')?.value || 0)]
      ].filter(([, count]) => count > 0);
      const total = items.reduce((sum, [, count]) => sum + count, 0);
      if (!total) return '';
      const summary = `${items.map(([label, count]) => `${label}${count}`).join(' + ')}，共${total}人`;
      const hasCareNeed = Number(document.querySelector('input[name="child_count"]')?.value || 0) > 0
        || Number(document.querySelector('input[name="elderly_count"]')?.value || 0) > 0
        || Number(document.querySelector('input[name="infant_count"]')?.value || 0) > 0;
      if (includeWeightNote && hasCareNeed) {
        return `${summary}（系统据此提高了直飞/白天/行李权重）`;
      }
      return summary;
    }

    function quickPassengerSummary() {
      const count = Number(document.querySelector('input[name="passenger_count"]')?.value || 1);
      return `购票人数：${Math.max(1, count)}人`;
    }

    function syncPrefCards() {
      setPrefValue('companions', precisePassengerSummary() || mappedPrefLabel('companions', '未设置'));
      setPrefValue('time', mappedPrefLabel('time_preference', '使用默认：避免红眼'));
      setPrefValue('refund', mappedPrefLabel('refund_flexibility', '使用默认：便宜优先，提醒风险'));
      setPrefValue('airline', mappedPrefLabel('airline_policy', '使用默认：不限'));
      setPrefValue('self_transfer', mappedPrefLabel('accept_self_transfer', '使用默认：不接受'));
    }

    function updatePrefCards() {
      syncPrefCards();
    }

    function togglePrefDetail(key) {
      const detail = document.getElementById(`pref-detail-${key}`);
      if (!detail) return;
      prefCardDetails.forEach(item => {
        if (item !== detail) {
          item.classList.remove('open');
        }
      });
      detail.classList.toggle('open');
      if (detail.classList.contains('open')) {
        detail.scrollIntoView({behavior: 'smooth', block: 'nearest'});
      }
      syncPrefCards();
    }

    function selectedCheckboxLabels(name) {
      return Array.from(document.querySelectorAll(`input[name="${name}"]:checked`))
        .map(input => input.parentElement.textContent.trim());
    }

    function money(value) {
      const num = Number(value || 0);
      return num > 0 ? `¥${num.toLocaleString('zh-CN')}` : '';
    }

    function summaryLine(label, value, target = '') {
      if (!value) {
        return;
      }
      addSummaryLine(`${label}: ${value}`, '', target);
    }

    function displayDate(value) {
      return value ? value.replaceAll('-', '/') : '';
    }

    function toggleReturnDate() {
      const isRoundTrip = checkedValue('round_trip') === 'true';
      returnWrap.style.display = isRoundTrip ? 'block' : 'none';
      returnDate.required = isRoundTrip;
      toggleTimePreference();
    }

    function syncSameDayRoundTrip() {
      if (checkedValue('route_type') !== 'domestic' && sameDayRoundTrip) {
        sameDayRoundTrip.checked = false;
      }
      if (!sameDayRoundTrip || !sameDayRoundTrip.checked) {
        updateMeetingTimeHandoff();
        return;
      }
      setRadio('round_trip', 'true');
      if (form.depart_date && returnDate) {
        returnDate.value = form.depart_date.value || returnDate.value;
      }
      const direct = document.querySelector('input[name="transfer_policy"][value="direct_only"]');
      if (direct) {
        direct.checked = true;
      }
      const business = document.querySelector('input[name="travel_scenario"][value="business"]');
      if (business) {
        business.checked = true;
      }
      updateConditionalFields();
      toggleReturnDate();
      toggleShortTransferOptions();
      updateMeetingTimeHandoff();
      refreshSummaryIfFinalStep();
    }

    function toggleBudgetRequired() {
      const strategy = checkedValue('price_strategy');
      const explicit = strategy === 'explicit';
      if (budgetAmountFields) {
        budgetAmountFields.style.display = explicit ? 'block' : 'none';
      }
      maxBudgetInput.required = false;
      targetPriceInput.required = false;
      if (!explicit) {
        if (strategy === 'auto_judge') {
          setRadio('max_budget_mode', 'none');
          setRadio('target_price_mode', 'auto');
        } else if (strategy === 'low_price_alert') {
          setRadio('max_budget_mode', 'none');
          setRadio('target_price_mode', 'low_zone');
        }
      } else {
        setRadio('max_budget_mode', 'fixed');
        setRadio('target_price_mode', 'fixed');
      }
      validatePriceInputs();
      updateRequiredProgress();
    }

    function toggleNotificationMethod() {
      const method = checkedValue('notification_method') || 'pushplus';
      const needsEmail = method === 'email' || method === 'both';
      if (emailReminderWrap) {
        emailReminderWrap.style.display = needsEmail ? 'block' : 'none';
      }
      if (notificationEmailInput) {
        notificationEmailInput.required = needsEmail;
      }
      if (pageOnlyHint) {
        pageOnlyHint.style.display = method === 'page_only' ? 'block' : 'none';
      }
      validateEmailField(false);
    }

    function validateEmailField(showAlert) {
      const method = checkedValue('notification_method') || 'pushplus';
      const needsEmail = method === 'email' || method === 'both';
      const value = notificationEmailInput ? notificationEmailInput.value.trim() : '';
      const ok = !needsEmail || /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(value);
      if (emailError) {
        emailError.textContent = ok ? '' : (value ? '邮箱格式不正确，请检查' : '请填写接收邮箱');
        emailError.style.display = ok ? 'none' : 'block';
      }
      if (!ok && showAlert) {
        alert(value ? '邮箱格式不正确，请检查' : '请填写接收邮箱');
        notificationEmailInput?.focus();
      }
      return ok;
    }

    function syncNotificationFrequencyFromRule() {
      syncNotificationFrequencyShadow();
    }

    function syncNotificationFrequencyToRule() {
      const value = checkedValue('notification_frequency');
      if (notificationFrequencyRuleShadow) {
        notificationFrequencyRuleShadow.value = value || 'important_only';
      }
    }

    function syncNotificationFrequencyShadow() {
      syncNotificationFrequencyToRule();
    }

    function toggleShortTransferOptions() {
      const policy = checkedValue('transfer_policy');
      const precise = checkedValue('monitor_mode') === 'precise';
      const visible = precise && (policy === 'reasonable' || policy === 'price_first');
      const priceFirst = precise && policy === 'price_first';
      shortTransferInputs.forEach(input => {
        input.disabled = !visible;
      });
      overnightTransferOptions?.querySelectorAll('input, select').forEach(input => {
        input.disabled = !priceFirst;
      });
      selfTransferOptions?.querySelectorAll('input, select').forEach(input => {
        input.disabled = !priceFirst;
      });
    }

    function validatePriceInputs() {
      const maxBudgetMode = checkedValue('max_budget_mode');
      const targetPriceMode = checkedValue('target_price_mode');
      const maxBudget = Number(maxBudgetInput.value || 0);
      const targetPrice = Number(targetPriceInput.value || 0);
      const invalid = checkedValue('price_strategy') === 'explicit'
        && maxBudgetMode === 'fixed'
        && targetPriceMode === 'fixed'
        && maxBudget > 0
        && targetPrice > 0
        && targetPrice > maxBudget;

      priceValidationError.style.display = invalid ? 'block' : 'none';
      targetPriceInput.setCustomValidity(
        invalid ? '理想入手价应低于最高可接受价，请确认是否填反了' : ''
      );
      return !invalid;
    }

    function applyDefaultSecondaryGoals() {
      const defaults = goalDefaults[checkedValue('primary_goal')] || [];
      secondaryGoalChecks.forEach(check => {
        check.checked = defaults.includes(check.value);
      });
      updateDateFlexHint();
    }

    function updateDateFlexHint() {
      const fixedDate = checkedValue('date_flexibility') === '0';
      const showHint = fixedDate;
      if (cheaperDateLabel) {
        cheaperDateLabel.classList.toggle('muted-option', fixedDate);
      }
      if (dateFlexWarning) {
        dateFlexWarning.style.display = showHint ? 'block' : 'none';
      }
    }

    function setRadio(name, value, mark = false) {
      const input = document.querySelector(`input[name="${name}"][value="${value}"]`);
      if (!input) return;
      input.checked = true;
      if (mark) {
        input.closest('label')?.classList.add('auto-suggested');
      }
    }

    function setCheckbox(name, value, checked, mark = false) {
      const input = document.querySelector(`input[name="${name}"][value="${value}"]`);
      if (!input) return;
      input.checked = checked;
      if (mark) {
        input.closest('label')?.classList.add('auto-suggested');
      }
    }

    const scenarioDefaults = {
      personal: {
        notice: '个人出行：默认按价格和便利性均衡处理，可接受合理中转和早晚班。',
        radios: {price_sensitivity: 'medium', transfer_policy: 'reasonable'}
      },
      business: {
        notice: '商务/会议：已默认提高准点、直飞、可改签和到达时间稳定性的权重。',
        radios: {time_preference: 'daytime', transfer_policy: 'direct_only', refund_flexibility: 'preferred', airline_policy: 'prefer_full_service'}
      },
      tourism: {
        notice: '旅游：已默认提高低价日期和合理中转权重，适合继续比较前后日期。',
        radios: {price_sensitivity: 'high', transfer_policy: 'reasonable', date_flexibility: '3'}
      },
      family_visit: {
        notice: '探亲/回家：已默认提高行李明确和合理价格权重，不推荐极端折腾方案。',
        radios: {baggage: 'required', price_sensitivity: 'medium', transfer_policy: 'reasonable'}
      },
      family: {
        notice: '家庭/亲子：已默认优先白天航班、直飞/短中转、行李明确，降低红眼和高风险中转推荐。',
        radios: {companions: 'with_child', time_preference: 'no_redeye', transfer_policy: 'reasonable', short_transfer_limit: 'extra_3', baggage: 'required'},
        constraints: ['direct_preferred', 'no_redeye', 'avoid_long_layover', 'need_baggage', 'daytime_arrival']
      },
      elderly: {
        notice: '有老人同行：已默认优先直飞/短中转、白天到达、全服务航司和可退改方案。',
        radios: {companions: 'with_elderly', time_preference: 'no_redeye', transfer_policy: 'reasonable', short_transfer_limit: 'extra_3', baggage: 'required', refund_flexibility: 'preferred', airline_policy: 'prefer_full_service'},
        constraints: ['direct_preferred', 'no_redeye', 'avoid_long_layover', 'need_baggage', 'need_refund_change', 'daytime_arrival']
      },
      important: {
        notice: '重要事项：已启用保守默认规则，优先提前/稳定到达、可退改，降低复杂中转、非联程和红眼风险。',
        radios: {time_preference: 'no_redeye', transfer_policy: 'direct_only', refund_flexibility: 'required', accept_self_transfer: 'false', accept_overnight_transfer: 'false'},
        constraints: ['direct_preferred', 'no_redeye', 'need_refund_change', 'daytime_arrival']
      },
      price_first: {
        notice: '价格优先：已默认提高低价权重，允许中转和一定不便，系统仍会提示执行风险。',
        radios: {price_sensitivity: 'max', transfer_policy: 'price_first'}
      }
    };

    function showPreciseCompanionSettings() {
      setRadio('monitor_mode', 'precise');
      applyMonitorMode();
      setSmartPanel(advanced, true);
      togglePrefDetail('companions');
      document.getElementById('pref-detail-companions')?.scrollIntoView({behavior: 'smooth', block: 'center'});
    }

    function applyTravelScenarioDefaults() {
      let scenarios = selectedTravelScenarios();
      if (scenarios.length > 1 && scenarios.includes('personal')) {
        const personal = document.querySelector('input[name="travel_scenario"][value="personal"]');
        if (personal) personal.checked = false;
        scenarios = scenarios.filter(value => value !== 'personal');
      }
      if (!scenarios.length) {
        setCheckbox('travel_scenario', 'personal', true);
        scenarios = ['personal'];
      }
      const configs = scenarios.map(value => scenarioDefaults[value] || scenarioDefaults.personal);
      clearAutoSuggestions();
      document.querySelectorAll('input[name="companion_constraints"]').forEach(input => {
        input.checked = false;
        input.closest('label')?.classList.remove('auto-suggested');
      });
      configs.forEach(config => {
        Object.entries(config.radios || {}).forEach(([name, value]) => setRadio(name, value, true));
        (config.constraints || []).forEach(value => setCheckbox('companion_constraints', value, true, true));
      });
      toggleTimePreference();
      toggleShortTransferOptions();
      updateConditionalFields();
      syncPrefCards();
      if (travelScenarioNotice) {
        const scenarioText = selectedLabels('travel_scenario').join(' + ');
        const notices = configs.map(config => config.notice).filter(Boolean);
        const tradeoffs = [];
        if (scenarios.includes('tourism') && scenarios.includes('family')) {
          tradeoffs.push('说明：孩子出行的安全舒适要求会优先于纯价格考虑。');
        }
        if (scenarios.includes('elderly') && scenarios.includes('family_visit')) {
          tradeoffs.push('说明：老人同行的直飞、白天到达和稳定性会优先于极致低价。');
        }
        if (scenarios.includes('price_first') && scenarios.includes('important')) {
          tradeoffs.push('说明：重要事项会先保证可靠性，再在可靠方案中选择更低价格。');
        }
        travelScenarioNotice.innerHTML = `已按【${scenarioText}】组合启用规则：<br>${notices.map(item => `✓ ${item}`).join('<br>')}${tradeoffs.length ? '<br>' + tradeoffs.join('<br>') : ''} <button id="scenario-open-precise" class="link-button" type="button">想调整？进入精准设置</button>`;
        travelScenarioNotice.style.display = 'block';
        document.getElementById('scenario-open-precise')?.addEventListener('click', showPreciseCompanionSettings);
      }
      refreshSummaryIfFinalStep();
    }

    function applyEditSubscription(data) {
      if (!data || !Object.keys(data).length) return;
      document.getElementById('subscription_index').value = data._index ?? document.getElementById('subscription_index').value;
      if (data.origin_type === 'city' && originSelect.querySelector(`option[value="${data.origin}"]`)) {
        originSelect.value = data.origin;
      } else {
        originSelect.value = 'OTHER';
        originManual.value = data.origin || '';
      }
      destinationInput.value = data.destination || '';
      updateOriginAirportHint(data.origin_airports_active || data.origin_airports);
      updateDestinationAirportHint(data.destination_airports_active || data.destination_airports || data.dest_airports);
      const savedRouteType = (data.basic && data.basic.route_type) || data.route_type || '';
      if (savedRouteType) {
        routeTypeTouched = true;
        setRadio('route_type', savedRouteType);
      }
      setRadio('round_trip', String(Boolean(data.round_trip)));
      form.depart_date.value = data.depart_date || '';
      if (data.return_date) {
        returnDate.value = data.return_date;
      }
      setRadio('date_flexibility', String(data.date_flexibility ?? 0));
      const hard = data.hard_constraints || {};
      const soft = data.soft_preferences || {};
      const goals = data.notification_goals || {};
      if (data.monitor_mode) setRadio('monitor_mode', data.monitor_mode);
      if (hard.budget_strategy) setRadio('price_strategy', hard.budget_strategy);
      if (hard.max_budget) maxBudgetInput.value = hard.max_budget;
      if (hard.target_price || soft.target_price) targetPriceInput.value = hard.target_price || soft.target_price;
      if (hard.transfer_policy) setRadio('transfer_policy', hard.transfer_policy);
      if (hard.baggage) setRadio('baggage', hard.baggage);
      const savedScenarios = Array.isArray(soft.travel_scenarios)
        ? soft.travel_scenarios
        : String(soft.travel_scenarios || soft.travel_scenario || '').split(',').map(item => item.trim()).filter(Boolean);
      if (savedScenarios.length) {
        document.querySelectorAll('input[name="travel_scenario"]').forEach(input => { input.checked = false; });
        savedScenarios.forEach(value => setCheckbox('travel_scenario', value, true));
      }
      if (soft.companions) setRadio('companions', soft.companions);
      const savedCompanionConstraints = Array.isArray(soft.companion_constraints)
        ? soft.companion_constraints
        : String(soft.companion_constraints || '').split(',').map(item => item.trim()).filter(Boolean);
      savedCompanionConstraints.forEach(value => setCheckbox('companion_constraints', value, true));
      const soloTravelInput = document.querySelector('input[name="solo_travel"]');
      const noLateArrivalInput = document.querySelector('input[name="no_late_arrival"]');
      const preferDaytimeArrivalInput = document.querySelector('input[name="prefer_daytime_arrival"]');
      if (soloTravelInput) soloTravelInput.checked = Boolean(soft.solo_travel);
      if (noLateArrivalInput) noLateArrivalInput.checked = Boolean(soft.no_late_arrival);
      if (preferDaytimeArrivalInput) preferDaytimeArrivalInput.checked = Boolean(soft.prefer_daytime_arrival);
      const savedTimeMode = soft.time_preference_mode || soft.time_preference || hard.time_preference;
      if (savedTimeMode) setRadio('time_preference', savedTimeMode === 'any' ? 'unlimited' : savedTimeMode);
      if (soft.refund_flexibility) setRadio('refund_flexibility', soft.refund_flexibility);
      if (soft.price_sensitivity) setRadio('price_sensitivity', soft.price_sensitivity);
      if (soft.airline_policy) setRadio('airline_policy', soft.airline_policy);
      if (soft.exclude_airlines) form.exclude_airlines.value = (soft.exclude_airlines || []).join(', ');
      if (hard.accept_self_transfer !== undefined) setRadio('accept_self_transfer', String(Boolean(hard.accept_self_transfer)));
      if (hard.accept_overnight_transfer !== undefined) setRadio('accept_overnight_transfer', String(Boolean(hard.accept_overnight_transfer)));
      if (goals.primary) setRadio('primary_goal', goals.primary);
      secondaryGoalChecks.forEach(check => {
        check.checked = (goals.secondary || []).includes(check.value);
      });
      if (goals.method) setRadio('notification_method', goals.method);
      if (goals.email) notificationEmailInput.value = goals.email;
      if (goals.frequency) {
        setRadio('notification_frequency', goals.frequency);
        syncNotificationFrequencyShadow();
      }
    }

    function slotSummary(name, labelMap) {
      const values = checkedValues(name);
      if (!values.length) return '未选择';
      return values.map(value => labelMap[value] || value).join('、');
    }

    function clearAutoSuggestions() {
      document.querySelectorAll('.auto-suggested').forEach(item => {
        item.classList.remove('auto-suggested');
      });
      autoPreferenceNotice.style.display = 'none';
      autoPreferenceNotice.textContent = '';
    }

    function applyCompanionDefaults() {
      const companions = checkedValue('companions');
      clearAutoSuggestions();
      if (companions === 'solo' || companions === 'couple_friends') {
        return;
      }
      if (companions === 'group') {
        autoPreferenceNotice.textContent = '多人同行：低价库存可能不足，系统会提高最终支付价校验和库存可购买性权重';
        autoPreferenceNotice.style.display = 'block';
        return;
      }

      [
        'departure_slots',
        'outbound_departure_slots',
        'return_departure_slots'
      ].forEach(name => setCheckbox(name, 'redeye', false, true));
      if (departurePolicyInput) {
        departurePolicyInput.value = 'no_redeye';
      }
      setRadio('time_preference', 'no_redeye', true);
      toggleTimePreference();
      setRadio('baggage', 'required', true);

      if (companions === 'with_elderly' || companions === 'with_elderly_child') {
        setRadio(
          'transfer_policy',
          companions === 'with_elderly_child' ? 'direct_only' : 'reasonable',
          true
        );
        setRadio('short_transfer_limit', 'extra_3', true);
        toggleShortTransferOptions();
        autoPreferenceNotice.textContent = '已根据老人同行自动调整推荐偏好，你仍可手动修改';
      } else if (companions === 'with_child') {
        autoPreferenceNotice.textContent = '已根据带小孩出行自动调整推荐偏好，你仍可手动修改';
      }
      if (companions === 'with_child') {
        setRadio('transfer_policy', 'reasonable', true);
        setRadio('short_transfer_limit', 'extra_3', true);
      }
      toggleShortTransferOptions();
      autoPreferenceNotice.textContent = '已根据老人/小孩同行，默认提高白天航班、直飞、行李权重，你仍可手动修改';
      autoPreferenceNotice.style.display = 'block';
    }

    function addSummaryLine(text, className = '', target = '') {
      const li = document.createElement('li');
      if (className) {
        li.className = className;
      }
      if (target) {
        li.classList.add('summary-row');
        const span = document.createElement('span');
        span.textContent = text;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'summary-row-edit';
        button.dataset.summaryTarget = target;
        button.textContent = '修改';
        li.appendChild(span);
        li.appendChild(button);
      } else {
        li.textContent = text;
      }
      summaryList.appendChild(li);
    }

    function addSummaryHeader(text) {
      addSummaryLine(text, 'summary-section-title');
    }

    const summaryEditTargets = {
      route_type: {step: 1, selector: 'input[name="route_type"]'},
      route: {step: 1, selector: '#destination'},
      trip_dates: {step: 1, selector: 'input[name="round_trip"]'},
      date_flexibility: {step: 1, selector: 'input[name="date_flexibility"]'},
      price_strategy: {step: 2, selector: 'input[name="price_strategy"]'},
      transfer_policy: {step: 2, selector: 'input[name="transfer_policy"]'},
      baggage: {step: 2, selector: 'input[name="baggage"]'},
      scenarios: {step: 3, selector: 'input[name="travel_scenario"]'},
      passengers: {step: 3, selector: 'input[name="passenger_count"], input[name="adult_count"]'},
      primary_goal: {step: 3, selector: 'input[name="primary_goal"]'},
      notification: {step: 3, selector: 'input[name="notification_method"]'},
      notification_frequency: {step: 3, selector: 'input[name="notification_frequency"]'},
      precise_companions: {step: 3, selector: '#advanced-preferences'},
      precise_time: {step: 3, selector: '#pref-detail-time'},
      precise_business: {step: 3, selector: '#business-rules-module'},
      precise_rules: {step: 3, selector: '#advanced-rules'}
    };

    function highlightField(el) {
      if (!el) return;
      el.classList.add('summary-highlight');
      el.scrollIntoView({behavior: 'smooth', block: 'center'});
      if (typeof el.focus === 'function' && !el.disabled) {
        el.focus({preventScroll: true});
      }
      window.setTimeout(() => el.classList.remove('summary-highlight'), 1800);
    }

    function editSummaryTarget(target) {
      const config = summaryEditTargets[target];
      if (!config) return;
      summaryCard.style.display = 'none';
      goToStep(config.step);
      if (target.startsWith('precise_')) {
        setRadio('monitor_mode', 'precise');
        applyMonitorMode();
      }
      if (['precise_companions', 'precise_time', 'precise_business'].includes(target)) {
        setSmartPanel(advanced, true);
      }
      if (target === 'precise_rules') {
        setSmartPanel(advancedRules, true);
      }
      const targetEl = document.querySelector(config.selector);
      if (targetEl && targetEl.classList.contains('pref-card-detail')) {
        targetEl.classList.add('open');
      }
      highlightField(targetEl);
    }

    function systemDefaultRulesForSummary() {
      const precise = checkedValue('monitor_mode') === 'precise';
      const rules = [];
      const scenarios = selectedTravelScenarios();
      if (scenarios.includes('family')) {
        rules.push('✓ 家庭/亲子：优先白天航班、直飞/短中转、行李明确');
      }
      if (scenarios.includes('elderly')) {
        rules.push('✓ 老人同行：优先白天到达、短中转、全服务航司和可退改');
      }
      if (scenarios.includes('business')) {
        rules.push('✓ 商务/会议：准点、直飞和可改签优先');
      }
      if (scenarios.includes('tourism')) {
        rules.push('✓ 旅游：保留价格敏感和日期弹性');
      }
      if (scenarios.includes('family_visit')) {
        rules.push('✓ 探亲/回家：行李权重高，避免极端折腾');
      }
      if (scenarios.includes('important')) {
        rules.push('✓ 重要事项：降低复杂中转、非联程和红眼风险');
      }
      if (scenarios.includes('price_first')) {
        rules.push('✓ 价格优先：低价权重最高，同时提示执行风险');
      }
      if (scenarios.includes('tourism') && scenarios.includes('family')) {
        rules.push('✓ 冲突权衡：孩子安全舒适优先于纯低价');
      }
      if (scenarios.includes('elderly') && scenarios.includes('family_visit')) {
        rules.push('✓ 冲突权衡：老人同行优先直飞、白天到达和稳定性');
      }
      if (scenarios.includes('price_first') && scenarios.includes('important')) {
        rules.push('✓ 冲突权衡：重要事项先保证可靠性，再比较价格');
      }
      if (meetingHandoffActive()) {
        rules.push('✓ 会议模式接管时间设置：通用时间偏好本次不生效');
      } else if (!precise || !moduleIsDirty('time')) {
        rules.push('✓ 不推荐红眼/凌晨到达');
      }
      if (!precise || checkedValue('baggage') === 'unknown') {
        rules.push('✓ 优先含托运行李方案');
      }
      if (!precise || !moduleIsDirty('transfer')) {
        rules.push('✓ 不推荐非联程中转');
      }
      if (!precise || !moduleIsDirty('transfer')) {
        rules.push('✓ 不推荐过夜中转');
      }
      if (!moduleIsDirty('alerts') || checkedValue('notification_frequency') === 'important_only') {
        rules.push('✓ 只在重要变化时提醒');
      }
      rules.push('✓ 自动检测异常低价和涨价风险');
      return [...new Set(rules)];
    }

    function inputCurrentState(input) {
      return (input.type === 'radio' || input.type === 'checkbox')
        ? String(input.checked)
        : input.value;
    }

    function moduleIsDirty(moduleName) {
      return moduleInputs(moduleName).some(input => (
        input.dataset.default !== undefined && inputCurrentState(input) !== input.dataset.default
      ));
    }

    function addAdvancedRule(label, value) {
      if (value) {
        addSummaryLine(`${label}: ${value}`, 'summary-advanced-rule');
        return true;
      }
      return false;
    }

    function validateCabinArrangement(showAlert = false) {
      const warning = document.getElementById('cabin-arrangement-warning');
      const hasBusinessNature = checkedValues('trip_natures').length > 0;
      const arrangement = checkedValue('cabin_arrangement');
      if (!hasBusinessNature || arrangement !== 'mixed') {
        if (warning) warning.style.display = 'none';
        return true;
      }
      const total = Number(document.querySelector('input[name="team_passenger_count"]')?.value || 0);
      const business = Number(document.querySelector('input[name="business_seats"]')?.value || 0);
      const economy = Number(document.querySelector('input[name="economy_seats"]')?.value || 0);
      const valid = !total || business + economy === total;
      if (warning) warning.style.display = valid ? 'none' : 'block';
      if (!valid && showAlert) {
        alert('商务舱人数 + 经济舱人数应等于同行总人数');
      }
      return valid;
    }

    function customTimeSummary() {
      const isRoundTrip = checkedValue('round_trip') === 'true';
      if (isRoundTrip) {
        return [
          `去程起飞 ${slotSummary('outbound_departure_slots', labels.departureSlots)}`,
          `去程到达 ${slotSummary('outbound_arrival_slots', labels.arrivalSlots)}`,
          `返程起飞 ${slotSummary('return_departure_slots', labels.departureSlots)}`,
          `返程到达 ${slotSummary('return_arrival_slots', labels.arrivalSlots)}`
        ].join('；');
      }
      return [
        `起飞 ${slotSummary('departure_slots', labels.departureSlots)}`,
        `到达 ${slotSummary('arrival_slots', labels.arrivalSlots)}`
      ].join('；');
    }

    function addPreciseRulesForSummary() {
      if (checkedValue('monitor_mode') !== 'precise') {
        return;
      }
      const lines = [];
      if (moduleIsDirty('time')) {
        const timeText = checkedValue('time_preference') === 'custom'
          ? customTimeSummary()
          : timePreferenceText();
        lines.push(['时间', timeText]);
      }
      if (moduleIsDirty('transfer')) {
        const parts = [];
        if (checkedValue('transfer_policy') !== 'direct_only') {
          parts.push(`总时长 ${selectedLabel('short_transfer_limit')}`);
        }
        parts.push(`过夜中转 ${selectedLabel('accept_overnight_transfer')}`);
        parts.push(`非联程 ${selectedLabel('accept_self_transfer')}`);
        lines.push(['中转', parts.join('，')]);
      }
      if (moduleIsDirty('airline')) {
        const blocked = selectedCheckboxLabels('blocked_airlines_common');
        const typed = document.querySelector('input[name="exclude_airlines"]')?.value.trim();
        const blockedText = [typed, ...blocked].filter(Boolean).join('、');
        lines.push(['航司', blockedText ? `不接受 ${blockedText}` : selectedLabel('airline_policy')]);
      }
      if (moduleIsDirty('alerts')) {
        const secondary = selectedCheckboxLabels('secondary_goals');
        const text = secondary.length ? secondary.join('、') : selectedLabel('primary_goal');
        lines.push(['提醒', text]);
      }
      if (!lines.length) {
        return;
      }
      addSummaryHeader('【精准规则】（你自定义的）');
      lines.forEach(([label, value]) => addAdvancedRule(label, value));
    }

    function buildSummary() {
      summaryList.innerHTML = "";
      const origin = selectedOrigin();
      const destination = destinationInput.value.trim();
      const isRoundTrip = checkedValue('round_trip') === 'true';
      const maxBudgetMode = checkedValue('max_budget_mode');
      const targetPriceMode = checkedValue('target_price_mode');

      addSummaryHeader('【你填写的条件】');
      const routeType = checkedValue('route_type');
      summaryLine('航线类型', routeType ? `${routeTypeLabel(routeType)}（${routeTypeEnabledFeatures(routeType)}）` : '', 'route_type');
      summaryLine(
        '路线',
        origin && destination
          ? `${origin} ${activeAirportText('origin') || ''} → ${destination} ${activeAirportText('destination') || ''}`
          : '',
        'route'
      );
      summaryLine('行程', isRoundTrip ? '往返' : '单程', 'trip_dates');
      summaryLine('出发日期', displayDate(form.depart_date.value), 'trip_dates');
      if (isRoundTrip) {
        summaryLine('返程日期', displayDate(returnDate.value), 'trip_dates');
      }
      summaryLine('日期弹性', selectedLabel('date_flexibility'), 'date_flexibility');
      summaryLine(
        '购票人数',
        checkedValue('monitor_mode') === 'precise'
          ? precisePassengerSummary(true)
          : quickPassengerSummary().replace('购票人数：', ''),
        'passengers'
      );
      summaryLine('价格策略', selectedLabel('price_strategy'), 'price_strategy');
      if (checkedValue('price_strategy') === 'explicit') {
        summaryLine(
          '最高可接受价',
          maxBudgetMode === 'fixed' ? money(maxBudgetInput.value) : selectedLabel('max_budget_mode'),
          'price_strategy'
        );
        summaryLine(
          '理想入手价',
          targetPriceMode === 'fixed' ? money(targetPriceInput.value) : selectedLabel('target_price_mode'),
          'price_strategy'
        );
      }
      summaryLine('中转', selectedLabel('transfer_policy'), 'transfer_policy');
      summaryLine('行李', selectedLabel('baggage'), 'baggage');
      summaryLine('出行场景', selectedScenarioLabels().join(' + '), 'scenarios');
      if (checkedValue('companions') !== 'solo') {
        summaryLine('同行', selectedLabel('companions'), 'precise_companions');
      }
      if (checkedValue('monitor_mode') === 'precise') {
        const companionDetails = selectedCheckboxLabels('companion_constraints');
        if (companionDetails.length) {
          summaryLine('同行具体约束', companionDetails.join('、'), 'precise_companions');
        }
        const realNeeds = [];
        if (document.querySelector('input[name="solo_travel"]')?.checked) realNeeds.push('独自出行');
        if (document.querySelector('input[name="no_late_arrival"]')?.checked) realNeeds.push('不接受深夜到达');
        if (document.querySelector('input[name="prefer_daytime_arrival"]')?.checked) realNeeds.push('希望优先白天到达');
        if (realNeeds.length) {
          summaryLine('实际需求', realNeeds.join('、'), 'precise_companions');
        }
        const tripNatureLabels = selectedLabels('trip_natures');
        summaryLine('出行性质', tripNatureLabels.join(' + '), 'precise_business');
        if (checkedValues('trip_natures').includes('meeting')) {
          const meetingStart = document.querySelector('input[name="meeting_start"]')?.value || '';
          const meetingEnd = document.querySelector('input[name="meeting_end"]')?.value || '';
          if (meetingStart || meetingEnd) summaryLine('会议时间', `${meetingStart || '未填'}-${meetingEnd || '未填'}（系统将按此反推航班+预留车程缓冲）`, 'precise_business');
        }
        if (checkedValues('trip_natures').includes('team_building')) {
          summaryLine('团建规则', `${selectedLabel('team_date_flexibility')}，全员同航班：${selectedLabel('same_flight_required')}`, 'precise_business');
        }
        summaryLine('舱位政策', selectedLabel('cabin_policy'), 'precise_business');
        if (checkedValues('trip_natures').length) {
          summaryLine('职级', selectedLabel('user_level'), 'precise_business');
          const teamCount = document.querySelector('input[name="team_passenger_count"]')?.value || '';
          if (teamCount) summaryLine('团队人数', `${teamCount}人`, 'precise_business');
          summaryLine('舱位安排', selectedLabel('cabin_arrangement'), 'precise_business');
          const businessSeats = document.querySelector('input[name="business_seats"]')?.value || '';
          const economySeats = document.querySelector('input[name="economy_seats"]')?.value || '';
          if (businessSeats || economySeats) {
            summaryLine('团队舱位', `商务${businessSeats || 0}人，经济${economySeats || 0}人`, 'precise_business');
          }
        }
        const reimburse = document.querySelector('input[name="reimburse_per_person"]')?.value || '';
        if (reimburse) summaryLine('每人报销上限', money(reimburse), 'precise_business');
        const invoiceNeeds = [];
        if (document.querySelector('input[name="invoice_needed"]')?.checked) invoiceNeeds.push('需要行程单/发票');
        if (document.querySelector('input[name="invoice_special_vat"]')?.checked) invoiceNeeds.push('优先可开专票渠道');
        if (document.querySelector('input[name="invoice_cabin_limit"]')?.checked) invoiceNeeds.push('报销有舱位限制');
        if (invoiceNeeds.length) {
          summaryLine('报销需求', invoiceNeeds.join('、'), 'precise_business');
        }
        if (meetingHandoffActive()) {
          const start = document.querySelector('input[name="business_start"]')?.value || '';
          const end = document.querySelector('input[name="business_end"]')?.value || '';
          const reserve = meetingReserveParts();
            summaryLine(
              '时间安排',
              `由会议时间推算:会议${start || '未填'}-${end || '未填'} | 去程预留${formatReserveMinutes(reserve.outboundReserve)}(含路途冗余${reserve.outboundMargin.minutes}分钟) → ${addMinutesToTime(start, -reserve.outboundReserve) || '提交后计算'}前到达 | 返程预留${formatReserveMinutes(reserve.returnReserve)}(含路途冗余${reserve.returnMargin.minutes}分钟) → ${addMinutesToTime(end, reserve.returnReserve) || '提交后计算'}后出发`,
              'precise_time'
            );
        } else {
          summaryLine('时间偏好', timePreferenceText(), 'precise_time');
        }
        const outboundSetOff = document.querySelector('input[name="outbound_set_off"]')?.value || '';
        const returnSetOff = document.querySelector('input[name="return_set_off"]')?.value || '';
        if (outboundSetOff || returnSetOff) {
          const reserve = meetingReserveParts();
          summaryLine(
            '行程可行性',
            `去程动身${outboundSetOff || '未填'}${isRoundTrip ? ` | 返程动身${returnSetOff || '未填'}` : ''} | 车程${reserve.transport}分钟 | 路途冗余${marginModeText(reserve.marginMode)}`,
            'precise_time'
          );
        }
        if (!meetingHandoffActive() && checkedValue('time_preference') === 'custom') {
          if (isRoundTrip) {
            summaryLine('去程起飞时段', slotSummary('outbound_departure_slots', labels.departureSlots), 'precise_time');
            summaryLine('返程起飞时段', slotSummary('return_departure_slots', labels.departureSlots), 'precise_time');
          } else {
            summaryLine('起飞时段', slotSummary('departure_slots', labels.departureSlots), 'precise_time');
          }
        }
        if (checkedValue('transfer_policy') !== 'direct_only') {
          summaryLine('最长总行程', selectedLabel('short_transfer_limit'), 'precise_rules');
        }
        if (checkedValue('transfer_policy') === 'price_first') {
          summaryLine('过夜中转', selectedLabel('accept_overnight_transfer'), 'precise_rules');
          summaryLine('非联程', selectedLabel('accept_self_transfer'), 'precise_rules');
        }
      }
      const secondary = selectedCheckboxLabels('secondary_goals');
      summaryLine('提醒', secondary.length ? secondary.join('、') : selectedLabel('primary_goal'), 'primary_goal');
      const methodText = selectedLabel('notification_method');
      const emailText = notificationEmailInput && notificationEmailInput.value.trim()
        ? ` ${notificationEmailInput.value.trim()}`
        : '';
      summaryLine('提醒方式', methodText ? `${methodText}${emailText}` : '', 'notification');
      summaryLine('提醒频率', selectedLabel('notification_frequency'), 'notification_frequency');
      addPreciseRulesForSummary();
      addSummaryHeader(
        checkedValue('monitor_mode') === 'precise'
          ? '【系统默认规则】'
          : '【系统默认规则】（快速模式自动套用）'
      );
      systemDefaultRulesForSummary().forEach(rule => addSummaryLine(rule, 'summary-default-rule'));
      if (checkedValue('monitor_mode') !== 'precise') {
        addSummaryLine('你可以展开“精准监控”修改这些默认规则');
      }
      addSummaryLine('未填写的偏好将按普通出行默认处理');
      return;
    }

    function collectPreferenceTemplate() {
      const precise = checkedValue('monitor_mode') === 'precise';
      const quickPassengerCount = Number(document.querySelector('input[name="passenger_count"]')?.value || 1);
      const precisePassengers = {
        adult: Number(document.querySelector('input[name="adult_count"]')?.value || 0),
        child: Number(document.querySelector('input[name="child_count"]')?.value || 0),
        elderly: Number(document.querySelector('input[name="elderly_count"]')?.value || 0),
        infant: Number(document.querySelector('input[name="infant_count"]')?.value || 0)
      };
      return {
        monitor_mode: checkedValue('monitor_mode'),
        route_type: checkedValue('route_type'),
        same_day_round_trip: Boolean(document.querySelector('input[name="same_day_round_trip"]')?.checked),
        business_start: document.querySelector('input[name="business_start"]')?.value || '',
        business_end: document.querySelector('input[name="business_end"]')?.value || '',
        outbound_set_off: precise ? (document.querySelector('input[name="outbound_set_off"]')?.value || '') : '',
        return_set_off: precise ? (document.querySelector('input[name="return_set_off"]')?.value || '') : '',
        user_transport_min: precise ? (document.querySelector('input[name="user_transport_min"]')?.value || '') : '',
        transport_margin_mode: precise ? checkedValue('transport_margin_mode') : 'standard',
        redundancy_min: precise ? checkedValue('redundancy_min') : '25',
        budget_strategy: checkedValue('price_strategy'),
        transfer_policy: checkedValue('transfer_policy'),
        baggage: checkedValue('baggage'),
        time_preference: precise ? checkedValue('time_preference') : 'no_redeye',
        refund_flexibility: precise ? checkedValue('refund_flexibility') : 'preferred',
        price_sensitivity: precise ? checkedValue('price_sensitivity') : 'low',
        trip_type: deriveTripTypeFromScenarios(selectedTravelScenarios()),
        trip_nature: precise ? (checkedValues('trip_natures')[0] || checkedValue('trip_nature')) : '',
        trip_natures: precise ? checkedValues('trip_natures') : [],
        meeting_start: precise ? (document.querySelector('input[name="meeting_start"]')?.value || '') : '',
        meeting_end: precise ? (document.querySelector('input[name="meeting_end"]')?.value || '') : '',
        team_date_flexibility: precise ? checkedValue('team_date_flexibility') : 'fixed',
        same_flight_required: precise ? checkedValue('same_flight_required') : 'false',
        team_passenger_count: precise ? Number(document.querySelector('input[name="team_passenger_count"]')?.value || 0) : 0,
        cabin_arrangement: precise ? checkedValue('cabin_arrangement') : 'economy_all',
        cabin_policy: precise ? checkedValue('cabin_policy') : 'economy_only',
        user_level: precise ? checkedValue('user_level') : 'staff',
        business_seats: precise ? Number(document.querySelector('input[name="business_seats"]')?.value || 0) : 0,
        economy_seats: precise ? Number(document.querySelector('input[name="economy_seats"]')?.value || 0) : quickPassengerCount,
        reimburse_per_person: precise ? Number(document.querySelector('input[name="reimburse_per_person"]')?.value || 0) : 0,
        invoice_needed: precise && Boolean(document.querySelector('input[name="invoice_needed"]')?.checked),
        invoice_special_vat: precise && Boolean(document.querySelector('input[name="invoice_special_vat"]')?.checked),
        invoice_cabin_limit: precise && Boolean(document.querySelector('input[name="invoice_cabin_limit"]')?.checked),
        travel_scenario: selectedTravelScenarios()[0],
        travel_scenarios: selectedTravelScenarios(),
        travel_purposes: selectedTravelScenarios(),
        passenger_count: precise
          ? Object.values(precisePassengers).reduce((sum, value) => sum + value, 0) || quickPassengerCount
          : quickPassengerCount,
        passengers: precise
          ? precisePassengers
          : {adult: quickPassengerCount, child: 0, elderly: 0, infant: 0},
        companions: precise ? checkedValue('companions') : 'solo',
        companion_constraints: precise ? checkedValues('companion_constraints') : [],
        solo_travel: precise && Boolean(document.querySelector('input[name="solo_travel"]')?.checked),
        no_late_arrival: precise && Boolean(document.querySelector('input[name="no_late_arrival"]')?.checked),
        prefer_daytime_arrival: precise && Boolean(document.querySelector('input[name="prefer_daytime_arrival"]')?.checked),
        airline_policy: precise ? checkedValue('airline_policy') : 'any',
        notification_method: checkedValue('notification_method'),
        notification_frequency: checkedValue('notification_frequency'),
        secondary_goals: precise ? checkedValues('secondary_goals') : [],
        short_transfer_limit: precise ? checkedValue('short_transfer_limit') : 'extra_6',
        accept_overnight_transfer: precise ? checkedValue('accept_overnight_transfer') : 'false',
        accept_self_transfer: precise ? checkedValue('accept_self_transfer') : 'false'
      };
    }

    function applyLocationFromTemplate(data) {
      if (data.origin) {
        if (originSelect.querySelector(`option[value="${data.origin}"]`)) {
          originSelect.value = data.origin;
          originManual.value = '';
        } else {
          originSelect.value = 'OTHER';
          originManual.value = data.origin;
        }
        originSelect.dispatchEvent(new Event('change', {bubbles: true}));
        originManual.dispatchEvent(new Event('input', {bubbles: true}));
        updateOriginAirportHint(data.origin_airports_active || data.origin_airports);
      }
      if (data.destination) {
        destinationInput.value = data.destination;
        destinationInput.dispatchEvent(new Event('input', {bubbles: true}));
        destinationInput.dispatchEvent(new Event('change', {bubbles: true}));
        updateDestinationAirportHint(data.destination_airports_active || data.destination_airports || data.dest_airports);
      }
    }

    function applyPreferenceTemplate(data) {
      if (!data) return;
      applyLocationFromTemplate(data);
      [
        'monitor_mode',
        'price_strategy',
        'transfer_policy',
        'baggage',
        'time_preference',
        'refund_flexibility',
        'price_sensitivity',
        'travel_scenario',
        'companions',
        'airline_policy',
        'notification_method',
        'notification_frequency',
        'short_transfer_limit',
        'accept_overnight_transfer',
        'accept_self_transfer'
      ].forEach(name => {
        if (data[name]) setRadio(name, name === 'time_preference' && data[name] === 'any' ? 'unlimited' : data[name]);
      });
      if (data.budget_strategy) {
        setRadio('price_strategy', data.budget_strategy);
      }
      if (data.notification_frequency) {
        setRadio('notification_frequency', data.notification_frequency);
        syncNotificationFrequencyShadow();
      }
      if (sameDayRoundTrip) {
        sameDayRoundTrip.checked = Boolean(data.same_day_round_trip);
      }
      const businessStart = document.querySelector('input[name="business_start"]');
      const businessEnd = document.querySelector('input[name="business_end"]');
      if (businessStart && data.business_start) businessStart.value = data.business_start;
      if (businessEnd && data.business_end) businessEnd.value = data.business_end;
      const userTransportInput = document.querySelector('input[name="user_transport_min"]');
      if (userTransportInput && data.user_transport_min !== undefined) userTransportInput.value = data.user_transport_min || '';
      const outboundSetOffInput = document.querySelector('input[name="outbound_set_off"]');
      const returnSetOffInput = document.querySelector('input[name="return_set_off"]');
      if (outboundSetOffInput && data.outbound_set_off !== undefined) outboundSetOffInput.value = data.outbound_set_off || '';
      if (returnSetOffInput && data.return_set_off !== undefined) returnSetOffInput.value = data.return_set_off || '';
      if (data.transport_margin_mode) setRadio('transport_margin_mode', String(data.transport_margin_mode));
      if (data.redundancy_min) setRadio('redundancy_min', String(data.redundancy_min));
      const templateTripNatures = data.trip_natures || (data.trip_nature ? [data.trip_nature] : []);
      document.querySelectorAll('input[name="trip_natures"]').forEach(input => {
        input.checked = templateTripNatures.includes(input.value);
      });
      if (data.meeting_start) document.querySelector('input[name="meeting_start"]').value = data.meeting_start;
      if (data.meeting_end) document.querySelector('input[name="meeting_end"]').value = data.meeting_end;
      if (data.team_date_flexibility) setRadio('team_date_flexibility', data.team_date_flexibility);
      if (data.same_flight_required !== undefined) setRadio('same_flight_required', String(data.same_flight_required));
      if (data.team_passenger_count !== undefined) document.querySelector('input[name="team_passenger_count"]').value = data.team_passenger_count || '';
      if (data.cabin_arrangement) setRadio('cabin_arrangement', data.cabin_arrangement);
      if (data.route_type) {
        routeTypeTouched = true;
        setRadio('route_type', data.route_type);
      }
      if (data.cabin_policy) setRadio('cabin_policy', data.cabin_policy);
      if (data.user_level) setRadio('user_level', data.user_level);
      const businessSeatsInput = document.querySelector('input[name="business_seats"]');
      const economySeatsInput = document.querySelector('input[name="economy_seats"]');
      const reimburseInput = document.querySelector('input[name="reimburse_per_person"]');
      if (businessSeatsInput && data.business_seats !== undefined) businessSeatsInput.value = data.business_seats;
      if (economySeatsInput && data.economy_seats !== undefined) economySeatsInput.value = data.economy_seats;
      if (reimburseInput && data.reimburse_per_person !== undefined) reimburseInput.value = data.reimburse_per_person || '';
      ['invoice_needed', 'invoice_special_vat', 'invoice_cabin_limit'].forEach(name => {
        const input = document.querySelector(`input[name="${name}"]`);
        if (input && data[name] !== undefined) input.checked = Boolean(data[name]);
      });
      document.querySelectorAll('input[name="companion_constraints"]').forEach(input => {
        input.checked = (data.companion_constraints || []).includes(input.value);
      });
      const soloTravelInput = document.querySelector('input[name="solo_travel"]');
      const noLateArrivalInput = document.querySelector('input[name="no_late_arrival"]');
      const preferDaytimeArrivalInput = document.querySelector('input[name="prefer_daytime_arrival"]');
      if (soloTravelInput) soloTravelInput.checked = Boolean(data.solo_travel);
      if (noLateArrivalInput) noLateArrivalInput.checked = Boolean(data.no_late_arrival);
      if (preferDaytimeArrivalInput) preferDaytimeArrivalInput.checked = Boolean(data.prefer_daytime_arrival);
      secondaryGoalChecks.forEach(check => {
        check.checked = (data.secondary_goals || []).includes(check.value);
      });
      applyMonitorMode();
      toggleBudgetRequired();
      toggleNotificationMethod();
      syncSameDayRoundTrip();
      toggleReturnDate();
      updateRequiredProgress();
      syncPrefCards();
    }

    function setupSavedTemplatePrompt() {
      try {
        const raw = localStorage.getItem('flightMonitorPreferenceTemplate');
        if (!raw || !savedTemplateBanner) return;
        const data = JSON.parse(raw);
        const summaryParts = [
          timePreferenceTextFromValue(data.time_preference),
          data.baggage === 'required' ? '必须托运行李' : '',
          labels.frequency[data.notification_frequency] || ''
        ].filter(Boolean);
        if (savedTemplateSummary) {
          savedTemplateSummary.textContent = summaryParts.length
            ? `已保存默认偏好：${summaryParts.join(' | ')}`
            : '已保存默认偏好';
        }
        savedTemplateBanner.style.display = 'block';
        applyTemplateButton?.addEventListener('click', () => {
          applyPreferenceTemplate(data);
          savedTemplateBanner.style.display = 'none';
        });
        ignoreTemplateButton?.addEventListener('click', () => {
          savedTemplateBanner.style.display = 'none';
        });
        clearTemplateButton?.addEventListener('click', () => {
          localStorage.removeItem('flightMonitorPreferenceTemplate');
          savedTemplateBanner.style.display = 'none';
        });
      } catch (err) {
        console.warn('读取偏好模板失败', err);
      }
    }

    stepPrev.addEventListener('click', () => {
      goToStep(currentStep - 1);
    });

    stepNext.addEventListener('click', () => {
      if (!validateCurrentStep()) {
        return;
      }
      goToStep(currentStep + 1);
    });
    mobileNextButton?.addEventListener('click', () => {
      if (!validateCurrentStep()) {
        return;
      }
      goToStep(currentStep + 1);
    });
    mobileSummaryButton?.addEventListener('click', () => {
      buildSummary();
      summaryCard.style.display = 'block';
      summaryCard.scrollIntoView({behavior: 'smooth', block: 'start'});
    });
    mobileSubmitButton?.addEventListener('click', () => {
      if (summaryCard.style.display !== 'block') {
        buildSummary();
        summaryCard.style.display = 'block';
      }
      form.requestSubmit();
    });

    window.addEventListener('resize', updateStepper);

    advancedToggle.addEventListener('click', () => {
      const expanded = advanced.classList.contains('open');
      setSmartPanel(advanced, !expanded);
      advancedToggle.textContent = expanded ? '＋ 补充偏好，让推荐更准确' : '－ 收起补充偏好';
    });

    rulesToggle.addEventListener('click', () => {
      const expanded = advancedRules.classList.contains('open');
      setSmartPanel(advancedRules, !expanded);
      rulesToggle.textContent = expanded ? '＋ 更细的筛选规则' : '－ 收起筛选规则';
    });

    previewButton.addEventListener('click', () => {
      toggleBudgetRequired();
      toggleReturnDate();
      const missing = updateRequiredProgress();
      if (missing.length) {
        alert(`请先填写${humanJoin(missing)}`);
        return;
      }
      if (!validatePriceInputs()) {
        alert('理想入手价应低于最高可接受价，请确认是否填反了');
        targetPriceInput.focus();
        return;
      }
      if (!validateCabinArrangement(true)) {
        return;
      }
      if (!form.reportValidity()) {
        return;
      }
      buildSummary();
      summaryCard.style.display = 'block';
      summaryCard.scrollIntoView({behavior: 'smooth', block: 'start'});
    });

    editButton.addEventListener('click', () => {
      summaryCard.style.display = 'none';
      if (isMobileStepper()) {
        goToStep(1);
        return;
      }
      window.scrollTo({top: 0, behavior: 'smooth'});
    });
    summaryList.addEventListener('click', event => {
      const button = event.target.closest('[data-summary-target]');
      if (!button) return;
      editSummaryTarget(button.dataset.summaryTarget);
    });

    tripRadios.forEach(radio => radio.addEventListener('change', () => {
      if (checkedValue('round_trip') !== 'true' && sameDayRoundTrip) {
        sameDayRoundTrip.checked = false;
      }
      updateMeetingTimeHandoff();
      toggleReturnDate();
      updateConditionalFields();
      updateStepper();
      updateRequiredProgress();
    }));
    sameDayRoundTrip?.addEventListener('change', () => {
      if (sameDayRoundTrip.checked && checkedValue('monitor_mode') === 'precise' && hasManualTimeSettings()) {
        const confirmed = window.confirm('会议模式将接管时间设置,你已填的时间窗口将不生效,确认启用?');
        if (!confirmed) {
          sameDayRoundTrip.checked = false;
        }
      }
      syncSameDayRoundTrip();
      updateRequiredProgress();
    });
    form.depart_date?.addEventListener('change', () => {
      syncSameDayRoundTrip();
      validateDateFields();
      refreshPriceHint();
    });
    returnDate?.addEventListener('change', validateDateFields);
    budgetStrategyRadios.forEach(radio => radio.addEventListener('change', toggleBudgetRequired));
    maxBudgetRadios.forEach(radio => radio.addEventListener('change', toggleBudgetRequired));
    targetPriceRadios.forEach(radio => radio.addEventListener('change', toggleBudgetRequired));
    maxBudgetInput.addEventListener('input', () => { validatePriceInputs(); updateRequiredProgress(); });
    targetPriceInput.addEventListener('input', () => { validatePriceInputs(); updateRequiredProgress(); });
    originSelect.addEventListener('change', () => { updateOriginAirportHint(); validateLocationField('origin'); updateRequiredProgress(); refreshPriceHint(); });
    originManual.addEventListener('input', () => { updateOriginAirportHint(); setFieldError(originManual, originValidationError, ''); updateRequiredProgress(); refreshPriceHint(); });
    originManual.addEventListener('blur', () => validateLocationField('origin'));
    destinationInput.addEventListener('input', () => { updateDestinationAirportHint(); setFieldError(destinationInput, destinationValidationError, ''); updateRequiredProgress(); refreshPriceHint(); });
    destinationInput.addEventListener('blur', () => validateLocationField('destination'));
    destinationInput.addEventListener('change', () => { updateDestinationAirportHint(); validateLocationField('destination'); updateRequiredProgress(); refreshPriceHint(); });
    modeRadios.forEach(radio => radio.addEventListener('change', applyMonitorMode));
    travelScenarioRadios.forEach(radio => radio.addEventListener('change', applyTravelScenarioDefaults));
    timePreferenceRadios.forEach(radio => radio.addEventListener('change', toggleTimePreference));
    preciseTimeToggle?.addEventListener('click', () => {
      if (!preciseTimeOptions) return;
      preciseTimeOptions.style.display = preciseTimeOptions.style.display === 'block' ? 'none' : 'block';
      syncPreciseTimeDisabled();
      refreshSummaryIfFinalStep();
    });
    notificationMethodRadios.forEach(radio => radio.addEventListener('change', () => {
      toggleNotificationMethod();
      refreshSummaryIfFinalStep();
    }));
    notificationEmailInput?.addEventListener('input', () => {
      validateEmailField(false);
      refreshSummaryIfFinalStep();
    });
    notificationFrequencyRuleRadios.forEach(radio => radio.addEventListener('change', () => {
      syncNotificationFrequencyFromRule();
      refreshSummaryIfFinalStep();
    }));
    notificationFrequencyRadios.forEach(radio => radio.addEventListener('change', () => {
      syncNotificationFrequencyToRule();
      refreshSummaryIfFinalStep();
    }));
    prefCardButtons.forEach(button => {
      button.addEventListener('click', () => togglePrefDetail(button.dataset.prefTarget));
    });
    resetModuleButtons.forEach(button => {
      button.addEventListener('click', () => resetModule(button.dataset.resetModule));
    });
    routeTypeRadios.forEach(radio => radio.addEventListener('change', () => {
      routeTypeTouched = true;
      if (checkedValue('route_type') !== 'domestic' && sameDayRoundTrip) {
        sameDayRoundTrip.checked = false;
      }
      updateMeetingTimeHandoff();
      updateConditionalFields();
      refreshSummaryIfFinalStep();
    }));
    ['companions', 'adult_count', 'child_count', 'elderly_count', 'infant_count', 'passenger_count', 'trip_natures', 'team_passenger_count', 'cabin_arrangement', 'business_start', 'business_end', 'outbound_set_off', 'return_set_off', 'user_transport_min', 'transport_margin_mode', 'redundancy_min', 'meeting_start', 'meeting_end', 'team_date_flexibility', 'same_flight_required', 'cabin_policy', 'user_level', 'business_seats', 'economy_seats', 'reimburse_per_person', 'invoice_context', 'invoice_needed', 'invoice_special_vat', 'invoice_cabin_limit', 'time_preference', 'refund_flexibility', 'airline_policy', 'accept_self_transfer', 'companion_constraints', 'solo_travel', 'no_late_arrival', 'prefer_daytime_arrival'].forEach(name => {
      document.querySelectorAll(`input[name="${name}"]`).forEach(input => {
        input.addEventListener('change', () => {
          syncPrefCards();
          validateCabinArrangement(false);
          updateMeetingTimeHandoff();
          refreshSummaryIfFinalStep();
        });
        input.addEventListener('input', () => {
          syncPrefCards();
          validateCabinArrangement(false);
          updateMeetingTimeHandoff();
          refreshSummaryIfFinalStep();
        });
      });
    });
    document.querySelectorAll('.pref-card-detail input, .pref-card-detail select').forEach(input => {
      input.addEventListener('change', syncPrefCards);
    });
    openPreciseModeButton?.addEventListener('click', () => {
      setRadio('monitor_mode', 'precise');
      applyMonitorMode();
      setSmartPanel(advanced, true);
      setSmartPanel(advancedRules, true);
      advancedToggle.textContent = '－ 收起补充偏好';
      rulesToggle.textContent = '－ 收起筛选规则';
      advanced.scrollIntoView({behavior: 'smooth', block: 'start'});
    });
    transferRadios.forEach(radio => radio.addEventListener('change', () => {
      toggleShortTransferOptions();
      updateConditionalFields();
      updateRequiredProgress();
    }));
    dateFlexRadios.forEach(radio => radio.addEventListener('change', updateDateFlexHint));
    secondaryGoalChecks.forEach(check => check.addEventListener('change', updateDateFlexHint));
    primaryGoalRadios.forEach(radio => radio.addEventListener('change', () => {
      applyDefaultSecondaryGoals();
      if (isMobileStepper() && currentStep === stepTitles.length) {
        buildSummary();
      }
    }));
    companionRadios.forEach(radio => radio.addEventListener('change', applyCompanionDefaults));
    captureModuleDefaults();
    applyEditSubscription(editSubscription);
    syncSameDayRoundTrip();
    toggleReturnDate();
    toggleBudgetRequired();
    toggleNotificationMethod();
    syncNotificationFrequencyShadow();
    toggleShortTransferOptions();
    updateConditionalFields();
    updateDateFlexHint();
    updateOriginAirportHint(editSubscription.origin_airports_active || editSubscription.origin_airports);
    updateDestinationAirportHint(editSubscription.destination_airports_active || editSubscription.destination_airports || editSubscription.dest_airports);
    validateDateFields();
    refreshPriceHint();
    updateRequiredProgress();
    updateStrictRulesWarning();
    advanced.style.display = 'none';
    advancedToggle.textContent = '＋ 补充偏好，让推荐更准确';
    advancedRules.style.display = 'none';
    rulesToggle.textContent = '＋ 更细的筛选规则';
    applyMonitorMode();
    toggleTimePreference();
    updatePrefCards();
    applyDefaultSecondaryGoals();
    setupSavedTemplatePrompt();
    updateStepper();
    form.addEventListener('input', () => {
      updateRequiredProgress();
      updateStrictRulesWarning();
      refreshSummaryIfFinalStep();
    });
    form.addEventListener('change', () => {
      updateConditionalFields();
      updateRequiredProgress();
      updateStrictRulesWarning();
      syncPrefCards();
      refreshSummaryIfFinalStep();
    });
    form.addEventListener('submit', event => {
      const missing = updateRequiredProgress();
      if (missing.length) {
        event.preventDefault();
        alert(`请先填写${humanJoin(missing)}`);
        return;
      }
      if (!validatePriceInputs()) {
        event.preventDefault();
        alert('理想入手价应低于最高可接受价，请确认是否填反了');
        targetPriceInput.focus();
        return;
      }
      if (!validateDateFields() || !validateLocationField('origin') || !validateLocationField('destination')) {
        event.preventDefault();
        return;
      }
      if (!validateCabinArrangement(true)) {
        event.preventDefault();
        return;
      }
      if (!validateEmailField(true)) {
        event.preventDefault();
        return;
      }
      if (summaryCard.style.display !== 'block') {
        event.preventDefault();
        if (!form.reportValidity()) {
          return;
        }
        buildSummary();
        summaryCard.style.display = 'block';
        summaryCard.scrollIntoView({behavior: 'smooth', block: 'start'});
        return;
      }
      if (rememberPreferences && rememberPreferences.checked) {
        try {
          localStorage.setItem(
            'flightMonitorPreferenceTemplate',
            JSON.stringify(collectPreferenceTemplate())
          );
        } catch (err) {
          console.warn('保存偏好模板失败', err);
        }
      }
    });
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
    <p><b>接下来系统会:</b></p>
    <ol>
      <li>立即进行第一次采集和购买判断(约30秒-1分钟)</li>
      <li>结果将推送到: {{ summary.notification_text or "你的邮箱 / PushPlus微信" }}</li>
      <li>之后发现低价、涨价风险或更优方案时自动提醒</li>
      <li>可随时在「我的监控」暂停或修改</li>
    </ol>
    <p>💡 第一次判断稍后到达,你可以关掉此页,留意邮箱/微信推送。</p>
    <p><a href="{{ url_for('subscription_list') }}">查看我的所有监控</a> <a class="secondary-link" href="{{ url_for('index') }}">再创建一个</a></p>
    <p><b>{{ summary.route }}</b></p>
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

    <p><b>订阅已保存。系统正在采集第一批数据，预计1-2分钟内收到首次推送。</b></p>
  </div>
  <a href="{{ url_for('index') }}">继续添加订阅</a>
  <a class="secondary-link" href="{{ url_for('subscription_list') }}">查看我的所有监控 →</a>
  {% if index is not none %}
  <div class="card" style="margin-top:16px;">
    <p><b>想让推荐更准确？你还可以补充（可选）：</b></p>
    <p>这些不影响监控运行，只让推荐排序更贴合你的需求。</p>
    <div class="quick-actions">
      <form method="post" action="{{ url_for('quick_update_subscription', index=index) }}">
        <input type="hidden" name="field" value="time_preference">
        <input type="hidden" name="value" value="no_redeye">
        <button type="submit">不接受红眼</button>
      </form>
      <form method="post" action="{{ url_for('quick_update_subscription', index=index) }}">
        <input type="hidden" name="field" value="airline_policy">
        <input type="hidden" name="value" value="prefer_full_service">
        <button type="submit">偏好全服务航司</button>
      </form>
      <form method="post" action="{{ url_for('quick_update_subscription', index=index) }}">
        <input type="hidden" name="field" value="accept_self_transfer">
        <input type="hidden" name="value" value="false">
        <button type="submit">不接受非联程</button>
      </form>
      <form method="post" action="{{ url_for('quick_update_subscription', index=index) }}">
        <input type="hidden" name="field" value="refund_flexibility">
        <input type="hidden" name="value" value="preferred">
        <button type="submit">最好可改签</button>
      </form>
    </div>
  </div>
  <a class="secondary-link" href="{{ url_for('index', edit=index) }}">修改这条监控的偏好</a>
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
      <a class="button-link" href="{{ url_for('index', edit=item.index) }}">编辑</a>
      <form method="post" action="{{ url_for('toggle_subscription', index=item.index) }}">
        <button type="submit">{{ "暂停" if item.status == "active" else "恢复" }}</button>
      </form>
      <form method="post" action="{{ url_for('delete_subscription', index=item.index) }}" onsubmit="return confirm('确认删除这条监控?');">
        <button class="danger" type="submit">删除</button>
      </form>
    </div>
  </div>
  {% endfor %}
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


def load_subscriptions() -> list[dict]:
    if not SUBSCRIPTIONS_PATH.exists():
        return []
    try:
        data = json.loads(SUBSCRIPTIONS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def save_subscription(subscription: dict, index: int | None = None) -> int:
    SUBSCRIPTIONS_PATH.parent.mkdir(exist_ok=True)
    subscriptions = load_subscriptions()
    if index is not None and 0 <= index < len(subscriptions):
        subscriptions[index] = subscription
        saved_index = index
    else:
        subscriptions.append(subscription)
        saved_index = len(subscriptions) - 1
    SUBSCRIPTIONS_PATH.write_text(
        json.dumps(subscriptions, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return saved_index


def save_subscriptions(subscriptions: list[dict]) -> None:
    SUBSCRIPTIONS_PATH.parent.mkdir(exist_ok=True)
    SUBSCRIPTIONS_PATH.write_text(
        json.dumps(subscriptions, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_feedback() -> list[dict]:
    if not FEEDBACK_PATH.exists():
        return []
    try:
        data = json.loads(FEEDBACK_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def save_feedback(record: dict) -> None:
    FEEDBACK_PATH.parent.mkdir(exist_ok=True)
    records = load_feedback()
    records.append(record)
    FEEDBACK_PATH.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_page_results() -> list[dict]:
    if not PAGE_RESULTS_PATH.exists():
        return []
    try:
        data = json.loads(PAGE_RESULTS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _safe_payload_id(subscription_id: str) -> str:
    return sanitize_filename(subscription_id)


def _load_payload_result(subscription_id: str) -> dict | None:
    if not subscription_id:
        return None
    path = PAGE_PAYLOADS_DIR / f"{_safe_payload_id(subscription_id)}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _detail_storage_keys(results: list[dict]) -> list[str]:
    keys = {str(item.get("subscription_id", "")) for item in results if item.get("subscription_id")}
    if PAGE_PAYLOADS_DIR.exists():
        keys.update(path.stem for path in PAGE_PAYLOADS_DIR.glob("*.json"))
    return sorted(keys)


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


def _relative_time_label(value: str) -> str:
    if not value:
        return ""
    try:
        created = datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return str(value)
    minutes = max(0, int((datetime.now() - created).total_seconds() // 60))
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return f"{minutes}分钟前"
    if minutes < 24 * 60:
        return f"{minutes // 60}小时前"
    return f"{minutes // (24 * 60)}天前"


def _subscription_last_decision(sub: dict, index: int) -> str:
    subscription_id = str(sub.get("id") or index)
    record = _load_payload_result(subscription_id) or {}
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
    if not isinstance(payload, dict) or not payload:
        return "暂无"
    decision = payload.get("execution_advice") or payload.get("recommendation") or payload.get("push_type") or "已生成"
    price = payload.get("current_price") or payload.get("display_price") or payload.get("price")
    price_text = f"(¥{int(price):,})" if isinstance(price, (int, float)) and price else ""
    time_text = _relative_time_label(str(record.get("created_at") or payload.get("created_at") or ""))
    return f"{decision}{price_text}" + (f" · {time_text}" if time_text else "")


def build_subscription_list_items(subscriptions: list[dict]) -> list[dict]:
    items = []
    for index, sub in enumerate(subscriptions):
        route_type = _sub_value(sub, "route_type", "domestic")
        round_trip = bool(sub.get("round_trip") or _sub_value(sub, "trip_type") == "round_trip")
        subscription_id = str(sub.get("id") or index)
        items.append(
            {
                "index": index,
                "route": _subscription_route_text(sub),
                "route_type": route_type,
                "route_type_label": ROUTE_TYPE_LABELS.get(route_type, route_type),
                "trip": "往返" if round_trip else "单程",
                "dates": _subscription_dates_text(sub),
                "status": sub.get("status", "active"),
                "last_decision": _subscription_last_decision(sub, index),
                "scenario": _subscription_scenario_text(sub),
                "detail_url": url_for("detail", sub=subscription_id),
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


def update_subscription_preference(index: int, field: str, value: str) -> bool:
    subscriptions = load_subscriptions()
    if not (0 <= index < len(subscriptions)):
        return False

    subscription = subscriptions[index]
    hard = subscription.setdefault("hard_constraints", {})
    soft = subscription.setdefault("soft_preferences", {})

    if field == "time_preference":
        value = normalize_time_preference_mode(value)
        hard["time_preference"] = value
        hard["time_preference_mode"] = value
        soft["time_preference"] = value
        soft["time_preference_mode"] = value
        if value == "no_redeye":
            hard["departure_time_policy"] = "no_redeye"
            hard["arrival_time_policy"] = "no_midnight"
            soft["departure_time_windows"] = [["06:00", "23:00"]]
            soft["arrival_time_windows"] = [["06:00", "23:00"]]
            soft["red_eye_allowed"] = False
            soft["early_morning_allowed"] = True
        elif value == "daytime":
            hard["departure_time_policy"] = "daytime"
            hard["arrival_time_policy"] = "daytime_only"
            soft["departure_time_windows"] = [["06:00", "20:00"]]
            soft["arrival_time_windows"] = [["06:00", "20:00"]]
            soft["red_eye_allowed"] = False
            soft["early_morning_allowed"] = True
        elif value == "unlimited":
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
    elif field == "refund_flexibility":
        soft["refund_flexibility"] = value
    else:
        return False

    save_subscription(subscription, index)
    return True


def run_single_subscription(subscription: dict) -> None:
    """Run one subscription collection in a background thread."""
    print("[后台] 线程已启动")
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
        ok = process_subscription(normalized_subscription, ensure_db=True)
        print(f"[后台] 采集分析推送结束: ok={ok}")
    except Exception as exc:
        print(f"[后台] 执行失败: {exc}")
        traceback.print_exc()


def start_background_collection(subscription: dict) -> None:
    """Start collection without blocking the form response."""
    try:
        print("[后台] 准备启动采集线程")
        thread = threading.Thread(
            target=run_single_subscription,
            args=(subscription,),
            daemon=True,
        )
        thread.start()
        print(f"[后台] 采集线程已启动: {thread.name}")
    except Exception as exc:
        print(f"[后台] 启动线程失败: {exc}")
        traceback.print_exc()


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
        raise ValueError(
            f"无法识别地点 {origin_info.get('value')},请输入机场三字码或已支持的城市"
        )
    if destination_info.get("type") == "unknown":
        raise ValueError(
            f"无法识别地点 {destination_info.get('value')},请输入机场三字码或已支持的城市"
        )
    origin_airports_active = parse_active_airports(
        form.get("origin_airports_active"), origin_info["airports"]
    )
    destination_airports_active = parse_active_airports(
        form.get("destination_airports_active"), destination_info["airports"]
    )
    route_type = form.get("route_type") or infer_route_type(
        origin_airports_active,
        destination_airports_active,
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
    business_start = form.get("business_start", "").strip()
    business_end = form.get("business_end", "").strip()
    outbound_set_off = form.get("outbound_set_off", "").strip()
    return_set_off = form.get("return_set_off", "").strip()
    user_transport_min = parse_int(form.get("user_transport_min"), 0)
    transport_margin_mode = form.get("transport_margin_mode", "standard").strip() or "standard"
    if transport_margin_mode not in {"tight", "standard", "loose"}:
        transport_margin_mode = "standard"
    redundancy_min = parse_int(form.get("redundancy_min"), 25)
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
        outbound_set_off = ""
        return_set_off = ""
        user_transport_min = 0
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
    companion_constraints = form.getlist("companion_constraints")
    solo_travel = parse_bool(form.get("solo_travel", "false"))
    no_late_arrival = parse_bool(form.get("no_late_arrival", "false"))
    prefer_daytime_arrival = parse_bool(form.get("prefer_daytime_arrival", "false"))
    invoice_context = parse_bool(form.get("invoice_context", "false"))
    invoice_needed = parse_bool(form.get("invoice_needed", "false"))
    invoice_special_vat = parse_bool(form.get("invoice_special_vat", "false"))
    invoice_cabin_limit = parse_bool(form.get("invoice_cabin_limit", "false"))
    raw_trip_natures = form.getlist("trip_natures") if hasattr(form, "getlist") else []
    refund_flexibility = form.get("refund_flexibility", "preferred")
    price_sensitivity = form.get("price_sensitivity", "low")
    if monitor_mode != "precise":
        companion_constraints = []
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
    if cabin_arrangement == "business_all" and passenger_count:
        business_seats = passenger_count
        economy_seats = 0
        cabin_policy = "business_allowed"
    elif cabin_arrangement == "economy_all" and passenger_count:
        business_seats = 0
        economy_seats = passenger_count
    elif cabin_arrangement == "mixed" and business_seats + economy_seats > 0:
        passenger_count = business_seats + economy_seats
    team_date_flexibility = form.get("team_date_flexibility", "fixed").strip() or "fixed"
    same_flight_required = parse_bool(form.get("same_flight_required", "false"))
    reimburse_per_person = parse_int(form.get("reimburse_per_person"), 0)
    if monitor_mode != "precise" or not business_context:
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
    use_hourly_time = monitor_mode == "precise" and time_mode == "custom"

    return {
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
            "date_flexibility_days": parse_int(form.get("date_flexibility"), 0),
            "transfer_policy": transfer_policy,
            "checked_baggage_required": form.get("baggage", "required") == "required",
            "same_day_round_trip": same_day_round_trip,
            "business_start": business_start if same_day_round_trip else "",
            "business_end": business_end if same_day_round_trip else "",
            "outbound_set_off": outbound_set_off,
            "return_set_off": return_set_off if round_trip else "",
            "user_transport_min": user_transport_min if user_transport_min > 0 else None,
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
        "same_day_round_trip": same_day_round_trip,
        "passenger_count": passenger_count,
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
            "transfer_policy": transfer_policy,
            "route_type": route_type,
            "same_day_round_trip": same_day_round_trip,
            "business_start": business_start if same_day_round_trip else "",
            "business_end": business_end if same_day_round_trip else "",
            "outbound_set_off": outbound_set_off,
            "return_set_off": return_set_off if round_trip else "",
            "user_transport_min": user_transport_min if user_transport_min > 0 else None,
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
            "reimburse_per_person": reimburse_per_person or None,
            "max_extra_duration_hours": max_extra_duration_hours,
            "max_total_duration_hours": max_total_duration_hours,
            "departure_time_policy": form.get("departure_time_policy", "no_redeye"),
            "arrival_time_policy": form.get("arrival_time_policy", "any"),
            "time_preference": time_mode,
            **time_constraints,
            "baggage": form.get("baggage", "required"),
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
        },
        "notification_goals": {
            "primary": primary_goal,
            "secondary": secondary_goals,
            "method": form.get("notification_method", "pushplus"),
            "email": form.get("notification_email", "").strip(),
            "frequency": notification_frequency,
        },
        "status": "active",
        "created_at": datetime.now().isoformat(),
    }


def build_success_summary(subscription: dict) -> dict:
    subscription_with_defaults = apply_default_rules(subscription)
    hard = subscription.get("hard_constraints", {})
    notification_goals = subscription.get("notification_goals", {}) or {}
    method = notification_goals.get("method", "pushplus")
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

    return {
        "route": f"{city_label(subscription.get('origin'))} → {city_label(subscription.get('destination'))}",
        "airport_coverage": coverage,
        "defaults_applied": subscription_with_defaults.get("defaults_applied", []),
        "reminders": reminders,
        "exclusions": exclusions,
        "notification_text": notification_labels.get(method, "你的邮箱 / PushPlus微信"),
    }


@app.get("/")
def index():
    edit_subscription = None
    edit_index = None
    edit_arg = request.args.get("edit")
    if edit_arg not in (None, ""):
        try:
            candidate_index = int(edit_arg)
            subscriptions = load_subscriptions()
            if 0 <= candidate_index < len(subscriptions):
                edit_subscription = {**subscriptions[candidate_index], "_index": candidate_index}
                edit_index = candidate_index
        except ValueError:
            edit_subscription = None
            edit_index = None
    return render_template_string(
        FORM_TEMPLATE,
        origins=COMMON_ORIGINS,
        city_airports=CITY_AIRPORTS,
        city_aliases=CITY_ALIASES,
        airport_short_names=AIRPORT_SHORT_NAMES,
        edit_subscription=edit_subscription or {},
        edit_index=edit_index,
        form_error="",
    )


@app.get("/price_hint")
def price_hint():
    origin = request.args.get("origin", "").strip().upper()
    dest = request.args.get("dest", "").strip().upper()
    if not origin or not dest:
        return jsonify({"has_data": False, "scope": "oneway"})
    for route in (f"{origin}-{dest}", f"{origin}_{dest}", f"{origin}→{dest}"):
        hint = build_price_hint_from_calendar(load_calendar(route))
        if hint.get("has_data"):
            hint["route"] = route
            return jsonify(hint)
    return jsonify({"has_data": False, "scope": "oneway"})


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
        raw_index = request.form.get("subscription_index")
        edit_index = int(raw_index) if str(raw_index).strip().isdigit() else None
        index = save_subscription(subscription, edit_index)
        print(f"[表单] 订阅保存完成: index={index}")

        print("[表单] 开始触发后台采集")
        start_background_collection(subscription)
        print("[表单] 后台采集触发完成")

        return redirect(url_for("success", index=index))
    except ValueError as exc:
        print(f"[表单] 提交订阅失败: {exc}")
        return render_template_string(
            FORM_TEMPLATE,
            origins=COMMON_ORIGINS,
            city_airports=CITY_AIRPORTS,
            city_aliases=CITY_ALIASES,
            airport_short_names=AIRPORT_SHORT_NAMES,
            edit_subscription={},
            edit_index=None,
            form_error=str(exc),
        ), 400
    except Exception as exc:
        print(f"[表单] 提交订阅失败: {exc}")
        traceback.print_exc()
        raise


@app.get("/subscriptions")
def subscription_list():
    subscriptions = load_subscriptions()
    return render_template_string(
        LIST_TEMPLATE,
        items=build_subscription_list_items(subscriptions),
    )


@app.post("/subscriptions/<int:index>/toggle")
def toggle_subscription(index: int):
    subscriptions = load_subscriptions()
    if 0 <= index < len(subscriptions):
        current = subscriptions[index].get("status", "active")
        subscriptions[index]["status"] = "paused" if current == "active" else "active"
        save_subscriptions(subscriptions)
    return redirect(url_for("subscription_list"))


@app.post("/subscriptions/<int:index>/delete")
def delete_subscription(index: int):
    subscriptions = load_subscriptions()
    if 0 <= index < len(subscriptions):
        subscriptions.pop(index)
        save_subscriptions(subscriptions)
    return redirect(url_for("subscription_list"))


@app.post("/subscriptions/<int:index>/quick-update")
def quick_update_subscription(index: int):
    field = request.form.get("field", "")
    value = request.form.get("value", "")
    ok = update_subscription_preference(index, field, value)
    print(f"[表单] 快捷更新偏好: index={index}, field={field}, ok={ok}")
    return redirect(url_for("success", index=index))


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
        summary=build_success_summary(subscription) if subscription else {},
        first_push_time=first_push_text(),
        index=index if subscriptions else None,
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
    subscription_id = request.args.get("sub", "")
    results = load_page_results()
    keys = _detail_storage_keys(results)
    print(f"[详情读取] 查找订阅 {subscription_id},存储里现有的key: {keys}")
    matched = _load_payload_result(subscription_id)
    if subscription_id:
        if not matched:
            for item in reversed(results):
                if str(item.get("subscription_id", "")) == str(subscription_id):
                    matched = item
                    break
    elif results:
        matched = results[-1]
    return render_template_string(DETAIL_TEMPLATE, result=matched)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
