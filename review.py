import json
from pathlib import Path


SIGNALS_LOG = Path(__file__).parent / "data" / "signals_history.jsonl"


def load_signals():
    if not SIGNALS_LOG.exists():
        return []

    signals = []
    with open(SIGNALS_LOG, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if line:
                signals.append(json.loads(line))
    return signals


def run_review():
    """Review how prices moved after each recorded signal."""
    signals = load_signals()

    if not signals:
        print("暂无信号记录")
        return

    print(f"共 {len(signals)} 条信号记录")
    print("")

    signals.sort(key=lambda item: item.get("timestamp", ""))

    reviewed = 0
    correct = 0
    incorrect = 0

    for index in range(len(signals) - 1):
        current = signals[index]
        next_signal = signals[index + 1]

        price_now = current.get("current_min_price", 0)
        price_later = next_signal.get("current_min_price", 0)

        if not price_now or not price_later:
            continue

        change = price_later - price_now
        change_pct = round(change / price_now * 100, 1) if price_now > 0 else 0

        waiting_risk = current.get("waiting_risk") or {}
        up_prob = waiting_risk.get("up_prob", 50)

        if up_prob and up_prob > 50:
            was_correct = change > 0
        elif up_prob and up_prob < 50:
            was_correct = change < 0
        else:
            was_correct = None

        if was_correct is True:
            correct += 1
        elif was_correct is False:
            incorrect += 1

        reviewed += 1

        timestamp = current.get("timestamp", "")[:16]
        confidence = current.get("confidence") or {}
        confidence_level = confidence.get("level", "?")
        status = "✅" if was_correct else ("❌" if was_correct is False else "➖")

        print(
            f"{timestamp} | ¥{price_now:,.0f} → ¥{price_later:,.0f} "
            f"({change:+,.0f}, {change_pct:+.1f}%) | "
            f"置信度:{confidence_level} | {status}"
        )

    print("")
    print("━━━ 复盘总结 ━━━")
    print(f"已复盘：{reviewed}条")
    if reviewed > 0:
        accuracy = round(correct / reviewed * 100)
        print(f"判断正确：{correct}条")
        print(f"判断错误：{incorrect}条")
        print(f"准确率：{accuracy}%")

        if accuracy >= 65:
            print("📊 评估：系统判断可靠性较高")
        elif accuracy >= 50:
            print("📊 评估：系统判断可靠性一般，需要更多数据")
        else:
            print("📊 评估：系统判断可靠性较低，需要调整分析逻辑")


if __name__ == "__main__":
    run_review()
