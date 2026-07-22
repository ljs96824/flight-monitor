"""只读列出无法由机场主字典解析的订阅。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from airports import location_error_message, resolve_location


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


def list_unresolvable(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("subscriptions.json 顶层必须是数组")

    rows = []
    for index, subscription in enumerate(payload):
        if not isinstance(subscription, dict):
            continue
        origin = _value(subscription, "origin") or ""
        destination = _value(subscription, "destination") or ""
        origin_info = resolve_location(origin)
        destination_info = resolve_location(destination)
        reasons = []
        if origin_info.get("type") == "unknown":
            reasons.append(location_error_message("origin", origin_info))
        if destination_info.get("type") == "unknown":
            reasons.append(location_error_message("destination", destination_info))
        if not reasons:
            continue
        rows.append(
            {
                "index": index,
                "name": _label(subscription, index),
                "origin": origin or "?",
                "destination": destination or "?",
                "reason": "；".join(reasons),
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        default=ROOT / "data" / "subscriptions.json",
    )
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    rows = list_unresolvable(args.path)
    for item in rows:
        print(
            f"_index={item['index']} name={item['name']} "
            f"航线={item['origin']}->{item['destination']} 原因={item['reason']}"
        )
    print(f"统计: 无法解析订阅={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
