"""In-process fan-out for live agent events.

The database is the *durable* event log (that is what ``Last-Event-ID``
replays from); this bus only carries events to clients that are connected
right now. It is deliberately tiny: if the process restarts, nothing is lost
because every event has already been committed to ``agent_events``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from opspilot_backend.core.logging import log_event


class EventBus:
    """Per-run subscriber queues with back-pressure-safe delivery."""

    def __init__(self, *, queue_size: int = 500) -> None:
        self._queue_size = queue_size
        self._subscribers: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.setdefault(str(run_id), set()).add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        subscribers = self._subscribers.get(str(run_id))
        if subscribers is None:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(str(run_id), None)

    def subscriber_count(self, run_id: str) -> int:
        return len(self._subscribers.get(str(run_id), ()))

    async def publish(self, run_id: str, payload: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(str(run_id), ())):
            if queue.full():
                # A stalled client must not stall the agent: drop the oldest
                # frame rather than blocking the workflow.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - race
                    pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:  # pragma: no cover - defensive
                log_event("eventbus.drop", run_id=str(run_id))


_BUS: EventBus | None = None


def get_event_bus() -> EventBus:
    global _BUS
    if _BUS is None:
        _BUS = EventBus()
    return _BUS


def reset_event_bus() -> None:
    """Test hook — forget all subscribers between cases."""
    global _BUS
    _BUS = None


__all__ = ["EventBus", "get_event_bus", "reset_event_bus"]
