"""配对 / 定向指派 / 领取 / 执行 / 回传 的端到端测试（标准库 unittest）。

运行：cd task-dispatch && python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("dispatch_server", ROOT / "server.py")
server = importlib.util.module_from_spec(_spec)
sys.modules["dispatch_server"] = server
_spec.loader.exec_module(server)

TOKEN = "test-token"


class DispatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        server.DB_PATH = Path(cls.tmp.name) / "test.db"
        server.TOKEN = TOKEN
        server.LEASE_SECONDS = 2.0
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

    def setUp(self):
        with server.tx(immediate=True) as c:
            c.execute("DELETE FROM tasks")
            c.execute("DELETE FROM agents")
            c.execute("DELETE FROM pairing_codes")
        self.tokens = {}

    # ---- helpers ----

    def req(self, path, payload=None, token=TOKEN, agent_token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Dispatch-Token"] = token
        if agent_token:
            headers["X-Agent-Token"] = agent_token
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers,
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")

    def register(self, name):
        """服务端登记 Agent（生成配对码，但不兑换 → 待配对状态）。"""
        return server.create_pairing_code(name)

    def token(self, name):
        """给 Agent 完成配对并缓存凭证，避免重复兑换覆盖 token。"""
        if name not in self.tokens:
            self.tokens[name] = server.redeem_pairing_code(name, self.register(name))
        return self.tokens[name]

    def new_task(self, title="任务", command="echo hi", assignee="agent-1"):
        self.register(assignee)
        status, obj = self.req("/api/tasks", {
            "title": title, "assignee": assignee, "payload": {"command": command},
        })
        self.assertEqual(status, 201, obj)
        return obj["task"]

    def claim(self, name, token=None):
        return self.req(
            "/api/tasks/claim", {"agent_id": name},
            agent_token=token if token is not None else self.token(name),
        )

    def task_post(self, tid, action, agent_id, extra=None, agent_token=None):
        payload = {"agent_id": agent_id}
        if extra:
            payload.update(extra)
        return self.req(
            "/api/tasks/%d/%s" % (tid, action), payload,
            agent_token=agent_token if agent_token is not None else self.token(agent_id),
        )

    def write_cred(self, name):
        path = Path(self.tmp.name) / ("%s.cred.json" % name)
        path.write_text(json.dumps({
            "agent_id": name, "token": self.token(name),
            "server": self.base, "paired_at": time.time(),
        }), encoding="utf-8")
        return path

    def agent_cmd(self, name, once=False):
        cmd = [
            sys.executable, str(ROOT / "agent.py"), "start",
            "--cred-file", str(self.write_cred(name)),
            "--interval", "0.2", "--timeout", "10",
        ]
        if once:
            cmd.append("--once")
        return cmd

    # ---- 基础与指派 ----

    def test_health_and_auth(self):
        self.assertEqual(self.req("/api/health")[0], 200)
        self.assertEqual(self.req("/api/tasks", token=None)[0], 401)
        self.assertEqual(self.req("/api/tasks", token="wrong")[0], 401)
        self.assertEqual(self.req("/api/tasks")[0], 200)
        self.assertEqual(self.req("/api/agents")[0], 200)

    def test_create_requires_assignee(self):
        status, obj = self.req("/api/tasks", {"title": "没指定人", "payload": {}})
        self.assertEqual(status, 400)
        self.assertIn("assignee", obj["error"])
        self.assertEqual(self.req("/api/tasks", {"title": "空白", "assignee": "  ", "payload": {}})[0], 400)
        self.assertEqual(self.req("/api/tasks")[1]["tasks"], [])

    def test_create_rejects_unregistered_agent(self):
        """只能指派给服务端登记过的 Agent。"""
        status, obj = self.req("/api/tasks", {
            "title": "随便派", "assignee": "nobody", "payload": {},
        })
        self.assertEqual(status, 400)
        self.assertIn("未在服务端登记", obj["error"])

    def test_create_and_list(self):
        a = self.new_task("A", "echo a", assignee="a")
        b = self.new_task("B", "echo b", assignee="b")
        status, obj = self.req("/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual([t["id"] for t in obj["tasks"]], [a["id"], b["id"]])
        self.assertEqual([t["assignee"] for t in obj["tasks"]], ["a", "b"])
        self.assertEqual(obj["tasks"][0]["payload"]["command"], "echo a")
        self.assertEqual(len(self.req("/api/tasks?assignee=a")[1]["tasks"]), 1)
        self.assertEqual(self.req("/api/tasks?status=nonsense")[0], 400)

    # ---- 配对与凭证 ----

    def test_unpaired_or_wrong_token_cannot_claim(self):
        """核心安全约束：没配对 / 凭证不对，一律拿不到任务。"""
        self.new_task("给 a", assignee="a")
        self.assertEqual(self.req("/api/tasks/claim", {"agent_id": "a"})[0], 401)
        self.assertEqual(
            self.req("/api/tasks/claim", {"agent_id": "a"}, agent_token="forged")[0], 401
        )
        self.assertEqual(
            self.req("/api/tasks/claim", {"agent_id": "a"},
                     agent_token=self.token("other"))[0], 401
        )
        # 只有正确配对的 a 能领
        self.assertEqual(self.claim("a")[1]["task"]["status"], "running")

    def test_pairing_code_rules(self):
        """配对码：错码 / 名字不符 / 一次性 / 过期都必须拒绝。"""
        self.assertEqual(
            self.req("/api/pair", {"name": "x", "code": "NOPE-NOPE"})[0], 400
        )
        code = self.register("real")
        status, obj = self.req("/api/pair", {"name": "fake", "code": code})
        self.assertEqual(status, 400)
        self.assertIn("不匹配", obj["error"])

        code2 = self.register("once")
        status, obj = self.req("/api/pair", {"name": "once", "code": code2})
        self.assertEqual(status, 200)
        self.assertTrue(obj["token"])
        self.assertEqual(self.req("/api/pair", {"name": "once", "code": code2})[0], 400)

        expired = self.register("exp")
        with server.tx(immediate=True) as c:
            c.execute("UPDATE pairing_codes SET expires_at=? WHERE name='exp'",
                      (time.time() - 1,))
        self.assertEqual(self.req("/api/pair", {"name": "exp", "code": expired})[0], 400)

        # 服务端只存哈希，不存凭证明文
        with server.tx() as c:
            row = c.execute("SELECT token_hash FROM agents WHERE name='once'").fetchone()
        self.assertNotEqual(row["token_hash"], obj["token"])

    def test_revoke_denies_immediately(self):
        tok = self.token("a")
        self.new_task("给 a", assignee="a")
        self.assertTrue(server.revoke_agent("a"))
        status, obj = self.req("/api/tasks/claim", {"agent_id": "a"}, agent_token=tok)
        self.assertEqual(status, 401)

    def test_pair_and_revoke_http_endpoints(self):
        """页面「配对向导 / Agent 管理」依赖的两个管理 HTTP 端点。"""
        # 管理面鉴权：无 token 不能生成配对码
        self.assertEqual(self.req("/api/agents/pair", {"name": "web-a"}, token=None)[0], 401)
        # 空名字拒绝
        self.assertEqual(self.req("/api/agents/pair", {"name": "  "})[0], 400)
        # 生成配对码：201，返回明文 code 和 agent_id
        status, obj = self.req("/api/agents/pair", {"name": "web-a"})
        self.assertEqual(status, 201, obj)
        code = obj["code"]
        self.assertEqual(obj["agent_id"], "web-a")
        self.assertRegex(code, r"^[A-Z2-9]{4}(-[A-Z2-9]{4})+$")  # 安全加固：8/12 位分段均可
        # 该码可用于客户端兑换，说明真的登记了
        status, redeemed = self.req("/api/pair", {"name": "web-a", "code": code})
        self.assertEqual(status, 200, redeemed)
        # Agent 管理列表能看到它已配对
        by_name = {a["name"]: a for a in self.req("/api/agents")[1]["agents"]}
        self.assertTrue(by_name["web-a"]["paired"])
        # 撤销端点：无 token 拒绝；有效 token 立即撤销
        self.assertEqual(self.req("/api/agents/revoke", {"name": "web-a"}, token=None)[0], 401)
        self.assertEqual(self.req("/api/agents/revoke", {"name": "web-a"})[0], 200)
        self.assertEqual(
            self.req("/api/tasks/claim", {"agent_id": "web-a"},
                     agent_token=redeemed["token"])[0], 401
        )
        # 撤销不存在的 Agent 返回 404
        self.assertEqual(self.req("/api/agents/revoke", {"name": "ghost"})[0], 404)

    def test_agents_list_shows_pair_status(self):
        self.new_task("给 a", assignee="a")
        self.claim("a")
        self.register("b")  # 只登记，未配对
        by_name = {a["name"]: a for a in self.req("/api/agents")[1]["agents"]}
        self.assertTrue(by_name["a"]["paired"])
        self.assertTrue(by_name["a"]["online"])
        self.assertFalse(by_name["b"]["paired"])
        self.assertFalse(by_name["b"]["online"])
        self.assertEqual(by_name["a"]["claimed_count"], 1)

    # ---- 定向领取 ----

    def test_only_assigned_agent_can_claim(self):
        t1 = self.new_task("给 a", assignee="a")
        t2 = self.new_task("给 b", assignee="b")
        t3 = self.new_task("给 c", assignee="c")

        self.assertIsNone(self.claim("stranger")[1]["task"])
        self.assertEqual(self.claim("a")[1]["task"]["id"], t1["id"])
        self.assertIsNone(self.claim("a")[1]["task"], "a 不该拿到 b/c 的任务")
        self.assertEqual(self.claim("b")[1]["task"]["id"], t2["id"])
        self.assertEqual(self.claim("c")[1]["task"]["id"], t3["id"])

        tasks = {t["id"]: t for t in self.req("/api/tasks")[1]["tasks"]}
        for tid, owner in ((t1["id"], "a"), (t2["id"], "b"), (t3["id"], "c")):
            self.assertEqual(tasks[tid]["assignee"], owner)
            self.assertEqual(tasks[tid]["agent_id"], owner)

    def test_claim_exclusive_under_concurrency(self):
        """同名 Agent 起 10 个实例抢指派给它的 6 个任务：不重复、不丢。"""
        tok = self.token("pool")
        ids = [self.new_task("task-%d" % i, assignee="pool")["id"] for i in range(6)]
        claimed, errors, lock = [], [], threading.Lock()

        def worker(_):
            try:
                status, obj = self.req(
                    "/api/tasks/claim", {"agent_id": "pool"}, agent_token=tok
                )
                with lock:
                    if status == 200 and obj.get("task"):
                        claimed.append(obj["task"]["id"])
                    elif status != 200:
                        errors.append(status)
            except Exception as e:  # pragma: no cover
                with lock:
                    errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(claimed), sorted(ids))
        self.assertEqual(len(set(claimed)), 6, "同一任务被重复领取")
        self.assertIsNone(self.claim("pool")[1]["task"])

    def test_claim_requires_agent_id(self):
        self.new_task()
        self.assertEqual(
            self.req("/api/tasks/claim", {"agent_id": ""}, agent_token="x")[0], 400
        )

    # ---- 回传 ----

    def test_progress_heartbeat_complete_fail_and_owner_check(self):
        self.new_task("正常任务", assignee="a1")
        status, obj = self.claim("a1")
        self.assertEqual(status, 200)
        tid = obj["task"]["id"]

        self.assertEqual(
            self.task_post(tid, "complete", "other", {"result": "x"},
                           agent_token=self.token("a1"))[0], 409
        )
        self.assertEqual(
            self.task_post(tid, "complete", "a1", {"result": "x"},
                           agent_token="forged")[0], 409
        )
        self.assertEqual(
            self.task_post(tid, "progress", "a1",
                           {"progress": 60, "message": "跑到一半"})[0], 200
        )
        self.assertEqual(self.task_post(tid, "heartbeat", "a1")[0], 200)
        task = self.req("/api/tasks/%d" % tid)[1]["task"]
        self.assertEqual(task["progress"], 60)

        self.assertEqual(self.task_post(tid, "complete", "a1", {"result": "done"})[0], 200)
        task = self.req("/api/tasks/%d" % tid)[1]["task"]
        self.assertEqual(task["status"], "succeeded")
        self.assertEqual(task["result"], "done")
        self.assertEqual(self.task_post(tid, "complete", "a1", {"result": "again"})[0], 409)

        self.new_task("失败任务", "exit 3", assignee="a2")
        tid2 = self.claim("a2")[1]["task"]["id"]
        self.assertEqual(self.task_post(tid2, "fail", "a2", {"error": "boom"})[0], 200)
        self.assertEqual(self.req("/api/tasks/%d" % tid2)[1]["task"]["status"], "failed")

    def test_lease_reclaim_keeps_assignee(self):
        t = self.new_task("掉线任务", assignee="gone")
        self.claim("gone")
        time.sleep(2.3)
        self.assertIsNone(self.claim("fresh")[1]["task"])
        status, obj = self.claim("gone")
        self.assertEqual(status, 200)
        self.assertEqual(obj["task"]["id"], t["id"])
        self.assertEqual(obj["task"]["assignee"], "gone")

    # ---- 真 Agent 端 ----

    def test_agent_end_to_end(self):
        t = self.new_task("真执行", "echo hello-from-agent", assignee="e2e-agent")
        r = subprocess.run(self.agent_cmd("e2e-agent", once=True),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        task = self.req("/api/tasks/%d" % t["id"])[1]["task"]
        self.assertEqual(task["status"], "succeeded", r.stdout + r.stderr)
        self.assertIn("hello-from-agent", task["result"])
        self.assertEqual(task["agent_id"], "e2e-agent")
        self.assertIn("领到指派给我的任务", r.stdout)

    def test_unpaired_agent_cli_refuses_to_start(self):
        empty = Path(self.tmp.name) / "missing.cred.json"
        r = subprocess.run(
            [sys.executable, str(ROOT / "agent.py"), "start", "--cred-file", str(empty)],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("尚未配对", r.stdout)

    def test_wrong_agent_never_executes_task(self):
        t = self.new_task("只给 right", "echo should-not-run", assignee="right")
        r = subprocess.run(self.agent_cmd("wrong", once=True),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("暂无指派给我的任务", r.stdout)
        task = self.req("/api/tasks/%d" % t["id"])[1]["task"]
        self.assertEqual(task["status"], "pending")
        self.assertIsNone(task["agent_id"])

    def test_agent_reports_failure(self):
        t = self.new_task("失败命令", "exit 7", assignee="fail-agent")
        r = subprocess.run(self.agent_cmd("fail-agent", once=True),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        task = self.req("/api/tasks/%d" % t["id"])[1]["task"]
        self.assertEqual(task["status"], "failed", r.stdout + r.stderr)
        self.assertIn("exit=7", task["error"])

    def test_multiple_agents_parallel(self):
        for i in range(4):
            self.new_task("并行-%d" % i, "echo out-%d" % i,
                          assignee="worker-%d" % (i % 2))
        procs = [
            subprocess.Popen(self.agent_cmd("worker-%d" % i),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for i in range(2)
        ]
        try:
            deadline = time.time() + 20
            while time.time() < deadline:
                tasks = self.req("/api/tasks")[1]["tasks"]
                if len(tasks) == 4 and all(t["status"] == "succeeded" for t in tasks):
                    break
                time.sleep(0.3)
            tasks = self.req("/api/tasks")[1]["tasks"]
            for t in tasks:
                self.assertEqual(t["status"], "succeeded", t)
                self.assertIn("out-", t["result"] or "")
                self.assertEqual(t["agent_id"], t["assignee"], "任务被非指定 Agent 执行了")
            self.assertEqual(len({t["id"] for t in tasks}), 4)
        finally:
            for p in procs:
                p.terminate()
                try:
                    p.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.communicate()

    def test_cli_pair_end_to_end(self):
        """真 CLI 闭环：ctl.py 生成配对码 → agent.py pair 换凭证 → start 执行 → 回传。"""
        import os
        import re

        env = os.environ.copy()
        env["DISPATCH_DB"] = str(server.DB_PATH)
        r = subprocess.run(
            [sys.executable, str(ROOT / "ctl.py"), "pair", "cli-agent"],
            capture_output=True, text=True, timeout=20, env=env,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        match = re.search(r"配对码:\s*([A-Z0-9-]+)", r.stdout)
        self.assertIsNotNone(match, r.stdout)
        code = match.group(1)

        cred = Path(self.tmp.name) / "cli.cred.json"
        p = subprocess.run(
            [sys.executable, str(ROOT / "agent.py"), "pair",
             "--server", self.base, "--name", "cli-agent",
             "--code", code, "--cred-file", str(cred)],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("配对成功", p.stdout)
        self.assertTrue(cred.exists())
        self.assertEqual(cred.stat().st_mode & 0o777, 0o600)

        st = subprocess.run(
            [sys.executable, str(ROOT / "agent.py"), "status", "--cred-file", str(cred)],
            capture_output=True, text=True, timeout=20,
        )
        self.assertIn("cli-agent", st.stdout)

        t = self.new_task("CLI 闭环", "echo cli-ok", assignee="cli-agent")
        s = subprocess.run(
            [sys.executable, str(ROOT / "agent.py"), "start",
             "--cred-file", str(cred), "--once"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(s.returncode, 0, s.stdout + s.stderr)
        task = self.req("/api/tasks/%d" % t["id"])[1]["task"]
        self.assertEqual(task["status"], "succeeded", s.stdout + s.stderr)
        self.assertIn("cli-ok", task["result"])
        self.assertEqual(task["agent_id"], "cli-agent")
        self.assertEqual(self.req("/api/pair", {"name": "cli-agent", "code": code})[0], 400)

    # ---- 迁移与页面 ----

    def test_old_db_migration(self):
        """旧库启动时自动补 tasks.assignee、agents 凭证字段并回填历史任务。"""
        import sqlite3

        old_db = Path(self.tmp.name) / "old.db"
        con = sqlite3.connect(str(old_db))
        con.executescript("""
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                payload TEXT, status TEXT, progress INTEGER DEFAULT 0,
                message TEXT DEFAULT '', agent_id TEXT, result TEXT, error TEXT,
                claimed_at REAL, finished_at REAL, created_at REAL, updated_at REAL
            );
            CREATE INDEX idx_tasks_status ON tasks(status, id);
            CREATE TABLE agents (
                name TEXT PRIMARY KEY, first_seen REAL NOT NULL,
                last_seen REAL NOT NULL, claimed_count INTEGER NOT NULL DEFAULT 0
            );
        """)
        con.execute(
            "INSERT INTO tasks (title,payload,status,agent_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)", ("历史任务", "{}", "succeeded", "old-agent", 1.0, 1.0),
        )
        con.execute("INSERT INTO agents (name,first_seen,last_seen,claimed_count) "
                    "VALUES ('legacy',1.0,1.0,0)")
        con.commit()
        con.close()

        saved_db = server.DB_PATH
        with server._lock:
            if server._conn is not None:
                server._conn.close()
                server._conn = None
        server.DB_PATH = old_db
        try:
            server.init_db()
            con = sqlite3.connect(str(old_db))
            self.assertIn("assignee", [r[1] for r in con.execute("PRAGMA table_info(tasks)")])
            self.assertEqual(
                con.execute("SELECT assignee FROM tasks WHERE id=1").fetchone()[0], "old-agent"
            )
            agent_cols = [r[1] for r in con.execute("PRAGMA table_info(agents)")]
            for col in ("token_hash", "paired_at", "revoked"):
                self.assertIn(col, agent_cols)
            self.assertIsNone(
                con.execute("SELECT token_hash FROM agents WHERE name='legacy'").fetchone()[0]
            )
            self.assertIn("idx_tasks_assignee",
                          [r[1] for r in con.execute("PRAGMA index_list(tasks)")])
            con.close()
        finally:
            with server._lock:
                if server._conn is not None:
                    server._conn.close()
                    server._conn = None
            server.DB_PATH = saved_db
            server.init_db()

    def test_static_page(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("任务下发与 Agent 协作中心", html)
        # 三大功能区都在
        self.assertIn("Agent 管理中心", html)
        self.assertIn("如何配对", html)
        self.assertIn("下发任务", html)
        self.assertIn("任务看板", html)
        # 页面能操作配对/撤销/下发
        self.assertIn("/api/agents/pair", html)
        self.assertIn("/api/agents/revoke", html)
        self.assertIn("/api/tasks", html)
        self.assertIn("指派给", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
