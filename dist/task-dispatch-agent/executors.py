"""可插拔执行器：把"一个任务"翻译成"一条命令"，不同开发工具只是模板不同。

这样 Claude Code / Codex / 自定义工具都能成为调度中心里的 Agent——配对、领取、
回传协议完全不用变，变的只是"领到任务后具体跑什么命令"。

内置执行器（--executor 的值）：
  shell      把 payload.command 当 shell 命令跑（默认，保持向后兼容）
  claude     Claude Code 非交互：claude -p "<任务>" --allowedTools "..."
  codex      Codex 非交互：codex exec --sandbox workspace-write "<任务>"
  workbuddy  WorkBuddy / CodeBuddy Code 非交互：codebuddy -p "<任务>" -y
  chatgpt    ChatGPT（OpenAI API）：python3 chatgpt_run.py "<任务>"（需 OPENAI_API_KEY）
  echo       只回显任务，不执行任何东西（演示/自检用，最安全）

任意自定义模板：直接把整条命令作为 --executor 的值传入，用占位符引用任务：
  {command}  payload.command 原值        {title}  任务标题
  {task}     标题+命令组成的任务描述      {prompt} 同 {task}，语义别名
  {executor} 当前执行器名                  {id}     任务 ID
  {{ / }}    无特殊含义，占位符用 str.format

例：--executor 'codex exec --sandbox workspace-write "{task}"'
    --executor 'my-agent run --prompt "{task}" --json'
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class Builtin:
    name: str
    template: str
    description: str


BUILTINS = {
    "shell": Builtin("shell", "{command}", "把 payload.command 当 shell 命令执行（默认）"),
    "claude": Builtin(
        "claude",
        'claude -p {task_q} --allowedTools "Read,Edit,Bash,Write,Glob,Grep"',
        "Claude Code 非交互模式（-p），预授权常用工具",
    ),
    "codex": Builtin(
        "codex",
        'codex exec --sandbox workspace-write {task_q}',
        "Codex 非交互模式（exec），允许改工作区文件，结果走 stdout",
    ),
    "workbuddy": Builtin(
        "workbuddy",
        'codebuddy -p {task_q} -y',
        "WorkBuddy / CodeBuddy Code 非交互模式（-p），-y 跳过授权确认",
    ),
    "chatgpt": Builtin(
        "chatgpt",
        'python3 chatgpt_run.py {task_q}',
        "ChatGPT（OpenAI API），需设 OPENAI_API_KEY；标准库实现，零依赖",
    ),
    "echo": Builtin("echo", 'echo {task_q}', "只回显任务，不执行（演示/自检）"),
}


def is_builtin(name: str) -> bool:
    return name in BUILTINS


def describe(name: str) -> str:
    """返回人类可读的执行器说明。"""
    if name in BUILTINS:
        b = BUILTINS[name]
        return "%s — %s" % (b.name, b.description)
    return "自定义模板 — %s" % name


def task_text(task: dict) -> str:
    """把任务拼成给 AI 工具的自然语言提示词。"""
    title = str(task.get("title") or "").strip()
    payload = task.get("payload") or {}
    command = str(payload.get("command") or "").strip()
    # payload 里还可能带额外的 prompt 字段：优先用它，其次用 command
    extra = str(payload.get("prompt") or "").strip()
    body = extra or command
    if title and body:
        return "%s：%s" % (title, body)
    return title or body


def build_command(executor: str, task: dict) -> str:
    """根据执行器名构造要执行的 shell 命令字符串。

    - 内置执行器用其模板；
    - 其他值本身当作命令模板（支持占位符）。
    未知占位符保持原样，便于排查；命令始终以字符串返回，由调用方决定怎么跑。
    """
    name = (executor or "shell").strip() or "shell"
    template = BUILTINS[name].template if name in BUILTINS else name

    payload = task.get("payload") or {}
    values = {
        "command": str(payload.get("command") or ""),
        "title": str(task.get("title") or ""),
        "task": task_text(task),
        "prompt": task_text(task),
        "task_q": shlex.quote(task_text(task)),
        "command_q": shlex.quote(str(payload.get("command") or "")),
        "id": str(task.get("id") or ""),
        "executor": name,
    }

    class _SafeDict(dict):
        def __missing__(self, key):  # 未知占位符原样保留，不抛异常
            return "{%s}" % key

    try:
        return template.format_map(_SafeDict(values))
    except (ValueError, IndexError):
        # 模板里有非法格式（如孤立 {），回退为原样，交由 shell 报错
        return template


def binary_of(executor: str) -> str | None:
    """返回执行器依赖的可执行文件名，供启动前做友好检查；无依赖返回 None。"""
    name = (executor or "").strip()
    if name in ("claude", "codex"):
        return name
    if name == "workbuddy":
        return "codebuddy"  # WorkBuddy CLI 的可执行名是 codebuddy（短名 cbc）
    return None


def missing_binary(executor: str) -> str | None:
    """若依赖的可执行文件不在 PATH，返回可读的错误提示；否则 None。"""
    import shutil
    binary = binary_of(executor)
    if binary and shutil.which(binary) is None:
        return ("执行器 %s 依赖的可执行文件「%s」不在 PATH 中，"
                "请先安装并确保它在环境变量里" % (executor, binary))
    return None


# 部分 CLI（如 codebuddy -p）在"未登录/鉴权失败"时把提示打到 stdout 却仍返回
# 退出码 0。仅凭 returncode 判定成功会把这类提示当成果回传，看板显示"已完成"
# 但实际什么都没做。这里识别输出中的已知失败信号，作为 exit=0 的补充判据。
# 用"整段输出就是一条错误提示"的强特征，避免误伤任务本身讨论这些词的正常输出。
_FAILURE_SIGNALS = (
    "authentication required",
    "please use /login",
    "please log in",
    "please sign in",
    "not logged in",
    "not authenticated",
    "you are not logged in",
    "command not found",
    "unknown command",
)


def detect_failure_signal(out: str) -> str | None:
    """识别'退出码为 0 但输出其实是失败提示'的情况。

    仅当输出很短（最多 5 行，说明是一句错误提示而非任务的真实产出）且命中
    已知失败信号时返回失败原因；否则返回 None 表示未检测到。
    """
    if not out:
        return None
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if not lines or len(lines) > 5:
        return None
    low = out.lower()
    for sig in _FAILURE_SIGNALS:
        if sig in low:
            return ("执行器报告失败（退出码为 0 但输出为错误提示，未真正执行任务）："
                    + out.strip()[:300])
    return None
