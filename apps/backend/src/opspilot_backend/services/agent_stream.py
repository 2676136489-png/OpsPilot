"""Operator-visible projection of the agent event log, and the live SSE stream.

The write path already persists one ``AgentEvent`` row per notable step and
pushes the same payload onto an in-process :class:`EventBus`. What was missing
is a *read* path: the frontend had no way to obtain the real step-by-step
timeline, so it invented one by re-reading a run snapshot and stamping
fabricated timestamps onto it (see ``AgentStreamTimeline.synthesiseBacklog``
before this module existed).

Two things live here.

**Projection.** Every byte that reaches a client goes through
:func:`project_event`, which copies the envelope from real columns and then
keeps only the ``data`` keys that
:data:`EVENT_FIELDS` declares for that event type. Keys absent from the table
are dropped, and an event type that is not in the table at all has its ``data``
withheld entirely (the row still appears — an undeclared event is visible as a
bare occurrence, never as a silent hole). This is deliberate: event payloads
are written at a dozen call sites inside the agent, and "add whatever seems
useful to the payload" must not be able to widen the public API by accident.
A test enforces that every event type the UI depends on is declared.

Nothing here is a model's hidden reasoning. The fields that look like
explanations (``rationale`` on a plan, ``reasoning`` on a hypothesis) are
first-class columns the system already commits to and already returns through
``GET /agent/runs/{id}`` — they are part of the answer, not the scratchpad.

**Streaming.** :func:`event_stream` yields already-formatted SSE frames. It is
deliberately transport-shaped but framework-free (no FastAPI import) so it can
be exercised directly in a test. Ordering and completeness come from the
database, not from the bus:

* whatever is already committed is replayed first, in ``seq`` order;
* the bus then supplies new frames live;
* every frame is de-duplicated by ``seq``, so an event delivered by both paths
  arrives once;
* a disconnect and a reconnect are the same code path — a client that sends
  ``Last-Event-ID`` gets everything after it, which is exactly the guarantee
  the ``AgentEvent.seq`` column was added for.

The stream closes itself once the run reaches a terminal state and the log is
drained, so a finished investigation does not pin a connection open.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Iterable

from opspilot_backend.core.logging import log_event
from opspilot_backend.domain.enums import AgentRunStatus, EventType
from opspilot_backend.services.events import EventBus

# ---------------------------------------------------------------------------
# Projection table
# ---------------------------------------------------------------------------

_E = EventType

#: ``data`` keys each event type may expose to a client.
#:
#: A value of ``None`` means "not declared" — the event is still streamed, but
#: its payload is withheld until someone decides what is safe to publish.
#: Declaring a type is a deliberate act; see the module docstring.
EVENT_FIELDS: dict[str, frozenset[str] | None] = {
    # -- run lifecycle -------------------------------------------------
    _E.AGENT_STARTED.value: frozenset(),
    _E.AGENT_STEP_STARTED.value: frozenset({"attempt"}),
    _E.AGENT_STEP_COMPLETED.value: frozenset({"duration_ms"}),
    _E.AGENT_COMPLETED.value: frozenset(),
    _E.AGENT_FAILED.value: frozenset({"error", "errors"}),
    _E.RUN_ESCALATED.value: frozenset(
        {"reason", "outcome", "evidence_count", "hypotheses"}
    ),
    _E.BUDGET_EXHAUSTED.value: frozenset({"tool", "reason", "spent"}),
    # -- investigation -------------------------------------------------
    _E.INVESTIGATION_STARTED.value: frozenset({"service", "severity"}),
    _E.INVESTIGATION_PLAN_CREATED.value: frozenset(
        {
            "iteration",
            "tools",
            "targets",
            "domains",
            "top_domain",
            "confidence",
            "rationale",
            "saturated",
            "budget",
            "decision",
            "reason",
            "evidence_gained",
        }
    ),
    _E.EVIDENCE_CREATED.value: frozenset(
        {"ref", "type", "title", "severity", "confidence", "relevance", "source", "service"}
    ),
    _E.HYPOTHESIS_CREATED.value: frozenset(
        {
            "ref",
            "statement",
            "domain",
            "category",
            "confidence",
            "evidence_refs",
            "count",
            "reason",
        }
    ),
    _E.HYPOTHESIS_UPDATED.value: frozenset(
        {"ref", "statement", "domain", "status", "confidence", "evidence_refs", "reasoning"}
    ),
    _E.HYPOTHESIS_REJECTED.value: frozenset(
        {"ref", "statement", "domain", "status", "confidence", "evidence_refs", "reasoning"}
    ),
    _E.DIAGNOSIS_COMPLETED.value: frozenset(
        {
            "root_cause",
            "category",
            "domain",
            "confidence",
            "evidence_refs",
            "outcome",
            "actionable",
            "escalation_reason",
            "reasoning_summary",
        }
    ),
    # -- tools ---------------------------------------------------------
    _E.TOOL_STARTED.value: frozenset({"tool_name", "arguments", "risk_level"}),
    _E.TOOL_COMPLETED.value: frozenset(
        {"tool_name", "ok", "result_summary", "duration_ms", "attempts"}
    ),
    _E.TOOL_FAILED.value: frozenset(
        {"tool_name", "ok", "error_code", "error_message", "duration_ms", "attempts"}
    ),
    # -- risk / approval ----------------------------------------------
    _E.RISK_ASSESSED.value: frozenset(
        {
            "risk_level",
            "approval_tier",
            "requires_approval",
            "requires_reverification",
            "manual_only",
            "by_action",
        }
    ),
    _E.APPROVAL_REQUIRED.value: frozenset(
        {"approval_id", "risk_level", "approval_tier", "requires_reverification",
         "manual_only", "actions", "tool_name", "arguments"}
    ),
    _E.APPROVAL_DECIDED.value: frozenset({"approval_id", "decision", "decided_by", "note"}),
    # -- recovery ------------------------------------------------------
    _E.RECOVERY_PLAN_CREATED.value: frozenset(
        {"plan_id", "actions", "approval_tier", "risk_level", "verification_criteria"}
    ),
    _E.RECOVERY_STARTED.value: frozenset({"plan_id", "approval_tier", "actions"}),
    _E.RECOVERY_ACTION_COMPLETED.value: frozenset(
        {
            "ref",
            "tool",
            "target",
            "status",
            "effective",
            "error",
            "removed_faults",
            "resisted_faults",
        }
    ),
    _E.RECOVERY_COMPLETED.value: frozenset({"executed", "effective_ref"}),
    _E.RECOVERY_FAILED.value: frozenset(
        {"reason", "executed", "actions", "checks", "rollback_targets"}
    ),
    _E.RECOVERY_ROLLBACK_STARTED.value: frozenset({"compensated", "refused_or_failed"}),
    _E.RECOVERY_ROLLBACK_COMPLETED.value: frozenset(
        {"ref", "tool", "status", "error", "reason"}
    ),
    # -- verification / follow-up --------------------------------------
    _E.VERIFICATION_STARTED.value: frozenset({"targets"}),
    _E.VERIFICATION_COMPLETED.value: frozenset(
        {"status", "passed_checks", "total_checks", "checks", "reason"}
    ),
    _E.POSTMORTEM_CREATED.value: frozenset({"postmortem_id", "summary"}),
    # -- transport-only ------------------------------------------------
    _E.HEARTBEAT.value: frozenset(),
    _E.STATE_SYNC.value: frozenset(),
    # Incident lifecycle events are written to the *incident* timeline
    # (``incident_events``) by ``IncidentRepository``, not to ``agent_events``,
    # and are served by the incidents API. Declared as ``None`` so the table in
    # this module stays a total map over ``EventType`` and the split between
    # the two logs is stated rather than implied by an absence.
    _E.INCIDENT_STATUS_CHANGED.value: None,
    _E.INCIDENT_RESOLVED.value: None,
}

#: Event types deliberately absent from :data:`EVENT_FIELDS` because they
#: belong to a different log. Kept as a named set so a test can assert the
#: projection table covers every ``EventType`` except these.
OTHER_LOG_EVENTS = frozenset(
    {_E.INCIDENT_STATUS_CHANGED.value, _E.INCIDENT_RESOLVED.value}
)

#: Frames the stream emits that are *not* domain events. They carry no
#: ``seq`` (nothing is persisted for them) and exist so the client can tell
#: "the investigation is over" apart from "the connection blipped".
STREAM_OPENED = "stream.opened"
STREAM_CLOSED = "stream.closed"

_TERMINAL_RUN_STATUSES = frozenset(
    {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED}
)


def visible_data(event_type: str, data: Any) -> dict[str, Any]:
    """Keep only the declared fields for ``event_type``.

    Undeclared types yield ``{}``; declared types yield the intersection. A
    missing key is simply absent — the frontend treats absent as "not
    reported", which is the same contract the rest of the API uses.
    """
    allowed = EVENT_FIELDS.get(str(event_type))
    if not allowed or not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if key in allowed}


def project_event(event: Any) -> dict[str, Any]:
    """Database row → the operator-visible event frame.

    The envelope is taken from columns, never from the payload, so a payload
    cannot forge a ``run_id`` or a ``seq``.
    """
    return {
        "seq": int(event.seq),
        "event_id": str(event.event_id),
        "run_id": str(event.run_id) if event.run_id else "",
        "incident_id": str(event.incident_id) if event.incident_id else "",
        "event_type": str(event.event_type),
        "stage": event.stage,
        "data": visible_data(event.event_type, event.data),
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


def project_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Bus payload → the same frame :func:`project_event` produces.

    The bus carries the dict :meth:`RepositoryToolHooks._payload` built, which
    is intentionally unfiltered (it is also what gets written to the row). The
    live path therefore has to re-apply the projection, or the SSE client would
    see more than a reconnecting client replaying from the table.
    """
    event_type = str(payload.get("event_type", ""))
    return {
        "seq": int(payload.get("seq") or 0),
        "event_id": str(payload.get("event_id", "")),
        "run_id": str(payload.get("run_id", "")),
        "incident_id": str(payload.get("incident_id") or ""),
        "event_type": event_type,
        "stage": payload.get("stage"),
        "data": visible_data(event_type, payload.get("data")),
        "created_at": payload.get("created_at"),
    }


# ---------------------------------------------------------------------------
# SSE framing
# ---------------------------------------------------------------------------


def _frame(event_type: str, payload: dict[str, Any], *, event_id: str = "") -> str:
    """One SSE message.

    ``id:`` is the row's ``seq`` so a reconnecting client's ``Last-Event-ID``
    header is directly usable as the replay cursor.
    """
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_type}")
    lines.append(f"data: {json.dumps(payload, default=str, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


def comment(text: str) -> str:
    """An SSE comment — ignored by ``EventSource``, keeps proxies awake."""
    return f": {text}\n\n"


def control_payload(
    event_type: str, run_id: str, *, data: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    """Envelope for the stream's own control frames.

    These two frames are not domain events — nothing is persisted, so they have
    no ``seq`` — but the client-side vocabulary treats every frame it receives
    as one (`EVENT_META` lists both, `describeEvent` writes copy for both), and
    that is the right call: "已连接事件流" belongs on the same timeline as the
    investigation it opens.

    So they have to carry the same envelope as everything else. They used to
    send a bare payload — no ``event_type``, no ``data`` — and a client that
    trusted its own declared type fell over the moment it rendered one:
    ``Object.entries(undefined)``. The frame is the producer's contract; the
    keys already established (``last_event_id``, ``reason``) stay at the top
    level for callers that read them there, and are mirrored into ``data`` so
    the frame is a valid event as well.
    """
    return {
        # No cursor: replay would duplicate a frame that was never stored.
        "seq": 0,
        "event_id": "",
        "run_id": run_id,
        "incident_id": "",
        "event_type": event_type,
        "stage": None,
        "data": data,
        "created_at": None,
        **extra,
    }


def parse_last_event_id(raw: Any) -> int:
    """``Last-Event-ID`` (or the ``after_seq`` query param) → a cursor.

    Anything unparseable means "start from the beginning" rather than "guess":
    replaying is idempotent, skipping is not.
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


# ---------------------------------------------------------------------------
# The stream
# ---------------------------------------------------------------------------


async def event_stream(
    *,
    run_id: str,
    bus: EventBus,
    fetch_events: Any,
    fetch_status: Any,
    after_seq: int = 0,
    poll_interval: float = 1.0,
    heartbeat_interval: float = 15.0,
    max_idle_seconds: float = 900.0,
) -> AsyncIterator[str]:
    """Yield SSE frames for one run until it finishes or the client leaves.

    ``fetch_events(after_seq)`` must return already-committed frames in the
    shape :func:`project_event` produces, ascending by ``seq``.
    ``fetch_status()`` returns the run's status as a string. Both are injected
    so this function needs no session of its own — the caller owns the
    connection, and each call is short-lived on purpose so the stream never
    holds a read transaction open while it waits.

    ``max_idle_seconds`` bounds the connection: a run that is somehow never
    marked terminal must not leave a socket open forever. The client is told
    why via ``stream.closed``.
    """
    queue = bus.subscribe(run_id)
    cursor = max(0, int(after_seq))
    started = time.monotonic()
    last_beat = started

    try:
        status = str(await fetch_status() or "")
        yield _frame(
            STREAM_OPENED,
            control_payload(
                STREAM_OPENED,
                run_id,
                data={"status": status, "last_event_id": cursor},
                status=status,
                last_event_id=cursor,
            ),
        )

        while True:
            # 1. Everything already committed, in seq order.
            pending: Iterable[dict[str, Any]] = await fetch_events(cursor) or ()
            drained_any = False
            for frame in pending:
                seq = int(frame.get("seq") or 0)
                if seq <= cursor:
                    continue  # the bus may have delivered this one already
                cursor = seq
                drained_any = True
                yield _frame(
                    str(frame.get("event_type", "message")), frame, event_id=str(seq)
                )

            status = str(await fetch_status() or "")
            terminal = status in {s.value for s in _TERMINAL_RUN_STATUSES}
            if terminal and not drained_any and queue.empty():
                yield _frame(
                    STREAM_CLOSED,
                    control_payload(
                        STREAM_CLOSED,
                        run_id,
                        data={"status": status, "reason": "terminal"},
                        status=status,
                        reason="terminal",
                    ),
                )
                return

            if time.monotonic() - started > max_idle_seconds:
                log_event(
                    "agent.stream.idle_timeout", run_id=run_id, timeout=max_idle_seconds
                )
                yield _frame(
                    STREAM_CLOSED,
                    control_payload(
                        STREAM_CLOSED,
                        run_id,
                        data={"status": status, "reason": "idle_timeout"},
                        status=status,
                        reason="idle_timeout",
                    ),
                )
                return

            # 2. Live frames. The timeout is what lets a terminal run with no
            #    further traffic still close instead of blocking forever.
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=poll_interval)
            except asyncio.TimeoutError:
                now = time.monotonic()
                if now - last_beat >= heartbeat_interval:
                    last_beat = now
                    yield comment(f"keepalive {status}")
                continue

            frame = project_payload(payload)
            seq = int(frame["seq"])
            if seq and seq <= cursor:
                continue  # already replayed from the table
            if seq:
                cursor = seq
            yield _frame(
                str(frame["event_type"] or "message"), frame, event_id=str(seq) if seq else ""
            )
    finally:
        # Runs on client disconnect too — ``asyncio.CancelledError`` propagates
        # through the generator and this is the only place that can release the
        # queue.
        bus.unsubscribe(run_id, queue)


__all__ = [
    "EVENT_FIELDS",
    "OTHER_LOG_EVENTS",
    "STREAM_CLOSED",
    "STREAM_OPENED",
    "comment",
    "event_stream",
    "parse_last_event_id",
    "project_event",
    "project_payload",
    "visible_data",
]
