"""Fault models — what can break, how it progresses, and what fixes it.

Every fault declares:

* ``apply``    — how it moves the component's own metrics,
* ``progress`` — how it evolves minute by minute (leaks grow, pools drain),
* ``logs``     — the log lines a component in this state would emit,
* ``resistant_to`` — actions that *cannot* fix it.

``resistant_to`` is what stops the Agent from "solving" every incident with a
restart. A connection-pool leak lives in the deployed code, so bouncing the
pods drains the pool for a few seconds and then it fills again: the simulated
environment says so, and verification fails until the real fix is applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from opspilot_simulator.world import ActiveFault, ServiceRuntime, _now

LogLine = tuple[str, str]  # (level, message)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ramp(fault: ActiveFault, minutes: float = 3.0) -> float:
    """0 → 1 over ``minutes``, so incidents have a slope, not a step."""
    return max(0.0, min(1.0, fault.elapsed_minutes / max(0.001, minutes)))


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


# ---------------------------------------------------------------------------
# Fault models
# ---------------------------------------------------------------------------


@dataclass
class FaultModel:
    kind: str
    label: str
    applies_to: tuple[str, ...] = ("service", "cache", "datastore", "external")
    resistant_to: frozenset[str] = frozenset()
    #: True when the fault accumulates inside the running process, so bouncing
    #: the process resets the clock. This is why a restart "fixes" an OOM for
    #: a while and then the leak climbs again.
    progressive: bool = False
    apply: Callable[[ActiveFault, ServiceRuntime, dict[str, float]], None] | None = None
    progress: Callable[[ActiveFault, ServiceRuntime], None] | None = None
    logs: Callable[[ActiveFault, ServiceRuntime, dict[str, float]], list[LogLine]] | None = None


def _apply_db_connection_exhaustion(
    fault: ActiveFault, rt: ServiceRuntime, m: dict[str, float]
) -> None:
    ramp = _ramp(fault, 4.0) * fault.intensity
    util = rt.pool_utilisation
    m["db_connections"] = rt.pool_open
    m["error_rate"] += 0.34 * ramp * (0.4 + 0.6 * util)
    m["latency_p50"] += 700.0 * ramp
    m["latency_p95"] += 2100.0 * ramp
    m["cpu"] += 22.0 * ramp
    m["memory_mb"] += 120.0 * ramp


def _progress_db_connection_exhaustion(fault: ActiveFault, rt: ServiceRuntime) -> None:
    base = float(rt.spec.db_connections)
    ceiling = float(rt.spec.pool_max or base)
    # Drains towards the ceiling with a saturating curve — fast at first,
    # then asymptotic, exactly like a real pool under a leak.
    minutes = fault.elapsed_minutes
    target = base + (ceiling - base) * (1.0 - pow(2.718, -minutes / 3.0))
    # Absolute assignment, not max(): the engine replays the past and the pool
    # must show the ramp, not the value it eventually reached.
    rt.pool_open = min(ceiling, target * fault.intensity)


def _logs_db_connection_exhaustion(
    fault: ActiveFault, rt: ServiceRuntime, m: dict[str, float]
) -> list[LogLine]:
    util = rt.pool_utilisation
    out: list[LogLine] = []
    if util >= 0.9:
        out.append(
            (
                "ERROR",
                f"sqlalchemy.exc.TimeoutError: QueuePool limit of size "
                f"{rt.spec.pool_max} overflow {int(rt.spec.pool_max * 0.1)} reached, "
                f"connection timed out, timeout 30.00",
            )
        )
        out.append(
            (
                "ERROR",
                f"failed to acquire database connection after 30000ms "
                f"(active={int(rt.pool_open)}/{rt.spec.pool_max})",
            )
        )
    if util >= 0.7:
        out.append(
            (
                "WARN",
                f"Database connection pool saturation at {_pct(util)} — queuing requests",
            )
        )
    out.append(
        (
            "ERROR",
            f"checkout request failed: upstream dependency timeout after "
            f"{int(m['latency_p95'])}ms",
        )
    )
    return out


def _apply_memory_leak(
    fault: ActiveFault, rt: ServiceRuntime, m: dict[str, float]
) -> None:
    limit = rt.spec.memory_limit_mb
    m["memory_mb"] = min(limit * 1.02, rt.spec.memory_mb + rt.memory_growth_mb)
    ratio = m["memory_mb"] / limit
    if ratio >= 0.85:
        over = (ratio - 0.85) / 0.15
        m["error_rate"] += 0.22 * min(1.0, over)
        m["latency_p95"] += 300.0 * min(1.0, over)
        m["cpu"] += 25.0 * min(1.0, over)
    m["cpu"] += 8.0 * _ramp(fault, 6.0)


def _progress_memory_leak(fault: ActiveFault, rt: ServiceRuntime) -> None:
    rate = float(fault.params.get("growth_mb_per_min", 42.0))
    rt.memory_growth_mb = rate * fault.elapsed_minutes * fault.intensity


def _logs_memory_leak(
    fault: ActiveFault, rt: ServiceRuntime, m: dict[str, float]
) -> list[LogLine]:
    ratio = m["memory_mb"] / rt.spec.memory_limit_mb
    out: list[LogLine] = []
    if ratio >= 0.95:
        out.append(
            (
                "ERROR",
                f"OutOfMemoryError: heap space — {int(m['memory_mb'])}MB of "
                f"{int(rt.spec.memory_limit_mb)}MB used, container will be OOM-killed",
            )
        )
    if ratio >= 0.85:
        out.append(
            (
                "WARN",
                f"GC pause 480ms — heap at {_pct(ratio)}, suspected unbounded retention",
            )
        )
    out.append(
        ("WARN", f"heap usage {int(m['memory_mb'])}MB (limit {int(rt.spec.memory_limit_mb)}MB)")
    )
    return out


def _apply_cpu_spike(
    fault: ActiveFault, rt: ServiceRuntime, m: dict[str, float]
) -> None:
    ramp = _ramp(fault, 2.0)
    # Interpolate towards the saturated value instead of overwriting: an
    # action that only *mitigates* the fault (restart) leaves the component
    # warm, one that cures it (scale out) returns it to baseline.
    m["cpu"] = m["cpu"] + (92.0 + 6.0 * ramp - m["cpu"]) * fault.intensity
    m["latency_p50"] += 260.0 * ramp * fault.intensity
    m["latency_p95"] += 900.0 * ramp * fault.intensity
    m["error_rate"] += 0.12 * ramp * fault.intensity


def _logs_cpu_spike(
    fault: ActiveFault, rt: ServiceRuntime, m: dict[str, float]
) -> list[LogLine]:
    return [
        ("WARN", f"CPU saturation {m['cpu']:.0f}% — request queue depth rising"),
        ("ERROR", f"request deadline exceeded after {int(m['latency_p95'])}ms"),
    ]


def _apply_redis_failure(
    fault: ActiveFault, rt: ServiceRuntime, m: dict[str, float]
) -> None:
    ramp = _ramp(fault, 1.0) * fault.intensity
    m["error_rate"] = 0.97 * ramp
    m["latency_p50"] = 900.0 * ramp
    m["latency_p95"] = 2600.0 * ramp
    m["cpu"] = 8.0


def _logs_redis_failure(
    fault: ActiveFault, rt: ServiceRuntime, m: dict[str, float]
) -> list[LogLine]:
    return [
        ("ERROR", "redis: connection refused — no reachable node in the cluster"),
        ("ERROR", "cache read failed: all 3 sentinel endpoints unreachable"),
    ]


def _apply_bad_deployment(
    fault: ActiveFault, rt: ServiceRuntime, m: dict[str, float]
) -> None:
    ramp = _ramp(fault, 1.5) * fault.intensity
    m["error_rate"] += 0.31 * ramp
    m["latency_p50"] += 30.0 * ramp
    m["latency_p95"] += 90.0 * ramp
    m["cpu"] += 6.0 * ramp


def _logs_bad_deployment(
    fault: ActiveFault, rt: ServiceRuntime, m: dict[str, float]
) -> list[LogLine]:
    detail = str(fault.params.get("error", "unhandled application error"))
    return [
        ("ERROR", f"{detail} — request terminated with HTTP 500"),
        (
            "ERROR",
            f"error rate for {rt.name} version {rt.version} is {_pct(m['error_rate'])} "
            f"(baseline 0.3%)",
        ),
        ("WARN", f"circuit breaker for {rt.name} reporting elevated failures"),
    ]


def _apply_slow_database(
    fault: ActiveFault, rt: ServiceRuntime, m: dict[str, float]
) -> None:
    ramp = _ramp(fault, 3.0) * fault.intensity
    m["latency_p50"] += 900.0 * ramp
    m["latency_p95"] += 2600.0 * ramp
    m["cpu"] += 30.0 * ramp
    m["error_rate"] += 0.08 * ramp
    m["db_connections"] += 40.0 * ramp


def _logs_slow_database(
    fault: ActiveFault, rt: ServiceRuntime, m: dict[str, float]
) -> list[LogLine]:
    return [
        (
            "WARN",
            f"slow query detected: SELECT * FROM orders WHERE status = 'open' "
            f"took {int(m['latency_p95'])}ms",
        ),
        ("ERROR", f"lock wait timeout exceeded; try restarting transaction ({int(m['latency_p50'])}ms)"),
        ("WARN", f"postgres: {int(m['db_connections'])} active connections, queue depth rising"),
    ]


def _apply_api_timeout(
    fault: ActiveFault, rt: ServiceRuntime, m: dict[str, float]
) -> None:
    ramp = _ramp(fault, 2.0)
    m["latency_p50"] = m["latency_p50"] + (
        2400.0 * ramp - m["latency_p50"]
    ) * fault.intensity
    m["latency_p95"] = m["latency_p95"] + (
        8200.0 * ramp - m["latency_p95"]
    ) * fault.intensity
    m["error_rate"] = m["error_rate"] + (
        0.55 * ramp - m["error_rate"]
    ) * fault.intensity


def _logs_api_timeout(
    fault: ActiveFault, rt: ServiceRuntime, m: dict[str, float]
) -> list[LogLine]:
    return [
        ("ERROR", "partner payment API returned 504 Gateway Timeout"),
        ("ERROR", f"upstream call to acme-pay exceeded 30000ms (p95={int(m['latency_p95'])}ms)"),
        ("WARN", "retry budget exhausted for payment authorisation"),
    ]


def _apply_third_party_failure(
    fault: ActiveFault, rt: ServiceRuntime, m: dict[str, float]
) -> None:
    ramp = _ramp(fault, 1.0) * fault.intensity
    m["error_rate"] = 0.92 * ramp
    m["latency_p95"] = 5000.0 * ramp


def _logs_third_party_failure(
    fault: ActiveFault, rt: ServiceRuntime, m: dict[str, float]
) -> list[LogLine]:
    return [
        ("ERROR", "acme-pay returned 503 Service Unavailable"),
        ("ERROR", "payment authorisation rejected: provider outage"),
    ]


def _apply_dependency_failure(
    fault: ActiveFault, rt: ServiceRuntime, m: dict[str, float]
) -> None:
    ramp = _ramp(fault, 1.0) * fault.intensity
    m["error_rate"] = 0.88 * ramp
    m["latency_p95"] += 1800.0 * ramp


def _logs_dependency_failure(
    fault: ActiveFault, rt: ServiceRuntime, m: dict[str, float]
) -> list[LogLine]:
    return [
        ("ERROR", "all upstream replicas unhealthy — failing fast"),
        ("ERROR", f"readiness probe failed for {rt.name}; removed from load balancer"),
    ]


def _apply_high_error_rate(
    fault: ActiveFault, rt: ServiceRuntime, m: dict[str, float]
) -> None:
    ramp = _ramp(fault, 2.0) * fault.intensity
    m["error_rate"] += 0.26 * ramp
    m["latency_p95"] += 120.0 * ramp


def _logs_high_error_rate(
    fault: ActiveFault, rt: ServiceRuntime, m: dict[str, float]
) -> list[LogLine]:
    return [
        ("ERROR", f"HTTP 500 on {int(m['request_rate'])} rpm — error budget exhausted"),
        ("ERROR", "unhandled exception in request handler (see stack trace)"),
    ]


FAULT_MODELS: dict[str, FaultModel] = {
    model.kind: model
    for model in (
        FaultModel(
            kind="db_connection_exhaustion",
            label="Database connection pool exhaustion",
            resistant_to=frozenset({"restart_service", "scale_service"}),
            progressive=True,
            apply=_apply_db_connection_exhaustion,
            progress=_progress_db_connection_exhaustion,
            logs=_logs_db_connection_exhaustion,
        ),
        FaultModel(
            kind="memory_leak",
            label="Memory leak leading to OOM pressure",
            resistant_to=frozenset({"scale_service"}),
            progressive=True,
            apply=_apply_memory_leak,
            progress=_progress_memory_leak,
            logs=_logs_memory_leak,
        ),
        FaultModel(
            kind="cpu_spike",
            label="CPU saturation",
            apply=_apply_cpu_spike,
            logs=_logs_cpu_spike,
        ),
        FaultModel(
            kind="redis_failure",
            label="Cache layer unavailable",
            applies_to=("cache",),
            resistant_to=frozenset({"restart_service", "scale_service"}),
            apply=_apply_redis_failure,
            logs=_logs_redis_failure,
        ),
        FaultModel(
            kind="bad_deployment",
            label="Defective deployment",
            resistant_to=frozenset({"restart_service", "scale_service", "flush_cache"}),
            apply=_apply_bad_deployment,
            logs=_logs_bad_deployment,
        ),
        FaultModel(
            kind="api_timeout",
            label="Upstream API timeout",
            resistant_to=frozenset({"restart_service", "scale_service"}),
            apply=_apply_api_timeout,
            logs=_logs_api_timeout,
        ),
        FaultModel(
            kind="third_party_api_failure",
            label="Third-party provider outage",
            applies_to=("external",),
            resistant_to=frozenset({"restart_service", "scale_service"}),
            apply=_apply_third_party_failure,
            logs=_logs_third_party_failure,
        ),
        FaultModel(
            kind="slow_database",
            label="Slow or blocked database queries",
            applies_to=("datastore",),
            resistant_to=frozenset({"restart_service", "scale_service"}),
            apply=_apply_slow_database,
            logs=_logs_slow_database,
        ),
        FaultModel(
            kind="dependency_failure",
            label="Dependency unavailable",
            apply=_apply_dependency_failure,
            logs=_logs_dependency_failure,
        ),
        FaultModel(
            kind="high_error_rate",
            label="Elevated error rate",
            apply=_apply_high_error_rate,
            logs=_logs_high_error_rate,
        ),
    )
}


# ---------------------------------------------------------------------------
# Recovery actions
# ---------------------------------------------------------------------------


@dataclass
class ActionModel:
    name: str
    label: str
    risk: str
    removes: frozenset[str] = frozenset()
    """Fault kinds this action eliminates outright."""
    resets: tuple[str, ...] = ()
    """Runtime state fields it zeroes (pool, memory growth, ...)."""
    mitigates: dict[str, float] = field(default_factory=dict)
    """Fault kind → intensity multiplier (a partial, non-curative fix)."""
    applies_to: tuple[str, ...] = ("service", "cache", "datastore", "external")


ACTION_MODELS: dict[str, ActionModel] = {
    model.name: model
    for model in (
        ActionModel(
            name="restart_service",
            label="Restart the service pods",
            risk="HIGH",
            # A restart clears in-process state and re-registers a component
            # that fell out of the load balancer — but it cannot un-ship code.
            removes=frozenset({"dependency_failure"}),
            resets=("pool_open", "memory_growth_mb"),
            mitigates={"cpu_spike": 0.4, "high_error_rate": 0.5},
        ),
        ActionModel(
            name="rollback_deployment",
            label="Roll back to the previous release",
            risk="CRITICAL",
            removes=frozenset({"bad_deployment", "memory_leak", "db_connection_exhaustion", "high_error_rate"}),
        ),
        ActionModel(
            name="scale_service",
            label="Scale the service out",
            risk="MEDIUM",
            # Capacity problems are genuinely solved by capacity; everything
            # else only gets diluted.
            removes=frozenset({"cpu_spike"}),
            mitigates={"high_error_rate": 0.7},
        ),
        ActionModel(
            name="increase_pool_size",
            label="Raise the database connection pool limit",
            risk="MEDIUM",
            removes=frozenset({"db_connection_exhaustion"}),
            mitigates={"slow_database": 0.7},
        ),
        ActionModel(
            name="restart_redis",
            label="Fail over / restart the Redis cluster",
            risk="HIGH",
            applies_to=("cache",),
            removes=frozenset({"redis_failure", "dependency_failure"}),
        ),
        ActionModel(
            name="flush_cache",
            label="Flush the cache",
            risk="MEDIUM",
            applies_to=("cache",),
            mitigates={"redis_failure": 0.6},
        ),
        ActionModel(
            name="restart_postgres",
            label="Restart the database and clear stuck sessions",
            risk="CRITICAL",
            applies_to=("datastore",),
            removes=frozenset({"slow_database", "dependency_failure"}),
            resets=("pool_open",),
        ),
        ActionModel(
            name="enable_circuit_breaker",
            label="Open the circuit breaker for the failing upstream",
            risk="MEDIUM",
            removes=frozenset({"api_timeout", "third_party_api_failure"}),
            mitigates={"dependency_failure": 0.5},
        ),
        ActionModel(
            name="switch_payment_provider",
            label="Fail over to the backup payment provider",
            risk="CRITICAL",
            applies_to=("external",),
            removes=frozenset({"api_timeout", "third_party_api_failure"}),
        ),
        ActionModel(
            name="clear_deadlock",
            label="Kill blocking database transactions",
            risk="HIGH",
            applies_to=("datastore",),
            removes=frozenset({"slow_database"}),
            resets=("pool_open",),
        ),
        ActionModel(
            name="notify_oncall",
            label="Page the on-call engineer",
            risk="LOW",
        ),
    )
}


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def apply_faults(rt: ServiceRuntime, metrics: dict[str, float]) -> dict[str, float]:
    """Apply every fault attached to ``rt`` in order."""
    for fault in rt.faults:
        model = FAULT_MODELS.get(fault.kind)
        if model is not None and model.apply is not None:
            model.apply(fault, rt, metrics)
    return metrics


def progress_faults(rt: ServiceRuntime) -> None:
    """Advance time-dependent faults (leaks grow, pools drain)."""
    for fault in rt.faults:
        model = FAULT_MODELS.get(fault.kind)
        if model is not None and model.progress is not None:
            model.progress(fault, rt)


def fault_logs(
    rt: ServiceRuntime, metrics: dict[str, float]
) -> list[LogLine]:
    """Log lines implied by the faults currently on this component."""
    out: list[LogLine] = []
    for fault in rt.faults:
        model = FAULT_MODELS.get(fault.kind)
        if model is not None and model.logs is not None:
            out.extend(model.logs(fault, rt, metrics))
    return out


def healthy_logs(rt: ServiceRuntime, metrics: dict[str, float]) -> list[LogLine]:
    """What a component says when nothing is wrong with it."""
    spec = rt.spec
    if spec.kind == "datastore":
        return [
            ("INFO", f"checkpoint complete; {int(metrics['db_connections'])} active connections"),
            ("DEBUG", f"query p95 {int(metrics['latency_p95'])}ms"),
        ]
    if spec.kind == "cache":
        return [("DEBUG", f"hit ratio 0.94, {int(metrics['request_rate'])} ops/s")]
    if spec.kind == "external":
        return [("INFO", "authorisation settled in 118ms")]
    return [
        ("INFO", f"handled {int(metrics['request_rate'])} rpm, p95 {int(metrics['latency_p95'])}ms"),
        ("DEBUG", "cache hit ratio 0.91"),
    ]


def propagation_logs(rt: ServiceRuntime, metrics: dict[str, float]) -> list[LogLine]:
    """Log lines implied by *dependencies* being unhealthy.

    Without these, an operator could see ``gateway`` failing with every local
    metric clean and have no way to know the fault is downstream.
    """
    from opspilot_simulator.world import SPEC_BY_NAME, health_of

    out: list[LogLine] = []
    for dep_name, kind, _crit in rt.spec.depends_on:
        dep_spec = SPEC_BY_NAME.get(dep_name)
        if dep_spec is None:
            continue
        # Read the dependency's live snapshot from the engine's last compute.
        dep_metrics = rt.dep_metrics.get(dep_name)
        if not dep_metrics:
            continue
        if health_of(dep_metrics, dep_spec) == "healthy":
            continue
        if dep_name == "redis":
            out.append(("ERROR", "redis: connection refused — falling back to origin"))
        elif dep_name == "postgres":
            out.append(
                (
                    "ERROR",
                    f"database call timed out after {int(dep_metrics['latency_p95'])}ms",
                )
            )
        elif dep_name == "external-payment-api":
            out.append(("ERROR", "upstream payment provider returned 5xx"))
        else:
            out.append(
                (
                    "ERROR",
                    f"upstream {dep_name} returned 503 — request failed after "
                    f"{int(metrics['latency_p95'])}ms",
                )
            )
    return out


def now_iso() -> str:
    return _now().isoformat()


def minutes_ago(minutes: float) -> datetime:
    return _now() - timedelta(minutes=minutes)


__all__ = [
    "ACTION_MODELS",
    "ActionModel",
    "FAULT_MODELS",
    "FaultModel",
    "LogLine",
    "apply_faults",
    "fault_logs",
    "healthy_logs",
    "minutes_ago",
    "now_iso",
    "progress_faults",
    "propagation_logs",
]
