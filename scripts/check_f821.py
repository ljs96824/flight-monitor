"""Run Ruff F821 and enforce the repository's exact known-debt set."""

from __future__ import annotations

import argparse
import ast
from functools import lru_cache
import json
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Keys deliberately exclude line numbers so harmless line movement does not churn the
# contract. New or resolved triples must be reviewed and this set changed explicitly.
KNOWN_F821_DEBT = frozenset(
    {
        ("notifier.py", "_append_detailed_analysis_section", "_append_multi_window_analysis"),
        ("notifier.py", "_append_detailed_analysis_section", "_append_price_anomaly_lines"),
        ("notifier.py", "_append_detailed_analysis_section", "_append_price_references"),
        ("notifier.py", "_append_detailed_analysis_section", "_append_purchase_checklist"),
        ("notifier.py", "_append_detailed_analysis_section", "_append_system_health_lines"),
        ("notifier.py", "_append_round_trip_recommendations", "_round_trip_city_code"),
        ("notifier.py", "_append_round_trip_recommendations", "_round_trip_date_text"),
        ("notifier.py", "_booking_link", "_google_flights_url"),
        ("notifier.py", "_format_structured_html_message", "_append_low_option_count_notice"),
        ("notifier.py", "_format_structured_html_message", "_append_price_explanation_lines"),
        ("notifier.py", "_format_structured_html_message", "_append_push_trend_linechart"),
        ("notifier.py", "_round_trip_score_line", "_flight_slot_label"),
        ("notifier.py", "_round_trip_score_line", "_round_trip_time_range"),
        ("notifier.py", "generate_neutral_summary", "_plain_price_position"),
    }
)


def _scope_for_line(tree: ast.AST, line: int) -> str:
    scopes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.lineno <= line <= getattr(node, "end_lineno", node.lineno)
    ]
    if not scopes:
        return "<module>"
    scopes.sort(key=lambda node: (node.lineno, -getattr(node, "end_lineno", node.lineno)))
    return ".".join(node.name for node in scopes)


@lru_cache(maxsize=4)
def scan_f821(root: Path = PROJECT_ROOT) -> frozenset[tuple[str, str, str]]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            ".",
            "--select",
            "F821",
            "--output-format",
            "json",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if completed.returncode not in {0, 1} or not completed.stdout.strip():
        raise RuntimeError(f"Ruff F821 scan failed: {completed.stderr.strip()}")
    issues = json.loads(completed.stdout or "[]")
    trees: dict[Path, ast.AST] = {}
    findings = set()
    for issue in issues:
        path = Path(issue["filename"]).resolve()
        if path not in trees:
            trees[path] = ast.parse(path.read_text(encoding="utf-8"))
        tree = trees[path]
        message = str(issue["message"])
        symbol = message.split("`", 2)[1]
        findings.add(
            (
                path.relative_to(root.resolve()).as_posix(),
                _scope_for_line(tree, int(issue["location"]["row"])),
                symbol,
            )
        )
    return frozenset(findings)


def _format_rows(rows) -> str:
    return "\n".join(f"  {path} | {scope} | {symbol}" for path, scope, symbol in sorted(rows))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--print-current",
        action="store_true",
        help="Print the current F821 scope triples without enforcing the debt contract.",
    )
    args = parser.parse_args(argv)
    current = scan_f821()
    if args.print_current:
        print(_format_rows(current))
        return 0
    added = current - KNOWN_F821_DEBT
    resolved = KNOWN_F821_DEBT - current
    if added or resolved:
        if added:
            print("[F821] unregistered findings:\n" + _format_rows(added))
        if resolved:
            print("[F821] resolved debt still registered:\n" + _format_rows(resolved))
        return 1
    print(f"[F821] exact debt matched: {len(current)} scope triples")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
