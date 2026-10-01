"""Incident scenarios — the ground truth the Agent must *not* be able to read.

Each scenario is a complete, self-consistent story:

    trigger → injected faults → observable symptoms → hidden root cause
    → the recovery that actually fixes it → the criteria that prove it

``GET /simulator/scenarios`` returns only the operator-visible half. The
hidden root cause and the correct recovery are served from
``/simulator/ground-truth/{name}``, which is only mounted when the simulator
runs with ``OPSPILOT_SIM_EVAL_MODE=1`` — the Agent has no path to them during
a normal run and has to earn the answer with tool calls.

**Language.** Everything an operator can read — ``title``, ``description``,
``trigger``, ``symptoms``, deployment notes, the hidden root cause — is written
in Chinese, because all of it is rendered verbatim by the console. The
identifiers the code switches on (``name``, ``alert_service``, ``severity``,
``root_cause_category``, ``correct_recovery``, ``runbook_hint``) stay in
English or as slugs: they are wire vocabulary, not copy. ``expected_evidence``
is Chinese because it is matched against Chinese log text at scoring time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Trigger side-effects — what injecting a scenario leaves behind in history
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FaultSpec:
    kind: str
    target: str
    params: dict[str, Any] = field(default_factory=dict)
    intensity: float = 1.0


@dataclass(frozen=True)
class DeployEvent:
    """A deployment record created by the trigger.

    A bad release has to exist in ``get_deployments`` — otherwise no amount of
    investigation could ever correlate the incident with a change.
    """

    service: str
    version: str
    minutes_ago: float = 8.0
    status: str = "success"
    author: str = "ci-bot"
    notes: str = ""
    commit_message: str = ""


@dataclass(frozen=True)
class Criterion:
    service: str
    metric: str
    op: str  # "<=" | ">=" | "==" | "<" | ">"
    threshold: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "metric": self.metric,
            "op": self.op,
            "threshold": self.threshold,
        }


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    description: str
    severity: str
    #: The service the alert fires on — not necessarily where the fault lives.
    alert_service: str
    trigger: str
    symptoms: tuple[str, ...]
    faults: tuple[FaultSpec, ...]
    deploys: tuple[DeployEvent, ...] = ()
    #: Never exposed to the Agent outside eval mode.
    hidden_root_cause: str = ""
    root_cause_category: str = "unknown"
    correct_recovery: tuple[str, ...] = ()
    mitigations: tuple[str, ...] = ()
    verification_criteria: tuple[Criterion, ...] = ()
    expected_evidence: tuple[str, ...] = ()
    runbook_hint: str = ""

    def public_dict(self) -> dict[str, Any]:
        """What an operator (and therefore the Agent) may see."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "severity": self.severity,
            "alert_service": self.alert_service,
            "trigger": self.trigger,
            "symptoms": list(self.symptoms),
            "affected_services": sorted({f.target for f in self.faults}),
        }

    def ground_truth(self) -> dict[str, Any]:
        """Full definition — evaluation only."""
        return {
            **self.public_dict(),
            "hidden_root_cause": self.hidden_root_cause,
            "root_cause_category": self.root_cause_category,
            "correct_recovery": list(self.correct_recovery),
            "mitigations": list(self.mitigations),
            "verification_criteria": [c.as_dict() for c in self.verification_criteria],
            "expected_evidence": list(self.expected_evidence),
            "faults": [
                {"kind": f.kind, "target": f.target, "intensity": f.intensity}
                for f in self.faults
            ],
        }


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


def _healthy(service: str, **extra: Any) -> tuple[Criterion, ...]:
    base = [
        Criterion(service, "error_rate", "<=", 0.01),
        Criterion(service, "latency_p95_ms", "<=", 500.0),
        Criterion(service, "health", "==", "healthy"),
    ]
    base.extend(Criterion(service, k, "<=", v) for k, v in extra.items())
    return tuple(base)


SCENARIOS: dict[str, Scenario] = {}


def _register(scenario: Scenario) -> Scenario:
    SCENARIOS[scenario.name] = scenario
    return scenario


_register(
    Scenario(
        name="checkout-db-pool-exhaustion",
        title="结算服务数据库连接超时，下单大面积失败",
        description=(
            "结算请求成片超时，错误预算正在被烧穿。"
            "第一波报错出现前不久刚发布过一个新版本。"
        ),
        severity="SEV1",
        alert_service="checkout-service",
        trigger="发布 checkout-service v1.8.3（首次报错前 8 分钟）",
        symptoms=(
            "5xx 比例超过 20%",
            "p95 延迟超过 2s",
            "数据库连接池被打满",
        ),
        faults=(
            FaultSpec("db_connection_exhaustion", "checkout-service"),
        ),
        deploys=(
            DeployEvent(
                service="checkout-service",
                version="v1.8.3",
                minutes_ago=8.0,
                notes="checkout：购物车查询复用数据库会话",
                commit_message="perf(checkout): 购物车查询复用数据库会话",
            ),
        ),
        hidden_root_cause=(
            "v1.8.3 把数据库会话的获取挪进了每次购物车查询里，却只在成功路径上释放，"
            "于是每一次失败的查询都会泄漏一个连接，几分钟内就把连接池耗尽。"
        ),
        root_cause_category="database",
        correct_recovery=("rollback_deployment",),
        mitigations=("increase_pool_size",),
        verification_criteria=_healthy("checkout-service", db_connections=28),
        expected_evidence=(
            "连接池",
            "QueuePool",
            "db_connections",
            "v1.8.3",
        ),
        runbook_hint="database/connection-pool",
    )
)

_register(
    Scenario(
        name="redis-failure",
        title="缓存集群整体不可达",
        description=(
            "缓存读取在全集群范围内失败。依赖缓存的服务回退到数据库，"
            "延迟随之上升。"
        ),
        severity="SEV2",
        alert_service="checkout-service",
        trigger="redis 主节点丢失",
        symptoms=(
            "缓存连接被拒绝",
            "走缓存的接口 p95 延迟上升",
            "数据库负载上升",
        ),
        faults=(FaultSpec("redis_failure", "redis"),),
        hidden_root_cause=(
            "Redis 主节点故障，sentinel 尚未完成故障转移，"
            "所有缓存客户端都在快速失败。"
        ),
        root_cause_category="redis",
        correct_recovery=("restart_redis",),
        mitigations=("flush_cache",),
        verification_criteria=(
            Criterion("redis", "health", "==", "healthy"),
            *_healthy("checkout-service"),
        ),
        expected_evidence=("redis", "连接被拒绝", "缓存"),
        runbook_hint="redis/unavailable",
    )
)

_register(
    Scenario(
        name="checkout-memory-leak",
        title="结算服务实例内存逼近上限",
        description=(
            "自上一个版本发布后结算服务内存持续攀升，"
            "实例一旦触到上限就会被杀掉。"
        ),
        severity="SEV2",
        alert_service="checkout-service",
        trigger="发布 checkout-service v1.9.0",
        symptoms=(
            "堆内存占用超过上限的 85%",
            "GC 停顿越来越长",
            "偶发实例重启",
        ),
        faults=(
            FaultSpec(
                "memory_leak",
                "checkout-service",
                {"growth_mb_per_min": 48.0},
            ),
        ),
        deploys=(
            DeployEvent(
                service="checkout-service",
                version="v1.9.0",
                minutes_ago=22.0,
                notes="checkout：加入内存价格缓存",
                commit_message="feat(checkout): 把价格查询缓存在内存里",
            ),
        ),
        hidden_root_cause=(
            "v1.9.0 加了一个没有 TTL、也没有淘汰策略的内存价格缓存，"
            "堆内存一路涨到容器被 OOM 杀掉。"
        ),
        root_cause_category="memory",
        correct_recovery=("rollback_deployment",),
        mitigations=("restart_service",),
        verification_criteria=_healthy("checkout-service", memory_mb=1024.0),
        expected_evidence=("内存", "堆", "v1.9.0", "GC 停顿"),
        runbook_hint="memory/leak",
    )
)

_register(
    Scenario(
        name="payment-bad-deployment",
        title="版本发布后支付授权开始报错",
        description=(
            "发布之后，一部分支付授权请求立刻开始失败。"
        ),
        severity="SEV1",
        alert_service="payment-service",
        trigger="发布 payment-service v2.5.0",
        symptoms=(
            "错误率从 0.2% 跳到 30% 以上",
            "延迟没有劣化",
            "失败与新版本的发布时间吻合",
        ),
        faults=(
            FaultSpec(
                "bad_deployment",
                "payment-service",
                {"error": "ValueError: 金额为负数"},
            ),
        ),
        deploys=(
            DeployEvent(
                service="payment-service",
                version="v2.5.0",
                minutes_ago=6.0,
                notes="payment：重写退款金额校验",
                commit_message="refactor(payment): 重写退款金额校验",
            ),
        ),
        hidden_root_cause=(
            "v2.5.0 重写了退款金额校验，遇到负数金额就抛 ValueError，"
            "而退款本来就可能是负数。"
        ),
        root_cause_category="deployment",
        correct_recovery=("rollback_deployment",),
        mitigations=(),
        verification_criteria=_healthy("payment-service"),
        expected_evidence=("v2.5.0", "发布", "错误率", "ValueError"),
        runbook_hint="deployment/rollback",
    )
)

_register(
    Scenario(
        name="payment-third-party-timeout",
        title="支付渠道大面积超时",
        description=(
            "支付请求先挂起、再失败。合作方渠道一直在返回网关超时。"
        ),
        severity="SEV2",
        alert_service="payment-service",
        trigger="合作方支付 API 劣化",
        symptoms=(
            "p95 延迟超过 8s",
            "渠道侧返回 504",
            "重试预算被打光",
        ),
        faults=(FaultSpec("api_timeout", "external-payment-api"),),
        hidden_root_cause=(
            "合作方支付 API（acme-pay）持续返回 504 Gateway Timeout，"
            "调用方一直阻塞到自己超时为止。"
        ),
        root_cause_category="third_party",
        correct_recovery=("enable_circuit_breaker", "switch_payment_provider"),
        mitigations=(),
        verification_criteria=_healthy("payment-service"),
        expected_evidence=("504", "超时", "external-payment-api", "上游"),
        runbook_hint="payment/timeout",
    )
)

_register(
    Scenario(
        name="postgres-slow-queries",
        title="数据库查询互相阻塞",
        description=(
            "查询从毫秒级变成秒级，连接不断堆积。"
        ),
        severity="SEV1",
        alert_service="checkout-service",
        trigger="一个长时间运行的 migration 留下了阻塞事务",
        symptoms=(
            "查询延迟超过 2s",
            "锁等待超时",
            "连接数持续攀升",
        ),
        faults=(FaultSpec("slow_database", "postgres"),),
        hidden_root_cause=(
            "一个未提交的 migration 一直占着 orders 表的行锁，"
            "普通的结算查询全被堵在它后面，连接因此不断堆积。"
        ),
        root_cause_category="database",
        correct_recovery=("clear_deadlock", "restart_postgres"),
        mitigations=("increase_pool_size",),
        verification_criteria=(
            Criterion("postgres", "latency_p95_ms", "<=", 200.0),
            *_healthy("checkout-service"),
        ),
        expected_evidence=("慢查询", "锁", "postgres", "超时"),
        runbook_hint="database/deadlock",
    )
)

_register(
    Scenario(
        name="inventory-cpu-saturation",
        title="库存服务 CPU 打满",
        description=(
            "库存服务 CPU 钉在高位，请求全堵在调度器后面排队。"
        ),
        severity="SEV3",
        alert_service="inventory-service",
        trigger="库存接口流量激增",
        symptoms=(
            "CPU 超过 90%",
            "请求队列深度持续上涨",
            "延迟随队列同步上升",
        ),
        faults=(FaultSpec("cpu_spike", "inventory-service"),),
        hidden_root_cause=(
            "一波流量激增把库存服务推过了它的预留容量，"
            "每个副本都跑满，请求只能排在调度器后面。"
        ),
        root_cause_category="capacity",
        correct_recovery=("scale_service",),
        mitigations=("restart_service",),
        verification_criteria=_healthy("inventory-service", cpu_percent=80.0),
        expected_evidence=("cpu", "饱和", "队列"),
        runbook_hint="deployment/rollback",
    )
)

_register(
    Scenario(
        name="gateway-dependency-cascade",
        title="网关报错，根因在下游依赖",
        description=(
            "边缘网关成片返回 5xx，但它自己的指标看起来基本正常——"
            "失败来自下游某个组件。"
        ),
        severity="SEV1",
        alert_service="gateway",
        trigger="user-service 实例全部变为未就绪",
        symptoms=(
            "网关 5xx 超过 30%",
            "网关 CPU 与内存正常",
            "user-service 就绪探针失败",
        ),
        faults=(FaultSpec("dependency_failure", "user-service"),),
        hidden_root_cause=(
            "user-service 丢掉了全部就绪副本，被负载均衡摘除，"
            "于是无论网关自身是否健康，总有一部分请求必然失败。"
        ),
        root_cause_category="cascading",
        correct_recovery=("restart_service",),
        mitigations=("enable_circuit_breaker",),
        verification_criteria=(
            Criterion("user-service", "health", "==", "healthy"),
            *_healthy("gateway"),
        ),
        expected_evidence=("user-service", "上游", "503", "依赖"),
        runbook_hint="deployment/rollback",
    )
)

_register(
    Scenario(
        name="payment-high-error-rate",
        title="支付错误预算耗尽",
        description=(
            "支付以很高的比例失败，找不到单一明显的诱因。"
            "最近刚落地过一次发布。"
        ),
        severity="SEV2",
        alert_service="payment-service",
        trigger="未知——报错在上一次发布窗口之后开始",
        symptoms=(
            "大量请求返回 HTTP 500",
            "请求处理里出现未捕获异常",
            "基础设施指标没有劣化",
        ),
        faults=(FaultSpec("high_error_rate", "payment-service"),),
        deploys=(
            DeployEvent(
                service="payment-service",
                version="v2.4.9",
                minutes_ago=11.0,
                notes="payment：升级依赖版本",
                commit_message="chore(payment): 升级 sdk 与 http 客户端",
            ),
        ),
        hidden_root_cause=(
            "v2.4.9 升级了 HTTP 客户端，破坏了连接复用，"
            "导致一部分出站调用在请求处理里直接抛异常。"
        ),
        root_cause_category="deployment",
        correct_recovery=("rollback_deployment",),
        mitigations=("restart_service",),
        verification_criteria=_healthy("payment-service"),
        expected_evidence=("错误率", "v2.4.9", "发布", "500"),
        runbook_hint="deployment/rollback",
    )
)

_register(
    Scenario(
        name="checkout-deployment-cascade",
        title="结算服务的回归在边缘暴露",
        description=(
            "网关是喊得最响的那个，但它自己的指标很干净。"
            "它调用的某个组件正在失败。"
        ),
        severity="SEV1",
        alert_service="gateway",
        trigger="发布 checkout-service v1.8.4",
        symptoms=(
            "网关 5xx 超过 20%",
            "网关延迟被一个慢依赖拉高",
            "checkout-service 错误率抬升",
        ),
        faults=(
            FaultSpec(
                "bad_deployment",
                "checkout-service",
                {"error": "NullPointerException: 购物车金额"},
            ),
        ),
        deploys=(
            DeployEvent(
                service="checkout-service",
                version="v1.8.4",
                minutes_ago=7.0,
                notes="checkout：重写购物车金额计算",
                commit_message="feat(checkout): 带折扣重算购物车金额",
            ),
        ),
        hidden_root_cause=(
            "v1.8.4 重写了购物车金额计算，解引用了一个空的折扣节点，"
            "结算因此抛异常，网关再把由此产生的 5xx 报出来。"
        ),
        root_cause_category="deployment",
        correct_recovery=("rollback_deployment",),
        mitigations=(),
        verification_criteria=(
            Criterion("checkout-service", "health", "==", "healthy"),
            *_healthy("gateway"),
        ),
        expected_evidence=("checkout-service", "v1.8.4", "发布", "500"),
        runbook_hint="deployment/rollback",
    )
)

_register(
    Scenario(
        name="payment-provider-outage",
        title="支付渠道持续返回错误",
        description=(
            "合作方支付渠道对每一次授权尝试都返回服务端错误。"
        ),
        severity="SEV1",
        alert_service="payment-service",
        trigger="合作方渠道整体故障",
        symptoms=(
            "渠道返回 503",
            "授权被拒绝",
            "本地基础设施指标没有劣化",
        ),
        faults=(FaultSpec("third_party_api_failure", "external-payment-api"),),
        hidden_root_cause=(
            "acme-pay 正在经历整体故障，对每一次授权请求都返回 503。"
        ),
        root_cause_category="third_party",
        correct_recovery=("switch_payment_provider", "enable_circuit_breaker"),
        mitigations=(),
        verification_criteria=_healthy("payment-service"),
        expected_evidence=("503", "acme-pay", "支付渠道", "外部"),
        runbook_hint="payment/timeout",
    )
)

_register(
    Scenario(
        name="checkout-cpu-saturation",
        title="结算服务 CPU 打满",
        description=(
            "结算服务受 CPU 限制开始丢弃负载，延迟随队列深度线性上升。"
        ),
        severity="SEV3",
        alert_service="checkout-service",
        trigger="流量激增",
        symptoms=(
            "CPU 超过 90%",
            "延迟随队列深度上升",
            "错误率暂时还没劣化",
        ),
        faults=(FaultSpec("cpu_spike", "checkout-service"),),
        hidden_root_cause=(
            "结算服务正跑在预留容量上限，吃不下当前的流量水位。"
        ),
        root_cause_category="capacity",
        correct_recovery=("scale_service",),
        mitigations=("restart_service",),
        verification_criteria=_healthy("checkout-service", cpu_percent=80.0),
        expected_evidence=("cpu", "饱和", "队列"),
        runbook_hint="deployment/rollback",
    )
)


def get_scenario(name: str) -> Scenario | None:
    return SCENARIOS.get(name)


def list_scenarios() -> list[dict[str, Any]]:
    """Operator-visible metadata. Deliberately excludes the ground truth."""
    return [s.public_dict() for s in SCENARIOS.values()]


def scenario_names() -> list[str]:
    return sorted(SCENARIOS)


__all__ = [
    "Criterion",
    "DeployEvent",
    "FaultSpec",
    "SCENARIOS",
    "Scenario",
    "get_scenario",
    "list_scenarios",
    "scenario_names",
]
