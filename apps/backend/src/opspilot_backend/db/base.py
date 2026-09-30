"""Declarative base + shared column helpers.

Kept separate from :mod:`opspilot_backend.db.session` so that Alembic (and
tests) can import the metadata without creating an engine.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Timezone-naive UTC timestamp (columns are declared without tz)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Root declarative base for every OpsPilot table."""

    pass


class TimestampMixin:
    """created_at / updated_at on every table, per the schema contract."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


def uuid_fk(table_column: str, **kwargs: object) -> Mapped[uuid.UUID]:
    """Foreign key helper guaranteeing every relation has an FK constraint."""
    from sqlalchemy import ForeignKey

    return mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(table_column, ondelete=kwargs.pop("ondelete", "CASCADE")),
        nullable=kwargs.pop("nullable", False),
        index=kwargs.pop("index", True),
        **kwargs,  # type: ignore[arg-type]
    )
