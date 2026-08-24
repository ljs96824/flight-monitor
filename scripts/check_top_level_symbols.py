"""检查 Python 模块命名空间中的重复顶层符号。"""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


DEFINITION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


@dataclass(frozen=True)
class SymbolDefinition:
    name: str
    kind: str
    line: int


def _walk_module_statements(statements: list[ast.stmt]):
    """遍历模块级控制流，但不进入函数或类的局部作用域。"""
    for node in statements:
        if isinstance(node, DEFINITION_NODES):
            yield node
            continue
        if isinstance(node, ast.If):
            yield from _walk_module_statements(node.body)
            yield from _walk_module_statements(node.orelse)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            yield from _walk_module_statements(node.body)
            yield from _walk_module_statements(node.orelse)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            yield from _walk_module_statements(node.body)
        elif isinstance(node, (ast.Try, ast.TryStar)):
            yield from _walk_module_statements(node.body)
            for handler in node.handlers:
                yield from _walk_module_statements(handler.body)
            yield from _walk_module_statements(node.orelse)
            yield from _walk_module_statements(node.finalbody)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                yield from _walk_module_statements(case.body)


def duplicate_top_level_symbols(path: Path) -> dict[str, list[SymbolDefinition]]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    definitions: dict[str, list[SymbolDefinition]] = defaultdict(list)
    for node in _walk_module_statements(tree.body):
        definitions[node.name].append(
            SymbolDefinition(
                name=node.name,
                kind=type(node).__name__,
                line=node.lineno,
            )
        )
    return {
        name: rows
        for name, rows in definitions.items()
        if len(rows) > 1
    }


def scan_repository(root: Path) -> dict[tuple[str, str], list[SymbolDefinition]]:
    duplicates: dict[tuple[str, str], list[SymbolDefinition]] = {}
    skipped_parts = {".git", ".pytest_cache", "__pycache__", ".venv", "venv"}
    for path in sorted(root.rglob("*.py")):
        if any(part in skipped_parts for part in path.parts):
            continue
        relative = path.relative_to(root).as_posix()
        for name, rows in duplicate_top_level_symbols(path).items():
            duplicates[(relative, name)] = rows
    return duplicates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    duplicates = scan_repository(args.root.resolve())
    if not duplicates:
        print("重复顶层符号=0")
        return 0
    print(f"重复顶层符号={len(duplicates)}")
    for (path, name), rows in duplicates.items():
        locations = ", ".join(f"{row.kind}@{row.line}" for row in rows)
        print(f"{path} :: {name} :: {locations}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
