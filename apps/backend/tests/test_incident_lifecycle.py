"""End-to-end incident lifecycle — the test that proves the refactor works.

It drives the real path:

    create incident → run the 14-node LangGraph workflow → the simulator is
    queried for real metrics/logs/deployments → evidence → hypotheses →
    verified root cause → recovery plan → human approval (interrupt + resume
    from the database checkpoint) → executed recovery → verification probe →
    resolved → postmortem

Every assertion reads rows back from the database. Nothing is mocked except
the transport to the simulator (in-process ASGI, still a real HTTP round-trip).
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from opspilot_backend.agent.checkpointer import DatabaseCheckpointer
from opspilot_backend.domain.enums import (
    AgentRunStatus,
    ApprovalStatus,
    IncidentStatus,
    RecoveryActionStatus,
    VerificationStatus,
)
from opspilot_backend.models import (
    AgentEvent,
    AgentStep,
    Approval,
    Evidence,
    Hypothesis,
    HypothesisEvidenceLink,
    Incident,
    Postmortem,
    RecoveryAction,
    RecoveryPlan,
    Service,
    ToolCall,
    VerificationResult,
)
from opspilot_backend.repositories.agent_run import AgentRunRepository
from opspilot_backend.repositories.incident import IncidentRepository
from opspilot_backend.services.agent_runtime import AgentRuntimeService

SERVICE_NAME = "payment-service"
# Scenario names come from opspilot_simulator.scenarios; the fault kinds are
# no longer scenario names, so "bad_deployment" would silently inject nothing.
SCENARIO = "payment-bad-deployment"


async def _make_incident(session) -> Incident:
    service = Service(
        name=SERVICE_NAME, description="Payments API", tier="application", owner="payments"
    )
    session.add(service)
    await session.flush()
    repo = IncidentRepository(session)
    incident = await repo.create(
        title="Payment API error rate spike after v1.8.4",
        service_id=service.id,
        severity="SEV1",
        description="Automated alert: error_rate above threshold.",
        scenario=SCENARIO,
    )
    await session.commit()
    return incident


@pytest.mark.parametrize("simulator", [SCENARIO], indirect=True)
async def test_incident_lifecycle_runs_end_to_end(
    db_session, session_factory, simulator
) -> None:
    incident = await _make_incident(db_session)

    runtime = AgentRuntimeService(
        db_session,
        session_factory=session_factory,
        checkpointer=DatabaseCheckpointer(session_factory),
    )
    run = await runtime.start(incident.id, background=False)

    # ------------------------------------------------------------------
    # Phase 1: investigation up to the approval gate
    # ------------------------------------------------------------------
    # The run executes in its own session (like a real background task);
    # repository reads use populate_existing so we always see fresh rows.
    run_id = run.id
    runs = AgentRunRepository(db_session)
    run = await runs.get(run_id)
    assert run.status == AgentRunStatus.WAITING_APPROVAL, run.status

    incident = await IncidentRepository(db_session).get_or_raise(incident.id)
    assert incident.status == IncidentStatus.WAITING_APPROVAL.value, incident.status

    steps = await runs.steps(run.id)
    stages = [s.stage.value for s in steps]
    assert "load_context" in stages
    assert "parallel_investigation" in stages
    assert "root_cause_diagnosis" in stages
    assert "human_approval" in stages
    # Every step that ran must have been closed out with a duration.
    assert all(s.duration_ms is not None for s in steps if s.status != "running")

    calls = await runs.tool_calls(run.id)
    assert calls, "investigation must record the tool calls it made"
    assert {c.tool_name for c in calls} >= {
        "get_service_status",
        "query_metrics",
        "get_deployments",
    }
    assert all(c.duration_ms is not None for c in calls)

    evidence = (await db_session.execute(
        select(Evidence).where(Evidence.incident_id == incident.id)
    )).scalars().all()
    assert len(evidence) >= 3, [e.ref for e in evidence]
    assert {e.ref for e in evidence} >= {"E001", "E002"}
    # Evidence must be traceable back to the tool call that produced it.
    assert sum(1 for e in evidence if e.tool_call_id is not None) >= 3

    hypotheses = (await db_session.execute(
        select(Hypothesis).where(Hypothesis.incident_id == incident.id)
    )).scalars().all()
    assert hypotheses, "no hypothesis was generated"
    links = (await db_session.execute(
        select(HypothesisEvidenceLink).where(
            HypothesisEvidenceLink.hypothesis_id == hypotheses[0].id
        )
    )).scalars().all()
    assert links, "hypothesis cites no evidence row"

    assert incident.root_cause, "diagnosis did not reach the incident"
    assert incident.root_cause_category == "deployment", incident.root_cause_category

    plan = (await db_session.execute(
        select(RecoveryPlan).where(RecoveryPlan.incident_id == incident.id)
    )).scalars().first()
    assert plan is not None
    assert plan.risk_level == "CRITICAL", plan.risk_level
    assert plan.status == "pending_approval"

    actions = (await db_session.execute(
        select(RecoveryAction).where(RecoveryAction.plan_id == plan.id)
    )).scalars().all()
    assert actions and actions[0].tool_name == "rollback_deployment"

    approval = (await db_session.execute(
        select(Approval).where(Approval.incident_id == incident.id)
    )).scalars().first()
    assert approval is not None
    assert approval.status == ApprovalStatus.PENDING.value
    assert approval.risk_level == "CRITICAL"

    events = await runs.events(run.id)
    assert events, "no agent events were persisted"
    assert [e.seq for e in events] == sorted(e.seq for e in events)
    event_types = {e.event_type for e in events}
    assert "agent.started" in event_types
    assert "evidence.created" in event_types
    assert "approval.required" in event_types

    # Nothing may execute before a human says so.
    assert not any(a.status == RecoveryActionStatus.SUCCEEDED.value for a in actions)

    # ------------------------------------------------------------------
    # Phase 2: human approves → resume from the checkpoint
    # ------------------------------------------------------------------
    decided = await runs.decide_approval(
        str(approval.id), decision="approve", decided_by="oncall@example.com",
        note="Rollout window agreed with release manager.",
    )
    assert decided["status"] == ApprovalStatus.APPROVED.value
    await db_session.commit()

    await runtime.resume(
        run.id, {"decision": "approve", "approved_by": "oncall@example.com"},
        background=False,
    )

    incident = await IncidentRepository(db_session).get_or_raise(incident.id)
    assert incident.status == IncidentStatus.RESOLVED.value, incident.status

    run = await runs.get(run.id)
    assert run.status == AgentRunStatus.COMPLETED.value, run.status

    actions = (await db_session.execute(
        select(RecoveryAction)
        .where(RecoveryAction.plan_id == plan.id)
        .execution_options(populate_existing=True)  # the run mutated these rows in another session
    )).scalars().all()
    executed = [a for a in actions if a.status == RecoveryActionStatus.SUCCEEDED.value]
    assert executed, [(a.ref, a.status, a.error) for a in actions]
    assert executed[0].tool_call_id is not None
    assert executed[0].executed_by == "agent"

    verification = (await db_session.execute(
        select(VerificationResult).where(VerificationResult.incident_id == incident.id)
    )).scalars().first()
    assert verification is not None
    assert verification.total_checks >= 1
    # Verification is a real probe, not a constant.
    assert verification.status == VerificationStatus.PASSED.value, verification.checks

    postmortem = (await db_session.execute(
        select(Postmortem).where(Postmortem.incident_id == incident.id)
    )).scalars().first()
    assert postmortem is not None
    assert postmortem.root_cause
    assert postmortem.timeline, "postmortem timeline is empty"

    timeline = await IncidentRepository(db_session).timeline(incident.id)
    kinds = {e.event_type for e in timeline}
    assert "incident.created" in kinds
    assert "diagnosis.completed" in kinds
    assert "approval.decided" in kinds

    # ------------------------------------------------------------------
    # Traceability: the whole run is reconstructable from the database
    # ------------------------------------------------------------------
    call_count = await db_session.scalar(
        select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run.id)
    )
    step_count = await db_session.scalar(
        select(func.count()).select_from(AgentStep).where(AgentStep.run_id == run.id)
    )
    event_count = await db_session.scalar(
        select(func.count()).select_from(AgentEvent).where(AgentEvent.run_id == run.id)
    )
    assert call_count >= 5
    assert step_count >= 10
    assert event_count >= 10

    # Resume must not have duplicated the approval request.
    approvals = (await db_session.execute(
        select(Approval)
        .where(Approval.incident_id == incident.id)
        .execution_options(populate_existing=True)
    )).scalars().all()
    assert len(approvals) == 1, [a.status for a in approvals]
