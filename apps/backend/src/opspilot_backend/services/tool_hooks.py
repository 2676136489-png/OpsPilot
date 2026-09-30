"""Repository-backed ToolHooks.

The Tool Layer is not allowed to import SQLAlchemy, so it calls this instead:
every tool call, event and approval request it produces is written through the
repositories and pushed onto the live event bus.

Two subtleties handled here:

* Events are committed immediately (not merely flushed) — that is the only way
  a second connection (the SSE stream) can see them mid-run.
* ``parallel_investigation`` fires several tool calls concurrently on the same
  ``AsyncSession``. SQLAlchemy sessions are not concurrency-safe, so every
  database operation these hooks perform is serialized behind a lock. The
  tool *executions* (HTTP) stay fully parallel; only their bookkeeping
  serializes.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_backend.core.tracing import current_ids
from opspilot_backend.models import AuditLog
from opspilot_backend.repositories.agent_run import AgentRunRepository, _uuid_or_none
from opspilot_backend.services.events import EventBus, get_event_bus
from opspilot_backend.tools.hooks import NullToolHooks

# Columns the Tool Layer reports as opaque strings but the schema stores as UUID.
_UUID_FIELDS = ("incident_id", "run_id", "approval_id")


class RepositoryToolHooks(NullToolHooks):
    """Persist + broadcast everything the Tool Layer reports."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        incident_id: str,
        bus: EventBus | None = None,
        db_lock: asyncio.Lock | None = None,
    ) -> None:
        self.session = session
        self.repo = AgentRunRepository(session)
        self.run_id = str(run_id)
        self.incident_id = str(incident_id)
        self.bus = bus or get_event_bus()
        # The lock can be shared with the NodeContext that owns this run, so
        # bookkeeping the node itself writes between tool calls (budget
        # updates) serializes against these hooks instead of racing them on
        # the same session. Two locks over one AsyncSession is one lock too
        # many: whichever task loses the race does not get a warning, it gets
        # an InvalidRequestError and its probe result is dropped.
        self._lock = db_lock or asyncio.Lock()

    async def commit(self) -> None:
        """End the current transaction — see ``AgentPersistence.commit``."""
        async with self._lock:
            await self.session.commit()

    # -- tool calls ----------------------------------------------------
    async def on_tool_start(self, **kwargs: Any) -> str:
        # Stamp the call with whatever span is in scope. The tool executor
        # opens one per call, so this is the id that ties a tool row back to
        # the node that asked for it.
        kwargs.setdefault("trace_id", current_ids().get("trace_id", ""))
        kwargs.setdefault("span_id", current_ids().get("span_id", ""))
        async with self._lock:
            call_id = await self.repo.record_tool_call(**kwargs)
            await self.session.commit()
            return call_id

    async def on_tool_finish(self, tool_call_id: str, **kwargs: Any) -> None:
        async with self._lock:
            await self.repo.finish_tool_call(tool_call_id, **kwargs)
            await self.session.commit()

    # -- events --------------------------------------------------------
    async def emit(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        run_id: str,
        incident_id: str,
        stage: str | None = None,
    ) -> None:
        async with self._lock:
            event = await self.repo.append_event(
                event_type,
                data,
                run_id=run_id or self.run_id,
                incident_id=incident_id or self.incident_id,
                stage=stage,
            )
            await self.session.commit()
        await self.bus.publish(str(event.run_id), self._payload(event))

    @staticmethod
    def _payload(event: Any) -> dict[str, Any]:
        return {
            "seq": event.seq,
            "event_id": event.event_id,
            "run_id": str(event.run_id),
            "incident_id": str(event.incident_id) if event.incident_id else None,
            "event_type": event.event_type,
            "stage": event.stage,
            "data": event.data,
            "created_at": event.created_at.isoformat() if event.created_at else None,
        }

    # -- audit ---------------------------------------------------------
    async def audit(self, **kwargs: Any) -> None:
        async with self._lock:
            params = dict(kwargs)
            for field in _UUID_FIELDS:
                params[field] = _uuid_or_none(params.get(field))
            self.session.add(AuditLog(**params))
            await self.session.commit()

    # -- approvals -----------------------------------------------------
    async def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        async with self._lock:
            return await self.repo.get_approval(approval_id)

    async def create_approval(self, **kwargs: Any) -> dict[str, Any]:
        async with self._lock:
            approval = await self.repo.create_approval(**kwargs)
            await self.session.commit()
            return approval


__all__ = ["RepositoryToolHooks"]
