"""Structure and construction contracts for registered active factory contexts.

The context-free factory and direct adapters remain compatible and callable.
AST structure does not prove runtime context validity. Controlled entrypoint
executions are supplemental evidence, not a claim about every possible call.
This is not universal unreachability or a process-wide network firewall.
"""

import ast
import builtins
from collections import Counter
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta
import importlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parent
ANCHOR_TODAY = date(2026, 8, 30)
# HasData is retired; the other three are inactive_by_profile, not identically retired.
OLD_ADAPTERS = frozenset({'hasdata', 'searchapi', 'travelpayouts', 'skyscanner'})
ADAPTERS = {
    'juhe': ('JuheSource', 'JUHE_FLIGHT_KEY'),
    'serpapi': ('SerpAPISource', 'SERPAPI_KEY'),
    'duffel': ('DuffelSource', 'DUFFEL_TOKEN'),
    'hasdata': ('HasDataSource', 'HASDATA_KEY'),
    'searchapi': ('SearchAPISource', 'SEARCHAPI_KEY'),
    'travelpayouts': ('TravelpayoutsSource', 'TRAVELPAYOUTS_TOKEN'),
    'skyscanner': ('SkyscannerSource', 'RAPIDAPI_KEY'),
}


@contextmanager
def _isolated_sources(testcase, key_mode='all'):
    """Patch before project import; counts remain observable if exceptions are caught."""
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            'NO_LIVE_API': '1', 'PYTHON_DOTENV_DISABLED': '1', 'MPLBACKEND': 'Agg',
        }, clear=True))
        dotenv = importlib.import_module('dotenv')
        stack.enter_context(patch.object(dotenv, 'load_dotenv', return_value=False))
        stack.enter_context(patch.object(dotenv, 'dotenv_values', return_value={}, create=True))
        stack.enter_context(patch('logging.basicConfig'))
        guards = {}
        forbidden_file = Mock()
        for owner in (builtins, io):
            original_open = owner.open

            def guarded_open(file, *args, _open=original_open, **kwargs):
                if isinstance(file, (str, bytes, os.PathLike)):
                    path = Path(os.fsdecode(file)).resolve()
                    if path.name == '.env' or path.is_relative_to(ROOT / 'data'):
                        forbidden_file()
                        raise AssertionError('fixture attempted repository data access')
                return _open(file, *args, **kwargs)

            stack.enter_context(patch.object(owner, 'open', side_effect=guarded_open))
        guards['repository_data_or_dotenv'] = forbidden_file
        for target in ('socket.socket.connect', 'socket.socket.connect_ex',
                       'socket.create_connection', 'socket.getaddrinfo', 'sqlite3.connect',
                       'smtplib.SMTP', 'smtplib.SMTP_SSL'):
            guards[target] = stack.enter_context(patch(target, side_effect=AssertionError('offline boundary')))
        for name in ('httpx', 'requests'):
            module = importlib.import_module(name)
            for method in ('get', 'post', 'request'):
                guards[name + '.' + method] = stack.enter_context(
                    patch.object(module, method, side_effect=AssertionError('offline HTTP'), create=True))
        constructors = {}
        fetch = Mock(side_effect=AssertionError('real fetch is outside this contract'))
        fake_modules = {}
        for name, (class_name, key) in ADAPTERS.items():
            constructors[name] = Mock(side_effect=lambda n=name: SimpleNamespace(
                name=n, supported_cabins=frozenset({'economy', 'business'}), fetch=fetch))
            fake_modules['sources.' + name + '_source'] = SimpleNamespace(**{class_name: constructors[name]})
            if name in OLD_ADAPTERS or key_mode == 'all' or (key_mode == 'partial' and name == 'juhe'):
                os.environ[key] = 'synthetic-source-factory-test-only'
        stack.enter_context(patch.dict(sys.modules, fake_modules))
        from serpapi_credentials import SERPAPI_KEY_ALIASES
        if key_mode != 'all':
            testcase.assertTrue(all(name not in os.environ for name in SERPAPI_KEY_ALIASES))
        from sources import aggregator
        stack.enter_context(patch.object(aggregator, 'safe_log'))
        try:
            yield SimpleNamespace(stack=stack, aggregator=aggregator, constructors=constructors, fetch=fetch)
        finally:
            testcase.assertEqual({name: mock.call_count for name, mock in guards.items()},
                                 {name: 0 for name in guards})
            testcase.assertEqual(fetch.call_count, 0)


def _old_constructor_violations(sandbox):
    return frozenset('old_adapter_constructed:' + name + ':' + str(mock.call_count)
                     for name in OLD_ADAPTERS
                     if (mock := sandbox.constructors[name]).call_count)


def _assert_old_not_constructed(testcase, sandbox):
    testcase.assertEqual(_old_constructor_violations(sandbox), frozenset())


def _subscription():
    return {'_index': 0, 'subscription_id': '11111111-1111-4111-8111-111111111111',
            'origin': 'PVG', 'destination': 'KIX', 'origin_airports_active': ['PVG'],
            'destination_airports_active': ['KIX'], 'route_type': 'international',
            'depart_date': (ANCHOR_TODAY + timedelta(days=30)).isoformat(),
            'round_trip': False, 'cabin_classes': ['economy', 'business']}


class ActiveFactoryMatrixTest(unittest.TestCase):
    def test_three_profiles_times_three_key_states_and_explicit_only_context(self):
        from source_profiles import ROUTE_SOURCE_PROFILES
        routes = {'domestic': ('PVG', 'PEK'), 'international': ('PVG', 'KIX'),
                  'greater_china': ('PVG', 'HKG')}
        for route_type, airports in routes.items():
            for mode in ('all', 'old_only', 'partial'):
                for context in ('airports', 'explicit_only'):
                    with self.subTest(route_type=route_type, keys=mode, context=context):
                        with _isolated_sources(self, mode) as sandbox:
                            origin, dest = airports if context == 'airports' else (None, None)
                            search, enrichment = sandbox.aggregator.build_default_sources(
                                origin, dest, route_type=route_type)
                            specs = [s for s in ROUTE_SOURCE_PROFILES[route_type]['sources']
                                     if os.environ.get(ADAPTERS[s['name']][1])]
                            self.assertEqual([s.name for s in search],
                                             [s['name'] for s in specs if s['role'] != 'enrichment'])
                            self.assertEqual([s.name for s in enrichment],
                                             [s['name'] for s in specs if s['role'] == 'enrichment'])
                            for source in search + enrichment:
                                spec = next(s for s in specs if s['name'] == source.name)
                                self.assertEqual((source.role, source.weight, source.route_type),
                                                 (spec['role'], float(spec['weight']), route_type))
                                self.assertEqual(source.supported_cabins,
                                                 frozenset(spec.get('cabins', ['economy', 'business'])))
                            expected_names = {s['name'] for s in specs}
                            self.assertEqual({n: c.call_count for n, c in sandbox.constructors.items()},
                                             {n: int(n in expected_names) for n in ADAPTERS})
                            _assert_old_not_constructed(self, sandbox)
                            if mode == 'old_only':
                                self.assertEqual((search, enrichment), ([], []))

    def test_empty_result_cannot_hide_an_old_constructor_call(self):
        for route_type in ('domestic', 'international', 'greater_china'):
            for name in sorted(OLD_ADAPTERS):
                with self.subTest(route_type=route_type, constructor=name):
                    with _isolated_sources(self, 'old_only') as sandbox:
                        # Mutation stays in a safe test double; the real factory is unchanged.
                        def incorrect_factory(*, route_type):
                            sandbox.constructors[name]()
                            return [], []

                        result = incorrect_factory(route_type=route_type)
                        self.assertEqual(result, ([], []))
                        marker = 'old_adapter_constructed:' + name + ':1'
                        self.assertEqual(_old_constructor_violations(sandbox), frozenset({marker}))
                        with self.assertRaisesRegex(AssertionError, marker):
                            _assert_old_not_constructed(self, sandbox)


class ActiveRootParametersTest(unittest.TestCase):
    """Supplemental controlled executions, not universal runtime-validity claims."""

    def _recorder(self, sandbox):
        calls = []
        real_factory = sandbox.aggregator.build_default_sources

        def recording(origin=None, dest=None, route_type=None):
            from source_profiles import normalize_route_type
            self.assertTrue((origin and dest) or normalize_route_type(route_type))
            result = real_factory(origin, dest, route_type=route_type)
            self.assertTrue(OLD_ADAPTERS.isdisjoint(s.name for group in result for s in group))
            calls.append((origin, dest, route_type))
            return result

        return calls, recording

    def _plans(self, sandbox, recording):
        import collection_plan
        plans = []
        real_build = collection_plan.build_collection_plan

        def build(**kwargs):
            kwargs['include_calendars'] = False
            kwargs['source_builder'] = recording
            plan = real_build(**kwargs)
            self.assertTrue(plan.source_counts)
            self.assertTrue(OLD_ADAPTERS.isdisjoint(plan.source_counts))
            plans.append(plan)
            return plan

        sandbox.stack.enter_context(patch.object(collection_plan.CollectionPlan, 'execute',
                                                return_value=SimpleNamespace(ledger_degraded=False, actual_requests=0, outcomes=())))
        sandbox.stack.enter_context(patch.object(collection_plan.CollectionPlan, 'log_summary'))
        return plans, build

    def test_plan_subscription_and_basket_missing_fields_skip_before_factory(self):
        with _isolated_sources(self) as sandbox:
            from collection_plan import build_collection_plan
            calls, recording = self._recorder(sandbox)
            sub = _subscription()
            basket = {'origin': 'PVG', 'dest': 'KIX', 'depart_date': sub['depart_date'],
                      'route_type': 'international', 'sources': ['hasdata', 'juhe']}
            # The no-source_builder path resolves its lazy import in this module at runtime.
            with patch.object(sandbox.aggregator, 'build_default_sources', side_effect=recording):
                plan = build_collection_plan(subscriptions=[sub], basket_requests=[basket],
                                             include_calendars=False)
            self.assertEqual(calls, [('PVG', 'KIX', 'international')] * 2)
            self.assertTrue(plan.source_counts)
            self.assertTrue(OLD_ADAPTERS.isdisjoint(plan.source_counts))
            for branch, record, fields in (('subscriptions', sub, ('origin', 'destination', 'depart_date')),
                                           ('basket_requests', basket, ('origin', 'dest', 'depart_date'))):
                for field in fields:
                    with self.subTest(branch=branch, missing=field):
                        invalid = deepcopy(record)
                        invalid.pop(field)
                        if field in ('origin', 'destination'):
                            invalid.pop(field + '_airports_active', None)
                        calls.clear()
                        empty = build_collection_plan(**{branch: [invalid]}, source_builder=recording,
                                                      include_calendars=False)
                        self.assertEqual(calls, [])
                        self.assertEqual(empty.source_counts, {})
            _assert_old_not_constructed(self, sandbox)

    def _main_boundary(self, sandbox):
        import main
        calls, recording = self._recorder(sandbox)
        plans, build = self._plans(sandbox, recording)
        stack = sandbox.stack
        for name in ('init_db', 'activate_collection_plan', 'deactivate_collection_plan',
                     'set_current_round', 'reset_current_round', 'start_request_cache_round',
                     'print_request_cache_stats', 'start_round_log_archive', 'end_round_log_archive',
                     '_run_basket_sentinel_for_main', 'log_retention_dry_run', 'record_subscription_attempt',
                     '_log_subscription_failure', '_notify_subscription_failure'):
            stack.enter_context(patch.object(main, name))
        for name, value in {'get_constraint_epoch_boundary': None, 'get_constraint_history_limit': 14,
                            '_collection_plan_log_options': {}, '_shanghai_today': ANCHOR_TODAY,
                            'acquire_collection_singleflight': Mock(acquired=True),
                            'load_file_subscriptions': [_subscription()],
                            'collect_for_airport_matrix': {'flights': []}}.items():
            stack.enter_context(patch.object(main, name, return_value=value))
        stack.enter_context(patch.object(main, 'build_default_sources', side_effect=recording))
        stack.enter_context(patch.object(main, 'build_collection_plan', side_effect=build))
        sinks = {}
        for name in ('send', 'send_email', 'persist_notification_payload', 'sync_subscriptions',
                     'save_roundtrip_snapshot', 'save_flight_details'):
            sinks[name] = stack.enter_context(patch.object(main, name, side_effect=AssertionError('unexpected sink')))
        return main, calls, plans, sinks

    def test_scheduled_run_reaches_real_locked_processor_with_route_context(self):
        with _isolated_sources(self) as sandbox:
            main, calls, plans, sinks = self._main_boundary(sandbox)
            main.run(sync_remote=False)
            self.assertEqual(calls, [('PVG', 'KIX', 'international')] * 2)
            self.assertEqual(len(plans), 1)
            self.assertEqual({n: m.call_count for n, m in sinks.items()}, dict.fromkeys(sinks, 0))
            _assert_old_not_constructed(self, sandbox)

    def test_web_worker_reaches_real_processor_and_is_joined_before_assertions(self):
        with _isolated_sources(self) as sandbox:
            _main, calls, plans, sinks = self._main_boundary(sandbox)
            import web_form
            sandbox.stack.enter_context(patch.object(web_form, '_record_last_attempt_safely', return_value=True))
            workers = []
            thread_class = threading.Thread

            def capture_thread(*args, **kwargs):
                worker = thread_class(*args, **kwargs)
                workers.append(worker)
                return worker

            with patch.object(web_form.threading, 'Thread', side_effect=capture_thread):
                try:
                    result = web_form.start_background_collection(_subscription(), timeout_seconds=2)
                finally:
                    for worker in workers:
                        worker.join(timeout=2)
                        self.assertFalse(worker.is_alive())
            self.assertEqual(result['status'], 'started')
            self.assertEqual(len(workers), 1)
            self.assertEqual(calls, [('PVG', 'KIX', 'international')] * 2)
            self.assertEqual(len(plans), 1)
            self.assertEqual({n: m.call_count for n, m in sinks.items()}, dict.fromkeys(sinks, 0))
            _assert_old_not_constructed(self, sandbox)

    def test_run_basket_legacy_and_cohort_route_context_and_available_only_selection(self):
        for strategy in ('legacy', 'cohort_v2'):
            with self.subTest(strategy=strategy), _isolated_sources(self) as sandbox, tempfile.TemporaryDirectory() as tmp:
                import basket_collect as basket
                calls, recording = self._recorder(sandbox)
                plans, build = self._plans(sandbox, recording)
                stack = sandbox.stack
                state = {'revision': 1, 'routes': {r['route']: {'A': _subscription()['depart_date'],
                                                              'B': _subscription()['depart_date']}
                                                  for r in basket.BASKET_ROUTES}}
                request = {'origin': 'PVG', 'dest': 'KIX', 'depart_date': _subscription()['depart_date'],
                           'route_type': 'international', 'sources': ['juhe'], 'cohort_id': 'synthetic-cohort'}
                settings = {'research_basket_enabled': True, 'research_basket_strategy': strategy,
                            'source_quota_budget': {}, 'source_quota_low_remaining_threshold': 0}
                values = {'load_collection_settings': settings, 'load_or_create_state': state,
                          'acquire_collection_singleflight': Mock(acquired=True),
                          'research_runtime_enabled': True, 'renew_expired_queues': [],
                          '_prepare_research_basket': (state, [request], {'ready': True}, {}),
                          '_persist_state': state, 'load_usage_strict': {}, 'usage_snapshot': {},
                          'count_observations_for_round': 0, 'apply_research_round_outcomes': []}
                for name, value in values.items():
                    stack.enter_context(patch.object(basket, name, return_value=value))
                for name in ('start_round_log_archive', 'end_round_log_archive', 'reset_request_cache',
                             'start_request_cache_round', 'set_current_round', 'reset_current_round',
                             'activate_collection_plan', 'deactivate_collection_plan',
                             'print_request_cache_stats', 'log_retention_dry_run'):
                    stack.enter_context(patch.object(basket, name))
                stack.enter_context(patch.object(basket, 'build_collection_plan', side_effect=build))
                selected = []

                def aggregate(search, enrichment, **kwargs):
                    selected.append(tuple(s.name for s in search))
                    self.assertEqual(enrichment, [])
                    self.assertTrue(OLD_ADAPTERS.isdisjoint(selected[-1]))
                    return SimpleNamespace(last_outcome_reads=0, collect_from_outcomes=Mock(return_value={'flights': []}))

                result = basket.run_basket(today=ANCHOR_TODAY, now=datetime(2026, 8, 30, 12),
                                          state_path=Path(tmp) / 'state.json', db_path=Path(tmp) / 'observations.sqlite3',
                                          usage_path=Path(tmp) / 'usage.json', source_builder=recording,
                                          aggregator_factory=aggregate, quota_guard_notifier=Mock())
                expected_routes = ([(r['origin'], r['dest'], r['route_type']) for r in basket.BASKET_ROUTES]
                                   if strategy == 'legacy' else [('PVG', 'KIX', 'international')])
                expected_calls = ([r for r in expected_routes for _ in range(2)] + expected_routes
                                  if strategy == 'legacy' else expected_routes * 2)
                self.assertEqual(calls, expected_calls)
                self.assertEqual(selected, [('juhe',)] * len(expected_routes))
                self.assertEqual(result['queues'], 6 if strategy == 'legacy' else 1)
                self.assertEqual(len(plans), 1)
                self.assertEqual(list(Path(tmp).iterdir()), [])
                _assert_old_not_constructed(self, sandbox)


FACTORY = 'sources.aggregator.build_default_sources'
PRIVATE_FACTORY = 'sources.aggregator._instantiate_source'
COMPATIBLE_WRAPPERS = frozenset('collector.' + name for name in (
    'get_aggregator', 'fetch_flights', 'collect_all_flights', 'collect_and_classify'))
CONSTRUCTORS = frozenset('sources.' + name + '_source.' + ADAPTERS[name][0]
                         for name in OLD_ADAPTERS)
RELATED = CONSTRUCTORS | COMPATIBLE_WRAPPERS | {FACTORY, PRIVATE_FACTORY}
RELATED_MODULES = frozenset(target.rsplit('.', 1)[0] for target in RELATED)


def _is_test_path(file):
    return 'tests' in Path(file).parts or Path(file).name.startswith('test_')


def _role(file, scope, target):
    if _is_test_path(file):
        return 'test_only'
    if file.startswith('scripts/'):
        return 'manual_or_script'
    if file == 'collector.py' or target in CONSTRUCTORS:
        return 'legacy_compatibility'
    return 'active_runtime'


def _factory_inventory(source, file):
    """Small symbol-specific inventory, not a general cross-module call graph."""
    tree = ast.parse(source, filename=file)
    records, unresolved = [], []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.names = {}
            self.scope = []

        def resolve(self, node):
            if isinstance(node, ast.Name):
                return self.names.get(node.id, node.id)
            if isinstance(node, ast.Attribute):
                return self.resolve(node.value) + '.' + node.attr
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'getattr':
                base = self.resolve(node.args[0]) if node.args else ''
                if base in RELATED_MODULES:
                    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                        return base + '.' + str(node.args[1].value)
                    return '<unresolved-related>'
            return ''

        def record(self, node, target, shape):
            scope = '.'.join(self.scope) or '<module>'
            records.append(((file, scope, target, shape, _role(file, scope, target)), node.lineno))

        def unknown(self, node):
            unresolved.append((file, '.'.join(self.scope) or '<module>', ast.unparse(node)))

        def visit_Import(self, node):
            for item in node.names:
                self.names[item.asname or item.name.split('.')[0]] = item.name if item.asname else item.name.split('.')[0]

        def visit_ImportFrom(self, node):
            for item in node.names:
                if item.name == '*' and node.module in RELATED_MODULES:
                    self.unknown(node)
                self.names[item.asname or item.name] = (node.module or '') + '.' + item.name

        def visit_FunctionDef(self, node):
            old = self.names.copy()
            self.scope.append(node.name)
            args = list(node.args.posonlyargs) + list(node.args.args)
            defaults = [None] * (len(args) - len(node.args.defaults)) + list(node.args.defaults)
            args += list(node.args.kwonlyargs)
            defaults += list(node.args.kw_defaults)
            for arg, default in zip(args, defaults):
                resolved = self.resolve(default)
                if arg.arg == 'source_builder' or resolved in RELATED:
                    self.record(node, resolved if resolved in RELATED else FACTORY,
                                'parameter:' + arg.arg + '=' + (ast.unparse(default) if default is not None else '<required>'))
                    self.names[arg.arg] = resolved if resolved in RELATED else FACTORY
                else:
                    self.names[arg.arg] = arg.arg
            for child in node.body:
                self.visit(child)
            self.scope.pop()
            self.names = old

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            self.scope.append(node.name)
            for child in node.body:
                self.visit(child)
            self.scope.pop()

        def visit_Assign(self, node):
            resolved = self.resolve(node.value)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.names[target.id] = resolved or target.id
                    if resolved in RELATED:
                        self.record(node, resolved, 'binding:' + target.id)
                    elif resolved == '<unresolved-related>':
                        self.unknown(node)
            self.generic_visit(node)

        def visit_Call(self, node):
            resolved = self.resolve(node.func)
            if resolved in RELATED:
                parts = [ast.unparse(arg) for arg in node.args]
                parts += [(kw.arg or '**') + '=' + ast.unparse(kw.value) for kw in node.keywords]
                self.record(node, resolved, 'call(' + ', '.join(parts) + ')')
            elif resolved == '<unresolved-related>':
                self.unknown(node)
            if self.resolve(node) == '<unresolved-related>':
                self.unknown(node)
            self.generic_visit(node)

    visitor = Visitor()
    if file == 'collector.py':
        visitor.names.update({name.rsplit('.', 1)[1]: name for name in COMPATIBLE_WRAPPERS})
    elif file == 'sources/aggregator.py':
        visitor.names.update({'_instantiate_source': PRIVATE_FACTORY, 'build_default_sources': FACTORY})
    visitor.visit(tree)
    return records, sorted(set(unresolved))


def _tracked_sources():
    paths = subprocess.check_output(['git', 'ls-files', '-z', '--', '*.py'], cwd=ROOT).decode().split('\0')
    return {p: (ROOT / p).read_text(encoding='utf-8-sig') for p in paths if p}


# Scope + resolved target + call shape + role are stable; lines are diagnostics only.
REGISTERED_CALLS = (
    ('main.py', '_process_subscription_locked', FACTORY, 'call(first_origin, first_dest, route_type=route_type)'),
    ('collector.py', 'get_aggregator', FACTORY, 'call(origin, dest, route_type=route_type)'),
    ('collector.py', 'fetch_flights', 'collector.get_aggregator', 'call(origin, dest)'),
    ('collector.py', 'collect_all_flights', 'collector.get_aggregator', 'call(origin, dest)'),
    ('collector.py', 'collect_and_classify', 'collector.get_aggregator', 'call(origin, dest)'),
    ('collection_plan.py', 'build_collection_plan', FACTORY, 'parameter:source_builder=None'),
    ('collection_plan.py', 'build_collection_plan', FACTORY, 'binding:source_builder'),
    ('collection_plan.py', 'build_collection_plan', FACTORY, 'call(origin, dest, route_type=route_type)'),
    ('collection_plan.py', 'build_collection_plan', FACTORY, 'call(origin, dest, route_type=route_type)'),
    ('basket_collect.py', '_build_route_aggregator', FACTORY, 'parameter:source_builder=<required>'),
    ('basket_collect.py', '_build_route_aggregator', FACTORY, "call(route['origin'], route['dest'], route_type=route['route_type'])"),
    ('basket_collect.py', '_simulate_runtime_quota', FACTORY, 'parameter:source_builder=<required>'),
    ('basket_collect.py', '_prepare_research_basket', FACTORY, 'parameter:source_builder=<required>'),
    ('basket_collect.py', 'run_basket', FACTORY, 'parameter:source_builder=build_default_sources'),
    ('basket_collect.py', '_run_basket_locked', FACTORY, 'parameter:source_builder=build_default_sources'),
    ('scripts/research_quota_simulation.py', '_build_report_inputs', FACTORY, 'parameter:source_builder=build_default_sources'),
    ('scripts/research_quota_simulation.py', 'build_report', FACTORY, 'parameter:source_builder=build_default_sources'),
    ('sources/aggregator.py', 'build_default_sources', PRIVATE_FACTORY, "call(spec.get('name'))"),
    ('sources/aggregator.py', 'FlightAggregator._ordered_search_sources', PRIVATE_FACTORY, "call('juhe')"),
) + tuple(('sources/aggregator.py', scope, target, 'call()')
          for scope in ('_instantiate_source', 'build_default_sources') for target in sorted(CONSTRUCTORS))
REGISTERED_INVENTORY = Counter((*row, _role(*row[:3])) for row in REGISTERED_CALLS)
PRIVATE_CALL_SCOPES = frozenset({
    ('sources/aggregator.py', 'build_default_sources'),
    ('sources/aggregator.py', 'FlightAggregator._ordered_search_sources'),
})


def _source_name_provenance(source):
    tree = ast.parse(source)
    factory = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'build_default_sources')
    profile_branch = next((n for n in factory.body if isinstance(n, ast.If)
                           and ast.unparse(n.test) == 'resolved_route_type'), None)
    if profile_branch is None:
        return False
    assignments = {ast.unparse(n.targets[0]): ast.unparse(n.value)
                   for n in profile_branch.body if isinstance(n, ast.Assign)}
    loops = [n for n in profile_branch.body if isinstance(n, ast.For)
             and ast.unparse(n.target) == 'spec' and ast.unparse(n.iter) == 'specs']
    stores = Counter(n.id for n in ast.walk(profile_branch) if isinstance(n, ast.Name)
                     and isinstance(n.ctx, ast.Store) and n.id in {'profile', 'specs', 'spec'})
    mutating_calls = [n for n in ast.walk(profile_branch) if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                      and n.func.value.id in {'profile', 'specs', 'spec'} and n.func.attr != 'get']
    subscript_writes = [n for n in ast.walk(profile_branch) if isinstance(n, ast.Subscript)
                        and isinstance(n.ctx, ast.Store) and isinstance(n.value, ast.Name)
                        and n.value.id in {'profile', 'specs', 'spec'}]
    return (assignments.get('profile') == 'get_source_profile(resolved_route_type)'
            and assignments.get('specs') == "list(profile.get('sources') or [])"
            and stores == Counter({'profile': 1, 'specs': 1, 'spec': 1})
            and not mutating_calls and not subscript_writes
            and len(loops) == 1
            and any(isinstance(n, ast.Call) and ast.unparse(n) == "_instantiate_source(spec.get('name'))"
                    for n in ast.walk(loops[0])))


def _violations(sources):
    actual = Counter()
    unknown = []
    for file, source in sources.items():
        records, unresolved = _factory_inventory(source, file)
        actual.update(row for row, _line in records if row[-1] != 'test_only')
        unknown.extend(item for item in unresolved if not _is_test_path(item[0]))
    violations = set()
    if actual != REGISTERED_INVENTORY:
        violations.add('factory_inventory_drift')
    if unknown:
        violations.add('unresolved_related_reference')
    private = {row[:2] for row in actual if row[2] == PRIVATE_FACTORY}
    if private != PRIVATE_CALL_SCOPES:
        violations.add('private_factory_scope_drift')
    injection = [row for row in actual if row[2] == PRIVATE_FACTORY
                 and row[1] == 'FlightAggregator._ordered_search_sources']
    if len(injection) != 1 or injection[0][3] != "call('juhe')" or actual[injection[0]] != 1:
        violations.add('dynamic_injection_not_literal_juhe')
    if not _source_name_provenance(sources['sources/aggregator.py']):
        violations.add('profile_name_provenance_changed')
    profile_tree = ast.parse(sources['source_profiles.py'])
    profile = next(ast.literal_eval(n.value) for n in profile_tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == 'ROUTE_SOURCE_PROFILES' for t in n.targets))
    if any(s['name'] in OLD_ADAPTERS for p in profile.values() for s in p['sources']):
        violations.add('old_adapter_enabled_by_profile')
    return frozenset(violations), actual, unknown


class SourceFactoryInventoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = _tracked_sources()

    def test_registered_inventory_and_private_scopes_are_exact(self):
        violations, actual, unknown = _violations(self.sources)
        self.assertEqual(unknown, [])
        self.assertEqual(actual, REGISTERED_INVENTORY)
        self.assertEqual(violations, frozenset())

    def test_permanent_bypass_mutations_have_exact_violations(self):
        inventory = 'factory_inventory_drift'
        cases = [
            ('new_context_free', 'main.py', '\ndef bypass():\n    build_default_sources()\n', {inventory}),
            ('explicit_empty_context', 'main.py', '\ndef bypass():\n    build_default_sources(None, None, route_type=None)\n', {inventory}),
            ('alias_context_free', 'main.py', '\ndef bypass():\n    from sources.aggregator import build_default_sources as factory\n    factory()\n', {inventory}),
            ('collector_bypass', 'main.py', '\ndef bypass():\n    from collector import get_aggregator as legacy\n    legacy()\n', {inventory}),
            ('private_bypass', 'main.py', '\ndef bypass():\n    from sources.aggregator import _instantiate_source as make\n    make("hasdata")\n', {inventory, 'private_factory_scope_drift'}),
            ('direct_constructor', 'main.py', '\ndef bypass():\n    from sources.hasdata_source import HasDataSource as Old\n    Old()\n', {inventory}),
            ('unknown_dynamic', 'main.py', '\ndef bypass(name):\n    from sources import aggregator\n    factory = getattr(aggregator, name)\n    factory()\n', {'unresolved_related_reference'}),
        ]
        for name, file, addition, expected in cases:
            with self.subTest(mutation=name):
                mutated = dict(self.sources)
                tree = ast.parse(mutated[file])
                target = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                              and n.name == '_process_subscription_locked')
                target.body.extend(ast.parse(addition).body[0].body)
                mutated[file] = ast.unparse(tree)
                found, actual, _unknown = _violations(mutated)
                self.assertEqual(found, frozenset(expected))
                if name == 'new_context_free':
                    extra = ('main.py', '_process_subscription_locked', FACTORY,
                             'call()', 'active_runtime')
                    self.assertEqual(actual - REGISTERED_INVENTORY, Counter({extra: 1}))
        for name, old, new, expected in (
            ('dynamic_old', '_instantiate_source("juhe")', '_instantiate_source("hasdata")',
             {inventory, 'dynamic_injection_not_literal_juhe'}),
            ('dynamic_variable', '_instantiate_source("juhe")', '_instantiate_source(source_name)',
             {inventory, 'dynamic_injection_not_literal_juhe'}),
            ('profile_origin', 'specs = list(profile.get("sources") or [])', 'specs = list(profile.get("retired_sources") or [])',
             {'profile_name_provenance_changed'}),
        ):
            with self.subTest(mutation=name):
                mutated = dict(self.sources)
                self.assertEqual(mutated['sources/aggregator.py'].count(old), 1)
                mutated['sources/aggregator.py'] = mutated['sources/aggregator.py'].replace(old, new)
                self.assertEqual(_violations(mutated)[0], frozenset(expected))

    def test_profile_reactivation_and_default_binding_changes_are_rejected(self):
        mutated = dict(self.sources)
        tree = ast.parse(mutated['source_profiles.py'])
        profiles = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == 'ROUTE_SOURCE_PROFILES' for t in n.targets))
        enabled = next(v for k, v in zip(profiles.values[0].keys, profiles.values[0].values)
                       if isinstance(k, ast.Constant) and k.value == 'sources')
        enabled.elts.append(ast.parse("{'name': 'hasdata', 'role': 'primary'}", mode='eval').body)
        mutated['source_profiles.py'] = ast.unparse(tree)
        self.assertEqual(_violations(mutated)[0], frozenset({'old_adapter_enabled_by_profile'}))
        mutated = dict(self.sources)
        mutated['basket_collect.py'] = mutated['basket_collect.py'].replace(
            'source_builder: Callable = build_default_sources', 'source_builder: Callable = None', 1)
        self.assertEqual(_violations(mutated)[0], frozenset({'factory_inventory_drift'}))

    def test_compatible_tests_comments_and_historical_names_are_not_active_roots(self):
        mutated = dict(self.sources)
        mutated['test_parser_only.py'] = ('from sources.travelpayouts_source import TravelpayoutsSource as Old\n'
                                          'def test_parser():\n    Old()\n')
        factory_test = ('from sources.aggregator import build_default_sources as factory\n'
                        'def fixture():\n    factory()\n')
        mutated['test_compatible_factory.py'] = factory_test
        mutated['tests/fixtures/compatible_factory.py'] = factory_test
        mutated['main.py'] += ('\n# build_default_sources()\n'
                               '"""_instantiate_source(\"hasdata\")"""\n'
                               'expected_sources = {"hasdata", "searchapi"}\n')
        violations, actual, _ = _violations(mutated)
        self.assertEqual(violations, frozenset())
        compatible = [row for row in actual if row[0] == 'collector.py']
        self.assertTrue(compatible)
        self.assertTrue(all(row[-1] == 'legacy_compatibility' for row in compatible))
        rows, unknown = _factory_inventory(mutated['test_parser_only.py'], 'test_parser_only.py')
        self.assertEqual(unknown, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0][-1], 'test_only')
        for file in ('test_compatible_factory.py', 'tests/fixtures/compatible_factory.py'):
            rows, unknown = _factory_inventory(mutated[file], file)
            self.assertEqual(unknown, [])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0][-1], 'test_only')
