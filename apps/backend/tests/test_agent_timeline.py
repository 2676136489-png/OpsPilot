"""The timeline API: what the dashboard is allowed to know, and when.

Before these endpoints existed the frontend had no way to read a run's real
progress, so it reconstructed one from ``current_node`` and ``updated_at`` and
stamped invented timestamps on it. Two things had to become true for that to
stop:

* the persisted steps, tool calls and events had to be *reachable* over HTTP,
  and
* the payloads had to be *bounded*, so that adding a field to an ``emit()``
  call inside the agent cannot silently widen the public API.

Both are asserted here, plus the streaming behaviour that makes a page refresh
harmless: replay from ``seq``, de-duplicate against the live bus, close when
the run ends.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select

from opspilot_backend.domain.enums import AgentRunStatus, ApprovalStatus, EventType
from opspilot_backend.models import ToolCall
from opspilot_backend.services.agent_run import AgentRunQueryService
from opspilot_backend.services.agent_stream import (
    EVENT_FIELDS,
    OTHER_LOG_EVENTS,
    STREAM_CLOSED,
    STREAM_OPENED,
    comment,
    event_stream,
    parse_last_event_id,
    project_event,
    project_payload,
    visible_data,
)
from opspilot_backend.services.events import EventBus

from .test_incident_lifecycle import SCENARIO, _make_incident



# ---------------------------------------------------------------------------
# Projection — the boundary
# ---------------------------------------------------------------------------


def test_every_event_type_is_accounted_for() -> None:
    """A new ``EventType`` must be declared, not silently withheld.

    ``EVENT_FIELDS`` is an allowlist, so an undeclared type loses its payload.
    That is the safe default, but it is also invisible: this test is what turns
    "someone added an event and forgot the projection" into a failure instead
    of a timeline row that quietly renders nothing.
    """
    undeclared = {
        member.value
        for member in EventType
        if member.value not in EVENT_FIELDS and member.value not in OTHER_LOG_EVENTS
    }
    assert not undeclared, (
        "declare these in EVENT_FIELDS (or OTHER_LOG_EVENTS if they belong to "
        f"the incident timeline): {sorted(undeclared)}"
    )


def test_no_event_type_has_an_empty_declaration_by_accident() -> None:
    """An empty set means "envelope only" — say so on purpose.

    ``frozenset()`` and "forgot to fill this in" look identical in a dict
    literal. The lifecycle events legitimately have nothing to add beyond the
    envelope; anything else claiming to expose fields must expose some.
    """
    envelope_only = {member.value for member in EventType if EVENT_FIELDS.get(member.value) == frozenset()}
    assert envelope_only, "expected at least the lifecycle events to be envelope-only"
    for event_type in envelope_only:
        assert event_type.startswith(("agent.", "heartbeat", "state.")), event_type


def test_undeclared_payload_keys_are_dropped() -> None:
    kept = visible_data(
        "evidence.created",
        {"ref": "E001", "title": "Error rate 0.29", "internal_debug": {"stack": "..."}},
    )
    assert kept == {"ref": "E001", "title": "Error rate 0.29"}


def test_undeclared_event_type_withholds_its_payload_entirely() -> None:
    assert visible_data("something.brand_new", {"secret": "value"}) == {}


def test_non_dict_payload_does_not_explode() -> None:
    assert visible_data("evidence.created", None) == {}
    assert visible_data("evidence.created", ["not", "a", "dict"]) == {}


def test_projection_table_does_not_reference_unknown_event_types() -> None:
    known = {member.value for member in EventType}
    assert set(EVENT_FIELDS) <= known, sorted(set(EVENT_FIELDS) - known)


def test_bus_payload_and_row_projection_agree() -> None:
    """Two transports, one shape — otherwise SSE and REST would disagree."""

    class Row:
        seq = 7
        event_id = "abc"
        run_id = "11111111-1111-1111-1111-111111111111"
        incident_id = None
        event_type = "evidence.created"
        stage = "parallel_investigation"
        data = {"ref": "E002", "title": "t", "internal_debug": 1}
        created_at = None

    from_row = project_event(Row())
    from_bus = project_payload(
        {
            "seq": 7,
            "event_id": "abc",
            "run_id": "11111111-1111-1111-1111-111111111111",
            "incident_id": None,
            "event_type": "evidence.created",
            "stage": "parallel_investigation",
            "data": {"ref": "E002", "title": "t", "internal_debug": 1},
            "created_at": None,
        }
    )
    assert from_row == from_bus
    assert "internal_debug" not in from_row["data"]


# ---------------------------------------------------------------------------
# SSE framing
# ---------------------------------------------------------------------------


def test_parse_last_event_id_treats_junk_as_the_beginning() -> None:
    """Replaying is idempotent; skipping is not. Default to replaying."""
    assert parse_last_event_id("42") == 42
    assert parse_last_event_id(42) == 42
    assert parse_last_event_id(" 7 ") == 7
    assert parse_last_event_id(None) == 0
    assert parse_last_event_id("") == 0
    assert parse_last_event_id("not-a-number") == 0
    assert parse_last_event_id("-5") == 0
    assert parse_last_event_id("0") == 0


def test_heartbeat_is_an_sse_comment() -> None:
    assert comment("keepalive") == ": keepalive\n\n"


def _parse_frames(chunks: list[str]) -> list[tuple[str, dict | None]]:
    """SSE text → ``(event_name, payload)`` pairs. Comments are skipped."""
    parsed: list[tuple[str, dict | None]] = []
    for chunk in chunks:
        name = ""
        payload: dict | None = None
        for line in chunk.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                payload = json.loads(line[len("data: ") :])
        if name:
            parsed.append((name, payload))
    return parsed


async def test_stream_replays_then_closes_on_a_terminal_run() -> None:
    """A finished run streams its whole log and then hangs up."""
    frames = [
        {
            "seq": 1,
            "event_id": "a",
            "run_id": "run-1",
            "incident_id": "",
            "event_type": "agent.started",
            "stage": None,
            "data": {},
            "created_at": None,
        },
        {
            "seq": 2,
            "event_id": "b",
            "run_id": "run-1",
            "incident_id": "",
            "event_type": "evidence.created",
            "stage": "parallel_investigation",
            "data": {"ref": "E001"},
            "created_at": None,
        },
    ]

    async def fetch_events(after: int):
        return [f for f in frames if f["seq"] > after]

    async def fetch_status() -> str:
        return AgentRunStatus.COMPLETED.value

    bus = EventBus()
    chunks = [
        chunk
        async for chunk in event_stream(
            run_id="run-1", bus=bus, fetch_events=fetch_events,
            fetch_status=fetch_status, poll_interval=0.01,
        )
    ]
    parsed = _parse_frames(chunks)

    assert parsed[0][0] == STREAM_OPENED
    assert [name for name, _ in parsed[1:3]] == ["agent.started", "evidence.created"]
    assert parsed[-1][0] == STREAM_CLOSED
    assert parsed[-1][1]["reason"] == "terminal"
    assert parsed[-1][1]["status"] == "completed"
    # The subscriber must be released, or every finished stream leaks a queue.
    assert bus.subscriber_count("run-1") == 0


async def test_control_frames_are_valid_events_too() -> None:
    """The stream's own frames must satisfy the client's event shape.

    Both sides of this wire model ``stream.opened`` / ``stream.closed`` as
    events — the client's ``EVENT_META`` labels them and ``describeEvent``
    writes copy for them — but they are not *domain* events: nothing is
    persisted, so they carry no ``seq``. They used to send a bare payload
    (``{"run_id", "status", "last_event_id"}``), which a client trusting its
    declared ``AgentEvent`` type reads as ``event_type: undefined`` and
    ``data: undefined``. Rendering one then threw
    ``TypeError: Cannot convert undefined or null to object`` and took the whole
    timeline page down — the crash appeared only on the events tab, and only
    because a control frame was the first row in it.

    A frame is the producer's contract, so the contract is asserted here rather
    than defended against on every consumer.
    """

    async def fetch_events(after: int):
        return []

    async def fetch_status() -> str:
        return AgentRunStatus.COMPLETED.value

    chunks = [
        chunk
        async for chunk in event_stream(
            run_id="run-1", bus=EventBus(), fetch_events=fetch_events,
            fetch_status=fetch_status, poll_interval=0.01,
        )
    ]
    parsed = _parse_frames(chunks)

    control = {name: payload for name, payload in parsed if name in {STREAM_OPENED, STREAM_CLOSED}}
    assert set(control) == {STREAM_OPENED, STREAM_CLOSED}

    for name, payload in control.items():
        assert payload is not None, name
        # Every key the client reads without a guard.
        assert payload["event_type"] == name
        assert isinstance(payload["data"], dict), (
            f"{name} must carry an object payload: the client renders every "
            f"frame through Object.entries(evt.data)"
        )
        # No cursor: replaying a frame that was never stored would duplicate it.
        assert payload["seq"] == 0

    # The keys callers already read off the top level stay there.
    assert control[STREAM_CLOSED]["reason"] == "terminal"
    assert control[STREAM_CLOSED]["data"]["reason"] == "terminal"
    assert control[STREAM_OPENED]["last_event_id"] == 0
    assert control[STREAM_OPENED]["data"]["last_event_id"] == 0


async def test_stream_resumes_after_last_event_id() -> None:
    """Reconnecting must not repeat what the client already saw."""
    seen: list[int] = []

    async def fetch_events(after: int):
        for seq in (1, 2, 3):
            if seq > after:
                seen.append(seq)
        return [
            {
                "seq": seq,
                "event_id": str(seq),
                "run_id": "run-1",
                "incident_id": "",
                "event_type": "evidence.created",
                "stage": None,
                "data": {},
                "created_at": None,
            }
            for seq in (1, 2, 3)
            if seq > after
        ]

    async def fetch_status() -> str:
        return AgentRunStatus.COMPLETED.value

    chunks = [
        chunk
        async for chunk in event_stream(
            run_id="run-1", bus=EventBus(), fetch_events=fetch_events,
            fetch_status=fetch_status, after_seq=2, poll_interval=0.01,
        )
    ]
    parsed = _parse_frames(chunks)

    assert parsed[0][1]["last_event_id"] == 2
    # Everything after the cursor, and only that: no repeat of seq 1 or 2.
    assert [name for name, _ in parsed[1:]] == ["evidence.created", STREAM_CLOSED]
    assert [payload["seq"] for name, payload in parsed if name == "evidence.created"] == [3]
    assert 1 not in seen and 2 not in seen, "must not re-request events already seen"



async def test_stream_delivers_live_frames_without_duplicating() -> None:
    """A frame on the bus arrives once, even though the table also holds it."""
    row = {
        "seq": 9,
        "event_id": "nine",
        "run_id": "run-1",
        "incident_id": "",
        "event_type": "tool.started",
        "stage": None,
        "data": {"tool_name": "query_metrics", "arguments": {"service": "checkout"}},
        "created_at": None,
    }

    async def fetch_events(after: int):
        # The row is committed as soon as the bus carries it, so both paths
        # can see it — that is exactly the duplicate this must absorb.
        return [row] if row["seq"] > after else []

    async def fetch_status() -> str:
        return AgentRunStatus.RUNNING.value

    bus = EventBus()
    stream = event_stream(
        run_id="run-1", bus=bus, fetch_events=fetch_events,
        fetch_status=fetch_status, poll_interval=0.01, max_idle_seconds=0.2,
    )
    collected: list[str] = []
    # First frame is ``stream.opened``, which also means we are subscribed.
    collected.append(await stream.__anext__())
    await bus.publish("run-1", row)
    # Drain until the stream closes itself on the idle bound.
    async for chunk in stream:
        collected.append(chunk)

    parsed = _parse_frames(collected)
    names = [name for name, _ in parsed]
    assert names.count("tool.started") == 1, names
    assert parsed[-1][0] == STREAM_CLOSED
    assert parsed[-1][1]["reason"] == "idle_timeout"
    # Live frames go through the projection too, not just replayed ones.
    tool_frame = next(payload for name, payload in parsed if name == "tool.started")
    assert tool_frame["data"] == {"tool_name": "query_metrics", "arguments": {"service": "checkout"}}


async def test_stream_drops_undeclared_fields_on_the_live_path() -> None:
    row = {
        "seq": 3,
        "event_id": "three",
        "run_id": "run-1",
        "incident_id": "",
        "event_type": "evidence.created",
        "stage": None,
        "data": {"ref": "E003", "internal_debug": "should not ship"},
        "created_at": None,
    }

    async def fetch_events(after: int):
        return []

    async def fetch_status() -> str:
        return AgentRunStatus.RUNNING.value

    bus = EventBus()
    stream = event_stream(
        run_id="run-1", bus=bus, fetch_events=fetch_events,
        fetch_status=fetch_status, poll_interval=0.01, max_idle_seconds=0.2,
    )
    collected = [await stream.__anext__()]
    await bus.publish("run-1", row)
    async for chunk in stream:
        collected.append(chunk)

    parsed = _parse_frames(collected)
    frame = next(payload for name, payload in parsed if name == "evidence.created")
    assert frame["data"] == {"ref": "E003"}


async def test_stream_releases_its_subscription_when_cancelled() -> None:
    """Closing the browser tab must not leave a queue behind forever."""

    async def fetch_events(after: int):
        return []

    async def fetch_status() -> str:
        return AgentRunStatus.RUNNING.value

    bus = EventBus()
    stream = event_stream(
        run_id="run-1", bus=bus, fetch_events=fetch_events,
        fetch_status=fetch_status, poll_interval=5.0, max_idle_seconds=60.0,
    )
    assert await stream.__anext__()  # subscribe happened
    assert bus.subscriber_count("run-1") == 1
    await stream.aclose()
    assert bus.subscriber_count("run-1") == 0


# ---------------------------------------------------------------------------
# End to end — the readers against a real run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("simulator", [SCENARIO], indirect=True)
async def test_run_telemetry_and_timeline_come_from_real_rows(
    db_session, session_factory, simulator
) -> None:
    """Drive a real investigation, then read it back the way the UI does."""
    from opspilot_backend.agent.checkpointer import DatabaseCheckpointer
    from opspilot_backend.services.agent_runtime import AgentRuntimeService

    incident = await _make_incident(db_session)
    runtime = AgentRuntimeService(
        db_session,
        session_factory=session_factory,
        checkpointer=DatabaseCheckpointer(session_factory),
    )
    run = await runtime.start(incident.id, background=False)
    run_id = str(run.id)

    # Every read goes through a *fresh* session, which is what an HTTP request
    # does. Reusing the test's long-lived session would hand back the identity
    # map's copy of the run — the one this test created — and happily report
    # ``pending`` while the run had long since parked at the approval gate.
    @asynccontextmanager
    async def read():
        async with session_factory() as session:
            yield AgentRunQueryService(session)

    # --- the timeline the UI renders ---------------------------------
    async with read() as query:
        timeline = await query.timeline(run_id)
    assert timeline is not None
    steps = timeline["steps"]
    assert len(steps) >= 10, len(steps)
    sequences = [s["sequence"] for s in steps]
    assert sequences == sorted(sequences), sequences
    assert all(s["stage"] for s in steps)
    assert all(s["duration_ms"] is not None for s in steps if s["status"] == "completed")
    # No call may be unattributable: the whole point of the step link is that
    # "which node asked for this metric" has an answer.
    assert timeline["orphaned_tool_calls"] == []

    nested_calls = [c for s in steps for c in s["tool_calls"]]
    assert nested_calls, "tool calls must be attached to the step that made them"
    assert {c["tool_name"] for c in nested_calls} >= {"get_service_status", "query_metrics"}
    # Every call carries the audit fields the UI shows.
    for call in nested_calls:
        assert call["id"] and call["status"] and call["created_at"]
        assert isinstance(call["arguments"], dict)
    # Grouping must be a partition of the stored calls, not a subset: a call
    # that vanished from the timeline is worse than one shown in the wrong row.
    stored = await db_session.scalar(
        select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run.id)
    )
    assert len(nested_calls) == stored, (len(nested_calls), stored)

    # --- the persisted event log, projected ---------------------------
    async with read() as query:
        events = await query.events(run_id)
    assert events is not None and len(events) >= 10
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
    assert events[0]["event_type"] == "agent.started"
    assert {"evidence.created", "approval.required"} <= {e["event_type"] for e in events}

    # A cursor must actually page, and must never repeat a frame.
    midpoint = events[len(events) // 2]["seq"]
    async with read() as query:
        tail = await query.events(run_id, after_seq=midpoint)
    assert tail is not None
    assert all(e["seq"] > midpoint for e in tail)
    assert len(tail) < len(events)

    # Payloads are bounded: nothing outside the declaration table leaks.
    for event in events:
        allowed = EVENT_FIELDS.get(event["event_type"])
        assert allowed is not None, event["event_type"]
        assert set(event["data"]) <= allowed, (event["event_type"], set(event["data"]) - allowed)

    # --- the run contract ---------------------------------------------
    async with read() as query:
        payload = await query.get_run(run_id)
    assert payload is not None
    assert payload["status"] == "awaiting_approval"
    assert payload["current_node"] == "human_approval"

    # Usage and budget were persisted all along; they are now reported.
    usage, budget = payload["usage"], payload["budget"]
    assert usage["tool_calls"] > 0
    assert usage["tool_calls"] <= budget["tool_calls"]
    assert usage["duration_ms"] is not None and usage["duration_ms"] > 0
    assert budget["tool_calls"] > 0 and budget["seconds"] > 0
    assert budget["exhausted"] is False
    assert payload["attempt"] == 1 and payload["max_attempts"] >= 1
    assert payload["trace_id"] and payload["request_id"]

    # A parked run has no verdict yet — ``None``, not a fabricated empty one.
    assert payload["final_result"] is None

    # The root cause now cites the evidence it rests on.
    root_cause = payload["root_cause"]
    assert root_cause is not None
    assert root_cause["category"] == "deployment"
    assert root_cause["evidence_ids"], "diagnosis must cite the evidence it used"
    assert root_cause["reasoning_summary"], "the recorded reasoning is not empty"
    cited = {e["id"] for e in payload["evidence"]}
    assert set(root_cause["evidence_ids"]) <= cited, root_cause["evidence_ids"]

    # The plan exposes the risk model the approval depends on.
    plan = payload["recovery_plan"]
    assert plan is not None
    assert plan["status"] == "pending_approval"
    assert plan["risk_level"] == "CRITICAL"
    first = plan["steps"][0]
    assert first["action"] == "rollback_deployment"
    assert first["risk_level"] and first["target"]
    assert first["approval_tier"] and first["verification_strategy"]
    assert first["effective"] is None, "an action that has not run is not 'ineffective'"

    # ------------------------------------------------------------------
    # Approve, finish, and re-read: the verdict appears, and the timeline
    # is complete without a single client-side inference.
    # ------------------------------------------------------------------
    from opspilot_backend.repositories.agent_run import AgentRunRepository

    approval_id = payload["approval_required"]["id"]
    decided = await AgentRunRepository(db_session).decide_approval(
        approval_id, decision="approve", decided_by="tester", note="ok"
    )
    assert decided["status"] == ApprovalStatus.APPROVED.value
    await db_session.commit()
    await runtime.resume(run.id, {"decision": "approve"}, background=False)

    async with read() as query:
        done = await query.get_run(run_id)
    assert done is not None
    assert done["status"] == "completed"
    final = done["final_result"]
    assert final is not None
    assert final["status"] == "completed"
    assert final["root_cause"] and final["category"] == "deployment"
    assert final["recovery_status"] == "executed"
    assert final["verification_status"] == "passed"
    assert final["error"] is None

    # After resume the timeline grew rather than restarted: sequences are
    # still strictly increasing and the earlier steps are still there.
    async with read() as query:
        grown = await query.timeline(run_id)
        after_events = await query.events(run_id)
    assert grown is not None
    assert len(grown["steps"]) > len(steps)
    assert [s["sequence"] for s in grown["steps"]] == sorted(
        s["sequence"] for s in grown["steps"]
    )
    # The resumed leg's calls are attributed too, so its steps are not empty.
    assert grown["orphaned_tool_calls"] == []

    assert after_events is not None
    assert len(after_events) > len(events)
    assert after_events[-1]["event_type"] == "agent.completed"


@pytest.mark.parametrize("simulator", [SCENARIO], indirect=True)
async def test_unknown_run_is_not_an_empty_timeline(
    db_session, session_factory, simulator
) -> None:
    """``404`` and ``[]`` are different answers and must stay different.

    ``None`` (unknown run) versus an empty timeline (known run, no steps yet)
    is the whole reason these readers return an optional result.
    """
    query = AgentRunQueryService(db_session)
    missing = "3f1c2b4a-0000-0000-0000-000000000000"
    assert await query.timeline(missing) is None
    assert await query.events(missing) is None
    assert await query.timeline("not-a-uuid") is None
    assert await query.get_run(missing) is None


@pytest.mark.parametrize("simulator", [SCENARIO], indirect=True)
async def test_orphaned_tool_call_is_reported_not_dropped(
    db_session, session_factory, simulator
) -> None:
    """A tool call whose step row is gone still happened."""
    from opspilot_backend.agent.checkpointer import DatabaseCheckpointer
    from opspilot_backend.services.agent_runtime import AgentRuntimeService

    incident = await _make_incident(db_session)
    runtime = AgentRuntimeService(
        db_session,
        session_factory=session_factory,
        checkpointer=DatabaseCheckpointer(session_factory),
    )
    run = await runtime.start(incident.id, background=False)

    # Detach one call from its step, exactly as ``ON DELETE SET NULL`` would.
    call = (
        await db_session.execute(
            select(ToolCall).where(ToolCall.run_id == run.id).limit(1)
        )
    ).scalar_one()
    assert call.step_id is not None, (
        "tool calls must be attributed to the node that made them — a NULL "
        "step_id here means NodeContext stopped passing it through"
    )
    call.step_id = None
    await db_session.commit()

    timeline = await AgentRunQueryService(db_session).timeline(str(run.id))
    assert timeline is not None
    orphans = timeline["orphaned_tool_calls"]
    assert len(orphans) == 1, orphans
    assert orphans[0]["id"] == str(call.id)
    # It must not double-count: the call moved buckets, it did not multiply.
    total = sum(len(s["tool_calls"]) for s in timeline["steps"]) + len(orphans)
    stored = await db_session.scalar(
        select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run.id)
    )
    assert total == stored


async def test_stream_polls_state_not_the_request_session() -> None:
    """The stream must observe status changes made after it opened.

    A stream that captured the run's status once would report a finished
    investigation as still running, and one holding the request's session open
    would block the run's own commits on SQLite.
    """
    states = iter([AgentRunStatus.RUNNING.value, AgentRunStatus.RUNNING.value,
                   AgentRunStatus.COMPLETED.value, AgentRunStatus.COMPLETED.value])
    calls = {"n": 0}

    async def fetch_events(after: int):
        return []

    async def fetch_status() -> str:
        calls["n"] += 1
        return next(states, AgentRunStatus.COMPLETED.value)

    chunks = [
        chunk
        async for chunk in event_stream(
            run_id="run-1", bus=EventBus(), fetch_events=fetch_events,
            fetch_status=fetch_status, poll_interval=0.01,
        )
    ]
    parsed = _parse_frames(chunks)
    assert parsed[-1][0] == STREAM_CLOSED
    assert parsed[-1][1]["status"] == "completed"
    assert calls["n"] >= 2, "status must be re-read, not cached"


async def test_stream_stays_open_while_the_run_is_live() -> None:
    """A running investigation must not be closed early."""

    async def fetch_events(after: int):
        return []

    async def fetch_status() -> str:
        return AgentRunStatus.RUNNING.value

    chunks = [
        chunk
        async for chunk in event_stream(
            run_id="run-1", bus=EventBus(), fetch_events=fetch_events,
            fetch_status=fetch_status, poll_interval=0.01, max_idle_seconds=0.15,
        )
    ]
    parsed = _parse_frames(chunks)
    assert parsed[-1][1]["reason"] == "idle_timeout", parsed[-1]


# ---------------------------------------------------------------------------
# Finding an existing run — the entry point the dashboard needed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("simulator", [SCENARIO], indirect=True)
async def test_incident_run_is_findable_before_and_after_starting(
    db_session, session_factory, simulator
) -> None:
    """A page load must be able to *discover* the run, not create one.

    ``None`` before anything ran is the signal to offer "start"; a run after
    that is the signal to watch it. Collapsing those two into "always POST"
    is what made every refresh either conflict with the live run or spawn a
    second one.
    """
    from opspilot_backend.agent.checkpointer import DatabaseCheckpointer
    from opspilot_backend.services.agent_runtime import AgentRuntimeService

    incident = await _make_incident(db_session)
    async with session_factory() as session:
        assert await AgentRunQueryService(session).run_for_incident(str(incident.id)) is None

    runtime = AgentRuntimeService(
        db_session,
        session_factory=session_factory,
        checkpointer=DatabaseCheckpointer(session_factory),
    )
    run = await runtime.start(incident.id, background=False)

    async with session_factory() as session:
        found = await AgentRunQueryService(session).run_for_incident(str(incident.id))
    assert found is not None
    assert found["id"] == str(run.id)
    # It is the full contract, not a stub — the page renders straight from it.
    assert found["status"] == "awaiting_approval"
    assert found["usage"]["tool_calls"] > 0
    assert found["root_cause"]["evidence_ids"]

    async with session_factory() as session:
        assert await AgentRunQueryService(session).run_for_incident(
            "3f1c2b4a-0000-0000-0000-000000000000"
        ) is None
        assert await AgentRunQueryService(session).run_for_incident("nope") is None


@pytest.mark.parametrize("simulator", [SCENARIO], indirect=True)
async def test_an_active_run_outranks_a_newer_finished_one(
    db_session, session_factory, simulator
) -> None:
    """Re-investigating must not hide the investigation that is still running."""
    from opspilot_backend.agent.checkpointer import DatabaseCheckpointer
    from opspilot_backend.repositories.agent_run import AgentRunRepository
    from opspilot_backend.services.agent_runtime import AgentRuntimeService

    incident = await _make_incident(db_session)
    runtime = AgentRuntimeService(
        db_session,
        session_factory=session_factory,
        checkpointer=DatabaseCheckpointer(session_factory),
    )
    oldest = await runtime.start(incident.id, background=False)

    # A newer run that already finished: without the active-first rule this is
    # what the dashboard would show, and the live one would be invisible.
    newest = AgentRunRepository(db_session)
    finished = await newest.create_run(
        incident_id=incident.id, thread_id="thread-finished", reasoning_mode="deterministic"
    )
    await newest.update_run(str(finished.id), status=AgentRunStatus.COMPLETED.value)
    await db_session.commit()

    async with session_factory() as session:
        found = await AgentRunQueryService(session).run_for_incident(str(incident.id))
    assert found is not None
    assert found["id"] == str(oldest.id), "the running investigation must win"
    assert found["status"] == "awaiting_approval"

    # With nothing in flight, the newest one is the right answer.
    await newest.update_run(str(oldest.id), status=AgentRunStatus.COMPLETED.value)
    await db_session.commit()
    async with session_factory() as session:
        found = await AgentRunQueryService(session).run_for_incident(str(incident.id))
    assert found is not None
    assert found["id"] == str(finished.id)
    assert found["status"] == "completed"


@pytest.mark.parametrize("simulator", [SCENARIO], indirect=True)
async def test_incident_timeline_serialisation_carries_what_the_ui_shows(
    db_session, session_factory, simulator
) -> None:
    """The serializer is the contract; assert it field by field.

    The endpoint builds its payload by hand from model columns, so a renamed
    attribute would break the dashboard at runtime rather than at import.
    """
    from opspilot_backend.agent.checkpointer import DatabaseCheckpointer
    from opspilot_backend.repositories.incident import IncidentRepository
    from opspilot_backend.services.agent_runtime import AgentRuntimeService

    incident = await _make_incident(db_session)
    runtime = AgentRuntimeService(
        db_session,
        session_factory=session_factory,
        checkpointer=DatabaseCheckpointer(session_factory),
    )
    await runtime.start(incident.id, background=False)

    rows = await IncidentRepository(db_session).timeline(incident.id)
    assert rows
    kinds = {row.event_type for row in rows}
    assert {"incident.created", "diagnosis.completed"} <= kinds

    # Mirror the controller's serialisation exactly.
    serialised = [
        {
            "id": str(row.id),
            "event_type": row.event_type,
            "summary": row.summary,
            "actor": row.actor,
            "actor_type": getattr(row.actor_type, "value", str(row.actor_type)),
            "from_status": row.from_status,
            "to_status": row.to_status,
            "data": row.data or {},
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]
    for entry in serialised:
        assert entry["id"] and entry["event_type"] and entry["created_at"]
        assert isinstance(entry["data"], dict)
        assert isinstance(entry["summary"], str)
    # Order is the whole point of a timeline.
    assert [e["created_at"] for e in serialised] == sorted(
        e["created_at"] for e in serialised
    )
