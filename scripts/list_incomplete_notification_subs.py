"""只读列出 UX 一期后通知渠道残缺或需人工复核的订阅。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from notification_config import VALID_NOTIFICATION_METHODS  # noqa: E402
DEFAULT_SUBSCRIPTIONS_PATH = PROJECT_ROOT / "data" / "subscriptions.json"
PHASE_ONE_DEPLOYED_AT = datetime.fromisoformat("2026-08-12T19:40:39+08:00")
SHANGHAI_TZ = timezone(timedelta(hours=8))


def _parse_timestamp(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed


def _route_label(subscription: dict) -> str:
    basic = subscription.get("basic") if isinstance(subscription.get("basic"), dict) else {}
    origin = basic.get("origin") or subscription.get("origin") or "?"
    destination = basic.get("destination") or subscription.get("destination") or "?"
    return f"{origin}->{destination}"


def scan_notification_config_issues(
    path: str | Path = DEFAULT_SUBSCRIPTIONS_PATH,
    *,
    since: datetime = PHASE_ONE_DEPLOYED_AT,
) -> list[dict]:
    """扫描问题但绝不修改订阅文件。"""

    source = Path(path)
    if not source.exists():
        return []
    records = json.loads(source.read_text(encoding="utf-8"))
    issues = []
    for position, subscription in enumerate(records):
        if not isinstance(subscription, dict):
            continue
        created_at = _parse_timestamp(subscription.get("created_at"))
        if created_at is None or created_at < since:
            continue
        goals = subscription.get("notification_goals")
        goals = goals if isinstance(goals, dict) else {}
        method = str(goals.get("method") or "").strip().lower()
        has_email = bool(str(goals.get("email") or "").strip())
        reasons = []
        if not method:
            reasons.append("method缺失")
        elif method not in VALID_NOTIFICATION_METHODS:
            reasons.append(f"method非法({method})")
        if method in {"email", "both"} and not has_email:
            reasons.append("邮件渠道已启用但邮箱缺失")
        if method == "pushplus":
            reasons.append("UX一期后保存为pushplus，可能由旧默认写入，需人工复核")
        if not reasons:
            continue
        issues.append(
            {
                "_index": subscription.get("_index", position),
                "name": subscription.get("name") or "",
                "route": _route_label(subscription),
                "created_at": subscription.get("created_at") or "",
                "method": method or "缺失",
                "email": "有" if has_email else "无",
                "issue": "；".join(reasons),
            }
        )
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default=str(DEFAULT_SUBSCRIPTIONS_PATH))
    parser.add_argument("--since", default=PHASE_ONE_DEPLOYED_AT.isoformat())
    args = parser.parse_args()
    since = _parse_timestamp(args.since)
    if since is None:
        parser.error("--since 必须是 ISO 时间")
    issues = scan_notification_config_issues(args.path, since=since)
    for item in issues:
        print(
            f"_index={item['_index']} name={item['name'] or '-'} "
            f"航线={item['route']} created_at={item['created_at']} "
            f"method={item['method']} email={item['email']} 问题={item['issue']}"
        )
    print(f"[通知配置体检] 起点={since.isoformat()} 需复核={len(issues)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
