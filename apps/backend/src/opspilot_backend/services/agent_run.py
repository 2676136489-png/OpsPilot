"""Query-side service: assemble agent runs in the frontend contract.

The write path (:class:`AgentRuntimeService` + :class:`AgentRunRepository`)
stores normalised rows. The frontend expects the shape the old in-memory
streaming module returned — one ``AgentRun`` dict with evidence, hypotheses,
root cause, recovery plan, verification and the pending approval embedded.

This module is the single place that maps database rows onto that contract:

* run status uses the frontend vocabulary (``investigating`` /
  ``awaiting_approval`` / ``recovering``), not the internal enum values;
* evidence and hypotheses are keyed by their ``ref`` (``E001`` / ``H001``),
  which is what hypothesis ``evidence_refs`` point at;
* the recovery plan embeds its ordered actions as ``steps``;
* ``usage`` / ``budget`` report what the run actually spent against the limits
  it started with. These were persisted all along and simply not returned,
  which left the dashboard unable to answer "what did this investigation
  cost" — and left the frontend with nothing to show but a spinner.

No FastAPI imports here — controllers serialise, services decide.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_backend.domain.enums import (
    AgentRunStatus,
    ApprovalStatus,
    RecoveryActionStatus,
    RecoveryPlanStatus,
)
from opspilot_backend.models import (
    AgentEvent,
    AgentRun,
    AgentStep,
    Approval,
    Evidence,
    Hypothesis,
    Incident,
    IncidentEvent,
    RecoveryAction,
    RecoveryPlan,
    Service,
    ToolCall,
    VerificationResult,
)

# Internal AgentRunStatus → the six states the frontend knows about
# (apps/frontend/src/types/index.ts AgentRunStatus). CANCELLED has no
# frontend counterpart; surfacing it as failed keeps the UI honest about
# "this run produced no result".
_STATUS_MAP: dict[AgentRunStatus, str] = {
    AgentRunStatus.PENDING: "pending",
    AgentRunStatus.RUNNING: "investigating",
    AgentRunStatus.WAITING_APPROVAL: "awaiting_approval",
    AgentRunStatus.COMPLETED: "completed",
    AgentRunStatus.FAILED: "failed",
    AgentRunStatus.CANCELLED: "failed",
}

_TERMINAL_STATUSES = frozenset(
    {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED}
)

_RECOVERY_STEP_STATUS: dict[str, str] = {
    RecoveryActionStatus.PENDING.value: "pending",
    RecoveryActionStatus.RUNNING.value: "active",
    RecoveryActionStatus.SUCCEEDED.value: "done",
    RecoveryActionStatus.FAILED.value: "failed",
    RecoveryActionStatus.SKIPPED.value: "pending",
    RecoveryActionStatus.ROLLED_BACK.value: "failed",
    # A call that returned 200 and changed nothing. Rendering it as "done"
    # would report a recovery that never happened.
    RecoveryActionStatus.INEFFECTIVE.value: "ineffective",
}

# Kept 1:1 with ``RecoveryPlanStatus`` rather than remapped: the vocabulary is
# already lowercase and unambiguous, and inventing synonyms here would put a
# second naming scheme between the database and the UI.
_RECOVERY_PLAN_STATUS: dict[str, str] = {
    status.value: status.value for status in RecoveryPlanStatus
}


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else ""


class AgentRunQueryService:
    """Read model for agent runs — one method per REST endpoint."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def list_runs(self) -> list[dict[str, Any]]:
        stmt = select(AgentRun).order_by(AgentRun.created_at.desc())
        runs = list((await self.session.execute(stmt)).scalars())
        return [await self._assemble(run) for run in runs]

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        key = _uuid(run_id)
        if key is None:
            return None
        run = await self.session.get(AgentRun, key)
        if run is None:
            return None
        return await self._assemble(run)

    async def run_for_incident(self, incident_id: str) -> dict[str, Any] | None:
        """The incident's live run if it has one, else its most recent.

        This is what lets the dashboard *find* an existing investigation
        instead of starting a new one. Without it a page load had no way to
        discover an in-flight run, so the only move available was to POST and
        hope — which either conflicted with the running investigation or
        quietly created a second one.
        """
        key = _uuid(incident_id)
        if key is None:
            return None
        stmt = (
            select(AgentRun)
            .where(AgentRun.incident_id == key)
            .order_by(AgentRun.created_at.desc())
        )
        runs = list((await self.session.execute(stmt)).scalars())
        if not runs:
            return None
        # Newest first, but an *active* run outranks a newer finished one: a
        # re-investigation that is still working is the one to show. "Active"
        # is defined as the complement of terminal rather than a second set,
        # so a new status cannot end up in neither list.
        for run in runs:
            if run.status not in _TERMINAL_STATUSES:
                return await self._assemble(run)
        return await self._assemble(runs[0])

    async def stats(self) -> dict[str, Any]:
        runs = list(
            (await self.session.execute(select(AgentRun))).scalars()
        )
        verifications = list(
            (await self.session.execute(select(VerificationResult))).scalars()
        )

        total = len(runs)
        completed = sum(1 for r in runs if r.status == AgentRunStatus.COMPLETED)
        failed = sum(1 for r in runs if r.status == AgentRunStatus.FAILED)
        in_progress = sum(
            1 for r in runs if r.status in {AgentRunStatus.PENDING, AgentRunStatus.RUNNING}
        )
        awaiting = sum(1 for r in runs if r.status == AgentRunStatus.WAITING_APPROVAL)

        # "Decided" runs are the success-rate denominator — an in-flight
        # investigation must not count against the agent.
        decided = completed + failed
        success_rate = round(completed / decided, 4) if decided else None

        passed = sum(1 for v in verifications if v.status == "passed")
        verification_failed = sum(1 for v in verifications if v.status == "failed")
        attempted = passed + verification_failed
        recovery_rate = round(passed / attempted, 4) if attempted else None

        return {
            "total_runs": total,
            "completed": completed,
            "failed": failed,
            "in_progress": in_progress,
            "awaiting_approval": awaiting,
            "success_rate": success_rate,
            "recovery_attempted": attempted,
            "recovery_verified": passed,
            "recovery_rate": recovery_rate,
            # Explicit so the UI can render "no data yet" instead of a lying 0%.
            "has_data": decided > 0,
        }

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------
    async def _assemble(self, run: AgentRun) -> dict[str, Any]:
        incident_id = run.incident_id
        evidence = await self._evidence(run.id)
        hypotheses = await self._hypotheses(run.id)
        incident, service = await self._incident_with_service(incident_id)
        plan = await self._latest_plan(run.id)
        verification = await self._latest_verification(run.id)
        approval = await self._relevant_approval(run.id)
        api_status = _STATUS_MAP.get(run.status, "failed")

        return {
            "id": str(run.id),
            "incident_id": str(incident_id) if incident_id else "",
            "status": api_status,
            "current_node": run.current_stage.value if run.current_stage else None,
            "interrupted_at_node": (
                run.interrupted_at_stage.value if run.interrupted_at_stage else None
            ),
            "confidence": float(incident.confidence) if incident and incident.confidence else 0.0,
            "evidence": evidence,
            "hypotheses": hypotheses,
            "root_cause": self._root_cause(
                incident, service, await self._diagnosis_evidence(incident_id)
            ),
            "recovery_plan": await self._plan_dict(plan) if plan else None,
            "verification": self._verification_dict(verification),
            "approval_required": approval,
            "error": run.error,
            # How the diagnosis was reached, in the run's own vocabulary: the
            # four-state outcome plus the reason it stopped short when it did.
            "outcome": incident.diagnosis_outcome if incident else None,
            "reasoning_mode": run.reasoning_mode,
            "escalation_reason": (
                run.escalation_reason.value if run.escalation_reason else None
            ),
            # The run's own trace handles, so a UI can offer "open the trace"
            # without a second lookup by timestamp.
            "trace_id": run.trace_id or "",
            "request_id": run.request_id or "",
            "attempt": run.attempt,
            "max_attempts": run.max_attempts,
            "usage": self._usage(run),
            "budget": self._budget(run),
            "final_result": self._final_result(
                run, incident, api_status, plan, verification
            ),
            "created_at": _iso(run.created_at),
            "started_at": _iso(run.started_at),
            "ended_at": _iso(run.ended_at),
            "updated_at": _iso(run.ended_at or run.started_at or run.updated_at),
        }

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------
    @staticmethod
    def _usage(run: AgentRun) -> dict[str, Any]:
        """What the run spent. Measured wall-clock when the row has both ends."""
        duration_ms: int | None = None
        if run.started_at and run.ended_at:
            duration_ms = max(0, int((run.ended_at - run.started_at).total_seconds() * 1000))
        elif run.spent_seconds is not None:
            # A run that has not ended yet still knows how long it has been
            # going, which is the number an operator watching it wants.
            duration_ms = max(0, int(float(run.spent_seconds) * 1000))
        return {
            "tool_calls": int(run.spent_tool_calls or 0),
            "tokens": int(run.spent_tokens or 0),
            "retries": int(run.spent_retries or 0),
            "seconds": float(run.spent_seconds or 0.0),
            "duration_ms": duration_ms,
        }

    @staticmethod
    def _budget(run: AgentRun) -> dict[str, Any]:
        return {
            "tool_calls": int(run.budget_tool_calls or 0),
            "tokens": int(run.budget_tokens or 0),
            "seconds": float(run.budget_seconds or 0.0),
            "max_retries": int(run.budget_max_retries or 0),
            "max_parallel_tools": int(run.budget_max_parallel_tools or 0),
            # Distinguishes "gave up because it hit a wall" from "kept within
            # limits and still could not conclude" — different follow-ups.
            "exhausted": bool(run.budget_exhausted),
        }

    def _final_result(
        self,
        run: AgentRun,
        incident: Incident | None,
        api_status: str,
        plan: RecoveryPlan | None,
        verification: VerificationResult | None,
    ) -> dict[str, Any] | None:
        """The run's verdict, or ``None`` while it is still working.

        Present only for terminal runs so the UI can render "no result yet"
        without inferring it from an empty object.
        """
        if run.status not in _TERMINAL_STATUSES:
            return None
        return {
            "status": api_status,
            "outcome": incident.diagnosis_outcome if incident else None,
            "root_cause": (incident.root_cause if incident else None) or "",
            "category": (incident.root_cause_category if incident else None) or "unknown",
            "recovery_status": (
                _RECOVERY_PLAN_STATUS.get(str(plan.status or ""), None) if plan else None
            ),
            "verification_status": str(verification.status) if verification else None,
            "escalation_reason": (
                run.escalation_reason.value if run.escalation_reason else None
            ),
            "error": run.error,
        }

    async def _diagnosis_evidence(self, incident_id: Any) -> list[str]:
        """Evidence refs the recorded diagnosis rests on.

        Read back from the ``diagnosis.completed`` timeline entry rather than
        recomputed. Re-deriving it would mean re-implementing the hypothesis
        ranking a second time, and the moment those two drift the UI cites
        evidence the diagnosis never used.
        """
        if incident_id is None:
            return []
        stmt = (
            select(IncidentEvent)
            .where(
                IncidentEvent.incident_id == incident_id,
                IncidentEvent.event_type == "diagnosis.completed",
            )
            .order_by(IncidentEvent.created_at.desc())
            .limit(1)
        )
        row = await self.session.scalar(stmt)
        if row is None or not isinstance(row.data, dict):
            return []
        refs = row.data.get("evidence_refs")
        return [str(ref) for ref in refs] if isinstance(refs, list) else []

    # ------------------------------------------------------------------
    # Timeline readers — the real steps, not a reconstruction
    # ------------------------------------------------------------------
    async def timeline(self, run_id: str) -> dict[str, Any] | None:
        """Every node execution in order, with the tool calls it made.

        ``None`` means the run does not exist, which the controller turns into
        a 404; an empty ``steps`` list means it exists and has not run a node
        yet. The two are not the same answer and must not be flattened.

        ``orphaned_tool_calls`` is a sibling field rather than a synthetic
        extra entry in ``steps``: every entry in ``steps`` is a node that
        really ran, and a caller that renders one per row should not have to
        special-case an entry that is not. The list is normally empty — it only
        fills if a step row was deleted out from under its calls
        (``ON DELETE SET NULL``) — but a call that happened and cannot be
        attributed is still a call that happened.
        """
        key = _uuid(run_id)
        if key is None:
            return None
        if await self.session.get(AgentRun, key) is None:
            return None

        step_stmt = (
            select(AgentStep)
            .where(AgentStep.run_id == key)
            .order_by(AgentStep.sequence.asc(), AgentStep.started_at.asc())
        )
        steps = list((await self.session.execute(step_stmt)).scalars())

        call_stmt = (
            select(ToolCall)
            .where(ToolCall.run_id == key)
            .order_by(ToolCall.created_at.asc())
        )
        calls = list((await self.session.execute(call_stmt)).scalars())
        # Seed a bucket per step *before* bucketing the calls. Without this the
        # lookup below never matches and every single call is filed as an
        # orphan — which looks like a data problem but is an indexing one.
        by_step: dict[str, list[dict[str, Any]]] = {str(step.id): [] for step in steps}
        orphans: list[dict[str, Any]] = []
        for call in calls:
            payload = self._tool_call_dict(call)
            bucket = by_step.get(str(call.step_id)) if call.step_id else None
            if bucket is None:
                orphans.append(payload)
            else:
                bucket.append(payload)

        return {
            "steps": [
                {
                    "id": str(step.id),
                    "sequence": step.sequence,
                    "stage": step.stage.value if step.stage else "",
                    "status": str(step.status),
                    "attempt": step.attempt,
                    "duration_ms": step.duration_ms,
                    "error": step.error,
                    "started_at": _iso(step.started_at),
                    "ended_at": _iso(step.ended_at),
                    "trace_id": step.trace_id or "",
                    "span_id": step.span_id or "",
                    "tool_calls": by_step.get(str(step.id), []),
                }
                for step in steps
            ],
            "orphaned_tool_calls": orphans,
        }


    @staticmethod
    def _tool_call_dict(call: ToolCall) -> dict[str, Any]:
        return {
            "id": str(call.id),
            "tool_name": call.tool_name,
            "status": str(call.status),
            "arguments": dict(call.arguments or {}),
            "result": call.result,
            "risk_level": call.risk_level,
            "permission_level": call.permission_level,
            "error_code": call.error_code,
            "error_message": call.error_message,
            "attempt": call.attempt,
            "duration_ms": call.duration_ms,
            "transport": call.transport,
            "span_id": call.span_id or "",
            "created_at": _iso(call.created_at),
        }

    async def events(self, run_id: str, *, after_seq: int = 0, limit: int = 500):
        """Projected agent events (``seq``-ascending) for replay.

        Returns ``None`` when the run is unknown. Delegates to
        ``services.agent_stream`` so the REST replay and the SSE stream cannot
        disagree about what a client is allowed to see.
        """
        from opspilot_backend.services.agent_stream import project_event

        key = _uuid(run_id)
        if key is None:
            return None
        if await self.session.get(AgentRun, key) is None:
            return None
        stmt = (
            select(AgentEvent)
            .where(AgentEvent.run_id == key, AgentEvent.seq > max(0, int(after_seq)))
            .order_by(AgentEvent.seq.asc())
            .limit(max(1, int(limit)))
        )
        rows = list((await self.session.execute(stmt)).scalars())
        return [project_event(row) for row in rows]


    async def _evidence(self, run_id: Any) -> list[dict[str, Any]]:
        stmt = (
            select(Evidence)
            .where(Evidence.run_id == run_id)
            .order_by(Evidence.ref.asc())
        )
        rows = (await self.session.execute(stmt)).scalars()
        return [
            {
                # Frontend cross-references hypotheses.evidence_ids by ref.
                "id": row.ref,
                "type": row.type,
                "source": row.source,
                "service": row.service,
                "description": row.description or row.title,
                "timestamp": _iso(row.observed_at),
                "value": row.value,
                "severity": row.severity,
            }
            for row in rows
        ]

    async def _hypotheses(self, run_id: Any) -> list[dict[str, Any]]:
        stmt = (
            select(Hypothesis)
            .where(Hypothesis.run_id == run_id)
            .order_by(Hypothesis.ref.asc())
        )
        rows = (await self.session.execute(stmt)).scalars()
        return [
            {
                "id": row.ref,
                "description": row.statement,
                "confidence": float(row.confidence or 0.0),
                # evidence_refs holds the Evidence.ref values the frontend expects.
                "evidence_ids": list(row.evidence_refs or []),
                "reasoning": row.reasoning or "",
            }
            for row in rows
        ]

    async def _incident_with_service(
        self, incident_id: Any
    ) -> tuple[Incident | None, Service | None]:
        if incident_id is None:
            return None, None
        incident = await self.session.get(Incident, incident_id)
        service = (
            await self.session.get(Service, incident.service_id)
            if incident is not None and incident.service_id
            else None
        )
        return incident, service

    @staticmethod
    def _root_cause(
        incident: Incident | None,
        service: Service | None,
        evidence_ids: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """The verdict, with the evidence it cites and the reason it was reached.

        Both extras used to be returned as ``[]`` / ``""`` — placeholders that
        looked like data. The UI cross-references ``evidence_ids`` to highlight
        which findings support the cause, so an always-empty list read as "this
        diagnosis rests on nothing".
        """
        if incident is None or not incident.root_cause:
            return None
        return {
            "root_cause": incident.root_cause,
            "service": service.name if service else "",
            "confidence": float(incident.confidence or 0.0),
            "evidence_ids": list(evidence_ids or []),
            "reasoning_summary": incident.diagnosis_summary or "",
            "category": incident.root_cause_category or "unknown",
            "outcome": incident.diagnosis_outcome,
            "resolved_at": _iso(incident.resolved_at) if incident.resolved_at else None,
        }


    async def _latest_plan(self, run_id: Any) -> RecoveryPlan | None:
        stmt = (
            select(RecoveryPlan)
            .where(RecoveryPlan.run_id == run_id)
            .order_by(RecoveryPlan.created_at.desc())
            .limit(1)
        )
        return await self.session.scalar(stmt)

    async def _plan_dict(self, plan: RecoveryPlan) -> dict[str, Any]:
        stmt = (
            select(RecoveryAction)
            .where(RecoveryAction.plan_id == plan.id)
            .order_by(RecoveryAction.order.asc())
        )
        actions = (await self.session.execute(stmt)).scalars()
        return {
            "id": str(plan.id),
            "ref": plan.ref or "",
            "status": _RECOVERY_PLAN_STATUS.get(str(plan.status or ""), str(plan.status or "")),
            "risk_level": plan.risk_level,
            "steps": [self._action_dict(action) for action in actions],
            "summary": plan.rationale or plan.root_cause or "",
            "expected_impact": plan.expected_impact or "",
            "verification_criteria": list(plan.verification_criteria or []),
            "created_at": _iso(plan.created_at),
            "executed_at": _iso(plan.executed_at) if plan.executed_at else None,
        }

    @staticmethod
    def _action_dict(action: RecoveryAction) -> dict[str, Any]:
        """One planned action, including how it can be undone.

        The rollback fields are part of the plan, not an afterthought: an
        operator approving a high-risk action needs to see *before* approving
        that it is reversible and by what. They were previously dropped here,
        so the approval dialog could only say "restart something".
        """
        return {
            "id": action.ref,
            "order": action.order,
            "description": action.reason or action.tool_name,
            "action": action.tool_name,
            "status": _RECOVERY_STEP_STATUS.get(str(action.status or "pending"), "pending"),
            "target": action.target_service or "",
            "parameters": dict(action.parameters or {}),
            "risk_level": action.risk_level,
            "expected_impact": action.expected_impact or "",
            "approval_tier": action.approval_tier,
            "approval_status": action.approval_status,
            "required_permission": action.required_permission or "",
            "verification_strategy": action.verification_strategy or "",
            "rollback_tool": action.rollback_tool,
            # ``None`` while the action has not run — distinct from ``False``,
            # which is "it ran and the environment did not change".
            "effective": action.effective,
            "error": action.error,
            "executed_by": action.executed_by,
        }


    async def _latest_verification(self, run_id: Any) -> VerificationResult | None:
        stmt = (
            select(VerificationResult)
            .where(VerificationResult.run_id == run_id)
            .order_by(VerificationResult.verified_at.desc())
            .limit(1)
        )
        return await self.session.scalar(stmt)

    @staticmethod
    def _verification_dict(row: VerificationResult | None) -> dict[str, Any] | None:
        if row is None:
            return None
        checks = list(row.checks or [])
        return {
            "id": str(row.id),
            "status": str(row.status),
            "description": f"{row.passed_checks}/{row.total_checks} 项检查通过",
            # Flat, not wrapped in a fake ``metrics_checked`` object: the
            # frontend renders each check, and a needless nesting level was
            # only ever an obstacle.
            "checks": checks,
            "passed_checks": row.passed_checks,
            "total_checks": row.total_checks,
            # How sure the verification itself is. A pass with 0.4 confidence
            # is not the same evidence as a pass with 0.99.
            "confidence": float(row.confidence or 0.0),
            "plan_id": str(row.plan_id) if row.plan_id else None,
            "timestamp": _iso(row.verified_at),
        }


    async def _relevant_approval(self, run_id: Any) -> dict[str, Any] | None:
        """The approval the UI should surface: pending first, newest overall."""
        stmt = (
            select(Approval)
            .where(Approval.run_id == run_id)
            .order_by(
                # Pending rows first so an undecided request is never buried
                # under an older decided one; newest within each group.
                Approval.status == ApprovalStatus.PENDING.value,
                Approval.created_at.desc(),
            )
            .limit(1)
        )
        approval = await self.session.scalar(stmt)
        if approval is None:
            return None
        return {
            "id": str(approval.id),
            "agent_run_id": str(approval.run_id) if approval.run_id else "",
            "type": "recovery",
            "description": approval.requested_reason or "",
            "status": approval.status,
            "requested_at": _iso(approval.created_at),
            "responded_at": _iso(approval.decided_at) if approval.decided_at else None,
            "reason": approval.decision_note,
        }


def _uuid(value: Any) -> Any:
    import uuid as _uuid_mod

    if isinstance(value, _uuid_mod.UUID):
        return value
    try:
        return _uuid_mod.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


__all__ = ["AgentRunQueryService"]
