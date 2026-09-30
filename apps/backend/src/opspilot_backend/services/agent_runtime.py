"""Agent Runtime application service.

This is the seam between HTTP and the LangGraph workflow:

* it creates the ``AgentRun`` row (and therefore the checkpoint ``thread_id``),
* builds the per-run ``NodeContext`` (tool executor + persistence + events),
* invokes or resumes the graph,
* translates "the graph parked at an interrupt" into ``WAITING_APPROVAL``.

Nothing here knows about FastAPI, and nothing in ``agent/`` knows about SQL.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from langgraph.errors import GraphInterrupt
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_backend.agent.budget import Budget
from opspilot_backend.agent.context import NodeContext
from opspilot_backend.agent.graph import build_graph
from opspilot_backend.agent.llm import get_llm
from opspilot_backend.agent.state import IncidentRef, IncidentState
from opspilot_backend.core.logging import log_event
from opspilot_backend.core.tracing import (
    begin_span_collection,
    bind,
    bind_run_id,
    current_context,
    current_ids,
    drain_spans,
    new_span_id,
    span,
    suspend_recording,
    unbind,
)
from opspilot_backend.db.session import async_session_factory
from opspilot_backend.domain.enums import AgentRunStatus, EventType
from opspilot_backend.domain.errors import ConflictError, NotFoundError
from opspilot_backend.models import AgentRun, Incident, Service
from opspilot_backend.repositories.agent_run import AgentRunRepository
from opspilot_backend.services.events import EventBus, get_event_bus
from opspilot_backend.services.tool_hooks import RepositoryToolHooks
from opspilot_backend.tools.executor import ToolExecutor

_RECURSION_LIMIT = 60

_ACTIVE_RUN_STATUSES = frozenset(
    {
        AgentRunStatus.PENDING,
        AgentRunStatus.RUNNING,
        AgentRunStatus.WAITING_APPROVAL,
    }
)


class AgentRuntimeService:
    """Start, resume and monitor Agent Runs."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        bus: EventBus | None = None,
        checkpointer: Any | None = None,
        session_factory: Any | None = None,
    ) -> None:
        self.session = session
        self.repo = AgentRunRepository(session)
        self.bus = bus or get_event_bus()
        self._checkpointer = checkpointer
        self._session_factory = session_factory or async_session_factory
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def start(
        self,
        incident_id: Any,
        *,
        background: bool = True,
        reasoning_mode: str | None = None,
    ) -> Any:
        """Start a run for ``incident_id``."""
        key = self._as_uuid(incident_id)
        incident = await self.session.get(Incident, key)
        if incident is None:
            raise NotFoundError("Incident", incident_id)
        # Identity-map hits skip eager loading, so fetch the service by its
        # own primary key instead of relying on ``incident.service``.
        service = (
            await self.session.get(Service, incident.service_id)
            if incident.service_id
            else None
        )

        active = await self._active_run(key)
        if active is not None:
            raise ConflictError(
                f"incident {key} already has a run in state {active.status}"
            )

        # How the run will actually reason, recorded once: nothing ever wrote
        # this field back after creation, so every run reported "deterministic"
        # including the ones that paid for a model. A run that says less about
        # itself than is true misleads in exactly the way the one that says
        # more does — the operator cannot tell whether the LLM is wired up.
        # The provider is a process-wide singleton, so it cannot change between
        # starting the run and reaching the nodes that use it.
        effective_mode = reasoning_mode or get_llm().name
        thread_id = f"{key}:{uuid.uuid4().hex[:12]}"
        # One AgentRun == one trace. `bind()` inherits the trace of the HTTP
        # request that started the run when there is one, so "the click" and
        # "the investigation it triggered" end up in the same timeline; with no
        # ambient context it mints a fresh trace. Either way the ids are stored
        # on the run row, so an operator can jump straight to the trace later.
        #
        # The ambient span id is captured *before* binding: it is the HTTP
        # span of the triggering request, and the only legitimate parent for
        # the run's root span. `bind()` would otherwise manufacture an id that
        # no span in the trace ever had.
        parent_span_id = current_context().span_id
        token = bind()
        try:
            ids = current_ids()
            run = await self.repo.create_run(
                incident_id=key,
                thread_id=thread_id,
                reasoning_mode=effective_mode,
                trace_id=ids["trace_id"],
                request_id=ids["request_id"],
            )
            # Commit before the run starts: a background run lives in its own
            # session, so the row must be durable and visible outside this one.
            await self.session.commit()

            state = self._initial_state(incident, service)
            if background:
                self._spawn(
                    self._execute(thread_id, initial=state, parent_span_id=parent_span_id)
                )
            else:
                await self._execute(thread_id, initial=state, parent_span_id=parent_span_id)
        finally:
            # Hand the caller's context back exactly as it was. The id minted by
            # ``bind`` names no recorded span, and leaving it in scope means a
            # later leg (the post-approval resume) adopts a parent id that
            # exists nowhere — a broken chain precisely where the human
            # intervened.
            unbind(token)
        return run

    async def resume(self, run_id: Any, decision: dict[str, Any], *, background: bool = True) -> None:
        """Continue a run parked at ``interrupt()`` from its checkpoint."""
        run = await self.repo.get(run_id)
        if run is None:
            raise NotFoundError("AgentRun", run_id)
        if run.status != AgentRunStatus.WAITING_APPROVAL:
            raise ConflictError(
                f"run {run.id} is {run.status}, only waiting_approval can be resumed"
            )
        # Release this session's read transaction before the graph runs in its
        # own session — a held SHARED lock would starve the run's commits on
        # non-WAL SQLite.
        await self.session.commit()
        parent_span_id = current_context().span_id
        if background:
            self._spawn(
                self._execute(run.thread_id, resume=decision, parent_span_id=parent_span_id)
            )
        else:
            await self._execute(run.thread_id, resume=decision, parent_span_id=parent_span_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _as_uuid(value: Any) -> uuid.UUID:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

    async def _active_run(self, incident_id: uuid.UUID) -> AgentRun | None:
        stmt = select(AgentRun).where(AgentRun.incident_id == incident_id)
        for row in (await self.session.execute(stmt)).scalars():
            if row.status in _ACTIVE_RUN_STATUSES:
                return row
        return None

    def _initial_state(self, incident: Incident, service: Any) -> IncidentState:
        service_name = service.name if service else ""
        return IncidentState(
            incident=IncidentRef(
                incident_id=str(incident.id),
                service=service_name,
                severity=incident.severity,
                title=incident.title,
                scenario=incident.scenario,
                detected_at=incident.detected_at.isoformat() if incident.detected_at else None,
            )
        )

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _graph(self) -> Any:
        return build_graph(checkpointer=self._checkpointer)

    async def _execute(
        self,
        thread_id: str,
        *,
        initial: IncidentState | None = None,
        resume: dict[str, Any] | None = None,
        parent_span_id: str = "",
    ) -> None:
        """Run the graph in its own session/transaction.

        A dedicated session is mandatory: the HTTP request that started the run
        has already returned, and every event must be committed before the SSE
        stream (a different connection) can read it.
        """
        async with self._session_factory() as session:
            repo = AgentRunRepository(session)
            run = await repo.get_by_thread(thread_id)
            if run is None:
                log_event("agent.run.missing", thread_id=thread_id)
                return

            run_id = str(run.id)
            incident_id = str(run.incident_id)
            # Re-enter the run's own trace. A resumed execution (post-approval)
            # continues the original trace instead of starting an unrelated
            # one, otherwise the approval would visually split the timeline.
            #
            # The run's root span hangs off the triggering HTTP span *only* when
            # that span belongs to the same trace. The approval click, for
            # instance, arrives on its own trace, and pointing the run's root at
            # it would create a parent that exists nowhere in the store.
            ambient = current_context()
            attach = bool(parent_span_id) and ambient.trace_id == (run.trace_id or "")
            if resume is not None and not attach:
                # A resumed leg continues an existing run, so its root span
                # hangs off the span that opened the run rather than starting a
                # second tree. The lookup is untraced on purpose: a span
                # recording the act of finding the parent would be parented
                # under a context that does not exist yet.
                with suspend_recording():
                    parent_span_id = await self._previous_run_span(repo, run.id)
                attach = bool(parent_span_id)
            token = bind(
                trace_id=run.trace_id or None,
                request_id=run.request_id or None,
                span_id=parent_span_id if attach else new_span_id(),
            )
            bind_run_id(run_id)
            # Own the collector here rather than inheriting whatever the
            # caller's context happened to hold: this task's spans are this
            # run's spans, and nothing else gets flushed into the run's trace.
            begin_span_collection()
            try:
                # The run span opens before any of the setup reads: those reads
                # are part of the run, and a span that is the parent of nothing
                # while queries name it as their parent is a dangling id.
                with span(
                    "agent.run",
                    kind="run",
                    root=not attach,
                    incident_id=incident_id,
                    thread_id=thread_id,
                    resumed=resume is not None,
                ):
                    db_lock = asyncio.Lock()
                    hooks = RepositoryToolHooks(
                        session,
                        run_id=run_id,
                        incident_id=incident_id,
                        bus=self.bus,
                        db_lock=db_lock,
                    )
                    # A resumed run re-enters mid-workflow, so step sequence
                    # numbers must continue after the ones already stored.
                    existing_steps = await repo.steps(run.id)
                    sequence_start = max(
                        (s.sequence for s in existing_steps), default=0
                    )
                    ctx = NodeContext(
                        run_id=run_id,
                        incident_id=incident_id,
                        executor=ToolExecutor(hooks),
                        hooks=hooks,
                        persistence=repo,
                        sequence_start=sequence_start,
                        db_lock=db_lock,
                        # The resumed leg continues the same allowance: without
                        # this it starts from zero and its first persist wipes
                        # the pre-approval spend off the run row.
                        budget=(
                            Budget.restore(
                                spent_tool_calls=run.spent_tool_calls,
                                spent_tokens=run.spent_tokens,
                                spent_retries=run.spent_retries,
                                spent_seconds=run.spent_seconds,
                            )
                            if resume is not None
                            else None
                        ),
                    )
                    config: dict[str, Any] = {
                        "configurable": {"thread_id": thread_id, "ctx": ctx},
                        "recursion_limit": _RECURSION_LIMIT,
                    }
                    await self._drive(
                        run_id=run_id,
                        incident_id=incident_id,
                        thread_id=thread_id,
                        session=session,
                        repo=repo,
                        hooks=hooks,
                        config=config,
                        initial=initial,
                        resume=resume,
                    )
            finally:
                await self._persist_spans(repo, run_id, session)
                # Last, so the flush still runs inside the run's own trace
                # context. Leaving it bound would silently reparent spans
                # recorded later in this task.
                unbind(token)

    async def _drive(
        self,
        *,
        run_id: str,
        incident_id: str,
        thread_id: str,
        session: AsyncSession,
        repo: AgentRunRepository,
        hooks: RepositoryToolHooks,
        config: dict[str, Any],
        initial: IncidentState | None,
        resume: dict[str, Any] | None,
    ) -> None:
        """Invoke (or resume) the graph and translate its exit into run status."""
        try:
            if resume is not None:
                final = await self._graph().ainvoke(
                    Command(resume=resume), config=config
                )
            else:
                await hooks.emit(
                    EventType.AGENT_STARTED.value,
                    {"thread_id": thread_id, "incident_id": incident_id},
                    run_id=run_id,
                    incident_id=incident_id,
                )
                final = await self._graph().ainvoke(
                    (initial or IncidentState(incident=IncidentRef(incident_id=incident_id))).model_dump(),
                    config=config,
                )
        except GraphInterrupt:
            # Fallback for langgraph builds that raise instead of returning
            # __interrupt__ — same handling as the return path below.
            await repo.update_run(
                run_id,
                status=AgentRunStatus.WAITING_APPROVAL.value,
                interrupt_payload={"stage": "human_approval"},
            )
            await session.commit()
            log_event("agent.run.interrupted", run_id=run_id, thread_id=thread_id)
            return
        except Exception as exc:  # noqa: BLE001 - the run must never vanish
            await session.rollback()
            message = f"{type(exc).__name__}: {exc}"
            await repo.update_run(
                run_id, status=AgentRunStatus.FAILED.value, error=message
            )
            await hooks.emit(
                EventType.AGENT_FAILED.value,
                {"error": message},
                run_id=run_id,
                incident_id=incident_id,
            )
            await repo.set_incident_status(
                incident_id, "FAILED", actor="agent", summary=message
            )
            await session.commit()
            log_event("agent.run.failed", run_id=run_id, error=message)
            return

        # LangGraph 1.x does NOT raise on interrupt(): ainvoke returns the
        # state with an ``__interrupt__`` entry, checkpoint already saved.
        # Without this check the parked run would fall through and be
        # marked COMPLETED.
        interrupts = (
            final.get("__interrupt__") if isinstance(final, dict) else None
        )
        if interrupts:
            await repo.update_run(
                run_id,
                status=AgentRunStatus.WAITING_APPROVAL.value,
                interrupt_payload={"stage": "human_approval"},
            )
            await session.commit()
            log_event("agent.run.interrupted", run_id=run_id, thread_id=thread_id)
            return

        # A run that stopped early with recorded node errors is a failure,
        # even though the graph reached END without raising.
        errors = (
            final.get("execution", {}).get("errors")
            if isinstance(final, dict)
            else None
        )
        if errors:
            message = "; ".join(
                f"{e.get('stage')}:{e.get('error_type')}: {e.get('message')}"
                for e in errors[-3:]
            )
            await repo.update_run(
                run_id, status=AgentRunStatus.FAILED.value, error=message
            )
            await hooks.emit(
                EventType.AGENT_FAILED.value,
                {"errors": errors[-3:]},
                run_id=run_id,
                incident_id=incident_id,
            )
            await session.commit()
            log_event("agent.run.failed", run_id=run_id, error=message)
            return

        await repo.update_run(run_id, status=AgentRunStatus.COMPLETED.value)
        await hooks.emit(
            EventType.AGENT_COMPLETED.value,
            {"thread_id": thread_id},
            run_id=run_id,
            incident_id=incident_id,
        )
        await session.commit()

    @staticmethod
    async def _previous_run_span(repo: AgentRunRepository, run_id: str) -> str:
        """The span that opened this run, so a resumed leg can continue it.

        The first ``agent.run`` span of the run is the one that legitimately
        begins the trace; the resumed leg's own span is marked ``resumed`` and
        must not be picked, or every resume would nest inside the previous
        resume.
        """
        for row in await repo.spans_for_run(run_id):
            if row.name == "agent.run" and not (row.attributes or {}).get("resumed"):
                return row.span_id
        return ""

    async def _persist_spans(
        self, repo: AgentRunRepository, run_id: str, session: AsyncSession
    ) -> None:
        """Write out every span the run recorded, then clear the buffer.

        Best-effort by design: losing a trace is a monitoring gap, and a
        monitoring gap must never be the reason a recovery reports failure.
        """
        spans = drain_spans()
        if not spans:
            return
        try:
            with suspend_recording():  # do not trace the act of writing the trace
                written = await repo.save_spans(run_id, spans)
                await session.commit()
            log_event("agent.trace.persisted", run_id=run_id, spans=written)
        except Exception as exc:  # noqa: BLE001 - see docstring
            await session.rollback()
            log_event(
                "agent.trace.persist_failed",
                run_id=run_id,
                spans=len(spans),
                error=f"{type(exc).__name__}: {exc}",
            )


__all__ = ["AgentRuntimeService"]
