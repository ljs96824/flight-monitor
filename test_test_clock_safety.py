import ast
import unittest
from dataclasses import dataclass, fields
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DYNAMIC_CLOCK_CALLS = frozenset(
    {
        "datetime.date.today",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "time.time",
    }
)


@dataclass(frozen=True, order=True)
class ClockCall:
    file: str
    scope: str
    resolved_call: str


@dataclass(frozen=True)
class ClockAllowance:
    file: str
    scope: str
    resolved_call: str
    reason: str

    @property
    def call(self):
        return ClockCall(self.file, self.scope, self.resolved_call)


HOST_CLOCK_ALLOWLIST = frozenset(
    {
        ClockAllowance(
            "test_availability_freshness.py",
            "AvailabilityFreshnessTest.test_recent_collected_at_produces_small_age",
            "datetime.datetime.now",
            "elapsed_time_dependency: the fixture must remain two minutes behind the code under test",
        ),
        ClockAllowance(
            "test_collection_context_singleflight.py",
            "CollectionSingleflightTest.test_stale_unlocked_metadata_is_taken_over_and_logged",
            "datetime.datetime.now",
            "elapsed_time_dependency: stale lock metadata is intentionally relative to lock acquisition time",
        ),
        ClockAllowance(
            "test_collection_context_singleflight.py",
            "CollectionSingleflightTest.test_released_metadata_is_not_reported_as_stale_takeover",
            "datetime.datetime.now",
            "elapsed_time_dependency: released lock metadata is intentionally relative to lock inspection time",
        ),
        ClockAllowance(
            "test_collection_singleflight_review.py",
            "_hold_stale_collection_lock",
            "datetime.datetime.now",
            "elapsed_time_dependency: the cross-process stale holder must age relative to the reviewing process",
        ),
        ClockAllowance(
            "test_panel_reuse.py",
            "PanelReuseTest.test_seven_hour_old_panel_snapshot_falls_back_to_real_fetch",
            "datetime.datetime.now",
            "elapsed_time_dependency: the panel snapshot is deliberately older than the live TTL",
        ),
        ClockAllowance(
            "test_price_calendar.py",
            "PriceCalendarTest.test_update_calendar_queries_nearby_and_sample_dates_with_cache",
            "datetime.datetime.now",
            "timezone_boundary: fresh and stale timestamps are built in the project's Shanghai timezone",
        ),
        ClockAllowance(
            "test_request_cache.py",
            "RequestCacheTest.test_legacy_listing_cache_without_complete_flight_details_is_rejected",
            "datetime.datetime.now",
            "elapsed_time_dependency: the legacy cache row must be current while its schema is rejected",
        ),
    }
)


EXPECTED_HOST_CLOCK_VIOLATIONS = frozenset()


@dataclass
class _Scope:
    parent: object
    aliases: dict
    bindings: set
    conflicts: set


class _BindingCollector(ast.NodeVisitor):
    def __init__(self):
        self.aliases = {}
        self.bindings = set()
        self.conflicts = set()

    def _bind(self, name, alias=None):
        self.bindings.add(name)
        if alias is None:
            if name in self.aliases:
                self.conflicts.add(name)
            return
        previous = self.aliases.get(name)
        if previous is not None and previous != alias:
            self.conflicts.add(name)
        self.aliases[name] = alias

    def visit_Import(self, node):
        for item in node.names:
            self._bind(item.asname or item.name.split(".")[0], item.name)

    def visit_ImportFrom(self, node):
        if node.level or not node.module:
            for item in node.names:
                self._bind(item.asname or item.name)
            return
        for item in node.names:
            self._bind(item.asname or item.name, f"{node.module}.{item.name}")

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self._bind(node.id)

    def visit_FunctionDef(self, node):
        self._bind(node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self._bind(node.name)

    def visit_Lambda(self, node):
        return

    def visit_ListComp(self, node):
        return

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp


def _make_scope(body, parent, arguments=None):
    collector = _BindingCollector()
    for statement in body:
        collector.visit(statement)
    if arguments is not None:
        for argument in [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]:
            collector._bind(argument.arg)
        if arguments.vararg:
            collector._bind(arguments.vararg.arg)
        if arguments.kwarg:
            collector._bind(arguments.kwarg.arg)
    return _Scope(
        parent=parent,
        aliases=collector.aliases,
        bindings=collector.bindings,
        conflicts=collector.conflicts,
    )


def _dotted_name(node):
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return [*parent, node.attr] if parent else None
    return None


def _resolve_call(parts, scope):
    if not parts:
        return None
    first = parts[0]
    current = scope
    while current is not None:
        if first in current.conflicts:
            return None
        if first in current.aliases:
            return ".".join([current.aliases[first], *parts[1:]])
        if first in current.bindings:
            return None
        current = current.parent
    return ".".join(parts)


class _HostClockVisitor(ast.NodeVisitor):
    def __init__(self, relative_path, tree):
        self.relative_path = relative_path
        self.scope = _make_scope(tree.body, None)
        self.display_scope = []
        self.calls = set()

    def visit_Call(self, node):
        resolved = _resolve_call(_dotted_name(node.func), self.scope)
        if resolved in DYNAMIC_CLOCK_CALLS:
            self.calls.add(
                ClockCall(
                    self.relative_path,
                    ".".join(self.display_scope) or "<module>",
                    resolved,
                )
            )
        self.generic_visit(node)

    def _visit_outer_function_expressions(self, node):
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)
        if node.returns:
            self.visit(node.returns)

    def visit_FunctionDef(self, node):
        self._visit_outer_function_expressions(node)
        previous_scope = self.scope
        previous_display = self.display_scope
        parent = previous_scope
        if previous_display and previous_display[-1].startswith("class:"):
            parent = previous_scope.parent
        self.scope = _make_scope(node.body, parent, node.args)
        name = node.name
        if previous_display and previous_display[-1].startswith("class:"):
            name = f"{previous_display[-1][6:]}.{name}"
            self.display_scope = [*previous_display[:-1], name]
        else:
            self.display_scope = [*previous_display, name]
        for statement in node.body:
            self.visit(statement)
        self.scope = previous_scope
        self.display_scope = previous_display

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        previous_scope = self.scope
        previous_display = self.display_scope
        self.scope = _make_scope(node.body, previous_scope)
        self.display_scope = [*previous_display, f"class:{node.name}"]
        for statement in node.body:
            self.visit(statement)
        self.scope = previous_scope
        self.display_scope = previous_display


def _scan_source(source, *, relative_path):
    tree = ast.parse(source, filename=relative_path)
    visitor = _HostClockVisitor(relative_path, tree)
    visitor.visit(tree)
    return frozenset(visitor.calls)


def scan_test_host_clocks(root=ROOT):
    findings = set()
    for path in sorted(root.rglob("test_*.py")):
        findings.update(
            _scan_source(
                path.read_text(encoding="utf-8-sig"),
                relative_path=path.relative_to(root).as_posix(),
            )
        )
    return frozenset(findings)


class TestClockSafetyContract(unittest.TestCase):
    def test_scanner_resolves_aliases_and_honors_local_shadowing(self):
        findings = _scan_source(
            """
import datetime as dt
from datetime import date as calendar_date
import time as clock
from time import time as epoch_time

def test_aliases():
    dt.datetime.now()
    calendar_date.today()
    clock.time()
    epoch_time()

def test_shadowed(date, datetime, time):
    date.today()
    datetime.now()
    time.time()

def test_assignment_shadow():
    calendar_date = object()
    calendar_date.today()
""",
            relative_path="test_example.py",
        )
        self.assertEqual(
            findings,
            frozenset(
                {
                    ClockCall("test_example.py", "test_aliases", "datetime.datetime.now"),
                    ClockCall("test_example.py", "test_aliases", "datetime.date.today"),
                    ClockCall("test_example.py", "test_aliases", "time.time"),
                }
            ),
        )

    def test_host_clock_allowlist_has_exact_auditable_schema(self):
        self.assertEqual(
            tuple(field.name for field in fields(ClockAllowance)),
            ("file", "scope", "resolved_call", "reason"),
        )
        self.assertTrue(HOST_CLOCK_ALLOWLIST)
        self.assertTrue(
            all(
                allowance.reason.startswith(
                    ("elapsed_time_dependency:", "timezone_boundary:")
                )
                for allowance in HOST_CLOCK_ALLOWLIST
            )
        )

    def test_repository_has_zero_unapproved_host_clock_dependencies(self):
        findings = scan_test_host_clocks()
        allowed = frozenset(allowance.call for allowance in HOST_CLOCK_ALLOWLIST)
        self.assertEqual(findings & allowed, allowed)
        expected_violation_set = EXPECTED_HOST_CLOCK_VIOLATIONS
        violations = findings - allowed
        self.assertEqual(violations, expected_violation_set)
        self.assertEqual(violations, frozenset())


if __name__ == "__main__":
    unittest.main()
