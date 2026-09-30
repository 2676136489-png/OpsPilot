"""Every node of the OpsPilot incident-response workflow.

Each node declares (via :func:`workflow_node`) what it reads, what it writes,
how many attempts it gets and how long it may run. None of them writes to the
database directly — they go through ``NodeContext``.

The workflow is not a straight line. Three edges go backwards on purpose:
a failed evidence round and a falsified hypothesis both return to the planner,
and a recovery that does not hold goes to ``rollback`` before anything else is
decided. All three are bounded by state, not by hope.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from opspilot_backend.agent.analysis import (
    apply_verification,
    decide_root_cause,
    generate_hypotheses,
    recovery_target,
    verification_criteria,
    verification_query,
    verification_target,
)
from opspilot_backend.agent.context import NodeContext, get_context
from opspilot_backend.agent.investigation import (
    assess,
    dependency_targets,
    extract_signals,
    normalise_evidence,
    relevance_for,
    score_domains,
)
from opspilot_backend.agent.llm import complete_charged, get_llm, parse_json_object
from opspilot_backend.agent.node_spec import NodeSpec, workflow_node
from opspilot_backend.agent.recovery import (
    build_recovery_actions,
    may_attempt_rollback,
    plan_summary,
)
from opspilot_backend.agent.state import (
    DiagnosisInfo,
    EvidenceItem,
    HypothesisItem,
    IncidentState,
    InvestigationStep,
    RecoveryActionItem,
)
from opspilot_backend.core.config import get_settings
from opspilot_backend.core.logging import log_event
from opspilot_backend.domain.enums import (
    AgentStage,
    DiagnosisOutcome,
    EscalationReason,
    EventType,
    HypothesisStatus,
    RecoveryActionStatus,
    RecoveryPlanStatus,
    RiskLevel,
    VerificationStatus,
)
from opspilot_backend.tools.registry import TOOL_REGISTRY

#: Confidence at which the Agent stops investigating and commits to an
#: explanation. Below this it keeps probing while it still has budget.
_COMMIT_CONFIDENCE = 0.75


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_evidence_ref(items: list[EvidenceItem]) -> str:
    """Next free E-ref via max-scan. Nodes create several items before their
    state update lands, so ``len(state.evidence) + 1`` would hand out the same
    ref twice and the DB upsert would silently overwrite evidence."""
    highest = 0
    for item in items:
        digits = item.ref[1:] if item.ref.startswith("E") else ""
        if digits.isdigit():
            highest = max(highest, int(digits))
    return f"E{highest + 1:03d}"


def _evidence_item(
    state: IncidentState,
    running: list[EvidenceItem] | None = None,
    *,
    type: str,
    source: str,
    title: str,
    description: str = "",
    value: Any = None,
    severity: str = "medium",
    confidence: float = 0.6,
    tool_call_id: str | None = None,
    service: str | None = None,
) -> EvidenceItem:
    """Build one evidence item.

    ``running`` is the node's own in-flight evidence list: refs are allocated
    against ``state.evidence`` *plus* everything created earlier in this node,
    so the second item in a node gets E002, not another E001.
    """
    return EvidenceItem(
        ref=_next_evidence_ref([*state.evidence, *(running or [])]),
        type=type,
        source=source,
        # Defaults to the alerting service, but a probe run against a
        # dependency must be attributed to that dependency — otherwise
        # "the thing we call is down" is recorded as "we are down".
        service=service or state.incident.service,
        title=title,
        description=description,
        value=value if isinstance(value, dict) else ({"raw": value} if value is not None else None),
        severity=severity,
        confidence=confidence,
        tool_call_id=tool_call_id,
        observed_at=_now(),
    )


def _result_value(result: Any) -> dict[str, Any]:
    return result.result if isinstance(result.result, dict) else {}


async def _escalate(
    ctx: NodeContext,
    state: IncidentState,
    reason: str,
    *,
    stage: AgentStage = AgentStage.RECOVERY_EXECUTOR,
    detail: dict[str, Any] | None = None,
) -> None:
    """Hand the incident to a human and say why.

    Escalation is a first-class outcome, not a failure of the Agent: the run
    reached the edge of what it is allowed to do and stopped there instead of
    pretending. The incident status, the run event and the reason all move
    together so the timeline cannot disagree with the state machine.
    """
    await ctx.persistence.set_incident_status(
        state.incident.incident_id,
        "ESCALATED",
        stage=stage,
        summary=reason,
    )
    await ctx.emit(
        EventType.RUN_ESCALATED.value,
        {"reason": reason, "detail": detail or {}},
        stage=stage,
    )


def _verification_targets(state: IncidentState) -> list[str]:
    """Every component whose health the recovery claimed to restore.

    The alerting service is always probed. The diagnosed dependency and the
    target of each executed action are added when they differ — a pool
    exhaustion declared fixed at checkout-service says nothing about the
    datastore it was exhausting.
    """
    targets: list[str] = [state.incident.service]
    dependency = recovery_target(state)
    if dependency:
        targets.append(dependency)

    executed = set(state.recovery.executed_refs or ())
    for action in state.recovery.actions:
        if action.ref in executed and action.target_service:
            targets.append(action.target_service)

    seen: list[str] = []
    for name in targets:
        if name and name not in seen:
            seen.append(name)
    return seen


#: Checks that only make sense for a service that owns a process. `cpu_percent`
#: on a datastore is reported but is not a criterion the plan claimed to fix.
def _health_checks(target: str, status: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn one component's status into pass/fail checks with the raw numbers.

    Deliberately returns the actual value and the threshold alongside the
    verdict, so the UI can show *why* something failed instead of a bare red
    cross.
    """
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, actual: Any, threshold: Any) -> None:
        checks.append(
            {"name": f"{target}.{name}", "passed": bool(passed),
             "actual": actual, "threshold": threshold}
        )

    health = str(status.get("health") or "unknown").lower()
    add("health", health == "healthy", health, "healthy")

    error_rate = status.get("error_rate")
    if error_rate is not None:
        add("error_rate", float(error_rate) <= 0.01, error_rate, 0.01)

    latency = status.get("latency_p95")
    if latency is not None:
        add("latency_p95", float(latency) <= 500.0, latency, 500.0)

    kind = str(status.get("kind") or "service").lower()
    if kind == "service":
        cpu = status.get("cpu_percent")
        if cpu is not None:
            add("cpu_percent", float(cpu) < 80.0, cpu, 80.0)

    pool_max = status.get("pool_max")
    connections = status.get("db_connections")
    if pool_max and connections is not None:
        ceiling = 0.8 * float(pool_max)
        add("db_connections", float(connections) <= ceiling, connections, round(ceiling, 1))

    limit = status.get("memory_limit_mb")
    memory = status.get("memory_mb")
    if limit and memory is not None:
        ceiling = 0.85 * float(limit)
        add("memory_mb", float(memory) <= ceiling, memory, round(ceiling, 1))

    return checks


def _rollbackable(recovery: dict[str, Any]) -> bool:
    """Is there an executed action the Agent is allowed to compensate?"""
    executed = set(recovery.get("executed_refs") or ())
    for action in recovery.get("actions") or []:
        if str(action.get("ref")) not in executed:
            continue
        if not action.get("rollback_tool"):
            continue
        if not may_attempt_rollback(str(action.get("risk_level") or "")):
            continue
        return True
    return False


def _current_replicas(state: IncidentState, service: str) -> int | None:
    """The last replica count the Agent actually measured for a component.

    Read from evidence rather than assumed, because every capacity action is a
    relative one: "scale to N" means nothing without the current N. Newest
    first, since a fleet that has already been scaled during this incident has
    more than one count in the record.
    """
    for item in reversed(state.evidence):
        value = item.value if isinstance(item.value, dict) else {}
        if str(item.service or "") != service:
            continue
        replicas = value.get("replicas")
        if isinstance(replicas, (int, float)) and replicas > 0:
            return int(replicas)
    return None


def _significant_log_entries(
    entries: list[Any], *, limit: int = 8
) -> list[dict[str, Any]]:
    """Pick the log lines worth turning into evidence.

    Two traps here, both of which silently destroyed diagnoses:

    * Logs come back oldest-first, so ``entries[:8]`` keeps the eight *first*
      lines of the window — typically minutes of identical, pre-incident
      noise, with the one line that names the fault sitting just past the cut.
    * A fault that repeats produces dozens of identical lines. Keeping them
      all inflates a signal's weight for no information gain.

    So: newest first, collapsed by message text.
    """
    rows = [e for e in entries if isinstance(e, dict)]
    newest_first = list(reversed(rows))
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for entry in newest_first:
        key = str(entry.get("message") or "")[:160]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(entry)
        if len(out) >= limit:
            break
    return out


def _annotate_evidence(
    evidence: list[EvidenceItem],
    *,
    dependency_targets: tuple[str, ...] = (),
) -> None:
    """Attach the normalized form and the relevance verdict to every row.

    Relevance is a property of the *current* question, not of the row, so it
    is recomputed for the whole set rather than once at collection time: an
    item that looked like noise when it arrived becomes the key fact once the
    ranking shifts.
    """
    signals = extract_signals(evidence, dependency_targets=dependency_targets)
    domains = score_domains(signals)
    for item in evidence:
        item.normalized = normalise_evidence(item)
        relevance, reason = relevance_for(item, signals, domains)
        item.relevance = relevance
        item.relevance_reason = reason


async def _persist_evidence(
    ctx: NodeContext,
    state: IncidentState,
    before: int,
    new_evidence: list[EvidenceItem],
    *,
    dependency_targets: tuple[str, ...] = (),
    stage: AgentStage,
) -> None:
    """Store and announce the evidence this node produced."""
    fresh = new_evidence[before:]
    if not fresh:
        return
    _annotate_evidence(new_evidence, dependency_targets=dependency_targets)
    await ctx.persistence.save_evidence(
        state.incident.incident_id,
        ctx.run_id,
        [e.model_dump() for e in fresh],
    )
    for item in fresh:
        await ctx.emit(
            EventType.EVIDENCE_CREATED.value,
            {
                "ref": item.ref,
                "type": item.type,
                "title": item.title,
                "severity": item.severity,
                "confidence": item.confidence,
                "relevance": item.relevance,
                "source": item.source,
                "service": item.service,
            },
            stage=stage,
        )


# ---------------------------------------------------------------------------
# 1. Load context
# ---------------------------------------------------------------------------


@workflow_node(
    NodeSpec(
        stage=AgentStage.LOAD_CONTEXT,
        reads=("incident",),
        writes=("evidence", "execution"),
        description="Load service status and dependency topology before anything else.",
        max_attempts=2,
        timeout_s=30.0,
    )
)
async def load_context(state: IncidentState, config: RunnableConfig) -> dict[str, Any]:
    ctx = get_context(config)
    service = state.incident.service
    if not service:
        return {"decision": "stop"}

    status = await ctx.call_tool(
        "get_service_status", {"service": service}, stage=AgentStage.LOAD_CONTEXT
    )
    new_evidence: list[EvidenceItem] = list(state.evidence)
    if status.ok:
        value = _result_value(status)
        new_evidence.append(
            _evidence_item(
                state, new_evidence,
                type="metric",
                source="get_service_status",
                title=f"{service} health is {value.get('health', 'unknown')}",
                description=(
                    f"error_rate={value.get('error_rate')} "
                    f"latency_p95={value.get('latency_p95')} "
                    f"db_connections={value.get('db_connections')}"
                ),
                value=value,
                severity="high" if value.get("health") != "healthy" else "low",
                confidence=0.9,
                tool_call_id=status.tool_call_id,
            )
        )
        await ctx.persistence.set_incident_status(
            state.incident.incident_id,
            "TRIAGING",
            stage=AgentStage.LOAD_CONTEXT,
            summary="Agent loaded service context",
        )

    deps = await ctx.call_tool("get_dependencies", {}, stage=AgentStage.LOAD_CONTEXT)
    if deps.ok:
        value = _result_value(deps)
        upstream = [
            e for e in value.get("edges", []) if e.get("service") == service
        ]
        new_evidence.append(
            _evidence_item(
                state, new_evidence,
                type="dependency",
                source="get_dependencies",
                title=f"{service} depends on {len(upstream)} component(s)",
                description=", ".join(e.get("depends_on", "") for e in upstream),
                value={"edges": upstream},
                severity="low",
                confidence=0.8,
                tool_call_id=deps.tool_call_id,
            )
        )

    # Persist this node's own evidence — later nodes must be able to cite it
    # by ref, and the timeline must show what the agent saw first.
    await _persist_evidence(
        ctx, state, 0, new_evidence, stage=AgentStage.LOAD_CONTEXT
    )

    return {"evidence": new_evidence, "decision": "continue"}


# ---------------------------------------------------------------------------
# 2. Triage
# ---------------------------------------------------------------------------


@workflow_node(
    NodeSpec(
        stage=AgentStage.TRIAGE,
        reads=("evidence",),
        writes=("meta", "plan"),
        description="Assess blast radius and decide whether to investigate further.",
        max_attempts=1,
        timeout_s=20.0,
    )
)
async def triage(state: IncidentState, config: RunnableConfig) -> dict[str, Any]:
    ctx = get_context(config)
    unhealthy = [
        e for e in state.evidence if (e.value or {}).get("health") not in (None, "healthy")
    ]
    severity = state.incident.severity
    if unhealthy:
        severity = "SEV1" if len(unhealthy) > 1 else "SEV2"

    await ctx.emit(
        EventType.INVESTIGATION_STARTED.value,
        {"service": state.incident.service, "severity": severity},
        stage=AgentStage.TRIAGE,
    )
    await ctx.persistence.set_incident_status(
        state.incident.incident_id,
        "TRIAGING",
        stage=AgentStage.TRIAGE,
        summary=f"Triaged as {severity}",
    )
    plan = state.plan.model_dump()
    plan["iteration"] = 0
    return {"plan": plan, "decision": "continue"}


# ---------------------------------------------------------------------------
# 3. Investigation planner — decides WHAT to query, not "query everything"
# ---------------------------------------------------------------------------


@workflow_node(
    NodeSpec(
        stage=AgentStage.INVESTIGATION_PLANNER,
        reads=("evidence", "plan"),
        writes=("plan",),
        description=(
            "Rank the explanations the evidence still allows and pick the "
            "probes that separate them."
        ),
        max_attempts=1,
        timeout_s=20.0,
    )
)
async def investigation_planner(
    state: IncidentState, config: RunnableConfig
) -> dict[str, Any]:
    """Choose the next probes from what the evidence currently says.

    There is no fixed checklist here. The planner re-reads *all* evidence on
    every pass, works out which fault domains are still live, and selects the
    unexplored dimensions that would move the ranking. A service whose CPU is
    clean is never asked about CPU twice.
    """
    ctx = get_context(config)
    plan = state.plan.model_dump()
    plan["iteration"] = int(plan.get("iteration", 0)) + 1
    plan["steps"] = []
    plan["last_round_evidence"] = len(state.evidence)

    deps = dependency_targets(state)
    # Never plan more probes than the budget can actually pay for.
    affordable = min(4, ctx.budget.remaining_tool_calls)
    assessment = assess(
        state,
        dependency_targets=deps,
        max_probes=affordable,
        excluded_domains=tuple(state.plan.rejected_domains or ()),
    )
    steps = assessment.probes
    plan["steps"] = [s.model_dump() for s in steps]
    plan["ruled_out"] = sorted(assessment.explored)

    await ctx.emit(
        EventType.INVESTIGATION_PLAN_CREATED.value,
        {
            "iteration": plan["iteration"],
            "tools": [s.tool for s in steps],
            "targets": [s.arguments.get("service") for s in steps],
            "domains": [{"key": k, "score": s} for k, s in assessment.domains],
            "top_domain": assessment.top_domain,
            "confidence": round(assessment.confidence, 3),
            "rationale": assessment.reason,
            "saturated": assessment.saturated,
            "budget": ctx.budget.as_dict()["spent"],
        },
        stage=AgentStage.INVESTIGATION_PLANNER,
    )
    await ctx.persistence.set_incident_status(
        state.incident.incident_id,
        "INVESTIGATING",
        stage=AgentStage.INVESTIGATION_PLANNER,
        summary=(
            f"Round {plan['iteration']}: {len(steps)} probe(s); "
            f"{assessment.reason}"
        ),
    )
    return {"plan": plan, "decision": "continue"}


# ---------------------------------------------------------------------------
# 4. Parallel investigation — concurrent tool calls, then evidence
# ---------------------------------------------------------------------------


@workflow_node(
    NodeSpec(
        stage=AgentStage.PARALLEL_INVESTIGATION,
        reads=("plan",),
        writes=("evidence", "plan", "execution"),
        description="Execute the planned tool calls concurrently and turn results into evidence.",
        max_attempts=2,
        timeout_s=60.0,
    )
)
async def parallel_investigation(
    state: IncidentState, config: RunnableConfig
) -> dict[str, Any]:
    ctx = get_context(config)
    settings = get_settings()
    # LangGraph validates the state, so steps may come back as models —
    # normalise everything to dicts before touching them.
    raw_steps = [
        s.model_dump() if isinstance(s, InvestigationStep) else dict(s)
        for s in (state.plan.steps or [])
    ]
    steps = [InvestigationStep(**s) for s in raw_steps]
    semaphore = asyncio.Semaphore(max(1, settings.agent_max_parallel_tools))

    async def run(step: InvestigationStep) -> tuple[InvestigationStep, Any]:
        async with semaphore:
            result = await ctx.call_tool(
                step.tool, step.arguments, stage=AgentStage.PARALLEL_INVESTIGATION
            )
            return step, result

    outcomes = await asyncio.gather(*(run(s) for s in steps), return_exceptions=True)

    new_evidence: list[EvidenceItem] = list(state.evidence)
    completed: list[dict[str, Any]] = []
    probe_failures: list[dict[str, Any]] = []
    plan = state.plan.model_dump()
    ruled_out = set(plan.get("ruled_out") or [])

    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            # A probe that raised used to be dropped on the floor. The round
            # then looked like "the tools answered and had nothing to say", the
            # evidence list silently stayed short, and the run starved itself
            # of the very facts that would have produced a hypothesis. Record
            # it, announce it, and keep the other probes' results.
            message = f"{type(outcome).__name__}: {outcome}"
            probe_failures.append({"stage": "parallel_investigation", "error": message})
            log_event(
                "agent.investigation.probe_failed",
                run_id=ctx.run_id,
                error=message,
            )
            continue
        step, result = outcome
        completed.append({"tool": step.tool, "ok": result.ok})
        if not result.ok:
            probe_failures.append(
                {
                    "stage": "parallel_investigation",
                    "tool": step.tool,
                    "error": result.error_message
                    or result.error_type
                    or "tool returned no result",
                }
            )
            continue
        value = _result_value(result)
        # Which component this probe was aimed at. Everything parsed below is
        # re-attributed to it, so a dependency probe lands on the dependency.
        svc = str(step.arguments.get("service") or state.incident.service)
        mark = len(new_evidence)
        if step.tool == "query_metrics":
            series = value.get("series", [])
            for entry in series:
                latest = entry.get("latest")
                previous = entry.get("previous")
                delta = (
                    round(latest - previous, 6)
                    if isinstance(latest, (int, float)) and isinstance(previous, (int, float))
                    else None
                )
                new_evidence.append(
                    _evidence_item(
                        state, new_evidence,
                        type="metric",
                        source="query_metrics",
                        title=f"{entry.get('metric')} = {latest}",
                        description=(
                            f"previous={previous} delta={delta}" if delta is not None else ""
                        ),
                        value=entry,
                        severity="high" if entry.get("metric") in {"error_rate", "latency_p95"} else "medium",
                        confidence=0.9,
                        tool_call_id=result.tool_call_id,
                    )
                )
        elif step.tool == "query_logs":
            entries = value.get("entries", [])
            if not entries:
                # No error logs at all is itself evidence — and it lets the
                # planner stop asking about this signal forever.
                ruled_out.add("error_signature")
                new_evidence.append(
                    _evidence_item(
                        state, new_evidence,
                        type="log",
                        source="query_logs",
                        title="No ERROR-level logs in the window",
                        description="Ruled out log-signature based causes.",
                        value={"count": 0},
                        severity="low",
                        confidence=0.7,
                        tool_call_id=result.tool_call_id,
                    )
                )
            for entry in _significant_log_entries(entries):
                new_evidence.append(
                    _evidence_item(
                        state, new_evidence,
                        type="log",
                        source="query_logs",
                        title=str(entry.get("message", ""))[:200],
                        description=f"level={entry.get('level')} at {entry.get('timestamp')}",
                        value=entry,
                        severity="high" if entry.get("level") == "ERROR" else "medium",
                        confidence=0.85,
                        tool_call_id=result.tool_call_id,
                    )
                )
        elif step.tool == "get_deployments":
            deployments = value.get("deployments", [])
            new_evidence.append(
                _evidence_item(
                    state, new_evidence,
                    type="deployment",
                    source="get_deployments",
                    title=f"{len(deployments)} recent deployment(s)",
                    description=", ".join(d.get("version", "") for d in deployments[:3]),
                    value={"deployments": deployments},
                    severity="high" if deployments else "low",
                    confidence=0.9,
                    tool_call_id=result.tool_call_id,
                )
            )
        elif step.tool == "get_recent_commits":
            commits = value.get("commits", [])
            new_evidence.append(
                _evidence_item(
                    state, new_evidence,
                    type="commit",
                    source="get_recent_commits",
                    title=f"{len(commits)} recent commit(s)",
                    description="; ".join(c.get("message", "")[:80] for c in commits[:3]),
                    value={"commits": commits},
                    severity="medium",
                    confidence=0.8,
                    tool_call_id=result.tool_call_id,
                )
            )
        elif step.tool == "search_runbooks":
            hits = value.get("hits", [])
            for hit in hits[:2]:
                new_evidence.append(
                    _evidence_item(
                        state, new_evidence,
                        type="runbook",
                        source="search_runbooks",
                        title=f"Runbook: {hit.get('title')}",
                        description=str(hit.get("excerpt", ""))[:300],
                        value=hit,
                        severity="low",
                        confidence=0.5,
                        tool_call_id=result.tool_call_id,
                    )
                )
        elif step.tool == "get_service_status":
            # The dependency-health probe. Without a branch here the result of
            # "is the thing we call healthy?" was fetched, paid for, and then
            # dropped on the floor — the single most load-bearing fact for a
            # cascading failure.
            health = str(value.get("health") or "unknown")
            new_evidence.append(
                _evidence_item(
                    state, new_evidence,
                    type="metric",
                    source="get_service_status",
                    service=str(step.arguments.get("service") or state.incident.service),
                    title=(
                        f"{step.arguments.get('service')} health is {health}"
                    ),
                    description=(
                        f"error_rate={value.get('error_rate')} "
                        f"latency_p95={value.get('latency_p95')} "
                        f"cpu={value.get('cpu_percent') or value.get('cpu')} "
                        f"db_connections={value.get('db_connections')}"
                    ),
                    value=value,
                    severity="high" if health != "healthy" else "low",
                    confidence=0.9,
                    tool_call_id=result.tool_call_id,
                )
            )
        else:
            # Any other read tool still produced a fact; dropping it because
            # nobody wrote a bespoke parser is how evidence goes missing.
            new_evidence.append(
                _evidence_item(
                    state, new_evidence,
                    type="observation",
                    source=step.tool,
                    service=str(step.arguments.get("service") or state.incident.service),
                    title=f"{step.tool} returned {len(value)} field(s)",
                    description=str(value)[:300],
                    value=value,
                    severity="low",
                    confidence=0.6,
                    tool_call_id=result.tool_call_id,
                )
            )
        for item in new_evidence[mark:]:
            item.service = svc

    plan["ruled_out"] = sorted(ruled_out)
    plan["steps"] = [
        {**s, "status": "done" if any(c["tool"] == s["tool"] for c in completed) else "failed"}
        for s in raw_steps
    ]

    await _persist_evidence(
        ctx,
        state,
        len(state.evidence),
        new_evidence,
        dependency_targets=dependency_targets(state),
        stage=AgentStage.PARALLEL_INVESTIGATION,
    )

    execution = state.execution.model_dump()
    execution["tool_call_refs"] = execution["tool_call_refs"] + [
        c["tool"] for c in completed
    ]
    if probe_failures:
        # On the plan (readable on the timeline) and in the event stream — but
        # deliberately NOT in ``execution.errors``: the runtime treats those as
        # run-fatal, and a lost probe is a degraded round, not a dead run.
        plan["probe_failures"] = probe_failures
        await ctx.emit(
            "agent.investigation.probe_failed",
            {"failures": probe_failures[-3:]},
            stage=AgentStage.PARALLEL_INVESTIGATION,
        )
    return {"evidence": new_evidence, "plan": plan, "execution": execution,
            "decision": "continue"}


# ---------------------------------------------------------------------------
# 5. Evidence aggregation — stop / replan / move on
# ---------------------------------------------------------------------------


@workflow_node(
    NodeSpec(
        stage=AgentStage.EVIDENCE_AGGREGATION,
        reads=("evidence", "plan"),
        writes=("plan", "decision"),
        description="Decide whether the evidence is sufficient to start explaining it.",
        max_attempts=1,
        timeout_s=15.0,
    )
)
async def evidence_aggregation(
    state: IncidentState, config: RunnableConfig
) -> dict[str, Any]:
    """Stop, or keep going — and be able to justify which.

    Four ways out, in priority order:

    * the budget is spent → escalate, because looping is no longer affordable;
    * nothing new came back → stop, another round would ask the same question;
    * no dimension is left that could change the ranking → stop (saturated);
    * the leading explanation is already confident → stop and commit.

    Only if none of those hold does it replan. "Three evidence rows" is not a
    stopping criterion and never was.
    """
    ctx = get_context(config)
    plan = state.plan.model_dump()
    iteration = int(plan.get("iteration", 0))
    max_iterations = int(plan.get("max_iterations", 4))

    assessment = assess(
        state,
        dependency_targets=dependency_targets(state),
        max_probes=1,
        excluded_domains=tuple(state.plan.rejected_domains or ()),
    )
    gained = len(state.evidence) - int(plan.get("last_round_evidence", 0))

    if ctx.budget.exhausted:
        decision, why = "escalate", (
            f"investigation budget exhausted: {ctx.budget.exhaustion_detail}"
        )
    elif gained <= 0:
        decision, why = "diagnose", "the last round produced no new evidence"
    elif not assessment.probes:
        decision, why = "diagnose", assessment.reason
    elif (
        assessment.top_domain == "cascading"
        and assessment.probes
        and iteration < max_iterations
    ):
        # "Something we call is broken" is a finding, not a diagnosis. Until
        # the dependency's own failure mode is characterised there is no
        # action to take — restarting a dependency that is failing because of
        # a bad release changes nothing. Keep drilling.
        decision, why = "replan", (
            "a dependency is implicated but its failure mode is not "
            "characterised yet"
        )
    elif assessment.confidence >= _COMMIT_CONFIDENCE and iteration >= 1:
        decision, why = "diagnose", (
            f"'{assessment.top_domain}' reached {assessment.confidence:.2f} — "
            "committed"
        )
    elif iteration >= max_iterations:
        decision, why = "diagnose", f"reached the {max_iterations}-round limit"
    else:
        decision, why = "replan", assessment.reason

    await ctx.emit(
        EventType.INVESTIGATION_PLAN_CREATED.value
        if decision == "replan"
        else EventType.AGENT_STEP_COMPLETED.value,
        {
            "iteration": iteration,
            "decision": decision,
            "reason": why,
            "evidence_gained": gained,
            "top_domain": assessment.top_domain,
            "confidence": round(assessment.confidence, 3),
            "budget": ctx.budget.as_dict(),
        },
        stage=AgentStage.EVIDENCE_AGGREGATION,
    )
    return {"plan": plan, "decision": decision}


# ---------------------------------------------------------------------------
# 6. Hypothesis generation
# ---------------------------------------------------------------------------


@workflow_node(
    NodeSpec(
        stage=AgentStage.HYPOTHESIS_GENERATION,
        reads=("evidence",),
        writes=("hypotheses",),
        description="Propose explanations, each backed by specific evidence refs.",
        max_attempts=1,
        timeout_s=90.0,
    )
)
async def hypothesis_generation(
    state: IncidentState, config: RunnableConfig
) -> dict[str, Any]:
    ctx = get_context(config)
    hypotheses = generate_hypotheses(
        state, dependency_targets=dependency_targets(state)
    )
    if not hypotheses:
        # Nothing is supported by the evidence. That is a valid outcome and it
        # has to reach the diagnosis node so the run records
        # INSUFFICIENT_EVIDENCE instead of vanishing on a "stop" edge.
        await ctx.emit(
            EventType.HYPOTHESIS_CREATED.value,
            {"count": 0, "reason": "no fault domain was supported by the evidence"},
            stage=AgentStage.HYPOTHESIS_GENERATION,
        )
        return {"hypotheses": [], "decision": "continue"}

    llm = get_llm()
    if llm.name != "deterministic":
        statements = "\n".join(f"- {h.statement}" for h in hypotheses)
        reply = await complete_charged(
            llm,
            ctx,
            purpose="hypothesis_reasoning",
            # The schema has to be stated, not implied. Asked merely to "refine
            # the reasoning", small models answer in prose: Tokens get charged,
            # ``parse_json_object`` returns ``None``, and the hypotheses keep
            # their template reasoning while every trace says a model ran. That
            # is the worst possible outcome — paying traffic that changes
            # nothing, and no evidence in the logs that it was dropped.
            system=(
                "You are a site reliability engineer. Rewrite the reasoning for "
                "each hypothesis below, keeping every piece of evidence it cites.\n"
                "Reply with a JSON array and nothing else — one object per "
                "hypothesis, in the same order, each with exactly one key "
                '"reasoning" whose value is a single sentence under 40 words. '
                "No markdown fences, no commentary."
            ),
            user=statements,
        )
        enriched = parse_json_object(reply.text)
        if isinstance(enriched, list):
            for index, item in enumerate(enriched):
                if index < len(hypotheses) and isinstance(item, dict):
                    hypotheses[index].reasoning = str(
                        item.get("reasoning", hypotheses[index].reasoning)
                    )
        elif reply.total_tokens:
            # Charged for a reply we could not use. Say so loudly: a model that
            # quietly fails is indistinguishable from one that never ran, and
            # the run reports itself as model-driven either way.
            log_event(
                "llm.unusable_reply",
                purpose="hypothesis_reasoning",
                model=reply.model,
                tokens=reply.total_tokens,
                parsed=type(enriched).__name__,
            )

    await ctx.persistence.save_hypotheses(
        state.incident.incident_id, ctx.run_id, [h.model_dump() for h in hypotheses]
    )
    for hyp in hypotheses:
        await ctx.emit(
            EventType.HYPOTHESIS_CREATED.value,
            {
                "ref": hyp.ref,
                "statement": hyp.statement,
                "confidence": hyp.confidence,
                "category": hyp.category,
                "domain": hyp.domain,
                "evidence_refs": hyp.evidence_refs,
            },
            stage=AgentStage.HYPOTHESIS_GENERATION,
        )
    return {"hypotheses": hypotheses, "decision": "continue"}


# ---------------------------------------------------------------------------
# 7. Hypothesis verification
# ---------------------------------------------------------------------------


@workflow_node(
    NodeSpec(
        stage=AgentStage.HYPOTHESIS_VERIFICATION,
        reads=("hypotheses", "evidence"),
        writes=("hypotheses", "evidence"),
        description="Run a targeted probe per hypothesis and move confidence accordingly.",
        max_attempts=2,
        timeout_s=60.0,
    )
)
async def hypothesis_verification(
    state: IncidentState, config: RunnableConfig
) -> dict[str, Any]:
    """Test each hypothesis with a probe that can falsify it.

    The interesting branch is the one where nothing is confirmed. The Agent
    then does not fall through to a diagnosis — it discards the explanation,
    records *why*, and goes back to the planner to generate a new one, until
    the hypothesis-round budget says otherwise.
    """
    ctx = get_context(config)
    deps = dependency_targets(state)
    updated: list[HypothesisItem] = []
    new_evidence: list[EvidenceItem] = list(state.evidence)

    for hyp in state.hypotheses:
        if hyp.status in (
            HypothesisStatus.REJECTED.value,
            HypothesisStatus.CONFIRMED.value,
        ):
            # Rejected: already falsified. Confirmed: already proven — probing
            # again only spends budget to re-learn what the run knows.
            updated.append(hyp)
            continue
        # Refs are allocated against everything already known *plus* what this
        # hypothesis has just produced. Forgetting the second term minted the
        # same ref three times and the DB upsert collapsed them into one row.
        running: list[EvidenceItem] = []
        query = verification_query(hyp)
        target = verification_target(hyp, state)
        if query is None or not target:
            # No probe exists that could falsify this, so it stays proposed
            # and is reported as probable at best — never as confirmed.
            updated.append(hyp)
            continue
        args = dict(query)
        tool = args.pop("tool")
        args["service"] = target
        result = await ctx.call_tool(
            tool, args, stage=AgentStage.HYPOTHESIS_VERIFICATION
        )
        fresh: list[EvidenceItem] = []
        if result.ok:
            value = _result_value(result)
            if tool == "query_metrics":
                for entry in value.get("series", []):
                    fresh.append(
                        _evidence_item(
                            state, [*new_evidence, *running],
                            type="metric",
                            source=f"{tool}:verify:{hyp.ref}",
                            service=target,
                            title=f"[{hyp.ref}] {entry.get('metric')} = {entry.get('latest')}",
                            value=entry,
                            severity="high",
                            confidence=0.9,
                            tool_call_id=result.tool_call_id,
                        )
                    )
            elif tool == "get_deployments":
                fresh.append(
                    _evidence_item(
                        state, [*new_evidence, *running],
                        type="deployment",
                        source=f"{tool}:verify:{hyp.ref}",
                        service=target,
                        title=f"[{hyp.ref}] deployment history",
                        value=value,
                        severity="high",
                        confidence=0.85,
                        tool_call_id=result.tool_call_id,
                    )
                )
            elif tool == "query_logs":
                for entry in _significant_log_entries(
                    value.get("entries", []), limit=5
                ):
                    fresh.append(
                        _evidence_item(
                            state, [*new_evidence, *running],
                            type="log",
                            source=f"{tool}:verify:{hyp.ref}",
                            service=target,
                            title=f"[{hyp.ref}] {str(entry.get('message', ''))[:160]}",
                            value=entry,
                            severity="high",
                            confidence=0.85,
                            tool_call_id=result.tool_call_id,
                        )
                    )
            elif tool == "get_service_status":
                fresh.append(
                    _evidence_item(
                        state, [*new_evidence, *running],
                        type="metric",
                        source=f"{tool}:verify:{hyp.ref}",
                        service=target,
                        title=f"[{hyp.ref}] {target} health is {value.get('health')}",
                        value=value,
                        severity="high",
                        confidence=0.9,
                        tool_call_id=result.tool_call_id,
                    )
                )
        running.extend(fresh)
        updated.append(
            apply_verification(
                hyp.model_copy(deep=True), fresh, dependency_targets=deps
            )
        )
        new_evidence.extend(fresh)

    await _persist_evidence(
        ctx,
        state,
        len(state.evidence),
        new_evidence,
        dependency_targets=deps,
        stage=AgentStage.HYPOTHESIS_VERIFICATION,
    )
    await ctx.persistence.save_hypotheses(
        state.incident.incident_id, ctx.run_id, [h.model_dump() for h in updated]
    )
    for hyp in updated:
        rejected = hyp.status == HypothesisStatus.REJECTED.value
        await ctx.emit(
            EventType.HYPOTHESIS_REJECTED.value
            if rejected
            else EventType.HYPOTHESIS_UPDATED.value,
            {
                "ref": hyp.ref,
                "statement": hyp.statement,
                "confidence": hyp.confidence,
                "status": hyp.status,
                "domain": hyp.domain,
                "evidence_refs": hyp.evidence_refs,
                "reasoning": hyp.reasoning,
            },
            stage=AgentStage.HYPOTHESIS_VERIFICATION,
        )

    confirmed = [h for h in updated if h.status == HypothesisStatus.CONFIRMED.value]
    viable = [h for h in updated if h.status != HypothesisStatus.REJECTED.value]
    if confirmed or not viable:
        return {"hypotheses": updated, "evidence": new_evidence, "decision": "diagnose"}

    # Nothing was confirmed. Either throw the explanation away and look again,
    # or accept that the run cannot do better and say so.
    if ctx.budget.can_start_hypothesis_round():
        ctx.budget.record_hypothesis_round()
        await ctx.persist_budget()
        plan = state.plan.model_dump()
        plan["hypothesis_rounds"] = int(plan.get("hypothesis_rounds", 0)) + 1
        plan["rejected_domains"] = sorted(
            set(plan.get("rejected_domains") or [])
            | {h.domain for h in updated if h.status == HypothesisStatus.REJECTED.value and h.domain}
        )
        await ctx.persistence.set_incident_status(
            state.incident.incident_id,
            "INVESTIGATING",
            stage=AgentStage.HYPOTHESIS_VERIFICATION,
            summary=(
                f"Hypothesis round {plan['hypothesis_rounds']}: rejected "
                + ", ".join(h.domain or h.category for h in updated
                            if h.status == HypothesisStatus.REJECTED.value)
                + " — generating a new one"
            ),
        )
        return {
            "hypotheses": updated,
            "evidence": new_evidence,
            "plan": plan,
            "decision": "replan",
        }

    return {"hypotheses": updated, "evidence": new_evidence, "decision": "diagnose"}


# ---------------------------------------------------------------------------
# 8. Root cause diagnosis
# ---------------------------------------------------------------------------


@workflow_node(
    NodeSpec(
        stage=AgentStage.ROOT_CAUSE_DIAGNOSIS,
        reads=("hypotheses", "evidence"),
        writes=("diagnosis",),
        description=(
            "Pick the winning hypothesis and state one of four outcomes. "
            "Never invents a cause to avoid admitting uncertainty."
        ),
        max_attempts=1,
        timeout_s=30.0,
    )
)
async def root_cause_diagnosis(
    state: IncidentState, config: RunnableConfig
) -> dict[str, Any]:
    ctx = get_context(config)
    verdict = decide_root_cause(state)
    statement = verdict.statement
    category = verdict.category
    confidence = verdict.confidence
    refs = verdict.evidence_refs
    summary = verdict.summary
    outcome = verdict.outcome

    # "I could not prove it" and "my tooling gave up" are different failures
    # with different follow-ups, so the distinction is made here rather than
    # reported as a single shrug.
    escalation_reason = ""
    if outcome in (
        DiagnosisOutcome.INSUFFICIENT_EVIDENCE.value,
        DiagnosisOutcome.INVESTIGATION_FAILED.value,
    ):
        if ctx.budget.exhausted:
            outcome = DiagnosisOutcome.INVESTIGATION_FAILED.value
            escalation_reason = (
                ctx.budget.exhausted_reason.value
                if ctx.budget.exhausted_reason
                else EscalationReason.BUDGET_EXHAUSTED.value
            )
            summary = (
                f"{summary} The investigation budget was spent: "
                f"{ctx.budget.exhaustion_detail}."
            )
        elif not state.evidence:
            outcome = DiagnosisOutcome.INVESTIGATION_FAILED.value
            escalation_reason = EscalationReason.TOOL_FAILURES.value
        else:
            escalation_reason = EscalationReason.NO_HYPOTHESIS_CONFIRMED.value

    if not statement:
        statement = {
            DiagnosisOutcome.INSUFFICIENT_EVIDENCE.value: (
                "Insufficient evidence to determine a root cause"
            ),
            DiagnosisOutcome.INVESTIGATION_FAILED.value: (
                "Investigation could not be completed"
            ),
        }.get(outcome, "No root cause determined")

    diagnosis = DiagnosisInfo(
        root_cause=statement,
        category=category,
        # The domain is what the recovery layer keys its plan off, so it is
        # carried out of the diagnosis rather than re-derived later from the
        # category — that re-derivation is what erased the difference between
        # "pool exhausted" and "queries blocked".
        domain=verdict.domain,
        confidence=confidence,
        evidence_refs=refs,
        reasoning_summary=summary,
        decided_at=_now(),
        outcome=outcome,
        escalation_reason=escalation_reason,
    )
    await ctx.persistence.save_diagnosis(
        state.incident.incident_id, ctx.run_id, diagnosis.model_dump()
    )

    actionable = outcome in (
        DiagnosisOutcome.ROOT_CAUSE_CONFIRMED.value,
        DiagnosisOutcome.ROOT_CAUSE_PROBABLE.value,
    )
    if actionable:
        await ctx.persistence.set_incident_status(
            state.incident.incident_id,
            "DIAGNOSING",
            stage=AgentStage.ROOT_CAUSE_DIAGNOSIS,
            summary=f"[{outcome}] {statement}",
        )
    else:
        await ctx.persistence.set_incident_status(
            state.incident.incident_id,
            "ESCALATED",
            stage=AgentStage.ROOT_CAUSE_DIAGNOSIS,
            summary=f"[{outcome}] {statement}",
        )
        await ctx.emit(
            EventType.RUN_ESCALATED.value,
            {
                "outcome": outcome,
                "reason": escalation_reason,
                "detail": ctx.budget.exhaustion_detail,
                "evidence_count": len(state.evidence),
                "hypotheses": [
                    {"ref": h.ref, "domain": h.domain, "status": h.status,
                     "confidence": h.confidence}
                    for h in state.hypotheses
                ],
            },
            stage=AgentStage.ROOT_CAUSE_DIAGNOSIS,
        )

    await ctx.emit(
        EventType.DIAGNOSIS_COMPLETED.value,
        {
            "root_cause": statement,
            "category": category,
            "domain": verdict.domain,
            "confidence": confidence,
            "outcome": outcome,
            "escalation_reason": escalation_reason,
            "evidence_refs": refs,
            "actionable": actionable,
        },
        stage=AgentStage.ROOT_CAUSE_DIAGNOSIS,
    )
    return {
        "diagnosis": diagnosis.model_dump(),
        "decision": "continue" if actionable else "escalate",
    }


# ---------------------------------------------------------------------------
# 9. Recovery planner
# ---------------------------------------------------------------------------


@workflow_node(
    NodeSpec(
        stage=AgentStage.RECOVERY_PLANNER,
        reads=("diagnosis",),
        writes=("recovery",),
        description="Turn the diagnosis into concrete, ordered recovery actions.",
        max_attempts=1,
        timeout_s=30.0,
    )
)
async def recovery_planner(
    state: IncidentState, config: RunnableConfig
) -> dict[str, Any]:
    ctx = get_context(config)
    diagnosis = state.diagnosis
    if diagnosis is None or not diagnosis.root_cause:
        return {"decision": "stop"}

    # Domain first, category second. They are not the same axis: "slow and
    # blocked database queries" and "connection pool exhaustion" share the
    # category ``database`` but need completely different remediation — the
    # first is cleared on the datastore, the second on the service. Collapsing
    # them would have the Agent roll back a release to fix a stuck migration.
    kind = diagnosis.domain or diagnosis.category
    dependency = recovery_target(state)
    # Capacity actions are relative, so the planner needs the fleet size the
    # investigation already measured for whichever component it will act on.
    actor = dependency or state.incident.service
    actions = build_recovery_actions(
        kind,
        state.incident.service,
        dependency=dependency,
        current_replicas=_current_replicas(state, actor),
    )
    if not actions:
        return {"decision": "stop"}

    summary = plan_summary(actions)

    recovery = state.recovery.model_dump()
    recovery["plan_ref"] = f"RP-{state.incident.incident_id[:8]}"
    recovery["actions"] = [RecoveryActionItem(**a).model_dump() for a in actions]
    recovery["status"] = RecoveryPlanStatus.DRAFT.value
    recovery["risk_level"] = summary["risk_level"]
    # The tier is decided by the *worst* action in the plan, and it is decided
    # here — by the recovery policy — not by the approval node, which only
    # honours it.
    recovery["approval_tier"] = summary["tier"]
    recovery["requires_approval"] = summary["requires_approval"]
    recovery["requires_reverification"] = summary["reverify"]
    recovery["manual_only"] = summary["manual_only"]
    recovery["rationale"] = (
        f"Diagnosed {diagnosis.root_cause} (confidence {diagnosis.confidence:.2f}, "
        f"outcome {diagnosis.outcome})."
    )
    recovery["expected_impact"] = "; ".join(a["expected_impact"] for a in actions)
    recovery["verification_criteria"] = verification_criteria(kind)

    saved = await ctx.persistence.save_recovery_plan(
        state.incident.incident_id, ctx.run_id, recovery
    )
    recovery["plan_ref"] = str(saved.get("id", recovery["plan_ref"]))
    await ctx.emit(
        EventType.RECOVERY_PLAN_CREATED.value,
        {
            "plan_id": recovery["plan_ref"],
            "approval_tier": recovery["approval_tier"],
            "risk_level": recovery["risk_level"],
            "actions": [
                {
                    "ref": a["ref"],
                    "tool": a["tool"],
                    "target": a["target_service"],
                    "risk_level": a["risk_level"],
                    "approval_tier": a["approval_tier"],
                    "rollback_tool": a.get("rollback_tool"),
                }
                for a in recovery["actions"]
            ],
            "verification_criteria": recovery["verification_criteria"],
        },
        stage=AgentStage.RECOVERY_PLANNER,
    )
    return {"recovery": recovery, "decision": "continue"}


# ---------------------------------------------------------------------------
# 10. Risk assessment
# ---------------------------------------------------------------------------


@workflow_node(
    NodeSpec(
        stage=AgentStage.RISK_ASSESSMENT,
        reads=("recovery",),
        writes=("recovery",),
        description="Derive the plan's approval tier from its worst action; nothing above LOW runs unattended.",
        max_attempts=1,
        timeout_s=15.0,
    )
)
async def risk_assessment(
    state: IncidentState, config: RunnableConfig
) -> dict[str, Any]:
    ctx = get_context(config)
    recovery = state.recovery.model_dump()
    actions = list(recovery.get("actions") or [])
    summary = plan_summary(actions)

    recovery["risk_level"] = summary["risk_level"]
    recovery["approval_tier"] = summary["tier"]
    recovery["requires_approval"] = summary["requires_approval"]
    recovery["requires_reverification"] = summary["reverify"]
    recovery["manual_only"] = summary["manual_only"]
    recovery["status"] = (
        RecoveryPlanStatus.PENDING_APPROVAL.value
        if summary["requires_approval"]
        else RecoveryPlanStatus.APPROVED.value
    )
    # Persist the assessed risk — otherwise the plan row stays at the
    # planner's LOW/draft default while the approval gate is deciding on a
    # CRITICAL change, and the audit trail disagrees with the workflow.
    await ctx.persistence.save_recovery_plan(
        state.incident.incident_id, ctx.run_id, recovery
    )
    await ctx.emit(
        EventType.RISK_ASSESSED.value,
        {
            "risk_level": summary["risk_level"],
            "approval_tier": summary["tier"],
            "requires_approval": summary["requires_approval"],
            "requires_reverification": summary["reverify"],
            "manual_only": summary["manual_only"],
            "by_action": [
                {
                    "ref": a.get("ref"),
                    "tool": a.get("tool"),
                    "risk_level": a.get("risk_level"),
                    "approval_tier": a.get("approval_tier"),
                    "required_permission": a.get("required_permission"),
                    "rollback_tool": a.get("rollback_tool"),
                }
                for a in actions
            ],
        },
        stage=AgentStage.RISK_ASSESSMENT,
    )
    return {"recovery": recovery, "decision": "continue"}


# ---------------------------------------------------------------------------
# 11. Human approval — the interrupt point
# ---------------------------------------------------------------------------


@workflow_node(
    NodeSpec(
        stage=AgentStage.HUMAN_APPROVAL,
        reads=("recovery",),
        writes=("recovery",),
        description="Pause the graph until a human approves; nothing auto-approves this.",
        max_attempts=1,
        timeout_s=120.0,
    )
)
async def human_approval(
    state: IncidentState, config: RunnableConfig
) -> dict[str, Any]:
    ctx = get_context(config)
    recovery = state.recovery.model_dump()

    if not recovery.get("requires_approval"):
        recovery["approval_status"] = "not_required"
        return {"recovery": recovery, "decision": "continue"}

    approval_id = recovery.get("approval_id")
    existing = await ctx.persistence.get_approval(approval_id) if approval_id else None
    if existing is None:
        # Resuming re-runs this node from the top and ``interrupt()`` discards
        # the state update that carried the id, so recover it from the database
        # rather than opening a second, orphaned approval request.
        existing = await ctx.persistence.find_pending_approval(ctx.run_id)
    if existing is None:
        # A human normally decides while the run is parked, so by the time the
        # resume replays this node the pending window is closed. The decided
        # row is the ground truth — adopt it (and its id, which the executor
        # must present to the high-risk tool) instead of raising a new request.
        existing = await ctx.persistence.find_latest_approval(ctx.run_id)
    if existing is not None:
        recovery["approval_id"] = str(existing.get("id"))
        recovery["approval_status"] = str(existing.get("status") or "pending")

    if existing is None:
        first_action = (recovery.get("actions") or [{}])[0]
        approval = await ctx.persistence.create_approval(
            incident_id=state.incident.incident_id,
            run_id=ctx.run_id,
            action_id=first_action.get("ref"),
            action_type=first_action.get("tool", "recovery"),
            risk_level=recovery.get("risk_level", RiskLevel.HIGH.value),
            reason=recovery.get("rationale", ""),
            requested_by="agent",
        )
        approval_id = str(approval.get("id"))
        recovery["approval_id"] = approval_id
        recovery["approval_status"] = "pending"
        recovery["status"] = RecoveryPlanStatus.PENDING_APPROVAL.value
        await ctx.persistence.update_run(
            ctx.run_id,
            status="waiting_approval",
            current_stage=AgentStage.HUMAN_APPROVAL,
            interrupt_payload={"approval_id": approval_id},
        )
        await ctx.persistence.set_incident_status(
            state.incident.incident_id,
            "WAITING_APPROVAL",
            stage=AgentStage.HUMAN_APPROVAL,
            summary=f"Risk {recovery.get('risk_level')} — approval required",
        )
        await ctx.emit(
            EventType.APPROVAL_REQUIRED.value,
            {
                "approval_id": approval_id,
                "risk_level": recovery.get("risk_level"),
                "approval_tier": recovery.get("approval_tier"),
                "requires_reverification": recovery.get("requires_reverification"),
                "manual_only": recovery.get("manual_only"),
                "actions": recovery.get("actions"),
            },
            stage=AgentStage.HUMAN_APPROVAL,
        )

    # Parks the run. LangGraph persists the checkpoint here; the process may
    # even restart — resume happens through Command(resume=...).
    decision_payload = interrupt(
        {
            "approval_id": approval_id,
            "risk_level": recovery.get("risk_level"),
            "message": "Human approval required before executing recovery",
        }
    )

    resume = decision_payload if isinstance(decision_payload, dict) else {}
    approved = str(resume.get("decision", "")).lower() == "approve"
    recovery["approval_status"] = "approved" if approved else "rejected"
    if not approved:
        recovery["status"] = RecoveryPlanStatus.REJECTED.value
        await ctx.persistence.save_recovery_plan(
            state.incident.incident_id, ctx.run_id, recovery
        )
        await ctx.persistence.set_incident_status(
            state.incident.incident_id,
            "FAILED",
            stage=AgentStage.HUMAN_APPROVAL,
            summary="Recovery rejected by human reviewer",
        )
        return {"recovery": recovery, "decision": "stop"}

    recovery["status"] = RecoveryPlanStatus.APPROVED.value
    await ctx.persistence.save_recovery_plan(
        state.incident.incident_id, ctx.run_id, recovery
    )
    await ctx.persistence.set_incident_status(
        state.incident.incident_id,
        "RECOVERING",
        stage=AgentStage.HUMAN_APPROVAL,
        summary="Recovery approved",
    )
    return {"recovery": recovery, "decision": "continue"}


# ---------------------------------------------------------------------------
# 12. Recovery executor
# ---------------------------------------------------------------------------


@workflow_node(
    NodeSpec(
        stage=AgentStage.RECOVERY_EXECUTOR,
        reads=("recovery",),
        writes=("recovery",),
        description="Run the plan in order, stopping at the first action that changes the environment.",
        max_attempts=1,
        timeout_s=90.0,
    )
)
async def recovery_executor(
    state: IncidentState, config: RunnableConfig
) -> dict[str, Any]:
    """Run the approved plan, in order, until something actually changes.

    The plan is a *ranked set of hypotheses about the fix*, not a script. The
    executor stops at the first action that leaves the environment different
    from how it found it: running the remaining candidates afterwards would
    mean applying a treatment whose rationale has just been invalidated, and
    would make the verification result impossible to attribute.

    An action that returns HTTP 200 and changes nothing is not a success. It is
    recorded as ``ineffective`` and the executor moves on — silently counting it
    as a win is how a system reports a recovery that never happened.
    """
    ctx = get_context(config)
    recovery = state.recovery.model_dump()
    approval_id = recovery.get("approval_id")
    actions: list[dict[str, Any]] = list(recovery.get("actions") or [])

    recovery["status"] = RecoveryPlanStatus.EXECUTING.value
    # Persist before running anything: the plan status is what the dashboard
    # shows while the actions execute, and leaving it at ``approved`` meant a
    # plan could be visibly done in the timeline while the database still
    # described it as merely signed off.
    await ctx.persistence.save_recovery_plan(
        state.incident.incident_id, ctx.run_id, recovery
    )
    await ctx.emit(
        EventType.RECOVERY_STARTED.value,
        {
            "plan_id": recovery.get("plan_ref"),
            "approval_tier": recovery.get("approval_tier"),
            "actions": [a.get("ref") for a in actions],
        },
        stage=AgentStage.RECOVERY_EXECUTOR,
    )

    executed: list[str] = []
    effective_ref: str | None = None
    for action in actions:
        spec = TOOL_REGISTRY.get(action.get("tool", ""))
        if spec is None:
            action["status"] = RecoveryActionStatus.FAILED.value
            action["error"] = f"unknown tool {action.get('tool')!r}"
            continue

        # Whether a human had to sign this off is a property of *this* action,
        # not of the plan: a MEDIUM step in a plan whose worst step is CRITICAL
        # is still covered by the approval that released the node, but a LOW
        # step is not gated at all.
        needs_approval = bool(spec.requires_approval)
        action["approval_status"] = "approved" if needs_approval else "not_required"

        result = await ctx.call_tool(
            action["tool"],
            action.get("parameters", {}),
            stage=AgentStage.RECOVERY_EXECUTOR,
            approval_id=approval_id if needs_approval else None,
        )
        executed.append(str(action.get("ref")))
        value = _result_value(result) if result.ok else {}

        if not result.ok:
            action["status"] = RecoveryActionStatus.FAILED.value
            action["error"] = result.error_message
            action["effective"] = False
        else:
            effective = bool(value.get("effective", True))
            action["effective"] = effective
            action["status"] = (
                RecoveryActionStatus.SUCCEEDED.value
                if effective
                else RecoveryActionStatus.INEFFECTIVE.value
            )
            if effective:
                action["result"] = value

        await ctx.persistence.save_recovery_action_result(
            str(action.get("id") or action.get("ref")),
            status=action["status"],
            result=value or None,
            error=action.get("error"),
            tool_call_id=result.tool_call_id,
            executed_by="agent",
        )
        await ctx.emit(
            EventType.RECOVERY_ACTION_COMPLETED.value,
            {
                "ref": action.get("ref"),
                "tool": action.get("tool"),
                "target": action.get("target_service"),
                "status": action["status"],
                "effective": action.get("effective"),
                "removed_faults": value.get("removed_faults"),
                "resisted_faults": value.get("resisted_faults"),
                "error": action.get("error"),
            },
            stage=AgentStage.RECOVERY_EXECUTOR,
        )

        if action["status"] == RecoveryActionStatus.SUCCEEDED.value:
            effective_ref = str(action.get("ref"))
            break

    recovery["executed_refs"] = executed
    recovery["effective_ref"] = effective_ref

    if effective_ref:
        recovery["status"] = RecoveryPlanStatus.EXECUTED.value
        # The plan ran and something in it actually changed the environment.
        # Recording that on the plan is what lets a reader tell a finished
        # recovery from one that is still queued.
        await ctx.persistence.save_recovery_plan(
            state.incident.incident_id, ctx.run_id, recovery
        )
        await ctx.emit(
            EventType.RECOVERY_COMPLETED.value,
            {"executed": executed, "effective_ref": effective_ref},
            stage=AgentStage.RECOVERY_EXECUTOR,
        )
        return {"recovery": recovery, "decision": "verify"}

    # Nothing in the plan changed the environment. There is nothing to
    # compensate for, so this goes straight to a human — rolling back an action
    # that had no effect is theatre.
    recovery["status"] = RecoveryPlanStatus.FAILED.value
    recovery["escalation_reason"] = EscalationReason.RECOVERY_FAILED.value
    await ctx.persistence.save_recovery_plan(
        state.incident.incident_id, ctx.run_id, recovery
    )
    await ctx.emit(
        EventType.RECOVERY_FAILED.value,
        {
            "executed": executed,
            "reason": "no action changed the observed state",
            "actions": [
                {"ref": a.get("ref"), "status": a.get("status"), "effective": a.get("effective")}
                for a in actions
            ],
        },
        stage=AgentStage.RECOVERY_EXECUTOR,
    )
    await _escalate(ctx, state, "No recovery action changed the observed state")
    return {"recovery": recovery, "decision": "escalate"}


# ---------------------------------------------------------------------------
# 12b. Rollback — compensate, then re-verify
# ---------------------------------------------------------------------------


@workflow_node(
    NodeSpec(
        stage=AgentStage.ROLLBACK,
        reads=("recovery",),
        writes=("recovery",),
        description="Undo the executed actions via their declared compensating tools.",
        max_attempts=1,
        timeout_s=90.0,
    )
)
async def rollback(
    state: IncidentState, config: RunnableConfig
) -> dict[str, Any]:
    """Apply the declared compensating action for whatever was executed.

    An action that cannot be reversed, and a plan whose worst action is
    ``manual_only``, are not compensated by the Agent at all. Undoing a change
    that was dangerous enough to need a human is at least as dangerous — and
    the Agent has just demonstrated that its model of the system was wrong.
    """
    ctx = get_context(config)
    recovery = state.recovery.model_dump()
    executed = set(recovery.get("executed_refs") or [])
    approval_id = recovery.get("approval_id")
    rollback_refs: list[str] = []
    failures: list[str] = []

    for action in recovery.get("actions") or []:
        if str(action.get("ref")) not in executed:
            continue
        if action.get("status") not in (
            RecoveryActionStatus.SUCCEEDED.value,
            RecoveryActionStatus.INEFFECTIVE.value,
        ):
            continue

        tool = action.get("rollback_tool")
        if not tool:
            # No compensating action is declared. Say so rather than
            # improvising one — an undeclared reversal is an unreviewed change.
            continue
        if not may_attempt_rollback(str(action.get("risk_level") or "")):
            failures.append(str(action.get("ref")))
            await ctx.emit(
                EventType.RECOVERY_ROLLBACK_COMPLETED.value,
                {
                    "ref": action.get("ref"),
                    "tool": tool,
                    "status": "refused",
                    "reason": "CRITICAL action is never compensated autonomously",
                },
                stage=AgentStage.ROLLBACK,
            )
            continue

        spec = TOOL_REGISTRY.get(tool)
        if spec is None:
            failures.append(str(action.get("ref")))
            continue

        result = await ctx.call_tool(
            tool,
            action.get("rollback_parameters") or {},
            stage=AgentStage.ROLLBACK,
            approval_id=approval_id if spec.requires_approval else None,
            # The executor's post-action probe asks "is the environment healthy
            # now?" — which is the wrong question here: the incident is still
            # open, and that is precisely why a rollback is being attempted.
            # Vetoing the compensation for failing to cure what it was never
            # meant to cure would report a working undo as a broken tool.
            # Whether the environment recovered is decided by the node after
            # this one, on purpose.
            skip_verification=True,
        )
        rollback_refs.append(str(action.get("ref")))
        await ctx.emit(
            EventType.RECOVERY_ROLLBACK_COMPLETED.value,
            {
                "ref": action.get("ref"),
                "tool": tool,
                "status": "succeeded" if result.ok else "failed",
                "error": result.error_message,
            },
            stage=AgentStage.ROLLBACK,
        )
        if not result.ok:
            failures.append(str(action.get("ref")))

    recovery["rollback_refs"] = rollback_refs
    recovery["status"] = RecoveryPlanStatus.ROLLED_BACK.value
    recovery["rollback_outcome"] = "failed" if failures else "completed"

    await ctx.persistence.save_recovery_plan(
        state.incident.incident_id, ctx.run_id, recovery
    )
    await ctx.persistence.set_incident_status(
        state.incident.incident_id,
        "ROLLING_BACK",
        stage=AgentStage.ROLLBACK,
        summary=f"Compensating {len(rollback_refs)} action(s)",
    )
    await ctx.emit(
        EventType.RECOVERY_ROLLBACK_STARTED.value,
        {"compensated": rollback_refs, "refused_or_failed": failures},
        stage=AgentStage.ROLLBACK,
    )

    # Re-probe: the point of a rollback is to get back to a known state, and
    # the only way to know that is to look.
    return {
        "recovery": recovery,
        "verification": state.verification.model_dump(),
        "decision": "reverify",
    }


# ---------------------------------------------------------------------------
# 13. Verification
# ---------------------------------------------------------------------------


@workflow_node(
    NodeSpec(
        stage=AgentStage.VERIFICATION,
        reads=("recovery",),
        writes=("verification",),
        description="Probe the service against the plan's criteria — no hardcoded 'passed'.",
        max_attempts=2,
        timeout_s=60.0,
    )
)
async def verification(
    state: IncidentState, config: RunnableConfig
) -> dict[str, Any]:
    """Re-measure every component the recovery touched.

    Verifying only the alerting service is how a "fixed" incident stays broken:
    the alert clears because the caller has stopped calling the thing that was
    down, while the dependency is still failing. So the probe set is the
    service that alerted *plus* every component the plan acted on.
    """
    ctx = get_context(config)
    criteria = state.recovery.verification_criteria or [
        "error_rate <= 1%",
        "latency_p95 <= 500ms",
        "health_status == healthy",
    ]

    checks: list[dict[str, Any]] = []
    for target in _verification_targets(state):
        result = await ctx.call_tool(
            "get_service_status",
            {"service": target},
            stage=AgentStage.VERIFICATION,
        )
        if not result.ok:
            checks.append(
                {
                    "name": f"{target}.probe",
                    "passed": False,
                    "actual": result.error_message,
                    "threshold": "tool success",
                }
            )
            continue
        checks.extend(_health_checks(target, _result_value(result)))

    passed = sum(1 for c in checks if c.get("passed"))
    ok = bool(checks) and passed == len(checks)
    payload = {
        "status": (
            VerificationStatus.PASSED.value if ok else VerificationStatus.FAILED.value
        ),
        "checks": checks,
        "passed_checks": passed,
        "total_checks": len(checks),
        "verified_at": _now(),
        "criteria": criteria,
    }
    await ctx.persistence.save_verification(
        state.incident.incident_id,
        ctx.run_id,
        state.recovery.plan_ref,
        payload,
    )
    await ctx.persistence.set_incident_status(
        state.incident.incident_id,
        "VERIFYING",
        stage=AgentStage.VERIFICATION,
        summary=f"{passed}/{len(checks)} checks passed",
    )
    await ctx.emit(
        EventType.VERIFICATION_COMPLETED.value,
        payload,
        stage=AgentStage.VERIFICATION,
    )

    if ok:
        return {"verification": payload, "decision": "close"}

    recovery = state.recovery.model_dump()

    # A verification that fails twice — once before the compensation and once
    # after — is the end of the Agent's authority. It does not get to keep
    # improvising.
    if recovery.get("rollback_outcome"):
        recovery["escalation_reason"] = EscalationReason.VERIFICATION_FAILED.value
        await ctx.persistence.save_recovery_plan(
            state.incident.incident_id, ctx.run_id, recovery
        )
        await _escalate(
            ctx,
            state,
            "Verification failed again after rollback",
            stage=AgentStage.VERIFICATION,
            detail={"checks": checks, "rollback_outcome": recovery.get("rollback_outcome")},
        )
        return {
            "recovery": recovery,
            "verification": payload,
            "decision": "escalate",
        }

    if _rollbackable(recovery):
        await ctx.emit(
            EventType.RECOVERY_FAILED.value,
            {
                "reason": "verification failed after the recovery was effective",
                "checks": checks,
                "rollback_targets": [
                    a.get("ref")
                    for a in recovery.get("actions") or []
                    if str(a.get("ref")) in set(recovery.get("executed_refs") or [])
                    and a.get("rollback_tool")
                ],
            },
            stage=AgentStage.VERIFICATION,
        )
        return {"recovery": recovery, "verification": payload, "decision": "rollback"}

    recovery["escalation_reason"] = EscalationReason.VERIFICATION_FAILED.value
    await ctx.persistence.save_recovery_plan(
        state.incident.incident_id, ctx.run_id, recovery
    )
    await _escalate(
        ctx,
        state,
        "Recovery failed and no action could be compensated autonomously",
        stage=AgentStage.VERIFICATION,
        detail={"checks": checks},
    )
    return {"recovery": recovery, "verification": payload, "decision": "escalate"}


# ---------------------------------------------------------------------------
# 14. Postmortem
# ---------------------------------------------------------------------------


@workflow_node(
    NodeSpec(
        stage=AgentStage.POSTMORTEM,
        reads=("diagnosis", "evidence", "recovery", "verification"),
        writes=("postmortem_ref",),
        description="Write the postmortem from the stored timeline and close the incident.",
        max_attempts=1,
        timeout_s=90.0,
    )
)
async def postmortem(
    state: IncidentState, config: RunnableConfig
) -> dict[str, Any]:
    ctx = get_context(config)
    diagnosis = state.diagnosis
    timeline = [
        {
            "at": item.observed_at,
            "kind": "evidence",
            "ref": item.ref,
            "title": item.title,
        }
        for item in state.evidence
    ]
    payload = {
        "summary": (
            f"{state.incident.title or state.incident.service}: "
            f"{diagnosis.root_cause if diagnosis else 'unresolved'}"
        ),
        "root_cause": diagnosis.root_cause if diagnosis else None,
        "timeline": timeline,
        "contributing_factors": [
            h.statement
            for h in state.hypotheses
            if h.status != HypothesisStatus.REJECTED.value
        ],
        "lessons_learned": [
            "Detection relied on metric thresholds rather than log alerts.",
            "Recovery required manual approval — verify the on-call rotation.",
        ],
        "action_items": [
            f"Add an alert for {state.incident.service} error_rate > 1%",
            "Extend the runbook with the verified recovery procedure",
        ],
        "generated_by": "agent",
    }

    llm = get_llm()
    if llm.name != "deterministic" and diagnosis is not None:
        narrative = await complete_charged(
            llm,
            ctx,
            purpose="postmortem_narrative",
            system="You are an SRE writing a blameless postmortem. Be specific and short.",
            user=(
                f"Root cause: {diagnosis.root_cause}\n"
                f"Evidence: {[e.title for e in state.evidence][:10]}\n"
                f"Recovery: {[a.tool for a in state.recovery.actions]}\n"
                f"Verification: {state.verification.status}"
            ),
        )
        if narrative.text:
            payload["summary"] = narrative.text[:2000]

    ref = await ctx.persistence.save_postmortem(state.incident.incident_id, payload)
    await ctx.persistence.set_incident_status(
        state.incident.incident_id,
        "RESOLVED",
        stage=AgentStage.POSTMORTEM,
        summary="Incident resolved and postmortem written",
    )
    await ctx.emit(
        EventType.POSTMORTEM_CREATED.value,
        {"postmortem_id": ref, "summary": payload["summary"]},
        stage=AgentStage.POSTMORTEM,
    )
    return {"postmortem_ref": ref, "decision": "close"}


__all__ = [
    "load_context",
    "triage",
    "investigation_planner",
    "parallel_investigation",
    "evidence_aggregation",
    "hypothesis_generation",
    "hypothesis_verification",
    "root_cause_diagnosis",
    "recovery_planner",
    "risk_assessment",
    "human_approval",
    "recovery_executor",
    "rollback",
    "verification",
    "postmortem",
]
