"""Agent 端一键部署向导（跨平台，零第三方依赖）。

用法：
  python3 setup.py check                                   # 只做环境自检 + 连通性 + 配对状态
  python3 setup.py --server http://192.168.1.10:8765 \
                   --name agent-2 --code XXXX-XXXX \
                   [--executor shell]                      # 非交互，直接配对
  python3 setup.py                                         # 交互向导（读 agent.env 作为默认值）

它会：① 检查 Python 与核心文件 ② 检查中心连通性 ③ 调 agent.py 配对
     ④ 配对后验证 status 与 fetch。凭证由 agent.py 写入 agent-cred.json。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CORE_FILES = ["agent.py", "executors.py", "chatgpt_run.py", "AGENT_PROMPT.md"]
EXEC_CHOICES = ["shell", "echo", "codex", "workbuddy", "claude", "chatgpt"]
PY = sys.executable


def _run(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run([PY, *args], cwd=str(ROOT), capture_output=True,
                          text=True, timeout=timeout, encoding="utf-8", errors="replace")


def ok(msg: str) -> None:
    print("  [OK]   " + msg)


def warn(msg: str) -> None:
    print("  [WARN] " + msg)


def bad(msg: str) -> None:
    print("  [FAIL] " + msg)


def check_env(server: str | None = None, probe: bool = True) -> bool:
    print("\n=== 1. 环境自检 ===")
    all_ok = True
    v = sys.version_info
    if v >= (3, 7):
        ok("Python %d.%d.%d（>=3.7，OK）" % (v.major, v.minor, v.micro))
    else:
        bad("Python 版本过低：%d.%d（需要 >=3.7）" % (v.major, v.minor))
        all_ok = False

    for f in CORE_FILES:
        (ok if (ROOT / f).exists() else bad)("核心文件 %s %s"
                                              % (f, "存在" if (ROOT / f).exists() else "缺失"))
        if not (ROOT / f).exists():
            all_ok = False

    cred = ROOT / "agent-cred.json"
    if cred.exists():
        ok("凭证 agent-cred.json 已存在（已配对，可跳过配对步骤）")
    else:
        warn("凭证不存在（尚未配对，需执行配对）")

    if server and probe:
        probe_connectivity(server)
    return all_ok


def probe_connectivity(server: str) -> bool:
    print("\n=== 2. 中心连通性 ===")
    url = server.rstrip("/") + "/api/health"
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=6) as r:
            body = r.read().decode("utf-8", "replace")
        ok("可连接中心 %s" % server)
        try:
            j = json.loads(body)
            if j:
                ok("健康检查响应：%s" % json.dumps(j, ensure_ascii=False)[:120])
        except ValueError:
            pass
        return True
    except Exception as e:
        bad("连不上中心 %s" % server)
        print("         错误：%s" % e)
        print("         排查：① 中心服务是否启动？② 地址是否为 http://IP:8765（带 http://）？")
        print("               ③ 跨机器时中心要用 --host 0.0.0.0 启动；④ 防火墙/安全组放行 8765。")
        return False


def read_env() -> dict:
    """读取 agent.env 作为交互默认值（KEY=VALUE 格式，忽略 # 注释）。"""
    out = {}
    env_file = ROOT / "agent.env"
    if not env_file.exists():
        return out
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, val = line.partition("=")
        out[k.strip().upper()] = val.strip()
    return out


def save_env(cfg: dict) -> None:
    """配对成功后回写 agent.env（去掉一次性配对码，避免误复用）。"""
    lines = [
        "# Agent 端配置（setup.py 会读取这些作为默认值）",
        "# 配对码是一次性的，配对成功后会从此处删除。",
        "SERVER=%s" % cfg.get("server", ""),
        "NAME=%s" % cfg.get("name", ""),
        "EXECUTOR=%s" % cfg.get("executor", "shell"),
    ]
    (ROOT / "agent.env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def do_pair(server: str, name: str, code: str, executor: str) -> int:
    print("\n=== 3. 配对 ===")
    args = ["agent.py", "pair", "--server", server, "--name", name, "--code", code]
    if executor:
        args += ["--executor", executor]
    print("  执行：python3 %s" % " ".join(args))
    try:
        r = _run(args, timeout=25)
    except subprocess.TimeoutExpired:
        bad("配对超时（中心无响应）")
        return 1
    out = (r.stdout + r.stderr).strip()
    # 过滤结构化日志行，只显示人类可读输出
    lines = [ln for ln in out.splitlines() if not ln.lstrip().startswith('{"event"')]
    print("\n".join("  " + ln for ln in lines))
    if r.returncode != 0:
        bad("配对失败（退出码 %d）" % r.returncode)
        return 1
    ok("配对成功")
    return 0


def verify() -> int:
    print("\n=== 4. 配对后验证 ===")
    r = _run(["agent.py", "status"])
    print((r.stdout + r.stderr).strip())
    print()
    r2 = _run(["agent.py", "fetch"])
    out = (r2.stdout + r2.stderr).strip()
    # 取最后一行 JSON
    last = out.splitlines()[-1] if out else ""
    print("  fetch 返回：%s" % last[:200])
    try:
        j = json.loads(last)
        if j.get("ok") or "task" in j:
            ok("通信正常（fetch 可达中心）")
            return 0
    except ValueError:
        pass
    if "尚未配对" in out or "配对" in out:
        bad("凭证未生效")
    else:
        warn("fetch 返回非预期内容，请检查中心状态")
    return 1


def prompt(msg: str, default: str = "") -> str:
    suffix = " [%s]" % default if default else ""
    try:
        val = input("  %s%s: " % (msg, suffix)).strip()
    except EOFError:
        val = ""
    return val or default


def main() -> int:
    ap = argparse.ArgumentParser(description="Agent 端一键部署向导")
    ap.add_argument("action", nargs="?", default="deploy", choices=["deploy", "check"],
                    help="deploy=自检并配对（默认）；check=只做自检")
    ap.add_argument("--server", default=None, help="中心地址，如 http://192.168.1.10:8765")
    ap.add_argument("--name", default=None, help="Agent 名字（服务端已登记）")
    ap.add_argument("--code", default=None, help="服务端生成的一次性配对码")
    ap.add_argument("--executor", default=None, help="执行器：%s（默认 shell）" % "/".join(EXEC_CHOICES))
    args = ap.parse_args()

    defaults = read_env()
    server = args.server or defaults.get("SERVER")
    name = args.name or defaults.get("NAME")
    executor = args.executor or defaults.get("EXECUTOR") or "shell"
    code = args.code

    if args.action == "check":
        good = check_env(server)
        if (ROOT / "agent-cred.json").exists():
            verify()
        print("\n自检结束：%s" % ("环境正常" if good else "存在问题，请按上面提示修复"))
        return 0 if good else 1

    # deploy：先自检（连通性放到收集完参数后统一探测，避免重复）
    if not check_env(server, probe=False):
        bad("环境自检未通过，请先修复上面的问题")
        return 1

    # 若已配对且用户没给配对码，询问是否重新配对
    if (ROOT / "agent-cred.json").exists() and not code:
        ans = prompt("已配对，是否用新配对码重新配对？(y/N)", "N")
        if ans.lower() not in ("y", "yes"):
            print("\n跳过配对，直接验证现有凭证：")
            return verify()

    # 收集配对参数
    print("\n--- 请填写配对信息（服务端管理员用 ctl.py pair 生成配对码）---")
    if not server:
        server = prompt("中心地址（含 http:// 和端口）", "http://127.0.0.1:8765")
    if not name:
        name = prompt("Agent 名字", "agent-2")
    if not code:
        code = prompt("一次性配对码")
    if not code:
        bad("配对码不能为空")
        return 1
    if executor not in EXEC_CHOICES:
        warn("执行器 '%s' 非内置名，将作为自定义命令模板处理" % executor)

    if not probe_connectivity(server):
        bad("中心不可达，配对中止")
        return 1

    rc = do_pair(server, name, code, executor)
    if rc != 0:
        return rc

    save_env({"server": server, "name": name, "executor": executor})
    ok("配置已写入 agent.env（配对码已移除）")

    rc = verify()
    if rc == 0:
        print("\n" + "=" * 52)
        print("部署完成！接下来任选一种方式接活：")
        print("  ① 常驻进程：./start.sh           （macOS/Linux）")
        print("     Windows：  start.bat")
        print("  ② 提示词驱动：把 AGENT_PROMPT.md 第四节的提示词贴给智能体")
        print("=" * 52)
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(130)
