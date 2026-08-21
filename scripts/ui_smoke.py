"""UX 3.0 本地 Edge 无头浏览器烟雾测试。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "scripts" / "ui_smoke_driver.mjs"
EDGE_CANDIDATES = (
    Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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


def _edge_path() -> Path:
    override = os.environ.get("EDGE_PATH")
    if override and Path(override).is_file():
        return Path(override)
    for candidate in EDGE_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise RuntimeError("未找到 Microsoft Edge；可用 EDGE_PATH 指定 msedge.exe")


def _node_path() -> str:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("未找到 Node.js；CDP 驱动需要 Node 22+ 内置 WebSocket")
    return node


def _serve(port: int, data_dir: Path) -> None:
    sys.path.insert(0, str(ROOT))
    import web_form

    data_dir.mkdir(parents=True, exist_ok=True)
    web_form.SUBSCRIPTIONS_PATH = data_dir / "subscriptions.json"
    web_form.FEEDBACK_PATH = data_dir / "feedback.json"
    web_form.PAGE_PAYLOADS_DIR = data_dir / "payloads"
    web_form.start_background_collection = lambda _subscription: None
    web_form.load_calendar = lambda _route: []
    web_form.app.run(
        host="127.0.0.1",
        port=port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


def run_smoke() -> int:
    edge = _edge_path()
    node = _node_path()
    app_port = _free_port()
    cdp_port = _free_port()
    base_url = f"http://127.0.0.1:{app_port}"
    lines = [
        f"[UI smoke] Edge={edge}",
        f"[UI smoke] URL={base_url}",
        "[UI smoke] 模式=本地Edge CDP，零Selenium/零外部API",
    ]

    with tempfile.TemporaryDirectory(prefix="flight-ui-smoke-", ignore_cleanup_errors=True) as tmpdir:
        tmp = Path(tmpdir)
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
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        edge_process = None
        try:
            _wait_http(base_url + "/")
            edge_process = subprocess.Popen(
                [
                    str(edge),
                    "--headless=new",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    f"--remote-debugging-port={cdp_port}",
                    f"--user-data-dir={tmp / 'edge-profile'}",
                    base_url + "/",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _wait_http(f"http://127.0.0.1:{cdp_port}/json/version")
            completed = subprocess.run(
                [node, str(DRIVER), base_url, str(cdp_port)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
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
                server_output, _ = server.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server_output, _ = server.communicate()
            if server_output.strip():
                lines.append("[UI smoke] 本地服务日志:")
                lines.extend(server_output.strip().splitlines()[-20:])

    output = "\n".join(lines) + "\n"
    print(output, end="")
    log_path = ROOT / "data" / "ui_smoke_latest.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(output, encoding="utf-8")
    return return_code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    if args.serve:
        if not args.port or not args.data_dir:
            parser.error("--serve 需要 --port 与 --data-dir")
        _serve(args.port, args.data_dir)
        return 0
    return run_smoke()


if __name__ == "__main__":
    raise SystemExit(main())