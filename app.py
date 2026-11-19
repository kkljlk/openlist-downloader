"""Entry point for OpenList 下载器."""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from ol_downloader.config import APP_ID, APP_NAME, APP_VERSION, Config, data_dir, resource_dir
from ol_downloader.web import AppServer

LOG = logging.getLogger("openlist_downloader")
DEFAULT_PORT = 8765


def _say(message: str) -> None:
    """Print when a console exists; windowed builds have sys.stdout=None."""
    LOG.info(message)
    stream = getattr(sys, "stdout", None)
    if stream is not None:
        try:
            print(message, flush=True)
        except (OSError, ValueError):
            pass


def setup_logging(log_path: Path, verbose: bool = False) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=2 * 1024 * 1024,
            backupCount=2,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        file_handler.setLevel(logging.DEBUG)
        root.addHandler(file_handler)
    except OSError:
        pass
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        console.setLevel(logging.DEBUG if verbose else logging.INFO)
        root.addHandler(console)


def _ping(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/ping", timeout=0.7
        ) as resp:
            data = json.loads(resp.read(4096).decode("utf-8"))
        return bool(data.get("ok") and data.get("data", {}).get("app") == APP_ID)
    except Exception:
        return False


def _open_browser(url: str) -> None:
    def run() -> None:
        time.sleep(0.4)
        try:
            webbrowser.open(url, new=2)
        except Exception:
            LOG.exception("could not open browser")

    threading.Thread(target=run, daemon=True).start()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认自动选择）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--data-dir", default=None, help="自定义数据目录（测试用）")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--version", action="version", version=APP_VERSION)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    directory = Path(args.data_dir).resolve() if args.data_dir else data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    setup_logging(directory / "app.log", verbose=args.verbose)
    LOG.info("%s %s starting", APP_NAME, APP_VERSION)
    config = Config(directory)
    static_dir = resource_dir() / "static"
    if not (static_dir / "index.html").is_file():
        LOG.error("static files not found: %s", static_dir)
        _say(f"找不到界面文件：{static_dir}")
        return 1

    preferred = args.port
    if preferred is None:
        preferred = DEFAULT_PORT
    if preferred:
        if _ping(preferred):
            url = f"http://127.0.0.1:{preferred}/"
            LOG.info("application already running at %s", url)
            if not args.no_browser:
                webbrowser.open(url, new=2)
            _say(f"{APP_NAME} 已在运行：{url}")
            return 0
        ports = [preferred] + [p for p in range(DEFAULT_PORT, DEFAULT_PORT + 20) if p != preferred]
    else:
        ports = [0]

    server: AppServer | None = None
    last_error: OSError | None = None
    for port in ports:
        try:
            server = AppServer(("127.0.0.1", port), config, static_dir)
            break
        except OSError as exc:
            last_error = exc
            if args.port is not None:
                break
    if server is None:
        LOG.error("could not bind server: %s", last_error)
        _say(f"无法启动本地服务：{last_error}")
        return 1

    actual_port = server.server_port
    url = f"http://127.0.0.1:{actual_port}/"
    LOG.info("listening on %s", url)
    _say(f"OpenList 下载器已启动：{url}")
    _say(f"OPENLIST_DOWNLOADER_URL={url}")
    _say(f"OPENLIST_DOWNLOADER_DATA={directory}")

    # Start aria2 in the background so the UI is usable immediately.
    def warm_aria2() -> None:
        try:
            server.state.aria2.ensure_ready(auto_start=True)
        except Exception:
            LOG.exception("aria2 warm-up failed")

    threading.Thread(target=warm_aria2, name="aria2-warmup", daemon=True).start()
    if not args.no_browser:
        _open_browser(url)
    try:
        server.serve_forever(poll_interval=0.35)
    except KeyboardInterrupt:
        LOG.info("interrupted")
    except Exception:
        LOG.exception("server crashed")
        return 1
    finally:
        try:
            server.state.tickets.flush()
        except Exception:
            LOG.exception("could not flush download tickets")
        server.server_close()
        LOG.info("server stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())