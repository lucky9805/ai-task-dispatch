# 智能体接入提示词（WorkBuddy / Claude Code / Codex 通用）

像 WorkBuddy、Claude Code、Codex 这类智能体，**不用安装任何专用程序**就能成为调度中心里的一个 Agent。原因是它们天生具备两样东西：

1. 能理解自然语言提示词（也就是本文件）；
2. 能执行 shell 命令、能用自己的能力完成任务。

所以"接入"这件事被拆成两层：

- **通信层**（领任务 / 回传结果）：交给 `agent.py` 的几个原子命令，它们输出单行 JSON，稳定好解析。
- **执行层**（真正干活）：由智能体自己完成 —— 读任务描述，用你（智能体）的能力去执行。

本项目默认这些命令在 `task-dispatch/` 目录下。下文把命令写成 `python3 agent.py ...`，若当前目录不对，请先 `cd` 到该项目目录，或使用绝对路径。

---

## 一、一次性配对（管理员先把配对码给你）

管理员在中心执行 `ctl.py pair <你的名字>` 会给你一个一次性配对码。你只需兑换一次：

```bash
python3 agent.py pair --server http://<中心IP>:8765 --name <你的名字> --code <配对码>
```

成功后凭证存进 `agent-cred.json`，**中心地址会记在里面，之后所有命令都不用再带 `--server`**。多 Agent 并行时各自用 `--cred-file <不同路径>` 区分。

可用 `python3 agent.py status` 确认配对状态与中心地址。

---

## 二、工作循环（这是你要反复执行的核心）

把下面这段当作你的行为准则，**严格按顺序循环**：

### 第 1 步：领任务

```bash
python3 agent.py fetch
```

- 返回一行 JSON。`task` 为 `null` 表示暂时没任务 → 等待几秒（如 5 秒）后重试第 1 步。
- `task` 不为 `null` → 记下 `task.id`、`task.title`、`task.payload`。其中：
  - `title` + `payload.command`（或 `payload.prompt`）就是你要完成的**任务描述**；
  - `payload.executor` 是中心建议的执行方式（shell / workbuddy / claude / codex / chatgpt），仅供参考，你用自己最擅长的方式完成即可。

### 第 2 步：执行任务

把你领到的任务描述当作一条指令，**用你自己的能力去完成**：读写文件、写代码、跑命令、查资料、生成文档……做完后整理出一段清晰的结果文本。

> 如果任务预估耗时较长（超过租约，默认 120 秒），执行期间定期调用一次保活：
> ```bash
> python3 agent.py heartbeat <task_id>
> ```

### 第 3 步：回传结果

成功：
```bash
python3 agent.py report <task_id> --result '这里是完成结果/摘要'
```

失败（确实做不了或执行出错）：
```bash
python3 agent.py fail <task_id> --error '这里是失败原因'
```

回传返回 `{"ok": true, ...}` 即完成。然后回到第 1 步继续领下一个任务。

---

## 三、行为规则（务必遵守）

1. **先配对，再领任务** —— 没配对 `fetch` 会返回 `{"ok": false, "error": "尚未配对..."}`。
2. **领到任务必须回传** —— 成功用 `report`，失败用 `fail`，不要领了不回传（否则租约超时后中心会回收重派）。
3. **只处理指派给你的任务** —— fetch 只会返回 `assignee` 是你名字的任务，别的 Agent 的任务你领不到，这是正常的。
4. **结果用简洁文本** —— `--result` 里放完成摘要/产物路径/关键结论，别塞超长内容。
5. **地址只填一次** —— 配对时写了 `--server`，后续命令都不用写；想确认跑 `agent.py status`。
6. **跨机器注意** —— 若你在另一台机器，配对时 `--server` 要填中心的真实 IP（不是 127.0.0.1），且中心要用 `--host 0.0.0.0` 启动、放行端口。

---

## 四、可直接复制的"投喂版"提示词

下面这段可以整段复制给 WorkBuddy / Claude Code / Codex，让它按上述规则接入：

```
你现在是一个任务调度中心的 Agent，名字是 <你的名字>。中心地址 <中心IP:端口>。
通信靠 task-dispatch 目录下的 agent.py（已配对，凭证就绪）。请严格执行：

1. 执行 `python3 agent.py fetch` 领取任务（输出单行 JSON）。
2. 若 task 为 null，等待 5 秒后重复第 1 步。
3. 若有 task：
   - 把 task.title 与 task.payload.command（或 prompt）作为任务描述；
   - 用你自己的能力完成它（读写文件、写码、跑命令等）；
   - 长任务期间定期执行 `python3 agent.py heartbeat <task.id>` 保活；
   - 完成后执行 `python3 agent.py report <task.id> --result '<结果摘要>'`；
   - 若失败，执行 `python3 agent.py fail <task.id> --error '<原因>'`。
4. 回传成功后回到第 1 步，持续工作。

规则：必须先配对再领取；领到必回传；只处理指派给你的任务；
--server 已在配对时记入凭证，不用再写；结果用简洁文本。
现在开始工作循环。
```

---

## 五、与"装 CLI 执行器"的区别

- **本方案（提示词驱动）**：智能体自己读任务、自己干，只用 `agent.py` 通信。适合 WorkBuddy 桌面端、任何能在对话里执行工具调用的智能体。**无需安装 CLI、无需登录额外账号**，只要你本来就在用这个智能体。
- **执行器方案（`--executor workbuddy`/`codex`/`claude`）**：`agent.py start` 常驻循环，领到任务后自动调用对应 CLI 的 Non-interactive 模式去跑。适合**无人值守**、7×24 自动接活的场景，但需要先装好相应 CLI 并登录。

两者用的是同一套配对/领取/回传协议，可以混用：同一个中心下，有的 Agent 用提示词驱动，有的用 CLI 执行器。
