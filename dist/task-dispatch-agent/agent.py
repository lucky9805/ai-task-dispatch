"""Agent 端 CLI：先配对，再凭凭证领取「指派给自己」的任务并执行。

  # 第一次：用服务端给的一次性配对码换凭证（保存在本机，权限 600）
  python3 agent.py pair --server http://127.0.0.1:8765 --name agent-1 --code K7M2-9QX4

  # 之后：凭证自动读取，只领指派给自己的任务
  python3 agent.py start
  python3 agent.py status       # 查看当前配对信息

--executor 决定"领到任务后用什么干活"，默认 shell（跑 payload.command）：
  python3 agent.py start --executor claude     # Claude Code（需本机装 claude）
  python3 agent.py start --executor codex      # Codex（需本机装 codex）
  python3 agent.py start --executor workbuddy  # WorkBuddy/CodeBuddy（需装 codebuddy）
  python3 agent.py start --executor chatgpt    # ChatGPT（需 OPENAI_API_KEY）
  python3 agent.py start --executor echo --dry-run   # 只预览命令，不真跑
  python3 agent.py start -e 'my-agent run "{task}"'  # 任意工具：命令模板 + {task} 占位符

中心也可在任务的 payload.executor 里指定执行器，覆盖启动参数。
多个 Agent 并行：各自用 --cred-file 保存不同的凭证。

提示词驱动模式（WorkBuddy / Claude Code / Codex 等智能体自己干活时）：
  python3 agent.py pair ...                        # 一次配对，凭证记下中心地址
  python3 agent.py fetch                           # 领一个任务，输出 JSON（不执行不回传）
  python3 agent.py heartbeat <task_id>             # 长任务保活，防租约超时
  python3 agent.py report <task_id> --result '...' # 回传成功结果
  python3 agent.py fail <task_id> --error '...'    # 回传失败原因
这些命令输出单行 JSON，智能体读提示词即可驱动，无需把本程序装成常驻服务。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import executors

ROOT = Path(__file__).resolve().parent
DEFAULT_SERVER = "http://127.0.0.1:8765"


def log(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def cred_path(explicit: str | None = None) -> Path:
    return Path(explicit or os.environ.get("AGENT_CRED_FILE") or (ROOT / "agent-cred.json"))


def post(base: str, path: str, payload: dict, agent_token: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if agent_token:
        headers["X-Agent-Token"] = agent_token
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base.rstrip("/") + path, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def load_cred(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if not all(k in data for k in ("agent_id", "token", "server")):
        return None
    return data


def execute(task: dict, timeout: float, dry_run: bool, on_heartbeat,
            executor: str = "shell") -> tuple[bool, str]:
    """可插拔执行器：把任务交给指定工具跑，默认是 shell（向后兼容）。

    executor 可以是内置名（shell/claude/codex/workbuddy/chatgpt/echo）或任意自定义命令模板。
    任务可自带 payload.executor 覆盖启动参数——这样中心能指定"这条活该谁干"。
    配对/领取/回传协议与此无关，换工具不用动任何一处。
    """
    payload = task.get("payload") or {}
    chosen = str(payload.get("executor") or executor or "shell")
    if not chosen.strip():
        chosen = "shell"

    # shell 执行器沿用旧校验：必须有 command；其他执行器用标题/描述即可
    if chosen in ("shell",) and not payload.get("command"):
        return False, "任务缺少 payload.command"

    command = executors.build_command(chosen, task)

    if dry_run:
        time.sleep(0.2)
        return True, "[dry-run][%s] %s" % (chosen, command)

    # 外部工具没装时给友好错误，而不是抛一堆栈
    missing = executors.missing_binary(chosen)
    if missing:
        return False, missing

    stop = threading.Event()

    def heartbeat_loop():
        while not stop.wait(10):
            try:
                on_heartbeat()
            except Exception:
                pass

    threading.Thread(target=heartbeat_loop, daemon=True).start()
    try:
        r = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        text = (out or "(无输出, exit=%d)" % r.returncode)[:20000]
        if r.returncode != 0:
            return False, text
        # 某些 CLI（如未登录的 codebuddy）失败时仍返回 0，不能只看退出码。
        failure = executors.detect_failure_signal(out)
        if failure:
            return False, failure
        return True, text
    except subprocess.TimeoutExpired:
        return False, "执行超时（>%ss）" % timeout
    except Exception as e:
        return False, "执行异常: %s" % e
    finally:
        stop.set()


def cmd_pair(args) -> int:
    if not args.code or not args.code.strip():
        print("必须提供 --code（服务端 ctl.py pair 生成的一次性配对码）")
        return 1
    if not args.name or not args.name.strip():
        print("必须提供 --name（与服务端登记的名字一致）")
        return 1
    base = args.server or DEFAULT_SERVER
    try:
        resp = post(base, "/api/pair", {"name": args.name.strip(), "code": args.code.strip()})
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", str(e))
        except Exception:
            msg = str(e)
        print("配对失败: %s" % msg)
        return 1
    except Exception as e:
        print("配对失败: %s" % e)
        return 1

    path = cred_path(args.cred_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    cred = {
        "agent_id": resp["agent_id"],
        "token": resp["token"],
        "server": base.rstrip("/"),
        "paired_at": time.time(),
    }
    if args.executor:  # 可选：把默认执行器记进凭证，之后 start 不用再带
        cred["executor"] = args.executor
    path.write_text(json.dumps(cred, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    print("配对成功：Agent「%s」" % resp["agent_id"])
    print("凭证已保存到 %s（权限 600，请妥善保管）" % path)
    print("中心地址已记为 %s —— 已存进凭证，之后 start/领任务/回传都自动用它，无需再写 --server。"
          % base)
    print("启动：python3 agent.py start%s" % (
        " --cred-file %s" % path if args.cred_file else "",
    ))
    return 0


def cmd_status(args) -> int:
    path = cred_path(args.cred_file)
    data = load_cred(path)
    if not data:
        print("尚未配对。请先执行：python3 agent.py pair --name <名字> --code <配对码>")
        return 1
    print("凭证文件: %s" % path)
    print("Agent:    %s" % data["agent_id"])
    print("Server:   %s  （start 会自动连这里，通常无需再写 --server）" % data["server"])
    print("执行器:   %s" % executors.describe(data.get("executor") or "shell"))
    print("凭证:     %s..." % str(data["token"])[:8])
    return 0


def _emit(obj: dict) -> None:
    """打印一行 JSON，供提示词驱动的智能体稳定解析（日志走 stderr，不污染 stdout）。"""
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _cred_or_emit(args):
    """读凭证；失败时打印 JSON 错误并返回 None。"""
    data = load_cred(cred_path(args.cred_file))
    if not data:
        _emit({"ok": False, "error": "尚未配对，请先执行 agent.py pair"})
        return None
    return data


def cmd_fetch(args) -> int:
    """领一个指派给自己的任务，只返回任务 JSON，不执行、不回传。

    专为 WorkBuddy / Claude / Codex 这类"提示词驱动"的智能体设计：它们自己读
    提示词、用自己的能力干活，只需要用这几个原子命令跟中心打交道。
    """
    data = _cred_or_emit(args)
    if not data:
        return 1
    name, token = data["agent_id"], data["token"]
    base = args.server or data["server"]
    try:
        resp = post(base, "/api/tasks/claim", {"agent_id": name}, token)
        task = resp.get("task")
        if not task:
            _emit({"ok": True, "task": None, "message": "暂无指派给我的任务"})
            return 0
        _emit({
            "ok": True,
            "task": {
                "id": task["id"],
                "title": task.get("title"),
                "payload": task.get("payload"),
                "assignee": task.get("assignee"),
            },
            "hint": "执行任务后请用 agent.py report %s --result '<结果>' 回传；"
                    "失败用 agent.py fail %s --error '<原因>'" % (task["id"], task["id"]),
        })
        return 0
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "replace") if e.fp else str(e)
        _emit({"ok": False, "error": "HTTP %s: %s" % (e.code, msg[:200])})
        return 1
    except Exception as e:
        _emit({"ok": False, "error": "领取失败: %s" % e})
        return 1


def cmd_report(args) -> int:
    """回传任务结果（成功）。task_id 是必填位置参数。"""
    return _finish(args, ok=True)


def cmd_fail(args) -> int:
    """回传任务失败原因。task_id 是必填位置参数。"""
    return _finish(args, ok=False)


def _finish(args, ok: bool) -> int:
    data = _cred_or_emit(args)
    if not data:
        return 1
    name, token = data["agent_id"], data["token"]
    base = args.server or data["server"]
    text = (args.result if ok else args.error) or ""
    if not text.strip():
        _emit({"ok": False, "error": "结果内容不能为空（--result / --error）"})
        return 1
    path = "/api/tasks/%s/complete" % args.task_id if ok else "/api/tasks/%s/fail" % args.task_id
    key = "result" if ok else "error"
    try:
        resp = post(base, path, {"agent_id": name, key: text}, token)
        _emit({"ok": True, "task_id": args.task_id,
               "status": "succeeded" if ok else "failed", "response": resp})
        return 0
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "replace") if e.fp else str(e)
        _emit({"ok": False, "error": "HTTP %s: %s" % (e.code, msg[:200])})
        return 1
    except Exception as e:
        _emit({"ok": False, "error": "回传失败: %s" % e})
        return 1


def cmd_heartbeat(args) -> int:
    """长任务保活：领到任务后每隔一段时间调用一次，防止租约超时被回收。"""
    data = _cred_or_emit(args)
    if not data:
        return 1
    name, token = data["agent_id"], data["token"]
    base = args.server or data["server"]
    try:
        post(base, "/api/tasks/%s/heartbeat" % args.task_id, {"agent_id": name}, token)
        _emit({"ok": True, "task_id": args.task_id, "heartbeat": "sent"})
        return 0
    except Exception as e:
        _emit({"ok": False, "error": "心跳失败: %s" % e})
        return 1


def cmd_start(args) -> int:
    path = cred_path(args.cred_file)
    data = load_cred(path)
    if not data:
        print("尚未配对，无法领取任务。请先执行：")
        print("  python3 agent.py pair --server <地址> --name <名字> --code <配对码>")
        return 1
    name, token = data["agent_id"], data["token"]
    base = args.server or data["server"]
    # 执行器优先级：命令行 --executor > 凭证文件里保存的 > shell
    chosen = args.executor or data.get("executor") or "shell"
    log("Agent「%s」已配对 → %s，执行器=%s，只领取指派给我的任务%s"
        % (name, base, executors.describe(chosen), "（dry-run）" if args.dry_run else ""))

    while True:
        try:
            task = post(base, "/api/tasks/claim", {"agent_id": name}, token).get("task")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                log("凭证无效或已被撤销，请重新执行 pair")
                return 1
            log("领取失败：HTTP %s，%.1fs 后重试" % (e.code, args.interval))
            time.sleep(args.interval)
            continue
        except Exception as e:
            log("领取失败：%s，%.1fs 后重试" % (e, args.interval))
            time.sleep(args.interval)
            continue

        if not task:
            if args.once:
                log("暂无指派给我的任务，退出")
                break
            time.sleep(args.interval)
            continue

        tid = task["id"]
        log("领到指派给我的任务 #%s「%s」 payload=%s"
            % (tid, task["title"], json.dumps(task.get("payload"), ensure_ascii=False)))

        def heartbeat():
            post(base, "/api/tasks/%s/heartbeat" % tid, {"agent_id": name}, token)

        ok, text = execute(task, args.timeout, args.dry_run, heartbeat, chosen)
        try:
            if ok:
                post(base, "/api/tasks/%s/complete" % tid,
                     {"agent_id": name, "result": text}, token)
            else:
                post(base, "/api/tasks/%s/fail" % tid,
                     {"agent_id": name, "error": text}, token)
            log("任务 #%s 已回传：%s | %s"
                % (tid, "succeeded" if ok else "failed", text[:120].replace("\n", " ")))
        except Exception as e:
            log("任务 #%s 结果回传失败：%s（租约超时后中心会回收重派给同一 Agent）" % (tid, e))

        if args.once:
            break
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Agent 端 CLI（先配对，再领取任务）")
    ap.add_argument("command", nargs="?", default="start",
                    choices=["pair", "start", "status", "fetch", "report", "fail", "heartbeat"],
                    help="默认 start；fetch/report/fail/heartbeat 供提示词驱动的智能体逐步调用")
    ap.add_argument("task_id", nargs="?", default=None,
                    help="report/fail/heartbeat 时必填：任务 ID（fetch 返回的 task.id）")
    ap.add_argument("--server", default=None, help="中心地址；start 时默认用凭证里保存的地址")
    ap.add_argument("--name", default=None, help="pair 时用，Agent 名字")
    ap.add_argument("--code", default=None, help="pair 时用，服务端生成的一次性配对码")
    ap.add_argument("--cred-file", default=None, help="凭证文件路径，多 Agent 并行时各自指定")
    ap.add_argument("--interval", type=float, default=2.0, help="无任务时的轮询间隔（秒）")
    ap.add_argument("--timeout", type=float, default=300.0, help="单任务执行超时（秒）")
    ap.add_argument("--once", action="store_true", help="只处理一个任务后退出")
    ap.add_argument("--dry-run", action="store_true", help="不真执行命令，只回显（安全模式）")
    ap.add_argument("--executor", "-e", default=None,
                    help="执行器：shell/claude/codex/workbuddy/chatgpt/echo 或自定义命令模板"
                         "（默认 shell；也可用 payload.executor 为单条任务指定）")
    ap.add_argument("--result", default=None, help="report 时用：任务执行结果文本")
    ap.add_argument("--error", default=None, help="fail 时用：任务失败原因")
    args = ap.parse_args()

    if args.command == "pair":
        return cmd_pair(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command in ("report", "fail", "heartbeat") and not args.task_id:
        _emit({"ok": False, "error": "%s 需要任务 ID：agent.py %s <task_id> ..."
               % (args.command, args.command)})
        return 1
    if args.command == "fetch":
        return cmd_fetch(args)
    if args.command == "report":
        return cmd_report(args)
    if args.command == "fail":
        return cmd_fail(args)
    if args.command == "heartbeat":
        return cmd_heartbeat(args)
    return cmd_start(args)


if __name__ == "__main__":
    sys.exit(main())
