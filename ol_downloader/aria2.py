"""aria2 JSON-RPC client plus a zero-configuration bundled aria2 manager."""
from __future__ import annotations

import json
import logging
import os
import random
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .config import Config, resource_dir, safe_arcname, sanitize_filename
from .openlist import OpenListClient, OpenListError

LOG = logging.getLogger(__name__)
DEFAULT_PORTS = (16800, 16801, 16802, 16803, 16804, 6800, 6801)


class Aria2Error(Exception):
    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.message = message
        self.code = code


def normalize_rpc_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    parts = urllib.parse.urlsplit(value)
    path = parts.path or "/jsonrpc"
    if path in ("", "/"):
        path = "/jsonrpc"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class Aria2Client:
    def __init__(self, endpoint: str, secret: str = "", timeout: float = 6.0):
        self.endpoint = normalize_rpc_url(endpoint)
        self.secret = (secret or "").strip()
        self.timeout = timeout

    def _params(self, params: list[Any]) -> list[Any]:
        if self.secret:
            return [f"token:{self.secret}", *params]
        return params

    def _post(self, payload: Any, timeout: float | None = None) -> Any:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read(16 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            raise Aria2Error(f"aria2 RPC 返回 HTTP {exc.code}", exc.code) from exc
        except urllib.error.URLError as exc:
            raise Aria2Error(f"无法连接 aria2：{exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise Aria2Error(f"无法连接 aria2：{exc}") from exc
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise Aria2Error("aria2 RPC 返回了无效数据") from exc

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": method,
            "params": self._params(params or []),
        }
        result = self._post(payload)
        if isinstance(result, dict) and result.get("error"):
            error = result["error"] or {}
            raise Aria2Error(
                str(error.get("message") or "aria2 调用失败"),
                int(error.get("code") or 0),
            )
        return result.get("result") if isinstance(result, dict) else None

    def batch(self, calls: list[tuple[str, list[Any]]]) -> dict[str, Any]:
        payload = []
        aliases: dict[str, str] = {}
        for index, (method, params) in enumerate(calls):
            alias = f"c{index}"
            aliases[alias] = method
            payload.append(
                {
                    "jsonrpc": "2.0",
                    "id": alias,
                    "method": method,
                    "params": self._params(params),
                }
            )
        result = self._post(payload)
        output: dict[str, Any] = {}
        if isinstance(result, list):
            for item in result:
                if not isinstance(item, dict):
                    continue
                alias = str(item.get("id") or "")
                method = aliases.get(alias, alias)
                if item.get("error"):
                    error = item["error"] or {}
                    output[method] = Aria2Error(
                        str(error.get("message") or "aria2 调用失败"),
                        int(error.get("code") or 0),
                    )
                else:
                    output[method] = item.get("result")
        return output

    def version(self) -> str:
        data = self.call("aria2.getVersion")
        return str((data or {}).get("version") or "")

    def add_uri(self, url: str, options: dict[str, Any]) -> str:
        gid = self.call("aria2.addUri", [[url], options])
        if not isinstance(gid, str) or not gid:
            raise Aria2Error("aria2 没有返回任务编号")
        return gid

    def tell_status(self, gid: str) -> dict[str, Any]:
        data = self.call("aria2.tellStatus", [gid])
        return data if isinstance(data, dict) else {}

    def change_global_option(self, options: dict[str, Any]) -> None:
        self.call("aria2.changeGlobalOption", [options])


def find_aria2_binary() -> str:
    candidates: list[Path] = [
        resource_dir() / "tools" / "aria2" / "aria2c.exe",
        Path(__file__).resolve().parent.parent / "tools" / "aria2" / "aria2c.exe",
    ]
    found = shutil.which("aria2c")
    if found:
        candidates.append(Path(found))
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            candidates.extend(
                [
                    Path(base) / "aria2" / "aria2c.exe",
                    Path(base) / "Programs" / "aria2" / "aria2c.exe",
                ]
            )
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return ""


def read_aria2_conf() -> dict[str, str]:
    locations = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        locations.append(Path(appdata) / "aria2" / "aria2.conf")
    locations.extend([Path.home() / ".aria2" / "aria2.conf", Path.home() / "aria2.conf"])
    result: dict[str, str] = {}
    for path in locations:
        try:
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                result[key.strip().lower()] = value.strip()
        except OSError:
            continue
    return result


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.35):
            return True
    except OSError:
        return False


class TaskIndex:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.items: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.items = {
                    str(k): v for k, v in raw.items() if isinstance(v, dict)
                }
        except (OSError, ValueError):
            self.items = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.items, ensure_ascii=False, indent=1) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            LOG.warning("could not save aria2 task index: %s", exc)

    def add(self, gid: str, meta: dict[str, Any]) -> None:
        with self.lock:
            self.items[gid] = {**meta, "added": time.time()}
            self._save()

    def remove(self, gid: str) -> None:
        with self.lock:
            if self.items.pop(gid, None) is not None:
                self._save()

    def clear_finished(self) -> None:
        with self.lock:
            self.items.clear()
            self._save()

    def all(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            return {key: dict(value) for key, value in self.items.items()}


class Aria2Manager:
    def __init__(self, config: Config, data_dir: Path):
        self.config = config
        self.data_dir = data_dir
        self.task_index = TaskIndex(data_dir / "aria2_tasks.json")
        self._lock = threading.RLock()
        self._client: Aria2Client | None = None
        self._status: dict[str, Any] = {
            "available": False,
            "state": "idle",
            "message": "",
            "version": "",
            "endpoint": "",
            "managed": False,
            "bundled": bool(find_aria2_binary()),
            "dir": config.aria2.get("dir", ""),
            "secret_required": False,
        }
        self._log_path = data_dir / "aria2.log"
        self._session_path = data_dir / "aria2.session"

    # -- discovery -------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self._lock:
            result = dict(self._status)
        result["dir"] = self.config.aria2.get("dir", result.get("dir", ""))
        result["split"] = self.config.aria2.get("split", 16)
        result["concurrency"] = self.config.aria2.get("concurrency", 8)
        result["direct"] = bool(self.config.aria2.get("direct", True))
        result["tasks"] = len(self.task_index.all())
        return result

    def _set_status(self, **values: Any) -> None:
        with self._lock:
            self._status.update(values)

    def _try_client(self, endpoint: str, secret: str) -> tuple[Aria2Client | None, str]:
        client = Aria2Client(endpoint, secret, timeout=2.2)
        try:
            version = client.version()
        except Aria2Error as exc:
            if "unauthor" in exc.message.lower() or exc.code == 1:
                return None, "secret"
            return None, exc.message
        self._client = client
        self._set_status(
            available=True,
            state="ready",
            message="",
            version=version,
            endpoint=endpoint,
            managed=bool(self.config.aria2.get("managed")),
            secret_required=False,
        )
        self.apply_tuning()
        return client, ""

    def ensure_ready(self, auto_start: bool = True) -> dict[str, Any]:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.version()
                    self._set_status(available=True, state="ready")
                    return self.status()
                except Aria2Error:
                    self._client = None
            cfg = self.config.aria2
            endpoint = normalize_rpc_url(str(cfg.get("endpoint") or ""))
            secret = str(cfg.get("secret") or "")
            if endpoint:
                client, reason = self._try_client(endpoint, secret)
                if client:
                    return self.status()
                if reason == "secret":
                    self._set_status(
                        available=False,
                        state="need_secret",
                        message="aria2 已运行，但需要 RPC 密钥",
                        endpoint=endpoint,
                    )
                    return self.status()
            # Try the configured port and common ports without a secret.
            ports = []
            if int(cfg.get("port") or 0):
                ports.append(int(cfg["port"]))
            conf = read_aria2_conf()
            if conf.get("rpc-listen-port", "").isdigit():
                configured_port = int(conf["rpc-listen-port"])
                if configured_port not in ports:
                    ports.append(configured_port)
            for port in DEFAULT_PORTS:
                if port not in ports:
                    ports.append(port)
            conf_secret = conf.get("rpc-secret", "")
            for port in ports:
                if not _port_open(port) and port not in (int(cfg.get("port") or 0),):
                    continue
                probe_secrets = []
                if conf_secret:
                    probe_secrets.append(conf_secret)
                if secret:
                    probe_secrets.append(secret)
                probe_secrets.append("")
                for candidate in probe_secrets:
                    client, reason = self._try_client(
                        f"http://127.0.0.1:{port}/jsonrpc", candidate
                    )
                    if client:
                        if candidate:
                            self.config.update_aria2(
                                endpoint=client.endpoint,
                                secret=candidate,
                                managed=False,
                                port=port,
                            )
                        return self.status()
            if auto_start:
                return self.start_bundled()
            self._set_status(
                available=False,
                state="offline",
                message="aria2 未连接",
            )
            return self.status()

    # -- bundled daemon --------------------------------------------------
    def _copy_bundled_binary(self) -> str:
        source = find_aria2_binary()
        if not source:
            return ""
        if not source.lower().endswith("aria2c.exe"):
            return source
        bundled_root = resource_dir() / "tools" / "aria2"
        if Path(source).resolve() == (bundled_root / "aria2c.exe").resolve():
            target = self.data_dir / "bin" / "aria2c.exe"
            target.parent.mkdir(parents=True, exist_ok=True)
            if (
                not target.exists()
                or target.stat().st_size != Path(source).stat().st_size
            ):
                tmp = target.with_suffix(".tmp")
                shutil.copyfile(source, tmp)
                os.replace(tmp, target)
            return str(target)
        return source

    def start_bundled(self) -> dict[str, Any]:
        with self._lock:
            self._set_status(state="starting", message="正在启动内置 aria2…")
        binary = self._copy_bundled_binary()
        if not binary:
            self._set_status(
                available=False,
                state="missing",
                message="没有找到 aria2，可切换为浏览器下载",
            )
            return self.status()

        cfg = self.config.aria2
        existing_secret = str(cfg.get("secret") or "")
        secret = existing_secret or os.urandom(24).hex()
        configured_port = int(cfg.get("port") or 16800)
        port = 0
        for candidate in [configured_port, *range(16800, 16840), *range(6800, 6810)]:
            if 0 < candidate < 65536 and not _port_open(candidate):
                port = candidate
                break
        if not port:
            self._set_status(
                available=False,
                state="error",
                message="找不到可用的 aria2 端口",
            )
            return self.status()

        download_dir = str(cfg.get("dir") or "")
        try:
            Path(download_dir).mkdir(parents=True, exist_ok=True)
        except OSError:
            download_dir = str(self.data_dir / "downloads")
            Path(download_dir).mkdir(parents=True, exist_ok=True)
            self.config.update_aria2(dir=download_dir)
        self._session_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._session_path.exists():
            self._session_path.write_text("", encoding="utf-8")
        try:
            log_handle = open(self._log_path, "ab", buffering=0)
        except OSError:
            log_handle = subprocess.DEVNULL  # type: ignore[assignment]

        args = [
            binary,
            "--enable-rpc=true",
            "--rpc-listen-all=false",
            "--rpc-listen-port=" + str(port),
            "--rpc-secret=" + secret,
            "--rpc-max-request-size=32M",
            "--continue=true",
            "--auto-file-renaming=true",
            "--allow-overwrite=false",
            "--file-allocation=none",
            "--max-concurrent-downloads=" + str(int(cfg.get("concurrency") or 8)),
            "--split=" + str(int(cfg.get("split") or 16)),
            "--max-connection-per-server=" + str(int(cfg.get("split") or 16)),
            "--min-split-size=1M",
            "--optimize-concurrent-downloads=true",
            "--max-overall-download-limit=0",
            "--max-download-limit=0",
            "--retry-wait=2",
            "--max-tries=5",
            "--connect-timeout=10",
            "--timeout=30",
            "--enable-http-keep-alive=true",
            "--http-accept-gzip=false",
            "--disk-cache=64M",
            "--remote-time=true",
            "--summary-interval=0",
            "--console-log-level=warn",
            "--log-level=warn",
            "--log=" + str(self._log_path),
            "--save-session=" + str(self._session_path),
            "--save-session-interval=30",
            "--input-file=" + str(self._session_path),
            "--dir=" + download_dir,
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        try:
            subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                close_fds=True,
                creationflags=creationflags,
                cwd=str(self.data_dir),
            )
        except OSError as exc:
            self._set_status(
                available=False,
                state="error",
                message=f"启动 aria2 失败：{exc}",
            )
            return self.status()
        finally:
            try:
                if log_handle is not subprocess.DEVNULL:
                    log_handle.close()  # type: ignore[union-attr]
            except Exception:
                pass

        endpoint = f"http://127.0.0.1:{port}/jsonrpc"
        deadline = time.time() + 10
        while time.time() < deadline:
            client, reason = self._try_client(endpoint, secret)
            if client:
                self.config.update_aria2(
                    endpoint=endpoint,
                    secret=secret,
                    managed=True,
                    port=port,
                    dir=download_dir,
                )
                self._set_status(managed=True, bundled=True)
                return self.status()
            if reason == "secret":
                break
            time.sleep(0.25)
        self._set_status(
            available=False,
            state="error",
            message="内置 aria2 启动超时，请查看 data/aria2.log",
            endpoint=endpoint,
        )
        return self.status()

    def apply_tuning(self) -> None:
        """Apply throughput options to an aria2 that was already running."""
        client = self._client
        if client is None:
            return
        cfg = self.config.aria2
        try:
            client.change_global_option(
                {
                    "max-concurrent-downloads": str(
                        max(1, int(cfg.get("concurrency") or 8))
                    ),
                    "optimize-concurrent-downloads": "true",
                    "max-overall-download-limit": "0",
                    "max-download-limit": "0",
                }
            )
        except Aria2Error as exc:
            LOG.debug("could not apply aria2 tuning: %s", exc.message)

    def configure(self, endpoint: str, secret: str, download_dir: str) -> dict[str, Any]:
        endpoint = normalize_rpc_url(endpoint)
        with self._lock:
            self._client = None
        self.config.update_aria2(
            endpoint=endpoint,
            secret=(secret or "").strip(),
            dir=download_dir,
            managed=False,
        )
        return self.ensure_ready(auto_start=False)

    # -- tasks -----------------------------------------------------------
    def tasks(self) -> dict[str, Any]:
        client = self._client
        if client is None:
            return {"available": False, "tasks": [], "global": {}}
        calls = [
            ("aria2.tellActive", []),
            ("aria2.tellWaiting", [0, 1000]),
            ("aria2.tellStopped", [0, 1000]),
            ("aria2.getGlobalStat", []),
        ]
        try:
            result = client.batch(calls)
        except Aria2Error as exc:
            self._set_status(available=False, state="offline", message=exc.message)
            self._client = None
            return {"available": False, "tasks": [], "global": {}, "error": exc.message}
        known = self.task_index.all()
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for method in ("aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped"):
            value = result.get(method)
            if isinstance(value, Aria2Error) or not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict):
                    continue
                gid = str(item.get("gid") or "")
                if not gid or gid in seen or gid not in known:
                    continue
                seen.add(gid)
                rows.append(_normalize_task(item, known.get(gid, {})))
        status_order = {
            "active": 0,
            "waiting": 1,
            "paused": 2,
            "error": 3,
            "complete": 4,
            "removed": 5,
        }
        rows.sort(
            key=lambda row: (
                status_order.get(row["status"], 9),
                row.get("added", 0),
            )
        )
        global_stat = result.get("aria2.getGlobalStat")
        if isinstance(global_stat, Aria2Error) or not isinstance(global_stat, dict):
            global_stat = {}
        return {
            "available": True,
            "tasks": rows,
            "global": {
                "downloadSpeed": int(global_stat.get("downloadSpeed") or 0),
                "uploadSpeed": int(global_stat.get("uploadSpeed") or 0),
                "numActive": int(global_stat.get("numActive") or 0),
                "numWaiting": int(global_stat.get("numWaiting") or 0),
                "numStopped": int(global_stat.get("numStopped") or 0),
            },
        }

    def control(self, gid: str, action: str) -> dict[str, Any]:
        client = self._client
        if client is None:
            raise Aria2Error("aria2 未连接")
        if action == "pause":
            client.call("aria2.pause", [gid])
        elif action == "resume":
            client.call("aria2.unpause", [gid])
        elif action == "remove":
            try:
                client.call("aria2.forceRemove", [gid])
            except Aria2Error:
                client.call("aria2.remove", [gid])
            self.task_index.remove(gid)
        elif action == "retry":
            meta = self.task_index.all().get(gid)
            if not meta:
                raise Aria2Error("找不到这个任务")
            client.call("aria2.retryDownload", [gid])
        else:
            raise Aria2Error("不支持的操作")
        return {"ok": True}

    def clear_finished(self, gids: list[str] | None = None) -> dict[str, Any]:
        client = self._client
        if client is None:
            self.task_index.clear_finished()
            return {"ok": True}
        if gids:
            for gid in gids:
                try:
                    client.call("aria2.removeDownloadResult", [gid])
                except Aria2Error:
                    pass
                self.task_index.remove(gid)
        else:
            for gid in list(self.task_index.all()):
                try:
                    client.call("aria2.removeDownloadResult", [gid])
                except Aria2Error:
                    pass
                self.task_index.remove(gid)
        return {"ok": True}

    def clear_all(self, *, stop_active: bool = True) -> dict[str, Any]:
        """Clear every row in the download list.

        Active/waiting tasks are stopped first when ``stop_active`` is true.
        """
        client = self._client
        index = self.task_index.all()
        stopped = 0
        if client is not None:
            tasks = self.tasks().get("tasks", [])
            for task in tasks:
                gid = str(task.get("gid") or "")
                if not gid:
                    continue
                status = str(task.get("status") or "")
                if stop_active and status in ("active", "waiting", "paused"):
                    try:
                        client.call("aria2.forceRemove", [gid])
                        stopped += 1
                    except Aria2Error:
                        pass
                try:
                    client.call("aria2.removeDownloadResult", [gid])
                except Aria2Error:
                    pass
            for gid in index:
                try:
                    client.call("aria2.removeDownloadResult", [gid])
                except Aria2Error:
                    pass
        self.task_index.clear_finished()
        return {"ok": True, "cleared": len(index), "stopped": stopped}

    def add_files(
        self,
        client: OpenListClient,
        files: list[dict[str, Any]],
        *,
        base_dir: str,
        tickets,
        proxy_origin: str,
        split: int = 16,
        direct: bool = False,
    ) -> dict[str, Any]:
        aria = self._client
        if aria is None:
            raise Aria2Error("aria2 未连接")
        base = Path(base_dir).expanduser()
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise Aria2Error(f"下载目录不可用：{exc}") from exc
        remote = not _is_loopback_endpoint(aria.endpoint)
        success: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for item in files:
            path = str(item.get("path") or "")
            arcname = safe_arcname(str(item.get("arcname") or item.get("name") or ""))
            if not path or not arcname:
                continue
            try:
                info = client.get_file(path)
            except OpenListError as exc:
                errors.append({"path": path, "message": exc.message})
                continue
            name = sanitize_filename(arcname.rsplit("/", 1)[-1])
            parent = "/".join(arcname.split("/")[:-1])
            target_dir = base / Path(*[p for p in parent.split("/") if p])
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                errors.append({"path": path, "message": f"无法创建目录：{exc}"})
                continue
            options: dict[str, Any] = {
                "dir": str(target_dir),
                "out": name,
                "continue": "true",
                "auto-file-renaming": "true",
                "split": str(max(1, split)),
                "max-connection-per-server": str(max(1, split)),
                "min-split-size": "1M",
                "remote-time": "true",
                "optimize-concurrent-downloads": "true",
            }
            use_direct = direct or remote
            if use_direct:
                options["header"] = [
                    f"{key}: {value}" for key, value in info["headers"].items()
                ]
                if client.insecure:
                    options["check-certificate"] = "false"
                url = info["link"]
            else:
                token = tickets.create(
                    link=info["link"],
                    headers=info["headers"],
                    filename=name,
                    size=info["size"],
                    source=path,
                    client=client,
                )
                url = f"{proxy_origin.rstrip('/')}/api/aria2/stream/{token}"
            try:
                gid = aria.add_uri(url, options)
            except Aria2Error as exc:
                errors.append({"path": path, "message": exc.message})
                continue
            self.task_index.add(
                gid,
                {
                    "path": path,
                    "arcname": arcname,
                    "name": name,
                    "dir": str(target_dir),
                    "size": info["size"],
                    "direct": use_direct,
                },
            )
            success.append({"path": path, "gid": gid})
        return {
            "added": len(success),
            "failed": len(errors),
            "errors": errors[:50],
            "gids": [item["gid"] for item in success],
            "direct": direct and not remote,
        }

    def add_direct(
        self,
        items: list[dict[str, Any]],
        *,
        base_dir: str,
        headers: dict[str, str],
        tickets,
        proxy_origin: str,
        split: int = 16,
        direct: bool = True,
    ) -> dict[str, Any]:
        """Add plain http(s) URLs, useful when no OpenList mount is used."""
        aria = self._client
        if aria is None:
            raise Aria2Error("aria2 未连接")
        base = Path(base_dir).expanduser()
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise Aria2Error(f"下载目录不可用：{exc}") from exc
        remote = not _is_loopback_endpoint(aria.endpoint)
        direct_client = OpenListClient("http://127.0.0.1:1", insecure=False)
        success: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        used_names: set[str] = set()
        for index, item in enumerate(items):
            raw_url = str(item.get("url") or "").strip()
            if not raw_url.startswith(("http://", "https://")):
                errors.append({"url": raw_url, "message": "只支持 http/https 直链"})
                continue
            suggested = str(item.get("filename") or "").strip()
            filename = sanitize_filename(
                suggested or _filename_from_url(raw_url, index), "download"
            )
            stem, dot, ext = filename.rpartition(".")
            if not dot:
                stem, ext = filename, ""
            counter = 2
            candidate = filename
            while candidate.lower() in used_names:
                candidate = f"{stem} ({counter}){('.' + ext) if ext else ''}"
                counter += 1
            filename = candidate
            used_names.add(filename.lower())
            options: dict[str, Any] = {
                "dir": str(base),
                "out": filename,
                "continue": "true",
                "auto-file-renaming": "true",
                "split": str(max(1, split)),
                "max-connection-per-server": str(max(1, split)),
                "min-split-size": "1M",
                "remote-time": "true",
                "optimize-concurrent-downloads": "true",
            }
            use_direct = direct or remote
            if use_direct:
                if headers:
                    options["header"] = [
                        f"{key}: {value}" for key, value in headers.items()
                    ]
                url = raw_url
            else:
                token = tickets.create(
                    link=raw_url,
                    headers=headers,
                    filename=filename,
                    client=direct_client,
                    source=raw_url,
                )
                url = f"{proxy_origin.rstrip('/')}/api/aria2/stream/{token}"
            try:
                gid = aria.add_uri(url, options)
            except Aria2Error as exc:
                errors.append({"url": raw_url, "message": exc.message})
                continue
            self.task_index.add(
                gid,
                {
                    "path": raw_url,
                    "arcname": filename,
                    "name": filename,
                    "dir": str(base),
                    "size": 0,
                    "direct": use_direct,
                    "source": "direct",
                },
            )
            success.append({"url": raw_url, "gid": gid})
        return {
            "added": len(success),
            "failed": len(errors),
            "errors": errors[:50],
            "gids": [item["gid"] for item in success],
            "direct": direct and not remote,
        }


def _normalize_task(item: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    total = int(item.get("totalLength") or 0)
    completed = int(item.get("completedLength") or 0)
    files = item.get("files") or []
    filename = str(meta.get("name") or "")
    if not filename and isinstance(files, list) and files:
        first = files[0] if isinstance(files[0], dict) else {}
        filename = Path(str(first.get("path") or "")).name
    status = str(item.get("status") or "unknown")
    labels = {
        "active": "下载中",
        "waiting": "等待中",
        "paused": "已暂停",
        "error": "失败",
        "complete": "已完成",
        "removed": "已移除",
    }
    speed = int(item.get("downloadSpeed") or 0)
    eta = 0
    if status == "active" and speed > 0 and total > completed:
        eta = int((total - completed) / speed)
    error = str(item.get("errorMessage") or "")
    error_code = str(item.get("errorCode") or "")
    if error_code == "16" or "access is denied" in error.lower():
        error = "无法写入保存目录（权限不足或被占用），请在设置中更换下载目录"
    elif "status=410" in error or "status=404" in error:
        error = "下载链接已过期，请重新添加任务"
    elif "status=403" in error:
        error = "资源站拒绝了下载请求（403），请检查账号权限"
    return {
        "gid": str(item.get("gid") or ""),
        "name": filename,
        "path": str(meta.get("path") or ""),
        "arcname": str(meta.get("arcname") or filename),
        "dir": str(item.get("dir") or meta.get("dir") or ""),
        "source": str(meta.get("source") or "openlist"),
        "status": status,
        "statusText": labels.get(status, status),
        "total": total,
        "completed": completed,
        "percent": round(completed * 100 / total, 1) if total else 0,
        "speed": speed,
        "eta": eta,
        "connections": int(item.get("connections") or 0),
        "error": error,
        "added": float(meta.get("added") or 0),
    }


def _filename_from_url(url: str, index: int = 0) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        for key in ("filename", "file_name", "download_name", "name"):
            values = query.get(key)
            if values and values[0].strip():
                return urllib.parse.unquote(values[0].strip())
        name = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
        if name:
            return name
    except (ValueError, UnicodeDecodeError):
        pass
    return f"download_{index + 1}"


def _is_loopback_endpoint(endpoint: str) -> bool:
    host = urllib.parse.urlsplit(endpoint).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")