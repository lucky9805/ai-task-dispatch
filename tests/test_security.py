"""安全加固专项测试：密码哈希、令牌、限速、IP 白名单、认证、RBAC、审计、数据隔离。"""
import importlib.util
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as db_mod
import security
from auth_service import SecurityService
from errors import AuthError, PermissionDeniedError

_spec = importlib.util.spec_from_file_location("sec_server", ROOT / "server.py")
server = importlib.util.module_from_spec(_spec)
sys.modules["sec_server"] = server
_spec.loader.exec_module(server)


def make_env():
    tmp = tempfile.TemporaryDirectory()
    conn = db_mod.connect(Path(tmp.name) / "sec.db")
    db_mod.init_db(conn)
    lock = threading.RLock()

    @contextmanager
    def tx(immediate=False):
        with lock:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                if immediate:
                    conn.execute("COMMIT")
            except Exception:
                if immediate:
                    conn.execute("ROLLBACK")
                raise
    return tmp, conn, tx


class SecurityUtilTest(unittest.TestCase):
    def test_password_hash_and_verify(self):
        h = security.hash_password("StrongPass1!")
        self.assertTrue(h.startswith("bcrypt$") or h.startswith("scrypt$"))
        self.assertTrue(security.verify_password("StrongPass1!", h))
        self.assertFalse(security.verify_password("wrong", h))

    def test_password_strength(self):
        self.assertIsNotNone(security.validate_password_strength("short"))
        self.assertIsNotNone(security.validate_password_strength("abcdefgh"))  # 单一类型
        self.assertIsNone(security.validate_password_strength("Abcdefg1"))

    def test_pair_code_12(self):
        c = security.new_pair_code()
        self.assertEqual(c.count("-"), 2)
        self.assertEqual(len(c.replace("-", "")), 12)

    def test_token_hash_and_safe_equal(self):
        t = security.new_token()
        self.assertNotEqual(t, security.token_hash(t))
        self.assertTrue(security.safe_equal("a", "a"))
        self.assertFalse(security.safe_equal("a", "b"))

    def test_ip_allowlist(self):
        self.assertTrue(security.ip_allowed("1.2.3.4", None))
        self.assertTrue(security.ip_allowed("192.168.8.20", ["192.168.8.0/24"]))
        self.assertFalse(security.ip_allowed("10.0.0.1", ["192.168.8.0/24"]))

    def test_rate_limiter(self):
        r = security.RateLimiter()
        self.assertTrue(all(r.allow("k", 3, 60) for _ in range(3)))
        self.assertFalse(r.allow("k", 3, 60))

    def test_security_headers(self):
        h = security.security_headers("https")
        self.assertIn("Strict-Transport-Security", h)
        self.assertEqual(h["X-Frame-Options"], "DENY")


class AuthServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.conn, self.tx = make_env()
        self.sec = SecurityService(self.conn, self.tx)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_bootstrap_once(self):
        b1 = self.sec.bootstrap_admin()
        self.assertIsNotNone(b1)
        self.assertIsNone(self.sec.bootstrap_admin())  # 第二次不再创建

    def test_login_and_session(self):
        _, pw = self.sec.bootstrap_admin()
        res = self.sec.login("admin", pw, ip="1.2.3.4")
        self.assertTrue(res["token"])
        self.assertEqual(res["user"]["role"], "admin")
        u = self.sec.resolve_session(res["token"])
        self.assertEqual(u["username"], "admin")
        self.sec.logout(res["token"], actor="admin")
        self.assertIsNone(self.sec.resolve_session(res["token"]))

    def test_wrong_password_and_lock(self):
        self.sec.bootstrap_admin()
        for _ in range(5):
            with self.assertRaises(AuthError):
                self.sec.login("admin", "bad-password")
        with self.assertRaises(PermissionDeniedError):
            self.sec.login("admin", "bad-password")  # 锁定

    def test_rbac(self):
        admin = {"role": "admin"}
        op = {"role": "operator"}
        viewer = {"role": "viewer"}
        self.assertTrue(SecurityService.has_permission(admin, "user.manage"))
        self.assertFalse(SecurityService.has_permission(op, "user.manage"))
        self.assertTrue(SecurityService.has_permission(op, "task.create"))
        self.assertFalse(SecurityService.has_permission(viewer, "task.create"))

    def test_audit_recorded(self):
        self.sec.audit("user", "admin", "task.create", target="a")
        ev = self.sec.list_audit(10, None, None)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["action"], "task.create")

    def test_data_isolation(self):
        self.sec.bootstrap_admin()
        u = self.sec.create_user("bob", "BobPass1!", "operator", actor="admin")
        self.assertEqual(self.sec.owned_agent_names({"id": u["id"], "role": "operator"}), [])
        self.assertIsNone(self.sec.owned_agent_names({"id": 0, "role": "admin"}))


class HttpSecurityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dbpath = Path(cls.tmp.name) / "http.db"
        cls.TOKEN = "sec-http-token"
        old = (server.DB_PATH, server.TOKEN, server.ALLOW_ANON, server._conn)
        cls._old = old
        server.DB_PATH = cls.dbpath
        server.TOKEN = cls.TOKEN
        server.ALLOW_ANON = False
        server._conn = None
        server.init_db()
        cls.boot = server._security().bootstrap_admin()
        cls.httpd = server.DispatchServer(("127.0.0.1", 0), server.Handler)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close()
        server.DB_PATH, server.TOKEN, server.ALLOW_ANON, server._conn = cls._old
        with server._lock:
            if server._conn:
                server._conn.close(); server._conn = None
        cls.tmp.cleanup()

    def req(self, path, method="GET", data=None, headers=None):
        h = dict(headers or {}); body = None
        if data is not None:
            body = __import__("json").dumps(data).encode(); h["Content-Type"] = "application/json"
        r = urllib.request.Request(self.base + path, data=body, headers=h, method=method)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                raw = resp.read().decode()
                return resp.status, (__import__("json").loads(raw) if raw else {}), dict(resp.headers)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            return e.code, (__import__("json").loads(raw) if raw else {}), dict(e.headers)

    def test_anonymous_denied(self):
        self.assertEqual(self.req("/api/tasks")[0], 401)

    def test_wrong_token_denied(self):
        self.assertEqual(self.req("/api/tasks", headers={"X-Dispatch-Token": "bad"})[0], 401)

    def test_service_token_ok(self):
        self.assertEqual(self.req("/api/tasks", headers={"X-Dispatch-Token": self.TOKEN})[0], 200)

    def test_login_cookie_flags(self):
        s, j, h = self.req("/api/login", "POST",
                           {"username": self.boot[0], "password": self.boot[1]})
        self.assertEqual(s, 200)
        cookie = h.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_rbac_forbidden(self):
        _, _, h = self.req("/api/login", "POST",
                           {"username": self.boot[0], "password": self.boot[1]})
        admin_cookie = h.get("Set-Cookie", "").split(";")[0]
        self.req("/api/users", "POST",
                 {"username": "eve", "password": "EvePass1!", "role": "viewer"},
                 headers={"Cookie": admin_cookie})
        _, _, h2 = self.req("/api/login", "POST",
                            {"username": "eve", "password": "EvePass1!"})
        eve_cookie = h2.get("Set-Cookie", "").split(";")[0]
        self.assertEqual(self.req("/api/users", headers={"Cookie": eve_cookie})[0], 403)

    def test_security_headers(self):
        _, _, h = self.req("/api/health")
        self.assertEqual(h.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(h.get("X-Frame-Options"), "DENY")


if __name__ == "__main__":
    unittest.main()
