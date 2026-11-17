"""Agent 分类与归档专项测试：schema v4 迁移、归档停活、分类、API 端点。"""
import importlib.util
import json
import sqlite3
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

# 直接 import：确保异常/安全模块与被测 service 是同一个模块对象（避免身份不一致）
import errors as errors_mod
import security as security_mod


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


db_mod = _load("arch_db", "db.py")
service_mod = _load("arch_service", "service.py")
repository_mod = _load("arch_repository", "repository.py")


def make_svc(path):
    conn = db_mod.connect(path)
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

    svc = service_mod.Services(conn, tx, lease_seconds=120, online_window=30, pair_ttl=600)
    return conn, svc, tx


class ArchiveServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn, self.svc, self.tx = make_svc(Path(self.tmp.name) / "t.db")
        # 建一个已配对 agent
        self.svc.pairing.create_code("bot1")
        with self.tx(immediate=True) as c:
            repository_mod.AgentRepo(c).set_token(
                "bot1", security_mod.token_hash("tok123"), 1)

    def tearDown(self):
        self.tmp.cleanup()

    def _agent(self, name="bot1"):
        return next(a for a in self.svc.agents.list() if a["name"] == name)

    def test_schema_v4_fields(self):
        self.assertEqual(db_mod.schema_version(self.conn), 4)
        a = self._agent()
        self.assertFalse(a["archived"])
        self.assertEqual(a["category"], "")
        self.assertIsNone(a["archived_at"])

    def test_archive_and_restore(self):
        r = self.svc.agents.set_archived("bot1", True)
        self.assertTrue(r["archived"])
        self.assertIsNotNone(r["archived_at"])
        self.assertTrue(self._agent()["archived"])
        r = self.svc.agents.set_archived("bot1", False)
        self.assertFalse(r["archived"])
        self.assertIsNone(r["archived_at"])
        self.assertFalse(self._agent()["archived"])

    def test_archived_agent_cannot_claim(self):
        # 先下一单
        self.svc.tasks.create("t", "bot1", {"command": "echo hi"})
        self.assertIsNotNone(self.svc.tasks.claim("bot1", "tok123"))  # 归档前可领
        # 归档后再下一单，应拒绝领取
        self.svc.tasks.create("t2", "bot1", {"command": "echo hi2"})
        self.svc.agents.set_archived("bot1", True)
        with self.assertRaises(errors_mod.AuthError):
            self.svc.tasks.claim("bot1", "tok123")

    def test_category_set_and_clear(self):
        r = self.svc.agents.set_category("bot1", "生产环境")
        self.assertEqual(r["category"], "生产环境")
        self.assertEqual(self._agent()["category"], "生产环境")
        self.assertIn("生产环境", self.svc.agents.list_categories())
        r = self.svc.agents.set_category("bot1", "")
        self.assertEqual(r["category"], "")
        self.assertNotIn("生产环境", self.svc.agents.list_categories())

    def test_archive_unknown_agent_raises(self):
        with self.assertRaises(errors_mod.ValidationError):
            self.svc.agents.set_archived("nope", True)
        with self.assertRaises(errors_mod.ValidationError):
            self.svc.agents.set_category("nope", "x")


class ArchiveMigrationTest(unittest.TestCase):
    def test_v3_to_v4_keeps_data(self):
        tmp = tempfile.TemporaryDirectory()
        p = Path(tmp.name) / "old.db"
        c = sqlite3.connect(str(p))
        c.executescript("""
        CREATE TABLE agents (name TEXT PRIMARY KEY, token_hash TEXT, paired_at REAL,
          revoked INTEGER DEFAULT 0, first_seen REAL, last_seen REAL, claimed_count INTEGER DEFAULT 0,
          owner_user_id INTEGER, approval TEXT DEFAULT 'approved', allowed_ips TEXT DEFAULT '',
          token_issued_at REAL, token_expires_at REAL, paired_ip TEXT DEFAULT '', paired_by TEXT DEFAULT '');
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO schema_meta VALUES ('version','3');
        INSERT INTO agents (name,token_hash,first_seen,last_seen) VALUES ('legacy','abc',1,2);
        """)
        c.commit()
        c.close()
        c = db_mod.connect(p)
        db_mod.init_db(c)
        self.assertEqual(db_mod.schema_version(c), 4)
        a = c.execute("SELECT * FROM agents WHERE name='legacy'").fetchone()
        self.assertEqual(a["token_hash"], "abc")
        self.assertEqual(a["archived"], 0)
        self.assertEqual(a["category"], "")
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
