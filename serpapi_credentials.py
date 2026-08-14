"""SerpAPI 密钥别名解析与安全诊断。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Mapping

from log_utils import safe_log


SERPAPI_KEY_ALIASES = (
    "SERPAPI_KEY",
    "SERPAPI_API_KEY",
    "SERP_API_KEY",
)
_DOTENV_NAME_RE = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*="
)


def resolve_serpapi_key(
    environment: Mapping[str, str] | None = None,
    *,
    announce: bool = True,
    logger: Callable[[object], None] = safe_log,
) -> tuple[str | None, str | None]:
    """按稳定优先级返回密钥及来源变量名，日志永不包含密钥值。"""
    values = environment if environment is not None else os.environ
    for variable_name in SERPAPI_KEY_ALIASES:
        value = str(values.get(variable_name) or "").strip()
        if not value:
            continue
        if announce:
            logger(f"[密钥] 已识别 来源变量={variable_name}")
        return value, variable_name
    return None, None


def serpapi_key_available(environment: Mapping[str, str] | None = None) -> bool:
    """静默判断任一受支持别名是否可用。"""
    key, _ = resolve_serpapi_key(environment, announce=False)
    return bool(key)


def dotenv_variable_names(path: str | Path) -> list[str]:
    """只读取 dotenv 左侧变量名，不解析、返回或记录变量值。"""
    env_path = Path(path)
    if not env_path.is_file():
        return []
    names = set()
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        match = _DOTENV_NAME_RE.match(line)
        if match:
            names.add(match.group(1))
    return sorted(names)
