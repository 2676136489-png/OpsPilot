"""Startup reconciliation: a run this process did not start is not running.

The database outlives the process and an agent run does not, so a redeploy
leaves rows claiming progress that can never arrive. What makes that worth a
test file rather than a log line is how it compounds: the dashboard shows an
investigation permanently under way, the client keeps polling because the row
is not terminal, and the stream for it can only replay and never advance — so
the page settles into a reconnect loop with no exit. That is the state an
operator reported as "怎么又要重连".

The one case that must *not* be swept is ``waiting_approval``: it is parked at
a LangGraph interrupt whose checkpoint is in this same database, so approving
it after a restart resumes real work. Sweeping it would throw that away.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

import opspilot_backend.db.session as db_session
from opspilot_backend.domain.enums import (
    ALLOWED_TRANSITIONS,
    AgentRunStatus,
    EventType,
    IncidentStatus,
)
from opspilot_backend.main import _reconcile_orphaned_runs
from opspilot_backend.models import AgentRun
from opspilot_backend.repositories.agent_run import AgentRunRepository

from .test_incident_lifecycle import _make_incident


@pytest.fixture
def wired(monkeypatch, session_factory):
    """Point the startup hook at the test database.

    ``_reconcile_orphaned_runs`` imports ``async_session_factory`` inside its
    body precisely so this works: a lifespan helper has no session to be handed
    and no app to override on, so the module attribute is the only seam — and
    reading it at call time is what makes that seam effective.
    """
    monkeypatch.setattr(db_session, "async_session_factory", session_factory)
    return session_factory


async def _add_run(session_factory, incident, status: AgentRunStatus, thread_id: str) -> None:
    async with session_factory() as session:
        session.add(
            AgentRun(
                incident_id=incident.id,
                status=status,
                thread_id=thread_id,
            )
        )
        await session.commit()


async def _runs(session_factory) -> list[AgentRun]:
    async with session_factory() as session:
        return list((await session.execute(select(AgentRun))).scalars().all())


async def _events_for(session_factory, run_id) -> list[str]:
    async with session_factory() as session:
        return [e.event_type for e in await AgentRunRepository(session).events(run_id)]


async def test_pending_and_running_runs_are_terminated(wired, db_session) -> None:
    """Both in-flight states end, with a reason a person can act on."""
    incident = await _make_incident(db_session)
    await _add_run(wired, incident, AgentRunStatus.PENDING, "t-pending")
    await _add_run(wired, incident, AgentRunStatus.RUNNING, "t-running")

    assert await _reconcile_orphaned_runs() == "2 orphaned run(s) terminated"

    runs = await _runs(wired)
    assert {run.status for run in runs} == {AgentRunStatus.FAILED}
    for run in runs:
        assert run.ended_at is not None, "a failed run must not look open-ended"
        assert run.error and "服务重启" in run.error
        # The timeline reads these, so the page says why instead of just
        # stopping. `agent.failed` is the same event the crash path emits.
        assert EventType.AGENT_FAILED.value in await _events_for(wired, run.id)


async def test_waiting_approval_survives_a_restart(wired, db_session) -> None:
    """Parked at an interrupt is not the same as abandoned.

    Its checkpoint is in the database, so a human approving after the restart
    resumes the run for real. Terminating it here would destroy recoverable
    work and turn a resumable investigation into a dead one.
    """
    incident = await _make_incident(db_session)
    await _add_run(wired, incident, AgentRunStatus.WAITING_APPROVAL, "t-parked")

    assert await _reconcile_orphaned_runs() == ""

    runs = await _runs(wired)
    assert [run.status for run in runs] == [AgentRunStatus.WAITING_APPROVAL]
    assert await _events_for(wired, runs[0].id) == []


async def test_terminal_runs_are_left_alone(wired, db_session) -> None:
    incident = await _make_incident(db_session)
    await _add_run(wired, incident, AgentRunStatus.COMPLETED, "t-done")
    await _add_run(wired, incident, AgentRunStatus.CANCELLED, "t-cancelled")

    assert await _reconcile_orphaned_runs() == ""

    assert {run.status for run in await _runs(wired)} == {
        AgentRunStatus.COMPLETED,
        AgentRunStatus.CANCELLED,
    }


async def test_reconcile_is_idempotent(wired, db_session) -> None:
    """Two boots in a row must not append a second failure each."""
    incident = await _make_incident(db_session)
    await _add_run(wired, incident, AgentRunStatus.RUNNING, "t-running")

    assert await _reconcile_orphaned_runs() == "1 orphaned run(s) terminated"
    assert await _reconcile_orphaned_runs() == ""

    runs = await _runs(wired)
    assert [e for e in await _events_for(wired, runs[0].id)].count(
        EventType.AGENT_FAILED.value
    ) == 1


async def test_incident_stops_claiming_to_be_in_progress(wired, db_session) -> None:
    """The list card is the incident's status, so fixing only the run is half a fix."""
    incident = await _make_incident(db_session)
    await _add_run(wired, incident, AgentRunStatus.RUNNING, "t-running")

    await _reconcile_orphaned_runs()

    async with wired() as session:
        from opspilot_backend.models import Incident

        refreshed = await session.get(Incident, incident.id)
        assert refreshed is not None
        assert refreshed.status == IncidentStatus.FAILED.value


def test_a_failed_reinvestigation_is_still_possible() -> None:
    """Marking an incident FAILED must not be a one-way door.

    The reconciliation writes FAILED on the operator's behalf, so it has to
    leave the re-run button working. If someone ever removes this edge, the
    startup hook becomes a way to permanently strand incidents — which is
    exactly the class of bug it exists to fix.
    """
    assert IncidentStatus.INVESTIGATING in ALLOWED_TRANSITIONS[IncidentStatus.FAILED]


async def test_bookkeeping_failure_never_blocks_startup(monkeypatch) -> None:
    """A broken database is already being reported elsewhere; do not add a crash.

    Failing to boot has no recovery path, while a stale "running" row merely
    stays wrong until the next restart.
    """

    class Explodes:
        def __call__(self):  # noqa: ANN204 - deliberately raises
            raise RuntimeError("database is gone")

    monkeypatch.setattr(db_session, "async_session_factory", Explodes())

    result = await _reconcile_orphaned_runs()
    assert result.startswith("reconcile failed:")
    assert "database is gone" in result
