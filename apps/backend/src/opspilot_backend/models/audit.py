"""Audit log — who did what, when, on which agent run, with which risk."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from opspilot_backend.db.base import Base, UUIDPrimaryKeyMixin, utcnow
from opspilot_backend.models.types import ACTOR_TYPE, RISK_LEVEL


class AuditLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_logs"

    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    actor_type: Mapped[str] = mapped_column(
        ACTOR_TYPE, nullable=False, default="system"
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    resource_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    incident_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tool_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    risk_level: Mapped[Optional[str]] = mapped_column(RISK_LEVEL, nullable=True)
    approval_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("approvals.id", ondelete="SET NULL"),
        nullable=True,
    )
    parameters: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="success")
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, index=True, default=utcnow, server_default=func.now()
    )


__all__ = ["AuditLog"]
