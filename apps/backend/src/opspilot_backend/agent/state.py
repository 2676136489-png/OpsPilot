"""Layered, validated Agent state.

Replaces the old flat ``TypedDict(total=False)`` with 30 loosely-typed keys.
LangGraph validates every node's return value against this model, so a node
that forgets a required field fails loudly instead of silently dropping it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from opspilot_backend.domain.enums import (
    AgentStage,
    DiagnosisOutcome,
    EvidenceRelevance,
    HypothesisStatus,
    IncidentSeverity,
    RiskLevel,
)

#: What a node hands the router. ``rollback`` / ``reverify`` are the
#: compensation path: a recovery that did not hold asks to be undone, and the
#: compensation asks to be measured before anything else is decided.
Decision = Literal[
    "continue",
    "replan",
    "diagnose",
    "stop",
    "approve",
    "verify",
    "rollback",
    "reverify",
    "close",
    "escalate",
]


class _Base(BaseModel):
    model_config = ConfigDict(validate_assignment=False, arbitrary_types_allowed=True)


class IncidentRef(_Base):
    incident_id: str
    service: str = ""
    severity: str = IncidentSeverity.SEV3.value
    title: str = ""
    scenario: str | None = None
    detected_at: str | None = None


class InvestigationStep(_Base):
    """One planned tool invocation."""

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    status: Literal["pending", "done", "skipped", "failed"] = "pending"
    # Why this step was chosen — shown in the UI as the agent's plan.
    depends_on_signal: str | None = None


class InvestigationPlan(_Base):
    steps: list[InvestigationStep] = Field(default_factory=list)
    iteration: int = 0
    max_iterations: int = 4
    # Lines of enquiry already answered. Keyed by dimension ("resource_metrics")
    # or "dimension:target" when the probe ran against a dependency.
    ruled_out: list[str] = Field(default_factory=list)
    # Explanations the Agent has already tested and discarded. The next round
    # must not propose them again — otherwise "generate a new hypothesis" is
    # just re-proposing the same one with a fresh ref.
    rejected_domains: list[str] = Field(default_factory=list)
    # How many times the Agent has thrown away its leading explanation.
    hypothesis_rounds: int = 0
    # Evidence count when this round was planned. If the round comes back with
    # the same number, the probes taught the Agent nothing and looping again
    # would just spend the budget on the same questions.
    last_round_evidence: int = 0


class EvidenceItem(_Base):
    """A traceable fact. ``ref`` (E001…) is what a diagnosis cites.

    ``value`` is whatever the tool returned; ``normalized`` is the same fact
    reduced to comparable fields. Reasoning runs on the normalized form, so a
    metric that arrives as ``latency_p95`` from one tool and ``latency_p95_ms``
    from another is still the same number.
    """

    ref: str
    type: str
    source: str
    service: str = ""
    title: str
    description: str = ""
    value: dict[str, Any] | None = None
    normalized: dict[str, Any] | None = None
    severity: str = "medium"
    confidence: float = 0.5
    # How much this row bears on the question being asked. Set once the
    # hypotheses are ranked, not at collection time — evidence collected before
    # anything is ranked is context, not proof.
    relevance: str = EvidenceRelevance.MEDIUM.value
    relevance_reason: str = ""
    tool_call_id: str | None = None
    observed_at: str | None = None


class HypothesisItem(_Base):
    ref: str
    statement: str
    category: str = "unknown"
    #: Which fault domain this hypothesis came from. The same domain must not
    #: be re-proposed after it has been rejected.
    domain: str = ""
    confidence: float = 0.0
    status: str = HypothesisStatus.PROPOSED.value
    reasoning: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class DiagnosisInfo(_Base):
    root_cause: str = ""
    category: str = "unknown"
    #: The fault domain the winning hypothesis came from, kept so the recovery
    #: layer can resolve which component to act on.
    domain: str = ""
    confidence: float = 0.0
    # Mandatory: a root cause without cited evidence is not a diagnosis.
    evidence_refs: list[str] = Field(default_factory=list)
    reasoning_summary: str = ""
    decided_at: str | None = None
    #: CONFIRMED / PROBABLE / INSUFFICIENT_EVIDENCE / INVESTIGATION_FAILED.
    #: "unknown" is not an acceptable answer — the run has to say which kind
    #: of not-knowing it is in, because they imply different next steps.
    outcome: str = DiagnosisOutcome.INSUFFICIENT_EVIDENCE.value
    #: Why the Agent stopped without an answer, when it did.
    escalation_reason: str = ""


class RecoveryActionItem(_Base):
    """A *proposed* change. Nothing here has been applied yet.

    The Agent never mutates an incident, a service or a database itself — it
    fills this in and the domain layer decides whether the proposal becomes a
    state change.
    """

    ref: str
    tool: str
    order: int = 0
    target_service: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    risk_level: str = RiskLevel.MEDIUM.value
    expected_impact: str = ""
    #: Every action declares how it will be checked and how it will be undone.
    verification_strategy: str = ""
    required_permission: str = ""
    rollback_tool: str | None = None
    rollback_parameters: dict[str, Any] = Field(default_factory=dict)
    rollback_strategy: str = ""
    #: auto / approval / approval_and_reverify / manual_only
    approval_tier: str = "approval"
    approval_status: str = "not_required"
    status: str = "pending"
    #: False when the tool succeeded but the fault is still present — an action
    #: that ran and changed nothing. Distinct from an error, and the more
    #: common way a recovery quietly fails.
    effective: bool | None = None
    error: str | None = None


class RecoveryInfo(_Base):
    plan_ref: str | None = None
    actions: list[RecoveryActionItem] = Field(default_factory=list)
    risk_level: str = RiskLevel.LOW.value
    status: str = "none"
    rationale: str = ""
    expected_impact: str = ""
    verification_criteria: list[str] = Field(default_factory=list)
    approval_id: str | None = None
    approval_status: str = "none"
    requires_approval: bool = False
    #: Which gate this plan sits behind, and whether a second probe is required.
    approval_tier: str = "auto"
    requires_reverification: bool = False
    manual_only: bool = False
    executed_refs: list[str] = Field(default_factory=list)
    #: The action that actually changed the observed state, if any.
    effective_ref: str | None = None
    #: Compensating actions run after a failure.
    rollback_refs: list[str] = Field(default_factory=list)
    rollback_outcome: str = ""
    escalation_reason: str = ""


class VerificationInfo(_Base):
    status: str = "pending"
    checks: list[dict[str, Any]] = Field(default_factory=list)
    passed_checks: int = 0
    total_checks: int = 0
    verified_at: str | None = None


class ErrorRecord(_Base):
    stage: str
    error_type: str
    message: str
    at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class ExecutionInfo(_Base):
    completed_stages: list[str] = Field(default_factory=list)
    tool_call_refs: list[str] = Field(default_factory=list)
    errors: list[ErrorRecord] = Field(default_factory=list)


class MetaInfo(_Base):
    run_id: str = ""
    current_stage: str = AgentStage.LOAD_CONTEXT.value
    attempt: int = 1
    reasoning_mode: str = "deterministic"
    started_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    terminal_reason: str | None = None


class IncidentState(_Base):
    """Top-level graph state.

    Grouped rather than flat, and every field is typed — which is what makes
    "each node has a defined input / output / state update" enforceable.
    """

    incident: IncidentRef
    plan: InvestigationPlan = Field(default_factory=InvestigationPlan)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    hypotheses: list[HypothesisItem] = Field(default_factory=list)
    diagnosis: Optional[DiagnosisInfo] = None
    recovery: RecoveryInfo = Field(default_factory=RecoveryInfo)
    verification: VerificationInfo = Field(default_factory=VerificationInfo)
    postmortem_ref: str | None = None
    execution: ExecutionInfo = Field(default_factory=ExecutionInfo)
    meta: MetaInfo = Field(default_factory=MetaInfo)

    # Routing signal set by nodes and read by the conditional edges.
    decision: Decision = "continue"

    def next_evidence_ref(self) -> str:
        """Max-scan, not ``len()+1``: a node may create several items before
        its state update lands, and sparse refs must never collide."""
        highest = 0
        for item in self.evidence:
            digits = item.ref[1:] if item.ref.startswith("E") else ""
            if digits.isdigit():
                highest = max(highest, int(digits))
        return f"E{highest + 1:03d}"

    def next_hypothesis_ref(self) -> str:
        highest = 0
        for item in self.hypotheses:
            digits = item.ref[1:] if item.ref.startswith("H") else ""
            if digits.isdigit():
                highest = max(highest, int(digits))
        return f"H{highest + 1:03d}"

    def evidence_by_ref(self, ref: str) -> EvidenceItem | None:
        for item in self.evidence:
            if item.ref == ref:
                return item
        return None


__all__ = [
    "IncidentState",
    "IncidentRef",
    "InvestigationPlan",
    "InvestigationStep",
    "EvidenceItem",
    "HypothesisItem",
    "DiagnosisInfo",
    "RecoveryInfo",
    "RecoveryActionItem",
    "VerificationInfo",
    "ExecutionInfo",
    "MetaInfo",
    "ErrorRecord",
]
