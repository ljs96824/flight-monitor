"""只读列出仍使用 total 预算口径的订阅。"""

from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUBSCRIPTIONS_PATH = PROJECT_ROOT / "data" / "subscriptions.json"
SCOPE_FIELDS = ("budget_scope", "max_budget_scope", "target_price_scope")
TOTAL_SCOPE_VALUES = {"total", "all", "all_passenger", "all_passengers", "overall"}


def _scope_value(subscription: dict, field: str):
    value = subscription.get(field)
    if value not in (None, ""):
        return value
    for section_name in ("preferences", "hard_constraints", "constraints", "basic"):
        section = subscription.get(section_name)
        if isinstance(section, dict) and section.get(field) not in (None, ""):
            return section.get(field)
    return None


def list_scope_total_subscriptions(path: str | Path = DEFAULT_SUBSCRIPTIONS_PATH) -> list[dict]:
    source_path = Path(path)
    try:
        subscriptions = json.loads(source_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(subscriptions, list):
        raise ValueError("subscriptions.json 顶层必须是数组")

    items = []
    for index, subscription in enumerate(subscriptions):
        if not isinstance(subscription, dict):
            continue
        scopes = {field: _scope_value(subscription, field) for field in SCOPE_FIELDS}
        if not any(
            str(value or "").strip().lower() in TOTAL_SCOPE_VALUES
            for value in scopes.values()
        ):
            continue
        basic = subscription.get("basic") or {}
        origin = subscription.get("origin") or basic.get("origin") or "?"
        destination = subscription.get("destination") or basic.get("destination") or "?"
        items.append(
            {
                "_index": index,
                "name": subscription.get("name") or subscription.get("id") or f"订阅{index}",
                "route": f"{origin}->{destination}",
                **scopes,
            }
        )
    return items


def render_report(items: list[dict]) -> None:
    for item in items:
        print(
            f"_index={item['_index']} name={item['name']} 航线={item['route']} "
            f"budget_scope={item.get('budget_scope')} "
            f"max_budget_scope={item.get('max_budget_scope')} "
            f"target_price_scope={item.get('target_price_scope')}"
        )
    print(f"统计: 整单口径订阅={len(items)}")


def main() -> int:
    render_report(list_scope_total_subscriptions())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
