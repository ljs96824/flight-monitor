"""Run Ruff F821 and require the repository to have zero findings."""

from __future__ import annotations

import argparse
import ast
from functools import lru_cache
import json
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Historical compatibility only. Enforcement deliberately does not read this value:
# undefined names may not be registered as debt after the zero-debt gate was adopted.
KNOWN_F821_DEBT = frozenset()


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


def enforce_zero_f821(findings) -> int:
    findings = frozenset(findings)
    if findings:
        print("[F821] zero-debt gate failed:\n" + _format_rows(findings))
        return 1
    print("[F821] zero-debt gate passed")
    return 0


def main(argv=None, *, root: Path = PROJECT_ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--print-current",
        action="store_true",
        help="Print the current F821 scope triples without enforcing the zero gate.",
    )
    args = parser.parse_args(argv)
    current = scan_f821(Path(root))
    if args.print_current:
        print(_format_rows(current))
        return 0
    return enforce_zero_f821(current)


if __name__ == "__main__":
    raise SystemExit(main())
