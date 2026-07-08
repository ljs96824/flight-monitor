import os
import sys
from datetime import datetime

from review import load_signals


def _safe_print(text):
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="strict")
        except Exception:
            pass
    print(str(text))


def system_health_check():
    """系统自检"""
    issues = []

    signals = load_signals()
    if signals:
        last = signals[-1]
        last_time = datetime.fromisoformat(last["timestamp"])
        hours_ago = (datetime.now() - last_time).total_seconds() / 3600
        if hours_ago > 8:
            issues.append(f"⚠️ 上次采集在{hours_ago:.0f}小时前，可能采集中断")
    else:
        issues.append("⚠️ 无任何采集记录")

    if len(signals) >= 2:
        recent = signals[-1].get("total_options", 0)
        prev = signals[-2].get("total_options", 0)
        if recent == 0:
            issues.append("⚠️ 最近一次采集返回0个方案，API可能有问题")
        elif prev > 0 and recent < prev * 0.5:
            issues.append(f"⚠️ 方案数从{prev}骤降到{recent}，需要检查")

    for key_name in ["SERPAPI_KEY", "SEARCHAPI_KEY"]:
        value = os.environ.get(key_name, "")
        if not value:
            issues.append(f"⚠️ {key_name}未配置")

    if not issues:
        _safe_print("✅ 系统运行正常")
    else:
        for issue in issues:
            _safe_print(issue)

    return issues


if __name__ == "__main__":
    system_health_check()
