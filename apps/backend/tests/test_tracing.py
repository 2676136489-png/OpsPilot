"""Distributed tracing — the test that proves the ids actually connect layers.

Requirement: every HTTP request, agent node, tool call, MCP round-trip and DB
query carries ``request_id / trace_id / span_id``, and the whole thing composes
into one chain:

    HTTP Request → Agent Run → LangGraph Node → MCP → Tool → Database/Simulator

These tests drive a *real* run against the in-process simulator and then read
the spans back out of the database. Nothing is asserted about a mock: if the
collector did not survive LangGraph's task boundaries, or the parent ids did
not line up, these fail.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import select, text

from opspilot_backend.agent.checkpointer import DatabaseCheckpointer
from opspilot_backend.core.logging import set_run_id
from opspilot_backend.core.tracing import (
    aspan,
    begin_span_collection,
    bind_run_id,
    current_ids,
    drain_spans,
    parse_traceparent,
    record_db_span,
)
from opspilot_backend.models import Incident, Service
from opspilot_backend.repositories.agent_run import AgentRunRepository
from opspilot_backend.repositories.incident import IncidentRepository
from opspilot_backend.services.agent_runtime import AgentRuntimeService

SERVICE_NAME = "payment-service"
SCENARIO = "payment-bad-deployment"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_parse_traceparent_accepts_only_well_formed_values() -> None:
    good = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    assert parse_traceparent(good) == "a" * 32
    # Malformed input must cost the correlation id, never raise into a request.
    assert parse_traceparent("") == ""
    assert parse_traceparent("garbage") == ""
    assert parse_traceparent("00-short-b-01") == ""
    assert parse_traceparent("00-" + "z" * 32 + "-" + "b" * 16 + "-01") == ""


# ---------------------------------------------------------------------------
# HTTP middleware
# ---------------------------------------------------------------------------


async def _probe_app(monkeypatch) -> tuple[FastAPI, dict, list[tuple[str, str]]]:
    """A minimal app carrying the real trace middleware.

    The persistence sink is replaced, not the middleware: this test is about
    the binding + header contract. That spans actually reach the database is
    asserted against a real run below.
    """
    import opspilot_backend.main as main_module

    persisted: list[tuple[str, str]] = []

    async def fake_persist(request: Request, span_id: str) -> None:
        persisted.append((request.method, span_id))

    monkeypatch.setattr(main_module, "_persist_request_spans", fake_persist)

    app = FastAPI()
    app.middleware("http")(main_module.trace_context)
    seen: dict = {}

    @app.get("/ping")
    async def ping() -> dict:
        seen.update(current_ids())
        return {"ok": True}

    @app.post("/poke")
    async def poke() -> dict:
        seen.update(current_ids())
        return {"ok": True}

    return app, seen, persisted


async def test_middleware_inherits_inbound_trace_and_echoes_ids(monkeypatch) -> None:
    app, seen, _ = await _probe_app(monkeypatch)
    inbound = "c" * 32
    headers = {
        "traceparent": f"00-{inbound}-{'d' * 16}-01",
        "X-Request-Id": "req-from-client",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://probe"
    ) as client:
        response = await client.get("/ping", headers=headers)

    assert response.status_code == 200
    # Continuation, not replacement: an inbound trace is adopted.
    assert response.headers["x-trace-id"] == inbound
    assert response.headers["x-request-id"] == "req-from-client"
    assert response.headers["traceparent"].split("-")[1] == inbound
    # …and the handler really saw them, so everything downstream inherits them.
    assert seen["trace_id"] == inbound
    assert seen["request_id"] == "req-from-client"
    assert len(seen["span_id"]) == 16


async def test_middleware_mints_trace_when_header_is_missing_or_broken(monkeypatch) -> None:
    app, _, _ = await _probe_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://probe"
    ) as client:
        first = await client.get("/ping")
        second = await client.get("/ping", headers={"traceparent": "nonsense"})

    for response in (first, second):
        trace_id = response.headers["x-trace-id"]
        assert len(trace_id) == 32 and all(c in "0123456789abcdef" for c in trace_id)
        assert response.headers["x-request-id"]
    assert first.headers["x-trace-id"] != second.headers["x-trace-id"]


async def test_http_span_is_persisted_for_writes_only(monkeypatch) -> None:
    app, _, persisted = await _probe_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://probe"
    ) as client:
        await client.get("/ping")
        await client.post("/poke", json={})

    # Reads are the polling traffic; persisting them would drown the store.
    assert [m for m, _ in persisted] == ["POST"]


# ---------------------------------------------------------------------------
# DB query spans
# ---------------------------------------------------------------------------


async def test_db_spans_are_recorded_only_inside_a_run(db_session) -> None:
    """The gate: an agent run's DB traffic is traced, a stray read is not."""
    begin_span_collection()
    await db_session.execute(text("SELECT 1"))
    assert drain_spans() == []

    try:
        bind_run_id("run-for-test")
        begin_span_collection()
        await db_session.execute(text("SELECT 1"))
        recorded = drain_spans()
    finally:
        set_run_id("-")

    assert recorded, "a query issued during a run must be traced"
    assert {s["kind"] for s in recorded} <= {"db", "db.batch"}


def test_fast_queries_roll_up_and_slow_ones_are_kept() -> None:
    """One incident issues hundreds of statements; the trace must stay readable."""
    begin_span_collection()
    record_db_span(
        statement="SELECT 1",
        operation="SELECT",
        duration_ms=0.2,
        dialect="sqlite",
        min_ms=5.0,
    )
    record_db_span(
        statement="SELECT pg_sleep(9)",
        operation="SELECT",
        duration_ms=900.0,
        dialect="postgresql",
        min_ms=5.0,
    )

    by_name = {s["name"]: s for s in drain_spans()}
    # The slow one keeps its own span, with its statement intact.
    assert by_name["db.query"]["attributes"]["dialect"] == "postgresql"
    assert by_name["db.query"]["duration_ms"] == 900.0
    # The fast one is counted, not stored — but it is not lost.
    batch = by_name["db.query.batch"]
    assert batch["attributes"]["statements"] == 1
    assert batch["attributes"]["slowest"] == "SELECT 1"
    assert batch["kind"] == "db.batch"


async def test_rollup_attributes_queries_to_their_own_parent() -> None:
    """Two nodes' DB cost must not be merged into one pile.

    Each node runs in its own task (as LangGraph does), so this also pins the
    fact that the roll-up buckets are shared by reference across tasks.
    """
    begin_span_collection()
    parents: dict[str, str] = {}

    async def node(name: str) -> None:
        async with aspan(f"node.{name}", kind="node") as parent:
            parents[name] = parent.span_id
            record_db_span(
                statement="SELECT 1",
                operation="SELECT",
                duration_ms=0.2,
                dialect="sqlite",
                min_ms=5.0,
            )

    await asyncio.create_task(node("a"))
    await asyncio.create_task(node("b"))

    recorded = drain_spans()
    batches = [s for s in recorded if s["kind"] == "db.batch"]
    assert len(batches) == 2
    assert {b["parent_span_id"] for b in batches} == set(parents.values())
    assert all(b["attributes"]["statements"] == 1 for b in batches)


# ---------------------------------------------------------------------------
# End-to-end: one run == one nested trace
# ---------------------------------------------------------------------------


async def _make_incident(session) -> Incident:
    service = Service(
        name=SERVICE_NAME, description="Payments API", tier="application", owner="payments"
    )
    session.add(service)
    await session.flush()
    repo = IncidentRepository(session)
    incident = await repo.create(
        title="Payment API error rate spike after v1.8.4",
        service_id=service.id,
        severity="SEV1",
        description="Automated alert: error_rate above threshold.",
        scenario=SCENARIO,
    )
    await session.commit()
    return incident


@pytest.mark.parametrize("simulator", [SCENARIO], indirect=True)
async def test_run_produces_one_nested_cross_layer_trace(
    db_session, session_factory, simulator
) -> None:
    incident = await _make_incident(db_session)
    runtime = AgentRuntimeService(
        db_session,
        session_factory=session_factory,
        checkpointer=DatabaseCheckpointer(session_factory),
    )
    run = await runtime.start(incident.id, background=False)

    runs = AgentRunRepository(db_session)
    stored = await runs.get(run.id)

    # The run row is self-describing: the trace it used is stored on it.
    assert stored.trace_id, "the run was not given a trace id"
    assert stored.request_id, "the run was not given a request id"

    spans = await runs.spans_for_run(run.id)
    assert spans, "no span survived the run — check the collector's task scope"

    # --- one trace, no stragglers -------------------------------------
    trace_ids = {s.trace_id for s in spans}
    assert trace_ids == {stored.trace_id}, trace_ids
    assert all(s.span_id for s in spans)
    assert all(s.request_id for s in spans)

    by_kind: dict[str, list] = {}
    for span in spans:
        by_kind.setdefault(span.kind, []).append(span)

    # --- every layer in the chain is present --------------------------
    assert by_kind.get("run"), "the agent run itself is not in the trace"
    assert by_kind.get("node"), "LangGraph nodes are missing"
    assert by_kind.get("tool"), "tool calls are missing"
    # The database leg is present whether it came through as individual spans
    # or as a roll-up — on a warm SQLite almost everything is sub-millisecond.
    assert by_kind.get("db") or by_kind.get("db.batch"), "the database leg is missing"
    assert by_kind.get("http.client"), "the simulator round-trip is missing"

    # --- every step points at the span that produced it ---------------
    steps = await runs.steps(run.id)
    node_span_ids = {s.span_id for s in by_kind["node"]}
    assert [s for s in steps if s.span_id in node_span_ids], (
        "an AgentStep does not point at its own span"
    )
    assert all(s.trace_id == stored.trace_id for s in steps if s.trace_id)

    # --- and they nest correctly --------------------------------------
    run_span = by_kind["run"][0]
    tool_ids = {s.span_id for s in by_kind["tool"]}

    assert all(s.parent_span_id == run_span.span_id for s in by_kind["node"])
    # Every tool call is made *by a node* — a tool span parented anywhere else
    # would mean a call path that bypasses the workflow.
    assert all(s.parent_span_id in node_span_ids for s in by_kind["tool"]), [
        (s.name, s.parent_span_id) for s in by_kind["tool"]
    ]
    # The simulator is called from inside a tool, not beside it.
    assert any(s.parent_span_id in tool_ids for s in by_kind["http.client"])

    # --- nothing floats, and the run is the single root ---------------
    valid_parents = {run_span.span_id} | node_span_ids | tool_ids
    for span in spans:
        if span.span_id == run_span.span_id:
            continue
        # A parent must be a span that actually exists in this trace — a
        # dangling id means part of the chain was lost between layers.
        assert span.parent_span_id in valid_parents, (span.name, span.parent_span_id)

    from opspilot_backend.api.v1.endpoints.agent import _span_payload, _span_tree

    tree = _span_tree([_span_payload(s) for s in spans])
    assert [node["name"] for node in tree] == ["agent.run"], [
        node["name"] for node in tree
    ]
    root = tree[0]
    children = {c["name"]: c for c in root["children"]}
    # Everything directly under the run is either a node or the run's own
    # bookkeeping (status writes, event emission) — never a tool call or an
    # outbound request escaping the workflow.
    assert {"node", "db", "db.batch"} >= {c["kind"] for c in root["children"]}, sorted(
        {c["kind"] for c in root["children"]}
    )
    assert any(name.startswith("node.") for name in children), list(children)
    # …and the depth really is run → node → tool → leaf.
    assert [
        c
        for n in root["children"]
        if n["kind"] == "node"
        for c in n["children"]
        if c["kind"] == "tool" and c["children"]
    ], "no tool span has children — the simulator/DB leaf is missing"


@pytest.mark.parametrize("simulator", [SCENARIO], indirect=True)
async def test_resume_continues_the_same_trace(
    db_session, session_factory, simulator
) -> None:
    """Approving a recovery must extend the trace, not start a second one."""
    incident = await _make_incident(db_session)
    runtime = AgentRuntimeService(
        db_session,
        session_factory=session_factory,
        checkpointer=DatabaseCheckpointer(session_factory),
    )
    run = await runtime.start(incident.id, background=False)
    runs = AgentRunRepository(db_session)
    stored = await runs.get(run.id)

    from opspilot_backend.models import Approval

    approval = (
        await db_session.execute(
            select(Approval).where(Approval.incident_id == incident.id)
        )
    ).scalars().first()
    assert approval is not None
    await runs.decide_approval(
        str(approval.id), decision="approve", decided_by="oncall@example.com"
    )
    await db_session.commit()

    await runtime.resume(run.id, {"decision": "approve"}, background=False)

    spans = await runs.spans_for_run(run.id)
    # Two legs of execution (before and after the approval gate)…
    run_spans = [s for s in spans if s.kind == "run"]
    assert len(run_spans) >= 2, [s.name for s in run_spans]
    assert any(s.attributes.get("resumed") for s in run_spans)
    # …but still exactly one trace.
    assert {s.trace_id for s in spans} == {stored.trace_id}

    # Spans from the resumed leg belong to the same trace as the first leg.
    step_spans = {
        s.span_id for s in await runs.spans_for_run(run.id) if s.kind == "node"
    }
    recovered = [s for s in spans if s.name.startswith("node.recovery")]
    assert recovered, "the recovery leg produced no node spans"
    assert all(s.span_id in step_spans for s in recovered)

    # The trace endpoint reports the run's own trace id, not a fresh one.
    assert stored.trace_id in {
        s.trace_id for s in await runs.spans_for_trace(stored.trace_id)
    }


async def test_span_collector_survives_task_boundaries() -> None:
    """The load-bearing invariant.

    LangGraph runs nodes in child tasks, which *copy* the context. A
    copy-on-write collector would silently drop every node span, so this pins
    the behaviour the run test depends on.
    """
    begin_span_collection()

    async def child() -> None:
        async with aspan("child.span", kind="node"):
            await asyncio.sleep(0)

    async def grandchild() -> None:
        await asyncio.create_task(child())

    await asyncio.create_task(grandchild())

    recorded = drain_spans()
    assert [s["name"] for s in recorded] == ["child.span"]
    assert recorded[0]["trace_id"]


async def test_child_task_spans_keep_their_parent() -> None:
    begin_span_collection()

    async def child() -> str:
        async with aspan("child.span", kind="node") as span:
            return span.span_id

    async with aspan("parent.span", kind="run") as parent:
        child_span_id = await asyncio.create_task(child())

    recorded = {s["name"]: s for s in drain_spans()}
    assert recorded["child.span"]["parent_span_id"] == parent.span_id
    assert recorded["parent.span"]["span_id"] == parent.span_id
    assert child_span_id != parent.span_id
