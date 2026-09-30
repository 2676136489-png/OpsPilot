"""Incident scenarios — the ground truth the Agent must *not* be able to read.

Each scenario is a complete, self-consistent story:

    trigger → injected faults → observable symptoms → hidden root cause
    → the recovery that actually fixes it → the criteria that prove it

``GET /simulator/scenarios`` returns only the operator-visible half. The
hidden root cause and the correct recovery are served from
``/simulator/ground-truth/{name}``, which is only mounted when the simulator
runs with ``OPSPILOT_SIM_EVAL_MODE=1`` — the Agent has no path to them during
a normal run and has to earn the answer with tool calls.
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
        title="Checkout failing on database connection timeouts",
        description=(
            "Checkout requests are timing out and the error budget is burning. "
            "A new release went out shortly before the first errors."
        ),
        severity="SEV1",
        alert_service="checkout-service",
        trigger="deploy checkout-service v1.8.3 (8 minutes before first error)",
        symptoms=(
            "5xx rate above 20%",
            "p95 latency above 2s",
            "database connection pool saturated",
        ),
        faults=(
            FaultSpec("db_connection_exhaustion", "checkout-service"),
        ),
        deploys=(
            DeployEvent(
                service="checkout-service",
                version="v1.8.3",
                minutes_ago=8.0,
                notes="checkout: reuse session for cart lookups",
                commit_message="perf(checkout): reuse db session across cart lookups",
            ),
        ),
        hidden_root_cause=(
            "v1.8.3 acquires a database session per cart lookup but only releases it "
            "on the success path, so every failed lookup leaks a connection and the "
            "pool is exhausted within minutes."
        ),
        root_cause_category="database",
        correct_recovery=("rollback_deployment",),
        mitigations=("increase_pool_size",),
        verification_criteria=_healthy("checkout-service", db_connections=28),
        expected_evidence=(
            "connection pool",
            "queuepool",
            "db_connections",
            "v1.8.3",
        ),
        runbook_hint="database/connection-pool",
    )
)

_register(
    Scenario(
        name="redis-failure",
        title="Cache cluster unreachable",
        description=(
            "Cache reads are failing cluster-wide. Services that depend on the "
            "cache are falling back to the database and slowing down."
        ),
        severity="SEV2",
        alert_service="checkout-service",
        trigger="redis primary node lost",
        symptoms=(
            "cache connection refused",
            "p95 latency rising on cached endpoints",
            "database load increasing",
        ),
        faults=(FaultSpec("redis_failure", "redis"),),
        hidden_root_cause=(
            "The Redis primary node failed and sentinel has not completed failover, "
            "so every cache client is failing fast."
        ),
        root_cause_category="redis",
        correct_recovery=("restart_redis",),
        mitigations=("flush_cache",),
        verification_criteria=(
            Criterion("redis", "health", "==", "healthy"),
            *_healthy("checkout-service"),
        ),
        expected_evidence=("redis", "connection refused", "cache"),
        runbook_hint="redis/unavailable",
    )
)

_register(
    Scenario(
        name="checkout-memory-leak",
        title="Checkout pods approaching OOM",
        description=(
            "Checkout memory is climbing steadily since the last release and pods "
            "are being killed once they hit the limit."
        ),
        severity="SEV2",
        alert_service="checkout-service",
        trigger="deploy checkout-service v1.9.0",
        symptoms=(
            "heap usage above 85% of limit",
            "garbage collection pauses growing",
            "occasional pod restarts",
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
                notes="checkout: add in-memory price cache",
                commit_message="feat(checkout): cache price lookups in memory",
            ),
        ),
        hidden_root_cause=(
            "v1.9.0 added an unbounded in-memory price cache with no TTL or "
            "eviction, so the heap grows until the container is OOM-killed."
        ),
        root_cause_category="memory",
        correct_recovery=("rollback_deployment",),
        mitigations=("restart_service",),
        verification_criteria=_healthy("checkout-service", memory_mb=1024.0),
        expected_evidence=("memory", "heap", "v1.9.0", "gc pause"),
        runbook_hint="memory/leak",
    )
)

_register(
    Scenario(
        name="payment-bad-deployment",
        title="Payment errors after release",
        description=(
            "Payment authorisation is failing for a subset of requests immediately "
            "after a release."
        ),
        severity="SEV1",
        alert_service="payment-service",
        trigger="deploy payment-service v2.5.0",
        symptoms=(
            "error rate jumped from 0.2% to over 30%",
            "no latency regression",
            "failures correlate with the new release",
        ),
        faults=(
            FaultSpec(
                "bad_deployment",
                "payment-service",
                {"error": "ValueError: negative currency amount"},
            ),
        ),
        deploys=(
            DeployEvent(
                service="payment-service",
                version="v2.5.0",
                minutes_ago=6.0,
                notes="payment: refund validation rewrite",
                commit_message="refactor(payment): rewrite refund amount validation",
            ),
        ),
        hidden_root_cause=(
            "v2.5.0 rewrote refund amount validation and throws ValueError on "
            "negative amounts, which refunds legitimately produce."
        ),
        root_cause_category="deployment",
        correct_recovery=("rollback_deployment",),
        mitigations=(),
        verification_criteria=_healthy("payment-service"),
        expected_evidence=("v2.5.0", "deployment", "error rate", "ValueError"),
        runbook_hint="deployment/rollback",
    )
)

_register(
    Scenario(
        name="payment-third-party-timeout",
        title="Payment provider timing out",
        description=(
            "Payment requests hang and then fail. The partner provider is returning "
            "gateway timeouts."
        ),
        severity="SEV2",
        alert_service="payment-service",
        trigger="partner payment API degradation",
        symptoms=(
            "p95 latency above 8s",
            "504 responses from the provider",
            "retry budget exhausted",
        ),
        faults=(FaultSpec("api_timeout", "external-payment-api"),),
        hidden_root_cause=(
            "The partner payment API (acme-pay) is returning 504 Gateway Timeout; "
            "callers block until their own deadline expires."
        ),
        root_cause_category="third_party",
        correct_recovery=("enable_circuit_breaker", "switch_payment_provider"),
        mitigations=(),
        verification_criteria=_healthy("payment-service"),
        expected_evidence=("504", "timeout", "external-payment-api", "upstream"),
        runbook_hint="payment/timeout",
    )
)

_register(
    Scenario(
        name="postgres-slow-queries",
        title="Database queries blocking",
        description=(
            "Queries are taking seconds instead of milliseconds and connections are "
            "piling up."
        ),
        severity="SEV1",
        alert_service="checkout-service",
        trigger="long-running migration left blocking transactions",
        symptoms=(
            "query latency above 2s",
            "lock wait timeouts",
            "connection count climbing",
        ),
        faults=(FaultSpec("slow_database", "postgres"),),
        hidden_root_cause=(
            "An uncommitted migration is holding row locks on orders, so ordinary "
            "checkout queries block behind it and connections accumulate."
        ),
        root_cause_category="database",
        correct_recovery=("clear_deadlock", "restart_postgres"),
        mitigations=("increase_pool_size",),
        verification_criteria=(
            Criterion("postgres", "latency_p95_ms", "<=", 200.0),
            *_healthy("checkout-service"),
        ),
        expected_evidence=("slow query", "lock", "postgres", "timeout"),
        runbook_hint="database/deadlock",
    )
)

_register(
    Scenario(
        name="inventory-cpu-saturation",
        title="Inventory service CPU saturated",
        description=(
            "Inventory is pinned at high CPU and requests are queueing behind the "
            "scheduler."
        ),
        severity="SEV3",
        alert_service="inventory-service",
        trigger="traffic surge on stock endpoints",
        symptoms=(
            "CPU above 90%",
            "request queue depth rising",
            "latency rising proportionally",
        ),
        faults=(FaultSpec("cpu_spike", "inventory-service"),),
        hidden_root_cause=(
            "A traffic surge pushed inventory past its provisioned capacity; each "
            "replica is saturated and requests queue behind the scheduler."
        ),
        root_cause_category="capacity",
        correct_recovery=("scale_service",),
        mitigations=("restart_service",),
        verification_criteria=_healthy("inventory-service", cpu_percent=80.0),
        expected_evidence=("cpu", "saturation", "queue"),
        runbook_hint="deployment/rollback",
    )
)

_register(
    Scenario(
        name="gateway-dependency-cascade",
        title="Gateway errors caused by a failing dependency",
        description=(
            "The edge gateway is returning 5xx but its own metrics look mostly "
            "fine — the failures come from somewhere downstream."
        ),
        severity="SEV1",
        alert_service="gateway",
        trigger="user-service replicas became unready",
        symptoms=(
            "gateway 5xx above 30%",
            "gateway CPU and memory normal",
            "user-service readiness probe failing",
        ),
        faults=(FaultSpec("dependency_failure", "user-service"),),
        hidden_root_cause=(
            "user-service lost all ready replicas and was removed from the load "
            "balancer, so a share of gateway requests fail regardless of gateway "
            "health."
        ),
        root_cause_category="cascading",
        correct_recovery=("restart_service",),
        mitigations=("enable_circuit_breaker",),
        verification_criteria=(
            Criterion("user-service", "health", "==", "healthy"),
            *_healthy("gateway"),
        ),
        expected_evidence=("user-service", "upstream", "503", "dependency"),
        runbook_hint="deployment/rollback",
    )
)

_register(
    Scenario(
        name="payment-high-error-rate",
        title="Payment error budget exhausted",
        description=(
            "Payments are failing at a high rate with no single obvious cause. A "
            "release landed recently."
        ),
        severity="SEV2",
        alert_service="payment-service",
        trigger="unknown — errors began after the last release window",
        symptoms=(
            "HTTP 500 on a large share of requests",
            "unhandled exceptions in the request handler",
            "no infrastructure metric regression",
        ),
        faults=(FaultSpec("high_error_rate", "payment-service"),),
        deploys=(
            DeployEvent(
                service="payment-service",
                version="v2.4.9",
                minutes_ago=11.0,
                notes="payment: dependency bumps",
                commit_message="chore(payment): bump sdk and http client",
            ),
        ),
        hidden_root_cause=(
            "v2.4.9 bumped the HTTP client and broke connection reuse, so a share "
            "of outbound calls throw inside the request handler."
        ),
        root_cause_category="deployment",
        correct_recovery=("rollback_deployment",),
        mitigations=("restart_service",),
        verification_criteria=_healthy("payment-service"),
        expected_evidence=("error rate", "v2.4.9", "deployment", "500"),
        runbook_hint="deployment/rollback",
    )
)

_register(
    Scenario(
        name="checkout-deployment-cascade",
        title="Checkout regression surfacing at the edge",
        description=(
            "The gateway is the loudest alarm, but its own metrics are clean. "
            "Something it calls is failing."
        ),
        severity="SEV1",
        alert_service="gateway",
        trigger="deploy checkout-service v1.8.4",
        symptoms=(
            "gateway 5xx above 20%",
            "gateway latency inflated by a slow dependency",
            "checkout-service error rate elevated",
        ),
        faults=(
            FaultSpec(
                "bad_deployment",
                "checkout-service",
                {"error": "NullPointerException: cart totals"},
            ),
        ),
        deploys=(
            DeployEvent(
                service="checkout-service",
                version="v1.8.4",
                minutes_ago=7.0,
                notes="checkout: new cart totals calculation",
                commit_message="feat(checkout): recompute cart totals with discounts",
            ),
        ),
        hidden_root_cause=(
            "v1.8.4 rewrote cart total calculation and dereferences a null discount "
            "node, so checkout throws and the gateway reports the resulting 5xx."
        ),
        root_cause_category="deployment",
        correct_recovery=("rollback_deployment",),
        mitigations=(),
        verification_criteria=(
            Criterion("checkout-service", "health", "==", "healthy"),
            *_healthy("gateway"),
        ),
        expected_evidence=("checkout-service", "v1.8.4", "deployment", "500"),
        runbook_hint="deployment/rollback",
    )
)

_register(
    Scenario(
        name="payment-provider-outage",
        title="Payment provider returning errors",
        description=(
            "The partner payment provider is returning server errors for every "
            "authorisation attempt."
        ),
        severity="SEV1",
        alert_service="payment-service",
        trigger="partner provider outage",
        symptoms=(
            "503 from the provider",
            "authorisation rejected",
            "no local infrastructure regression",
        ),
        faults=(FaultSpec("third_party_api_failure", "external-payment-api"),),
        hidden_root_cause=(
            "acme-pay is experiencing a full outage and returns 503 for every "
            "authorisation request."
        ),
        root_cause_category="third_party",
        correct_recovery=("switch_payment_provider", "enable_circuit_breaker"),
        mitigations=(),
        verification_criteria=_healthy("payment-service"),
        expected_evidence=("503", "acme-pay", "provider", "external"),
        runbook_hint="payment/timeout",
    )
)

_register(
    Scenario(
        name="checkout-cpu-saturation",
        title="Checkout CPU saturated",
        description=(
            "Checkout is CPU-bound and shedding load; latency scales with the "
            "queue depth."
        ),
        severity="SEV3",
        alert_service="checkout-service",
        trigger="traffic surge",
        symptoms=(
            "CPU above 90%",
            "latency increasing with queue depth",
            "no error-rate regression yet",
        ),
        faults=(FaultSpec("cpu_spike", "checkout-service"),),
        hidden_root_cause=(
            "Checkout is running at its provisioned capacity and cannot absorb the "
            "current traffic level."
        ),
        root_cause_category="capacity",
        correct_recovery=("scale_service",),
        mitigations=("restart_service",),
        verification_criteria=_healthy("checkout-service", cpu_percent=80.0),
        expected_evidence=("cpu", "saturation", "queue"),
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
