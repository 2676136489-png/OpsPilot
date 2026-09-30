"""Evidence + Hypothesis — the traceable backbone of every diagnosis."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    # String relationship target — resolved at runtime by SQLAlchemy.
    from opspilot_backend.models.incident import Incident

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from opspilot_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow
from opspilot_backend.models.types import (
    EVIDENCE_RELEVANCE,
    EVIDENCE_TYPE,
    HYPOTHESIS_STATUS,
)


class Evidence(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single traceable fact produced by a Tool Call.

    ``source_tool_call_id`` is the link that makes *Diagnosis → Evidence →
    Tool Result* auditable: a root cause may only cite evidence ids that
    exist, and every evidence row points back at the tool call that made it.
    """

    __tablename__ = "evidence"

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
    tool_call_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tool_calls.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Human-friendly, stable within an incident: E001, E002, ...
    ref: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    type: Mapped[str] = mapped_column(EVIDENCE_TYPE, nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    service: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: What the tool actually returned, verbatim.
    value: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    #: The same fact reduced to comparable fields (metric/value/threshold).
    #: Reasoning runs on this, not on the raw payload shape.
    normalized: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    #: How much this item bears on the question being asked right now.
    relevance: Mapped[str] = mapped_column(
        EVIDENCE_RELEVANCE, nullable=False, default="MEDIUM"
    )
    #: Why it is relevant — the sentence a reviewer reads to decide whether
    #: the Agent's reasoning holds.
    relevance_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    observed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default=func.now()
    )

    incident: Mapped["Incident"] = relationship("Incident", back_populates="evidence")


class Hypothesis(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A falsifiable explanation, always backed by evidence ids."""

    __tablename__ = "hypotheses"

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
    ref: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(
        HYPOTHESIS_STATUS, nullable=False, default="proposed", index=True
    )
    reasoning: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # JSON array of Evidence.ref (plus ids) — denormalised for fast reads; the
    # authoritative link lives in hypothesis_evidence_links.
    evidence_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    verified_by_tool_calls: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )

    incident: Mapped["Incident"] = relationship("Incident", back_populates="hypotheses")


class HypothesisEvidenceLink(Base):
    """Association table: a hypothesis cites N evidence items."""

    __tablename__ = "hypothesis_evidence_links"

    hypothesis_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("hypotheses.id", ondelete="CASCADE"),
        primary_key=True,
    )
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("evidence.id", ondelete="CASCADE"),
        primary_key=True,
    )


__all__ = ["Evidence", "Hypothesis", "HypothesisEvidenceLink"]
