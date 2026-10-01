"""The single tool registry.

Registration *is* validation: every tool declares Pydantic input/output
models, a timeout, a retry budget, an error taxonomy, a permission level and
a risk level. There is no second dispatch table anywhere else in the codebase.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field

from opspilot_backend.domain.enums import (
    AgentStage,
    PermissionLevel,
    RiskLevel,
)
from opspilot_backend.infrastructure.container import get_providers
from opspilot_backend.tools.spec import ToolContext, ToolSpec

# ---------------------------------------------------------------------------
# Input / output models
# ---------------------------------------------------------------------------


class ServiceTarget(BaseModel):
    service: str = Field(..., description="服务名，例如 payment-service")


class GetServiceStatusInput(ServiceTarget):
    pass


class ServiceStatusOutput(BaseModel):
    service: str
    health: str
    error_rate: float | None = None
    latency_p95: float | None = None
    cpu_percent: float | None = None
    memory_mb: float | None = None
    db_connections: int | None = None
    request_rate: float | None = None


class QueryLogsInput(ServiceTarget):
    level: str | None = Field(default=None, description="DEBUG | INFO | WARN | ERROR")
    minutes: int = Field(default=30, ge=1, le=1440)
    limit: int = Field(default=50, ge=1, le=500)


class LogEntry(BaseModel):
    timestamp: str
    level: str
    service: str
    message: str


class QueryLogsOutput(BaseModel):
    service: str
    total: int
    entries: list[LogEntry]


class QueryMetricsInput(ServiceTarget):
    metric_names: list[str] = Field(default_factory=list)
    minutes: int = Field(default=30, ge=1, le=1440)


class MetricSeries(BaseModel):
    metric: str
    service: str
    points: list[dict[str, Any]]
    latest: float | None = None
    previous: float | None = None


class QueryMetricsOutput(BaseModel):
    service: str
    series: list[MetricSeries]


class GetDeploymentsInput(ServiceTarget):
    limit: int = Field(default=10, ge=1, le=100)


class DeploymentRecord(BaseModel):
    version: str
    deployed_at: str
    status: str
    author: str | None = None
    rolled_back_from: str | None = None


class GetDeploymentsOutput(BaseModel):
    service: str
    deployments: list[DeploymentRecord]


class GetRecentCommitsInput(BaseModel):
    repository: str = Field(default="opspilot")
    limit: int = Field(default=10, ge=1, le=100)


class CommitRecord(BaseModel):
    sha: str
    repository: str
    author: str
    message: str
    committed_at: str


class GetRecentCommitsOutput(BaseModel):
    commits: list[CommitRecord]


class GetDependenciesInput(BaseModel):
    service: str | None = Field(default=None)


class GetDependenciesOutput(BaseModel):
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]


class SearchRunbooksInput(BaseModel):
    query: str = Field(..., min_length=1)
    limit: int = Field(default=5, ge=1, le=20)


class RunbookHit(BaseModel):
    id: str
    title: str
    category: str
    score: int
    heading: str
    excerpt: str


class SearchRunbooksOutput(BaseModel):
    hits: list[RunbookHit]


class GetRunbookInput(BaseModel):
    runbook_id: str


class GetRunbookOutput(BaseModel):
    id: str
    title: str
    category: str
    content: str


class RecoveryTarget(BaseModel):
    service: str
    reason: str = Field(default="", description="为什么要执行这个动作")


class RestartServiceInput(RecoveryTarget):
    pass


class RollbackDeploymentInput(RecoveryTarget):
    target_version: str | None = None


class ScaleServiceInput(RecoveryTarget):
    replicas: int = Field(default=2, ge=1, le=50)


# The remaining remediations share one input shape: a target and a reason.
# They are separate models so each tool's JSON schema tells the planner which
# component it expects — a cache action against a service name, and vice versa,
# is a mistake worth catching before the approval request is filed.
class RestartRedisInput(RecoveryTarget):
    pass


class FlushCacheInput(RecoveryTarget):
    pass


class RestartPostgresInput(RecoveryTarget):
    pass


class IncreasePoolSizeInput(RecoveryTarget):
    pass


class ClearDeadlockInput(RecoveryTarget):
    pass


class EnableCircuitBreakerInput(RecoveryTarget):
    pass


class SwitchPaymentProviderInput(RecoveryTarget):
    pass


class NotifyOncallInput(RecoveryTarget):
    summary: str = Field(default="", description="值班工程师需要知道的信息")


class MutationOutput(BaseModel):
    service: str
    action: str
    status: str
    #: Whether the action actually removed the fault. A recovery can succeed
    #: as an HTTP call and still change nothing — a restart does not fix a bad
    #: release. Without this the executor reported "succeeded" for actions the
    #: environment ignored.
    effective: bool | None = None
    removed_faults: list[str] = Field(default_factory=list)
    mitigated_faults: list[str] = Field(default_factory=list)
    resisted_faults: list[str] = Field(default_factory=list)
    remaining_faults: list[str] = Field(default_factory=list)
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    to_version: str | None = None
    from_version: str | None = None
    replicas: int | None = None


class VerifyServiceHealthInput(ServiceTarget):
    max_error_rate: float = Field(default=0.01, ge=0.0, le=1.0)
    max_latency_p95: float = Field(default=500.0, ge=0.0)


class HealthCheck(BaseModel):
    name: str
    passed: bool
    actual: Any = None
    threshold: Any = None


class VerifyServiceHealthOutput(BaseModel):
    service: str
    passed: bool
    checks: list[HealthCheck]


class CreateGithubIssueInput(BaseModel):
    title: str = Field(..., min_length=1)
    body: str = Field(default="")
    labels: list[str] = Field(default_factory=list)


class CreateGithubIssueOutput(BaseModel):
    number: int | None = None
    url: str | None = None
    title: str | None = None


# ---------------------------------------------------------------------------
# Handlers — each one delegates to a provider, none of them invent data
# ---------------------------------------------------------------------------


async def _get_service_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Current state of one component.

    Note what is *not* copied across: ``active_faults``. It names the injected
    fault, which is the scenario's hidden root cause — an Agent allowed to read
    it is not diagnosing anything.

    Everything else is fair game, including the fields that make a number
    interpretable: a connection count is meaningless without ``pool_max``, a
    heap size is meaningless without ``memory_limit_mb``, and a broken
    component cannot be classified without ``kind``. Dropping those three
    silently reduced the Agent's whole view of the world to "unhealthy".
    """
    providers = get_providers()
    raw = await providers.services.get_status(args["service"])
    return {
        "service": raw.get("service", args["service"]),
        "health": raw.get("health", "unknown"),
        "kind": raw.get("kind", "service"),
        "error_rate": raw.get("error_rate"),
        "latency_p95": raw.get("latency_p95_ms"),
        "latency_p50": raw.get("latency_p50_ms"),
        "cpu_percent": raw.get("cpu_percent"),
        "memory_mb": raw.get("memory_mb"),
        "memory_limit_mb": raw.get("memory_limit_mb"),
        "db_connections": raw.get("db_connections"),
        "pool_max": raw.get("pool_max"),
        "pool_utilisation": raw.get("pool_utilisation"),
        "request_rate": raw.get("request_rate"),
        "version": raw.get("version"),
        "replicas": raw.get("replicas"),
    }


async def _query_logs(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    providers = get_providers()
    payload = await providers.logs.query_logs(
        args["service"], args.get("level"), args.get("minutes", 30), args.get("limit", 50)
    )
    return {
        "service": payload["service"],
        "total": payload["total"],
        "entries": payload["entries"],
    }


async def _query_metrics(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    providers = get_providers()
    return await providers.metrics.query(
        args["service"], args.get("metric_names") or [], args.get("minutes", 30)
    )


async def _get_deployments(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    providers = get_providers()
    rows = await providers.deployments.list_deployments(
        args["service"], args.get("limit", 10)
    )
    return {"service": args["service"], "deployments": rows}


async def _get_recent_commits(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    providers = get_providers()
    rows = await providers.deployments.recent_commits(
        args.get("repository", "opspilot"), args.get("limit", 10)
    )
    return {"commits": rows}


async def _get_dependencies(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    providers = get_providers()
    return await providers.services.get_dependencies()


async def _search_runbooks(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    providers = get_providers()
    hits = await providers.runbooks.search(args["query"], args.get("limit", 5))
    return {"hits": hits}


async def _get_runbook(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    providers = get_providers()
    rb = await providers.runbooks.get(args["runbook_id"])
    if rb is None:
        raise KeyError(f"找不到 runbook：{args['runbook_id']}")
    return {
        "id": rb["id"],
        "title": rb["title"],
        "category": rb["category"],
        "content": rb["content"],
    }


async def _restart_service(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    providers = get_providers()
    return await providers.services.restart(args["service"])


async def _rollback_deployment(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    providers = get_providers()
    return await providers.deployments.rollback(args["service"])


async def _scale_service(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    providers = get_providers()
    return await providers.services.scale(args["service"], args.get("replicas", 2))


# The remediations below all share one wire shape: POST /simulator/actions/{name}
# with the target component in the query string. They exist as distinct tools
# rather than one generic "act" so that the Agent's proposal names *what* it
# wants to do — "restart the Redis cluster" is a reviewable statement in a way
# that "act: restart_redis on redis" is not.
async def _restart_redis(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    providers = get_providers()
    return await providers.services.act("restart_redis", args["service"])


async def _flush_cache(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    providers = get_providers()
    return await providers.services.act("flush_cache", args["service"])


async def _restart_postgres(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    providers = get_providers()
    return await providers.services.act("restart_postgres", args["service"])


async def _increase_pool_size(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    providers = get_providers()
    return await providers.services.act("increase_pool_size", args["service"])


async def _clear_deadlock(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    providers = get_providers()
    return await providers.services.act("clear_deadlock", args["service"])


async def _enable_circuit_breaker(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    providers = get_providers()
    return await providers.services.act("enable_circuit_breaker", args["service"])


async def _switch_payment_provider(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    providers = get_providers()
    return await providers.services.act("switch_payment_provider", args["service"])


async def _notify_oncall(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    providers = get_providers()
    return await providers.services.act(
        "notify_oncall", args["service"], summary=args.get("summary", "")
    )


async def _verify_service_health(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    providers = get_providers()
    status = await providers.services.get_status(args["service"])
    max_error = args.get("max_error_rate", 0.01)
    max_latency = args.get("max_latency_p95", 500.0)
    error_rate = float(status.get("error_rate") or 0.0)
    latency = float(status.get("latency_p95_ms") or 0.0)
    health = str(status.get("health", "unknown"))
    checks = [
        HealthCheck(
            name="error_rate",
            passed=error_rate <= max_error,
            actual=error_rate,
            threshold=max_error,
        ),
        HealthCheck(
            name="latency_p95",
            passed=latency <= max_latency,
            actual=latency,
            threshold=max_latency,
        ),
        HealthCheck(
            name="health_status", passed=health == "healthy", actual=health, threshold="healthy"
        ),
    ]
    return {
        "service": status.get("service", args["service"]),
        "passed": all(c.passed for c in checks),
        "checks": [c.model_dump() for c in checks],
    }


async def _create_github_issue(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    providers = get_providers()
    return await providers.github.create_issue(
        args["title"], args.get("body", ""), args.get("labels", [])
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

INVESTIGATION_STAGES = [
    AgentStage.PARALLEL_INVESTIGATION,
    AgentStage.HYPOTHESIS_VERIFICATION,
    AgentStage.TRIAGE,
]
#: Stages a mutating tool may run in. ROLLBACK is included because a
#: compensating action is one of these tools run on purpose — under a separate
#: stage so the trace distinguishes "the fix" from "the undo".
RECOVERY_STAGES = [
    AgentStage.RECOVERY_EXECUTOR,
    AgentStage.ROLLBACK,
    AgentStage.VERIFICATION,
]


def _spec(**kwargs: Any) -> ToolSpec:
    return ToolSpec(**kwargs)


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "get_service_status": _spec(
        name="get_service_status",
        description="获取某个服务的当前健康状态、错误率、延迟与资源占用。",
        category="service_health",
        input_model=GetServiceStatusInput,
        output_model=ServiceStatusOutput,
        timeout_s=5.0,
        max_retries=2,
        permission_level=PermissionLevel.READ_ONLY,
        risk_level=RiskLevel.LOW,
        error_types=["timeout", "upstream_error", "unknown_service"],
        mcp_server="ops",
    ),
    "query_logs": _spec(
        name="query_logs",
        description="在指定时间窗内检索某个服务的结构化日志。",
        category="logs",
        input_model=QueryLogsInput,
        output_model=QueryLogsOutput,
        timeout_s=6.0,
        max_retries=2,
        permission_level=PermissionLevel.READ_ONLY,
        risk_level=RiskLevel.LOW,
        error_types=["timeout", "upstream_error", "unknown_service"],
        mcp_server="ops",
    ),
    "query_metrics": _spec(
        name="query_metrics",
        description="查询时间序列指标（错误率、延迟、CPU、内存、数据库连接数）。",
        category="metrics",
        input_model=QueryMetricsInput,
        output_model=QueryMetricsOutput,
        timeout_s=8.0,
        max_retries=2,
        permission_level=PermissionLevel.READ_ONLY,
        risk_level=RiskLevel.LOW,
        error_types=["timeout", "upstream_error", "unknown_metric"],
        mcp_server="ops",
    ),
    "get_deployments": _spec(
        name="get_deployments",
        description="获取某个服务最近的发布历史。",
        category="deployments",
        input_model=GetDeploymentsInput,
        output_model=GetDeploymentsOutput,
        timeout_s=5.0,
        max_retries=2,
        permission_level=PermissionLevel.READ_ONLY,
        risk_level=RiskLevel.LOW,
        error_types=["timeout", "upstream_error"],
        mcp_server="ops",
    ),
    "get_recent_commits": _spec(
        name="get_recent_commits",
        description="获取某个服务代码仓库的最近提交。",
        category="deployments",
        input_model=GetRecentCommitsInput,
        output_model=GetRecentCommitsOutput,
        timeout_s=5.0,
        max_retries=2,
        permission_level=PermissionLevel.READ_ONLY,
        risk_level=RiskLevel.LOW,
        error_types=["timeout", "upstream_error"],
        mcp_server="ops",
    ),
    "get_dependencies": _spec(
        name="get_dependencies",
        description="获取服务依赖关系图（用于爆炸半径分析）。",
        category="service_health",
        input_model=GetDependenciesInput,
        output_model=GetDependenciesOutput,
        timeout_s=5.0,
        max_retries=2,
        permission_level=PermissionLevel.READ_ONLY,
        risk_level=RiskLevel.LOW,
        error_types=["timeout", "upstream_error"],
        mcp_server="ops",
    ),
    "search_runbooks": _spec(
        name="search_runbooks",
        description="检索运维 runbook，获取处置建议。",
        category="runbooks",
        input_model=SearchRunbooksInput,
        output_model=SearchRunbooksOutput,
        timeout_s=3.0,
        max_retries=1,
        permission_level=PermissionLevel.READ_ONLY,
        risk_level=RiskLevel.LOW,
        error_types=["timeout", "not_found"],
        mcp_server="ops",
    ),
    "get_runbook": _spec(
        name="get_runbook",
        description="按 id 获取一份 runbook 的完整内容。",
        category="runbooks",
        input_model=GetRunbookInput,
        output_model=GetRunbookOutput,
        timeout_s=3.0,
        max_retries=1,
        permission_level=PermissionLevel.READ_ONLY,
        risk_level=RiskLevel.LOW,
        error_types=["timeout", "not_found"],
        mcp_server="ops",
    ),
    "restart_service": _spec(
        name="restart_service",
        description="重启服务，清掉进程内状态（连接池、缓存）。",
        category="recovery",
        input_model=RestartServiceInput,
        output_model=MutationOutput,
        timeout_s=15.0,
        max_retries=1,
        permission_level=PermissionLevel.MUTATE_INFRA,
        risk_level=RiskLevel.HIGH,
        idempotent=False,
        side_effect=True,
        allowed_stages=RECOVERY_STAGES,
        error_types=["timeout", "upstream_error", "approval_required", "unknown_service"],
        mcp_server="ops",
    ),
    "rollback_deployment": _spec(
        name="rollback_deployment",
        description="把服务回滚到上一个部署版本。",
        category="recovery",
        input_model=RollbackDeploymentInput,
        output_model=MutationOutput,
        timeout_s=20.0,
        max_retries=1,
        permission_level=PermissionLevel.DESTRUCTIVE,
        risk_level=RiskLevel.CRITICAL,
        idempotent=False,
        side_effect=True,
        allowed_stages=RECOVERY_STAGES,
        error_types=["timeout", "upstream_error", "approval_required", "no_deployment_history"],
        mcp_server="ops",
    ),
    "scale_service": _spec(
        name="scale_service",
        description="横向扩容服务，把负载摊开。",
        category="recovery",
        input_model=ScaleServiceInput,
        output_model=MutationOutput,
        timeout_s=15.0,
        max_retries=1,
        permission_level=PermissionLevel.MUTATE_INFRA,
        risk_level=RiskLevel.MEDIUM,
        idempotent=False,
        side_effect=True,
        allowed_stages=RECOVERY_STAGES,
        error_types=["timeout", "upstream_error", "unknown_service"],
        mcp_server="ops",
    ),
    "restart_redis": _spec(
        name="restart_redis",
        description="对 Redis 集群做故障转移或重启，让客户端能重新连上。",
        category="recovery",
        input_model=RestartRedisInput,
        output_model=MutationOutput,
        timeout_s=20.0,
        max_retries=1,
        permission_level=PermissionLevel.MUTATE_INFRA,
        risk_level=RiskLevel.HIGH,
        idempotent=False,
        side_effect=True,
        allowed_stages=RECOVERY_STAGES,
        error_types=["timeout", "upstream_error", "approval_required", "unknown_service"],
        mcp_server="ops",
    ),
    "flush_cache": _spec(
        name="flush_cache",
        description="丢弃正在被返回给客户端的缓存条目。",
        category="recovery",
        input_model=FlushCacheInput,
        output_model=MutationOutput,
        timeout_s=15.0,
        max_retries=1,
        permission_level=PermissionLevel.MUTATE_INFRA,
        risk_level=RiskLevel.MEDIUM,
        idempotent=False,
        side_effect=True,
        allowed_stages=RECOVERY_STAGES,
        error_types=["timeout", "upstream_error", "approval_required", "unknown_service"],
        mcp_server="ops",
    ),
    "restart_postgres": _spec(
        name="restart_postgres",
        description="重启数据库，清掉卡住的会话与锁。",
        category="recovery",
        input_model=RestartPostgresInput,
        output_model=MutationOutput,
        timeout_s=30.0,
        max_retries=1,
        permission_level=PermissionLevel.DESTRUCTIVE,
        risk_level=RiskLevel.CRITICAL,
        idempotent=False,
        side_effect=True,
        allowed_stages=RECOVERY_STAGES,
        error_types=["timeout", "upstream_error", "approval_required", "unknown_service"],
        mcp_server="ops",
    ),
    "increase_pool_size": _spec(
        name="increase_pool_size",
        description="调高某个服务的数据库连接池上限。",
        category="recovery",
        input_model=IncreasePoolSizeInput,
        output_model=MutationOutput,
        timeout_s=15.0,
        max_retries=1,
        permission_level=PermissionLevel.MUTATE_INFRA,
        risk_level=RiskLevel.MEDIUM,
        idempotent=False,
        side_effect=True,
        allowed_stages=RECOVERY_STAGES,
        error_types=["timeout", "upstream_error", "approval_required", "unknown_service"],
        mcp_server="ops",
    ),
    "clear_deadlock": _spec(
        name="clear_deadlock",
        description="杀掉持有行锁的阻塞事务。",
        category="recovery",
        input_model=ClearDeadlockInput,
        output_model=MutationOutput,
        timeout_s=20.0,
        max_retries=1,
        permission_level=PermissionLevel.MUTATE_INFRA,
        risk_level=RiskLevel.HIGH,
        idempotent=False,
        side_effect=True,
        allowed_stages=RECOVERY_STAGES,
        error_types=["timeout", "upstream_error", "approval_required", "unknown_service"],
        mcp_server="ops",
    ),
    "enable_circuit_breaker": _spec(
        name="enable_circuit_breaker",
        description="为失败的上游打开熔断器，让调用方快速失败。",
        category="recovery",
        input_model=EnableCircuitBreakerInput,
        output_model=MutationOutput,
        timeout_s=15.0,
        max_retries=1,
        permission_level=PermissionLevel.MUTATE_INFRA,
        risk_level=RiskLevel.MEDIUM,
        idempotent=True,
        side_effect=True,
        allowed_stages=RECOVERY_STAGES,
        error_types=["timeout", "upstream_error", "approval_required", "unknown_service"],
        mcp_server="ops",
    ),
    "switch_payment_provider": _spec(
        name="switch_payment_provider",
        description="把授权请求切到备用支付渠道。",
        category="recovery",
        input_model=SwitchPaymentProviderInput,
        output_model=MutationOutput,
        timeout_s=25.0,
        max_retries=1,
        permission_level=PermissionLevel.WRITE_EXTERNAL,
        risk_level=RiskLevel.CRITICAL,
        idempotent=False,
        side_effect=True,
        allowed_stages=RECOVERY_STAGES,
        error_types=["timeout", "upstream_error", "approval_required", "unknown_service"],
        mcp_server="ops",
    ),
    "notify_oncall": _spec(
        name="notify_oncall",
        description="带着当前诊断结果呼叫值班工程师。",
        category="recovery",
        input_model=NotifyOncallInput,
        output_model=MutationOutput,
        timeout_s=8.0,
        max_retries=2,
        permission_level=PermissionLevel.READ_ONLY,
        risk_level=RiskLevel.LOW,
        idempotent=True,
        side_effect=False,
        allowed_stages=RECOVERY_STAGES,
        error_types=["timeout", "upstream_error", "unknown_service"],
        mcp_server="ops",
    ),
    "verify_service_health": _spec(
        name="verify_service_health",
        description="在恢复之后，按明确的健康阈值探测服务。",
        category="service_health",
        input_model=VerifyServiceHealthInput,
        output_model=VerifyServiceHealthOutput,
        timeout_s=6.0,
        max_retries=2,
        permission_level=PermissionLevel.READ_ONLY,
        risk_level=RiskLevel.LOW,
        error_types=["timeout", "upstream_error"],
        mcp_server="ops",
    ),
    "create_github_issue": _spec(
        name="create_github_issue",
        description="在服务仓库里建一个跟进 issue。",
        category="github",
        input_model=CreateGithubIssueInput,
        output_model=CreateGithubIssueOutput,
        timeout_s=8.0,
        max_retries=1,
        permission_level=PermissionLevel.WRITE_EXTERNAL,
        risk_level=RiskLevel.MEDIUM,
        idempotent=False,
        side_effect=True,
        error_types=["timeout", "upstream_error", "not_configured"],
        mcp_server="ops",
    ),
}

HANDLERS: dict[str, Any] = {
    "get_service_status": _get_service_status,
    "query_logs": _query_logs,
    "query_metrics": _query_metrics,
    "get_deployments": _get_deployments,
    "get_recent_commits": _get_recent_commits,
    "get_dependencies": _get_dependencies,
    "search_runbooks": _search_runbooks,
    "get_runbook": _get_runbook,
    "restart_service": _restart_service,
    "rollback_deployment": _rollback_deployment,
    "scale_service": _scale_service,
    "restart_redis": _restart_redis,
    "flush_cache": _flush_cache,
    "restart_postgres": _restart_postgres,
    "increase_pool_size": _increase_pool_size,
    "clear_deadlock": _clear_deadlock,
    "enable_circuit_breaker": _enable_circuit_breaker,
    "switch_payment_provider": _switch_payment_provider,
    "notify_oncall": _notify_oncall,
    "verify_service_health": _verify_service_health,
    "create_github_issue": _create_github_issue,
}

# Guard against the two registries drifting apart ever again.
assert set(TOOL_REGISTRY) == set(HANDLERS), (
    f"tool registry / handler mismatch: "
    f"{set(TOOL_REGISTRY) ^ set(HANDLERS)}"
)


def get_tool(name: str) -> ToolSpec:
    try:
        return TOOL_REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"未知工具：{name!r}") from exc


def list_tools() -> list[dict[str, Any]]:
    return [spec.describe() for spec in TOOL_REGISTRY.values()]


def idempotency_key(
    tool_name: str, arguments: dict[str, Any], *, scope: str = "", occurrence: int = 0
) -> str:
    """Stable key for one *invocation*, so a replay returns the recorded result.

    The key is scoped to the agent run and to the invocation's position in it,
    not merely to the tool and its arguments. Hashing the arguments alone made
    every incident in the system share one key per (tool, args) pair: the second
    incident's ``get_service_status`` returned the *first* incident's row, so
    its own ``tool_calls`` table stayed empty and ``finish_tool_call`` rewrote
    another run's audit record. Replay protection has to be per run, or it is
    not protection — it is a merge.
    """
    canonical = json.dumps(arguments, sort_keys=True, default=str)
    digest = hashlib.sha256(
        f"{scope}:{occurrence}:{tool_name}:{canonical}".encode()
    ).hexdigest()
    return digest[:64]
