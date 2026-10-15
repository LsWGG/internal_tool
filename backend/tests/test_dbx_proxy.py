import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dbx_manager import DBXManager
from app.dbx_proxy import mount_dbx_proxy, rewrite_upstream_location


class _Upstream(BaseHTTPRequestHandler):
    def do_GET(self):
        self._reply()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self._reply()

    def _reply(self):
        if self.path.startswith("/dbx/redirect"):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/dbx/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps({"path": self.path, "cookie": self.headers.get("Cookie", "")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "dbx_session=abc; Path=/dbx; HttpOnly; SameSite=Lax")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class DBXProxyTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        manager = DBXManager("/tmp/dbx-proxy-test", port=self.server.server_address[1])
        app = FastAPI()
        mount_dbx_proxy(app, manager)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_login_cookie_stays_on_the_tool_origin(self):
        response = self.client.post(
            "/dbx/api/auth/login",
            json={"password": "secret"},
            headers={"cookie": "dbx_session=old"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("dbx_session=abc", response.headers["set-cookie"])
        self.assertIn("Path=/dbx", response.headers["set-cookie"])
        self.assertEqual(response.json()["path"], "/dbx/api/auth/login")
        self.assertIn("dbx_session=old", response.json()["cookie"])

    def test_directory_request_keeps_the_trailing_slash(self):
        response = self.client.get("/dbx/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["path"], "/dbx/")

    def test_absolute_redirect_is_rewritten_to_the_embed_path(self):
        response = self.client.get("/dbx/redirect", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/dbx/login")

    def test_rewrite_upstream_location(self):
        self.assertEqual(rewrite_upstream_location("http://127.0.0.1:4224/dbx/", "http://127.0.0.1:4224"), "/dbx/")
        self.assertEqual(rewrite_upstream_location("/dbx/login", "http://127.0.0.1:4224"), "/dbx/login")
