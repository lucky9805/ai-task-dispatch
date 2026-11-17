"""M5 Gate: CLI 与页面在分层后接口不变、可联通。"""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402


class CliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        server.DB_PATH = Path(cls.tmp.name) / "cli.db"
        server.TOKEN = ""
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

    def run_cli(self, script, *args, env=None):
        e = os.environ.copy()
        e["DISPATCH_URL"] = self.base
        e["DISPATCH_DB"] = str(server.DB_PATH)  # 子进程 ctl.py 需指向同一个测试库
        e.pop("DISPATCH_TOKEN", None)
        if env:
            e.update(env)
        r = subprocess.run([sys.executable, str(ROOT / script), *args],
                           capture_output=True, text=True, timeout=30, env=e, cwd=str(ROOT))
        return r.returncode, r.stdout + r.stderr

    def test_ctl_pair_assign_and_agent_execute(self):
        # ctl pair
        rc, out = self.run_cli("ctl.py", "pair", "cli-agent")
        self.assertEqual(rc, 0, out)
        code = re.search(r"配对码:\s*([A-Z0-9-]+)", out).group(1)

        # agent pair
        cred = str(Path(self.tmp.name) / "cred.json")
        rc, out = self.run_cli("agent.py", "pair", "--server", self.base,
                               "--name", "cli-agent", "--code", code,
                               "--cred-file", cred)
        self.assertEqual(rc, 0, out)
        self.assertIn("配对成功", out)

        # submit 下发（走 HTTP）
        rc, out = self.run_cli("submit.py", "--agent", "cli-agent",
                               "--title", "CLI任务", "--command", "echo CLI-OK")
        self.assertEqual(rc, 0, out)
        self.assertIn("指派给", out)

        # agent start 执行
        rc, out = self.run_cli("agent.py", "start", "--cred-file", cred, "--once")
        self.assertEqual(rc, 0, out)
        self.assertIn("CLI-OK", out)

        # 任务确实 succeeded
        tasks = json.load(urllib.request.urlopen(self.base + "/api/tasks"))["tasks"]
        t = [x for x in tasks if x["assignee"] == "cli-agent"][-1]
        self.assertEqual(t["status"], "succeeded")

    def test_ctl_agents_lists(self):
        server.create_pairing_code("listed")
        rc, out = self.run_cli("ctl.py", "agents")
        self.assertEqual(rc, 0, out)
        self.assertIn("listed", out)

    def test_submit_list_agents(self):
        rc, out = self.run_cli("submit.py", "--list-agents")
        self.assertEqual(rc, 0, out)
        self.assertIn("Agent", out)

    def test_page_has_three_sections(self):
        html = urllib.request.urlopen(self.base + "/", timeout=5).read().decode()
        for section in ("Agent 管理中心", "如何配对", "下发任务", "任务看板"):
            self.assertIn(section, html)

    def test_server_access_hint_loopback_warns(self):
        """绑回环地址时要明确警告外部连不上，并给出 --host 0.0.0.0 方案。"""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            server.print_access_hint("127.0.0.1", 8765)
        out = buf.getvalue()
        self.assertIn("只有本机", out)
        self.assertIn("--host 0.0.0.0", out)
        self.assertIn("--server", out)

    def test_server_access_hint_bound_addr_shows_url(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            server.print_access_hint("0.0.0.0", 9000)
        out = buf.getvalue()
        self.assertIn("http://", out)
        self.assertIn("9000", out)

    def test_prompt_driven_fetch_report_cycle(self):
        """提示词驱动模式：fetch 领任务 → report 回传，全程单行 JSON，任务状态变了。"""
        code = server.create_pairing_code("prompt-agent")
        cred = str(Path(self.tmp.name) / "prompt-cred.json")

        # 配对
        rc, out = self.run_cli("agent.py", "pair", "--server", self.base,
                               "--name", "prompt-agent", "--code", code,
                               "--cred-file", cred)
        self.assertEqual(rc, 0, out)

        # 下发任务
        task = server.create_task("提示词任务", "prompt-agent",
                                  {"command": "say hello", "executor": "workbuddy"})

        # fetch 领取（应输出单行 JSON，含 task.id）
        rc, out = self.run_cli("agent.py", "fetch", "--cred-file", cred)
        self.assertEqual(rc, 0, out)
        line = [l for l in out.splitlines() if l.strip().startswith("{")][-1]
        data = json.loads(line)
        self.assertTrue(data["ok"])
        self.assertEqual(data["task"]["id"], task["id"])
        self.assertIn("report", data["hint"])

        # 再 fetch 应无任务
        rc, out = self.run_cli("agent.py", "fetch", "--cred-file", cred)
        line = [l for l in out.splitlines() if l.strip().startswith("{")][-1]
        self.assertIsNone(json.loads(line)["task"])

        # report 回传成功
        rc, out = self.run_cli("agent.py", "report", str(task["id"]),
                               "--result", "任务完成", "--cred-file", cred)
        self.assertEqual(rc, 0, out)
        line = [l for l in out.splitlines() if l.strip().startswith("{")][-1]
        self.assertTrue(json.loads(line)["ok"])

        # 校验任务状态
        tasks = json.load(urllib.request.urlopen(self.base + "/api/tasks"))["tasks"]
        t = [x for x in tasks if x["id"] == task["id"]][0]
        self.assertEqual(t["status"], "succeeded")

    def test_prompt_driven_fail_and_heartbeat(self):
        """fail 回传失败 + heartbeat 保活：都返回 ok JSON。"""
        code = server.create_pairing_code("fail-agent")
        cred = str(Path(self.tmp.name) / "fail-cred.json")
        rc, out = self.run_cli("agent.py", "pair", "--server", self.base,
                               "--name", "fail-agent", "--code", code,
                               "--cred-file", cred)
        self.assertEqual(rc, 0, out)
        task = server.create_task("失败任务", "fail-agent", {"command": "do x"})

        # fetch 领取
        self.run_cli("agent.py", "fetch", "--cred-file", cred)
        # heartbeat 保活
        rc, out = self.run_cli("agent.py", "heartbeat", str(task["id"]), "--cred-file", cred)
        line = [l for l in out.splitlines() if l.strip().startswith("{")][-1]
        self.assertTrue(json.loads(line)["ok"])
        # fail 回传
        rc, out = self.run_cli("agent.py", "fail", str(task["id"]),
                               "--error", "无法完成", "--cred-file", cred)
        line = [l for l in out.splitlines() if l.strip().startswith("{")][-1]
        self.assertTrue(json.loads(line)["ok"])
        tasks = json.load(urllib.request.urlopen(self.base + "/api/tasks"))["tasks"]
        t = [x for x in tasks if x["id"] == task["id"]][0]
        self.assertEqual(t["status"], "failed")

    def test_report_requires_task_id_and_content(self):
        """report 缺任务 ID / 缺结果内容时报 JSON 错误，不崩溃。"""
        rc, out = self.run_cli("agent.py", "report")
        self.assertEqual(rc, 1)
        self.assertIn("需要任务 ID", out)
        code = server.create_pairing_code("noresult-agent")
        cred = str(Path(self.tmp.name) / "noresult-cred.json")
        self.run_cli("agent.py", "pair", "--server", self.base,
                     "--name", "noresult-agent", "--code", code, "--cred-file", cred)
        rc, out = self.run_cli("agent.py", "report", "999", "--cred-file", cred)
        self.assertEqual(rc, 1)
        self.assertIn("结果内容不能为空", out)

    def test_ctl_pair_hint_mentions_cred_reuse(self):
        """pair 提示必须说明地址只写一次、存凭证复用，且本地地址给出跨机器告警。"""
        rc, out = self.run_cli("ctl.py", "pair", "hint-agent")
        self.assertEqual(rc, 0, out)
        self.assertIn("只需在 pair 时写一次", out)
        # 测试环境 DISPATCH_URL 是 127.0.0.1 的临时端口，落到本地地址告警分支
        self.assertIn("本机地址", out)
        self.assertIn("--host 0.0.0.0", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
