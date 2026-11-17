"""功能自检 Gate：验证自检本身覆盖全部功能点且真实通过 + HTTP 端点可用。"""
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


selftest = _load("st_selftest", "selftest.py")
server = _load("st_server", "server.py")


class SelftestUnitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = selftest.run_all()

    def test_all_passed(self):
        r = self.result
        self.assertTrue(r["passed"], "自检存在失败项：%s" % [
            i["name"] for i in r["items"] if not i["passed"]])
        self.assertEqual(r["ok"], r["total"])
        self.assertEqual(r["failed"], 0)

    def test_covers_all_feature_areas(self):
        """覆盖配对/下发/领取/回传/撤销/并发/迁移等关键功能域。"""
        names = " ".join(i["name"] for i in self.result["items"])
        for keyword in ("配对", "下发", "领取", "回传", "撤销", "并发", "迁移", "租约"):
            self.assertIn(keyword, names, "自检未覆盖功能域：%s" % keyword)
        self.assertGreaterEqual(self.result["total"], 15)

    def test_each_item_has_structure(self):
        for it in self.result["items"]:
            self.assertIn("name", it)
            self.assertIn("passed", it)
            self.assertIn("detail", it)
            self.assertIsInstance(it["passed"], bool)
            self.assertTrue(it["detail"])

    def test_isolation_no_leak(self):
        """连跑两次都全过：独立沙箱不互相污染。"""
        self.assertTrue(selftest.run_all()["passed"])


class SelftestEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        server.DB_PATH = Path(cls.tmp.name) / "ep.db"
        server.TOKEN = "adm"
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

    def _get(self, path, token="adm"):
        req = urllib.request.Request(self.base + path)
        if token:
            req.add_header("X-Dispatch-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_selftest_endpoint(self):
        status, obj = self._get("/api/selftest")
        self.assertEqual(status, 200)
        self.assertTrue(obj["passed"])
        self.assertGreaterEqual(obj["total"], 15)
        self.assertEqual(len(obj["items"]), obj["total"])

    def test_selftest_endpoint_requires_admin_token(self):
        status, obj = self._get("/api/selftest", token="wrong")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
