# OpsPilot

**AI 事故响应系统** — 一个自己跑完「发现 → 调查 → 诊断 → 计划 → 审批 → 执行 → 验证 → 回滚 → 复盘」的 Agent 运行时。

线上地址：**https://opspilot-v2.app.workbuddy.host/**

> 打开后点「注入故障」→ 选一个场景 → 点「开始调查」，就能看着 Agent 一步步调工具、改主意、给出诊断。
> 想先读代码：从 `apps/backend/src/opspilot_backend/agent/graph.py` 和 `services/agent_stream.py` 入手。

---

## 它解决的是什么问题

值班工程师遇到告警时的真实流程是：看指标 → 翻日志 → 查最近部署 → 凭经验猜 → 试一下 → 反复。
每一步都要人做，而且中间过程不留痕：事后复盘时没人说得清当初为什么排除了某个假设。

OpsPilot 把这段流程交给一个**状态机驱动的 Agent**，并且要求它把每一步都落库：
调了哪个工具、拿到什么证据、提出哪些假设、哪些被否掉、为什么否掉、诊断置信度多少、恢复动作有没有生效。
用户可以实时看着它跑，也可以事后翻完整的调查账本。

关键约束是**不许编**。诊断允许输出 `UNKNOWN`，验证失败就是失败，恢复后指标没回基线就触发回滚。
一个会说「我没查出来」的系统，比一个总能给出漂亮答案的系统更适合放在生产旁边。

---

## 一次调查里到底发生了什么

15 个节点，跑在 LangGraph 的 `StateGraph` 上（`agent/graph.py`），每个节点声明自己的 stage、超时、预算和失败语义（`agent/node_spec.py`）。

| Stage | 节点 | 做什么 |
|---|---|---|
| `load_context` | `load_context` | 拉服务拓扑、依赖、当前健康态，建调查上下文 |
| `triage` | `triage` | 定级（SEV1–4）、圈定受影响面、划时间窗 |
| `investigation_planner` | `investigation_planner` | 规划要采哪些证据，而不是把工具全调一遍 |
| `parallel_investigation` | `parallel_investigation` | 并发跑工具调用，任一超时不阻塞其余 |
| `evidence_aggregation` | `evidence_aggregation` | 证据归一化、去重、标注可信度与时效 |
| `hypothesis_generation` | `hypothesis_generation` | 生成候选假设并给出置信度（模型参与） |
| `hypothesis_verification` | `hypothesis_verification` | **主动找反驳证据**，能证伪就退回重新规划 |
| `root_cause_diagnosis` | `root_cause_diagnosis` | 出根因、类别与把握程度；证据不足就如实弃权 |
| `recovery_planner` | `recovery_planner` | 生成分步恢复方案（含回滚点） |
| `risk_assessment` | `risk_assessment` | 按动作风险定审批策略 |
| `human_approval` | `human_approval` | 人在环闸门（LangGraph `interrupt`） |
| `recovery_executor` | `recovery_executor` | 逐步执行，每步记录是否真正改变了环境 |
| `rollback` | `rollback` | 执行失败或验证不通过时按方案回退 |
| `verification` | `verification` | 回查指标，判断是否真的恢复 |
| `postmortem` | `postmortem` | 生成复盘：时间线、证据链、被否假设、遗留风险 |

循环不是装饰。评测里 **12/12 次运行都发生了重新规划**，平均 4.9 轮，平均 18.9 步、14 个不同 stage。
一个只会一条道走到黑的流程不需要 15 个节点。

### 状态与断点

- `agent/state.py` 定义 `IncidentState`，节点只返回增量，不原地改。
- `agent/checkpointer.py` 把 LangGraph 的检查点落到项目自己的仓储层，所以一次运行可以在进程重启后继续，
  被人审批打断的流程也能从 `human_approval` 恢复而不是从头再来。
- `agent/budget.py` 给每次运行设 token / 工具调用 / 墙钟预算，越界即停并记明原因，不让一次异常调查拖垮服务。

---

## 工具层：22 个工具，每个都带权限和风险等级

工具定义在 `tools/registry.py`，声明式，不是散落的函数。

| 权限 | 数量 | 工具 |
|---|---|---|
| `read_only` | 10 | `get_service_status` `query_logs` `query_metrics` `get_deployments` `get_recent_commits` `get_dependencies` `search_runbooks` `get_runbook` `notify_oncall` `verify_service_health` |
| `mutate_infra` | 8 | `restart_service` `scale_service` `restart_redis` `flush_cache` `restart_postgres` `increase_pool_size` `clear_deadlock` `enable_circuit_breaker` |
| `destructive` | 2 | `rollback_deployment` `restart_postgres` |
| `write_external` | 2 | `switch_payment_provider` `create_github_issue` |

权限分四档而不是两档，是因为「改自己的基础设施」和「动外部支付通道」要走的审批和审计路径不一样——
一个可以自动执行，另一个必须有凭据和外部留痕。`rollback_deployment` 也算 `destructive`：
回滚会改变线上流量走向，它需要真正的回滚点，而不只是「再部署一次」。

每个 `ToolSpec` 还声明：`input_model` / `output_model`（pydantic 双向校验）、`timeout_s`、`max_retries`、
`risk_level`、可预期的 `error_types`、所属 `mcp_server`。

几个刻意的设计：

- **超时是每个工具的属性，不是全局常量。** 查日志 5 秒、重启服务 30 秒，用同一个数字要么误杀要么白等。
- **幂等键。** `tools/executor.py` 带请求指纹，重复调用返回首次结果而不是再重启一次服务（`tests/test_tool_idempotency.py`）。
- **钩子。** `tools/hooks.py` 在调用前后插审计和指标，写操作调用点无法绕过。
- **MCP。** 工具通过 MCP 协议暴露，Agent 侧只认 spec，换后端不用改节点代码。

---

## 恢复安全：分级审批 + 真的回滚

恢复动作按风险分级，策略在 `agent/recovery.py`：

| 风险 | 典型动作 | 行为 |
|---|---|---|
| `low` | 单实例重启、临时扩容 | 自动执行 |
| `medium` | 滚动重启、配置热更新、清缓存 | 自动执行，留审计 |
| `high` | 回滚部署、切换支付通道 | 挂起等人工批准（LangGraph `interrupt`），拒绝即终止 |
| `critical` | 数据变更、删除类动作 | 直接拦下，标记失败，不进入审批队列 |

执行完必须验证：`verification` 回查指标，没回基线就走 `rollback`。
评测里 `effective_action_rate` 和 `environment_fixed_rate` 分开统计，就是为了区分
「动作返回成功」和「环境真的被修好了」——很多恢复系统把两者混为一谈。

**诊断的诚实度是单独一项指标。** `diagnosis_honesty.false_diagnosis_rate` 统计「自信地给出了错误根因」，
`recovery.success_rate` 统计「宣称成功但环境仍然坏着」。这两项比根因准确率更能说明系统能不能信。

---

## 模型接入

模型走 provider 抽象（`agent/llm.py`），两个实现：

- `deterministic` — 规则引擎，不需要任何外部依赖，跑测试和评估时用。
- `openai_compatible` — 任何 OpenAI 兼容端点，通过 `OPENAI_BASE_URL` / `OPENAI_MODEL` / `OPENAI_API_KEY` 配置。

`get_llm()` 返回进程级单例，运行记录里的 `reasoning_mode` 如实反映当前用的是哪一个，
不是写死的常量——**运行元数据说谎比没有元数据更糟**。

提示词上的两条硬规矩：

1. **要求 JSON 就必须写明 JSON 的 schema。** 含糊地说「优化一下推理过程」，模型会回一段散文，
   解析失败、输出被丢弃，而 token 已经扣了。现在提示词显式规定「只回 JSON 数组、每项一个 `reasoning` 键、
   40 词内、不要 markdown 围栏」，并在「扣了 token 却解析失败」时记 `llm.unusable_reply` 事件。
   一次典型假设生成从 651 个 token 浪费掉，降到 147–170 个 token 正常落库。
2. **证据不足允许弃权。** `root_cause_diagnosis` 可以产出 `UNKNOWN`，这条路径计入 `escalated`，
   不算失败也不算成功。
3. **输出语言在提示词里锁死。** 界面是中文的，提示词不写死语言，模型就会按自己的语料习惯回英文句子，
   于是「中文界面里夹一段英文根因」——比整站英文更难读，因为它看起来像 bug。
   `hypothesis_generation` 和 `postmortem` 的 system prompt 都显式写了「只用中文作答」，
   并且限定「不要 markdown 围栏、不要任何说明文字」，把语言和结构一起约束掉。

---

## 评测：12 个场景，每个都能追到证据

`evals/` 是独立的一层，不依赖后端进程，直接跑运行时并出报告。

```bash
cd evals && python cli.py
```

最近一次结果（`evals/reports/`，也是前端「评估」页读的那份）：

| 指标 | 值 |
|---|---|
| 场景数 / 错误数 | 12 / 0 |
| 根因准确率 | **1.00** |
| 诊断类别准确率 | **1.00**（7 个类别全对：capacity / cascading / database / deployment / memory / redis / third_party） |
| 工具选择准确率 | **1.00** |
| 恢复成功率 | **1.00**（`effective_action_rate` 1.00，`rollback_rate` 0.00） |
| 验证准确率 | **1.00**（12 条计分，0 次「宣称成功但环境未恢复」） |
| 误诊断率 | **0.00**（12 次全部给出结论，0 次自信地错） |
| 证据召回 / 利用率 / 可追溯 | 0.701 / 0.292 / 0.332 |
| 平均步数 / 平均 stage 数 | 18.92 / 14.0 |
| 平均重新规划轮数 | 4.92（12/12 次都重规划过） |
| 工具调用总数 | 162（平均 13.5，失败 0 次） |
| 延迟 平均 / p50 / p95 / max | 1237 / 1193 / 1787 / 2099 ms |
| Trace | 12 次单一 root，0 个悬空 parent，平均 126 个 span，深度 5 |

根因、工具选择、恢复、验证四项是 1.00，说明在已定义的场景上闭环是稳的。
**证据利用率只有 0.29 才是真问题**：Agent 采到的证据里，有七成没被写进最终诊断的引用里。
这是下一步要改的地方——不是"低分"，是"还没查明白"。

---

## 可观测性

- **指标**（`core/observability.py`）：`agent_runs_total` / `agent_success_total` / `agent_failure_total` /
  `active_incidents` / `tool_calls_total` / `tool_failures_total` / `agent_duration_seconds` /
  `recovery_success_total` / `recovery_failure_total` / `verification_success_total` /
  `verification_failure_total`。`GET /api/v1/metrics` 直接输出 Prometheus 文本格式，`infra/prometheus/prometheus.yml` 已配好抓取。
- **链路**（`core/tracing.py`）：每次运行一棵 trace，节点、工具调用、模型调用都是 span，
  带 `run_id` / `stage` / `tool_name` / `model` 属性。评测里的 `trace_integrity` 断言每个 span 都能上溯到单一 root——
  这不是顺手做的检查，是因为一开始真有悬空 parent，图上多出几个孤岛却没人发现。
- **结构化日志**：`core/logging.py` 输出 JSON event，`log_event("llm.unusable_reply", ...)` 这类事件让
  「扣了钱没拿到结果」这种事在日志里能被数出来。

---

## 前端

React 19 + TypeScript + Vite，9 个页面（指挥中心 / 故障 / 服务拓扑 / Agent 运行 / 审批 / 运维手册 / 评估 / 可观测性 / 故障详情）。

- **SSE 事件流**。`GET /api/v1/agent/runs/{id}/stream` 从 `seq 0` 回放落库的完整事件日志，然后转到实时跟随。
  断线重连带 `Last-Event-ID`，服务端从游标续传，所以断连不会在时间线上留一个洞。
  已结束的运行照样能看完整历史——回放读的是数据库，不是内存里的环形缓冲。
- **时间线有两个页签且都只说真话**。「步骤」来自节点执行记录（含每次工具调用的耗时和结果），
  「事件」来自 SSE 事件日志。没有一份数据是从运行快照反推出来的——反推出来的时间线无法展示
  重新规划、被否掉的假设或回滚，而这三样恰好是证明 Agent 在思考而不是在背稿的地方。
- **拓扑图**用 dagre 做分层布局，节点颜色只编码健康度；健康度未知就画成灰色，不画成绿色。
- **文案分层**：接口里的枚举值、工具名、指标名、服务名、厂商名一律保持英文原样
  （`ROOT_CAUSE_CONFIRMED`、`latency_p95`、`acme-pay`），因为它们要么参与匹配、要么是别人系统的名字，
  翻一次就会有两处各自为政的真话。给用户看的句子则全部走中文词表：后端 `domain/enums.py` 的
  `zh_incident_status()` / `zh_risk()` / `zh_outcome()`，前端 `lib/labels.ts` 与 `i18n.ts`。
  同一份词表只放一处——两个页面各自翻译同一个枚举，是两处迟早会分歧的地方。
- **框架自己的英文也要收**：被拒的请求默认返回 pydantic 的 `Input should be a valid UUID...`，
  而 `api/client.ts` 有一段专门把这些 `msg` 拼起来渲染的分支——也就是说这句话有通路到屏幕上，
  只是 SPA 自己不会走那条路。"不常出现"正是一句英文能在中文界面里活下来的方式。
  现在 `main.py` 注册了 `RequestValidationError` 处理器：`detail` 换成中文句子，
  结构化的 `type`/`location` 原样留在 `errors` 里——那是定位字段的依据，翻译它等于删掉信息。
- **主题与交互**：设计 token 集中在 `styles/tokens.css`，命令面板（⌘K）、抽屉、Toast、下拉都走 `ui/` 下的统一实现，
  页面不自己手搓浮层。

---

## 部署：单端口托管

线上跑的是 `scripts/build_deploy.py` 打出来的一个单元：

```
deploy/
├── src/            # 后端 + 内嵌模拟器
├── webroot/        # 编译好的 SPA
├── runbooks/       # 知识库
├── opspilot.db     # SQLite（只带 schema，不带数据）
├── requirements.txt
└── serve.py        # 入口
```

`opspilot.db` 是**空表 + 完整 schema**，并在头部写入本次构建的时间戳。首屏那一条故障由
`OPSPILOT_DEMO_SEED` 在启动时按场景定义生成，文案跟着 `opspilot_simulator.scenarios` 走。
启动时会先核对那个戳：托管平台的上传是覆盖式的，SQLite 的 `-wal` 会让上一版的数据库接管新文件，
而两者的 schema 完全相同、`integrity_check` 也都是 `ok`——只有戳能分辨。
不匹配就把 `.db` 连同 `-wal`/`-shm` 一起删掉重建（库是可再生的），然后把戳补写回去。

`serve.py` 用一个 FastAPI 进程同时提供 API、SSE、模拟器和 SPA（catch-all 回落到 `index.html`）。
`OPSPILOT_EMBED_SIMULATOR=true` 会把故障模拟器挂到 `/__sim` 并把基础设施 provider 指回自己，
所以托管环境不需要第二个进程。

```bash
python scripts/build_deploy.py     # 需要先跑过前端 build
cd deploy && python serve.py
```

> 托管平台会把入口猜成 `python main.py`。显式指定 `startCmd="python serve.py"`、
> `installCmd="pip install -r requirements.txt"`、`port=8000` 可以少试一次。

`.env` 不进仓库。密钥通过环境变量或构建时从当前 shell 透传进 `deploy/.env`，`deploy/` 整个目录都在 `.gitignore` 里。

---

## 本地运行

前置：Python 3.12+、Node 20+。

```bash
# 后端（默认 SQLite，零外部依赖）
cd apps/backend
python -m venv .venv && ./.venv/Scripts/activate     # Windows
pip install -r requirements.txt
python serve.py                                       # http://localhost:8000

# 前端
cd apps/frontend
npm install
npm run dev                                           # http://localhost:5173
```

开发模式下前后端分离（Vite 5173，后端 8000，CORS 已配）。
单端口模式由 `scripts/build_deploy.py` 负责——它**不**替你跑前端 build，而是要求你已经跑过：
先校验 `dist/` 比所有前端源文件都新（否则直接拒绝，见踩坑 #15），
再把 `dist/` 拷成后端认的 `webroot/`，输出一个可以直接 `python serve.py` 的目录：

```bash
cd apps/frontend && npm run build
cd ../.. && python scripts/build_deploy.py
cd deploy && python serve.py
```

要在本地试完整栈（含 Prometheus / Grafana / Loki / Postgres / Redis 共 10 个服务）：

```bash
docker compose up -d
```

**接真实模型**（可选，不配就用确定性 provider）：

```bash
cp .env.example .env
# OPENAI_BASE_URL=https://...   OPENAI_MODEL=<模型名>   OPENAI_API_KEY=<key>
```

`Settings` 的 `env_file=".env"` 由 `python-dotenv` 提供，`requirements.txt` 里是显式依赖——
它曾经是隐式的，本地能跑、干净环境起不来。

---

## 测试

```bash
cd apps/backend
PYTHONPATH=src python -m pytest tests -q          # 128 passed
cd apps/frontend && npx tsc -b && npx oxlint      # 类型 + lint
cd evals && python cli.py                         # 12 个场景的端到端评估
```

后端测试覆盖的是**失败路径**，不是快乐路径：`test_failure_recovery_paths.py`（工具失败、超时、级联失败）、
`test_recovery_rollback.py`（恢复不生效时的回退）、`test_tool_idempotency.py`（重复调用不重复生效）、
`test_tracing.py`（span 血缘完整）、`test_agent_timeline.py`（时间线事件契约）、
`test_incident_lifecycle.py`（状态机合法迁移）。

另外四组守的是"不报错但结果不对"的那类问题，所以单独列出来：
`test_status_labels.py` 断言每个线上枚举值都有中文标签（查表漏掉一个的形态是英文词漏进中文句子，
不抛异常）；`test_deploy_database.py` 断言部署库只带 schema、且 `-wal` 残留组合不出"能打开但没表"的库；
`test_shipped_database.py` 断言启动时能认出"这不是本次发布带的数据库"并重建，
以及删不掉时不要因此起不来；`test_validation_errors.py` 断言被拒绝的请求不会把 pydantic 的英文
原样送到浏览器，同时保留机器可读的字段定位。

---

## 工程踩坑记录

这一节留在这里是因为它们是真实发生过的，而且每一个都只在特定环境才暴露。

**1. 「部署跑不起来」和「本地跑得起来」是两件事。**
干净 venv 只装 `requirements.txt` 时，SQLAlchemy 的 async 引擎在 import 阶段就报 `No module named 'greenlet'`——
传递依赖在本地 .venv 里被别的包带进来了。修法是把 `sqlalchemy[asyncio]` 写清楚，而不是往环境里补一个包。
判定"能不能部署"的标准因此改成：全新 venv + 模拟沙箱的浅路径，跑一遍。

**2. 路径硬编码在 import 阶段爆炸。**
`_RUNBOOK_ROOT = parents[5] / "runbooks"` 把六层目录结构写死了。沙箱里的布局少一层，
直接 IndexError 崩在 import，连日志都来不及打。改成向上搜索锚点 + 环境变量可覆盖。

**3. `.env` 对 import 期的代码是隐形的。**
`main.py` 在 pydantic 真正加载 `.env` 之前，就读了 `os.environ` 里的开关。
结果 `.env` 里明明写着 `OPSPILOT_EMBED_SIMULATOR=true`，代码读到的是 `None`，模拟器没挂上，
所有 provider 调用 503——而日志指向一个根本不存在的伴随进程。
修法是加 `_bootstrap_env_file()`，在 import 期把 `.env` 灌进 `os.environ`，用 `setdefault` 让真实环境变量优先。

**4. 本机代理会劫持回环调用。**
`httpx` 默认 `trust_env=True`，会去读 `http_proxy`。在带透明代理的机器上，后端对自己 `/__sim` 的调用
被送给代理，代理回 404。同一个 URL `curl` 返回 200、`httpx` 返回 404，看着像见了鬼。
修法是加 `trust_env` 参数，并用 `_targets_this_host()` 判定目标是否本机（IPv4/IPv6 回环 + 反转解析 localhost），
本机就绕开代理；调外部模型不受影响。

**5. 被扣了 token 却没拿到结果，而且没人知道。**
提示词只说「refine these hypotheses' reasoning」，没说格式。模型回了一段散文，JSON 解析返回 `None`，
代码 `if isinstance(parsed, list)` 直接跳过——**token 扣了，输出静默丢弃，日志里一个字都没有**。
两处都要改：提示词显式写明 schema，以及「扣了 token 但解析失败」必须记事件。

**6. 元数据说谎比没有元数据更糟。**
`reasoning_mode` 只在创建运行时写了一次，之后没人更新，所以用了模型的运行也对外宣称 `deterministic`。
改成从 `get_llm().name` 推导。判断模型是否真被调用也不能看这个字段，要看 `usage.tokens`。

**7. 声明了但从未注册的东西。**
`domain/errors.py` 的注释写着「API 层是唯一把 AppError 转成 HTTP 响应的地方」，
但全项目没有任何 `add_exception_handler`。于是所有领域错误都漏成空 body 的 500，
调用方无法区分「这个事件已经在跑了」和「服务坏了」。注册一个 handler 之后，
重复启动返回 409 `{"code":"CONFLICT"}`，不存在的事件返回 404。

**8. 改写日志文本会让信号抽取静默失效。**
`agent/investigation.py` 的 `_LOG_PATTERNS` 是靠**子串匹配日志正文**来点亮信号的
（`"queuepool"` → 连接池饱和、`"outofmemoryerror"` → OOM）。把模拟器日志翻成中文，
匹配会全部落空，而失败形态是「证据变少、置信度变低」——不报错、不抛异常，只是诊断质量
悄悄退化。所以模式表做成双语的：中文新词在前、英文旧词兜底。
另一个坑是中文词必须是**短语**：`"缓存"` 会被健康日志里的「缓存命中率 0.91」点亮，
必须写成 `"缓存读取失败"`；同理 `"连接池"`、`"堆空间"`、`"锁等待"`。
这类改动只能靠评测集回归来守——`expected_evidence` 的召回率是唯一能看见它的指标。

复查时发现同一类问题还有第二种形态，而且它躲在**评测夹具**里：场景自己声明了症状
（「渠道返回 503」「GC 停顿越来越长」「缓存连接被拒绝」），但模拟器的日志正文里没有这些术语。
agent 查日志时读的是**告警服务**，看到的是下游视角的**传播日志**，例如「上游支付渠道返回 5xx」——
既没说是谁，也没说状态码。于是 rubric 在考一个语料里根本不存在的词。

更要命的是同一行传播日志被两个故障共用：渠道**故障**（503）和渠道**超时**（504）走同一个分支，
所以超时场景也会打印「返回 5xx」。改成按依赖的 `latency_p95` 分支，各自说出真实症状与厂商名：

```
外部支付渠道 acme-pay 返回 503 — 上游链路失败
外部支付渠道 acme-pay 调用超时（504 Gateway Timeout）：上游调用已等待 8200ms
```

顺带补上「堆内存不足 — GC 停顿 2.1s 仍无法回收」（原句只说「堆空间不足」，
而 `GC 停顿` 那行是 WARN，`query_logs` 默认只取 ERROR，永远不会被采到）。

证据召回率的完整轨迹是 **70.1% → 65.3% → 67.4% → 82.6%**：70.1% 是英文期基线，
中文化第一版掉到 65.3%（翻译日志正文打坏了信号匹配），修掉 redis / outage 回到 67.4%，
按延迟分支拆开传播日志后到 **82.6%**。12 个场景**无一项低于**英文期基线，4 项反超
（`payment-provider-outage` 0.5→1.0、`payment-third-party-timeout` 0.5→1.0、
`checkout-memory-leak` 0.75→1.0、`gateway-dependency-cascade` 0.75→1.0）。
根因判定、工具选择、恢复、验证仍全部 100%，误诊率仍为 0。

**教训**：评判「指标有没有回退」必须和**改动前的基线**比。我一开始只看「指标全绿」，
而绿是相对于上一次运行——那一次已经在英文期之后了，回退被自己人掩盖了。中间那两个
数字（65.3%、67.4%）留着不删，是因为它们才是这件事的证据：光看首尾你会以为翻译一遍
就把分数抬上去了，实际中间先砸了一个坑。

剩下 17.4% 的缺口也查清了，是**能力边界**而不是文案问题：两个日志探针
（`error_signature`、`dependency_logs`）把 `level="ERROR"` 写死了——标签就叫「读取服务当前
输出的错误特征」，这是有意的——而容量类故障的诊断术语（`CPU 饱和 … 请求队列深度上升`、
`检测到慢查询`）都在 **WARN** 行。所以 agent 能正确判出 `capacity` 类别，却拿不到
「饱和」「队列」这两个词。要补就得让容量域的探针也读 WARN，那会改变 agent 看到的语料，
得重跑整套评测确认没有引入噪声——所以留着，等有明确需求时再动。

**9. 重建部署包会静默吞掉凭据。**
`build_deploy.py` 先 `rmtree` 掉整个 `deploy/`，再重新生成 `.env`，而 `.env` 只从**当前 shell**
的环境变量取值。Key 平时只存在于 `deploy/.env`（该目录被 gitignore，这是刻意的），
于是「换台机器重建一次」就足以让线上实例丢掉模型 Key——症状是 agent 悄悄退化成模板输出，
`usage.tokens` 恒为 0，不报任何错。改成删除前先读旧 `.env`，按「shell 优先、旧值兜底」合并，
并在构建输出里区分来源。

**10. 服务端的控制帧违反了前端的类型契约。**
SSE 的 `stream.opened` / `stream.closed` 是传输控制帧（不落库、所以没有 `seq`），
载荷只有 `{"run_id","status","last_event_id"}`——缺 `event_type`、缺 `data`。
但它们被登记进了前端的事件类型表，于是被当成普通事件派发到时间线上，
`Object.entries(undefined)` 直接把「事件」页签打崩。
**TypeScript 的类型是编译期断言，网线上的数据在运行时没有任何保证。**
修法是在生产端补齐信封（`control_payload()`），而不是在消费端到处 `?? {}`；
消费端只保留一处网络值信任边界。补了回归测试，并验证了该测试能拦住旧实现。

**11. 一个只在别人的浏览器里复现的渲染崩溃。**
「未能在 '节点' 上执行 'insertBefore'：新节点要插入的节点不是该节点的子节点」——
本地用 CDP 压测 32 次路由切换 + 浮层反复开关，一次都没复现。

两个原因叠在一起：

- `index.html` 写着 `lang="en"`，界面却是中文。浏览器认为这是一个英文页面，
  如果用户对英文开过自动翻译，它就会**替 React 改写文本节点**（把每段文字包进 `<font>`）。
  React 手里握着的是那些已经被移出文档的文本节点引用，下一次 commit 就必然报这个错。
  这个失败只在「浏览器正在翻译」时出现，所以干净的测试 profile 永远看不到。
  修法是把语言声明改对（`lang="zh-CN"`），并显式声明 `translate="no"` + `<meta name="google" content="notranslate">`。
- 恢复路径本身是死的。原来的自愈逻辑监听 `window.onerror`，
  但只要路由上有 `errorElement`，React 就会把 commit 阶段的错误交给错误边界，**根本不会发到 window**。
  于是错误页面一直挂着，用户只能手动刷新——这才是「老是出现」的真正来源。
  现在错误边界自己触发重建，并且有跨刷新持久的修复预算：连续失效就停在一个纯 DOM 写的页面上说明原因，而不是无限重建循环。

顺带修掉的还有：软重建会把旧的浮层（`.scrim`、`.toast-host`）留在 `<body>` 里，
一个孤儿遮罩层盖在刚修好的界面上——所以浮层现在统一挂到受管的 `#overlay-root`，重建时整块换掉。
CDP 验证：路由扫荡 8 个页面无异常、端口探针记录 0 次 DOM 不变量违规、
真实 Agent 运行的事件页签正常渲染 67 行、重建后 `<body>` 子节点数不增长。

**12. 构建产物把开发库一起带上线，还顺手关掉了首屏种子。**
`build_deploy.py` 原本是把 `apps/backend/opspilot.db` 原样拷进部署包，那是**开发库**——
里面躺着本地跑出来的 4 条故障记录，连标题带时间戳一起被发布出去。
更隐蔽的是它的副作用：`main.py` 的 `_seed_demo_incidents()` 守卫是 `count(incidents) == 0`，
表非空就直接 `return`，于是这个「让新实例不至于空着首屏」的种子从来没执行过。
表现是线上首屏是 4 条没人认识的旧故障，而不是按场景定义生成的那一条。
改成只带 schema：`snapshot_sqlite()` 读一份自洽副本，`reset_sqlite()` 清空所有表（`VACUUM` 顺带收回页），
首屏交还给种子逻辑。

同一个函数上还叠着一个更安静的坑：`shutil.copy2` 只拷 `.db`，而 SQLite 把已提交但未 checkpoint
的事务放在 `-wal` 里；目标目录若还残留上一轮构建的 `-wal`，就组合出一个"看着正常"的库。
实测这个组合的失败形态是：**库能打开、`PRAGMA integrity_check` 返回 `ok`、但表整个不见了**
（`no such table: incidents`）。所以构建闸门不能只看 pragma，`check_sqlite()` 现在会另外断言 schema 存在——
一个没有表的库不算通过检查。`tests/test_deploy_database.py` 把这三种形态都固化了（8 个用例），
其中「旧实现会红」是实测过的，不是推的。

**13. 托管平台上传是"覆盖"，不是"替换"，于是线上跑的其实是上一版的数据库。**
这是最花时间的一个，因为它同时伪装成两种完全不同的现象。

线上要更新，平台是把新文件写到旧文件上。而 `db/session.py` 每个连接都执行
`PRAGMA journal_mode=WAL`，所以**上一版留下的 `-wal`/`-shm` 会原地不动地留在那里**。
SQLite 的 WAL 里带着 page 1 的副本，于是它堂而皇之地接管了新上传的主文件。实测（本次部署包 + 上一版的 WAL）：

```
integrity_check : ok
user_version    : 1600000000      ← 上一版的构建戳
incidents       : ['版本发布后支付授权开始报错', '缓存集群整体不可达']   ← 上一版的数据，共 2 条
```

我这次上传的是一个 0 行、23 表的新库——**它被完全忽略了**。没有报错、没有告警，
`PRAGMA integrity_check` 说一切正常。之前几轮"重新发布"能"成功"，靠的就是这个：
线上一直在跑更早的数据库，所以无论我改了多少文案，页面上的英文都没变过。
同一机制的另一面是启动直接崩：上一版的 WAL 若是在写入中途被 kill 掉的（torn），
`create_all` 的第一次反射就抛 `sqlite3.DatabaseError`，服务根本起不来。

判据不能是文件内容——同一份 schema 的旧库和健康库长得一模一样。所以构建时给库盖一个戳：
`build_deploy.py` 写 `PRAGMA user_version = <构建时间戳>` 并把同一个值写进 `.env` 的
`OPSPILOT_BUILD_STAMP`；启动时对比，不一致就说明这个文件不是本次发布带的。

```
[opspilot] database: opspilot.db was unusable (build stamp mismatch
  (file says 1600000000, this release ships 1790854896) — a previous release's
  database is still in place) — deleted, schema will be rebuilt
```

三个细节是必须的，少一个就会变成新 bug：

- **`-wal` 和 `-shm` 要一起删**。只删主文件的话，`create_all` 把表建进新文件，
  旁边的旧 WAL 再覆盖一次——同一个故障推迟一个版本复发。
- **重建之后要把戳补写回去**。否则下一次启动看到戳是 0，判定"不是我的库"，
  再删一次——一次性的修复变成了每次重启都清空数据。
- **删不掉不能导致启动失败**。只读挂载、权限位、别的进程占着文件，这些都会让 unlink 抛错；
  而起不来是唯一没有恢复路径的结果。现在删失败只记一行、继续启动。

复现是照着沙箱行为搭的：先用旧戳跑起一个实例、注入一个场景（得到「缓存集群整体不可达」），
用 `/F` 杀掉以留下 WAL，再把新构建的主文件覆盖上去、旧 sidecar 原样放回。
启动前 2 条旧故障 + 旧戳，启动后 1 条中文种子 + 新戳。
`tests/test_shipped_database.py` 覆盖了戳不匹配、sidecar 连带删除、删失败不致命这几条。

**14. 中文句子里的 Python repr。**

`f"必须是 {sorted(VALID_SEVERITIES)} 之一。"` 是我写中文文案时最顺手的写法，
它渲染出来是：

```
severity 取值 'foo' 不合法，必须是 ['critical', 'high', 'low'] 之一。
```

诊断没错，句子是坏的。值本身是 wire 词、必须保持原样（调用方要照着发），
坏的是**表示法**——方括号、引号、逗号全带进来了。同一批代码里我扫出 9 处，
分布在线索推理（`所需信号 ['deployment_recent'] 已经出现`）、校验错误、
部署状态三块；它们都在操作员会读到的路径上。

修法是一行 `join_values()`：`critical、high、low`。

值得记的是**这一处是靠端到端 dump 抓到的，不是靠静态扫描**——因为它语法完全合法，
lint 和类型检查都不会响。端到端那次 dump 里 `reasoning_summary` 结尾挂着
`| 被 ['deployment_recent'] 证实`，和中文句子并排放在一起，一眼就不对。

所以补了一条 AST 扫描测试（`tests/test_prose_containers.py`）：遍历所有 f-string，
凡是直接插值 `sorted()/list()/set()/dict()` 或 `.keys()/.items()/.values()` 的都判失败。
用 AST 而不是正则，是因为 `f"{sorted(x)[0]}"`（插值单个元素）完全合法，
正则分不出来。扫描器自己也有一条自检，会先用一份人造的泄漏代码确认它真能报错——
否则这条测试可能只是永远为真。

**15. 源码改了，`dist/` 没重建，部署包照样打出来了。**

这个比 #13 更安静。`build_deploy.py` 只检查 `dist/index.html` **是否存在**，
而一份陈旧的产物是一份**完全合法**的产物：服务端跑得对、仓库里代码是对的，
只有浏览器里还是旧字符串。没有任何一步会报错。

实际发生的是：`api/client.ts` 加了一个分支，让 404 显示后端的 `message`
（原来会显示 `fetch` 的 HTTP 状态行「API 404 Not Found」，比服务端说的还差）。
部署包却是从一个比这次修改**早一小时**的 `dist/` 打出来的。修复在仓库里、
在服务端里、就是不在屏幕上。靠端到端 dump 才发现——那种事找到一次是本事，
每次都靠它不是。

所以构建时加了一道新鲜度闸门：比较 `dist/index.html` 与 `src/`、`index.html`、
`package.json`、vite / tsconfig 的时间戳，任何源文件更新就直接拒绝构建。

```
error: frontend bundle is stale — src/api/client.ts is newer than dist/index.html (56s).
Run `npm run build` in apps/frontend, then rebuild the deploy unit.
```

时间戳是这里唯一可用的判据（没有 build manifest），但够用：
`vite build` 先读 `src/` 再写 `dist/`，所以源文件比产物新就意味着产物早于这次改动。
`tests/test_build_gates.py` 覆盖了通过、拦下、点名到具体文件、以及不该误报的几种
（前端的 README/Dockerfile、`src/` 下万一出现的 `node_modules`）。

**16. 在 bundle 里搜到字符串，不等于那行代码会执行。**

重建之后我以为错误路径已经全中文了——`grep` 一下产物，`HTTP_STATUS_ZH` 在里面，
十二个状态码一个不少。但 `ApiError.detail` 是个 getter，它有三跳：
`detail` → `message`（AppError 信封）→ 兜底。搜字符串只能证明**词存在**，
证明不了**哪一跳在跑**；而当时的兜底跳是 `return this.message`，
也就是 `fetch` 的 `API 500 Internal Server Error`。

所以改成直接执行产物里的那个类：

```js
const mod = await import('../apps/frontend/dist/assets/Panel-*.js')
const ApiError = mod.d              // 打包器的导出名，压缩后是单字母
new ApiError('API 500 Internal Server Error', 500, 'boom', url).detail
```

不依赖浏览器，但真的跑了要上线的那些字节。六个 payload 形状里第五个当场失败——
反代的错误页、纯文本 500、被截断的流都会走到那一跳，所以它不是假想路径。
修法是状态码查表给中文原因、并保留数字状态码（HTTP/2 干脆没有 reason phrase，
`statusText` 本来就不值得透传）。

这里还有一次自摆乌龙值得记：检查脚本第一版把**正确答案**判成失败——
`status 0`（连不上后端）的 message 本身就是最终文案，
却被"结果等于 statusText 就报错"这条规则误伤。断言写错方向，
和评测里"夹具本身缺关键词"是同一类错误：**验证代码也是代码，也得被验证**。
现在它改成只在 statusText 本身是英文时才报警。

---

## 目录结构

```
OpsPilot/
├── apps/
│   ├── backend/src/opspilot_backend/
│   │   ├── agent/              # LangGraph 图、15 个节点、预算、检查点、LLM 抽象
│   │   ├── api/v1/endpoints/    # agent / incidents / services / simulator / metrics / health
│   │   ├── core/                # 配置、结构化日志、指标、链路
│   │   ├── domain/              # 枚举、领域错误
│   │   ├── infrastructure/      # provider 装配、HTTP 客户端、容器
│   │   ├── mcp/                 # MCP 客户端
│   │   ├── models/              # incident / investigation / recovery / service / agent / audit / knowledge
│   │   ├── repositories/        # 数据访问
│   │   ├── services/            # 运行编排、事件流、工具钩子
│   │   ├── tools/               # 22 个工具的定义、执行器、钩子
│   │   └── main.py
│   ├── frontend/src/
│   │   ├── api/                 # REST 客户端 + SSE 客户端
│   │   ├── components/          # 时间线、表格、指标卡、服务网格
│   │   ├── lib/                 # 事件类型表、查询、格式化、浮层容器、重建策略
│   │   ├── pages/               # 9 个页面
│   │   ├── shell/               # 外壳、侧边栏、命令面板、错误边界
│   │   ├── styles/              # tokens / primitives / patterns
│   │   └── ui/                  # Button / Overlay / Toast / Dropdown / Panel ...
│   └── mcp-server/
├── evals/                       # 评测数据集、runner、指标聚合、历史报告
├── runbooks/                    # Markdown 知识源（database / deployment / memory / payment / redis）
├── simulator/                   # 独立模拟器（12 个场景）
├── infra/                       # prometheus / otel 配置
├── scripts/build_deploy.py      # 单端口部署单元
├── docker-compose.yml           # 10 个服务的完整栈
└── .github/workflows/           # CI
```

约 29k 行 Python、7k 行 TypeScript、4.5k 行 CSS。

---

## License

MIT
