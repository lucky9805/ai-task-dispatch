"""命令行把任务下发给指定的 Agent（需先在服务端登记该 Agent）。

示例：python3 submit.py --agent agent-1 --title "生成日报" --command "echo hello"
查看可指派对象：python3 submit.py --list-agents
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def call(server: str, token: str, path: str, payload=None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Dispatch-Token"] = token
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        server.rstrip("/") + path, data=data, headers=headers,
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", str(e))
        except Exception:
            msg = str(e)
        raise RuntimeError(msg)


def main():
    ap = argparse.ArgumentParser(description="把任务下发给指定的 Agent")
    ap.add_argument("--agent", "--assignee", dest="agent",
                    help="下发给哪个 Agent（必须是服务端 ctl.py pair 登记过的名字）")
    ap.add_argument("--title", help="任务标题")
    ap.add_argument("--command", help="Agent 端要执行的具体命令，写入 payload.command")
    ap.add_argument("--payload", default="{}", help="完整 payload JSON（与 --command 合并）")
    ap.add_argument("--executor", "-e", default=None,
                    help="执行器：shell/claude/codex/workbuddy/chatgpt/echo 或自定义命令模板，写入 payload.executor")
    ap.add_argument("--server", default=os.environ.get("DISPATCH_URL", "http://127.0.0.1:8765"),
                    help="中心地址；中心和你在同一台机器用默认值，否则填 http://<中心IP>:8765"
                         "（等价环境变量 DISPATCH_URL）")
    ap.add_argument("--token", default=os.environ.get("DISPATCH_TOKEN", ""))
    ap.add_argument("--list-agents", action="store_true", help="列出已登记 Agent 及配对状态")
    args = ap.parse_args()

    try:
        if args.list_agents:
            agents = call(args.server, args.token, "/api/agents")["agents"]
            print("共 %d 个 Agent（%d 已配对 / %d 在线）" % (
                len(agents), sum(1 for a in agents if a["paired"]),
                sum(1 for a in agents if a["online"]),
            ))
            for a in agents:
                state = "已撤销" if a["revoked"] else ("已配对" if a["paired"] else "待配对")
                print("  %-16s %-6s %-4s 已领 %d 单" % (
                    a["name"], state, "在线" if a["online"] else "离线", a["claimed_count"],
                ))
            return

        if not args.agent or not args.agent.strip():
            sys.exit("必须指定 --agent（下发给哪个 Agent），可用 --list-agents 查看")
        if not args.title or not args.title.strip():
            sys.exit("必须指定 --title")
        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError as e:
            sys.exit("--payload 不是合法 JSON: %s" % e)
        if not isinstance(payload, dict):
            sys.exit("--payload 必须是 JSON 对象")
        if args.command:
            payload["command"] = args.command
        if args.executor:
            payload["executor"] = args.executor

        task = call(args.server, args.token, "/api/tasks", {
            "title": args.title, "assignee": args.agent.strip(), "payload": payload,
        })["task"]
        print("已将任务 #%s「%s」指派给 %s（%s）" % (
            task["id"], task["title"], task["assignee"], task["status"],
        ))
    except Exception as e:
        sys.exit("操作失败: %s" % e)


if __name__ == "__main__":
    main()
