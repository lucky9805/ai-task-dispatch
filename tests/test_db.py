"""M2 Gate: db schema + 幂等迁移 + 旧库兼容。"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


db = _load("dispatch_db", "db.py")


class DbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_creates_tables(self):
        conn = db.connect(self.path)
        db.init_db(conn)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"tasks", "agents", "pairing_codes", "schema_meta"} <= names)
        self.assertEqual(db.schema_version(conn), db.SCHEMA_VERSION)
        conn.close()

    def test_init_idempotent(self):
        conn = db.connect(self.path)
        db.init_db(conn)
        db.init_db(conn)
        self.assertEqual(db.schema_version(conn), db.SCHEMA_VERSION)
        conn.close()

    def test_old_db_migration_keeps_data(self):
        """旧库（无 assignee / 无凭证列）升级后数据不丢，历史任务按 agent_id 回填。"""
        conn = sqlite3.connect(str(self.path))
        conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT, payload TEXT, "
                     "status TEXT, agent_id TEXT, created_at REAL, updated_at REAL)")
        conn.execute("INSERT INTO tasks VALUES (1,'老任务','{}','succeeded','worker-1',1,2)")
        conn.execute("CREATE TABLE agents (name TEXT PRIMARY KEY, first_seen REAL, last_seen REAL, "
                     "claimed_count INTEGER)")
        conn.execute("INSERT INTO agents VALUES ('worker-1',1,2,3)")
        conn.commit()
        conn.close()

        conn = db.connect(self.path)
        db.init_db(conn)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        self.assertIn("assignee", cols)
        self.assertEqual(
            conn.execute("SELECT assignee FROM tasks WHERE id=1").fetchone()[0], "worker-1")
        agent_cols = {r["name"] for r in conn.execute("PRAGMA table_info(agents)")}
        self.assertTrue({"token_hash", "paired_at", "revoked"} <= agent_cols)
        self.assertEqual(conn.execute("SELECT claimed_count FROM agents").fetchone()[0], 3)
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
