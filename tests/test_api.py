"""M4 Gate: 控制器层——错误结构规范化、request_id、端点行为不变。"""
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("api_server", ROOT / "server.py")
server = importlib.util.module_from_spec(_spec)
sys.modules["api_server"] = server
_spec.loader.exec_module(server)

TOKEN = "api-token"


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        server.DB_PATH = Path(cls.tmp.name) / "api.db"
        server.TOKEN = TOKEN
        server.init_db()
        cls.httpd = server.DispatchServer(("127.0.0.1", 0), server.Handler)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        with server._lock:
            if server._conn is not None:
                server._conn.close()
                server._conn = None
        cls.tmp.cleanup()

    def req(self, path, payload=None, token=TOKEN, agent_token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Dispatch-Token"] = token
        if agent_token:
            headers["X-Agent-Token"] = agent_token
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        r = urllib.request.Request(self.base + path, data=data, headers=headers,
                                   method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def test_error_body_has_code(self):
        status, body = self.req("/api/tasks", {"title": "x", "assignee": " ", "payload": {}})
        self.assertEqual(status, 400)
        self.assertIn("code", body)
        self.assertEqual(body["code"], "invalid_argument")
        status, body = self.req("/api/tasks", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "unauthorized")

    def test_health_and_all_endpoints(self):
        self.assertEqual(self.req("/api/health")[0], 200)
        self.assertEqual(self.req("/api/agents")[0], 200)
        self.assertEqual(self.req("/api/tasks")[0], 200)
        # 配对 → 下发 → 领取 全链路仍通
        code = server.create_pairing_code("api-a")
        self.assertEqual(self.req("/api/pair", {"name": "api-a", "code": code})[0], 200)
        _, obj = self.req("/api/agents/pair", {"name": "api-b"})
        self.assertGreaterEqual(obj["code"].count("-"), 1)  # 安全加固后为 12 位 3 段
        self.assertEqual(self.req("/api/tasks", {
            "title": "T", "assignee": "api-a", "payload": {"command": "echo x"}})[0], 201)
        self.assertEqual(self.req("/api/agents/revoke", {"name": "api-b"})[0], 200)

    def test_structured_log_has_request_id(self):
        buf = StringIO()
        with redirect_stdout(buf):
            self.req("/api/health")
        lines = [l for l in buf.getvalue().splitlines() if l.strip().startswith("{")]
        self.assertTrue(lines)
        rec = json.loads(lines[-1])
        self.assertEqual(rec["event"], "http_access")
        self.assertTrue(rec["request_id"])
        self.assertIn("ts", rec)

    def test_unknown_path_404_with_code(self):
        status, body = self.req("/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_found")


if __name__ == "__main__":
    unittest.main(verbosity=2)
