# task-dispatch 重构报告

- 日期：2026-11-17
- 驱动：RSI 方法论（两级拆解 → 里程碑 → 原子任务 → Gate → 全量测试 → 报告），主会话人工坐实
- 范围：仅 `task-dispatch/`，根目录 RSI 引擎与 `workspace/mao` 未动
- 结论：**6 个里程碑全部验收通过，全量回归全绿**

## 一、重构前 → 重构后

| | 重构前 | 重构后 |
|---|---|---|
| 结构 | `server.py` 单文件 22KB，配置/DB/事务/业务/路由全混在一起 | 8 个模块分层：config / errors / logging_util / db / repository / service / server + 3 CLI |
| 配置 | 各模块直接读 `os.environ` | `config.py` 集中读取 + `validate()` 启动快速失败，frozen 数据类 |
| 错误处理 | 散落的 `ValueError/PermissionError` + 临时 dict | `errors.py` 类型化错误层次 + error code + 统一 HTTP 映射，未知异常不泄露细节 |
| 日志 | 标准库明文访问日志 | `logging_util.py` 结构化 JSON + request_id 贯穿 + 敏感字段自动脱敏 |
| 数据访问 | SQL 散落在 12 处业务流程里 | `repository.py` 三仓储（Agent/Task/Pairing）收敛全部 SQL |
| 业务逻辑 | 与 HTTP handler 耦合 | `service.py` 三服务（Pairing/Task/Agent），纯业务、依赖注入、不依赖 HTTP |
| 迁移 | 内嵌在 init_db，索引顺序踩过坑 | `db.py` 独立 SCHEMA + 幂等迁移 + schema_meta 版本，旧库补列顺序正确 |
| 测试 | 22 个端到端 | 62 个（22 回归基线 + 40 个分层测试），另有 run_full_test.py 全量入口 |

## 二、六里程碑验收

| 里程碑 | 内容 | Gate 结果 |
|---|---|---|
| M1 基础设施 | config / errors / logging_util | 13 测试 PASS |
| M2 数据层 | db / repository（含旧库迁移不丢数据） | 8 测试 PASS |
| M3 业务层 | service（配对/领取/回传/回收，抛类型化错误） | 11 测试 PASS |
| M4 控制器 | server.py 变薄，委托 service + 兼容转发层 + 结构化日志 | 原 22 测试全绿 + 4 API 测试 PASS |
| M5 CLI/页面 | 三个 CLI 与页面接口不变，真实子进程闭环验证 | 4 测试 PASS |
| M6 集成回归 | run_full_test.py 全量入口 + 端到端实测 + 报告 | 全量 PASS |

## 三、全量回归结果（reports/full-test-latest.json）

```
[编译]      PASS
[M1 基础设施] PASS  Ran 13 tests OK
[M2 数据层]   PASS  Ran 8 tests OK
[M3 业务层]   PASS  Ran 11 tests OK
[M4 控制器]   PASS  Ran 4 tests OK
[M5 CLI/页面] PASS  Ran 4 tests OK
[回归基线]    PASS  Ran 22 tests OK
[迁移]      PASS  旧库迁移数据不丢
[启动]      PASS  health ok + 页面三大区块在
全量回归：全部通过（共 62 个单元测试）
```

## 四、向后兼容契约（均未破坏）

- 原 22 个测试全程全绿；
- HTTP 端点路径与响应结构不变（页面与 CLI 依赖的全部端点）；
- CLI 用法不变：`ctl.py pair/agents/revoke/assign`、`agent.py pair/start/status`、`submit.py`；
- 现有 `dispatch.db` 数据不丢（实测历史 8 个 Agent 记录、任务状态完好）；
- 保持零第三方依赖，`python3 server.py` 直接可跑。

## 五、关键文件

- `config.py` / `errors.py` / `logging_util.py`：基础设施
- `db.py` / `repository.py`：数据层
- `service.py`：业务层
- `server.py`：控制器 + 兼容转发层（server_version 升到 0.4）
- `run_full_test.py`：全量回归入口
- `REFACTOR_PLAN.md`：两级拆解计划（里程碑 + 原子任务 + gate_cmd）
- `tests/`：62 个测试

## 六、遗留 / 说明

- 执行器仍是 `agent.py` 的 `execute()`（`payload.command` 走 shell），这是产品定位的演示级执行环节，接真 Agent 只需替换该函数——与本次分层重构无关，未改动。
- 兼容转发层（server 模块级的 `create_pairing_code` 等）是为不破坏 ctl.py 与既有测试保留的；新代码应走 `Services` 聚合入口。
