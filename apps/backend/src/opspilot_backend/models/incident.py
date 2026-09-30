"""Incident aggregate + business timeline."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    # Resolve the string relationship targets for type checkers. SQLAlchemy
    # wires these up itself at runtime by name, so these imports exist only for
    # static analysis — hence TYPE_CHECKING, to keep the import graph acyclic.
    from opspilot_backend.models.agent import AgentRun
    from opspilot_backend.models.investigation import Evidence, Hypothesis
    from opspilot_backend.models.knowledge import Postmortem
    from opspilot_backend.models.recovery import RecoveryPlan, VerificationResult
    from opspilot_backend.models.service import Service

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from opspilot_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow
from opspilot_backend.domain.enums import (
    ActorType,
    AgentStage,
    IncidentSeverity,
    IncidentStatus,
)
from opspilot_backend.models.types import (
    ACTOR_TYPE,
    AGENT_STAGE,
    DIAGNOSIS_OUTCOME,
    ESCALATION_REASON,
    INCIDENT_SEVERITY,
    INCIDENT_STATUS,
)


class Incident(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The single aggregate the whole system rotates around."""

    __tablename__ = "incidents"

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    service_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("services.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    severity: Mapped[IncidentSeverity] = mapped_column(
        INCIDENT_SEVERITY,
        nullable=False,
        default=IncidentSeverity.SEV3,
        index=True,
    )
    status: Mapped[IncidentStatus] = mapped_column(
        INCIDENT_STATUS,
        nullable=False,
        default=IncidentStatus.CREATED,
        index=True,
    )
    current_stage: Mapped[Optional[AgentStage]] = mapped_column(
        AGENT_STAGE, nullable=True
    )
    # Simulator scenario key — null once real providers are wired in.
    scenario: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    root_cause: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    root_cause_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    #: Did the Agent *prove* a cause, believe one, or run out of road? This is
    #: the difference between "we fixed it" and "we think we fixed it".
    diagnosis_outcome: Mapped[Optional[str]] = mapped_column(
        DIAGNOSIS_OUTCOME, nullable=True
    )
    diagnosis_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Set when the run handed the incident to a human instead of closing it.
    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    escalation_reason: Mapped[Optional[str]] = mapped_column(
        ESCALATION_REASON, nullable=True
    )
    escalation_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    detected_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default=func.now()
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    service: Mapped["Service"] = relationship("Service", back_populates="incidents")
    events: Mapped[list["IncidentEvent"]] = relationship(
        "IncidentEvent", back_populates="incident", cascade="all, delete-orphan"
    )
    agent_runs: Mapped[list["AgentRun"]] = relationship(
        "AgentRun", back_populates="incident", cascade="all, delete-orphan"
    )
    evidence: Mapped[list["Evidence"]] = relationship(
        "Evidence", back_populates="incident", cascade="all, delete-orphan"
    )
    hypotheses: Mapped[list["Hypothesis"]] = relationship(
        "Hypothesis", back_populates="incident", cascade="all, delete-orphan"
    )
    recovery_plans: Mapped[list["RecoveryPlan"]] = relationship(
        "RecoveryPlan", back_populates="incident", cascade="all, delete-orphan"
    )
    verification_results: Mapped[list["VerificationResult"]] = relationship(
        "VerificationResult", back_populates="incident", cascade="all, delete-orphan"
    )
    postmortem: Mapped[Optional["Postmortem"]] = relationship(
        "Postmortem",
        back_populates="incident",
        cascade="all, delete-orphan",
        uselist=False,
    )


class IncidentEvent(UUIDPrimaryKeyMixin, Base):
    """Immutable business timeline — every status change appends one row."""

    __tablename__ = "incident_events"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    actor_type: Mapped[ActorType] = mapped_column(
        ACTOR_TYPE, nullable=False, default=ActorType.SYSTEM
    )
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    from_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    to_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, index=True, default=utcnow, server_default=func.now()
    )

    incident: Mapped[Incident] = relationship("Incident", back_populates="events")


__all__ = ["Incident", "IncidentEvent"]
