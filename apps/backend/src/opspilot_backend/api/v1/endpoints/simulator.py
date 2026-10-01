"""Simulator façade — inject faults, read the live environment, browse scenarios.

These routes used to be backed by a second, in-memory simulator that lived
inside the backend (``opspilot_backend/simulator_data.py``): its own
five-scenario catalogue, its own service cards whose metrics were derived from
``sha256(service_name)``, and its own set of "synthetic incidents" that only
ever existed in that module's dict. Nothing else could see any of it — the
Agent's tools talk to the real simulator over HTTP, and ``/incidents`` reads the
database — so pressing "inject fault" on the dashboard changed a variable and
produced no incident, while the service grid displayed invented numbers under
scenario names the evaluated path had never heard of.

The routes are now a thin façade over the same provider the tools use:

* reads are forwarded to the simulator, so what the dashboard shows is what the
  Agent will observe;
* ``inject`` breaks the environment *and* creates the incident row the dashboard
  and the Agent both read, so the demo path and the investigated path are the
  same path.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_backend.db.session import async_session
from opspilot_backend.domain.enums import IncidentSeverity, IncidentStatus
from opspilot_backend.infrastructure.container import get_providers
from opspilot_backend.infrastructure.http_client import HttpError
from opspilot_backend.models import Incident, Service
from opspilot_backend.repositories.incident import IncidentRepository

router = APIRouter(prefix="/simulator", tags=["simulator"])


def _infra() -> Any:
    return get_providers().extra["infra"]


async def _call(method: str, *args: Any, **kwargs: Any) -> Any:
    """Forward to the simulator, translating transport failures into 503.

    A simulator that is down is a dependency outage, not a bug in the caller's
    request; conflating the two would make the dashboard report a 500 and send
    an operator looking for a fault in the wrong place.
    """
    try:
        return await getattr(_infra(), method)(*args, **kwargs)
    except HttpError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"模拟器不可用：{exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

@router.get("/scenarios")
async def list_scenarios() -> list[dict]:
    """Every scenario the simulator can inject.

    Severity is passed through as the simulator's own ``SEV1``–``SEV4`` label
    rather than being translated here: the incident table stores the same
    vocabulary, so a translation step would only create a second place for the
    two to disagree.
    """
    scenarios = await _call("list_scenarios")
    return [
        {
            "name": s.get("name", ""),
            "title": s.get("title", ""),
            "description": s.get("description", ""),
            "service": s.get("alert_service", ""),
            "severity": s.get("severity", "SEV3"),
            "symptoms": list(s.get("symptoms", [])),
            "trigger": s.get("trigger"),
            "affected_services": list(s.get("affected_services", [])),
        }
        for s in scenarios
    ]


@router.get("/services")
async def list_services() -> list[dict]:
    """Live service cards from the simulator.

    Field names are adapted to ``ServiceRead`` because the simulator speaks
    metric units (``latency_p95_ms``, ``memory_mb``) and the dashboard speaks
    the API's own units. Only values the simulator actually reports are
    forwarded; a missing one stays ``None`` and renders as "—".
    """
    raw = await _call("list_services")
    return [_service_card(row) for row in raw]


@router.get("/logs")
async def query_logs(
    service: str,
    minutes: int = 30,
    level: str | None = None,
    limit: int = 200,
) -> list[dict]:
    payload = await _call("query_logs", service, level, minutes, limit)
    return list(payload.get("entries", []))


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------

@router.post("/incidents/{name}/inject")
async def inject_scenario(
    name: str,
    session: AsyncSession = Depends(async_session),
) -> dict:
    """Break the environment for ``name`` and open a real incident for it.

    Re-injecting a scenario that already has an open incident returns that
    incident instead of opening a second one: the operator's intent is "show me
    this fault", and two rows for one broken service is noise, not information.
    """
    try:
        scenario = next(
            s for s in await _call("list_scenarios") if s.get("name") == name
        )
    except StopIteration:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未知场景：{name!r}",
        ) from None

    result = await _call("inject", name)

    service_name = scenario.get("alert_service") or name
    service = (
        await session.execute(select(Service).where(Service.name == service_name))
    ).scalars().first()
    if service is None:
        session.add(
            Service(
                name=service_name,
                description=f"{service_name} (simulated)",
                tier="application",
                owner="opspilot-simulator",
            )
        )
        await session.flush()
        service = (
            await session.execute(select(Service).where(Service.name == service_name))
        ).scalars().first()

    existing = (
        await session.execute(
            select(Incident)
            .where(
                Incident.scenario == name,
                Incident.status.notin_(
                    [IncidentStatus.RESOLVED.value, IncidentStatus.CLOSED.value]
                ),
            )
            .order_by(Incident.detected_at.desc())
        )
    ).scalars().first()

    if existing is not None:
        incident = existing
        reused = True
    else:
        incident = await IncidentRepository(session).create(
            title=scenario.get("title") or name,
            service_id=service.id,
            severity=_as_severity(scenario.get("severity")),
            # The operator-visible description only. The scenario's expected
            # root cause must not travel through the Agent's own context load —
            # that would be handing it the answer.
            description=scenario.get("description") or name,
            scenario=name,
        )
        reused = False

    await session.commit()

    return {
        **result,
        "incident_id": str(incident.id),
        "service": service_name,
        "reused_incident": reused,
    }


@router.post("/incidents/{name}/reset")
async def reset_scenario(
    name: str,
    session: AsyncSession = Depends(async_session),
) -> dict:
    """Heal the environment for ``name`` without touching incident history."""
    try:
        result = await _call("reset", name)
    except HttpError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"场景 {name!r} 当前不处于激活状态：{exc}",
        ) from exc
    await session.commit()
    return result


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------

_SEVERITY_ALIASES = {
    "SEV1": IncidentSeverity.SEV1,
    "SEV2": IncidentSeverity.SEV2,
    "SEV3": IncidentSeverity.SEV3,
    "SEV4": IncidentSeverity.SEV4,
}


def _as_severity(raw: Any) -> str:
    """Normalise a scenario severity to the incident vocabulary.

    The incident column stores ``SEV1``–``SEV4``; anything unrecognised becomes
    ``SEV3`` rather than being stored verbatim, because a severity the rest of
    the system cannot rank is worse than a conservative default.
    """
    return _SEVERITY_ALIASES.get(str(raw).upper(), IncidentSeverity.SEV3).value


def _service_card(row: dict[str, Any]) -> dict[str, Any]:
    name = str(row.get("service", ""))
    memory_mb = row.get("memory_mb")
    memory_limit = row.get("memory_limit_mb")
    memory_pct: float | None = None
    if isinstance(memory_mb, int | float) and isinstance(memory_limit, int | float):
        if memory_limit:
            memory_pct = round(memory_mb / memory_limit * 100, 1)

    return {
        "id": name,
        "name": name,
        "health": row.get("health"),
        "error_rate": row.get("error_rate"),
        "latency_p95": row.get("latency_p95_ms"),
        "latency_p99": None,
        "cpu_usage": row.get("cpu_percent"),
        "memory_usage": memory_pct,
        "version": row.get("version"),
        "description": _describe(row),
    }


def _describe(row: dict[str, Any]) -> str:
    """One-line summary of what the service currently looks like."""
    parts: list[str] = []
    kind = row.get("kind")
    if kind:
        parts.append(str(kind))
    replicas = row.get("replicas")
    if replicas:
        parts.append(f"{replicas} 副本")
    faults = row.get("active_faults") or []
    if faults:
        parts.append("故障：" + "、".join(str(f) for f in faults))
    if row.get("circuit_breaker_open"):
        parts.append("熔断已打开")
    return " · ".join(parts)
