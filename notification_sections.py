"""通知小节完整性契约。

这里仅声明通知结构和可见证据，不参与航班筛选、价格计算或推荐判定。
邮件与详情页共同组成完整通知包；任何 canonical 小节缺失都应由契约测试捕获。
"""

from __future__ import annotations

import html
import re


STANDARD_SECTIONS = (
    "action_panel",
    "primary_plan",
    "excluded_plans",
    "price_trend",
    "price_signal",
    "data_source",
    "data_freshness",
    "quota_overview",
    "provenance",
)

NO_MATCH_SECTIONS = tuple(
    "alternative_plans" if section == "primary_plan" else section
    for section in STANDARD_SECTIONS
)

DATA_INCOMPLETE_SECTIONS = (
    "action_panel",
    "price_trend",
    "price_signal",
    "data_source",
    "data_freshness",
    "quota_overview",
    "provenance",
)

SECTION_EVIDENCE = {
    "action_panel": ("行动面板",),
    "primary_plan": ("首选推荐", "首选方案"),
    "alternative_plans": ("可选备选方案",),
    "excluded_plans": ("为什么不推荐更便宜方案", "展开:排除方案"),
    "price_trend": ("价格走势",),
    "price_signal": ("价格信号",),
    "data_source": ("数据来源", "详细数据来源"),
    "data_freshness": ("数据时点:", "采集时间:"),
    "quota_overview": ("[配额总览]",),
    "provenance": ("数据依据",),
    "mixed_cabin": ("经济舱 / 商务舱并列参考", "混舱报价"),
}

SECTION_FALLBACKS = {
    "alternative_plans": "本轮没有可组成完整往返的备选方案。",
    "excluded_plans": "本轮未保留可组成完整往返的结构化排除候选。",
    "price_trend": "同条件历史样本不足，继续积累中，暂不给出价格走势判断。",
    "price_signal": "同条件历史样本不足，继续积累中，暂不给出价格位置判断。",
    "data_source": "本轮数据来源明细不足。",
    "data_freshness": "本轮采集时点未记录。",
    "quota_overview": "本地配额台账暂不可读。",
    "provenance": "本次未引用历史统计值。",
    "mixed_cabin": "混舱报价信息不足，暂不能形成可订组合价。",
}


def _is_no_match(trigger_type: str | None) -> bool:
    value = str(trigger_type or "").strip().lower()
    return value in {
        "no_match",
        "no_result",
        "无符合方案",
        "无符合方案·备选参考",
    } or "无符合方案" in value


def _is_data_incomplete(trigger_type: str | None) -> bool:
    value = str(trigger_type or "").strip().lower()
    return value in {"data_incomplete", "数据不完整"} or "数据不完整" in value


def canonical_sections(trigger_type: str | None, *, mixed_cabin: bool = False) -> tuple[str, ...]:
    """返回指定触发类型的 canonical 小节，顺序即通知结构顺序。"""
    if _is_data_incomplete(trigger_type):
        sections = list(DATA_INCOMPLETE_SECTIONS)
    else:
        sections = list(NO_MATCH_SECTIONS if _is_no_match(trigger_type) else STANDARD_SECTIONS)
    if mixed_cabin:
        sections.insert(2, "mixed_cabin")
    return tuple(sections)


def section_fallback(section: str, reason: str | None = None) -> str:
    """生成带原因的诚实降级行。"""
    base = SECTION_FALLBACKS.get(section, "本节数据不足。")
    reason_text = str(reason or "").strip()
    return f"{base} 原因={reason_text}" if reason_text else base


def _visible_text(*rendered_parts: str) -> str:
    source = " ".join(str(part or "") for part in rendered_parts)
    source = re.sub(r"<script\b[^>]*>.*?</script>", " ", source, flags=re.I | re.S)
    source = re.sub(r"<style\b[^>]*>.*?</style>", " ", source, flags=re.I | re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", source))).strip()


def missing_notification_sections(
    email_html: str,
    detail_html: str,
    *,
    trigger_type: str | None,
    mixed_cabin: bool = False,
) -> list[str]:
    """按用户可见文本检查邮件+详情组成的通知包是否缺 canonical 小节。"""
    visible = _visible_text(email_html, detail_html)
    missing = []
    for section in canonical_sections(trigger_type, mixed_cabin=mixed_cabin):
        evidence = SECTION_EVIDENCE[section]
        if not any(token in visible for token in evidence):
            missing.append(section)
    return missing
