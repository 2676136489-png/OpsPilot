"""Evaluation runner — drive the real workflow and score what it did.

This is not a simulation of the Agent. Every case runs the production path:

    create incident → AgentRuntimeService.start() → the real 15-node
    LangGraph workflow → the real ToolExecutor → the real simulator over a
    real HTTP round-trip → evidence / hypotheses / diagnosis persisted by the
    real repositories → the real human-approval interrupt → resume from the
    database checkpoint → executed recovery → verification probe → postmortem

and every number in the report is read back out of the database rows that run
produced — ``AgentStep``, ``ToolCall``, ``Evidence``, ``Hypothesis``,
``RecoveryAction``, ``VerificationResult``, ``TraceSpan``. Nothing is
hand-counted and nothing is inferred from the Agent's own summary of itself,
which is the only way a "94% root-cause accuracy" number means anything.

The two things the harness *does* stand in for are the human at the approval
gate (it approves) and the clock. It never supplies an answer: the expected
cause lives in the simulator's catalog, reachable only through an endpoint
mounted under ``OPSPILOT_SIM_EVAL_MODE`` that no tool in the registry calls.

Usage::

    python -m opspilot_evals.runners.eval_runner            # every scenario
    python -m opspilot_evals.runners.eval_runner --json     # machine-readable
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opspilot_evals.datasets.ground_truth import (
    EvalCase,
    category_matches,
    load_eval_cases,
    tool_requirement_satisfied,
)

#: Who the harness signs the approval as. Kept obviously non-human so a report
#: can never be mistaken for evidence that a real reviewer was in the loop.
HARNESS_REVIEWER = "eval-harness@opspilot.local"

_MONOREPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def bootstrap_paths() -> None:
    """Make the backend and the simulator importable from source.

    The evaluator drives two sibling projects; requiring an install step to
    score the build would mean the evaluation runs against whatever was
    installed last, not against the working tree.
    """
    for relative in ("apps/backend/src", "simulator/src", "evals"):
        path = str(_MONOREPO_ROOT / relative)
        if path not in sys.path:
            sys.path.insert(0, path)


def prepare_environment(db_path: str | Path, *, eval_mode: bool = True) -> dict[str, str]:
    """Install everything the run needs *before* the first backend import.

    Two settings are read at import time, not at call time, so they cannot be
    fixed up afterwards:

    * ``OPSPILOT_DATABASE_URL`` is consumed when ``db.session`` builds its
      engine,
    * ``OPSPILOT_SIMULATOR_URL`` is irrelevant here — the harness talks to the
      simulator in-process over ASGI — but it is pinned anyway so a stray
      ``.env`` cannot make half the suite talk to a stale server on a port.

    ``OPSPILOT_SIM_EVAL_MODE`` only has to be set before the first ground-truth
    *request* (the simulator re-reads it per request), but it is set here anyway
    so the whole run, including imports, sees a consistent environment.

    Returns the applied settings so the report can record how it was run.
    """
    bootstrap_paths()
    applied = {
        "OPSPILOT_SIM_EVAL_MODE": "1" if eval_mode else "0",
        "OPSPILOT_DATABASE_URL": f"sqlite+aiosqlite:///{Path(db_path).as_posix()}",
        "OPSPILOT_SIMULATOR_URL": "http://simulator.invalid",
        "OPSPILOT_DB_TRACE_SPANS": os.environ.get("OPSPILOT_DB_TRACE_SPANS", "1"),
        "OPSPILOT_DB_SPAN_MIN_MS": os.environ.get("OPSPILOT_DB_SPAN_MIN_MS", "5"),
    }
    for key, value in applied.items():
        os.environ[key] = value
    return applied


def reportable_environment(applied: dict[str, str]) -> dict[str, str]:
    """The applied settings as the report should show them.

    The report is committed and published, so it records *what* was configured
    rather than where this particular machine keeps its temp files. An absolute
    host path in a published artefact is noise at best, and tells the reader
    something about the machine that produced it that they did not ask for.
    """
    out = dict(applied)
    url = out.get("OPSPILOT_DATABASE_URL")
    if url:
        # Keep the URL scheme, drop the whole host path — including the temp
        # directory the harness created.
        scheme, sep, _path = url.partition("://")
        if sep:
            out["OPSPILOT_DATABASE_URL"] = f"{scheme}:///<eval-db>"
    return out


# ---------------------------------------------------------------------------
# Result records — plain data, so the report is serialisable and diffable
# ---------------------------------------------------------------------------


@dataclass
class StepRecord:
    stage: str
    status: str
    attempt: int
    sequence: int
    duration_ms: int
    error: str | None = None


@dataclass
class ToolRecord:
    name: str
    ok: bool
    status: str
    duration_ms: int
    risk_level: str | None = None
    permission_level: str | None = None
    error: str | None = None
    attempt: int = 1


@dataclass
class EvidenceRecord:
    ref: str
    type: str
    source: str
    service: str
    title: str
    relevance: str
    confidence: float
    has_tool_call: bool


@dataclass
class HypothesisRecord:
    ref: str
    domain: str
    category: str
    confidence: float
    status: str
    evidence_refs: list[str] = field(default_factory=list)
    reasoning: str = ""


@dataclass
class ActionRecord:
    ref: str
    tool: str
    target: str
    risk: str
    tier: str
    status: str
    effective: bool | None
    rollback_tool: str | None
    executed_by: str | None
    error: str | None = None


@dataclass
class CaseResult:
    """Everything one scenario run produced, scored where it can be."""

    scenario: str
    title: str
    service: str
    severity: str
    difficulty: str
    expected_category: str
    expected_root_cause: str
    expected_recovery: list[str]
    required_tools: list[str]

    run_id: str = ""
    incident_id: str = ""
    trace_id: str = ""
    run_status: str = ""
    incident_status: str = ""
    reasoning_mode: str = ""
    error: str | None = None

    # --- diagnosis ---
    diagnosis_outcome: str | None = None
    diagnosed_category: str | None = None
    diagnosed_root_cause: str | None = None
    diagnosis_confidence: float = 0.0
    diagnosis_evidence_refs: list[str] = field(default_factory=list)
    hypotheses: list[HypothesisRecord] = field(default_factory=list)
    rejected_hypotheses: int = 0

    # --- evidence ---
    evidence: list[EvidenceRecord] = field(default_factory=list)
    evidence_recall: float = 0.0
    #: Share of collected evidence the final diagnosis cites. Reported next to
    #: recall, not folded into it: collecting broadly and reasoning from a
    #: subset is normal, and averaging the two would make a correct answer look
    #: half-wrong.
    evidence_utilisation: float = 0.0
    evidence_linked_share: float = 0.0
    missing_evidence: list[str] = field(default_factory=list)

    # --- tools ---
    tools_called: list[str] = field(default_factory=list)
    missing_tools: list[str] = field(default_factory=list)
    tool_selection_ok: bool = False
    tool_calls: list[ToolRecord] = field(default_factory=list)
    tool_call_count: int = 0
    failed_tool_calls: int = 0

    # --- workflow shape ---
    steps: list[StepRecord] = field(default_factory=list)
    #: The nodes in the order they actually ran, repeats included. A fixed
    #: pipeline produces a strictly-increasing sequence with no duplicates;
    #: a run that re-planned after an inconclusive round shows it here.
    stage_sequence: list[str] = field(default_factory=list)
    node_count: int = 0
    replan_rounds: int = 0
    retried_nodes: int = 0
    node_errors: list[str] = field(default_factory=list)
    graph_errors: list[dict[str, Any]] = field(default_factory=list)

    # --- recovery ---
    approval_required: bool = False
    approval_risk: str | None = None
    actions: list[ActionRecord] = field(default_factory=list)
    recovery_attempted: bool = False
    recovery_effective: bool = False
    rollback_used: bool = False
    escalation_reason: str | None = None

    # --- verification, two-sided ---
    agent_verdict: str | None = None
    env_evaluated: bool = False
    env_passed: bool | None = None
    env_checks: list[dict[str, Any]] = field(default_factory=list)

    # --- cost ---
    budget: dict[str, Any] = field(default_factory=dict)
    tokens: int = 0
    latency_ms: float = 0.0
    workflow_ms: float = 0.0
    span_count: int = 0
    span_kinds: dict[str, int] = field(default_factory=dict)

    # --- trace integrity ---
    trace_single_root: bool = False
    trace_dangling_parents: int = 0
    trace_max_depth: int = 0

    # ------------------------------------------------------------------
    # Derived verdicts
    #
    # Properties rather than fields on purpose: the runner and the report
    # would otherwise each hold their own copy of "was this run right", and
    # the two would drift the first time one of them was edited. There is one
    # definition of each, and both read it.
    # ------------------------------------------------------------------
    @property
    def root_cause_correct(self) -> bool:
        """Did the Agent land on the fault the scenario actually has?"""
        return category_matches(self.expected_category, self.diagnosed_category)

    @property
    def abstained(self) -> bool:
        return self.diagnosis_outcome in {
            "INSUFFICIENT_EVIDENCE",
            "INVESTIGATION_FAILED",
        }

    @property
    def false_diagnosis(self) -> bool:
        """A confident answer that was wrong.

        An abstention is not a wrong answer and is deliberately excluded —
        conflating the two is how a system learns that guessing beats
        admitting uncertainty.
        """
        answered = self.diagnosis_outcome in {
            "ROOT_CAUSE_CONFIRMED",
            "ROOT_CAUSE_PROBABLE",
        }
        return bool(answered and not self.root_cause_correct)

    @property
    def escalated(self) -> bool:
        return bool(
            self.escalation_reason
            or self.incident_status == "ESCALATED"
            or self.abstained
        )

    @property
    def recovery_success(self) -> bool:
        """The environment is healthy *and* an action this run took held."""
        return bool(self.env_passed and self.recovery_effective)

    @property
    def verification_scored(self) -> bool:
        """Whether there was a verdict to compare at all.

        A run that escalated before recovery produced no verification, and
        scoring it as a disagreement would make quitting early look like the
        safest strategy.
        """
        return self.agent_verdict is not None and self.env_passed is not None

    @property
    def verification_agrees(self) -> bool | None:
        """Does the Agent's "fixed" mean what the environment's does?"""
        if not self.verification_scored:
            return None
        return (self.agent_verdict == "passed") == bool(self.env_passed)

    @property
    def reported_success_env_broken(self) -> bool:
        """The Agent's own verification passed while the environment disagrees.

        The failure mode the two-sided check exists to catch: a recovery that
        declares victory over a fault that is still there.
        """
        return bool(self.agent_verdict == "passed" and self.env_passed is False)

    _DERIVED = (
        "root_cause_correct",
        "false_diagnosis",
        "escalated",
        "recovery_success",
        "verification_scored",
        "verification_agrees",
        "reported_success_env_broken",
    )

    def as_dict(self) -> dict[str, Any]:
        payload = _to_plain(self)
        for name in self._DERIVED:
            payload[name] = getattr(self, name)
        return payload


def _to_plain(value: Any) -> Any:
    if isinstance(value, list):
        return [_to_plain(v) for v in value]
    if hasattr(value, "__dataclass_fields__"):
        return {k: _to_plain(v) for k, v in value.__dict__.items()}
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# Scoring helpers — pure, so they can be tested without a simulator
# ---------------------------------------------------------------------------


def normalise(text: str) -> str:
    return " ".join(str(text).lower().split())


def score_evidence(case: EvalCase, corpus: str, cited: int, total: int) -> tuple[float, float, list[str]]:
    """Recall against the scenario's expected keywords, utilisation of citations.

    Recall answers "did the Agent actually look at the right things"; the
    second number answers "of everything it looked at, how much fed the answer
    it gave". Both come from rows: recall from the evidence text the tools
    returned, utilisation from the citations the diagnosis recorded.
    """
    haystack = normalise(corpus)
    missing = [kw for kw in case.expected_evidence if normalise(kw) not in haystack]
    hits = len(case.expected_evidence) - len(missing)
    recall = hits / len(case.expected_evidence) if case.expected_evidence else 1.0
    utilisation = min(1.0, cited / total) if total else 0.0
    return round(recall, 4), round(utilisation, 4), missing


def score_tool_selection(case: EvalCase, called: set[str]) -> tuple[bool, list[str]]:
    """Did the Agent consult every tool family the fault requires?

    Scored on *capability coverage*, not on a literal tool list: the question is
    whether the Agent asked the environment the questions whose answers hold
    the fault, and there is more than one way to ask most of them.
    """
    missing = [t for t in sorted(case.required_tools) if not tool_requirement_satisfied(t, called)]
    return (not missing), missing


def trace_integrity(spans: list[dict[str, Any]]) -> tuple[bool, int, int]:
    """Root count, dangling parents and depth of a span set.

    A trace with two roots or a parent nothing ever emitted is a broken trace,
    and a broken trace is how an operator ends up unable to answer "which layer
    failed" — so the evaluator checks it rather than assuming it.
    """
    ids = {s["span_id"] for s in spans}
    roots = [s for s in spans if not s["parent_span_id"]]
    dangling = [s for s in spans if s["parent_span_id"] and s["parent_span_id"] not in ids]
    depth = {s["span_id"]: 1 for s in roots}
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for s in spans:
        by_parent.setdefault(s["parent_span_id"], []).append(s)
    frontier = [s["span_id"] for s in roots]
    while frontier:
        nxt: list[str] = []
        for parent in frontier:
            for child in by_parent.get(parent, []):
                depth[child["span_id"]] = depth[parent] + 1
                nxt.append(child["span_id"])
        frontier = nxt
    return (len(roots) == 1), len(dangling), (max(depth.values()) if depth else 0)


# ---------------------------------------------------------------------------
# The run itself
# ---------------------------------------------------------------------------


async def _build_incident(session: Any, case: EvalCase) -> Any:
    from sqlalchemy import select

    from opspilot_backend.models import Service
    from opspilot_backend.repositories.incident import IncidentRepository

    # Service names are unique and several scenarios alert on the same one
    # (payment-service carries four of them), so the catalogue row is reused
    # rather than re-inserted. The incident — which is what gets scored — is
    # always new.
    service = (
        await session.execute(select(Service).where(Service.name == case.service))
    ).scalars().first()
    if service is None:
        service = Service(
            name=case.service,
            description=f"{case.service} (simulated)",
            tier="application",
            owner="opspilot-eval",
        )
        session.add(service)
        await session.flush()
    incident = await IncidentRepository(session).create(
        title=f"[eval] {case.title}",
        service_id=service.id,
        severity=case.severity,
        # The operator-visible description only. Putting the expected category
        # here would hand the Agent the answer through its own context load.
        description=case.description or case.title,
        scenario=case.scenario,
    )
    await session.commit()
    return incident


async def _drive_to_completion(
    case: EvalCase,
    *,
    factory: Any,
) -> dict[str, Any]:
    """Start the run, clear the human gate, and report how it went."""
    from opspilot_backend.agent.checkpointer import DatabaseCheckpointer
    from opspilot_backend.domain.enums import AgentRunStatus
    from opspilot_backend.repositories.agent_run import AgentRunRepository
    from opspilot_backend.services.agent_runtime import AgentRuntimeService

    def runtime_for(session: Any) -> Any:
        # The checkpointer must be handed the *same* database as the run: its
        # default session factory is bound to the application's configured URL,
        # which is not the scratch database the suite migrates.
        return AgentRuntimeService(
            session,
            session_factory=factory,
            checkpointer=DatabaseCheckpointer(factory),
        )

    outcome: dict[str, Any] = {
        "run_id": "",
        "parked": False,
        "approved": False,
        "resumed": False,
        "approval": None,
        "error": None,
    }

    async with factory() as session:
        incident = await _build_incident(session, case)
        outcome["incident_id"] = str(incident.id)
        # ``background=False`` awaits the graph in this process, so the harness
        # observes the same lifecycle a test does — no polling and no sleeping
        # hoping the task finished.
        run = await runtime_for(session).start(incident.id, background=False)
        outcome["run_id"] = str(run.id)

    async with factory() as session:
        repo = AgentRunRepository(session)
        run = await repo.get(outcome["run_id"])
        if run is None:  # pragma: no cover - the row was just written
            outcome["error"] = "run row vanished"
            return outcome
        if run.status != AgentRunStatus.WAITING_APPROVAL:
            # Either the plan cleared the gate on its own (a low-risk action) or
            # the run escalated before planning. Both are legitimate outcomes.
            return outcome

        outcome["parked"] = True
        approval = await repo.find_pending_approval(outcome["run_id"])
        if approval is None:
            outcome["error"] = "run is waiting_approval but no pending approval exists"
            return outcome
        outcome["approval"] = {
            "id": approval["id"],
            "risk": approval.get("risk_level"),
            "action_type": approval.get("action_type"),
            "reason": approval.get("requested_reason"),
        }
        await repo.decide_approval(
            approval["id"],
            decision="approve",
            decided_by=HARNESS_REVIEWER,
            note="Approved by the evaluation harness.",
        )
        await session.commit()
        outcome["approved"] = True

        await runtime_for(session).resume(
            outcome["run_id"],
            {"decision": "approve", "approved_by": HARNESS_REVIEWER},
            background=False,
        )
        outcome["resumed"] = True
    return outcome


async def _collect(
    case: EvalCase,
    outcome: dict[str, Any],
    *,
    factory: Any,
    infra: Any,
    latency_ms: float,
) -> CaseResult:
    """Read the run back out of the database and score it."""
    from sqlalchemy import select

    from opspilot_backend.models import (
        Evidence,
        Hypothesis,
        HypothesisEvidenceLink,
        Incident,
        RecoveryAction,
        RecoveryPlan,
        VerificationResult,
    )
    from opspilot_backend.repositories.agent_run import AgentRunRepository

    result = CaseResult(
        scenario=case.scenario,
        title=case.title,
        service=case.service,
        severity=case.severity,
        difficulty=case.difficulty,
        expected_category=case.expected_category,
        expected_root_cause=case.expected_root_cause,
        expected_recovery=list(case.correct_recovery),
        required_tools=sorted(case.required_tools),
        run_id=outcome["run_id"],
        incident_id=outcome["incident_id"],
        error=outcome.get("error"),
        latency_ms=round(latency_ms, 1),
    )
    if outcome.get("approval"):
        result.approval_required = True
        result.approval_risk = outcome["approval"].get("risk")

    async with factory() as session:
        repo = AgentRunRepository(session)
        run = await repo.get(result.run_id)
        if run is None:  # pragma: no cover
            result.error = result.error or "run row missing at collect time"
            return result

        result.trace_id = run.trace_id or ""
        result.run_status = str(run.status)
        result.reasoning_mode = run.reasoning_mode
        result.tokens = int(run.spent_tokens or 0)
        result.escalation_reason = (
            run.escalation_reason.value if run.escalation_reason else None
        )
        result.budget = {
            "limits": {
                "tool_calls": run.budget_tool_calls,
                "seconds": run.budget_seconds,
                "tokens": run.budget_tokens,
                "retries": run.budget_max_retries,
                "parallel_tools": run.budget_max_parallel_tools,
            },
            "spent": {
                "tool_calls": run.spent_tool_calls,
                "tokens": run.spent_tokens,
                "retries": run.spent_retries,
                "seconds": run.spent_seconds,
            },
            "exhausted": bool(run.budget_exhausted),
        }
        if run.started_at and run.ended_at:
            result.workflow_ms = round(
                (run.ended_at - run.started_at).total_seconds() * 1000, 1
            )

        incident = await session.get(Incident, run.incident_id)
        if incident is not None:
            result.incident_status = incident.status or ""
            result.diagnosed_root_cause = incident.root_cause
            result.diagnosed_category = incident.root_cause_category

        # --- workflow shape -------------------------------------------------
        steps = await repo.steps(run.id)
        for s in steps:
            result.steps.append(
                StepRecord(
                    stage=str(s.stage),
                    status=str(s.status),
                    attempt=s.attempt,
                    sequence=s.sequence,
                    duration_ms=s.duration_ms or 0,
                    error=s.error,
                )
            )
            result.stage_sequence.append(str(s.stage))
            if s.attempt > 1:
                result.retried_nodes += 1
            if s.error:
                result.node_errors.append(f"{s.stage}: {s.error}")
        result.node_count = len({s.stage for s in steps})
        # Every re-execution of a node is one turn of the investigate → assess
        # → re-plan loop. If this is zero for every scenario, the workflow is a
        # pipeline wearing a graph's clothes, and saying so is the point of
        # measuring it rather than asserting it.
        counts: dict[str, int] = defaultdict(int)
        for stage in result.stage_sequence:
            counts[stage] += 1
        result.replan_rounds = sum(n - 1 for n in counts.values() if n > 1)

        events = await repo.events(run.id, limit=1000)
        # The events are the only persisted carrier of the fault *domain* (the
        # Hypothesis table stores the category) and of the diagnosis's own
        # evidence citations, so they are read before the rows they annotate.
        domains: dict[str, str] = {}
        diagnosis_refs: list[str] = []
        for e in events:
            data = e.data or {}
            if e.event_type == "hypothesis.created" and data.get("ref"):
                domains[str(data["ref"])] = str(data.get("domain") or "")
            elif e.event_type == "diagnosis.completed":
                if not result.diagnosis_outcome:
                    result.diagnosis_outcome = data.get("outcome")
                    result.diagnosis_confidence = float(data.get("confidence") or 0.0)
                diagnosis_refs = [str(r) for r in (data.get("evidence_refs") or [])]
                result.escalation_reason = (
                    result.escalation_reason or data.get("escalation_reason")
                )
            elif e.event_type == "agent.escalated":
                result.escalation_reason = result.escalation_reason or data.get("reason")
                result.graph_errors.append({"type": e.event_type, "data": data})
            elif e.event_type == "agent.failed":
                result.graph_errors.append({"type": e.event_type, "data": data})
            elif e.event_type.startswith("recovery.rollback"):
                result.rollback_used = True
        result.diagnosis_evidence_refs = diagnosis_refs

        # --- tools ----------------------------------------------------------
        calls = await repo.tool_calls(run.id)
        for c in calls:
            ok = str(c.status) == "succeeded"
            result.tool_calls.append(
                ToolRecord(
                    name=c.tool_name,
                    ok=ok,
                    status=str(c.status),
                    duration_ms=c.duration_ms or 0,
                    risk_level=c.risk_level,
                    permission_level=c.permission_level,
                    error=c.error_message,
                    attempt=c.attempt,
                )
            )
            if not ok:
                result.failed_tool_calls += 1
        result.tool_call_count = len(calls)
        result.tools_called = sorted({c.tool_name for c in calls})
        result.tool_selection_ok, result.missing_tools = score_tool_selection(
            case, set(result.tools_called)
        )

        # --- evidence -------------------------------------------------------
        rows = (
            await session.execute(
                select(Evidence).where(Evidence.incident_id == run.incident_id)
            )
        ).scalars().all()
        corpus_parts: list[str] = []
        for e in rows:
            result.evidence.append(
                EvidenceRecord(
                    ref=e.ref,
                    type=str(e.type),
                    source=e.source,
                    service=e.service,
                    title=e.title,
                    relevance=str(e.relevance),
                    confidence=round(float(e.confidence or 0.0), 3),
                    has_tool_call=e.tool_call_id is not None,
                )
            )
            corpus_parts.extend([e.title or "", e.description or "", json.dumps(e.value or {})])
            corpus_parts.extend(json.dumps(e.normalized or {}))
        for e in rows:
            corpus_parts.append(e.ref)

        # --- hypotheses -----------------------------------------------------
        hyps = (
            await session.execute(
                select(Hypothesis).where(Hypothesis.incident_id == run.incident_id)
            )
        ).scalars().all()
        linked_refs: set[str] = set()
        for h in hyps:
            links = (
                await session.execute(
                    select(HypothesisEvidenceLink).where(
                        HypothesisEvidenceLink.hypothesis_id == h.id
                    )
                )
            ).scalars().all()
            refs = [str(r) for r in (h.evidence_refs or [])]
            # The link rows are the authoritative Hypothesis → Evidence edges;
            # the JSON column is a read-optimised copy. Comparing them catches a
            # divergence instead of silently trusting one side.
            if len(links) != len(refs):
                result.graph_errors.append(
                    {
                        "type": "traceability",
                        "data": {
                            "hypothesis": h.ref,
                            "links": len(links),
                            "refs": len(refs),
                        },
                    }
                )
            linked_refs.update(refs)
            result.hypotheses.append(
                HypothesisRecord(
                    ref=h.ref,
                    domain=domains.get(h.ref) or str(h.category or "unknown"),
                    category=str(h.category or "unknown"),
                    confidence=round(float(h.confidence or 0.0), 3),
                    status=str(h.status),
                    evidence_refs=refs,
                    reasoning=(h.reasoning or "")[-400:],
                )
            )
            if str(h.status) == "rejected":
                result.rejected_hypotheses += 1

        result.evidence_linked_share = (
            round(len(linked_refs) / len(rows), 4) if rows else 0.0
        )

        # Precision is measured against what the *final answer* cited, not
        # against whichever hypothesis happened to be widest: "of everything I
        # collected, how much did I actually reason from" is the question.
        cited = len(set(diagnosis_refs) & {e.ref for e in result.evidence})
        result.evidence_recall, result.evidence_utilisation, result.missing_evidence = (
            score_evidence(case, " ".join(corpus_parts), cited, len(rows))
        )

        # --- recovery -------------------------------------------------------
        plan = None
        if incident is not None:
            plan = (
                await session.execute(
                    select(RecoveryPlan).where(RecoveryPlan.incident_id == run.incident_id)
                )
            ).scalars().first()
        if plan is not None:
            actions = (
                await session.execute(
                    select(RecoveryAction).where(RecoveryAction.plan_id == plan.id)
                )
            ).scalars().all()
            for a in actions:
                status = str(a.status)
                result.actions.append(
                    ActionRecord(
                        ref=a.ref,
                        tool=a.tool_name,
                        target=a.target_service,
                        risk=str(a.risk_level),
                        tier=a.approval_tier,
                        status=status,
                        effective=a.effective,
                        rollback_tool=a.rollback_tool,
                        executed_by=a.executed_by,
                        error=a.error,
                    )
                )
                if status in {"succeeded", "ineffective"}:
                    result.recovery_attempted = True
                if status == "succeeded" and a.effective is not False:
                    result.recovery_effective = True
                if status == "rolled_back":
                    result.rollback_used = True

        # --- verification, both sides --------------------------------------
        verification = None
        if incident is not None:
            verification = (
                await session.execute(
                    select(VerificationResult).where(
                        VerificationResult.incident_id == run.incident_id
                    )
                )
            ).scalars().first()
        if verification is not None:
            result.agent_verdict = str(verification.status)

        # --- spans ----------------------------------------------------------
        spans = await repo.spans_for_run(run.id)
        span_dicts = [
            {"span_id": s.span_id, "parent_span_id": s.parent_span_id, "kind": s.kind}
            for s in spans
        ]
        result.span_count = len(span_dicts)
        for s in spans:
            result.span_kinds[s.kind] = result.span_kinds.get(s.kind, 0) + 1
        if span_dicts:
            single_root, dangling, depth = trace_integrity(span_dicts)
            result.trace_single_root = single_root
            result.trace_dangling_parents = dangling
            result.trace_max_depth = depth

    # --- the environment's own verdict -------------------------------------
    truth = await infra.ground_truth(case.scenario)
    criteria = truth.get("verification_criteria") or list(case.verification_criteria)
    if criteria:
        evaluated = await infra.evaluate_criteria(criteria)
        result.env_evaluated = True
        result.env_passed = bool(evaluated.get("passed"))
        result.env_checks = evaluated.get("checks", [])
    return result


async def run_case(case: EvalCase, *, factory: Any, infra: Any, sleep_after: float = 0.0) -> CaseResult:
    """Reset the environment, run the incident end to end, score the result."""
    started = time.perf_counter()
    await infra.reset(case.scenario)
    await infra.inject(case.scenario)

    outcome = await _drive_to_completion(case, factory=factory)
    result = await _collect(
        case,
        outcome,
        factory=factory,
        infra=infra,
        latency_ms=(time.perf_counter() - started) * 1000,
    )
    if sleep_after:
        await asyncio.sleep(sleep_after)
    return result


# ---------------------------------------------------------------------------
# Suite
# ---------------------------------------------------------------------------


@dataclass
class SuiteResult:
    cases: list[CaseResult]
    environment: dict[str, str]
    database: str
    started_at: str = ""
    duration_ms: float = 0.0
    simulator_mode: str = "in-process (ASGI)"

    def as_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "database": self.database,
            "simulator_mode": self.simulator_mode,
            "started_at": self.started_at,
            "duration_ms": round(self.duration_ms, 1),
            "cases": [c.as_dict() for c in self.cases],
        }


async def run_suite(
    *,
    scenarios: list[str] | None = None,
    db_path: str | Path | None = None,
    keep_db: bool = False,
) -> SuiteResult:
    """Run every scenario against a fresh database on a private simulator."""
    cases = load_eval_cases(scenarios)

    workdir = Path(db_path).parent if db_path else Path(tempfile.mkdtemp(prefix="opspilot-eval-"))
    workdir.mkdir(parents=True, exist_ok=True)
    db_file = Path(db_path) if db_path else workdir / "eval.db"
    # Before the first backend import, not after: the database URL and the
    # simulator's eval flag are read when those modules are imported, so a
    # `prepare_environment` that runs later configures the wrong process.
    environment = prepare_environment(db_file)

    import httpx
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import opspilot_backend.db.session  # noqa: F401 - installs the engine listeners
    from opspilot_backend.infrastructure.container import Providers, set_providers
    from opspilot_backend.infrastructure.providers import (
        FileRunbookProvider,
        GitHubAdapter,
        SimulatorInfraProvider,
    )
    from opspilot_backend.models import Base

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_file.as_posix()}",
        echo=False,
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # The simulator runs in this process over ASGI: no port to collide with, no
    # stale server to accidentally score. The ground-truth route opens because
    # ``prepare_environment`` set the eval flag; the simulator re-reads that
    # flag per request, so it does not matter whether some other module
    # imported the app first.
    from opspilot_simulator.server import app as sim_app

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=sim_app), base_url="http://simulator.test"
    )
    infra = SimulatorInfraProvider(client=client)
    set_providers(
        Providers(
            services=infra,
            metrics=infra,
            logs=infra,
            deployments=infra,
            runbooks=FileRunbookProvider(),
            github=GitHubAdapter(),
            extra={"infra": infra},
        )
    )

    started = time.perf_counter()
    from datetime import datetime, timezone

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results: list[CaseResult] = []
    try:
        for case in cases:
            try:
                results.append(await run_case(case, factory=factory, infra=infra))
            except Exception as exc:  # noqa: BLE001 - one bad case must not hide the rest
                results.append(
                    CaseResult(
                        scenario=case.scenario,
                        title=case.title,
                        service=case.service,
                        severity=case.severity,
                        difficulty=case.difficulty,
                        expected_category=case.expected_category,
                        expected_root_cause=case.expected_root_cause,
                        expected_recovery=list(case.correct_recovery),
                        required_tools=sorted(case.required_tools),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
    finally:
        await client.aclose()
        await engine.dispose()

    suite = SuiteResult(
        cases=results,
        environment=reportable_environment(environment),
        database=Path(db_file).name,
        started_at=started_at,
        duration_ms=(time.perf_counter() - started) * 1000,
    )
    if not keep_db:
        _best_effort_remove(db_file)
        _best_effort_remove(Path(f"{db_file}-wal"))
        _best_effort_remove(Path(f"{db_file}-shm"))
    return suite


def _best_effort_remove(path: Path) -> None:
    """Delete a scratch artefact; never let cleanup be the thing that fails."""
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - Windows file locks
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_case(result: CaseResult) -> None:
    mark = "OK  " if result.root_cause_correct else "MISS"
    if result.error:
        mark = "ERR "
    env = "-" if result.env_passed is None else ("fixed" if result.env_passed else "broken")
    print(
        f"[{mark}] {result.scenario:<32} "
        f"got={str(result.diagnosed_category):<12} want={result.expected_category:<12} "
        f"{result.diagnosis_outcome or '-':<22} "
        f"tools={result.tool_call_count:<3} "
        f"recall={result.evidence_recall:.2f} "
        f"verify={result.agent_verdict or '-':<7} env={env:<6} "
        f"{result.latency_ms:>7.0f}ms"
    )
    if result.error:
        print(f"        error: {result.error}")
    if result.missing_tools:
        print(f"        missing tools: {', '.join(result.missing_tools)}")
    if result.missing_evidence:
        print(f"        missed evidence: {', '.join(result.missing_evidence)}")
    if result.escalation_reason:
        print(f"        escalated: {result.escalation_reason}")


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score the OpsPilot agent end to end.")
    parser.add_argument("--only", default="", help="substring filter on scenario names")
    parser.add_argument("--json", action="store_true", help="emit the raw suite as JSON")
    parser.add_argument("--db", default="", help="reuse this database file instead of a temp one")
    parser.add_argument("--keep-db", action="store_true", help="do not delete the scratch database")
    args = parser.parse_args(argv)

    bootstrap_paths()
    names = load_eval_cases()
    selected = [c.scenario for c in names if args.only in c.scenario] if args.only else None

    suite = await run_suite(scenarios=selected, db_path=args.db or None, keep_db=args.keep_db)

    if args.json:
        print(json.dumps(suite.as_dict(), indent=2, default=str))
        return 0

    print("=" * 100)
    print("OpsPilot agent evaluation — end to end, scored against the simulator's answer key")
    print("=" * 100)
    for result in suite.cases:
        _print_case(result)

    from opspilot_evals.metrics.aggregator import build_report

    report = build_report(suite)
    print()
    print("-" * 100)
    for line in report.headline():
        print(line)
    return 0 if report.failures() == 0 else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(asyncio.run(_main()))
