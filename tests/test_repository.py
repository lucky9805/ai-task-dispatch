"""M2 Gate: 三个仓储的数据访问正确性。"""
import importlib.util
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


db = _load("dispatch_db2", "db.py")
repo_mod = _load("dispatch_repo", "repository.py")


class RepoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        db.init_db(self.conn)
        self.agents = repo_mod.AgentRepo(self.conn)
        self.tasks = repo_mod.TaskRepo(self.conn)
        self.pairing = repo_mod.PairingRepo(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_agent_register_touch_revoke(self):
        self.agents.register("a", 100.0)
        self.assertIsNotNone(self.agents.get("a"))
        self.agents.touch("a", 200.0)
        self.assertEqual(self.agents.get("a")["last_seen"], 200.0)
        self.agents.touch("new", 201.0)  # 不存在则登记
        self.assertIsNotNone(self.agents.get("new"))
        self.agents.set_token("a", "hash", 300.0)
        a = self.agents.get("a")
        self.assertEqual(a["token_hash"], "hash")
        self.assertFalse(a["revoked"])
        self.assertTrue(self.agents.revoke("a"))
        self.assertIsNone(self.agents.get("a")["token_hash"])
        self.assertFalse(self.agents.revoke("ghost"))

    def test_pairing_insert_and_mark(self):
        self.pairing.insert("H", "a", 999.0, 1.0)
        row = self.pairing.get_by_hash("H")
        self.assertEqual(row["name"], "a")
        self.assertFalse(row["used"])
        self.pairing.mark_used("H")
        self.assertTrue(self.pairing.get_by_hash("H")["used"])

    def test_task_crud_and_state(self):
        now = time.time()
        t = self.tasks.insert("标题", "a", {"command": "echo hi"}, now)
        self.assertEqual(t["status"], "pending")
        self.assertEqual(t["payload"]["command"], "echo hi")
        self.assertIsNone(self.tasks.claim_candidate("b"))  # 别人领不到
        row = self.tasks.claim_candidate("a")
        self.assertEqual(row["id"], t["id"])
        self.tasks.mark_running(t["id"], "a", now)
        self.assertTrue(self.tasks.owned(t["id"], "a"))
        self.assertFalse(self.tasks.owned(t["id"], "b"))
        self.tasks.set_progress(t["id"], 50, "half", now)
        self.assertEqual(self.tasks.get(t["id"])["progress"], 50)
        self.tasks.finish(t["id"], True, "done", now)
        self.assertEqual(self.tasks.get(t["id"])["status"], "succeeded")

    def test_list_filters_and_reclaim(self):
        now = time.time()
        self.tasks.insert("A", "a", {}, now)
        self.tasks.insert("B", "b", {}, now)
        self.assertEqual(len(self.tasks.list(None, None)), 2)
        self.assertEqual(len(self.tasks.list("pending", None)), 2)
        self.assertEqual(len(self.tasks.list(None, "a")), 1)
        # 造一个超时 running 任务（复合 key 需完整字段，用 insert 再改）
        t = self.tasks.insert("C", "c", {}, now)
        self.conn.execute("UPDATE tasks SET status='running', claimed_at=? WHERE id=?",
                          (now - 200, t["id"]))
        n = self.tasks.reclaim_expired(now, 120)
        self.assertEqual(n, 1)
        self.assertEqual(self.tasks.get(t["id"])["status"], "pending")

    def test_agents_inc_claimed(self):
        self.agents.register("a", 1.0)
        self.agents.inc_claimed("a", 2.0)
        self.assertEqual(self.agents.get("a")["claimed_count"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
