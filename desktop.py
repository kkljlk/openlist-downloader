"""Desktop entry point: a native window instead of an external browser.

The content is still the local web UI, but it is hosted inside a WebView2
window.  If pywebview/WebView2 is unavailable, the app gracefully falls back
to the system browser.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from app import _ping, _say, setup_logging
from ol_downloader.config import APP_NAME, APP_VERSION, Config, data_dir, resource_dir
from ol_downloader.web import AppServer

LOG = logging.getLogger("openlist_desktop")
DEFAULT_PORT = 8765


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--no-window", action="store_true", help="使用外部浏览器")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    directory = Path(args.data_dir).resolve() if args.data_dir else data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    setup_logging(directory / "app.log", verbose=args.verbose)
    LOG.info("%s desktop %s starting", APP_NAME, APP_VERSION)
    config = Config(directory)
    static_dir = resource_dir() / "static"
    if not (static_dir / "index.html").is_file():
        _say(f"找不到界面文件：{static_dir}")
        return 1

    preferred = args.port or DEFAULT_PORT
    if _ping(preferred):
        url = f"http://127.0.0.1:{preferred}/"
        _say(f"应用已在运行：{url}")
        if args.no_window:
            webbrowser.open(url, new=2)
            return 0
        if _open_app_window(url, directory):
            return 0
        webbrowser.open(url, new=2)
        return 0

    server: AppServer | None = None
    last_error: OSError | None = None
    ports = [preferred] + [p for p in range(DEFAULT_PORT, DEFAULT_PORT + 20) if p != preferred]
    for port in ports:
        try:
            server = AppServer(("127.0.0.1", port), config, static_dir)
            break
        except OSError as exc:
            last_error = exc
    if server is None:
        _say(f"无法启动本地服务：{last_error}")
        return 1

    url = f"http://127.0.0.1:{server.server_port}/"
    LOG.info("listening on %s", url)
    _say(f"OpenList 下载器已启动：{url}")

    def warm_aria2() -> None:
        try:
            server.state.aria2.ensure_ready(auto_start=True)
        except Exception:
            LOG.exception("aria2 warm-up failed")

    threading.Thread(target=warm_aria2, name="aria2-warmup", daemon=True).start()
    server_thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.35},
        name="http-server",
        daemon=True,
    )
    server_thread.start()

    try:
        if args.no_window:
            webbrowser.open(url, new=2)
            while True:
                time.sleep(1)
        return _show_window(url, args.verbose, directory)
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            server.state.tickets.flush()
        except Exception:
            LOG.exception("could not flush tickets")
        server.shutdown()
        server.server_close()
        LOG.info("server stopped")


def _browser_exe() -> str:
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return ""


def _open_app_window(url: str, data_dir: Path) -> bool:
    """Open a standalone Edge/Chrome app window (no address bar)."""
    exe = _browser_exe()
    if not exe:
        return False
    profile = data_dir / "window-profile"
    try:
        profile.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [
                exe,
                f"--app={url}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--window-size=1280,860",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return True
    except OSError as exc:
        LOG.warning("standalone browser window failed: %s", exc)
        return False


def _show_window(url: str, debug: bool, data_dir: Path) -> int:
    # The packaged build uses the Edge/Chrome app window: it is a standalone
    # window without an address bar, and avoids shipping the .NET loader.
    if getattr(sys, "frozen", False) and _open_app_window(url, data_dir):
        # Keep the local server alive.  Edge's launcher process may hand the
        # window to an existing browser process and exit immediately; if we
        # returned here the window would show ERR_CONNECTION_REFUSED.
        while True:
            time.sleep(1)
    try:
        import webview  # type: ignore
    except ImportError:
        webview = None
    if webview is not None:
        if "ALLOW_DOWNLOADS" in getattr(webview, "settings", {}):
            webview.settings["ALLOW_DOWNLOADS"] = True
        try:
            webview.create_window(
                APP_NAME,
                url,
                width=1280,
                height=860,
                min_size=(960, 640),
                background_color="#080c11",
                text_select=True,
            )
            webview.start(debug=debug)
            return 0
        except Exception as exc:  # pragma: no cover - depends on WebView2
            LOG.exception("WebView window failed: %s", exc)
    if _open_app_window(url, data_dir):
        return 0
    _say("独立窗口不可用，已改用默认浏览器")
    webbrowser.open(url, new=2)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())