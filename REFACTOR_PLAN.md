# task-dispatch 重构计划（RSI 方法论 · 人工坐实）

- 日期：2026-11-17
- 驱动方式：按 RSI Loop 方法论（两级拆解 → 里程碑 → 原子任务 → Gate → 修复 → 全量测试 → 报告），由主会话人工坐实，不点火 LLM
- 目标形态：工程化分层（按功能组织 + 控制器/服务/仓储三层）
- 范围：仅 `task-dispatch/`，根目录 RSI 引擎与 `workspace/mao` 不动

---

## 0. 现状通读结论

现有 `server.py`（22KB）是一个「全塞一格」的文件：配置、SQLite 连接与迁移、事务、配对逻辑、任务调度逻辑、HTTP 路由、鉴权全混在一起。`agent.py` / `ctl.py` / `submit.py` 三个 CLI、`index.html` 页面、22 个端到端测试已经能完整跑通「下发 → 配对 → 定向领取 → 执行 → 回传 → 撤销」闭环。

**必须保留的资产（不可回归破坏）**：
- 22 个测试全绿；
- 现有 HTTP 契约（页面与 CLI 依赖的所有端点及响应结构）；
- 现有 CLI 用法（`ctl.py pair/agents/revoke/assign`、`agent.py pair/start/status`、`submit.py`）；
- 现有 `dispatch.db` 数据（迁移必须幂等、向后兼容）。

**核心矛盾**：分层要拆文件，但测试直接引用 `server.create_pairing_code`、`server.DB_PATH`、`server.tx()`、`server._conn` 等模块级符号。因此分层采用**依赖注入**：新模块（db/repository/service）不持有全局状态，由 `server.py` 保留唯一的连接/配置来源并注入；`server.py` 同时保留公共函数作为兼容转发层。

---

## 1. 目标架构

```
task-dispatch/
  config.py          # M1 集中配置 + 启动校验 + 快速失败
  errors.py          # M1 类型化错误 + 错误码 + HTTP 状态映射
  logging_util.py    # M1 结构化 JSON 日志 + request_id
  db.py              # M2 schema + 幂等迁移 + 连接辅助（接受 conn，不持有全局）
  repository.py      # M2 仓储层：AgentRepo / TaskRepo / PairingRepo（只做数据访问）
  service.py         # M3 服务层：PairingService / TaskService / AgentService（纯业务，不依赖 HTTP）
  server.py          # M4 控制器：路由/参数/鉴权/响应 + 兼容转发层（业务委托 service）
  agent.py           # M5 Agent 端（接口不变）
  ctl.py             # M5 管理 CLI（接口不变）
  submit.py          # M5 下发 CLI（接口不变）
  index.html         # M5 页面（三大功能区不变）
  run_full_test.py   # M6 全量测试入口（RSI 风格）
  reports/
    refactor-report.md
  tests/
    test_config.py / test_errors.py / test_logging.py   # M1
    test_db.py / test_repository.py                     # M2
    test_service.py                                     # M3
    test_api.py                                         # M4（HTTP 契约）
    test_dispatch.py                                    # 原有 22 个（回归基线，不许改坏）
```

分层铁律：控制器不写业务逻辑；服务层不依赖请求/响应对象；仓储层只做数据查询；多步写入走事务；输入在边界校验。

---

## 2. 两级拆解

### 里程碑 M1 · 配置 / 错误 / 日志基础设施（纯新增，不改 server.py）
| 原子任务 | gate_cmd |
|---|---|
| T1.1 config.py：环境变量集中读取 + validate() 快速失败 | `pytest:tests/test_config.py` |
| T1.2 errors.py：类型化错误层次 + error code + HTTP 状态映射 | `pytest:tests/test_errors.py` |
| T1.3 logging_util.py：结构化 JSON 日志 + request_id 生成/注入 | `pytest:tests/test_logging.py` |

里程碑 Gate：`pytest:tests/test_config.py tests/test_errors.py tests/test_logging.py`

### 里程碑 M2 · 数据层（db + repository，纯新增）
| 原子任务 | gate_cmd |
|---|---|
| T2.1 db.py：SCHEMA + 幂等迁移（schema_version）+ init_db(conn) | `pytest:tests/test_db.py` |
| T2.2 repository.py：AgentRepo / TaskRepo / PairingRepo，接受 conn+tx | `pytest:tests/test_repository.py` |

里程碑 Gate：`pytest:tests/test_db.py tests/test_repository.py`

### 里程碑 M3 · 业务层 service（纯新增，不依赖 HTTP）
| 原子任务 | gate_cmd |
|---|---|
| T3.1 PairingService：生成/兑换/撤销配对码（哈希、一次性、过期、常量时间比对） | `pytest:tests/test_service.py` |
| T3.2 TaskService：下发/原子领取/心跳/进展/回传/租约回收 | `pytest:tests/test_service.py` |
| T3.3 AgentService：列表 + 在线判定 | `pytest:tests/test_service.py` |

里程碑 Gate：`pytest:tests/test_service.py`

### 里程碑 M4 · 控制器层重构（server.py 变薄，业务委托 service）
| 原子任务 | gate_cmd |
|---|---|
| T4.1 Handler 只做路由/参数/鉴权/响应，统一错误映射 | `pytest:tests/test_api.py` |
| T4.2 保留全部现有端点与响应结构 + 兼容转发函数 | `shell:python3 -m unittest discover -s tests` |
| T4.3 结构化日志 + request_id 贯穿请求 | `pytest:tests/test_api.py` |

里程碑 Gate：`shell:python3 -m unittest discover -s tests`（原 22 个必须全绿）

### 里程碑 M5 · CLI 与页面适配（接口不变，验证联通）
| 原子任务 | gate_cmd |
|---|---|
| T5.1 agent.py / ctl.py / submit.py 兼容新 server，命令接口不变 | `pytest:tests/test_cli.py` |
| T5.2 index.html 三大功能区端点联通 | `pytest:tests/test_cli.py` |

里程碑 Gate：`pytest:tests/test_cli.py`

### 里程碑 M6 · 整体集成与回归（合并验收）
| 原子任务 | gate_cmd |
|---|---|
| T6.1 run_full_test.py：从头跑所有 gate + pytest 全量 + 启动冒烟 + 数据迁移体检 | `shell:python3 run_full_test.py` |
| T6.2 端到端实测：下发 → 配对 → 领取 → 执行 → 回传 → 看板 → 撤销 | `shell:python3 run_full_test.py` |
| T6.3 输出报告 reports/refactor-report.md | `file:reports/refactor-report.md` |

里程碑 Gate：`shell:python3 run_full_test.py`

---

## 3. 硬约束（防回归破坏契约）

1. 每个里程碑结束必须 Gate 全绿才进入下一个；失败就地修复，不带着红账前进。
2. 原 `tests/test_dispatch.py` 22 个测试是回归基线，**任何时刻都必须全绿**；M4 重构 server.py 时通过兼容转发层保证。
3. 数据库迁移幂等、向后兼容；现有 `dispatch.db` 数据不丢。
4. HTTP 端点路径与响应结构、CLI 命令接口、页面功能区全部保持不变。
5. 保持零第三方依赖（分层用标准库实现），项目仍可直接 `python3 server.py` 运行。
6. Gate 只认 exit code；收尾用 `run_full_test.py` 从头重跑全部 gate，不看单点「都 passed」。

## 4. 验收标准

- `python3 run_full_test.py` 全绿；
- 原 22 个测试 + 新增分层测试全部通过；
- 真实启动服务，端到端跑通「下发 → 配对 → 定向领取 → 执行 → 回传 → 看板 → 撤销」；
- 报告落盘 `reports/refactor-report.md`。
