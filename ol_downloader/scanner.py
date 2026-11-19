"""Recursive directory walking and whole-site scan jobs."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from .config import safe_arcname
from .openlist import OpenListClient, OpenListError, normalize_path

LOG = logging.getLogger(__name__)

MAX_FILES = 100_000
MAX_ERRORS = 80


class ScanLimit(Exception):
    pass


def expand_items(
    client: OpenListClient,
    items: list[dict[str, Any]],
    *,
    max_files: int = MAX_FILES,
    concurrency: int = 6,
) -> dict[str, Any]:
    """Expand selected files/folders into a deduplicated file list.

    Each root keeps its display name as archive prefix, so selecting a folder
    ``Movies`` produces ``Movies/2024/a.mkv`` inside the archive.
    """
    started = time.time()
    files: list[dict[str, Any]] = []
    dirs: list[str] = []
    errors: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    seen_arc: set[str] = set()
    total_size = 0

    queue: list[tuple[str, str]] = []
    for item in items:
        path = normalize_path(str(item.get("path") or "/"))
        name = str(item.get("name") or path.rsplit("/", 1)[-1] or "files")
        prefix = safe_arcname(name)
        if item.get("is_dir"):
            queue.append((path, prefix))
        else:
            arc = safe_arcname(prefix or name)
            if path in seen_paths:
                continue
            seen_paths.add(path)
            arc = _unique_arc(arc, seen_arc)
            seen_arc.add(arc.lower())
            size = int(item.get("size") or 0)
            files.append({"path": path, "arcname": arc, "size": size})
            total_size += size
            if len(files) > max_files:
                raise ScanLimit(f"文件数量超过上限 {max_files:,}，请分批下载")

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        level = queue
        while level:
            next_level: list[tuple[str, str]] = []
            futures = {
                pool.submit(client.list_dir, path): (path, prefix)
                for path, prefix in level
            }
            for future in as_completed(futures):
                path, prefix = futures[future]
                try:
                    entries, _meta = future.result()
                except OpenListError as exc:
                    if len(errors) < MAX_ERRORS:
                        errors.append({"path": path, "message": exc.message})
                    continue
                if not entries:
                    if prefix:
                        dirs.append(prefix)
                    continue
                for entry in entries:
                    name = str(entry.get("name") or "")
                    if not name:
                        continue
                    child_path = str(entry.get("path") or (path.rstrip("/") + "/" + name))
                    child_arc = f"{prefix}/{safe_arcname(name)}" if prefix else safe_arcname(name)
                    if entry.get("is_dir"):
                        next_level.append((child_path, child_arc))
                    elif child_path not in seen_paths:
                        seen_paths.add(child_path)
                        arc = _unique_arc(child_arc, seen_arc)
                        seen_arc.add(arc.lower())
                        size = int(entry.get("size") or 0)
                        files.append({"path": child_path, "arcname": arc, "size": size})
                        total_size += size
                        if len(files) > max_files:
                            raise ScanLimit(f"文件数量超过上限 {max_files:,}，请分批下载")
            level = next_level
    files.sort(key=lambda item: item["arcname"].lower())
    dirs = sorted({d for d in dirs if d and d.lower() not in seen_arc})
    return {
        "files": files,
        "dirs": dirs,
        "errors": errors,
        "count": len(files),
        "size": total_size,
        "elapsed": round(time.time() - started, 2),
    }


def _unique_arc(arc: str, seen: set[str]) -> str:
    arc = safe_arcname(arc)
    if not arc:
        arc = "download"
    if arc.lower() not in seen:
        return arc
    if "." in arc.rsplit("/", 1)[-1]:
        head, ext = arc.rsplit(".", 1)
        counter = 2
        candidate = f"{head} ({counter}).{ext}"
        while candidate.lower() in seen:
            counter += 1
            candidate = f"{head} ({counter}).{ext}"
        return candidate
    counter = 2
    candidate = f"{arc} ({counter})"
    while candidate.lower() in seen:
        counter += 1
        candidate = f"{arc} ({counter})"
    return candidate


class SiteScan:
    """Background BFS over an entire OpenList site."""

    def __init__(
        self,
        client: OpenListClient,
        root: str = "/",
        *,
        max_files: int = MAX_FILES,
        concurrency: int = 6,
    ):
        self.id = uuid.uuid4().hex
        self.client = client
        self.root = normalize_path(root)
        self.max_files = max_files
        self.concurrency = max(1, concurrency)
        self.created = time.time()
        self.started_at = 0.0
        self.finished_at = 0.0
        self.state = "queued"
        self.cancel_event = threading.Event()
        self.lock = threading.Lock()
        self.files: list[dict[str, Any]] = []
        self.dirs: list[str] = []
        self.errors: list[dict[str, str]] = []
        self.dirs_done = 0
        self.dirs_queued = 1
        self.total_size = 0
        self.message = ""
        self._seen_paths: set[str] = set()
        self._seen_arc: set[str] = set()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"scan-{self.id[:6]}", daemon=True
        )
        self._thread.start()

    def cancel(self) -> None:
        self.cancel_event.set()
        with self.lock:
            self.message = "正在停止…"

    def to_dict(self, include_files: bool = False) -> dict[str, Any]:
        with self.lock:
            data = {
                "id": self.id,
                "root": self.root,
                "state": self.state,
                "created": self.created,
                "elapsed": round(
                    (self.finished_at or time.time()) - (self.started_at or self.created),
                    1,
                ),
                "dirsDone": self.dirs_done,
                "dirsQueued": self.dirs_queued,
                "files": len(self.files),
                "size": self.total_size,
                "errors": len(self.errors),
                "errorSamples": self.errors[:10],
                "message": self.message,
                "sample": self.files[:80],
                "dirSamples": self.dirs[:40],
            }
            if include_files:
                data["fileList"] = list(self.files)
                data["dirList"] = list(self.dirs)
            return data

    def _run(self) -> None:
        with self.lock:
            self.state = "running"
            self.started_at = time.time()
        try:
            self._walk()
            with self.lock:
                if self.cancel_event.is_set():
                    self.state = "cancelled"
                else:
                    self.state = "done"
                    self.message = "扫描完成"
        except ScanLimit as exc:
            with self.lock:
                self.state = "limited"
                self.message = str(exc)
        except Exception as exc:  # pragma: no cover - defensive
            LOG.exception("site scan failed")
            with self.lock:
                self.state = "error"
                self.message = f"扫描失败：{exc}"
        finally:
            with self.lock:
                self.finished_at = time.time()

    def _walk(self) -> None:
        level = [(self.root, "")]
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            while level and not self.cancel_event.is_set():
                futures = {}
                for path, prefix in level:
                    if self.cancel_event.is_set():
                        break
                    futures[pool.submit(self.client.list_dir, path)] = (path, prefix)
                next_level: list[tuple[str, str]] = []
                for future in as_completed(futures):
                    if self.cancel_event.is_set():
                        break
                    path, prefix = futures[future]
                    try:
                        entries, _meta = future.result()
                    except OpenListError as exc:
                        with self.lock:
                            if len(self.errors) < MAX_ERRORS:
                                self.errors.append({"path": path, "message": exc.message})
                            self.dirs_done += 1
                        continue
                    with self.lock:
                        self.dirs_done += 1
                    if not entries:
                        if prefix:
                            with self.lock:
                                self.dirs.append(prefix)
                        continue
                    for entry in entries:
                        name = str(entry.get("name") or "")
                        if not name:
                            continue
                        child_path = str(
                            entry.get("path") or (path.rstrip("/") + "/" + name)
                        )
                        child_arc = (
                            f"{prefix}/{safe_arcname(name)}"
                            if prefix
                            else safe_arcname(name)
                        )
                        if entry.get("is_dir"):
                            next_level.append((child_path, child_arc))
                        else:
                            with self.lock:
                                if child_path in self._seen_paths:
                                    continue
                                self._seen_paths.add(child_path)
                                arc = _unique_arc(child_arc, self._seen_arc)
                                self._seen_arc.add(arc.lower())
                                size = int(entry.get("size") or 0)
                                self.files.append(
                                    {"path": child_path, "arcname": arc, "size": size}
                                )
                                self.total_size += size
                                if len(self.files) > self.max_files:
                                    raise ScanLimit(
                                        f"文件数量超过上限 {self.max_files:,}，已停止扫描"
                                    )
                with self.lock:
                    self.dirs_queued += len(next_level)
                level = next_level
        if self.cancel_event.is_set():
            return