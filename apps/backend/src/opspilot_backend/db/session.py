"""Async engine / session factory.

The driver is chosen purely from ``DATABASE_URL`` — there is no silent
downgrade any more. If you point it at Postgres you get Postgres; if it cannot
connect, ``/health`` reports ``degraded`` with the real reason instead of
quietly swapping in SQLite.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
import time
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from opspilot_backend.core.config import get_settings
from opspilot_backend.core.logging import get_run_id
from opspilot_backend.core.tracing import record_db_span

settings = get_settings()

DATABASE_URL: str = settings.database_url

_is_sqlite = DATABASE_URL.startswith("sqlite")

_connect_args: dict[str, Any] = {}
if _is_sqlite:
    # Single writer + shared connection across the event loop.
    _connect_args["check_same_thread"] = False

async_engine = create_async_engine(
    DATABASE_URL,
    echo=bool(getattr(settings, "database_echo", False)),
    future=True,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    async_engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


# ---------------------------------------------------------------------------
# SQLite: foreign keys are OFF by default, which silently breaks every
# ON DELETE CASCADE in the schema. Turn them on for every connection.
# ---------------------------------------------------------------------------
if _is_sqlite:

    @event.listens_for(Engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: Any, _record: Any) -> None:  # noqa: ANN401
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        # WAL: readers never block the writer (the agent run session, the SSE
        # poller and the LangGraph checkpointer all share this file). Without
        # it, one connection's read transaction can starve another's COMMIT.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


async def async_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — one session (== one transaction) per request."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# DB query spans — the "…→ Database" tail of every trace
#
# SQLAlchemy fires these two hooks around each cursor execution, so they are
# the only place that sees *every* statement regardless of which session or
# repository issued it (run session, checkpointer, SSE poller). They run in
# the sync half of the async engine, hence no awaits here — the span collector
# itself is a plain list append.
#
# Registered on ``Engine`` (class level, like the SQLite pragma above) rather
# than on one instance: tests and any secondary engine must be traced too, and
# an engine nobody remembered to instrument is a hole in the trace, not a
# performance win.
#
# Gated on an active agent run rather than on "any request": a dashboard that
# polls every two seconds would otherwise write more trace rows than the
# incident it is watching, and "which query did the investigation run" is the
# question that actually gets asked.
# ---------------------------------------------------------------------------


def _db_spans_enabled() -> bool:
    return settings.db_trace_spans and get_run_id() not in ("", "-")


@event.listens_for(Engine, "before_cursor_execute")
def _trace_query_start(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:  # noqa: ANN401
    if _db_spans_enabled():
        context._opspilot_started = time.perf_counter()


@event.listens_for(Engine, "after_cursor_execute")
def _trace_query_end(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:  # noqa: ANN401
    started = getattr(context, "_opspilot_started", None)
    if started is None:
        return
    # A statement that blew up never reaches this hook, so an error here is
    # impossible — the failure surfaces on the engine's own handle_error.
    try:
        dialect = getattr(getattr(conn, "dialect", None), "name", "") or "unknown"
        record_db_span(
            statement=(statement or "").strip()[:200],
            operation=(statement or "").split(None, 1)[0].upper()[:16],
            duration_ms=(time.perf_counter() - started) * 1000,
            dialect=dialect,
            executemany=bool(executemany),
            min_ms=settings.db_span_min_ms,
        )
    except Exception:  # pragma: no cover - see tracing._record
        pass


async def probe_database() -> tuple[bool, str]:
    """Real health probe used by ``/health`` — never a hardcoded status."""
    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim in /health
        return False, f"{type(exc).__name__}: {exc}"
    return True, "ok"
