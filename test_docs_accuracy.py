import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from serpapi_credentials import SERPAPI_KEY_ALIASES


ROOT = Path(__file__).resolve().parent
README = ROOT / "README.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
ENV_EXAMPLE = ROOT / ".env.example"
LICENSE = ROOT / "LICENSE"
REQUIREMENTS_INPUT = ROOT / "requirements.in"
DEV_REQUIREMENTS_INPUT = ROOT / "requirements-dev.in"
DEV_REQUIREMENTS_LOCK = ROOT / "requirements-dev.txt"
RUNTIME_BACKUP_MANUAL = ROOT / "docs" / "runtime-backup-and-restore.md"
EXTERNAL_NETWORK_COVERAGE = (
    ROOT / "docs" / "external-network-no-live-api-coverage-2026-09-03.md"
)

EXPECTED_SECTIONS = (
    "定位",
    "设计哲学",
    "功能清单",
    "架构",
    "数据源与配额经济学",
    "快速开始",
    "日常运行",
    "工程纪律",
    "目录导览",
    "限制与非目标",
)

def _environment_contract(
    usage_class: str,
    documentation_target: str,
    effective_source_by_entrypoint: dict[str, str],
    repository_read_status: str,
    external_consumer_status: str,
    rationale: str,
) -> dict[str, object]:
    return {
        "usage_class": usage_class,
        "documentation_target": documentation_target,
        "effective_source_by_entrypoint": effective_source_by_entrypoint,
        "repository_read_status": repository_read_status,
        "external_consumer_status": external_consumer_status,
        "rationale": rationale,
    }


# This mapping is the sole hand-maintained environment-variable fact source.
ENVIRONMENT_CONTRACTS = {
    "AGREEMENT_WINDOW_DAYS": _environment_contract(
        "runtime_tuning", "dotenv_active",
        {"provenance import": "process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "Cross-source agreement lookback.",
    ),
    "ALLOW_PUBLIC_WEB_BIND": _environment_contract(
        "security_process_only", "readme_process_only",
        {"run_web.main": "process_environment_before_dotenv"},
        "active", "user_configurable", "Explicit consent for a non-loopback Web bind.",
    ),
    "BASKET_SENTINEL_AFTER": _environment_contract(
        "runtime_tuning", "dotenv_active",
        {"main": "process_environment_after_project_dotenv"},
        "active", "user_configurable", "Basket sentinel schedule override.",
    ),
    "BROWSER_PATH": _environment_contract(
        "tooling", "dotenv_active", {"scripts.ui_smoke": "process_environment"},
        "current_script", "user_configurable", "Explicit browser binary for UI smoke.",
    ),
    "CHECK_INTERVAL_HOURS": _environment_contract(
        "runtime_tuning", "dotenv_active",
        {"notifier": "process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "Notification interval context.",
    ),
    "COLLECTION_LOCK_PATH": _environment_contract(
        "runtime_path", "dotenv_active",
        {"collection_singleflight": "process_environment_after_primary_project_dotenv"},
        "active", "user_configurable", "Shared collection lock location.",
    ),
    "COLLECTION_STARTUP_TIMEOUT_SECONDS": _environment_contract(
        "runtime_tuning", "dotenv_active",
        {"web_form import": "process_environment_after_project_dotenv"},
        "active", "user_configurable", "Web startup handshake timeout.",
    ),
    "CSRF_TOKEN_TTL_SECONDS": _environment_contract(
        "security_runtime", "dotenv_active",
        {"web_form": "process_environment_after_project_dotenv"},
        "active", "user_configurable", "CSRF token lifetime.",
    ),
    "DUFFEL_TOKEN": _environment_contract(
        "credential", "dotenv_active",
        {"standard collection entrypoints": "process_environment_after_project_dotenv"},
        "active", "user_configurable", "Duffel enrichment credential.",
    ),
    "EDGE_PATH": _environment_contract(
        "tooling", "dotenv_active", {"scripts.ui_smoke": "process_environment"},
        "current_script", "user_configurable", "Windows-compatible browser override alias.",
    ),
    "FEEDBACK_NOTIFY_EMAIL": _environment_contract(
        "runtime_notification", "dotenv_active",
        {"web_form": "process_environment_after_project_dotenv"},
        "active", "user_configurable", "Feedback notification recipient.",
    ),
    "FLASK_SECRET_KEY": _environment_contract(
        "credential", "dotenv_active",
        {"web_form": "process_environment_after_project_dotenv"},
        "active", "user_configurable", "Persistent Flask session secret.",
    ),
    "FLIGHT_DEBUG_FULL_ARRAYS": _environment_contract(
        "diagnostic", "dotenv_active",
        {"analyzer": "process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "Opt-in verbose flight diagnostics.",
    ),
    "HASDATA_KEY": _environment_contract(
        "retired_credential", "none_retired",
        {"retired hasdata adapter": "process_environment"},
        "retired", "unverified", "Retired source adapter remains importable but is not configured.",
    ),
    "HOLIDAY_SHOULDER_DAYS": _environment_contract(
        "runtime_tuning", "dotenv_active",
        {"holidays import": "process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "Holiday shoulder calculation width.",
    ),
    "JUHE_FLIGHT_ENDPOINT": _environment_contract(
        "runtime_endpoint", "dotenv_active",
        {"juhe source": "process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "Juhe flight endpoint override.",
    ),
    "JUHE_FLIGHT_KEY": _environment_contract(
        "credential", "dotenv_active",
        {"standard collection entrypoints": "process_environment_after_project_dotenv"},
        "active", "user_configurable", "Primary economy listing credential.",
    ),
    "JUHE_ONTIME_ENDPOINT": _environment_contract(
        "runtime_endpoint", "dotenv_active",
        {"on_time_data": "process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "Juhe on-time endpoint presence gate.",
    ),
    "JUHE_QUOTA_CODES": _environment_contract(
        "runtime_tuning", "dotenv_active",
        {"juhe source import": "process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "Provider quota error code set.",
    ),
    "LOCALAPPDATA": _environment_contract(
        "platform_environment", "system_environment",
        {"scripts.ui_smoke": "operating_system_process_environment"},
        "current_script", "operating_system_managed", "Windows browser discovery root.",
    ),
    "MIN_BACKTEST_CASES": _environment_contract(
        "runtime_tuning", "dotenv_active",
        {"forecast import": "process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "Forecast backtest sample floor.",
    ),
    "MIN_OBS_FOR_LEVEL": _environment_contract(
        "runtime_tuning", "dotenv_active",
        {"forecast import": "process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "Forecast level observation floor.",
    ),
    "MIN_PAIRS_FOR_AGREEMENT": _environment_contract(
        "runtime_tuning", "dotenv_active",
        {"provenance import": "process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "Cross-source agreement pair floor.",
    ),
    "MIN_PATTERN_N": _environment_contract(
        "runtime_tuning", "dotenv_active",
        {"patterns import": "process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "Pattern sample floor.",
    ),
    "MIN_SAMPLE_FOR_PRICE_SIGNAL": _environment_contract(
        "runtime_tuning", "dotenv_active",
        {"analyzer import": "process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "Price signal sample floor.",
    ),
    "MIN_SAMPLE_FOR_TCURVE": _environment_contract(
        "runtime_tuning", "dotenv_active",
        {"tcurve import": "process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "T-curve sample floor.",
    ),
    "NO_LIVE_API": _environment_contract(
        "safety_only", "dotenv_commented",
        {
            "email_notifier/notifier imports": "process_environment_or_project_dotenv",
            "manual-live audit CLI": "process_environment_before_optional_dotenv",
            "CI": "workflow_process_environment",
        },
        "active", "user_configurable",
        "Test and controlled-audit safety switch with sink-specific coverage.",
    ),
    "PROGRAMFILES": _environment_contract(
        "platform_environment", "system_environment",
        {"scripts.ui_smoke": "operating_system_process_environment"},
        "current_script", "operating_system_managed", "Windows browser discovery root.",
    ),
    "PROGRAMFILES(X86)": _environment_contract(
        "platform_environment", "system_environment",
        {"scripts.ui_smoke": "operating_system_process_environment"},
        "current_script", "operating_system_managed", "Windows 32-bit browser discovery root.",
    ),
    "PUSHPLUS_TOKEN": _environment_contract(
        "credential", "dotenv_active",
        {"notifier": "process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "PushPlus notification credential.",
    ),
    "PYTHONANYWHERE_FORM_URL": _environment_contract(
        "runtime_endpoint", "dotenv_active",
        {"notifier": "process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "Hosted form URL fallback.",
    ),
    "PYTHONANYWHERE_TOKEN": _environment_contract(
        "credential", "dotenv_active",
        {"main/sync_subscriptions": "process_environment_after_project_dotenv"},
        "active", "user_configurable", "PythonAnywhere Files API credential.",
    ),
    "PYTHONANYWHERE_USER": _environment_contract(
        "runtime_identity", "dotenv_active",
        {"main/sync_subscriptions": "process_environment_after_project_dotenv"},
        "active", "user_configurable", "PythonAnywhere account path component.",
    ),
    "RAPIDAPI_KEY": _environment_contract(
        "retired_credential", "none_retired",
        {"retired skyscanner adapter": "process_environment"},
        "retired", "unverified", "Retired source adapter remains importable but is not configured.",
    ),
    "SEARCHAPI_KEY": _environment_contract(
        "retired_credential", "none_retired",
        {"retired searchapi adapter and legacy health check": "process_environment"},
        "retired", "unverified",
        "Retired listing credential remains referenced by dormant compatibility code.",
    ),
    "SERP_API_KEY": _environment_contract(
        "credential", "dotenv_active",
        {"serpapi credential resolver": "injected_mapping_or_process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "Supported SerpAPI credential alias.",
    ),
    "SERPAPI_API_KEY": _environment_contract(
        "credential", "dotenv_active",
        {"serpapi credential resolver": "injected_mapping_or_process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "Supported SerpAPI credential alias.",
    ),
    "SERPAPI_KEY": _environment_contract(
        "credential", "dotenv_active",
        {"serpapi credential resolver": "injected_mapping_or_process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "Preferred SerpAPI credential name.",
    ),
    "SESSION_COOKIE_SECURE": _environment_contract(
        "security_runtime", "dotenv_active",
        {"web_form": "process_environment_after_project_dotenv"},
        "active", "user_configurable", "Secure cookie policy.",
    ),
    "SHARED_DETAIL_TOKEN": _environment_contract(
        "credential", "dotenv_active",
        {"detail access": "process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "Shared detail-page access token.",
    ),
    "SKILL_GATE_IMPROVEMENT": _environment_contract(
        "runtime_tuning", "dotenv_active",
        {"forecast import": "process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "Forecast skill gate threshold.",
    ),
    "SMTP_HOST": _environment_contract(
        "runtime_endpoint", "dotenv_active",
        {"email_notifier import": "process_environment_after_project_dotenv"},
        "active", "user_configurable", "SMTP host override.",
    ),
    "SMTP_PASS": _environment_contract(
        "credential", "dotenv_active",
        {"email_notifier import": "process_environment_after_project_dotenv"},
        "active", "user_configurable", "SMTP credential.",
    ),
    "SMTP_PORT": _environment_contract(
        "runtime_endpoint", "dotenv_active",
        {"email_notifier import": "process_environment_after_project_dotenv"},
        "active", "user_configurable", "SMTP port override.",
    ),
    "SMTP_PROVIDER": _environment_contract(
        "runtime_notification", "dotenv_active",
        {"email_notifier import": "process_environment_after_project_dotenv"},
        "active", "user_configurable", "SMTP provider preset.",
    ),
    "SMTP_SSL": _environment_contract(
        "runtime_notification", "dotenv_active",
        {"email_notifier import": "process_environment_after_project_dotenv"},
        "active", "user_configurable", "SMTP transport mode.",
    ),
    "SMTP_USER": _environment_contract(
        "credential", "dotenv_active",
        {"email_notifier import": "process_environment_after_project_dotenv"},
        "active", "user_configurable", "SMTP account identity.",
    ),
    "SNAPSHOT_DEPART_DATE": _environment_contract(
        "tooling", "dotenv_active", {"scripts.snapshot_run": "process_environment"},
        "current_script", "user_configurable", "Snapshot fixture departure date override.",
    ),
    "SNAPSHOT_RETURN_DATE": _environment_contract(
        "tooling", "dotenv_active", {"scripts.snapshot_run": "process_environment"},
        "current_script", "user_configurable", "Snapshot fixture return date override.",
    ),
    "SUBSCRIPTION_FORM_URL": _environment_contract(
        "runtime_endpoint", "dotenv_active",
        {"notifier": "process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "Subscription form URL.",
    ),
    "TCURVE_MIN_CELLS": _environment_contract(
        "runtime_tuning", "dotenv_active",
        {"tcurve import": "process_environment_after_entrypoint_dotenv"},
        "active", "user_configurable", "T-curve cell floor.",
    ),
    "TEST_EMAIL_TO": _environment_contract(
        "repository_orphan_candidate", "dotenv_active",
        {"repository": "no_current_read"},
        "repository_orphan_candidate", "unverified",
        "Legacy live email entrypoint was retired; external use has not been verified.",
    ),
    "TRAVELPAYOUTS_TOKEN": _environment_contract(
        "retired_credential", "none_retired",
        {"retired travelpayouts adapter": "process_environment"},
        "retired", "unverified", "Retired source adapter remains importable but is not configured.",
    ),
    "WEB_HOST": _environment_contract(
        "runtime_process_only", "readme_process_only",
        {"run_web.main": "process_environment_before_dotenv"},
        "active", "user_configurable", "Web bind host resolved before web_form loads dotenv.",
    ),
    "WEB_PORT": _environment_contract(
        "runtime_process_only", "readme_process_only",
        {"run_web.main": "process_environment_before_dotenv"},
        "active", "user_configurable", "Web bind port resolved before web_form loads dotenv.",
    ),
}

ACTIVE_SECRET_VARIABLES = {
    name
    for name, contract in ENVIRONMENT_CONTRACTS.items()
    if contract["usage_class"] == "credential"
}
ACTIVE_ENV_VARIABLES = {
    name
    for name, contract in ENVIRONMENT_CONTRACTS.items()
    if contract["documentation_target"] == "dotenv_active"
}
SAFETY_ONLY_ENV_VARIABLES = {
    name
    for name, contract in ENVIRONMENT_CONTRACTS.items()
    if contract["documentation_target"] == "dotenv_commented"
}
DOCUMENTED_ENV_VARIABLES = ACTIVE_ENV_VARIABLES | SAFETY_ONLY_ENV_VARIABLES
RETIRED_OR_DORMANT_SOURCE_VARIABLES = {
    name
    for name, contract in ENVIRONMENT_CONTRACTS.items()
    if contract["repository_read_status"] == "retired"
}

EXPECTED_RED_TEST_IDS = frozenset(
    {
        "test_docs_accuracy.DocsAccuracyTest."
        "test_web_startup_documents_process_only_bind_environment",
    }
)
ENVIRONMENT_CONTRACT_FIELDS = frozenset(
    {
        "usage_class",
        "documentation_target",
        "effective_source_by_entrypoint",
        "repository_read_status",
        "external_consumer_status",
        "rationale",
    }
)


@dataclass(frozen=True)
class EnvironmentRead:
    variable: str
    file: str
    scope: str
    access_form: str
    access_mode: str
    line: int = field(compare=False, hash=False)


@dataclass(frozen=True)
class UnresolvedEnvironmentRead:
    file: str
    scope: str
    access_form: str
    access_mode: str
    expression: str
    line: int = field(compare=False, hash=False)


@dataclass(frozen=True)
class EnvironmentDiscovery:
    reads: frozenset[EnvironmentRead]
    unresolved_dynamic: frozenset[UnresolvedEnvironmentRead]

    @property
    def variables(self) -> frozenset[str]:
        return frozenset(item.variable for item in self.reads)


def _scope_nodes(scope_node):
    body = getattr(scope_node, "body", ())

    def walk(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        yield node
        for child in ast.iter_child_nodes(node):
            yield from walk(child)

    for statement in body:
        yield from walk(statement)


def _direct_nested_scopes(scope_node):
    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                yield child
            else:
                yield from walk(child)

    yield from walk(scope_node)


def _static_string_values(node, values: dict[str, frozenset[str]]):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return frozenset({node.value})
    if isinstance(node, ast.Name):
        return values.get(node.id)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        resolved = [_static_string_values(item, values) for item in node.elts]
        if any(item is None for item in resolved):
            return None
        return frozenset().union(*(item for item in resolved if item is not None))
    if isinstance(node, ast.Dict):
        resolved = [_static_string_values(item, values) for item in node.values]
        if any(item is None for item in resolved):
            return None
        return frozenset().union(*(item for item in resolved if item is not None))
    if isinstance(node, (ast.IfExp, ast.BoolOp)):
        branches = (
            (node.body, node.orelse)
            if isinstance(node, ast.IfExp)
            else tuple(node.values)
        )
        resolved = [_static_string_values(item, values) for item in branches]
        available = [item for item in resolved if item]
        return frozenset().union(*available) if available else None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Name)
    ):
        return values.get(node.func.value.id)
    return None


def _assigned_names(node):
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(
            name
            for item in node.elts
            for name in _assigned_names(item)
        )
    return ()


def _import_aliases(tree):
    os_aliases = set()
    getenv_aliases = set()
    environ_aliases = set()
    dotenv_values_aliases = set()
    dotenv_module_aliases = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name == "os":
                    os_aliases.add(item.asname or item.name)
                if item.name == "dotenv":
                    dotenv_module_aliases.add(item.asname or item.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "os":
                for item in node.names:
                    if item.name == "getenv":
                        getenv_aliases.add(item.asname or item.name)
                    elif item.name == "environ":
                        environ_aliases.add(item.asname or item.name)
            elif node.module == "dotenv":
                for item in node.names:
                    if item.name == "dotenv_values":
                        dotenv_values_aliases.add(item.asname or item.name)
    return (
        os_aliases,
        getenv_aliases,
        environ_aliases,
        dotenv_values_aliases,
        dotenv_module_aliases,
    )


def _is_os_environ(node, os_aliases, environ_aliases) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id in os_aliases
    ) or (isinstance(node, ast.Name) and node.id in environ_aliases)


def _is_dotenv_values_call(node, dotenv_values_aliases, dotenv_module_aliases) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        return node.func.id in dotenv_values_aliases
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "dotenv_values"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in dotenv_module_aliases
    )


def _references_name(node, names: set[str]) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.IfExp):
        return _references_name(node.body, names) or _references_name(node.orelse, names)
    if isinstance(node, ast.BoolOp):
        return any(_references_name(item, names) for item in node.values)
    return False


def _references_environment(node, os_aliases, environ_aliases, env_mapping_aliases) -> bool:
    if node is None:
        return False
    if _is_os_environ(node, os_aliases, environ_aliases):
        return True
    if isinstance(node, ast.Name):
        return node.id in env_mapping_aliases
    if isinstance(node, ast.IfExp):
        return _references_environment(
            node.body, os_aliases, environ_aliases, env_mapping_aliases
        ) or _references_environment(
            node.orelse, os_aliases, environ_aliases, env_mapping_aliases
        )
    if isinstance(node, ast.BoolOp):
        return any(
            _references_environment(
                item, os_aliases, environ_aliases, env_mapping_aliases
            )
            for item in node.values
        )
    return False


def _function_parameter_values(tree, module_values):
    definitions: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.setdefault(node.name, []).append(node)
    unique = {name: items[0] for name, items in definitions.items() if len(items) == 1}
    result: dict[int, dict[str, set[str]]] = {
        id(node): {} for node in unique.values()
    }
    for call in (item for item in ast.walk(tree) if isinstance(item, ast.Call)):
        if not isinstance(call.func, ast.Name) or call.func.id not in unique:
            continue
        target = unique[call.func.id]
        parameters = [*target.args.posonlyargs, *target.args.args]
        for parameter, argument in zip(parameters, call.args):
            resolved = _static_string_values(argument, module_values)
            if resolved:
                result[id(target)].setdefault(parameter.arg, set()).update(resolved)
        parameter_names = {parameter.arg for parameter in parameters}
        for keyword in call.keywords:
            if keyword.arg not in parameter_names:
                continue
            resolved = _static_string_values(keyword.value, module_values)
            if resolved:
                result[id(target)].setdefault(keyword.arg, set()).update(resolved)
    return {
        function_id: {
            name: frozenset(values)
            for name, values in parameters.items()
        }
        for function_id, parameters in result.items()
    }


def _scope_context(
    scope_node,
    module_values,
    parameter_values,
    os_aliases,
    environ_aliases,
    dotenv_values_aliases,
    dotenv_module_aliases,
):
    values = dict(module_values)
    values.update(parameter_values.get(id(scope_node), {}))
    parameter_names = {
        argument.arg
        for argument in (
            [*scope_node.args.posonlyargs, *scope_node.args.args, *scope_node.args.kwonlyargs]
            if isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef))
            else []
        )
    }
    env_mapping_aliases = set(environ_aliases)
    env_mapping_aliases.update(parameter_names & {"env", "environ", "environment"})
    dotenv_mapping_aliases = set()
    nodes = tuple(_scope_nodes(scope_node))

    for _ in range(12):
        before = (dict(values), set(env_mapping_aliases), set(dotenv_mapping_aliases))
        for node in nodes:
            if isinstance(node, ast.For):
                resolved = _static_string_values(node.iter, values)
                if resolved:
                    for name in _assigned_names(node.target):
                        values[name] = resolved
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                assigned = (
                    tuple(name for target in node.targets for name in _assigned_names(target))
                    if isinstance(node, ast.Assign)
                    else _assigned_names(node.target)
                )
                rhs = node.value
                resolved = _static_string_values(rhs, values)
                for name in assigned:
                    if resolved:
                        values[name] = resolved
                    if _references_environment(
                        rhs, os_aliases, environ_aliases, env_mapping_aliases
                    ):
                        env_mapping_aliases.add(name)
                    if _is_dotenv_values_call(
                        rhs, dotenv_values_aliases, dotenv_module_aliases
                    ) or _references_name(rhs, dotenv_mapping_aliases):
                        dotenv_mapping_aliases.add(name)
        after = (values, env_mapping_aliases, dotenv_mapping_aliases)
        if before == after:
            break
    return values, env_mapping_aliases, dotenv_mapping_aliases


class _EnvironmentAccessVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        file,
        scope,
        values,
        os_aliases,
        getenv_aliases,
        environ_aliases,
        env_mapping_aliases,
        dotenv_values_aliases,
        dotenv_module_aliases,
        dotenv_mapping_aliases,
    ):
        self.file = file
        self.scope = scope
        self.values = values
        self.os_aliases = os_aliases
        self.getenv_aliases = getenv_aliases
        self.environ_aliases = environ_aliases
        self.env_mapping_aliases = env_mapping_aliases
        self.dotenv_values_aliases = dotenv_values_aliases
        self.dotenv_module_aliases = dotenv_module_aliases
        self.dotenv_mapping_aliases = dotenv_mapping_aliases
        self.reads = set()
        self.unresolved = set()

    def visit_FunctionDef(self, node):
        return None

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef

    def _mapping_kind(self, node):
        if _is_os_environ(node, self.os_aliases, self.environ_aliases):
            return "os.environ"
        if isinstance(node, ast.Name) and node.id in self.env_mapping_aliases:
            return "environment_alias"
        if isinstance(node, ast.Name) and node.id in self.dotenv_mapping_aliases:
            return "dotenv_values"
        if _is_dotenv_values_call(
            node, self.dotenv_values_aliases, self.dotenv_module_aliases
        ):
            return "dotenv_values"
        return None

    def _record(self, key_node, access_form, access_mode, line):
        resolved = _static_string_values(key_node, self.values)
        if resolved:
            for variable in resolved:
                self.reads.add(
                    EnvironmentRead(
                        variable=variable,
                        file=self.file,
                        scope=self.scope,
                        access_form=access_form,
                        access_mode=access_mode,
                        line=line,
                    )
                )
            return
        try:
            expression = ast.unparse(key_node)
        except Exception:
            expression = type(key_node).__name__
        self.unresolved.add(
            UnresolvedEnvironmentRead(
                file=self.file,
                scope=self.scope,
                access_form=access_form,
                access_mode=access_mode,
                expression=expression,
                line=line,
            )
        )

    def visit_Call(self, node):
        access_form = None
        access_mode = "read"
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in self.os_aliases
            and node.func.attr == "getenv"
        ):
            access_form = "os.getenv"
        elif isinstance(node.func, ast.Name) and node.func.id in self.getenv_aliases:
            access_form = "getenv_alias"
        elif isinstance(node.func, ast.Attribute) and node.func.attr in {"get", "setdefault"}:
            mapping_kind = self._mapping_kind(node.func.value)
            if mapping_kind:
                access_form = f"{mapping_kind}.{node.func.attr}"
                if node.func.attr == "setdefault":
                    access_mode = "read_write_default"
        if access_form and node.args:
            self._record(node.args[0], access_form, access_mode, node.lineno)
        self.generic_visit(node)

    def visit_Subscript(self, node):
        if isinstance(node.ctx, ast.Load):
            mapping_kind = self._mapping_kind(node.value)
            if mapping_kind:
                self._record(node.slice, f"{mapping_kind}[]", "read", node.lineno)
        self.generic_visit(node)


def _discover_environment_reads(sources: dict[str, str]) -> EnvironmentDiscovery:
    reads = set()
    unresolved = set()
    for relative_path, source in sorted(sources.items()):
        tree = ast.parse(source, filename=relative_path)
        (
            os_aliases,
            getenv_aliases,
            environ_aliases,
            dotenv_values_aliases,
            dotenv_module_aliases,
        ) = _import_aliases(tree)
        module_values: dict[str, frozenset[str]] = {}
        for _ in range(12):
            before = dict(module_values)
            for node in _scope_nodes(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    assigned = (
                        tuple(name for target in node.targets for name in _assigned_names(target))
                        if isinstance(node, ast.Assign)
                        else _assigned_names(node.target)
                    )
                    resolved = _static_string_values(node.value, module_values)
                    if resolved:
                        for name in assigned:
                            module_values[name] = resolved
            if before == module_values:
                break
        parameter_values = _function_parameter_values(tree, module_values)

        def scan_scope(scope_node, parent_scope=""):
            scope_name = getattr(scope_node, "name", "<module>")
            qualified_scope = (
                f"{parent_scope}.{scope_name}"
                if parent_scope and scope_name != "<module>"
                else scope_name
            )
            values, env_mapping_aliases, dotenv_mapping_aliases = _scope_context(
                scope_node,
                module_values,
                parameter_values,
                os_aliases,
                environ_aliases,
                dotenv_values_aliases,
                dotenv_module_aliases,
            )
            visitor = _EnvironmentAccessVisitor(
                file=relative_path,
                scope=qualified_scope,
                values=values,
                os_aliases=os_aliases,
                getenv_aliases=getenv_aliases,
                environ_aliases=environ_aliases,
                env_mapping_aliases=env_mapping_aliases,
                dotenv_values_aliases=dotenv_values_aliases,
                dotenv_module_aliases=dotenv_module_aliases,
                dotenv_mapping_aliases=dotenv_mapping_aliases,
            )
            for statement in getattr(scope_node, "body", ()):
                visitor.visit(statement)
            reads.update(visitor.reads)
            unresolved.update(visitor.unresolved)
            for nested in _direct_nested_scopes(scope_node):
                scan_scope(nested, qualified_scope if qualified_scope != "<module>" else "")

        scan_scope(tree)
    return EnvironmentDiscovery(frozenset(reads), frozenset(unresolved))


def _tracked_runtime_python_sources() -> dict[str, str]:
    completed = subprocess.run(
        ["git", "ls-files", "--", "*.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    sources = {}
    for raw_path in completed.stdout.splitlines():
        relative_path = raw_path.replace("\\", "/")
        path = Path(relative_path)
        if (
            path.name.startswith("test_")
            or path.name == "conftest.py"
            or "tests" in path.parts
        ):
            continue
        sources[relative_path] = (ROOT / relative_path).read_text(encoding="utf-8-sig")
    return sources


def _environment_contract_violations(
    discovery: EnvironmentDiscovery,
    documented_names: set[str],
    manifest: dict[str, dict[str, object]],
) -> frozenset[str]:
    violations = set()
    candidate_names = discovery.variables | frozenset(documented_names)
    violations.update(f"unregistered:{name}" for name in candidate_names - manifest.keys())
    violations.update(f"undiscovered:{name}" for name in manifest.keys() - candidate_names)
    violations.update(
        f"unresolved:{item.file}:{item.scope}:{item.expression}"
        for item in discovery.unresolved_dynamic
    )
    for name, contract in manifest.items():
        if set(contract) != ENVIRONMENT_CONTRACT_FIELDS:
            violations.add(f"schema:{name}")
        if not all(contract.get(field_name) for field_name in ENVIRONMENT_CONTRACT_FIELDS):
            violations.add(f"empty-field:{name}")
    return frozenset(violations)


class EnvironmentSourceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
        cls.discovery = _discover_environment_reads(_tracked_runtime_python_sources())

    def test_repository_environment_contract_is_exact(self):
        documented_names = _dotenv_names(self.env_example)
        self.assertEqual(
            _environment_contract_violations(
                self.discovery,
                documented_names,
                ENVIRONMENT_CONTRACTS,
            ),
            frozenset(),
        )
        self.assertEqual(
            self.discovery.variables | frozenset(documented_names),
            frozenset(ENVIRONMENT_CONTRACTS),
        )
        self.assertEqual(self.discovery.unresolved_dynamic, frozenset())

    def test_contract_has_one_complete_record_per_variable(self):
        for variable, contract in ENVIRONMENT_CONTRACTS.items():
            with self.subTest(variable=variable):
                self.assertEqual(set(contract), ENVIRONMENT_CONTRACT_FIELDS)
                self.assertTrue(contract["usage_class"])
                self.assertTrue(contract["documentation_target"])
                self.assertIsInstance(contract["effective_source_by_entrypoint"], dict)
                self.assertTrue(contract["effective_source_by_entrypoint"])
                self.assertTrue(contract["repository_read_status"])
                self.assertTrue(contract["external_consumer_status"])
                self.assertTrue(contract["rationale"])

    def test_documentation_targets_match_dotenv_and_process_only_boundaries(self):
        documented_names = _dotenv_names(self.env_example)
        dotenv_targets = {
            name
            for name, contract in ENVIRONMENT_CONTRACTS.items()
            if str(contract["documentation_target"]).startswith("dotenv_")
        }
        self.assertEqual(dotenv_targets, documented_names)
        for variable in ("WEB_HOST", "WEB_PORT", "ALLOW_PUBLIC_WEB_BIND"):
            with self.subTest(variable=variable):
                contract = ENVIRONMENT_CONTRACTS[variable]
                self.assertEqual(contract["documentation_target"], "readme_process_only")
                self.assertEqual(
                    contract["effective_source_by_entrypoint"],
                    {"run_web.main": "process_environment_before_dotenv"},
                )
                self.assertNotIn(variable, documented_names)

    def test_no_live_and_orphan_contracts_are_explicit(self):
        no_live = ENVIRONMENT_CONTRACTS["NO_LIVE_API"]
        self.assertEqual(no_live["usage_class"], "safety_only")
        self.assertEqual(no_live["documentation_target"], "dotenv_commented")
        self.assertIn("CI", no_live["effective_source_by_entrypoint"])

        test_email_reads = [
            item for item in self.discovery.reads if item.variable == "TEST_EMAIL_TO"
        ]
        self.assertEqual(test_email_reads, [])
        self.assertEqual(
            ENVIRONMENT_CONTRACTS["TEST_EMAIL_TO"]["repository_read_status"],
            "repository_orphan_candidate",
        )
        self.assertEqual(
            ENVIRONMENT_CONTRACTS["TEST_EMAIL_TO"]["external_consumer_status"],
            "unverified",
        )

    def test_repository_and_documentation_differences_are_explicit(self):
        documented_names = _dotenv_names(self.env_example)
        self.assertEqual(
            self.discovery.variables - documented_names,
            frozenset(
                {
                    "ALLOW_PUBLIC_WEB_BIND",
                    "HASDATA_KEY",
                    "LOCALAPPDATA",
                    "PROGRAMFILES",
                    "PROGRAMFILES(X86)",
                    "RAPIDAPI_KEY",
                    "SEARCHAPI_KEY",
                    "TRAVELPAYOUTS_TOKEN",
                    "WEB_HOST",
                    "WEB_PORT",
                }
            ),
        )
        self.assertEqual(
            frozenset(documented_names) - self.discovery.variables,
            frozenset({"TEST_EMAIL_TO"}),
        )
        for variable in ENVIRONMENT_CONTRACTS.keys() - self.discovery.variables:
            with self.subTest(variable=variable):
                self.assertIn(
                    ENVIRONMENT_CONTRACTS[variable]["repository_read_status"],
                    {"repository_orphan_candidate", "retired", "external_only"},
                )

    def test_serpapi_alias_constant_loop_is_expanded(self):
        resolver_reads = {
            item.variable
            for item in self.discovery.reads
            if item.file == "serpapi_credentials.py"
            and item.scope == "resolve_serpapi_key"
        }
        self.assertEqual(resolver_reads, frozenset(SERPAPI_KEY_ALIASES))

    def test_source_environment_key_mapping_is_expanded(self):
        source_gate_reads = {
            item.variable
            for item in self.discovery.reads
            if item.file == "main.py"
            and item.scope == "_estimated_saved_api_calls"
        }
        self.assertEqual(
            source_gate_reads,
            frozenset({"HASDATA_KEY", "DUFFEL_TOKEN", "JUHE_FLIGHT_KEY"}),
        )

    def test_environment_read_discovery_synthetic_matrix(self):
        cases = {
            "getenv": ("import os\nvalue = os.getenv('FROM_GETENV')\n", {"FROM_GETENV"}, 0),
            "environ_get": ("import os\nvalue = os.environ.get('FROM_GET')\n", {"FROM_GET"}, 0),
            "environ_subscript": ("import os\nvalue = os.environ['FROM_SUBSCRIPT']\n", {"FROM_SUBSCRIPT"}, 0),
            "setdefault": ("import os\nvalue = os.environ.setdefault('FROM_SETDEFAULT', 'x')\n", {"FROM_SETDEFAULT"}, 0),
            "getenv_alias": ("from os import getenv as read_env\nvalue = read_env('FROM_ALIAS')\n", {"FROM_ALIAS"}, 0),
            "environ_alias": (
                "import os\ndef load(environ=None):\n    values = os.environ if environ is None else environ\n    return values.get('FROM_ENV_ALIAS')\n",
                {"FROM_ENV_ALIAS"},
                0,
            ),
            "constant_collection_loop": (
                "import os\nENV_NAMES = ('LOOP_ONE', 'LOOP_TWO')\ndef load():\n    for name in ENV_NAMES:\n        os.environ.get(name)\n",
                {"LOOP_ONE", "LOOP_TWO"},
                0,
            ),
            "dotenv_values_mapping": (
                "from dotenv import dotenv_values\nvalues = dotenv_values('.env')\nresult = values.get('FROM_DOTENV_VALUES')\n",
                {"FROM_DOTENV_VALUES"},
                0,
            ),
            "write_is_not_read": ("import os\nos.environ['WRITE_ONLY'] = 'x'\n", set(), 0),
            "ordinary_dict_get": ("values = {}\nresult = values.get('NOT_ENVIRONMENT')\n", set(), 0),
            "unresolved_dynamic": (
                "import os\ndef key_name():\n    return 'DYNAMIC'\nvalue = os.environ.get(key_name())\n",
                set(),
                1,
            ),
        }
        for mutation_id, (source, expected, unresolved_count) in cases.items():
            with self.subTest(mutation_id=mutation_id):
                discovery = _discover_environment_reads({"synthetic.py": source})
                self.assertEqual(discovery.variables, frozenset(expected))
                self.assertEqual(len(discovery.unresolved_dynamic), unresolved_count)

    def test_unclassified_variable_is_rejected(self):
        discovery = _discover_environment_reads(
            {"synthetic.py": "import os\nvalue = os.getenv('UNCLASSIFIED_ENV')\n"}
        )
        self.assertEqual(
            _environment_contract_violations(discovery, set(), {}),
            frozenset({"unregistered:UNCLASSIFIED_ENV"}),
        )

    def test_read_identity_does_not_depend_on_diagnostic_line(self):
        first = EnvironmentRead("NAME", "module.py", "load", "os.getenv", "read", 3)
        second = EnvironmentRead("NAME", "module.py", "load", "os.getenv", "read", 90)
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))


def _dotenv_entries(text: str) -> list[dict[str, str | bool]]:
    entries = []
    for line in text.splitlines():
        match = re.match(
            r"^\s*(?P<comment>#\s*)?(?P<name>[A-Z][A-Z0-9_]*)\s*=(?P<value>.*)$",
            line,
        )
        if match:
            entries.append(
                {
                    "variable_name": match.group("name"),
                    "commented": bool(match.group("comment")),
                    "raw_value_present": bool(match.group("value").strip()),
                }
            )
    return entries


def _dotenv_names(text: str) -> set[str]:
    return {str(entry["variable_name"]) for entry in _dotenv_entries(text)}


def _dotenv_section(text: str, heading: str) -> str:
    lines = text.splitlines()
    marker = re.compile(rf"^#\s*=+\s*{re.escape(heading)}\s*=+\s*$")
    start = next((index for index, line in enumerate(lines) if marker.match(line)), None)
    if start is None:
        return ""
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if re.match(r"^#\s*=+\s*.+?\s*=+\s*$", lines[index])
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _local_markdown_links(text: str) -> list[str]:
    links = re.findall(r"\[[^]]+\]\(([^)]+)\)", text)
    return [
        item.split("#", 1)[0]
        for item in links
        if item
        and not item.startswith(("http://", "https://", "#", "mailto:"))
        and not item.startswith("../../actions/")
    ]


def _script_references(text: str) -> set[str]:
    return {
        item.replace("\\", "/")
        for item in re.findall(
            r"(?<![A-Za-z0-9_.-])((?:scripts|analytics)/[A-Za-z0-9_.-]+\.py|[A-Za-z0-9_.-]+\.py)",
            text.replace("\\", "/"),
        )
    }


def _documented_python_commands(text: str) -> set[str]:
    commands = set()
    for line in text.replace("\\", "/").splitlines():
        stripped = line.strip()
        if not re.match(r"^(?:python|python3(?:\.13)?)\s", stripped):
            continue
        match = re.search(
            r"((?:scripts|analytics)/[A-Za-z0-9_.-]+\.py|[A-Za-z0-9_.-]+\.py)",
            stripped,
        )
        if match:
            commands.add(match.group(1))
    return commands


def _fenced_blocks(text: str, language: str) -> list[str]:
    return re.findall(
        rf"```{re.escape(language)}\s*\r?\n(.*?)```",
        text,
        flags=re.DOTALL,
    )


def _markdown_section(text: str, heading: str) -> str:
    lines = text.splitlines()
    try:
        start = lines.index(heading)
    except ValueError as exc:
        raise AssertionError(f"missing-contract: Markdown缺少章节 {heading}") from exc
    level = len(heading) - len(heading.lstrip("#"))
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = re.match(r"^(#+)\s", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    return "\n".join(lines[start:end])


class DocsAccuracyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme = README.read_text(encoding="utf-8")
        cls.env_example = ENV_EXAMPLE.read_text(encoding="utf-8")

    def test_readme_has_the_approved_ten_section_skeleton(self):
        headings = re.findall(r"^##\s+(?:\d+\.\s*)?(.+?)\s*$", self.readme, re.MULTILINE)
        for section in EXPECTED_SECTIONS:
            self.assertTrue(
                any(section in heading for heading in headings),
                f"README缺少章节: {section}",
            )
        self.assertIn("An evidence-first flight monitoring system", self.readme)
        self.assertIn(
            "[![tests](../../actions/workflows/tests.yml/badge.svg)]",
            self.readme,
        )
        self.assertIn("### License", self.readme)
        self.assertIn("### 开发方式", self.readme)

    def test_license_file_uses_mit(self):
        self.assertTrue(LICENSE.is_file(), "缺少LICENSE文件")
        self.assertIn("MIT License", LICENSE.read_text(encoding="utf-8"))

    def test_readme_has_no_unresolved_placeholders(self):
        self.assertNotIn("待定", self.readme)
        self.assertNotRegex(
            self.readme,
            r"(?im)^\s*(?:TODO|TBD)(?:\s|:|$)",
        )

    def test_all_linked_paths_and_python_script_references_exist(self):
        missing = []
        for relative in _local_markdown_links(self.readme):
            if not (ROOT / relative).exists():
                missing.append(relative)
        for relative in sorted(_script_references(self.readme)):
            if not (ROOT / relative).is_file():
                missing.append(relative)
        self.assertEqual(missing, [])

    def test_env_example_and_readme_match_active_secret_contract(self):
        env_names = _dotenv_names(self.env_example)
        aliases = set(SERPAPI_KEY_ALIASES)
        readme_aliases = {
            name for name in aliases if re.search(rf"\b{re.escape(name)}\b", self.readme)
        }

        self.assertEqual(readme_aliases, aliases)
        self.assertTrue(aliases <= env_names)
        self.assertTrue(ACTIVE_SECRET_VARIABLES <= env_names)
        self.assertEqual(env_names, DOCUMENTED_ENV_VARIABLES)
        self.assertEqual(RETIRED_OR_DORMANT_SOURCE_VARIABLES & env_names, set())
        self.assertNotIn("ljs96824", self.env_example)
        self.assertNotIn("@", self.env_example)
        self.assertNotRegex(self.env_example, r"(?i)(secret|token|key)_[a-z0-9]{16,}")

    def test_safety_only_env_variables_are_commented(self):
        entries = _dotenv_entries(self.env_example)
        by_name = {
            name: [
                entry
                for entry in entries
                if entry["variable_name"] == name
            ]
            for name in SAFETY_ONLY_ENV_VARIABLES
        }
        self.assertEqual(
            [name for name, matching in by_name.items() if not matching],
            [],
            "missing-contract: 缺少安全专用变量",
        )
        self.assertEqual(
            [
                name
                for name, matching in by_name.items()
                if not all(entry["commented"] for entry in matching)
            ],
            [],
            "safety-only变量不得默认启用",
        )
        no_live_entries = by_name["NO_LIVE_API"]
        self.assertEqual(len(no_live_entries), 1)
        self.assertTrue(no_live_entries[0]["raw_value_present"])

    def test_no_live_api_safety_section_contract(self):
        section = _dotenv_section(self.env_example, "测试与受控审计安全开关")
        self.assertTrue(section, "missing-contract: 缺少NO_LIVE_API安全开关分区")
        self.assertEqual(
            [term for term in ("CI", "离线测试", "受控审计") if term not in section],
            [],
        )
        self.assertRegex(section, r"只有精确值\s*1\s*生效")
        self.assertIn("不保护", section)
        self.assertEqual(
            [
                term
                for term in ("PA Files", "Juhe", "SerpAPI", "Duffel")
                if term not in section
            ],
            [],
        )

    def test_readme_links_no_live_api_coverage_from_env_setup(self):
        section = _markdown_section(self.readme, "### 6.3 创建 `.env`")
        self.assertEqual(
            [
                term
                for term in (
                    "NO_LIVE_API",
                    "CI",
                    "受控离线验证",
                    "不是全局断网开关",
                    "docs/external-network-no-live-api-coverage-2026-09-03.md",
                )
                if term not in section
            ],
            [],
            "missing-contract: README 6.3缺少NO_LIVE_API安全边界导航",
        )

    def test_web_startup_documents_process_only_bind_environment(self):
        section = _markdown_section(self.readme, "### 6.4 启动网页")
        required_terms = (
            "WEB_HOST",
            "WEB_PORT",
            "ALLOW_PUBLIC_WEB_BIND",
            "进程环境",
            ".env",
            "不会影响绑定决策",
            "run_web.main",
            "PowerShell",
            "Bash",
            "$env:ALLOW_PUBLIC_WEB_BIND",
            "应输出空",
        )
        self.assertEqual(
            [term for term in required_terms if term not in section],
            [],
            "missing-contract: Web启动章节缺少process-only来源与不外溢示例",
        )
        self.assertRegex(
            section,
            r"(?m)^WEB_HOST=0\.0\.0\.0 WEB_PORT=5000 "
            r"ALLOW_PUBLIC_WEB_BIND=1 python -u -X utf8 run_web\.py$",
        )
        self.assertIn("cmd.exe /d /c", section)
        self.assertIn("set ALLOW_PUBLIC_WEB_BIND=1", section)
        self.assertNotRegex(section, r"\$env:ALLOW_PUBLIC_WEB_BIND\s*=")

    def test_quick_start_contains_required_install_run_and_scheduler_contracts(self):
        required = (
            "Python 3.13",
            "python -m pip install -r requirements.txt -r requirements-dev.txt",
            "requirements.in",
            "requirements-dev.in",
            "requirements-dev.txt",
            "pip-compile",
            "锁文件由 pip-compile 生成，勿手改",
            "python -u -X utf8 run_web.py",
            "python -X utf8 -m pytest -q",
            "python -X utf8 -m unittest discover",
            "schtasks.exe /Create",
            "30 9 * * *",
            "git pull --ff-only",
            "Reload",
            "出站",
        )
        missing = [item for item in required if item not in self.readme]
        self.assertEqual(missing, [])
        self.assertTrue(REQUIREMENTS_INPUT.is_file())
        self.assertTrue(DEV_REQUIREMENTS_INPUT.is_file())
        self.assertTrue(DEV_REQUIREMENTS_LOCK.is_file())

    def test_readme_source_and_quota_claims_match_current_profiles(self):
        from source_profiles import ROUTE_SOURCE_PROFILES

        international = ROUTE_SOURCE_PROFILES["international"]
        active = {item["name"] for item in international["sources"]}
        retired = {item["name"] for item in international["retired_sources"]}

        self.assertEqual(active, {"juhe", "serpapi", "duffel"})
        self.assertEqual(retired, {"hasdata"})
        for phrase in (
            "聚合数据（Juhe）",
            "SerpAPI",
            "Duffel",
            "HasData",
            "550",
            "250",
            "2026-08-14",
        ):
            self.assertIn(phrase, self.readme)
        self.assertNotIn("monitor.yml", self.readme)

    def test_readme_documents_blocking_smoke_and_subscription_fact_sources(self):
        source_section = _markdown_section(
            self.readme,
            "## 5. 数据源与配额经济学",
        )
        runtime_section = _markdown_section(
            self.readme,
            "### 6.2 创建本地运行配置",
        )
        test_section = _markdown_section(
            self.readme,
            "### 6.4 运行离线测试",
        )

        self.assertNotRegex(
            source_section,
            r"本地订阅\s*属于运行事实.*data/runtime_config\.yaml",
            "stale-lock: README仍把现行订阅写成runtime_config运行事实",
        )
        self.assertNotIn(
            "目标日期、研究开关及本地订阅",
            runtime_section,
            "stale-lock: README运行配置步骤仍要求把真实订阅写入runtime_config",
        )
        self.assertNotIn(
            "观察模式",
            test_section,
            "stale-lock: README仍把ui-smoke描述为观察模式",
        )

        required = (
            "现行 Web CRUD、订阅采集、尝试状态与 PA 同步",
            "`data/subscriptions.json`",
            "权威持久化源",
            "`data/runtime_config.yaml`",
            "`subscriptions: []`",
            "配置校验",
            "legacy 迁移",
            "6b 完成前必须保持为空数组",
            "不得写入真实订阅",
            "不得提前删除该字段",
            "`validate_runtime_config`",
        )
        combined = "\n".join((source_section, runtime_section))
        self.assertEqual(
            [item for item in required if item not in combined],
            [],
            "missing-contract: README缺少订阅事实源与兼容占位边界",
        )
        self.assertIn(
            "阻断",
            test_section,
            "missing-contract: README缺少ui-smoke阻断语义",
        )

    def test_contributing_delivery_evidence_contract_is_normative_and_complete(self):
        section = _markdown_section(
            CONTRIBUTING.read_text(encoding="utf-8"),
            "## 交付声明与证据要求",
        )
        normative_terms = (
            "未来交付声明的规范性合同",
            "docs/codex-operational-evidence-audit-2026-08-30.md",
            "形成过程的历史出处",
            "后续规则更新只改 `CONTRIBUTING.md`",
            "不回写历史审计报告",
            "真实输出属于每次交付报告",
            "不写进静态规范",
        )
        self.assertEqual(
            [item for item in normative_terms if item not in section],
            [],
            "missing-contract: CONTRIBUTING缺少规范源与真实输出边界",
        )

        claims = {
            "### 1. 声明：已创建 PR": (
                "gh pr view <N> --repo ljs96824/flight-monitor --json number,state,url,baseRefOid,headRefOid,headRefName,commits,files",
                "state=OPEN",
                "baseRefOid == 任务基线",
                "headRefOid == 本地提交 SHA",
                "len(commits) == 1",
            ),
            "### 2. 声明：已推送": (
                "LOCAL=$(git rev-parse HEAD)",
                "git ls-remote --heads origin refs/heads/<branch>",
                "LOCAL == REMOTE",
                "命令有输出不等于声明成立",
            ),
            "### 3. 声明：main 为 X": (
                "git fetch --prune origin",
                "git branch --show-current",
                "git rev-parse refs/heads/main",
                "git rev-parse origin/main",
                "当前分支为 `main`",
                "refs/heads/main == origin/main == X",
            ),
            "### 4. 声明：CI 全绿": (
                "run_id",
                "head_sha",
                "jobs[].name/status/conclusion",
                "run.head_sha == 被验收提交 SHA",
                "completed/success",
                "PR 分支 checks 不等于 main post-merge checks",
            ),
            "### 5. 声明：哈希不变": (
                "before",
                "after",
                "静默窗口起止",
                "不与历史数值比较",
                "prices.db",
                "observations.sqlite3",
                "api_usage.json",
            ),
            "### 6. 声明：某文件无消费者": (
                "扫描命令",
                "命中数",
                "仓库内",
                "仓库外",
                "user_reported",
            ),
            "### 7. 声明：worktree 合规": (
                "git worktree list --porcelain",
                "项目目录与 `data/` 目录之外",
                "固定路径",
                "任务结束清理",
            ),
            "### 8. 声明：提交身份已核对": (
                "git config --get user.name",
                "git config --get user.email",
                "提交前",
            ),
            "### 9. 声明：可以删除远端 PR 分支": (
                "state=MERGED",
                "git merge-base --is-ancestor <MERGE_SHA> origin/main",
                "git ls-remote --heads origin refs/heads/<branch>",
                "已验收 head",
                "无其他 open PR 使用该分支",
                "不得以网页提示语或本地 `git pull` 单独作为依据",
                "本地 `main` 同步是独立收尾动作",
            ),
            "### 10. 声明：冻结 SHA 未漂移": (
                "完整 64 位",
                "fixture 路径",
                "生成命令",
                "字节数",
                "不得直接更新期望值",
            ),
        }
        self.assertEqual(len(claims), 10)
        for heading, required_terms in claims.items():
            with self.subTest(claim=heading):
                claim_section = _markdown_section(section, heading)
                self.assertIn("声明", claim_section)
                self.assertIn("命令", claim_section)
                self.assertIn("必填字段", claim_section)
                self.assertIn("**通过条件**", claim_section)
                self.assertEqual(
                    [item for item in required_terms if item not in claim_section],
                    [],
                    f"missing-contract: {heading} 证据合同不完整",
                )

    def test_external_network_no_live_api_coverage_contract(self):
        self.assertTrue(
            EXTERNAL_NETWORK_COVERAGE.is_file(),
            "missing-contract: 缺少外部网络NO_LIVE_API覆盖清单",
        )
        text = EXTERNAL_NETWORK_COVERAGE.read_text(encoding="utf-8")
        snapshot_section = _markdown_section(text, "## 快照与总声明")
        self.assertEqual(
            [
                item
                for item in (
                    "c59b8bc16041df97cad8baa7650b5f211a846870",
                    "Asia/Shanghai",
                    "静态快照",
                    "source_profiles",
                    "新增适配器",
                    "gateway",
                    "SMTP",
                    "PushPlus",
                    "不是全局网络防火墙",
                )
                if item not in snapshot_section
            ],
            [],
            "missing-contract: 快照点、复核触发器或非全局防火墙边界不完整",
        )

        scan_section = _markdown_section(text, "## 范围完备性扫描")
        self.assertIn("生产 Python", scan_section)
        self.assertIn("测试文件", scan_section)
        self.assertIn("异常项", scan_section)

        active_section = _markdown_section(text, "## 现役网络路径")
        actual_service_ids = set(
            re.findall(
                r"^\|\s*`([a-z][a-z0-9_]*)`\s*\|",
                active_section,
                flags=re.MULTILINE,
            )
        )
        self.assertEqual(
            actual_service_ids,
            {
                "smtp_email",
                "pushplus",
                "pa_subscription_download",
                "pa_payload_upload",
                "juhe",
                "serpapi",
                "duffel",
            },
        )
        for term in (
            "gate_status",
            "operational_controls",
            "runtime_contracts",
            "evidence_basis",
            "evidence_level",
        ):
            self.assertIn(term, active_section)

        inactive_section = _markdown_section(text, "## 非现役或退役适配器")
        self.assertIn("直接", inactive_section)
        self.assertIn("NO_LIVE_API", inactive_section)
        self.assertIn("当前调用方", inactive_section)

        controls_section = _markdown_section(text, "## 控制层分类")
        for term in ("prevention", "containment", "detection", "不能阻止"):
            self.assertIn(term, controls_section)

        contracts_section = _markdown_section(
            text,
            "## runtime_contracts 与 documentation_contract",
        )
        for term in (
            "SMTP",
            "PushPlus",
            "私有 gateway 调用图",
            "runtime_contracts",
            "documentation_contract",
        ):
            self.assertIn(term, contracts_section)

        gaps_section = _markdown_section(text, "## 当前缺口")
        for term in (
            "PA 订阅下载",
            "PA payload 上传",
            "Juhe",
            "SerpAPI",
            "Duffel",
            "当前无门，依赖上游控制及调用方测试隔离",
        ):
            self.assertIn(term, gaps_section)

        boundaries_section = _markdown_section(text, "## 已知边界")
        for term in (
            "process",
            ".env",
            "effective",
            "import",
            "patch target",
            "WSGI",
            "计划任务",
            "仓库内 Python",
        ):
            self.assertIn(term, boundaries_section)

    def test_documented_python_entrypoints_have_offline_help_or_import_probe(self):
        probes = {
            "run_web.py": [sys.executable, "-X", "utf8", "-m", "py_compile", "run_web.py"],
            "main.py": [sys.executable, "-X", "utf8", "-m", "py_compile", "main.py"],
            "basket_collect.py": [sys.executable, "-X", "utf8", "-m", "py_compile", "basket_collect.py"],
            "scripts/snapshot_run.py": [sys.executable, "-X", "utf8", "scripts/snapshot_run.py", "--help"],
            "scripts/tcurve_report.py": [sys.executable, "-X", "utf8", "scripts/tcurve_report.py", "--help"],
            "scripts/provenance_report.py": [sys.executable, "-X", "utf8", "scripts/provenance_report.py", "--help"],
            "scripts/forecast_report.py": [sys.executable, "-X", "utf8", "scripts/forecast_report.py", "--help"],
            "scripts/list_expired_subs.py": [sys.executable, "-X", "utf8", "scripts/list_expired_subs.py", "--help"],
            "scripts/list_unresolvable_subs.py": [sys.executable, "-X", "utf8", "scripts/list_unresolvable_subs.py", "--help"],
            "scripts/list_incomplete_notification_subs.py": [sys.executable, "-X", "utf8", "scripts/list_incomplete_notification_subs.py", "--help"],
            "scripts/ui_smoke.py": [sys.executable, "-X", "utf8", "-m", "py_compile", "scripts/ui_smoke.py"],
            "scripts/migrate_runtime_config.py": [
                sys.executable,
                "-X",
                "utf8",
                "scripts/migrate_runtime_config.py",
                "--help",
            ],
            "scripts/initialize_api_usage.py": [
                sys.executable,
                "-X",
                "utf8",
                "scripts/initialize_api_usage.py",
                "--help",
            ],
        }
        referenced = _documented_python_commands(self.readme)
        unchecked = sorted(
            item
            for item in referenced
            if item not in probes and item not in {"test_docs_accuracy.py", "test_frozen_email_baseline.py"}
        )
        self.assertEqual(unchecked, [], f"README脚本缺少离线探针: {unchecked}")

        for relative in sorted(referenced & probes.keys()):
            with self.subTest(script=relative):
                result = subprocess.run(
                    probes[relative],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    check=False,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    f"{relative}离线探针失败:\n{result.stdout}\n{result.stderr}",
                )

    def test_all_documented_non_api_commands_have_safe_probes(self):
        self.assertEqual(sys.version_info[:2], (3, 13))
        cli_python = shutil.which("python") or sys.executable
        commands = {
            line.strip()
            for block in _fenced_blocks(self.readme, "bash")
            for line in block.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        copy_command = (
            'python -c "from pathlib import Path; src=Path(\'.env.example\'); '
            "dst=Path('.env'); dst.exists() or dst.write_bytes(src.read_bytes())\""
        )
        live_commands = {
            "python -u -X utf8 main.py",
            "python -u -X utf8 basket_collect.py",
        }
        path_only_commands = {"cd ~/flight-monitor"}
        probes = {
            "python --version": [cli_python, "--version"],
            "python -X utf8 scripts/initialize_api_usage.py": [
                cli_python,
                "-X",
                "utf8",
                "scripts/initialize_api_usage.py",
                "--help",
            ],
            "python -m pip install -r requirements.txt -r requirements-dev.txt": [
                cli_python, "-m", "pip", "install", "--help",
            ],
            "python -m piptools compile --allow-unsafe --generate-hashes --no-emit-index-url --no-emit-trusted-host --output-file requirements.txt --strip-extras requirements.in": [
                cli_python, "-m", "pip", "install", "--help",
            ],
            "python -m piptools compile --allow-unsafe --generate-hashes --no-emit-index-url --no-emit-trusted-host --output-file requirements-dev.txt --strip-extras requirements-dev.in": [
                cli_python, "-m", "pip", "install", "--help",
            ],
            "python -u -X utf8 run_web.py": [
                cli_python, "-X", "utf8", "-m", "py_compile", "run_web.py",
            ],
            "WEB_HOST=0.0.0.0 WEB_PORT=5000 ALLOW_PUBLIC_WEB_BIND=1 python -u -X utf8 run_web.py": [
                cli_python, "-X", "utf8", "-m", "py_compile", "run_web.py",
            ],
            "python -X utf8 -m pytest -q": [
                cli_python, "-X", "utf8", "-m", "pytest", "--version",
            ],
            "python -X utf8 -m unittest discover": [
                cli_python, "-X", "utf8", "-m", "unittest", "-h",
            ],
            "python -X utf8 scripts/ui_smoke.py --log-path data/ui-smoke-artifacts/ui-smoke.log --artifact-dir data/ui-smoke-artifacts": [
                cli_python, "-X", "utf8", "-m", "py_compile", "scripts/ui_smoke.py",
            ],
            "git pull --ff-only": ["git", "pull", "-h"],
            "python3.13 -m pip install --user -r requirements.txt": [
                cli_python, "-m", "pip", "install", "--help",
            ],
            "python -X utf8 scripts/list_expired_subs.py --help": [
                cli_python, "-X", "utf8", "scripts/list_expired_subs.py", "--help",
            ],
            "python -X utf8 scripts/list_unresolvable_subs.py --help": [
                cli_python, "-X", "utf8", "scripts/list_unresolvable_subs.py", "--help",
            ],
            "python -X utf8 scripts/list_incomplete_notification_subs.py --help": [
                cli_python, "-X", "utf8", "scripts/list_incomplete_notification_subs.py", "--help",
            ],
            "python -X utf8 scripts/tcurve_report.py --help": [
                cli_python, "-X", "utf8", "scripts/tcurve_report.py", "--help",
            ],
            "python -X utf8 scripts/provenance_report.py --help": [
                cli_python, "-X", "utf8", "scripts/provenance_report.py", "--help",
            ],
            "python -X utf8 scripts/forecast_report.py --help": [
                cli_python, "-X", "utf8", "scripts/forecast_report.py", "--help",
            ],
            "python -X utf8 scripts/migrate_runtime_config.py --source <path-to-legacy-config>": [
                cli_python,
                "-X",
                "utf8",
                "scripts/migrate_runtime_config.py",
                "--help",
            ],
            "python -X utf8 scripts/snapshot_run.py --output data/snapshot_check.json": [
                cli_python, "-X", "utf8", "scripts/snapshot_run.py", "--help",
            ],
        }
        handled = set(probes) | live_commands | path_only_commands | {copy_command}
        self.assertEqual(
            commands,
            handled,
            f"README命令缺少安全探针: {sorted(commands - handled)}",
        )

        for documented, probe in probes.items():
            with self.subTest(command=documented):
                result = subprocess.run(
                    probe,
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    check=False,
                )
                allowed = {0, 129} if documented == "git pull --ff-only" else {0}
                self.assertIn(
                    result.returncode,
                    allowed,
                    f"命令安全探针失败: {documented}\n{result.stdout}\n{result.stderr}",
                )

        copy_code = copy_command[len('python -c "'):-1]
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy2(ENV_EXAMPLE, Path(tmp) / ".env.example")
            result = subprocess.run(
                [cli_python, "-c", copy_code],
                cwd=tmp,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (Path(tmp) / ".env").read_bytes(),
                (Path(tmp) / ".env.example").read_bytes(),
            )

        powershell_blocks = _fenced_blocks(self.readme, "powershell")
        self.assertGreaterEqual(len(powershell_blocks), 1)
        if os.name == "nt":
            shell = shutil.which("powershell.exe") or shutil.which("powershell")
            self.assertIsNotNone(shell)
            for block_index, block in enumerate(powershell_blocks):
                with self.subTest(powershell_block=block_index):
                    result = subprocess.run(
                        [
                            shell,
                            "-NoProfile",
                            "-NonInteractive",
                            "-Command",
                            "$null=[scriptblock]::Create([Console]::In.ReadToEnd())",
                        ],
                        input=block,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=30,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

        cron_blocks = _fenced_blocks(self.readme, "cron")
        self.assertEqual(len(cron_blocks), 1)
        cron_line = cron_blocks[0].strip()
        self.assertRegex(cron_line, r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+.+$")
        self.assertIn("basket_collect.py", cron_line)
    def test_live_collection_commands_are_visibly_marked_as_quota_consuming(self):
        for command in (
            "python -u -X utf8 main.py",
            "python -u -X utf8 basket_collect.py",
        ):
            position = self.readme.find(command)
            self.assertNotEqual(position, -1, f"README缺少命令: {command}")
            context = self.readme[max(0, position - 180): position + len(command) + 180]
            self.assertIn("消耗配额", context)

    def test_runtime_backup_manual_has_restore_replay_and_privacy_contracts(self):
        self.assertTrue(RUNTIME_BACKUP_MANUAL.is_file())
        text = RUNTIME_BACKUP_MANUAL.read_text(encoding="utf-8")
        for phrase in (
            "只有成功恢复过的备份才算有效备份",
            "每周至少一次",
            "每次重大改动前",
            "--output-dir",
            "必须是绝对路径",
            "create",
            "verify",
            "restore",
            "rehearse",
            "--force-production",
            "--confirm-production-restore RESTORE",
            "未加密归档不得上传公共或共享云目录",
            "age",
            "7-Zip AES",
            "real_api_calls",
        ):
            self.assertIn(phrase, text)
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "scripts/runtime_backup.py", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--output-dir", result.stdout)
        self.assertIn("--label", result.stdout)
        self.assertIn("--round-log-days", result.stdout)
        self.assertIn("兼容子命令", result.stdout)
        restore_help = subprocess.run(
            [sys.executable, "-X", "utf8", "scripts/runtime_restore.py", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        self.assertEqual(restore_help.returncode, 0, restore_help.stderr)
        self.assertIn("--archive", restore_help.stdout)
        self.assertIn("--verify-off-disk", restore_help.stdout)
        self.assertIn("--status", restore_help.stdout)

if __name__ == "__main__":
    unittest.main()
