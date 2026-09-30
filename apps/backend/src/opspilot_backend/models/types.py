"""Shared SQLAlchemy Enum instances.

Postgres refuses to create the same named type twice, so every model must
reuse the *same* ``SAEnum`` object instead of constructing a new one per
column. All of these store the enum's **value** (lower/upper case as declared)
so SQLite and Postgres agree on the wire.
"""

from __future__ import annotations

from sqlalchemy import Enum as SAEnum

from opspilot_backend.domain.enums import (
    ActorType,
    AgentRunStatus,
    AgentStage,
    ApprovalStatus,
    DiagnosisOutcome,
    EscalationReason,
    EvidenceRelevance,
    EvidenceType,
    HypothesisStatus,
    IncidentSeverity,
    IncidentStatus,
    RecoveryActionStatus,
    RecoveryPlanStatus,
    RiskLevel,
    ServiceHealth,
    StepStatus,
    ToolCallStatus,
    VerificationStatus,
)


def _e(enum_cls: type, name: str) -> SAEnum:
    return SAEnum(
        enum_cls,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        native_enum=True,
        create_constraint=False,
    )


INCIDENT_STATUS = _e(IncidentStatus, "incident_status")
INCIDENT_SEVERITY = _e(IncidentSeverity, "incident_severity")
AGENT_STAGE = _e(AgentStage, "agent_stage")
AGENT_RUN_STATUS = _e(AgentRunStatus, "agent_run_status")
STEP_STATUS = _e(StepStatus, "step_status")
TOOL_CALL_STATUS = _e(ToolCallStatus, "tool_call_status")
SERVICE_HEALTH = _e(ServiceHealth, "service_health")
EVIDENCE_TYPE = _e(EvidenceType, "evidence_type")
HYPOTHESIS_STATUS = _e(HypothesisStatus, "hypothesis_status")
RISK_LEVEL = _e(RiskLevel, "risk_level")
RECOVERY_PLAN_STATUS = _e(RecoveryPlanStatus, "recovery_plan_status")
RECOVERY_ACTION_STATUS = _e(RecoveryActionStatus, "recovery_action_status")
APPROVAL_STATUS = _e(ApprovalStatus, "approval_status")
VERIFICATION_STATUS = _e(VerificationStatus, "verification_status")
ACTOR_TYPE = _e(ActorType, "actor_type")
DIAGNOSIS_OUTCOME = _e(DiagnosisOutcome, "diagnosis_outcome")
EVIDENCE_RELEVANCE = _e(EvidenceRelevance, "evidence_relevance")
ESCALATION_REASON = _e(EscalationReason, "escalation_reason")

__all__ = [
    "INCIDENT_STATUS",
    "INCIDENT_SEVERITY",
    "AGENT_STAGE",
    "AGENT_RUN_STATUS",
    "STEP_STATUS",
    "TOOL_CALL_STATUS",
    "SERVICE_HEALTH",
    "EVIDENCE_TYPE",
    "HYPOTHESIS_STATUS",
    "RISK_LEVEL",
    "RECOVERY_PLAN_STATUS",
    "RECOVERY_ACTION_STATUS",
    "APPROVAL_STATUS",
    "VERIFICATION_STATUS",
    "ACTOR_TYPE",
    "DIAGNOSIS_OUTCOME",
    "EVIDENCE_RELEVANCE",
    "ESCALATION_REASON",
]
