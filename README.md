# task-dispatch · 任务下发与 Agent 调度中心

服务端指派任务 → Agent 端配对领取 → 独立执行 → 回传结果。零第三方依赖（Python 标准库），已按 RSI 方法论重构成分层架构。

> **仓库简介**：A lightweight multi-agent task orchestrator. Submit a task, the dispatcher analyzes it and dispatches it to the most suitable specialized AI agent — such as Codex (coding), Claude Code (CC, engineering/complex reasoning), WorkBuddy (docs, reports, data analysis) — then aggregates and returns the final output.

> **第一次接触本项目？请先看 [《项目说明文档（初学者版）》](项目说明文档.md)** —— 从"这是什么"讲到"每一步怎么操作"，含网页/CLI 使用、功能自检、API 参考、排错 FAQ。本 README 只作速查卡。

## 快速开始

```bash
python3 server.py --port 8765        # 启动中心，页面 http://127.0.0.1:8765/
python3 ctl.py pair agent-1          # 登记 Agent，拿一次性配对码
python3 agent.py pair --name agent-1 --code <配对码>   # Agent 端换凭证
python3 agent.py start               # Agent 端启动，只领指派给自己的任务
python3 ctl.py assign agent-1 "生成日报" "echo hello"  # 下发任务
```

### 页面六大区块（完整可操作，不必碰命令行）

1. **Agent 管理中心** — 登记 / 生成配对码 / 查看在线与配对状态 / 撤销
2. **如何配对** — 三步向导，配对码与命令可一键复制
3. **下发任务** — 指定 Agent、标题、命令
4. **Agent 工作台** — 在页面里用配对码接入 → 领取指派任务 → 填结果回传，完整闭环可视化（凭证只存内存，刷新即失效）
5. **任务看板** — 每 2 秒自动刷新，看所有并发状态
6. **功能自检** — 点一个按钮，自动逐项真实验证全部功能，逐条显示 PASS/FAIL

CLI（`ctl.py` / `agent.py` / `submit.py`）作为等价入口完整保留；页面工作台与 CLI Agent 走的是同一套 HTTP 协议，可混用。

**服务器地址只在 `agent.py pair --server` 时填一次**，会存进凭证文件，`start`/领任务/回传都自动复用，所以 `start` 不用带地址。跨机器部署三要素：中心用 `--host 0.0.0.0` 启动 + pair 填中心真实 IP（不是 127.0.0.1）+ 放行端口。详见文档 12.5.0。

**接入真实 AI 工具**有两种姿势：
- **无人值守**：`python3 agent.py start --executor claude`（或 `codex` / `workbuddy` / `chatgpt`），Agent 常驻循环、自动领任务并调对应 CLI 执行。需装好对应 CLI 并登录。四条完整命令见[《项目说明文档》12.5.1](项目说明文档.md)。
- **提示词驱动**（WorkBuddy 桌面端等智能体，无需装 CLI）：智能体自己读提示词、自己干活，只用 `agent.py fetch` / `report` / `fail` 原子命令通信。可直接投喂的提示词见 [AGENT_PROMPT.md](AGENT_PROMPT.md)。

两种姿势共用同一套配对/领取/回传协议，可混用。

## 分层架构

| 文件 | 职责 |
|---|---|
| `config.py` | 集中配置 + 启动校验（环境变量，快速失败） |
| `errors.py` | 类型化错误 + error code + HTTP 状态映射 |
| `logging_util.py` | 结构化 JSON 日志 + request_id + 敏感字段脱敏 |
| `db.py` | SCHEMA + 幂等迁移 + 连接辅助（不持有全局） |
| `repository.py` | 仓储层：AgentRepo / TaskRepo / PairingRepo（只做数据访问） |
| `service.py` | 业务层：PairingService / TaskService / AgentService（纯业务，依赖注入） |
| `server.py` | 控制器：路由/鉴权/参数/响应；含兼容转发层 + `/api/selftest` |
| `selftest.py` | 功能自检：15 项在独立临时库上真实调用 service 逐项验证 |
| `agent.py` / `ctl.py` / `submit.py` | Agent 端 / 管理 CLI / 下发 CLI；agent.py 另含 fetch/report/fail/heartbeat 供提示词驱动 |
| `AGENT_PROMPT.md` | 可直接投喂给 WorkBuddy/Claude/Codex 的接入提示词（无需装 CLI） |
| `executors.py` | 可插拔执行器：把任务拼成 shell/claude/codex/workbuddy/chatgpt/自定义命令 |
| `chatgpt_run.py` | ChatGPT(OpenAI API) 桥接：纯标准库，零第三方依赖 |

铁律：控制器不写业务；服务层不依赖 HTTP；仓储层只做数据访问；事务通过注入的 `tx` 管理；错误在边界映射。

## 测试

```bash
python3 selftest.py                  # 功能自检：15 项逐条 PASS/FAIL（页面「功能自检」按钮同源）
python3 run_full_test.py             # 全量回归（7 Gate + 编译 + 迁移 + 启动冒烟）
python3 -m unittest discover -s tests -v   # 全部单元测试（当前 82 项）
```

功能自检 15 项覆盖：登记与配对码、凭证兑换、错码/复用/绑名拒绝、下发校验、未配对领取拒绝、定向领取、进展与完成回传、失败与 owner 校验、撤销即时失效、租约回收、并发零重复、旧库迁移。自检全程用独立临时库，绝不污染现有任务与 Agent 数据。

- `REFACTOR_PLAN.md`：两级拆解的重构计划（里程碑 + 原子任务 + gate_cmd）
- `reports/refactor-report.md`：重构报告
- `reports/full-test-latest.json`：最近一次全量回归结果

## 环境变量

`DISPATCH_DB`（库路径）、`DISPATCH_TOKEN`（管理面令牌，空=本地免鉴权）、`DISPATCH_LEASE`（租约秒）、`DISPATCH_PAIR_TTL`（配对码有效期）、`DISPATCH_ONLINE_WINDOW`（在线判定窗口）、`DISPATCH_HOST` / `DISPATCH_PORT`。

## 安全加固（v0.5）

管理面已支持完整的身份与权限体系，**生产务必不要加 `--allow-anon`**：

- **用户与登录**：`users`/`sessions` 表，密码 bcrypt（缺失回退 scrypt），会话 Cookie（HttpOnly+SameSite=Strict），连续 5 次失败锁定 5 分钟。
- **RBAC 三角色**：admin（全局+用户管理+审计）、operator（自己的 Agent/任务/配对）、viewer（只读）。非管理员只能看自己名下的 Agent 与任务（数据隔离）。
- **接入管理**：配对码 12 位、来源限流、Agent 审批、单 Agent IP 白名单、token 时效与轮换；现有 Agent 凭证保留不失效。
- **网络安全**：IP/CIDR 白名单（`--allow-cidr`）、全局/登录/配对限速、请求体上限、安全响应头、可选内置 TLS（`--cert/--key`）。
- **审计**：`audit_log` 记录登录、用户变更、任务下发、配对/撤销、越权拒绝；Web「审计日志」或 `python3 ctl.py audit` 查看。

首次生产启动（users 表为空）会在控制台打印初始管理员账号密码，登录后请立即改密。

用户管理命令：`python3 ctl.py user add/list/role/status/password/delete <...>`；审计：`python3 ctl.py audit --limit 100`。

详细部署见 `HTTPS与混合部署.md`，规划与差距分析见 `服务端安全加固规划.md`。

### Agent 凭证（设备侧，保持不变）

配对码一次性、默认 10 分钟有效、12 位随机；凭证 32 字节随机数，服务端只存 SHA-256、常量时间比对；撤销即时失效。执行器当前把 `payload.command` 当 shell 命令跑（`shell=True`），仅限本地可信环境；接真 Agent 只需替换 `agent.py` 的 `execute()`。

### Agent 分类与归档（v0.6）

不想让某些 Agent 一直显示在管理中心？用「归档」（虚拟删除）而不是「撤销」：

- **归档**：Agent 从主页隐藏、**停止领取任务**，但凭证保留、历史不删；随时可「恢复」重新显示并接活。撤销则是永久吊销凭证（不可逆）。
- **分类**：给 Agent 打标签（如 `生产` / `测试` / `闲置`），管理中心按分类分组显示，未分类的归到一组。
- **Web 操作**：Agent 管理中心每行有「归档 / 恢复」按钮和分类输入框（回车保存）；勾选「显示已归档 Agent」可临时查看归档的（带删除线）。
- **CLI 操作**：
  ```bash
  python3 ctl.py archive agent-1            # 归档（隐藏+停活）
  python3 ctl.py archive agent-1 --restore  # 恢复
  python3 ctl.py category agent-1 生产环境   # 设置分类
  python3 ctl.py category agent-1 ""        # 清除分类
  python3 ctl.py agents                      # 列表（归档的单独提示，不混入）
  ```
- **权限与审计**：归档/分类需 `agent.manage` 权限（admin/operator），非管理员只能操作自己名下的 Agent；每次操作写入 `audit_log`（`agent.archive` / `agent.unarchive` / `agent.category`）。
- 数据库 schema v3→v4 幂等迁移，现有 Agent 默认未归档、无分类，凭证与历史完全保留。
