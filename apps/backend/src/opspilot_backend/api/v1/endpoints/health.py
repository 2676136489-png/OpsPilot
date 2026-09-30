"""Health check endpoint.

Reports the real state of the dependencies this service actually has. It
previously returned three hard-coded ``"pending"`` entries — including Redis
and MCP, neither of which this service uses — so the observability page showed
a system permanently stuck at "not ready" while cheerfully reporting ``ok``.
"""

from typing import Any

from fastapi import APIRouter
from sqlalchemy import text

from opspilot_backend.db.session import async_session_factory

router = APIRouter(tags=["health"])


async def _probe_database() -> tuple[bool, str]:
    """Run a trivial query against the store — the one real dependency."""

    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — the point is to report any failure
        return False, f"{type(exc).__name__}: {exc}"
    return True, "ok"


@router.get("/health")
async def health_check() -> dict[str, Any]:
    """Return service health, reflecting whether the store is reachable."""

    db_ok, detail = await _probe_database()
    return {
        "status": "ok" if db_ok else "degraded",
        "service": "opspilot-backend",
        "version": "0.1.0",
        "checks": {"database": "ok" if db_ok else "error"},
        # Only surface the reason when there is one — a healthy response should
        # not carry a misleading empty `detail` field.
        **({"detail": detail} if not db_ok else {}),
    }


@router.get("/ready")
async def readiness() -> dict[str, str]:
    """Liveness probe — simply confirms the process is up."""

    return {"status": "ready"}
