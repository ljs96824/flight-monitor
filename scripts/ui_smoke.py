"""UX 3.0 本地 Chromium 系浏览器烟雾测试。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "scripts" / "ui_smoke_driver.mjs"
DEFAULT_ARTIFACT_DIR = ROOT / "data" / "ui-smoke-artifacts"


def _default_browser_candidates(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> tuple[Path, ...]:
    platform_name = platform_name or sys.platform
    environ = environ if environ is not None else os.environ
    which = which or shutil.which
    candidates: list[Path] = []

    def add(path: str | Path | None) -> None:
        if not path:
            return
        candidate = Path(path)
        if candidate not in candidates:
            candidates.append(candidate)

    if platform_name.startswith("win"):
        program_files = environ.get("PROGRAMFILES", "")
        program_files_x86 = environ.get("PROGRAMFILES(X86)", "")
        local_app_data = environ.get("LOCALAPPDATA", "")
        for root in (program_files_x86, program_files):
            if root:
                add(Path(root) / "Microsoft/Edge/Application/msedge.exe")
                add(Path(root) / "Google/Chrome/Application/chrome.exe")
        if local_app_data:
            add(Path(local_app_data) / "Microsoft/Edge/Application/msedge.exe")
            add(Path(local_app_data) / "Google/Chrome/Application/chrome.exe")
        command_names = ("msedge", "chrome", "chromium")
    elif platform_name == "darwin":
        add("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
        add("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        add("/Applications/Chromium.app/Contents/MacOS/Chromium")
        command_names = ("microsoft-edge", "google-chrome", "chromium")
    else:
        command_names = (
            "microsoft-edge-stable",
            "microsoft-edge",
            "google-chrome-stable",
            "google-chrome",
            "chromium",
            "chromium-browser",
        )
        for name in command_names:
            add(Path("/usr/bin") / name)
        add("/snap/bin/chromium")
    for name in command_names:
        add(which(name))
    return tuple(candidates)


BROWSER_CANDIDATES = _default_browser_candidates()

_BROWSER_BLOCKED_PORTS = frozenset(
    {
        1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53,
        69, 77, 79, 87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115,
        117, 119, 123, 135, 137, 139, 143, 161, 179, 389, 427, 465, 512,
        513, 514, 515, 526, 530, 531, 532, 540, 548, 554, 556, 563, 587,
        601, 636, 989, 990, 993, 995, 1719, 1720, 1723, 2049, 3659, 4045,
        4190, 5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668, 6669, 6697,
        10080,
    }
)


def _free_port() -> int:
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in _BROWSER_BLOCKED_PORTS:
            return port


def _wait_http(url: str, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status < 500:
                    return
        except OSError:
            time.sleep(0.15)
    raise RuntimeError(f"等待服务超时: {url}")


def _browser_path() -> Path:
    probed: list[Path] = []
    for variable in ("BROWSER_PATH", "EDGE_PATH"):
        override = os.environ.get(variable)
        if not override:
            continue
        candidate = Path(override).expanduser()
        probed.append(candidate)
        if candidate.is_file():
            return candidate
    for candidate in BROWSER_CANDIDATES:
        probed.append(candidate)
        if candidate.is_file():
            return candidate
    checked = "\n".join(f"- {path}" for path in probed) or "- 无平台默认候选"
    raise RuntimeError(
        "未找到可用的 Edge/Chrome/Chromium；可用 BROWSER_PATH 指定浏览器"
        f"（Windows 仍兼容 EDGE_PATH）。已探测:\n{checked}"
    )


def _node_path() -> str:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("未找到 Node.js；CDP 驱动需要 Node 22+ 内置 WebSocket")
    return node


def _file_presence_and_sha256(path: Path) -> tuple[bool, str | None]:
    if not path.exists():
        return False, None
    return True, hashlib.sha256(path.read_bytes()).hexdigest()


def _browser_command(
    browser: Path,
    *,
    cdp_port: int,
    profile_dir: Path,
    base_url: str,
    platform_name: str | None = None,
) -> list[str]:
    command = [
        str(browser),
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={profile_dir}",
        base_url + "/",
    ]
    if (platform_name or sys.platform).startswith("linux"):
        command.insert(2, "--no-sandbox")
    return command


def _serve(port: int, data_dir: Path) -> None:
    sys.path.insert(0, str(ROOT))
    import web_form

    data_dir.mkdir(parents=True, exist_ok=True)
    web_form.SUBSCRIPTIONS_PATH = data_dir / "subscriptions.json"
    web_form.FEEDBACK_PATH = data_dir / "feedback.json"
    web_form.PAGE_PAYLOADS_DIR = data_dir / "payloads"
    web_form.start_background_collection = lambda _subscription: {"status": "started", "entrypoint": "ui_smoke"}
    web_form.load_calendar = lambda _route: []
    web_form.app.run(
        host="127.0.0.1",
        port=port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


def _write_failure_logs(
    output: str,
    server_output: str,
    *,
    log_path: Path,
    artifact_dir: Path,
) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(output, encoding="utf-8")
    (artifact_dir / "server.log").write_text(server_output, encoding="utf-8")


def run_smoke(*, log_path: Path | None = None, artifact_dir: Path | None = None) -> int:
    artifact_dir = artifact_dir or DEFAULT_ARTIFACT_DIR
    log_path = log_path or artifact_dir / "ui-smoke.log"
    production_subscriptions = ROOT / "data" / "subscriptions.json"
    production_subscriptions_before = _file_presence_and_sha256(
        production_subscriptions
    )
    try:
        browser = _browser_path()
    except Exception as exc:
        output = f"[UI smoke] 结果=FAIL 原因={exc}\n"
        print(output, end="")
        _write_failure_logs(output, "", log_path=log_path, artifact_dir=artifact_dir)
        return 1
    node = _node_path()
    app_port = _free_port()
    cdp_port = _free_port()
    base_url = f"http://127.0.0.1:{app_port}"
    lines = [
        f"[UI smoke] Browser={browser}",
        f"[UI smoke] URL={base_url}",
        "[UI smoke] 模式=本地Chromium CDP，零Selenium/零外部API",
    ]

    with tempfile.TemporaryDirectory(prefix="flight-ui-smoke-", ignore_cleanup_errors=True) as tmpdir:
        tmp = Path(tmpdir)
        server_log_path = tmp / "server-process.log"
        server_log_stream = server_log_path.open(
            "w",
            encoding="utf-8",
            errors="replace",
            newline="",
        )
        server = subprocess.Popen(
            [
                sys.executable,
                "-X",
                "utf8",
                str(Path(__file__).resolve()),
                "--serve",
                "--port",
                str(app_port),
                "--data-dir",
                str(tmp / "data"),
            ],
            cwd=ROOT,
            stdout=server_log_stream,
            stderr=subprocess.STDOUT,
        )
        edge_process = None
        try:
            _wait_http(base_url + "/")
            edge_process = subprocess.Popen(
                _browser_command(
                    browser,
                    cdp_port=cdp_port,
                    profile_dir=tmp / "browser-profile",
                    base_url=base_url,
                ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _wait_http(f"http://127.0.0.1:{cdp_port}/json/version")
            driver_command = [
                node,
                str(DRIVER),
                base_url,
                str(cdp_port),
                str(artifact_dir),
                str(tmp / "data" / "subscriptions.json"),
            ]
            try:
                completed = subprocess.run(
                    driver_command,
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                partial_stdout = exc.stdout or ""
                partial_stderr = exc.stderr or ""
                if isinstance(partial_stdout, bytes):
                    partial_stdout = partial_stdout.decode("utf-8", errors="replace")
                if isinstance(partial_stderr, bytes):
                    partial_stderr = partial_stderr.decode("utf-8", errors="replace")
                if partial_stdout.strip():
                    lines.extend(partial_stdout.strip().splitlines())
                if partial_stderr.strip():
                    lines.extend(partial_stderr.strip().splitlines())
                raise
            if completed.stdout.strip():
                lines.extend(completed.stdout.strip().splitlines())
            if completed.stderr.strip():
                lines.extend(completed.stderr.strip().splitlines())
            if completed.returncode:
                raise RuntimeError(f"浏览器契约失败，exit={completed.returncode}")
            lines.append("[UI smoke] 结果=PASS")
            return_code = 0
        except Exception as exc:
            lines.append(f"[UI smoke] 结果=FAIL 原因={exc}")
            return_code = 1
        finally:
            if edge_process is not None:
                edge_process.terminate()
                try:
                    edge_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    edge_process.kill()
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            server_log_stream.close()
            server_output = server_log_path.read_text(
                encoding="utf-8",
                errors="replace",
            )
            if server_output.strip():
                lines.append("[UI smoke] 本地服务日志:")
                lines.extend(server_output.strip().splitlines()[-20:])

    production_subscriptions_after = _file_presence_and_sha256(
        production_subscriptions
    )
    if production_subscriptions_after != production_subscriptions_before:
        lines.append(
            "[UI smoke] 结果=FAIL 原因=生产subscriptions存在性或SHA发生变化"
        )
        return_code = 1
    else:
        exists, digest = production_subscriptions_after
        lines.append(
            "[UI smoke] 生产subscriptions状态=PASS "
            f"exists={exists} sha256={digest or 'not-applicable'}"
        )

    output = "\n".join(lines) + "\n"
    print(output, end="")
    if return_code:
        _write_failure_logs(
            output,
            server_output,
            log_path=log_path,
            artifact_dir=artifact_dir,
        )
    return return_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--log-path", type=Path, help="失败时写入完整 smoke 日志")
    parser.add_argument("--artifact-dir", type=Path, help="失败时写入浏览器与服务端证据")
    args = parser.parse_args(argv)
    if args.serve:
        if not args.port or not args.data_dir:
            parser.error("--serve 需要 --port 与 --data-dir")
        _serve(args.port, args.data_dir)
        return 0
    return run_smoke(log_path=args.log_path, artifact_dir=args.artifact_dir)


if __name__ == "__main__":
    raise SystemExit(main())
