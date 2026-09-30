"""The simulated production environment: topology, baselines, state derivation.

This is a *model*, not a fixture. A service's observed metrics are derived
from three things:

1. its own baseline,
2. the faults currently attached to it (which may progress over time),
3. the health of everything it depends on — propagated along the edges of the
   dependency graph.

Because (3) exists, injecting a fault into ``postgres`` really does degrade
``checkout-service`` and then ``gateway``. A recovery action only "works" if
it removes the fault that is actually present, which is what makes
*Recovery → Verification* a meaningful closed loop instead of a scripted one.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

SERVICE = "service"
CACHE = "cache"
DATASTORE = "datastore"
EXTERNAL = "external"

METRIC_FIELDS: tuple[str, ...] = (
    "request_rate",
    "error_rate",
    "latency_p50",
    "latency_p95",
    "cpu",
    "memory_mb",
    "db_connections",
)

#: Metric name → how it is rendered to an operator.
METRIC_UNITS: dict[str, str] = {
    "request_rate": "rpm",
    "error_rate": "ratio",
    "latency_p50": "ms",
    "latency_p95": "ms",
    "cpu": "percent",
    "memory_mb": "MB",
    "db_connections": "connections",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceSpec:
    """Static definition of one component in the simulated environment."""

    name: str
    kind: str = SERVICE
    depends_on: tuple[tuple[str, str, float], ...] = ()
    """(target, edge_type, criticality) — criticality in [0, 1] is how much of
    the dependency's pain is transmitted upstream."""

    request_rate: float = 100.0
    error_rate: float = 0.002
    latency_p50: float = 40.0
    latency_p95: float = 120.0
    cpu: float = 25.0
    memory_mb: float = 512.0
    db_connections: int = 8

    pool_max: int = 0
    """Size of this component's own connection pool. 0 = no pool."""
    memory_limit_mb: float = 2048.0
    replicas: int = 2
    version: str = "v1.0.0"
    owner: str = "platform"

    def baseline(self) -> dict[str, float]:
        return {
            "request_rate": self.request_rate,
            "error_rate": self.error_rate,
            "latency_p50": self.latency_p50,
            "latency_p95": self.latency_p95,
            "cpu": self.cpu,
            "memory_mb": self.memory_mb,
            "db_connections": float(self.db_connections),
        }


# The environment the Agent is asked to reason about.
#
#     gateway
#      ├── user-service ──┬── redis
#      │                  └── postgres
#      ├── checkout-service ─┬── payment-service ──┬── postgres
#      │                     │                     └── external-payment-api
#      │                     ├── inventory-service ── postgres
#      │                     ├── postgres
#      │                     └── redis
#      └── inventory-service
#
# Edge criticality encodes how badly a dependency failure hurts the caller.
TOPOLOGY: tuple[ServiceSpec, ...] = (
    ServiceSpec(
        name="gateway",
        depends_on=(
            ("user-service", "sync_call", 0.7),
            ("checkout-service", "sync_call", 1.0),
            ("inventory-service", "sync_call", 0.3),
        ),
        request_rate=900.0,
        error_rate=0.0005,
        latency_p50=18.0,
        latency_p95=55.0,
        cpu=18.0,
        memory_mb=320.0,
        db_connections=0,
        replicas=3,
        version="v4.2.1",
    ),
    ServiceSpec(
        name="checkout-service",
        depends_on=(
            ("payment-service", "sync_call", 0.9),
            ("inventory-service", "sync_call", 0.6),
            ("postgres", "datastore", 0.85),
            ("redis", "cache", 0.35),
        ),
        request_rate=260.0,
        error_rate=0.001,
        latency_p50=65.0,
        latency_p95=190.0,
        cpu=34.0,
        memory_mb=720.0,
        db_connections=18,
        pool_max=40,
        memory_limit_mb=2048.0,
        replicas=3,
        version="v1.8.2",
    ),
    ServiceSpec(
        name="payment-service",
        depends_on=(
            ("postgres", "datastore", 0.8),
            ("external-payment-api", "sync_call", 0.95),
        ),
        request_rate=180.0,
        error_rate=0.001,
        latency_p50=70.0,
        latency_p95=160.0,
        cpu=30.0,
        memory_mb=640.0,
        db_connections=14,
        pool_max=30,
        replicas=2,
        version="v2.4.0",
    ),
    ServiceSpec(
        name="user-service",
        depends_on=(("redis", "cache", 0.5), ("postgres", "datastore", 0.4)),
        request_rate=420.0,
        error_rate=0.0005,
        latency_p50=25.0,
        latency_p95=70.0,
        cpu=22.0,
        memory_mb=480.0,
        db_connections=10,
        pool_max=25,
        replicas=2,
        version="v3.1.4",
    ),
    ServiceSpec(
        name="inventory-service",
        depends_on=(("postgres", "datastore", 0.75),),
        request_rate=300.0,
        error_rate=0.0008,
        latency_p50=35.0,
        latency_p95=110.0,
        cpu=26.0,
        memory_mb=560.0,
        db_connections=12,
        pool_max=30,
        replicas=2,
        version="v2.0.7",
    ),
    ServiceSpec(
        name="redis",
        kind=CACHE,
        request_rate=1500.0,
        error_rate=0.0,
        latency_p50=1.0,
        latency_p95=4.0,
        cpu=12.0,
        memory_mb=256.0,
        db_connections=0,
        replicas=3,
        version="v7.2.4",
    ),
    ServiceSpec(
        name="postgres",
        kind=DATASTORE,
        request_rate=2400.0,
        error_rate=0.0002,
        latency_p50=6.0,
        latency_p95=22.0,
        cpu=38.0,
        memory_mb=4096.0,
        memory_limit_mb=8192.0,
        db_connections=45,
        pool_max=120,
        replicas=1,
        version="v16.2",
    ),
    ServiceSpec(
        name="external-payment-api",
        kind=EXTERNAL,
        request_rate=180.0,
        error_rate=0.0005,
        latency_p50=90.0,
        latency_p95=210.0,
        cpu=0.0,
        memory_mb=0.0,
        db_connections=0,
        replicas=1,
        version="partner-v3",
        owner="acme-pay",
    ),
)

SERVICE_NAMES: tuple[str, ...] = tuple(s.name for s in TOPOLOGY)
SPEC_BY_NAME: dict[str, ServiceSpec] = {s.name: s for s in TOPOLOGY}

#: Leaf-first evaluation order. A component is only computed once everything
#: it depends on has been computed, so propagation is a single pass.
_EVALUATION_ORDER: list[str] = []


def _compute_evaluation_order() -> list[str]:
    order: list[str] = []
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in order or name in visiting:
            return
        visiting.add(name)
        for dep, _kind, _crit in SPEC_BY_NAME[name].depends_on:
            if dep in SPEC_BY_NAME:
                visit(dep)
        visiting.discard(name)
        order.append(name)

    for spec in TOPOLOGY:
        visit(spec.name)
    return order


_EVALUATION_ORDER = _compute_evaluation_order()


def dependency_graph() -> dict[str, Any]:
    """Topology as the Agent sees it through ``get_dependencies``."""
    return {
        "nodes": [
            {"name": s.name, "kind": s.kind, "version": s.version, "owner": s.owner}
            for s in TOPOLOGY
        ],
        "edges": [
            {
                "service": s.name,
                "depends_on": dep,
                "type": kind,
                "criticality": crit,
            }
            for s in TOPOLOGY
            for dep, kind, crit in s.depends_on
        ],
        "entrypoints": ["gateway"],
    }


def upstream_of(name: str) -> list[str]:
    return [dep for dep, _k, _c in SPEC_BY_NAME[name].depends_on]


def downstream_of(name: str) -> list[str]:
    return [
        s.name for s in TOPOLOGY if any(dep == name for dep, _k, _c in s.depends_on)
    ]


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------


@dataclass
class ActiveFault:
    """A fault currently attached to a component."""

    kind: str
    target: str
    started_at: datetime
    params: dict[str, Any] = field(default_factory=dict)
    intensity: float = 1.0
    """0 → 1 ramp. Faults ramp up over a few minutes rather than appearing
    fully-formed, because real incidents have a slope."""
    clock_offset: float = 0.0
    """Minutes added to the elapsed time. Used when the engine replays the
    recent past so metric charts show the ramp instead of a step."""

    @property
    def elapsed_minutes(self) -> float:
        return max(
            0.0,
            (_now() - self.started_at).total_seconds() / 60.0 + self.clock_offset,
        )


@dataclass
class ServiceRuntime:
    """Mutable per-component state."""

    spec: ServiceSpec
    version: str = ""
    replicas: int = 0
    restart_count: int = 0
    faults: list[ActiveFault] = field(default_factory=list)
    deployments: list[dict[str, Any]] = field(default_factory=list)
    commits: list[dict[str, Any]] = field(default_factory=list)
    metrics_history: list[tuple[datetime, dict[str, float]]] = field(default_factory=list)
    log_buffer: list[dict[str, Any]] = field(default_factory=list)
    #: Faults that progression has fully materialised (used for pool/leak).
    pool_open: float = 0.0
    memory_growth_mb: float = 0.0
    circuit_breaker_open: bool = False
    cleared_at: datetime | None = None
    #: Last computed metrics of every dependency, filled in by the engine so
    #: log generation can explain "my caller is fine, my dependency is not".
    dep_metrics: dict[str, dict[str, float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.version:
            self.version = self.spec.version
        if not self.replicas:
            self.replicas = self.spec.replicas

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def pool_utilisation(self) -> float:
        if not self.spec.pool_max:
            return 0.0
        return min(1.0, self.pool_open / float(self.spec.pool_max))

    def has_fault(self, *kinds: str) -> bool:
        return any(f.kind in kinds for f in self.faults)

    def remove_faults(self, *kinds: str) -> list[ActiveFault]:
        removed = [f for f in self.faults if f.kind in kinds]
        self.faults = [f for f in self.faults if f.kind not in kinds]
        return removed


def _jitter(seed: str, index: int, spread: float) -> float:
    """Deterministic pseudo-noise so a chart does not look like a flat line
    but is identical across restarts (a dashboard that jitters on refresh
    reads as broken)."""
    rng = random.Random(f"{seed}:{index}")
    return (rng.random() - 0.5) * 2.0 * spread


# ---------------------------------------------------------------------------
# Metric derivation
# ---------------------------------------------------------------------------


def _fault_direct_effect(
    rt: ServiceRuntime, metrics: dict[str, float]
) -> dict[str, float]:
    """Apply the component's own faults. See :mod:`opspilot_simulator.faults`."""
    from opspilot_simulator.faults import apply_faults

    return apply_faults(rt, metrics)


def derive_metrics(
    runtimes: dict[str, ServiceRuntime],
) -> dict[str, dict[str, float]]:
    """Compute the current metric snapshot for every component.

    Runs leaf-first so a dependency's degradation is already known by the time
    its callers are evaluated.
    """
    computed: dict[str, dict[str, float]] = {}

    for name in _EVALUATION_ORDER:
        rt = runtimes[name]
        spec = rt.spec
        metrics = spec.baseline()

        # 1. Faults attached to this component (may progress over time).
        metrics = _fault_direct_effect(rt, metrics)

        # 2. Propagate pain from dependencies.
        for dep, _kind, criticality in spec.depends_on:
            dep_metrics = computed.get(dep)
            if dep_metrics is None:
                continue
            dep_error = dep_metrics["error_rate"]
            dep_latency = dep_metrics["latency_p95"]

            # A failing dependency shows up as latency first, then errors.
            #
            # The coefficients are damped deliberately: propagating a
            # dependency's full latency up every edge compounds, and after
            # three hops a perfectly healthy stack would look like an outage.
            metrics["latency_p50"] += dep_metrics["latency_p50"] * criticality * 0.35
            metrics["latency_p95"] += dep_latency * criticality * 0.45
            metrics["error_rate"] += dep_error * criticality * 0.85

            # Downstream saturation makes callers hold connections longer.
            if spec.pool_max and dep_metrics.get("db_connections"):
                dep_util = dep_metrics["db_connections"] / max(
                    1.0, float(SPEC_BY_NAME[dep].pool_max or 1)
                )
                if dep_util > 0.7:
                    metrics["db_connections"] *= 1.0 + (dep_util - 0.7) * 2.0

            # A broken cache means every request falls through to the DB.
            if dep == "redis" and dep_metrics["error_rate"] > 0.05:
                metrics["cpu"] += 18.0 * criticality
                metrics["latency_p95"] += 45.0 * criticality
                if spec.pool_max:
                    metrics["db_connections"] *= 1.35

        # 3. Replicas spread the load (but never fix a bad code path).
        extra = max(0, rt.replicas - spec.replicas)
        if extra:
            relief = 1.0 / (1.0 + 0.35 * extra)
            metrics["cpu"] *= relief
            metrics["latency_p50"] *= 1.0 - min(0.30, 0.10 * extra)
            metrics["latency_p95"] *= 1.0 - min(0.30, 0.10 * extra)
            metrics["memory_mb"] = spec.baseline()["memory_mb"]

        # 4. Clamp to physically sensible ranges.
        metrics["cpu"] = max(0.0, min(100.0, metrics["cpu"]))
        metrics["error_rate"] = max(0.0, min(1.0, metrics["error_rate"]))
        metrics["latency_p50"] = max(0.5, metrics["latency_p50"])
        metrics["latency_p95"] = max(metrics["latency_p50"], metrics["latency_p95"])
        metrics["memory_mb"] = max(16.0, metrics["memory_mb"])
        metrics["db_connections"] = max(
            0.0, min(float(spec.pool_max or 0) or 1e6, metrics["db_connections"])
        )
        if not spec.pool_max:
            metrics["db_connections"] = 0.0
        metrics["request_rate"] = max(0.0, metrics["request_rate"])

        computed[name] = metrics

    return computed


#: What each component measures when *nothing is wrong*. Captured once at
#: startup and used as the reference for "is this component healthy?".
#:
#: Comparing against ``ServiceSpec.latency_p95`` instead would be wrong: that
#: field is the component's own contribution, while the observed value also
#: includes everything it calls. Without this, a healthy gateway — whose p95
#: legitimately includes checkout and the database — would read as degraded.
_HEALTH_BASELINES: dict[str, dict[str, float]] = {}


def set_health_baselines(baselines: dict[str, dict[str, float]]) -> None:
    global _HEALTH_BASELINES
    _HEALTH_BASELINES = dict(baselines)


def health_baseline(name: str) -> dict[str, float]:
    return _HEALTH_BASELINES.get(name) or SPEC_BY_NAME[name].baseline()


def health_of(metrics: dict[str, float], spec: ServiceSpec) -> str:
    """Health is *derived*, never set — so it can never disagree with metrics."""
    error_rate = metrics["error_rate"]
    latency = metrics["latency_p95"]
    base = health_baseline(spec.name)
    base_latency = max(1.0, base["latency_p95"])
    base_error = base["error_rate"]
    util = (
        metrics["db_connections"] / float(spec.pool_max) if spec.pool_max else 0.0
    )
    memory_ratio = (
        metrics["memory_mb"] / spec.memory_limit_mb if spec.memory_limit_mb else 0.0
    )

    if error_rate >= 0.35 or util >= 0.98 or memory_ratio >= 0.98:
        return "down"
    if util >= 0.85 or memory_ratio >= 0.9:
        return "degraded"
    # Thresholds are relative to *this component's* normal, not to an
    # absolute constant — 8% error rate is an outage for checkout and a
    # slow Tuesday for a partner API nobody depends on heavily.
    if error_rate >= max(0.05, base_error * 12):
        return "degraded"
    if latency >= base_latency * 2.5:
        return "degraded"
    return "healthy"


def service_snapshot(
    rt: ServiceRuntime, metrics: dict[str, float]
) -> dict[str, Any]:
    """The payload ``GET /simulator/services/{name}`` returns."""
    spec = rt.spec
    health = health_of(metrics, spec)
    return {
        "service": spec.name,
        "kind": spec.kind,
        "health": health,
        "version": rt.version,
        "replicas": rt.replicas,
        "restart_count": rt.restart_count,
        "error_rate": round(metrics["error_rate"], 5),
        "latency_p50_ms": round(metrics["latency_p50"], 2),
        "latency_p95_ms": round(metrics["latency_p95"], 2),
        "request_rate": round(metrics["request_rate"], 2),
        "cpu_percent": round(metrics["cpu"], 2),
        "memory_mb": round(metrics["memory_mb"], 1),
        "memory_limit_mb": spec.memory_limit_mb,
        "db_connections": int(round(metrics["db_connections"])),
        "pool_max": spec.pool_max,
        "pool_utilisation": round(rt.pool_utilisation, 4),
        "circuit_breaker_open": rt.circuit_breaker_open,
        "active_faults": [f.kind for f in rt.faults],
        "depends_on": [d for d, _k, _c in spec.depends_on],
    }


__all__ = [
    "ActiveFault",
    "CACHE",
    "DATASTORE",
    "EXTERNAL",
    "METRIC_FIELDS",
    "METRIC_UNITS",
    "SERVICE",
    "SERVICE_NAMES",
    "SPEC_BY_NAME",
    "ServiceRuntime",
    "ServiceSpec",
    "TOPOLOGY",
    "dependency_graph",
    "derive_metrics",
    "downstream_of",
    "health_of",
    "service_snapshot",
    "upstream_of",
    "_jitter",
    "_now",
]
