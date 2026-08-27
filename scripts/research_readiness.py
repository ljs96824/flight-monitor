"""只读打印研究采样启用前的全部硬门。"""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import date
import io
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_readiness import render_readiness_summary  # noqa: E402
from config_loader import DEFAULT_CONFIG_PATH, RUNTIME_CONFIG_PATH  # noqa: E402
from scripts.research_quota_simulation import build_report  # noqa: E402
from subscription_preflight import shanghai_today  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-defaults", type=Path, default=DEFAULT_CONFIG_PATH
    )
    parser.add_argument(
        "--runtime-config", type=Path, default=RUNTIME_CONFIG_PATH
    )
    parser.add_argument("--state", type=Path, default=ROOT / "data" / "basket_state.json")
    parser.add_argument("--subscriptions", type=Path, default=ROOT / "data" / "subscriptions.json")
    parser.add_argument("--observations", type=Path, default=ROOT / "data" / "observations.sqlite3")
    parser.add_argument("--prices", type=Path, default=ROOT / "data" / "prices.db")
    parser.add_argument("--usage", type=Path, default=ROOT / "data" / "api_usage.json")
    parser.add_argument("--backup-status", type=Path, default=ROOT / "data" / "backup_status.json")
    parser.add_argument("--today", type=date.fromisoformat, default=None)
    parser.add_argument(
        "--scheduled-subscription-runs-per-day",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--other-non-subscription-calls-per-day",
        type=int,
        default=None,
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    diagnostics = io.StringIO()
    with redirect_stdout(diagnostics):
        report = build_report(
            today=args.today or shanghai_today(),
            config_path=args.config_defaults,
            runtime_config_path=args.runtime_config,
            state_path=args.state,
            subscriptions_path=args.subscriptions,
            observations_path=args.observations,
            prices_path=args.prices,
            usage_path=args.usage,
            backup_status_path=args.backup_status,
            scheduled_subscription_runs_per_day=(
                args.scheduled_subscription_runs_per_day
            ),
            other_non_subscription_calls_per_day=(
                args.other_non_subscription_calls_per_day
            ),
        )
    if diagnostics.getvalue():
        print(diagnostics.getvalue(), file=sys.stderr, end="")
    print(render_readiness_summary(report["hard_gate"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
