"""M3 Gate: 业务层 service，覆盖配对/领取/回传全部业务规则。"""
import importlib.util
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import errors  # noqa: E402  service.py 内部也 from errors import，须同一模块实例

import db as db  # noqa: E402
import service as service_mod  # noqa: E402


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        db.init_db(self.conn)

        @contextmanager
        def tx(immediate=False):
            if immediate:
                self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                if immediate:
                    self.conn.execute("COMMIT")
            except Exception:
                if immediate:
                    self.conn.execute("ROLLBACK")
                raise

        self.s = service_mod.Services(
            self.conn, tx, lease_seconds=2.0, online_window=30.0, pair_ttl=600.0)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # ---- 配对 ----
    def test_pairing_full_flow(self):
        code = self.s.pairing.create_code("a")
        self.assertRegex(code, r"^[A-Z2-9]{4}(-[A-Z2-9]{4})+$")  # 安全加固：8/12 位分段均可
        tok = self.s.pairing.redeem("a", code)
        self.assertTrue(tok)
        self.assertTrue(self.s.pairing.check_token("a", tok))
        self.assertFalse(self.s.pairing.check_token("a", "wrong"))
        self.assertFalse(self.s.pairing.check_token("ghost", tok))

    def test_pairing_code_rules(self):
        code = self.s.pairing.create_code("real")
        for name, c in (("fake", code), ("real", "NOPE-NOPE")):
            with self.assertRaises(errors.ValidationError):
                self.s.pairing.redeem(name, c)
        self.s.pairing.redeem("real", code)
        with self.assertRaises(errors.ValidationError):
            self.s.pairing.redeem("real", code)  # 复用

    def test_expired_code(self):
        code = self.s.pairing.create_code("e", ttl=-1)
        with self.assertRaises(errors.ValidationError):
            self.s.pairing.redeem("e", code)

    def test_revoke_kills_token(self):
        code = self.s.pairing.create_code("a")
        tok = self.s.pairing.redeem("a", code)
        self.assertTrue(self.s.pairing.revoke("a"))
        self.assertFalse(self.s.pairing.check_token("a", tok))
        self.assertFalse(self.s.pairing.revoke("ghost"))

    def test_empty_name(self):
        with self.assertRaises(errors.ValidationError):
            self.s.pairing.create_code("  ")

    # ---- 任务 ----
    def _paired(self, name):
        tok = self.s.pairing.redeem(name, self.s.pairing.create_code(name))
        return tok

    def test_create_task_and_assignee_rules(self):
        self._paired("a")
        t = self.s.tasks.create("标题", "a", {"command": "echo hi"})
        self.assertEqual(t["status"], "pending")
        with self.assertRaises(errors.ValidationError):
            self.s.tasks.create("x", "ghost", {})  # 未登记
        with self.assertRaises(errors.ValidationError):
            self.s.tasks.create("x", "", {})

    def test_claim_requires_pairing_and_target(self):
        tok_a = self._paired("a")
        tok_b = self._paired("b")
        self.s.tasks.create("t", "a", {})
        with self.assertRaises(errors.AuthError):
            self.s.tasks.claim("a", "bad-token")  # 凭证错
        self.assertIsNone(self.s.tasks.claim("b", tok_b))  # b 配对但没被指派
        self.assertEqual(self.s.tasks.claim("a", tok_a)["status"], "running")

    def test_claim_progress_finish_owner_check(self):
        tok_a = self._paired("a")
        tok_b = self._paired("b")
        t = self.s.tasks.create("t", "a", {})
        got = self.s.tasks.claim("a", tok_a)
        self.assertEqual(got["id"], t["id"])
        # 别人不能回写
        self.assertFalse(self.s.tasks.progress(t["id"], "b", tok_b, 50, "x"))
        self.assertFalse(self.s.tasks.finish(t["id"], "b", tok_b, True, "x"))
        # 本人可以
        self.assertTrue(self.s.tasks.progress(t["id"], "a", tok_a, 50, "half"))
        self.assertTrue(self.s.tasks.heartbeat(t["id"], "a", tok_a))
        self.assertTrue(self.s.tasks.finish(t["id"], "a", tok_a, True, "done"))
        self.assertEqual(self.s.tasks.get(t["id"])["status"], "succeeded")

    def test_lease_reclaim_keeps_assignee(self):
        tok = self._paired("a")
        t = self.s.tasks.create("t", "a", {})
        self.s.tasks.claim("a", tok)
        # 把 claimed_at 改成超时
        self.conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?", (time.time() - 100, t["id"]))
        self.s.tasks.list(None, None)  # 触发回收
        self.assertEqual(self.s.tasks.get(t["id"])["status"], "pending")
        self.assertEqual(self.s.tasks.get(t["id"])["assignee"], "a")

    def test_status_validation(self):
        with self.assertRaises(errors.ValidationError):
            self.s.tasks.list("bogus", None)

    # ---- Agent ----
    def test_agent_list_status(self):
        self.s.pairing.create_code("b")  # 只登记未配对
        self._paired("a")
        by = {x["name"]: x for x in self.s.agents.list()}
        self.assertTrue(by["a"]["paired"])
        self.assertFalse(by["b"]["paired"])
        self.assertFalse(by["b"]["revoked"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
