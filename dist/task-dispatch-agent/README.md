# 任务调度中心 · Agent 端部署包

这是**客户端（Agent 端）**的独立部署包。把它解压到另一台机器，配对一次，就能从中心领取指派给你的任务、执行、回传结果。

**零第三方依赖**：只需要 Python 3.7+（macOS / Linux / Windows 都行），不用 `pip install` 任何东西。

---

## 目录内容

| 文件 | 作用 |
|---|---|
| `agent.py` | Agent 主程序（配对 / 领任务 / 回传，全部命令都在这里） |
| `executors.py` | 执行器：决定领到任务后"用什么干活" |
| `chatgpt_run.py` | ChatGPT 执行器（走 OpenAI API，用不到可忽略） |
| `AGENT_PROMPT.md` | 提示词接入说明：让 WorkBuddy / Claude / Codex 这类智能体直接接活 |
| `setup.py` | **一键部署向导**：环境自检 + 配对 + 验证 |
| `start.sh` / `start.bat` | 一键启动"常驻自动接活" |
| `agent.env.example` | 配置模板 |

> 本包**不含**凭证、不含数据库、不含服务端代码。凭证会在配对时在本机生成（`agent-cred.json`），请妥善保管、不要外传。

---

## 部署三步走

### 前置：中心管理员先在服务端登记这个 Agent

在**中心服务器**上，管理员执行：

```bash
cd /path/to/task-dispatch
python3 ctl.py pair agent-2 --server http://<中心IP>:8765
```

记下打印出来的**一次性配对码**（形如 `K7M2-9QX4`）。同时确认中心是这样启动的：

```bash
# 跨机器必须监听所有网卡（默认 127.0.0.1 只有本机能连）
python3 server.py --host 0.0.0.0 --port 8765
# 并确保防火墙 / 安全组放行 8765 端口
```

### 第 1 步：解压并自检

```bash
unzip task-dispatch-agent-v1.zip
cd task-dispatch-agent
chmod +x start.sh               # macOS/Linux 首次需赋予执行权限（Windows 跳过）
python3 setup.py check          # 检查 Python、核心文件、中心连通性、配对状态
```

> 注：zip 解压后脚本的可执行权限可能丢失，所以要先 `chmod +x start.sh`。若嫌麻烦，也可以始终用 `bash start.sh` 来启动，效果一样。

### 第 2 步：配对（只需一次）

复制配置模板（可选，也可全靠交互输入）：

```bash
cp agent.env.example agent.env  # 然后按需编辑 SERVER / NAME / EXECUTOR
```

然后选择一种配对方式：

**方式 A —— 交互向导（推荐）：**
```bash
python3 setup.py
# 依次提示输入：中心地址、Agent 名字、一次性配对码、执行器
```

**方式 B —— 一条命令直接配对：**
```bash
python3 setup.py --server http://192.168.8.192:8765 --name agent-2 --code K7M2-9QX4 --executor shell
```

两种方式都会：检查连通性 → 配对 → 写入凭证 → 验证 `fetch` 能通。看到"部署完成！"即成功。

**关键：中心地址填真实 IP，不要填 `127.0.0.1`** —— 在 Agent 机器上，`127.0.0.1` 指的是 Agent 自己，不是中心。

### 第 3 步：开始接活（二选一）

#### 模式 ① 常驻进程 —— 无人值守、准实时

```bash
./start.sh              # macOS / Linux，轮询间隔 5 秒
./start.sh 2            # 2 秒轮询，秒级响应
start.bat               # Windows
```

进程常驻，每 N 秒问一次"有我的活吗"，领到就干、回传、继续。配合 `nohup` / systemd / supervisor 可长期运行：

```bash
nohup ./start.sh 5 > agent.log 2>&1 &     # 丢后台跑
```

#### 模式 ② 提示词驱动 —— 让 WorkBuddy / Claude / Codex 自己接活

把 [`AGENT_PROMPT.md`](AGENT_PROMPT.md) 第四节的"投喂版提示词"整段复制给那台机器上的智能体。它会自行循环 `fetch → 理解任务并干活 → report`。**无需装任何 CLI、无需额外登录**——只要你本来就在用那个智能体。

---

## 执行器怎么选（`--executor`）

领到任务后用什么干活，由执行器决定：

| 执行器 | 说明 | 前提 |
|---|---|---|
| `shell`（默认） | 执行任务里的 `payload.command` | 无，最通用 |
| `echo` | 只回显任务，不真执行 | 无，**演示/自检最安全** |
| `codex` | `codex exec` 非交互 | 本机装 Codex 并登录 |
| `workbuddy` | `codebuddy -p` 非交互 | 本机装 CodeBuddy CLI 并登录 |
| `claude` | `claude -p` 非交互 | 本机装 Claude Code 并登录 |
| `chatgpt` | 走 OpenAI API | 设 `OPENAI_API_KEY` |

**重要坑**：中心下发任务时若带了 `--executor`，任务里的执行器**优先级最高**，会覆盖你启动时的设置。如果那台机器没装对应 CLI，就会执行失败。解决办法：下发任务时别带 executor，或带 `echo` / `shell`。

---

## 常用命令速查

```bash
python3 agent.py status                       # 看当前配对信息、中心地址、执行器
python3 agent.py fetch                        # 领一个任务（返回 JSON，不执行）
python3 agent.py report <id> --result '结果'   # 回传成功
python3 agent.py fail <id> --error '原因'      # 回传失败
python3 agent.py start --once --dry-run       # 领一个任务只预览不执行（安全自检）
python3 setup.py check                        # 再次自检
```

---

## 常见问题

**Q：连不上中心 / `fetch` 报连接错误？**
按顺序查：① 中心服务在跑吗？② 地址带 `http://` 和端口了吗？③ 中心用 `--host 0.0.0.0` 启动了吗？④ 防火墙放行 8765 了吗？⑤ 填的是中心 IP 而不是 `127.0.0.1` 吧？跑 `python3 setup.py check` 会自动排查并提示。

**Q：配对码报错 / 提示已使用？**
配对码是一次性的，用掉即失效。请让中心管理员重新执行 `ctl.py pair <名字>` 生成一个新的。

**Q：能同时跑多个 Agent 吗？**
可以。两个办法：① 用不同的 `--cred-file` 指向不同凭证文件；② 或者干脆解压两份到不同目录。每个 Agent 用不同名字在中心登记即可。

**Q：任务领到了但执行失败？**
先看 `agent.py status` 的执行器，再对照上表。最常见原因是任务带了本机没装的 CLI 执行器（见上面的"重要坑"）。

**Q：Windows 上 `python` 还是 `python3`？**
安装 Python 时勾选 "Add Python to PATH"，一般用 `python`。把上面命令里的 `python3` 换成 `python` 即可；`start.bat` 已自动处理。

---

## 版本

见同目录 `VERSION` 文件。客户端与服务端通过 HTTP 协议通信，只要协议版本一致即可混用。
