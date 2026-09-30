"""Agent runtime persistence: runs, steps, tool calls, streamable events."""

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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from opspilot_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow
from opspilot_backend.domain.enums import (
    AgentRunStatus,
    AgentStage,
    EscalationReason,
    StepStatus,
    ToolCallStatus,
)
from opspilot_backend.models.types import (
    AGENT_RUN_STATUS,
    AGENT_STAGE,
    ESCALATION_REASON,
    STEP_STATUS,
    TOOL_CALL_STATUS,
)


class AgentRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One execution of the incident response workflow for one incident."""

    __tablename__ = "agent_runs"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[AgentRunStatus] = mapped_column(
        AGENT_RUN_STATUS,
        nullable=False,
        default=AgentRunStatus.PENDING,
        index=True,
    )
    current_stage: Mapped[Optional[AgentStage]] = mapped_column(
        AGENT_STAGE, nullable=True
    )
    # LangGraph thread id — the checkpoint key that makes resume possible.
    thread_id: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    reasoning_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="deterministic"
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    interrupted_at_stage: Mapped[Optional[AgentStage]] = mapped_column(
        AGENT_STAGE, nullable=True
    )
    interrupt_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # --- Observability -------------------------------------------------
    # One trace per run, so a failure can be followed HTTP → node → tool →
    # simulator without correlating timestamps by hand.
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    # --- Investigation budget -------------------------------------------
    # The Agent is not allowed to investigate forever. These are the limits it
    # started with and what it actually spent; hitting a limit escalates
    # instead of producing a guess.
    budget_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=25)
    budget_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=300.0)
    budget_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=50_000)
    budget_max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    budget_max_parallel_tools: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    spent_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spent_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spent_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spent_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    budget_exhausted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    escalation_reason: Mapped[Optional[EscalationReason]] = mapped_column(
        ESCALATION_REASON, nullable=True
    )

    incident: Mapped["Incident"] = relationship("Incident", back_populates="agent_runs")
    steps: Mapped[list["AgentStep"]] = relationship(
        "AgentStep", back_populates="run", cascade="all, delete-orphan"
    )
    events: Mapped[list["AgentEvent"]] = relationship(
        "AgentEvent", back_populates="run", cascade="all, delete-orphan"
    )


class AgentStep(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One node execution inside a run (with retry bookkeeping)."""

    __tablename__ = "agent_steps"

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stage: Mapped[AgentStage] = mapped_column(AGENT_STAGE, nullable=False, index=True)
    status: Mapped[StepStatus] = mapped_column(
        STEP_STATUS,
        nullable=False,
        default=StepStatus.PENDING,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    output: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    span_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    run: Mapped[AgentRun] = relationship("AgentRun", back_populates="steps")
    tool_calls: Mapped[list["ToolCall"]] = relationship(
        "ToolCall", back_populates="step", cascade="all, delete-orphan"
    )


class ToolCall(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single Tool Layer invocation, fully audited."""

    __tablename__ = "tool_calls"

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_steps.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    arguments: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    status: Mapped[ToolCallStatus] = mapped_column(
        TOOL_CALL_STATUS,
        nullable=False,
        default=ToolCallStatus.PENDING,
    )
    risk_level: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    permission_level: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Idempotency: sha256(tool_name + canonical args). Unique → replays are safe.
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    transport: Mapped[str] = mapped_column(String(32), nullable=False, default="inprocess")
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    span_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    run: Mapped[AgentRun] = relationship("AgentRun")
    step: Mapped[Optional[AgentStep]] = relationship(
        "AgentStep", back_populates="tool_calls"
    )


class AgentEvent(Base):
    """Persisted SSE event.

    The auto-incrementing integer ``seq`` is what makes ``Last-Event-ID``
    replay possible: a reconnecting client asks for everything after the id it
    last saw and the server replays from this table rather than an in-memory
    ring buffer.
    """

    __tablename__ = "agent_events"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    incident_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    stage: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, index=True, default=utcnow, server_default=func.now()
    )

    run: Mapped[AgentRun] = relationship("AgentRun", back_populates="events")


class AgentCheckpoint(Base):
    """LangGraph checkpoint persisted in the database.

    Without this table an Agent Run dies with the process: closing the browser
    tab at ``WAITING_APPROVAL`` would lose the whole investigation and the
    only way forward would be to start again from scratch.
    """

    __tablename__ = "agent_checkpoints"

    thread_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(
        String(128), primary_key=True, default=""
    )
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parent_checkpoint_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False, default="json")
    checkpoint: Mapped[str] = mapped_column(Text, nullable=False)
    # ``metadata`` is reserved by Declarative — the column keeps the name,
    # the attribute does not.
    meta: Mapped[Optional[dict]] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, index=True, default=utcnow, server_default=func.now()
    )


class AgentCheckpointWrite(Base):
    """Pending writes attached to a checkpoint (needed for interrupt/resume)."""

    __tablename__ = "agent_checkpoint_writes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    checkpoint_ns: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    checkpoint_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False, default="json")
    blob: Mapped[str] = mapped_column(Text, nullable=False)


class TraceSpan(UUIDPrimaryKeyMixin, Base):
    """One recorded span of work.

    Persisted so the Observability page can render
    ``HTTP → Agent Run → Node → Tool → Simulator`` as a tree instead of five
    unrelated log streams the operator has to join by timestamp.
    """

    __tablename__ = "trace_spans"

    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    span_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    parent_span_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="internal")
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    incident_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    attributes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default=func.now()
    )


__all__ = [
    "AgentRun",
    "AgentStep",
    "ToolCall",
    "AgentEvent",
    "AgentCheckpoint",
    "AgentCheckpointWrite",
    "TraceSpan",
]
