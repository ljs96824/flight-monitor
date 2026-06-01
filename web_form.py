"""Flask form for flight monitor subscriptions."""

from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, redirect, render_template_string, request, url_for

from airports import AIRPORT_SHORT_NAMES, CITY_AIRPORTS, format_airport, resolve_location
from analyzer import apply_default_rules


BASE_DIR = Path(__file__).parent
SUBSCRIPTIONS_PATH = BASE_DIR / "data" / "subscriptions.json"
FEEDBACK_PATH = BASE_DIR / "data" / "feedback.json"
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
    "with_elderly": "有老人同行",
    "with_child": "有小孩同行（12岁以下）",
    "with_elderly_child": "老人和小孩都有",
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
    }
    @media (max-width: 520px) {
      .button-row { grid-template-columns: 1fr; }
      .step-nav-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <h1>航班监控订阅</h1>
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
    <div class="mode-toggle">
      <div class="mode-toggle-title">模式</div>
      <div class="choice">
        <label><input type="radio" name="monitor_mode" value="quick" checked> 快速监控</label>
        <label><input type="radio" name="monitor_mode" value="precise"> 精准监控</label>
      </div>
      <p class="hint">快速监控只填写基础信息；精准监控会展开补充偏好和筛选规则。</p>
    </div>
    <div id="required-progress" class="required-progress">已完成 0/10 个基础项</div>

    <fieldset class="form-step active" data-step="1">
      <legend>行程信息</legend>

      <label for="origin">出发地</label>
      <select id="origin" name="origin_select">
        {% for code, label in origins %}
        <option value="{{ code }}">{{ label }}</option>
        {% endfor %}
      </select>
      <input name="origin_manual" placeholder="或手动输入城市名/机场代码，例如上海或PVG">
      <p id="origin-airport-hint" class="hint"></p>
      <div id="origin-airport-tags" class="airport-tags"></div>
      <input id="origin_airports_active" name="origin_airports_active" type="hidden">

      <label for="destination">目的地</label>
      <input id="destination" name="destination" placeholder="输入城市名（如大阪、东京）或机场代码（如KIX）" required>
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

      <div id="return-date-wrap" data-show-if="round_trip=true">
        <label for="return_date">返程日期</label>
        <input id="return_date" name="return_date" type="date">

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
        <label><input type="radio" name="budget_strategy" value="explicit" checked> 我有明确预算</label>
        <label><input type="radio" name="budget_strategy" value="auto_judge"> 不知道合理价，帮我判断</label>
        <label><input type="radio" name="budget_strategy" value="low_price_alert"> 只要进入低价区间就提醒</label>
      </div>

      <div id="budget-amount-fields" data-show-if="budget_strategy=explicit">
      <label>最高可接受价格（超过这个价通常不考虑）</label>
      <input id="max_budget" name="max_budget" type="number" min="1" step="1" placeholder="例如 8000">
      <p class="hint">超过这个价通常不考虑</p>
      <div class="choice">
        <label><input type="radio" name="max_budget_mode" value="fixed" checked> 输入具体金额</label>
        <label><input type="radio" name="max_budget_mode" value="none"> 不确定，帮我判断</label>
      </div>

      <label>理想入手价格（可选，到这个价格就值得买）</label>
      <input id="target_price" name="target_price" type="number" min="1" step="1" placeholder="例如 6000（选填）">
      <p class="hint">到这个价格就值得买（可选）</p>
      <p id="price-validation-error" class="field-error">理想入手价应低于最高可接受价，请确认是否填反了</p>
      <div class="choice">
        <label><input type="radio" name="target_price_mode" value="fixed" checked> 输入具体金额</label>
        <label><input type="radio" name="target_price_mode" value="auto"> 不确定，帮我判断合理价格</label>
        <label><input type="radio" name="target_price_mode" value="low_zone"> 没有明确预算，进入低价区间时提醒我</label>
      </div>
      </div>

      <input type="hidden" name="price_tolerance_mode" value="100">
      <input id="price_tolerance_custom" name="price_tolerance_custom" type="hidden">

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
      <legend>监控目标</legend>
      <label>主目标</label>
      <div class="choice">
        <label><input type="radio" name="primary_goal" value="price_drop_alert" required> 找到合适价格时提醒我 <small style="color:gray">（适合还没急着买，等低价）</small></label>
        <label><input type="radio" name="primary_goal" value="buy_timing" checked required> 判断现在该不该买 <small style="color:gray">（适合已看到价格，想知道能不能下手）</small></label>
        <label><input type="radio" name="primary_goal" value="cheaper_date" required> 帮我找更便宜的日期 <small style="color:gray">（适合日期可以调整）</small></label>
        <label><input type="radio" name="primary_goal" value="best_overall" required> 帮我找最合适航班 <small style="color:gray">（不只看价格，综合时间/行李/中转）</small></label>
      </div>

      <button id="advanced-toggle" class="secondary-button precise-only" type="button">＋ 补充偏好，让推荐更准确</button>
      <div id="advanced-preferences" class="smart-panel precise-only">
      <fieldset>
        <legend>提高推荐准确度</legend>
        <p class="hint">不填也可以，系统会按普通出行默认规则监控</p>

        <div class="pref-cards">
          <div class="pref-card">
            <div class="pref-name">同行人员</div>
            <div class="pref-value" data-pref-value="companions">未设置</div>
            <button class="secondary-button pref-card-button" type="button" data-pref-target="companions">补充</button>
          </div>
          <div class="pref-card">
            <div class="pref-name">时间偏好</div>
            <div class="pref-value" data-pref-value="time">使用默认：避免红眼</div>
            <button class="secondary-button pref-card-button" type="button" data-pref-target="time">修改</button>
          </div>
          <div class="pref-card">
            <div class="pref-name">退改签</div>
            <div class="pref-value" data-pref-value="refund">使用默认：便宜优先，提醒风险</div>
            <button class="secondary-button pref-card-button" type="button" data-pref-target="refund">修改</button>
          </div>
          <div class="pref-card">
            <div class="pref-name">航司偏好</div>
            <div class="pref-value" data-pref-value="airline">使用默认：不限</div>
            <button class="secondary-button pref-card-button" type="button" data-pref-target="airline">修改</button>
          </div>
          <div class="pref-card">
            <div class="pref-name">非联程中转</div>
            <div class="pref-value" data-pref-value="self_transfer">使用默认：不接受</div>
            <button class="secondary-button pref-card-button" type="button" data-pref-target="self_transfer">修改</button>
          </div>
        </div>

        <div id="pref-detail-companions" class="pref-card-detail">
        <label>同行人员</label>
        <div class="choice">
          <label><input type="radio" name="companions" value="solo" checked> 仅本人</label>
          <label><input type="radio" name="companions" value="with_elderly"> 有老人同行</label>
          <label><input type="radio" name="companions" value="with_child"> 有小孩同行（12岁以下）</label>
          <label><input type="radio" name="companions" value="with_elderly_child"> 老人和小孩都有</label>
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
        <button id="precise-time-toggle" class="secondary-button" type="button" style="font-size:13px;padding:8px;margin-top:10px;">需要更精确？按小时设置</button>
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

        <label>出行类型</label>
        <select name="trip_type">
          <option value="business_meeting">商务会议</option>
          <option value="tourism" selected>旅游</option>
          <option value="family_visit">探亲</option>
          <option value="student_return">学生返校</option>
          <option value="family_elder">家庭老人同行</option>
          <option value="other">其他</option>
        </select>
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

          <div id="advanced-frequency-copy" style="display:none">
          <label>提醒频率</label>
          <div class="choice">
            <label><input type="radio" name="notification_frequency_rule" value="important_only" checked> 仅重要变化时提醒（价格显著下降、即将涨价）</label>
            <label><input type="radio" name="notification_frequency_rule" value="daily_digest"> 每天汇总推送一次</label>
            <label><input type="radio" name="notification_frequency_rule" value="price_change"> 每次价格变化都提醒</label>
          </div>
          <div class="sub-options" data-show-if="notification_frequency_rule=price_change">
            <label>什么算价格变化？</label>
            <div class="choice">
              <label><input type="radio" name="price_change_threshold" value="any"> 每次变化</label>
              <label><input type="radio" name="price_change_threshold" value="down_100" checked> 降超100元</label>
              <label><input type="radio" name="price_change_threshold" value="down_300"> 降超300元</label>
              <label><input type="radio" name="price_change_threshold" value="low_zone"> 进入低价区间</label>
            </div>
          </div>
          <div class="sub-options" data-show-if="notification_frequency_rule=daily_digest">
            <label>汇总时间</label>
            <div class="choice">
              <label><input type="radio" name="digest_time" value="09:00" checked> 早9点</label>
              <label><input type="radio" name="digest_time" value="12:00"> 中午12点</label>
              <label><input type="radio" name="digest_time" value="20:00"> 晚8点</label>
            </div>
          </div>
          </div>
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

    <div id="quick-defaults-note" class="default-rules-note">
      系统将使用默认安全偏好：避免红眼、优先含行李、避免高风险中转、仅重要变化提醒。
      <br>
      <button id="open-precise-mode" class="secondary-button" type="button">查看/修改默认偏好</button>
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
      <fieldset>
        <legend>提醒设置</legend>
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
      </fieldset>
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
      price_drop_alert: ["low_price_alert", "price_risk_alert"],
      buy_timing: ["price_risk_alert", "low_price_alert"],
      cheaper_date: ["cheaper_date"],
      best_overall: ["better_same_day"]
    };
    const cityAirports = {{ city_airports|tojson }};
    const airportShortNames = {{ airport_short_names|tojson }};
    const editSubscription = {{ edit_subscription|tojson }};

    const form = document.getElementById('subscription-form');
    const modeRadios = document.querySelectorAll('input[name="monitor_mode"]');
    const budgetStrategyRadios = document.querySelectorAll('input[name="budget_strategy"]');
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
    const destinationInput = document.getElementById('destination');
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
    const dateFlexWarning = document.getElementById('date-flex-warning');
    const cheaperDateCheck = document.querySelector('input[name="secondary_goals"][value="cheaper_date"]');
    const cheaperDateLabel = cheaperDateCheck ? cheaperDateCheck.closest('label') : null;
    const companionRadios = document.querySelectorAll('input[name="companions"]');
    const autoPreferenceNotice = document.getElementById('auto-preference-notice');
    const departurePolicyInput = document.querySelector('input[name="departure_time_policy"]');
    const stepPanels = Array.from(document.querySelectorAll('.form-step'));
    const mobileStepper = document.getElementById('mobile-stepper');
    const stepDots = document.getElementById('step-dots');
    const stepLabel = document.getElementById('step-label');
    const stepPrev = document.getElementById('step-prev');
    const stepNext = document.getElementById('step-next');
    const stepTimePreferences = document.getElementById('step-time-preferences');
    const prefCardButtons = document.querySelectorAll('.pref-card-button');
    const prefCardDetails = document.querySelectorAll('.pref-card-detail');
    const resetModuleButtons = document.querySelectorAll('.reset-module');
    const stepTitles = ['行程信息', '价格底线', '监控目标', '完成'];
    let currentStep = 1;

    if (stepTimePreferences && customTimeOptions) {
      stepTimePreferences.appendChild(customTimeOptions);
    }

    function checkedValue(name) {
      const selected = document.querySelector(`input[name="${name}"]:checked`);
      if (selected) return selected.value;
      const field = document.querySelector(`[name="${name}"]`);
      return field ? field.value : "";
    }

    function updateConditionalFields() {
      document.querySelectorAll('[data-show-if]').forEach(el => {
        const rule = el.dataset.showIf || '';
        const [field, rawValue] = rule.split('=');
        const values = String(rawValue || '').split('|');
        const current = checkedValue(field);
        const shouldShow = field && values.includes(current);
        el.style.display = shouldShow ? 'block' : 'none';
      });
    }

    document.querySelectorAll('input, select').forEach(input => {
      input.addEventListener('change', updateConditionalFields);
    });

    const moduleInputContainers = {
      time: ['pref-detail-time'],
      transfer: ['transfer-rules-module'],
      airline: ['pref-detail-airline'],
      alerts: ['alerts-secondary-options', 'advanced-frequency-copy']
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
      toggleBudgetRequired();
      toggleReturnDate();
      if (!validatePriceInputs()) {
        alert('理想入手价应低于最高可接受价，请确认是否填反了');
        targetPriceInput.focus();
        return false;
      }
      return form.reportValidity();
    }

    function updateRequiredProgress() {
      const missing = [];
      const origin = selectedOrigin();
      const destination = destinationInput.value.trim();
      const isRoundTrip = checkedValue('round_trip') === 'true';
      const strategy = checkedValue('budget_strategy');
      const baseItems = [
        {done: Boolean(origin && origin !== '其他'), label: '出发地'},
        {done: Boolean(destination), label: '目的地'},
        {done: Boolean(checkedValue('round_trip')), label: '单程/往返'},
        {done: Boolean(form.depart_date.value), label: '出发日期'},
        {done: !isRoundTrip || Boolean(returnDate.value), label: '返程日期'},
        {done: Boolean(checkedValue('date_flexibility')), label: '日期弹性'},
        {done: Boolean(strategy), label: '价格策略'},
        {done: Boolean(checkedValue('transfer_policy')), label: '中转接受程度'},
        {done: Boolean(checkedValue('baggage')), label: '托运行李'},
        {done: Boolean(checkedValue('primary_goal')), label: '主目标'}
      ];
      baseItems.forEach(item => {
        if (!item.done) missing.push(item.label);
      });
      const done = baseItems.length - missing.length;
      if (requiredProgress) {
        requiredProgress.textContent = missing.length
          ? `已完成 ${done}/10 个基础项；还需填写：${missing.join('、')}`
          : '已完成 10/10 个基础项';
        requiredProgress.classList.toggle('incomplete', missing.length > 0);
      }
      if (previewButton) {
        previewButton.textContent = missing.length ? `请先填写${missing[0]}` : '开始监控';
        previewButton.disabled = missing.length > 0;
      }
      if (missingRequiredWarning) {
        missingRequiredWarning.style.display = missing.length ? 'block' : 'none';
        missingRequiredWarning.textContent = missing.length
          ? `请先填写：${missing.join('、')}`
          : '';
      }
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
      return [upper];
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
        hintEl.textContent = state.active.length ? '将搜索这些机场：' : '';
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
          refreshSummaryIfFinalStep();
        });
        tag.appendChild(remove);
        tagsEl.appendChild(tag);
      });
    }

    function updateAirportSelection(kind) {
      const value = kind === 'origin' ? selectedOrigin() : destinationInput.value.trim();
      const airports = resolveAirportsForInput(value);
      airportState[kind].all = airports;
      airportState[kind].active = airports.slice();
      renderAirportTags(kind);
    }

    function updateOriginAirportHint() {
      updateAirportSelection('origin');
    }

    function updateDestinationAirportHint() {
      updateAirportSelection('destination');
    }

    function activeAirportText(kind) {
      const active = airportState[kind].active;
      return active.length ? active.join('、') : '';
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

    function applyMonitorMode() {
      const precise = checkedValue('monitor_mode') === 'precise';
      document.querySelectorAll('.precise-only').forEach(el => {
        el.style.display = precise ? 'block' : 'none';
      });
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
      toggleShortTransferOptions();
      refreshSummaryIfFinalStep();
    }

    function toggleTimePreference() {
      const preference = checkedValue('time_preference') || 'unlimited';
      const custom = preference === 'custom' && checkedValue('monitor_mode') === 'precise';
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
    }

    function timePreferenceText() {
      return timePreferenceTextFromValue(checkedValue('time_preference'));
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

    function setPrefValue(key, value) {
      const target = document.querySelector(`[data-pref-value="${key}"]`);
      if (target) {
        target.textContent = value || '未设置';
      }
    }

    function updatePrefCards() {
      const companions = checkedValue('companions');
      setPrefValue('companions', companions === 'solo' ? '未设置' : selectedLabel('companions'));
      const timePreference = checkedValue('time_preference');
      setPrefValue(
        'time',
        timePreference === 'unlimited' ? '使用默认：避免红眼' : selectedLabel('time_preference')
      );
      const refund = checkedValue('refund_flexibility');
      setPrefValue(
        'refund',
        refund === 'preferred' ? '使用默认：便宜优先，提醒风险' : selectedLabel('refund_flexibility')
      );
      const airline = checkedValue('airline_policy');
      setPrefValue('airline', airline === 'any' ? '使用默认：不限' : selectedLabel('airline_policy'));
      setPrefValue(
        'self_transfer',
        checkedValue('accept_self_transfer') === 'true' ? '已设置：可以接受' : '使用默认：不接受'
      );
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
    }

    function selectedCheckboxLabels(name) {
      return Array.from(document.querySelectorAll(`input[name="${name}"]:checked`))
        .map(input => input.parentElement.textContent.trim());
    }

    function money(value) {
      const num = Number(value || 0);
      return num > 0 ? `¥${num.toLocaleString('zh-CN')}` : '';
    }

    function summaryLine(label, value) {
      if (!value) {
        return;
      }
      addSummaryLine(`${label}: ${value}`);
    }

    function toggleReturnDate() {
      const isRoundTrip = checkedValue('round_trip') === 'true';
      returnWrap.style.display = isRoundTrip ? 'block' : 'none';
      returnDate.required = isRoundTrip;
      toggleTimePreference();
    }

    function toggleBudgetRequired() {
      const explicit = checkedValue('budget_strategy') === 'explicit';
      if (budgetAmountFields) {
        budgetAmountFields.style.display = explicit ? 'block' : 'none';
      }
      maxBudgetInput.required = false;
      targetPriceInput.required = false;
      if (!explicit) {
        if (checkedValue('budget_strategy') === 'auto_judge') {
          setRadio('max_budget_mode', 'none');
          setRadio('target_price_mode', 'auto');
        } else if (checkedValue('budget_strategy') === 'low_price_alert') {
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
      const value = checkedValue('notification_frequency_rule');
      if (value) {
        setRadio('notification_frequency', value);
      }
    }

    function syncNotificationFrequencyToRule() {
      const value = checkedValue('notification_frequency');
      if (value) {
        setRadio('notification_frequency_rule', value);
      }
    }

    function toggleShortTransferOptions() {
      const policy = checkedValue('transfer_policy');
      const precise = checkedValue('monitor_mode') === 'precise';
      const visible = precise && (policy === 'reasonable' || policy === 'price_first');
      const priceFirst = precise && policy === 'price_first';
      shortTransferOptions.style.display = visible ? 'block' : 'none';
      shortTransferInputs.forEach(input => {
        input.disabled = !visible;
      });
      if (overnightTransferOptions) {
        overnightTransferOptions.style.display = priceFirst ? 'block' : 'none';
      }
      if (selfTransferOptions) {
        selfTransferOptions.style.display = priceFirst ? 'block' : 'none';
      }
    }

    function validatePriceInputs() {
      const maxBudgetMode = checkedValue('max_budget_mode');
      const targetPriceMode = checkedValue('target_price_mode');
      const maxBudget = Number(maxBudgetInput.value || 0);
      const targetPrice = Number(targetPriceInput.value || 0);
      const invalid = maxBudgetMode === 'fixed'
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
      if (hard.budget_strategy) setRadio('budget_strategy', hard.budget_strategy);
      if (hard.max_budget) maxBudgetInput.value = hard.max_budget;
      if (hard.target_price || soft.target_price) targetPriceInput.value = hard.target_price || soft.target_price;
      if (hard.transfer_policy) setRadio('transfer_policy', hard.transfer_policy);
      if (hard.baggage) setRadio('baggage', hard.baggage);
      if (soft.companions) setRadio('companions', soft.companions);
      const savedTimeMode = soft.time_preference_mode || soft.time_preference || hard.time_preference;
      if (savedTimeMode) setRadio('time_preference', savedTimeMode === 'any' ? 'unlimited' : savedTimeMode);
      if (soft.refund_flexibility) setRadio('refund_flexibility', soft.refund_flexibility);
      if (soft.price_sensitivity) setRadio('price_sensitivity', soft.price_sensitivity);
      if (soft.trip_type && form.trip_type) form.trip_type.value = soft.trip_type;
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
        setRadio('notification_frequency_rule', goals.frequency);
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
      if (companions === 'solo') {
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

    function addSummaryLine(text, className = '') {
      const li = document.createElement('li');
      li.textContent = text;
      if (className) {
        li.className = className;
      }
      summaryList.appendChild(li);
    }

    function addSummaryHeader(text) {
      addSummaryLine(text, 'summary-section-title');
    }

    function systemDefaultRulesForSummary() {
      const precise = checkedValue('monitor_mode') === 'precise';
      const rules = [];
      if (!precise || !moduleIsDirty('time')) {
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
        let text = selectedLabel('notification_frequency_rule') || selectedLabel('notification_frequency');
        if (checkedValue('notification_frequency_rule') === 'price_change') {
          text += `，${selectedLabel('price_change_threshold')}`;
        } else if (checkedValue('notification_frequency_rule') === 'daily_digest') {
          text += `，${selectedLabel('digest_time')}`;
        }
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
      summaryLine('路线', origin && destination ? `${origin} → ${destination}` : '');
      summaryLine('覆盖机场', `去${activeAirportText('origin') || '-'} | 到${activeAirportText('destination') || '-'}`);
      summaryLine('行程', isRoundTrip ? '往返' : '单程');
      summaryLine('出发', form.depart_date.value);
      if (isRoundTrip) {
      summaryLine('返程', returnDate.value);
      }
      summaryLine('日期弹性', selectedLabel('date_flexibility'));
      summaryLine('价格策略', selectedLabel('budget_strategy'));
      if (checkedValue('budget_strategy') === 'explicit') {
        summaryLine(
          '最高可接受价',
          maxBudgetMode === 'fixed' ? money(maxBudgetInput.value) : selectedLabel('max_budget_mode')
        );
        summaryLine(
          '理想入手价',
          targetPriceMode === 'fixed' ? money(targetPriceInput.value) : selectedLabel('target_price_mode')
        );
      }
      summaryLine('中转', selectedLabel('transfer_policy'));
      summaryLine('行李', selectedLabel('baggage'));
      if (checkedValue('companions') !== 'solo') {
        summaryLine('同行', selectedLabel('companions'));
      }
      if (checkedValue('monitor_mode') === 'precise') {
        summaryLine('时间偏好', timePreferenceText());
        if (checkedValue('time_preference') === 'custom') {
          if (isRoundTrip) {
            summaryLine('去程起飞时段', slotSummary('outbound_departure_slots', labels.departureSlots));
            summaryLine('返程起飞时段', slotSummary('return_departure_slots', labels.departureSlots));
          } else {
            summaryLine('起飞时段', slotSummary('departure_slots', labels.departureSlots));
          }
        }
        if (checkedValue('transfer_policy') !== 'direct_only') {
          summaryLine('最长总行程', selectedLabel('short_transfer_limit'));
        }
        if (checkedValue('transfer_policy') === 'price_first') {
          summaryLine('过夜中转', selectedLabel('accept_overnight_transfer'));
          summaryLine('非联程', selectedLabel('accept_self_transfer'));
        }
      }
      const secondary = selectedCheckboxLabels('secondary_goals');
      summaryLine('提醒', secondary.length ? secondary.join('、') : selectedLabel('primary_goal'));
      const methodText = selectedLabel('notification_method');
      const emailText = notificationEmailInput && notificationEmailInput.value.trim()
        ? ` ${notificationEmailInput.value.trim()}`
        : '';
      summaryLine('提醒方式', methodText ? `${methodText}${emailText}` : '');
      summaryLine('提醒频率', selectedLabel('notification_frequency'));
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
      return {
        monitor_mode: checkedValue('monitor_mode'),
        budget_strategy: checkedValue('budget_strategy'),
        transfer_policy: checkedValue('transfer_policy'),
        baggage: checkedValue('baggage'),
        time_preference: checkedValue('time_preference'),
        refund_flexibility: checkedValue('refund_flexibility'),
        price_sensitivity: checkedValue('price_sensitivity'),
        trip_type: form.trip_type ? form.trip_type.value : '',
        companions: checkedValue('companions'),
        airline_policy: checkedValue('airline_policy'),
        notification_method: checkedValue('notification_method'),
        notification_frequency: checkedValue('notification_frequency'),
        secondary_goals: checkedValues('secondary_goals'),
        short_transfer_limit: checkedValue('short_transfer_limit'),
        accept_overnight_transfer: checkedValue('accept_overnight_transfer'),
        accept_self_transfer: checkedValue('accept_self_transfer')
      };
    }

    function applyPreferenceTemplate(data) {
      if (!data) return;
      [
        'monitor_mode',
        'budget_strategy',
        'transfer_policy',
        'baggage',
        'time_preference',
        'refund_flexibility',
        'price_sensitivity',
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
      if (data.notification_frequency) {
        setRadio('notification_frequency_rule', data.notification_frequency);
      }
      if (form.trip_type && data.trip_type) {
        form.trip_type.value = data.trip_type;
      }
      secondaryGoalChecks.forEach(check => {
        check.checked = (data.secondary_goals || []).includes(check.value);
      });
      applyMonitorMode();
      toggleBudgetRequired();
      toggleNotificationMethod();
      toggleReturnDate();
      updateRequiredProgress();
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
      if (!validatePriceInputs()) {
        alert('理想入手价应低于最高可接受价，请确认是否填反了');
        targetPriceInput.focus();
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

    tripRadios.forEach(radio => radio.addEventListener('change', () => {
      toggleReturnDate();
      updateConditionalFields();
      updateStepper();
      updateRequiredProgress();
    }));
    budgetStrategyRadios.forEach(radio => radio.addEventListener('change', toggleBudgetRequired));
    maxBudgetRadios.forEach(radio => radio.addEventListener('change', toggleBudgetRequired));
    targetPriceRadios.forEach(radio => radio.addEventListener('change', toggleBudgetRequired));
    maxBudgetInput.addEventListener('input', () => { validatePriceInputs(); updateRequiredProgress(); });
    targetPriceInput.addEventListener('input', () => { validatePriceInputs(); updateRequiredProgress(); });
    originSelect.addEventListener('change', () => { updateOriginAirportHint(); updateRequiredProgress(); });
    originManual.addEventListener('input', () => { updateOriginAirportHint(); updateRequiredProgress(); });
    destinationInput.addEventListener('input', () => { updateDestinationAirportHint(); updateRequiredProgress(); });
    modeRadios.forEach(radio => radio.addEventListener('change', applyMonitorMode));
    timePreferenceRadios.forEach(radio => radio.addEventListener('change', toggleTimePreference));
    preciseTimeToggle?.addEventListener('click', () => {
      if (!preciseTimeOptions) return;
      preciseTimeOptions.style.display = preciseTimeOptions.style.display === 'block' ? 'none' : 'block';
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
    ['companions', 'time_preference', 'refund_flexibility', 'airline_policy', 'accept_self_transfer'].forEach(name => {
      document.querySelectorAll(`input[name="${name}"]`).forEach(input => {
        input.addEventListener('change', updatePrefCards);
      });
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
    toggleReturnDate();
    toggleBudgetRequired();
    toggleNotificationMethod();
    toggleShortTransferOptions();
    updateConditionalFields();
    updateDateFlexHint();
    updateOriginAirportHint();
    updateDestinationAirportHint();
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
      refreshSummaryIfFinalStep();
    });
    form.addEventListener('submit', event => {
      if (!validatePriceInputs()) {
        event.preventDefault();
        alert('理想入手价应低于最高可接受价，请确认是否填反了');
        targetPriceInput.focus();
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
    <h1>✅ 已创建监控</h1>
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
      <p>感谢反馈。系统会先记录这些信息，后续用于优化可购买性校验和推荐排序。</p>
      <a href="{{ url_for('index') }}">返回订阅表单</a>
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
    origin_manual = form.get("origin_manual", "").strip()
    origin_select = form.get("origin_select", "").strip()
    origin_input = origin_manual or ("" if origin_select == "OTHER" else origin_select)
    origin_info = resolve_location(origin_input)
    destination_info = resolve_location(normalize_destination(form.get("destination", "")))
    origin_airports_active = parse_active_airports(
        form.get("origin_airports_active"), origin_info["airports"]
    )
    destination_airports_active = parse_active_airports(
        form.get("destination_airports_active"), destination_info["airports"]
    )
    excluded_airports = sorted(
        (
            set(origin_info["airports"])
            | set(destination_info["airports"])
        )
        - (set(origin_airports_active) | set(destination_airports_active))
    )
    budget_strategy = form.get("budget_strategy", "explicit")
    max_budget_mode = form.get("max_budget_mode", "fixed")
    target_price_mode = form.get("target_price_mode", "fixed")
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
    max_extra_duration_hours = None
    max_total_duration_hours = None
    if form.get("transfer_policy", "reasonable") in {"reasonable", "short_ok", "price_first"}:
        max_extra_duration_hours, max_total_duration_hours = parse_short_transfer_limit(
            form.get("short_transfer_limit") or "extra_6"
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
    notification_frequency = frequency_aliases.get(
        form.get("notification_frequency", "important_only"),
        form.get("notification_frequency", "important_only"),
    )
    blocked_airlines = [
        item.strip()
        for item in form.get("exclude_airlines", "").replace("，", ",").split(",")
        if item.strip()
    ]
    for item in form.getlist("blocked_airlines_common"):
        if item and item not in blocked_airlines:
            blocked_airlines.append(item)

    return {
        "basic": {
            "origin": origin_info["value"],
            "origin_airports": origin_info["airports"],
            "origin_airports_active": origin_airports_active,
            "destination": destination_info["value"],
            "dest_airports": destination_info["airports"],
            "destination_airports": destination_info["airports"],
            "destination_airports_active": destination_airports_active,
            "trip_type": "round_trip" if round_trip else "one_way",
            "departure_date": form.get("depart_date", "").strip(),
            "return_date": form.get("return_date", "").strip() if round_trip else None,
        },
        "constraints": {
            "budget_strategy": budget_strategy,
            "max_price": max_budget,
            "ideal_price": target_price,
            "date_flexibility_days": parse_int(form.get("date_flexibility"), 0),
            "transfer_policy": form.get("transfer_policy", "reasonable"),
            "checked_baggage_required": form.get("baggage", "required") == "required",
        },
        "preferences": {
            "travelers": form.get("companions", "solo"),
            "time_pref": time_mode,
            "refund_policy": form.get("refund_flexibility", "preferred"),
            "price_sensitivity": form.get("price_sensitivity", "low"),
            "travel_type": form.get("trip_type", "tourism"),
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
                    "departure_start": form.get("departure_time_start", ""),
                    "departure_end": form.get("departure_time_end", ""),
                    "arrival_start": form.get("arrival_time_start", ""),
                    "arrival_end": form.get("arrival_time_end", ""),
                },
            },
            "transfer": {
                "max_total_duration": max_total_duration_hours,
                "max_extra_duration_hours": max_extra_duration_hours,
                "overnight_transfer": parse_bool(form.get("accept_overnight_transfer", "false")),
                "self_transfer": parse_bool(form.get("accept_self_transfer", "false")),
            },
            "airlines": {
                "preference": form.get("airline_policy", "any"),
                "blocked": blocked_airlines,
            },
            "alerts": {
                "frequency": notification_frequency,
                "types": form.getlist("secondary_goals"),
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
        "excluded_airports": excluded_airports,
        "monitor_mode": form.get("monitor_mode", "quick"),
        "depart_date": form.get("depart_date", "").strip(),
        "return_date": form.get("return_date", "").strip() if round_trip else None,
        "round_trip": round_trip,
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
            "transfer_policy": form.get("transfer_policy", "reasonable"),
            "max_extra_duration_hours": max_extra_duration_hours,
            "max_total_duration_hours": max_total_duration_hours,
            "departure_time_policy": form.get("departure_time_policy", "no_redeye"),
            "arrival_time_policy": form.get("arrival_time_policy", "any"),
            "time_preference": time_mode,
            **time_constraints,
            "baggage": form.get("baggage", "required"),
            "origin_airport_preference": form.get("origin_airport_preference", "all"),
            "accept_overnight_transfer": parse_bool(
                form.get("accept_overnight_transfer", "false")
            ),
            "accept_self_transfer": parse_bool(form.get("accept_self_transfer", "false")),
        },
        "soft_preferences": {
            "trip_type": form.get("trip_type", "tourism"),
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
            "companions": form.get("companions", "solo"),
            "price_sensitivity": form.get("price_sensitivity", "low"),
            "trip_rigidity": form.get("trip_rigidity", "confirmed"),
            "refund_flexibility": form.get("refund_flexibility", "preferred"),
            "airline_policy": form.get("airline_policy", "any"),
            "exclude_airlines": blocked_airlines,
            "target_price": target_price,
            "target_price_mode": target_price_mode,
            "price_tolerance": price_tolerance,
            "max_budget": max_budget,
        },
        "notification_goals": {
            "primary": form.get("primary_goal", "buy_timing"),
            "secondary": form.getlist("secondary_goals"),
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
        airport_short_names=AIRPORT_SHORT_NAMES,
        edit_subscription=edit_subscription or {},
        edit_index=edit_index,
    )


@app.post("/subscribe")
def subscribe():
    try:
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
    except Exception as exc:
        print(f"[表单] 提交订阅失败: {exc}")
        traceback.print_exc()
        raise


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
