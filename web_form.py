"""Flask form for flight monitor subscriptions."""

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
    0: "不能调，就这天",
    1: "前后1天可以",
    3: "前后3天都行",
    7: "前后一周都行",
}

BUDGET_MODE_LABELS = {
    "fixed": "输入具体金额",
    "none": "没有硬上限",
    "unknown": "不确定，帮我判断合理价格",
    "low_zone": "只要进入低价区间就提醒",
}

TRANSFER_LABELS = {
    "direct_only": "必须直飞",
    "short_ok": "可以中转，但总耗时别太长",
    "cheap_ok": "便宜很多的话可以中转",
    "price_first": "价格优先，怎么转都行",
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
    "price_drop_alert": "跌到合适价格时提醒我",
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
    .secondary-button {
      background: #f5f7fb;
      color: #1a73e8;
      border: 1px solid #c8d6f0;
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
    #summary-card {
      border: 1px solid #c8d6f0;
      border-radius: 8px;
      background: #f7f9fc;
      padding: 16px;
      margin-top: 20px;
    }
    #summary-card h2 { margin-top: 0; font-size: 20px; }
    #summary-card ul { padding-left: 22px; }
    .button-row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    @media (max-width: 520px) {
      .button-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <h1>航班监控订阅</h1>
  <p class="hint">先填基础需求即可；高级偏好可以按需展开。</p>

  <form id="subscription-form" method="post" action="{{ url_for('subscribe') }}">
    <fieldset>
      <legend>基础问题</legend>

      <label for="origin">出发地</label>
      <select id="origin" name="origin_select">
        {% for code, label in origins %}
        <option value="{{ code }}">{{ label }}</option>
        {% endfor %}
      </select>
      <input name="origin_manual" placeholder="或手动输入IATA代码，例如 PVG">

      <label for="destination">目的地</label>
      <input id="destination" name="destination" placeholder="例如 MCO / Orlando / 奥兰多" required>

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
      <p class="hint">影响系统搜索范围，选择越灵活越可能找到便宜日期</p>

      <label>同行人员</label>
      <div class="choice">
        <label><input type="radio" name="companions" value="solo" checked> 仅本人</label>
        <label><input type="radio" name="companions" value="with_elderly"> 有老人同行</label>
        <label><input type="radio" name="companions" value="with_child"> 有小孩同行（12岁以下）</label>
        <label><input type="radio" name="companions" value="with_elderly_child"> 老人和小孩都有</label>
      </div>
      <div id="auto-preference-notice" class="auto-notice"></div>

      <label>最高能接受的价格（超过就不考虑）</label>
      <input id="max_budget" name="max_budget" type="number" min="1" step="1" placeholder="例如 8000">
      <div class="choice">
        <label><input type="radio" name="max_budget_mode" value="fixed" checked> 输入具体金额</label>
        <label><input type="radio" name="max_budget_mode" value="none"> 没有硬上限</label>
      </div>

      <label>理想入手价（到这个价格就值得买）</label>
      <input id="target_price" name="target_price" type="number" min="1" step="1" placeholder="例如 6000（选填）">
      <div class="choice">
        <label><input type="radio" name="target_price_mode" value="fixed" checked> 输入具体金额</label>
        <label><input type="radio" name="target_price_mode" value="auto"> 不确定，帮我判断合理价格</label>
        <label><input type="radio" name="target_price_mode" value="low_zone"> 只要进入低价区间就提醒我</label>
      </div>

      <label>中转接受程度</label>
      <div class="choice">
        <label><input type="radio" name="transfer_policy" value="direct_only"> 必须直飞</label>
        <label><input type="radio" name="transfer_policy" value="short_ok" checked> 可以中转，但总耗时别太长</label>
        <label><input type="radio" name="transfer_policy" value="cheap_ok"> 便宜很多的话可以中转</label>
        <label><input type="radio" name="transfer_policy" value="price_first"> 价格优先，怎么转都行</label>
      </div>
      <div id="short-transfer-options" class="sub-options">
        <label>最长可接受总行程时间</label>
        <div class="choice">
          <label><input type="radio" name="short_transfer_limit" value="extra_3" checked> 不超过直飞时间+3小时</label>
          <label><input type="radio" name="short_transfer_limit" value="extra_6"> 不超过直飞时间+6小时</label>
          <label><input type="radio" name="short_transfer_limit" value="total_18"> 不超过18小时</label>
          <label><input type="radio" name="short_transfer_limit" value="total_24"> 不超过24小时</label>
        </div>
      </div>

      <label>可接受起飞时间</label>
      <div class="choice">
        <label><input type="radio" name="departure_time_policy" value="any"> 不限制</label>
        <label><input type="radio" name="departure_time_policy" value="no_redeye" checked> 不接受红眼凌晨</label>
        <label><input type="radio" name="departure_time_policy" value="daytime"> 希望白天出行</label>
      </div>

      <label>是否需要托运行李</label>
      <div class="choice">
        <label><input type="radio" name="baggage" value="required" checked> 必须</label>
        <label><input type="radio" name="baggage" value="not_needed"> 不需要</label>
        <label><input type="radio" name="baggage" value="unknown"> 不确定</label>
      </div>

      <label>主目标</label>
      <div class="choice">
        <label><input type="radio" name="primary_goal" value="price_drop_alert" required> 跌到合适价格时提醒我</label>
        <label><input type="radio" name="primary_goal" value="buy_timing" checked required> 判断现在该不该买</label>
        <label><input type="radio" name="primary_goal" value="cheaper_date" required> 帮我找更便宜的日期</label>
        <label><input type="radio" name="primary_goal" value="best_overall" required> 帮我找最合适航班</label>
      </div>
    </fieldset>

    <button id="advanced-toggle" class="secondary-button" type="button">▼ 展开高级偏好（可选）</button>

    <div id="advanced-preferences">
      <fieldset>
        <legend>高级偏好</legend>

        <label>这次行程是否可能取消或改期？</label>
        <div class="choice">
          <label><input type="radio" name="trip_rigidity" value="confirmed" checked> 铁定出发，不会变</label>
          <label><input type="radio" name="trip_rigidity" value="mostly"> 可能微调日期</label>
          <label><input type="radio" name="trip_rigidity" value="flexible"> 不太确定，有可能取消</label>
        </div>
        <p class="hint">影响退改签推荐，不确定的行程建议选择可退改机票</p>

        <label>可接受到达时间</label>
        <div class="choice">
          <label><input type="radio" name="arrival_time_policy" value="any" checked> 不限制</label>
          <label><input type="radio" name="arrival_time_policy" value="no_midnight"> 不接受凌晨到达（00:00-06:00）</label>
          <label><input type="radio" name="arrival_time_policy" value="daytime_only"> 必须白天到达（06:00-22:00）</label>
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

        <label>航司偏好</label>
        <div class="choice">
          <label><input type="radio" name="airline_policy" value="any" checked> 不限制</label>
          <label><input type="radio" name="airline_policy" value="prefer_full_service"> 偏好全服务航司</label>
          <label><input type="radio" name="airline_policy" value="no_lcc"> 不接受廉航</label>
          <label><input type="radio" name="airline_policy" value="exclude_airlines"> 有不接受的航司吗？</label>
        </div>
        <input name="exclude_airlines" placeholder="选填，多个航司用逗号分隔，例如 Spirit, Frontier">

        <label>附加关注</label>
        <div class="choice">
          <label><input type="checkbox" name="secondary_goals" value="low_price_alert"> 异常低价提醒</label>
          <label><input type="checkbox" name="secondary_goals" value="price_risk_alert"> 涨价风险提醒</label>
          <label><input type="checkbox" name="secondary_goals" value="cheaper_date"> 前后日期更便宜提醒</label>
          <label><input type="checkbox" name="secondary_goals" value="better_same_day"> 同日更优方案提醒</label>
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

    <button id="preview-button" type="button">开始监控</button>

    <div id="summary-card">
      <h2>系统将这样理解你的需求：</h2>
      <ul id="summary-list"></ul>
      <div class="button-row">
        <button type="submit">确认订阅</button>
        <button id="edit-button" class="secondary-button" type="button">返回修改</button>
      </div>
    </div>
  </form>

  <script>
    const labels = {
      dateFlex: {"0": "不能调，就这天", "1": "前后1天可以", "3": "前后3天都行", "7": "前后一周都行"},
      maxBudgetMode: {"fixed": "最高可接受", "none": "没有硬上限"},
      targetPriceMode: {"fixed": "理想入手价", "auto": "不确定，帮我判断合理价格", "low_zone": "只要进入低价区间就提醒"},
      transfer: {"direct_only": "必须直飞", "short_ok": "可以短中转", "cheap_ok": "便宜很多可以中转", "price_first": "价格优先，中转也可以"},
      departure: {"any": "不限制", "no_redeye": "不接受红眼凌晨", "daytime": "希望白天出行"},
      baggage: {"required": "必须托运", "not_needed": "不需要托运", "unknown": "不确定"},
      primary: {"price_drop_alert": "跌到合适价格时提醒我", "buy_timing": "判断现在该不该买", "cheaper_date": "帮我找更便宜的日期", "best_overall": "帮我找最合适航班"}
    };
    const goalDefaults = {
      price_drop_alert: ["low_price_alert"],
      buy_timing: ["price_risk_alert", "low_price_alert"],
      cheaper_date: ["cheaper_date"],
      best_overall: ["better_same_day"]
    };

    const form = document.getElementById('subscription-form');
    const tripRadios = document.querySelectorAll('input[name="round_trip"]');
    const returnWrap = document.getElementById('return-date-wrap');
    const returnDate = document.getElementById('return_date');
    const maxBudgetRadios = document.querySelectorAll('input[name="max_budget_mode"]');
    const targetPriceRadios = document.querySelectorAll('input[name="target_price_mode"]');
    const maxBudgetInput = document.getElementById('max_budget');
    const targetPriceInput = document.getElementById('target_price');
    const advanced = document.getElementById('advanced-preferences');
    const advancedToggle = document.getElementById('advanced-toggle');
    const previewButton = document.getElementById('preview-button');
    const summaryCard = document.getElementById('summary-card');
    const summaryList = document.getElementById('summary-list');
    const editButton = document.getElementById('edit-button');
    const transferRadios = document.querySelectorAll('input[name="transfer_policy"]');
    const shortTransferOptions = document.getElementById('short-transfer-options');
    const primaryGoalRadios = document.querySelectorAll('input[name="primary_goal"]');
    const secondaryGoalChecks = document.querySelectorAll('input[name="secondary_goals"]');
    const companionRadios = document.querySelectorAll('input[name="companions"]');
    const autoPreferenceNotice = document.getElementById('auto-preference-notice');

    function checkedValue(name) {
      const selected = document.querySelector(`input[name="${name}"]:checked`);
      return selected ? selected.value : "";
    }

    function selectedOrigin() {
      const manual = form.origin_manual.value.trim().toUpperCase();
      return manual || form.origin_select.value;
    }

    function toggleReturnDate() {
      const isRoundTrip = checkedValue('round_trip') === 'true';
      returnWrap.style.display = isRoundTrip ? 'block' : 'none';
      returnDate.required = isRoundTrip;
    }

    function toggleBudgetRequired() {
      maxBudgetInput.required = false;
      targetPriceInput.required = false;
    }

    function toggleShortTransferOptions() {
      shortTransferOptions.style.display =
        checkedValue('transfer_policy') === 'short_ok' ? 'block' : 'none';
    }

    function applyDefaultSecondaryGoals() {
      const defaults = goalDefaults[checkedValue('primary_goal')] || [];
      secondaryGoalChecks.forEach(check => {
        check.checked = defaults.includes(check.value);
      });
    }

    function setRadio(name, value, mark = false) {
      const input = document.querySelector(`input[name="${name}"][value="${value}"]`);
      if (!input) return;
      input.checked = true;
      if (mark) {
        input.closest('label')?.classList.add('auto-suggested');
      }
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

      setRadio('departure_time_policy', 'no_redeye', true);
      setRadio('baggage', 'required', true);

      if (companions === 'with_elderly' || companions === 'with_elderly_child') {
        setRadio('transfer_policy', 'short_ok', true);
        setRadio('short_transfer_limit', 'extra_3', true);
        toggleShortTransferOptions();
        autoPreferenceNotice.textContent = '已根据老人同行自动调整推荐偏好，你仍可手动修改';
      } else if (companions === 'with_child') {
        autoPreferenceNotice.textContent = '已根据带小孩出行自动调整推荐偏好，你仍可手动修改';
      }
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
      const destination = form.destination.value.trim().toUpperCase();
      const maxBudgetMode = checkedValue('max_budget_mode');
      const targetPriceMode = checkedValue('target_price_mode');
      const maxBudget = Number(maxBudgetInput.value || 0);
      const targetPrice = Number(targetPriceInput.value || 0);

      addSummaryLine(`监控航线：${origin} → ${destination}`);
      addSummaryLine(`出发日期：${form.depart_date.value}`);
      if (checkedValue('round_trip') === 'true') {
        addSummaryLine(`返程日期：${returnDate.value}`);
      }
      addSummaryLine(`出发日期可调整：${labels.dateFlex[checkedValue('date_flexibility')]}`);
      if (maxBudgetMode === 'fixed') {
        addSummaryLine(`最高可接受：¥${maxBudget.toLocaleString('zh-CN')}`);
      } else {
        addSummaryLine(`最高可接受：${labels.maxBudgetMode[maxBudgetMode]}`);
      }
      if (targetPriceMode === 'fixed') {
        addSummaryLine(`理想入手价：¥${targetPrice.toLocaleString('zh-CN')}`);
      } else {
        addSummaryLine(`理想入手价：${labels.targetPriceMode[targetPriceMode]}`);
      }
      addSummaryLine(`中转策略：${labels.transfer[checkedValue('transfer_policy')]}`);
      if (checkedValue('transfer_policy') === 'short_ok') {
        const limit = document.querySelector('input[name="short_transfer_limit"]:checked');
        addSummaryLine(`最长总行程时间：${limit ? limit.parentElement.textContent.trim() : '不超过直飞时间+3小时'}`);
      }
      addSummaryLine(`时间要求：${labels.departure[checkedValue('departure_time_policy')]}`);
      addSummaryLine(`行李：${labels.baggage[checkedValue('baggage')]}`);
      addSummaryLine(`主目标：${labels.primary[checkedValue('primary_goal')]}`);
    }

    advancedToggle.addEventListener('click', () => {
      const expanded = advanced.style.display === 'block';
      advanced.style.display = expanded ? 'none' : 'block';
      advancedToggle.textContent = expanded ? '▼ 展开高级偏好（可选）' : '▲ 收起高级偏好';
    });

    previewButton.addEventListener('click', () => {
      toggleBudgetRequired();
      toggleReturnDate();
      if (!form.reportValidity()) {
        return;
      }
      buildSummary();
      summaryCard.style.display = 'block';
      summaryCard.scrollIntoView({behavior: 'smooth', block: 'start'});
    });

    editButton.addEventListener('click', () => {
      summaryCard.style.display = 'none';
      window.scrollTo({top: 0, behavior: 'smooth'});
    });

    tripRadios.forEach(radio => radio.addEventListener('change', toggleReturnDate));
    maxBudgetRadios.forEach(radio => radio.addEventListener('change', toggleBudgetRequired));
    targetPriceRadios.forEach(radio => radio.addEventListener('change', toggleBudgetRequired));
    transferRadios.forEach(radio => radio.addEventListener('change', toggleShortTransferOptions));
    primaryGoalRadios.forEach(radio => radio.addEventListener('change', applyDefaultSecondaryGoals));
    companionRadios.forEach(radio => radio.addEventListener('change', applyCompanionDefaults));
    toggleReturnDate();
    toggleBudgetRequired();
    toggleShortTransferOptions();
    applyDefaultSecondaryGoals();
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

    <p><b>预计首次推送：</b>下一次定时采集完成后</p>
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


def parse_short_transfer_limit(value: str | None) -> tuple[int | None, int | None]:
    if value == "extra_6":
        return 6, None
    if value == "total_18":
        return None, 18
    if value == "total_24":
        return None, 24
    return 3, None


def first_push_text() -> str:
    next_time = datetime.now() + timedelta(minutes=10)
    return next_time.strftime("%Y-%m-%d %H:%M")


def build_subscription(form) -> dict:
    round_trip = parse_bool(form.get("round_trip", "false"))
    origin = (
        form.get("origin_manual", "").strip().upper()
        or form.get("origin_select", "").strip().upper()
    )
    max_budget_mode = form.get("max_budget_mode", "fixed")
    target_price_mode = form.get("target_price_mode", "fixed")
    target_price = parse_optional_budget(form.get("target_price"), target_price_mode)
    max_budget = None
    if max_budget_mode == "fixed":
        max_budget = infer_max_budget(parse_int(form.get("max_budget"), 0), target_price)
    max_extra_duration_hours = None
    max_total_duration_hours = None
    if form.get("transfer_policy", "short_ok") == "short_ok":
        max_extra_duration_hours, max_total_duration_hours = parse_short_transfer_limit(
            form.get("short_transfer_limit")
        )
    return {
        "origin": origin,
        "destination": normalize_destination(form.get("destination", "")),
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
            "transfer_policy": form.get("transfer_policy", "short_ok"),
            "max_extra_duration_hours": max_extra_duration_hours,
            "max_total_duration_hours": max_total_duration_hours,
            "departure_time_policy": form.get("departure_time_policy", "no_redeye"),
            "arrival_time_policy": form.get("arrival_time_policy", "any"),
            "baggage": form.get("baggage", "required"),
            "refund_flexibility": form.get("refund_flexibility", "preferred"),
            "airline_policy": form.get("airline_policy", "any"),
            "exclude_airlines": [
                item.strip()
                for item in form.get("exclude_airlines", "").replace("，", ",").split(",")
                if item.strip()
            ],
        },
        "soft_preferences": {
            "trip_type": form.get("trip_type", "tourism"),
            "companions": form.get("companions", "solo"),
            "price_sensitivity": form.get("price_sensitivity", "low"),
            "trip_rigidity": form.get("trip_rigidity", "confirmed"),
            "target_price": target_price,
            "target_price_mode": target_price_mode,
        },
        "notification_goals": {
            "primary": form.get("primary_goal", "buy_timing"),
            "secondary": form.getlist("secondary_goals"),
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
    departure_policy = hard.get("departure_time_policy")
    if departure_policy == "no_redeye":
        exclusions.append("23:00-06:00起飞的红眼航班")
    elif departure_policy == "daytime":
        exclusions.append("不符合白天出行要求的航班")
    if hard.get("baggage") == "required":
        exclusions.append("不含免费托运的方案")
    if hard.get("transfer_policy") == "direct_only":
        exclusions.append("需要中转的方案")
    budget = hard.get("max_budget", hard.get("budget"))
    if budget:
        exclusions.append(f"超出¥{budget:,}预算的方案")

    return {
        "route": f"{city_label(subscription.get('origin'))} → {city_label(subscription.get('destination'))}",
        "reminders": reminders,
        "exclusions": exclusions,
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
        summary=build_success_summary(subscription) if subscription else {},
        first_push_time=first_push_text(),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
