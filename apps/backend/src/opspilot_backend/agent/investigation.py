"""The investigation policy — decides what to look at next, and why.

This is the part that makes OpsPilot an Agent rather than a script.

A scripted workflow runs the same five tools every time and then explains
whatever they returned. This module does the opposite: it looks at what the
evidence collected *so far* says, works out which explanations are still
plausible, and then picks the probe that best separates them.

    evidence → signals → plausible fault domains → the probe that
    discriminates between them

Four properties matter:

1. **It is driven by evidence, not by a checklist.** A service with a clean
   CPU and no recent deploy never gets asked about CPU or deploys again.
2. **It attributes a signal to a component.** " unhealthy" is meaningless
   without saying *whose*. A database that is slow is a different diagnosis
   depending on whether the slow queries are on the caller or on postgres.
3. **It can follow the dependency graph.** When the alerting service looks
   fine but its caller-visible error rate is not, the next probe targets the
   dependency, not the service.
4. **It stops.** When no unexplored dimension can change the ranking, it says
   so instead of re-querying the same tools until the budget runs out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from opspilot_backend.agent.state import EvidenceItem, IncidentState, InvestigationStep

# A deploy older than this is history, not a suspect. The simulator's baseline
# releases land ~72h out precisely so that "there is a deployment" and "there
# was a deployment *just now*" are distinguishable facts.
_DEPLOYMENT_WINDOW_MINUTES = 120.0

# Components whose name starts with this are outside the platform: they cannot
# be restarted, so an outage there is a "third party" explanation rather than
# a "dependency is down, go fix it" one.
_EXTERNAL_PREFIX = "external-"

#: Ceiling on a single signal's weight. See ``extract_signals``.
_MAX_SIGNAL_WEIGHT = 3.0

#: Ceilings on how far corroboration can lift a domain's base confidence.
_MAX_SUPPORT_BONUS = 0.30
_MAX_REQUIRED_BONUS = 0.15

# ---------------------------------------------------------------------------
# Signals — the normalised vocabulary every source is reduced to
# ---------------------------------------------------------------------------

#: Keyword patterns that light up a signal, per evidence type.
#:
#: These are matched against the simulator's real log lines, so they are
#: deliberately specific. "circuit breaker" is *not* in ``upstream_timeout``
#: even though the simulator emits it under a bad deployment — a breaker
#: tripping is a symptom of almost any failure, and treating it as evidence of
#: an upstream timeout made every bad deploy look like a provider problem.
_LOG_PATTERNS: dict[str, tuple[str, ...]] = {
    "db_pool_saturation": (
        "connection pool", "pool limit", "queuepool", "connection timed out",
        "too many connections", "failed to acquire database connection",
        "database connection pool",
    ),
    "db_latency": ("database call timed out", "query timeout", "sqlalchemy", "deadlock"),
    "slow_queries": ("slow query", "query took", "lock wait", "blocking"),
    "oom": ("outofmemoryerror", "out-of-memory", "oom", "heap", "gc pause"),
    "cache_failure": ("redis", "cache miss", "connection refused", "sentinel", "cache read failed"),
    "upstream_timeout": ("504", "gateway timeout", "upstream call", "upstream dependency"),
    "external_provider": (
        "acme-pay", "partner payment", "payment provider", "provider outage",
        "third-party", "external-payment-api",
    ),
    "http_5xx": ("500", "internal server error", "unhandled exception", "unhandled application"),
    "deployment_recent": ("deployed", "rollout", "released", "new version"),
    "cpu_saturation": ("cpu saturation", "queue depth", "deadline exceeded"),
}

#: Metric name → (signal, threshold). "latest" above threshold lights it up.
_METRIC_PATTERNS: dict[str, tuple[str, float]] = {
    "error_rate": ("high_error_rate", 0.05),
    "latency_p95": ("high_latency", 500.0),
    "latency_p95_ms": ("high_latency", 500.0),
    "latency_p50": ("high_latency", 400.0),
    "latency_p50_ms": ("high_latency", 400.0),
    "cpu": ("cpu_saturation", 80.0),
    "cpu_percent": ("cpu_saturation", 80.0),
    # Deliberately no absolute memory threshold. A datastore idles at 4GB and
    # a service at 512MB, so "memory_mb > 1024" flagged every healthy postgres
    # as an OOM candidate. Memory pressure is only meaningful against the
    # container limit, which the status branch below checks.
    "db_connections": ("db_pool_pressure", 60.0),
    "pool_utilisation": ("db_pool_pressure", 0.6),
}

#: Fields read straight off a status snapshot, with the threshold that matters.
_STATUS_PATTERNS: tuple[tuple[str, str, float], ...] = (
    ("error_rate", "high_error_rate", 0.05),
    ("latency_p95_ms", "high_latency", 500.0),
    ("latency_p95", "high_latency", 500.0),
    ("cpu_percent", "cpu_saturation", 80.0),
    ("cpu", "cpu_saturation", 80.0),
)


@dataclass
class Signal:
    """One normalised observation, with the evidence that produced it."""

    name: str
    weight: float = 1.0
    service: str = ""
    evidence_refs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "weight": round(self.weight, 3),
            "service": self.service,
            "evidence_refs": list(self.evidence_refs),
        }


def _parse_ts(raw: Any) -> datetime | None:
    """Parse the timestamps the simulator emits (naive UTC or ISO with Z)."""
    if not isinstance(raw, str):
        return None
    text = raw.replace("Z", "").strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        match = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", text)
        if not match:
            return None
        parsed = datetime.fromisoformat(f"{match.group(1)}T{match.group(2)}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def normalise_evidence(item: EvidenceItem) -> dict[str, Any]:
    """Reduce a raw tool payload to comparable fields.

    Reasoning must not depend on whether a number arrived as ``latency_p95``
    from a metric query or ``latency_p95_ms`` from a status call, so every
    evidence row carries both the raw payload and this.
    """
    value = item.value or {}
    out: dict[str, Any] = {"type": item.type, "service": item.service}

    if isinstance(value, dict):
        for key in (
            "health", "error_rate", "latency_p50_ms", "latency_p95_ms",
            "latency_p95", "cpu_percent", "cpu", "memory_mb", "memory",
            "memory_limit_mb", "db_connections", "pool_max", "pool_utilisation",
            "request_rate", "version", "replicas",
        ):
            if key in value and value[key] is not None:
                out.setdefault("fields", {})[key] = value[key]

        series = value.get("series")
        if isinstance(series, list):
            for entry in series:
                if not isinstance(entry, dict):
                    continue
                metric = str(entry.get("metric") or "")
                latest = entry.get("latest")
                if not metric or latest is None:
                    continue
                out.setdefault("fields", {})[metric] = latest
                previous = entry.get("previous")
                if isinstance(latest, (int, float)) and isinstance(
                    previous, (int, float)
                ):
                    out.setdefault("trend", {})[metric] = {
                        "latest": latest,
                        "previous": previous,
                        "delta": round(latest - previous, 6),
                    }

        deployments = value.get("deployments")
        if isinstance(deployments, list) and deployments:
            newest = deployments[0] if isinstance(deployments[0], dict) else {}
            at = _parse_ts(newest.get("deployed_at"))
            out["deployments"] = {
                "count": len(deployments),
                "latest_version": newest.get("version"),
                "latest_status": newest.get("status"),
                "latest_at": newest.get("deployed_at"),
                "minutes_ago": (
                    round((datetime.now(timezone.utc) - at).total_seconds() / 60.0, 1)
                    if at is not None
                    else None
                ),
            }

        entries = value.get("entries")
        if isinstance(entries, list):
            out["log_count"] = len(entries)
    return out


def extract_signals(
    evidence: list[EvidenceItem],
    *,
    dependency_targets: tuple[str, ...] = (),
    now: datetime | None = None,
) -> dict[str, Signal]:
    """Turn a pile of evidence rows into weighted signals.

    ``dependency_targets`` is what lets the same observation mean two
    different things: "error_rate 40%" on the alerting service is a local
    failure, on something the alerting service calls it is a dependency
    failure, and those have different fixes.
    """
    now = now or datetime.now(timezone.utc)
    dep_targets = set(dependency_targets)
    signals: dict[str, Signal] = {}

    def bump(name: str, weight: float, ref: str, service: str = "") -> None:
        signal = signals.get(name)
        if signal is None:
            signal = Signal(name=name, weight=0.0, service=service)
            signals[name] = signal
        # Capped, not summed. The same fact re-read by a verification probe is
        # not new evidence, and without the cap a signal that happened to be
        # confirmed five times outranked one confirmed once — which quietly
        # turned "we fetched this repeatedly" into "this is the answer".
        signal.weight = min(_MAX_SIGNAL_WEIGHT, signal.weight + weight)
        if ref and ref not in signal.evidence_refs:
            signal.evidence_refs.append(ref)
        if service and not signal.service:
            signal.service = service

    for item in evidence:
        value = item.value or {}
        blob = f"{item.title} {item.description} {item.source}".lower()

        # --- log text → signals ---------------------------------------
        if item.type in {"log", "observation"} or "message" in value:
            text = str(value.get("message") or f"{item.title} {item.description}").lower()
            for name, patterns in _LOG_PATTERNS.items():
                if any(p in text or p in blob for p in patterns):
                    bump(name, 1.0, item.ref, item.service)

        # --- metric series → signals ----------------------------------
        series = value.get("series") if isinstance(value, dict) else None
        if isinstance(series, list):
            for entry in series:
                if not isinstance(entry, dict):
                    continue
                metric = str(entry.get("metric") or "")
                rule = _METRIC_PATTERNS.get(metric)
                if rule is None:
                    continue
                name, threshold = rule
                latest = entry.get("latest")
                if isinstance(latest, (int, float)) and latest >= threshold:
                    # Scale the weight by how far past the threshold it is:
                    # 6% and 60% error rate are not the same signal strength.
                    excess = latest / threshold if threshold else 1.0
                    bump(name, min(3.0, 1.0 + (excess - 1.0)), item.ref, item.service)
            if item.service in dep_targets:
                for entry in series:
                    if not isinstance(entry, dict):
                        continue
                    latest = entry.get("latest")
                    metric = str(entry.get("metric") or "")
                    if metric == "health_status" and str(latest).lower() in {
                        "degraded", "down", "critical"
                    }:
                        bump("dependency_unhealthy", 2.0, item.ref, item.service)

        # --- status snapshot → signals --------------------------------
        if isinstance(value, dict):
            health = str(value.get("health") or "").lower()
            if health in {"degraded", "down", "critical"}:
                if item.service and item.service in dep_targets:
                    bump("dependency_unhealthy", 2.0, item.ref, item.service)
                else:
                    bump("service_unhealthy", 1.0, item.ref, item.service)
                # What *kind* of component is down tells you what kind of
                # failure this is. Reading health alone, a broken cache and a
                # broken service look identical, and the only way to tell them
                # apart was a log line — which meant a status probe could never
                # confirm or falsify a cache hypothesis.
                kind = str(value.get("kind") or "").lower()
                if kind == "cache":
                    bump("cache_failure", 2.0, item.ref, item.service)
                elif kind == "external":
                    bump("external_provider", 2.0, item.ref, item.service)
                elif kind == "datastore":
                    bump("db_latency", 2.0, item.ref, item.service)

            pool_max = value.get("pool_max")
            connections = value.get("db_connections")
            if isinstance(pool_max, (int, float)) and pool_max > 0 and isinstance(
                connections, (int, float)
            ):
                if connections / pool_max >= 0.8:
                    bump("db_pool_saturation", 2.0, item.ref, item.service)
                elif connections / pool_max >= 0.6:
                    bump("db_pool_pressure", 1.0, item.ref, item.service)

            for field_name, name, threshold in _STATUS_PATTERNS:
                raw = value.get(field_name)
                if isinstance(raw, (int, float)) and raw >= threshold:
                    bump(name, 1.5, item.ref, item.service)

            memory = value.get("memory_mb")
            limit = value.get("memory_limit_mb") or value.get("memory_limit")
            if isinstance(memory, (int, float)) and isinstance(limit, (int, float)) and limit > 0:
                ratio = memory / limit
                if ratio >= 0.85:
                    bump("oom", 2.0, item.ref, item.service)
                elif ratio >= 0.70:
                    bump("oom", 1.0, item.ref, item.service)

        # --- deployments → change signal, only if it is recent ---------
        if item.type == "deployment":
            deployments = value.get("deployments") if isinstance(value, dict) else None
            if isinstance(deployments, list) and deployments:
                # ``get_deployments`` returns newest first.
                newest = deployments[0] if isinstance(deployments[0], dict) else {}
                at = _parse_ts(newest.get("deployed_at"))
                if at is not None:
                    minutes = (now - at).total_seconds() / 60.0
                    if minutes <= _DEPLOYMENT_WINDOW_MINUTES:
                        # A change 4 minutes before onset is a much stronger
                        # suspect than one from two hours ago.
                        recency = 1.0 - min(1.0, minutes / _DEPLOYMENT_WINDOW_MINUTES)
                        bump("deployment_recent", 1.0 + recency, item.ref, item.service)

    return signals


def signals_as_weights(signals: dict[str, Signal]) -> dict[str, float]:
    return {name: s.weight for name, s in signals.items()}


# ---------------------------------------------------------------------------
# Fault domains
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FaultDomain:
    """A candidate explanation and the signals that would support it."""

    key: str
    #: What the recovery layer is told, so a diagnosis maps to a real action.
    category: str
    label: str
    required: tuple[str, ...]
    supporting: tuple[str, ...]
    #: Signals that make this explanation much less likely.
    contradicts: tuple[str, ...] = ()
    base_confidence: float = 0.45


FAULT_DOMAINS: tuple[FaultDomain, ...] = (
    FaultDomain(
        key="database",
        category="database",
        label="Database connection pool exhaustion",
        required=("db_pool_saturation",),
        supporting=("db_pool_pressure", "db_latency", "high_latency", "service_unhealthy"),
        base_confidence=0.58,
    ),
    FaultDomain(
        key="slow_database",
        category="database",
        label="Slow or blocked database queries",
        required=("slow_queries",),
        supporting=("db_latency", "high_latency", "high_error_rate", "db_pool_pressure"),
        contradicts=("db_pool_saturation",),
        base_confidence=0.52,
    ),
    FaultDomain(
        key="deployment",
        category="deployment",
        label="A recent change introduced the regression",
        required=("deployment_recent",),
        supporting=("high_error_rate", "http_5xx", "service_unhealthy"),
        # A deploy is a *temporal correlation*; a saturated pool or an
        # exhausted heap is a *mechanism*. When both are present the mechanism
        # is the better answer, and it is also the one with a better fix: the
        # recovery for "database" is still "roll back the release", but the
        # reverse is not true.
        contradicts=(
            "oom", "cpu_saturation", "db_pool_saturation", "slow_queries",
            "cache_failure", "external_provider",
        ),
        base_confidence=0.52,
    ),
    FaultDomain(
        key="memory",
        category="memory",
        label="Memory leak leading to OOM pressure",
        required=("oom",),
        supporting=("service_unhealthy", "high_latency"),
        base_confidence=0.55,
    ),
    FaultDomain(
        key="redis",
        category="redis",
        label="Cache layer unavailable",
        required=("cache_failure",),
        supporting=("high_latency", "service_unhealthy", "high_error_rate"),
        base_confidence=0.60,
    ),
    FaultDomain(
        key="third_party",
        category="third_party",
        label="Third-party provider failing or timing out",
        required=("external_provider",),
        supporting=("upstream_timeout", "high_latency", "high_error_rate"),
        base_confidence=0.60,
    ),
    FaultDomain(
        key="capacity",
        category="capacity",
        label="Capacity saturation on the affected service",
        required=("cpu_saturation",),
        supporting=("high_latency", "high_error_rate", "service_unhealthy"),
        contradicts=("oom",),
        base_confidence=0.45,
    ),
    FaultDomain(
        key="cascading",
        category="dependency",
        label="A dependency is failing and the alerting service reports it",
        required=("dependency_unhealthy",),
        supporting=("upstream_timeout", "high_error_rate", "http_5xx", "service_unhealthy"),
        # This is the explanation of *last* resort: it only survives when no
        # specific failure mode has been characterised anywhere — not on the
        # caller, and not on the dependency. Any named cause beats "something
        # downstream is broken", because a named cause has a real fix.
        contradicts=(
            "deployment_recent", "cache_failure", "external_provider",
            "slow_queries", "db_pool_saturation", "oom", "cpu_saturation",
        ),
        base_confidence=0.45,
    ),
)

DOMAIN_BY_KEY: dict[str, FaultDomain] = {d.key: d for d in FAULT_DOMAINS}


def score_domains(
    signals: dict[str, Signal], *, exclude: tuple[str, ...] = ()
) -> list[tuple[str, float]]:
    """Rank explanations by how much evidence supports them.

    A domain with no ``required`` signal present is not a candidate at all —
    that is what keeps the Agent from asserting a database cause because the
    database happened to be mentioned in a log line.

    ``exclude`` carries the domains already tested and rejected in earlier
    rounds, so "generate a new hypothesis" cannot mean "re-propose the one
    that just failed".
    """
    weights = {name: s.weight for name, s in signals.items()}
    excluded = set(exclude)
    scored: list[tuple[str, float]] = []
    for domain in FAULT_DOMAINS:
        if domain.key in excluded:
            continue
        if not all(weights.get(sig, 0.0) > 0 for sig in domain.required):
            continue
        # Both bonuses are capped. Uncapped, a domain with four corroborating
        # signals scored ~1.0 no matter how weak its base confidence was, so
        # "a dependency is implicated" outranked "this specific thing broke".
        support = sum(min(weights.get(sig, 0.0), 2.0) for sig in domain.supporting)
        required_strength = sum(
            min(weights.get(sig, 0.0), 3.0) for sig in domain.required
        )
        contradiction = sum(
            min(weights.get(sig, 0.0), 3.0) for sig in domain.contradicts
        )
        score = (
            domain.base_confidence
            + min(_MAX_SUPPORT_BONUS, 0.05 * support)
            + min(_MAX_REQUIRED_BONUS, 0.05 * required_strength)
            - 0.10 * contradiction
        )
        if score <= 0.05:
            continue
        scored.append((domain.key, round(min(0.95, score), 3)))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


def evidence_refs_for(
    signals: dict[str, Signal], domain_key: str
) -> list[str]:
    """The evidence a given explanation is allowed to cite.

    A hypothesis may only cite rows that produced one of its signals. This is
    what stops "here is everything we collected" from masquerading as support.
    """
    domain = DOMAIN_BY_KEY.get(domain_key)
    if domain is None:
        return []
    wanted = set(domain.required) | set(domain.supporting)
    refs: list[str] = []
    for name, signal in signals.items():
        if name not in wanted:
            continue
        for ref in signal.evidence_refs:
            if ref not in refs:
                refs.append(ref)
    return sorted(refs, key=lambda r: (len(r), r))


# ---------------------------------------------------------------------------
# Dimensions — the things that can still be looked at
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Dimension:
    """An unexplored line of enquiry and the tool that answers it."""

    key: str
    label: str
    tool: str
    discriminates: tuple[str, ...]
    rationale: str
    build: Callable[..., dict[str, Any]]
    priority: float = 1.0
    #: True when this probe targets the service's dependencies rather than
    #: the service itself.
    on_dependencies: bool = False
    #: True for the *deep* dependency probes (logs, changes). A dependency
    #: already measured healthy does not get its logs read — that is how the
    #: Agent affords to drill properly into the one that is actually broken
    #: instead of spending four calls finding out three things are fine.
    follow_unhealthy_only: bool = False


def _args(service: str, **extra: Any) -> dict[str, Any]:
    return {"service": service, **extra}


DIMENSIONS: tuple[Dimension, ...] = (
    Dimension(
        key="error_signature",
        label="Read the error signature the service is emitting",
        tool="query_logs",
        discriminates=("database", "slow_database", "redis", "third_party", "memory", "deployment"),
        rationale="The error text names the failure class directly.",
        build=lambda s, **kw: _args(s, level="ERROR", minutes=30),
        priority=3.0,
    ),
    Dimension(
        key="resource_metrics",
        label="Measure which resource actually moved",
        tool="query_metrics",
        discriminates=("capacity", "memory", "database", "slow_database"),
        rationale="Separates a saturated resource from a broken dependency.",
        build=lambda s, **kw: _args(
            s,
            metric_names=["error_rate", "latency_p95", "cpu", "memory", "db_connections"],
            minutes=30,
        ),
        priority=2.8,
    ),
    Dimension(
        key="change_correlation",
        label="Check whether a change landed before the incident",
        tool="get_deployments",
        discriminates=("deployment", "memory", "database"),
        rationale="A deploy minutes before onset is the strongest prior.",
        build=lambda s, **kw: _args(s, limit=5),
        priority=2.6,
    ),
    Dimension(
        key="dependency_health",
        label="Probe the services this one depends on",
        tool="get_service_status",
        discriminates=("cascading", "redis", "slow_database", "third_party"),
        rationale=(
            "The alerting service looks locally healthy, so the failure may be "
            "in something it calls."
        ),
        build=lambda s, **kw: _args(kw.get("target", s)),
        priority=2.4,
        on_dependencies=True,
    ),
    Dimension(
        key="dependency_logs",
        label="Read the dependency's own error signature",
        tool="query_logs",
        discriminates=("cascading", "redis", "slow_database", "third_party", "deployment"),
        rationale="Confirms the dependency's failure mode, not just its health.",
        build=lambda s, **kw: _args(kw.get("target", s), level="ERROR", minutes=30),
        priority=2.0,
        on_dependencies=True,
        follow_unhealthy_only=True,
    ),
    Dimension(
        key="dependency_changes",
        label="Check whether the dependency also changed",
        tool="get_deployments",
        discriminates=("cascading", "deployment"),
        rationale="Distinguishes 'they broke' from 'we changed how we call them'.",
        build=lambda s, **kw: _args(kw.get("target", s), limit=5),
        priority=1.6,
        on_dependencies=True,
        follow_unhealthy_only=True,
    ),
    Dimension(
        key="commit_diff",
        label="Diff what actually shipped",
        tool="get_recent_commits",
        discriminates=("deployment", "memory", "database"),
        rationale="Only worth reading once a deploy has been correlated.",
        build=lambda s, **kw: {"repository": s, "limit": 5},
        priority=1.2,
    ),
    Dimension(
        key="remediation_guidance",
        label="Pull the runbook for this signature",
        tool="search_runbooks",
        discriminates=(),
        rationale="Known guidance for the observed signature.",
        build=lambda s, **kw: {"query": kw.get("query") or f"{s} error latency", "limit": 3},
        priority=0.6,
    ),
)

DIMENSION_BY_KEY: dict[str, Dimension] = {d.key: d for d in DIMENSIONS}

#: Dimensions that answer "is the problem here?" before anything else can be
#: ruled out. Dependency probes are only worth their budget once these are in.
_LOCAL_DIMENSIONS = frozenset({"error_signature", "resource_metrics"})


# ---------------------------------------------------------------------------
# Relevance
# ---------------------------------------------------------------------------


def relevance_for(
    item: EvidenceItem, signals: dict[str, Signal], domains: list[tuple[str, float]]
) -> tuple[str, str]:
    """How much this evidence bears on the question being asked.

    An evidence row is relevant when the signals it produced are the signals
    one of the leading explanations needs. Everything else is context, and
    saying so is the difference between a diagnosis and a data dump.
    """
    if not domains:
        return ("MEDIUM", "collected before any explanation was ranked")

    item_signals = {
        name for name, signal in signals.items() if item.ref in signal.evidence_refs
    }
    if not item_signals:
        return ("LOW", "no signal extracted from this item")

    top_keys = [key for key, _ in domains[:2]]
    for key in top_keys:
        domain = DOMAIN_BY_KEY.get(key)
        if domain is None:
            continue
        wanted = set(domain.required) | set(domain.supporting)
        hit = item_signals & wanted
        if hit and item_signals & set(domain.required):
            return (
                "HIGH",
                f"required by the leading explanation ({domain.key}): "
                + ", ".join(sorted(hit)),
            )
        if hit:
            return (
                "MEDIUM",
                f"supports the leading explanation ({domain.key}): "
                + ", ".join(sorted(hit)),
            )
    return ("LOW", "signals present but not used by any leading explanation")


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass
class InvestigationAssessment:
    """What the Agent currently believes and what it still needs."""

    signals: dict[str, Signal]
    domains: list[tuple[str, float]]
    top_domain: str | None
    confidence: float
    explored: set[str]
    probes: list[InvestigationStep]
    saturated: bool
    reason: str
    dependency_targets: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "top_domain": self.top_domain,
            "confidence": round(self.confidence, 3),
            "domains": [{"key": k, "score": s} for k, s in self.domains],
            "signals": [s.as_dict() for s in self.signals.values()],
            "explored": sorted(self.explored),
            "probes": [p.model_dump() for p in self.probes],
            "saturated": self.saturated,
            "reason": self.reason,
            "dependency_targets": list(self.dependency_targets),
        }


def dependency_targets(state: IncidentState) -> tuple[str, ...]:
    """The components this incident's service calls, external ones excluded.

    Sourced from the dependency evidence ``load_context`` collected, so the
    Agent is following the topology it read rather than a hardcoded list.
    Ordered by criticality: with a limited probe budget, the dependency that
    matters most gets looked at first.
    """
    service = state.incident.service
    found: dict[str, float] = {}
    for item in state.evidence:
        if item.type != "dependency":
            continue
        for edge in ((item.value or {}).get("edges") or []):
            if not isinstance(edge, dict):
                continue
            target = str(edge.get("depends_on") or "")
            if not target or target == service:
                continue
            if target.startswith(_EXTERNAL_PREFIX):
                continue
            try:
                criticality = float(edge.get("criticality") or 0.0)
            except (TypeError, ValueError):
                criticality = 0.0
            found[target] = max(criticality, found.get(target, 0.0))
    ordered = sorted(found.items(), key=lambda pair: pair[1], reverse=True)
    return tuple(name for name, _ in ordered)


def unhealthy_dependencies(
    state: IncidentState, targets: tuple[str, ...]
) -> tuple[str, ...]:
    """Which of those dependencies we have already measured as unhealthy.

    Probing every dependency is affordable on a three-service graph and
    pointless on a real one. Once one is known to be sick, the follow-up
    probes go there — that is what turns "a dependency might be broken" into
    "checkout-service is broken, and here is why".
    """
    sick = {
        str(item.service)
        for item in state.evidence
        if str(item.service) in set(targets)
        and str((item.value or {}).get("health") or "").lower()
        in {"degraded", "down", "critical"}
    }
    return tuple(t for t in targets if t in sick)


def assess(
    state: IncidentState,
    *,
    dependency_targets: tuple[str, ...] | None = None,
    max_probes: int = 4,
    excluded_domains: tuple[str, ...] = (),
    dependency_probe_limit: int = 3,
) -> InvestigationAssessment:
    """Decide the next probes from the evidence collected so far."""
    deps = tuple(dependency_targets or ()) or dependency_targets_of(state)
    # Drill into what is already known to be broken before sweeping the rest.
    sick = unhealthy_dependencies(state, deps)
    signals = extract_signals(state.evidence, dependency_targets=deps)
    domains = score_domains(signals, exclude=excluded_domains)
    top_domain = domains[0][0] if domains else None
    confidence = domains[0][1] if domains else 0.0

    explored = set(state.plan.ruled_out or [])
    explored.update(_explored_from_evidence(state.evidence, state.incident.service))

    ranked = _rank_dimensions(domains, explored, deps, signals)

    probes: list[InvestigationStep] = []
    for dimension, _score in ranked:
        if len(probes) >= max_probes:
            break
        if dimension.on_dependencies:
            # Read the dependency already known to be sick first, then the
            # rest. Ordering, not filtering: narrowing to the sick ones alone
            # meant the one that was *actually* broken never got probed,
            # because only its callers had been measured as unhealthy.
            if dimension.follow_unhealthy_only:
                healthy = {
                    str(item.service)
                    for item in state.evidence
                    if str(item.service) in set(deps)
                    and str((item.value or {}).get("health") or "")
                    and str(item.service) not in set(sick)
                }
                candidates = tuple(
                    t for t in (*sick, *deps) if t not in healthy
                )
            else:
                candidates = (*sick, *(t for t in deps if t not in sick))
            pending = [
                t for t in candidates if f"{dimension.key}:{t}" not in explored
            ]
            for target in pending[:dependency_probe_limit]:
                if len(probes) >= max_probes:
                    break
                probes.append(
                    InvestigationStep(
                        tool=dimension.tool,
                        arguments=dimension.build(state.incident.service, target=target),
                        rationale=f"{dimension.rationale} (target: {target})",
                        depends_on_signal=f"{dimension.key}:{target}",
                    )
                )
                explored.add(f"{dimension.key}:{target}")
        else:
            probes.append(
                InvestigationStep(
                    tool=dimension.tool,
                    arguments=dimension.build(state.incident.service),
                    rationale=dimension.rationale,
                    depends_on_signal=dimension.key,
                )
            )
            explored.add(dimension.key)

    if not probes:
        return InvestigationAssessment(
            signals=signals,
            domains=domains,
            top_domain=top_domain,
            confidence=confidence,
            explored=explored,
            probes=[],
            saturated=True,
            reason=(
                "every dimension that could change the ranking has been "
                "explored; further queries would repeat themselves"
            ),
            dependency_targets=deps,
        )

    return InvestigationAssessment(
        signals=signals,
        domains=domains,
        top_domain=top_domain,
        confidence=confidence,
        explored=explored,
        probes=probes,
        saturated=False,
        reason=f"top candidate '{top_domain}' at {confidence:.2f}"
        if top_domain
        else "no candidate explanation yet",
        dependency_targets=deps,
    )


# Kept as a module-level alias so callers that imported the state-derived
# helper by name still resolve.
dependency_targets_of = dependency_targets


def _explored_from_evidence(
    evidence: list[EvidenceItem], service: str
) -> set[str]:
    """Dimensions already answered, derived from what has been collected."""
    explored: set[str] = set()
    for item in evidence:
        source = str(item.source or "")
        base = source.split(":")[0]
        mapping = {
            "query_logs": "error_signature",
            "query_metrics": "resource_metrics",
            "get_deployments": "change_correlation",
            "get_recent_commits": "commit_diff",
            "search_runbooks": "remediation_guidance",
            "get_service_status": "dependency_health",
        }
        key = mapping.get(base)
        if key is None:
            continue
        # A probe against another component is a different line of enquiry.
        target = str(item.service or "")
        if target and target != service:
            if base == "query_logs":
                explored.add(f"dependency_logs:{target}")
            elif base == "get_deployments":
                explored.add(f"dependency_changes:{target}")
            else:
                explored.add(f"{key}:{target}")
            continue
        if key != "dependency_health":
            explored.add(key)
    return explored


def _rank_dimensions(
    domains: list[tuple[str, float]],
    explored: set[str],
    dependency_targets: tuple[str, ...],
    signals: dict[str, Signal],
) -> list[tuple[Dimension, float]]:
    """Score unexplored dimensions by how much they can change the ranking."""
    domain_scores = dict(domains)
    ranked: list[tuple[Dimension, float]] = []

    for dimension in DIMENSIONS:
        if dimension.key in explored:
            continue
        if dimension.on_dependencies:
            if not dependency_targets:
                continue
            pending = [
                t for t in dependency_targets
                if f"{dimension.key}:{t}" not in explored
            ]
            if not pending:
                continue
        score = dimension.priority
        for key in dimension.discriminates:
            score += domain_scores.get(key, 0.0)
        if dimension.on_dependencies:
            # Following the graph pays off in exactly two situations: a
            # dependency is already known to be sick (drill into it), or the
            # alerting service has been looked at locally and nothing there
            # explains it. Otherwise it is speculation with a tool-call price.
            if "dependency_unhealthy" in signals:
                score += 2.0
            elif not domains and _LOCAL_DIMENSIONS <= explored:
                score += 1.5
        ranked.append((dimension, score))

    ranked.sort(key=lambda pair: pair[1], reverse=True)
    return ranked


__all__ = [
    "DOMAIN_BY_KEY",
    "DIMENSIONS",
    "DIMENSION_BY_KEY",
    "Dimension",
    "FAULT_DOMAINS",
    "FaultDomain",
    "InvestigationAssessment",
    "Signal",
    "assess",
    "dependency_targets",
    "evidence_refs_for",
    "extract_signals",
    "normalise_evidence",
    "relevance_for",
    "score_domains",
    "signals_as_weights",
]
