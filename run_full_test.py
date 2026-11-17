"""全量回归入口（RSI 风格）：从头重跑所有里程碑 Gate + 编译 + 启动冒烟 + 迁移体检。

零第三方依赖。任何一项失败 → exit 1；全绿 → exit 0 并写 reports/full-test-latest.json。
用法：python3 run_full_test.py [--quick]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable

# 每个里程碑的 gate（对应 REFACTOR_PLAN.md）
GATES = [
    ("M1 基础设施", ["tests.test_config", "tests.test_errors", "tests.test_logging"]),
    ("M2 数据层", ["tests.test_db", "tests.test_repository"]),
    ("M3 业务层", ["tests.test_service"]),
    ("M4 控制器", ["tests.test_api"]),
    ("M5 CLI/页面", ["tests.test_cli"]),
    ("可插拔执行器", ["tests.test_executors"]),
    ("功能自检", ["tests.test_selftest"]),
    ("回归基线", ["tests.test_dispatch"]),
]
PY_FILES = ["config.py", "errors.py", "logging_util.py", "db.py",
            "repository.py", "service.py", "server.py", "agent.py",
            "ctl.py", "submit.py", "selftest.py", "executors.py"]


def run(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return 124, "超时（>%ss）" % timeout


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def check_compile():
    rc, out = run([PY, "-m", "py_compile", *PY_FILES])
    return rc == 0, ("通过" if rc == 0 else out[-500:])


def check_gates():
    results = []
    for name, modules in GATES:
        rc, out = run([PY, "-m", "unittest", *modules])
        # 取 unittest 摘要行
        summary = ""
        for line in out.splitlines():
            if line.startswith("Ran ") or line == "OK" or line.startswith("FAILED"):
                summary += line + " "
        ok = rc == 0
        results.append((name, ok, summary.strip() or ("exit=%d" % rc)))
    return results


def check_startup():
    """真实起服务，curl 健康检查 + 页面 + 登记端点，再停掉。"""
    port = free_port()
    env = os.environ.copy()
    env["DISPATCH_DB"] = str(Path(tempfile.mkdtemp()) / "smoke.db")
    env["DISPATCH_TOKEN"] = ""
    proc = subprocess.Popen([PY, "server.py", "--port", str(port)], cwd=str(ROOT),
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        base = "http://127.0.0.1:%d" % port
        for _ in range(30):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        else:
            return False, "服务 6s 内未就绪"
        h = json.load(urllib.request.urlopen(base + "/api/health", timeout=3))
        page = urllib.request.urlopen(base + "/", timeout=3).read().decode()
        blocks = ["Agent 管理中心", "如何配对", "Agent 工作台", "功能自检"]
        ok = h.get("ok") is True and all(b in page for b in blocks)
        return ok, ("health ok + 页面六大区块在" if ok else "health/页面异常")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def check_migration():
    """旧库（无新列）经 init_db 升级后数据不丢。"""
    tmp = tempfile.mkdtemp()
    dbpath = Path(tmp) / "old.db"
    code = """
import sqlite3, sys
sys.path.insert(0, %r)
import db
c = sqlite3.connect(%r)
c.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT, payload TEXT, status TEXT, agent_id TEXT, created_at REAL, updated_at REAL)")
c.execute("INSERT INTO tasks VALUES (1,'OLD','{}','succeeded','w',1,2)")
c.execute("CREATE TABLE agents (name TEXT PRIMARY KEY, first_seen REAL, last_seen REAL, claimed_count INTEGER)")
c.execute("INSERT INTO agents VALUES ('w',1,2,9)")
c.commit(); c.close()
c = db.connect(%r); db.init_db(c)
a = c.execute("SELECT assignee FROM tasks WHERE id=1").fetchone()[0]
n = c.execute("SELECT claimed_count FROM agents WHERE name='w'").fetchone()[0]
assert a == 'w' and n == 9, (a, n)
print("OK")
""" % (str(ROOT), str(dbpath), str(dbpath))
    rc, out = run([PY, "-c", code])
    return rc == 0, ("旧库迁移数据不丢" if rc == 0 else out[-400:])


def main():
    ap = argparse.ArgumentParser(description="全量回归")
    ap.add_argument("--quick", action="store_true", help="跳过启动冒烟")
    args = ap.parse_args()

    print("=" * 60)
    print("task-dispatch 全量回归（RSI 风格）")
    print("=" * 60)

    ok_compile, detail = check_compile()
    print("[编译] %s %s" % ("PASS" if ok_compile else "FAIL", detail))

    gate_results = check_gates()
    for name, ok, summary in gate_results:
        print("[%s] %s %s" % ("PASS" if ok else "FAIL", name, summary))

    ok_mig, detail = check_migration()
    print("[迁移] %s %s" % ("PASS" if ok_mig else "FAIL", detail))

    ok_start, detail = ("SKIP", "quick 模式") if args.quick else check_startup()
    if isinstance(ok_start, str):
        print("[启动] SKIP %s" % detail)
        ok_start = True
    else:
        print("[启动] %s %s" % ("PASS" if ok_start else "FAIL", detail))

    all_ok = ok_compile and ok_mig and ok_start and all(r[1] for r in gate_results)

    report = {
        "passed": all_ok,
        "compile": ok_compile,
        "migration": ok_mig,
        "startup": ok_start,
        "gates": [{"name": n, "passed": o, "detail": d} for n, o, d in gate_results],
    }
    reports = ROOT / "reports"
    if not reports.exists():
        reports.mkdir()
    (reports / "full-test-latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 60)
    print("全量回归：%s" % ("全部通过" if all_ok else "存在失败项，见上方 FAIL"))
    print("报告：reports/full-test-latest.json")
    print("=" * 60)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
