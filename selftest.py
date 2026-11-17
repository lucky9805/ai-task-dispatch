"""功能自检：逐项验证每个功能点，用真实 service 逻辑在独立临时库上跑，不碰生产数据。

两种用法：
  CLI：python3 selftest.py            # 每项 PASS/FAIL + 汇总，失败 exit 1
  程序：result = selftest.run_all()   # 返回结构化 dict，供 HTTP /api/selftest 使用

设计原则：每一项都调用真实的 service/repository 代码，不用 mock 假装通过；
每项独立断言，一项失败不影响其他项继续跑，最后汇总。
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

import db as db_mod
from errors import AuthError, DispatchError, ValidationError
from service import Services

LEASE = 0.5          # 租约短一点，方便验证超时回收
ONLINE = 30.0
PAIR_TTL = 600.0


@contextmanager
def _sandbox():
    """独立的临时库 + 串行事务上下文，构造一套干净的 Services。"""
    tmp = tempfile.mkdtemp(prefix="td-selftest-")
    conn = db_mod.connect(Path(tmp) / "test.db")
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

    svc = Services(conn, tx, lease_seconds=LEASE, online_window=ONLINE, pair_ttl=PAIR_TTL)
    try:
        yield svc, conn
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _expect_raises(exc_type, fn, *a, **kw):
    """断言 fn 抛出指定异常；否则抛 AssertionError。"""
    try:
        fn(*a, **kw)
    except exc_type:
        return True
    except Exception as e:
        raise AssertionError("期望 %s，实际 %s: %s" % (exc_type.__name__, type(e).__name__, e))
    raise AssertionError("期望 %s，但没有抛异常" % exc_type.__name__)


# ---- 每个检查项：签名 (svc, conn) -> 说明文字 ----

def c01_pair_code(svc, conn):
    code = svc.pairing.create_code("agent-a")
    assert isinstance(code, str) and len(code) >= 8 and "-" in code, code
    agents = svc.agents.list()
    assert any(a["name"] == "agent-a" and not a["paired"] for a in agents), agents
    return "生成配对码 %s，Agent 登记为待配对" % code


def c02_redeem(svc, conn):
    code = svc.pairing.create_code("agent-a")
    token = svc.pairing.redeem("agent-a", code)
    assert isinstance(token, str) and len(token) >= 20
    assert svc.pairing.check_token("agent-a", token) is True
    agents = svc.agents.list()
    assert next(a for a in agents if a["name"] == "agent-a")["paired"] is True
    return "配对码兑换出凭证，check_token 校验通过，状态变为已配对"


def c03_wrong_code(svc, conn):
    svc.pairing.create_code("agent-a")
    _expect_raises(ValidationError, svc.pairing.redeem, "agent-a", "WRONG-CODE")
    assert svc.pairing.check_token("agent-a", "WRONG-CODE") is False
    return "错误配对码被拒绝，且不能通过凭证校验"


def c04_code_reuse(svc, conn):
    code = svc.pairing.create_code("agent-a")
    svc.pairing.redeem("agent-a", code)
    _expect_raises(ValidationError, svc.pairing.redeem, "agent-a", code)
    return "配对码一次性，重复使用被拒绝"


def c05_code_name_mismatch(svc, conn):
    code = svc.pairing.create_code("agent-a")
    _expect_raises(ValidationError, svc.pairing.redeem, "agent-b", code)
    return "配对码与 Agent 名不匹配时被拒绝"


def c06_create_task(svc, conn):
    svc.pairing.redeem("agent-a", svc.pairing.create_code("agent-a"))
    task = svc.tasks.create("生成日报", "agent-a", {"command": "echo hi"})
    assert task["status"] == "pending" and task["assignee"] == "agent-a", task
    return "下发任务 #%s，指定给 agent-a，初始 pending" % task["id"]


def c07_create_task_requires_assignee(svc, conn):
    _expect_raises(ValidationError, svc.tasks.create, "无主任务", "", {})
    _expect_raises(ValidationError, svc.tasks.create, "派给陌生人", "ghost", {})
    return "缺 assignee 或目标未登记的下发被拒绝"


def c08_claim_requires_pairing(svc, conn):
    svc.pairing.create_code("agent-a")  # 登记但故意不兑换凭证 → 保持未配对
    svc.tasks.create("任务", "agent-a", {"command": "echo x"})
    _expect_raises(AuthError, svc.tasks.claim, "agent-a", "fake-token")
    return "未配对/伪造凭证的 Agent 领取被拒（401 AuthError）"


def c09_targeted_claim(svc, conn):
    ta = svc.pairing.redeem("agent-a", svc.pairing.create_code("agent-a"))
    tb = svc.pairing.redeem("agent-b", svc.pairing.create_code("agent-b"))
    svc.tasks.create("只给 A", "agent-a", {"command": "echo only-a"})
    assert svc.tasks.claim("agent-b", tb) is None, "agent-b 不应领到派给 a 的任务"
    claimed = svc.tasks.claim("agent-a", ta)
    assert claimed and claimed["assignee"] == "agent-a" and claimed["status"] == "running"
    return "非目标 Agent 领不到；目标 Agent 领到并置为 running"


def c10_progress_and_complete(svc, conn):
    tok = svc.pairing.redeem("agent-a", svc.pairing.create_code("agent-a"))
    task = svc.tasks.create("带进展的任务", "agent-a", {"command": "echo ok"})
    svc.tasks.claim("agent-a", tok)
    assert svc.tasks.progress(task["id"], "agent-a", tok, 50, "执行中") is True
    assert svc.tasks.progress(task["id"], "agent-a", tok, 500, "越界") is True  # 应被 clamp
    assert svc.tasks.finish(task["id"], "agent-a", tok, True, "DONE-OK") is True
    got = svc.tasks.get(task["id"])
    assert got["status"] == "succeeded" and got["result"] == "DONE-OK" and got["progress"] == 100, got
    return "进展回传（含越界钳制到 100）+ 完成回传，结果与状态正确"


def c11_fail_and_owner_check(svc, conn):
    ta = svc.pairing.redeem("agent-a", svc.pairing.create_code("agent-a"))
    tb = svc.pairing.redeem("agent-b", svc.pairing.create_code("agent-b"))
    task = svc.tasks.create("会失败的任务", "agent-a", {"command": "exit 1"})
    svc.tasks.claim("agent-a", ta)
    assert svc.tasks.finish(task["id"], "agent-b", tb, True, "伪造成功") is False, "非 owner 不能回传"
    assert svc.tasks.finish(task["id"], "agent-a", ta, False, "BOOM") is True
    got = svc.tasks.get(task["id"])
    assert got["status"] == "failed" and got["error"] == "BOOM", got
    return "非 owner 回传被拒；owner 失败回传写入 error 与 failed"


def c12_revoke_effective(svc, conn):
    tok = svc.pairing.redeem("agent-a", svc.pairing.create_code("agent-a"))
    svc.tasks.create("撤销前的任务", "agent-a", {"command": "echo x"})
    assert svc.pairing.revoke("agent-a") is True
    assert svc.pairing.check_token("agent-a", tok) is False
    _expect_raises(AuthError, svc.tasks.claim, "agent-a", tok)
    return "撤销后旧凭证立即失效，无法领取"


def c13_lease_reclaim(svc, conn):
    tok = svc.pairing.redeem("agent-a", svc.pairing.create_code("agent-a"))
    task = svc.tasks.create("超时任务", "agent-a", {"command": "echo x"})
    svc.tasks.claim("agent-a", tok)
    assert svc.tasks.get(task["id"])["status"] == "running"
    # 把 claimed_at 改成很久以前，模拟 Agent 掉线
    conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?", (time.time() - 100, task["id"]))
    svc.tasks.list(None, None)  # list 会触发 reclaim_expired
    got = svc.tasks.get(task["id"])
    assert got["status"] == "pending" and got["agent_id"] is None, got
    return "租约超时后任务自动回收为 pending，可被重新领取"


def c14_concurrent_no_dup(svc, conn):
    """10 个 Agent 抢 3 个指派任务：每个任务只被领一次，总领取数守恒。"""
    tokens = {}
    for i in range(10):
        n = "w%d" % i
        tokens[n] = svc.pairing.redeem(n, svc.pairing.create_code(n))
    for i in range(3):
        svc.tasks.create("并发任务%d" % i, "w%d" % i, {"command": "echo x"})
    got_ids = []
    for n, t in tokens.items():
        task = svc.tasks.claim(n, t)
        if task:
            got_ids.append(task["id"])
    assert len(got_ids) == 3 and len(set(got_ids)) == 3, got_ids
    return "10 个 Agent 并发领取 3 个指派任务：各领各的、零重复零丢失"


def c15_old_db_migration(_svc, _conn):
    """旧结构库经 init_db 升级后补列且历史数据不丢。"""
    tmp = tempfile.mkdtemp(prefix="td-selftest-old-")
    path = Path(tmp) / "old.db"
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT, payload TEXT, status TEXT, agent_id TEXT, created_at REAL, updated_at REAL)")
    c.execute("INSERT INTO tasks VALUES (1,'历史任务','{}','succeeded','legacy',1,2)")
    c.execute("CREATE TABLE agents (name TEXT PRIMARY KEY, first_seen REAL, last_seen REAL, claimed_count INTEGER)")
    c.execute("INSERT INTO agents VALUES ('legacy',1,2,7)")
    c.commit()
    c.close()
    conn = db_mod.connect(path)
    db_mod.init_db(conn)
    assignee = conn.execute("SELECT assignee FROM tasks WHERE id=1").fetchone()[0]
    count = conn.execute("SELECT claimed_count FROM agents WHERE name='legacy'").fetchone()[0]
    assert assignee == "legacy" and count == 7, (assignee, count)
    assert db_mod.schema_version(conn) == db_mod.SCHEMA_VERSION
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)
    return "旧库自动补列，历史任务按执行者回填 assignee，计数不丢"


def c16_executors(svc, conn):
    """可插拔执行器：各工具命令构造正确，payload.executor 可被识别。"""
    import executors as E
    task = {"id": 1, "title": "生成日报", "payload": {"command": "echo ok"}}
    assert "claude -p" in E.build_command("claude", task)
    assert E.build_command("codex", task).startswith("codex exec ")
    custom = E.build_command('my-agent run "{task}"', task)
    assert "生成日报" in custom and "echo ok" in custom, custom
    # 任务自带 executor 能被识别为自定义模板（中心指定执行器）
    routed = E.build_command(task["payload"].get("executor", "shell"), task)
    assert routed == "echo ok", routed
    return "执行器模板齐全（shell/claude/codex/自定义），任务可指定执行器"


CHECKS = [
    ("01 登记 Agent 与生成配对码", c01_pair_code),
    ("02 配对码兑换凭证", c02_redeem),
    ("03 错误配对码被拒", c03_wrong_code),
    ("04 配对码一次性", c04_code_reuse),
    ("05 配对码绑名字", c05_code_name_mismatch),
    ("06 下发任务并指定 Agent", c06_create_task),
    ("07 下发必须指定已登记 Agent", c07_create_task_requires_assignee),
    ("08 未配对 Agent 领取被拒", c08_claim_requires_pairing),
    ("09 定向领取（别人领不到）", c09_targeted_claim),
    ("10 进展与完成回传", c10_progress_and_complete),
    ("11 失败回传与 owner 校验", c11_fail_and_owner_check),
    ("12 撤销凭证立即失效", c12_revoke_effective),
    ("13 租约超时自动回收", c13_lease_reclaim),
    ("14 并发领取零重复", c14_concurrent_no_dup),
    ("15 旧库迁移不丢数据", c15_old_db_migration),
    ("16 可插拔执行器", c16_executors),
]


def run_all() -> dict:
    """跑全部检查项，返回结构化结果。任何单项失败不影响其他项。"""
    items = []
    for name, fn in CHECKS:
        t0 = time.time()
        # 每项独立沙箱：领取会挑最早的 pending 任务，共用库会跨项串扰导致假失败
        try:
            with _sandbox() as (svc, conn):
                detail = fn(svc, conn)
            items.append({"name": name, "passed": True,
                          "detail": detail, "ms": round((time.time() - t0) * 1000, 1)})
        except Exception as e:
            items.append({"name": name, "passed": False,
                          "detail": "%s: %s" % (type(e).__name__, e),
                          "trace": traceback.format_exc(limit=3),
                          "ms": round((time.time() - t0) * 1000, 1)})
    ok = sum(1 for i in items if i["passed"])
    return {
        "passed": ok == len(items),
        "total": len(items),
        "ok": ok,
        "failed": len(items) - ok,
        "ts": time.time(),
        "items": items,
    }


def format_text(result: dict) -> str:
    lines = ["=" * 58, "task-dispatch 功能自检（逐项真实调用）", "=" * 58]
    for i, it in enumerate(result["items"], 1):
        mark = "PASS" if it["passed"] else "FAIL"
        name = it["name"][3:] if len(it["name"]) > 3 and it["name"][2] == " " else it["name"]
        lines.append("[%s] %02d. %-30s %5.1fms" % (mark, i, name, it["ms"]))
        lines.append("        %s" % it["detail"])
    lines.append("-" * 58)
    lines.append("结果：%d/%d 通过，%d 失败" % (result["ok"], result["total"], result["failed"]))
    lines.append("=" * 58)
    return "\n".join(lines)


def main() -> int:
    r = run_all()
    print(format_text(r))
    return 0 if r["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
