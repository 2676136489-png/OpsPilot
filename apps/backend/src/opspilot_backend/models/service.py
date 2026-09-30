"""Service topology tables."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    # String relationship target — resolved at runtime by SQLAlchemy.
    from opspilot_backend.models.incident import Incident

from sqlalchemy import ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from opspilot_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from opspilot_backend.domain.enums import ServiceHealth
from opspilot_backend.models.types import SERVICE_HEALTH


class Service(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "services"

    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tier: Mapped[str] = mapped_column(String(32), nullable=False, default="application")
    owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    health: Mapped[ServiceHealth] = mapped_column(
        SERVICE_HEALTH, nullable=False, default=ServiceHealth.UNKNOWN
    )

    incidents: Mapped[list["Incident"]] = relationship(
        "Incident", back_populates="service", cascade="all, delete-orphan"
    )
    dependencies: Mapped[list["ServiceDependency"]] = relationship(
        "ServiceDependency",
        back_populates="service",
        foreign_keys="ServiceDependency.service_id",
        cascade="all, delete-orphan",
    )


class ServiceDependency(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """``service_id`` depends on ``depends_on_id``."""

    __tablename__ = "service_dependencies"

    service_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("services.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    depends_on_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("services.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dependency_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="sync_call"
    )
    criticality: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    service: Mapped[Service] = relationship(
        "Service", back_populates="dependencies", foreign_keys=[service_id]
    )
    depends_on: Mapped[Service] = relationship("Service", foreign_keys=[depends_on_id])


class Deployment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A deployment record for a service.

    The investigation agent correlates deployment timing against incident
    onset ("did a deploy land right before the incident?"), so every deploy
    that could matter must be persisted here — the API is the write path,
    :meth:`DeploymentRepository.get_by_service` the read path.
    """

    __tablename__ = "deployments"

    service_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("services.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[str] = mapped_column(String(128), nullable=False)
    previous_version: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # One of DEPLOYING / SUCCESSFUL / FAILED / ROLLED_BACK — validated by the
    # request schema; stored as plain text so historic rows survive vocabulary
    # changes.
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    deployer: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    service: Mapped[Service] = relationship("Service")


__all__ = ["Service", "ServiceDependency", "Deployment"]
