"""Agent API endpoints — start investigations, inspect runs, decide approvals.

Thin controllers over :class:`AgentRuntimeService` (write path) and
:class:`AgentRunQueryService` (read path). Every response matches the
frontend's ``AgentRun`` / ``AgentStats`` contract, so the dashboard needs no
knowledge of how runs are persisted.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_backend.db.session import async_session, async_session_factory
from opspilot_backend.repositories.agent_run import AgentRunRepository
from opspilot_backend.services.agent_run import AgentRunQueryService
from opspilot_backend.services.agent_runtime import AgentRuntimeService
from opspilot_backend.services.agent_stream import event_stream, parse_last_event_id
from opspilot_backend.services.events import get_event_bus

router = APIRouter(prefix="/agent", tags=["agent"])

#: Replay window per poll. Bounded so one request cannot pull an unbounded
#: backlog into memory; a client that needs more just keeps reading, since the
#: cursor advances and the next poll resumes where this one stopped.
_STREAM_REPLAY_LIMIT = 500

# One operator double-clicking "approve" must not spawn two resumed graph
# executions. Serialise per-run approval handling in-process; the runtime's
# own status check guards everything else.
_approval_locks: dict[str, asyncio.Lock] = {}



def _lock_for(run_id: str) -> asyncio.Lock:
    lock = _approval_locks.get(run_id)
    if lock is None:
        lock = asyncio.Lock()
        _approval_locks[run_id] = lock
    return lock


class StartInvestigationRequest(BaseModel):
    incident_id: str = Field(default="", max_length=128)
    # Accepted for API compatibility; the investigation reads scenario context
    # from the persisted incident/service/deployment rows, not from the caller.
    scenario: str | None = Field(default=None, max_length=64)


class ApprovalRespondRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


@router.post("/incidents/{incident_id}/start")
async def start_investigation(
    incident_id: str = Path(..., min_length=1, max_length=128),
    body: StartInvestigationRequest | None = None,
    session: AsyncSession = Depends(async_session),
) -> dict:
    """Kick off a new agent investigation for an incident.

    Returns the run immediately — the graph executes in the background and
    its status becomes visible through ``GET /agent/runs/{id}``.
    """
    runtime = AgentRuntimeService(session)
    run = await runtime.start(incident_id, background=True)
    return await AgentRunQueryService(session).get_run(str(run.id))


@router.get("/runs")
async def list_runs(session: AsyncSession = Depends(async_session)) -> list[dict]:
    """Every run the server knows about, newest first."""
    return await AgentRunQueryService(session).list_runs()


@router.get("/incidents/{incident_id}/run")
async def get_incident_run(
    incident_id: str = Path(..., min_length=1, max_length=128),
    session: AsyncSession = Depends(async_session),
) -> dict | None:
    """The incident's investigation, or ``null`` if it has never had one.

    The dashboard's entry point. ``null`` (never investigated) and a run that
    has not left ``pending`` are different states and the UI must be able to
    tell them apart to decide between "start" and "watch".
    """
    return await AgentRunQueryService(session).run_for_incident(incident_id)


@router.get("/stats")
async def agent_stats(session: AsyncSession = Depends(async_session)) -> dict:
    """Aggregate run statistics for the dashboard summary cards."""
    return await AgentRunQueryService(session).stats()


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str, session: AsyncSession = Depends(async_session)
) -> dict:
    """Return the current state of an agent run (REST, non-streaming)."""
    payload = await AgentRunQueryService(session).get_run(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"运行 {run_id} 不存在")
    return payload


def _span_payload(row: object) -> dict:
    return {
        "span_id": row.span_id,
        "parent_span_id": row.parent_span_id,
        "trace_id": row.trace_id,
        "request_id": row.request_id,
        "name": row.name,
        "kind": row.kind,
        "status": row.status,
        "error": row.error,
        "duration_ms": round(float(row.duration_ms or 0.0), 3),
        "attributes": dict(row.attributes or {}),
        "started_at": row.started_at.isoformat() if row.started_at else None,
    }


def _span_tree(spans: list[dict]) -> list[dict]:
    """Nest spans by ``parent_span_id``.

    Spans whose parent is missing (the run was resumed, or a parent was
    dropped by the buffer cap) become roots rather than disappearing — an
    orphan in the tree is information, a silently dropped span is not.
    """
    nodes: dict[str, dict] = {}
    for span in spans:
        node = dict(span)
        node["children"] = []
        nodes[span["span_id"]] = node
    roots: list[dict] = []
    for span in spans:
        parent = nodes.get(span["parent_span_id"])
        if parent is None:
            roots.append(nodes[span["span_id"]])
        else:
            parent["children"].append(nodes[span["span_id"]])
    return roots


@router.get("/runs/{run_id}/trace")
async def get_run_trace(
    run_id: str, session: AsyncSession = Depends(async_session)
) -> dict:
    """The full trace of a run: Agent Run → Node → MCP → Tool → DB/Simulator.

    Flat list *and* nested tree, because the frontend timeline renders the
    former and the Observability view renders the latter.
    """
    repo = AgentRunRepository(session)
    run = await repo.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行 {run_id} 不存在")
    rows = await repo.spans_for_run(run_id)
    flat = [_span_payload(row) for row in rows]
    by_kind: dict[str, int] = {}
    for span in flat:
        by_kind[span["kind"]] = by_kind.get(span["kind"], 0) + 1
    return {
        "run_id": str(run.id),
        "incident_id": str(run.incident_id),
        "trace_id": run.trace_id or "",
        "request_id": run.request_id or "",
        "status": getattr(run.status, "value", run.status),
        "span_count": len(flat),
        "by_kind": by_kind,
        "duration_ms": round(sum(s["duration_ms"] for s in flat), 3),
        "spans": flat,
        "tree": _span_tree(flat),
    }


@router.get("/traces/{trace_id}")
async def get_trace(
    trace_id: str, session: AsyncSession = Depends(async_session)
) -> dict:
    """Every span sharing a trace id, across every run and HTTP request."""
    rows = await AgentRunRepository(session).spans_for_trace(trace_id)
    flat = [_span_payload(row) for row in rows]
    return {
        "trace_id": trace_id,
        "span_count": len(flat),
        "spans": flat,
        "tree": _span_tree(flat),
    }


@router.get("/runs/{run_id}/steps")
async def get_run_steps(
    run_id: str, session: AsyncSession = Depends(async_session)
) -> dict:
    """Every node the run executed, each with the tool calls it made.

    This is the real timeline. It used to be unavailable, which is precisely
    why the dashboard rendered a fabricated one from the run snapshot: a
    ``current_node`` and an ``updated_at`` are not a step history, and no
    amount of client-side inference turns them into one.
    """
    timeline = await AgentRunQueryService(session).timeline(run_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail=f"运行 {run_id} 不存在")
    steps = timeline["steps"]
    orphans = timeline["orphaned_tool_calls"]
    return {
        "run_id": run_id,
        "count": len(steps),
        "tool_call_count": sum(len(s["tool_calls"]) for s in steps) + len(orphans),
        "steps": steps,
        "orphaned_tool_calls": orphans,
    }


@router.get("/runs/{run_id}/events")
async def get_run_events(
    run_id: str,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2000),
    session: AsyncSession = Depends(async_session),
) -> dict:
    """Replay the run's persisted event log, ascending by ``seq``.

    ``?after_seq=N`` is the same cursor ``Last-Event-ID`` uses on the stream,
    so a client that lost its connection can catch up over plain REST and only
    then reopen the stream.
    """
    events = await AgentRunQueryService(session).events(run_id, after_seq=after_seq, limit=limit)
    if events is None:
        raise HTTPException(status_code=404, detail=f"运行 {run_id} 不存在")
    return {
        "run_id": run_id,
        "after_seq": after_seq,
        "count": len(events),
        "last_seq": events[-1]["seq"] if events else after_seq,
        "events": events,
    }


@router.get("/runs/{run_id}/stream")
async def stream_run_events(
    request: Request,
    run_id: str,
    after_seq: int | None = Query(default=None, ge=0),
    session: AsyncSession = Depends(async_session),
) -> StreamingResponse:
    """Live SSE stream of the run's events, resumable via ``Last-Event-ID``.

    Completeness is the database's job, not the bus's: whatever is already
    committed is replayed from the table first, then the in-process bus
    supplies new frames. A client that never connected during the run still
    gets the whole timeline, and one that reconnects gets everything it
    missed rather than a hole.
    """
    run = await AgentRunRepository(session).get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行 {run_id} 不存在")

    # Explicit cursor wins; otherwise honour a reconnecting EventSource's
    # header. Release this session's read transaction before streaming — it is
    # held for the whole response, and the polls below use their own
    # short-lived sessions on purpose.
    raw_cursor = after_seq if after_seq is not None else request.headers.get("last-event-id")
    cursor = parse_last_event_id(raw_cursor)
    await session.commit()

    async def _fetch_events(after: int) -> list[dict]:
        async with async_session_factory() as poll_session:
            return (
                await AgentRunQueryService(poll_session).events(
                    run_id, after_seq=after, limit=_STREAM_REPLAY_LIMIT
                )
                or []
            )

    async def _fetch_status() -> str:
        async with async_session_factory() as poll_session:
            row = await AgentRunRepository(poll_session).get(run_id)
            return getattr(row.status, "value", "") if row is not None else ""

    return StreamingResponse(
        event_stream(
            run_id=run_id,
            bus=get_event_bus(),
            fetch_events=_fetch_events,
            fetch_status=_fetch_status,
            after_seq=cursor,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # Proxies that buffer would defeat the point of streaming.
            "X-Accel-Buffering": "no",
        },
    )


async def _decide(
    session: AsyncSession, approval_id: str, decision: str, note: str | None
) -> dict:
    """Shared approve/reject path: decide → persist → resume the graph."""
    repo = AgentRunRepository(session)
    approval = await repo.get_approval(approval_id)
    if approval is None:
        raise HTTPException(
            status_code=404, detail=f"审批 {approval_id} 不存在"
        )
    run_id = approval["run_id"]
    if not run_id:
        raise HTTPException(
            status_code=409, detail="该审批没有关联到任何一次运行"
        )

    async with _lock_for(run_id):
        # Re-read inside the lock: a concurrent request may have decided
        # while this one was waiting.
        approval = await repo.get_approval(approval_id)
        if approval is None:
            raise HTTPException(
                status_code=404, detail=f"审批 {approval_id} 不存在"
            )
        if approval["status"] != "pending":
            raise HTTPException(
                status_code=409,
                detail="该审批不处于 'pending' 状态",
            )

        await repo.decide_approval(
            approval_id, decision=decision, decided_by="operator", note=note
        )
        # Persist the decision before resuming — the resumed run re-reads
        # the approval from the database in its own session.
        await session.commit()

        runtime = AgentRuntimeService(session)
        await runtime.resume(run_id, {"decision": decision})

        payload = await AgentRunQueryService(session).get_run(run_id)
    return payload


@router.post("/approvals/{approval_id}/approve")
async def approve_approval(
    approval_id: str, session: AsyncSession = Depends(async_session)
) -> dict:
    """Approve a pending recovery and let the agent proceed."""
    return await _decide(session, approval_id, "approve", None)


@router.post("/approvals/{approval_id}/reject")
async def reject_approval(
    approval_id: str,
    body: ApprovalRespondRequest | None = None,
    session: AsyncSession = Depends(async_session),
) -> dict:
    """Reject a pending recovery — the run ends without executing actions."""
    reason = body.reason if body and body.reason else "操作人员拒绝"
    return await _decide(session, approval_id, "reject", reason)
