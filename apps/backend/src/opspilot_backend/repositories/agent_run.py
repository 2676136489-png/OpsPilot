"""Agent run persistence.

This repository is the concrete implementation of
:class:`opspilot_backend.agent.context.AgentPersistence`. The Agent layer only
ever sees the Protocol, so it stays free of SQLAlchemy; everything the workflow
produces (steps, evidence, hypotheses, plans, approvals, verification,
postmortem) lands here.

Two invariants are enforced in this file:

* **Every incident state change goes through the state machine** — no node,
  service or controller may set ``Incident.status`` directly.
* **Every state change leaves an IncidentEvent *and* an AuditLog**, so the
  timeline a human reads is the same one the machine wrote.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Only ever named in annotations, so importing it at runtime would be an
    # import cycle for nothing.
    from opspilot_backend.agent.budget import Budget

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_backend.core.logging import log_event
from opspilot_backend.db.base import utcnow
from opspilot_backend.domain.enums import (
    ActorType,
    AgentRunStatus,
    AgentStage,
    ApprovalStatus,
    DiagnosisOutcome,
    EvidenceRelevance,
    EvidenceType,
    IncidentStatus,
    RiskLevel,
    StepStatus,
    ToolCallStatus,
)
from opspilot_backend.domain.errors import InvalidStateTransitionError
from opspilot_backend.models import (
    AgentEvent,
    AgentRun,
    AgentStep,
    Approval,
    AuditLog,
    Evidence,
    Hypothesis,
    HypothesisEvidenceLink,
    Incident,
    Postmortem,
    RecoveryAction,
    RecoveryPlan,
    ToolCall,
    TraceSpan,
    VerificationResult,
)
from opspilot_backend.repositories.base import (
    Page,
    apply_filters,
    apply_sorting,
    paginate,
)
from opspilot_backend.repositories.incident import IncidentRepository

_TERMINAL_RUN_STATUSES = frozenset(
    {
        AgentRunStatus.COMPLETED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
    }
)


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    """Parse a UUID without exploding on placeholder ids (``tc-1`` in tests)."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _evidence_type(raw: Any) -> str:
    text = str(raw or "").lower()
    return text if text in EvidenceType._value2member_map_ else EvidenceType.OBSERVATION.value


def _risk(raw: Any, default: str = RiskLevel.MEDIUM.value) -> str:
    text = str(raw or "").upper()
    return text if text in RiskLevel._value2member_map_ else default


def _relevance(raw: Any) -> str:
    text = str(raw or "").upper()
    return (
        text if text in EvidenceRelevance._value2member_map_
        else EvidenceRelevance.MEDIUM.value
    )


def _outcome(raw: Any) -> str | None:
    text = str(raw or "").upper()
    return text if text in DiagnosisOutcome._value2member_map_ else None


class AgentRunRepository:
    """Runs, steps, tool calls, events and every artefact they produce."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def commit(self) -> None:
        """End the current transaction — see ``AgentPersistence.commit``."""
        await self.session.commit()

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------
    async def create_run(
        self,
        *,
        incident_id: Any,
        thread_id: str,
        reasoning_mode: str = "deterministic",
        max_attempts: int = 3,
        budget: "Budget | None" = None,
        trace_id: str = "",
        request_id: str = "",
    ) -> AgentRun:
        """Create a run. ``thread_id`` is the LangGraph checkpoint key."""
        existing = await self.get_by_thread(thread_id)
        if existing is not None:
            return existing

        run = AgentRun(
            incident_id=_uuid_or_none(incident_id),
            status=AgentRunStatus.PENDING,
            thread_id=thread_id,
            reasoning_mode=reasoning_mode,
            attempt=1,
            max_attempts=max_attempts,
            trace_id=trace_id,
            request_id=request_id,
        )
        if budget is not None:
            run.budget_tool_calls = budget.max_tool_calls
            run.budget_seconds = budget.max_seconds
            run.budget_tokens = budget.max_tokens
            run.budget_max_retries = budget.max_retries
            run.budget_max_parallel_tools = budget.max_parallel_tools
        else:
            # Keep the row self-describing even when the caller did not pass
            # one: an operator reading the run later needs the limits that
            # were in force, not the defaults in the model.
            from opspilot_backend.agent.budget import Budget as _Budget

            defaults = _Budget.from_settings()
            run.budget_tool_calls = defaults.max_tool_calls
            run.budget_seconds = defaults.max_seconds
            run.budget_tokens = defaults.max_tokens
            run.budget_max_retries = defaults.max_retries
            run.budget_max_parallel_tools = defaults.max_parallel_tools
        self.session.add(run)
        await self.session.flush()
        return run

    async def get(self, run_id: Any) -> AgentRun | None:
        key = _uuid_or_none(run_id)
        if key is None:
            return None
        # populate_existing: the run is mutated by its own background session,
        # so an identity-map hit must not serve stale attribute values.
        stmt = (
            select(AgentRun)
            .where(AgentRun.id == key)
            .execution_options(populate_existing=True)
        )
        return await self.session.scalar(stmt)

    async def get_by_thread(self, thread_id: str) -> AgentRun | None:
        stmt = select(AgentRun).where(AgentRun.thread_id == thread_id)
        return await self.session.scalar(stmt)

    async def get_or_raise(self, run_id: Any) -> AgentRun:
        run = await self.get(run_id)
        if run is None:
            from opspilot_backend.domain.errors import NotFoundError

            raise NotFoundError("AgentRun", run_id)
        return run

    async def list(
        self,
        *,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        page_size: int = 20,
        sort: str | None = None,
    ) -> Page[AgentRun]:
        stmt = select(AgentRun)
        stmt = apply_filters(stmt, AgentRun, filters or {})
        stmt = apply_sorting(stmt, AgentRun, sort or "-created_at")
        items, total = await paginate(
            self.session, stmt, page=page, page_size=page_size
        )
        return Page(items=list(items), total=total, page=page, page_size=page_size)

    async def update_run(
        self,
        run_id: Any,
        *,
        status: str | None = None,
        current_stage: AgentStage | None = None,
        error: str | None = None,
        interrupt_payload: dict[str, Any] | None = None,
    ) -> None:
        run = await self.get(run_id)
        if run is None:
            return
        if status is not None:
            try:
                run.status = AgentRunStatus(str(status).lower())
            except ValueError:
                log_event(
                    "agent.run.invalid_status", run_id=str(run.id), status=status
                )
            else:
                if run.started_at is None and run.status != AgentRunStatus.PENDING:
                    run.started_at = utcnow()
                if run.status in _TERMINAL_RUN_STATUSES and run.ended_at is None:
                    run.ended_at = utcnow()
                if run.status == AgentRunStatus.WAITING_APPROVAL:
                    run.interrupted_at_stage = (
                        current_stage or run.current_stage or AgentStage.HUMAN_APPROVAL
                    )
        if current_stage is not None:
            run.current_stage = current_stage
        if error is not None:
            run.error = error
        if interrupt_payload is not None:
            run.interrupt_payload = interrupt_payload
        await self.session.flush()

    async def update_run_budget(self, run_id: Any, budget: "Budget") -> None:
        """Persist what the run has spent.

        Called after every tool call, so a run that is burning its allowance
        shows it live instead of only after it escalates.
        """
        run = await self.get(run_id)
        if run is None:
            return
        run.spent_tool_calls = budget.tool_calls
        run.spent_tokens = budget.tokens
        run.spent_retries = budget.retries
        run.spent_seconds = round(budget.elapsed_seconds, 2)
        run.budget_exhausted = budget.exhausted
        if budget.exhausted_reason is not None:
            run.escalation_reason = budget.exhausted_reason
        await self.session.flush()

    async def save_spans(
        self,
        run_id: Any,
        spans: list[dict[str, Any]],
        *,
        incident_id: Any = None,
    ) -> int:
        """Persist recorded spans so the trace survives the run.

        ``run_id`` may be ``None``: spans produced by a bare HTTP request
        belong to the trace too, and dropping them would make "what did that
        click actually do" unanswerable from the trace store.
        """
        key = _uuid_or_none(run_id)
        if not spans:
            return 0
        resolved_incident = _uuid_or_none(incident_id)
        if key is not None:
            run = await self.get(run_id)
            if run is not None:
                resolved_incident = run.incident_id
        for payload in spans:
            self.session.add(
                TraceSpan(
                    trace_id=str(payload.get("trace_id") or ""),
                    span_id=str(payload.get("span_id") or ""),
                    parent_span_id=str(payload.get("parent_span_id") or ""),
                    request_id=str(payload.get("request_id") or ""),
                    name=str(payload.get("name") or "")[:128],
                    kind=str(payload.get("kind") or "internal")[:32],
                    run_id=key,
                    incident_id=resolved_incident,
                    status=str(payload.get("status") or "ok")[:16],
                    error=payload.get("error"),
                    duration_ms=float(payload.get("duration_ms") or 0.0),
                    attributes=payload.get("attributes") or {},
                )
            )
        await self.session.flush()
        return len(spans)

    async def spans_for_run(self, run_id: Any) -> list[TraceSpan]:
        """Every span of a run, oldest first."""
        key = _uuid_or_none(run_id)
        if key is None:
            return []
        stmt = (
            select(TraceSpan)
            .where(TraceSpan.run_id == key)
            .order_by(TraceSpan.started_at.asc(), TraceSpan.id.asc())
        )
        return list((await self.session.execute(stmt)).scalars())

    async def spans_for_trace(self, trace_id: str) -> list[TraceSpan]:
        """Every span sharing a trace id — the cross-layer view.

        Returns HTTP-request spans too, not just the agent's, so an operator can
        see the call that triggered the run next to the run itself.
        """
        if not trace_id:
            return []
        stmt = (
            select(TraceSpan)
            .where(TraceSpan.trace_id == trace_id)
            .order_by(TraceSpan.started_at.asc(), TraceSpan.id.asc())
        )
        return list((await self.session.execute(stmt)).scalars())

    async def runs_for_incident(self, incident_id: Any) -> list[AgentRun]:
        stmt = (
            select(AgentRun)
            .where(AgentRun.incident_id == _uuid_or_none(incident_id))
            .order_by(AgentRun.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars())

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------
    async def start_step(
        self,
        run_id: str,
        stage: AgentStage,
        sequence: int,
        attempt: int,
        input_payload: dict[str, Any],
        trace_id: str = "",
        span_id: str = "",
    ) -> str:
        step = AgentStep(
            run_id=_uuid_or_none(run_id),
            stage=stage,
            status=StepStatus.RUNNING,
            sequence=sequence,
            attempt=attempt,
            input=input_payload,
            started_at=utcnow(),
            trace_id=trace_id,
            span_id=span_id,
        )
        self.session.add(step)
        await self.session.flush()
        await self.update_run(
            run_id, status=AgentRunStatus.RUNNING.value, current_stage=stage
        )
        return str(step.id)

    async def finish_step(
        self,
        step_id: str,
        *,
        status: StepStatus,
        output: dict[str, Any],
        error: str | None = None,
        duration_ms: int = 0,
    ) -> None:
        step = await self.session.get(AgentStep, _uuid_or_none(step_id))
        if step is None:
            return
        step.status = status
        step.output = output
        step.error = error
        step.duration_ms = duration_ms
        if status != StepStatus.RUNNING:
            step.ended_at = utcnow()
        await self.session.flush()

    async def steps(self, run_id: Any) -> list[AgentStep]:
        stmt = (
            select(AgentStep)
            .where(AgentStep.run_id == _uuid_or_none(run_id))
            .order_by(AgentStep.sequence.asc())
        )
        return list((await self.session.execute(stmt)).scalars())

    # ------------------------------------------------------------------
    # Tool calls
    # ------------------------------------------------------------------
    async def record_tool_call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        idempotency_key: str,
        run_id: str,
        step_id: str | None,
        risk_level: str,
        permission_level: str,
        transport: str,
        trace_id: str = "",
        span_id: str = "",
    ) -> str:
        """Insert a pending tool call; replays return the existing row id."""
        existing = await self.session.scalar(
            select(ToolCall).where(ToolCall.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return str(existing.id)

        call = ToolCall(
            run_id=_uuid_or_none(run_id),
            step_id=_uuid_or_none(step_id),
            tool_name=tool_name,
            arguments=arguments or {},
            status=ToolCallStatus.PENDING,
            risk_level=risk_level,
            permission_level=permission_level,
            idempotency_key=idempotency_key,
            transport=transport,
            trace_id=trace_id,
            span_id=span_id,
        )
        self.session.add(call)
        await self.session.flush()
        return str(call.id)

    async def finish_tool_call(
        self,
        tool_call_id: str,
        *,
        status: str,
        result: Any,
        error_code: str | None,
        error_message: str | None,
        duration_ms: int,
        attempts: int,
    ) -> None:
        call = await self.session.get(ToolCall, _uuid_or_none(tool_call_id))
        if call is None:
            return
        call.status = ToolCallStatus(str(status).lower())
        call.result = result if isinstance(result, (dict, list)) else {"value": result}
        call.error_code = error_code
        call.error_message = error_message
        call.duration_ms = duration_ms
        call.attempt = attempts
        await self.session.flush()

    async def tool_calls(self, run_id: Any) -> list[ToolCall]:
        stmt = (
            select(ToolCall)
            .where(ToolCall.run_id == _uuid_or_none(run_id))
            .order_by(ToolCall.created_at.asc())
        )
        return list((await self.session.execute(stmt)).scalars())

    # ------------------------------------------------------------------
    # Events (SSE replay source)
    # ------------------------------------------------------------------
    async def append_event(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        run_id: str,
        incident_id: str | None = None,
        stage: str | None = None,
    ) -> AgentEvent:
        event = AgentEvent(
            event_id=uuid.uuid4().hex,
            run_id=_uuid_or_none(run_id),
            incident_id=_uuid_or_none(incident_id),
            event_type=event_type,
            stage=stage,
            data=data or {},
            created_at=utcnow(),
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def events(self, run_id: Any, *, limit: int = 200) -> list[AgentEvent]:
        stmt = (
            select(AgentEvent)
            .where(AgentEvent.run_id == _uuid_or_none(run_id))
            .order_by(AgentEvent.seq.asc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def events_since(
        self, run_id: Any, after_seq: int, *, limit: int = 200
    ) -> list[AgentEvent]:
        """Replay window used by ``Last-Event-ID`` reconnects."""
        stmt = (
            select(AgentEvent)
            .where(
                AgentEvent.run_id == _uuid_or_none(run_id),
                AgentEvent.seq > int(after_seq),
            )
            .order_by(AgentEvent.seq.asc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def max_seq(self, run_id: Any) -> int:
        stmt = select(AgentEvent.seq).where(
            AgentEvent.run_id == _uuid_or_none(run_id)
        )
        values = list((await self.session.execute(stmt)).scalars())
        return max(values) if values else 0

    # ------------------------------------------------------------------
    # Evidence / hypotheses
    # ------------------------------------------------------------------
    async def save_evidence(
        self, incident_id: str, run_id: str, items: list[dict[str, Any]]
    ) -> dict[str, str]:
        inc = _uuid_or_none(incident_id)
        run = _uuid_or_none(run_id)
        if inc is None:
            return {}

        existing = {
            row.ref: row
            for row in (
                await self.session.execute(
                    select(Evidence).where(Evidence.incident_id == inc)
                )
            ).scalars()
        }
        mapping: dict[str, str] = {}
        for raw in items:
            ref = str(raw.get("ref") or "").strip()
            if not ref:
                continue
            row = existing.get(ref)
            if row is None:
                row = Evidence(incident_id=inc, run_id=run, ref=ref)
                self.session.add(row)
                existing[ref] = row
            elif row.run_id != run:
                # A second investigation of the same incident re-mints the
                # same refs (E001…). Refs are scoped to the incident, so the
                # row is updated in place — but the *latest* run owns it, or
                # the read path (evidence by run) shows the re-run with an
                # empty evidence list while the rows sit on a run that ended.
                row.run_id = run
            row.type = _evidence_type(raw.get("type"))
            row.source = str(raw.get("source") or "")[:128]
            row.service = str(raw.get("service") or "")[:128]
            row.title = str(raw.get("title") or "")[:300]
            row.description = str(raw.get("description") or "")
            row.value = raw.get("value")
            row.severity = str(raw.get("severity") or "medium")[:16]
            try:
                row.confidence = float(raw.get("confidence") or 0.5)
            except (TypeError, ValueError):
                row.confidence = 0.5
            row.tool_call_id = _uuid_or_none(raw.get("tool_call_id"))
            row.normalized = raw.get("normalized")
            row.relevance = _relevance(raw.get("relevance"))
            row.relevance_reason = str(raw.get("relevance_reason") or "")
            row.observed_at = _parse_dt(raw.get("observed_at")) or utcnow()
            mapping[ref] = str(row.id)
        await self.session.flush()
        return mapping

    async def save_hypotheses(
        self, incident_id: str, run_id: str, items: list[dict[str, Any]]
    ) -> dict[str, str]:
        inc = _uuid_or_none(incident_id)
        run = _uuid_or_none(run_id)
        if inc is None:
            return {}

        evidence_ids = {
            ref: eid
            for ref, eid in (
                await self.session.execute(
                    select(Evidence.ref, Evidence.id).where(Evidence.incident_id == inc)
                )
            ).all()
        }
        existing = {
            row.ref: row
            for row in (
                await self.session.execute(
                    select(Hypothesis).where(Hypothesis.incident_id == inc)
                )
            ).scalars()
        }

        mapping: dict[str, str] = {}
        for raw in items:
            ref = str(raw.get("ref") or "").strip()
            if not ref:
                continue
            row = existing.get(ref)
            if row is None:
                row = Hypothesis(incident_id=inc, run_id=run, ref=ref)
                self.session.add(row)
                existing[ref] = row
            elif row.run_id != run:
                # Same reasoning as in save_evidence: a re-run owns the
                # incident's hypotheses, or the UI renders it with none.
                row.run_id = run
            # Assign every column BEFORE the first flush — flushing straight
            # after construction INSERTs a row whose NOT NULL columns are
            # still None and the upsert dies on the constraint.
            row.statement = str(raw.get("statement") or "")
            row.category = str(raw.get("category") or "unknown")[:64]
            try:
                row.confidence = float(raw.get("confidence") or 0.0)
            except (TypeError, ValueError):
                row.confidence = 0.0
            row.status = str(raw.get("status") or "proposed")
            row.reasoning = str(raw.get("reasoning") or "")
            refs = [str(r) for r in (raw.get("evidence_refs") or [])]
            row.evidence_refs = refs
            await self.session.flush()  # row.id exists before link rows

            # Rebuild the authoritative Hypothesis → Evidence links.
            await self.session.execute(
                delete(HypothesisEvidenceLink).where(
                    HypothesisEvidenceLink.hypothesis_id == row.id
                )
            )
            for evidence_ref in refs:
                evidence_id = evidence_ids.get(evidence_ref)
                if evidence_id is None:
                    continue
                self.session.add(
                    HypothesisEvidenceLink(
                        hypothesis_id=row.id, evidence_id=evidence_id
                    )
                )
            mapping[ref] = str(row.id)
        await self.session.flush()
        return mapping

    # ------------------------------------------------------------------
    # Diagnosis
    # ------------------------------------------------------------------
    async def save_diagnosis(
        self, incident_id: str, run_id: str, diagnosis: dict[str, Any]
    ) -> None:
        incident = await self.session.get(Incident, _uuid_or_none(incident_id))
        if incident is None:
            return
        incident.root_cause = str(diagnosis.get("root_cause") or "")
        incident.root_cause_category = str(diagnosis.get("category") or "unknown")
        # The verdict is stored alongside the claim: "here is the cause" and
        # "here is how sure we are that this is the cause" are different facts
        # and the UI must not collapse them into one.
        incident.diagnosis_outcome = _outcome(diagnosis.get("outcome"))
        incident.diagnosis_summary = str(diagnosis.get("reasoning_summary") or "")
        try:
            incident.confidence = float(diagnosis.get("confidence") or 0.0)
        except (TypeError, ValueError):
            incident.confidence = 0.0
        await self.session.flush()

        repo = IncidentRepository(self.session)
        await repo.append_event(
            incident.id,
            "diagnosis.completed",
            str(diagnosis.get("root_cause") or "no root cause"),
            actor="agent",
            actor_type=ActorType.AGENT,
            data={
                "category": diagnosis.get("category"),
                "confidence": diagnosis.get("confidence"),
                "evidence_refs": diagnosis.get("evidence_refs") or [],
                "reasoning_summary": diagnosis.get("reasoning_summary"),
            },
        )

    # ------------------------------------------------------------------
    # Recovery plan + actions
    # ------------------------------------------------------------------
    async def save_recovery_plan(
        self, incident_id: str, run_id: str, plan: dict[str, Any]
    ) -> dict[str, Any]:
        inc = _uuid_or_none(incident_id)
        run = _uuid_or_none(run_id)
        if inc is None:
            return {"id": "", "action_ids": {}}

        candidate = _uuid_or_none(plan.get("plan_ref"))
        existing: RecoveryPlan | None = None
        if candidate is not None:
            existing = await self.session.get(RecoveryPlan, candidate)

        incident = await self.session.get(Incident, inc)
        if existing is None:
            existing = RecoveryPlan(incident_id=inc, run_id=run)
            self.session.add(existing)
            await self.session.flush()

        existing.ref = str(plan.get("plan_ref") or existing.ref or f"RP-{str(existing.id)[:8]}")
        existing.root_cause = str(
            plan.get("root_cause") or (incident.root_cause if incident else "")
        )
        existing.risk_level = _risk(plan.get("risk_level"))
        existing.status = str(plan.get("status") or existing.status or "draft")
        existing.rationale = str(plan.get("rationale") or "")
        existing.expected_impact = str(plan.get("expected_impact") or "")
        existing.verification_criteria = list(plan.get("verification_criteria") or [])
        existing.created_by = str(plan.get("created_by") or "agent")
        await self.session.flush()

        known = {
            action.ref: action
            for action in (
                await self.session.execute(
                    select(RecoveryAction).where(RecoveryAction.plan_id == existing.id)
                )
            ).scalars()
        }
        seen: set[str] = set()
        action_ids: dict[str, str] = {}
        for index, raw in enumerate(plan.get("actions") or [], start=1):
            ref = str(raw.get("ref") or f"A{index:02d}")
            seen.add(ref)
            row = known.get(ref)
            if row is None:
                row = RecoveryAction(plan_id=existing.id, ref=ref)
                self.session.add(row)
                known[ref] = row
            row.order = int(raw.get("order") or index)
            row.tool_name = str(raw.get("tool") or raw.get("tool_name") or "")
            row.target_service = str(raw.get("target_service") or "")[:128]
            row.parameters = dict(raw.get("parameters") or {})
            row.reason = str(raw.get("reason") or "")
            row.risk_level = _risk(raw.get("risk_level"))
            row.expected_impact = str(raw.get("expected_impact") or "")
            row.verification_strategy = str(raw.get("verification_strategy") or "")
            row.required_permission = str(raw.get("required_permission") or "")[:32]
            row.rollback_tool = raw.get("rollback_tool") or None
            row.rollback_parameters = dict(raw.get("rollback_parameters") or {})
            row.rollback_strategy = str(raw.get("rollback_strategy") or "")
            row.approval_tier = str(raw.get("approval_tier") or "approval")[:32]
            row.approval_status = str(raw.get("approval_status") or "not_required")[:32]
            if raw.get("effective") is not None:
                row.effective = bool(raw["effective"])
            if raw.get("status"):
                row.status = str(raw["status"])
            if raw.get("error"):
                row.error = str(raw["error"])
            action_ids[ref] = str(row.id)
        await self.session.flush()

        # Drop actions that vanished on a re-plan — but never one that a human
        # already has an approval attached to (that would erase the audit trail).
        for ref, row in known.items():
            if ref in seen:
                continue
            linked = await self.session.scalar(
                select(Approval.id).where(Approval.action_id == row.id)
            )
            if linked is None:
                await self.session.delete(row)
        await self.session.flush()

        return {
            "id": str(existing.id),
            "ref": existing.ref,
            "status": existing.status,
            "action_ids": action_ids,
        }

    async def _resolve_action(self, action_id: str) -> RecoveryAction | None:
        key = _uuid_or_none(action_id)
        if key is not None:
            action = await self.session.get(RecoveryAction, key)
            if action is not None:
                return action
        # The agent cites refs (A01) before ids exist — resolve newest first.
        stmt = (
            select(RecoveryAction)
            .where(RecoveryAction.ref == str(action_id))
            .order_by(RecoveryAction.created_at.desc())
            .limit(1)
        )
        return await self.session.scalar(stmt)

    async def save_recovery_action_result(
        self,
        action_id: str,
        *,
        status: str,
        result: dict[str, Any] | None,
        error: str | None,
        tool_call_id: str | None,
        executed_by: str,
    ) -> None:
        action = await self._resolve_action(action_id)
        if action is None:
            log_event("recovery.action.not_found", action_id=str(action_id))
            return
        action.status = str(status)
        action.result = result
        action.error = error
        action.tool_call_id = _uuid_or_none(tool_call_id)
        action.executed_by = executed_by
        action.executed_at = utcnow()
        await self.session.flush()

    async def recovery_plan(self, plan_id: Any) -> RecoveryPlan | None:
        return await self.session.get(RecoveryPlan, _uuid_or_none(plan_id))

    async def recovery_actions(self, plan_id: Any) -> list[RecoveryAction]:
        stmt = (
            select(RecoveryAction)
            .where(RecoveryAction.plan_id == _uuid_or_none(plan_id))
            .order_by(RecoveryAction.order.asc())
        )
        return list((await self.session.execute(stmt)).scalars())

    # ------------------------------------------------------------------
    # Approvals
    # ------------------------------------------------------------------
    async def create_approval(
        self,
        *,
        incident_id: str,
        run_id: str,
        action_id: str | None,
        action_type: str,
        risk_level: str,
        reason: str,
        requested_by: str,
    ) -> dict[str, Any]:
        resolved: uuid.UUID | None = None
        if action_id:
            action = await self._resolve_action(action_id)
            resolved = action.id if action else None

        approval = Approval(
            incident_id=_uuid_or_none(incident_id),
            run_id=_uuid_or_none(run_id),
            action_id=resolved,
            action_type=str(action_type)[:64],
            risk_level=_risk(risk_level, RiskLevel.HIGH.value),
            status=ApprovalStatus.PENDING,
            requested_reason=str(reason or ""),
            requested_by=str(requested_by or "agent"),
        )
        self.session.add(approval)
        await self.session.flush()

        repo = IncidentRepository(self.session)
        await repo.append_event(
            approval.incident_id,
            "approval.required",
            f"Approval required for {action_type} (risk {approval.risk_level})",
            actor=str(requested_by or "agent"),
            actor_type=ActorType.AGENT,
            data={"approval_id": str(approval.id), "risk_level": approval.risk_level},
        )
        return self._approval_dict(approval)

    async def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        approval = await self.session.get(Approval, _uuid_or_none(approval_id))
        return self._approval_dict(approval) if approval else None

    async def decide_approval(
        self,
        approval_id: str,
        *,
        decision: str,
        decided_by: str,
        note: str | None = None,
    ) -> dict[str, Any] | None:
        approval = await self.session.get(Approval, _uuid_or_none(approval_id))
        if approval is None:
            return None
        approve = str(decision).lower() in {"approve", "approved", "yes"}
        approval.status = (
            ApprovalStatus.APPROVED.value if approve else ApprovalStatus.REJECTED.value
        )
        approval.decided_by = decided_by
        approval.decision_note = note
        approval.decided_at = utcnow()
        await self.session.flush()

        repo = IncidentRepository(self.session)
        await repo.append_event(
            approval.incident_id,
            "approval.decided",
            f"Approval {approval.status} by {decided_by}",
            actor=decided_by,
            actor_type=ActorType.HUMAN,
            data={"approval_id": str(approval.id), "note": note},
        )
        return self._approval_dict(approval)

    async def find_pending_approval(self, run_id: str) -> dict[str, Any] | None:
        """Approval waiting for a decision on this run.

        ``interrupt()`` throws away the node's state update, so a resumed node
        cannot rely on ``recovery.approval_id`` being present. It recovers the
        request from here instead of raising a duplicate one.
        """
        stmt = (
            select(Approval)
            .where(
                Approval.run_id == _uuid_or_none(run_id),
                Approval.status == ApprovalStatus.PENDING.value,
            )
            .order_by(Approval.created_at.desc())
            .limit(1)
        )
        approval = await self.session.scalar(stmt)
        return self._approval_dict(approval) if approval else None

    async def find_latest_approval(self, run_id: str) -> dict[str, Any] | None:
        """The most recent approval for this run, whatever its status.

        A human normally decides *while* the run is parked, so by the time
        ``Command(resume=...)`` replays the node the pending window is already
        closed. The decided row is the ground truth — the node must adopt it,
        not open a second request the executor would then (rightly) refuse.
        """
        stmt = (
            select(Approval)
            .where(Approval.run_id == _uuid_or_none(run_id))
            .order_by(Approval.created_at.desc())
            .limit(1)
        )
        approval = await self.session.scalar(stmt)
        return self._approval_dict(approval) if approval else None

    async def pending_approvals(self, incident_id: Any) -> list[Approval]:
        stmt = (
            select(Approval)
            .where(
                Approval.incident_id == _uuid_or_none(incident_id),
                Approval.status == ApprovalStatus.PENDING.value,
            )
            .order_by(Approval.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars())

    @staticmethod
    def _approval_dict(approval: Approval) -> dict[str, Any]:
        return {
            "id": str(approval.id),
            "incident_id": str(approval.incident_id),
            "run_id": str(approval.run_id) if approval.run_id else None,
            "action_id": str(approval.action_id) if approval.action_id else None,
            "action_type": approval.action_type,
            "risk_level": approval.risk_level,
            "status": approval.status,
            "requested_reason": approval.requested_reason,
            "requested_by": approval.requested_by,
            "decided_by": approval.decided_by,
            "decision_note": approval.decision_note,
            "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
            "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
            "created_at": approval.created_at.isoformat() if approval.created_at else None,
        }

    # ------------------------------------------------------------------
    # Verification + postmortem
    # ------------------------------------------------------------------
    async def save_verification(
        self,
        incident_id: str,
        run_id: str,
        plan_id: str | None,
        payload: dict[str, Any],
    ) -> str:
        row = VerificationResult(
            incident_id=_uuid_or_none(incident_id),
            run_id=_uuid_or_none(run_id),
            plan_id=_uuid_or_none(plan_id),
            status=str(payload.get("status") or "pending"),
            checks=list(payload.get("checks") or []),
            passed_checks=int(payload.get("passed_checks") or 0),
            total_checks=int(payload.get("total_checks") or 0),
            confidence=float(payload.get("confidence") or 0.0),
            verified_at=_parse_dt(payload.get("verified_at")) or utcnow(),
        )
        self.session.add(row)
        await self.session.flush()
        return str(row.id)

    async def save_postmortem(
        self, incident_id: str, payload: dict[str, Any]
    ) -> str:
        inc = _uuid_or_none(incident_id)
        if inc is None:
            return ""
        existing = await self.session.scalar(
            select(Postmortem).where(Postmortem.incident_id == inc)
        )
        row = existing or Postmortem(incident_id=inc)
        if existing is None:
            self.session.add(row)
        row.summary = str(payload.get("summary") or "")
        row.root_cause = payload.get("root_cause")
        row.timeline = list(payload.get("timeline") or [])
        row.contributing_factors = list(payload.get("contributing_factors") or [])
        row.lessons_learned = list(payload.get("lessons_learned") or [])
        row.action_items = list(payload.get("action_items") or [])
        row.generated_by = str(payload.get("generated_by") or "agent")
        await self.session.flush()
        return str(row.id)

    async def postmortem(self, incident_id: Any) -> Postmortem | None:
        return await self.session.scalar(
            select(Postmortem).where(Postmortem.incident_id == _uuid_or_none(incident_id))
        )

    # ------------------------------------------------------------------
    # Incident state machine (the only way status may change)
    # ------------------------------------------------------------------
    async def set_incident_status(
        self,
        incident_id: str,
        status: str,
        *,
        actor: str = "agent",
        summary: str = "",
        stage: AgentStage | None = None,
    ) -> None:
        incident = await self.session.get(Incident, _uuid_or_none(incident_id))
        if incident is None:
            return
        try:
            target = IncidentStatus(str(status).upper())
        except ValueError:
            log_event("incident.invalid_status", incident_id=str(incident_id), status=status)
            return

        if incident.status == target.value:
            return  # idempotent — the agent re-asserts stage status on every node

        repo = IncidentRepository(self.session)
        try:
            await repo.transition(
                incident,
                target,
                actor=actor,
                actor_type=ActorType.AGENT if actor == "agent" else ActorType.HUMAN,
                summary=summary,
                data={"stage": stage.value if stage else None},
            )
        except InvalidStateTransitionError as exc:
            # Never kill a run over a status nit: record the rejection so the
            # mismatch is visible in the timeline and fixable. ``message`` is
            # dropped from the spread — log_event's first positional parameter
            # is already named ``message`` and a duplicate kwarg raises.
            fields = {k: str(v) for k, v in exc.to_dict().items() if k != "message"}
            log_event(
                "incident.transition_rejected",
                incident_id=str(incident.id),
                **fields,
            )
            await repo.append_event(
                incident.id,
                "incident.transition_rejected",
                f"{incident.status} → {target.value} rejected by state machine",
                actor=actor,
                actor_type=ActorType.SYSTEM,
                data={"status": incident.status, "target": target.value},
            )
            return

        if stage is not None:
            incident.current_stage = stage
        await self.session.flush()
        self._audit(
            action=f"incident.status.{target.value}",
            actor=actor,
            resource_type="incident",
            resource_id=str(incident.id),
            incident_id=incident.id,
            run_id=None,
            outcome="success",
            detail=summary,
        )

    def _audit(self, **kwargs: Any) -> None:
        self.session.add(AuditLog(**kwargs))


__all__ = ["AgentRunRepository"]
