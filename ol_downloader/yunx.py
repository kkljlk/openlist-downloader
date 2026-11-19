"""Optional bridge to the separate YunX-Desktop application.

YunX is a full Kotlin/Compose desktop application (AGPL-3.0), not a parsing
library.  It is shipped as a sibling portable folder and launched as its own
process; nothing from YunX is imported into this Python application.
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

YUNX_RELEASE_URL = "https://github.com/tidain/YunX-Desktop/releases/latest"
YUNX_EXE = "YunX-Desktop.exe"


def _base_dirs() -> list[Path]:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent.parent
    result = [base, base / "YunX", base / "yunx", base / "tools" / "YunX"]
    appdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        result.append(Path(appdata) / "Programs" / "YunX-Desktop")
    env = os.environ.get("YUNX_DESKTOP_EXE", "").strip()
    if env:
        result.insert(0, Path(env))
    return result


def find_yunx() -> Path | None:
    for candidate in _base_dirs():
        try:
            if candidate.is_file() and candidate.name.lower() == YUNX_EXE.lower():
                return candidate
            if candidate.is_dir():
                direct = candidate / YUNX_EXE
                if direct.is_file():
                    return direct
                if candidate.name.lower() == "yunx":
                    for item in candidate.iterdir():
                        if item.is_file() and item.name.lower() == YUNX_EXE.lower():
                            return item
        except OSError:
            continue
    return None


def status() -> dict[str, Any]:
    path = find_yunx()
    return {
        "installed": path is not None,
        "path": str(path) if path else "",
        "name": "云析 YunX-Desktop",
        "version": _read_version(path) if path else "",
        "releaseUrl": YUNX_RELEASE_URL,
        "license": "AGPL-3.0",
        "separate": True,
    }


def _read_version(path: Path | None) -> str:
    if not path:
        return ""
    for candidate in (path.parent / "version.txt", path.parent / "VERSION.txt"):
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8", errors="replace").strip()[:80]
        except OSError:
            pass
    return ""


def _set_clipboard(text: str) -> bool:
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.restype = ctypes.c_bool
        user32.EmptyClipboard.restype = ctypes.c_bool
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.CloseClipboard.restype = ctypes.c_bool
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
            data = (text + "\x00").encode("utf-16-le")
            handle = kernel32.GlobalAlloc(0x0042, len(data))  # GMEM_MOVEABLE | GMEM_ZEROINIT
            if not handle:
                return False
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                kernel32.GlobalFree(handle)
                return False
            try:
                ctypes.memmove(pointer, data, len(data))
            finally:
                kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(13, handle):  # CF_UNICODETEXT
                kernel32.GlobalFree(handle)
                return False
            return True
        finally:
            user32.CloseClipboard()
    except Exception as exc:  # pragma: no cover - platform dependent
        LOG.warning("clipboard write failed: %s", exc)
        return False


def open_yunx(url: str = "") -> dict[str, Any]:
    path = find_yunx()
    if path is None:
        raise FileNotFoundError("没有找到云析便携版")
    url = (url or "").strip()
    if url:
        if not re.search(r"https?://[^\s]+", url, re.IGNORECASE):
            raise ValueError("没有找到 http/https 链接")
        # Copy the whole text: YunX can use a trailing extraction code too.
        _set_clipboard(url)
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    subprocess.Popen(
        [str(path)],
        cwd=str(path.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )
    return {
        "launched": True,
        "path": str(path),
        "copied": bool(url),
        "message": "链接已复制，云析已启动。首次使用请先在云析中登录网盘。",
    }