# OpsPilot 产品级 + 工程级重构 — Phase 1 审计报告

> 审计范围：apps/backend (7045 行) · apps/frontend (2427 行) · simulator · apps/mcp-server · evals · infra
> 审计基线：`git` 工作区（2026-09-28）
> **本报告只做审计，未修改任何业务代码。**

---

## 0. 执行摘要

当前 OpsPilot 是一套**功能完整、工程基础扎实的 MVP**：后端 77 测试通过、CI 6 job 齐备、LangGraph 状态机 + SSE 流 + Approval + Recovery 闭环真实可用。

但它现在的形态是**「带 AI 功能的运维后台管理系统」**，而不是目标中的 **「AI Native Incident Command Center」**。差距集中在四个层面：

| 层面 | 现状 | 目标 | 差距级别 |
|------|------|------|---------|
| 信息架构 | 3 个平铺页面（总览/服务/审批） | 指挥中心首页 + 8 个领域页面 | **结构性重构** |
| 视觉系统 | Dark theme，22 个 CSS 分区，无 token 约束 | Light-first Design Token 体系 | **推倒重来** |
| 数据模型 | 4 张表，字符串枚举 | 17 张表，完整 FK/Index/Enum | **重建** |
| API 契约 | 混合 synthetic+DB 的 CRUD | 10 个领域 API + 统一响应/错误/Request ID | **重新设计** |
| Agent 架构 | 单一 7 节点诊断图 | Orchestrator 驱动 8 阶段流水线 | **重构** |

**关键结论：这不是打补丁能解决的，必须按 Phase 分层重建。** 但重建的**资产继承面很大**——agent 的规则引擎、evidence 模型、recovery 模板、SSE 机制、observability shim、runbooks、simulator 都值得保留并迁移，而不是丢弃。

---

## 1. 当前项目问题清单

### P0 — 阻断「产品可信度」的硬伤

| # | 问题 | 证据 | 影响 |
|---|------|------|------|
| P0-1 | **incidents 接口混入 synthetic 假数据** | `api/v1/endpoints/incidents.py:74-97` — DB 查询结果与 `list_synthetic_incidents()` 合并后返回，`total` 被改写为 `total + len(synthetic)` | 生产 API 返回前端无法区分真假的记录；数据库为空时永远"看起来有数据" |
| P0-2 | **Recovery 执行是纯模拟** | `agent/recovery.py:507-543` — `execute_recovery_plan` 只 `await asyncio.sleep(0.001)` 后返回成功，**不调用任何真实工具** | 声称的"执行恢复"实际上是空转，与 README 的"自动恢复"叙述不符 |
| P0-3 | **Approval 自动批准绕过人工** | `streaming.py` `AUTO_APPROVE_GRACE_SECONDS = 15.0` — approval 事件后 15s 无人响应即自动 approve | 高风险回滚无人确认也会执行，违反 Human-in-the-Loop 安全承诺 |
| P0-4 | **前端无路由** | `App.tsx:8-18` — `useState<PageName>` 手写三分支切换；`Dashboard.tsx:108` 用 `selectedIncident` 状态渲染详情 | 无法 deep-link、无法刷新保持、浏览器后退失效；新增 8 个页面无从挂载 |
| P0-5 | **Recovery 模板硬编码且与实际场景耦合错误** | `recovery.py:488-495` — `deadlock` 场景映射到 `database_pool_exhaustion` 的 mock 数据（注释自认 "no dedicated mock scenario"） | 死锁故障的验证指标是错的，可能误判"已恢复" |
| P0-6 | **无全局异常处理器 / 无统一错误响应** | `main.py` 全文无 `exception_handler`；endpoints 各自 `HTTPException(status_code=..., detail="...")`，detail 是裸字符串 | 前端只能拿到字符串，无法区分错误类型；无 trace_id 便于排查 |
| P0-7 | **Agent 触发依赖前端传入 scenario** | `IncidentDetail.tsx:39-42` 把 `scenarioOf(incident)` 传给后端；若前端不带则后端落回 placeholder | 后端无法自主决策，Agent 的"调查能力"被前端参数绑架 |

### P1 — 影响生产就绪度

| # | 问题 | 证据 |
|---|------|------|
| P1-1 | `_APPROVALS` / `_RUNS` / `_QUEUES` 全部进程内内存字典 | `recovery.py:341`、`streaming.py` — 多 worker 部署即失效，重启丢状态 |
| P1-2 | 无 Request ID / Trace ID 中间件 | `main.py:40-47` 只有 CORS |
| P1-3 | CORS `allow_methods=["*"]` + `allow_headers=["*"]` + `allow_credentials=True` | `main.py:41-47` — 通配+凭据组合是安全隐患 |
| P1-4 | `create_all` 建表，无 Alembic 迁移 | `main.py:24-25` — schema 无法演进 |
| P1-5 | 无分页总数之外的过滤（服务/时间范围/负责人） | `repositories/incident.py:46-60` |
| P1-6 | Agent run 无持久化，刷新页面即丢失历史 | `streaming.py` `_RUNS` 内存字典 |
| P1-7 | `agent.py` 审批查找 O(N) 线性扫描 | `agent.py:128-137` 注释自认 "O(N) is fine for the MVP" |
| P1-8 | 无 rate limiting / 无认证 | 全 API 匿名可写（含 DELETE incident） |

### P2 — 工程质量与体验细节

- `models/incident.py` 用字符串存枚举，`Literal` 校验只在 Pydantic 层，DB 层无约束
- `schemas/incident.py` 的 `ListResponse[T]` 用 `items/total/offset/limit`，而 REST 惯例是 `data/meta`
- 前端 `Dashboard.tsx` 30s 轮询 + `IncidentDetail` SSE，两套数据源无统一缓存层
- 前端 `index.css` 1007 行单文件，22 分区靠注释切割，无 token 命名规范
- `docker-compose.yml` 只配 `http://localhost:5173` 单一 CORS origin，与 config 里 5173-5182 范围不一致

---

## 2. 前端问题（2427 行）

### 2.1 依赖极简 → 目标需求全部缺失

```json
"dependencies": { "react": "^19.2.8", "react-dom": "^19.2.8" }
```

**没有**：路由（react-router）、数据层（TanStack Query / SWR）、动画（Motion/Framer）、图可视化（React Flow / dagre）、图表（Recharts / visx）、状态（Zustand / Jotai）、表单、日期、i18n 框架、图标库。

用户要求的 **6 个新页面 + 全部动效 + 拓扑图 + 实时图表** 都需从零引入依赖。这本身不是错误（少依赖=快），但**必须承认引入量很大**，需要一次性规划好，否则 Phase 2-12 会反复改依赖。

### 2.2 视觉：Dark theme，与 Light-first 目标完全相反

`index.css` 变量现状（举核心几个）：

```css
--bg-primary: #0a0b0e;      /* 近黑 */
--bg-secondary: #12141a;
--text-primary: #e8eaed;    /* 浅色文字 */
--accent-red / --accent-green / --accent-amber / --accent-blue
```

**22 个 CSS 分区**：Layout / Cards / Stat Cards / Badges / Services Grid / Tables / Buttons / Incident Detail / Agent Timeline / Confidence Meter / Evidence Card / Dropdown / Filter Bar / Timeline / Empty States / Alerts / Scrollbar / Recovery Steps / Loading / Verification。

问题不是"做得差"——分区很清晰、命名也合理。问题是：
- 没有 `--background / --surface / --surface-elevated` 三层表面体系
- 没有 `--border / --border-strong` 层级、没有 `--muted / --muted-foreground` 语义
- 颜色是**按组件命名**（`--accent-green`）而非**按语义命名**（`--success`），导致 severity/status/health 各自复制一套色板
- 无 radius/shadow/motion token
- Light theme 下这些值**全部失效**，必须整体重写

### 2.3 组件结构：耦合、无分层

| 文件 | 问题 |
|------|------|
| `App.tsx` | 既是路由又渲染 Layout，无 Outlet 模式 |
| `Dashboard.tsx` (372 行) | 数据获取 + 轮询 + 筛选 + 注入 + 详情切换 + 4 个子组件全塞一个文件 |
| `IncidentDetail.tsx` (355 行) | 启动 Agent + SSE 状态管理 + 审批 + 5 个面板，职责过载 |
| `AgentTimeline.tsx` (464 行) | SSE 订阅 + 事件去重 + 初始事件合成 + 副作用映射 + 渲染，5 件事一个文件 |
| `applyEventSideEffects` | 事件→run state 的合并逻辑藏在 UI 组件里，不可测试 |

### 2.4 无设计系统、无复用

- 每个页面各自内联 `style={{}}`（`IncidentDetail.tsx` 有 30+ 处内联样式）
- Badge/Status/Severity 各写一套（`Dashboard.tsx:322-330`）
- 无 Skeleton、无 Toast、无 Modal/Drawer 基元、无统一 Loading

### 2.5 缺失的交互能力

- 无 optimistic update
- 无全局错误边界（`ErrorBoundary`）
- 无 `prefers-reduced-motion` 处理（因为根本没有动画）
- 无键盘导航 / focus 管理（Dropdown 靠 `mousedown` 关闭，无 Esc）

---

## 3. 后端问题（7045 行）

### 3.1 分层现状（这部分做得对，值得保留）

```
api/v1/endpoints → services → repositories → models
                     ↑
              schemas (Pydantic v2)
```
分层清晰，Repository 模式规范，`services/incident.py` 只做编排。**这个骨架应保留。**

### 3.2 API 层问题

| 问题 | 位置 |
|------|------|
| synthetic/DB 混合污染 | `incidents.py:27-42, 74-97, 106-116` |
| 错误处理不统一（裸字符串 detail） | 全 endpoints |
| 无 `/api/v1/incidents/{id}/investigate` 等语义化动作端点 | `agent.py:36` 用的是 `/agent/incidents/{id}/start` |
| 路径前缀散乱：`/agent/runs`、`/simulator/*` 与 `/incidents` 平级 | `router.py` |
| 无统一响应包装（直接返回 model 或裸 dict） | `agent.py` 全部返回 `dict` |
| 无 API 版本协商、无 deprecation 头 | — |
| `metrics.py` 只有 15 行，是唯一"零包装"的合理例外 | ✓ |

### 3.3 Agent 层问题

**现状是一个单一 LangGraph 图**（`graph.py`）：
```
START → load_incident → classify → collect_evidence → analyze
      → generate_hypothesis → decide_next_step →(条件)→ diagnose → END
```

问题：
- **没有 Orchestrator 概念**。用户要求的 `Orchestrator → Investigation → Diagnosis → Recovery Planning → Approval → Execution → Verification → Postmortem` 八阶段流水线，现在只有前 4 个在 graph 里，后 4 个散在 `streaming.py` 里用 Python 代码串（`_request_approval` / `_complete_recovery`）。
- **State 是 TypedDict 而非 Pydantic**。`state.py:IncidentState(TypedDict, total=False)` — 全字段可选、无类型约束、`_scenario/_decision/_stagnation_count` 等内部字段混在同一个 dict 里。
- **节点间数据流是 mutable dict 拷贝**，`nodes.py` 每个节点都 `state.get(...)` 再写回，没有明确的输入/输出 Schema。
- **规则引擎藏在 `prompts.py`** (`classify_category` / `build_diagnosis`)，名字是 prompts 但内容是规则，误导性强。
- **Recovery 与 graph 解耦但耦合于 streaming**：`_STEP_GENERATORS` 是模板单一事实源（好），但触发链路埋在 `streaming.py` 1084 行里。

### 3.4 Recovery 安全问题（对照用户要求逐条核）

| 用户要求 | 现状 | 判定 |
|---------|------|------|
| Policy Engine | `_STEP_GENERATORS` 里有 `risk_level` + `requires_approval`，`critical` 强制 approval（`recovery.py:308-310`） | ⚠️ 部分：是硬编码表，非可配置策略 |
| Permission Check | **无**。任何人可 approve | ❌ |
| Risk Assessment | 有 4 级（low/medium/high/critical） | ✅ |
| Approval Gate | 有 `ApprovalRequest` + approve/reject API | ✅ 但被 15s 自动批准削弱 |
| Idempotency Check | `streaming.py` approve/reject 有幂等守卫 | ⚠️ partial |
| Audit Log | **无**。approval 后无审计记录，`_APPROVALS` 重启即丢 | ❌ |
| 工具白名单化 | `tools.py:TOOL_REGISTRY` 5 个工具 | ✅ 设计对 |
| 禁止 Agent → 任意 Shell/SQL/HTTP | 工具都是 mock 层，**无真实执行路径** | ⚠️ 安全是因为"什么都没真做" |
| Simulator = 测试环境 | simulator 是独立服务，mock_data 在 backend | ⚠️ 边界模糊（backend 直接 import mock_data） |

**结论：安全能力刚过半，且"安全"部分建立在"根本没真执行"之上。**

---

## 4. Agent 问题

### 4.1 八阶段流水线映射

| 目标阶段 | 现状实现 | 位置 |
|---------|---------|------|
| Orchestrator | ❌ 无 | — |
| Investigation | ✅ graph 节点 | `nodes.py:361` |
| Diagnosis | ✅ graph 节点 | `nodes.py:686` |
| Recovery Planning | ⚠️ 在 streaming 里调 `generate_recovery_plan` | `streaming.py:_request_approval` |
| Approval | ✅ 有模型+API | `recovery.py:328` |
| Execution | ❌ 纯 sleep 模拟 | `recovery.py:507` |
| Verification | ✅ verify_recovery | `recovery.py:578` |
| Postmortem | ✅ generate_postmortem | `postmortem.py` |

### 4.2 State 问题

`IncidentState` 混装了三种东西：
1. **领域数据**：evidence / hypotheses / root_cause / symptoms / logs / metrics
2. **流程控制**：status / step_count / max_steps / _decision
3. **内部记账**：_evidence_counter / _stagnation_count / _emitted_* / _tool_events

目标要求"结构化 State（Pydantic Schema）"，且每一阶段有明确 State。现在是**一个扁平 dict 走完全程**。

### 4.3 可观测性到 Agent 的映射

`observability.py` 有 11 个 metrics（`agent_runs_total` / `tool_calls_total` / `agent_duration_seconds` 等），`tools.py` 已接入。这部分**质量不错，应保留**，只是 metrics 名和新的领域模型（agent_runs/agent_events/tool_calls 入库）需要对齐。

---

## 5. 数据库问题

### 5.1 现状 vs 目标

**现状 4 张表**：`services` / `incidents` / `incident_events` / `deployments`

**目标 17 张表**：incidents / incident_events / services / service_dependencies / agent_runs / agent_events / tool_calls / evidence / hypotheses / runbooks / runbook_chunks / recovery_plans / recovery_actions / approvals / verification_results / postmortems / audit_logs

缺失 13 张。**其中最重要的是**：
- `agent_runs` / `agent_events` / `tool_calls` — 现在全在内存，是 P1-6 的根因
- `evidence` / `hypotheses` — 现在只在 SSE 事件流里存在，刷新即丢
- `runbooks` / `runbook_chunks` — 现在从磁盘 `runbooks/*.md` 实时加载，无索引
- `recovery_plans` / `recovery_actions` / `approvals` / `verification_results` — 全内存
- `audit_logs` — 完全缺失
- `service_dependencies` — 拓扑图的数据基础，完全缺失（目标 `/topology` 页面的依赖）

### 5.2 Schema 质量问题

| 问题 | 位置 |
|------|------|
| 枚举存字符串，DB 无 CHECK 约束 | `models/incident.py:80-82, 139` |
| `DateTime` 不用 `timezone=True`，靠手动 strip tzinfo | `models/incident.py:26-33` |
| 无 `service_dependencies` 关联表 | — |
| `incident_events.details` 是 JSON 无 schema | `models/incident.py:116` |
| 无 `created_by` / `updated_by` 审计字段 | 全表 |
| 无唯一约束表达业务规则（如同一 incident 只能有一个 active recovery_plan） | — |

### 5.3 迁移缺失

`main.py:24-25` 用 `Base.metadata.create_all`。现有库无法加列/改类型。17 张表的演进**必须**上 Alembic。

---

## 6. API 问题

### 6.1 目标 Domain API vs 现状

| 目标 | 现状 | 差距 |
|------|------|------|
| `/api/v1/incidents` | ✅ 有（但混 synthetic） | 需净化 |
| `/api/v1/services` | ✅ 有 | 需扩展（health/dependencies） |
| `/api/v1/agents` | ⚠️ `/agent/runs` 单数前缀 | 需重命名+扩展 |
| `/api/v1/evidence` | ❌ 无独立端点 | 新增 |
| `/api/v1/recovery` | ❌ 只有 agent 内嵌 | 新增 |
| `/api/v1/approvals` | ❌ 只有 `/agent/approvals/{id}/approve` | 新增列表/详情/决策 |
| `/api/v1/runbooks` | ❌ 无 | 新增 |
| `/api/v1/evaluations` | ❌ evals 是独立 CLI | 新增 |
| `/api/v1/topology` | ❌ 无（无 dependencies 表） | 新增 |
| `/api/v1/observability` | ⚠️ `/metrics` 单独挂 | 需归入 |

### 6.2 契约问题

- **无统一 `ApiResponse<T>`**：现在 `ListResponse[T]` 只用于列表，单对象直接返回 model，agent 端点返回裸 `dict`
- **无统一 `ErrorResponse`**：`{"detail": "str"}` 是 FastAPI 默认
- **无 Request ID / Trace ID**：请求无法串联
- **HTTP 状态码**：`agent.py:141,163` 用 400 表示"审批非 pending"（应为 409 Conflict）
- **无 OpenAPI tag 分组一致性**：tags 混中英
- **无版本化策略**：`/api/v1` 有前缀但无弃用机制

---

## 7. UX 问题

| 维度 | 问题 |
|------|------|
| **信息架构** | 3 页平铺，无层级。用户要在"总览"和"服务"之间来回跳才能拼出全貌 |
| **导航** | 顶栏 chip 式 tab，无侧边栏、无面包屑、无 URL 感知 |
| **首页** | 4 个 stat card + 2 个 panel，**不是指挥中心**：看不到实时性、看不到拓扑、看不到 Agent 在干什么 |
| **Incident Detail** | 三栏布局方向对（对应用户要求的左中右），但右栏 5 个面板堆叠，"Evidence" 缺失（证据只在 timeline 文本里） |
| **Timeline** | 纯文本行 + 可展开 JSON。无节点状态可视化、无 Agent Graph 动画 |
| **实时性表达** | 只有一个小圆点（`● 实时`），无"正在发生"的强视觉 |
| **空状态** | 有，但文案弱（"暂无状态数据"） |
| **错误恢复** | 部分页面有重试按钮，不统一 |
| **可访问性** | 无 ARIA、无 focus ring 设计、无 reduced-motion |
| **移动端** | 无响应式设计，三栏在窄屏直接挤压 |
| **反馈** | 无 Toast，操作成功/失败靠重新拉数据体现 |

---

## 8. 性能问题

| # | 问题 | 证据 |
|---|------|------|
| 1 | Agent 图节点收 state **副本**，大 evidence 列表反复深拷贝 | LangGraph 固有行为，节点内 mutable 修改需显式返回 |
| 2 | `rag.py:456` `_vector_store.chunks.index(chunk)` **在循环里做 O(N) 线性查找** → 检索整体 O(N²) | `search_runbooks` |
| 3 | `embed_text` 对每个 3-gram 做 256 次 MD5 → 一次 query 数千次 hash | `rag.py:114-127` |
| 4 | 前端 30s 全量轮询（`Dashboard.tsx:61`），无增量/无 ETag | — |
| 5 | SSE 环形缓冲 500 事件，重连全量重放 | `streaming.py:_EVENT_BUFFER_LIMIT` |
| 6 | 无 DB 连接池配置调优（`db/session.py` 默认） | — |
| 7 | 无 N+1 查询保护（`service` relationship 默认 lazy） | `models/incident.py:98` |
| 8 | 前端单 bundle，无 code splitting | 无路由自然无 split |

---

## 9. 安全问题

| 级别 | 问题 | 位置 |
|------|------|------|
| **高** | 无认证/授权，匿名可 DELETE incident、可 approve recovery | 全 API |
| **高** | 15s 自动批准绕过人工审核 | `streaming.py:AUTO_APPROVE_GRACE_SECONDS` |
| **高** | CORS 通配 + credentials | `main.py:41-47` |
| **中** | 无 rate limiting，`/agent/runs/{id}/stream` 可被无限连接 | — |
| **中** | Recovery 无 audit log，"谁批准了什么"不可追溯 | `recovery.py:_APPROVALS` |
| **中** | 无 Permission 检查，Agent 权限与用户权限不分离 | — |
| **低** | 无输入长度全局约束（部分字段有 `max_length`） | schemas |
| **低** | 无安全响应头（CSP / X-Frame-Options 等） | `main.py` |
| **✅** | 工具白名单化（`TOOL_REGISTRY` 5 个）、密钥走 env、`.gitignore` 覆盖 `.env*` | 做得好，保留 |

**唯一真正的"安全屏障"是"没有真实执行能力"——这在重构引入真实工具调用后会立刻变成最危险的缺口。**

---

## 10. 目标架构

### 10.1 分层架构

```
┌──────────────────────────────────────────────────────────────────┐
│  FRONTEND — React 19 + TS + Vite + Router + Query + Motion       │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  Design System (tokens/primitives)                         │  │
│  │  App Shell (Sidebar + Command Palette + Toast Host)        │  │
│  │  Pages: Command Center / Incidents / Incident Detail /     │  │
│  │         Topology / Agents / Approvals / Runbooks /         │  │
│  │         Evaluations / Observability                        │  │
│  │  Data Layer: TanStack Query + SSE bridge                   │  │
│  └────────────────────────────────────────────────────────────┘  │
└────────────────────────────┬─────────────────────────────────────┘
                             │ REST /api/v1/*  +  SSE
┌────────────────────────────▼─────────────────────────────────────┐
│  BACKEND — FastAPI                                               │
│  middleware: RequestID → Trace → ErrorHandler → CORS → RateLimit  │
│  api/v1: incidents services agents evidence recovery approvals    │
│          runbooks evaluations topology observability              │
│  schemas: ApiResponse[T] / ErrorResponse / Paginated[T]           │
└────────────────────────────┬─────────────────────────────────────┘
                             │
┌────────────────────────────▼─────────────────────────────────────┐
│  AGENT — LangGraph Orchestrator                                  │
│  OrchestratorState (Pydantic)                                     │
│    ├─ InvestigationState → InvestigationSubgraph (existing)      │
│    ├─ DiagnosisState     → DiagnosisNode                         │
│    ├─ RecoveryPlanState  → Planner (existing templates)          │
│    ├─ ApprovalState      → ApprovalGate (persisted)              │
│    ├─ ExecutionState     → Executor (tool-backed, idempotent)    │
│    ├─ VerificationState  → Verifier (existing)                   │
│    └─ PostmortemState    → Postmortem (existing)                 │
└────────────────────────────┬─────────────────────────────────────┘
                             │ Tool Gateway (whitelist + policy)
┌────────────────────────────▼─────────────────────────────────────┐
│  DATA — PostgreSQL (+ Alembic) · Redis                            │
│  17 tables · enum constraints · audit_logs                        │
└──────────────────────────────────────────────────────────────────┘
```

### 10.2 资产继承决策表

| 现有资产 | 决策 | 理由 |
|---------|------|------|
| `agent/evidence.py` 模型 | **保留+扩展** | Pydantic 定义规范，加 DB 映射即可 |
| `agent/nodes.py` 7 节点 | **保留** | 成为 Investigation 子图 |
| `agent/recovery.py` `_STEP_GENERATORS` | **保留** | 模板单一事实源，只把 risk 表改成可配置 policy |
| `agent/postmortem.py` | **保留** | 已可用 |
| `agent/rag.py` | **保留+优化** | 修 O(N²)，加 DB 索引 |
| `agent/streaming.py` | **重构** | 拆出 Orchestrator，SSE 泵保留 |
| `agent/state.py` TypedDict | **替换** | 改 Pydantic 分层 State |
| `agent/prompts.py` 规则引擎 | **重命名+保留** | 改名为 `classifier.py` |
| `observability.py` | **保留** | 质量好 |
| `api/v1/endpoints/incidents.py` | **重写** | 去 synthetic 混合 |
| `repositories/` 模式 | **保留** | 扩展到 17 表 |
| `models/incident.py` | **重写** | 4→17 表 + 枚举约束 |
| `index.css` | **重写** | Light token 体系 |
| `Dashboard.tsx` / `IncidentDetail.tsx` | **重写** | 指挥中心 / 详情页 |
| `AgentTimeline.tsx` 事件映射逻辑 | **抽离+保留** | 移到 `lib/agentEvents.ts` 可测试 |
| simulator / mcp-server / evals / runbooks | **保留** | 独立服务，只需对齐契约 |

---

## 11. 新的信息架构

```
OpsPilot — AI Native Incident Command Center

├── 🎯 Command Center  (/)                    ← 默认落地页
│   ├─ System Status Bar   : SLO / Active Incidents / Agent Load / MTTD
│   ├─ ACTIVE INCIDENTS    : 实时故障列表 + 一键 Investigate
│   ├─ SYSTEM TOPOLOGY     : React Flow 实时拓扑（节点状态动画）
│   ├─ AI ACTIVITY         : Agent 实时活动流
│   └─ LIVE METRICS        : 底部实时指标带
│
├── 🚨 Incidents  (/incidents)
│   ├─ List (filter/sort/severity/status/time)
│   └─ Detail (/incidents/:id)
│        ├─ Incident Header  : 标题/severity/status/SLO 影响/计时器
│        ├─ LEFT   : Incident Timeline（事件流）
│        ├─ CENTER : AI INVESTIGATION（动态 Agent Graph）
│        └─ RIGHT  : Evidence · Hypotheses · Diagnosis · Recovery Plan
│                     · Approval · Verification（可展开 Accordion）
│
├── 🗺  Topology  (/topology)
│   ├─ Full graph canvas (services + dependencies)
│   ├─ 状态叠加：Healthy / Degraded / Critical / Investigating
│   └─ 点击节点 → 侧栏 Service Inspector
│
├── 🤖 Agents  (/agents)
│   ├─ Runs list（status / scenario / duration / steps / outcome）
│   ├─ Run detail → Agent Execution Drawer（节点/工具/事件/token）
│   └─ Tool catalog（白名单工具 + 调用统计）
│
├── ✅ Approvals  (/approvals)
│   ├─ Pending queue（risk badge / plan preview / SLA 计时）
│   ├─ Decision（approve / reject + reason）
│   └─ History（审计：who / when / what / why）
│
├── 📚 Runbooks  (/runbooks)
│   ├─ 按 category 分组
│   ├─ 全文检索（复用 rag）
│   └─ Chunk 预览 + 被引用次数
│
├── 📊 Evaluations  (/evaluations)
│   ├─ 数据集（20 条 ground truth）
│   ├─ Run evaluation → 实时进度
│   └─ Report（accuracy / steps / duration / breakdown）
│
└── 📈 Observability  (/observability)
    ├─ Metrics（Prometheus 指标可视化）
    ├─ Agent 性能（duration / success rate）
    └─ 系统健康（DB / Redis / 队列）
```

---

## 12. 新的页面结构（组件树）

```
<AppShell>
├─ <Sidebar>            (logo · nav · collapse · system status dot)
├─ <TopBar>             (breadcrumb · env badge · clock · command palette ⌘K · user)
├─ <ToastHost>          (全局通知)
└─ <RouterOutlet>

/  → <CommandCenter>
     ├─ <SystemStatusBar>      (SLO · Active · Load · MTTD)  [数字动画]
     ├─ <ActiveIncidentsPanel> (list · severity · elapsed · Investigate)
     ├─ <TopologyCanvas>       (React Flow · 节点脉冲 · 边流动)
     ├─ <AgentActivityFeed>    (SSE 实时流 · 逐条滑入)
     └─ <LiveMetricsStrip>     (迷你图 · 实时更新)

/incidents → <IncidentList>
     ├─ <FilterBar>
     ├─ <IncidentTable>        (DataTable primitive)
     └─ <Pagination>

/incidents/:id → <IncidentDetail>
     ├─ <IncidentHeader>       (severity · status · SLO impact · timer)
     ├─ <ThreeColumnLayout>
     │   ├─ <IncidentTimeline>      (垂直时间轴 · 事件图标 · 时间分组)
     │   ├─ <AgentGraphView>        (当前阶段高亮 · 节点状态动画 · 工具调用气泡)
     │   └─ <InvestigationPanel>
     │        ├─ <EvidenceAccordion>
     │        ├─ <HypothesisList>       (confidence bar · evidence 引用)
     │        ├─ <RootCauseCard>        (category · confidence meter · reasoning)
     │        ├─ <RecoveryPlanCard>     (步骤 · risk badge · 折叠展开)
     │        ├─ <ApprovalGate>         (approve/reject · SLA)
     │        └─ <VerificationBanner>
     └─ <AgentExecutionDrawer>  (可呼出：完整 trace)

/topology → <Topology>
     ├─ <GraphLegend>
     ├─ <TopologyCanvas>       (全屏 · 缩放/平移/小地图)
     └─ <ServiceInspector>     (抽屉)

/agents → <Agents>
     ├─ <RunFilters>
     ├─ <RunTable>             (status · scenario · duration · steps)
     └─ <ToolCatalog>

/approvals → <Approvals>
     ├─ <PendingQueue>         (risk badge · plan preview · SLA 倒计时)
     └─ <ApprovalHistory>      (audit trail)

/runbooks → <Runbooks>
     ├─ <RunbookSearch>
     ├─ <CategoryTabs>
     └─ <ChunkList>            (preview · metadata · cited count)

/evaluations → <Evaluations>
     ├─ <DatasetSummary>
     ├─ <RunEvaluationButton>  (进度条 · 实时日志)
     └─ <EvaluationReport>     (metrics cards · breakdown charts)

/observability → <Observability>
     ├─ <MetricsGrid>          (counter/gauge/histogram 卡片)
     ├─ <AgentPerformanceChart>
     └─ <SystemHealthPanel>
```

---

## 13. Design System

### 13.1 Color Tokens (Light-first)

```css
:root {
  /* ---- Surfaces (3 层) ---- */
  --background:        #fafafa;   /* 页面底 */
  --surface:           #ffffff;   /* 卡片 */
  --surface-elevated:  #ffffff;   /* 弹层/抽屉，配更强阴影 */

  /* ---- Borders (2 级) ---- */
  --border:            #e5e7eb;
  --border-strong:     #d1d5db;

  /* ---- Text ---- */
  --foreground:        #0a0a0a;
  --muted:             #6b7280;
  --muted-foreground:  #9ca3af;

  /* ---- Semantic ---- */
  --primary:           #2563eb;   /* 主操作 蓝 */
  --primary-foreground:#ffffff;
  --agent:             #7c3aed;   /* Agent 专属 紫 */
  --agent-foreground:  #ffffff;
  --accent-ai:         #0891b2;   /* AI/cyan 辅助 */

  --success:           #16a34a;
  --warning:           #d97706;   /* 橙 */
  --critical:          #dc2626;   /* 红 —— 仅用于 severity */

  /* ---- Severity 映射（唯一允许用红/橙的地方） ---- */
  --sev-critical: var(--critical);
  --sev-high:     #ea580c;
  --sev-medium:   var(--warning);
  --sev-low:      var(--muted);

  /* ---- Status 映射（服务健康） ---- */
  --status-healthy:   var(--success);
  --status-degraded:  var(--warning);
  --status-critical:  var(--critical);
  --status-investigating: var(--agent);
}

[data-theme="dark"] { /* 可选，Phase 后期 */ }
```

**纪律**：`--critical` / `--warning` **禁止**用于普通强调；普通强调用 `--primary` / `--agent`。

### 13.2 其他 Token

```css
/* Typography */
--font-sans: 'Inter var', system-ui, sans-serif;
--font-mono: 'JetBrains Mono', ui-monospace, monospace;
--text-xs/sm/base/lg/xl/2xl/3xl + --leading-*

/* Spacing (4px 基准) */
--space-1..12  (4/8/12/16/20/24/32/40/48/64)

/* Radius */
--radius-sm: 6px; --radius-md: 10px; --radius-lg: 14px; --radius-full: 9999px;

/* Shadow (克制、柔和) */
--shadow-sm: 0 1px 2px rgb(0 0 0 / .04);
--shadow-md: 0 2px 8px rgb(0 0 0 / .06);
--shadow-lg: 0 8px 24px rgb(0 0 0 / .08);

/* Motion */
--dur-fast: 120ms; --dur-base: 200ms; --dur-slow: 320ms;
--ease-out: cubic-bezier(.16,1,.3,1);
--ease-in-out: cubic-bezier(.65,0,.35,1);
@media (prefers-reduced-motion: reduce) { /* 全部 → 0.01ms */ }
```

### 13.3 动效清单（对应需求的 15 类）

| # | 动效 | 实现 | 时长 |
|---|------|------|------|
| 1 | 页面进入 | 容器 fade + 子元素 stagger | 320ms |
| 2 | 数字平滑变化 | `useSpring` 插值 | 500ms |
| 3 | 状态变化 | 颜色/背景 crossfade | 200ms |
| 4 | Timeline 实时追加 | 新行 `y:8→0` + fade | 200ms |
| 5 | Tool Call 出现 | 缩放 + 滑入 | 180ms |
| 6 | Evidence 添加 | 卡片进入 + 高亮闪烁 | 240ms |
| 7 | Root Cause 生成 | 卡片展开 + 置信度条填充 | 400ms |
| 8 | Recovery Plan 展开 | 高度 auto + 步骤逐个进入 | 300ms stagger 60ms |
| 9 | Approval 状态 | 徽章翻转 + 颜色过渡 | 260ms |
| 10 | Recovery progress | 进度条 spring | 跟随数据 |
| 11 | Service health | 圆点脉冲（critical 时） | 无限循环 |
| 12 | Chart 更新 | 数据点补间 | 300ms |
| 13 | Agent graph 节点状态 | 边框发光 + 状态色过渡 | 300ms |
| 14 | Drawer/Modal | overlay fade + panel slide | 280ms |
| 15 | Toast | 滑入 + 自动消失 | 240ms |

全部走 `Motion`（`motion/react`），统一 `useReducedMotion()` 守卫。

### 13.4 Primitives 清单

`Button` `IconButton` `Badge` `SeverityBadge` `StatusBadge` `HealthDot` `Card` `Panel` `Accordion` `Drawer` `Modal` `Toast` `Tooltip` `Skeleton` `EmptyState` `ConfidenceMeter` `MetricCard` `DataTable` `FilterChip` `Tabs` `Timeline` `Spinner` `CommandPalette`

---

## 14. 重构优先级

### 分阶段计划（每阶段结束后等待确认）

| Phase | 名称 | 交付 | 风险 |
|-------|------|------|------|
| **1** | **审计（本报告）** | 14 项审计 + 目标架构 | — |
| **2** | **Design System 地基** | Token 体系、primitives、App Shell、路由、Query/Motion 接入。**所有页面暂时保持功能不变** | 低 |
| **3** | **数据层重建** | 17 表模型 + Alembic + repositories + 净化 API（去 synthetic） | **高**（迁移） |
| **4** | **API 契约统一** | `ApiResponse[T]` / `ErrorResponse` / RequestID / Trace / 10 个领域 API | 中 |
| **5** | **Agent Orchestrator** | 分层 Pydantic State + Orchestrator 图 + 8 阶段 + 事件持久化 | **高** |
| **6** | **Recovery 安全加固** | Policy/Permission/Risk/Idempotency/Audit + 工具网关 + 去自动批准 | **高** |
| **7** | **Command Center 首页** | 新 Dashboard（Status/Topology/Activity/Metrics） | 中 |
| **8** | **Incident Detail 重设计** | 三栏 + Agent Graph + Evidence Accordion | 中 |
| **9** | **Topology 页面** | React Flow + 状态叠加 + force layout | 中 |
| **10** | **Agents / Approvals / Runbooks / Evaluations / Observability** | 5 个页面 | 中 |
| **11** | **动效统一打磨** | 15 类动效 + reduced-motion + 性能预算 | 低 |
| **12** | **最终验收** | 全链路 e2e、测试补齐、README/截图、可访问性 | 低 |

### Phase 2 前置决策（需用户确认）

1. **路由库**：React Router v7 (data router) — 推荐
2. **数据层**：TanStack Query v5 + 原生 EventSource — 推荐
3. **动画**：`motion` (原 Framer Motion) — 推荐
4. **拓扑/图**：`@xyflow/react` (React Flow) + `dagre` 自动布局 — 推荐
5. **图表**：`recharts`（轻）或 `visx`（可控）— 倾向 recharts
6. **样式方案**：CSS 变量 + CSS Modules（保留现有 CSS 架构，不引 Tailwind）— 推荐；若愿意可换 Tailwind v4
7. **Phase 3 迁移策略**：A) 破坏性重建（清库） / B) Alembic 增量 / C) 双写过渡。**倾向 B**（保留现有 4 表数据，新增 13 表）

### 明确不做的事

- ❌ 不引入 Bootstrap / Ant Design / MUI（与 premium technical UI 目标冲突）
- ❌ 不做深黑赛博朋克 / 霓虹风
- ❌ 不用静态 JSON 或 `setTimeout` 假装功能（Simulator 与 Production logic 必须分离）
- ❌ 不在 Phase 2 之前动业务逻辑

---

## 附录 A — 关键文件索引

| 文件 | 行数 | 角色 |
|------|------|------|
| `agent/streaming.py` | 1084 | run 生命周期 / SSE / 审批 / 图执行 ⚠️ 最需重构 |
| `agent/mock_data.py` | 738 | 6 场景 mock 数据 |
| `agent/nodes.py` | 736 | 7 个图节点 |
| `agent/recovery.py` | 725 | 恢复计划/审批/执行/验证 |
| `agent/rag.py` | 505 | Runbook 检索 ✅ 保留 |
| `agent/postmortem.py` | 496 | 复盘生成 ✅ 保留 |
| `core/observability.py` | 241 | Metrics shim ✅ 保留 |
| `models/incident.py` | 146 | 4 表 ⚠️ 重写 |
| `api/v1/endpoints/incidents.py` | 136 | ⚠️ synthetic 混合 |
| `frontend/index.css` | 1007 | Dark theme ⚠️ 重写 |
| `frontend/AgentTimeline.tsx` | 464 | 事件映射 ⚠️ 抽离 |
| `frontend/Dashboard.tsx` | 372 | ⚠️ 重写 |

## 附录 B — 验证现状（审计期间实测）

- 后端 pytest：**77 passed**
- 前端 `npm run build`：**通过**
- Agent 端到端：**completed / bad_deployment / verification passed / 14 类事件**
- Ruff / mypy：CI 中配置，本地未复跑

---

**审计结束。等待确认后进入 Phase 2（Design System 地基）。**
