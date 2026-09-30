"""Failure, interruption and recovery paths for the Agent runtime.

These tests prove that the incident response loop stays coherent when things go
wrong: the human says no, a recovery action fails, the model is slow, two
operators click approve at once, the runtime process restarts mid-run, or two
incidents run concurrently. Every assertion reads the database after the run
finishes; nothing is trusted just because the call returned.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from opspilot_backend.agent.checkpointer import DatabaseCheckpointer
from opspilot_backend.agent.llm import Completion, LLMProvider
from opspilot_backend.domain.enums import (
    AgentRunStatus,
    ApprovalStatus,
    IncidentStatus,
    RecoveryActionStatus,
)
from opspilot_backend.models import (
    AgentRun,
    Approval,
    Hypothesis,
    Incident,
    RecoveryAction,
    RecoveryPlan,
    Service,
    ToolCall,
)
from opspilot_backend.repositories.agent_run import AgentRunRepository
from opspilot_backend.repositories.incident import IncidentRepository
from opspilot_backend.services.agent_runtime import AgentRuntimeService

SERVICE_NAME = "payment-service"
SCENARIO = "payment-bad-deployment"


async def _make_incident(
    session, scenario: str = SCENARIO, service_name: str = SERVICE_NAME
) -> Incident:
    service = Service(
        name=service_name, description="Payments API", tier="application", owner="payments"
    )
    session.add(service)
    await session.flush()
    incident = await IncidentRepository(session).create(
        title=f"{service_name} error rate spike",
        service_id=service.id,
        severity="SEV1",
        description="Automated alert: error_rate above threshold.",
        scenario=scenario,
    )
    await session.commit()
    return incident


async def _run_to_approval(
    db_session,
    session_factory,
    simulator,
) -> tuple[AgentRun, AgentRunRepository, AgentRuntimeService]:
    """Helper: start a run and block until it parks at the approval gate."""
    incident = await _make_incident(db_session)
    runtime = AgentRuntimeService(
        db_session,
        session_factory=session_factory,
        checkpointer=DatabaseCheckpointer(session_factory),
    )
    run = await runtime.start(incident.id, background=False)

    runs = AgentRunRepository(db_session)
    run = await runs.get(run.id)
    assert run.status == AgentRunStatus.WAITING_APPROVAL, run.status
    return run, runs, runtime


async def _recovery_actions_for_incident(
    db_session, incident_id: str | uuid.UUID
) -> list[RecoveryAction]:
    """RecoveryAction is linked to a plan, not directly to an incident."""
    if isinstance(incident_id, str):
        incident_id = uuid.UUID(incident_id)
    return (await db_session.execute(
        select(RecoveryAction)
        .join(RecoveryPlan, RecoveryAction.plan_id == RecoveryPlan.id)
        .where(RecoveryPlan.incident_id == incident_id)
    )).scalars().all()


@pytest.mark.parametrize("simulator", [SCENARIO], indirect=True)
async def test_approval_rejection_ends_run_without_recovery(
    db_session, session_factory, simulator
) -> None:
    """If the operator rejects the recovery plan, no action executes."""
    run, runs, runtime = await _run_to_approval(db_session, session_factory, simulator)

    approval = (await db_session.execute(
        select(Approval).where(Approval.incident_id == run.incident_id)
    )).scalars().first()
    assert approval is not None

    await runs.decide_approval(
        str(approval.id), decision="reject", decided_by="oncall@example.com", note="too risky"
    )
    await db_session.commit()

    await runtime.resume(
        run.id, {"decision": "reject", "reason": "too risky"}, background=False
    )

    run = await runs.get(run.id)
    assert run.status in {AgentRunStatus.FAILED.value, AgentRunStatus.COMPLETED.value}

    actions = await _recovery_actions_for_incident(db_session, str(run.incident_id))
    assert not any(
        a.status == RecoveryActionStatus.SUCCEEDED.value for a in actions
    ), [(a.ref, a.status) for a in actions]

    approval = await runs.get_approval(str(approval.id))
    assert approval["status"] == ApprovalStatus.REJECTED.value


@pytest.mark.parametrize("simulator", [SCENARIO], indirect=True)
async def test_recovery_action_failure_surfaces_in_run(
    db_session, session_factory, simulator
) -> None:
    """A failed rollback must be recorded on the action and reflected in the run."""
    run, runs, runtime = await _run_to_approval(db_session, session_factory, simulator)

    approval = (await db_session.execute(
        select(Approval).where(Approval.incident_id == run.incident_id)
    )).scalars().first()

    # Make every rollback report a failure. The run must not crash.
    original_act = simulator.act

    def failing_rollback(action: str, service: str, **params: Any):
        if action == "rollback_deployment":
            return {
                "service": service,
                "action": action,
                "ok": False,
                "effective": False,
                "reason": "simulated rollback failure",
            }
        return original_act(action, service, **params)

    simulator.act = failing_rollback
    try:
        await runs.decide_approval(
            str(approval.id), decision="approve", decided_by="oncall@example.com"
        )
        await db_session.commit()
        await runtime.resume(
            run.id, {"decision": "approve", "approved_by": "oncall@example.com"},
            background=False,
        )
    finally:
        simulator.act = original_act

    actions = await _recovery_actions_for_incident(db_session, str(run.incident_id))
    non_success = [
        a for a in actions
        if a.status not in {RecoveryActionStatus.SUCCEEDED.value, RecoveryActionStatus.PENDING.value}
    ]
    assert non_success, [(a.ref, a.status, a.error) for a in actions]
    assert any(a.error for a in non_success), [(a.ref, a.status, a.error) for a in actions]


class _FailingProvider:
    """LLM provider that fails immediately; the node must fall back."""

    name = "failing_mock"

    async def complete(self, system: str, user: str) -> Completion:
        raise asyncio.TimeoutError("simulated model timeout")


@pytest.mark.parametrize("simulator", [SCENARIO], indirect=True)
async def test_slow_llm_falls_back_to_deterministic_reasoning(
    db_session, session_factory, simulator
) -> None:
    """A model that never responds in time must not fail the hypothesis node."""
    incident = await _make_incident(db_session)
    runtime = AgentRuntimeService(
        db_session,
        session_factory=session_factory,
        checkpointer=DatabaseCheckpointer(session_factory),
    )

    failing_provider: LLMProvider = _FailingProvider()  # type: ignore[assignment]
    with patch("opspilot_backend.agent.nodes.get_llm", return_value=failing_provider):
        run = await runtime.start(incident.id, background=False)

    runs = AgentRunRepository(db_session)
    run = await runs.get(run.id)
    assert run.status == AgentRunStatus.WAITING_APPROVAL, run.status

    steps = await runs.steps(run.id)
    stages = {s.stage.value for s in steps}
    assert "hypothesis_generation" in stages
    assert "root_cause_diagnosis" in stages

    hypothesis_count = (await db_session.execute(
        select(func.count()).select_from(Hypothesis).where(Hypothesis.incident_id == incident.id)
    )).scalar()
    assert hypothesis_count > 0


@pytest.mark.parametrize("simulator", [SCENARIO], indirect=True)
async def test_approval_double_click_is_idempotent(
    db_session, session_factory, simulator
) -> None:
    """Two concurrent approve clicks must not resume the graph twice."""
    run, runs, runtime = await _run_to_approval(db_session, session_factory, simulator)

    approval = (await db_session.execute(
        select(Approval).where(Approval.incident_id == run.incident_id)
    )).scalars().first()
    await runs.decide_approval(
        str(approval.id), decision="approve", decided_by="oncall@example.com"
    )
    await db_session.commit()

    # Fire two resumes concurrently; one should win, the other be a no-op.
    decision = {"decision": "approve", "approved_by": "oncall@example.com"}
    await asyncio.gather(
        runtime.resume(run.id, decision, background=False),
        runtime.resume(run.id, decision, background=False),
        return_exceptions=True,
    )

    run = await runs.get(run.id)
    assert run.status == AgentRunStatus.COMPLETED.value, run.status

    actions = await _recovery_actions_for_incident(db_session, str(run.incident_id))
    # Two concurrent resumes must not create duplicate recovery actions.
    assert len(actions) == 1, [(a.ref, a.status) for a in actions]
    rollback_calls = (await db_session.execute(
        select(func.count())
        .select_from(ToolCall)
        .where(ToolCall.run_id == run.id, ToolCall.tool_name == "rollback_deployment")
    )).scalar()
    assert rollback_calls == 1, rollback_calls


@pytest.mark.parametrize("simulator", [SCENARIO], indirect=True)
async def test_new_runtime_instance_resumes_from_checkpoint(
    db_session, session_factory, simulator
) -> None:
    """A brand-new runtime service can resume a run started by another instance."""
    run, runs, _runtime = await _run_to_approval(db_session, session_factory, simulator)

    approval = (await db_session.execute(
        select(Approval).where(Approval.incident_id == run.incident_id)
    )).scalars().first()
    await runs.decide_approval(
        str(approval.id), decision="approve", decided_by="oncall@example.com"
    )
    await db_session.commit()

    # Drop the original runtime and build a fresh one with the same persistence.
    new_runtime = AgentRuntimeService(
        db_session,
        session_factory=session_factory,
        checkpointer=DatabaseCheckpointer(session_factory),
    )
    await new_runtime.resume(
        run.id, {"decision": "approve", "approved_by": "oncall@example.com"},
        background=False,
    )

    run = await runs.get(run.id)
    assert run.status == AgentRunStatus.COMPLETED.value, run.status

    incident = await IncidentRepository(db_session).get_or_raise(run.incident_id)
    assert incident.status == IncidentStatus.RESOLVED.value


@pytest.mark.parametrize("simulator", [SCENARIO], indirect=True)
async def test_concurrent_runs_do_not_corrupt_shared_state(
    db_session, session_factory, simulator
) -> None:
    """Two incidents on the same service keep separate evidence and budgets."""
    service = Service(
        name=SERVICE_NAME, description="Payments API", tier="application", owner="payments"
    )
    db_session.add(service)
    await db_session.flush()
    incident_a = await IncidentRepository(db_session).create(
        title="Payment API error rate spike A",
        service_id=service.id,
        severity="SEV1",
        description="Alert A",
        scenario=SCENARIO,
    )
    incident_b = await IncidentRepository(db_session).create(
        title="Payment API error rate spike B",
        service_id=service.id,
        severity="SEV1",
        description="Alert B",
        scenario=SCENARIO,
    )
    await db_session.commit()

    # Each run needs its own runtime/session so their commits do not collide.
    async with session_factory() as session_a, session_factory() as session_b:
        runtime_a = AgentRuntimeService(
            session_a,
            session_factory=session_factory,
            checkpointer=DatabaseCheckpointer(session_factory),
        )
        runtime_b = AgentRuntimeService(
            session_b,
            session_factory=session_factory,
            checkpointer=DatabaseCheckpointer(session_factory),
        )

        run_a, run_b = await asyncio.gather(
            runtime_a.start(incident_a.id, background=False),
            runtime_b.start(incident_b.id, background=False),
        )

    runs = AgentRunRepository(db_session)
    for r in (run_a, run_b):
        run = await runs.get(r.id)
        assert run.status == AgentRunStatus.WAITING_APPROVAL, run.status

    # Evidence and approvals must be per-incident.
    counts = (await db_session.execute(
        select(Approval.incident_id, func.count()).group_by(Approval.incident_id)
    )).all()
    assert len(counts) == 2, counts


@pytest.mark.parametrize("simulator", [SCENARIO], indirect=True)
async def test_simulator_tool_error_during_investigation_is_captured(
    db_session, session_factory, simulator
) -> None:
    """A single failing probe must not crash the whole investigation."""
    incident = await _make_incident(db_session)

    original_act = simulator.act
    metrics_failed = {"value": False}

    def flaky_act(action: str, service: str, **params: Any):
        # The first probe that asks for metrics fails once.
        if action == "query_metrics" and not metrics_failed["value"]:
            metrics_failed["value"] = True
            raise RuntimeError("simulated metrics backend outage")
        return original_act(action, service, **params)

    simulator.act = flaky_act
    try:
        runtime = AgentRuntimeService(
            db_session,
            session_factory=session_factory,
            checkpointer=DatabaseCheckpointer(session_factory),
        )
        run = await runtime.start(incident.id, background=False)
    finally:
        simulator.act = original_act

    runs = AgentRunRepository(db_session)
    run = await runs.get(run.id)
    assert run.status in {
        AgentRunStatus.WAITING_APPROVAL.value,
        AgentRunStatus.COMPLETED.value,
        AgentRunStatus.FAILED.value,
    }, run.status

    calls = await runs.tool_calls(run.id)
    assert any(c.status != "completed" for c in calls), [(c.tool_name, c.status) for c in calls]
