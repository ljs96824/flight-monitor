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


BASE_DIR = Path(__file__).parent
SUBSCRIPTIONS_PATH = BASE_DIR / "data" / "subscriptions.json"
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
    "early_morning": "早班 06:00-09:00",
    "morning": "上午 09:00-12:00",
    "afternoon": "下午 12:00-17:00",
    "evening": "傍晚 17:00-20:00",
    "night": "晚班 20:00-23:00",
    "redeye": "红眼 23:00-06:00",
}

ARRIVAL_SLOT_LABELS = {
    "early_morning": "清晨 06:00-09:00",
    "morning": "上午 09:00-12:00",
    "afternoon": "下午 12:00-17:00",
    "evening": "傍晚 17:00-20:00",
    "night": "晚间 20:00-23:00",
    "redeye": "凌晨 23:00-06:00",
}

DEFAULT_DEPARTURE_SLOTS = ["early_morning", "morning", "afternoon", "evening", "night"]
DEFAULT_ARRIVAL_SLOTS = ["early_morning", "morning", "afternoon", "evening", "night"]
ALL_TIME_SLOTS = ["early_morning", "morning", "afternoon", "evening", "night", "redeye"]
DAYTIME_TIME_SLOTS = ["early_morning", "morning", "afternoon", "evening"]

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
    #overnight-transfer-options,
    #self-transfer-options {
      display: none;
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

  <div id="mobile-stepper">
    <div class="step-dots" id="step-dots">● ○ ○ ○</div>
    <div id="step-label">第1步/共4步：行程信息</div>
  </div>

  <form id="subscription-form" method="post" action="{{ url_for('subscribe') }}">
    <div class="mode-toggle">
      <div class="mode-toggle-title">模式</div>
      <div class="choice">
        <label><input type="radio" name="monitor_mode" value="quick" checked> 快速监控</label>
        <label><input type="radio" name="monitor_mode" value="precise"> 精准监控</label>
      </div>
      <p class="hint">快速监控只填写基础信息；精准监控会展开补充偏好和筛选规则。</p>
    </div>

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

      <div id="return-date-wrap">
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

      <button id="advanced-toggle" class="secondary-button" type="button">＋ 补充偏好，让推荐更准确</button>
      <div id="advanced-preferences" class="smart-panel">
      <fieldset>
        <legend>补充偏好，让推荐更准确</legend>
        <p class="hint">不填也可以，系统会按普通出行默认规则监控</p>

        <label>同行人员</label>
        <div class="choice">
          <label><input type="radio" name="companions" value="solo" checked> 仅本人</label>
          <label><input type="radio" name="companions" value="with_elderly"> 有老人同行</label>
          <label><input type="radio" name="companions" value="with_child"> 有小孩同行（12岁以下）</label>
          <label><input type="radio" name="companions" value="with_elderly_child"> 老人和小孩都有</label>
        </div>
        <div id="auto-preference-notice" class="auto-notice"></div>

        <input type="hidden" name="departure_time_policy" value="any">
        <input type="hidden" name="trip_rigidity" value="confirmed">

        <label>时间偏好</label>
        <div class="choice">
          <label><input type="radio" name="time_preference" value="any" checked> 不限制</label>
          <label><input type="radio" name="time_preference" value="daytime"> 白天优先</label>
          <label><input type="radio" name="time_preference" value="no_redeye"> 不接受红眼凌晨</label>
          <label><input type="radio" name="time_preference" value="custom"> 自定义时间段</label>
        </div>

        <div id="custom-time-options">
        <fieldset id="single-time-preferences" class="time-preferences time-outbound">
          <strong>时段偏好</strong>
          <label>偏好哪些时段起飞？（可多选）</label>
          <div class="choice">
            <label><input type="checkbox" name="departure_slots" value="early_morning" checked> 早班 06:00-09:00</label>
            <label><input type="checkbox" name="departure_slots" value="morning" checked> 上午 09:00-12:00</label>
            <label><input type="checkbox" name="departure_slots" value="afternoon" checked> 下午 12:00-17:00</label>
            <label><input type="checkbox" name="departure_slots" value="evening" checked> 傍晚 17:00-20:00</label>
            <label><input type="checkbox" name="departure_slots" value="night" checked> 晚班 20:00-23:00</label>
            <label><input type="checkbox" name="departure_slots" value="redeye"> 红眼 23:00-06:00</label>
          </div>

          <label>可接受哪些时段到达？（可多选）</label>
          <div class="choice">
            <label><input type="checkbox" name="arrival_slots" value="early_morning" checked> 清晨 06:00-09:00</label>
            <label><input type="checkbox" name="arrival_slots" value="morning" checked> 上午 09:00-12:00</label>
            <label><input type="checkbox" name="arrival_slots" value="afternoon" checked> 下午 12:00-17:00</label>
            <label><input type="checkbox" name="arrival_slots" value="evening" checked> 傍晚 17:00-20:00</label>
            <label><input type="checkbox" name="arrival_slots" value="night" checked> 晚间 20:00-23:00</label>
            <label><input type="checkbox" name="arrival_slots" value="redeye"> 凌晨 23:00-06:00</label>
          </div>
        </fieldset>

        <div id="round-trip-time-preferences">
          <fieldset class="time-preferences time-outbound">
            <strong>━━ 去程时段偏好 ━━</strong>
            <label>去程偏好哪些时段起飞？</label>
            <div class="choice">
              <label><input type="checkbox" name="outbound_departure_slots" value="early_morning" checked> 早班 06:00-09:00</label>
              <label><input type="checkbox" name="outbound_departure_slots" value="morning" checked> 上午 09:00-12:00</label>
              <label><input type="checkbox" name="outbound_departure_slots" value="afternoon" checked> 下午 12:00-17:00</label>
              <label><input type="checkbox" name="outbound_departure_slots" value="evening" checked> 傍晚 17:00-20:00</label>
              <label><input type="checkbox" name="outbound_departure_slots" value="night" checked> 晚班 20:00-23:00</label>
              <label><input type="checkbox" name="outbound_departure_slots" value="redeye"> 红眼 23:00-06:00</label>
            </div>

            <label>去程可接受哪些时段到达？</label>
            <div class="choice">
              <label><input type="checkbox" name="outbound_arrival_slots" value="early_morning" checked> 清晨 06:00-09:00</label>
              <label><input type="checkbox" name="outbound_arrival_slots" value="morning" checked> 上午 09:00-12:00</label>
              <label><input type="checkbox" name="outbound_arrival_slots" value="afternoon" checked> 下午 12:00-17:00</label>
              <label><input type="checkbox" name="outbound_arrival_slots" value="evening" checked> 傍晚 17:00-20:00</label>
              <label><input type="checkbox" name="outbound_arrival_slots" value="night" checked> 晚间 20:00-23:00</label>
              <label><input type="checkbox" name="outbound_arrival_slots" value="redeye"> 凌晨 23:00-06:00</label>
            </div>
          </fieldset>

          <fieldset class="time-preferences time-return">
            <strong>━━ 返程时段偏好 ━━</strong>
            <label>返程偏好哪些时段起飞？</label>
            <div class="choice">
              <label><input type="checkbox" name="return_departure_slots" value="early_morning" checked> 早班 06:00-09:00</label>
              <label><input type="checkbox" name="return_departure_slots" value="morning" checked> 上午 09:00-12:00</label>
              <label><input type="checkbox" name="return_departure_slots" value="afternoon" checked> 下午 12:00-17:00</label>
              <label><input type="checkbox" name="return_departure_slots" value="evening" checked> 傍晚 17:00-20:00</label>
              <label><input type="checkbox" name="return_departure_slots" value="night" checked> 晚班 20:00-23:00</label>
              <label><input type="checkbox" name="return_departure_slots" value="redeye"> 红眼 23:00-06:00</label>
            </div>

            <label>返程可接受哪些时段到达？</label>
            <div class="choice">
              <label><input type="checkbox" name="return_arrival_slots" value="early_morning" checked> 清晨 06:00-09:00</label>
              <label><input type="checkbox" name="return_arrival_slots" value="morning" checked> 上午 09:00-12:00</label>
              <label><input type="checkbox" name="return_arrival_slots" value="afternoon" checked> 下午 12:00-17:00</label>
              <label><input type="checkbox" name="return_arrival_slots" value="evening" checked> 傍晚 17:00-20:00</label>
              <label><input type="checkbox" name="return_arrival_slots" value="night" checked> 晚间 20:00-23:00</label>
              <label><input type="checkbox" name="return_arrival_slots" value="redeye"> 凌晨 23:00-06:00</label>
            </div>
          </fieldset>
        </div>
        </div>

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
      </fieldset>
    </div>

      <button id="rules-toggle" class="secondary-button" type="button">＋ 更细的筛选规则</button>
      <div id="advanced-rules" class="smart-panel">
        <fieldset>
          <legend>更细的筛选规则</legend>
          <p class="hint">适合有特定要求的用户，一般用户可跳过</p>

          <div id="short-transfer-options" class="sub-options">
            <label>最长可接受总行程时间</label>
            <div class="choice">
              <label><input type="radio" name="short_transfer_limit" value="extra_3"> 不超过直飞时间+3小时</label>
              <label><input type="radio" name="short_transfer_limit" value="extra_6" checked> 不超过直飞时间+6小时</label>
              <label><input type="radio" name="short_transfer_limit" value="total_18"> 不超过18小时</label>
              <label><input type="radio" name="short_transfer_limit" value="total_24"> 不超过24小时</label>
            </div>
          </div>

          <div id="overnight-transfer-options" class="sub-options">
            <label>是否接受过夜中转</label>
            <div class="choice">
              <label><input type="radio" name="accept_overnight_transfer" value="false" checked> 不接受</label>
              <label><input type="radio" name="accept_overnight_transfer" value="true"> 可以接受</label>
            </div>
          </div>

          <div id="self-transfer-options" class="sub-options">
            <label>是否接受非联程</label>
            <div class="choice">
              <label><input type="radio" name="accept_self_transfer" value="false" checked> 不接受</label>
              <label><input type="radio" name="accept_self_transfer" value="true"> 可以接受</label>
            </div>
          </div>

          <label>航司偏好</label>
          <div class="choice">
            <label><input type="radio" name="airline_policy" value="any" checked> 不限制</label>
            <label><input type="radio" name="airline_policy" value="prefer_full_service"> 偏好全服务航司</label>
            <label><input type="radio" name="airline_policy" value="no_lcc"> 不接受廉航</label>
            <label><input type="radio" name="airline_policy" value="exclude_airlines"> 有不接受的航司吗？</label>
          </div>

          <label>不接受的航司</label>
          <input name="exclude_airlines" placeholder="选填，多个航司用逗号分隔，例如 Spirit, Frontier">

          <label>附加关注</label>
          <div class="choice">
            <label><input type="checkbox" name="secondary_goals" value="low_price_alert"> 异常低价提醒</label>
            <label><input type="checkbox" name="secondary_goals" value="price_risk_alert"> 涨价风险提醒</label>
            <label><input type="checkbox" name="secondary_goals" value="cheaper_date"> 前后日期更便宜提醒</label>
            <label><input type="checkbox" name="secondary_goals" value="better_same_day"> 同日更优方案提醒</label>
          </div>
          <p id="date-flex-warning" class="inline-warning">你选了不可调整，但仍可接收前后日期差价参考</p>

          <label>提醒频率</label>
          <div class="choice">
            <label><input type="radio" name="notification_frequency" value="important_only" checked> 仅重要变化时提醒（价格显著下降、即将涨价）</label>
            <label><input type="radio" name="notification_frequency" value="daily_summary"> 每天汇总推送一次</label>
            <label><input type="radio" name="notification_frequency" value="every_change"> 每次价格变化都提醒</label>
          </div>
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

    <button id="preview-button" type="button">开始监控</button>

    <div id="summary-card">
      <h2>即将创建的监控：</h2>
      <ul id="summary-list"></ul>
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
        early_morning: "早班 06:00-09:00",
        morning: "上午 09:00-12:00",
        afternoon: "下午 12:00-17:00",
        evening: "傍晚 17:00-20:00",
        night: "晚班 20:00-23:00",
        redeye: "红眼 23:00-06:00"
      },
      arrivalSlots: {
        early_morning: "清晨 06:00-09:00",
        morning: "上午 09:00-12:00",
        afternoon: "下午 12:00-17:00",
        evening: "傍晚 17:00-20:00",
        night: "晚间 20:00-23:00",
        redeye: "凌晨 23:00-06:00"
      },
      baggage: {"required": "必须托运", "not_needed": "不需要托运", "unknown": "不确定"},
      primary: {"price_drop_alert": "找到合适价格时提醒我", "buy_timing": "判断现在该不该买", "cheaper_date": "帮我找更便宜的日期", "best_overall": "帮我找最合适航班"},
      frequency: {"important_only": "仅重要变化时提醒", "daily_summary": "每天汇总推送一次", "every_change": "每次价格变化都提醒"}
    };
    const goalDefaults = {
      price_drop_alert: ["low_price_alert", "price_risk_alert"],
      buy_timing: ["price_risk_alert", "low_price_alert"],
      cheaper_date: ["cheaper_date"],
      best_overall: ["better_same_day"]
    };
    const cityAirports = {{ city_airports|tojson }};
    const airportShortNames = {{ airport_short_names|tojson }};

    const form = document.getElementById('subscription-form');
    const modeRadios = document.querySelectorAll('input[name="monitor_mode"]');
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
    const stepTitles = ['行程信息', '价格底线', '监控目标', '完成'];
    let currentStep = 1;

    if (stepTimePreferences && customTimeOptions) {
      stepTimePreferences.appendChild(customTimeOptions);
    }

    function checkedValue(name) {
      const selected = document.querySelector(`input[name="${name}"]:checked`);
      return selected ? selected.value : "";
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
      toggleBudgetRequired();
      toggleReturnDate();
      if (!validatePriceInputs()) {
        alert('理想入手价应低于最高可接受价，请确认是否填反了');
        targetPriceInput.focus();
        return false;
      }
      return form.reportValidity();
    }

    function refreshSummaryIfFinalStep() {
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
      const preference = checkedValue('time_preference') || 'any';
      const custom = preference === 'custom' && checkedValue('monitor_mode') === 'precise';
      if (customTimeOptions) {
        customTimeOptions.style.display = custom ? 'block' : 'none';
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
      const map = {
        any: '不限制',
        daytime: '白天优先',
        no_redeye: '不接受红眼',
        custom: '自定义时间段'
      };
      return map[checkedValue('time_preference')] || '';
    }

    function selectedLabel(name) {
      const input = document.querySelector(`input[name="${name}"]:checked`);
      return input ? input.parentElement.textContent.trim() : '';
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
      maxBudgetInput.required = false;
      targetPriceInput.required = false;
      validatePriceInputs();
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

    function addSummaryLine(text) {
      const li = document.createElement('li');
      li.textContent = text;
      summaryList.appendChild(li);
    }

    function buildSummary() {
      summaryList.innerHTML = "";
      const origin = selectedOrigin();
      const destination = destinationInput.value.trim();
      const isRoundTrip = checkedValue('round_trip') === 'true';
      const maxBudgetMode = checkedValue('max_budget_mode');
      const targetPriceMode = checkedValue('target_price_mode');

      summaryLine('路线', origin && destination ? `${origin} → ${destination}` : '');
      summaryLine('覆盖机场', `去${activeAirportText('origin') || '-'} | 到${activeAirportText('destination') || '-'}`);
      summaryLine('行程', isRoundTrip ? '往返' : '单程');
      summaryLine('出发', form.depart_date.value);
      if (isRoundTrip) {
        summaryLine('返程', returnDate.value);
      }
      summaryLine('日期弹性', selectedLabel('date_flexibility'));
      summaryLine(
        '最高可接受价',
        maxBudgetMode === 'fixed' ? money(maxBudgetInput.value) : selectedLabel('max_budget_mode')
      );
      summaryLine(
        '理想入手价',
        targetPriceMode === 'fixed' ? money(targetPriceInput.value) : selectedLabel('target_price_mode')
      );
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
      summaryLine('提醒频率', selectedLabel('notification_frequency'));
      addSummaryLine('未填写的偏好将按普通出行默认处理');
      return;
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
      updateStepper();
    }));
    maxBudgetRadios.forEach(radio => radio.addEventListener('change', toggleBudgetRequired));
    targetPriceRadios.forEach(radio => radio.addEventListener('change', toggleBudgetRequired));
    maxBudgetInput.addEventListener('input', validatePriceInputs);
    targetPriceInput.addEventListener('input', validatePriceInputs);
    originSelect.addEventListener('change', updateOriginAirportHint);
    originManual.addEventListener('input', updateOriginAirportHint);
    destinationInput.addEventListener('input', updateDestinationAirportHint);
    modeRadios.forEach(radio => radio.addEventListener('change', applyMonitorMode));
    timePreferenceRadios.forEach(radio => radio.addEventListener('change', toggleTimePreference));
    transferRadios.forEach(radio => radio.addEventListener('change', toggleShortTransferOptions));
    dateFlexRadios.forEach(radio => radio.addEventListener('change', updateDateFlexHint));
    secondaryGoalChecks.forEach(check => check.addEventListener('change', updateDateFlexHint));
    primaryGoalRadios.forEach(radio => radio.addEventListener('change', () => {
      applyDefaultSecondaryGoals();
      if (isMobileStepper() && currentStep === stepTitles.length) {
        buildSummary();
      }
    }));
    companionRadios.forEach(radio => radio.addEventListener('change', applyCompanionDefaults));
    toggleReturnDate();
    toggleBudgetRequired();
    toggleShortTransferOptions();
    updateDateFlexHint();
    updateOriginAirportHint();
    updateDestinationAirportHint();
    advanced.style.display = 'none';
    advancedToggle.textContent = '＋ 补充偏好，让推荐更准确';
    advancedRules.style.display = 'none';
    rulesToggle.textContent = '＋ 更细的筛选规则';
    applyMonitorMode();
    toggleTimePreference();
    applyDefaultSecondaryGoals();
    updateStepper();
    form.addEventListener('input', refreshSummaryIfFinalStep);
    form.addEventListener('change', refreshSummaryIfFinalStep);
    form.addEventListener('submit', event => {
      if (!validatePriceInputs()) {
        event.preventDefault();
        alert('理想入手价应低于最高可接受价，请确认是否填反了');
        targetPriceInput.focus();
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
  </style>
</head>
<body>
  <div class="card">
    <h1>✅ 已创建监控</h1>
    <p><b>{{ summary.route }}</b></p>
    {% if summary.airport_coverage %}
    <p>{{ summary.airport_coverage }}</p>
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
    if preference == "any":
        return list(ALL_TIME_SLOTS)
    if preference == "daytime":
        return list(DAYTIME_TIME_SLOTS)
    if preference == "no_redeye":
        return list(DEFAULT_DEPARTURE_SLOTS)
    return form.getlist(field_name) or list(default_slots)


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
    max_budget_mode = form.get("max_budget_mode", "fixed")
    target_price_mode = form.get("target_price_mode", "fixed")
    target_price = parse_optional_budget(form.get("target_price"), target_price_mode)
    price_tolerance = parse_price_tolerance(form)
    max_budget = None
    if max_budget_mode == "fixed":
        max_budget = infer_max_budget(parse_int(form.get("max_budget"), 0), target_price)
    max_extra_duration_hours = None
    max_total_duration_hours = None
    if form.get("transfer_policy", "reasonable") in {"reasonable", "short_ok", "price_first"}:
        max_extra_duration_hours, max_total_duration_hours = parse_short_transfer_limit(
            form.get("short_transfer_limit") or "extra_6"
        )
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
    time_constraints = {
        "departure_slots": departure_slots,
        "arrival_slots": arrival_slots,
        "preferred_departure_slots": departure_slots,
        "preferred_arrival_slots": arrival_slots,
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
            }
        )
    return {
        "origin": origin_info["value"],
        "origin_type": origin_info["type"],
        "origin_airports": origin_info["airports"],
        "origin_airports_active": origin_airports_active,
        "destination": destination_info["value"],
        "destination_type": destination_info["type"],
        "destination_airports": destination_info["airports"],
        "destination_airports_active": destination_airports_active,
        "excluded_airports": excluded_airports,
        "depart_date": form.get("depart_date", "").strip(),
        "return_date": form.get("return_date", "").strip() if round_trip else None,
        "round_trip": round_trip,
        "date_flexibility": parse_int(form.get("date_flexibility"), 0),
        "return_date_flexibility": (
            parse_int(form.get("return_date_flexibility"), 0) if round_trip else 0
        ),
        "hard_constraints": {
            "max_budget": max_budget,
            "max_budget_mode": max_budget_mode,
            "transfer_policy": form.get("transfer_policy", "reasonable"),
            "max_extra_duration_hours": max_extra_duration_hours,
            "max_total_duration_hours": max_total_duration_hours,
            "departure_time_policy": form.get("departure_time_policy", "no_redeye"),
            "arrival_time_policy": form.get("arrival_time_policy", "any"),
            "time_preference": form.get("time_preference", "any"),
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
            "companions": form.get("companions", "solo"),
            "price_sensitivity": form.get("price_sensitivity", "low"),
            "trip_rigidity": form.get("trip_rigidity", "confirmed"),
            "refund_flexibility": form.get("refund_flexibility", "preferred"),
            "airline_policy": form.get("airline_policy", "any"),
            "exclude_airlines": [
                item.strip()
                for item in form.get("exclude_airlines", "").replace("，", ",").split(",")
                if item.strip()
            ],
            "target_price": target_price,
            "target_price_mode": target_price_mode,
            "price_tolerance": price_tolerance,
            "max_budget": max_budget,
        },
        "notification_goals": {
            "primary": form.get("primary_goal", "buy_timing"),
            "secondary": form.getlist("secondary_goals"),
            "frequency": form.get("notification_frequency", "important_only"),
        },
        "status": "active",
        "created_at": datetime.now().isoformat(),
    }


def build_success_summary(subscription: dict) -> dict:
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
        "reminders": reminders,
        "exclusions": exclusions,
    }


@app.get("/")
def index():
    return render_template_string(
        FORM_TEMPLATE,
        origins=COMMON_ORIGINS,
        city_airports=CITY_AIRPORTS,
        airport_short_names=AIRPORT_SHORT_NAMES,
    )


@app.post("/subscribe")
def subscribe():
    try:
        print("[表单] 开始构建订阅")
        subscription = build_subscription(request.form)
        print("[表单] 订阅构建完成")

        print("[表单] 开始保存订阅")
        save_subscription(subscription)
        subscriptions = load_subscriptions()
        index = len(subscriptions) - 1
        print(f"[表单] 订阅保存完成: index={index}")

        print("[表单] 开始触发后台采集")
        start_background_collection(subscription)
        print("[表单] 后台采集触发完成")

        return redirect(url_for("success", index=index))
    except Exception as exc:
        print(f"[表单] 提交订阅失败: {exc}")
        traceback.print_exc()
        raise


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
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
