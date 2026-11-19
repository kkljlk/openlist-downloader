"""Ephemeral download jobs, capability tickets and streaming helpers."""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .config import safe_arcname, sanitize_filename
from .openlist import OpenListClient, OpenListError

LOG = logging.getLogger(__name__)

CHUNK = 128 * 1024


class ClientDisconnected(Exception):
    pass


class DownloadError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


class Writer(Protocol):
    headers_sent: bool

    def send_headers(self, status: int, headers: dict[str, str], *, chunked: bool) -> None: ...
    def write(self, data: bytes) -> None: ...
    def finish(self) -> None: ...


@dataclass
class Ticket:
    token: str
    link: str
    headers: dict[str, str]
    filename: str
    size: int
    source: str
    client: OpenListClient
    created: float = field(default_factory=time.time)
    expires: float = field(default_factory=lambda: time.time() + 6 * 3600)


class TicketStore:
    """Capability URLs used by aria2.

    Tickets are persisted so that aria2 tasks queued before an application
    restart can still fetch their data (as long as the signed upstream link
    has not expired).
    """

    def __init__(self, ttl: float = 6 * 3600, path: Path | None = None):
        self.ttl = ttl
        self.path = Path(path) if path else None
        self._items: dict[str, Ticket] = {}
        self._lock = threading.RLock()
        self._dirty = False
        self._timer: threading.Timer | None = None
        if self.path:
            self._load()

    def create(
        self,
        *,
        link: str,
        headers: dict[str, str],
        filename: str,
        client: OpenListClient,
        size: int = 0,
        source: str = "",
    ) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._prune_locked()
            self._items[token] = Ticket(
                token=token,
                link=link,
                headers=dict(headers or {}),
                filename=filename,
                size=size,
                source=source,
                client=client,
                expires=time.time() + self.ttl,
            )
            self._dirty = True
            self._schedule_save_locked()
        return token

    def get(self, token: str) -> Ticket | None:
        with self._lock:
            ticket = self._items.get(token)
            if ticket is None:
                return None
            if ticket.expires < time.time():
                self._items.pop(token, None)
                self._dirty = True
                self._schedule_save_locked()
                return None
            return ticket

    def _prune_locked(self) -> None:
        now = time.time()
        for key, ticket in list(self._items.items()):
            if ticket.expires < now:
                self._items.pop(key, None)
                self._dirty = True

    # -- persistence -----------------------------------------------------
    def _load(self) -> None:
        if not self.path:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        entries = raw.get("tickets") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            return
        now = time.time()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                expires = float(entry.get("expires") or 0)
                if expires <= now:
                    continue
                client_info = entry.get("client") or {}
                client = OpenListClient(
                    str(client_info.get("base_url") or ""),
                    token=str(client_info.get("token") or ""),
                    insecure=bool(client_info.get("insecure")),
                )
                ticket = Ticket(
                    token=str(entry.get("token") or ""),
                    link=str(entry.get("link") or ""),
                    headers={
                        str(k): str(v)
                        for k, v in (entry.get("headers") or {}).items()
                    },
                    filename=str(entry.get("filename") or "download"),
                    size=int(entry.get("size") or 0),
                    source=str(entry.get("source") or ""),
                    client=client,
                    created=float(entry.get("created") or now),
                    expires=expires,
                )
            except (TypeError, ValueError, OSError, OpenListError):
                continue
            if ticket.token and ticket.link:
                self._items[ticket.token] = ticket

    def _schedule_save_locked(self) -> None:
        if not self.path or self._timer is not None:
            return
        timer = threading.Timer(0.4, self._flush)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _flush(self) -> None:
        with self._lock:
            self._timer = None
            if not self._dirty or not self.path:
                return
            self._dirty = False
            payload = {"version": 1, "tickets": []}
            for ticket in self._items.values():
                payload["tickets"].append(
                    {
                        "token": ticket.token,
                        "link": ticket.link,
                        "headers": ticket.headers,
                        "filename": ticket.filename,
                        "size": ticket.size,
                        "source": ticket.source,
                        "created": ticket.created,
                        "expires": ticket.expires,
                        "client": {
                            "base_url": ticket.client.base_url,
                            "token": ticket.client.token,
                            "insecure": ticket.client.insecure,
                        },
                    }
                )
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                os.replace(tmp, self.path)
            except OSError as exc:
                LOG.warning("could not persist download tickets: %s", exc)

    def flush(self) -> None:
        self._flush()


@dataclass
class DownloadJob:
    id: str
    files: list[dict[str, Any]]
    dirs: list[str]
    client: OpenListClient
    single: bool
    filename: str
    total_size: int
    created: float
    expires: float
    reserved_names: set[str] = field(default_factory=set)


class DownloadStore:
    def __init__(self, ttl: float = 6 * 3600):
        self.ttl = ttl
        self._jobs: dict[str, DownloadJob] = {}
        self._lock = threading.RLock()

    def create(
        self,
        files: list[dict[str, Any]],
        dirs: list[str],
        client: OpenListClient,
        *,
        force_zip: bool = False,
    ) -> DownloadJob:
        normalised: list[dict[str, Any]] = []
        used: set[str] = set()
        for item in files:
            arc = safe_arcname(str(item.get("arcname") or item.get("name") or ""))
            if not arc:
                continue
            arc = _dedupe_arc(arc, used)
            used.add(arc.lower())
            normalised.append(
                {
                    "path": str(item.get("path") or ""),
                    "arcname": arc,
                    "size": int(item.get("size") or 0),
                }
            )
        clean_dirs = []
        for value in dirs:
            arc = safe_arcname(value)
            if arc and arc.lower() not in used:
                clean_dirs.append(arc)
                used.add(arc.lower())
        single = len(normalised) == 1 and not clean_dirs and not force_zip
        if single:
            filename = normalised[0]["arcname"].rsplit("/", 1)[-1]
        else:
            filename = time.strftime("OpenList_%Y%m%d_%H%M%S.zip")
        jid = secrets.token_urlsafe(18)
        job = DownloadJob(
            id=jid,
            files=normalised,
            dirs=clean_dirs,
            client=client,
            single=single,
            filename=filename,
            total_size=sum(int(item.get("size") or 0) for item in normalised),
            created=time.time(),
            expires=time.time() + self.ttl,
        )
        with self._lock:
            self._prune_locked()
            self._jobs[jid] = job
        return job

    def get(self, jid: str) -> DownloadJob | None:
        with self._lock:
            job = self._jobs.get(jid)
            if job is None:
                return None
            if job.expires < time.time():
                self._jobs.pop(jid, None)
                return None
            return job

    def _prune_locked(self) -> None:
        now = time.time()
        if len(self._jobs) < 64:
            return
        for key, job in list(self._jobs.items()):
            if job.expires < now:
                self._jobs.pop(key, None)


def _dedupe_arc(arc: str, used: set[str]) -> str:
    if arc.lower() not in used:
        return arc
    head, _, tail = arc.rpartition("/")
    name = tail
    if "." in name:
        stem, ext = name.rsplit(".", 1)
        counter = 2
        candidate = f"{stem} ({counter}).{ext}"
        while f"{head}/{candidate}".lower().lstrip("/") in used:
            counter += 1
            candidate = f"{stem} ({counter}).{ext}"
        return f"{head}/{candidate}" if head else candidate
    counter = 2
    candidate = f"{name} ({counter})"
    while f"{head}/{candidate}".lower().lstrip("/") in used:
        counter += 1
        candidate = f"{name} ({counter})"
    return f"{head}/{candidate}" if head else candidate


def content_disposition(filename: str) -> str:
    filename = filename or "download"
    ascii_name = (
        filename.encode("ascii", "replace")
        .decode("ascii")
        .replace("\\", "_")
        .replace('"', "_")
    )
    if not ascii_name or set(ascii_name) == {"?"}:
        suffix = ""
        if "." in filename:
            candidate = filename.rsplit(".", 1)[-1]
            if candidate.isascii() and candidate.isalnum() and len(candidate) <= 8:
                suffix = "." + candidate
        ascii_name = "download" + suffix
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{urllib.parse.quote(filename, safe='')}"
    )


def stream_ticket(
    writer: Writer,
    ticket: Ticket,
    *,
    range_header: str = "",
    head: bool = False,
    disposition: bool = False,
) -> None:
    stream_link(
        writer,
        client=ticket.client,
        link=ticket.link,
        headers=ticket.headers,
        filename=ticket.filename,
        range_header=range_header,
        head=head,
        disposition=disposition,
    )


def stream_job(
    writer: Writer,
    job: DownloadJob,
    *,
    range_header: str = "",
    head: bool = False,
) -> None:
    if job.single:
        item = job.files[0]
        try:
            info = job.client.get_file(item["path"])
        except OpenListError as exc:
            raise DownloadError(exc.message, 401 if exc.auth else 502) from exc
        stream_link(
            writer,
            client=job.client,
            link=info["link"],
            headers=info["headers"],
            filename=job.filename,
            range_header=range_header,
            head=head,
            disposition=True,
            fallback_size=info["size"],
        )
        return
    stream_zip(writer, job, head=head)


def stream_link(
    writer: Writer,
    *,
    client: OpenListClient,
    link: str,
    headers: dict[str, str],
    filename: str,
    range_header: str = "",
    head: bool = False,
    disposition: bool = True,
    fallback_size: int = 0,
) -> None:
    method = "HEAD" if head else "GET"
    try:
        resp = client.open_stream(
            link,
            headers,
            range_header=range_header,
            method=method,
        )
    except urllib.error.HTTPError as exc:
        raise DownloadError(
            f"上游返回 HTTP {exc.code}", 404 if exc.code == 404 else 502
        ) from exc
    except OpenListError as exc:
        raise DownloadError(exc.message, 502) from exc
    with resp:
        status = getattr(resp, "status", 200) or 200
        upstream_headers = resp.headers
        out_headers: dict[str, str] = {}
        for key in (
            "Content-Type",
            "Content-Length",
            "Content-Range",
            "Accept-Ranges",
            "Content-Encoding",
            "Last-Modified",
            "ETag",
        ):
            value = upstream_headers.get(key)
            if value:
                out_headers[key] = value
        out_headers.setdefault("Content-Type", "application/octet-stream")
        if disposition:
            out_headers["Content-Disposition"] = content_disposition(filename)
        out_headers["Cache-Control"] = "no-store"
        out_headers["X-Accel-Buffering"] = "no"
        if head:
            writer.send_headers(status, out_headers, chunked=False)
        elif "Content-Length" in out_headers:
            writer.send_headers(status, out_headers, chunked=False)
        else:
            out_headers["Transfer-Encoding"] = "chunked"
            writer.send_headers(status, out_headers, chunked=True)
        if head:
            writer.finish()
            return
        try:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                writer.write(chunk)
            writer.finish()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ClientDisconnected() from exc


def stream_zip(writer: Writer, job: DownloadJob, *, head: bool = False) -> None:
    headers = {
        "Content-Type": "application/zip",
        "Content-Disposition": content_disposition(job.filename),
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
        "Transfer-Encoding": "chunked",
    }
    if head:
        # Size is unknown until the archive is generated.
        headers.pop("Transfer-Encoding", None)
        writer.send_headers(200, headers, chunked=False)
        writer.finish()
        return
    writer.send_headers(200, headers, chunked=True)
    errors: list[str] = []
    try:
        with zipfile.ZipFile(
            writer,  # type: ignore[arg-type]
            "w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as archive:
            for directory in job.dirs:
                try:
                    archive.writestr(directory.rstrip("/") + "/", b"")
                except (OSError, ValueError) as exc:
                    errors.append(f"{directory}/：{exc}")
            for item in job.files:
                path = item["path"]
                arcname = item["arcname"]
                try:
                    info = job.client.get_file(path)
                except OpenListError as exc:
                    errors.append(f"{arcname}：{exc.message}")
                    continue
                try:
                    resp = job.client.open_stream(info["link"], info["headers"])
                except urllib.error.HTTPError as exc:
                    errors.append(f"{arcname}：上游 HTTP {exc.code}")
                    continue
                except (OpenListError, OSError) as exc:
                    errors.append(f"{arcname}：{exc}")
                    continue
                try:
                    with resp, archive.open(arcname, "w") as target:
                        while True:
                            chunk = resp.read(CHUNK)
                            if not chunk:
                                break
                            target.write(chunk)
                except (BrokenPipeError, ConnectionResetError) as exc:
                    raise ClientDisconnected() from exc
                except (OSError, ValueError, zipfile.BadZipFile) as exc:
                    errors.append(f"{arcname}：{exc}")
                    continue
            if errors:
                report = (
                    "以下文件没有成功加入压缩包：\n\n"
                    + "\n".join(errors[:500])
                    + ("\n..." if len(errors) > 500 else "")
                    + "\n"
                )
                try:
                    archive.writestr("_下载失败清单.txt", report.encode("utf-8"))
                except (OSError, ValueError):
                    pass
        writer.finish()
    except (BrokenPipeError, ConnectionResetError) as exc:
        raise ClientDisconnected() from exc


def make_zip_test_bytes(
    files: list[tuple[str, bytes]], dirs: list[str] | None = None
) -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for directory in dirs or []:
            archive.writestr(directory.rstrip("/") + "/", b"")
        for name, data in files:
            archive.writestr(name, data)
    return buffer.getvalue()