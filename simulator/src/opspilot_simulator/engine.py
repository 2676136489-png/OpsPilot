"""The simulator engine: state, injection, time, recovery actions, queries.

Everything the Agent observes comes from here, and every recovery action
mutates the state this object owns. There is no canned response anywhere — if
the environment is healthy, the metrics say healthy.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from opspilot_simulator.faults import (
    ACTION_MODELS,
    FAULT_MODELS,
    LogLine,
    fault_logs,
    healthy_logs,
    progress_faults,
    propagation_logs,
)
from opspilot_simulator.scenarios import (
    Criterion,
    DeployEvent,
    get_scenario,
)
from opspilot_simulator.world import (
    METRIC_FIELDS,
    METRIC_UNITS,
    TOPOLOGY,
    ActiveFault,
    ServiceRuntime,
    _jitter,
    _now,
    dependency_graph,
    derive_metrics,
    service_snapshot,
    set_health_baselines,
)

#: How many minutes of fault ramp are already banked at injection time, per
#: fault kind. A leak needs to have been running for a while to be visible;
#: a bad deploy shows up almost immediately.
INITIAL_ELAPSED_MINUTES: dict[str, float] = {
    "memory_leak": 32.0,
    "db_connection_exhaustion": 7.0,
    "slow_database": 9.0,
    "cpu_spike": 4.0,
    "redis_failure": 3.0,
    "api_timeout": 6.0,
    "third_party_api_failure": 3.0,
    "dependency_failure": 3.0,
    "bad_deployment": 7.0,
    "high_error_rate": 8.0,
}

#: Metric name as the backend asks for it → internal field name.
METRIC_ALIASES: dict[str, str] = {
    "cpu": "cpu",
    "cpu_percent": "cpu",
    "memory": "memory_mb",
    "memory_mb": "memory_mb",
    "latency_p95": "latency_p95",
    "latency_p95_ms": "latency_p95",
    "latency_p50": "latency_p50",
    "latency_p50_ms": "latency_p50",
    "error_rate": "error_rate",
    "request_rate": "request_rate",
    "db_connections": "db_connections",
}

_HISTORY_MINUTES = 90
_HISTORY_STEP = 2
_LOG_RETENTION_MINUTES = 180


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class Simulator:
    """A small, stateful, causal model of a production environment."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.runtimes: dict[str, ServiceRuntime] = {}
        self.active_scenario: str | None = None
        self.scenario_injected_at: datetime | None = None
        self.chaos: str = "off"  # off | slow | unavailable
        self.reset()

    # ------------------------------------------------------------------
    # Construction / reset
    # ------------------------------------------------------------------

    def reset(self) -> dict[str, Any]:
        """Return every component to a clean, healthy baseline."""
        with self._lock:
            previous = self.active_scenario
            self.runtimes = {
                spec.name: ServiceRuntime(spec=spec) for spec in TOPOLOGY
            }
            for rt in self.runtimes.values():
                rt.deployments = self._seed_deployments(rt)
                rt.commits = self._seed_commits(rt)
            self.active_scenario = None
            self.scenario_injected_at = None
            # Capture the healthy reference *before* any fault exists: health
            # is judged against what this stack measures when it is fine.
            set_health_baselines(self._compute_locked())
            self._replay_history(minutes=_HISTORY_MINUTES, step=_HISTORY_STEP)
            return {"reset_from": previous, "status": "normal"}

    @staticmethod
    def _seed_deployments(rt: ServiceRuntime) -> list[dict[str, Any]]:
        """Oldest first — ``get_deployments`` reverses this list."""
        spec = rt.spec
        return [
            {
                "service": spec.name,
                "version": _previous_version(spec.version),
                "deployed_at": _iso(_now() - timedelta(hours=240)),
                "status": "success",
                "author": "ci-bot",
                "notes": "previous release",
            },
            {
                "service": spec.name,
                "version": spec.version,
                "deployed_at": _iso(_now() - timedelta(hours=72)),
                "status": "success",
                "author": "ci-bot",
                "notes": "scheduled release",
            },
        ]

    @staticmethod
    def _seed_commits(rt: ServiceRuntime) -> list[dict[str, Any]]:
        spec = rt.spec
        now = _now()
        messages = (
            "chore: bump dependencies",
            "test: cover the retry path",
            "refactor: extract the client factory",
            "docs: update the runbook link",
        )
        return [
            {
                "sha": f"{abs(hash((spec.name, i))) % (16 ** 8):08x}",
                "repository": spec.name,
                "author": ["alice", "bob", "carol", "dan"][i % 4],
                "message": messages[i % len(messages)],
                "committed_at": _iso(now - timedelta(hours=6 * (i + 1))),
            }
            for i in range(6)
        ]

    # ------------------------------------------------------------------
    # Injection
    # ------------------------------------------------------------------

    def inject(self, name: str) -> dict[str, Any]:
        """Inject a scenario: attach faults, plant the deployment history."""
        scenario = get_scenario(name)
        if scenario is None:
            raise KeyError(f"Unknown scenario: {name}")

        with self._lock:
            self.reset()

            for deploy in scenario.deploys:
                self._plant_deployment(deploy)

            for fault_spec in scenario.faults:
                rt = self.runtimes[fault_spec.target]
                elapsed = float(
                    fault_spec.params.get(
                        "elapsed_minutes",
                        INITIAL_ELAPSED_MINUTES.get(fault_spec.kind, 6.0),
                    )
                )
                rt.faults.append(
                    ActiveFault(
                        kind=fault_spec.kind,
                        target=fault_spec.target,
                        started_at=_now() - timedelta(minutes=elapsed),
                        params=dict(fault_spec.params),
                        intensity=float(fault_spec.intensity),
                    )
                )

            self.active_scenario = name
            self.scenario_injected_at = _now()
            self._replay_history(minutes=_HISTORY_MINUTES, step=_HISTORY_STEP)
            return {
                "scenario": name,
                "title": scenario.title,
                "alert_service": scenario.alert_service,
                "symptoms": list(scenario.symptoms),
                "affected_services": sorted({f.target for f in scenario.faults}),
                "injected_at": _iso(self.scenario_injected_at),
            }

    def _plant_deployment(self, deploy: DeployEvent) -> None:
        rt = self.runtimes[deploy.service]
        when = _now() - timedelta(minutes=deploy.minutes_ago)
        rt.version = deploy.version
        rt.deployments.append(
            {
                "service": deploy.service,
                "version": deploy.version,
                "deployed_at": _iso(when),
                "status": deploy.status,
                "author": deploy.author,
                "notes": deploy.notes,
            }
        )
        rt.commits.insert(
            0,
            {
                "sha": f"{abs(hash((deploy.service, deploy.version))) % (16 ** 8):08x}",
                "repository": deploy.service,
                "author": deploy.author,
                "message": deploy.commit_message or deploy.notes,
                "committed_at": _iso(when - timedelta(minutes=35)),
            },
        )

    # ------------------------------------------------------------------
    # Time
    # ------------------------------------------------------------------

    def advance(self, minutes: float) -> dict[str, Any]:
        """Move the incident forward. Leaks grow and pools drain."""
        with self._lock:
            if not self.active_scenario:
                return {"advanced_minutes": minutes, "scenario": None}
            for rt in self.runtimes.values():
                for fault in rt.faults:
                    fault.started_at -= timedelta(minutes=minutes)
            self._replay_history(minutes=int(minutes) + 4, step=1)
            return {
                "advanced_minutes": minutes,
                "scenario": self.active_scenario,
                "services": {
                    name: snapshot["health"]
                    for name, snapshot in self._snapshots().items()
                },
            }

    # ------------------------------------------------------------------
    # Computation
    # ------------------------------------------------------------------

    def _replay_history(self, *, minutes: int, step: int) -> None:
        """Recompute the recent past so metric charts have a real shape.

        A timeline that jumps from flat to broken in one sample is not
        diagnosable; the ramp is the evidence the Agent correlates against
        deployment timestamps.
        """
        with self._lock:
            offsets = list(range(-minutes, 1, step))
            for offset in offsets:
                at = _now() + timedelta(minutes=offset)
                for rt in self.runtimes.values():
                    for fault in rt.faults:
                        fault.clock_offset = offset
                metrics = self._compute_locked()
                for name, m in metrics.items():
                    rt = self.runtimes[name]
                    noisy = self._with_jitter(name, m, len(rt.metrics_history))
                    rt.metrics_history.append((at, noisy))
                if offset % max(step, 2) == 0:
                    self._emit_logs(metrics, at)
            for rt in self.runtimes.values():
                for fault in rt.faults:
                    fault.clock_offset = 0.0
            self._trim()

    def _compute_locked(self) -> dict[str, dict[str, float]]:
        for rt in self.runtimes.values():
            progress_faults(rt)
        # pool_open must start from the baseline, not from zero, or a healthy
        # service reports 0 connections and pool utilisation looks perfect.
        for rt in self.runtimes.values():
            if rt.spec.pool_max and rt.pool_open < rt.spec.db_connections:
                rt.pool_open = float(rt.spec.db_connections)
        metrics = derive_metrics(self.runtimes)
        for name, rt in self.runtimes.items():
            rt.dep_metrics = {
                dep: metrics[dep]
                for dep, _k, _c in rt.spec.depends_on
                if dep in metrics
            }
        return metrics

    def _snapshots(self) -> dict[str, dict[str, Any]]:
        metrics = self._compute_locked()
        return {
            name: service_snapshot(self.runtimes[name], m) for name, m in metrics.items()
        }

    @staticmethod
    def _with_jitter(
        name: str, metrics: dict[str, float], index: int
    ) -> dict[str, float]:
        out = dict(metrics)
        out["cpu"] = round(max(0.0, metrics["cpu"] + _jitter(name, index, 2.0)), 2)
        out["latency_p95"] = round(
            max(1.0, metrics["latency_p95"] * (1.0 + _jitter(name, index, 0.05))), 2
        )
        out["latency_p50"] = round(
            max(0.5, metrics["latency_p50"] * (1.0 + _jitter(name, index, 0.05))), 2
        )
        out["error_rate"] = round(
            max(0.0, metrics["error_rate"] * (1.0 + _jitter(name, index, 0.10))), 6
        )
        out["request_rate"] = round(
            max(0.0, metrics["request_rate"] * (1.0 + _jitter(name, index, 0.03))), 2
        )
        out["memory_mb"] = round(metrics["memory_mb"] + _jitter(name, index, 8.0), 1)
        out["db_connections"] = round(max(0.0, metrics["db_connections"]), 1)
        return out

    def _emit_logs(self, metrics: dict[str, dict[str, float]], at: datetime) -> None:
        for name, rt in self.runtimes.items():
            m = metrics[name]
            lines: list[LogLine] = []
            if rt.faults:
                lines.extend(fault_logs(rt, m))
            if not lines:
                lines.extend(propagation_logs(rt, m))
            if not lines:
                lines.extend(healthy_logs(rt, m))
            for level, message in lines[:3]:
                rt.log_buffer.append(
                    {
                        "timestamp": _iso(at),
                        "level": level,
                        "service": name,
                        "message": message,
                    }
                )

    def _trim(self) -> None:
        cutoff_ts = _now() - timedelta(minutes=_LOG_RETENTION_MINUTES)
        cutoff_history = _now() - timedelta(minutes=_HISTORY_MINUTES + 10)
        for rt in self.runtimes.values():
            rt.log_buffer = [
                entry
                for entry in rt.log_buffer
                if datetime.fromisoformat(
                    entry["timestamp"].replace("Z", "+00:00")
                )
                >= cutoff_ts
            ]
            rt.metrics_history = [
                (ts, m) for ts, m in rt.metrics_history if ts >= cutoff_history
            ]

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_services(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._snapshots().values())

    def get_service(self, name: str) -> dict[str, Any]:
        with self._lock:
            if name not in self.runtimes:
                raise KeyError(f"Unknown service: {name}")
            return self._snapshots()[name]

    def get_metrics(
        self, service: str, metric: str, minutes_back: int = 60
    ) -> list[dict[str, Any]]:
        field = METRIC_ALIASES.get(metric)
        if field is None:
            raise ValueError(
                f"Unknown metric '{metric}'. Supported: {sorted(METRIC_ALIASES)}"
            )
        with self._lock:
            rt = self.runtimes.get(service)
            if rt is None:
                raise KeyError(f"Unknown service: {service}")
            cutoff = _now() - timedelta(minutes=minutes_back)
            return [
                {"timestamp": _iso(ts), "value": round(m[field], 4)}
                for ts, m in rt.metrics_history
                if ts >= cutoff
            ]

    def get_logs(
        self,
        service: str,
        minutes_back: int = 30,
        level: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rt = self.runtimes.get(service)
            if rt is None:
                raise KeyError(f"Unknown service: {service}")
            cutoff = _now() - timedelta(minutes=minutes_back)
            entries = [
                e
                for e in rt.log_buffer
                if datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")) >= cutoff
            ]
            if level:
                wanted = level.upper()
                entries = [e for e in entries if e["level"] == wanted]
            return entries[-limit:]

    def get_deployments(self, service: str, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rt = self.runtimes.get(service)
            if rt is None:
                raise KeyError(f"Unknown service: {service}")
            return list(reversed(rt.deployments))[:limit]

    def get_commits(self, repository: str, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rt = self.runtimes.get(repository)
            if rt is None:
                # Unknown repository: still answer, so an Agent probing the
                # wrong name learns "no history" rather than "server broken".
                return []
            return list(rt.commits)[:limit]

    def dependencies(self) -> dict[str, Any]:
        return dependency_graph()

    # ------------------------------------------------------------------
    # Recovery actions
    # ------------------------------------------------------------------

    def act(self, action: str, service: str, **params: Any) -> dict[str, Any]:
        """Apply a recovery action. Reports honestly whether it did anything."""
        model = ACTION_MODELS.get(action)
        if model is None:
            raise KeyError(f"Unknown action: {action}")
        with self._lock:
            rt = self.runtimes.get(service)
            if rt is None:
                raise KeyError(f"Unknown service: {service}")

            before = self._snapshots()[service]

            if action == "rollback_deployment":
                rollback = self._rollback(rt)
                if rollback is None:
                    return {
                        "service": service,
                        "action": action,
                        "ok": True,
                        "effective": False,
                        "reason": "no deployment history to roll back",
                        "before": before,
                        "after": before,
                    }
                rt.version = rollback["version"]

            # Scaling is the one action whose effect depends on *direction*.
            # Capacity is bought by scaling out; scaling back down buys nothing,
            # so it must not clear a saturation fault. Without this, rolling
            # back a scale-out "fixed" the incident it had just failed to fix —
            # the Agent's own compensation silently became the cure, and the
            # verification that followed was green for the wrong reason.
            scaling_out = False
            if action == "scale_service":
                replicas = int(params.get("replicas", 4) or 4)
                previous_replicas = rt.replicas
                rt.replicas = max(1, replicas)
                scaling_out = rt.replicas > previous_replicas

            if action == "restart_service":
                rt.restart_count += 1

            if action == "enable_circuit_breaker":
                rt.circuit_breaker_open = True

            removed: list[str] = []
            resisted: list[str] = []
            mitigated: list[str] = []

            # Faults that this action cannot fix stay attached even if the
            # action also clears some runtime state — that is the difference
            # between "restart bought us five minutes" and "restart fixed it".
            for fault in list(rt.faults):
                fault_model = FAULT_MODELS.get(fault.kind)
                immune = fault_model is not None and action in fault_model.resistant_to
                if immune:
                    resisted.append(fault.kind)
                    continue
                if action == "scale_service" and not scaling_out:
                    resisted.append(fault.kind)
                    continue
                if fault.kind in model.removes:
                    removed.append(fault.kind)
                elif fault.kind in model.mitigates:
                    fault.intensity = round(
                        max(0.05, fault.intensity * model.mitigates[fault.kind]), 3
                    )
                    mitigated.append(fault.kind)

            rt.remove_faults(*removed)

            for field_name in model.resets:
                if resisted:
                    # The fault that owns this state survived — clearing it
                    # would be a lie that makes verification pass.
                    continue
                setattr(rt, field_name, 0.0)

            # A restart resets the clock on faults that accumulate inside the
            # process. It does not remove them: give the leak half an hour and
            # it is back where it was, which is exactly why "just restart it"
            # is a mitigation and not a fix.
            if action == "restart_service":
                for fault in rt.faults:
                    fault_model = FAULT_MODELS.get(fault.kind)
                    if fault_model is not None and fault_model.progressive:
                        fault.started_at = _now()

            # Circuit breaking / failing over is a decision made by the
            # *caller*, so an Agent that targets payment-service instead of the
            # provider itself still gets a correct outcome: the caller stops
            # waiting on the broken upstream.
            if action in {"enable_circuit_breaker", "switch_payment_provider"}:
                for dep_name, _kind, _crit in rt.spec.depends_on:
                    dep_rt = self.runtimes.get(dep_name)
                    if dep_rt is None:
                        continue
                    for kind in ("api_timeout", "third_party_api_failure"):
                        if dep_rt.remove_faults(kind):
                            removed.append(f"{dep_name}:{kind}")
                            rt.circuit_breaker_open = True

            self._replay_history(minutes=6, step=1)
            after = self._snapshots()[service]
            return {
                "service": service,
                "action": action,
                "risk": model.risk,
                "ok": True,
                "effective": (
                    bool(removed)
                    or bool(mitigated)
                    # Scaling out is a real change even with nothing to clear;
                    # scaling back in is a real change too, but it changes
                    # capacity in the wrong direction and fixes nothing.
                    or scaling_out
                ),
                "removed_faults": removed,
                "mitigated_faults": mitigated,
                "resisted_faults": resisted,
                "remaining_faults": [f.kind for f in rt.faults],
                "before": before,
                "after": after,
            }

    def _rollback(self, rt: ServiceRuntime) -> dict[str, Any] | None:
        if len(rt.deployments) < 1:
            return None
        current = rt.deployments[-1]
        previous_version = (
            rt.deployments[-2]["version"]
            if len(rt.deployments) > 1
            else _previous_version(current["version"])
        )
        rt.deployments.append(
            {
                "service": rt.name,
                "version": previous_version,
                "deployed_at": _iso(_now()),
                "status": "rolled_back",
                "author": "opspilot-agent",
                "notes": f"rolled back from {current['version']}",
                "rolled_back_from": current["version"],
            }
        )
        return {"version": previous_version, "from": current["version"]}

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def evaluate(self, criteria: Iterable[Criterion]) -> dict[str, Any]:
        """Check criteria against the *current* environment state."""
        with self._lock:
            snapshots = self._snapshots()
            checks: list[dict[str, Any]] = []
            for criterion in criteria:
                snapshot = snapshots.get(criterion.service)
                if snapshot is None:
                    checks.append(
                        {
                            "name": f"{criterion.service}.{criterion.metric}",
                            "passed": False,
                            "actual": None,
                            "threshold": criterion.threshold,
                            "error": "unknown service",
                        }
                    )
                    continue
                actual = snapshot.get(criterion.metric)
                passed = _compare(actual, criterion.op, criterion.threshold)
                checks.append(
                    {
                        "name": f"{criterion.service}.{criterion.metric}",
                        "passed": passed,
                        "actual": actual,
                        "threshold": criterion.threshold,
                        "op": criterion.op,
                    }
                )
            passed = sum(1 for c in checks if c["passed"])
            return {
                "passed": passed == len(checks),
                "passed_checks": passed,
                "total_checks": len(checks),
                "checks": checks,
            }

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            snapshots = self._snapshots()
            return {
                "scenario": self.active_scenario,
                "injected_at": _iso(self.scenario_injected_at)
                if self.scenario_injected_at
                else None,
                "chaos": self.chaos,
                "healthy": all(
                    s["health"] == "healthy"
                    for name, s in snapshots.items()
                    if name != "external-payment-api"
                ),
                "services": snapshots,
            }


def _previous_version(version: str) -> str:
    """v1.8.3 → v1.8.2. Purely cosmetic, but a rollback must name something."""
    try:
        head, tail = version.rsplit(".", 1)
        number = int("".join(ch for ch in tail if ch.isdigit()) or "0")
        return f"{head}.{max(0, number - 1)}"
    except (ValueError, IndexError):
        return f"{version}-previous"


def _compare(actual: Any, op: str, threshold: Any) -> bool:
    if actual is None:
        return False
    try:
        if op == "<=":
            return float(actual) <= float(threshold)
        if op == ">=":
            return float(actual) >= float(threshold)
        if op == "<":
            return float(actual) < float(threshold)
        if op == ">":
            return float(actual) > float(threshold)
        if op == "==":
            return str(actual) == str(threshold)
    except (TypeError, ValueError):
        return False
    return False


_simulator: Simulator | None = None
_simulator_lock = threading.Lock()


def get_simulator() -> Simulator:
    global _simulator
    if _simulator is None:
        with _simulator_lock:
            if _simulator is None:
                _simulator = Simulator()
    return _simulator


__all__ = [
    "METRIC_ALIASES",
    "METRIC_FIELDS",
    "METRIC_UNITS",
    "Simulator",
    "get_simulator",
]
