"""Shared pytest fixtures for the backend test suite.

Everything here points at one **in-memory SQLite database shared through
``StaticPool``**, so the agent runtime (which opens its own session, like a
real background task would), the checkpointer and the test itself all observe
the same rows. That is what makes the end-to-end test meaningful: it exercises
multi-connection behaviour instead of one convenient session.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import opspilot_backend.db.session  # noqa: F401 - installs the FK pragma listener
from opspilot_backend.infrastructure.container import Providers, reset_providers, set_providers
from opspilot_backend.infrastructure.providers import (
    FileRunbookProvider,
    GitHubAdapter,
    SimulatorInfraProvider,
)
from opspilot_backend.models import Base

# The simulator is a sibling project; tests drive it in-process over ASGI
# instead of spawning a server, so no port and no subprocess is needed.
SIMULATOR_SRC = Path(__file__).resolve().parents[3] / "simulator" / "src"
if str(SIMULATOR_SRC) not in sys.path:
    sys.path.insert(0, str(SIMULATOR_SRC))


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """pytest-asyncio requires a session-scoped event loop."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def engine(tmp_path):
    """A real file-backed SQLite database.

    A file (not ``:memory:`` + StaticPool) is deliberate: the agent runtime,
    the checkpointer and the test each open their own connection, exactly like
    production. StaticPool would force every session onto one DBAPI
    connection, where concurrent transactions are impossible.
    """
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'opspilot-test.db'}",
        echo=False,
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def simulator(request: pytest.FixtureRequest):
    """In-process simulator, optionally pre-loaded with a fault scenario.

    ``@pytest.mark.parametrize("simulator", ["bad_deployment"], indirect=True)``
    injects that scenario before the test body runs.
    """
    from opspilot_simulator.server import app as sim_app, get_simulator

    scenario = getattr(request, "param", None)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=sim_app),
        base_url="http://simulator.test",
    )
    state = get_simulator()
    state.reset()
    if scenario:
        state.inject(scenario)

    infra = SimulatorInfraProvider(client=client)
    set_providers(
        Providers(
            services=infra,
            metrics=infra,
            logs=infra,
            deployments=infra,
            runbooks=FileRunbookProvider(),
            github=GitHubAdapter(),
            extra={"infra": infra},
        )
    )
    try:
        yield state
    finally:
        state.reset()
        await client.aclose()
        reset_providers()
