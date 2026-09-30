"""Runbooks + postmortems."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    # String relationship target — resolved at runtime by SQLAlchemy.
    from opspilot_backend.models.incident import Incident

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from opspilot_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from opspilot_backend.models.types import RISK_LEVEL


class Runbook(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "runbooks"

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    service: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="general")
    risk_level: Mapped[str] = mapped_column(RISK_LEVEL, nullable=False, default="LOW")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    chunks: Mapped[list["RunbookChunk"]] = relationship(
        "RunbookChunk", back_populates="runbook", cascade="all, delete-orphan"
    )


class RunbookChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Retrieval unit for ``search_runbooks``."""

    __tablename__ = "runbook_chunks"

    runbook_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("runbooks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    heading: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    runbook: Mapped[Runbook] = relationship("Runbook", back_populates="chunks")


class Postmortem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "postmortems"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    root_cause: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timeline: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    contributing_factors: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    lessons_learned: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    action_items: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    generated_by: Mapped[str] = mapped_column(String(128), nullable=False, default="agent")

    incident: Mapped["Incident"] = relationship(
        "Incident", back_populates="postmortem"
    )


__all__ = ["Runbook", "RunbookChunk", "Postmortem"]
