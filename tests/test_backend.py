from __future__ import annotations

import hashlib
import http.cookiejar
import io
import json
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from mock_openlist import (  # noqa: E402
    MockAria2,
    MockOpenList,
    PASSWORD,
    TOKEN,
    USERNAME,
    start_server,
    stop_server,
)
from ol_downloader.config import Config  # noqa: E402
from ol_downloader.openlist import OpenListClient  # noqa: E402
from ol_downloader.transfers import TicketStore  # noqa: E402
from ol_downloader.web import AppServer  # noqa: E402


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def request(self, method: str, path: str, data=None, headers=None):
        body = None
        hdrs = dict(headers or {})
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base + path, data=body, headers=hdrs, method=method
        )
        try:
            with self.opener.open(req, timeout=30) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def json(self, method: str, path: str, data=None):
        status, headers, body = self.request(method, path, data)
        payload = json.loads(body.decode("utf-8"))
        return status, payload


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="old-test-"))
        cls.static = cls.tmp / "static"
        cls.static.mkdir()
        (cls.static / "index.html").write_text("<html>test</html>", encoding="utf-8")
        cls.mock, cls.mock_thread = start_server(MockOpenList)
        cls.mock_base = f"http://127.0.0.1:{cls.mock.server_port}"
        cls.config = Config(cls.tmp / "data")
        cls.server = AppServer(("127.0.0.1", 0), cls.config, cls.static)
        cls.app_base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = __import__("threading").Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()
        time.sleep(0.1)
        cls.client = Client(cls.app_base)
        status, data = cls.client.json(
            "POST", "/api/sites", {"url": cls.mock_base}
        )
        assert status == 200, data
        cls.site_id = data["data"]["site"]["id"]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        stop_server(cls.mock)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_01_bootstrap_connect_and_list(self):
        status, data = self.client.json("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(data["data"]["sites"][0]["name"], "测试资源站")
        status, data = self.client.json(
            "POST", "/api/connect", {"site_id": self.site_id}
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(data["data"]["session"]["siteName"], "测试资源站")
        status, data = self.client.json("POST", "/api/fs/list", {"path": "/"})
        self.assertEqual(status, 200, data)
        names = {entry["name"] for entry in data["data"]["entries"]}
        self.assertIn("Movies", names)
        self.assertIn("Docs", names)
        self.assertIn("中文", names)

    def test_02_expand_and_zip(self):
        status, data = self.client.json(
            "POST",
            "/api/expand",
            {"items": [{"path": "/Movies", "name": "Movies", "is_dir": True}]},
        )
        self.assertEqual(status, 200, data)
        expanded = data["data"]
        self.assertEqual(expanded["count"], 3)
        self.assertTrue(all(item["arcname"].startswith("Movies/") for item in expanded["files"]))
        status, data = self.client.json(
            "POST",
            "/api/download/prepare",
            {
                "items": [
                    {"path": "/Movies", "name": "Movies", "is_dir": True},
                    {"path": "/Docs/readme.txt", "name": "readme.txt", "is_dir": False},
                ]
            },
        )
        self.assertEqual(status, 200, data)
        job = data["data"]
        self.assertEqual(job["kind"], "zip")
        self.assertEqual(job["count"], 4)
        status, headers, body = self.client.request("GET", job["url"])
        self.assertEqual(status, 200)
        self.assertIn("application/zip", headers.get("Content-Type", ""))
        archive = zipfile.ZipFile(io.BytesIO(body))
        names = set(archive.namelist())
        self.assertIn("Movies/Inception.2010.1080p.mkv", names)
        self.assertIn("Movies/Subs/en.srt", names)
        self.assertIn("readme.txt", names)
        self.assertEqual(len(archive.read("Movies/Subs/en.srt")), 1234)

    def test_03_single_and_range(self):
        status, data = self.client.json(
            "POST",
            "/api/download/prepare",
            {"files": [{"path": "/Movies/Inception.2010.1080p.mkv", "arcname": "Inception.mkv", "size": 1_500_000}]},
        )
        self.assertEqual(status, 200, data)
        job = data["data"]
        self.assertEqual(job["kind"], "single")
        status, headers, body = self.client.request(
            "GET", job["url"], headers={"Range": "bytes=100-199"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(len(body), 100)
        self.assertIn("Content-Range", headers)

    def test_04_scan_all_and_prepare(self):
        status, data = self.client.json("POST", "/api/scan/start", {})
        self.assertEqual(status, 200, data)
        scan_id = data["data"]["scan"]["id"]
        deadline = time.time() + 15
        scan = None
        while time.time() < deadline:
            status, data = self.client.json("GET", f"/api/scan/{scan_id}")
            scan = data["data"]["scan"]
            if scan["state"] in ("done", "error", "limited", "cancelled"):
                break
            time.sleep(0.1)
        self.assertEqual(scan["state"], "done", scan)
        self.assertEqual(scan["files"], 6)
        self.assertGreater(scan["size"], 0)
        status, data = self.client.json(
            "POST", "/api/download/prepare", {"scan_id": scan_id}
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(data["data"]["count"], 6)
        self.assertEqual(data["data"]["kind"], "zip")

    def test_05_auth_required(self):
        mock, thread = start_server(MockOpenList, require_auth=True)
        try:
            base = f"http://127.0.0.1:{mock.server_port}"
            status, data = self.client.json("POST", "/api/sites", {"url": base})
            self.assertEqual(status, 200, data)
            site_id = data["data"]["site"]["id"]
            status, data = self.client.json("POST", "/api/connect", {"site_id": site_id})
            self.assertEqual(status, 401, data)
            self.assertTrue(data["error"]["auth"])
            status, data = self.client.json(
                "POST",
                "/api/connect",
                {"site_id": site_id, "username": USERNAME, "password": "wrong"},
            )
            self.assertEqual(status, 401, data)
            status, data = self.client.json(
                "POST",
                "/api/connect",
                {"site_id": site_id, "username": USERNAME, "password": PASSWORD},
            )
            self.assertEqual(status, 200, data)
            status, data = self.client.json("POST", "/api/fs/list", {"path": "/"})
            self.assertEqual(status, 200, data)
        finally:
            stop_server(mock)

    def test_06_aria2_add_and_proxy(self):
        status, data = self.client.json(
            "POST", "/api/connect", {"site_id": self.site_id}
        )
        self.assertEqual(status, 200, data)
        mock, thread = start_server(MockAria2)
        MockAria2.tasks.clear()
        MockAria2.calls.clear()
        try:
            rpc = f"http://127.0.0.1:{mock.server_port}/jsonrpc"
            status, data = self.client.json(
                "POST",
                "/api/aria2/config",
                {"endpoint": rpc, "secret": "", "dir": str(self.tmp / "downloads")},
            )
            self.assertEqual(status, 200, data)
            self.assertTrue(data["data"]["aria2"]["available"], data)
            status, data = self.client.json(
                "POST",
                "/api/aria2/add",
                {
                    "files": [
                        {
                            "path": "/Docs/readme.txt",
                            "arcname": "Docs/readme.txt",
                            "size": 5000,
                        }
                    ],
                    "base_dir": str(self.tmp / "downloads"),
                    "direct": False,
                },
            )
            self.assertEqual(status, 200, data)
            self.assertEqual(data["data"]["added"], 1, data)
            add_call = next(c for c in MockAria2.calls if c["method"] == "aria2.addUri")
            url = add_call["params"][0][0]
            options = add_call["params"][1]
            self.assertIn("/api/aria2/stream/", url)
            self.assertTrue(options["dir"].endswith("Docs"))
            self.assertEqual(options["out"], "readme.txt")
            status, headers, body = self.client.request("GET", url.replace("http://127.0.0.1:" + str(self.server.server_port), ""))
            self.assertEqual(status, 200)
            self.assertEqual(len(body), 5000)
        finally:
            stop_server(mock)

    def test_07_keepalive_body_is_consumed(self):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            conn.request("GET", "/api/bootstrap")
            response = conn.getresponse()
            data = json.loads(response.read())
            site_id = data["data"]["sites"][0]["id"]
            cookie = None
            for key, value in response.getheaders():
                if key.lower() == "set-cookie":
                    cookie = value.split(";", 1)[0]
            headers = {"Content-Type": "application/json"}
            if cookie:
                headers["Cookie"] = cookie
            conn.request(
                "POST",
                "/api/connect",
                body=json.dumps({"site_id": site_id}),
                headers=headers,
            )
            response = conn.getresponse()
            response.read()
            for key, value in response.getheaders():
                if key.lower() == "set-cookie":
                    cookie = value.split(";", 1)[0]
            headers["Cookie"] = cookie
            conn.request(
                "POST",
                "/api/fs/list",
                body=json.dumps({"path": "/", "refresh": False}),
                headers=headers,
            )
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            conn.request("POST", "/api/scan/start", body="{}", headers=headers)
            response = conn.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 200, payload)
            self.assertIn("scan", payload["data"])
        finally:
            conn.close()

    def test_08_ticket_store_persists(self):
        path = self.tmp / "tickets-test.json"
        store = TicketStore(path=path)
        client = OpenListClient(self.mock_base, token="abc", insecure=False)
        token = store.create(
            link="http://example.com/file",
            headers={"User-Agent": "test"},
            filename="file.bin",
            client=client,
            size=3,
        )
        store.flush()
        loaded = TicketStore(path=path)
        ticket = loaded.get(token)
        self.assertIsNotNone(ticket)
        assert ticket is not None
        self.assertEqual(ticket.filename, "file.bin")
        self.assertEqual(ticket.client.base_url, client.base_url)
        self.assertEqual(ticket.client.token, "abc")

    def test_09_direct_browser_without_session(self):
        fresh = Client(self.app_base)
        url = self.mock_base + "/file?path=/Docs/readme.txt&sig=mock"
        status, data = fresh.json(
            "POST",
            "/api/direct/add",
            {
                "urls": [url],
                "engine": "browser",
                "headers": "X-Test: one\nUser-Agent: DirectTest/1.0",
            },
        )
        self.assertEqual(status, 200, data)
        download_url = data["data"]["url"]
        self.assertTrue(download_url.startswith("/api/direct/stream/"))
        status, headers, body = fresh.request("GET", download_url)
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 5000)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))

    def test_10_clear_all_and_direct_aria2(self):
        mock, thread = start_server(MockAria2)
        MockAria2.tasks.clear()
        MockAria2.calls.clear()
        try:
            status, data = self.client.json(
                "POST",
                "/api/aria2/config",
                {
                    "endpoint": f"http://127.0.0.1:{mock.server_port}/jsonrpc",
                    "secret": "",
                    "dir": str(self.tmp / "direct-downloads"),
                },
            )
            self.assertEqual(status, 200, data)
            url = self.mock_base + "/file?path=/Docs/readme.txt&sig=mock"
            status, data = self.client.json(
                "POST",
                "/api/direct/add",
                {
                    "urls": [url],
                    "engine": "aria2",
                    "headers": "X-Test: one",
                    "base_dir": str(self.tmp / "direct-downloads"),
                    "direct": True,
                },
            )
            self.assertEqual(status, 200, data)
            self.assertEqual(data["data"]["added"], 1, data)
            add_call = next(c for c in MockAria2.calls if c["method"] == "aria2.addUri")
            self.assertEqual(add_call["params"][0][0], url)
            self.assertIn("X-Test: one", add_call["params"][1].get("header", []))
            status, data = self.client.json(
                "POST", "/api/aria2/clear-all", {"stop_active": True}
            )
            self.assertEqual(status, 200, data)
            self.assertGreaterEqual(data["data"]["cleared"], 1)
            status, data = self.client.json("GET", "/api/aria2/tasks")
            self.assertEqual(data["data"]["tasks"], [])
        finally:
            stop_server(mock)

    def test_11_share_page_is_rejected(self):
        status, data = self.client.json(
            "POST",
            "/api/direct/add",
            {"urls": ["https://pan.baidu.com/s/1abc"], "engine": "browser"},
        )
        self.assertEqual(status, 400, data)
        self.assertIn("分享页", data["error"]["message"])

    def test_12_netdisk_site_is_rejected(self):
        status, data = self.client.json(
            "POST", "/api/sites", {"url": "https://pan.baidu.com/"}
        )
        self.assertEqual(status, 400, data)
        self.assertIn("百度网盘", data["error"]["message"])

    def test_13_yunx_status(self):
        status, data = self.client.json("GET", "/api/yunx/status")
        self.assertEqual(status, 200, data)
        self.assertIn("installed", data["data"])
        self.assertEqual(data["data"]["license"], "AGPL-3.0")
        self.assertIn("github.com", data["data"]["releaseUrl"])

    def test_14_turbo_settings(self):
        status, data = self.client.json(
            "POST",
            "/api/settings",
            {
                "downloadDir": str(self.tmp / "turbo"),
                "split": 32,
                "concurrency": 12,
                "direct": False,
                "engine": "aria2",
            },
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(data["data"]["aria2"]["split"], 32)
        self.assertEqual(data["data"]["aria2"]["concurrency"], 12)
        self.assertFalse(data["data"]["aria2"]["direct"])


if __name__ == "__main__":
    unittest.main(verbosity=2)