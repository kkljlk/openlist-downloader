"""HTTP server, JSON API and static-file hosting for the downloader."""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import secrets
import socket
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .aria2 import Aria2Error, Aria2Manager
from .config import (
    APP_ID,
    APP_NAME,
    APP_VERSION,
    Config,
    default_download_dir,
    downloads_dir,
    sanitize_filename,
)
from .openlist import OpenListClient, OpenListError, normalize_path, probe_site
from .scanner import SiteScan, expand_items
from .transfers import (
    ClientDisconnected,
    DownloadError,
    DownloadStore,
    TicketStore,
    stream_job,
    stream_ticket,
)
from . import yunx

LOG = logging.getLogger(__name__)
COOKIE_NAME = "old_sid"
SESSION_TTL = 7 * 24 * 3600
MAX_BODY = 32 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, *, auth: bool = False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.auth = auth


@dataclass
class Session:
    id: str
    site_id: str
    site_name: str
    url: str
    username: str
    role: str
    client: OpenListClient
    created: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "siteId": self.site_id,
            "siteName": self.site_name,
            "url": self.url,
            "username": self.username or "访客",
            "role": self.role,
            "connected": True,
            "insecure": self.client.insecure,
        }


def _now() -> float:
    return time.time()


class AppState:
    def __init__(self, config: Config, static_dir: Path, port: int):
        self.config = config
        self.static_dir = static_dir
        self.port = port
        self.origin = f"http://127.0.0.1:{port}"
        self.sessions: dict[str, Session] = {}
        self.session_lock = threading.RLock()
        self.tickets = TicketStore(path=config.dir / "tickets.json")
        self.downloads = DownloadStore()
        self.scans: dict[str, SiteScan] = {}
        self.scan_owner: dict[str, str] = {}
        self.scans_lock = threading.RLock()
        self.aria2 = Aria2Manager(config, config.dir)
        self.started = time.time()
        self._detect_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])

    # -- sessions --------------------------------------------------------
    def new_session(self, site: dict[str, Any], client: OpenListClient, user: dict[str, Any]) -> Session:
        sid = secrets.token_urlsafe(24)
        session = Session(
            id=sid,
            site_id=str(site.get("id") or ""),
            site_name=str(site.get("name") or site.get("url") or ""),
            url=client.base_url,
            username=str(user.get("username") or user.get("name") or ""),
            role=str(user.get("role") or ""),
            client=client,
        )
        with self.session_lock:
            self._prune_sessions_locked()
            self.sessions[sid] = session
        self.config.set(session.site_id, "active_site")
        self.config.upsert_site(
            {
                **site,
                "id": session.site_id,
                "last_used": time.time(),
                "insecure": bool(client.insecure),
            }
        )
        return session

    def get_session(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        with self.session_lock:
            session = self.sessions.get(sid)
            if session is None:
                return None
            if _now() - session.created > SESSION_TTL:
                self.sessions.pop(sid, None)
                return None
            session.last_used = _now()
            return session

    def drop_session(self, sid: str | None) -> None:
        if sid:
            with self.session_lock:
                self.sessions.pop(sid, None)

    def drop_site_sessions(self, site_id: str) -> None:
        with self.session_lock:
            for sid, session in list(self.sessions.items()):
                if session.site_id == site_id:
                    self.sessions.pop(sid, None)

    def _prune_sessions_locked(self) -> None:
        cutoff = _now() - SESSION_TTL
        for sid, session in list(self.sessions.items()):
            if session.created < cutoff:
                self.sessions.pop(sid, None)

    def require_session(self, sid: str | None) -> Session:
        session = self.get_session(sid)
        if session is None:
            raise ApiError("请先选择一个资源站", 401, auth=True)
        return session

    # -- site connect ----------------------------------------------------
    def connect(
        self,
        site: dict[str, Any],
        *,
        username: str = "",
        password: str = "",
        token: str = "",
        otp_code: str = "",
    ) -> Session:
        client = OpenListClient(
            str(site.get("url") or ""),
            token=token.strip() if token else None,
            insecure=bool(site.get("insecure")),
        )
        user: dict[str, Any] = {}
        if token.strip():
            client.list_dir("/")
            user = _safe_me(client)
        elif username or password:
            client.login(username.strip(), password, otp_code.strip())
            user = _safe_me(client)
            client.list_dir("/")
        else:
            client.list_dir("/")
            user = _safe_me(client)
        if client.insecure and not site.get("insecure"):
            self.config.upsert_site({**site, "insecure": True})
        return self.new_session(site, client, user)

    # -- local discovery -------------------------------------------------
    def detect_local(self) -> list[dict[str, Any]]:
        cached_at, cached = self._detect_cache
        if _now() - cached_at < 60:
            return cached
        found: list[dict[str, Any]] = []
        candidates: list[str] = []
        env_url = (os.environ.get("OPENLIST_URL") or "").strip()
        if env_url:
            candidates.append(env_url)
        if _tcp_open("127.0.0.1", 5244):
            candidates.extend(["http://127.0.0.1:5244", "http://localhost:5244"])
        seen = set()
        for url in candidates:
            try:
                info = probe_site(url)
            except OpenListError:
                continue
            key = info["url"].lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "url": info["url"],
                    "name": info["name"],
                    "version": info["version"],
                    "auth_required": info["auth_required"],
                }
            )
        self._detect_cache = (_now(), found)
        return found

    def bootstrap(self, sid: str | None) -> dict[str, Any]:
        session = self.get_session(sid)
        sites = [_public_site(site) for site in self.config.sites]
        return {
            "app": {
                "id": APP_ID,
                "name": APP_NAME,
                "version": APP_VERSION,
                "port": self.port,
                "started": self.started,
            },
            "sites": sites,
            "activeSite": self.config.get("active_site", default=""),
            "session": session.to_dict() if session else None,
            "detected": self.detect_local() if not sites else [],
            "aria2": self.aria2.status(),
            "yunx": yunx.status(),
            "preferences": self.config.get("preferences", default={}),
            "directories": {
                "downloads": str(downloads_dir()),
                "default": default_download_dir(),
            },
            "limits": {"maxFiles": 100_000, "maxBody": MAX_BODY},
        }

    def prune_scans(self) -> None:
        with self.scans_lock:
            now = _now()
            for scan_id, scan in list(self.scans.items()):
                with scan.lock:
                    finished = scan.finished_at
                if finished and now - finished > 30 * 60:
                    self.scans.pop(scan_id, None)
                    self.scan_owner.pop(scan_id, None)

    def scan_for(self, sid: str, scan_id: str) -> SiteScan:
        with self.scans_lock:
            scan = self.scans.get(scan_id)
            owner = self.scan_owner.get(scan_id)
        if scan is None or owner != sid:
            raise ApiError("扫描任务不存在或已过期", 404)
        return scan


def _safe_me(client: OpenListClient) -> dict[str, Any]:
    try:
        return client.me()
    except OpenListError:
        return {}


def _public_site(site: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": site.get("id", ""),
        "name": site.get("name") or site.get("url", ""),
        "url": site.get("url", ""),
        "version": site.get("version", ""),
        "auth_required": bool(site.get("auth_required")),
        "insecure": bool(site.get("insecure")),
        "last_used": site.get("last_used", 0),
    }


def _tcp_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


class ResponseWriterImpl:
    def __init__(self, handler: BaseHTTPRequestHandler):
        self.handler = handler
        self.headers_sent = False
        self.chunked = False
        self.finished = False
        self.bytes_written = 0

    def send_headers(
        self, status: int, headers: dict[str, str], *, chunked: bool = False
    ) -> None:
        if self.headers_sent:
            return
        clean: dict[str, str] = {}
        for key, value in headers.items():
            lower = key.lower()
            if lower in ("connection", "transfer-encoding", "server", "date"):
                continue
            clean[key] = value
        if chunked:
            clean["Transfer-Encoding"] = "chunked"
        self.chunked = chunked
        self.handler.send_response(status)
        for key, value in clean.items():
            self.handler.send_header(key, value)
        if not chunked and "Content-Length" not in clean and status not in (204, 304):
            self.handler.send_header("Connection", "close")
            self.handler.close_connection = True
        self.handler.end_headers()
        self.headers_sent = True

    def write(self, data: bytes) -> None:
        if not data:
            return
        try:
            if self.chunked:
                self.handler.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
                self.handler.wfile.write(data)
                self.handler.wfile.write(b"\r\n")
            else:
                self.handler.wfile.write(data)
            self.bytes_written += len(data)
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ClientDisconnected() from exc

    def tell(self) -> int:
        return self.bytes_written

    def flush(self) -> None:
        try:
            self.handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def seekable(self) -> bool:
        return False

    def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        try:
            if self.chunked:
                self.handler.wfile.write(b"0\r\n\r\n")
            self.handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class AppHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OpenListDownloader/1.1"
    sys_version = ""

    @property
    def state(self) -> AppState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        # Do not write URLs: they may contain capability tokens.
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, POST, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    # -- plumbing --------------------------------------------------------
    def _handle(self, method: str) -> None:
        try:
            # The handler instance is reused for keep-alive requests.
            self._raw_body = None
            if method in ("POST", "DELETE"):
                self._check_origin()
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            if path.startswith("/api/"):
                self._handle_api(method, path, urllib.parse.parse_qs(parsed.query))
                return
            if method in ("GET", "HEAD"):
                self._handle_static(path, method == "HEAD")
                return
            self._send_json({"ok": False, "error": {"message": "不支持的请求"}}, 405)
        except ApiError as exc:
            self._send_api_error(exc.status, exc.message, auth=exc.auth)
        except OpenListError as exc:
            self._send_api_error(
                401 if exc.auth else 502,
                exc.message,
                auth=exc.auth,
                code=exc.code,
            )
        except DownloadError as exc:
            self._send_api_error(exc.status, exc.message)
        except Aria2Error as exc:
            self._send_api_error(503, exc.message)
        except (BrokenPipeError, ConnectionResetError, ClientDisconnected):
            self.close_connection = True
        except Exception as exc:  # pragma: no cover - defensive
            LOG.exception("unhandled request error")
            try:
                self._send_api_error(500, f"服务器内部错误：{exc}")
            except Exception:
                self.close_connection = True

    def _check_origin(self) -> None:
        origin = self.headers.get("Origin", "")
        if not origin:
            return
        host = self.headers.get("Host", "")
        allowed = {f"http://{host}", f"https://{host}"}
        if origin not in allowed:
            raise ApiError("跨站请求已被拒绝", 403)

    def _cookie_sid(self) -> str:
        raw = self.headers.get("Cookie", "")
        if not raw:
            return ""
        try:
            cookie = SimpleCookie()
            cookie.load(raw)
            morsel = cookie.get(COOKIE_NAME)
            return morsel.value if morsel else ""
        except Exception:
            return ""

    def _set_cookie(self, sid: str) -> None:
        self.send_header(
            "Set-Cookie",
            f"{COOKIE_NAME}={sid}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}",
        )

    def _clear_cookie(self) -> None:
        self.send_header(
            "Set-Cookie",
            f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
        )

    def _read_raw_body(self) -> bytes:
        cached = getattr(self, "_raw_body", None)
        if cached is not None:
            return cached
        length_header = self.headers.get("Content-Length", "0")
        try:
            length = int(length_header or "0")
        except ValueError:
            raise ApiError("请求长度无效", 400)
        if length > MAX_BODY:
            raise ApiError("请求内容过大", 413)
        self._raw_body = self.rfile.read(length) if length > 0 else b""
        return self._raw_body

    def _read_json(self) -> dict[str, Any]:
        raw = self._read_raw_body()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ApiError("请求内容不是有效的 JSON", 400) from exc
        if not isinstance(data, dict):
            raise ApiError("请求内容格式不正确", 400)
        return data

    def _send_json(
        self,
        payload: dict[str, Any],
        status: int = 200,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_api_error(
        self,
        status: int,
        message: str,
        *,
        auth: bool = False,
        code: Any = None,
    ) -> None:
        error: dict[str, Any] = {"message": message, "auth": bool(auth)}
        if code is not None:
            error["code"] = code
        self._send_json({"ok": False, "error": error}, status)

    def _ok(self, data: Any, *, cookie: str | None = None) -> None:
        headers = {"Set-Cookie": cookie} if cookie else None
        self._send_json({"ok": True, "data": data}, 200, headers=headers)

    # -- static ----------------------------------------------------------
    def _handle_static(self, path: str, head: bool) -> None:
        if path in ("/", "/index.html"):
            target = self.state.static_dir / "index.html"
        elif path == "/favicon.svg":
            target = self.state.static_dir / "favicon.svg"
        elif path.startswith("/static/"):
            rel = path[len("/static/") :]
            if not rel or ".." in rel:
                raise ApiError("文件不存在", 404)
            target = self.state.static_dir / rel
        else:
            raise ApiError("页面不存在", 404)
        try:
            target = target.resolve()
            root = self.state.static_dir.resolve()
            if root != target and root not in target.parents:
                raise ApiError("文件不存在", 404)
            body = target.read_bytes()
        except (OSError, ApiError):
            raise ApiError("文件不存在", 404)
        mime, _ = mimetypes.guess_type(str(target))
        if target.suffix == ".js":
            mime = "text/javascript; charset=utf-8"
        elif target.suffix == ".css":
            mime = "text/css; charset=utf-8"
        elif target.suffix == ".html":
            mime = "text/html; charset=utf-8"
        elif target.suffix == ".svg":
            mime = "image/svg+xml"
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    # -- API routing -----------------------------------------------------
    def _handle_api(
        self, method: str, path: str, query: dict[str, list[str]]
    ) -> None:
        state = self.state
        sid = self._cookie_sid()
        if method == "POST":
            # Always consume the JSON body, even for endpoints that do not
            # need it. Otherwise HTTP keep-alive would parse the leftover
            # "{}" as the method of the next request.
            self._read_raw_body()

        if path == "/api/ping" and method == "GET":
            self._ok({"app": APP_ID, "version": APP_VERSION, "port": state.port})
            return

        if path == "/api/bootstrap" and method == "GET":
            self._send_json(
                {"ok": True, "data": state.bootstrap(sid)},
                200,
            )
            return

        if path == "/api/session" and method == "GET":
            session = state.get_session(sid)
            self._ok({"session": session.to_dict() if session else None})
            return

        if path == "/api/sites" and method == "GET":
            self._ok({"sites": [_public_site(s) for s in state.config.sites]})
            return

        if path == "/api/sites" and method == "POST":
            data = self._read_json()
            url = str(data.get("url") or "").strip()
            try:
                info = probe_site(url)
            except OpenListError as exc:
                raise ApiError(exc.message, 400) from exc
            existing = next(
                (
                    site
                    for site in state.config.sites
                    if str(site.get("url", "")).rstrip("/").lower()
                    == info["url"].rstrip("/").lower()
                ),
                None,
            )
            site = {
                "id": (existing or {}).get("id") or uuid.uuid4().hex[:12],
                "url": info["url"],
                "name": str(data.get("name") or info["name"] or info["url"]),
                "version": info["version"],
                "auth_required": info["auth_required"],
                "insecure": info["insecure"],
                "last_used": (existing or {}).get("last_used") or 0,
            }
            saved = state.config.upsert_site(site)
            self._ok({"site": _public_site(saved)})
            return

        match = re.fullmatch(r"/api/sites/([A-Za-z0-9_-]+)", path)
        if match and method == "DELETE":
            site_id = match.group(1)
            if not state.config.remove_site(site_id):
                raise ApiError("资源站不存在", 404)
            state.drop_site_sessions(site_id)
            self._ok({"removed": True})
            return

        match = re.fullmatch(r"/api/sites/([A-Za-z0-9_-]+)", path)
        if match and method == "POST":
            data = self._read_json()
            site = state.config.find_site(match.group(1))
            if not site:
                raise ApiError("资源站不存在", 404)
            name = str(data.get("name") or "").strip()
            if not name:
                raise ApiError("请输入名称", 400)
            site["name"] = name[:80]
            state.config.upsert_site(site)
            self._ok({"site": _public_site(site)})
            return

        if path == "/api/connect" and method == "POST":
            data = self._read_json()
            site_id = str(data.get("site_id") or "")
            site = state.config.find_site(site_id)
            if not site:
                raise ApiError("请先添加资源站", 404)
            session = state.connect(
                site,
                username=str(data.get("username") or ""),
                password=str(data.get("password") or ""),
                token=str(data.get("token") or ""),
                otp_code=str(data.get("otp_code") or ""),
            )
            cookie = (
                f"{COOKIE_NAME}={session.id}; Path=/; HttpOnly; SameSite=Lax; "
                f"Max-Age={SESSION_TTL}"
            )
            self._send_json(
                {"ok": True, "data": {"session": session.to_dict()}},
                200,
                headers={"Set-Cookie": cookie},
            )
            return

        if path == "/api/disconnect" and method == "POST":
            state.drop_session(sid)
            self._send_json(
                {"ok": True, "data": {"disconnected": True}},
                200,
                headers={
                    "Set-Cookie": (
                        f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
                    )
                },
            )
            return

        if path == "/api/fs/list" and method == "POST":
            session = state.require_session(sid)
            data = self._read_json()
            target_path = normalize_path(str(data.get("path") or "/"))
            entries, meta = session.client.list_dir(
                target_path, refresh=bool(data.get("refresh"))
            )
            self._ok({"entries": entries, "meta": meta})
            return

        if path == "/api/fs/get" and method == "POST":
            session = state.require_session(sid)
            data = self._read_json()
            info = session.client.get_file(normalize_path(str(data.get("path") or "")))
            public = {
                key: info[key]
                for key in ("path", "name", "size", "provider", "modified")
            }
            self._ok({"file": public})
            return

        if path == "/api/expand" and method == "POST":
            session = state.require_session(sid)
            data = self._read_json()
            items = data.get("items") or []
            if not isinstance(items, list) or not items:
                raise ApiError("请选择文件或文件夹", 400)
            result = expand_items(session.client, items)
            self._ok(result)
            return

        if path == "/api/scan/start" and method == "POST":
            session = state.require_session(sid)
            state.prune_scans()
            with state.scans_lock:
                for scan_id, owner in state.scan_owner.items():
                    if owner != sid:
                        continue
                    scan = state.scans.get(scan_id)
                    if scan and scan.state in ("queued", "running"):
                        self._ok({"scan": scan.to_dict()})
                        return
            scan = SiteScan(session.client, "/")
            with state.scans_lock:
                state.scans[scan.id] = scan
                state.scan_owner[scan.id] = sid
            scan.start()
            self._ok({"scan": scan.to_dict()})
            return

        match = re.fullmatch(r"/api/scan/([A-Za-z0-9_-]+)", path)
        if match and method == "GET":
            session = state.require_session(sid)
            scan = state.scan_for(session.id, match.group(1))
            self._ok({"scan": scan.to_dict()})
            return

        match = re.fullmatch(r"/api/scan/([A-Za-z0-9_-]+)/cancel", path)
        if match and method == "POST":
            session = state.require_session(sid)
            scan = state.scan_for(session.id, match.group(1))
            scan.cancel()
            self._ok({"scan": scan.to_dict()})
            return

        match = re.fullmatch(r"/api/scan/([A-Za-z0-9_-]+)/files", path)
        if match and method == "GET":
            session = state.require_session(sid)
            scan = state.scan_for(session.id, match.group(1))
            try:
                offset = max(0, int((query.get("offset") or ["0"])[0]))
                limit = max(1, min(1000, int((query.get("limit") or ["250"])[0])))
            except ValueError:
                raise ApiError("分页参数无效", 400)
            with scan.lock:
                total = len(scan.files)
                chunk = list(scan.files[offset : offset + limit])
            self._ok({"total": total, "offset": offset, "files": chunk})
            return

        if path == "/api/download/prepare" and method == "POST":
            session = state.require_session(sid)
            data = self._read_json()
            files: list[dict[str, Any]] = []
            dirs: list[str] = []
            client = session.client
            scan_id = str(data.get("scan_id") or "")
            if scan_id:
                scan = state.scan_for(session.id, scan_id)
                with scan.lock:
                    if scan.state in ("queued", "running"):
                        raise ApiError("整站扫描还没结束", 409)
                    files = list(scan.files)
                    dirs = list(scan.dirs)
                client = scan.client
            else:
                items = data.get("items")
                if isinstance(items, list) and items:
                    try:
                        expanded = expand_items(client, items)
                    except Exception as exc:
                        from .scanner import ScanLimit

                        if isinstance(exc, ScanLimit):
                            raise ApiError(str(exc), 400) from exc
                        raise
                    files = expanded["files"]
                    dirs = expanded["dirs"]
                else:
                    raw_files = data.get("files")
                    if not isinstance(raw_files, list) or not raw_files:
                        raise ApiError("请选择要下载的文件", 400)
                    for item in raw_files:
                        if not isinstance(item, dict):
                            continue
                        files.append(
                            {
                                "path": str(item.get("path") or ""),
                                "arcname": str(
                                    item.get("arcname") or item.get("name") or ""
                                ),
                                "size": int(item.get("size") or 0),
                            }
                        )
            if not files and not dirs:
                raise ApiError("没有可下载的文件", 400)
            if len(files) > 100_000:
                raise ApiError("文件数量过多，请分批下载", 400)
            job = state.downloads.create(
                files,
                dirs,
                client,
                force_zip=bool(data.get("zip")),
            )
            self._ok(
                {
                    "id": job.id,
                    "url": f"/api/download/{job.id}",
                    "filename": job.filename,
                    "count": len(job.files),
                    "size": job.total_size,
                    "kind": "single" if job.single else "zip",
                }
            )
            return

        match = re.fullmatch(r"/api/download/([A-Za-z0-9_-]+)", path)
        if match and method in ("GET", "HEAD"):
            job = state.downloads.get(match.group(1))
            if job is None:
                raise ApiError("下载链接已过期，请重新下载", 410)
            self._stream_job(job, head=method == "HEAD")
            return

        if path == "/api/aria2/status" and method == "GET":
            self._ok({"aria2": state.aria2.status()})
            return

        if path == "/api/aria2/ensure" and method == "POST":
            status = state.aria2.ensure_ready(auto_start=True)
            self._ok({"aria2": status})
            return

        if path == "/api/aria2/config" and method == "POST":
            data = self._read_json()
            endpoint = str(data.get("endpoint") or "").strip()
            secret = str(data.get("secret") or "").strip()
            download_dir = str(
                data.get("dir") or state.config.aria2.get("dir") or default_download_dir()
            ).strip()
            if not download_dir:
                raise ApiError("请填写下载目录", 400)
            try:
                Path(download_dir).expanduser().mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ApiError(f"无法创建下载目录：{exc}", 400)
            status = state.aria2.configure(endpoint, secret, download_dir)
            self._ok({"aria2": status})
            return

        if path == "/api/aria2/tasks" and method == "GET":
            self._ok(state.aria2.tasks())
            return

        if path == "/api/aria2/add" and method == "POST":
            session = state.require_session(sid)
            data = self._read_json()
            files = data.get("files")
            if not isinstance(files, list) or not files:
                raise ApiError("请选择要下载的文件", 400)
            status = state.aria2.status()
            if not status.get("available"):
                status = state.aria2.ensure_ready(auto_start=True)
            if not status.get("available"):
                raise ApiError(
                    status.get("message") or "aria2 未连接，请改用浏览器下载",
                    503,
                )
            base_dir = str(
                data.get("base_dir")
                or state.config.aria2.get("dir")
                or default_download_dir()
            ).strip()
            result = state.aria2.add_files(
                session.client,
                files,
                base_dir=base_dir,
                tickets=state.tickets,
                proxy_origin=state.origin,
                split=_clamp_int(
                    data.get("split"),
                    1,
                    32,
                    int(state.config.aria2.get("split") or 16),
                ),
                direct=bool(
                    data.get("direct", state.config.aria2.get("direct", True))
                ),
            )
            self._ok(result)
            return

        if path == "/api/aria2/control" and method == "POST":
            data = self._read_json()
            gid = str(data.get("gid") or "")
            action = str(data.get("action") or "")
            if not gid:
                raise ApiError("缺少任务编号", 400)
            self._ok(state.aria2.control(gid, action))
            return

        if path == "/api/aria2/clear" and method == "POST":
            data = self._read_json()
            gids = data.get("gids")
            if not isinstance(gids, list) or not gids:
                gids = []
                tasks = state.aria2.tasks()
                for task in tasks.get("tasks", []):
                    if task.get("status") in ("complete", "error", "removed"):
                        gids.append(task.get("gid"))
            self._ok(state.aria2.clear_finished([str(g) for g in gids]))
            return

        if path == "/api/aria2/clear-all" and method == "POST":
            data = self._read_json()
            result = state.aria2.clear_all(
                stop_active=bool(data.get("stop_active", True))
            )
            self._ok(result)
            return

        match = re.fullmatch(r"/api/aria2/stream/([A-Za-z0-9_-]+)", path)
        if match and method in ("GET", "HEAD"):
            ticket = state.tickets.get(match.group(1))
            if ticket is None:
                raise ApiError("下载链接已过期", 410)
            self._stream_ticket(ticket, head=method == "HEAD")
            return

        if path == "/api/yunx/status" and method == "GET":
            self._ok(yunx.status())
            return

        if path == "/api/yunx/open" and method == "POST":
            data = self._read_json()
            try:
                result = yunx.open_yunx(str(data.get("url") or ""))
            except FileNotFoundError as exc:
                raise ApiError(
                    "没有找到云析便携版。请把 YunX 文件夹放在程序旁边，"
                    "或从 GitHub Releases 下载便携版。",
                    404,
                ) from exc
            except ValueError as exc:
                raise ApiError(str(exc), 400) from exc
            self._ok(result)
            return

        if path == "/api/direct/add" and method == "POST":
            data = self._read_json()
            raw_urls = data.get("urls")
            if isinstance(raw_urls, str):
                raw_urls = raw_urls.splitlines()
            if not isinstance(raw_urls, list) or not raw_urls:
                raise ApiError("请至少填写一个下载链接", 400)
            items: list[dict[str, str]] = []
            for item in raw_urls[:200]:
                if isinstance(item, dict):
                    url = str(item.get("url") or "").strip()
                    filename = str(item.get("filename") or "").strip()
                else:
                    line = str(item or "").strip()
                    filename = ""
                    url = line
                    match_name = re.match(
                        r"^(.+?)\s+\|\s+(https?://.+)$", line, re.IGNORECASE
                    )
                    if match_name:
                        filename = match_name.group(1).strip()
                        url = match_name.group(2).strip()
                if not url:
                    continue
                if not url.lower().startswith(("http://", "https://")):
                    raise ApiError("只支持 http/https 直链", 400)
                if _looks_like_share_page(url):
                    raise ApiError(
                        "这是网盘分享页，不是文件直链。请先在网盘/OpenList 中转存并取得直链，或把带 Cookie 的下载地址粘贴到这里。",
                        400,
                    )
                items.append({"url": url, "filename": filename})
            if not items:
                raise ApiError("请至少填写一个下载链接", 400)
            headers = _parse_headers(data.get("headers"))
            engine = str(data.get("engine") or "aria2")
            base_dir = str(
                data.get("base_dir")
                or state.config.aria2.get("dir")
                or default_download_dir()
            ).strip()
            if engine == "browser":
                if len(items) != 1:
                    raise ApiError("浏览器模式一次只能下载一个链接，多个链接请用 aria2", 400)
                dummy = OpenListClient("http://127.0.0.1:1")
                item = items[0]
                filename = sanitize_filename(
                    item["filename"] or _url_filename(item["url"]), "download"
                )
                token = state.tickets.create(
                    link=item["url"],
                    headers=headers,
                    filename=filename,
                    client=dummy,
                    source=item["url"],
                )
                self._ok(
                    {
                        "kind": "ticket",
                        "url": f"/api/direct/stream/{token}",
                        "filename": filename,
                    }
                )
                return
            status = state.aria2.status()
            if not status.get("available"):
                status = state.aria2.ensure_ready(auto_start=True)
            if not status.get("available"):
                raise ApiError(status.get("message") or "aria2 未连接", 503)
            result = state.aria2.add_direct(
                items,
                base_dir=base_dir,
                headers=headers,
                tickets=state.tickets,
                proxy_origin=state.origin,
                split=_clamp_int(
                    data.get("split"),
                    1,
                    32,
                    int(state.config.aria2.get("split") or 16),
                ),
                direct=bool(
                    data.get("direct", state.config.aria2.get("direct", True))
                ),
            )
            self._ok(result)
            return

        match = re.fullmatch(r"/api/direct/stream/([A-Za-z0-9_-]+)", path)
        if match and method in ("GET", "HEAD"):
            ticket = state.tickets.get(match.group(1))
            if ticket is None:
                raise ApiError("下载链接已过期", 410)
            self._stream_ticket(
                ticket, head=method == "HEAD", disposition=True
            )
            return

        if path == "/api/settings" and method == "GET":
            aria2 = state.config.aria2
            self._ok(
                {
                    "downloadDir": aria2.get("dir") or default_download_dir(),
                    "split": aria2.get("split", 16),
                    "concurrency": aria2.get("concurrency", 8),
                    "direct": bool(aria2.get("direct", True)),
                    "endpoint": aria2.get("endpoint", ""),
                    "secret": "",
                    "managed": bool(aria2.get("managed")),
                    "preferences": state.config.get("preferences", default={}),
                }
            )
            return

        if path == "/api/settings" and method == "POST":
            data = self._read_json()
            download_dir = str(
                data.get("downloadDir") or state.config.aria2.get("dir") or ""
            ).strip()
            if download_dir:
                try:
                    Path(download_dir).expanduser().mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise ApiError(f"无法创建下载目录：{exc}", 400)
            split = _clamp_int(data.get("split"), 1, 32, 16)
            concurrency = _clamp_int(data.get("concurrency"), 1, 16, 8)
            direct = bool(
                data.get("direct", state.config.aria2.get("direct", True))
            )
            state.config.update_aria2(
                dir=download_dir,
                split=split,
                concurrency=concurrency,
                direct=direct,
            )
            state.aria2.apply_tuning()
            preferences: dict[str, Any] = {}
            if "engine" in data:
                engine = str(data.get("engine") or "browser")
                if engine not in ("browser", "aria2"):
                    engine = "browser"
                preferences["engine"] = engine
            if "theme" in data:
                theme = str(data.get("theme") or "dark")
                preferences["theme"] = "light" if theme == "light" else "dark"
            if preferences:
                state.config.update_preferences(**preferences)
            aria2 = state.aria2.status()
            self._ok({"aria2": aria2, "settings": state.config.aria2})
            return

        if path == "/api/open-folder" and method == "POST":
            data = self._read_json()
            target = str(
                data.get("path")
                or state.config.aria2.get("dir")
                or default_download_dir()
            ).strip()
            target_path = Path(target).expanduser()
            try:
                target_path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ApiError(f"目录不可用：{exc}", 400)
            _open_folder(str(target_path))
            self._ok({"opened": str(target_path)})
            return

        raise ApiError("接口不存在", 404)

    # -- streaming -------------------------------------------------------
    def _stream_job(self, job, *, head: bool) -> None:
        writer = ResponseWriterImpl(self)
        try:
            stream_job(
                writer,
                job,
                range_header=self.headers.get("Range", ""),
                head=head,
            )
        except DownloadError as exc:
            if not writer.headers_sent:
                raise
            LOG.warning("download stream failed after headers: %s", exc.message)
            writer.finish()
        except (BrokenPipeError, ConnectionResetError, ClientDisconnected):
            self.close_connection = True
            writer.finish()

    def _stream_ticket(
        self, ticket, *, head: bool, disposition: bool = False
    ) -> None:
        writer = ResponseWriterImpl(self)
        try:
            stream_ticket(
                writer,
                ticket,
                range_header=self.headers.get("Range", ""),
                head=head,
                disposition=disposition,
            )
        except DownloadError as exc:
            if not writer.headers_sent:
                raise
            LOG.warning("ticket stream failed after headers: %s", exc.message)
            writer.finish()
        except (BrokenPipeError, ConnectionResetError, ClientDisconnected):
            self.close_connection = True
            writer.finish()


CSP = (
    "default-src 'self'; "
    "img-src 'self' data: blob:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)


def _parse_headers(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        raw = value
    elif isinstance(value, str):
        raw = {}
        for line in value.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, val = line.split(":", 1)
            raw[key.strip()] = val.strip()
    else:
        return {}
    result: dict[str, str] = {}
    blocked = {"host", "content-length", "transfer-encoding", "connection"}
    for key, val in list(raw.items())[:40]:
        if not isinstance(key, str) or not isinstance(val, (str, int, float)):
            continue
        name = key.strip()
        if (
            not name
            or name.lower() in blocked
            or any(ord(ch) < 33 or ord(ch) > 126 for ch in name)
            or ":" in name
        ):
            continue
        text = str(val).replace("\r", "").replace("\n", "")
        if text:
            result[name] = text[:4096]
    return result


def _looks_like_share_page(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if not path:
        return False
    share_hosts = (
        "pan.baidu.com",
        "yun.baidu.com",
        "aliyundrive.com",
        "www.aliyundrive.com",
        "pan.quark.cn",
        "115.com",
        "www.115.com",
        "cloud.189.cn",
    )
    if not any(host == item or host.endswith("." + item) for item in share_hosts):
        return False
    return path.startswith("/s/") or path.startswith("/share/") or path.startswith("/t/")


def _url_filename(url: str) -> str:
    try:
        name = urllib.parse.unquote(
            urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
        )
        if name:
            return sanitize_filename(name, "download")
    except (ValueError, UnicodeDecodeError):
        pass
    return "download"


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def _open_folder(path: str) -> None:
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    elif os.name == "posix":
        import subprocess

        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        raise ApiError("当前系统不支持打开文件夹", 400)


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], config: Config, static_dir: Path):
        super().__init__(address, AppHandler)
        self.state = AppState(config, static_dir, self.server_port)