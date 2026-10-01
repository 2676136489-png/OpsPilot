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

**Language.** Log bodies and the ``label`` fields are Chinese, because the
console renders log lines verbatim as evidence titles and lists action labels
in the recovery plan. The tokens the code switches on — fault ``kind``, action
``name``, ``risk``, metric keys, and real exception/class names such as
``QueuePool`` or ``OutOfMemoryError`` — stay as they are: they are identifiers
that appear in genuine stacks and metric series, not copy.

The Chinese phrasings here are deliberately specific ("连接池饱和度", "堆内存",
"锁等待") rather than generic words like "缓存" or "内存". The Agent's signal
extractor matches substrings against these lines, and a log line that merely
mentions a healthy cache must not light up the cache-failure signal.
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
                f"sqlalchemy.exc.TimeoutError: QueuePool 连接数达上限 "
                f"size={rt.spec.pool_max} overflow={int(rt.spec.pool_max * 0.1)}，"
                f"获取连接超时，timeout 30.00",
            )
        )
        out.append(
            (
                "ERROR",
                f"获取数据库连接失败：等待 30000ms 后仍未拿到 "
                f"（active={int(rt.pool_open)}/{rt.spec.pool_max}）",
            )
        )
    if util >= 0.7:
        out.append(
            (
                "WARN",
                f"数据库连接池饱和度已达 {_pct(util)}，请求开始排队",
            )
        )
    out.append(
        (
            "ERROR",
            f"结算请求失败：上游依赖超时 "
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
                f"OutOfMemoryError: 堆内存不足 — GC 停顿 2.1s 仍无法回收，已用 "
                f"{int(m['memory_mb'])}MB / "
                f"{int(rt.spec.memory_limit_mb)}MB，容器即将被 OOM-killed",
            )
        )
    if ratio >= 0.85:
        out.append(
            (
                "WARN",
                f"GC 停顿 480ms — 堆内存占用已达 {_pct(ratio)}，疑似存在无界持有",
            )
        )
    out.append(
        ("WARN", f"堆内存占用 {int(m['memory_mb'])}MB（上限 {int(rt.spec.memory_limit_mb)}MB）")
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
        ("WARN", f"CPU 饱和 {m['cpu']:.0f}% — 请求队列深度上升"),
        ("ERROR", f"请求超过截止时间：等待 {int(m['latency_p95'])}ms 仍未完成"),
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
        ("ERROR", "redis: 连接被拒绝 — 集群内没有可达节点"),
        ("ERROR", "缓存读取失败：3 个 sentinel 端点全部不可达"),
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
    detail = str(fault.params.get("error", "未捕获的应用异常"))
    return [
        ("ERROR", f"{detail} — 请求以 HTTP 500 结束"),
        (
            "ERROR",
            f"{rt.name} 版本 {rt.version} 错误率为 {_pct(m['error_rate'])} "
            f"（基线 0.3%）",
        ),
        ("WARN", f"{rt.name} 的熔断器报告失败率升高"),
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
            f"检测到慢查询：SELECT * FROM orders WHERE status = 'open' "
            f"耗时 {int(m['latency_p95'])}ms",
        ),
        ("ERROR", f"锁等待超时，请重试事务（已等待 {int(m['latency_p50'])}ms）"),
        ("WARN", f"postgres: 活跃连接 {int(m['db_connections'])}，队列深度上升"),
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
        ("ERROR", "合作方支付 API 返回 504 Gateway Timeout"),
        ("ERROR", f"上游调用 acme-pay 已超过 30000ms（p95={int(m['latency_p95'])}ms）"),
        ("WARN", "支付授权的重试预算已耗尽"),
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
        ("ERROR", "acme-pay 返回 503 Service Unavailable"),
        ("ERROR", "支付授权被拒绝：渠道整体故障"),
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
        ("ERROR", "上游副本全部不健康 — 正在快速失败"),
        ("ERROR", f"{rt.name} 就绪探针失败，已从负载均衡摘除"),
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
        ("ERROR", f"{int(m['request_rate'])} rpm 的请求出现 HTTP 500 — 错误预算耗尽"),
        ("ERROR", "请求处理中出现未捕获异常（见堆栈）"),
    ]


FAULT_MODELS: dict[str, FaultModel] = {
    model.kind: model
    for model in (
        FaultModel(
            kind="db_connection_exhaustion",
            label="数据库连接池耗尽",
            resistant_to=frozenset({"restart_service", "scale_service"}),
            progressive=True,
            apply=_apply_db_connection_exhaustion,
            progress=_progress_db_connection_exhaustion,
            logs=_logs_db_connection_exhaustion,
        ),
        FaultModel(
            kind="memory_leak",
            label="内存泄漏导致 OOM 压力",
            resistant_to=frozenset({"scale_service"}),
            progressive=True,
            apply=_apply_memory_leak,
            progress=_progress_memory_leak,
            logs=_logs_memory_leak,
        ),
        FaultModel(
            kind="cpu_spike",
            label="CPU 打满",
            apply=_apply_cpu_spike,
            logs=_logs_cpu_spike,
        ),
        FaultModel(
            kind="redis_failure",
            label="缓存层不可用",
            applies_to=("cache",),
            resistant_to=frozenset({"restart_service", "scale_service"}),
            apply=_apply_redis_failure,
            logs=_logs_redis_failure,
        ),
        FaultModel(
            kind="bad_deployment",
            label="有缺陷的版本发布",
            resistant_to=frozenset({"restart_service", "scale_service", "flush_cache"}),
            apply=_apply_bad_deployment,
            logs=_logs_bad_deployment,
        ),
        FaultModel(
            kind="api_timeout",
            label="上游 API 超时",
            resistant_to=frozenset({"restart_service", "scale_service"}),
            apply=_apply_api_timeout,
            logs=_logs_api_timeout,
        ),
        FaultModel(
            kind="third_party_api_failure",
            label="第三方渠道故障",
            applies_to=("external",),
            resistant_to=frozenset({"restart_service", "scale_service"}),
            apply=_apply_third_party_failure,
            logs=_logs_third_party_failure,
        ),
        FaultModel(
            kind="slow_database",
            label="数据库查询缓慢或阻塞",
            applies_to=("datastore",),
            resistant_to=frozenset({"restart_service", "scale_service"}),
            apply=_apply_slow_database,
            logs=_logs_slow_database,
        ),
        FaultModel(
            kind="dependency_failure",
            label="依赖不可用",
            apply=_apply_dependency_failure,
            logs=_logs_dependency_failure,
        ),
        FaultModel(
            kind="high_error_rate",
            label="错误率异常升高",
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
            label="重启服务实例",
            risk="HIGH",
            # A restart clears in-process state and re-registers a component
            # that fell out of the load balancer — but it cannot un-ship code.
            removes=frozenset({"dependency_failure"}),
            resets=("pool_open", "memory_growth_mb"),
            mitigates={"cpu_spike": 0.4, "high_error_rate": 0.5},
        ),
        ActionModel(
            name="rollback_deployment",
            label="回滚到上一个版本",
            risk="CRITICAL",
            removes=frozenset({"bad_deployment", "memory_leak", "db_connection_exhaustion", "high_error_rate"}),
        ),
        ActionModel(
            name="scale_service",
            label="扩容服务实例",
            risk="MEDIUM",
            # Capacity problems are genuinely solved by capacity; everything
            # else only gets diluted.
            removes=frozenset({"cpu_spike"}),
            mitigates={"high_error_rate": 0.7},
        ),
        ActionModel(
            name="increase_pool_size",
            label="调高数据库连接池上限",
            risk="MEDIUM",
            removes=frozenset({"db_connection_exhaustion"}),
            mitigates={"slow_database": 0.7},
        ),
        ActionModel(
            name="restart_redis",
            label="故障转移 / 重启 Redis 集群",
            risk="HIGH",
            applies_to=("cache",),
            removes=frozenset({"redis_failure", "dependency_failure"}),
        ),
        ActionModel(
            name="flush_cache",
            label="清空缓存",
            risk="MEDIUM",
            applies_to=("cache",),
            mitigates={"redis_failure": 0.6},
        ),
        ActionModel(
            name="restart_postgres",
            label="重启数据库，清掉卡住的会话",
            risk="CRITICAL",
            applies_to=("datastore",),
            removes=frozenset({"slow_database", "dependency_failure"}),
            resets=("pool_open",),
        ),
        ActionModel(
            name="enable_circuit_breaker",
            label="为失败的上游打开熔断器",
            risk="MEDIUM",
            removes=frozenset({"api_timeout", "third_party_api_failure"}),
            mitigates={"dependency_failure": 0.5},
        ),
        ActionModel(
            name="switch_payment_provider",
            label="切换到备用支付渠道",
            risk="CRITICAL",
            applies_to=("external",),
            removes=frozenset({"api_timeout", "third_party_api_failure"}),
        ),
        ActionModel(
            name="clear_deadlock",
            label="杀掉持有行锁的阻塞事务",
            risk="HIGH",
            applies_to=("datastore",),
            removes=frozenset({"slow_database"}),
            resets=("pool_open",),
        ),
        ActionModel(
            name="notify_oncall",
            label="呼叫值班工程师",
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
            ("INFO", f"检查点完成；活跃连接 {int(metrics['db_connections'])}"),
            ("DEBUG", f"查询 p95 {int(metrics['latency_p95'])}ms"),
        ]
    if spec.kind == "cache":
        return [("DEBUG", f"命中率 0.94，{int(metrics['request_rate'])} ops/s")]
    if spec.kind == "external":
        return [("INFO", "授权在 118ms 内完成")]
    return [
        ("INFO", f"已处理 {int(metrics['request_rate'])} rpm，p95 {int(metrics['latency_p95'])}ms"),
        ("DEBUG", "缓存命中率 0.91"),
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
            out.append(("ERROR", "redis: 缓存读取失败 — 连接被拒绝，回退到源站"))
        elif dep_name == "postgres":
            out.append(
                (
                    "ERROR",
                    f"数据库调用超时：等待 {int(dep_metrics['latency_p95'])}ms 仍未返回",
                )
            )
        elif dep_name == "external-payment-api":
            # Two different faults live behind this one dependency — a provider
            # returning 503 and a provider that never answers — and a single
            # "upstream returned 5xx" line describes neither. It is also the only
            # line the investigation sees: `query_logs` reads the *alert*
            # service, so the dependency's own logs are never collected, and
            # evidence that cannot name what broke cannot be attributed to it.
            # So the line names the vendor and the actual symptom, branching on
            # the dependency's latency for the timeout case — a 504 is a wait,
            # not a status the vendor chose to return.
            if dep_metrics.get("latency_p95", 0.0) > 5000:
                out.append(
                    (
                        "ERROR",
                        "外部支付渠道 acme-pay 调用超时（504 Gateway Timeout）："
                        f"上游调用已等待 {int(dep_metrics['latency_p95'])}ms",
                    )
                )
            else:
                out.append(("ERROR", "外部支付渠道 acme-pay 返回 503 — 上游链路失败"))
        else:
            out.append(
                (
                    "ERROR",
                    f"上游 {dep_name} 返回 503 — 请求在 "
                    f"{int(metrics['latency_p95'])}ms 后失败",
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
