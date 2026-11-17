"""服务端管理 CLI：登记 Agent、生成配对码、指派任务、撤销凭证。

在服务端本机运行，直接读写同一个 SQLite 库（可与 server.py 同时运行）：
  python3 ctl.py pair agent-1            # 登记并生成一次性配对码
  python3 ctl.py agents                  # 查看配对状态
  python3 ctl.py assign agent-1 "生成日报" "echo hello"
  python3 ctl.py revoke agent-1          # 立刻吊销凭证
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import server  # noqa: E402

DEFAULT_URL = os.environ.get("DISPATCH_URL", "http://127.0.0.1:8765")


def main() -> int:
    ap = argparse.ArgumentParser(description="任务中心管理 CLI（服务端本机运行）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pair", help="登记 Agent 并生成一次性配对码")
    p.add_argument("name", help="Agent 名字")
    p.add_argument("--ttl", type=float, default=None, help="配对码有效期（秒），默认 600")
    p.add_argument("--server", default=DEFAULT_URL,
                   help="中心对外地址，写进打印的客户端命令（如 http://192.168.1.10:8765）。"
                        "Agent 在别的机器时务必改成真实 IP/域名")

    sub.add_parser("agents", help="列出 Agent 与配对状态")

    p = sub.add_parser("revoke", help="撤销某个 Agent 的配对凭证")
    p.add_argument("name")

    p = sub.add_parser("archive", help="归档/取消归档 Agent（虚拟删除，隐藏且停活，可恢复）")
    p.add_argument("name")
    p.add_argument("--restore", action="store_true", help="取消归档（默认执行归档）")

    p = sub.add_parser("category", help="给 Agent 设置分类标签（传空串清除）")
    p.add_argument("name")
    p.add_argument("category", help="分类名，如 生产/测试/闲置；传空串表示清除")

    p = sub.add_parser("assign", help="把任务指派给已登记的 Agent")
    p.add_argument("name", help="指派给哪个 Agent")
    p.add_argument("title", help="任务标题")
    p.add_argument("command", help="Agent 要执行的具体命令或任务描述")
    p.add_argument("--executor", "-e", default=None,
                   help="执行器：shell/claude/codex/workbuddy/chatgpt/echo 或自定义模板，写入 payload.executor")

    # 安全加固：用户管理
    pu = sub.add_parser("user", help="用户管理（add/list/role/status/password/delete）")
    pua = pu.add_subparsers(dest="user_op", required=True)
    pa = pua.add_parser("add", help="新建用户")
    pa.add_argument("username"); pa.add_argument("password")
    pa.add_argument("--role", default="operator", choices=["admin", "operator", "viewer"])
    pa.add_argument("--display", default="")
    pua.add_parser("list", help="列出用户")
    pr = pua.add_parser("role", help="改角色"); pr.add_argument("username"); pr.add_argument("role", choices=["admin","operator","viewer"])
    ps = pua.add_parser("status", help="启用/禁用"); ps.add_argument("username"); ps.add_argument("state", choices=["active","disabled"])
    pp = pua.add_parser("password", help="重置密码"); pp.add_argument("username"); pp.add_argument("password")
    pd = pua.add_parser("delete", help="删除用户"); pd.add_argument("username")

    pa2 = sub.add_parser("audit", help="查看审计日志")
    pa2.add_argument("--limit", type=int, default=50)
    pa2.add_argument("--actor", default=None)
    pa2.add_argument("--action", default=None)

    args = ap.parse_args()
    server.init_db()

    if args.cmd == "pair":
        code = server.create_pairing_code(args.name, args.ttl)
        ttl = int(args.ttl if args.ttl is not None else server.PAIR_TTL)
        print("Agent: %s" % args.name.strip())
        print("配对码: %s   （%d 秒内有效，仅可使用一次）" % (code, ttl))
        adv = args.server.rstrip("/")
        print("客户端执行（在 Agent 所在机器运行）:")
        print("  python3 agent.py pair --server %s --name %s --code %s"
              % (adv, args.name.strip(), code))
        print("  python3 agent.py start")
        print("说明：--server 只需在 pair 时写一次，它会存进凭证，start/领任务/回传都自动复用。")
        if adv in ("http://127.0.0.1:8765", "http://localhost:8765") or "127.0.0.1" in adv:
            print("[注意] 当前 --server 是本机地址 %s。" % adv)
            print("       若 Agent 在另一台机器，请改成中心的真实 IP/域名，例如：")
            print("         python3 ctl.py pair %s --server http://192.168.1.10:8765"
                  % args.name.strip())
            print("       并且中心要用 --host 0.0.0.0 启动，否则外部连不上。")
        return 0

    if args.cmd == "agents":
        agents = server.list_agents()
        active = [a for a in agents if not a.get("archived")]
        print("共 %d 个 Agent（%d 归档隐藏 / %d 已配对 / %d 在线）" % (
            len(active), sum(1 for a in agents if a.get("archived")),
            sum(1 for a in active if a["paired"]),
            sum(1 for a in active if a["online"]),
        ))
        for a in active:
            state = "已撤销" if a["revoked"] else ("已配对" if a["paired"] else "待配对")
            cat = ("[%s]" % a["category"]) if a.get("category") else ""
            print("  %-16s %-6s %-4s 已领 %d 单 %s" % (
                a["name"], state, "在线" if a["online"] else "离线", a["claimed_count"], cat,
            ))
        archived = [a for a in agents if a.get("archived")]
        if archived:
            print("  归档（不显示）: " + ", ".join(a["name"] for a in archived))
        return 0

    if args.cmd == "revoke":
        if not server.revoke_agent(args.name):
            print("Agent「%s」不存在" % args.name)
            return 1
        print("已撤销 %s 的配对凭证，该 Agent 立即无法领取或回传任务" % args.name)
        return 0

    if args.cmd == "archive":
        try:
            r = server.archive_agent(args.name, not args.restore)
        except Exception as e:
            print("操作失败：%s" % e)
            return 1
        print(("已取消归档 %s，重新显示并恢复接活" if args.restore else "已归档 %s，主页隐藏且停止接活（凭证保留，可 --restore 恢复）") % r["name"])
        return 0

    if args.cmd == "category":
        try:
            r = server.set_agent_category(args.name, args.category)
        except Exception as e:
            print("操作失败：%s" % e)
            return 1
        print("已设置 %s 的分类为「%s」" % (r["name"], r["category"] or "(清除)"))
        return 0

    if args.cmd == "assign":
        try:
            payload = {"command": args.command}
            if args.executor:
                payload["executor"] = args.executor
            task = server.create_task(args.title, args.name, payload)
        except ValueError as e:
            print("指派失败: %s" % e)
            return 1
        print("已将任务 #%s「%s」指派给 %s（%s）" % (
            task["id"], task["title"], task["assignee"], task["status"],
        ))
        return 0

    if args.cmd == "user":
        import threading
        from auth_service import SecurityService
        from errors import DispatchError
        lock = threading.RLock()
        from contextlib import contextmanager
        @contextmanager
        def _tx(immediate=False):
            with lock:
                c = server.db()
                if immediate: c.execute("BEGIN IMMEDIATE")
                try:
                    yield c
                    if immediate: c.execute("COMMIT")
                except Exception:
                    if immediate: c.execute("ROLLBACK")
                    raise
        sec = SecurityService(server.db(), _tx)
        try:
            if args.user_op == "add":
                u = sec.create_user(args.username, args.password, args.role, args.display,
                                    actor="cli")
                sec.audit("user", "cli", "user.create", target=u["username"],
                          detail="role=%s" % args.role)
                print("已创建用户 %s（%s，ID %s）" % (u["username"], u["role"], u["id"]))
            elif args.user_op == "list":
                users = sec.list_users()
                print("共 %d 个用户" % len(users))
                for u in users:
                    print("  %-3s %-16s %-10s %-9s %s" % (
                        u["id"], u["username"], u["role"], u["status"], u["display_name"]))
            elif args.user_op == "role":
                u = sec.update_user(_uid(sec, args.username), role=args.role, actor="cli")
                print("已将 %s 角色改为 %s" % (u["username"], u["role"]))
            elif args.user_op == "status":
                u = sec.update_user(_uid(sec, args.username), status=args.state, actor="cli")
                print("已将 %s 状态改为 %s" % (u["username"], u["status"]))
            elif args.user_op == "password":
                sec.update_user(_uid(sec, args.username), password=args.password, actor="cli")
                print("已重置 %s 的密码" % args.username)
            elif args.user_op == "delete":
                sec.delete_user(_uid(sec, args.username), actor="cli")
                print("已删除用户 %s" % args.username)
        except (DispatchError, ValueError) as e:
            print("操作失败: %s" % getattr(e, "message", str(e)))
            return 1
        return 0

    if args.cmd == "audit":
        import threading
        from auth_service import SecurityService
        from contextlib import contextmanager
        lock = threading.RLock()
        @contextmanager
        def _tx2(immediate=False):
            with lock:
                c = server.db()
                if immediate: c.execute("BEGIN IMMEDIATE")
                try:
                    yield c
                    if immediate: c.execute("COMMIT")
                except Exception:
                    if immediate: c.execute("ROLLBACK")
                    raise
        events = SecurityService(server.db(), _tx2).list_audit(args.limit, args.actor, args.action)
        print("最近 %d 条审计事件：" % len(events))
        for e in events:
            print("  [%.19s] %-14s %-24s %-16s %s" % (
                __import__("time").strftime("%Y-%m-%d %H:%M:%S", __import__("time").localtime(e["ts"])),
                e["actor"], e["action"], e["target"], e["result"]))
        return 0

    return 1


def _uid(sec, username: str) -> int:
    from errors import NotFoundError
    row = sec._repos(server.db())[0].get_by_name(username)
    if not row:
        raise NotFoundError("用户不存在: %s" % username)
    return row["id"]


if __name__ == "__main__":
    sys.exit(main())
