"""Application paths and persistent settings.

The app is intentionally portable: when frozen, settings live in ``data/``
next to the executable.  If that directory is not writable we fall back to
``%LOCALAPPDATA%``.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

APP_NAME = "OpenList 下载器"
APP_ID = "openlist-downloader"
APP_VERSION = "1.2.1"


def _exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """Directory containing bundled read-only resources (static/, tools/)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-test-{uuid.uuid4().hex}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def data_dir() -> Path:
    preferred = _exe_dir() / "data"
    if _writable(preferred):
        return preferred
    fallback = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "OpenListDownloader"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def downloads_dir() -> Path:
    """Best effort lookup of the user's Downloads known folder."""
    if os.name == "nt":
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            )
            value, _ = winreg.QueryValueEx(
                key, "{374DE290-123F-4565-9164-39C4925E467B}"
            )
            expanded = Path(os.path.expandvars(value))
            if expanded.exists():
                return expanded
        except OSError:
            pass
    candidate = Path.home() / "Downloads"
    if candidate.exists():
        return candidate
    return Path.home()


def default_download_dir() -> str:
    return str(downloads_dir() / "OpenList")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _default_config() -> dict[str, Any]:
    return {
        "version": 1,
        "sites": [],
        "active_site": "",
        "preferences": {
            "engine": "aria2",
            "last_paths": {},
            "theme": "dark",
            "show_hidden": True,
        },
        "aria2": {
            "endpoint": "",
            "secret": "",
            "dir": default_download_dir(),
            "managed": False,
            "port": 16800,
            "split": 16,
            "concurrency": 8,
            "direct": True,
        },
    }


class Config:
    def __init__(self, directory: Path | None = None):
        self.dir = directory or data_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "config.json"
        self._lock = threading.RLock()
        self.data: dict[str, Any] = _default_config()
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                self.save()
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                backup = self.path.with_suffix(".broken.json")
                try:
                    self.path.replace(backup)
                except OSError:
                    pass
                self.data = _default_config()
                self.save()
                return
            merged = _default_config()
            _deep_merge(merged, raw if isinstance(raw, dict) else {})
            self.data = merged

    def save(self) -> None:
        with self._lock:
            _atomic_write(
                self.path,
                json.dumps(self.data, ensure_ascii=False, indent=2) + "\n",
            )

    def get(self, *keys: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self.data
            for key in keys:
                if not isinstance(node, dict) or key not in node:
                    return default
                node = node[key]
            return node

    def set(self, value: Any, *keys: str) -> None:
        with self._lock:
            node = self.data
            for key in keys[:-1]:
                child = node.get(key)
                if not isinstance(child, dict):
                    child = {}
                    node[key] = child
                node = child
            node[keys[-1]] = value
            self.save()

    # -- sites -----------------------------------------------------------
    @property
    def sites(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self.data.get("sites", []))

    def find_site(self, site_id: str) -> dict[str, Any] | None:
        for site in self.sites:
            if site.get("id") == site_id:
                return site
        return None

    def upsert_site(self, site: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            sites = self.data.setdefault("sites", [])
            for index, existing in enumerate(sites):
                if existing.get("id") == site.get("id"):
                    sites[index] = {**existing, **site}
                    self.save()
                    return deepcopy(sites[index])
            sites.append(deepcopy(site))
            self.save()
            return deepcopy(site)

    def remove_site(self, site_id: str) -> bool:
        with self._lock:
            before = len(self.data.get("sites", []))
            self.data["sites"] = [
                site
                for site in self.data.get("sites", [])
                if site.get("id") != site_id
            ]
            if self.data.get("active_site") == site_id:
                self.data["active_site"] = ""
            changed = len(self.data["sites"]) != before
            if changed:
                self.save()
            return changed

    # -- aria2 -----------------------------------------------------------
    @property
    def aria2(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self.data.get("aria2", {}))

    def update_aria2(self, **values: Any) -> dict[str, Any]:
        with self._lock:
            self.data.setdefault("aria2", {}).update(values)
            self.save()
            return deepcopy(self.data["aria2"])

    def update_preferences(self, **values: Any) -> dict[str, Any]:
        with self._lock:
            self.data.setdefault("preferences", {}).update(values)
            self.save()
            return deepcopy(self.data["preferences"])


def _deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def sanitize_filename(name: str, fallback: str = "download") -> str:
    """Make a single file name safe for Windows while keeping it readable."""
    name = (name or "").replace("\x00", "").strip()
    for char in '<>:"/\\|?*':
        name = name.replace(char, "_")
    name = name.rstrip(" .")
    if not name:
        return fallback
    if name.upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        name = "_" + name
    return name[:180]


def safe_arcname(path: str) -> str:
    """Normalise a path for use inside a ZIP / on disk."""
    path = (path or "").replace("\\", "/").strip("/")
    parts = []
    for part in path.split("/"):
        if not part or part in (".", ".."):
            continue
        parts.append(sanitize_filename(part))
    return "/".join(parts)