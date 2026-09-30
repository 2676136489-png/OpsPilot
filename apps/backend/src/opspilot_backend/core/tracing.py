"""Distributed tracing primitives — request_id / trace_id / span_id everywhere.

When an incident response fails, the question is always *which layer failed*:
the HTTP call, the agent node, the MCP round-trip, the tool, or the database.
Answering that needs one identifier carried across all of them, so this module
owns them and everything else borrows from here.

Deliberately dependency-free (no OpenTelemetry SDK required). The shape is
compatible with OTel — hex trace ids, parent/child spans, attributes — so
swapping in a real exporter later does not touch the call sites.

Usage
-----
::

    with span("tool.call", kind="tool", tool=name) as s:
        ...
        s.attribute("attempts", 2)

    # or, for async scope spanning an await:
    async with aspan("agent.node", kind="node", stage=stage):
        ...
"""

from __future__ import annotations

import contextvars
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from opspilot_backend.core.logging import log_event, set_request_id, set_run_id, set_trace_id

#: Ceiling on buffered spans per run. A chatty run (every node, tool and DB
#: query emits one) must not be able to grow this without bound.
_MAX_BUFFERED_SPANS = 2000

# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


def new_trace_id() -> str:
    """128-bit trace id, hex — the same shape OTel and W3C use."""
    return uuid.uuid4().hex


def new_span_id() -> str:
    """64-bit span id, hex."""
    return uuid.uuid4().hex[:16]


_trace_id = new_trace_id  # internal alias kept for readability at call sites


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceContext:
    """The identifiers currently in scope."""

    trace_id: str = ""
    span_id: str = ""
    request_id: str = ""

    def child(self, span_id: str) -> "TraceContext":
        return TraceContext(self.trace_id, span_id, self.request_id)

    def as_dict(self) -> dict[str, str]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "request_id": self.request_id,
        }


_current: contextvars.ContextVar[TraceContext] = contextvars.ContextVar(
    "opspilot_trace_context", default=TraceContext()
)

#: Spans completed in this context but not yet flushed anywhere.
#: ``default=None`` on purpose — a mutable default would be shared by every
#: context, so one run could drain another run's spans.
_COMPLETED: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "opspilot_trace_spans", default=None
)

#: Roll-up buckets for suppressed (fast) DB queries, keyed by parent span id.
#: A single incident run issues hundreds of statements; persisting every one of
#: them buries the twelve spans that explain what happened.
_DB_AGG: contextvars.ContextVar[dict[str, dict[str, Any]] | None] = contextvars.ContextVar(
    "opspilot_db_agg", default=None
)

#: While set, spans are dropped instead of recorded. Used around the trace
#: flush itself, so writing the trace does not add to the trace.
_SUSPENDED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "opspilot_trace_suspended", default=False
)


def _sync_logging(ctx: TraceContext) -> None:
    """Mirror the trace context into the logging contextvars.

    Two separate ContextVar families would drift the first time a call site
    updated one and forgot the other. Keeping the mirror inside the only two
    functions that mutate the context means a log line can never disagree with
    the span that produced it.
    """
    try:
        set_trace_id(ctx.trace_id or "-")
        set_request_id(ctx.request_id or "-")
    except Exception:  # pragma: no cover - telemetry must never break a run
        pass


def current_context() -> TraceContext:
    return _current.get()


def current_ids() -> dict[str, str]:
    """The triple every log line and DB row should carry."""
    return _current.get().as_dict()


def bind_run_id(run_id: str) -> None:
    """Tag every subsequent log line with the agent run that produced it."""
    try:
        set_run_id(run_id or "-")
    except Exception:  # pragma: no cover
        pass


def parse_traceparent(value: str) -> str:
    """Extract the trace id from a W3C ``traceparent`` header.

    Returns ``""`` for anything malformed, so a hostile or buggy client can
    only ever cost us the correlation id — never the request.
    """
    parts = (value or "").split("-")
    if len(parts) == 4 and len(parts[1]) == 32:
        try:
            int(parts[1], 16)
        except ValueError:
            return ""
        return parts[1]
    return ""


def bind(
    *,
    trace_id: str | None = None,
    span_id: str | None = None,
    request_id: str | None = None,
) -> contextvars.Token:
    """Install a new trace context. Reset with ``unbind(token)``."""
    current = _current.get()
    ctx = TraceContext(
        trace_id=trace_id or current.trace_id or _trace_id(),
        span_id=span_id or current.span_id or new_span_id(),
        request_id=request_id or current.request_id or uuid.uuid4().hex[:16],
    )
    token = _current.set(ctx)
    _sync_logging(ctx)
    return token


def unbind(token: contextvars.Token) -> None:
    _current.reset(token)
    _sync_logging(_current.get())


# ---------------------------------------------------------------------------
# Spans
# ---------------------------------------------------------------------------


@dataclass
class Span:
    """One unit of work. Cheap to create, cheap to discard."""

    name: str
    kind: str = "internal"
    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str = ""
    request_id: str = ""
    started_at: float = field(default_factory=time.perf_counter)
    duration_ms: float = 0.0
    status: str = "ok"
    error: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def fail(self, message: str) -> None:
        self.status = "error"
        self.error = message

    def finish(self) -> None:
        self.duration_ms = round((time.perf_counter() - self.started_at) * 1000, 3)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "request_id": self.request_id,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "error": self.error,
            "attributes": dict(self.attributes),
        }


@contextmanager
def span(
    name: str, kind: str = "internal", root: bool = False, **attributes: Any
) -> Iterator[Span]:
    """Sync span. Restores the previous span id on exit.

    ``root=True`` detaches the span from whatever is in scope — for a span that
    legitimately begins a trace (or continues it in a new leg) and would
    otherwise point at a parent that exists nowhere in the store.
    """
    parent = _current.get()
    current = Span(
        name=name,
        kind=kind,
        trace_id=parent.trace_id or _trace_id(),
        span_id=new_span_id(),
        parent_span_id="" if root else parent.span_id,
        request_id=parent.request_id,
        attributes=dict(attributes),
    )
    token = _current.set(parent.child(current.span_id))
    try:
        yield current
    except Exception as exc:  # noqa: BLE001 - a span records, it does not decide
        current.fail(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        current.finish()
        unbind(token)
        _record(current)


@asynccontextmanager
async def aspan(name: str, kind: str = "internal", root: bool = False, **attributes: Any):
    """Async variant of :func:`span` — must wrap awaits, or concurrent runs
    would overwrite each other's context."""
    with span(name, kind=kind, root=root, **attributes) as current:
        yield current


@contextmanager
def suspend_recording() -> Iterator[None]:
    """Drop spans recorded inside the block.

    Wrap anything that *writes* the trace — persisting spans, flushing buffers
    — because otherwise the trace would record its own bookkeeping.
    """
    token = _SUSPENDED.set(True)
    try:
        yield
    finally:
        _SUSPENDED.reset(token)


def begin_span_collection() -> list[dict[str, Any]]:
    """Install a fresh collector for the current run.

    The collector is deliberately a *single mutable list*: LangGraph executes
    nodes in tasks that copy the context, and a copy-on-write value would have
    every child append to a list the parent can no longer see. Sharing the
    reference is what makes ``drain_spans()`` at the end of the run return
    spans recorded by nodes, tools and DB listeners alike.

    The DB roll-up buckets are installed here too, for the same reason — a
    bucket lazily created inside a child task belongs to that child alone, and
    the finished run would report no database activity at all.
    """
    buffer: list[dict[str, Any]] = []
    _COMPLETED.set(buffer)
    _DB_AGG.set({})
    return buffer


def _buffer() -> list[dict[str, Any]]:
    buffer = _COMPLETED.get()
    if buffer is None:
        buffer = begin_span_collection()
    return buffer


def record_span(
    name: str,
    *,
    kind: str = "internal",
    duration_ms: float = 0.0,
    status: str = "ok",
    error: str | None = None,
    **attributes: Any,
) -> None:
    """Record a span that was measured elsewhere.

    The context-manager form cannot be used when the start and the end arrive
    in two separate callbacks — which is exactly the shape of a SQLAlchemy
    cursor event, where the driver owns the timing.
    """
    ctx = _current.get()
    _record(
        Span(
            name=name,
            kind=kind,
            trace_id=ctx.trace_id or _trace_id(),
            span_id=new_span_id(),
            parent_span_id=ctx.span_id,
            request_id=ctx.request_id,
            duration_ms=round(float(duration_ms), 3),
            status=status,
            error=error,
            attributes=dict(attributes),
        )
    )


def record_db_span(
    *,
    statement: str,
    operation: str,
    duration_ms: float,
    dialect: str,
    executemany: bool = False,
    min_ms: float = 0.0,
) -> None:
    """Record one SQL statement, rolling the fast ones up per parent span.

    A run issues hundreds of statements — every step, event and evidence row is
    one — and persisting all of them buries the handful that explain the
    incident. So a statement faster than ``min_ms`` is *counted* rather than
    stored, and ``drain_spans`` emits one ``db.query.batch`` per parent with the
    totals plus the slowest statement text. Nothing is lost: a node's total
    database cost stays visible, and every query slow enough to matter keeps
    its own span.
    """
    if min_ms and duration_ms < min_ms:
        bucket = _aggregate_bucket()
        bucket["statements"] += 1
        bucket["total_ms"] += duration_ms
        if duration_ms > bucket["max_ms"]:
            bucket["max_ms"] = duration_ms
            bucket["slowest"] = statement
        return
    record_span(
        "db.query",
        kind="db",
        duration_ms=duration_ms,
        dialect=dialect,
        operation=operation,
        # Truncated on purpose: a span is a pointer to the query, not a copy of
        # the data. Full statements (and their parameters) stay out of the store.
        statement=statement,
        executemany=bool(executemany),
    )


def _aggregate_bucket() -> dict[str, Any]:
    """The roll-up bucket for the span currently in scope.

    Keyed by the *parent* span, so a node's queries and the ones its tool calls
    made roll up separately instead of into one global pile.
    """
    groups = _DB_AGG.get()
    if groups is None:
        groups = {}
        _DB_AGG.set(groups)
    parent = _current.get().span_id
    return groups.setdefault(
        parent,
        {"statements": 0, "total_ms": 0.0, "max_ms": 0.0, "slowest": ""},
    )


def _record(finished: Span) -> None:
    """Buffer the span so the run can persist them, and log it.

    Persisting synchronously would mean a DB write inside a tool call, which
    is the last thing you want in the hot path of an incident.

    Entirely exception-safe on purpose: this runs from a ``finally`` block, so
    anything raised here would replace the caller's real result — a tracing
    bug must never be the reason a recovery fails.
    """
    try:
        if _SUSPENDED.get():
            return
        payload = finished.as_dict()
        buffer = _buffer()
        if len(buffer) < _MAX_BUFFERED_SPANS:
            buffer.append(payload)
    except Exception:  # pragma: no cover - see docstring
        return
    try:
        log_event(
            "trace.span",
            name=payload["name"],
            kind=payload["kind"],
            duration_ms=payload["duration_ms"],
            status=payload["status"],
            error=payload["error"],
            trace_id=payload["trace_id"],
            span_id=payload["span_id"],
            parent_span_id=payload["parent_span_id"],
            request_id=payload["request_id"],
            # Nested, never splatted: a span attribute is caller-supplied, and
            # splatting it into a ``**fields`` collector lets any attribute
            # named ``status`` collide with the keyword above and raise from
            # inside the span's own ``finally``.
            attributes=payload["attributes"],
        )
    except Exception:  # pragma: no cover - logging must never break a run
        pass


def drain_spans() -> list[dict[str, Any]]:
    """Return and clear the spans recorded in this context.

    Rolled-up DB batches are materialised here rather than at query time: a
    batch's parents (node, tool) are only fully known once they have finished,
    and one aggregate per parent is what keeps the trace readable.
    """
    buffer = _COMPLETED.get()
    _COMPLETED.set([])
    out = list(buffer or ())

    groups = _DB_AGG.get() or {}
    _DB_AGG.set(None)
    ctx = _current.get()
    for parent_span_id, bucket in groups.items():
        out.append(
            Span(
                name="db.query.batch",
                kind="db.batch",
                trace_id=ctx.trace_id or _trace_id(),
                span_id=new_span_id(),
                parent_span_id=parent_span_id,
                request_id=ctx.request_id,
                duration_ms=round(bucket["total_ms"], 3),
                attributes={
                    "statements": bucket["statements"],
                    "max_ms": round(bucket["max_ms"], 3),
                    "slowest": bucket["slowest"],
                    "rolled_up": True,
                },
            ).as_dict()
        )
    return out


def trace_headers() -> dict[str, str]:
    """W3C ``traceparent`` — what gets handed to the simulator / MCP server."""
    ctx = _current.get()
    if not ctx.trace_id:
        return {}
    return {
        "traceparent": f"00-{ctx.trace_id}-{ctx.span_id or new_span_id()}-01",
        "X-Request-Id": ctx.request_id,
    }


__all__ = [
    "Span",
    "TraceContext",
    "aspan",
    "begin_span_collection",
    "bind",
    "bind_run_id",
    "current_context",
    "current_ids",
    "drain_spans",
    "new_span_id",
    "new_trace_id",
    "parse_traceparent",
    "record_db_span",
    "record_span",
    "span",
    "suspend_recording",
    "trace_headers",
    "unbind",
]
