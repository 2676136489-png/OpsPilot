"""Single source of truth for every status / stage / risk vocabulary.

The frontend imports the generated mirror of this file, so a typo can no
longer drift between the two sides of the wire.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class StrEnum(str, Enum):
    """Enum whose members compare equal to their plain string value."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


# ---------------------------------------------------------------------------
# Incident lifecycle
# ---------------------------------------------------------------------------


class IncidentStatus(StrEnum):
    """Backend-owned incident state machine.

    The frontend may never set this directly — every transition goes through
    :class:`IncidentStateMachine` and produces an IncidentEvent + AuditLog.
    """

    CREATED = "CREATED"
    TRIAGING = "TRIAGING"
    INVESTIGATING = "INVESTIGATING"
    DIAGNOSING = "DIAGNOSING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RECOVERING = "RECOVERING"
    ROLLING_BACK = "ROLLING_BACK"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"
    CLOSED = "CLOSED"


class IncidentSeverity(StrEnum):
    SEV1 = "SEV1"
    SEV2 = "SEV2"
    SEV3 = "SEV3"
    SEV4 = "SEV4"


class ServiceHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Agent runtime
# ---------------------------------------------------------------------------


class AgentStage(StrEnum):
    """The LangGraph nodes of the incident response workflow."""

    LOAD_CONTEXT = "load_context"
    TRIAGE = "triage"
    INVESTIGATION_PLANNER = "investigation_planner"
    PARALLEL_INVESTIGATION = "parallel_investigation"
    EVIDENCE_AGGREGATION = "evidence_aggregation"
    HYPOTHESIS_GENERATION = "hypothesis_generation"
    HYPOTHESIS_VERIFICATION = "hypothesis_verification"
    ROOT_CAUSE_DIAGNOSIS = "root_cause_diagnosis"
    RECOVERY_PLANNER = "recovery_planner"
    RISK_ASSESSMENT = "risk_assessment"
    HUMAN_APPROVAL = "human_approval"
    RECOVERY_EXECUTOR = "recovery_executor"
    #: Compensates a recovery that did not hold. A separate node because
    #: "the fix made it worse" is a different event from "the fix ran".
    ROLLBACK = "rollback"
    VERIFICATION = "verification"
    POSTMORTEM = "postmortem"


class AgentRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"


class ToolCallStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# Investigation objects
# ---------------------------------------------------------------------------


class EvidenceType(StrEnum):
    METRIC = "metric"
    LOG = "log"
    DEPLOYMENT = "deployment"
    COMMIT = "commit"
    RUNBOOK = "runbook"
    DEPENDENCY = "dependency"
    OBSERVATION = "observation"


class HypothesisStatus(StrEnum):
    PROPOSED = "proposed"
    TESTING = "testing"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class DiagnosisOutcome(StrEnum):
    """How an investigation ended — the honest answer, not always "solved".

    An incident response system that always produces a root cause is lying
    about its own confidence. These four outcomes are the complete set: the
    Agent either proved a cause, believes one without proof, ran out of
    evidence, or broke.
    """

    ROOT_CAUSE_CONFIRMED = "ROOT_CAUSE_CONFIRMED"
    ROOT_CAUSE_PROBABLE = "ROOT_CAUSE_PROBABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    INVESTIGATION_FAILED = "INVESTIGATION_FAILED"


class EvidenceRelevance(StrEnum):
    """How much an evidence item actually bears on the current question.

    Separate from ``confidence`` (how sure the *source* is) and ``severity``
    (how bad it looks). A perfectly measured, terrifying CPU number is
    irrelevant to a Redis outage, and the UI has to be able to say so.
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"


class EscalationReason(StrEnum):
    """Why a run stopped short of a resolution and handed over to a human."""

    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    TOOL_FAILURES = "TOOL_FAILURES"
    NO_HYPOTHESIS_CONFIRMED = "NO_HYPOTHESIS_CONFIRMED"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    CRITICAL_RISK_UNACTIONABLE = "CRITICAL_RISK_UNACTIONABLE"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    NODE_ERROR = "NODE_ERROR"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class PermissionLevel(StrEnum):
    READ_ONLY = "read_only"
    WRITE_EXTERNAL = "write_external"
    MUTATE_INFRA = "mutate_infra"
    DESTRUCTIVE = "destructive"


# ---------------------------------------------------------------------------
# Recovery / approval / verification
# ---------------------------------------------------------------------------


class RecoveryPlanStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class RecoveryActionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    #: The call went through but the fault is still present. Kept separate from
    #: FAILED because "the environment ignored the action" and "the action
    #: errored" call for different next steps — and because reporting a no-op
    #: as a success is how a recovery silently lies.
    INEFFECTIVE = "ineffective"
    FAILED = "failed"
    SKIPPED = "skipped"
    ROLLED_BACK = "rolled_back"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class VerificationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"


class ActorType(StrEnum):
    AGENT = "agent"
    HUMAN = "human"
    SYSTEM = "system"


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class EventType(StrEnum):
    """SSE / persistence event vocabulary (§9 of the spec)."""

    AGENT_STARTED = "agent.started"
    AGENT_STEP_STARTED = "agent.step.started"
    AGENT_STEP_COMPLETED = "agent.step.completed"
    AGENT_FAILED = "agent.failed"
    AGENT_COMPLETED = "agent.completed"

    INVESTIGATION_STARTED = "investigation.started"
    INVESTIGATION_PLAN_CREATED = "investigation.plan_created"

    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"

    EVIDENCE_CREATED = "evidence.created"
    HYPOTHESIS_CREATED = "hypothesis.created"
    HYPOTHESIS_UPDATED = "hypothesis.updated"
    #: A hypothesis was tested and falsified. Distinct from "updated" so the
    #: timeline can show the Agent discarding an explanation, which is the
    #: observable proof it is reasoning rather than reciting.
    HYPOTHESIS_REJECTED = "hypothesis.rejected"
    DIAGNOSIS_COMPLETED = "diagnosis.completed"

    BUDGET_EXHAUSTED = "agent.budget.exhausted"
    #: The run stopped short of a resolution and handed over to a human.
    RUN_ESCALATED = "agent.escalated"

    RECOVERY_PLAN_CREATED = "recovery.plan.created"
    RISK_ASSESSED = "risk.assessed"
    APPROVAL_REQUIRED = "approval.required"
    APPROVAL_DECIDED = "approval.decided"

    RECOVERY_STARTED = "recovery.started"
    RECOVERY_ACTION_COMPLETED = "recovery.action.completed"
    RECOVERY_COMPLETED = "recovery.completed"
    RECOVERY_FAILED = "recovery.failed"
    #: The compensation path: a recovery was executed, then undone.
    RECOVERY_ROLLBACK_STARTED = "recovery.rollback.started"
    RECOVERY_ROLLBACK_COMPLETED = "recovery.rollback.completed"

    VERIFICATION_STARTED = "verification.started"
    VERIFICATION_COMPLETED = "verification.completed"

    INCIDENT_STATUS_CHANGED = "incident.status_changed"
    INCIDENT_RESOLVED = "incident.resolved"
    POSTMORTEM_CREATED = "postmortem.created"

    HEARTBEAT = "heartbeat"
    STATE_SYNC = "state.sync"


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

ALLOWED_TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
    IncidentStatus.CREATED: frozenset(
        {IncidentStatus.TRIAGING, IncidentStatus.FAILED, IncidentStatus.CLOSED}
    ),
    IncidentStatus.TRIAGING: frozenset(
        {IncidentStatus.INVESTIGATING, IncidentStatus.FAILED, IncidentStatus.CLOSED}
    ),
    IncidentStatus.INVESTIGATING: frozenset(
        {
            IncidentStatus.INVESTIGATING,
            IncidentStatus.DIAGNOSING,
            IncidentStatus.ESCALATED,
            IncidentStatus.FAILED,
            IncidentStatus.CLOSED,
        }
    ),
    IncidentStatus.DIAGNOSING: frozenset(
        {
            IncidentStatus.INVESTIGATING,
            IncidentStatus.WAITING_APPROVAL,
            IncidentStatus.ESCALATED,
            IncidentStatus.RESOLVED,
            IncidentStatus.FAILED,
        }
    ),
    IncidentStatus.WAITING_APPROVAL: frozenset(
        {IncidentStatus.RECOVERING, IncidentStatus.FAILED, IncidentStatus.CLOSED}
    ),
    IncidentStatus.RECOVERING: frozenset(
        {
            IncidentStatus.VERIFYING,
            IncidentStatus.ROLLING_BACK,
            IncidentStatus.ESCALATED,
            IncidentStatus.FAILED,
        }
    ),
    # ROLLING_BACK → VERIFYING is the "rollback, then verify again" step;
    # → ESCALATED is what happens when the verification fails a second time.
    IncidentStatus.ROLLING_BACK: frozenset(
        {IncidentStatus.VERIFYING, IncidentStatus.ESCALATED, IncidentStatus.FAILED}
    ),
    IncidentStatus.VERIFYING: frozenset(
        {
            IncidentStatus.RESOLVED,
            IncidentStatus.ROLLING_BACK,
            IncidentStatus.ESCALATED,
            IncidentStatus.INVESTIGATING,
            IncidentStatus.FAILED,
        }
    ),
    # RESOLVED → INVESTIGATING supports "re-investigate": the UI exposes a
    # re-run button on resolved incidents and the agent must be able to
    # re-open them.
    IncidentStatus.RESOLVED: frozenset(
        {IncidentStatus.CLOSED, IncidentStatus.INVESTIGATING}
    ),
    IncidentStatus.FAILED: frozenset(
        {IncidentStatus.INVESTIGATING, IncidentStatus.CLOSED}
    ),
    # ESCALATED → INVESTIGATING lets an operator re-run the agent after the
    # blocking condition (budget, tooling) has been dealt with.
    IncidentStatus.ESCALATED: frozenset(
        {IncidentStatus.INVESTIGATING, IncidentStatus.CLOSED}
    ),
    IncidentStatus.CLOSED: frozenset(),
}

TERMINAL_STATUSES: frozenset[IncidentStatus] = frozenset({IncidentStatus.CLOSED})

#: Incident status implied by each agent stage. Used to keep Incident.status and
#: AgentRun.current_stage consistent without letting either layer guess.
STAGE_TO_INCIDENT_STATUS: dict[AgentStage, IncidentStatus] = {
    AgentStage.LOAD_CONTEXT: IncidentStatus.TRIAGING,
    AgentStage.TRIAGE: IncidentStatus.TRIAGING,
    AgentStage.INVESTIGATION_PLANNER: IncidentStatus.INVESTIGATING,
    AgentStage.PARALLEL_INVESTIGATION: IncidentStatus.INVESTIGATING,
    AgentStage.EVIDENCE_AGGREGATION: IncidentStatus.INVESTIGATING,
    AgentStage.HYPOTHESIS_GENERATION: IncidentStatus.INVESTIGATING,
    AgentStage.HYPOTHESIS_VERIFICATION: IncidentStatus.INVESTIGATING,
    AgentStage.ROOT_CAUSE_DIAGNOSIS: IncidentStatus.DIAGNOSING,
    AgentStage.RECOVERY_PLANNER: IncidentStatus.DIAGNOSING,
    AgentStage.RISK_ASSESSMENT: IncidentStatus.DIAGNOSING,
    AgentStage.HUMAN_APPROVAL: IncidentStatus.WAITING_APPROVAL,
    AgentStage.RECOVERY_EXECUTOR: IncidentStatus.RECOVERING,
    AgentStage.ROLLBACK: IncidentStatus.ROLLING_BACK,
    AgentStage.VERIFICATION: IncidentStatus.VERIFYING,
    AgentStage.POSTMORTEM: IncidentStatus.RESOLVED,
}


# ---------------------------------------------------------------------------
# Display labels
# ---------------------------------------------------------------------------
#
# The **wire** vocabulary is the enum value above and it stays English: every
# comparison, every transition table and every persisted row keys off it.
#
# These tables exist for the strings an operator actually reads. Incident
# summaries are free prose and are rendered verbatim in the timeline, so a
# summary reading "WAITING_APPROVAL → RECOVERING" is a leak rather than a
# status update — and one that hides an escalation the operator was meant to
# notice. The frontend keeps its own table in `i18n.ts` for badges: two
# consumers, two copies, deliberately, so rewording a badge does not require a
# backend deploy.

INCIDENT_STATUS_ZH: dict[str, str] = {
    IncidentStatus.CREATED.value: "已创建",
    IncidentStatus.TRIAGING.value: "分诊中",
    IncidentStatus.INVESTIGATING.value: "调查中",
    IncidentStatus.DIAGNOSING.value: "诊断中",
    IncidentStatus.WAITING_APPROVAL.value: "等待审批",
    IncidentStatus.RECOVERING.value: "恢复中",
    IncidentStatus.ROLLING_BACK.value: "回滚中",
    IncidentStatus.VERIFYING.value: "验证中",
    IncidentStatus.RESOLVED.value: "已解决",
    IncidentStatus.FAILED.value: "失败",
    IncidentStatus.ESCALATED.value: "已升级",
    IncidentStatus.CLOSED.value: "已关闭",
}

RISK_LEVEL_ZH: dict[str, str] = {
    RiskLevel.LOW.value: "低",
    RiskLevel.MEDIUM.value: "中",
    RiskLevel.HIGH.value: "高",
    RiskLevel.CRITICAL.value: "极高",
}

DIAGNOSIS_OUTCOME_ZH: dict[str, str] = {
    DiagnosisOutcome.ROOT_CAUSE_CONFIRMED.value: "根因已确认",
    DiagnosisOutcome.ROOT_CAUSE_PROBABLE.value: "根因很可能成立",
    DiagnosisOutcome.INSUFFICIENT_EVIDENCE.value: "证据不足",
    DiagnosisOutcome.INVESTIGATION_FAILED.value: "调查失败",
    # Not a member of `DiagnosisOutcome`. A run that stopped before it could
    # reach a verdict stores no outcome, and the read paths denormalise that as
    # `UNKNOWN`. It has to render as an honest Chinese sentence rather than as
    # the bare token — the frontend carries the same key for the same reason.
    "UNKNOWN": "未得出结论",
}


def _zh(table: dict[str, str], value: Any) -> str:
    """Chinese label for a wire value, tolerant of how the caller cased it.

    The argument arrives three different ways in practice: as the enum member,
    as its `.value`, or title-cased for display ("Escalated" rather than
    "ESCALATED"). A plain dict lookup misses the third form and returns the bare
    English token, which then lands inside an otherwise Chinese sentence —
    a leak that reads like a translation bug rather than a missing case.
    Normalising costs one `.upper()` because every wire value in these tables is
    upper-case by construction.
    """
    token = str(value)
    return table.get(token, table.get(token.upper(), token))


def zh_incident_status(value: Any) -> str:
    """Chinese label for an incident status; unknown values pass through."""
    return _zh(INCIDENT_STATUS_ZH, value)


def zh_risk(value: Any) -> str:
    return _zh(RISK_LEVEL_ZH, value)


def zh_outcome(value: Any) -> str:
    return _zh(DIAGNOSIS_OUTCOME_ZH, value)
