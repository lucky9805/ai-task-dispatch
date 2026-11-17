"""ChatGPT 接入适配器：把一条任务描述发给 OpenAI API，把回答打印到 stdout。

ChatGPT 网页/客户端本身没有像 codex exec、claude -p 那样的非交互执行入口，
所以要让"ChatGPT 当 Agent"接活，走的是官方 OpenAI API。这个文件就是那座桥——
只用 Python 标准库（urllib），不装任何第三方包，符合本项目零依赖的约定。

用法：
  export OPENAI_API_KEY=sk-...                 # 必填
  export OPENAI_MODEL=gpt-4o-mini              # 可选，默认 gpt-4o-mini
  export OPENAI_BASE_URL=https://api.openai.com/v1   # 可选，兼容各类代理/中转
  python3 chatgpt_run.py "任务描述"

在调度中心里，它已被做成内置执行器 chatgpt，所以通常不直接调它，而是：
  python3 agent.py start --executor chatgpt
  # 或下发任务时指定：--executor chatgpt

说明：
- 回答走 stdout，进度/错误走 stderr，与 codex exec 的行为一致，便于 Agent 回传。
- 需要有网络与有效 API Key；没有 Key 时给出友好错误而不是堆栈。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    prompt = " ".join(sys.argv[1:]).strip()
    if not prompt:
        prompt = sys.stdin.read().strip()
    if not prompt:
        print("用法: python3 chatgpt_run.py \"任务描述\"", file=sys.stderr)
        return 2

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("缺少 OPENAI_API_KEY：请先 export OPENAI_API_KEY=sk-... 再运行",
              file=sys.stderr)
        return 2

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    url = base + "/chat/completions"

    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + api_key,
    })

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"]
        print(text)
        return 0
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        print("OpenAI API 返回 HTTP %s: %s" % (e.code, detail[:500]), file=sys.stderr)
        return 1
    except Exception as e:
        print("调用 OpenAI API 失败: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
