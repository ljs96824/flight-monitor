"""服务进程版本信标。"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def resolve_build_id(project_root: Path = PROJECT_ROOT, *, runner=subprocess.run) -> str:
    """优先读取 Git 短哈希，Git 不可用时回退 VERSION。"""
    try:
        completed = runner(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
        )
        candidate = str(getattr(completed, "stdout", "") or "").strip()
        if getattr(completed, "returncode", 1) == 0 and candidate:
            return candidate.splitlines()[0]
    except (OSError, subprocess.SubprocessError):
        pass

    version_path = project_root / "VERSION"
    try:
        candidate = version_path.read_text(encoding="utf-8").strip()
    except OSError:
        candidate = ""
    return candidate.splitlines()[0] if candidate else "unknown"


@dataclass(frozen=True)
class BuildInfo:
    build_id: str
    started_at: datetime

    def format_marker(self, port) -> str:
        port_text = str(port or "?").strip().lstrip(":") or "?"
        return (
            f"build {self.build_id} · 启动 {self.started_at:%m-%d %H:%M}"
            f" · :{port_text}"
        )


PROCESS_BUILD_INFO = BuildInfo(
    build_id=resolve_build_id(),
    started_at=datetime.now().astimezone(),
)
