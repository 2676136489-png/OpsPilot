"""Recovery plan / actions / approval / verification."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    # String relationship target — resolved at runtime by SQLAlchemy.
    from opspilot_backend.models.incident import Incident

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from opspilot_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from opspilot_backend.models.types import (
    APPROVAL_STATUS,
    RECOVERY_ACTION_STATUS,
    RECOVERY_PLAN_STATUS,
    RISK_LEVEL,
    VERIFICATION_STATUS,
)


class RecoveryPlan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "recovery_plans"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Human-friendly plan identifier (RP-xxxxxxxx), stable across API reads.
    ref: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    root_cause: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    risk_level: Mapped[str] = mapped_column(
        RISK_LEVEL, nullable=False, default="MEDIUM"
    )
    status: Mapped[str] = mapped_column(
        RECOVERY_PLAN_STATUS, nullable=False, default="draft", index=True
    )
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    expected_impact: Mapped[str] = mapped_column(Text, nullable=False, default="")
    verification_criteria: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="agent")
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    incident: Mapped["Incident"] = relationship(
        "Incident", back_populates="recovery_plans"
    )
    actions: Mapped[list["RecoveryAction"]] = relationship(
        "RecoveryAction", back_populates="plan", cascade="all, delete-orphan"
    )


class RecoveryAction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One executable step. High-risk actions cannot run without an Approval."""

    __tablename__ = "recovery_actions"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("recovery_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Business identifier (A01, A02...). The agent cites it in events before
    # the row has an id, so it must be queryable — otherwise approval and
    # execution results could not be written back to the right action.
    ref: Mapped[str] = mapped_column(String(16), nullable=False, default="", index=True)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    target_service: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    risk_level: Mapped[str] = mapped_column(RISK_LEVEL, nullable=False, default="MEDIUM")
    expected_impact: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # --- the action's contract -------------------------------------------
    # A proposal that does not state how it will be verified, how it will be
    # undone and what permission it needs is not reviewable by the human who
    # has to approve it.
    verification_strategy: Mapped[str] = mapped_column(Text, nullable=False, default="")
    required_permission: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    rollback_tool: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    rollback_parameters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    rollback_strategy: Mapped[str] = mapped_column(Text, nullable=False, default="")
    approval_tier: Mapped[str] = mapped_column(String(32), nullable=False, default="approval")
    approval_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_required")
    # False when the tool returned success but the fault survived it.
    effective: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    status: Mapped[str] = mapped_column(
        RECOVERY_ACTION_STATUS, nullable=False, default="pending", index=True
    )
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tool_call_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tool_calls.id", ondelete="SET NULL"),
        nullable=True,
    )
    executed_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    plan: Mapped[RecoveryPlan] = relationship("RecoveryPlan", back_populates="actions")
    approval: Mapped[Optional["Approval"]] = relationship(
        "Approval", back_populates="action", uselist=False, cascade="all, delete-orphan"
    )


class Approval(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Human-in-the-loop gate. Nothing auto-approves this — ever."""

    __tablename__ = "approvals"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("recovery_actions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_level: Mapped[str] = mapped_column(RISK_LEVEL, nullable=False)
    status: Mapped[str] = mapped_column(
        APPROVAL_STATUS, nullable=False, default="pending", index=True
    )
    requested_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False, default="agent")
    decided_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    decision_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    action: Mapped[Optional[RecoveryAction]] = relationship(
        "RecoveryAction", back_populates="approval"
    )


class VerificationResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Post-recovery health probe outcome."""

    __tablename__ = "verification_results"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    plan_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("recovery_plans.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        VERIFICATION_STATUS, nullable=False, default="pending", index=True
    )
    checks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    passed_checks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_checks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    incident: Mapped["Incident"] = relationship(
        "Incident", back_populates="verification_results"
    )


__all__ = ["RecoveryPlan", "RecoveryAction", "Approval", "VerificationResult"]
