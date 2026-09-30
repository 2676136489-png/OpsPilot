"""Database-backed LangGraph checkpointer.

This is what turns a long-running agent from a fire-and-forget coroutine into
a durable workflow: the graph state survives process restarts, so an incident
parked at ``WAITING_APPROVAL`` can be resumed hours later by invoking it with
``Command(resume=...)`` instead of re-running the investigation.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    PendingWrite,
    get_checkpoint_id,
)
from sqlalchemy import delete, select

from opspilot_backend.db.session import async_session_factory
from opspilot_backend.models import AgentCheckpoint, AgentCheckpointWrite


def _encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _decode(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


class DatabaseCheckpointer(BaseCheckpointSaver[str]):
    """Async-only saver backed by the primary database.

    ``session_factory`` is injectable so tests can point the checkpointer at
    the same in-memory database the rest of the fixture uses.
    """

    def __init__(self, session_factory: Any | None = None) -> None:
        super().__init__()
        self._session_factory = session_factory or async_session_factory

    # -- write ---------------------------------------------------------
    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        configurable = config.get("configurable", {})
        thread_id = configurable["thread_id"]
        checkpoint_ns = configurable.get("checkpoint_ns", "")
        checkpoint_id = checkpoint["id"]
        parent_id = configurable.get("checkpoint_id")

        type_, blob = self.serde.dumps_typed(checkpoint)
        async with self._session_factory() as session:
            existing = await session.scalar(
                select(AgentCheckpoint).where(
                    AgentCheckpoint.thread_id == thread_id,
                    AgentCheckpoint.checkpoint_ns == checkpoint_ns,
                    AgentCheckpoint.checkpoint_id == checkpoint_id,
                )
            )
            if existing is None:
                session.add(
                    AgentCheckpoint(
                        thread_id=thread_id,
                        checkpoint_ns=checkpoint_ns,
                        checkpoint_id=checkpoint_id,
                        parent_checkpoint_id=parent_id,
                        type=type_,
                        checkpoint=_encode(blob),
                        meta=dict(metadata or {}),
                    )
                )
            else:
                existing.type = type_
                existing.checkpoint = _encode(blob)
                existing.meta = dict(metadata or {})
                existing.parent_checkpoint_id = parent_id
            await session.commit()

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        configurable = config.get("configurable", {})
        thread_id = configurable["thread_id"]
        checkpoint_ns = configurable.get("checkpoint_ns", "")
        checkpoint_id = configurable["checkpoint_id"]
        if not checkpoint_id:
            return

        rows = []
        for idx, (channel, value) in enumerate(writes):
            type_, blob = self.serde.dumps_typed(value)
            rows.append(
                AgentCheckpointWrite(
                    thread_id=thread_id,
                    checkpoint_ns=checkpoint_ns,
                    checkpoint_id=checkpoint_id,
                    task_id=task_id,
                    idx=idx,
                    channel=channel,
                    type=type_,
                    blob=_encode(blob),
                )
            )
        if not rows:
            return
        async with self._session_factory() as session:
            session.add_all(rows)
            await session.commit()

    # -- read ----------------------------------------------------------
    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        configurable = config.get("configurable", {})
        thread_id = configurable.get("thread_id")
        if thread_id is None:
            return None
        checkpoint_ns = configurable.get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)

        async with self._session_factory() as session:
            stmt = select(AgentCheckpoint).where(
                AgentCheckpoint.thread_id == thread_id,
                AgentCheckpoint.checkpoint_ns == checkpoint_ns,
            )
            if checkpoint_id:
                stmt = stmt.where(AgentCheckpoint.checkpoint_id == checkpoint_id)
            stmt = stmt.order_by(AgentCheckpoint.created_at.desc()).limit(1)
            row = await session.scalar(stmt)
            if row is None:
                return None

            checkpoint = self.serde.loads_typed((row.type, _decode(row.checkpoint)))
            writes_result = await session.execute(
                select(AgentCheckpointWrite).where(
                    AgentCheckpointWrite.thread_id == thread_id,
                    AgentCheckpointWrite.checkpoint_ns == checkpoint_ns,
                    AgentCheckpointWrite.checkpoint_id == row.checkpoint_id,
                ).order_by(AgentCheckpointWrite.idx)
            )
            pending: list[PendingWrite] = [
                (
                    w.task_id,
                    w.channel,
                    self.serde.loads_typed((w.type, _decode(w.blob))),
                )
                for w in writes_result.scalars()
            ]

        parent_config: RunnableConfig | None = None
        if row.parent_checkpoint_id:
            parent_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": row.parent_checkpoint_id,
                }
            }

        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": row.checkpoint_id,
                }
            },
            checkpoint=checkpoint,
            metadata=row.meta or {},
            parent_config=parent_config,
            pending_writes=pending or None,
        )

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 - LangGraph API
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            return
        configurable = config.get("configurable", {})
        thread_id = configurable.get("thread_id")
        if thread_id is None:
            return
        checkpoint_ns = configurable.get("checkpoint_ns", "")

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(AgentCheckpoint)
                    .where(
                        AgentCheckpoint.thread_id == thread_id,
                        AgentCheckpoint.checkpoint_ns == checkpoint_ns,
                    )
                    .order_by(AgentCheckpoint.created_at.desc())
                    .limit(limit or 50)
                )
            ).scalars()

            for row in rows:
                checkpoint = self.serde.loads_typed((row.type, _decode(row.checkpoint)))
                parent_config = (
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": row.parent_checkpoint_id,
                        }
                    }
                    if row.parent_checkpoint_id
                    else None
                )
                yield CheckpointTuple(
                    config={
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": row.checkpoint_id,
                        }
                    },
                    checkpoint=checkpoint,
                    metadata=row.meta or {},
                    parent_config=parent_config,
                )

    async def adelete_thread(self, thread_id: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                delete(AgentCheckpointWrite).where(
                    AgentCheckpointWrite.thread_id == thread_id
                )
            )
            await session.execute(
                delete(AgentCheckpoint).where(AgentCheckpoint.thread_id == thread_id)
            )
            await session.commit()

    # -- sync API: unused, fail loudly rather than silently no-op -------
    def put(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError("DatabaseCheckpointer is async-only")

    def put_writes(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError("DatabaseCheckpointer is async-only")

    def get_tuple(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError("DatabaseCheckpointer is async-only")

    def list(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError("DatabaseCheckpointer is async-only")
