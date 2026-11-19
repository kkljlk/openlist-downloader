"""Small, dependency-free client for the OpenList / AList HTTP API."""
from __future__ import annotations

import hashlib
import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

LOG = logging.getLogger(__name__)

USER_AGENT = "OpenListDownloader/1.0 (+local)"


class OpenListError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: int | str | None = None,
        status: int = 0,
        auth: bool = False,
        cert: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.auth = auth
        self.cert = cert


def normalize_base_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise OpenListError("请输入资源站地址")
    if "://" not in value:
        value = "http://" + value
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise OpenListError("地址必须以 http:// 或 https:// 开头")
    if not parts.netloc:
        raise OpenListError("资源站地址不完整")
    path = parts.path.rstrip("/")
    for suffix in ("/@login", "/@manage", "/@index"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not leak OpenList credentials to a third-party download host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        try:
            old_host = urllib.parse.urlsplit(req.full_url).hostname
            new_host = urllib.parse.urlsplit(newurl).hostname
            if old_host and new_host and old_host.lower() != new_host.lower():
                new.remove_header("Authorization")
        except (ValueError, AttributeError):
            pass
        return new


def _make_opener(insecure: bool) -> urllib.request.OpenerDirector:
    context = ssl.create_default_context()
    if insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        _SafeRedirectHandler(),
    )


def _is_cert_error(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True
    text = str(reason).lower()
    return "certificate verify failed" in text or "certificate_verify_failed" in text


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_headers(value: Any) -> dict[str, str]:
    """OpenList has returned headers as a dict, JSON string and raw text."""
    if not value:
        return {}
    parsed: Any = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = {}
            for line in text.splitlines():
                if ":" in line:
                    key, val = line.split(":", 1)
                    parsed[key.strip()] = val.strip()
    if not isinstance(parsed, dict):
        return {}
    blocked = {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "accept-encoding",
        "range",
        "content-range",
        "if-range",
    }
    result: dict[str, str] = {}
    for key, val in parsed.items():
        if not isinstance(key, str) or not isinstance(val, (str, int, float)):
            continue
        name = key.strip()
        if not name or name.lower() in blocked:
            continue
        # Header names are tokens; reject control characters and spaces.
        if any(ord(ch) < 33 or ord(ch) > 126 for ch in name) or ":" in name:
            continue
        result[name] = str(val)
    return result


def join_url(base: str, link: str) -> str:
    link = (link or "").strip()
    if not link:
        return ""
    if link.startswith("//"):
        scheme = urllib.parse.urlsplit(base).scheme or "https"
        return f"{scheme}:{link}"
    if link.startswith(("http://", "https://")):
        return link
    return urllib.parse.urljoin(base.rstrip("/") + "/", link.lstrip("/"))


class OpenListClient:
    def __init__(self, base_url: str, token: str | None = None, insecure: bool = False):
        self.base_url = normalize_base_url(base_url)
        self.token = (token or "").strip()
        self.insecure = insecure
        self._opener = _make_opener(insecure)

    # -- low level -------------------------------------------------------
    def _request(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        method: str = "POST",
        timeout: float = 25.0,
        authenticated: bool = True,
        query: dict[str, str] | None = None,
    ) -> Any:
        url = self.base_url + path
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        data = None
        if method != "GET":
            data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if authenticated and self.token:
            headers["Authorization"] = self.token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                body = resp.read(8 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read(256 * 1024)
            except OSError:
                pass
            message, code = _error_from_body(body, exc.code)
            if exc.code in (401, 403):
                raise OpenListError(
                    message or "需要登录后才能访问该资源站",
                    code=code or exc.code,
                    status=exc.code,
                    auth=True,
                ) from exc
            raise OpenListError(
                message or f"资源站返回 HTTP {exc.code}",
                code=code,
                status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            if _is_cert_error(exc) and not self.insecure:
                self.insecure = True
                self._opener = _make_opener(True)
                return self._request(
                    path,
                    payload,
                    method=method,
                    timeout=timeout,
                    authenticated=authenticated,
                    query=query,
                )
            raise OpenListError(
                f"连接资源站失败：{_clean_reason(exc.reason)}",
                cert=_is_cert_error(exc),
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise OpenListError(f"连接资源站失败：{exc}") from exc

        if not body:
            raise OpenListError("资源站返回了空响应")
        try:
            result = json.loads(body.decode("utf-8", "replace"))
        except ValueError as exc:
            raise OpenListError("这个地址不像 OpenList/AList 资源站（返回的不是 JSON）") from exc
        if not isinstance(result, dict):
            raise OpenListError("资源站返回的数据格式不正确")
        code = _as_int(result.get("code"), 200)
        if code != 200:
            message = str(result.get("message") or result.get("msg") or "请求失败")
            raise OpenListError(
                message,
                code=code,
                status=401 if code in (401, 403) else 0,
                auth=code in (401, 403),
            )
        return result.get("data")

    # -- API -------------------------------------------------------------
    def public_settings(self) -> dict[str, Any]:
        data = self._request(
            "/api/public/settings",
            method="GET",
            authenticated=False,
            timeout=12.0,
        )
        return data if isinstance(data, dict) else {}

    def login(self, username: str, password: str, otp_code: str = "") -> str:
        if not username or not password:
            raise OpenListError("请输入用户名和密码")
        payload: dict[str, Any] = {"username": username, "password": password}
        if otp_code:
            payload["otp_code"] = otp_code
        errors: list[str] = []
        candidates = [password]
        hashed = hashlib.sha256(password.encode("utf-8")).hexdigest()
        if hashed != password:
            candidates.append(hashed)
        for index, candidate in enumerate(candidates):
            body = dict(payload)
            body["password"] = candidate
            try:
                data = self._request("/api/auth/login", body, authenticated=False)
            except OpenListError as exc:
                if exc.auth or exc.code in (401, 403):
                    errors.append(exc.message)
                    continue
                raise
            token = ""
            if isinstance(data, dict):
                token = str(data.get("token") or "")
            if not token:
                errors.append("登录成功但没有取得令牌")
                continue
            self.token = token
            return token
        raise OpenListError(
            errors[-1] if errors else "用户名或密码不正确",
            auth=True,
            status=401,
        )

    def me(self) -> dict[str, Any]:
        try:
            data = self._request("/api/me", method="GET", timeout=10.0)
        except OpenListError as exc:
            if exc.status == 404 or exc.code == 404:
                return {}
            raise
        return data if isinstance(data, dict) else {}

    def list_dir(
        self, path: str = "/", *, refresh: bool = False
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        path = normalize_path(path)
        data = self._request(
            "/api/fs/list",
            {
                "path": path,
                "password": "",
                "page": 1,
                "per_page": 0,
                "refresh": bool(refresh),
            },
        )
        if not isinstance(data, dict):
            data = {}
        content = data.get("content") or []
        if not isinstance(content, list):
            content = []
        entries = [_normalize_entry(item, path) for item in content if isinstance(item, dict)]
        total = _as_int(data.get("total"), len(entries))
        # Some deployments cap per_page=0.  Keep fetching until complete.
        seen = {entry["name"] for entry in entries}
        page = 2
        while total > len(entries) and 0 < len(content) < total and page <= 200:
            more = self._request(
                "/api/fs/list",
                {
                    "path": path,
                    "password": "",
                    "page": page,
                    "per_page": len(content),
                    "refresh": False,
                },
            )
            if not isinstance(more, dict):
                break
            batch = more.get("content") or []
            if not isinstance(batch, list) or not batch:
                break
            added = 0
            for item in batch:
                if not isinstance(item, dict):
                    continue
                entry = _normalize_entry(item, path)
                if entry["name"] in seen:
                    continue
                seen.add(entry["name"])
                entries.append(entry)
                added += 1
            if not added:
                break
            page += 1
        meta = {
            "path": path,
            "has_content_field": "content" in data,
            "total": max(total, len(entries)),
            "readme": str(data.get("readme") or ""),
            "write": bool(data.get("write")),
            "provider": str(data.get("provider") or ""),
            "header": str(data.get("header") or ""),
        }
        return entries, meta

    def get_file(self, path: str) -> dict[str, Any]:
        path = normalize_path(path)
        data = self._request(
            "/api/fs/get",
            {"path": path, "password": ""},
        )
        if not isinstance(data, dict):
            raise OpenListError("资源站没有返回文件信息")
        if data.get("is_dir"):
            raise OpenListError("这是一个文件夹，不能直接下载")
        link = str(data.get("raw_url") or data.get("url") or "").strip()
        if not link:
            sign = str(data.get("sign") or "")
            quoted = urllib.parse.quote(path, safe="/")
            link = "/d" + quoted
            if sign:
                link += "?sign=" + urllib.parse.quote(sign)
        link = join_url(self.base_url, link)
        if not link.startswith(("http://", "https://")):
            raise OpenListError("资源站没有返回可用的下载地址")
        headers = normalize_headers(data.get("header"))
        if not any(key.lower() == "user-agent" for key in headers):
            headers["User-Agent"] = USER_AGENT
        if self.token and _same_origin(self.base_url, link):
            if not any(key.lower() == "authorization" for key in headers):
                headers["Authorization"] = self.token
        return {
            "path": path,
            "name": str(data.get("name") or path.rsplit("/", 1)[-1]),
            "size": _as_int(data.get("size")),
            "is_dir": False,
            "link": link,
            "headers": headers,
            "provider": str(data.get("provider") or ""),
            "modified": str(data.get("modified") or ""),
        }

    def open_stream(
        self,
        link: str,
        headers: dict[str, str] | None = None,
        *,
        range_header: str = "",
        method: str = "GET",
        timeout: float = 45.0,
    ):
        """Open an upstream URL for the local streaming proxy."""
        if not link.startswith(("http://", "https://")):
            raise OpenListError("下载地址无效")
        merged = {"Accept": "*/*", "Accept-Encoding": "identity"}
        for key, value in (headers or {}).items():
            if key.lower() in ("host", "content-length", "transfer-encoding"):
                continue
            merged[key] = value
        if self.token and _same_origin(self.base_url, link):
            if not any(key.lower() == "authorization" for key in merged):
                merged["Authorization"] = self.token
        if range_header:
            merged["Range"] = range_header
        req = urllib.request.Request(link, headers=merged, method=method)
        try:
            return self._opener.open(req, timeout=timeout)
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError as exc:
            if _is_cert_error(exc) and not self.insecure:
                self.insecure = True
                self._opener = _make_opener(True)
                return self.open_stream(
                    link,
                    headers,
                    range_header=range_header,
                    method=method,
                    timeout=timeout,
                )
            raise OpenListError(
                f"连接下载地址失败：{_clean_reason(exc.reason)}",
                cert=_is_cert_error(exc),
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise OpenListError(f"连接下载地址失败：{exc}") from exc


def _normalize_entry(item: dict[str, Any], parent: str) -> dict[str, Any]:
    name = str(item.get("name") or "")
    path = parent.rstrip("/") + "/" + name if name else parent
    is_dir = bool(item.get("is_dir"))
    return {
        "name": name,
        "path": path,
        "is_dir": is_dir,
        "size": 0 if is_dir else _as_int(item.get("size")),
        "modified": str(item.get("modified") or ""),
        "created": str(item.get("created") or ""),
        "type": _as_int(item.get("type")),
        "thumb": str(item.get("thumb") or ""),
        "provider": str(item.get("provider") or ""),
    }


def normalize_path(path: str) -> str:
    path = "/" + (path or "").strip().lstrip("/")
    return path or "/"


def _same_origin(base: str, link: str) -> bool:
    try:
        left = urllib.parse.urlsplit(base)
        right = urllib.parse.urlsplit(link)
        return (
            left.scheme.lower() == right.scheme.lower()
            and (left.hostname or "").lower() == (right.hostname or "").lower()
            and (left.port or (443 if left.scheme == "https" else 80))
            == (right.port or (443 if right.scheme == "https" else 80))
        )
    except ValueError:
        return False


def _clean_reason(reason: Any) -> str:
    text = str(reason or "")
    if "timed out" in text.lower():
        return "连接超时"
    if "refused" in text.lower():
        return "连接被拒绝，资源站是否已启动？"
    if "name or service not known" in text.lower() or "getaddrinfo" in text.lower():
        return "域名解析失败"
    return text[:240]


def _error_from_body(body: bytes, status: int) -> tuple[str, int | None]:
    if not body:
        return "", None
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        text = body.decode("utf-8", "replace").strip()
        return (text[:200] if text and "<" not in text[:1] else ""), None
    if isinstance(data, dict):
        return (
            str(data.get("message") or data.get("msg") or ""),
            _as_int(data.get("code"), 0) or None,
        )
    return "", None


NETDISK_HOSTS = (
    "pan.baidu.com",
    "yun.baidu.com",
    "aliyundrive.com",
    "pan.quark.cn",
    "115.com",
    "cloud.189.cn",
    "drive.google.com",
    "onedrive.live.com",
    "mega.nz",
)


def _netdisk_host(url: str) -> str:
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    for item in NETDISK_HOSTS:
        if host == item or host.endswith("." + item):
            return item
    return ""


def probe_site(url: str, *, insecure: bool = False) -> dict[str, Any]:
    """Check whether a URL looks like an OpenList/AList site."""
    netdisk = _netdisk_host(url)
    if netdisk:
        if "baidu" in netdisk:
            raise OpenListError(
                "这是百度网盘网页，不是 OpenList/AList 资源站，不能在这里登录百度账号。"
                "请填写 OpenList 地址（例如 http://127.0.0.1:5244），或改用“链接下载”粘贴百度直链。"
            )
        raise OpenListError(
            "这是网盘网页，不是 OpenList/AList 资源站，不能在这里登录网盘账号。"
            "请填写 OpenList 地址，或改用“链接下载”。"
        )
    client = OpenListClient(url, insecure=insecure)
    settings: dict[str, Any] = {}
    settings_error: OpenListError | None = None
    try:
        settings = client.public_settings()
    except OpenListError as exc:
        settings_error = exc
    title = str(
        settings.get("site_title")
        or settings.get("title")
        or settings.get("site_name")
        or ""
    ).strip()
    version = str(settings.get("version") or "").strip()
    settings_valid = any(
        key in settings
        for key in (
            "site_title",
            "title",
            "site_name",
            "version",
            "allow_indexed",
            "allow_mounted",
            "favicon",
            "logo",
            "slogan",
        )
    )
    auth_required = False
    reachable = settings_valid
    api_shape = False
    if not reachable:
        try:
            _entries, meta = client.list_dir("/")
            api_shape = bool(
                meta.get("has_content_field")
                or meta.get("provider")
                or meta.get("write")
            )
            reachable = api_shape
        except OpenListError as exc:
            if exc.auth:
                reachable = True
                auth_required = True
            elif settings_error is None:
                settings_error = exc
    if not reachable:
        message = settings_error.message if settings_error else "无法连接"
        if not api_shape:
            message = "这个地址不是 OpenList/AList 资源站（没有找到 OpenList API）。" + message
        raise OpenListError(message)
    parsed = urllib.parse.urlsplit(client.base_url)
    return {
        "url": client.base_url,
        "name": title or parsed.netloc,
        "version": version,
        "auth_required": auth_required,
        "insecure": client.insecure,
        "settings": {
            "allow_indexed": bool(settings.get("allow_indexed")),
            "allow_mounted": bool(settings.get("allow_mounted")),
        },
    }