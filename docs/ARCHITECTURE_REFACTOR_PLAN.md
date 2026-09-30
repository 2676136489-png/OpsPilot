# OpsPilot 深度重构方案 v2

> 目标：后端架构 / Agent Runtime / API / 网络通信 / 数据流 / 安全机制 的全面重构。
> 本文是**方案文档，不含任何代码改动**。确认后再动工。

## 0. 扫描范围与方法

对下列路径做了逐文件只读审计（三路并行，共 ~120 个文件）：

| 区域 | 路径 | 结论 |
|---|---|---|
| Agent Runtime | `apps/backend/src/opspilot_backend/agent/`（11 文件，含 `streaming.py` 1097 行） | 名为 LangGraph，实为 7 节点规则链，无 checkpoint / interrupt / retry / 并行 |
| 数据层 | `db/` `models/` `repositories/` `services/` | 4 表裸 String，`session.py:19-23` Postgres 静默降级 SQLite，无 Alembic |
| API 层 | `api/v1/endpoints/`（11 个 router） | Controller 直写 SQL，无鉴权、无统一异常处理器、无统一信封 |
| 前端 | `apps/frontend/src/` | 20 条前后端契约不一致，含客户端伪造事件 |
| MCP | `apps/mcp-server/` | 8 个工具真实可运行，但后端零调用、未被接入 |
| 基础设施 | `docker-compose.yml` `infra/` `evals/` `simulator/` | 10 服务仅 3 个真需要；evals 全是同义反复 |

---

## 1. 现有架构问题

### P0 — 阻塞级（不修则上层设计全部落空）

**P0-1 Agent 恢复执行被绕过，结果硬编码**
`agent/streaming.py:972-1023` `_complete_recovery()` 不调用 `recovery.execute_recovery_plan`，而是 `await asyncio.sleep(0.2)` 后直接写 `verification.status = "passed"`。
后果：**「Recovery → Verification」闭环是假的**。审批通过后无论真实执行结果如何，永远显示验证通过。直接违反「不要制造假数据冒充真实运行」。

**P0-2 15 秒无人值守自动批准后门**
`streaming.py:202` `AUTO_APPROVE_GRACE_SECONDS = 15.0`，`:944` 处 sleep 后自动放行审批。
后果：高风险动作（restart_service / rollback_deployment）在无人操作时 15 秒后自行执行。直接违反「高风险工具必须经过 Human Approval」。

**P0-3 风险分级从未被消费**
`agent/recovery.py:308-310` 是唯一强制审批点（risk=critical），但 streaming 路径 `:862-869` 只读 `plan.steps` 从不读 `risk_level`。
后果：**Risk Check 这一环在真实执行路径上不存在**。

**P0-4 Agent 状态全内存，进程重启即失忆**
`streaming.py` 的 `_RUNS` / `_QUEUES` / `_EVENT_BUFFERS` 是三个模块级 dict。无 checkpoint、无持久化、无断线重放。
后果：Agent Run 不可回放、不可审计、不可重连；多副本部署直接不可用。

**P0-5 `_current_scenario` 模块级全局污染**
`agent/tools.py:31` 全局单例，`:37-38` `set_scenario()` 改写它。
后果：并发两个 incident 调查时，后一个 scenario 覆盖前一个，第二个调查查到的是第一个的故障数据。

**P0-6 名为 Agent，实为无 LLM 的规则引擎**
全仓无任何 LLM client 调用（`agent/nodes.py` 是纯 if/else + 模板字符串）。`rag.py` / `postmortem.py` 是无任何调用方（全仓零引用）。
后果：简历上「Agent 开发」这一条站不住；也无法体现 hypothesis 生成/评估。

### P1 — 架构级

**P1-1 Postgres 静默降级**
`db/session.py:19-23`：URL 以 `postgresql+asyncpg` 开头就**强制改写**成 SQLite。
后果：配了 Postgres 也永远连不上，且无任何告警。

**P1-2 分层被击穿**
- `services/incident.py:13` Service 层 `import fastapi` 并抛 `HTTPException`（11 处）→ 业务层污染 HTTP。
- `api/v1/endpoints/deployments.py:44-50`、`health.py:22-24` Controller 直写 SQL → 违反「Controller 不直接操作数据库」。

**P1-3 双工具注册表，Schema 是装饰品**
`tools.py:186` `TOOL_REGISTRY` 写了完整 JSON Schema，但 `:155` `call_tool` 派发走另一个 `_MOCK_TOOLS`（`:105`）。
后果：Schema 从不校验；两套实现随时漂移。另有 5 个 handler 恒返回 `ok: True`（假成功）。

**P1-4 SSE 协议不完整**
`streaming.py:77-94` `to_sse()` 只发 `event:` / `data:`，**不发 `id:`**，不支持 `Last-Event-ID`、无序号、无重试间隔。
后果：断线即丢事件，无法重连补偿。

**P1-5 数据模型无约束**
4 张表，状态字段裸 `String` 无 Enum / 无 CheckConstraint；SQLite 下 `PRAGMA foreign_keys=0`，CASCADE 全失效。
后果：状态词汇在前后端各写一套，拼写漂移无法被发现。

**P1-6 零鉴权、零异常处理器**
全仓无 auth 依赖、无全局 exception handler。错误直接以 FastAPI 默认 422/500 裸格式抛出，与前端约定不一致。

**P1-7 无迁移**
无 Alembic，启动期 `create_all()`。表结构变更只能删库重来。

**P1-8 可观测性序列爆炸**
`main.py:109-111` in-flight gauge 用**原始请求路径**做 label → 带 UUID 的路径使 Prometheus 序列无界增长。全仓零 logging、零 OTel。

**P1-9 前端契约不一致（20 条，摘最严重）**
- 状态筛选点一次即 400（前端传的状态词后端不认）
- `AgentEvent.seq` 前端声明，后端从不产生
- `synthesiseBacklog()` **在客户端伪造事件**覆盖真实流
- `recovery_plan_created` 前端造空 plan → 流式期间恒显示「暂无恢复方案」

### P2 — 工程级

- `docker-compose.yml` 10 个服务，只有 3 个（backend / frontend / simulator）真被用到
- `apps/mcp-server` 的 healthcheck 路径写错，容器无法启动 → 后端从未成功连接过它
- `evals/` 100% 是断言自己输出的自证测试；`eval_report.json` 静态提交
- README 多处与代码不符（架构图、端点、工具数）
- 分页 `total` 忽略过滤条件；列表接口 N+1 懒加载

---

## 2. 重构方案

### 2.1 目标分层

```
Frontend (React 18 + TS strict)
        │  REST /api/v1  +  SSE
API Layer            — 路由、鉴权、请求校验、响应封装；禁止碰 DB
        │
Application Service  — 用例编排、事务边界、权限判定；禁止 import fastapi
        │
Agent Runtime        — LangGraph 17 阶段状态机；禁止碰 DB，只通过 Repository
        │
Tool Layer (MCP)     — 统一 Registry + Schema 校验 + 风险分级 + 执行链
        │
Infrastructure       — Adapter：GitHub / Metrics / Logs / Deploy / Runbook
        │
Repository Layer     — 唯一 DB 出入口（AsyncSession）
        │
Event Layer          — 领域事件 → 持久化 agent_events → SSE 广播
        │
Observability        — structlog JSON + OTel + Prometheus
```

**硬约束（写进 lint + CI）**
1. `api/` 不得 import `sqlalchemy` / `models`
2. `services/` 不得 import `fastapi`
3. `agent/` 不得 import `sqlalchemy`
4. 业务代码不得 import `github` / `httpx` 裸客户端，只能经 `infrastructure/adapters/`

### 2.2 三条与你的规格的偏差（必须提前确认，见 §8）

| 你的规格 | 现实约束 | 建议方案 |
|---|---|---|
| PostgreSQL | 发布平台单端口、无托管 DB；`docker-compose` 声明 PG 会触发 pre-check 拒绝 | ORM 按 PG 语义建模（Enum / CheckConstraint / JSONB→JSON），默认 SQLite 驱动，**`DATABASE_URL` 一改即切 PG**，Alembic 管迁移。不是假数据，是真实可移植 |
| Redis | 平台无 Redis | 抽象 `EventBus` 接口；`InProcessEventBus`（asyncio.Queue）为默认，`RedisEventBus` 可选。**SSE 重放读 DB 持久化的 `agent_events` 表**，不靠内存环形缓冲 |
| LLM 驱动推理 | 环境无可用 API Key | `LLMProvider` 协议 + `OpenAICompatibleProvider` + `DeterministicProvider`（无 key 时的真实兜底，并在 API 响应里显式标注 `reasoning_mode`）。**绝不伪造 LLM 输出** |

### 2.3 假数据清零清单

逐项删除/改造，每一条都会写测试证明它不再发生：

| 位置 | 现状 | 处置 |
|---|---|---|
| `streaming.py:_complete_recovery` | sleep 0.2s 硬编码 passed | 删除；改调 `RecoveryService.execute()` 真实执行 + `verify_service_health` 真实探测 |
| `streaming.py:AUTO_APPROVE_GRACE_SECONDS` | 15s 自动放行 | 删除常量；审批只能经 `POST /approvals/{id}/decision` |
| `tools.py` 5 个 `ok:True` handler | 恒成功 | 全部改为真实调用 Adapter，失败即 `ok:False` + error code |
| 前端 `synthesiseBacklog()` | 客户端伪造事件 | 删除；断线重连改用 `Last-Event-ID` 服务端重放 |
| `evals/` 同义反复 | 断言自己的输出 | 重写为契约测试：工具 Schema 校验、状态机迁移合法性、审批链强制、SSE 协议一致性 |
| `eval_report.json` 静态 | 手改 | 由 CI 生成 |

---

## 3. 文件变更计划

### 3.1 新建

```
apps/backend/src/opspilot_backend/
├── core/
│   ├── errors.py              # 统一错误码 + AppError 层次
│   ├── security.py            # API Key / JWT 依赖、Actor 提取
│   ├── logging.py             # structlog JSON 配置、request_id 上下文
│   ├── telemetry.py           # OTel tracer/meter 初始化
│   └── circuit_breaker.py     # 外部调用熔断
├── api/
│   ├── deps.py                # 鉴权 / 分页 / Actor / Idempotency-Key
│   ├── middleware.py          # request_id、访问日志、全局异常 → 统一信封
│   └── v1/
│       ├── router.py          # 汇聚全部 v1 路由
│       └── endpoints/         # 见 §6 路由表（现有 11 个重写）
├── application/               # ★ 新增层：用例编排
│   ├── incident_service.py
│   ├── investigation_service.py
│   ├── approval_service.py
│   ├── recovery_service.py
│   └── postmortem_service.py
├── repositories/              # ★ 唯一 DB 出入口
│   ├── base.py
│   ├── incident_repo.py
│   ├── agent_run_repo.py
│   ├── tool_call_repo.py
│   ├── evidence_repo.py
│   ├── hypothesis_repo.py
│   ├── approval_repo.py
│   └── audit_repo.py
├── domain/                    # ★ 纯领域，零框架依赖
│   ├── enums.py               # 全部状态/风险/阶段 Enum（单一词汇源）
│   ├── schemas.py             # Pydantic v2 领域模型
│   └── policies.py            # 风险判定、审批路由、幂等规则
├── agent/
│   ├── state.py               # 重写：分层 Pydantic 模型（见 §5.2）
│   ├── stages.py              # 17 阶段定义 + 迁移合法性矩阵
│   ├── graph.py               # 重写：StateGraph + checkpointer + interrupt + retry
│   ├── nodes/                 # ★ 拆目录，一阶段一文件
│   │   ├── context_loading.py
│   │   ├── signal_collection.py      # 并行工具（Send）
│   │   ├── evidence_aggregation.py
│   │   ├── hypothesis_generation.py
│   │   ├── hypothesis_testing.py
│   │   ├── deployment_correlation.py
│   │   ├── runbook_retrieval.py
│   │   ├── root_cause_analysis.py
│   │   ├── confidence_assessment.py
│   │   ├── recovery_planning.py
│   │   ├── risk_assessment.py
│   │   ├── human_approval.py         # interrupt()
│   │   ├── recovery_execution.py
│   │   ├── verification.py
│   │   └── postmortem_generation.py
│   ├── router.py              # 条件边 / 重试边 / 降级边
│   ├── checkpointer.py        # DB 持久化 checkpoint
│   ├── llm/
│   │   ├── provider.py        # 协议
│   │   ├── openai_compatible.py
│   │   └── deterministic.py
│   └── runtime.py             # 运行编排：超时、取消、失败恢复
├── tools/
│   ├── registry.py            # ★ 单一注册表，Schema 强制校验
│   ├── executor.py            # Policy→Permission→Risk→Approval→Execute→Verify
│   ├── risk.py                # 风险分级矩阵
│   ├── audit.py               # 工具调用审计落库
│   └── definitions/           # 14+ 个工具定义（见 §7）
├── infrastructure/
│   ├── http_client.py         # Timeout / Retry / Exp-Backoff / Request-ID
│   └── adapters/
│       ├── github.py
│       ├── metrics.py
│       ├── logs.py
│       ├── deployment.py
│       └── runbook.py
├── events/
│   ├── bus.py                 # EventBus 协议 + InProcess / Redis 实现
│   ├── types.py               # 16 种 SSE 事件类型（Enum）
│   └── sse.py                 # id: / retry: / Last-Event-ID 重放 / 心跳
└── models/                    # 17 张表（见 §4.3）
```

### 3.2 重写

| 文件 | 变更 |
|---|---|
| `agent/state.py` | TypedDict 27 扁平字段 → 分层 Pydantic 模型 |
| `agent/graph.py` | 单链 7 节点 → 17 阶段 StateGraph + checkpointer + interrupt |
| `agent/nodes.py`（~750 行） | 拆解为 `agent/nodes/` 15 个小文件 |
| `agent/streaming.py`（1097 行） | 拆解：`events/sse.py` + `agent/runtime.py` + `application/investigation_service.py` |
| `agent/tools.py` | 拆为 `tools/registry.py` + `tools/executor.py` + `tools/definitions/` |
| `db/session.py` | 删静默降级；驱动由 URL 决定，PG/SQLite 均受支持 |
| `models/*` | 4 表 → 17 表，状态改 Enum |
| `api/v1/endpoints/*`（11 个） | 全部改薄 Controller，业务逻辑下沉 Application |
| `services/*` | 去 fastapi 依赖，改抛领域异常 |
| `main.py` | 加 middleware / 异常处理器 / 修 gauge label |

### 3.3 删除

- `agent/streaming.py` 中的 `_complete_recovery` / `AUTO_APPROVE_GRACE_SECONDS` 相关分支
- `tools.py` 的 `_MOCK_TOOLS` 与 `_current_scenario` 全局
- 前端 `synthesiseBacklog()`
- `docker-compose.yml` 中 7 个未使用服务
- `evals/` 现有同义反复测试

### 3.4 迁移与测试新增

```
alembic/versions/0001_initial_17_tables.py
tests/
├── unit/                      # 领域模型、策略、工具 Schema
├── integration/               # Repository + Service + DB
├── contract/                  # API 契约（信封、错误码）+ 前端类型对齐
├── agent/                     # 状态机迁移合法性、审批强制、失败恢复、幂等
└── e2e/                       # 完整 incident 生命周期（真实执行，非 mock）
```

---

## 4. 数据流

### 4.1 端到端生命周期

```
POST /api/v1/incidents
  → IncidentService.create() → incidents 表 → 返回 incident_id
  → 发 domain event: incident.created

POST /api/v1/incidents/{id}/investigate
  → InvestigationService.start()
      ├─ 创建 agent_runs 记录（run_id, status=running）
      ├─ 建 LangGraph thread_id = run_id，挂 DB checkpointer
      └─ 后台任务启动图，立即返回 run_id（202）

Graph 执行（每个 node）：
  ├─ 前置：写 agent_steps(status=running, started_at)
  ├─ 工具调用 → ToolExecutor（§7.3 六步链）
  │     ├─ 落 tool_calls 表（含入参/出参/耗时/幂等键）
  │     └─ 落 audit_logs 表（actor, run_id, tool, args, result, risk）
  ├─ 产出 evidence / hypotheses → 对应表
  ├─ 每一步发 SSE 事件（同时持久化到 agent_events 表）
  └─ 后置：更新 agent_steps(status, output, ended_at)

HUMAN_APPROVAL 阶段
  ├─ node 调 interrupt(payload=approval_request)
  ├─ 写 approvals(status=pending, risk_level, expires_at)
  ├─ 图暂停（checkpoint 已落库）→ SSE: approval.required
  └─ 前端点「批准」→ POST /approvals/{id}/decision
       → ApprovalService.decide() → Command(resume=...) → 图继续
       ※ 没有任何定时自动放行路径

RECOVERY_EXECUTION
  → RecoveryService.execute()（真实调用 restart_service / rollback_deployment）
  → 每个 action 落 recovery_actions(status, result)
  ※ 若失败 → 边路由到 FAILED，不再伪造成功

VERIFICATION
  → verify_service_health 真实探测 → verification_results 表
  → passed → POSTMORTEM_GENERATION；failed → 回 HYPOTHESIS_TESTING（限次）

POSTMORTEM_GENERATION → postmortems 表 → CLOSED
```

### 4.2 写入路径的唯一性

**所有 DB 写入只发生在两处**：Repository 层（业务数据）与 Event 层（`agent_events`）。
Agent Runtime 拿不到 Session；它通过 `AgentRuntimeContext`（含 tool executor + event emitter + repository facade）间接访问，接口定义在 `agent/runtime.py`。

### 4.3 17 张表

| 表 | 关键字段 | 说明 |
|---|---|---|
| `incidents` | id, title, service, severity(Enum), status(Enum), detected_at, current_stage(Enum) | 主聚合 |
| `incident_events` | id, incident_id, event_type(Enum), payload(JSON), actor, created_at | 审计级时间线 |
| `services` | id, name, tier(Enum), owner, health(Enum) | |
| `service_dependencies` | id, service_id, depends_on_id, dependency_type(Enum) | |
| `agent_runs` | id, incident_id, status(Enum), current_stage(Enum), llm_mode(Enum), started_at, ended_at, error | |
| `agent_steps` | id, run_id, stage(Enum), status(Enum), input(JSON), output(JSON), attempt, started_at, ended_at | 含重试计数 |
| `tool_calls` | id, run_id, step_id, tool_name, arguments(JSON), result(JSON), status(Enum), duration_ms, idempotency_key(unique) | |
| `evidence` | id, incident_id, source(Enum), content, confidence, collected_at | |
| `hypotheses` | id, incident_id, statement, confidence, status(Enum: proposed/testing/confirmed/rejected), supporting_evidence_ids | |
| `runbooks` | id, title, service, risk_level(Enum), content | |
| `runbook_chunks` | id, runbook_id, chunk_index, content, embedding_ref | 供检索 |
| `recovery_plans` | id, incident_id, steps(JSON), risk_level(Enum), status(Enum), created_by | |
| `recovery_actions` | id, plan_id, tool_name, arguments(JSON), status(Enum), result(JSON), executed_at, executed_by | |
| `approvals` | id, incident_id, run_id, action_type(Enum), risk_level(Enum), status(Enum), requested_at, decided_at, decided_by, decision_note, expires_at | |
| `verification_results` | id, incident_id, run_id, checks(JSON), status(Enum), verified_at | |
| `postmortems` | id, incident_id, summary, timeline(JSON), root_cause, lessons, generated_at | |
| `audit_logs` | id, actor, action(Enum), resource_type, resource_id, run_id, tool_name, risk_level(Enum), payload(JSON), created_at | |

所有表统一：`id` (UUID) / `created_at` / `updated_at`。状态字段一律 Enum（`domain/enums.py` 单一词汇源），前后端共享生成。

---

## 5. Agent State Machine

### 5.1 17 个阶段

| # | 阶段 | 主要工具 | 输出 | 可能的下一跳 |
|---|---|---|---|---|
| 1 | `INCIDENT_CREATED` | — | incident 落库 | → 2 |
| 2 | `CONTEXT_LOADING` | get_service_status, get_service_dependencies | 服务画像 | → 3 / FAILED |
| 3 | `SIGNAL_COLLECTION` | query_metrics, query_logs（**并行 Send**）, get_recent_commits | 原始信号 | → 4 |
| 4 | `EVIDENCE_AGGREGATION` | — | evidence 去重/打分 | → 5 |
| 5 | `HYPOTHESIS_GENERATION` | (LLM) | hypotheses[proposed] | → 6 |
| 6 | `HYPOTHESIS_TESTING` | query_metrics, query_logs（按假设定向，并行） | hypotheses 置信度更新 | → 7 |
| 7 | `DEPLOYMENT_CORRELATION` | get_deployments, get_recent_commits | 变更关联证据 | → 8 |
| 8 | `RUNBOOK_RETRIEVAL` | search_runbooks, get_runbook | 候选处置方案 | → 9 |
| 9 | `ROOT_CAUSE_ANALYSIS` | (LLM) | root_cause + confidence | → 10 |
| 10 | `CONFIDENCE_ASSESSMENT` | — | 置信度裁定 | 高 → 11；低且 attempts<2 → **回 3**；否则 → 11 并标记 low_confidence |
| 11 | `RECOVERY_PLANNING` | create_recovery_plan | recovery_plan | → 12 |
| 12 | `RISK_ASSESSMENT` | (risk.py 矩阵) | risk_level | low → 14（跳过审批）；medium/high/critical → 13 |
| 13 | `HUMAN_APPROVAL` | `interrupt()` | approval 记录 | approve → 14；reject → FAILED；超时 → **保持 pending（不自动放行）** |
| 14 | `RECOVERY_EXECUTION` | restart_service / rollback_deployment | recovery_actions | 成功 → 15；失败 → FAILED |
| 15 | `VERIFICATION` | verify_service_health（可重试 + backoff） | verification_result | passed → 16；failed 且 attempts<2 → 回 6；否则 → FAILED |
| 16 | `POSTMORTEM_GENERATION` | create_github_issue（可选） | postmortem | → 17 |
| 17 | `CLOSED` | — | 终态 | — |

终态另有 `FAILED`（携带 error + 最后阶段），与 `CLOSED` 并列。

### 5.2 状态模型（分层 Pydantic，替换 TypedDict）

```python
class IncidentRef(BaseModel):        incident_id, service, severity, detected_at
class Investigation(BaseModel):      evidence[], hypotheses[], root_cause, confidence
class Execution(BaseModel):          tool_calls[], investigation_steps[], errors[]
class Recovery(BaseModel):           recovery_plan, approval_status, verification_result
class Meta(BaseModel):               run_id, current_stage, attempt, timestamps{}, llm_mode
class IncidentState(BaseModel):      incident, investigation, execution, recovery, meta
```

**传输层水位字段（`_emitted_evidence_count` 等 7 个）从 State 中彻底移除**，改由 Event 层的 cursor 管理。State 只承载领域事实。

### 5.3 图能力矩阵

| 能力 | 实现 |
|---|---|
| 条件路由 | `router.py` 中 `add_conditional_edges`，路由函数只做纯判定 |
| Retry | 节点级 `RetryPolicy(max_attempts=3, backoff=exp)` + 阶段级自定义（第 10/15 步的回跳限次） |
| Timeout | `runtime.py` 每节点 `asyncio.wait_for`，超时写 `agent_steps.status=timeout` |
| Checkpoint | `checkpointer.py`：DB 持久化，thread_id = run_id；进程重启可 `get_state` 恢复 |
| Interrupt / HITL | 第 13 阶段 `interrupt()`；恢复走 `Command(resume=...)`，仅由 Approval API 触发 |
| Failure Recovery | 统一 `on_error` 边 → `FAILED`，落 `agent_runs.error` + `audit_logs` |
| 幂等 | 工具调用带 `idempotency_key`（工具名+参数哈希），`tool_calls` unique 约束；重入直接返回上次结果 |
| 并行工具 | 第 3/6 阶段用 `Send` 派发多个工具任务，结果聚合 |
| 持久化 | 每个 node 的输入输出写 `agent_steps`；事件写 `agent_events` |

### 5.4 不暴露 Chain-of-Thought

SSE 只发**结构化可解释事件**。LLM 的原始思考不落库、不下发；对外只有 `hypothesis.created` / `hypothesis.updated` / `diagnosis.updated` 这类带字段的事件。

---

## 6. API 设计

### 6.1 统一响应信封

```jsonc
// 成功
{ "success": true,  "data": {...}, "error": null,
  "request_id": "req_...", "timestamp": "2026-..Z" }
// 失败
{ "success": false, "data": null,
  "error": { "code": "INCIDENT_NOT_FOUND", "message": "...", "details": {} },
  "request_id": "req_...", "timestamp": "2026-..Z" }
```

列表额外包一层：`data = { items: [...], total, page, page_size }`。

### 6.2 错误码

| HTTP | code 前缀 | 场景 |
|---|---|---|
| 400 | `BAD_REQUEST` | 参数/JSON 非法 |
| 401 | `UNAUTHORIZED` | 缺/坏凭证 |
| 403 | `FORBIDDEN` | 权限不足（含审批权限） |
| 404 | `*_NOT_FOUND` | 资源不存在 |
| 409 | `CONFLICT` / `STATE_CONFLICT` | 状态迁移非法、幂等冲突 |
| 422 | `VALIDATION_ERROR` | Pydantic 校验失败 |
| 429 | `RATE_LIMITED` | 限流 |
| 500 | `INTERNAL_ERROR` | 未捕获异常（不泄漏细节） |
| 503 | `DEPENDENCY_UNAVAILABLE` | Adapter 下游不可用 |

### 6.3 路由表（`/api/v1`）

**Incidents**
`POST /incidents` · `GET /incidents`（分页+筛选+状态 Enum 校验）· `GET /incidents/{id}` · `PATCH /incidents/{id}` · `POST /incidents/{id}/investigate` · `GET /incidents/{id}/timeline` · `GET /incidents/{id}/evidence` · `GET /incidents/{id}/hypotheses` · `GET /incidents/{id}/root-cause`

**Agent Runs**
`GET /incidents/{id}/agent-runs` · `GET /agent-runs/{run_id}` · `GET /agent-runs/{run_id}/steps` · `GET /agent-runs/{run_id}/tool-calls` · `POST /agent-runs/{run_id}/cancel`

**Recovery & Approvals**
`POST /incidents/{id}/recovery-plan` · `GET /incidents/{id}/recovery-plan` · `POST /incidents/{id}/recovery/execute` · `GET /approvals` · `GET /approvals/{id}` · `POST /approvals/{id}/decision`

**Services / Runbooks / Tools**
`GET /services` · `GET /services/{name}` · `GET /services/{name}/dependencies` · `GET /runbooks` · `POST /runbooks/search` · `GET /tools` · `POST /tools/{name}/invoke`（`?dry_run=true` 支持）

**Postmortem / Audit**
`GET /postmortems/{incident_id}` · `GET /audit-logs`

**SSE**
`GET /stream/incidents/{id}` · `GET /stream/agent-runs/{run_id}`

**系统**
`GET /health` · `GET /health/ready` · `GET /health/live` · `GET /metrics`（Prometheus）

### 6.4 SSE 协议

```
id: 1042
event: tool.completed
retry: 3000
data: {"run_id":"..","stage":"SIGNAL_COLLECTION","tool":"query_metrics","status":"ok","duration_ms":213}

: heartbeat
```

- 每个事件带**单调递增 `id`** 且**持久化**到 `agent_events`
- 首帧发 `retry: 3000`
- 每 15s 发 `: heartbeat` 注释帧
- 断线：客户端带 `Last-Event-ID` 重连 → 服务端从 `agent_events` 按 id 重放，并补发 `agent.state_sync` 快照
- **Chain-of-Thought 不下发**

16 种事件：`agent.started` / `agent.thinking_started` / `agent.step.started` / `agent.step.completed` / `tool.started` / `tool.completed` / `tool.failed` / `evidence.found` / `hypothesis.created` / `hypothesis.updated` / `diagnosis.updated` / `recovery.plan_created` / `approval.required` / `approval.decided` / `recovery.started` / `recovery.completed` / `verification.started` / `verification.completed` / `agent.completed` / `agent.failed` / `agent.state_sync`

---

## 7. MCP / Tool 设计

### 7.1 工具清单（16 个，含你点名的 13 个）

| 工具 | 风险 | 说明 |
|---|---|---|
| `get_service_status` | low | 只读 |
| `query_logs` | low | 只读 |
| `query_metrics` | low | 只读 |
| `get_deployments` | low | 只读 |
| `get_recent_commits` | low | 只读 |
| `get_service_dependencies` | low | 只读 |
| `get_runbook` | low | 只读 |
| `search_runbooks` | low | 只读 |
| `analyze_blast_radius` | low | 只读，纯计算 |
| `verify_service_health` | low | 只读探测 |
| `create_recovery_plan` | medium | 生成计划，不执行 |
| `create_github_issue` | medium | 写外部系统 |
| `scale_service` | high | 变更基础设施 |
| `restart_service` | **high** | 变更基础设施，**必须人工审批** |
| `rollback_deployment` | **critical** | 破坏性，**必须人工审批** |
| `generate_postmortem` | low | 生成文档 |

### 7.2 单一注册表

`tools/registry.py` 是**唯一**注册点，注册即校验：
- 必须有 Pydantic 参数模型与返回模型（自动派生 JSON Schema）
- 必须声明 `risk_level`、`timeout_s`、`idempotent`、`side_effect`
- `call_tool` 只从这一个表派发，Schema 校验在入口强制执行
- 同一份定义同时导出给 MCP Server（`apps/mcp-server`），**消除现在的双实现**

### 7.3 执行链（六步，缺一不可）

```
1. Policy Check     — 该工具在当前 incident 阶段是否允许调用
2. Permission Check — actor 是否有权（角色 / scope）
3. Risk Check       — risk_level 判定，high/critical → 必须走 4
4. Human Approval   — 写 approvals(pending) → 中断 → 等 decision（无自动放行）
5. Execution        — 经 Adapter 执行，Timeout + Retry + Exp-Backoff + Request-ID
6. Verification     — 副作用类工具执行后强制 verify_service_health 并落 verification_results
```

每一步都写 `audit_logs`：who / run_id / tool / args / 时间 / 结果 / approval_id / risk_level。

### 7.4 MCP Server 接入

`apps/mcp-server` 从「零调用的独立服务」改为**后端 Tool Layer 的 MCP 视图**：后端以 MCP client 连接它（stdio），或前端工具调试页直连。二选一在后文 §8 决策。

### 7.5 Adapter 抽象

`infrastructure/adapters/` 五个 Adapter 封装全部外部依赖，`http_client.py` 统一提供 Timeout / Retry / 指数退避 / Request-ID 透传 / 结构化日志。业务代码不得直接 import 第三方 SDK。

---

## 8. 需要你拍板的 4 个决策

见对话末尾的选项卡片。

---

## 9. 执行分期

| Phase | 内容 | 验收 |
|---|---|---|
| P1 | 地基：enums / errors / logging / telemetry / http_client / config | 单测通过 |
| P2 | 数据层：17 表模型 + Alembic 0001 + Repository | 集成测试通过，PG/SQLite 双跑 |
| P3 | Tool Layer：registry + risk + executor + audit + 16 工具 + MCP 对齐 | Schema 校验测试 + 审批强制测试 |
| P4 | Agent Runtime：state 分层 + 17 阶段节点 + graph + checkpointer + router | 状态机迁移矩阵测试 |
| P5 | Application + API：services 去 fastapi、controllers 变薄、信封/错误码/SSE | 契约测试 |
| P6 | 安全：鉴权、审批链、审计、幂等 | 越权/绕过审批测试必须失败 |
| P7 | 前端适配：类型对齐、删伪造事件、Last-Event-ID 重连 | E2E 真实生命周期 |
| P8 | 可观测性 + 文档：OTel、Prometheus（修 label）、README 九章 | 部署 + 线上验证 |
