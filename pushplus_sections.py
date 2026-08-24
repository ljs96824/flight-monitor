"""PushPlus 结构化小节与整节降级工具。"""

from __future__ import annotations

import html
from dataclasses import dataclass
from urllib.parse import urlsplit


PUSHPLUS_MAX_CHARS = 30000
PUSHPLUS_COMPACT_CHARS = 25000
COMPACT_NOTICE = "完整方案已精简,请点击网页详情查看全部内容"


@dataclass(frozen=True)
class PushSection:
    section_id: str
    priority: int
    html: str
    mandatory: bool


@dataclass(frozen=True)
class PushRender:
    title: str
    sections: tuple[PushSection, ...]
    detail_url: str | None


@dataclass(frozen=True)
class PreparedPush:
    content: str
    kept_section_ids: tuple[str, ...]
    mode: str


def valid_detail_url(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return text


def detail_link_html(detail_url: str | None) -> str:
    url = valid_detail_url(detail_url)
    if not url:
        return (
            "更多完整分析见网页详情。"
            "网页详情未配置,完整结果见本通知"
        )
    escaped = html.escape(url, quote=True)
    return f'更多完整分析见网页详情:<a href="{escaped}" target="_blank">{escaped}</a>'


def render_push_render(render: PushRender) -> str:
    return "<br>".join(
        section.html
        for section in render.sections
        if section.html != ""
    )


def _compact_notice_section() -> PushSection:
    return PushSection("compact_notice", 0, COMPACT_NOTICE, True)


def _minimal_push_render(render: PushRender) -> PushRender:
    by_id = {section.section_id: section for section in render.sections}
    sections = []

    if "current_judgment" in by_id:
        sections.append(
            PushSection(
                "current_judgment",
                0,
                "当前判断:通知内容较长,请查看网页详情",
                True,
            )
        )
    price = by_id.get("current_price")
    if price is not None:
        price_html = price.html if len(price.html) <= 500 else "当前价:见网页详情"
        sections.append(PushSection("current_price", 0, price_html, True))
    if "purchase_condition" in by_id:
        sections.append(
            PushSection(
                "purchase_condition",
                0,
                "购买条件:以支付页最终价格、库存和票规为准",
                True,
            )
        )
    if "primary_plan" in by_id:
        sections.append(
            PushSection(
                "primary_plan",
                0,
                "首选方案概要:完整航班信息见网页详情",
                True,
            )
        )

    detail = by_id.get("detail_link")
    sections.append(
        PushSection(
            "detail_link",
            0,
            detail.html if detail is not None else detail_link_html(render.detail_url),
            True,
        )
    )
    freshness = by_id.get("data_freshness")
    if freshness is not None:
        freshness_html = (
            freshness.html if len(freshness.html) <= 500 else "数据时点:见网页详情"
        )
        sections.append(PushSection("data_freshness", 0, freshness_html, True))
    sections.append(
        PushSection(
            "disclaimer",
            0,
            "提示:最终价、库存、行李和票规以各平台支付页为准",
            True,
        )
    )
    return PushRender(render.title, tuple(sections), render.detail_url)


def prepare_push_render(
    render: PushRender,
    *,
    compact_chars: int = PUSHPLUS_COMPACT_CHARS,
    max_chars: int = PUSHPLUS_MAX_CHARS,
) -> PreparedPush:
    full_content = render_push_render(render)
    if len(full_content) <= compact_chars:
        return PreparedPush(
            full_content,
            tuple(section.section_id for section in render.sections if section.html != ""),
            "full",
        )

    kept = list(render.sections)
    removed_any = False
    for priority in (3, 2, 1):
        next_kept = [
            section
            for section in kept
            if not (section.priority == priority and not section.mandatory)
        ]
        if len(next_kept) == len(kept):
            continue
        removed_any = True
        kept = next_kept
        candidate = PushRender(
            render.title,
            tuple([*kept, _compact_notice_section()]),
            render.detail_url,
        )
        content = render_push_render(candidate)
        if len(content) <= max_chars:
            return PreparedPush(
                content,
                tuple(section.section_id for section in candidate.sections),
                "compact",
            )

    if removed_any:
        candidate = PushRender(
            render.title,
            tuple([*kept, _compact_notice_section()]),
            render.detail_url,
        )
        content = render_push_render(candidate)
        if len(content) <= max_chars:
            return PreparedPush(
                content,
                tuple(section.section_id for section in candidate.sections),
                "compact",
            )

    minimal = _minimal_push_render(render)
    content = render_push_render(minimal)
    return PreparedPush(
        content,
        tuple(section.section_id for section in minimal.sections),
        "minimal",
    )
