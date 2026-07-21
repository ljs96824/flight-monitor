"""只读列出所有采集日期均已过期的订阅。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from subscription_preflight import evaluate_subscription_preflight, shanghai_today


def _value(subscription: dict, key: str):
    value = subscription.get(key)
    if value not in (None, ""):
        return value
    for section_name in ("basic", "hard_constraints", "constraints"):
        section = subscription.get(section_name)
        if isinstance(section, dict) and section.get(key) not in (None, ""):
            return section[key]
    return None


def _label(subscription: dict, index: int) -> str:
    return str(subscription.get("name") or subscription.get("id") or index)


def list_expired(path: Path, *, today: date) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("subscriptions.json 顶层必须是数组")
    expired = []
    for index, subscription in enumerate(payload):
        if not isinstance(subscription, dict):
            continue
        result = evaluate_subscription_preflight(subscription, today=today)
        if not result.get("skip"):
            continue
        expired.append(
            {
                "index": index,
                "name": _label(subscription, index),
                "origin": _value(subscription, "origin") or "?",
                "destination": _value(subscription, "destination") or "?",
                "depart_date": _value(subscription, "depart_date") or "-",
                "return_date": _value(subscription, "return_date") or "-",
                "latest_date": result["latest_date"].isoformat(),
            }
        )
    return expired


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        default=ROOT / "data" / "subscriptions.json",
    )
    parser.add_argument("--today", type=date.fromisoformat, default=None)
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    effective_today = args.today or shanghai_today()
    expired = list_expired(args.path, today=effective_today)
    for item in expired:
        print(
            f"_index={item['index']} name={item['name']} "
            f"航线={item['origin']}->{item['destination']} "
            f"出发={item['depart_date']} 返程={item['return_date']} "
            f"最晚采集日期={item['latest_date']}"
        )
    print(f"统计: 过期订阅={len(expired)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
