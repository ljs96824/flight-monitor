import ast
import itertools
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RETIRED_LIVE_TEST_MODULES = frozenset({"test_email.py", "test_full.py"})
ZERO_LIVE_ENTRYPOINT_DEBT = frozenset()
HTTP_METHODS = frozenset(
    {
        "delete",
        "get",
        "head",
        "options",
        "patch",
        "post",
        "put",
        "request",
        "send",
        "stream",
        "urlopen",
        "urlretrieve",
    }
)
HTTP_CLIENT_CONSTRUCTORS = frozenset(
    {
        "httpx.AsyncClient",
        "httpx.Client",
        "requests.Session",
        "requests.session",
        "requests.sessions.Session",
    }
)


def _call_path(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_path(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


class _ScopeVisitor(ast.NodeVisitor):
    def visit_FunctionDef(self, node):
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            if default:
                self.visit(default)
        for argument in [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]:
            if argument.annotation:
                self.visit(argument.annotation)
        if node.args.vararg and node.args.vararg.annotation:
            self.visit(node.args.vararg.annotation)
        if node.args.kwarg and node.args.kwarg.annotation:
            self.visit(node.args.kwarg.annotation)
        if node.returns:
            self.visit(node.returns)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            if default:
                self.visit(default)

    def visit_ClassDef(self, node):
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        for statement in node.body:
            self.visit(statement)


class _CallCollector(_ScopeVisitor):
    def __init__(self):
        self.calls = []

    def visit_Call(self, node):
        self.calls.append(node)
        self.generic_visit(node)


class _AliasCollector(_ScopeVisitor):
    def __init__(self):
        self.aliases = {}

    def visit_Import(self, node):
        for alias in node.names:
            first = alias.name.split(".")[0]
            self.aliases[alias.asname or first] = alias.name if alias.asname else first

    def visit_ImportFrom(self, node):
        if not node.module:
            return
        for alias in node.names:
            self.aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"


class _BoundNameCollector(ast.NodeVisitor):
    def __init__(self):
        self.names = set()

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Store):
            self.names.add(node.id)

    def visit_Import(self, node):
        for alias in node.names:
            self.names.add(alias.asname or alias.name.split(".")[0])

    def visit_ImportFrom(self, node):
        for alias in node.names:
            self.names.add(alias.asname or alias.name)

    def visit_FunctionDef(self, node):
        self.names.add(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            if default:
                self.visit(default)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.names.add(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Lambda(self, node):
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            if default:
                self.visit(default)


class _AssignmentAliasCollector(_ScopeVisitor):
    def __init__(self, aliases):
        self.aliases = dict(aliases)

    def _record(self, targets, value):
        if not isinstance(value, (ast.Name, ast.Attribute)):
            return
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Call):
            constructor = _resolved_call_path(value.value.func, self.aliases)
            resolved = f"{constructor}.{value.attr}" if constructor else ""
        else:
            resolved = _resolved_call_path(value, self.aliases)
        if not resolved:
            return
        for target in targets:
            for name in _target_names(target):
                self.aliases[name] = resolved

    def visit_Assign(self, node):
        self._record(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self._record([node.target], node.value)
        self.generic_visit(node)


class _AggregatorBindingCollector(_ScopeVisitor):
    def __init__(self, aliases):
        self.aliases = aliases
        self.bindings = set()

    def visit_Assign(self, node):
        if isinstance(node.value, ast.Call):
            resolved = _resolved_call_path(node.value.func, self.aliases)
            if resolved.rsplit(".", 1)[-1] == "FlightAggregator":
                self.bindings.update(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if isinstance(node.target, ast.Name) and isinstance(node.value, ast.Call):
            resolved = _resolved_call_path(node.value.func, self.aliases)
            if resolved.rsplit(".", 1)[-1] == "FlightAggregator":
                self.bindings.add(node.target.id)
        self.generic_visit(node)


class _HttpClientBindingCollector(_ScopeVisitor):
    def __init__(self, aliases):
        self.aliases = aliases
        self.bindings = {}
        self.events = []
        self.branch_stack = []
        self.call_paths = {}

    def _record(self, targets, value, event_node):
        resolved = None
        if isinstance(value, ast.Call):
            candidate = _resolved_call_path(value.func, self.aliases)
            if candidate in HTTP_CLIENT_CONSTRUCTORS:
                resolved = candidate
        for target in targets:
            for name in _target_names(target):
                self.events.append(
                    (
                        event_node.lineno,
                        event_node.col_offset,
                        len(self.events),
                        tuple(self.branch_stack),
                        name,
                        resolved,
                    )
                )
                if resolved:
                    self.bindings[name] = resolved
                else:
                    self.bindings.pop(name, None)

    def visit_Assign(self, node):
        self._record(node.targets, node.value, node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self._record([node.target], node.value, node)
        self.generic_visit(node)

    def visit_With(self, node):
        for item in node.items:
            if item.optional_vars is not None:
                self._record([item.optional_vars], item.context_expr, item.context_expr)
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    def visit_If(self, node):
        self.visit(node.test)
        branch_id = (node.lineno, node.col_offset)
        self.branch_stack.append((branch_id, True))
        for statement in node.body:
            self.visit(statement)
        self.branch_stack.pop()
        self.branch_stack.append((branch_id, False))
        for statement in node.orelse:
            self.visit(statement)
        self.branch_stack.pop()

    def visit_Call(self, node):
        self.call_paths[id(node)] = tuple(self.branch_stack)
        self.generic_visit(node)


class _CallableCollector(ast.NodeVisitor):
    def __init__(self, prefix=""):
        self.prefix = prefix
        self.callables = {}
        self.class_bases = {}

    def _record(self, name, node):
        display_name = f"{self.prefix}.{name}" if self.prefix else name
        self.callables[name] = (display_name, node)

    def visit_FunctionDef(self, node):
        self._record(node.name, node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.class_bases[node.name] = [
            _call_path(base).rsplit(".", 1)[-1]
            for base in node.bases
            if _call_path(base)
        ]
        for statement in node.body:
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            method_name = f"{node.name}.{statement.name}"
            self._record(method_name, statement)

    def visit_Assign(self, node):
        if isinstance(node.value, ast.Lambda):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._record(target.id, node.value)
            return
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if isinstance(node.target, ast.Name) and isinstance(node.value, ast.Lambda):
            self._record(node.target.id, node.value)
            return
        self.generic_visit(node)

    def visit_Lambda(self, node):
        return


class _InstanceBindingCollector(_ScopeVisitor):
    def __init__(self, aliases, class_names):
        self.aliases = aliases
        self.class_names = class_names
        self.bindings = {}

    def _record(self, targets, value):
        if not isinstance(value, ast.Call):
            return
        resolved = _resolved_call_path(value.func, self.aliases)
        if resolved not in self.class_names:
            return
        for target in targets:
            for name in _target_names(target):
                self.bindings[name] = resolved

    def visit_Assign(self, node):
        self._record(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self._record([node.target], node.value)
        self.generic_visit(node)


def _visit_nodes(visitor, nodes):
    for node in nodes:
        visitor.visit(node)
    return visitor


def _target_names(target):
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.List, ast.Tuple)):
        return [
            name
            for child in target.elts
            for name in _target_names(child)
        ]
    return []


def _bound_names(nodes):
    return _visit_nodes(_BoundNameCollector(), nodes).names


def _parameter_names(function):
    arguments = function.args
    names = {
        argument.arg
        for argument in [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]
    }
    if arguments.vararg:
        names.add(arguments.vararg.arg)
    if arguments.kwarg:
        names.add(arguments.kwarg.arg)
    return names


def _aliases_for(nodes, inherited=None, shadowed=frozenset()):
    locally_bound = _bound_names(nodes) | set(shadowed)
    aliases = {
        name: target
        for name, target in (inherited or {}).items()
        if name not in locally_bound
    }
    aliases.update(_visit_nodes(_AliasCollector(), nodes).aliases)
    return _visit_nodes(_AssignmentAliasCollector(aliases), nodes).aliases


def _calls_for(nodes):
    return _visit_nodes(_CallCollector(), nodes).calls


def _resolved_call_path(function, aliases):
    raw = _call_path(function)
    if not raw:
        return ""
    first, dot, remainder = raw.partition(".")
    target = aliases.get(first, first)
    return f"{target}.{remainder}" if dot else target


def _aggregator_bindings(nodes, aliases):
    return _visit_nodes(_AggregatorBindingCollector(aliases), nodes).bindings


def _http_client_bindings(nodes, aliases):
    return _visit_nodes(_HttpClientBindingCollector(aliases), nodes).bindings


def _http_client_bindings_at(nodes, aliases, inherited, call):
    collector = _visit_nodes(_HttpClientBindingCollector(aliases), nodes)
    call_position = (call.lineno, call.col_offset)
    events = [
        event
        for event in collector.events
        if (event[0], event[1]) <= call_position
    ]
    fixed_choices = dict(collector.call_paths.get(id(call), ()))
    branch_ids = sorted(
        {
            branch_id
            for _line, _column, _sequence, branch_path, _name, _resolved in events
            for branch_id, _side in branch_path
            if branch_id not in fixed_choices
        }
    )
    states = []
    for sides in itertools.product((False, True), repeat=len(branch_ids)):
        choices = {**fixed_choices, **dict(zip(branch_ids, sides))}
        state = dict(inherited)
        for (
            _line,
            _column,
            _sequence,
            branch_path,
            name,
            resolved,
        ) in sorted(events, key=lambda event: event[:3]):
            if any(choices.get(branch_id) != side for branch_id, side in branch_path):
                continue
            if resolved:
                state[name] = resolved
            else:
                state.pop(name, None)
        states.append(state)

    possible = {}
    for state in states or [dict(inherited)]:
        for name, resolved in state.items():
            possible.setdefault(name, set()).add(resolved)
    return {
        name: sorted(resolved_values)[0]
        for name, resolved_values in possible.items()
    }


def _callables_for(nodes, prefix=""):
    collector = _visit_nodes(_CallableCollector(prefix), nodes)
    callables = dict(collector.callables)
    changed = True
    while changed:
        changed = False
        for class_name, base_names in collector.class_bases.items():
            for base_name in base_names:
                method_prefix = f"{base_name}."
                for method_name, (_display_name, function) in list(callables.items()):
                    if not method_name.startswith(method_prefix):
                        continue
                    inherited_name = f"{class_name}.{method_name[len(method_prefix):]}"
                    if inherited_name in callables:
                        continue
                    display_name = (
                        f"{prefix}.{inherited_name}"
                        if prefix
                        else inherited_name
                    )
                    callables[inherited_name] = (display_name, function)
                    changed = True
    return callables


def _class_names(callables):
    return {
        name.split(".", 1)[0]
        for name in callables
        if "." in name
    }


def _instance_bindings(nodes, aliases, callables):
    return _visit_nodes(
        _InstanceBindingCollector(aliases, _class_names(callables)),
        nodes,
    ).bindings


def _callable_body(node):
    return [node.body] if isinstance(node, ast.Lambda) else node.body


def _resolved_callable_target(call, aliases, callables, instance_bindings):
    resolved = _resolved_call_path(call.func, aliases)
    if resolved in callables:
        return resolved

    receiver, _, method = resolved.rpartition(".")
    if receiver in instance_bindings:
        candidate = f"{instance_bindings[receiver]}.{method}"
        if candidate in callables:
            return candidate

    if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Call):
        constructor = _resolved_call_path(call.func.value.func, aliases)
        candidate = f"{constructor}.{call.func.attr}"
        if candidate in callables:
            return candidate
    return None


def _classified_live_call(
    call,
    aliases,
    aggregator_bindings=frozenset(),
    http_client_bindings=None,
):
    http_client_bindings = http_client_bindings or {}
    resolved = _resolved_call_path(call.func, aliases)
    basename = resolved.rsplit(".", 1)[-1]

    basename_calls = {
        "SMTP": "SMTP",
        "SMTP_SSL": "SMTP_SSL",
        "cached_fetch": "cached_fetch",
        "collect_and_classify": "collect_and_classify",
        "init_db": "init_db",
        "load_dotenv": "load_dotenv",
        "send_email": "send_email",
    }
    if basename in basename_calls:
        return basename_calls[basename]

    exact = {
        "sys.stdout.reconfigure": "sys.stdout.reconfigure",
        "sys.stderr.reconfigure": "sys.stderr.reconfigure",
    }
    if resolved in exact:
        return exact[resolved]
    if resolved == "GoogleSearch" or resolved.endswith(".GoogleSearch"):
        return "network:GoogleSearch"
    if resolved == "app.run" or resolved.endswith(".app.run"):
        return "app.run"
    if basename == "fetch":
        return "source.fetch"

    receiver, _, method = resolved.rpartition(".")
    if method == "collect" and receiver in aggregator_bindings:
        return "FlightAggregator.collect"
    if (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "collect"
        and isinstance(call.func.value, ast.Call)
        and _resolved_call_path(call.func.value.func, aliases).endswith(".FlightAggregator")
    ):
        return "FlightAggregator.collect"

    if basename in HTTP_METHODS and any(
        resolved.startswith(f"{module}.")
        for module in ("httpx", "requests", "urllib.request")
    ):
        return f"network:{resolved}"
    if method in HTTP_METHODS and receiver in http_client_bindings:
        return f"network:{http_client_bindings[receiver]}.{method}"
    if (
        isinstance(call.func, ast.Attribute)
        and call.func.attr in HTTP_METHODS
        and isinstance(call.func.value, ast.Call)
    ):
        constructor = _resolved_call_path(call.func.value.func, aliases)
        if constructor in HTTP_CLIENT_CONSTRUCTORS:
            return f"network:{constructor}.{call.func.attr}"
    return None


def _is_main_guard(node):
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    values = [node.test.left, *node.test.comparators]
    return (
        len(node.test.ops) == 1
        and isinstance(node.test.ops[0], ast.Eq)
        and any(isinstance(value, ast.Name) and value.id == "__name__" for value in values)
        and any(
            isinstance(value, ast.Constant) and value.value == "__main__"
            for value in values
        )
    )


def _definition_expressions(node):
    expressions = [*node.decorator_list]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        expressions.extend(node.args.defaults)
        expressions.extend(default for default in node.args.kw_defaults if default)
        expressions.extend(
            argument.annotation
            for argument in [
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ]
            if argument.annotation
        )
        if node.args.vararg and node.args.vararg.annotation:
            expressions.append(node.args.vararg.annotation)
        if node.args.kwarg and node.args.kwarg.annotation:
            expressions.append(node.args.kwarg.annotation)
        if node.returns:
            expressions.append(node.returns)
    elif isinstance(node, ast.ClassDef):
        expressions.extend(node.bases)
        expressions.extend(keyword.value for keyword in node.keywords)
        expressions.extend(node.body)
    return expressions


def _context_fingerprint(context):
    aliases, aggregators, http_clients, instances = context
    return (
        tuple(sorted(aliases.items())),
        tuple(sorted(aggregators)),
        tuple(sorted(http_clients.items())),
        tuple(sorted(instances.items())),
    )


def _bind_call_arguments(context, function, call, caller_aliases):
    aliases, aggregators, http_clients, instances = context
    bound_aliases = dict(aliases)
    positional = [*function.args.posonlyargs, *function.args.args]
    pairs = list(zip(positional, call.args))
    keyword_parameters = {
        argument.arg: argument
        for argument in [*positional, *function.args.kwonlyargs]
    }
    pairs.extend(
        (keyword_parameters[keyword.arg], keyword.value)
        for keyword in call.keywords
        if keyword.arg in keyword_parameters
    )
    for parameter, argument in pairs:
        if not isinstance(argument, (ast.Name, ast.Attribute)):
            continue
        resolved = _resolved_call_path(argument, caller_aliases)
        if resolved:
            bound_aliases[f"__argument__:{parameter.arg}"] = resolved
    return bound_aliases, aggregators, http_clients, instances


def _function_live_calls(
    callables,
    entrypoint,
    callable_contexts,
):
    pending = [(entrypoint, callables, dict(callable_contexts))]
    visited = set()
    violations = set()
    while pending:
        function_key, scope_callables, scope_contexts = pending.pop()
        display_name, function = scope_callables[function_key]
        (
            scope_aliases,
            scope_aggregators,
            scope_http_clients,
            scope_instances,
        ) = scope_contexts[function_key]
        visit_key = (
            id(function),
            _context_fingerprint(scope_contexts[function_key]),
        )
        if visit_key in visited:
            continue
        visited.add(visit_key)
        body = _callable_body(function)
        parameters = _parameter_names(function)
        shadowed = _bound_names(body) | parameters
        argument_aliases = {
            name.removeprefix("__argument__:"): target
            for name, target in scope_aliases.items()
            if name.startswith("__argument__:")
        }
        lexical_aliases = {
            name: target
            for name, target in scope_aliases.items()
            if not name.startswith("__argument__:")
        }
        aliases = _aliases_for(body, lexical_aliases, parameters)
        aliases.update(argument_aliases)
        aggregators = {
            name for name in scope_aggregators if name not in shadowed
        } | _aggregator_bindings(body, aliases)
        base_http_clients = {
            name: client
            for name, client in scope_http_clients.items()
            if name not in shadowed
        }
        visible_callables = {
            **scope_callables,
            **_callables_for(body, display_name),
        }
        local_callable_names = set(visible_callables) - set(scope_callables)
        instances = {
            **{
                name: class_name
                for name, class_name in scope_instances.items()
                if name not in shadowed
            },
            **_instance_bindings(body, aliases, visible_callables),
        }
        positional = [*function.args.posonlyargs, *function.args.args]
        if "." in function_key and positional:
            instances[positional[0].arg] = function_key.split(".", 1)[0]

        default_context = (
            aliases,
            aggregators,
            _http_client_bindings_at(
                body,
                aliases,
                base_http_clients,
                function,
            ),
            instances,
        )
        visible_contexts = {
            **scope_contexts,
            **{
                name: default_context
                for name in local_callable_names
            },
        }
        for call in _calls_for(body):
            http_clients = _http_client_bindings_at(
                body,
                aliases,
                base_http_clients,
                call,
            )
            call_context = (aliases, aggregators, http_clients, instances)
            next_contexts = {
                **visible_contexts,
                **{
                    name: call_context
                    for name in local_callable_names
                },
            }
            target = _resolved_callable_target(
                call,
                aliases,
                visible_callables,
                instances,
            )
            if target:
                next_contexts[target] = _bind_call_arguments(
                    next_contexts[target],
                    visible_callables[target][1],
                    call,
                    aliases,
                )
                pending.append((target, visible_callables, next_contexts))
            if isinstance(call.func, ast.Lambda):
                lambda_key = f"<lambda:{call.lineno}:{call.col_offset}>"
                lambda_callables = {
                    **visible_callables,
                    lambda_key: (f"{display_name}.<lambda>", call.func),
                }
                lambda_contexts = {
                    **next_contexts,
                    lambda_key: call_context,
                }
                pending.append(
                    (lambda_key, lambda_callables, lambda_contexts)
                )
            prohibited = _classified_live_call(
                call,
                aliases,
                aggregators,
                http_clients,
            )
            if prohibited:
                violations.add((display_name, prohibited))
    return violations


def scan_test_module_live_entrypoints(source, path="<memory>"):
    tree = ast.parse(source, filename=path)
    aliases = _aliases_for(tree.body)
    callables = _callables_for(tree.body)
    violations = set()
    module_execution_nodes = []
    for statement in tree.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)) or _is_main_guard(statement):
            continue
        module_execution_nodes.extend(
            _definition_expressions(statement)
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            else [statement]
        )
    module_aliases = _aliases_for(module_execution_nodes, aliases)
    module_aggregators = _aggregator_bindings(module_execution_nodes, module_aliases)
    module_http_clients = _http_client_bindings(module_execution_nodes, module_aliases)
    module_instances = _instance_bindings(
        module_execution_nodes,
        module_aliases,
        callables,
    )
    module_context = (
        module_aliases,
        module_aggregators,
        module_http_clients,
        module_instances,
    )
    module_contexts = {name: module_context for name in callables}

    for statement in tree.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            continue
        if _is_main_guard(statement):
            guard_aliases = _aliases_for(statement.body, aliases)
            guard_bound = _bound_names(statement.body)
            inherited_guard_http = {
                name: client
                for name, client in module_http_clients.items()
                if name not in guard_bound
            }
            aggregators = {
                name for name in module_aggregators if name not in guard_bound
            } | _aggregator_bindings(
                statement.body,
                guard_aliases,
            )
            guard_locals = _callables_for(statement.body, "__main__")
            guard_callables = {**callables, **guard_locals}
            instances = {
                **{
                    name: class_name
                    for name, class_name in module_instances.items()
                    if name not in guard_bound
                },
                **_instance_bindings(statement.body, guard_aliases, guard_callables),
            }
            guard_context = (
                guard_aliases,
                aggregators,
                _http_client_bindings(statement.body, guard_aliases),
                instances,
            )
            guard_contexts = {
                **module_contexts,
                **{name: guard_context for name in guard_locals},
            }
            direct_live = set()
            for call in _calls_for(statement.body):
                http_at_call = _http_client_bindings_at(
                    statement.body,
                    guard_aliases,
                    inherited_guard_http,
                    call,
                )
                call_context = (
                    guard_aliases,
                    aggregators,
                    http_at_call,
                    instances,
                )
                call_contexts = {
                    **guard_contexts,
                    **{name: call_context for name in guard_locals},
                }
                target = _resolved_callable_target(
                    call,
                    guard_aliases,
                    guard_callables,
                    instances,
                )
                if target:
                    call_contexts[target] = _bind_call_arguments(
                        call_contexts[target],
                        guard_callables[target][1],
                        call,
                        guard_aliases,
                    )
                    reachable = _function_live_calls(
                        guard_callables,
                        target,
                        call_contexts,
                    )
                    if reachable:
                        violations.add(
                            (path, "__main__", f"custom-live-main:{target}")
                        )
                        violations.update(
                            (path, scope, prohibited)
                            for scope, prohibited in reachable
                        )
                if isinstance(call.func, ast.Lambda):
                    inline_callables = {"<lambda>": ("__main__.<lambda>", call.func)}
                    reachable = _function_live_calls(
                        inline_callables,
                        "<lambda>",
                        {"<lambda>": call_context},
                    )
                    if reachable:
                        violations.add(
                            (path, "__main__", "custom-live-main:<lambda>")
                        )
                        violations.update(
                            (path, scope, prohibited)
                            for scope, prohibited in reachable
                        )
                prohibited = _classified_live_call(
                    call,
                    guard_aliases,
                    aggregators,
                    http_at_call,
                )
                if prohibited:
                    direct_live.add(prohibited)
                    violations.add((path, "__main__", prohibited))
            if direct_live:
                violations.add((path, "__main__", "custom-live-main"))
            continue

        nodes = (
            _definition_expressions(statement)
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            else [statement]
        )
        for call in _calls_for(nodes):
            http_at_call = _http_client_bindings_at(
                module_execution_nodes,
                module_aliases,
                {},
                call,
            )
            call_context = (
                module_aliases,
                module_aggregators,
                http_at_call,
                module_instances,
            )
            call_contexts = {name: call_context for name in callables}
            target = _resolved_callable_target(
                call,
                module_aliases,
                callables,
                module_instances,
            )
            if target:
                call_contexts[target] = _bind_call_arguments(
                    call_contexts[target],
                    callables[target][1],
                    call,
                    module_aliases,
                )
                reachable = _function_live_calls(
                    callables,
                    target,
                    call_contexts,
                )
                if reachable:
                    violations.add(
                        (path, "<module>", f"module-live-helper:{target}")
                    )
                    violations.update(
                        (path, scope, prohibited)
                        for scope, prohibited in reachable
                    )
            if isinstance(call.func, ast.Lambda):
                inline_callables = {
                    "<lambda>": ("<module>.<lambda>", call.func)
                }
                reachable = _function_live_calls(
                    inline_callables,
                    "<lambda>",
                    {"<lambda>": call_context},
                )
                if reachable:
                    violations.add(
                        (path, "<module>", "module-live-helper:<lambda>")
                    )
                    violations.update(
                        (path, scope, prohibited)
                        for scope, prohibited in reachable
                    )
            prohibited = _classified_live_call(
                call,
                module_aliases,
                module_aggregators,
                http_at_call,
            )
            if prohibited:
                violations.add((path, "<module>", prohibited))

    return frozenset(violations)


def _tracked_test_modules():
    paths = subprocess.check_output(
        ["git", "ls-files", "*.py"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
    ).splitlines()
    return [
        ROOT / path
        for path in paths
        if Path(path).name.startswith("test_") and Path(path).suffix == ".py"
    ]


def scan_repository_test_module_live_entrypoints():
    violations = set()
    for path in _tracked_test_modules():
        relative = path.relative_to(ROOT).as_posix()
        if path.name in RETIRED_LIVE_TEST_MODULES:
            violations.add((relative, "tracked", "retired-live-test-module"))
        violations.update(
            scan_test_module_live_entrypoints(
                path.read_text(encoding="utf-8-sig"),
                relative,
            )
        )
    return frozenset(violations)


class TestModuleLiveEntrypointSafetyTest(unittest.TestCase):
    def test_repository_has_zero_test_module_live_entrypoint_debt(self):
        self.assertEqual(
            scan_repository_test_module_live_entrypoints(),
            ZERO_LIVE_ENTRYPOINT_DEBT,
        )

    def test_scanner_rejects_each_live_entrypoint_category_and_import_aliases(self):
        mutations = {
            "load_dotenv": (
                "from dotenv import load_dotenv as load\nload()\n",
                "load_dotenv",
            ),
            "stdout_reconfigure": (
                "import sys as system\nsystem.stdout.reconfigure(encoding='utf-8')\n",
                "sys.stdout.reconfigure",
            ),
            "stderr_reconfigure": (
                "from sys import stderr as error_stream\n"
                "error_stream.reconfigure(encoding='utf-8')\n",
                "sys.stderr.reconfigure",
            ),
            "init_db": (
                "from storage import init_db as initialize\ninitialize()\n",
                "init_db",
            ),
            "live_collection": (
                "from collector import collect_and_classify as collect\ncollect({})\n",
                "collect_and_classify",
            ),
            "cached_fetch": (
                "from request_cache import cached_fetch as fetch_cached\nfetch_cached(None)\n",
                "cached_fetch",
            ),
            "send_email": (
                "from email_notifier import send_email as deliver\ndeliver('x', 'y', 'z')\n",
                "send_email",
            ),
            "direct_send_email": (
                "def send_email(*args):\n    return False\n"
                "send_email('x', 'y', 'z')\n",
                "send_email",
            ),
            "smtp": (
                "import smtplib as smtp\nsmtp.SMTP_SSL('example.invalid')\n",
                "SMTP_SSL",
            ),
            "smtp_plain": (
                "from smtplib import SMTP as Client\nClient('example.invalid')\n",
                "SMTP",
            ),
            "app_run": (
                "from web_form import app\napp.run()\n",
                "app.run",
            ),
            "http": (
                "from httpx import get as download\ndownload('https://example.invalid')\n",
                "network:httpx.get",
            ),
            "requests_http": (
                "import requests as http\nhttp.post('https://example.invalid')\n",
                "network:requests.post",
            ),
            "requests_head": (
                "import requests\nrequests.head('https://example.invalid')\n",
                "network:requests.head",
            ),
            "requests_api_http": (
                "import requests.api as api\napi.get('https://example.invalid')\n",
                "network:requests.api.get",
            ),
            "requests_session_http": (
                "import requests\n"
                "requests.Session().get('https://example.invalid')\n",
                "network:requests.Session.get",
            ),
            "requests_session_send": (
                "import requests\nsession = requests.Session()\n"
                "session.send(None)\n",
                "network:requests.Session.send",
            ),
            "requests_session_factory": (
                "import requests\nsession = requests.session()\n"
                "session.get('https://example.invalid')\n",
                "network:requests.session.get",
            ),
            "requests_sessions_factory": (
                "import requests\nsession = requests.sessions.Session()\n"
                "session.get('https://example.invalid')\n",
                "network:requests.sessions.Session.get",
            ),
            "httpx_bound_client": (
                "import httpx\nclient = httpx.Client()\n"
                "client.get('https://example.invalid')\n",
                "network:httpx.Client.get",
            ),
            "httpx_call_before_rebind": (
                "import httpx\nfrom unittest.mock import Mock\n"
                "client = httpx.Client()\n"
                "client.get('https://example.invalid')\nclient = Mock()\n",
                "network:httpx.Client.get",
            ),
            "httpx_context_client": (
                "import httpx\nwith httpx.Client() as client:\n"
                "    client.get('https://example.invalid')\n",
                "network:httpx.Client.get",
            ),
            "urllib_http": (
                "import urllib.request\nurllib.request.urlopen('https://example.invalid')\n",
                "network:urllib.request.urlopen",
            ),
            "urllib_urlretrieve": (
                "import urllib.request\n"
                "urllib.request.urlretrieve('https://example.invalid')\n",
                "network:urllib.request.urlretrieve",
            ),
            "google_search": (
                "from serpapi import GoogleSearch as Search\nSearch({'q': 'test'})\n",
                "network:GoogleSearch",
            ),
            "source_fetch": (
                "source.fetch()\n",
                "source.fetch",
            ),
            "aggregator": (
                "from sources.aggregator import FlightAggregator as Aggregator\n"
                "aggregator = Aggregator([])\naggregator.collect('A', 'B', '2099-01-01')\n",
                "FlightAggregator.collect",
            ),
            "annotated_aggregator": (
                "from sources.aggregator import FlightAggregator\n"
                "aggregator: object = FlightAggregator([])\n"
                "aggregator.collect('A', 'B', '2099-01-01')\n",
                "FlightAggregator.collect",
            ),
            "test_method_default": (
                "from email_notifier import send_email as deliver\n"
                "class EmailTest:\n"
                "    def test_email(self, sent=deliver('x', 'y', 'z')):\n"
                "        return sent\n",
                "send_email",
            ),
            "test_method_annotation": (
                "from email_notifier import send_email as deliver\n"
                "def test_email(value: deliver('x', 'y', 'z')):\n"
                "    return value\n",
                "send_email",
            ),
        }
        for mutation_id, (source, violation) in mutations.items():
            with self.subTest(mutation_id=mutation_id):
                self.assertIn(
                    ("mutation.py", "<module>", violation),
                    scan_test_module_live_entrypoints(source, "mutation.py"),
                )

    def test_scanner_follows_main_helper_calls_with_import_aliases(self):
        source = """
from email_notifier import send_email as deliver

def helper():
    deliver("recipient", "subject", "body")

if __name__ == "__main__":
    helper()
"""
        self.assertEqual(
            scan_test_module_live_entrypoints(source, "mutation.py"),
            frozenset(
                {
                    ("mutation.py", "__main__", "custom-live-main:helper"),
                    ("mutation.py", "helper", "send_email"),
                }
            ),
        )

        import_source = """
from dotenv import load_dotenv as load_environment

def initialize():
    load_environment()

initialize()
"""
        self.assertEqual(
            scan_test_module_live_entrypoints(import_source, "mutation.py"),
            frozenset(
                {
                    ("mutation.py", "<module>", "module-live-helper:initialize"),
                    ("mutation.py", "initialize", "load_dotenv"),
                }
            ),
        )

        nested_source = """
from email_notifier import send_email

def main():
    def helper():
        send_email("recipient", "subject", "body")
    helper()

if __name__ == "__main__":
    main()
"""
        self.assertEqual(
            scan_test_module_live_entrypoints(nested_source, "mutation.py"),
            frozenset(
                {
                    ("mutation.py", "__main__", "custom-live-main:main"),
                    ("mutation.py", "main.helper", "send_email"),
                }
            ),
        )

        lambda_source = """
from email_notifier import send_email

deliver = lambda: send_email("recipient", "subject", "body")

if __name__ == "__main__":
    deliver()
"""
        self.assertEqual(
            scan_test_module_live_entrypoints(lambda_source, "mutation.py"),
            frozenset(
                {
                    ("mutation.py", "__main__", "custom-live-main:deliver"),
                    ("mutation.py", "deliver", "send_email"),
                }
            ),
        )

        inherited_cases = {
            "nested_outer_alias": (
                """
def main():
    from email_notifier import send_email as deliver
    def helper():
        deliver("recipient", "subject", "body")
    helper()

if __name__ == "__main__":
    main()
""",
                {
                    ("mutation.py", "__main__", "custom-live-main:main"),
                    ("mutation.py", "main.helper", "send_email"),
                },
            ),
            "nested_outer_http_client": (
                """
import httpx

def main():
    client = httpx.Client()
    def helper():
        client.get("https://example.invalid")
    helper()

if __name__ == "__main__":
    main()
""",
                {
                    ("mutation.py", "__main__", "custom-live-main:main"),
                    ("mutation.py", "main.helper", "network:httpx.Client.get"),
                },
            ),
            "nested_sibling_helper": (
                """
from email_notifier import send_email

def main():
    def deliver():
        send_email("recipient", "subject", "body")
    def helper():
        deliver()
    helper()

if __name__ == "__main__":
    main()
""",
                {
                    ("mutation.py", "__main__", "custom-live-main:main"),
                    ("mutation.py", "main.deliver", "send_email"),
                },
            ),
            "top_level_helper_keeps_module_aliases": (
                """
from email_notifier import send_email as action

def helper():
    action("recipient", "subject", "body")

def main():
    action = lambda *args: None
    helper()

if __name__ == "__main__":
    main()
""",
                {
                    ("mutation.py", "__main__", "custom-live-main:main"),
                    ("mutation.py", "helper", "send_email"),
                },
            ),
            "branch_may_hold_live_client": (
                """
import httpx
from unittest.mock import Mock

def main(flag):
    if flag:
        client = httpx.Client()
    else:
        client = Mock()
    client.get("https://example.invalid")

if __name__ == "__main__":
    main(True)
""",
                {
                    ("mutation.py", "__main__", "custom-live-main:main"),
                    ("mutation.py", "main", "network:httpx.Client.get"),
                },
            ),
            "same_helper_multiple_contexts": (
                """
import httpx
from unittest.mock import Mock

def main():
    client = httpx.Client()
    def helper():
        client.get("https://example.invalid")
    helper()
    client = Mock()
    helper()

if __name__ == "__main__":
    main()
""",
                {
                    ("mutation.py", "__main__", "custom-live-main:main"),
                    ("mutation.py", "main.helper", "network:httpx.Client.get"),
                },
            ),
        }
        for mutation_id, (source, expected) in inherited_cases.items():
            with self.subTest(mutation_id=mutation_id):
                self.assertEqual(
                    scan_test_module_live_entrypoints(source, "mutation.py"),
                    frozenset(expected),
                )

        callable_cases = {
            "class_helper": (
                """
from email_notifier import send_email

class Runner:
    def execute(self):
        send_email("recipient", "subject", "body")

if __name__ == "__main__":
    Runner().execute()
""",
                {
                    ("mutation.py", "__main__", "custom-live-main:Runner.execute"),
                    ("mutation.py", "Runner.execute", "send_email"),
                },
            ),
            "function_alias": (
                """
from email_notifier import send_email

def helper():
    send_email("recipient", "subject", "body")

run = helper
if __name__ == "__main__":
    run()
""",
                {
                    ("mutation.py", "__main__", "custom-live-main:helper"),
                    ("mutation.py", "helper", "send_email"),
                },
            ),
            "method_alias": (
                """
from email_notifier import send_email

class Runner:
    def execute(self):
        send_email("recipient", "subject", "body")

run = Runner().execute
if __name__ == "__main__":
    run()
""",
                {
                    ("mutation.py", "__main__", "custom-live-main:Runner.execute"),
                    ("mutation.py", "Runner.execute", "send_email"),
                },
            ),
            "method_to_method": (
                """
from email_notifier import send_email

class Runner:
    def execute(self):
        self.deliver()
    def deliver(self):
        send_email("recipient", "subject", "body")

if __name__ == "__main__":
    Runner().execute()
""",
                {
                    ("mutation.py", "__main__", "custom-live-main:Runner.execute"),
                    ("mutation.py", "Runner.deliver", "send_email"),
                },
            ),
            "inherited_method": (
                """
from email_notifier import send_email

class Base:
    def execute(self):
        send_email("recipient", "subject", "body")

class Runner(Base):
    pass

if __name__ == "__main__":
    Runner().execute()
""",
                {
                    ("mutation.py", "__main__", "custom-live-main:Runner.execute"),
                    ("mutation.py", "Runner.execute", "send_email"),
                },
            ),
            "callback_parameter": (
                """
from email_notifier import send_email

def invoke(callback):
    callback("recipient", "subject", "body")

if __name__ == "__main__":
    invoke(send_email)
""",
                {
                    ("mutation.py", "__main__", "custom-live-main:invoke"),
                    ("mutation.py", "invoke", "send_email"),
                },
            ),
            "immediate_lambda": (
                """
from email_notifier import send_email

if __name__ == "__main__":
    (lambda: send_email("recipient", "subject", "body"))()
""",
                {
                    ("mutation.py", "__main__", "custom-live-main:<lambda>"),
                    ("mutation.py", "__main__.<lambda>", "send_email"),
                },
            ),
            "async_context_client": (
                """
import asyncio
import httpx

async def main():
    async with httpx.AsyncClient() as client:
        await client.get("https://example.invalid")

if __name__ == "__main__":
    asyncio.run(main())
""",
                {
                    ("mutation.py", "__main__", "custom-live-main:main"),
                    ("mutation.py", "main", "network:httpx.AsyncClient.get"),
                },
            ),
        }
        for mutation_id, (source, expected) in callable_cases.items():
            with self.subTest(mutation_id=mutation_id):
                self.assertEqual(
                    scan_test_module_live_entrypoints(source, "mutation.py"),
                    frozenset(expected),
                )

    def test_scanner_allows_mocked_test_methods_and_unittest_main(self):
        source = """
import unittest
from unittest.mock import patch
from email_notifier import send_email

class EmailTest(unittest.TestCase):
    def test_mocked_email(self):
        with patch("email_notifier.smtplib.SMTP_SSL"):
            send_email("recipient", "subject", "body")

if __name__ == "__main__":
    unittest.main()
"""
        self.assertEqual(
            scan_test_module_live_entrypoints(source, "mutation.py"),
            frozenset(),
        )

        shadowed_client = """
import httpx
from unittest.mock import Mock

client = httpx.Client()

def main():
    client = Mock()
    client.get("https://example.invalid")

if __name__ == "__main__":
    main()
"""
        self.assertEqual(
            scan_test_module_live_entrypoints(shadowed_client, "mutation.py"),
            frozenset(),
        )

        same_scope_rebind = """
import httpx
from unittest.mock import Mock

def main():
    client = httpx.Client()
    client = Mock()
    client.get("https://example.invalid")

if __name__ == "__main__":
    main()
"""
        self.assertEqual(
            scan_test_module_live_entrypoints(same_scope_rebind, "mutation.py"),
            frozenset(),
        )

        caller_binding_does_not_leak = """
import httpx
from unittest.mock import Mock

client = Mock()

def helper():
    client.get("https://example.invalid")

def main():
    client = httpx.Client()
    helper()

if __name__ == "__main__":
    main()
"""
        self.assertEqual(
            scan_test_module_live_entrypoints(
                caller_binding_does_not_leak,
                "mutation.py",
            ),
            frozenset(),
        )


if __name__ == "__main__":
    unittest.main()
