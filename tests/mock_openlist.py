"""A tiny OpenList/AList + aria2 mock used by the test suite."""
from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

TOKEN = "test-token-123"
USERNAME = "tester"
PASSWORD = "pass123"


def _content_for(path: str, size: int) -> bytes:
    seed = (path + "|").encode("utf-8")
    data = (seed * ((size // max(1, len(seed))) + 2))[:size]
    return data


class Tree:
    def __init__(self):
        self.dirs: set[str] = {"/", "/Movies", "/Movies/Subs", "/Docs", "/中文", "/Empty"}
        self.files: dict[str, int] = {
            "/Movies/Inception.2010.1080p.mkv": 1_500_000,
            "/Movies/Subs/en.srt": 1234,
            "/Movies/Subs/zh.srt": 2345,
            "/Docs/readme.txt": 5000,
            "/中文/测试 文件.txt": 2048,
            "/Empty/.keep": 7,
        }

    def list_dir(self, path: str) -> list[dict[str, Any]]:
        path = "/" + path.strip("/")
        if path == "/":
            prefix = "/"
        else:
            prefix = path + "/"
        entries = []
        for directory in sorted(self.dirs):
            if directory == "/" or not directory.startswith(prefix):
                continue
            rest = directory[len(prefix) :]
            if rest and "/" not in rest:
                entries.append(
                    {
                        "name": rest,
                        "size": 0,
                        "is_dir": True,
                        "modified": "2026-01-02T03:04:05+09:00",
                        "created": "2026-01-01T00:00:00+09:00",
                        "type": 1,
                    }
                )
        for file_path, size in sorted(self.files.items()):
            if not file_path.startswith(prefix):
                continue
            rest = file_path[len(prefix) :]
            if not rest or "/" in rest:
                continue
            entries.append(
                {
                    "name": rest,
                    "size": size,
                    "is_dir": False,
                    "modified": "2026-02-03T04:05:06+09:00",
                    "created": "2026-01-01T00:00:00+09:00",
                    "type": 0,
                }
            )
        entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
        return entries


class MockOpenList(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    @property
    def tree(self) -> Tree:
        return self.server.tree  # type: ignore[attr-defined]

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _auth_ok(self) -> bool:
        if not self.server.require_auth:  # type: ignore[attr-defined]
            return True
        return self.headers.get("Authorization", "") == TOKEN

    def _require(self) -> bool:
        if self._auth_ok():
            return True
        self._json({"code": 401, "message": "token is invalid", "data": None}, 401)
        return False

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/auth/login":
            body = self._body()
            supplied = str(body.get("password") or "")
            if (
                body.get("username") == USERNAME
                and supplied
                in (PASSWORD, hashlib.sha256(PASSWORD.encode()).hexdigest())
            ):
                self._json({"code": 200, "message": "success", "data": {"token": TOKEN}})
            else:
                self._json({"code": 401, "message": "wrong credentials", "data": None}, 401)
            return
        if path == "/api/fs/list":
            if not self._require():
                return
            body = self._body()
            target = str(body.get("path") or "/")
            entries = self.tree.list_dir(target)
            self._json(
                {
                    "code": 200,
                    "message": "success",
                    "data": {
                        "content": entries,
                        "total": len(entries),
                        "readme": "",
                        "write": True,
                        "provider": "Mock",
                    },
                }
            )
            return
        if path == "/api/fs/get":
            if not self._require():
                return
            body = self._body()
            target = str(body.get("path") or "")
            if target not in self.tree.files:
                self._json({"code": 404, "message": "not found", "data": None}, 404)
                return
            base = f"http://127.0.0.1:{self.server.server_port}"
            encoded = urllib.parse.quote(target)
            self._json(
                {
                    "code": 200,
                    "message": "success",
                    "data": {
                        "name": Path(target).name,
                        "size": self.tree.files[target],
                        "is_dir": False,
                        "modified": "2026-02-03T04:05:06+09:00",
                        "raw_url": f"{base}/file?path={encoded}&sig=mock",
                        "url": f"{base}/file?path={encoded}&sig=mock",
                        "header": {
                            "X-From-OpenList": "yes",
                            "User-Agent": "MockOpenList/1.0",
                        },
                    },
                }
            )
            return
        self._json({"code": 404, "message": "not found", "data": None}, 404)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/public/settings":
            self._json(
                {
                    "code": 200,
                    "message": "success",
                    "data": {
                        "site_title": "测试资源站",
                        "version": "v3.40.0",
                        "allow_indexed": True,
                    },
                }
            )
            return
        if parsed.path == "/api/me":
            if not self._require():
                return
            self._json(
                {
                    "code": 200,
                    "message": "success",
                    "data": {"username": USERNAME, "role": "admin"},
                }
            )
            return
        if parsed.path == "/file":
            query = urllib.parse.parse_qs(parsed.query)
            target = (query.get("path") or [""])[0]
            if target not in self.tree.files:
                self.send_error(404)
                return
            size = self.tree.files[target]
            data = _content_for(target, size)
            range_header = self.headers.get("Range", "")
            if range_header.startswith("bytes="):
                spec = range_header[len("bytes=") :]
                start_s, _, end_s = spec.partition("-")
                try:
                    start = int(start_s or 0)
                    end = int(end_s) if end_s else size - 1
                except ValueError:
                    self.send_error(416)
                    return
                if start >= size or end < start:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                chunk = data[start : end + 1]
                self.send_response(206)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(chunk)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            return
        self.send_error(404)

    def do_HEAD(self) -> None:
        self.do_GET()


class MockAria2(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    calls: list[dict[str, Any]] = []
    tasks: dict[str, dict[str, Any]] = {}

    def log_message(self, *args):
        pass

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _result(self, method: str, params: list[Any], call_id: Any) -> dict[str, Any]:
        self.__class__.calls.append({"method": method, "params": params})
        if method == "aria2.getVersion":
            result = {"version": "1.37.0", "enabledFeatures": []}
        elif method == "aria2.getGlobalStat":
            result = {
                "downloadSpeed": "0",
                "uploadSpeed": "0",
                "numActive": "0",
                "numWaiting": "0",
                "numStopped": "0",
            }
        elif method == "aria2.addUri":
            gid = f"gid{len(self.tasks) + 1}"
            url = params[0][0]
            options = params[1] if len(params) > 1 else {}
            self.tasks[gid] = {
                "gid": gid,
                "status": "active",
                "totalLength": "100",
                "completedLength": "10",
                "downloadSpeed": "0",
                "connections": "1",
                "dir": options.get("dir", ""),
                "files": [{"path": options.get("out", "file")}],
                "errorMessage": "",
                "url": url,
                "options": options,
            }
            result = gid
        elif method in ("aria2.tellActive",):
            result = list(self.tasks.values())
        elif method in ("aria2.tellWaiting", "aria2.tellStopped"):
            result = []
        elif method == "aria2.tellStatus":
            result = self.tasks.get(params[0], {})
        elif method in (
            "aria2.pause",
            "aria2.unpause",
            "aria2.forceRemove",
            "aria2.remove",
            "aria2.removeDownloadResult",
            "aria2.retryDownload",
            "aria2.changeGlobalOption",
        ):
            result = "OK"
        else:
            return {
                "jsonrpc": "2.0",
                "id": call_id,
                "error": {"code": 1, "message": "unknown method " + method},
            }
        return {"jsonrpc": "2.0", "id": call_id, "result": result}

    def do_POST(self) -> None:
        payload = self._body()
        if isinstance(payload, list):
            results = []
            for item in payload:
                params = item.get("params") or []
                if params and isinstance(params[0], str) and params[0].startswith("token:"):
                    params = params[1:]
                results.append(self._result(item.get("method", ""), params, item.get("id")))
            body = json.dumps(results).encode()
        else:
            params = payload.get("params") or []
            if params and isinstance(params[0], str) and params[0].startswith("token:"):
                params = params[1:]
            body = json.dumps(
                self._result(payload.get("method", ""), params, payload.get("id"))
            ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_server(handler_cls, *, tree: Tree | None = None, require_auth: bool = False):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    server.daemon_threads = True
    if handler_cls is MockOpenList:
        server.tree = tree or Tree()  # type: ignore[attr-defined]
        server.require_auth = require_auth  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    return server, thread


def stop_server(server) -> None:
    server.shutdown()
    server.server_close()